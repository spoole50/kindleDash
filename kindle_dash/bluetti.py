"""Bluetti power stations, from the InfluxDB bucket that bluetti2influx writes.

One point every few seconds per station (measurement "bluetti", tagged with the
model in "device"): battery percent, AC/DC input and output watts, and on some
models the AC/DC output switches.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Mapping

from .data import Point, _flatten, local_midnight

MEASUREMENT = "bluetti"
POWER_FIELDS = ("ac_input_power", "dc_input_power", "ac_output_power", "dc_output_power")
FIELDS = ("total_battery_percent", *POWER_FIELDS, "ctrl_ac", "ctrl_dc")
NAMES = {"AP300": "APEX 300"}
STALE_S = 120  # bluetti2influx writes every ~5 s; stopped (e.g. to use the app) after this
FLOW_W = 5  # less than this either way counts as idle


@dataclass
class Station:
    model: str
    capacity_wh: float | None = None
    latest: dict[str, float | bool] = field(default_factory=dict)
    latest_time: datetime | None = None
    soc: list[Point] = field(default_factory=list)
    watts_in: list[Point] = field(default_factory=list)
    watts_out: list[Point] = field(default_factory=list)
    today_wh: dict[str, float] = field(default_factory=dict)  # by power field, since local midnight

    @property
    def name(self) -> str:
        return NAMES.get(self.model, self.model)

    @property
    def percent(self) -> float | None:
        v = self.latest.get("total_battery_percent")
        return None if v is None else float(v)

    def watts(self, key: str) -> float | None:
        v = self.latest.get(key)
        return None if v is None or isinstance(v, bool) else float(v)

    def _sum(self, keys) -> float | None:
        vals = [self.watts(k) for k in keys]
        return None if all(v is None for v in vals) else sum(v or 0.0 for v in vals)

    @property
    def power_in(self) -> float | None:
        return self._sum(("ac_input_power", "dc_input_power"))

    @property
    def power_out(self) -> float | None:
        return self._sum(("ac_output_power", "dc_output_power"))

    @property
    def net(self) -> float | None:
        """Into the battery (+) or out of it (-), ignoring conversion losses."""
        if self.power_in is None or self.power_out is None:
            return None
        return self.power_in - self.power_out

    @property
    def state(self) -> str:
        net = self.net
        if net is None or abs(net) < FLOW_W:
            return "IDLE"
        return "CHARGING" if net > 0 else "DISCHARGING"

    def time_left(self) -> float | None:
        """Seconds to empty while discharging, or to full while charging, from the
        capacity and the flow now. Rough: it ignores inverter losses."""
        pct, net = self.percent, self.net
        if pct is None or net is None or not self.capacity_wh or abs(net) < FLOW_W:
            return None
        left = pct if net < 0 else 100 - pct
        return left / 100 * self.capacity_wh / abs(net) * 3600

    def stale(self, now: datetime) -> bool:
        return self.latest_time is None or (now - self.latest_time).total_seconds() > STALE_S

    @property
    def today_in_wh(self) -> float | None:
        vals = [self.today_wh[k] for k in ("ac_input_power", "dc_input_power") if k in self.today_wh]
        return sum(vals) if vals else None

    @property
    def today_out_wh(self) -> float | None:
        vals = [self.today_wh[k] for k in ("ac_output_power", "dc_output_power") if k in self.today_wh]
        return sum(vals) if vals else None


@dataclass
class BluettiSnapshot:
    fetched_at: datetime
    stations: list[Station] = field(default_factory=list)

    @property
    def latest_time(self) -> datetime | None:
        times = [s.latest_time for s in self.stations if s.latest_time]
        return max(times) if times else None

    def to_json(self) -> list[dict]:
        return [
            {
                "model": s.model,
                "at": s.latest_time.isoformat() if s.latest_time else None,
                "battery_pct": s.percent,
                "in_w": s.power_in,
                "out_w": s.power_out,
                "time_left_s": s.time_left(),
                "today_in_wh": s.today_in_wh,
                "today_out_wh": s.today_out_wh,
                "values": s.latest,
            }
            for s in self.stations
        ]


def _filter(fields: Iterable[str]) -> str:
    return " or ".join(f'r["_field"] == "{f}"' for f in fields)


def queries(bucket: str, chart_hours: int, today_start: datetime, window: str = "5m") -> list[str]:
    """Latest values, chart series and today's energy, as separate queries so
    each one's filters are pushed down to storage."""
    head = f'''from(bucket: "{bucket}")
  |> range(start: {{start}})
  |> filter(fn: (r) => r["_measurement"] == "{MEASUREMENT}")
  |> filter(fn: (r) => {{fields}})'''
    latest = head.format(start=f"-{chart_hours}h", fields=_filter(FIELDS)) + '''
  |> last()
  |> yield(name: "latest")'''
    series = head.format(start=f"-{chart_hours}h", fields=_filter(("total_battery_percent", *POWER_FIELDS))) + f'''
  |> aggregateWindow(every: {window}, fn: mean, createEmpty: false)
  |> yield(name: "series")'''
    today = head.format(start=today_start.isoformat(), fields=_filter(POWER_FIELDS)) + '''
  |> integral(unit: 1h)
  |> yield(name: "today_wh")'''
    return [latest, series, today]


def build_snapshot(records: Iterable[Mapping], fetched_at: datetime, stations: tuple[tuple[str, float | None], ...] = ()) -> BluettiSnapshot:
    """Map flat Flux records (result/_field/device/_time/_value).

    `stations` lists models to show first, in order, with their capacity in Wh;
    any other model in the data follows, without a capacity."""
    by_model: dict[str, Station] = {m: Station(m, cap) for m, cap in stations}
    series: dict[tuple[str, str], list[Point]] = {}
    for rec in records:
        model, key, value, ts = rec.get("device"), rec.get("_field"), rec.get("_value"), rec.get("_time")
        if not model or value is None:
            continue
        st = by_model.setdefault(model, Station(model))
        result = rec.get("result")
        if result == "latest":
            st.latest[key] = value if isinstance(value, bool) else float(value)
            if ts and (st.latest_time is None or ts > st.latest_time):
                st.latest_time = ts
        elif result == "series" and ts:
            series.setdefault((model, key), []).append((ts, float(value)))
        elif result == "today_wh":
            st.today_wh[key] = max(float(value), 0.0)
    for (model, key), pts in series.items():
        pts.sort(key=lambda p: p[0])
        if key == "total_battery_percent":
            by_model[model].soc = pts
    for st in by_model.values():
        st.watts_in = _add(series.get((st.model, "ac_input_power"), []), series.get((st.model, "dc_input_power"), []))
        st.watts_out = _add(series.get((st.model, "ac_output_power"), []), series.get((st.model, "dc_output_power"), []))
    # a listed model with no data at all is still shown (as "no data")
    return BluettiSnapshot(fetched_at, list(by_model.values()))


def _add(a: list[Point], b: list[Point]) -> list[Point]:
    """Sum two series on their shared window times (aggregateWindow aligns them)."""
    total: dict[datetime, float] = {}
    for t, v in [*a, *b]:
        total[t] = total.get(t, 0.0) + v
    return sorted(total.items())


class BluettiSource:
    def __init__(self, settings):
        from influxdb_client import InfluxDBClient

        self.s = settings
        self.client = InfluxDBClient(url=settings.influx_url, token=settings.influx_token, org=settings.influx_org, timeout=15_000)
        self.query_api = self.client.query_api()

    def fetch(self, now: datetime | None = None) -> BluettiSnapshot:
        s = self.s
        now = now or datetime.now(timezone.utc)
        recs = [
            rec
            for q in queries(s.bluetti_bucket, s.chart_hours, local_midnight(now, s.zone))
            for rec in _flatten(self.query_api.query(q))
        ]
        return build_snapshot(recs, now, s.bluetti_stations)

    def close(self) -> None:
        self.client.close()
