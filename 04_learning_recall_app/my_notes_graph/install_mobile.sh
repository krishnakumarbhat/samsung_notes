#!/usr/bin/env bash
# Open the Recall study workstation on a USB-connected Android phone.
#
# This app has no APK: it ships as a PWA. Chrome installs a WebAPK when the
# page is opened from a secure context, and `adb reverse` makes the host's
# loopback address appear as localhost on the phone, which is exactly that.
set -euo pipefail

PORT="${PORT:-8787}"
URL="http://127.0.0.1:${PORT}/"

if ! command -v adb >/dev/null 2>&1; then
  echo "adb is not installed; install android-tools-adb first." >&2
  exit 1
fi

mapfile -t devices < <(adb devices | awk 'NR > 1 && $2 == "device" { print $1 }')
if [[ ${#devices[@]} -eq 0 ]]; then
  cat >&2 <<'MSG'
No authorised Android device found.

1. On the phone: Settings → About → tap Build number 7× to unlock Developer options.
2. Enable USB debugging, connect the cable, and accept the RSA fingerprint prompt.
3. Re-run this script.

There is no APK to sideload: the app installs as a PWA from Chrome.
MSG
  exit 2
fi

adb reverse "tcp:${PORT}" "tcp:${PORT}"
adb shell am start -a android.intent.action.VIEW -d "$URL" >/dev/null

cat <<MSG
Recall is open on ${devices[0]} at ${URL}

Install it as an app: Chrome menu (⋮) → "Add to Home screen".
Because the origin is localhost, the service worker and IndexedDB deck work,
so Travel mode keeps functioning with the cable unplugged.
MSG
