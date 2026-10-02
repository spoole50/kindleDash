"""Kasa smart plugs over the LAN (python-kasa). No cloud, no Apple Home changes.

Plugs are addressed by IP: broadcast discovery doesn't reach them from the Pi,
so give them DHCP reservations. State lives on the plug, so a change made here
shows up in Apple Home and vice versa.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

log = logging.getLogger("kindle_dash.plugs")

TIMEOUT_S = 5


@dataclass(frozen=True)
class Plug:
    """What the lights page shows for one plug. `on` is None when unreachable."""

    host: str
    name: str
    on: bool | None


class KasaPlugs:
    def __init__(self, hosts: tuple[str, ...]):
        self.hosts = hosts
        self._devices: dict[str, object] = {}  # host -> kasa Device, once connected
        self._names: dict[str, str] = {}  # last known alias, kept while unreachable
        self.plugs: list[Plug] = [Plug(h, h, None) for h in hosts]
        self._toggles: dict[str, int] = {}  # per host, bumped by each toggle

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
            return Plug(host, self._names[host], bool(dev.is_on))
        except Exception as exc:
            log.warning("plug %s: %s", host, exc)
            await self._forget(host)  # reconnect from scratch next time
            return Plug(host, self._names.get(host, host), None)

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
