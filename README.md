# kindle-solar-dashboard

Renders Victron solar/battery stats from InfluxDB as a 1236×1648 8-bit grayscale PNG
for a wall-mounted, jailbroken Kindle Paperwhite 11th gen. The data and queries
mirror the **Solar Setup Home** Grafana dashboard.

| Daytime (charging) | Night (discharging, InfluxDB down) |
|---|---|
| ![sample](docs/sample.png) | ![stale](docs/sample-night-stale.png) |

*(Screenshots use synthetic data: `python -m kindle_dash.sample docs/sample.png America/Los_Angeles`.)*

## What's on it

| Tile / chart | Source (Grafana panel) |
|---|---|
| Battery SoC %, gauge, time left | `state_of_charge_percent`, `time_to_go_s` (smartshunt) |
| Net power + charging/discharging state, SoC sparkline (6 h) | `battery_power_w`, `battery_current_a` (smartshunt) |
| Voltage: shunt and MPPT | `battery_voltage_v` (smartshunt, smartsolar_mppt) |
| Battery current, solar input W (+ 6 h sparklines) | `battery_current_a`, `pv_power_w` |
| Battery power, 24 h: solid fill = charge, hatched = discharge | "Net Battery Power" |
| MPPT solar input, 24 h | `pv_power_w` timeseries |
| Solar today Wh / $, battery used today Wh / $ | "Solar Production" / "Battery Usage" integrals, from local midnight |
| Production per day (7 days, today hatched since it's partial) | "Production Per Day" |

Design rules for e-ink: every gray is a multiple of `0x11` (the panel's 16 levels),
nothing relies on colour, type is large and bold, and there's an "Updated HH:MM" stamp.
When InfluxDB can't be reached, the last good image is kept and gets a black
**STALE since HH:MM** badge.

## Running it (Raspberry Pi, Docker)

The service has its own compose project in this folder. It joins the existing
`docker_default` network so it can reach the `influxdb` container by name, and
it doesn't touch `../docker-compose.yml`.

```sh
cd ~/docker/kindleDash
cp .env.example .env        # then set INFLUX_TOKEN (read access to the victron bucket is enough)
docker compose up -d --build
curl -s localhost:8765/health
```

Open `http://<pi>:8765/dashboard.png` in a browser to check it.

### Environment variables

| Variable | Default | Notes |
|---|---|---|
| `INFLUX_URL` | `http://influxdb:8086` | container name on `docker_default` |
| `INFLUX_TOKEN` | – | **required**; keep it in `.env` (git-ignored) |
| `INFLUX_ORG` | `sp_inc` | |
| `INFLUX_BUCKET` | `victron` | |
| `TZ` | `America/Los_Angeles` | used for "today", day buckets and timestamps |
| `PRICE_PER_KWH` | `0.48` | $ per kWh for the cost figures |
| `REFRESH_SECONDS` | `30` | query + render interval |
| `CHART_HOURS` / `SPARK_HOURS` / `DAYS` | `24` / `6` / `7` | chart, sparkline and bar-chart spans |

### Endpoints

| Endpoint | |
|---|---|
| `GET /dashboard.png[?rotate=0\|90\|180\|270]` | Latest render, served from memory. Sends an `ETag` and supports `If-None-Match` (returns 304). |
| `GET /status` | Current values as JSON, plus `last_success`, `stale_since` and `last_error` |
| `GET /health` | 200 if the last successful render is under 3 minutes old, otherwise 503. The Docker `HEALTHCHECK` uses it. |

A background task queries InfluxDB and renders every `REFRESH_SECONDS`. Requests
never trigger a render. A cycle takes about 0.4 s on a Pi 5: three small Flux
queries, plus the per-day query every 5 minutes.

### Development

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

## Kindle client

`kindle/extensions/kindledash/` is a KUAL extension. Its loop:

1. sets `preventScreenSaver 1` and stops the Kindle UI (`stop lab126_gui`, falling back to `stop framework`) so the UI can't draw over the dashboard
2. every `INTERVAL` seconds, `curl`s the PNG with connect and total timeouts
3. if the image's md5 changed, draws it with FBInk using a partial refresh, with a full flashing refresh every `FULL_REFRESH_EVERY` (15) draws to clear ghosting
4. if a fetch fails, leaves the last image up, draws a small inverted "! offline since HH:MM" marker in the bottom-left corner, and toggles Wi-Fi via `lipc` to reconnect. The next successful fetch does a full redraw that removes the marker.

### Requirements on the Kindle

- Jailbreak, KUAL and MRPI (you already have these with spidercat on FW 5.18.6)
- **FBInk**: the script looks in `/mnt/us/libkh/bin/fbink`, `/usr/bin/fbink`, `/usr/local/bin/fbink`, `/mnt/us/usbnet/bin/fbink` and `/mnt/us/koreader/fbink`. Set `FBINK_BIN` in `config.sh` if yours is somewhere else.
- Optional but recommended: USBNetwork or another SSH option, so you can stop the dashboard remotely.

### Install

1. Connect the Kindle over USB and copy `kindle/extensions/kindledash` to `/mnt/us/extensions/kindledash` (it shows up as `extensions/` on the USB drive).
2. Edit `extensions/kindledash/bin/config.sh`. Set `URL` to `http://<pi-ip>:8765/dashboard.png` (currently `192.168.113.186`) and adjust `ROTATE`, `INTERVAL` and so on if needed.
3. Eject, open **KUAL → Solar Dashboard → Start dashboard**.

The log is at `/var/tmp/kindledash/kindledash.log` (KUAL → *Show last log lines*).

### Stopping it

With `STOP_FRAMEWORK=1` (the default), the Kindle UI and KUAL aren't running, so:

- **over SSH:** `sh /mnt/us/extensions/kindledash/bin/stop.sh`. This kills the loop, re-enables the screensaver and restarts the UI.
- **without SSH:** hold the power button until the Kindle restarts (this can take up to ~40 s). Nothing autostarts, so it boots back to the normal UI.

With `STOP_FRAMEWORK=0`, KUAL stays usable and **Stop dashboard** works from the menu. The trade-off is that the stock UI may occasionally draw over the dashboard.
