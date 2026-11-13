#!/usr/bin/env bash
# Install (or refresh) the click-to-open Recall shortcut on the Desktop.
# Creates an executable .desktop entry plus a .sh fallback, and marks both as
# trusted so GNOME / DING launches them instead of opening a text editor.
set -euo pipefail

APP_DIR="$(cd -- "$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")" && pwd -P)"
DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || true)"
DESKTOP_DIR="${DESKTOP_DIR:-$HOME/Desktop}"
ENTRY="$DESKTOP_DIR/Recall Study.desktop"
FALLBACK="$DESKTOP_DIR/Recall Study.sh"
ICON="$APP_DIR/static/icon-192.png"
LAUNCHER="$APP_DIR/launch_recall.sh"

mkdir -p "$DESKTOP_DIR"
chmod +x "$LAUNCHER" "$APP_DIR/run_app.sh"

cat >"$ENTRY" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=Recall Study
GenericName=Study Dashboard
Comment=Start the local Recall server and open the dashboard
Exec=$LAUNCHER
Icon=$ICON
Terminal=false
StartupNotify=false
Categories=Education;
Keywords=recall;study;flashcards;questions;sudoku;
EOF

cat >"$FALLBACK" <<EOF
#!/usr/bin/env bash
# Fallback launcher for desktops that refuse to run .desktop files.
exec "$LAUNCHER" "\$@"
EOF

chmod +x "$ENTRY" "$FALLBACK"
for file in "$ENTRY" "$FALLBACK"; do
  gio set "$file" metadata::trusted true >/dev/null 2>&1 || true
done

printf 'Installed:\n  %s\n  %s\n' "$ENTRY" "$FALLBACK"
