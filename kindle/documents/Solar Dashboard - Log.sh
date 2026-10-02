#!/bin/sh
# Name: Solar Dashboard - Log
# Author: spoole50
# Prints the last log lines on screen (output is shown via FBInk).
tail -n 15 /mnt/us/kindledash/kindledash.log 2>/dev/null || echo "no log yet"
for f in /mnt/us/libkh/bin/fbink /usr/bin/fbink /usr/local/bin/fbink; do [ -x "$f" ] && echo "fbink: $f" && exit 0; done
echo "fbink: NOT FOUND"
