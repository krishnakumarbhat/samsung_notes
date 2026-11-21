#!/usr/bin/env bash
# Start or stop the local, loopback-only Socratic recall dashboard.
set -euo pipefail

APP_DIR="$(cd -- "$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")" && pwd -P)"
PROJECT_ROOT="$(cd -- "$APP_DIR/../.." && pwd -P)"
DATA_DIR="$APP_DIR/data"
PID_FILE="$DATA_DIR/recall_web.pid"
LOG_FILE="$APP_DIR/server.log"
source "$PROJECT_ROOT/00_orchestrator/runtime.sh"
PYTHON="$OPENCODE_PYTHON"
GPU_SITE="$PROJECT_ROOT/../python_libs_gpu/site-packages"
CPU_SITE="$PROJECT_ROOT/../python_libs/site-packages"

PYTHONPATH_PREFIX=()
for directory in "$GPU_SITE" "$CPU_SITE"; do
  [[ -d "$directory" ]] && PYTHONPATH_PREFIX+=("$directory")
done
PYTHONPATH="$(IFS=:; printf '%s' "${PYTHONPATH_PREFIX[*]}")"
export PYTHONPATH
export PYTHONNOUSERSITE=1
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 TOKENIZERS_PARALLELISM=false OMP_NUM_THREADS=2

LD_PATHS=()
for directory in "$GPU_SITE"/nvidia/*/lib "$CPU_SITE"/nvidia/*/lib "$GPU_SITE"/*.libs "$CPU_SITE"/*.libs; do
  [[ -d "$directory" ]] && LD_PATHS+=("$directory")
done
if ((${#LD_PATHS[@]})); then
  LD_LIBRARY_PATH="$(IFS=:; printf '%s' "${LD_PATHS[*]}")${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
  export LD_LIBRARY_PATH
fi

mkdir -p "$DATA_DIR"
if [[ "${1:-}" == "--stop" ]]; then
  if [[ -f "$PID_FILE" ]]; then
    pid=$(cat "$PID_FILE")
    if [[ "$pid" =~ ^[0-9]+$ ]] && ps -p "$pid" -o args= 2>/dev/null | grep -q 'uvicorn 00_main:app'; then
      kill -TERM "$pid" 2>/dev/null || true
      for _ in $(seq 1 20); do
        kill -0 "$pid" 2>/dev/null || break
        sleep 1
      done
      if kill -0 "$pid" 2>/dev/null && [[ "$(ps -p "$pid" -o stat=)" != Z* ]]; then
        echo "Recall is still gracefully draining requests (PID $pid); no forced kill." >&2
        exit 1
      fi
    fi
    rm -f "$PID_FILE"
  fi
  exit 0
fi

if curl -fsS http://127.0.0.1:8787/api/health >/dev/null 2>&1; then
  echo "Recall dashboard already running at http://127.0.0.1:8787"
  exit 0
fi

cd "$APP_DIR"
BIND_HOST="${RECALL_HOST:-127.0.0.1}"
setsid nohup env TMPDIR=/tmp/opencode "$PYTHON" -m uvicorn 00_main:app --host "$BIND_HOST" --port 8787 --workers 1 \
  >>"$LOG_FILE" 2>&1 </dev/null &
echo "$!" > "$PID_FILE"
for _ in $(seq 1 60); do
  if curl -fsS http://127.0.0.1:8787/api/health >/dev/null 2>&1; then
    echo "Recall dashboard ready at http://127.0.0.1:8787"
    if [[ "$BIND_HOST" == "0.0.0.0" ]]; then
      echo "LAN/phone URL: http://$(hostname -I | awk '{print $1}'):8787 (or use adb reverse)"
    fi
    exit 0
  fi
  sleep 1
done
echo "Recall dashboard failed to start; inspect $LOG_FILE" >&2
exit 1
