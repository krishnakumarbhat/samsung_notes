#!/usr/bin/env bash
# Host-side OCR launcher with the installed CUDA/Paddle native libraries.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"
source "$PROJECT_ROOT/00_orchestrator/runtime.sh"
PYTHON="$OPENCODE_PYTHON"
if [[ "${1:-}" == "--probe" ]]; then
  shift
  "$PYTHON" "$PROJECT_ROOT/scripts/doctor.py" --mode ocr
  exit $?
fi
mkdir -p "$PROJECT_ROOT/02_extracted_notes_pdf/ai_agend_history"
exec 8>"$PROJECT_ROOT/02_extracted_notes_pdf/ai_agend_history/gpu_ocr.lock"
flock -x 8
"$PYTHON" "$SCRIPT_DIR/paddle_ocr_pdf_batch.py" "$@"
