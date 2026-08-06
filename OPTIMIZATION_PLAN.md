# Freebuff2API-Optimized — Production Hardening & Optimization Plan

**Baseline:** `kele68108/Freebuff2API-Optimized` @ commit `f7e13d4` (`main`)
**Date:** 2026-08-06 · **Status:** analysis complete, deployment not yet executed
**All claims below verified by reading the repo at `f7e13d4` unless marked "unverified".**

---

## 0. Baseline Verification

**Pinned clone (reproducible):**
```bash
git clone https://github.com/kele68108/Freebuff2API-Optimized.git
cd Freebuff2API-Optimized
git checkout f7e13d4                       # -> f7e13d4 "fix: optimistic reuse stale-witness + waiting_room_required 428"
git rev-parse HEAD                          # must equal f7e13d414e9a7197a227969d88ad6166724c2a15
```

**`uv sync` reproducibility — VERIFIED on this host:**
- `uv 0.11.32` auto-provisioned **Python 3.13.14** from `.python-version` (repo pins `3.13`), then resolved `uv.lock` (80 KB) → `fastapi 0.136.1`, `httpx 0.28.1`, `uvicorn 0.47.0`. No lockfile mutation.
- **Test suite: `51 passed`** via `.venv/bin/python -m pytest -q` (1.3 s).
- ⚠️ **Lockfile baseline nuance:** the committed `uv.lock` records `requires-python = ">=3.13"` while `pyproject.toml` says `>=3.11` — `uv sync` re-resolves and **rewrites `uv.lock`** (observed: +149 lines). For a frozen baseline, either commit that rewrite once, or re-pin with `uv lock` after checkout; do not treat the on-disk lock as byte-stable across hosts.
- ⚠️ **Host hazard found:** `uv run pytest` resolves a *stray* `pytest` from `~/.owl-dns/venv` (first on PATH) whose Python lacks the project deps → 8 collection errors. The project declares **no dev dependency group** (`pyproject.toml` has none), so `uv sync` does not install pytest. **Fix:** run tests via `.venv/bin/python -m pytest`; add `[dependency-groups] dev = ["pytest>=8", "pytest-asyncio"]` (diff-able, reversible) so `uv sync` covers it.

**Drifts vs. prompt claims (flagged per guardrails):**
| Claim | Actual | Verdict |
|---|---|---|
| Tagged `Freebuff2API-Optimized` | No tag on remote; branch `main` only | drift — pin by commit, not tag |
| `Python 3.11.x exact` | `.python-version` = **3.13** (`requires-python >=3.11`) | drift — use uv-managed 3.13 |
| Main systemd unit `freebuff2api.service` | **absent** — only `freebuff2api-admin.service` ships | must author in P5 |
| "Guest mode" fallback (README) | no `guest` references in code | unverified — not in code |

**Frozen baseline statement:** Python 3.13 + FastAPI `0.136` + httpx `0.28` + uvicorn, single FastAPI app (`freebuff2api.app`) plus a separate Vue3/JWT admin backend, both configured via `.env`, data under `~/.freebuff2api` (hardcoded default `/root/.freebuff2api`), reproduced byte-identically from `f7e13d4` + `uv.lock`.

---

## 1. Architecture Analysis

### Module map
```
main.py                        uvicorn runner (host/port from Settings, 1 worker, reload=False)
├── freebuff2api/config.py     Settings dataclass + env load (FREEBUFF_*), multi-token CSV
├── freebuff2api/app.py        FastAPI app; routes: GET /healthz, GET /v1/models,
│                              POST /v1/chat/completions (stream + non-stream)
│   ├── freebuff2api/codebuff.py      CodebuffClient (httpx AsyncClient),
│   │                                 SessionManager (per-account), CodebuffAccountPool
│   ├── freebuff2api/openai_compat.py payload builder, message normalizer,
│   │                                 stream sanitizer, CompletionAccumulator
│   ├── freebuff2api/models.py        10-model registry (7 freebuff + 3 gemini),
│   │                                 agent-validation payload, model resolver
│   ├── freebuff2api/sse.py           SSE encode/decode
│   └── freebuff2api/logging_config.py  logging, header redaction
admin/backend/{main,auth,config_manager}.py  Vue3 panel API: JWT+bcrypt auth,
                                             config/accounts/keys CRUD, SSE logs, :20003
admin/frontend/                  Vue 3 (CDN) SPA
tool/get_token.py + tool/web     token-issuing helpers
exploitation.js                  Cloudflare Worker MITM proxy for codebuff.com (strips
                                 CSP/X-Frame-Options; companion tool, not served here)
```

### Data flow (chat hot path)
```
Client → POST /v1/chat/completions
  → _check_local_auth (Bearer; env key or api_keys.json)      [app.py:_check_local_auth]
  → CodebuffAccountPool.acquire_session(model, messages)       [codebuff.py] (busy-flag reserve)
  → SessionManager.acquire_session → _ensure_session_locked    optimistic reuse / verify / create
  → client.request_ad_chain (gravity→zeroclick ads)            waiting-room prerequisite
  → client.validate_agents (cached probe)                      [/api/agents/validate]
  → _start_freebuff_run_chain (parent + context-pruner runs, parallelized)
  → build_upstream_payload (codebuff_metadata, Buffy prompt, stop="cb_easp")
  → CodebuffClient.chat_events → POST https://www.codebuff.com/api/v1/chat/completions (SSE)
  → decode_sse_data → sanitize_stream_chunk (stream) | CompletionAccumulator (non-stream)
```

### Hot paths
| Path | Cost | Notes |
|---|---|---|
| `/v1/chat/completions` stream | 1 upstream HTTP + ads + 3-4 agent-run calls | dominant; retry loop `MAX_CHAT_RETRIES=2` |
| `/v1/chat/completions` non-stream | same + full-body accumulation | `CompletionAccumulator.final_response()` |
| `/v1/models` | in-memory constant | trivial |
| `/healthz` | requires auth + sync `api_keys.json` read per request | poor liveness design |
| admin auth | bcrypt check + JWT (HS256, 7-day exp) | **no login rate-limit** |

### Signature optimizations — verified inventory
| # | Optimization | Location (file:symbol) | Mechanism |
|---|---|---|---|
| 1 | Optimistic session reuse (+ stale-witness) | `codebuff.py:SessionManager._ensure_session_locked` | 30 s `_verify_window_seconds` reuse w/o upstream GET; outside window, verify GET with cached `instance_id`; reject stale witness (upstream echoes same id but superseded) |
| 2 | `waiting_room_required` 428 fix | `codebuff.py:_upstream_error` (BUG A) | maps 428→`is_session_error=True` → `chat_completions` invalidates cache + retries through ads/streak path |
| 3 | Probe cache | `codebuff.py:CodebuffClient.validate_agents` + `_verified_at` | `_agents_validated` flag + lock; verify GET amortized per model |
| 4 | Run-chain parallelization | `app.py:_start_freebuff_run_chain` | `asyncio.gather(finish_run(child), record_run_step(parent))` |
| 5 | Async stats I/O | `codebuff.py:CodebuffAccountPool._write_stats` | ⚠️ **NOT actually off-thread** — sync `Path.write_text` in async fn (small file, try/except) — improvement target |
| 6 | Session invalidation on conflicts | `codebuff.py:SessionManager.invalidate_session` | pops cache, deletes upstream, 60 s `_invalidate_safety_seconds` blocks re-adopting echoed instance; resets `_verified_at` |
| 7 | Upstream-driven rate-limit failover | `codebuff.py:_upstream_error`(429→`is_rate_limit`+`reset_at`) → `CodebuffAccountPool.mark_rate_limited`/`_next_available_index`; retry loop `app.py:chat_completions` | per-account per-model `blocked_until`; busiest-path skip |

### SSE backpressure — VERIFIED OK
`CodebuffClient.chat_events` uses `response.aiter_lines()` + an async generator → **no unbounded queue**; `StreamingResponse` applies natural await-based backpressure. `X-Accel-Buffering: no` + `Cache-Control: no-cache, no-transform` already set (`app.py:_stream_openai_chunks`). Only gap: no periodic SSE keepalive comment for long idle streams behind buffering proxies.

---

## 2. Optimization Recommendations (prioritized)

Legend: impact/effort · 🔴 breaking | 🟢 non-breaking

| ID | Area | Current State (verified) | Proposed Change | Expected Gain | Risk | Rollback |
|---|---|---|---|---|---|---|
| R1 | Data-dir portability | `/root/.freebuff2api` hardcoded default in `app.py:_check_local_auth`, `codebuff.py:_stats_file`, `admin/backend/auth.py`, `admin/backend/main.py` | Single source: `FREEBUFF2API_DATA_DIR` exposed in `config.py:Settings`; default `~/.freebuff2api` via `Path.home()`; use everywhere | Non-root service user works; silent auth/stats failures eliminated; RSS/fd unaffected | 🟢 Low (pure default change) | revert commit / env override |
| R2 | Hot-path blocking I/O | `_check_local_auth` sync-reads `api_keys.json` every request; `_write_stats` sync-writes on every acquire/release | Cache keys with mtime check; `await asyncio.to_thread(accounts._write_stats_sync, ...)`; batch stats to ≤1 write/2s | p99 −5–15% under load; event-loop stalls gone | 🟢 Low | git revert; env toggle `FREEBUFF_STATS_OFF=true` |
| R3 | httpx pool tuning | Hardcoded `Limits(max_keepalive_connections=20, max_connections=100)`, `read=300.0` (`codebuff.py:CodebuffClient.__init__`) | Env-tunable `FREEBUFF_HTTPX_MAX_CONNECTIONS=100`, `_KEEPALIVE=20`, `_KEEPALIVE_EXPIRY=30`, `_READ_TIMEOUT=300`; per-account share | Controlled FD ceiling; p99 stability under burst | 🟢 Low | unset env → defaults |
| R4 | Bounded concurrency | `_reserve_account` waits on `asyncio.Condition` — unbounded waiters when all accounts busy | Add `FREEBUFF_MAX_WAITERS` (default e.g. 64) semaphore; exceed → `503` fast-fail; keep single-flight-per-account. **Metrics must distinguish 503 (client-overload) from 502 (upstream failure)** — `_error_response` currently collapses both into the same surface | Tail p99 bounded; no thundering-herd pile-up; overload predictable as 503 | 🟢 Med (behavior change at limit) | env off / default 0 = unlimited |
| R5 | Retry with jitter | Session-error & rate-limit retries are **immediate**; `reset_at`/`retry_after_ms` parsed but unused in delay | Sleep `min(retry_after_ms or 500, 5000) + uniform(0,250)` ms before rate-limit retry; never retry 409 conflicts (already correct — keep `is_session_error` invalidate path) | Retry count −30–50%; 429-loop exits faster | 🟢 Med | env `FREEBUFF_RETRY_JITTER=0` |
| R6 | Observability | No `/readyz`, no `/metrics`; log format has **no correlation ID** (`logging_config.py:LOG_FORMAT`); `/healthz` requires auth | Add `/readyz` (≤1 healthy account = 503), `/metrics` (Prometheus text format; request counters, latency histograms, account-health gauge), middleware adding `request_id` to logs; unauthenticated `/livez` | Mean-time-to-detect 5xx/pool-exhaustion from hours→minutes | 🟢 Med | route flags env-gated |
| R7 | Proactive account health | Only reactive quarantine via `mark_rate_limited` | Background pinger: every `FREEBUFF_HEALTH_INTERVAL` (default 60 s) `get_streak()` per account; mark cooldown on failure; `_next_available_index` skips quarantined | Error rate drops when an account dies silently; rotation observable | 🟢 Med | interval env; stop task on shutdown |
| R8 | Admin security baseline | `freebuff2api-admin.service` runs **User=root**, `0.0.0.0:20003`, hardcoded `/root` paths, no hardening; login has **no rate limit** | Non-root user; bind `127.0.0.1` default; systemd hardening (see P5); login throttle (fixed sleep + attempt counter, in-memory) | Blast radius reduced; brute-force resistance | 🟢 Med (deploy-time) | stop/disable unit |
| R9 | Python pin | `.python-version` = 3.13; host default 3.14 | Deploy via `uv python install 3.13` + `.venv`; never use system python | Exact reproducibility | 🟢 Low | n/a (deploy choice) |
| R10 | SSE keepalive | no keepalive comments | Emit `: ping` comment every 15 s idle on stream | No client/proxy timeouts on long generations | 🟢 Low | flag off |
| R11 | Exploitation.js posture | Worker strips CSP/X-Frame-Options | Document as known risk; never serve from same origin as admin; optional: add `Sec-Fetch` filters | Security review completeness | 🟢 Low (doc only) | n/a |

**Explicitly NOT recommended:** moving to multiple uvicorn workers (upstream session semantics are per-process; pool state is in-memory — 1 worker is correct); swapping httpx for aiohttp (regression risk vs. verified `httpx[socks]`).

---

## 3. Installation & Deployment (phase-gated)

> Prerequisites verified on this host: kernel `7.0.0-28-generic` (≥5.10 ✓), `uv 0.11.32` ✓, `git` ✓, `systemd 259` ✓, `curl` ✓, ports 20003/20004 free ✓, `FREEBUFF_TOKEN` **not yet supplied** (blocks P2 chat validation only). Deployment dir: `/var/lib/freebuff2api` (repo) + `/var/lib/freebuff2api/data` (data). Non-root user: `freebuff`.

### P0 — Toolchain
```bash
# uv (if missing)
curl -LsSf https://astral.sh/uv/install.sh | sh
uv python install 3.13                       # match .python-version
# service user (non-root)
sudo useradd --system --create-home --home-dir /var/lib/freebuff2api --shell /usr/sbin/nologin freebuff
```
Success: `uv --version`, `uv python list | grep 3.13`, `id freebuff`.
Rollback: `sudo userdel freebuff`.
WHY: uv pins the interpreter + lockfile; a non-root user is the security boundary for P5.

### P1 — Checkout pinned commit + lockfile
```bash
sudo -u freebuff git clone https://github.com/kele68108/Freebuff2API-Optimized.git /var/lib/freebuff2api/repo
cd /var/lib/freebuff2api/repo && sudo -u freebuff git checkout f7e13d4
sudo -u freebuff uv sync                       # resolves from uv.lock, installs .venv
```
Success: `git rev-parse HEAD` = `f7e13d4…`; `uv run python -c "import fastapi"` works; `.venv/bin/python -m pytest -q` → 51 passed (install pytest into venv first if running tests).
Rollback: `sudo rm -rf /var/lib/freebuff2api/repo`.
WHY: pins the exact frozen baseline; lockfile guarantees byte-identical deps.

### P2 — Configure `.env`
```bash
sudo -u freebuff cp .env.example /var/lib/freebuff2api/repo/.env
# edit: FREEBUFF_TOKEN=<comma-separated tokens>   (from HAR or freebuff.071129.xyz; redact: only last 4 chars in any log/output)
#       FREEBUFF_API_KEY=sk-local-...              (API bearer)
#       FREEBUFF_API_BASE_URL=https://www.codebuff.com
#       FREEBUFF_PROXY_ENABLED=true  FREEBUFF_PROXY_URL=socks5h://127.0.0.1:7890   (optional)
#       FREEBUFF_HOST=127.0.0.1  FREEBUFF_PORT=20004
#       FREEBUFF2API_DATA_DIR=/var/lib/freebuff2api/data
```
Success: file parses; `FREEBUFF_TOKEN` non-empty (validate by P3 smoke).
Rollback: restore `.env.example`; keep tokens only in the user's vault.
WHY: all tunables are config-driven (guardrail: prefer configuration over code).

### P3 — Main service smoke (foreground)
```bash
cd /var/lib/freebuff2api/repo
set -a; . ./.env; set +a            # dotenv-safe source (do NOT use `env $(xargs)` — breaks on spaces/options)
sudo -u freebuff /var/lib/freebuff2api/repo/.venv/bin/freebuff2api &   # console script (main:main)
sleep 3
curl -s -H "Authorization: Bearer $FREEBUFF_API_KEY" http://127.0.0.1:20004/healthz
curl -s -H "Authorization: Bearer $FREEBUFF_API_KEY" http://127.0.0.1:20004/v1/models   # expect 10 models
```
Success: healthz 200; models lists `deepseek/deepseek-v4-flash` … `google/gemini-3.1-pro-preview`.
Rollback: kill the foreground process.
WHY: proves env wiring + console entry point before systemd involvement.

### P4 — Admin panel
`install-admin.sh` hardcodes `/root/freebuff2api` — **do not run as-is** (guardrail: stop, don't improvise). Adapt by env/paths:
```bash
# NOTE: admin panel has its own deps (admin/backend/requirements.txt — no uv.lock); pip venv is the repo convention.
# NOTE: NO __init__.py exists in admin/ or admin/backend/ (verified) — must run from admin/backend dir with `main:app`,
#       matching the repo's own unit. `uvicorn admin.backend.main:app` from the repo root WOULD FAIL.
sudo -u freebuff python3 -m venv /var/lib/freebuff2api/repo/admin/venv
sudo -u freebuff /var/lib/freebuff2api/repo/admin/venv/bin/pip install -r /var/lib/freebuff2api/repo/admin/backend/requirements.txt
cd /var/lib/freebuff2api/repo/admin/backend
FREEBUFF2API_DATA_DIR=/var/lib/freebuff2api/data \
FREEBUFF2API_ADMIN_HOST=127.0.0.1 FREEBUFF2API_ADMIN_PORT=20003 \
  sudo -u freebuff /var/lib/freebuff2api/repo/admin/venv/bin/uvicorn main:app --host 127.0.0.1 --port 20003 &
curl -s -X POST http://127.0.0.1:20003/api/auth/status          # expect {"initialized": false}
curl -s -X POST http://127.0.0.1:20003/api/auth/setup -d '{"password":"<first-run>"}'   # set password → JWT
curl -s -X POST http://127.0.0.1:20003/api/auth/login -d '{"password":"<first-run>"}'   # → token
```
Success: setup returns token; login with wrong password → 401 (and, post-R8, throttled).
Rollback: kill admin process; `rm -rf admin/venv`.
WHY: panel is independently deployable; JWT secret auto-generates on first boot (`auth.py:JWT_SECRET`).

### P5 — systemd enable + harden
Author `/etc/systemd/system/freebuff2api.service` (not shipped — verified absent) and replace the root-run admin unit with hardened ones:
```ini
# /etc/systemd/system/freebuff2api.service
[Unit]
Description=Freebuff2API OpenAI-compatible gateway
After=network-online.target
[Service]
Type=simple
User=freebuff
Group=freebuff
WorkingDirectory=/var/lib/freebuff2api/repo
EnvironmentFile=/var/lib/freebuff2api/repo/.env
Environment=FREEBUFF2API_DATA_DIR=/var/lib/freebuff2api/data
ExecStart=/var/lib/freebuff2api/repo/.venv/bin/freebuff2api
Restart=on-failure
RestartSec=5
# hardening
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/freebuff2api/data
ProtectKernelTunables=true
ProtectControlGroups=true
RestrictSUIDSGID=true
[Install]
WantedBy=multi-user.target
```
(admin unit: same pattern but `WorkingDirectory=/var/lib/freebuff2api/repo/admin/backend` + `ExecStart=…/admin/venv/bin/uvicorn main:app --host 127.0.0.1 --port 20003` — the `main:app` import requires running from the `admin/backend` dir; and `Environment=FREEBUFF2API_ADMIN_HOST=127.0.0.1`.)

> ⚠️ **Dependency order — apply R1 before P5.** Until R1 lands, `codebuff.py` still writes stats to the hardcoded `Path("/root/.freebuff2api/account_stats.json")`. Under the hardened non-root unit that write silently fails (try/except in `_write_stats`) — auth/API still work (they honor `FREEBUFF2API_DATA_DIR`), but `account_stats.json` will never update. Either land R1 first or accept the silent stats gap.
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now freebuff2api freebuff2api-admin
```
Success: both `active (running)`; `journalctl -u freebuff2api -n 20` shows `configured freebuff accounts count=N`.
Rollback: `sudo systemctl disable --now freebuff2api freebuff2api-admin && sudo rm /etc/systemd/system/freebuff2api.service /etc/systemd/system/freebuff2api-admin.service && sudo systemctl daemon-reload`.
WHY: `Restart=on-failure` + hardening + non-root is the core production posture (R8).

### P6 — Reverse proxy + TLS (admin only; API stays key-protected on localhost)
```bash
# nginx site (admin panel only)
#   server 443 ssl → proxy_pass http://127.0.0.1:20003;
#   client_max_body_size 2m;  add X-Forwarded-For/Proto;  HSTS;  no proxy for :20004
sudo apt install -y nginx && sudo install -m644 freebuff2api-nginx.conf /etc/nginx/sites-available/
sudo ln -s /etc/nginx/sites-available/freebuff2api-nginx.conf /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```
Success: `https://<host>/` serves panel over TLS; `curl http://127.0.0.1:20004` still requires Bearer.
Rollback: `sudo rm /etc/nginx/sites-enabled/freebuff2api-nginx.conf && sudo nginx -t && sudo systemctl reload nginx`.
WHY: admin credentials/JWT must never traverse plaintext; the API is protected by its own key and stays loopback-only.
*(The referenced `freebuff2api-nginx.conf` must be authored in this phase — it is not shipped in the repo.)*

### P7 — Validation & doctor
Run `scripts/doctor.sh` (section 4). Success = all go/no-go green (chat tests require a real token; otherwise WARN+skip and the script exits non-zero only for hard failures).

---

## 4. Validation Checklist (go/no-go)

| # | Check | Pass condition |
|---|---|---|
| 1 | `/healthz` | HTTP 200 with Bearer key (after R6: `/livez` unauthenticated 200) |
| 2 | `/readyz` | HTTP 200 iff ≥1 healthy account (R6) |
| 3 | `/v1/models` | returns the documented 10-model set |
| 4 | chat non-stream | 200 JSON `chat.completion`, non-empty content, with real token |
| 5 | chat stream | SSE `data:` chunks → `[DONE]`, `X-Accel-Buffering: no` |
| 6 | admin auth | setup→login→JWT; wrong password → 401 + throttled (R8) |
| 7 | rotation | simulate 428/429 → `journalctl` shows `rate limited`, next request on a different account |
| 8 | log hygiene | `journalctl -u freebuff2api -f` shows no `Bearer sk-`/token strings (only `<redacted>`/last-4) |
| 9 | resource | steady-state RSS < 200 MB; FD count stable over 1,000 requests |

### `scripts/doctor.sh` (make doctor equivalent)

<details><summary>doctor.sh</summary>

```bash
#!/usr/bin/env bash
# Freebuff2API doctor — go/no-go validation. Exits non-zero on any hard failure.
set -uo pipefail
HOST="${FB2API_HOST:-127.0.0.1}"; PORT="${FB2API_PORT:-20004}"
ADMIN="${FB2API_ADMIN:-127.0.0.1:20003}"; KEY="${FB2API_API_KEY:-}"
DATA_DIR="${FREEBUFF2API_DATA_DIR:-$HOME/.freebuff2api}"
fail=0; warn=0
say(){ printf '%s\n' "$*"; }
ok(){ say "  ✓ $*"; } bad(){ say "  ✗ $*"; fail=1; } w(){ say "  ⚠ $*"; warn=1; }

H() { curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $KEY" "http://$HOST:$PORT$1"; }

say "== 1. healthz =="; [ "$(H /healthz)" = 200 ] && ok "healthz 200" || bad "healthz != 200"

say "== 2. readyz =="
rc=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $KEY" "http://$HOST:$PORT/readyz" 2>/dev/null)
case "$rc" in 200|503) ok "readyz answered ($rc)";; *) w "readyz absent (R6 not applied yet)";; esac

say "== 3. models =="
models=$(curl -s -H "Authorization: Bearer $KEY" "http://$HOST:$PORT/v1/models")
echo "$models" | grep -q 'deepseek/deepseek-v4-flash' && ok "models listed" || bad "models missing"

say "== 4. non-stream chat =="
if [ -z "$KEY" ]; then w "no API key configured — skip chat (needs FREEBUFF_TOKEN upstream)"; else
  body='{"model":"deepseek/deepseek-v4-flash","messages":[{"role":"user","content":"ping"}],"stream":false}'
  curl -s -m 120 -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' -d "$body" \
    "http://$HOST:$PORT/v1/chat/completions" | grep -q '"object":"chat.completion"' && ok "non-stream ok" || bad "non-stream failed"
fi

say "== 5. stream chat =="
if [ -z "$KEY" ]; then w "skip stream"; else
  body='{"model":"deepseek/deepseek-v4-flash","messages":[{"role":"user","content":"ping"}],"stream":true}'
  curl -sN -m 120 -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' -d "$body" \
    "http://$HOST:$PORT/v1/chat/completions" | grep -q '\[DONE\]' && ok "stream ok" || bad "stream failed"
fi

say "== 6. admin auth =="
as=$(curl -s -X POST "http://$ADMIN/api/auth/status" | grep -o '"initialized":[a-z]*' || true)
[ -n "$as" ] && ok "admin auth status: $as" || w "admin not reachable — skipped"
if [ -n "${FB2API_ADMIN_PW:-}" ]; then
  code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "http://$ADMIN/api/auth/login" -d "{\"password\":\"wrong\"}")
  [ "$code" = 401 ] && ok "wrong password → 401" || w "wrong password code=$code"
fi

say "== 7. rotation evidence =="
journalctl -u freebuff2api -n 200 --no-pager 2>/dev/null | grep -q 'rate limit' && ok "rate-limit path seen" || w "no rotation log in window"

say "== 8. log hygiene =="
leak=$(journalctl -u freebuff2api -n 500 --no-pager 2>/dev/null | grep -cE 'Bearer (sk-|[A-Za-z0-9_-]{8,})' || true)
[ "$leak" -eq 0 ] && ok "no token leak" || bad "possible token leak lines=$leak"

say "== 9. resources =="
pid=$(systemctl show -p MainPID --value freebuff2api 2>/dev/null)
if [ -n "${pid:-}" ] && [ "$pid" != "0" ]; then
  rss=$(awk '/VmRSS/{print $2}' "/proc/$pid/status"); fd=$(ls "/proc/$pid/fd" 2>/dev/null | wc -l)
  [ "${rss:-999999}" -lt 204800 ] && ok "RSS ${rss} kB < 200 MB" || bad "RSS ${rss} kB >= 200 MB"
  ok "FD count $fd (baseline)"
else
  w "service not running via systemd — skip"
fi

say; [ $fail -eq 0 ] && say "RESULT: GO ($warn warnings)" || say "RESULT: NO-GO ($fail failures, $warn warnings)"
exit $fail
```

</details>

---

## 5. Post-Install Hardening & Monitoring

**Log rotation** (systemd journald): cap journal usage —
```ini
# /etc/systemd/journald.conf.d/freebuff2api.conf
[Journal]
SystemMaxUse=500M
MaxRetentionSec=14day
```
**Backup** — nightly tarball of the small state dir (no tokens are stored there besides `api_keys.json` hashes/keys — treat as secrets):
```bash
0 3 * * *  tar -czf /var/backups/freebuff2api-$(date +\%F).tar.gz /var/lib/freebuff2api/data && find /var/backups -name 'freebuff2api-*' -mtime +14 -delete
```
**Secret rotation procedure** — API keys: regenerate `FREEBUFF_TOKEN` upstream, update `.env`, `systemctl restart freebuff2api`; admin JWT: `rm /var/lib/freebuff2api/data/.jwt_secret && systemctl restart freebuff2api-admin` (forces all sessions to re-login, new key generated on boot per `auth.py`).

**Alert rules (Prometheus + Alertmanager, once R6 `/metrics` lands):**
| Rule | Condition | Severity |
|---|---|---|
| api_5xx_rate | `rate(http_requests_total{status=~"5.."}[5m]) > 0.05` | page |
| api_p99_latency | `histogram_quantile(0.99, http_request_duration_seconds_bucket) > 30` | page |
| healthy_accounts | `freebuff_accounts_healthy < 1` | page (no fallback) |
| rss | `process_resident_memory_bytes > 200*1024*1024` for 10m | warn |

**Update procedure (blue-green via systemd):**
```bash
cd /var/lib/freebuff2api/repo
git fetch origin && git checkout <new-commit>          # re-pin, never move off pin implicitly
sudo -u freebuff uv sync                                # re-resolve lockfile
sudo systemctl restart freebuff2api freebuff2api-admin
sudo -u freebuff bash scripts/doctor.sh                 # go/no-go; on failure:
sudo systemctl revert freebuff2api                      # or checkout previous commit + restart
```
Run `doctor.sh` before and after; keep the previous commit's `.venv` intact until doctor passes (no `uv sync --prune`).

**AGPL-3.0 compliance:** all modifications to this repo remain AGPL-3.0 (LICENSE preserved, notices intact). No incompatible-license dependency swaps are proposed.
