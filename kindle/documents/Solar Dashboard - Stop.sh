#!/bin/sh
# Name: Solar Dashboard - Stop
# Author: spoole50
# DontUseFBInk
# Stops the loop and restores the Kindle UI and screensaver.
# (Only reachable from the library when STOP_FRAMEWORK=0; otherwise hold the
# power button for 2 s, or run this over SSH.)
. /mnt/us/kindledash/common.sh
PIDFILE="$STATE/pid"
if [ -f "$PIDFILE" ]; then
    kill "$(cat "$PIDFILE")" 2>/dev/null
    rm -f "$PIDFILE"
fi
restore_ui
exit 0
