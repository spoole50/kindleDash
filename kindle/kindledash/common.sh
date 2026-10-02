# Shared helpers, sourced by kindledash.sh and the Stop scriptlet.

KD_DIR=/mnt/us/kindledash
STATE=/var/tmp/kindledash
LOG="$KD_DIR/kindledash.log"   # on the USB drive, readable from a computer
mkdir -p "$STATE"

log() { echo "$(date '+%F %T') $*" >> "$LOG"; }

find_fbink() {
    for f in "$FBINK_BIN" /mnt/us/libkh/bin/fbink /usr/bin/fbink /usr/local/bin/fbink /mnt/us/usbnet/bin/fbink /mnt/us/koreader/fbink; do
        [ -n "$f" ] && [ -x "$f" ] && echo "$f" && return 0
    done
    return 1
}

# Bring the stock UI back. Starting lab126_gui alone leaves the screen blank
# until the next wake, so wait for the app manager and open Home to force a repaint.
restore_ui() {
    log "restoring UI"
    lipc-set-prop com.lab126.powerd preventScreenSaver 0 2>/dev/null
    out=$(start lab126_gui 2>&1) || out=$(start framework 2>&1) || out=$(/etc/init.d/framework start 2>&1)
    log "start ui: $out"
    i=0
    while [ $i -lt 120 ] && ! lipc-probe com.lab126.appmgrd >/dev/null 2>&1; do
        sleep 1; i=$((i + 1))
    done
    sleep 5  # let the home booklet register after appmgrd is up
    if lipc-set-prop com.lab126.appmgrd start app://com.lab126.booklet.home 2>>"$LOG"; then
        log "opened home after ${i}s"
    else
        log "could not open home (appmgrd after ${i}s); press power twice to repaint"
    fi
}

# Backlight: 0..flMaxIntensity via powerd. Remembers the last non-zero level.
backlight_off() {
    cur=$(lipc-get-prop com.lab126.powerd flIntensity 2>/dev/null)
    [ "${cur:-0}" -gt 0 ] || return 0
    echo "$cur" > "$STATE/backlight"
    lipc-set-prop com.lab126.powerd flIntensity 0
    log "backlight off (was $cur)"
}

backlight_on() {
    lvl=$(cat "$STATE/backlight" 2>/dev/null)
    lipc-set-prop com.lab126.powerd flIntensity "${lvl:-12}"
    log "backlight on (${lvl:-12})"
}

backlight_toggle() {
    rm -f "$STATE/dark"  # the idle switch-off (kindledash.sh) is over either way
    cur=$(lipc-get-prop com.lab126.powerd flIntensity 2>/dev/null)
    if [ "${cur:-0}" -gt 0 ]; then backlight_off; else backlight_on; fi
}
