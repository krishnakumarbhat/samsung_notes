#!/usr/bin/env bash
# Pack local-only assets into transferable, checksummed tar parts.
#
# The default bundle contains the exported notes, history/state, the embedding
# model and app data.  The 34 GB Windows VM disk is opt-in because it is slow
# to copy; recreate it with 01_vm_windows_sync/build-vm.sh or copy it with
# rsync when moving between machines.
#
# Examples:
#   bash scripts/pack_local_assets.sh
#   bash scripts/pack_local_assets.sh --include-vm --output /media/usb/sam_win
#   bash scripts/pack_local_assets.sh --notes-only
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
OUTPUT="${HOME}/sam_win_transfer/$(date +%Y%m%d-%H%M%S)"
PART_SIZE="95M"
INCLUDE_VM=0
NOTES_ONLY=0

usage() {
  cat <<'EOF'
Usage: bash scripts/pack_local_assets.sh [options]

  --output DIR     destination (default: ~/sam_win_transfer/<timestamp>)
  --include-vm     include 01_vm_windows_sync/vm/notes.qcow2 (~34 GB)
  --notes-only     only the original/compressed PDF corpus
  --part-size SIZE split size (default 95M; keep below 99 MB)
  -h, --help       show this help

The script streams tar into split, so it does not need a second 50 GB copy.
Each part is checksummed in SHA256SUMS and a RESTORE.txt is written.
EOF
}

while (($#)); do
  case "$1" in
    --output) OUTPUT="$2"; shift ;;
    --include-vm) INCLUDE_VM=1 ;;
    --notes-only) NOTES_ONLY=1 ;;
    --part-size) PART_SIZE="$2"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 2 ;;
  esac
  shift
done

mkdir -p "$OUTPUT"
OUTPUT="$(cd -- "$OUTPUT" && pwd -P)"
PARTS="$OUTPUT/parts"
mkdir -p "$PARTS"

PATHS=("02_extracted_notes_pdf/notes")
if ((!NOTES_ONLY)); then
  PATHS+=(
    "02_extracted_notes_pdf/ai_agend_history"
    "05_environments_and_venvs/models"
    "04_learning_recall_app/my_notes_graph/data"
    "04_learning_recall_app/notes_mirror"
    "02_extracted_notes_pdf/notes/export_manifest.json"
  )
fi
if ((INCLUDE_VM)); then
  PATHS+=("01_vm_windows_sync/vm")
fi

EXCLUDES=(
  "--exclude=*/__pycache__/*"
  "--exclude=*.pyc"
  "--exclude=*/page_cache/*"
  "--exclude=*/chroma/*"
  "--exclude=*.pid"
  "--exclude=*.sock"
  "--exclude=*.lock"
  "--exclude=*/ocr_visual_cache/*"
  "--exclude=*/preserved_originals/*/rollback*"
)

echo "Repository : $ROOT"
echo "Output     : $OUTPUT"
echo "Part size  : $PART_SIZE"
echo "Contents   : ${PATHS[*]}"
echo "Hashing    : $(command -v tar) | split"
echo

START=$(date +%s)
# This detailed inventory contains note filenames and stays in the transfer
# bundle; the committed manifest is aggregate-only.
source "$ROOT/00_orchestrator/runtime.sh"
"$OPENCODE_PYTHON" "$ROOT/scripts/assets_manifest.py" generate --hash
tar -C "$ROOT" -cf - "${EXCLUDES[@]}" "${PATHS[@]}" \
  | split -b "$PART_SIZE" -d -a 4 - "$PARTS/assets.part."

(
  cd "$PARTS"
  sha256sum assets.part.* > SHA256SUMS
)
cp "$ROOT/manifests/local_assets.private.json" "$OUTPUT/local_assets.json" 2>/dev/null || true
cp "$ROOT/scripts/restore_local_assets.sh" "$OUTPUT/restore_local_assets.sh" 2>/dev/null || true

cat > "$OUTPUT/RESTORE.txt" <<EOF
Samsung Notes local asset bundle
Created: $(date -Is)
Source : $ROOT

Restore on the target machine from the repository root:

  bash restore_local_assets.sh "$ROOT"

Manual restore:

  cd "$PARTS"
  sha256sum -c SHA256SUMS
  cat assets.part.* | tar -x -C "$ROOT"

Verify afterwards:

  python scripts/assets_manifest.py verify --hash

Notes
- The bundle excludes .env files, Python environments and regenerable caches.
- Include the VM disk only when you intentionally copied it; otherwise rebuild
  the Windows VM with 01_vm_windows_sync/build-vm.sh and the guest checklist.
- Keep the bundle on encrypted media: the PDFs contain personal notes.
EOF

SECONDS_ELAPSED=$(( $(date +%s) - START ))
echo
echo "Wrote $(find "$PARTS" -name 'assets.part.*' | wc -l) part(s) in $SECONDS_ELAPSED s"
du -sh "$OUTPUT" | awk '{print "Bundle size: " $1}'
echo "Restore with: bash $OUTPUT/restore_local_assets.sh $ROOT"
