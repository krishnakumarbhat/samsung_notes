#!/usr/bin/env bash
# Desktop button entry point: run the full sync/export pipeline in a terminal
# and keep the window open so the log stays readable afterwards.
# Reliable repeat clicks via flock; terminal logging status kept open only on tty.
set -uo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."

# Keep the launch lock and run log with this workspace.
HISTORY="$PWD/02_extracted_notes_pdf/ai_agend_history"
mkdir -p "$HISTORY"
exec 9>"$HISTORY/button.lock"
flock -n 9 || { echo "Another sync button invocation is running." >&2; exit 1; }

LOG="$HISTORY/orchestrator_run_$(date +%Y%m%d_%H%M%S).log"
exec > >(tee -a "$LOG") 2>&1

echo "Samsung Notes sync — $(date -Is)"
echo "workspace: $(pwd)"
echo

if bash "$SCRIPT_DIR/sync_and_export_notes.sh" "$@"; then
  rc=0
else
  rc=$?
fi

echo
if (( rc == 0 )); then
  echo "== finished cleanly =="
else
  echo "== finished with exit $rc — read the log above, then: =="
  echo "   tail -40 02_extracted_notes_pdf/ai_agend_history/incremental.log"
fi
echo

# Keep window open only when attached to a real TTY (desktop .desktop Terminal=true)
if [[ -t 0 ]]; then
  read -r -p "Press Enter to close this window..." _
fi
exit "$rc"
