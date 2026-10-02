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

# Orientation: one of $ROTATIONS, remembered across runs on the USB drive.
SERVER="${SERVER:-${URL%/*}}"  # older config.sh files set URL instead
ROT_FILE="$KD_DIR/rotation"
set -- $ROTATIONS
ROTATE="$(cat "$ROT_FILE" 2>/dev/null)"
case " $ROTATIONS " in *" $ROTATE "*) [ -n "$ROTATE" ] || ROTATE=$1 ;; *) ROTATE=$1 ;; esac

# The rotation after the current one in $ROTATIONS, wrapping around.
next_rotation() {
    first=""; take=""; nxt=""
    for r in $ROTATIONS; do
        [ -z "$first" ] && first=$r
        [ -n "$take" ] && { nxt=$r; break; }
        [ "$r" = "$ROTATE" ] && take=1
    done
    echo "${nxt:-$first}"
}

# 90/270 are the landscape layout turned to fit the portrait panel. The offline
# marker goes on the panel row holding the image's empty left margin / footer.
apply_rotation() {
    case "$ROTATE" in 90|270) layout=landscape ;; *) layout=portrait ;; esac
    case "$ROTATE" in 90|180) MARKER_ROW=0 ;; *) MARKER_ROW=-1 ;; esac
    FULL_URL="$SERVER/$layout.png?rotate=$ROTATE"
}
apply_rotation

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

# Touchscreen (pt_mt, multitouch). A hold of TOUCH_HOLD_MS..TOUCH_MAX_MS,
# measured from touch down to lift, asks the main loop to rotate (USR2).
# A touch produces many events, so read them in chunks: one read() returns as
# many whole 16-byte events as are queued.
EV_BTN_TOUCH=$((1 + 330 * 65536))
EV_MT_TRACKING_ID=$((3 + 57 * 65536))  # 4294967295 (-1) = finger lifted
EV_MT_X=$((3 + 53 * 65536)); EV_MT_Y=$((3 + 54 * 65536))
EV_ABS_X=3; EV_ABS_Y=$((3 + 65536))
TOUCH_LOG_MAX=20  # log coordinates for the first touches of each run

in_touch_area() {
    [ -z "$TOUCH_AREA" ] && return 0
    set -- $TOUCH_AREA
    [ -n "$sx" ] && [ -n "$sy" ] && [ "$sx" -ge "$1" ] && [ "$sy" -ge "$2" ] && [ "$sx" -le "$3" ] && [ "$sy" -le "$4" ]
}

touch_watcher() {
    exec 4< "$TOUCH_DEV" || { log "cannot open $TOUCH_DEV"; return; }
    down_s=""; down_us=""; sx=""; sy=""; x=""; y=""; logged=0
    while :; do
        set -- $(dd bs=1024 count=1 <&4 2>/dev/null | od -An -tu4 -v)
        [ $# -ge 4 ] || { sleep 1; continue; }
        while [ $# -ge 4 ]; do
            s=$1; us=$2; tc=$3; v=$4; shift 4
            case "$tc" in
                "$EV_MT_X"|"$EV_ABS_X") x=$v; [ -n "$down_s" ] && [ -z "$sx" ] && sx=$v; continue ;;
                "$EV_MT_Y"|"$EV_ABS_Y") y=$v; [ -n "$down_s" ] && [ -z "$sy" ] && sy=$v; continue ;;
                "$EV_BTN_TOUCH") [ "$v" = 0 ] && ev=up || ev=down ;;
                "$EV_MT_TRACKING_ID") [ "$v" = 4294967295 ] && ev=up || ev=down ;;
                *) continue ;;
            esac
            if [ "$ev" = down ]; then
                [ -n "$down_s" ] && continue  # already down (2nd finger / both event kinds)
                down_s=$s; down_us=$us; sx=""; sy=""
            elif [ -n "$down_s" ]; then
                ms=$(( (s - down_s) * 1000 + (us - down_us) / 1000 ))
                down_s=""
                if [ $logged -lt $TOUCH_LOG_MAX ]; then
                    logged=$((logged + 1))
                    log "touch ${ms}ms down x=$sx y=$sy up x=$x y=$y"
                fi
                if [ "$ms" -ge "$TOUCH_HOLD_MS" ] && [ "$ms" -le "$TOUCH_MAX_MS" ] && in_touch_area; then
                    log "touch hold ${ms}ms at x=$sx y=$sy -> rotate"
                    kill -USR2 "$MAIN_PID"
                fi
            fi
        done
    done
}

# Small inverted label drawn over the last image, on panel row MARKER_ROW.
error_marker() {
    "$FBINK" -q -h -x 0 -y "$MARKER_ROW" " ! offline since $1 " 2>/dev/null
}

MAIN_PID=$$
WATCHER=""
TOUCHER=""
shutdown() {
    [ -n "$WATCHER" ] && kill "$WATCHER" 2>/dev/null
    [ -n "$TOUCHER" ] && kill "$TOUCHER" 2>/dev/null
    rm -f "$STATE/pid"
    [ "$1" = "library" ] && restore_ui
    log "stopped ($1)"
    sync
    exit 0
}
trap 'shutdown library' USR1
trap 'shutdown signal' TERM INT
rotate_now=""
trap 'rotate_now=1' USR2

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
if [ "$TOUCH_ROTATE" = "1" ] && [ -r "$TOUCH_DEV" ]; then
    touch_watcher &
    TOUCHER=$!
    log "touch watcher on $TOUCH_DEV (hold ${TOUCH_HOLD_MS}ms, area: ${TOUCH_AREA:-anywhere})"
fi

last_sum=""
draws=0
fail_since=""
last_rc=""
while true; do
    if [ -n "$rotate_now" ]; then
        rotate_now=""
        ROTATE="$(next_rotation)"
        echo "$ROTATE" > "$ROT_FILE"
        apply_rotation
        "$FBINK" -q -c -f -m -y 10 "Rotating..." 2>/dev/null
        log "rotated to $ROTATE: $FULL_URL"; sync
        last_sum=""  # full redraw of the new layout
        draws=0
    fi
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
        # the Rotate button on $SERVER/rotate bumps this counter
        if [ "$REMOTE_ROTATE" = "1" ]; then
            rc="$(curl -s -f --connect-timeout "$CONNECT_TIMEOUT" -m 10 "$SERVER/rotate/count")"
            case "$rc" in
                ''|*[!0-9]*) ;;
                *) if [ -n "$last_rc" ] && [ "$rc" -gt "$last_rc" ]; then
                       log "remote rotate request"; rotate_now=1
                   fi
                   last_rc=$rc ;;
            esac
        fi
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
    [ -n "$rotate_now" ] && continue
    sleep "$INTERVAL" &
    wait $!  # interrupted by USR1/USR2, so presses and touches act at once
    kill $! 2>/dev/null
done
