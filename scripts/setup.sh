#!/usr/bin/env bash
# One-shot setup: venv-free install (uses the ambient python3), browsers, schema,
# test data.  Idempotent.
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${PYTHON:-python3}"
echo "==> Checking dependencies"
"$PY" - <<'EOF'
import importlib
missing = []
for mod in ("flask", "playwright", "openpyxl", "pypdf", "reportlab", "pytest"):
    try:
        importlib.import_module(mod)
    except ImportError:
        missing.append(mod)
if missing:
    raise SystemExit(f"缺少依赖：{', '.join(missing)}\n请先 pip install -r requirements.txt")
EOF

echo "==> Installing Playwright Chromium if needed"
"$PY" -m playwright install chromium >/dev/null 2>&1 || true

echo "==> Initialising databases"
PYTHONPATH=. "$PY" -m app.init_db

echo "==> Generating sample invoices"
PYTHONPATH=. "$PY" scripts/make_test_data.py --out data/samples

echo
echo "Setup complete."
echo "  1) 启动模拟海关系统 : python -m mock_system.app"
echo "  2) 启动报关控制台   : python -m app.web.console"
echo "  3) 打开 http://127.0.0.1:5050 导入 data/samples 下的发票"
