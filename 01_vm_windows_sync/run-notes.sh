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
# Shared with sync_and_export_notes.sh: "<live orc pid> <sync|export>". Set as
# soon as the VM is available, before guest sync, so the monitor never owns a
# shutdown decision for a guest the pipeline is actively using.
EXPORT_FLAG="$PROJECT_DIR/temp/export_active.flag"
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
  # Ceiling aligned with orchestrator: 55 GB workspace cap, 15 GiB free floor.
  FREE_MB=$(df --output=avail -m "$PROJECT_DIR" 2>/dev/null | tail -1 | tr -d ' ')
  VM_MB=$(du -sm "$PROJECT_DIR" 2>/dev/null | cut -f1)
  VM_BYTES=$(du -s --block-size=1 "$PROJECT_DIR" 2>/dev/null | cut -f1)
  if [[ ! "${FREE_MB:-}" =~ ^[0-9]+$ || ! "${VM_BYTES:-}" =~ ^[0-9]+$ ]]; then
    echo "ABORT: unable to measure project disk usage/free space; refusing VM boot." >&2
    exit 1
  fi
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
  # Yield CPU to the host export/verification workload during the background run.
  setsid nohup nice -n 10 qemu-system-x86_64 "${ARGS[@]}" > "$DIR/qemu.log" 2>&1 < /dev/null &
  sleep 1
  VM_PID=$(find_vm_pid)
  if [[ -z "$VM_PID" ]]; then
    echo "QEMU did not remain running; inspect $DIR/qemu.log" >&2
    return 1
  fi
  echo "VM started (mem=${MEM}MB, vcpu=2). VNC: 127.0.0.1:5901"
  if ! monitor_disk; then
    echo "Could not start the disk monitor; requesting graceful VM shutdown." >&2
    stop || true
    return 1
  fi
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

# Disk counters. Fail closed: a failed/unparseable du or df is an error, never
# a "0 bytes used" reading that could silence the cap. The caller decides
# whether to treat an unmeasurable disk as a shutdown or a deferred warning.
disk_bytes() {
  local out
  out=$(du -s --block-size=1 "$1" 2>/dev/null | cut -f1) || return 1
  [[ "$out" =~ ^[0-9]+$ ]] || return 1
  printf '%s\n' "$out"
}

free_bytes() {
  local out
  out=$(df --output=avail --block-size=1 "$PROJECT_DIR" 2>/dev/null | tail -1 | tr -d '[:space:]') || return 1
  [[ "$out" =~ ^[0-9]+$ ]] || return 1
  printf '%s\n' "$out"
}

# Print "sync" or "export" only for a live pipeline that declared the phase.
# A stale flag from a dead PID (or an unparseable record) cannot suppress the
# monitor. Returns 1 when no pipeline owns the guest.
pipeline_phase() {
  local owner="" phase=""
  [ -f "$EXPORT_FLAG" ] || return 1
  read -r owner phase < "$EXPORT_FLAG" 2>/dev/null || return 1
  [[ "$owner" =~ ^[0-9]+$ ]] && kill -0 "$owner" 2>/dev/null || return 1
  [[ "$phase" == sync || "$phase" == export ]] || return 1
  printf '%s\n' "$phase"
}

# Returns 0=OK, 1=warning, 2=critical shutdown candidate.
# VM_DISK_ONLY=1 measures the qcow2 (early 54 GB warning, deferrable while the
# pipeline owns the guest); otherwise the whole workspace is measured against
# the hard 55 GB cap. During "export" both are deferred because transient
# staging lives in temp/ and the exporter guard owns the cap. During "sync"
# the qcow2 warning is advisory but the 55 GB workspace cap stays hard, so a
# long guest sync cannot exceed the cap. With no phase, both are hard.
check_disk() {
  local usage_bytes available_bytes scope shutdown_at
  if [[ "${VM_DISK_ONLY:-0}" == 1 ]]; then
    scope="vm-qcow2"; shutdown_at=54000000000
  else
    scope="workspace"; shutdown_at=55000000000
  fi
  local phase
  phase=$(pipeline_phase 2>/dev/null || true)
  local measured=1
  if [[ "${VM_DISK_ONLY:-0}" == 1 ]]; then
    if ! usage_bytes=$(disk_bytes "$DISK"); then measured=0; fi
  else
    if ! usage_bytes=$(disk_bytes "$PROJECT_DIR"); then measured=0; fi
  fi
  if ! available_bytes=$(free_bytes); then measured=0; fi

  local defer=0
  [[ "$phase" == export ]] && defer=1
  [[ "${VM_DISK_ONLY:-0}" == 1 && "$phase" == sync ]] && defer=1

  if (( measured == 0 )); then
    if (( defer )); then
      echo "DEFERRED[$scope]: measurement failed; exporter guard owns the cap (phase=${phase:-none})" >&2
      return 1
    fi
    echo "CRITICAL[$scope]: measurement failed (du/df); refusing to continue VM (phase=${phase:-none})" >&2
    return 2
  fi

  local floor=16106127360 warn_at=52000000000 critical=0
  if (( usage_bytes >= shutdown_at )) || (( available_bytes < floor )); then critical=1; fi
  if (( critical )) && (( defer )); then
    echo "DEFERRED[$scope]: phase=$phase usage=$usage_bytes free=$available_bytes (cap=55000000000); guest stays up" >&2
    return 1
  fi
  if (( critical )); then
    echo "CRITICAL[$scope]: phase=${phase:-none} usage=$usage_bytes free=$available_bytes (shutdown=$shutdown_at cap=55000000000 floor=15GiB)" >&2
    return 2
  fi
  if (( usage_bytes >= warn_at )); then
    echo "HEADROOM[$scope]: phase=${phase:-none} usage=$usage_bytes free=$available_bytes (approaching $shutdown_at)" >&2
    return 1
  fi
  echo "OK[$scope]: phase=${phase:-none} usage=$usage_bytes free=$available_bytes"
  return 0
}

# Background monitor: runs while the VM is alive. It checks qcow2 growth every
# 30 seconds and checks the whole workspace every 30 seconds during guest sync
# or while unowned. During export, the per-note exporter guard enforces the
# workspace cap because transient staging is part of that total. On critical,
# request graceful ACPI shutdown (no force-kill).
# Writes pid to $DIR/disk_monitor.pid with VM pid for stale detection.
monitor_disk() {
  local vm_pid
  vm_pid=$(find_vm_pid)
  if [[ -z "$vm_pid" ]]; then
    echo "VM not running; cannot start monitor" >&2
    return 1
  fi
  # Reuse a verified monitor for the same VM; never signal an unverified PID.
  if [ -f "$DIR/disk_monitor.pid" ]; then
    local old_mpid old_vm_pid
    read -r old_mpid old_vm_pid < "$DIR/disk_monitor.pid" 2>/dev/null || true
    if [[ -n "$old_mpid" && "$old_vm_pid" == "$vm_pid" ]] && kill -0 "$old_mpid" 2>/dev/null; then
      local old_cmd
      old_cmd=$(tr '\0' ' ' < "/proc/$old_mpid/cmdline" 2>/dev/null || true)
      if [[ "$old_cmd" == *"$SCRIPT_DIR/run-notes.sh monitor-worker $vm_pid"* ]]; then
        echo "Disk monitor already active (pid $old_mpid) for VM pid $vm_pid"
        return 0
      fi
      echo "Recorded monitor PID does not match expected command; leaving it untouched." >&2
    fi
  fi
  nohup bash "$SCRIPT_DIR/run-notes.sh" monitor-worker "$vm_pid" \
    > "$DIR/disk_monitor.log" 2>&1 < /dev/null &
  local mpid=$!
  # Store both monitor pid and VM pid for stale detection
  printf '%s %s\n' "$mpid" "$vm_pid" > "$DIR/disk_monitor.pid"
  echo "Disk monitor started (pid $mpid) for VM pid $vm_pid"
}

request_vm_shutdown() {
  local vm_pid="$1"
  # Only ever address the VM this monitor was started for, and only through
  # its QEMU monitor socket. Never signal a PID from a stale pidfile.
  if ! kill -0 "$vm_pid" 2>/dev/null; then
    echo "Monitored VM pid $vm_pid is gone; no shutdown request sent." >&2
    return 0
  fi
  echo "Disk safety threshold reached; requesting graceful VM shutdown for pid $vm_pid..." >&2
  if [[ -S "$MONITOR" ]]; then
    printf 'system_powerdown\n' | timeout 5 socat - unix-connect:"$MONITOR" >/dev/null 2>&1 || true
  fi
}

monitor_worker() {
  local vm_pid="$2" rc phase
  while kill -0 "$vm_pid" 2>/dev/null; do
    # Guard the guest's own growth every tick. This never includes transient
    # exporter staging under temp/, which lives outside the qcow2.
    rc=0
    VM_DISK_ONLY=1 check_disk >/dev/null || rc=$?
    if (( rc == 2 )); then request_vm_shutdown "$vm_pid"; exit 2; fi
    # Enforce the 55 GB workspace cap on every tick during guest sync and when
    # unowned. Only during the export phase is it deferred to the exporter
    # guard, which is the phase that creates transient staging in temp/.
    phase=$(pipeline_phase 2>/dev/null || true)
    if [[ "$phase" != export ]]; then
      rc=0
      check_disk >/dev/null || rc=$?
      if (( rc == 2 )); then request_vm_shutdown "$vm_pid"; exit 2; fi
    fi
    sleep 30
  done
}

# Stop the background disk monitor if running and belongs to current VM.
monitor_stop() {
  local mpid="" vm_pid=""
  if [ -f "$DIR/disk_monitor.pid" ]; then
    read -r mpid vm_pid < "$DIR/disk_monitor.pid" 2>/dev/null || true
  fi
  local current_vm_pid
  current_vm_pid=$(find_vm_pid)
  local stopped=0
  # Never signal a PID unless both its command and its VM are verified against
  # this checkout and this disk. An unverified PID is never signalled and its
  # pidfile is kept so a later owner can still inspect it.
  if [[ -n "$mpid" ]] && kill -0 "$mpid" 2>/dev/null; then
    local monitor_cmd
    monitor_cmd=$(tr '\0' ' ' < "/proc/$mpid/cmdline" 2>/dev/null || true)
    if [[ -n "$current_vm_pid" && "$vm_pid" == "$current_vm_pid" &&
          "$monitor_cmd" == *"$SCRIPT_DIR/run-notes.sh monitor-worker $vm_pid"* ]]; then
      kill "$mpid" 2>/dev/null || true
      wait "$mpid" 2>/dev/null || true
      stopped=1
      echo "Disk monitor stopped (pid $mpid)"
    else
      echo "Monitor/VM PID identity could not be verified; leaving pid=$mpid untouched." >&2
    fi
  fi
  if (( stopped )) || [[ -z "$current_vm_pid" || "$vm_pid" != "$current_vm_pid" ]]; then
    rm -f "$DIR/disk_monitor.pid"
  else
    echo "Kept disk_monitor.pid: it does not belong to this VM." >&2
  fi
}

case "${1:-status}" in
  start)        start "$@" ;;
  stop)         stop; monitor_stop ;;
  status)       PID=$(find_vm_pid); if [[ -n "$PID" ]]; then echo "running (pid $PID)"; else echo "stopped"; fi ;;
  vnc)          echo "Open with any VNC client:  127.0.0.1:5901   (e.g. remmina, or: vncviewer 127.0.0.1:5901)" ;;
  check-disk)   check_disk ;;
  monitor-disk) monitor_disk ;;
  monitor-worker) monitor_worker "$@" ;;
  monitor-stop) monitor_stop ;;
  *) echo "Usage: $0 {start [memMB]|stop|status|vnc|check-disk|monitor-disk|monitor-stop}"; exit 1 ;;
esac
