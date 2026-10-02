"""Flux queries, lifted from the "Solar Setup Home" Grafana dashboard.

Measurement, fields, device_type tags and the integral / clamping logic are
identical to the Grafana panels; only the time ranges are fixed here instead
of coming from the dashboard picker (v.timeRangeStart / v.windowPeriod).
"""

from __future__ import annotations

from datetime import datetime

MEASUREMENT = "victron_energy"
SHUNT = "smartshunt"
MPPT = "smartsolar_mppt"

# (series key, field, device_type) -- straight from the Grafana panel filters.
SERIES = [
    ("soc", "state_of_charge_percent", SHUNT),
    ("shunt_v", "battery_voltage_v", SHUNT),
    ("mppt_v", "battery_voltage_v", MPPT),
    ("current_a", "battery_current_a", SHUNT),
    ("power_w", "battery_power_w", SHUNT),
    ("pv_w", "pv_power_w", MPPT),
]
LATEST_ONLY = [("ttg_s", "time_to_go_s", SHUNT)]


def _rfc3339(ts: datetime) -> str:
    return ts.isoformat()


def _device_filter(pairs) -> str:
    clauses = " or ".join(
        f'(r["_field"] == "{field}" and r["device_type"] == "{dev}")' for _, field, dev in pairs
    )
    return f"filter(fn: (r) => {clauses})"


def live_queries(bucket: str, chart_hours: int, today_start: datetime, window: str = "5m") -> list[str]:
    """Latest values, chart series and today's energy totals.

    Kept as separate self-contained queries: sharing one `from()` between yields
    stops InfluxDB pushing the filters down to storage (~2.3 s vs ~0.6 s here).
    """
    head = f'''from(bucket: "{bucket}")
  |> range(start: -{chart_hours}h)
  |> filter(fn: (r) => r["_measurement"] == "{MEASUREMENT}")'''
    today = f'''from(bucket: "{bucket}")
  |> range(start: {_rfc3339(today_start)})
  |> filter(fn: (r) => r["_measurement"] == "{MEASUREMENT}")'''
    latest = f'''{head}
  |> {_device_filter(SERIES + LATEST_ONLY)}
  |> last()
  |> yield(name: "latest")'''
    series = f'''{head}
  |> {_device_filter(SERIES)}
  |> aggregateWindow(every: {window}, fn: mean, createEmpty: false)
  |> yield(name: "series")'''
    totals = f'''// Grafana "Solar Production" stat
{today}
  |> filter(fn: (r) => r["_field"] == "pv_power_w")
  |> filter(fn: (r) => r["device_type"] == "{MPPT}")
  |> integral(unit: 1h)
  |> yield(name: "solar_wh")

// Grafana "Battery Usage" stat (discharge only)
{today}
  |> filter(fn: (r) => r["_field"] == "battery_power_w")
  |> filter(fn: (r) => r["device_type"] == "{SHUNT}")
  |> map(fn: (r) => ({{ r with _value: if r._value < 0.0 then r._value else 0.0 }}))
  |> integral(unit: 1h)
  |> map(fn: (r) => ({{ r with _value: r._value * -1.0 }}))
  |> yield(name: "battery_wh")'''
    return [latest, series, totals]


def daily_query(bucket: str, tz: str, start: datetime) -> str:
    """Grafana "Production Per Day" bar chart, aligned to local midnights."""
    return f'''
import "timezone"
option location = timezone.location(name: "{tz}")

from(bucket: "{bucket}")
  |> range(start: {_rfc3339(start)})
  |> filter(fn: (r) => r["_measurement"] == "{MEASUREMENT}")
  |> filter(fn: (r) => r["_field"] == "pv_power_w")
  |> filter(fn: (r) => r["device_type"] == "{MPPT}")
  |> map(fn: (r) => ({{ r with _value: if r._value < 0.0 then 0.0 else r._value }}))
  |> aggregateWindow(every: 1d, fn: (column, tables=<-) => tables |> integral(unit: 1h), createEmpty: true, timeSrc: "_start")
  |> yield(name: "daily_wh")
'''
