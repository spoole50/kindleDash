#!/bin/sh
# Name: Solar Dashboard - Start
# Author: spoole50
# DontUseFBInk
# Starts the dashboard loop in the background (see /mnt/us/kindledash/).
PIDFILE=/var/tmp/kindledash/pid
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    exit 0  # already running
fi
mkdir -p /var/tmp/kindledash
if command -v setsid >/dev/null 2>&1; then
    setsid nohup sh /mnt/us/kindledash/kindledash.sh >> /var/tmp/kindledash/kindledash.log 2>&1 < /dev/null &
else
    nohup sh /mnt/us/kindledash/kindledash.sh >> /var/tmp/kindledash/kindledash.log 2>&1 < /dev/null &
fi
exit 0
