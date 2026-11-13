#!/usr/bin/env bash
# Host-side OCR launcher with the installed CUDA/Paddle native libraries.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"
GPU_SITE="$PROJECT_ROOT/../python_libs_gpu/site-packages"
CPU_SITE="$PROJECT_ROOT/../python_libs/site-packages"
OCR_SITE="$PROJECT_ROOT/05_environments_and_venvs/python_ocr_env/site-packages"

PYTHONPATH_PREFIX=()
for directory in "$GPU_SITE" "$OCR_SITE" "$CPU_SITE" "$HOME/.local/lib/python3.10/site-packages"; do
  [[ -d "$directory" ]] && PYTHONPATH_PREFIX+=("$directory")
done
PYTHONPATH="$(IFS=:; printf '%s' "${PYTHONPATH_PREFIX[*]}")${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONPATH

LD_PATHS=()
for directory in \
  "$GPU_SITE"/nvidia/cuda_runtime/lib \
  "$GPU_SITE"/nvidia/cuda_nvrtc/lib \
  "$GPU_SITE"/nvidia/cublas/lib \
  "$GPU_SITE"/nvidia/cudnn/lib \
  "$CPU_SITE"/nvidia/*/lib \
  "$OCR_SITE"/nvidia/*/lib \
  "$OCR_SITE"/*.libs; do
  [[ -d "$directory" ]] && LD_PATHS+=("$directory")
done
if ((${#LD_PATHS[@]})); then
  LD_LIBRARY_PATH="$(IFS=:; printf '%s' "${LD_PATHS[*]}")${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
  export LD_LIBRARY_PATH
fi

PYTHON="${OPENCODE_PYTHON:-${SAM_PYTHON:-$HOME/.cache/sam_win/venv/bin/python3}}"
[[ -x "$PYTHON" ]] || PYTHON=/usr/bin/python3
if [[ "${1:-}" == "--probe" ]]; then
  shift
  exec "$PYTHON" "$SCRIPT_DIR/paddle_cuda_probe.py" "$@"
fi
mkdir -p "$PROJECT_ROOT/02_extracted_notes_pdf/ai_agend_history"
exec 8>"$PROJECT_ROOT/02_extracted_notes_pdf/ai_agend_history/gpu_ocr.lock"
flock -x 8
exec "$PYTHON" "$SCRIPT_DIR/paddle_ocr_pdf_batch.py" "$@"
