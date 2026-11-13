#!/usr/bin/env bash
# One-command local startup: mock customs system + console + optional worker.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
DATA="${DECLARANT_DATA_DIR:-$ROOT/data}"
mkdir -p "$DATA/logs" "$DATA/screenshots" "$DATA/uploads" "$DATA/exports" "$DATA/profiles"
export DECLARANT_DATA_DIR="$DATA"

command -v lsof >/dev/null 2>&1 || function lsof() { return 1; }
if lsof -ti :8099 >/dev/null 2>&1; then
  echo "[mock] 8099 已被占用，复用现有进程"
else
  echo "[mock] 启动 Mock 申报系统 http://127.0.0.1:8099"
  nohup python3 -m mock_system.app > "$DATA/logs/mock.log" 2>&1 &
  echo $! > "$DATA/mock.pid"
fi

if lsof -ti :8088 >/dev/null 2>&1; then
  echo "[web] 8088 已被占用，复用现有进程"
else
  echo "[web] 启动控制台 http://127.0.0.1:8088"
  nohup python3 -m declarant.web.app > "$DATA/logs/web.log" 2>&1 &
  echo $! > "$DATA/web.pid"
fi

if [[ "${1:-}" == "--worker" ]]; then
  echo "[worker] 启动后台录入进程"
  nohup python3 -m declarant.worker.engine > "$DATA/logs/worker.log" 2>&1 &
  echo $! > "$DATA/worker.pid"
fi

sleep 2
echo
echo "Mock 申报系统 : http://127.0.0.1:8099  (demo / demo123)"
echo "控制台       : http://127.0.0.1:8088"
echo "数据库       : $DATA/declarant.db"
echo "演示数据     : python3 scripts/seed_demo.py   # 首次可先导入 samples/ 里的发票"
echo "退出         : ./scripts/stop.sh"
