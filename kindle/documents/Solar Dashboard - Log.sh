#!/bin/sh
# Name: Solar Dashboard - Log
# Author: spoole50
# Prints the last log lines on screen (output is shown via FBInk).
tail -n 12 /mnt/us/kindledash/kindledash.log 2>/dev/null || echo "no log yet"
# Wi-Fi signal, for the settings page; also saved to kindledash/wifi.txt on the USB drive
{ cat /proc/net/wireless; echo "signalStrength: $(lipc-get-prop com.lab126.wifid signalStrength 2>&1)"; ls /sys/class/net; } > /mnt/us/kindledash/wifi.txt 2>&1
tail -n 3 /mnt/us/kindledash/wifi.txt
for f in /mnt/us/libkh/bin/fbink /usr/bin/fbink /usr/local/bin/fbink; do [ -x "$f" ] && echo "fbink: $f" && exit 0; done
echo "fbink: NOT FOUND"
