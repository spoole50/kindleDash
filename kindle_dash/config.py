"""Settings, read once from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Settings:
    influx_url: str = "http://influxdb:8086"
    influx_token: str = ""
    influx_org: str = "sp_inc"
    influx_bucket: str = "victron"
    tz: str = "UTC"
    price_per_kwh: float = 0.48
    refresh_seconds: int = 30
    chart_hours: int = 24
    spark_hours: int = 6
    days: int = 7
    stale_after_seconds: int = 180
    width: int = 1236
    height: int = 1648

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ.get
        return cls(
            influx_url=env("INFLUX_URL", cls.influx_url),
            influx_token=env("INFLUX_TOKEN", ""),
            influx_org=env("INFLUX_ORG", cls.influx_org),
            influx_bucket=env("INFLUX_BUCKET", cls.influx_bucket),
            tz=env("TZ", cls.tz) or "UTC",
            price_per_kwh=float(env("PRICE_PER_KWH", cls.price_per_kwh)),
            refresh_seconds=max(5, int(env("REFRESH_SECONDS", cls.refresh_seconds))),
            chart_hours=int(env("CHART_HOURS", cls.chart_hours)),
            spark_hours=int(env("SPARK_HOURS", cls.spark_hours)),
            days=int(env("DAYS", cls.days)),
        )
