#!/usr/bin/env bash
# Restore a bundle produced by scripts/pack_local_assets.sh.
#
# The script verifies every part before writing, refuses to overwrite a
# non-empty notes tree without --force, and never touches tracked repository
# files.
set -euo pipefail

BUNDLE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
TARGET="${1:-}"
FORCE=0

usage() {
  cat <<'EOF'
Usage: bash restore_local_assets.sh TARGET_ROOT [--force]

  TARGET_ROOT  repository root to restore into (for example /opt/sam_win)
  --force      allow restoring over an existing notes/ directory
EOF
}

while (($#)); do
  case "$1" in
    --force) FORCE=1 ;;
    -h|--help) usage; exit 0 ;;
    *) TARGET="$1" ;;
  esac
  shift
done

if [[ -z "$TARGET" ]]; then
  usage
  exit 2
fi
TARGET="$(cd -- "$TARGET" && pwd -P)"
if [[ ! -d "$TARGET/02_extracted_notes_pdf" ]]; then
  echo "Target does not look like the repo: $TARGET" >&2
  exit 2
fi

PARTS="$BUNDLE_DIR/parts"
if [[ ! -d "$PARTS" ]]; then
  echo "Missing parts directory: $PARTS" >&2
  exit 2
fi

if [[ -d "$TARGET/02_extracted_notes_pdf/notes" ]] && \
   [[ -n "$(find "$TARGET/02_extracted_notes_pdf/notes" -type f -print -quit 2>/dev/null)" ]] && \
   ((!FORCE)); then
  echo "Refusing to overwrite non-empty notes/ without --force." >&2
  exit 3
fi

echo "Verifying $(find "$PARTS" -name 'assets.part.*' | wc -l) part(s)..."
(
  cd "$PARTS"
  sha256sum -c SHA256SUMS
)

echo "Extracting into $TARGET ..."
cat "$PARTS"/assets.part.* | tar -x -C "$TARGET"

if [[ -f "$BUNDLE_DIR/local_assets.json" && -d "$TARGET/manifests" ]]; then
  cp "$BUNDLE_DIR/local_assets.json" "$TARGET/manifests/local_assets.private.json"
fi

echo "Restore complete. Verify with:"
echo "  cd \"$TARGET\" && python scripts/assets_manifest.py verify --output manifests/local_assets.private.json --hash"
