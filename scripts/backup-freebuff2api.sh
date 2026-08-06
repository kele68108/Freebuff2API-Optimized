#!/usr/bin/env bash
# Freebuff2API nightly backup — plan §5.
# Backs up the state dir (/var/lib/freebuff2api/data) and the .env (tokens)
# into a timestamped, freebuff-owned, mode-600 tarball under /var/backups/freebuff2api,
# pruning archives older than 14 days.
#
# Designed to run as the `freebuff` service user via systemd timer
# (backup-freebuff2api.timer) or cron:
#   0 3 * * * /var/lib/freebuff2api/backup.sh
set -euo pipefail

DATA_DIR="${FREEBUFF2API_DATA_DIR:-/var/lib/freebuff2api/data}"
ENV_FILE="${FREEBUFF2API_ENV_FILE:-/var/lib/freebuff2api/repo/.env}"
BACKUP_ROOT="${FREEBUFF2API_BACKUP_DIR:-/var/backups/freebuff2api}"
RETENTION_DAYS="${FREEBUFF2API_BACKUP_RETENTION:-14}"

mkdir -p "$BACKUP_ROOT"
stamp="$(date +%F-%H%M%S)"
archive="${BACKUP_ROOT}/freebuff2api-${stamp}.tar.gz"
tmpdir="$(mktemp -d)"

trap 'rm -rf "$tmpdir"' EXIT

# Stage into a temp dir first so the tarball is atomic and path-clean.
mkdir -p "$tmpdir/data"
if [ -d "$DATA_DIR" ] && [ -n "$(ls -A "$DATA_DIR" 2>/dev/null)" ]; then
    cp -a "$DATA_DIR"/. "$tmpdir/data/"
fi
if [ -f "$ENV_FILE" ]; then
    cp -a "$ENV_FILE" "$tmpdir/env"
fi

tar -czf "$archive" -C "$tmpdir" .

chmod 600 "$archive"
chown freebuff:freebuff "$archive" 2>/dev/null || true

# Retention: prune archives older than N days.
find "$BACKUP_ROOT" -name 'freebuff2api-*.tar.gz' -mtime "+${RETENTION_DAYS}" -delete

echo "backup complete: ${archive} ($(du -h "$archive" | cut -f1))"
