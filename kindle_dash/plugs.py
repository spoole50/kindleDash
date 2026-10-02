"""Kasa smart plugs over the LAN (python-kasa). No cloud, no Apple Home changes.

Plugs are addressed by IP: broadcast discovery doesn't reach them from the Pi,
so give them DHCP reservations. State lives on the plug, so a change made here
shows up in Apple Home and vice versa.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import date, timedelta

log = logging.getLogger("kindle_dash.plugs")

TIMEOUT_S = 5
HISTORY_MAX_AGE_S = 600  # daily totals change slowly; re-read them this often


@dataclass(frozen=True)
class Plug:
    """What the lights page shows for one plug. `on` is None when unreachable."""

    host: str
    name: str
    on: bool | None
    kind: str = ""  # what's plugged in, from KASA_HOSTS ("light", "ev charger", ...)
    power_w: float | None = None  # energy monitoring, where the plug has it
    today_kwh: float | None = None
    last30_kwh: float | None = None  # the 30 full days before today, like the Kasa app's "past 30 days"


def _reading(dev, feature: str) -> float | None:
    f = dev.features.get(feature)
    try:
        return None if f is None or f.value is None else float(f.value)
    except Exception:
        return None


class KasaPlugs:
    def __init__(self, hosts: tuple[str, ...], kinds: dict[str, str] | None = None):
        self.hosts = hosts
        self.kinds = kinds or {}
        self._devices: dict[str, object] = {}  # host -> kasa Device, once connected
        self._names: dict[str, str] = {}  # last known alias, kept while unreachable
        self.plugs: list[Plug] = [Plug(h, h, None, self.kinds.get(h, "")) for h in hosts]
        self._toggles: dict[str, int] = {}  # per host, bumped by each toggle
        self._history: dict[str, tuple[float, date, float | None]] = {}  # host -> (read at, for day, last-30-days kWh)

    async def _device(self, host: str):
        dev = self._devices.get(host)
        if dev is None:
            from kasa import Discover

            dev = await Discover.discover_single(host, discovery_timeout=TIMEOUT_S, timeout=TIMEOUT_S)
            if dev is None:
                raise ConnectionError(f"no Kasa device at {host}")
            self._devices[host] = dev
        return dev

    async def _read(self, host: str) -> Plug:
        try:
            dev = await self._device(host)
            await dev.update()
            self._names[host] = (dev.alias or "").strip() or host
            return Plug(
                host,
                self._names[host],
                bool(dev.is_on),
                self.kinds.get(host, ""),
                # whole watts: finer would redraw the page for noise
                None if (w := _reading(dev, "current_consumption")) is None else float(round(w)),
                _reading(dev, "consumption_today"),
                await self._last30(host, dev),
            )
        except Exception as exc:
            log.warning("plug %s: %s", host, exc)
            await self._forget(host)  # reconnect from scratch next time
            return Plug(host, self._names.get(host, host), None, self.kinds.get(host, ""))

    async def _last30(self, host: str, dev) -> float | None:
        """kWh over the 30 full days before today, from the plug's daily totals (this month and last)."""
        today = date.today()
        cached = self._history.get(host)
        if cached and cached[1] == today and time.monotonic() - cached[0] < HISTORY_MAX_AGE_S:
            return cached[2]
        energy = dev.modules.get("Energy")
        if energy is None:
            return None
        try:
            days: dict[date, float] = {}
            prev = today.replace(day=1) - timedelta(days=1)
            for month in (prev, today):
                for day, kwh in (await energy.get_daily_stats(year=month.year, month=month.month, kwh=True)).items():
                    days[month.replace(day=int(day))] = float(kwh)
            since = today - timedelta(days=30)
            total = sum(v for d, v in days.items() if since <= d < today)
        except Exception as exc:
            log.warning("plug %s daily totals: %s", host, exc)
            return cached[2] if cached else None
        self._history[host] = (time.monotonic(), today, total)
        return total

    async def _forget(self, host: str) -> None:
        dev = self._devices.pop(host, None)
        if dev is not None:
            try:
                await dev.disconnect()
            except Exception:
                pass

    async def refresh(self) -> bool:
        """Read every plug; True if anything shown changed."""
        before = dict(self._toggles)
        fresh = await asyncio.gather(*(self._read(h) for h in self.hosts))
        # a plug toggled while this ran keeps the state the toggle read back
        plugs = [new if self._toggles.get(new.host) == before.get(new.host) else cur for new, cur in zip(fresh, self.plugs)]
        changed = plugs != self.plugs
        self.plugs = plugs
        return changed

    async def toggle(self, host: str) -> bool:
        """Flip one plug and re-read it; True on success."""
        self._toggles[host] = self._toggles.get(host, 0) + 1
        try:
            dev = await self._device(host)
            await dev.update()
            turn_on = not dev.is_on
            await (dev.turn_on() if turn_on else dev.turn_off())
            log.info("plug %s (%s) turned %s", host, dev.alias, "on" if turn_on else "off")
            ok = True
        except Exception as exc:
            log.warning("toggle %s failed: %s", host, exc)
            ok = False
        fresh = await self._read(host)
        self.plugs = [fresh if p.host == host else p for p in self.plugs]
        return ok

    async def close(self) -> None:
        for host in list(self._devices):
            await self._forget(host)
