#!/bin/sh
# Main loop: fetch the dashboard PNG and draw it with FBInk when it changes.
BIN="$(cd "$(dirname "$0")" && pwd)"
. "$BIN/config.sh"

. "$BIN/common.sh"
PNG="$STATE/dashboard.png"
TMP="$STATE/download.png"
echo $$ > "$STATE/pid"
FBINK="$(find_fbink)"

# Keep the tail of the system log on the USB drive: /var/log is wiped by a reboot,
# so this is the only record of what happened just before one.
SYSLOG_COPY=/mnt/us/kindledash/messages.log
save_syslog() {
    { echo "=== $(date '+%F %T') uptime: $(cut -d' ' -f1 /proc/uptime)s"
      if [ -s /var/log/messages ]; then tail -n 300 /var/log/messages
      elif command -v logread >/dev/null 2>&1; then logread 2>/dev/null | tail -n 300
      fi
      echo "--- dmesg"
      dmesg 2>/dev/null | tail -n 100
    } > "$SYSLOG_COPY.tmp" && mv "$SYSLOG_COPY.tmp" "$SYSLOG_COPY"
}

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

# Power button (event0 = bd71828-pwrkey). The PMIC only reports "pushed": the
# kernel emits down+up with the same timestamp however long it's held, so we
# count presses instead of timing them.
#   single press -> toggle backlight
#   double press (2nd within DOUBLE_PRESS_MS) -> back to the library
#     (its two toggles cancel out, so the light ends where it started)
# Events are read through one persistent fd so none are missed; on this 32-bit
# kernel each is 16 bytes: tv_sec, tv_usec, type | code << 16, value.
KEY_POWER_WORD=$((1 + 116 * 65536))  # EV_KEY, KEY_POWER
button_watcher() {
    exec 3< "$BUTTON_DEV" || { log "cannot open $BUTTON_DEV"; return; }
    last_s=""; last_us=""
    while :; do
        set -- $(dd bs=16 count=1 <&3 2>/dev/null | od -An -tu4 -v)
        [ $# -eq 4 ] || { sleep 1; continue; }
        [ "$3" = "$KEY_POWER_WORD" ] && [ "$4" = "1" ] || continue
        backlight_toggle
        if [ -n "$last_s" ] && [ $(( $1 - last_s )) -le 10 ] &&
           [ $(( ($1 - last_s) * 1000 + ($2 - last_us) / 1000 )) -le "$DOUBLE_PRESS_MS" ]; then
            "$FBINK" -q -c -f -m -y 10 "Returning to the library..." 2>/dev/null
            log "power double-press -> exit to library"; sync
            kill -USR1 "$MAIN_PID"
            return
        fi
        log "power pressed -> backlight"; sync
        last_s=$1; last_us=$2
    done
}

# Small inverted label drawn over the last image. FBInk draws in the panel's
# native portrait orientation, so pick the panel row that lands in the image's
# empty left margin / footer: ROTATE=90 puts the image's left edge on panel row 0.
case "$ROTATE" in 90) MARKER_ROW=${MARKER_ROW:-0} ;; *) MARKER_ROW=${MARKER_ROW:--1} ;; esac
error_marker() {
    "$FBINK" -q -h -x 0 -y "$MARKER_ROW" " ! offline since $1 " 2>/dev/null
}

MAIN_PID=$$
WATCHER=""
shutdown() {
    [ -n "$WATCHER" ] && kill "$WATCHER" 2>/dev/null
    rm -f "$STATE/pid"
    [ "$1" = "library" ] && restore_ui
    log "stopped ($1)"
    sync
    exit 0
}
trap 'shutdown library' USR1
trap 'shutdown signal' TERM INT

lipc-set-prop com.lab126.powerd preventScreenSaver 1 2>/dev/null
if [ "$STOP_FRAMEWORK" = "1" ]; then
    sleep 3  # let the scriptlet launcher return first
    stop lab126_gui 2>/dev/null || stop framework 2>/dev/null || /etc/init.d/framework stop 2>/dev/null
    sleep 2
fi
"$FBINK" -q -c -f 2>/dev/null
log "started: $FULL_URL (fbink: $FBINK) uptime=$(cut -d' ' -f1 /proc/uptime)s"
[ -f "$SYSLOG_COPY" ] && mv "$SYSLOG_COPY" "$SYSLOG_COPY.prev"  # what preceded the last reboot
log "syslog candidates: $(ls -la /var/log 2>&1 | tr '\n' ' ' | cut -c1-600)"
if [ "$POWER_BUTTON" = "1" ] && [ -r "$BUTTON_DEV" ]; then
    button_watcher &
    WATCHER=$!
    log "power button watcher on $BUTTON_DEV"
fi

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
            log "drew (draws=$draws) uptime=$(cut -d' ' -f1 /proc/uptime)s powerd=$(lipc-get-prop com.lab126.powerd state 2>/dev/null)"
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
    save_syslog
    sleep "$INTERVAL" &
    wait $!  # interruptible, so a long press is handled immediately
done
