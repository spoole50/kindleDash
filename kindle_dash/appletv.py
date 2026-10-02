"""Apple TV over the LAN (pyatv): remote buttons, app launches, power, now playing.

Pairing is done from /admin, once per protocol, with a PIN the TV shows:
Companion for buttons, apps and power; AirPlay for what's playing. The
credentials are kept in a JSON file in the container's /data volume.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from typing import Callable

log = logging.getLogger("kindle_dash.appletv")

PROTOCOLS = ("companion", "airplay")  # pairing order: buttons first, then now playing
REMOTE_KEYS = ("up", "down", "left", "right", "select", "menu", "home", "play_pause", "skip_forward", "skip_backward")
AUDIO_KEYS = ("volume_up", "volume_down")
RETRY_S = 30


@dataclass(frozen=True)
class NowPlaying:
    app: str = ""
    title: str = ""
    subtitle: str = ""  # artist, or series and episode
    state: str = ""  # "playing", "paused", "idle", ...
    position: int | None = None  # seconds
    total: int | None = None


def _now_playing(playing, app) -> NowPlaying:
    sub = playing.artist or playing.series_name or ""
    if playing.series_name and playing.season_number and playing.episode_number:
        sub = f"{playing.series_name} · S{playing.season_number} E{playing.episode_number}"
    return NowPlaying(
        app=(app.name if app else "") or "",
        title=playing.title or "",
        subtitle=sub,
        state=playing.device_state.name.lower() if playing.device_state else "",
        position=playing.position,
        total=playing.total_time,
    )


class AppleTV:
    """One Apple TV: a connection kept open while paired, and what the TV page shows."""

    def __init__(self, host: str, credentials_path: str, on_change: Callable[[], None] = lambda: None):
        self.host = host
        self.path = credentials_path
        self.on_change = on_change  # called (in the event loop) when anything shown changes
        self.creds: dict[str, str] = self._load()
        self.atv = None
        self.error: str | None = None
        self.power_on: bool | None = None
        self.playing: NowPlaying | None = None
        self.installed_apps: list[tuple[str, str]] = []  # (name, bundle id), for /admin
        self.pairing: tuple[str, object] | None = None  # (protocol, pyatv PairingHandler)
        self._lock = asyncio.Lock()

    # ---------------------------------------------------------------- credentials

    def _load(self) -> dict[str, str]:
        try:
            with open(self.path) as fh:
                data = json.load(fh)
            return {k: v for k, v in data.items() if k in PROTOCOLS and isinstance(v, str)}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            log.warning("can't read %s: %s", self.path, exc)
            return {}

    def _save(self) -> None:
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(self.creds, fh)
        os.replace(tmp, self.path)

    def paired(self, protocol: str = "companion") -> bool:
        return protocol in self.creds

    @property
    def connected(self) -> bool:
        return self.atv is not None

    # ---------------------------------------------------------------- connection

    async def _config(self):
        import pyatv
        from pyatv.const import Protocol

        confs = await pyatv.scan(asyncio.get_running_loop(), hosts=[self.host], timeout=5)
        if not confs:
            raise ConnectionError(f"no Apple TV answered at {self.host}")
        conf = confs[0]
        for proto, cred in self.creds.items():
            conf.set_credentials(Protocol.Companion if proto == "companion" else Protocol.AirPlay, cred)
        return conf

    async def connect(self) -> None:
        import pyatv
        from pyatv.const import PowerState

        async with self._lock:
            if self.atv is not None or not self.paired() or self.pairing:
                return
            conf = await self._config()
            atv = await pyatv.connect(conf, asyncio.get_running_loop())
            atv.listener = self
            atv.power.listener = self
            self.power_on = atv.power.power_state == PowerState.On
            if self.paired("airplay"):
                atv.push_updater.listener = self
                atv.push_updater.start()
            try:
                self.installed_apps = sorted((a.name or a.identifier, a.identifier) for a in await atv.apps.app_list())
            except Exception as exc:
                log.info("app list unavailable: %s", exc)
            self.atv, self.error = atv, None
            log.info("connected to Apple TV at %s", self.host)
        self.on_change()

    async def disconnect(self) -> None:
        atv, self.atv = self.atv, None
        if atv is not None:
            try:
                await asyncio.gather(*atv.close(), return_exceptions=True)
            except Exception:
                pass

    async def run(self) -> None:
        """Background task: (re)connect whenever paired and not connected."""
        while True:
            if self.atv is None and self.paired() and not self.pairing:
                try:
                    await self.connect()
                except Exception as exc:
                    self.error = f"{type(exc).__name__}: {exc}"
                    log.warning("Apple TV connect failed: %s", self.error)
                    self.on_change()
            await asyncio.sleep(RETRY_S)

    # pyatv listeners -------------------------------------------------

    def connection_lost(self, exception) -> None:
        log.warning("Apple TV connection lost: %s", exception)
        self.atv, self.error = None, f"connection lost: {exception}"
        self.on_change()

    def connection_closed(self) -> None:
        self.atv = None
        self.on_change()

    def powerstate_update(self, old_state, new_state) -> None:
        from pyatv.const import PowerState

        self.power_on = new_state == PowerState.On
        self.on_change()

    def playstatus_update(self, updater, playstatus) -> None:
        app = None
        try:
            app = self.atv.metadata.app if self.atv else None
        except Exception:
            pass
        playing = _now_playing(playstatus, app)
        if playing != self.playing:
            self.playing = playing
            self.on_change()

    def playstatus_error(self, updater, exception) -> None:
        log.info("now playing update failed: %s", exception)

    # ---------------------------------------------------------------- commands

    async def command(self, action: str) -> bool:
        """A remote key, "volume_up"/"volume_down", "power" (toggle) or "app:<bundle id>"."""
        try:
            if self.atv is None:
                await self.connect()
            atv = self.atv
            if atv is None:
                raise ConnectionError("not paired" if not self.paired() else "not connected")
            if action in REMOTE_KEYS:
                await getattr(atv.remote_control, action)()
            elif action in AUDIO_KEYS:
                await getattr(atv.audio, action)()
            elif action == "power":
                await (atv.power.turn_off() if self.power_on else atv.power.turn_on())
            elif action.startswith("app:"):
                await atv.apps.launch_app(action[4:])
            else:
                raise ValueError(f"unknown action {action!r}")
            return True
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            log.warning("Apple TV %s failed: %s", action, self.error)
            await self.disconnect()  # reconnect from scratch next time
            self.on_change()
            return False

    # ---------------------------------------------------------------- pairing

    async def pair_begin(self, protocol: str) -> None:
        """Start pairing one protocol; the TV shows a PIN for pair_pin()."""
        import pyatv
        from pyatv.const import Protocol

        if protocol not in PROTOCOLS:
            raise ValueError(f"protocol must be one of {', '.join(PROTOCOLS)}")
        await self.pair_cancel()
        await self.disconnect()
        conf = await self._config()
        handler = await pyatv.pair(conf, Protocol.Companion if protocol == "companion" else Protocol.AirPlay, asyncio.get_running_loop())
        try:
            await handler.begin()
        except Exception:
            await handler.close()
            raise
        self.pairing = (protocol, handler)
        log.info("pairing %s: PIN shown on the TV", protocol)

    async def pair_pin(self, pin: str) -> bool:
        """Finish pairing with the PIN from the TV; True if it worked."""
        if not self.pairing:
            raise RuntimeError("no pairing in progress")
        protocol, handler = self.pairing
        try:
            handler.pin(pin)
            await handler.finish()
            ok = handler.has_paired
            if ok:
                self.creds[protocol] = handler.service.credentials
                self._save()
                log.info("paired %s", protocol)
            return ok
        finally:
            self.pairing = None
            await handler.close()
            self.on_change()

    async def pair_cancel(self) -> None:
        if self.pairing:
            _, handler = self.pairing
            self.pairing = None
            try:
                await handler.close()
            except Exception:
                pass

    async def close(self) -> None:
        await self.pair_cancel()
        await self.disconnect()
