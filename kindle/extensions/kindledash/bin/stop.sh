#!/bin/sh
# Stop the loop and give the Kindle its normal UI and power management back.
PIDFILE=/var/tmp/kindledash/pid
if [ -f "$PIDFILE" ]; then
    kill "$(cat "$PIDFILE")" 2>/dev/null
    rm -f "$PIDFILE"
fi
lipc-set-prop com.lab126.powerd preventScreenSaver 0 2>/dev/null
start lab126_gui 2>/dev/null || start framework 2>/dev/null || /etc/init.d/framework start 2>/dev/null
exit 0
