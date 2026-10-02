"""Fetch from InfluxDB and map flat Flux records into a Snapshot."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Mapping

from . import queries
from .config import Settings

Point = tuple[datetime, float]

# (field, device_type) -> series key
_KEYS = {(f, d): k for k, f, d in queries.SERIES + queries.LATEST_ONLY}


@dataclass
class Snapshot:
    fetched_at: datetime
    latest: dict[str, float] = field(default_factory=dict)
    latest_time: datetime | None = None
    series: dict[str, list[Point]] = field(default_factory=dict)
    solar_today_wh: float | None = None
    battery_today_wh: float | None = None
    daily_wh: list[tuple[date, float]] = field(default_factory=list)
    price_per_kwh: float = 0.48

    def get(self, key: str) -> float | None:
        return self.latest.get(key)

    @property
    def solar_today_cost(self) -> float | None:
        return None if self.solar_today_wh is None else self.solar_today_wh / 1000 * self.price_per_kwh

    @property
    def battery_today_cost(self) -> float | None:
        return None if self.battery_today_wh is None else self.battery_today_wh / 1000 * self.price_per_kwh

    def to_json(self) -> dict:
        return {
            "fetched_at": self.fetched_at.isoformat(),
            "latest_point_at": self.latest_time.isoformat() if self.latest_time else None,
            "battery_soc_pct": self.get("soc"),
            "time_to_go_s": self.get("ttg_s"),
            "shunt_voltage_v": self.get("shunt_v"),
            "mppt_voltage_v": self.get("mppt_v"),
            "battery_current_a": self.get("current_a"),
            "battery_power_w": self.get("power_w"),
            "solar_power_w": self.get("pv_w"),
            "solar_today_wh": self.solar_today_wh,
            "solar_today_usd": self.solar_today_cost,
            "battery_used_today_wh": self.battery_today_wh,
            "battery_used_today_usd": self.battery_today_cost,
            "price_per_kwh": self.price_per_kwh,
            "daily_production_wh": [{"date": d.isoformat(), "wh": v} for d, v in self.daily_wh],
        }


def local_midnight(now: datetime, tz) -> datetime:
    local = now.astimezone(tz)
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


def build_snapshot(
    records: Iterable[Mapping],
    daily_records: Iterable[Mapping],
    fetched_at: datetime,
    tz,
    price_per_kwh: float,
) -> Snapshot:
    """Map flat Flux records (dicts with result/_field/device_type/_time/_value)."""
    snap = Snapshot(fetched_at=fetched_at, price_per_kwh=price_per_kwh)
    for rec in records:
        value = rec.get("_value")
        if value is None:
            continue
        result = rec.get("result")
        if result == "solar_wh":
            snap.solar_today_wh = float(value)
            continue
        if result == "battery_wh":
            snap.battery_today_wh = float(value)
            continue
        key = _KEYS.get((rec.get("_field"), rec.get("device_type")))
        if key is None:
            continue
        ts = rec.get("_time")
        if result == "latest":
            snap.latest[key] = float(value)
            if ts and (snap.latest_time is None or ts > snap.latest_time):
                snap.latest_time = ts
        elif result == "series":
            snap.series.setdefault(key, []).append((ts, float(value)))
    for pts in snap.series.values():
        pts.sort(key=lambda p: p[0])

    days: dict[date, float] = {}
    for rec in daily_records:
        ts = rec.get("_time")
        if ts is None:
            continue
        # timeSrc=_start => the bucket's local midnight
        days[ts.astimezone(tz).date()] = float(rec.get("_value") or 0.0)
    # The daily query is cached for a few minutes; keep today's bar live.
    today = fetched_at.astimezone(tz).date()
    if snap.solar_today_wh is not None and (not days or today >= max(days)):
        days[today] = max(snap.solar_today_wh, 0.0)
    snap.daily_wh = sorted(days.items())
    return snap


def _flatten(tables) -> list[dict]:
    return [rec.values for table in tables for rec in table.records]


class InfluxSource:
    """Thin wrapper so the poller can be tested without a server."""

    def __init__(self, settings: Settings):
        from influxdb_client import InfluxDBClient

        self.s = settings
        self.client = InfluxDBClient(
            url=settings.influx_url, token=settings.influx_token, org=settings.influx_org, timeout=15_000
        )
        self.query_api = self.client.query_api()
        self._daily: list[dict] = []
        self._daily_at: datetime | None = None

    def fetch(self, now: datetime | None = None) -> Snapshot:
        s = self.s
        now = now or datetime.now(timezone.utc)
        midnight = local_midnight(now, s.zone)
        live = [
            rec
            for q in queries.live_queries(s.influx_bucket, s.chart_hours, midnight)
            for rec in _flatten(self.query_api.query(q))
        ]
        # Past days don't change; recompute the bar chart every 5 minutes.
        if self._daily_at is None or now - self._daily_at > timedelta(minutes=5):
            start = midnight - timedelta(days=s.days - 1)
            self._daily = _flatten(self.query_api.query(queries.daily_query(s.influx_bucket, s.tz, start)))
            self._daily_at = now
        return build_snapshot(live, self._daily, now, s.zone, s.price_per_kwh)

    def close(self) -> None:
        self.client.close()
