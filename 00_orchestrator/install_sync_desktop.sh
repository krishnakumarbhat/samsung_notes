#!/usr/bin/env bash
# Install a desktop launcher that resolves this checkout dynamically.
# Nothing is launched here. DESKTOP_DIR can be overridden for isolated tests.
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"
DESKTOP_DIR="${DESKTOP_DIR:-${XDG_DESKTOP_DIR:-$HOME/Desktop}}"
DRY_RUN=0
for arg in "$@"; do [[ "$arg" != --dry-run ]] || DRY_RUN=1; done
LAUNCHER="$SCRIPT_DIR/launch_sync.sh"
[[ -x "$LAUNCHER" ]] || { echo "Missing executable launcher: $LAUNCHER" >&2; exit 1; }
mkdir -p "$DESKTOP_DIR"
TARGET="$DESKTOP_DIR/Samsung Notes Sync.desktop"
TMP=$(mktemp --suffix=.desktop "${TMPDIR:-/tmp}/samsung-notes-desktop.XXXXXX")
trap 'rm -f "$TMP"' EXIT
cat >"$TMP" <<EOF
[Desktop Entry]
Type=Application
Name=Samsung Notes Sync
Comment=Sync Samsung Notes from the Windows VM, export PDFs, run OCR, launch Recall
Exec="$LAUNCHER"
Icon=org.gnome.Notes
Terminal=true
StartupNotify=true
Categories=Utility;
Keywords=samsung;notes;sync;ocr;export;
EOF
if command -v desktop-file-validate >/dev/null 2>&1; then
  desktop-file-validate "$TMP"
fi
if (( DRY_RUN )); then
  echo "DRY-RUN: would install $TARGET with Exec=\"$LAUNCHER\"" >&2
  cat "$TMP"
  exit 0
fi
mv -f "$TMP" "$TARGET"
chmod 755 "$TARGET"
trap - EXIT
echo "Installed $TARGET -> $LAUNCHER (root: $ROOT)"
