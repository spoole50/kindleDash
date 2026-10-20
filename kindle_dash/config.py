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
    # plugs of these kinds need a second tap on the Kindle to switch
    confirm_kinds: tuple[str, ...] = ("ev charger",)
    # Apple TV: its IP, app shortcuts ("Name=bundle.id", up to 4), and where pairing is saved
    atv_host: str = ""
    atv_apps: tuple[tuple[str, str], ...] = ()
    atv_credentials: str = "/data/appletv.json"
    # Bluetti stations (bluetti2influx): the bucket, and the models to show in order
    # with their battery capacity in Wh, for the time-left estimate
    bluetti_bucket: str = "bluetti"
    bluetti_stations: tuple[tuple[str, float | None], ...] = (("AP300", 2764.8), ("AC180", 1152.0))
    # UniFi Protect, for the CAMERA page: the console's address, a local account
    # (a Protect "View Only" user is enough), and the cameras by their Protect
    # name, optionally with a label: PROTECT_CAMERAS=garageCam=Outer garage
    protect_host: str = ""
    protect_user: str = ""
    protect_password: str = ""
    protect_cameras: tuple[tuple[str, str], ...] = ()
    camera_refresh_seconds: int = 30  # while the Kindle shows the page; every 5 min otherwise
    # Hyundai Blue Link (US), for the CAR page: the MyHyundai login and Blue Link PIN.
    # Cached reads (the car stays asleep) every car_refresh_seconds; a tap may wake
    # the car for fresh numbers at most every car_wake_min_seconds.
    bluelink_user: str = ""
    bluelink_password: str = ""
    bluelink_pin: str = ""
    car_refresh_seconds: int = 1800
    car_wake_min_seconds: int = 1800
    car_climate_temp: float = 72  # what START CLIMATE sets, in the car's units (°F in the US)
    car_climate_minutes: int = 10
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
            confirm_kinds=tuple(k.lower() for k in _list(env("CONFIRM_KINDS", ",".join(cls.confirm_kinds)))),
            lights_refresh_seconds=max(5, int(env("LIGHTS_REFRESH_SECONDS", cls.lights_refresh_seconds))),
            atv_host=env("ATV_HOST", ""),
            atv_apps=tuple((n, b) for n, b in _plugs(env("ATV_APPS", "")) if b),
            atv_credentials=env("ATV_CREDENTIALS", cls.atv_credentials),
            bluetti_bucket=env("BLUETTI_BUCKET", cls.bluetti_bucket),
            bluetti_stations=_stations(env("BLUETTI_STATIONS", "")) or cls.bluetti_stations,
            protect_host=env("PROTECT_HOST", ""),
            protect_user=env("PROTECT_USER", ""),
            protect_password=env("PROTECT_PASSWORD", ""),
            protect_cameras=tuple((n, label or n) for n, label in _plugs(env("PROTECT_CAMERAS", ""))),
            camera_refresh_seconds=max(10, int(env("CAMERA_REFRESH_SECONDS", cls.camera_refresh_seconds))),
            bluelink_user=env("BLUELINK_USER", ""),
            bluelink_password=env("BLUELINK_PASSWORD", ""),
            bluelink_pin=env("BLUELINK_PIN", ""),
            car_refresh_seconds=max(300, int(env("CAR_REFRESH_SECONDS", cls.car_refresh_seconds))),
            car_wake_min_seconds=max(600, int(env("CAR_WAKE_MIN_SECONDS", cls.car_wake_min_seconds))),
            car_climate_temp=float(env("CAR_CLIMATE_TEMP", cls.car_climate_temp)),
            car_climate_minutes=max(1, min(30, int(env("CAR_CLIMATE_MINUTES", cls.car_climate_minutes)))),
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


def _stations(value: str) -> tuple[tuple[str, float | None], ...]:
    """"AP300=2764.8,AC180" -> (("AP300", 2764.8), ("AC180", None))."""
    out = []
    for model, wh in _plugs(value):
        try:
            out.append((model, float(wh) if wh else None))
        except ValueError:
            out.append((model, None))
    return tuple(out)
