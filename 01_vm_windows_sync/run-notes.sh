#!/usr/bin/env bash
# run-notes.sh — headless start/stop of the Samsung Notes Windows VM.
# Usage:
#   ./run-notes.sh start   [mem MB, default 6144]
#   ./run-notes.sh stop            (graceful ACPI shutdown)
#   ./run-notes.sh vnc             (print VNC viewer command to watch the screen)
#   ./run-notes.sh status
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"
DIR="$SCRIPT_DIR/vm"
DISK="$DIR/notes.qcow2"
ISO="$DIR/notes-boot.iso"          # only used on first install
PIDFILE="$DIR/qemu.pid"
MONITOR="$DIR/monitor.sock"
VNC_DISPLAY=1                       # :1 -> port 5901

find_vm_pid() {
  local pid=""
  if [ -f "$PIDFILE" ]; then
    pid=$(cat "$PIDFILE" 2>/dev/null || true)
  fi
  if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
    if ps -p "$pid" -o args= 2>/dev/null | grep -Fq "$DISK"; then
      printf '%s\n' "$pid"
      return 0
    fi
  fi
  pgrep -f "[q]emu-system-x86_64.*${DISK}" | head -1 || true
}

start() {
  [ -d "$DIR" ] || { echo "No $DIR — run build-vm.sh first"; exit 1; }
  EXISTING_PID=$(find_vm_pid)
  if [[ -n "$EXISTING_PID" ]]; then
    printf '%s\n' "$EXISTING_PID" > "$PIDFILE"
    echo "VM already running (pid $EXISTING_PID)"
    exit 0
  fi
  # Disk guardrails apply to the complete project directory, including its OCR environment.
  FREE_MB=$(df --output=avail -m "$PROJECT_DIR" 2>/dev/null | tail -1 | tr -d ' ')
  VM_MB=$(du -sm "$PROJECT_DIR" 2>/dev/null | cut -f1)
  VM_BYTES=$(du -s --block-size=1 "$PROJECT_DIR" 2>/dev/null | cut -f1)
  if [ "${FREE_MB:-0}" -lt 15360 ]; then
    notify-send -u critical "VM boot aborted" "projecteo has ${FREE_MB}MB free (<15GB)" 2>/dev/null || true
    echo "ABORT: projecteo free ${FREE_MB}MB < 15GB"; exit 1
  fi
  if [ "${VM_BYTES:-0}" -ge 55000000000 ]; then
    notify-send -u critical "VM boot aborted" "VM dir ${VM_MB}MB exceeds 55GB ceiling" 2>/dev/null || true
    echo "ABORT: VM dir ${VM_MB}MB > 55GB ceiling"; exit 1
  fi
  MEM="${2:-6144}"
  ARGS=(
    -accel kvm -machine q35 -cpu host -smp 2 -m "$MEM"
    -drive if=pflash,format=raw,readonly=on,file=/usr/share/OVMF/OVMF_CODE_4M.fd
    -drive if=pflash,format=raw,file="$DIR/OVMF_VARS.fd"
    # Samsung Galaxy Book spoof: Samsung Notes sync checks OEM strings
    -smbios type=1,manufacturer="Samsung Electronics",product="NP960XFG-KC4UK",version="P08CFG",serial="SPOOFEDNOTES1"
    -smbios type=2,manufacturer="Samsung Electronics",product="NP960XFG-KC4UK"
    -device ich9-ahci,id=ahci
    -drive file="$DISK",format=qcow2,if=none,id=disk,media=disk,discard=on
    -device ide-hd,drive=disk,bus=ahci.0
    -netdev user,id=n1,hostfwd=tcp:127.0.0.1:2222-:22 -device e1000,netdev=n1
    -device qxl-vga -spice port=5930,addr=127.0.0.1,disable-ticketing=on -display none
    -vnc 127.0.0.1:$VNC_DISPLAY
    -monitor unix:"$MONITOR",server,nowait
    -qmp unix:"$DIR/qmp.sock",server,nowait
    -pidfile "$PIDFILE"
    -usb -device usb-tablet
  )
  if [ ! -f "$DIR/.installed" ]; then
    [ -f "$ISO" ] || { echo "Boot ISO missing: $ISO"; exit 1; }
    ARGS+=(-drive file="$ISO",format=raw,if=none,id=cdrom,media=cdrom -device ide-cd,drive=cdrom,bus=ahci.1 -boot order=dc)
    echo "== FIRST INSTALL BOOT (unattended). Watch: ./run-notes.sh vnc"
  fi
  setsid nohup qemu-system-x86_64 "${ARGS[@]}" > "$DIR/qemu.log" 2>&1 < /dev/null &
  sleep 1
  echo "VM started (mem=${MEM}MB, vcpu=2). VNC: 127.0.0.1:5901"
}

stop() {
  PID=$(find_vm_pid)
  if [[ -z "$PID" ]]; then
    rm -f "$PIDFILE"
    echo "Not running"
    return 0
  fi
  if [[ ! -S "$MONITOR" ]]; then
    echo "Cannot request graceful shutdown: QEMU monitor socket is missing; VM pid=$PID remains untouched." >&2
    return 1
  fi
  printf 'system_powerdown\n' | timeout 5 socat - unix-connect:"$MONITOR" >/dev/null 2>&1 || true
  echo "ACPI shutdown requested for VM pid=$PID; waiting up to 180 seconds."
  for _ in $(seq 1 180); do
    if ! kill -0 "$PID" 2>/dev/null; then
      rm -f "$PIDFILE"
      echo "VM shut down gracefully."
      return 0
    fi
    sleep 1
  done
  notify-send -u critical "VM shutdown timed out" "QEMU pid $PID is still running; it was not force-killed." 2>/dev/null || true
  echo "Graceful shutdown timed out; QEMU pid=$PID is still running and was not force-killed." >&2
  return 1
}

case "${1:-status}" in
  start)  start "$@" ;;
  stop)   stop ;;
  status) PID=$(find_vm_pid); if [[ -n "$PID" ]]; then echo "running (pid $PID)"; else echo "stopped"; fi ;;
  vnc)    echo "Open with any VNC client:  127.0.0.1:5901   (e.g. remmina, or: vncviewer 127.0.0.1:5901)" ;;
  *) echo "Usage: $0 {start [memMB]|stop|status|vnc}"; exit 1 ;;
esac
