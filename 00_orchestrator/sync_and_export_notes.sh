#!/usr/bin/env bash
# Sync -> safe page delta -> owned-VM ACPI shutdown -> derived OCR -> local RAG -> UI.
# --dry-run: no VM boot, no guest sync, no index/app mutation.
set -euo pipefail
SCRIPT_PATH="$(readlink -f -- "${BASH_SOURCE[0]}")"
source "$(dirname -- "$SCRIPT_PATH")/runtime.sh"
RUN="$SAM_ROOT/01_vm_windows_sync/run-notes.sh"
EXPORTER="$SAM_ROOT/02_extracted_notes_pdf/script/incremental_export.py"
RECONCILER="$SAM_ROOT/02_extracted_notes_pdf/script/reconcile_tree.py"
APP="$SAM_ROOT/04_learning_recall_app/my_notes_graph/run_app.sh"
HISTORY="$SAM_ROOT/02_extracted_notes_pdf/ai_agend_history"
mkdir -p "$HISTORY"
# Phase handshake with run-notes.sh's monitor: "<our live PID> <sync|export>".
# Declared as soon as the VM is available, before guest sync. In "sync" the
# monitor keeps the hard 55 GB workspace cap enforced; in "export" it defers
# because transient staging is external and the exporter guard owns publication.
EXPORT_FLAG="$SAM_ROOT/temp/export_active.flag"

exec 9>"$HISTORY/orchestrator.lock"
flock -n 9 || { echo 'Another sync/export run owns the workspace lock.' >&2; exit 1; }
rm -f "$EXPORT_FLAG"   # clear any stale flag from a previous run
# With the workspace lock held, reclaim abandoned scratch from dead runs only.
if declare -F sam_sweep_run_tmp >/dev/null 2>&1; then sam_sweep_run_tmp; fi

# Preflight: GPU runtime, disk, dependencies — all before any VM start.
DRY=0
for arg in "$@"; do [[ "$arg" != --dry-run ]] || DRY=1; done

# Preflight disk guard. Never let a failed du/df abort the pipeline under
# `set -e`: failed/non-numeric readings are treated as a refusal, not a crash.
check_disk() {
  local used free
  used=$(du -s --block-size=1 "$SAM_ROOT" 2>/dev/null | cut -f1) || used=""
  free=$(df --output=avail --block-size=1 "$SAM_ROOT" 2>/dev/null | tail -1 | tr -d '[:space:]') || free=""
  if [[ ! "$used" =~ ^[0-9]+$ || ! "$free" =~ ^[0-9]+$ ]]; then
    echo "Disk guard refused: could not measure workspace allocation/free space." >&2
    return 1
  fi
  if (( used >= 55000000000 || free < 16106127360 )); then
    echo "Disk guard refused: allocated=$used free=$free (cap 55 GB, floor 15 GiB)." >&2
    return 1
  fi
  echo "Disk guard OK: allocated=$used free=$free"
  return 0
}

if (( ! DRY )); then
  # GPU is mandatory for OCR. Refuse to boot rather than silently using CPU.
  gpu_preflight || { echo "GPU preflight failed; refusing to boot VM or start export." >&2; exit 1; }
  check_disk || exit 1
  # Dependencies
  "$OPENCODE_PYTHON" -c 'import pikepdf,fitz,paddle,PIL,pypdf,numpy'
  [[ -x "$HOME/.local/bin/sdocx-cli" ]] || { echo 'sdocx-cli missing' >&2; exit 1; }
fi

STARTED_VM=0
FAILURES=0
INDEXED=0
index_valid_outputs() {
  if (( INDEXED )); then return 0; fi
  INDEXED=1
  echo "Indexing valid standalone compressed outputs..." >&2
  "$OPENCODE_PYTHON" - <<'PY' 2>&1 | tee -a "$HISTORY/incremental.log" || true
import json
from src.local_index import index_all
from src.config import PROJECT_ROOT
result = index_all()
path = PROJECT_ROOT / '02_extracted_notes_pdf/ai_agend_history/stage2_local_index_run.json'
path.write_text(json.dumps(result, indent=2))
print(json.dumps({key: result.get(key) for key in ('processed','indexed','unchanged','searchable','empty','unreadable')}))
PY
}
cleanup() {
  local status=$?
  trap - EXIT
  rm -f "$EXPORT_FLAG"   # release the monitor before touching the guest
  # Stop VM only if this pipeline started it
  if (( STARTED_VM )); then
    bash "$RUN" stop || status=1
  fi
  # On any early failure, index valid standalone outputs once.
  if (( status != 0 || FAILURES > 0 )); then index_valid_outputs; fi
  if declare -F sam_cleanup_run_tmp >/dev/null 2>&1; then sam_cleanup_run_tmp; fi
  exit "$status"
}
trap cleanup EXIT
trap 'echo "Interrupted; running cleanup..." >&2; exit 130' INT
trap 'echo "Terminated; running cleanup..." >&2; exit 143' TERM

if (( DRY )); then
  echo "== DRY-RUN: skipping VM, guest sync, export, postprocess, index, app =="
  exit 0
fi

if ! bash "$RUN" status | grep -q '^running'; then
  bash "$RUN" start 6144
  STARTED_VM=1
fi

# Claim the guest before any sync happens. Without this, a long guest sync
# could reach the workspace cap while the monitor still considered the VM
# unowned and shut it down before the export phase was ever declared.
mkdir -p "$(dirname -- "$EXPORT_FLAG")"
printf '%s sync\n' "$$" > "$EXPORT_FLAG"

bash "$SAM_ROOT/01_vm_windows_sync/guestsetup/sync_now.sh"

# Phase 0: after sync quiescence but before the exporter, reconcile the local
# PDF tree and path-bearing state against the fresh, read-only guest inventory.
# ONE guest query writes an offline snapshot; the dry-run plan is validated,
# and --apply runs automatically ONLY when that plan passes, replaying from the
# snapshot so the guest is never queried twice. A refusal is advisory: it must
# not mutate anything or block an otherwise valid export, but it is logged.
PRIVATE_STATE="${SAM_NOTES_ROOT:-$(dirname -- "$SAM_ROOT")/sam_notes}/private_state"
mkdir -p "$PRIVATE_STATE"
chmod 700 "$PRIVATE_STATE" 2>/dev/null || true
RECONCILE_SNAPSHOT="$PRIVATE_STATE/reconcile_inventory.json"
set +e
"$OPENCODE_PYTHON" -u "$RECONCILER" --inventory-snapshot "$RECONCILE_SNAPSHOT" 2>&1 | tee -a "$HISTORY/reconcile_tree.log"
RECONCILE_STATUS=${PIPESTATUS[0]}
if (( RECONCILE_STATUS == 0 )); then
  "$OPENCODE_PYTHON" -u "$RECONCILER" --apply --inventory-json "$RECONCILE_SNAPSHOT" 2>&1 | tee -a "$HISTORY/reconcile_tree.log"
  RECONCILE_APPLY_STATUS=${PIPESTATUS[0]}
else
  RECONCILE_APPLY_STATUS=2
fi
set -e
if (( RECONCILE_STATUS != 0 || RECONCILE_APPLY_STATUS != 0 )); then
  echo "Tree reconciliation did not pass/apply cleanly (plan=$RECONCILE_STATUS apply=$RECONCILE_APPLY_STATUS); inspect reconcile_tree.log." >&2
  FAILURES=$((FAILURES+1))
fi

# Switch to export phase: monitor defers to the exporter guard, which enforces
# the workspace cap and free-space floor for external per-note staging.
printf '%s export\n' "$$" > "$EXPORT_FLAG"
set +e
"$OPENCODE_PYTHON" -u "$EXPORTER" "$@" 2>&1 | tee -a "$HISTORY/incremental.log"
EXPORT_STATUS=${PIPESTATUS[0]}
set -e
rm -f "$EXPORT_FLAG"

# Release owned VM RAM before OCR/embeddings; never stop a pre-existing session.
if (( STARTED_VM )); then
  bash "$RUN" stop
  STARTED_VM=0
fi

POST_STATUS=0
"$OPENCODE_PYTHON" -u "$EXPORTER" --postprocess || { POST_STATUS=$?; FAILURES=$((FAILURES+1)); }
"$OPENCODE_PYTHON" "$EXPORTER" --manifest || { FAILURES=$((FAILURES+1)); }

# Full corpus discovery, incremental hashes; no remote embedding endpoint.
index_valid_outputs

# Do not pass the orchestrator's flock descriptor into the long-lived Recall
# server. Otherwise uvicorn keeps FD 9 open after this script exits and every
# later desktop sync falsely reports that another run owns the workspace lock.
bash "$APP" 9>&-
curl --fail --silent --show-error --max-time 10 http://127.0.0.1:8787/api/health
echo

if (( EXPORT_STATUS || POST_STATUS || FAILURES > 0 )); then
  echo "Preserved previous files; export_status=$EXPORT_STATUS derived_status=$POST_STATUS failures=$FAILURES. Inspect incremental.log." >&2
  exit 2
fi
