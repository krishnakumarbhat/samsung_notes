#!/usr/bin/env bash
# Wrapper for desktop entry; resolve the workspace from this file, not a fixed mount path.
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
exec "$SCRIPT_DIR/sync_button.sh" "$@"
