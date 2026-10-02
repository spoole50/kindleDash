#!/bin/sh
# Name: Solar Dashboard - Probe
# Author: spoole50
# DontUseFBInk
# Records how this Kindle reports the power button, touch and backlight while
# the UI is stopped (the dashboard's conditions). Follow the on-screen prompts.
# Output: kindledash/probe.log on the USB drive. The library comes back at the end.

OUT=/mnt/us/kindledash/probe.log
mkdir -p /mnt/us/kindledash
for f in /mnt/us/libkh/bin/fbink /usr/bin/fbink /usr/local/bin/fbink; do [ -x "$f" ] && FBINK="$f" && break; done

say() { "$FBINK" -q -c -f -m -y 10 "$1" 2>/dev/null; echo "$(date +%T) --- PROMPT: $1" >> "$OUT"; }
stamp() { while read -r l; do echo "$(date +%T) $1: $l"; done >> "$OUT"; }

{
    echo "=== probe $(date '+%F %T') uid=$(id -u)"
    uname -a
    cat /etc/prettyversion.txt 2>/dev/null
    echo "--- /proc/bus/input/devices"; cat /proc/bus/input/devices
    echo "--- /dev/input"; ls -l /dev/input
    echo "--- backlight sysfs"
    for d in /sys/class/backlight/*; do echo "$d max=$(cat "$d/max_brightness" 2>/dev/null) cur=$(cat "$d/brightness" 2>/dev/null)"; done
    echo "--- powerd props"
    lipc-probe -v com.lab126.powerd 2>&1
    for p in flIntensity flMaxIntensity currentAmberLevel status state battLevel isCharging preventScreenSaver; do
        echo "$p = $(lipc-get-prop com.lab126.powerd "$p" 2>&1)"
    done
    echo "--- which tools"; for t in od hexdump dd setsid evtest lipc-wait-event lipc-probe; do echo "$t: $(command -v $t)"; done
} > "$OUT" 2>&1

lipc-set-prop com.lab126.powerd preventScreenSaver 1 2>/dev/null
sleep 3
stop lab126_gui 2>/dev/null || stop framework 2>/dev/null || /etc/init.d/framework stop 2>/dev/null
sleep 2

# capture lipc events and raw input in the background
PIDS=""
lipc-wait-event -m com.lab126.powerd '*' 2>&1 | stamp powerd & PIDS="$PIDS $!"
lipc-wait-event -m com.lab126.hal '*' 2>&1 | stamp hal & PIDS="$PIDS $!"
for ev in /dev/input/event*; do
    od -An -tx1 "$ev" 2>&1 | stamp "$(basename "$ev")" & PIDS="$PIDS $!"
done

say "PROBE: do NOT touch the screen. In 5 s press POWER once, briefly."
sleep 15
say "Now HOLD POWER for about 3 seconds, then let go."
sleep 15
say "Now TAP the TOP-RIGHT corner of the screen once."
sleep 10
say "Done. Restoring the library..."
echo "flIntensity after = $(lipc-get-prop com.lab126.powerd flIntensity 2>&1)" >> "$OUT"

kill $PIDS 2>/dev/null
killall lipc-wait-event od 2>/dev/null
lipc-set-prop com.lab126.powerd preventScreenSaver 0 2>/dev/null
sleep 2
start lab126_gui 2>/dev/null || start framework 2>/dev/null || /etc/init.d/framework start 2>/dev/null
echo "=== probe finished $(date +%T)" >> "$OUT"
exit 0
