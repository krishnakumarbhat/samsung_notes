#!/usr/bin/env bash
# Source to resolve existing Python/package trees; never creates an environment.
SAM_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
if [[ -z "${OPENCODE_PYTHON:-}" ]]; then
  for py in "${SAM_PYTHON:-}" "${SAM_VENV:+$SAM_VENV/bin/python3}" "$HOME/.cache/sam_win/venv/bin/python3" "$SAM_ROOT/.venv/bin/python3" "$SAM_ROOT/../.shared_venv/bin/python3" "$HOME/.cache/opencode_shared_venv/bin/python3" /usr/bin/python3; do
    if [[ -n "$py" && -x "$py" ]]; then export OPENCODE_PYTHON="$py"; break; fi
  done
fi
APP_SITE="$SAM_ROOT/04_learning_recall_app/my_notes_graph/.venv/lib/python3.10/site-packages"
for candidate in "$SAM_ROOT"/04_learning_recall_app/my_notes_graph/.venv/lib/python*/site-packages; do
  [[ -d "$candidate" ]] && APP_SITE="$candidate"
done
OCR_SITE="$SAM_ROOT/05_environments_and_venvs/python_ocr_env/site-packages"
GPU_SITE="$SAM_ROOT/../python_libs_gpu/site-packages"
CPU_SITE="$SAM_ROOT/../python_libs/site-packages"
EXTRA_SITE="${SAM_EXTRA_SITE_PACKAGES:-}"
export PYTHONPATH="$SAM_ROOT/04_learning_recall_app/my_notes_graph:$SAM_ROOT/02_extracted_notes_pdf/script:$SAM_ROOT/03_rtx_compression_ocr:$APP_SITE:$GPU_SITE:$OCR_SITE:$CPU_SITE:$HOME/.local/lib/python3.10/site-packages${EXTRA_SITE:+:$EXTRA_SITE}${PYTHONPATH:+:$PYTHONPATH}"
LD_PATHS=()
for directory in "$GPU_SITE"/nvidia/*/lib "$CPU_SITE"/nvidia/*/lib "$OCR_SITE"/nvidia/*/lib "$OCR_SITE"/*.libs; do
  [[ ! -d "$directory" ]] || LD_PATHS+=("$directory")
done
export LD_LIBRARY_PATH="$(IFS=:; printf '%s' "${LD_PATHS[*]}")${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True
