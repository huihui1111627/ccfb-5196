#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
DATA="${DECLARANT_DATA_DIR:-$(pwd)/data}"
for name in worker web mock; do
  if [[ -f "$DATA/$name.pid" ]]; then
    pid="$(cat "$DATA/$name.pid")"
    if kill -0 "$pid" 2>/dev/null; then kill "$pid" 2>/dev/null || true; echo "停止 $name ($pid)"; fi
    rm -f "$DATA/$name.pid"
  fi
done
