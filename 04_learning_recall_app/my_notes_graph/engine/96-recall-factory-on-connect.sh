#!/usr/bin/env bash
# /etc/NetworkManager/dispatcher.d/96-recall-factory-on-connect.sh
# Trigger: only on "up" event with connectivity=full
# Action: start the user-level recall question factory service

INTERFACE="$1"
STATUS="$2"

# NetworkManager arguments are: interface, event. Trigger only on link-up.
if [ "$STATUS" != "up" ]; then
  exit 0
fi

# NetworkManager's connectivity check distinguishes internet access from link-up.
if ! nmcli -t networking connectivity check 2>/dev/null | grep -qx 'full'; then
  exit 0
fi

# Start the user service in pope's user session; never expose credentials in logs.
runuser -u pope -- env XDG_RUNTIME_DIR=/run/user/1000 \
  DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus \
  systemctl --user start recall-question-factory.service

# Also log the event
logger -t recall-factory "Network up on $INTERFACE — started recall-question-factory.service"
exit 0
