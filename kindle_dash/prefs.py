"""The settings page's choices, saved across restarts, and what the Kindle reports about itself.

The server keeps the choices and draws with them (the theme), or hands them to
the Kindle in a header on every image (home tab, idle timer, idle light-off).
Orientation, brightness and warmth live on the Kindle: the server only sends
it commands and shows what it last reported.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, fields
from datetime import datetime, time
from typing import NamedTuple

log = logging.getLogger("kindle_dash")

THEMES = ("light", "dark", "auto")
IDLE_CHOICES = (0, 120, 300, 900, 1800)  # seconds before going home (0 = stay put)
KINDLE_SCRIPT_VERSION = "2026-10-19.3"  # KD_VERSION in kindle/kindledash/kindledash.sh; older Kindles lack the settings commands
DARK_FULL_REFRESH_EVERY = 6  # dark pages ghost more, so flash-clear them more often


@dataclass(frozen=True)
class Prefs:
    theme: str = "light"
    home: str = "solar"
    idle_return: int = 300
    idle_light_off: bool = True

    def valid(self, pages: tuple[str, ...]) -> Prefs:
        """These prefs with anything unknown put back to its default."""
        d = Prefs()
        return Prefs(
            self.theme if self.theme in THEMES else d.theme,
            self.home if self.home in pages else d.home,
            self.idle_return if self.idle_return in IDLE_CHOICES else d.idle_return,
            bool(self.idle_light_off),
        )


def load(path: str, pages: tuple[str, ...]) -> Prefs:
    if not path:
        return Prefs()
    try:
        with open(path) as f:
            raw = json.load(f)
        known = {f.name for f in fields(Prefs)}
        return Prefs(**{k: v for k, v in raw.items() if k in known}).valid(pages)
    except FileNotFoundError:
        return Prefs()
    except (OSError, ValueError, TypeError) as exc:
        log.warning("settings: can't read %s (%s); using defaults", path, exc)
        return Prefs()


def save(path: str, prefs: Prefs) -> None:
    """Write a whole new file and rename it, so a crash never leaves half a file."""
    if not path:
        return
    try:
        tmp = f"{path}.tmp"
        with open(tmp, "w") as f:
            json.dump(asdict(prefs), f)
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("settings: can't save %s (%s); kept until restart", path, exc)


def is_dark(theme: str, now: datetime, dark_from: str, dark_until: str) -> bool:
    """`now` is local time. AUTO is dark from dark_from to dark_until, across midnight if need be."""
    if theme != "auto":
        return theme == "dark"
    start, end, t = time.fromisoformat(dark_from), time.fromisoformat(dark_until), now.time()
    return start <= t < end if start <= end else t >= start or t < end


class KindleDevice(NamedTuple):
    """What the Kindle sent with its last image fetch (None = it didn't say)."""

    light: int | None = None  # front light, 0..light_max
    light_max: int | None = None
    warm: int | None = None  # warm light (amber), 0..warm_max; None if the Kindle has none
    wifi: int | None = None  # signal bars, of wifi_max (the Kindle's status bar: 0..5)
    wifi_max: int | None = None
    uptime_s: int | None = None
    version: str | None = None  # KD_VERSION of its scripts
    ip: str | None = None
    rotate: int | None = None

    @property
    def scripts_current(self) -> bool:
        return bool(self.version) and self.version >= KINDLE_SCRIPT_VERSION


WARM_MAX = 24  # the PW11's currentAmberLevel range (its probe: 0..24); powerd has no property for it


def device_from(params, ip: str | None, rotate: int) -> KindleDevice:
    """A KindleDevice from an image fetch's query parameters; anything malformed is left out."""

    def num(name: str, lo: int, hi: int) -> int | None:
        try:
            v = int(params.get(name))
        except (TypeError, ValueError):
            return None
        return v if lo <= v <= hi else None

    version = params.get("ver") or None
    if version and not (len(version) <= 20 and all(ch.isalnum() or ch in ".-" for ch in version)):
        version = None
    return KindleDevice(
        light=num("light", 0, 100),
        light_max=num("lightmax", 1, 100),
        warm=num("warm", 0, 100),
        wifi=num("wifi", 0, 10),
        wifi_max=num("wifimax", 1, 10),
        uptime_s=num("up", 0, 10**9),
        version=version,
        ip=ip,
        rotate=rotate,
    )


def header(prefs: Prefs, dark: bool) -> str:
    """X-Kindle-Settings, for the Kindle to apply: space-separated key=value."""
    parts = [f"home={prefs.home}", f"idle={prefs.idle_return}", f"idlelight={int(prefs.idle_light_off)}", f"theme={'dark' if dark else 'light'}"]
    if dark:
        parts.append(f"full={DARK_FULL_REFRESH_EVERY}")
    return " ".join(parts)
