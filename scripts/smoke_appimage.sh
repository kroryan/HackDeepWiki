#!/usr/bin/env bash
set -euo pipefail

image="${1:-HackDeepWiki-x86_64.AppImage}"
test -x "$image"
APPIMAGE_EXTRACT_AND_RUN=1 timeout 120s "$image" --help

smoke_root="$(mktemp -d)"
smoke_log="$smoke_root/app.log"
health_out="$smoke_root/health.json"
frontend_out="$smoke_root/frontend.html"

setsid env BROWSER=/bin/true APPIMAGE_EXTRACT_AND_RUN=1 \
  "$image" --api-port 18101 --port 13101 >"$smoke_log" 2>&1 &
app_pid=$!
cleanup() {
  kill -TERM -- "-$app_pid" 2>/dev/null || true
  wait "$app_pid" 2>/dev/null || true
}
trap cleanup EXIT

ready=0
for _attempt in $(seq 1 120); do
  if curl --noproxy '*' -fsS http://127.0.0.1:18101/health >"$health_out" 2>/dev/null \
    && curl --noproxy '*' -fsS http://127.0.0.1:13101/ >"$frontend_out" 2>/dev/null; then
    ready=1
    break
  fi
  sleep 0.5
done
if [ "$ready" -ne 1 ]; then
  tail -100 "$smoke_log"
  exit 1
fi
grep -Eq '"status"[[:space:]]*:[[:space:]]*"healthy"' "$health_out"
grep -Eiq '<!doctype html|<html' "$frontend_out"
