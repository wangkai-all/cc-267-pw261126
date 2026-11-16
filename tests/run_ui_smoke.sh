#!/usr/bin/env bash
# Boot the console against a throwaway DB and drive it with tests/ui_smoke.mjs.
set -u
cd "$(dirname "$0")/.."
PORT="${PORT:-8811}"
DB="$(mktemp -u /tmp/ui-smoke-XXXXXX.sqlite3)"
python3 app/server.py --port "$PORT" --db "$DB" >/tmp/ui-smoke-server.log 2>&1 &
SERVER_PID=$!
trap 'kill "$SERVER_PID" 2>/dev/null; rm -f "$DB" "$DB-wal" "$DB-shm"' EXIT

for _ in $(seq 1 100); do
  if python3 - "$PORT" <<'PY'
import sys, urllib.request
try:
    with urllib.request.urlopen(f"http://127.0.0.1:{sys.argv[1]}/api/bootstrap", timeout=1) as r:
        r.read(1)
except Exception:
    raise SystemExit(1)
PY
  then break; fi
  sleep 0.1
done

APP_BASE="http://127.0.0.1:$PORT" node tests/ui_smoke.mjs
status=$?
if [ "$status" -ne 0 ]; then
  echo "--- server log ---" >&2
  tail -30 /tmp/ui-smoke-server.log >&2
fi
exit "$status"
