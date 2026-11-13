#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ -x "$ROOT_DIR/.venv/bin/streamlit" ]; then
  exec env -u PYTHONPATH "$ROOT_DIR/.venv/bin/streamlit" run "$ROOT_DIR/app.py"
fi

exec env -u PYTHONPATH streamlit run "$ROOT_DIR/app.py"
