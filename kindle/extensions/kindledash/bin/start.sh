#!/bin/sh
# KUAL "Start dashboard": run the loop detached from KUAL.
BIN="$(cd "$(dirname "$0")" && pwd)"
PIDFILE=/var/tmp/kindledash/pid
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    exit 0  # already running
fi
mkdir -p /var/tmp/kindledash
nohup sh "$BIN/kindledash.sh" >> /var/tmp/kindledash/kindledash.log 2>&1 &
