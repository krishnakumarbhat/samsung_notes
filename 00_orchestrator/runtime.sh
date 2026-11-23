#!/usr/bin/env bash
# Source to resolve existing Python/package trees; never creates an environment.
SAM_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
SAM_NOTES_ROOT="$(dirname -- "$SAM_ROOT")/sam_notes"
mkdir -p "$SAM_NOTES_ROOT"
export SAM_NOTES_ROOT
RUN_TMP_CLEANER="$SAM_ROOT/scripts/run_tmp_cleanup.py"
if [[ -z "${SAM_RUN_TMP:-}" ]]; then
  SAM_RUN_TMP="$(mktemp -d "$SAM_NOTES_ROOT/_run_tmp_$$.XXXXXX")"
  SAM_RUN_TMP_OWNER_PID="$BASHPID"
  export SAM_RUN_TMP SAM_RUN_TMP_OWNER_PID
  SAM_RUN_TMP_OWNED=1
else
  SAM_RUN_TMP_OWNED=0
fi
# Hold an exclusive owner lock for this run's lifetime. A concurrent sweeper
# treats a held lock as proof of a live run and leaves the directory alone.
if (( SAM_RUN_TMP_OWNED )) && [[ ! -L "$SAM_RUN_TMP" ]]; then
  : > "$SAM_RUN_TMP/.owner.lock" 2>/dev/null || true
  exec {SAM_RUN_TMP_LOCK_FD}>"$SAM_RUN_TMP/.owner.lock" 2>/dev/null || SAM_RUN_TMP_LOCK_FD=""
  if [[ -n "${SAM_RUN_TMP_LOCK_FD:-}" ]]; then
    flock -n "$SAM_RUN_TMP_LOCK_FD" 2>/dev/null || true
  fi
fi
sam_cleanup_run_tmp() {
  if [[ "${SAM_RUN_TMP_OWNER_PID:-}" == "${BASHPID:-$$}" \
        && "${SAM_RUN_TMP:-}" == "$SAM_NOTES_ROOT"/_run_tmp_* \
        && ! -L "${SAM_RUN_TMP:-}" ]]; then
    if [[ -f "$RUN_TMP_CLEANER" && -n "${OPENCODE_PYTHON:-}" ]]; then
      "$OPENCODE_PYTHON" "$RUN_TMP_CLEANER" --notes-root "$SAM_NOTES_ROOT" \
        --own "$SAM_RUN_TMP" --own-pid "$BASHPID" >/dev/null 2>&1 || true
    else
      rm -rf -- "$SAM_RUN_TMP"
    fi
  fi
}
# Reclaim only abandoned runs; live pids, held locks and fresh trees survive.
sam_sweep_run_tmp() {
  [[ -f "$RUN_TMP_CLEANER" && -n "${OPENCODE_PYTHON:-}" ]] || return 0
  "$OPENCODE_PYTHON" "$RUN_TMP_CLEANER" --notes-root "$SAM_NOTES_ROOT" \
    --exclude-pid "$BASHPID" >/dev/null 2>&1 || true
  return 0
}
if (( SAM_RUN_TMP_OWNED )); then
  trap sam_cleanup_run_tmp EXIT
  trap 'sam_cleanup_run_tmp; exit 130' INT
  trap 'sam_cleanup_run_tmp; exit 143' TERM
fi
if [[ -z "${OPENCODE_PYTHON:-}" ]]; then
    for py in "${SAM_PYTHON:-}" "${SAM_VENV:+$SAM_VENV/bin/python3}" "$SAM_ROOT/.venv/bin/python3" "$SAM_ROOT/../.shared_venv/bin/python3" "$HOME/.cache/opencode_shared_venv/bin/python3" /usr/bin/python3; do
    if [[ -n "$py" && -x "$py" ]]; then export OPENCODE_PYTHON="$py"; break; fi
  done
fi
sam_sweep_run_tmp
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
