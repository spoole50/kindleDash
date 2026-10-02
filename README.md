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

The client runs as **scriptlets**, the launcher used by the Universal Hotfix that
ships with current jailbreaks. KUAL is obsolete on this firmware. Any `.sh` file in
`documents/` appears in the library like a book and runs when you tap it.

```
kindle/
  kindledash/            -> Kindle drive: kindledash/      (/mnt/us/kindledash)
    config.sh               URL, rotation, interval, ...
    kindledash.sh           the loop
  documents/             -> Kindle drive: documents/       (/mnt/us/documents)
    Solar Dashboard - Start.sh
    Solar Dashboard - Stop.sh
    Solar Dashboard - Log.sh
```

The loop:

1. sets `preventScreenSaver 1` and stops the Kindle UI (`stop lab126_gui`, falling back to `stop framework`) so the UI can't draw over the dashboard
2. every `INTERVAL` seconds, `curl`s the PNG with connect and total timeouts
3. if the image's md5 changed, draws it with FBInk using a partial refresh, with a full flashing refresh every `FULL_REFRESH_EVERY` (15) draws to clear ghosting
4. if a fetch fails, leaves the last image up, draws a small inverted "! offline since HH:MM" marker in the bottom-left corner, and toggles Wi-Fi via `lipc` to reconnect. The next successful fetch does a full redraw that removes the marker.

### Install (from a Mac)

`/mnt/us` on the Kindle is the root of its USB drive (`/Volumes/Kindle`).

```sh
git clone https://github.com/spoole50/kindleDash.git ~/kindleDash
cp -RX ~/kindleDash/kindle/kindledash /Volumes/Kindle/
cp -X ~/kindleDash/kindle/documents/*.sh /Volumes/Kindle/documents/
dot_clean /Volumes/Kindle
```

1. Check `URL` in `/Volumes/Kindle/kindledash/config.sh`. It should be `http://<pi-ip>:8765/dashboard.png` (currently `192.168.113.186`).
2. Eject. The three **Solar Dashboard** entries appear in the library.
3. Tap **Solar Dashboard - Log** first. It shows whether FBInk was found; the hotfix normally installs it at `/mnt/us/libkh/bin/fbink`.
4. Tap **Solar Dashboard - Start**.

The log is at `/mnt/us/kindledash/kindledash.log`.

### Power button

While the dashboard runs, the loop reads the power key directly (`/dev/input/event0`):

- **press**: toggles the backlight on or off, remembering the previous level
- **double-press** (two presses within 1.5 s): returns to the library. The two toggles cancel out, so the light ends where it started. It restarts the Kindle UI and opens Home, which can take up to about a minute.

On this Kindle (PW11, `bd71828-pwrkey`), the power-management chip only reports "pushed", with press and release at the same instant. How long the button is held can't be measured, which is why this uses press counting rather than a long press.
**Don't hold the button down:** a hold of 15 s or more forces a hardware restart.
Every press is logged and the log is saved to disk right away. Set `POWER_BUTTON=0` or change `DOUBLE_PRESS_MS` in `config.sh`.

### Stopping it

- **Double-press the power button** (see above).
- **Over SSH:** `sh "/mnt/us/documents/Solar Dashboard - Stop.sh"`.
- **Last resort:** hold the power button until the Kindle restarts. Nothing autostarts.

If the screen stays blank after returning to the library, press power twice (sleep, then wake) to force a repaint.
