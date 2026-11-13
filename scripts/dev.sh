#!/usr/bin/env bash
# Start both services for local use. Ctrl-C stops both.
set -euo pipefail
cd "$(dirname "$0")/.."

export PYTHONPATH="$(pwd)"
export MOCK_PORT="${MOCK_PORT:-5055}"
export CONSOLE_PORT="${CONSOLE_PORT:-5050}"
export MOCK_BASE_URL="http://127.0.0.1:${MOCK_PORT}"

cleanup() {
  echo
  echo "==> Stopping services"
  kill "${MOCK_PID:-}" "${CONSOLE_PID:-}" 2>/dev/null || true
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "==> Starting mock customs system on :$MOCK_PORT"
python3 -m mock_system.app &
MOCK_PID=$!

sleep 2

echo "==> Starting broker console on :$CONSOLE_PORT"
python3 -m app.web.console &
CONSOLE_PID=$!

echo
echo "Mock system : http://127.0.0.1:$MOCK_PORT  (admin / admin123)"
echo "Console     : http://127.0.0.1:$CONSOLE_PORT"
echo "Press Ctrl-C to stop both."
wait
