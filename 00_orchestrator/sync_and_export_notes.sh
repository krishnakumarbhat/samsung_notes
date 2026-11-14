#!/usr/bin/env bash
# Sync -> safe page delta -> owned-VM ACPI shutdown -> derived OCR -> local RAG -> UI.
set -euo pipefail
SCRIPT_PATH="$(readlink -f -- "${BASH_SOURCE[0]}")"
source "$(dirname -- "$SCRIPT_PATH")/runtime.sh"
RUN="$SAM_ROOT/01_vm_windows_sync/run-notes.sh"
EXPORTER="$SAM_ROOT/02_extracted_notes_pdf/script/incremental_export.py"
APP="$SAM_ROOT/04_learning_recall_app/my_notes_graph/run_app.sh"
HISTORY="$SAM_ROOT/02_extracted_notes_pdf/ai_agend_history"
mkdir -p "$HISTORY"
exec 9>"$HISTORY/orchestrator.lock"
flock -n 9 || { echo 'Another sync/export run owns the workspace lock.' >&2; exit 1; }

USED=$(du -s --block-size=1 "$SAM_ROOT" | cut -f1)
FREE=$(df --output=avail --block-size=1 "$SAM_ROOT" | tail -1 | tr -d ' ')
if (( USED >= 59000000000 || FREE < 16106127360 )); then
  echo "Disk guard refused: allocated=$USED free=$FREE (cap59GB, floor15GiB)." >&2
  exit 1
fi
"$OPENCODE_PYTHON" -c 'import pikepdf,fitz'
[[ -x "$HOME/.local/bin/sdocx-cli" ]] || { echo 'sdocx-cli missing' >&2; exit 1; }
DRY=0
for arg in "$@"; do [[ "$arg" != --dry-run ]] || DRY=1; done
STARTED_VM=0
cleanup() {
  status=$?
  trap - EXIT
  if (( STARTED_VM )); then
    bash "$RUN" stop || status=1
  fi
  exit "$status"
}
trap cleanup EXIT
if ! bash "$RUN" status | grep -q '^running'; then
  bash "$RUN" start 6144
  STARTED_VM=1
fi
bash "$SAM_ROOT/01_vm_windows_sync/guestsetup/sync_now.sh"
set +e
"$OPENCODE_PYTHON" -u "$EXPORTER" "$@" 2>&1 | tee -a "$HISTORY/incremental.log"
EXPORT_STATUS=${PIPESTATUS[0]}
set -e

# Release owned VM RAM before OCR/embeddings; never stop a pre-existing session.
if (( STARTED_VM )); then
  bash "$RUN" stop
  STARTED_VM=0
fi
POST_STATUS=0
if (( ! DRY )); then
  "$OPENCODE_PYTHON" -u "$EXPORTER" --postprocess || POST_STATUS=$?
  "$OPENCODE_PYTHON" "$EXPORTER" --manifest
  # Full corpus discovery, incremental hashes; no remote embedding endpoint.
  "$OPENCODE_PYTHON" - <<'PY'
import json
from pathlib import Path
from src.local_index import index_all
from src.config import PROJECT_ROOT
result = index_all()
path = PROJECT_ROOT / '02_extracted_notes_pdf/ai_agend_history/stage2_local_index_run.json'
path.write_text(json.dumps(result, indent=2))
print(json.dumps({key: result.get(key) for key in ('processed','indexed','unchanged','searchable','empty','unreadable')}))
PY
fi
bash "$APP"
curl --fail --silent --show-error --max-time 10 http://127.0.0.1:8787/api/health
echo
if (( EXPORT_STATUS || POST_STATUS )); then
  echo "Preserved previous files; export_status=$EXPORT_STATUS derived_status=$POST_STATUS. Inspect incremental.log." >&2
  exit 2
fi
