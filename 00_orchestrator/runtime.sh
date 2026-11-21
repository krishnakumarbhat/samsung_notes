#!/usr/bin/env bash
# Source to resolve existing Python/package trees; never creates an environment.
SAM_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
SAM_NOTES_ROOT="$(dirname -- "$SAM_ROOT")/sam_notes"
mkdir -p "$SAM_NOTES_ROOT"
export SAM_NOTES_ROOT
if [[ -z "${SAM_RUN_TMP:-}" ]]; then
  SAM_RUN_TMP="$(mktemp -d "$SAM_NOTES_ROOT/_run_tmp_$$.XXXXXX")"
  SAM_RUN_TMP_OWNER_PID="$BASHPID"
  export SAM_RUN_TMP SAM_RUN_TMP_OWNER_PID
  SAM_RUN_TMP_OWNED=1
else
  SAM_RUN_TMP_OWNED=0
fi
sam_cleanup_run_tmp() {
  if [[ "${SAM_RUN_TMP_OWNER_PID:-}" == "${BASHPID:-$$}" \
        && "${SAM_RUN_TMP:-}" == "$SAM_NOTES_ROOT"/_run_tmp_* ]]; then
    rm -rf -- "$SAM_RUN_TMP"
  fi
}
if (( SAM_RUN_TMP_OWNED )); then trap sam_cleanup_run_tmp EXIT; fi
if [[ -z "${OPENCODE_PYTHON:-}" ]]; then
    for py in "${SAM_PYTHON:-}" "${SAM_VENV:+$SAM_VENV/bin/python3}" "$SAM_ROOT/.venv/bin/python3" "$SAM_ROOT/../.shared_venv/bin/python3" "$HOME/.cache/opencode_shared_venv/bin/python3" /usr/bin/python3; do
    if [[ -n "$py" && -x "$py" ]]; then export OPENCODE_PYTHON="$py"; break; fi
  done
fi
GPU_SITE="$SAM_ROOT/../python_libs_gpu/site-packages"
CPU_SITE="$SAM_ROOT/../python_libs/site-packages"
export PYTHONNOUSERSITE=1
export PYTHONPATH="$SAM_ROOT/04_learning_recall_app/my_notes_graph:$SAM_ROOT/02_extracted_notes_pdf/script:$SAM_ROOT/03_rtx_compression_ocr:$GPU_SITE:$CPU_SITE"
LD_PATHS=()
for directory in "$GPU_SITE"/nvidia/*/lib "$CPU_SITE"/nvidia/*/lib "$GPU_SITE"/*.libs "$CPU_SITE"/*.libs; do
  [[ ! -d "$directory" ]] || LD_PATHS+=("$directory")
done
export LD_LIBRARY_PATH="$(IFS=:; printf '%s' "${LD_PATHS[*]}")${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True

# Ephemeral staging is external to the workspace and removed by the owning shell.
# The content-addressed page-cache remains inside the workspace by design.
export TMPDIR="$SAM_RUN_TMP"
export PADDLE_PDX_CACHE_HOME="$TMPDIR/paddlex"
export PADDLE_HOME="$TMPDIR/paddle"
export MPLCONFIGDIR="$TMPDIR/matplotlib"
export CUDA_CACHE_PATH="$TMPDIR/cuda-cache"
mkdir -p "$PADDLE_PDX_CACHE_HOME" "$PADDLE_HOME" "$MPLCONFIGDIR" "$CUDA_CACHE_PATH"
if [[ -n "${SN_EXPORT_WORK:-}" && "$SN_EXPORT_WORK" != "$TMPDIR" ]]; then
  echo "ERROR: SN_EXPORT_WORK override not allowed; must be $TMPDIR" >&2
  return 1 2>/dev/null || exit 1
fi
export SN_EXPORT_WORK="$TMPDIR"
if [[ -n "${SN_PAGE_CACHE_DIR:-}" && "$SN_PAGE_CACHE_DIR" != "$SAM_ROOT/02_extracted_notes_pdf/ai_agend_history/page_cache" ]]; then
  echo "ERROR: SN_PAGE_CACHE_DIR override not allowed; must be $SAM_ROOT/02_extracted_notes_pdf/ai_agend_history/page_cache" >&2
  return 1 2>/dev/null || exit 1
fi
export SN_PAGE_CACHE_DIR="$SAM_ROOT/02_extracted_notes_pdf/ai_agend_history/page_cache"
mkdir -p "$SN_PAGE_CACHE_DIR"

# GPU preflight helper for orchestrators. The python module fails closed and
# pins the tensor to CUDAPlace(0), so a sticky CPU default cannot pass it.
gpu_preflight() {
  "$OPENCODE_PYTHON" "$SAM_ROOT/00_orchestrator/gpu_preflight.py"
}
export -f gpu_preflight
