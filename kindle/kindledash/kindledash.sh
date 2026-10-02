#!/bin/sh
# Main loop: fetch the dashboard PNG and draw it with FBInk when it changes.
BIN="$(cd "$(dirname "$0")" && pwd)"
. "$BIN/config.sh"

STATE=/var/tmp/kindledash
LOG=/mnt/us/kindledash/kindledash.log   # on the USB drive, readable from a computer
PNG="$STATE/dashboard.png"
TMP="$STATE/download.png"
mkdir -p "$STATE"
echo $$ > "$STATE/pid"

FBINK=""
for f in "$FBINK_BIN" /mnt/us/libkh/bin/fbink /usr/bin/fbink /usr/local/bin/fbink /mnt/us/usbnet/bin/fbink /mnt/us/koreader/fbink; do
    [ -n "$f" ] && [ -x "$f" ] && FBINK="$f" && break
done

log() { echo "$(date '+%F %T') $*" >> "$LOG"; }

# keep the log small
[ -f "$LOG" ] && [ "$(wc -c < "$LOG")" -gt 200000 ] && tail -n 200 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"

if [ -z "$FBINK" ]; then
    log "fbink not found; set FBINK_BIN in config.sh"
    exit 1
fi

case "$URL" in *\?*) FULL_URL="$URL&rotate=$ROTATE" ;; *) FULL_URL="$URL?rotate=$ROTATE" ;; esac

wifi_up() {
    lipc-get-prop com.lab126.wifid cmState 2>/dev/null | grep -q CONNECTED
}

reconnect_wifi() {
    log "network check failed, reconnecting Wi-Fi"
    lipc-set-prop com.lab126.cmd wirelessEnable 1 2>/dev/null
    if ! wifi_up; then
        lipc-set-prop com.lab126.wifid enable 0 2>/dev/null
        sleep 3
        lipc-set-prop com.lab126.wifid enable 1 2>/dev/null
    fi
    i=0
    while [ $i -lt 30 ] && ! wifi_up; do sleep 1; i=$((i + 1)); done
    wifi_up && log "Wi-Fi connected" || log "Wi-Fi still down"
}

error_marker() {
    # small inverted label in the bottom-left corner; the image underneath stays
    "$FBINK" -q -h -x 0 -y -1 " ! offline since $1 " 2>/dev/null
}

lipc-set-prop com.lab126.powerd preventScreenSaver 1 2>/dev/null
if [ "$STOP_FRAMEWORK" = "1" ]; then
    sleep 3  # let the scriptlet launcher return first
    stop lab126_gui 2>/dev/null || stop framework 2>/dev/null || /etc/init.d/framework stop 2>/dev/null
    sleep 2
fi
"$FBINK" -q -c -f 2>/dev/null
log "started: $FULL_URL (fbink: $FBINK)"

last_sum=""
draws=0
fail_since=""
while true; do
    if curl -s -f --connect-timeout "$CONNECT_TIMEOUT" -m "$MAX_TIME" -o "$TMP" "$FULL_URL"; then
        sum="$(md5sum "$TMP" | cut -d' ' -f1)"
        if [ "$sum" != "$last_sum" ]; then
            mv "$TMP" "$PNG"
            draws=$((draws + 1))
            if [ $draws -ge "$FULL_REFRESH_EVERY" ] || [ -z "$last_sum" ]; then
                "$FBINK" -q -c -f -g file="$PNG" 2>>"$LOG"   # full flashing refresh
                draws=0
            else
                "$FBINK" -q -g file="$PNG" 2>>"$LOG"         # partial refresh
            fi
            last_sum="$sum"
        fi
        [ -n "$fail_since" ] && log "recovered"
        fail_since=""
    else
        log "fetch failed (curl exit $?)"
        if [ -z "$fail_since" ]; then
            fail_since="$(date '+%H:%M')"
            error_marker "$fail_since"
            last_sum=""  # force a full redraw (clearing the marker) on recovery
        fi
        wifi_up || reconnect_wifi
    fi
    sleep "$INTERVAL"
done
