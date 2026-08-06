#!/usr/bin/env python3
"""Generate installers/install_freebuff2api.sh — a self-contained unified installer.

The output script embeds every runtime file (freebuff2api package, admin panel,
tool/, deploy/ units, scripts/) as base64 payloads, mirroring the
install_owl_agent.sh pattern used in kiro-proxy-ecosystem. Run from the repo
root:

    python3 scripts/build_installer.py [--output installers/install_freebuff2api.sh]

The installer supports: install / update / uninstall / doctor / status.
"""

from __future__ import annotations

import argparse
import base64
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# (source path relative to repo root, dest path relative to F2A_HOME, mode)
MANIFEST: list[tuple[str, str, int]] = [
    # ── core package ───────────────────────────────────────────────────────
    ("freebuff2api/__init__.py", "repo/freebuff2api/__init__.py", 0o644),
    ("freebuff2api/app.py", "repo/freebuff2api/app.py", 0o644),
    ("freebuff2api/cli_prompt.py", "repo/freebuff2api/cli_prompt.py", 0o644),
    ("freebuff2api/codebuff.py", "repo/freebuff2api/codebuff.py", 0o644),
    ("freebuff2api/config.py", "repo/freebuff2api/config.py", 0o644),
    ("freebuff2api/logging_config.py", "repo/freebuff2api/logging_config.py", 0o644),
    ("freebuff2api/metrics.py", "repo/freebuff2api/metrics.py", 0o644),
    ("freebuff2api/models.py", "repo/freebuff2api/models.py", 0o644),
    ("freebuff2api/openai_compat.py", "repo/freebuff2api/openai_compat.py", 0o644),
    ("freebuff2api/sse.py", "repo/freebuff2api/sse.py", 0o644),
    # ── entrypoints / packaging ────────────────────────────────────────────
    ("main.py", "repo/main.py", 0o644),
    ("pyproject.toml", "repo/pyproject.toml", 0o644),
    (".env.example", "repo/.env.example", 0o644),
    # ── systemd units + journald drop-in ───────────────────────────────────
    ("deploy/freebuff2api.service", "repo/deploy/freebuff2api.service", 0o644),
    ("deploy/freebuff2api-admin.service", "repo/deploy/freebuff2api-admin.service", 0o644),
    ("deploy/backup-freebuff2api.service", "repo/deploy/backup-freebuff2api.service", 0o644),
    ("deploy/backup-freebuff2api.timer", "repo/deploy/backup-freebuff2api.timer", 0o644),
    ("deploy/journald-freebuff2api.conf", "repo/deploy/journald-freebuff2api.conf", 0o644),
    # ── ops scripts ────────────────────────────────────────────────────────
    ("scripts/doctor.sh", "repo/scripts/doctor.sh", 0o755),
    ("scripts/backup-freebuff2api.sh", "repo/scripts/backup-freebuff2api.sh", 0o755),
    ("scripts/backup-freebuff2api.sh", "backup.sh", 0o750),
    # ── token tool ─────────────────────────────────────────────────────────
    ("tool/get_token.py", "repo/tool/get_token.py", 0o644),
    ("tool/web/main.py", "repo/tool/web/main.py", 0o644),
    ("tool/web/templates/index.html", "repo/tool/web/templates/index.html", 0o644),
    ("tool/web/static/style.css", "repo/tool/web/static/style.css", 0o644),
    ("tool/web/static/logo-icon.webp", "repo/tool/web/static/logo-icon.webp", 0o644),
    # ── admin panel (backend) ──────────────────────────────────────────────
    ("admin/backend/main.py", "repo/admin/backend/main.py", 0o644),
    ("admin/backend/auth.py", "repo/admin/backend/auth.py", 0o644),
    ("admin/backend/config_manager.py", "repo/admin/backend/config_manager.py", 0o644),
    ("admin/backend/requirements.txt", "repo/admin/backend/requirements.txt", 0o644),
    # ── admin panel (frontend) ─────────────────────────────────────────────
    ("admin/frontend/index.html", "repo/admin/frontend/index.html", 0o644),
    ("admin/frontend/logo-icon.webp", "repo/admin/frontend/logo-icon.webp", 0o644),
    ("admin/frontend/css/style.css", "repo/admin/frontend/css/style.css", 0o644),
    ("admin/frontend/js/app.js", "repo/admin/frontend/js/app.js", 0o644),
]

# Bash variable name for each payload entry.
def var_name(dest: str) -> str:
    safe = dest.replace("/", "_").replace(".", "_").replace("-", "_")
    return f"_EMBED_{safe}"


INSTALLER_HEADER = r'''#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════
#  🚀 FREEBUFF2API UNIFIED INSTALLER v1.0 — self-contained
#
#  One script that installs the complete Freebuff2API gateway stack:
#    • API gateway  — freebuff2api/ package (FastAPI + httpx + SSE) :20004
#    • Admin panel  — Vue 3 admin backend + frontend :20003
#    • Systemd      — freebuff2api.service + freebuff2api-admin.service
#    • Backup       — nightly backup timer/service + journald rotation
#    • Ops scripts  — doctor.sh, backup-freebuff2api.sh, get_token.py
#    • Gate fix     — real Buffy CLI system-prompt injection + UA 3.0.25
#                     + x-freebuff-acting-user-id (free_mode_cli_required)
#    • R5/R6        — jittered rate-limit retries, /readyz, /metrics,
#                     correlation IDs, Prometheus exposure
#
#  All runtime files are EMBEDDED in this script (base64 payloads at the
#  bottom). No network dependency for the stack itself; uv + pip only pull
#  Python dependencies (fastapi, httpx, uvicorn, python-dotenv).
#
#  Usage:
#    sudo bash install_freebuff2api.sh [command] [options]
#
#  Commands:
#    install    Install (default)
#    update     Re-apply embedded files + refresh deps + restart services
#    uninstall  Remove install dir, services, timer, journald drop-in (asks)
#    doctor     Run scripts/doctor.sh against the installed gateway
#    status     Show service / port / health status
#
#  Options:
#    --home DIR        Install root (default /var/lib/freebuff2api)
#    --api-port N      API port (default 20004)
#    --admin-port N    Admin port (default 20003)
#    --user USER       Service user (default freebuff)
#    --no-admin        Skip the admin panel
#    --no-backup       Skip the backup timer + journald drop-in
#    --no-service      Do not enable/start systemd services
#    --yes             Non-interactive (auto-confirm destructive steps)
#    --help            Show this help
# ═══════════════════════════════════════════════════════════════════════

set -euo pipefail

F2A_VERSION="1.0"
F2A_HOME="${F2A_HOME:-/var/lib/freebuff2api}"
F2A_USER="${F2A_USER:-freebuff}"
API_PORT="${F2A_API_PORT:-20004}"
ADMIN_PORT="${F2A_ADMIN_PORT:-20003}"
DO_ADMIN=true
DO_BACKUP=true
DO_SERVICE=true
ASSUME_YES=false

log()  { printf '\033[1;34m[freebuff2api]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m  ✓\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m  ⚠\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m  ✗ %s\033[0m\n' "$*" >&2; exit 1; }

usage() {
  sed -n '2,45p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
}

parse_args() {
  while [ $# -gt 0 ]; do
    case "$1" in
      install|update|uninstall|doctor|status) CMD="$1" ;;
      --home) F2A_HOME="$2"; shift ;;
      --api-port) API_PORT="$2"; shift ;;
      --admin-port) ADMIN_PORT="$2"; shift ;;
      --user) F2A_USER="$2"; shift ;;
      --no-admin) DO_ADMIN=false ;;
      --no-backup) DO_BACKUP=false ;;
      --no-service) DO_SERVICE=false ;;
      --yes) ASSUME_YES=true ;;
      --help|-h) usage ;;
    esac
    shift
  done
  CMD="${CMD:-install}"
}

confirm() { # $1 prompt — returns 0 if yes
  [ "$ASSUME_YES" = true ] && return 0
  read -r -p "$1 [y/N] " ans
  [ "${ans:-n}" = "y" ] || [ "${ans:-n}" = "Y" ]
}

install_file_b64() { # $1 target  $2 base64 data  $3 mode
  local dir
  dir="$(dirname "$1")"
  mkdir -p "$dir"
  if command -v base64 &>/dev/null; then
    printf '%s' "$2" | base64 -d > "$1"
  else
    python3 -c 'import base64,sys; open(sys.argv[1],"wb").write(base64.b64decode(sys.argv[2]))' "$1" "$2"
  fi
  [ -n "${3:-}" ] && chmod "$3" "$1"
  ok "$1"
}

require_root() {
  [ "$(id -u)" = 0 ] || die "run as root (sudo bash $0)"
}

step_system_deps() {
  log "checking system dependencies"
  command -v python3 >/dev/null 2>&1 || die "python3 not found"
  if command -v uv >/dev/null 2>&1; then
    ok "uv $(uv --version 2>&1 | awk '{print $2}')"
  else
    warn "uv not found — will use python venv + pip"
  fi
  command -v curl >/dev/null 2>&1 || warn "curl not found (doctor uses it)"
}

step_service_user() {
  log "ensuring service user '$F2A_USER'"
  if id "$F2A_USER" &>/dev/null; then
    ok "user exists"
  else
    useradd --system --home-dir "$F2A_HOME" --shell /usr/sbin/nologin "$F2A_USER"
    ok "created system user"
  fi
}

step_dirs() {
  log "creating directories under $F2A_HOME"
  install -d -o "$F2A_USER" -g "$F2A_USER" "$F2A_HOME" "$F2A_HOME/data" "$F2A_HOME/repo"
  ok "dirs ready"
}

step_payloads() {
  log "writing embedded runtime files"
__PAYLOAD_STEPS__
}

step_env() {
  log "preparing .env"
  if [ -f "$F2A_HOME/repo/.env" ]; then
    ok ".env already exists (kept)"
  else
    cp "$F2A_HOME/repo/.env.example" "$F2A_HOME/repo/.env"
    chown "$F2A_USER:$F2A_USER" "$F2A_HOME/repo/.env"
    chmod 640 "$F2A_HOME/repo/.env"
    ok ".env created from template — edit $F2A_HOME/repo/.env and set FREEBUFF_TOKEN"
  fi
}

step_venv() {
  log "installing Python dependencies"
  cd "$F2A_HOME/repo"
  if command -v uv >/dev/null 2>&1; then
    if [ -f uv.lock ]; then
      sudo -u "$F2A_USER" -H env PATH="$PATH" uv sync --frozen 2>&1 | tail -3 || \
        sudo -u "$F2A_USER" -H env PATH="$PATH" uv sync 2>&1 | tail -3
    else
      sudo -u "$F2A_USER" -H env PATH="$PATH" uv sync 2>&1 | tail -3
    fi
  else
    python3 -m venv .venv
    chown -R "$F2A_USER:$F2A_USER" .venv
    sudo -u "$F2A_USER" -H .venv/bin/pip install --upgrade pip -q
    sudo -u "$F2A_USER" -H .venv/bin/pip install -e . -q
  fi
  [ -x .venv/bin/freebuff2api ] || .venv/bin/python -c 'import freebuff2api.app' || die "venv install failed"
  ok "deps installed"
}

step_admin() {
  [ "$DO_ADMIN" = true ] || { warn "admin skipped (--no-admin)"; return; }
  log "installing admin panel"
  cd "$F2A_HOME/repo/admin"
  python3 -m venv venv
  chown -R "$F2A_USER:$F2A_USER" venv
  ./venv/bin/pip install --upgrade pip -q
  ./venv/bin/pip install -r backend/requirements.txt -q
  ok "admin deps installed"
}

step_systemd() {
  [ "$DO_SERVICE" = true ] || { warn "systemd skipped (--no-service)"; return; }
  log "installing systemd units"
  cp "$F2A_HOME/repo/deploy/freebuff2api.service" /etc/systemd/system/
  cp "$F2A_HOME/repo/deploy/freebuff2api-admin.service" /etc/systemd/system/
  sed -i "s|^User=.*|User=$F2A_USER|; s|^Group=.*|Group=$F2A_USER|" \
    /etc/systemd/system/freebuff2api.service /etc/systemd/system/freebuff2api-admin.service
  sed -i "s|/var/lib/freebuff2api|$F2A_HOME|g" \
    /etc/systemd/system/freebuff2api.service /etc/systemd/system/freebuff2api-admin.service
  systemctl daemon-reload
  systemctl enable freebuff2api freebuff2api-admin >/dev/null 2>&1 || true
  ok "units installed + enabled"
}

step_backup() {
  [ "$DO_BACKUP" = true ] || { warn "backup skipped (--no-backup)"; return; }
  log "installing backup timer + journald rotation"
  cp "$F2A_HOME/backup.sh" "$F2A_HOME/backup.sh"
  chmod 750 "$F2A_HOME/backup.sh"
  chown "$F2A_USER:$F2A_USER" "$F2A_HOME/backup.sh"
  sed -i "s|/var/lib/freebuff2api|$F2A_HOME|g" "$F2A_HOME/backup.sh"
  mkdir -p /var/backups/freebuff2api
  chown "$F2A_USER:$F2A_USER" /var/backups/freebuff2api
  if [ "$DO_SERVICE" = true ]; then
    cp "$F2A_HOME/repo/deploy/backup-freebuff2api.service" /etc/systemd/system/
    cp "$F2A_HOME/repo/deploy/backup-freebuff2api.timer" /etc/systemd/system/
    sed -i "s|/var/lib/freebuff2api|$F2A_HOME|g" /etc/systemd/system/backup-freebuff2api.service
    sed -i "s|/var/lib/freebuff2api|$F2A_HOME|g" /etc/systemd/system/backup-freebuff2api.timer
    sed -i "s|^User=.*|User=$F2A_USER|; s|^Group=.*|Group=$F2A_USER|" /etc/systemd/system/backup-freebuff2api.service
    systemctl daemon-reload
    systemctl enable backup-freebuff2api.timer >/dev/null 2>&1 || true
    systemctl start backup-freebuff2api.timer >/dev/null 2>&1 || true
  fi
  cp "$F2A_HOME/repo/deploy/journald-freebuff2api.conf" /etc/systemd/journald.conf.d/freebuff2api.conf
  chmod 644 /etc/systemd/journald.conf.d/freebuff2api.conf
  systemctl restart systemd-journald || true
  ok "backup + rotation installed"
}

step_start() {
  [ "$DO_SERVICE" = true ] || { warn "services not started (--no-service)"; return; }
  log "starting services"
  systemctl restart freebuff2api
  [ "$DO_ADMIN" = true ] && systemctl restart freebuff2api-admin
  sleep 4
  local rc
  rc=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:$API_PORT/readyz 2>/dev/null || echo 000)
  ok "api :$API_PORT readyz HTTP $rc"
  if [ "$DO_ADMIN" = true ]; then
    local ar
    ar=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:$ADMIN_PORT/api/auth/status 2>/dev/null || echo 000)
    ok "admin :$ADMIN_PORT auth HTTP $ar"
  fi
}

cmd_install() {
  require_root
  step_system_deps
  step_service_user
  step_dirs
  step_payloads
  step_env
  step_venv
  step_admin
  step_systemd
  step_backup
  step_start
  log "install complete — edit $F2A_HOME/repo/.env, set FREEBUFF_TOKEN, then: sudo systemctl restart freebuff2api"
}

cmd_update() {
  require_root
  step_payloads
  step_venv
  step_admin
  step_systemd
  step_backup
  step_start
  log "update complete"
}

cmd_uninstall() {
  require_root
  confirm "Remove $F2A_HOME, services, timer and journald drop-in?" || { log "aborted"; exit 0; }
  systemctl stop freebuff2api freebuff2api-admin 2>/dev/null || true
  systemctl disable freebuff2api freebuff2api-admin 2>/dev/null || true
  systemctl disable --now backup-freebuff2api.timer 2>/dev/null || true
  rm -f /etc/systemd/system/freebuff2api.service /etc/systemd/system/freebuff2api-admin.service \
        /etc/systemd/system/backup-freebuff2api.service /etc/systemd/system/backup-freebuff2api.timer
  rm -f /etc/systemd/journald.conf.d/freebuff2api.conf
  systemctl daemon-reload
  systemctl restart systemd-journald 2>/dev/null || true
  rm -rf "$F2A_HOME"
  ok "uninstall complete"
}

cmd_doctor() {
  local key
  key=$(sed -n 's/^FREEBUFF_API_KEY=//p' "$F2A_HOME/repo/.env" 2>/dev/null | head -1)
  [ -x "$F2A_HOME/repo/scripts/doctor.sh" ] || die "doctor.sh not found (not installed?)"
  FB2API_API_KEY="$key" FB2API_BASE_URL="http://127.0.0.1:$API_PORT" \
  FB2API_ADMIN_URL="http://127.0.0.1:$ADMIN_PORT" \
  bash "$F2A_HOME/repo/scripts/doctor.sh"
}

cmd_status() {
  echo "Freebuff2API — home $F2A_HOME"
  [ -d "$F2A_HOME" ] || { echo "  not installed"; exit 1; }
  local rc ar
  rc=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:$API_PORT/readyz 2>/dev/null || echo 000)
  ar=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:$ADMIN_PORT/api/auth/status 2>/dev/null || echo 000)
  echo "  api :$API_PORT   readyz HTTP $rc"
  echo "  admin :$ADMIN_PORT auth HTTP $ar"
  command -v systemctl >/dev/null 2>&1 && {
    systemctl is-active freebuff2api 2>/dev/null | sed 's/^/  service: /'
    systemctl is-active freebuff2api-admin 2>/dev/null | sed 's/^/  admin service: /'
    systemctl is-active backup-freebuff2api.timer 2>/dev/null | sed 's/^/  backup timer: /'
  } || true
}

# ═══════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════
main() {
  parse_args "$@"
  case "$CMD" in
    install)   cmd_install ;;
    update)    cmd_update ;;
    uninstall) cmd_uninstall ;;
    doctor)    cmd_doctor ;;
    status)    cmd_status ;;
    *)         usage; exit 1 ;;
  esac
}
'''


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="installers/install_freebuff2api.sh")
    args = parser.parse_args()

    output = REPO_ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)

    # Validate every manifest source exists before generating.
    missing = [src for src, _, _ in MANIFEST if not (REPO_ROOT / src).is_file()]
    if missing:
        for src in missing:
            print(f"ERROR: missing {src}")
        return 1

    payload_steps: list[str] = []
    payload_vars: list[str] = []
    for src, dest, mode in MANIFEST:
        data = (REPO_ROOT / src).read_bytes()
        b64 = base64.b64encode(data).decode("ascii")
        var = var_name(dest)
        payload_vars.append(f'{var}="{b64}"')
        payload_steps.append(
            f"  install_file_b64 \"$F2A_HOME/{dest}\" \"${var}\" {mode:03o}"
        )

    body = INSTALLER_HEADER.replace("__PAYLOAD_STEPS__", "\n".join(payload_steps))
    trailer = (
        "\n# ── EMBEDDED PAYLOADS (generated by scripts/build_installer.py — do not edit) ──\n"
        + "\n".join(payload_vars)
        + "\n\nmain \"$@\"\n"
    )
    output.write_text(body + trailer, encoding="utf-8")
    output.chmod(0o755)
    print(f"generated {output.relative_to(REPO_ROOT)} ({output.stat().st_size} bytes, "
          f"{len(MANIFEST)} payloads)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
