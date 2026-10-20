#!/bin/sh
# Main loop: fetch the dashboard PNG and draw it with FBInk when it changes.
BIN="$(cd "$(dirname "$0")" && pwd)"
KD_VERSION=2026-10-19.3  # sent with each fetch; the server's settings page says when the Kindle needs a newer copy
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
echo "$ROTATE" > "$ROT_FILE"  # the touch watcher reads it for taps

# Page (tab) on screen, also remembered. Only the server's tap replies change it.
PAGE_FILE="$KD_DIR/page"
read_page() {
    PAGE="$(cat "$PAGE_FILE" 2>/dev/null)"
    case "$PAGE" in ''|*[!a-z]*) PAGE=solar ;; esac
}
read_page
echo "$PAGE" > "$PAGE_FILE"

# Back to HOME_PAGE after IDLE_RETURN seconds without a tap (0 = stay put),
# turning the backlight off as it goes if IDLE_BACKLIGHT_OFF=1. The next tap
# then only turns the light back on. Defaults here so an older config.sh
# still gets them.
HOME_PAGE="${HOME_PAGE:-solar}"
IDLE_RETURN="${IDLE_RETURN-300}"
IDLE_BACKLIGHT_OFF="${IDLE_BACKLIGHT_OFF-1}"
ACTIVITY_FILE="$STATE/activity"  # time of the last tap, written by the touch watcher
DARK_FILE="$STATE/dark"          # exists while the light is off from going idle
# Written by the watchers while the main loop reads it, so write a whole new
# file and rename it: "date > file" leaves it empty for a moment, and an empty
# read once made $(( )) fail, which kills the script.
mark_activity() {
    date +%s > "$ACTIVITY_FILE.$1" && mv "$ACTIVITY_FILE.$1" "$ACTIVITY_FILE"
}
# Seconds since the last tap or power press (0 if the file can't be read)
idle_seconds() {
    now=$(date +%s); last=$(cat "$ACTIVITY_FILE" 2>/dev/null)
    case "$last" in ''|*[!0-9]*) last=$now ;; esac
    echo $(( now - last ))
}
mark_activity main
rm -f "$DARK_FILE"

# The server's settings page: X-Kindle-Settings on each image ("home=solar
# idle=300 idlelight=1 theme=dark full=6") overrides the values above. Without
# one (an older server, or no answer) they stay as config.sh set them.
HEADERS="$STATE/headers.txt"
CONFIG_FULL_REFRESH_EVERY=$FULL_REFRESH_EVERY
THEME=""
apply_settings() {
    line="$(tr -d '\r' < "$HEADERS" 2>/dev/null | grep -i '^x-kindle-settings:' | tail -n 1 | cut -d: -f2-)"
    [ -n "$line" ] || return 0
    FULL_REFRESH_EVERY=$CONFIG_FULL_REFRESH_EVERY
    for kv in $line; do
        v="${kv#*=}"
        case "$kv" in
            home=*) case "$v" in ''|*[!a-z]*) ;; *) HOME_PAGE=$v ;; esac ;;
            idle=*) case "$v" in ''|*[!0-9]*) ;; *) IDLE_RETURN=$v ;; esac ;;
            idlelight=*) case "$v" in 0|1) IDLE_BACKLIGHT_OFF=$v ;; esac ;;
            full=*) case "$v" in ''|0|*[!0-9]*) ;; *) FULL_REFRESH_EVERY=$v ;; esac ;;
            theme=*)
                if [ -n "$THEME" ] && [ "$v" != "$THEME" ]; then
                    log "theme $v"
                    last_sum=""  # light <-> dark: a full flashing redraw, or the old page ghosts through
                fi
                THEME=$v ;;
        esac
    done
}

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
    FULL_URL="$SERVER/$layout.png?rotate=$ROTATE&page=$PAGE"
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
        mark_activity button  # a light turned on by hand stays on for IDLE_RETURN
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

# A tap goes to the server, which knows what's drawn where. Its reply (200) is
# lines of: "flash <left> <top> <width> <height>" (panel pixels) to show the
# press, "page <name>" to switch tabs, "redraw" to fetch the page again now.
# 204 means nothing was hit. Page switches and redraws are the main loop's
# job (ALRM); a flash with neither is undone by redrawing the current image.
send_tap() {
    mark_activity touch
    rot="$(cat "$ROT_FILE" 2>/dev/null)"; pg="$(cat "$PAGE_FILE" 2>/dev/null)"
    code="$(curl -s -o "$STATE/tap.txt" -w '%{http_code}' -X POST -H "X-Kindle-Token: $TOKEN" \
        --connect-timeout "$CONNECT_TIMEOUT" -m 15 "$SERVER/tap?x=$1&y=$2&rotate=${rot:-0}&page=${pg:-solar}")"
    case "$code" in
        200) ;;
        204) return ;;
        *) log "tap at $1,$2 refused or failed: HTTP ${code:-none} $(head -c 100 "$STATE/tap.txt" 2>/dev/null)"; return ;;
    esac
    flashed=""; wake=""
    while read -r cmd a b c d e; do
        case "$cmd" in
            flash)
                [ "$e" = white ] && img=white.png || img=black.png  # white on a dark page
                "$FBINK" -q -g file="$BIN/$img",x="$a",y="$b",w="$c",h="$d" 2>>"$LOG"; flashed=1 ;;
            page) case "$a" in ''|*[!a-z]*) ;; *) echo "$a" > "$PAGE_FILE"; wake=1 ;; esac ;;
            redraw) wake=1 ;;
            # from the settings page
            rotate) case "$a" in 0|90|180|270)
                        echo "$a" > "$ROT_FILE.tap" && mv "$ROT_FILE.tap" "$ROT_FILE"; wake=1 ;; esac ;;
            light) case "$a" in ''|*[!0-9]*) ;;
                       0) backlight_off ;;
                       *) rm -f "$DARK_FILE"; lipc-set-prop com.lab126.powerd flIntensity "$a"; log "backlight $a" ;; esac ;;
            warm) case "$a" in ''|*[!0-9]*) ;; *) lipc-set-prop com.lab126.powerd currentAmberLevel "$a"; log "warm light $a" ;; esac ;;
            clean) touch "$STATE/clean"; wake=1 ;;
            # a "tap again" prompt: redraw once it has run out, so it doesn't stay on screen
            later) case "$a" in ''|*[!0-9]*) ;; *) ( sleep "$a"; kill -ALRM "$MAIN_PID" 2>/dev/null ) & ;; esac ;;
            exit)
                "$FBINK" -q -c -f -m -y 10 "Returning to the library..." 2>/dev/null
                log "settings page -> exit to library"; sync
                kill -USR1 "$MAIN_PID"
                return ;;
        esac
    done < "$STATE/tap.txt"
    if [ -n "$wake" ] && ! kill -ALRM "$MAIN_PID" 2>/dev/null; then
        log "main loop is gone; tap can't switch pages"
        "$FBINK" -q -g file="$PNG" 2>>"$LOG"  # at least undo the flash
    elif [ -z "$wake" ] && [ -n "$flashed" ]; then
        "$FBINK" -q -g file="$PNG" 2>>"$LOG"
    fi
}

# Touchscreen (pt_mt, multitouch). A tap shorter than TAP_MAX_MS goes to the
# server (send_tap). A hold of TOUCH_HOLD_MS..TOUCH_MAX_MS, measured from touch
# down to lift, asks the main loop to rotate (USR2).
# A touch produces many events, so read them in chunks: one read() returns as
# many whole 16-byte events as are queued.
EV_BTN_TOUCH=$((1 + 330 * 65536))
EV_MT_TRACKING_ID=$((3 + 57 * 65536))  # 4294967295 (-1) = finger lifted
EV_MT_X=$((3 + 53 * 65536)); EV_MT_Y=$((3 + 54 * 65536))
EV_ABS_X=3; EV_ABS_Y=$((3 + 65536))

in_touch_area() {
    [ -z "$TOUCH_AREA" ] && return 0
    set -- $TOUCH_AREA
    [ -n "$sx" ] && [ -n "$sy" ] && [ "$sx" -ge "$1" ] && [ "$sy" -ge "$2" ] && [ "$sx" -le "$3" ] && [ "$sy" -le "$4" ]
}

touch_watcher() {
    exec 4< "$TOUCH_DEV" || { log "cannot open $TOUCH_DEV"; return; }
    down_s=""; down_us=""; sx=""; sy=""; x=""; y=""
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
                # one line per touch, saying what became of it (the server logs the taps it gets too)
                what="down x=$sx y=$sy up x=$x y=$y"
                if [ "$ms" -lt "$TAP_MAX_MS" ] && [ -f "$DARK_FILE" ]; then
                    # dark from going idle: this tap only brings the light back
                    log "touch ${ms}ms $what -> light on (was off from idle)"
                    rm -f "$DARK_FILE"
                    mark_activity touch
                    backlight_on
                elif [ "$TAPS" = "1" ] && [ "$ms" -lt "$TAP_MAX_MS" ]; then
                    send_tap "${sx:-$x}" "${sy:-$y}"
                    log "touch ${ms}ms $what -> tap, HTTP ${code:-none}"
                elif [ "$TOUCH_ROTATE" = "1" ] && [ "$ms" -ge "$TOUCH_HOLD_MS" ] && [ "$ms" -le "$TOUCH_MAX_MS" ] && in_touch_area; then
                    log "touch ${ms}ms $what -> hold: rotate"
                    kill -USR2 "$MAIN_PID"
                else
                    log "touch ${ms}ms $what -> ignored (taps are under ${TAP_MAX_MS}ms, holds ${TOUCH_HOLD_MS}-${TOUCH_MAX_MS}ms)"
                fi
            fi
        done
    done
}

# The Kindle's own battery, sent with each fetch so the server can show it in
# the header, plus what its settings page shows: the front and warm light, the
# Wi-Fi signal, uptime and this script's version. Sets REPORT_QS to
# "&battery=..&charging=..&light=..", leaving out anything unreadable.
qs_num() {  # qs_num <name> <value>: "&name=value" if value is a whole number (or a negative one)
    case "${2#-}" in ''|*[!0-9]*) ;; *) printf '&%s=%s' "$1" "$2" ;; esac
}
read_report() {
    REPORT_QS="&ver=$KD_VERSION"
    bat="$(lipc-get-prop com.lab126.powerd battLevel 2>/dev/null)"
    chg="$(lipc-get-prop com.lab126.powerd isCharging 2>/dev/null)"
    case "$chg" in 1) ;; *) chg=0 ;; esac
    case "$bat" in ''|*[!0-9]*) bat="" ;; *) REPORT_QS="$REPORT_QS&battery=$bat&charging=$chg" ;; esac
    REPORT_QS="$REPORT_QS$(qs_num light "$(lipc-get-prop com.lab126.powerd flIntensity 2>/dev/null)")"
    REPORT_QS="$REPORT_QS$(qs_num lightmax "$(lipc-get-prop com.lab126.powerd flMaxIntensity 2>/dev/null)")"
    REPORT_QS="$REPORT_QS$(qs_num warm "$(lipc-get-prop com.lab126.powerd currentAmberLevel 2>/dev/null)")"
    # the signal in bars, like the Kindle's status bar: "5/5"
    ss="$(lipc-get-prop com.lab126.wifid signalStrength 2>/dev/null)"
    case "$ss" in */*) REPORT_QS="$REPORT_QS$(qs_num wifi "${ss%%/*}")$(qs_num wifimax "${ss#*/}")" ;; esac
    REPORT_QS="$REPORT_QS$(qs_num up "$(cut -d. -f1 /proc/uptime)")"
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
# a tap asked for a redraw or another page. (Not HUP: nohup leaves that ignored.)
refresh_now=""
trap 'refresh_now=1' ALRM

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
if { [ "$TOUCH_ROTATE" = "1" ] || [ "$TAPS" = "1" ]; } && [ -r "$TOUCH_DEV" ]; then
    touch_watcher &
    TOUCHER=$!
    log "touch watcher on $TOUCH_DEV (taps: $TAPS, rotate hold: $TOUCH_ROTATE ${TOUCH_HOLD_MS}ms, area: ${TOUCH_AREA:-anywhere})"
fi

last_sum=""
force=""  # draw the next fetch even if unchanged (it may be under a tap's flash)
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
    if [ "${IDLE_RETURN:-0}" -gt 0 ] && [ "$(idle_seconds)" -ge "$IDLE_RETURN" ]; then
        if [ "$PAGE" != "$HOME_PAGE" ]; then
            log "idle $(idle_seconds)s on $PAGE -> $HOME_PAGE"
            echo "$HOME_PAGE" > "$PAGE_FILE"
            refresh_now=1
        fi
        # on any page, once per idle spell (a tap or power press ends it)
        if [ "$IDLE_BACKLIGHT_OFF" = "1" ] && [ ! -f "$DARK_FILE" ] &&
           [ "$(lipc-get-prop com.lab126.powerd flIntensity 2>/dev/null)" -gt 0 ] 2>/dev/null; then
            log "idle $(idle_seconds)s -> backlight off"
            backlight_off
            touch "$DARK_FILE"
        fi
    fi
    if [ -n "$refresh_now" ]; then
        refresh_now=""
        want="$(cat "$ROT_FILE" 2>/dev/null)"  # the settings page may have chosen an orientation
        case "$want" in 0|90|180|270) if [ "$want" != "$ROTATE" ]; then
            ROTATE=$want
            apply_rotation
            "$FBINK" -q -c -f -m -y 10 "Rotating..." 2>/dev/null
            log "rotated to $ROTATE from the settings page: $FULL_URL"
            last_sum=""; draws=0
        fi ;; esac
        if [ -f "$STATE/clean" ]; then
            rm -f "$STATE/clean"
            last_sum=""  # a full flashing redraw of the same page
        fi
        old_page=$PAGE
        read_page
        if [ "$PAGE" != "$old_page" ]; then
            apply_rotation
            log "page $PAGE: $FULL_URL"
            last_sum=""  # full redraw of the new page
            draws=0
        else
            force=1
        fi
    fi
    read_report
    if curl -s -f -H "X-Kindle-Token: $TOKEN" --connect-timeout "$CONNECT_TIMEOUT" -m "$MAX_TIME" -D "$HEADERS" -o "$TMP" "$FULL_URL$REPORT_QS"; then
        apply_settings
        sum="$(md5sum "$TMP" | cut -d' ' -f1)"
        if [ "$sum" != "$last_sum" ] || [ -n "$force" ]; then
            mv "$TMP" "$PNG"
            draws=$((draws + 1))
            if [ $draws -ge "$FULL_REFRESH_EVERY" ] || [ -z "$last_sum" ]; then
                "$FBINK" -q -c -f -g file="$PNG" 2>>"$LOG"   # full flashing refresh
                draws=0
            else
                "$FBINK" -q -g file="$PNG" 2>>"$LOG"         # partial refresh
            fi
            last_sum="$sum"
            force=""
            log "drew (draws=$draws) battery=${bat:-?}% charging=${chg:-?} uptime=$(cut -d' ' -f1 /proc/uptime)s powerd=$(lipc-get-prop com.lab126.powerd state 2>/dev/null)"
        fi
        [ -n "$fail_since" ] && log "recovered"
        fail_since=""
        # the Rotate button on $SERVER/admin bumps this counter
        if [ "$REMOTE_ROTATE" = "1" ]; then
            rc="$(curl -s -f -H "X-Kindle-Token: $TOKEN" --connect-timeout "$CONNECT_TIMEOUT" -m 10 "$SERVER/rotate/count")"
            case "$rc" in
                ''|*[!0-9]*) ;;
                *) if [ -n "$last_rc" ] && [ "$rc" -gt "$last_rc" ]; then
                       log "remote rotate request"; rotate_now=1
                   fi
                   last_rc=$rc ;;
            esac
        fi
    else
        err=$?
        [ $err = 22 ] && log "fetch refused or failed: HTTP error (is TOKEN in config.sh right?)" || log "fetch failed (curl exit $err)"
        if [ -z "$fail_since" ]; then
            fail_since="$(date '+%H:%M')"
            error_marker "$fail_since"
            last_sum=""  # force a full redraw (clearing the marker) on recovery
        fi
        wifi_up || reconnect_wifi
    fi
    save_syslog
    [ -n "$rotate_now" ] || [ -n "$refresh_now" ] && continue
    sleep "$INTERVAL" &
    wait $!  # interrupted by USR1/USR2/ALRM, so presses and touches act at once
    kill $! 2>/dev/null
done
