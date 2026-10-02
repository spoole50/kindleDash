# Edit this file on the Kindle drive: kindledash/config.sh (/mnt/us/kindledash/config.sh)

# The Pi's LAN address and port (from docker-compose.yml)
SERVER="http://192.168.113.186:8765"
# Must match CONTROL_TOKEN in the Pi's .env. The server refuses every image,
# tap and rotate check without it.
TOKEN=""
# 1 = send taps to the server (tabs, light switches). Touches shorter than
# TAP_MAX_MS count as taps.
TAPS=1
TAP_MAX_MS=1000
# Orientations to cycle through. A 2 s touch-and-hold (or the Rotate button at
# $SERVER/admin) moves to the next one; the current one is remembered in
# kindledash/rotation on the USB drive.
#   0   portrait, upright
#   90  landscape, Kindle turned counter-clockwise (power button on the right)
#   180 portrait, upside down
#   270 landscape, Kindle turned clockwise (power button on the left)
# (to turn landscape the other way, use the other of 90/270)
ROTATIONS="0 270"
# Seconds between polls (the server re-renders every 30 s)
INTERVAL=60
# Full flashing refresh every N redraws to clear ghosting
FULL_REFRESH_EVERY=15
# 1 = stop the Kindle UI so it can't draw over the dashboard (recommended).
# While it is stopped the library (and the Stop scriptlet) is unavailable; see README.
STOP_FRAMEWORK=1
# curl timeouts (seconds)
CONNECT_TIMEOUT=10
MAX_TIME=30
# Power button: single press toggles the backlight; double press (second press
# within DOUBLE_PRESS_MS) returns to the library. (This Kindle can't report how
# long the button is held, and a ~15 s+ hold forces a hardware restart.)
POWER_BUTTON=1
DOUBLE_PRESS_MS=1500
BUTTON_DEV=/dev/input/event0
# Touch-and-hold anywhere for TOUCH_HOLD_MS (then let go) to rotate.
# Holds longer than TOUCH_MAX_MS are ignored (something resting on the screen).
TOUCH_ROTATE=1
TOUCH_HOLD_MS=2000
TOUCH_MAX_MS=8000
TOUCH_DEV=/dev/input/event1
# Limit the hold to one area, in raw touch coordinates: "xmin ymin xmax ymax".
# Empty = anywhere. The first touches of each run are logged with their
# coordinates ("touch down x=... y=...") so you can find a corner.
TOUCH_AREA=""
# 1 = also rotate when the Rotate button at $SERVER/admin is pressed
REMOTE_ROTATE=1
