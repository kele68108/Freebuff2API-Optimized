#!/usr/bin/env bash
# Freebuff2API doctor — go/no-go validation. Exits non-zero on any hard failure.
# Usage: FB2API_API_KEY=sk-... bash scripts/doctor.sh   (chat tests need a real token)
set -uo pipefail
HOST="${FB2API_HOST:-127.0.0.1}"
PORT="${FB2API_PORT:-20004}"
ADMIN="${FB2API_ADMIN:-127.0.0.1:20003}"
KEY="${FB2API_API_KEY:-}"
fail=0
warn=0

say() { printf '%s\n' "$*"; }
ok()  { say "  OK  $*"; }
bad() { say "  FAIL $*"; fail=1; }
w()   { say "  WARN $*"; warn=1; }

H() { curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $KEY" "http://$HOST:$PORT$1"; }

say "== 1. healthz =="
[ "$(H /healthz)" = 200 ] && ok "healthz 200" || bad "healthz != 200 (key required)"

say "== 2. readyz =="
rc=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $KEY" "http://$HOST:$PORT/readyz" 2>/dev/null)
case "$rc" in
  200|503) ok "readyz answered ($rc)";;
  *)       w "readyz absent (R6 not applied yet)";;
esac

say "== 3. models =="
models=$(curl -s -H "Authorization: Bearer $KEY" "http://$HOST:$PORT/v1/models")
echo "$models" | grep -q 'deepseek/deepseek-v4-flash' && ok "models listed" || bad "models missing"

say "== 4. non-stream chat =="
if [ -z "$KEY" ]; then
  w "no API key configured — skip chat (needs FREEBUFF_TOKEN upstream)"
else
  body='{"model":"deepseek/deepseek-v4-flash","messages":[{"role":"user","content":"ping"}],"stream":false}'
  curl -s -m 120 -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' -d "$body" \
    "http://$HOST:$PORT/v1/chat/completions" | grep -q '"object":"chat.completion"' \
    && ok "non-stream ok" || bad "non-stream failed"
fi

say "== 5. stream chat =="
if [ -z "$KEY" ]; then
  w "skip stream"
else
  body='{"model":"deepseek/deepseek-v4-flash","messages":[{"role":"user","content":"ping"}],"stream":true}'
  curl -sN -m 120 -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' -d "$body" \
    "http://$HOST:$PORT/v1/chat/completions" | grep -q '\[DONE\]' \
    && ok "stream ok" || bad "stream failed"
fi

say "== 6. admin auth =="
as=$(curl -s -X POST "http://$ADMIN/api/auth/status" | grep -o '"initialized":[a-z]*' || true)
[ -n "$as" ] && ok "admin auth status: $as" || w "admin not reachable — skipped"
if [ -n "${FB2API_ADMIN_PW:-}" ]; then
  code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "http://$ADMIN/api/auth/login" -d '{"password":"wrong"}')
  [ "$code" = 401 ] && ok "wrong password -> 401" || w "wrong password code=$code"
fi

say "== 7. rotation evidence =="
journalctl -u freebuff2api -n 200 --no-pager 2>/dev/null | grep -q 'rate limit' \
  && ok "rate-limit path seen" || w "no rotation log in window"

say "== 8. log hygiene =="
if [ "$(systemctl is-active freebuff2api 2>/dev/null)" = "active" ]; then
  leak=$(journalctl -u freebuff2api -n 500 --no-pager 2>/dev/null | grep -cE 'Bearer (sk-|[A-Za-z0-9_-]{8,})' || true)
  [ "$leak" -eq 0 ] && ok "no token leak" || bad "possible token leak lines=$leak"
else
  bad "service not active — cannot certify log hygiene"
fi

say "== 9. resources =="
pid=$(systemctl show -p MainPID --value freebuff2api 2>/dev/null)
if [ -n "${pid:-}" ] && [ "$pid" != "0" ] && [ -r "/proc/$pid/status" ]; then
  rss=$(awk '/VmRSS/{print $2}' "/proc/$pid/status")
  fd=$(ls "/proc/$pid/fd" 2>/dev/null | wc -l)
  if [ "${rss:-999999}" -lt 204800 ]; then
    ok "RSS ${rss} kB < 200 MB"
  else
    bad "RSS ${rss} kB >= 200 MB"
  fi
  ok "FD count $fd (baseline)"
else
  w "service not running via systemd — resource checks skipped"
fi

say
if [ $fail -eq 0 ]; then
  say "RESULT: GO ($warn warnings)"
else
  say "RESULT: NO-GO ($fail failures, $warn warnings)"
fi
exit $fail
