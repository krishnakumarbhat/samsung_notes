#!/usr/bin/env bash
# Create a blank Windows VM disk and OVMF variable store, then link the ISO.
#
# The Windows installation itself is interactive: create the user `notes`,
# enable OpenSSH Server, install Samsung Notes and accept its terms manually.
# See README.md for the full guest checklist.
#
# Example:
#   WINDOWS_ISO=~/Downloads/Win11_24H2.iso bash build-vm.sh --size 80G
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
DIR="$SCRIPT_DIR/vm"
ISO="${WINDOWS_ISO:-}"
SIZE="80G"
FORCE=0

usage() {
  cat <<'EOF'
Usage: bash build-vm.sh [--iso PATH] [--size 80G] [--force]

  --iso PATH   Windows 11 ISO (or set WINDOWS_ISO)
  --size SIZE  qcow2 virtual size (default 80G)
  --force      replace an existing disk and OVMF vars (destructive)
EOF
}

while (($#)); do
  case "$1" in
    --iso) ISO="$2"; shift ;;
    --size) SIZE="$2"; shift ;;
    --force) FORCE=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 2 ;;
  esac
  shift
done

if ! command -v qemu-img >/dev/null 2>&1; then
  echo "qemu-img is required (install qemu-utils)." >&2
  exit 2
fi
if ! command -v qemu-system-x86_64 >/dev/null 2>&1; then
  echo "qemu-system-x86_64 is required (install qemu-system-x86)." >&2
  exit 2
fi

OVMF_CODE="/usr/share/OVMF/OVMF_CODE_4M.fd"
OVMF_VARS="/usr/share/OVMF/OVMF_VARS_4M.fd"
for candidate in /usr/share/OVMF/OVMF_CODE_4M.fd /usr/share/OVMF/OVMF_CODE.fd; do
  [[ -f "$candidate" ]] && OVMF_CODE="$candidate" && break
done
for candidate in /usr/share/OVMF/OVMF_VARS_4M.fd /usr/share/OVMF/OVMF_VARS.fd; do
  [[ -f "$candidate" ]] && OVMF_VARS="$candidate" && break
done
if [[ ! -f "$OVMF_CODE" || ! -f "$OVMF_VARS" ]]; then
  echo "OVMF firmware not found; install the ovmf package." >&2
  exit 2
fi

mkdir -p "$DIR"

if [[ -f "$DIR/notes.qcow2" && "$FORCE" != 1 ]]; then
  echo "Refusing to replace existing $DIR/notes.qcow2 without --force." >&2
  exit 3
fi
if [[ ! -f "$ISO" ]]; then
  echo "Windows ISO not found: ${ISO:-<unset>}" >&2
  echo "Set WINDOWS_ISO=/path/to/win11.iso or pass --iso." >&2
  exit 2
fi

echo "Creating $DIR/notes.qcow2 ($SIZE)"
rm -f "$DIR/notes.qcow2"
qemu-img create -f qcow2 "$DIR/notes.qcow2" "$SIZE"

echo "Copying OVMF vars"
cp -f "$OVMF_VARS" "$DIR/OVMF_VARS.fd"

echo "Linking installer ISO"
ln -sf "$(readlink -f "$ISO")" "$DIR/notes-boot.iso"

rm -f "$DIR/.installed"

cat <<EOF

VM scaffold ready.

  bash run-notes.sh start
  bash run-notes.sh vnc      # 127.0.0.1:5901

After Windows is installed, the user 'notes' exists, OpenSSH is enabled and
Samsung Notes is signed in:

  bash guestsetup/sync_now.sh
  touch "$DIR/.installed"

The installer ISO is only attached while vm/.installed is absent.
EOF