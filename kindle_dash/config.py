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
    # The Kindle's routes (images, taps) need control_token; refused while it's empty.
    control_token: str = ""
    control_allow_ips: tuple[str, ...] = ()  # empty = any LAN client with the token
    # /admin, for a browser: HTTP Basic auth as "admin". Refused while empty.
    admin_password: str = ""
    # Kasa smart plugs, by IP (broadcast discovery doesn't reach them), in display order,
    # and what each one powers (shown on its tile): KASA_HOSTS=ip=kind,ip=kind,...
    kasa_hosts: tuple[str, ...] = ()
    kasa_kinds: tuple[tuple[str, str], ...] = ()
    lights_refresh_seconds: int = 15
    # Raw touch coordinates -> panel pixels (portrait 1236 x 1648). The defaults
    # assume the touch panel already reports panel pixels.
    touch_swap_xy: bool = False
    touch_flip_x: bool = False
    touch_flip_y: bool = False
    touch_max_x: int = 1235
    touch_max_y: int = 1647

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
            control_token=env("CONTROL_TOKEN", ""),
            control_allow_ips=_list(env("CONTROL_ALLOW_IPS", "")),
            admin_password=env("ADMIN_PASSWORD", ""),
            kasa_hosts=tuple(h for h, _ in _plugs(env("KASA_HOSTS", ""))),
            kasa_kinds=tuple((h, k) for h, k in _plugs(env("KASA_HOSTS", "")) if k),
            lights_refresh_seconds=max(5, int(env("LIGHTS_REFRESH_SECONDS", cls.lights_refresh_seconds))),
            touch_swap_xy=_flag(env("TOUCH_SWAP_XY", "")),
            touch_flip_x=_flag(env("TOUCH_FLIP_X", "")),
            touch_flip_y=_flag(env("TOUCH_FLIP_Y", "")),
            touch_max_x=int(env("TOUCH_MAX_X", cls.touch_max_x)),
            touch_max_y=int(env("TOUCH_MAX_Y", cls.touch_max_y)),
        )


def _list(value: str) -> tuple[str, ...]:
    return tuple(v.strip() for v in value.split(",") if v.strip())


def _plugs(value: str) -> list[tuple[str, str]]:
    """"ip=kind,ip" -> [(ip, kind), (ip, "")]."""
    return [(h.strip(), k.strip()) for h, _, k in (item.partition("=") for item in _list(value))]


def _flag(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes")
