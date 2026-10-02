"""Synthetic data for tests and README screenshots.

    python -m kindle_dash.sample out.png [TZ]
"""

from __future__ import annotations

import math
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .data import Snapshot, build_snapshot, local_midnight


def sample_records(now: datetime, tz, hours: int = 24, step_min: int = 5) -> tuple[list[dict], list[dict]]:
    """Flux-shaped records for a plausible sunny day on a 48 V bank."""
    recs: list[dict] = []
    n = hours * 60 // step_min
    soc = 62.0
    last: dict[tuple[str, str], float] = {}
    for i in range(n + 1):
        t = now - timedelta(minutes=step_min * (n - i))
        hour = t.astimezone(tz).hour + t.astimezone(tz).minute / 60
        sun = max(0.0, math.sin((hour - 6.5) / 13 * math.pi))
        pv = 820 * sun**1.4 * (0.85 + 0.15 * math.sin(i / 3.0))
        load = 180 + 90 * (18 <= hour <= 23) + 40 * math.sin(i / 7.0)
        p = pv - load
        soc = max(5.0, min(100.0, soc + p * step_min / 60 / 50))
        volts = 51.2 + soc / 100 * 2.6 + (0.4 if p > 0 else -0.2)
        vals = {
            ("state_of_charge_percent", "smartshunt"): soc,
            ("battery_voltage_v", "smartshunt"): volts,
            ("battery_voltage_v", "smartsolar_mppt"): volts + 0.05,
            ("battery_current_a", "smartshunt"): p / volts,
            ("battery_power_w", "smartshunt"): p,
            ("pv_power_w", "smartsolar_mppt"): pv,
        }
        for (f, dev), v in vals.items():
            recs.append({"result": "series", "_time": t, "_field": f, "device_type": dev, "_value": v})
        last = vals
    for (f, dev), v in last.items():
        recs.append({"result": "latest", "_time": now, "_field": f, "device_type": dev, "_value": v})
    recs.append({"result": "latest", "_time": now, "_field": "time_to_go_s", "device_type": "smartshunt", "_value": 41_400.0})
    recs.append({"result": "solar_wh", "_value": 3_184.6})
    recs.append({"result": "battery_wh", "_value": 1_207.3})

    midnight = local_midnight(now, tz)
    daily = [
        {"result": "daily_wh", "_time": midnight - timedelta(days=k), "_value": wh}
        for k, wh in zip(range(6, 0, -1), (4810.0, 5230.0, 2140.0, 3900.0, 5480.0, 4975.0))
    ]
    return recs, daily


def sample_snapshot(now: datetime | None = None, tz=ZoneInfo("UTC"), price: float = 0.48) -> Snapshot:
    now = now or datetime.now(timezone.utc)
    recs, daily = sample_records(now, tz)
    return build_snapshot(recs, daily, now, tz, price)


if __name__ == "__main__":
    from .render import render, to_png

    out = sys.argv[1] if len(sys.argv) > 1 else "sample.png"
    zone = ZoneInfo(sys.argv[2] if len(sys.argv) > 2 else "UTC")
    now = datetime.now(timezone.utc).astimezone(zone).replace(hour=15, minute=42).astimezone(timezone.utc)
    with open(out, "wb") as fh:
        fh.write(to_png(render(sample_snapshot(now, zone), zone)))
    print(out)
