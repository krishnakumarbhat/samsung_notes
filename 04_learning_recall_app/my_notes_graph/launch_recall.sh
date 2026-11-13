#!/usr/bin/env bash
# Open the Recall study dashboard from a desktop shortcut.
# Starts the loopback server when it is not already running, waits for the
# health endpoint, then opens the dashboard in the default browser.
set -uo pipefail

APP_DIR="$(cd -- "$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")" && pwd -P)"
URL="http://127.0.0.1:8787/"
HEALTH="${URL}api/health"
LOG_DIR="$APP_DIR/data"
LOG_FILE="$LOG_DIR/launcher.log"
mkdir -p "$LOG_DIR"

notify_error() {
  local message="$1"
  if command -v zenity >/dev/null 2>&1; then
    zenity --error --title="Recall Study" --text="$message" >/dev/null 2>&1 || true
  elif command -v notify-send >/dev/null 2>&1; then
    notify-send -u critical "Recall Study" "$message" >/dev/null 2>&1 || true
  fi
  printf '%s\n' "$message" >&2
}

open_dashboard() {
  local opener="${RECALL_BROWSER:-xdg-open}"
  # Detach so the launcher exits immediately and the browser survives it.
  setsid "$opener" "$URL" >>"$LOG_FILE" 2>&1 </dev/null &
  disown 2>/dev/null || true
}

printf '\n[%s] launcher invoked\n' "$(date -Is)" >>"$LOG_FILE"

if ! curl -fsS -m 3 "$HEALTH" >/dev/null 2>&1; then
  printf '[%s] server not running; starting it\n' "$(date -Is)" >>"$LOG_FILE"
  if ! bash "$APP_DIR/run_app.sh" >>"$LOG_FILE" 2>&1; then
    notify_error "Recall could not start. See $LOG_FILE"
    exit 1
  fi
fi

if ! curl -fsS -m 5 "$HEALTH" >/dev/null 2>&1; then
  notify_error "Recall started but never became healthy. See $LOG_FILE"
  exit 1
fi

open_dashboard
printf '[%s] opened %s\n' "$(date -Is)" "$URL" >>"$LOG_FILE"
exit 0
