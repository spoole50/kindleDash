# Edit this file on the Kindle drive: kindledash/config.sh (/mnt/us/kindledash/config.sh)

# Dashboard URL (the Pi's LAN address, port from docker-compose.yml)
URL="http://192.168.113.186:8765/dashboard.png"
# Optional rotation passed to the server: 0, 90, 180, 270
ROTATE=0
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
