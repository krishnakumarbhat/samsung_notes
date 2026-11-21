#!/usr/bin/env bash
# Ensure Samsung Notes is ready, invoke Sync now in its user session, then
# wait for read-only database and wdoc quiescence checks.
set -euo pipefail

VM_USER="notes"
VM_HOST="127.0.0.1"
VM_PORT=2222
GUEST_HOME='C:/Users/notes'
SCRIPTS_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_ROOT="$(cd -- "$SCRIPTS_DIR/../.." && pwd -P)"
NATIVE_ENV_FILE="$HOME/.config/sam_win/.env"
LEGACY_ENV_FILE="$PROJECT_ROOT/02_extracted_notes_pdf/.env"

read_guest_password() {
  local env_file="$1"
  [[ -f "$env_file" ]] || return 0
  /usr/bin/python3 - "$env_file" <<'PY'
from pathlib import Path
import sys

for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    key, separator, value = line.partition("=")
    if separator and key.strip() in {"SAMSUNG_NOTES_PASSWORD", "SAM_PASSWORD"} and value.strip().strip("\"'"):
        print(line.partition("=")[2].strip().strip("\"'"))
        break
PY
}

if [[ -z "${SAMSUNG_NOTES_PASSWORD:-}" ]]; then
  SAMSUNG_NOTES_PASSWORD="$(read_guest_password "$NATIVE_ENV_FILE")"
fi
if [[ -z "${SAMSUNG_NOTES_PASSWORD:-}" ]]; then
  SAMSUNG_NOTES_PASSWORD="$(read_guest_password "$LEGACY_ENV_FILE")"
fi
if [[ -z "${SAMSUNG_NOTES_PASSWORD:-}" ]]; then
  echo "Missing SAMSUNG_NOTES_PASSWORD in ~/.config/sam_win/.env, $LEGACY_ENV_FILE, or environment." >&2
  exit 2
fi
export SSHPASS="$SAMSUNG_NOTES_PASSWORD"
unset SAMSUNG_NOTES_PASSWORD

CONTROL_DIR="/tmp/opencode/sam_win_ssh"
mkdir -p "$CONTROL_DIR"
chmod 700 "$CONTROL_DIR" 2>/dev/null || true
for control_socket in "$CONTROL_DIR"/*; do
  [[ -S "$control_socket" ]] || continue
  if ! ssh -O check -p "$VM_PORT" -o "ControlPath=$control_socket" \
      "$VM_USER@$VM_HOST" >/dev/null 2>&1; then
    rm -f -- "$control_socket"
  fi
done
SSH_OPTIONS=(-o PubkeyAuthentication=no -o PreferredAuthentications=password
  -o StrictHostKeyChecking=no -o ConnectTimeout=10
  -o ControlMaster=auto -o ControlPersist=120
  -o "ControlPath=$CONTROL_DIR/%C")
ssh_vm() {
  sshpass -e ssh -p "$VM_PORT" "${SSH_OPTIONS[@]}" "$VM_USER@$VM_HOST" "$@"
}
scp_vm() {
  sshpass -e scp -O -P "$VM_PORT" "${SSH_OPTIONS[@]}" "$@"
}

echo "==[ $(date -Is) ]== waiting for Windows SSH"
connected=0
for attempt in $(seq 1 60); do
  if ssh_vm 'echo SSH_READY' 2>/dev/null | grep -q SSH_READY; then
    connected=1
    break
  fi
  sleep 2
done
if [[ "$connected" != 1 ]]; then
  echo "Windows VM SSH did not become ready within 120 seconds." >&2
  exit 3
fi

echo "Uploading session-safe sync helpers."
scp_vm \
  "$SCRIPTS_DIR/click_sync.ps1" \
  "$SCRIPTS_DIR/clicksync_task.xml" \
  "$SCRIPTS_DIR/launchnotes_task.xml" \
  "$SCRIPTS_DIR/pre_sync.ps1" \
  "$SCRIPTS_DIR/wait_sync_quiescence.py" \
  "$VM_USER@$VM_HOST:$GUEST_HOME/"

echo "Ensuring the tray and interactive Samsung Notes window are ready."
ssh_vm 'powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\notes\pre_sync.ps1'
echo "==[ $(date -Is) ]== sync completed and reached quiescence"
