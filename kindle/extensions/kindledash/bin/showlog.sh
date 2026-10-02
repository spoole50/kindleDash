#!/bin/sh
# KUAL: print the last few log lines on screen.
for f in /mnt/us/libkh/bin/fbink /usr/bin/fbink /usr/local/bin/fbink; do [ -x "$f" ] && FBINK="$f" && break; done
"$FBINK" -q -y 5 "$(tail -n 12 /var/tmp/kindledash/kindledash.log 2>/dev/null)"
