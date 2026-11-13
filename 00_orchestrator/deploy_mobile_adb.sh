#!/usr/bin/env bash
# Deploy Recall to a USB-connected Android phone/tablet.
#
# Two delivery paths, in order of preference:
#   1. An APK already built on this machine (dist/*.apk) is installed with
#      `adb install -r`.
#   2. Otherwise the PWA is opened at http://localhost:8787 through
#      `adb reverse`, which is a secure context, so the service worker and the
#      offline deck work with the cable unplugged.
#
# No package is downloaded: building a TWA/APK needs Gradle and the Android SDK,
# which this script never fetches on a metered connection.
set -euo pipefail

APP_DIR="$(cd -- "$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")/../04_learning_recall_app/my_notes_graph" && pwd -P)"
PORT="${PORT:-8787}"
URL="http://127.0.0.1:${PORT}/"
APK=""
OPEN=1

while (($#)); do
  case "$1" in
    --apk) APK="${2:?--apk needs a path}"; shift 2 ;;
    --no-open) OPEN=0; shift ;;
    --port) PORT="${2:?--port needs a number}"; shift 2 ;;
    -h|--help) sed -n '2,12p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; exit 64 ;;
  esac
done

if ! command -v adb >/dev/null 2>&1; then
  echo "adb is missing. Install android-tools-adb, then reconnect the phone." >&2
  exit 1
fi

mapfile -t devices < <(adb devices | awk 'NR > 1 && $2 == "device" { print $1 }')
unauthorised=$(adb devices | awk 'NR > 1 && $2 == "unauthorized" { print $1 }')
if [[ ${#devices[@]} -eq 0 ]]; then
  cat >&2 <<'MSG'
No authorised Android device found.

1. Phone: Settings -> About phone -> tap Build number 7x.
2. Developer options -> USB debugging ON.
3. Reconnect the cable and tap "Allow" on the RSA fingerprint prompt.
4. Re-run:  bash 00_orchestrator/deploy_mobile_adb.sh
MSG
  [[ -n "$unauthorised" ]] && echo "Waiting for authorisation: $unauthorised" >&2
  exit 2
fi

DEVICE="${devices[0]}"
MODEL=$(adb -s "$DEVICE" shell getprop ro.product.model 2>/dev/null | tr -d '\r')
ANDROID=$(adb -s "$DEVICE" shell getprop ro.build.version.release 2>/dev/null | tr -d '\r')
echo "Device: ${MODEL:-unknown} (Android ${ANDROID:-?}) [${DEVICE}]"

# Start the host if it is not already answering.  run_app.sh binds 0.0.0.0.
if ! curl -fsS "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then
  echo "Starting Recall host..."
  bash "$APP_DIR/run_app.sh"
fi

# USB reverse makes the host loopback reachable as localhost on the phone.
adb -s "$DEVICE" reverse "tcp:${PORT}" "tcp:${PORT}" >/dev/null
echo "USB reverse active: phone localhost:${PORT} -> host localhost:${PORT}"

if [[ -z "$APK" ]]; then
  for candidate in "$APP_DIR"/dist/*.apk "$APP_DIR"/android/app/build/outputs/apk/release/*.apk; do
    [[ -f "$candidate" ]] && APK="$candidate" && break
  done
fi
if [[ -n "$APK" ]]; then
  echo "Installing APK: $APK"
  adb -s "$DEVICE" install -r "$APK"
  adb -s "$DEVICE" shell monkey -p com.recall.study -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1 || true
else
  echo "No APK found; using the PWA path (no build tools or downloads needed)."
fi

if ((OPEN)); then
  # Honour the phone's default browser.  Some ROMs disable Chrome, so forcing
  # its component would fail and leave the user on a blank screen.
  if adb -s "$DEVICE" shell pm list packages -d 2>/dev/null | grep -q 'package:com.android.chrome'; then
    echo "Chrome is disabled on this device; opening the default browser."
  fi
  adb -s "$DEVICE" shell am start -a android.intent.action.VIEW -d "$URL" >/dev/null 2>&1 || true
fi

LAN_IP=$(hostname -I | awk '{print $1}')
cat <<MSG

Recall is ready on ${MODEL:-the device}.

  USB (works with Wi-Fi off):  ${URL}
  Same Wi-Fi / hotspot:        http://${LAN_IP}:${PORT}/

If the LAN URL times out, the host firewall is dropping the port:
  sudo ufw allow ${PORT}/tcp
USB reverse keeps working without any firewall change.

Install as an app: browser menu -> More -> "Add to Home screen".
The localhost origin is a secure context, so the service worker caches the quiz
for offline use.  Unplug the cable only after the first page finishes loading.

To refresh later:  bash 00_orchestrator/deploy_mobile_adb.sh
To stop the host:  bash 04_learning_recall_app/my_notes_graph/run_app.sh --stop
MSG
