"""FastAPI app: a background poller renders; requests only serve cached bytes."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import html
import logging
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import asdict
from urllib.parse import parse_qs
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from PIL import Image

from . import render, touch
from .config import Settings
from .data import Snapshot
from .appletv import PROTOCOLS as TV_PROTOCOLS
from .appletv import AppleTV
from .bluetti import BluettiSnapshot
from .plugs import KasaPlugs

log = logging.getLogger("kindle_dash")

KINDLE_REPORT_MAX_AGE_S = 15 * 60  # hide the Kindle's battery after this long without a report
PAGE_NAMES = tuple(name for name, _ in render.PAGES)
CONFIRM_S = 10  # how long a "tap again" prompt waits for the second tap
TV_REDRAW = ("play_pause", "skip_forward", "skip_backward", "power")  # change what's playing: redraw after
TV_SETTLE_S = 1.5  # let the Apple TV report the change before redrawing


class State:
    """Last good render plus everything /status and /health need."""

    def __init__(self, settings: Settings):
        self.s = settings
        self.lock = threading.Lock()
        self.render_lock = threading.Lock()  # one render at a time: poll cycle or battery change
        self.lights_lock = threading.Lock()  # lights renders are quick; don't queue them behind the solar one
        self.tv_lock = threading.Lock()
        self.bluetti_lock = threading.Lock()
        self.bluetti: BluettiSnapshot | None = None
        self.bluetti_error: str | None = None
        self.bluetti_stale_since: datetime | None = None
        self.snapshot: Snapshot | None = None
        # solar, per layout: last render from fresh data
        self.good_images: dict[str, Image.Image] = {}
        # what's being served, per (page, layout)
        self.images: dict[tuple[str, str], Image.Image] = {
            (page, lay): render.placeholder(msg, self.now(), settings.zone, lay, page)
            for page, msg in (("solar", "Waiting for data…"), ("bluetti", "Waiting for data…"), ("lights", "Connecting to plugs…"))
            for lay in render.LAYOUTS
        }
        self.plugs = KasaPlugs(settings.kasa_hosts, dict(settings.kasa_kinds))
        self.confirm: tuple[str, float] | None = None  # (plug host, monotonic deadline) awaiting a second tap
        self.lights_polled = False
        self.tv = AppleTV(settings.atv_host, settings.atv_credentials, self._tv_changed) if settings.atv_host else None
        self.last_render: datetime | None = None
        self.last_success: datetime | None = None
        self.stale_since: datetime | None = None
        self.last_error: str | None = None
        self._png: dict[tuple[str, str, int], tuple[bytes, str]] = {}
        # bumped by POST /rotate; the Kindle polls it and rotates once per increase
        self.rotate_requests = 0
        # the display's own battery, sent as query params on its image fetches
        self.kindle: render.KindleBattery | None = None
        self.kindle_at: datetime | None = None
        # (page, layout, rotate, when) of the Kindle's last image fetch, for /admin
        self.kindle_view: tuple[str, str, int, datetime] | None = None
        self.render_tv()

    @staticmethod
    def now() -> datetime:
        return datetime.now(timezone.utc)

    def _publish(self, images: dict[str, Image.Image], page: str = "solar") -> None:
        with self.lock:
            for lay, im in images.items():
                self.images[(page, lay)] = im
            self._png = {k: v for k, v in self._png.items() if k[0] != page}

    def report_kindle(self, battery: str | None, charging: str | None) -> bool:
        """Record a battery report; True if it changes what the header shows.

        Malformed values are ignored so the image is still served.
        """
        try:
            level = int(battery)
        except (TypeError, ValueError):
            return False
        if not 0 <= level <= 100:
            return False
        shown = self.kindle_battery()
        self.kindle = render.KindleBattery(level, charging in ("1", "true", "yes"))
        self.kindle_at = self.now()
        return self.kindle != shown

    def kindle_battery(self) -> render.KindleBattery | None:
        """The last report, if the Kindle has fetched recently."""
        if self.kindle_at and (self.now() - self.kindle_at).total_seconds() < KINDLE_REPORT_MAX_AGE_S:
            return self.kindle
        return None

    def _render_all(self, snap: Snapshot) -> dict[str, Image.Image]:
        kindle = self.kindle_battery()
        return {
            lay: render.render(snap, self.s.zone, self.s.chart_hours, self.s.spark_hours, layout=lay, kindle=kindle)
            for lay in render.LAYOUTS
        }

    def rerender(self) -> None:
        """Redraw the current data, e.g. so a new battery report shows in the image the Kindle is fetching."""
        self.render_lights()
        self.render_tv()
        self.render_bluetti()
        with self.render_lock:
            if self.snapshot is None or self.stale_since is not None:
                return  # nothing fresh to redraw; the next good poll picks the report up
            self.good_images = self._render_all(self.snapshot)
            self._publish(self.good_images)

    def armed(self) -> str | None:
        """The plug waiting for a confirming tap, if its prompt hasn't expired."""
        if self.confirm and time.monotonic() < self.confirm[1]:
            return self.confirm[0]
        return None

    def needs_confirm(self, host: str) -> bool:
        return dict(self.s.kasa_kinds).get(host, "").lower() in self.s.confirm_kinds

    def tv_status(self) -> str | None:
        """Why the TV page can't show what's playing, or None when it can."""
        tv = self.tv
        if tv is None:
            return "No Apple TV configured (ATV_HOST)"
        if tv.pairing:
            return "Pairing: enter the PIN from the TV at /admin"
        if not tv.paired():
            return "Pair the Apple TV at /admin"
        if not tv.connected:
            return f"Apple TV not connected: {tv.error}" if tv.error else "Connecting to the Apple TV\u2026"
        if not tv.paired("airplay"):
            return "Connected. Pair \u201cnow playing\u201d at /admin"
        return None

    def render_tv(self) -> None:
        with self.tv_lock:
            tv, kindle, status = self.tv, self.kindle_battery(), self.tv_status()
            playing, power = (tv.playing, tv.power_on) if tv else (None, None)
            self._publish(
                {lay: render.render_tv(playing, status, power, self.s.atv_apps, self.now(), self.s.zone, lay, kindle) for lay in render.LAYOUTS},
                "tv",
            )

    def _tv_changed(self) -> None:
        """AppleTV's callback, in the event loop: redraw off it."""
        try:
            asyncio.get_running_loop().run_in_executor(None, self.render_tv)
        except RuntimeError:  # no loop (tests): draw now
            self.render_tv()

    def render_bluetti(self) -> None:
        """Redraw the Bluetti page from the last fetch, badged STALE while fetches fail."""
        if self.bluetti is None:
            return
        with self.bluetti_lock:
            kindle = self.kindle_battery()
            imgs = {}
            for lay in render.LAYOUTS:
                img = render.render_bluetti(self.bluetti, self.now(), self.s.zone, lay, kindle, self.s.chart_hours)
                imgs[lay] = render.stale_badge(img, self.bluetti_stale_since, self.s.zone) if self.bluetti_stale_since else img
            self._publish(imgs, "bluetti")

    def update_bluetti(self, fetch) -> None:
        try:
            self.bluetti = fetch()
            self.bluetti_error = self.bluetti_stale_since = None
        except Exception as exc:  # keep showing the last data, badged
            log.warning("bluetti refresh failed: %s", exc)
            self.bluetti_error = f"{type(exc).__name__}: {exc}"
            if self.bluetti is None:
                self._publish({lay: render.placeholder("InfluxDB unavailable", self.now(), self.s.zone, lay, "bluetti") for lay in render.LAYOUTS}, "bluetti")
                return
            self.bluetti_stale_since = self.bluetti_stale_since or self.bluetti.fetched_at
        self.render_bluetti()

    def render_lights(self) -> None:
        """Redraw the lights page from the last plug states (once they've been read)."""
        if not self.lights_polled:
            return
        with self.lights_lock:
            plugs, kindle, confirm = list(self.plugs.plugs), self.kindle_battery(), self.armed()
            self._publish(
                {lay: render.render_lights(plugs, self.now(), self.s.zone, lay, kindle, confirm) for lay in render.LAYOUTS}, "lights"
            )

    async def poll_lights(self) -> None:
        await self.plugs.refresh()
        self.lights_polled = True
        await asyncio.to_thread(self.render_lights)  # every poll, so "Updated" stays current

    def update(self, fetch) -> None:
        """One poll cycle. `fetch` returns a Snapshot or raises."""
        try:
            snap = fetch()
            with self.render_lock:
                imgs = self._render_all(snap)
                self._apply(snap, imgs)
        except Exception as exc:  # keep serving the last good image
            log.warning("refresh failed: %s", exc)
            with self.render_lock:
                self.last_error = f"{type(exc).__name__}: {exc}"
                if self.stale_since is None:
                    self.stale_since = self.last_success or self.now()
                    if self.good_images:
                        self._publish({lay: render.stale_badge(im, self.stale_since, self.s.zone) for lay, im in self.good_images.items()})
                    else:
                        self._publish(
                            {lay: render.placeholder("InfluxDB unavailable", self.now(), self.s.zone, lay) for lay in render.LAYOUTS}
                        )

    def _apply(self, snap: Snapshot, imgs: dict[str, Image.Image]) -> None:
        self.snapshot = snap
        self.good_images = imgs
        self.last_success = self.last_render = snap.fetched_at
        self.stale_since = None
        self.last_error = None
        self._publish(imgs)

    def png(self, layout: str, rotate: int, page: str = "solar") -> tuple[bytes, str]:
        """Encoded once per (page, layout, rotation) per render; requests just read the cache."""
        with self.lock:
            cached = self._png.get((page, layout, rotate))
            if cached is None:
                data = render.to_png(self.images[(page, layout)], rotate)
                cached = (data, '"' + hashlib.md5(data).hexdigest() + '"')
                self._png[(page, layout, rotate)] = cached
            return cached

    def healthy(self) -> bool:
        return self.last_render is not None and (self.now() - self.last_render).total_seconds() < self.s.stale_after_seconds


async def _poll(state: State, fetch) -> None:
    while True:
        await asyncio.to_thread(state.update, fetch)
        await asyncio.sleep(state.s.refresh_seconds)


async def _poll_bluetti(state: State, fetch) -> None:
    while True:
        await asyncio.to_thread(state.update_bluetti, fetch)
        await asyncio.sleep(state.s.refresh_seconds)


async def _poll_lights(state: State) -> None:
    while True:
        try:
            await state.poll_lights()
        except Exception:
            log.exception("lights poll failed")
        await asyncio.sleep(state.s.lights_refresh_seconds)


def create_app(settings: Settings | None = None, fetch=None, poll: bool = True, fetch_bluetti=None) -> FastAPI:
    settings = settings or Settings.from_env()
    state = State(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        source = bluetti_source = None
        nonlocal fetch, fetch_bluetti
        if fetch is None:
            from .data import InfluxSource

            source = InfluxSource(settings)
            fetch = source.fetch
        if fetch_bluetti is None and poll and settings.bluetti_bucket:
            from .bluetti import BluettiSource

            bluetti_source = BluettiSource(settings)
            fetch_bluetti = bluetti_source.fetch
        tasks = [asyncio.create_task(_poll(state, fetch)), asyncio.create_task(_poll_lights(state))] if poll else []
        if poll and fetch_bluetti:
            tasks.append(asyncio.create_task(_poll_bluetti(state, fetch_bluetti)))
        if poll and state.tv:
            tasks.append(asyncio.create_task(state.tv.run()))
        yield
        for task in tasks:
            task.cancel()
        await state.plugs.close()
        if state.tv:
            await state.tv.close()
        for src in (source, bluetti_source):
            if src:
                src.close()

    app = FastAPI(title="kindle-solar-dashboard", lifespan=lifespan)
    app.state.dash = state

    def check_view(rotate: int, page: str) -> None:
        if rotate not in (0, 90, 180, 270):
            raise HTTPException(400, "rotate must be 0, 90, 180 or 270")
        if page not in PAGE_NAMES:
            raise HTTPException(400, f"page must be one of {', '.join(PAGE_NAMES)}")

    # ------------------------------------------------------------------ auth

    def kindle(request: Request) -> None:
        """The Kindle's routes: CONTROL_TOKEN in X-Kindle-Token, from CONTROL_ALLOW_IPS if that's set."""
        if not settings.control_token:
            raise HTTPException(503, "set CONTROL_TOKEN")
        client = request.client.host if request.client else ""
        if settings.control_allow_ips and client not in settings.control_allow_ips:
            log.warning("%s refused: %s not in CONTROL_ALLOW_IPS", request.url.path, client)
            raise HTTPException(403, "not allowed")
        if not _same(request.headers.get("x-kindle-token", ""), settings.control_token):
            log.warning("%s refused: bad token from %s", request.url.path, client)
            raise HTTPException(401, "bad token")

    def admin(request: Request) -> None:
        """/admin: HTTP Basic auth as "admin" / ADMIN_PASSWORD; posts must come from this site."""
        if not settings.admin_password:
            raise HTTPException(503, "set ADMIN_PASSWORD")
        user, password = _basic_auth(request.headers.get("authorization", ""))
        if not (_same(user, "admin") & _same(password, settings.admin_password)):
            raise HTTPException(401, "login required", headers={"WWW-Authenticate": 'Basic realm="kindleDash"'})
        # the browser sends the login with any request to this site, so refuse
        # posts started by another site's page
        origin = request.headers.get("origin")
        if request.method == "POST" and origin and origin.split("://", 1)[-1] != request.headers.get("host"):
            raise HTTPException(403, "cross-site request")

    Kindle, Admin = Depends(kindle), Depends(admin)

    # ------------------------------------------------------------------ Kindle

    def serve(request: Request, layout: str, rotate: int, page: str) -> Response:
        check_view(rotate, page)
        state.kindle_view = (page, layout, rotate, state.now())
        if state.report_kindle(request.query_params.get("battery"), request.query_params.get("charging")):
            state.rerender()
        data, etag = state.png(layout, rotate, page)
        headers = {"ETag": etag, "Cache-Control": "no-cache"}
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=headers)
        return Response(data, media_type="image/png", headers=headers)

    @app.get("/dashboard.png", dependencies=[Kindle])
    @app.get("/portrait.png", dependencies=[Kindle])
    def portrait(request: Request, rotate: int = Query(0), page: str = Query("solar")):
        """1236 x 1648 portrait layout (the Kindle's native orientation). ?page=solar|bluetti|lights|tv

        The Kindle client adds ?battery=<0-100>&charging=<0|1>; the next render shows it in the header.
        """
        return serve(request, "portrait", rotate, page)

    @app.get("/landscape.png", dependencies=[Kindle])
    def landscape(request: Request, rotate: int = Query(0), page: str = Query("solar")):
        """1648 x 1236 landscape layout; on the Kindle use ?rotate=90 or 270."""
        return serve(request, "landscape", rotate, page)

    @app.post("/tap", response_class=PlainTextResponse, dependencies=[Kindle])
    async def tap(x: int, y: int, rotate: int = Query(0), page: str = Query("solar")):
        """A tap at raw touch (x, y) on the page the Kindle is showing.

        Replies 204 if nothing was hit, otherwise lines for the Kindle to act on:
        `flash <left> <top> <width> <height>` (panel pixels), `page <name>`, `redraw`.
        """
        check_view(rotate, page)
        layout = "landscape" if rotate in (90, 270) else "portrait"
        size = render.size(layout)
        px, py = touch.raw_to_panel(x, y, settings)
        ix, iy = touch.panel_to_image(px, py, rotate, size)
        target = touch.hit(render.targets(page, layout, state.plugs.plugs, settings.atv_apps), ix, iy)
        log.info("tap raw=%d,%d panel=%d,%d %s/%s image=%d,%d -> %s", x, y, px, py, page, layout, ix, iy, target.action if target else "-")
        if target is None:
            return Response(status_code=204)
        lines = ["flash %d %d %d %d" % touch.box_to_panel(target.flash, rotate, size)]
        kind, _, arg = target.action.partition(":")
        armed, state.confirm = state.armed(), None  # any tap ends a pending prompt
        if kind == "page":
            lines.append(f"page {arg}" if arg != page else "redraw")
            if armed:
                await asyncio.to_thread(state.render_lights)  # don't come back to a stale prompt
        elif kind == "plug":
            if state.needs_confirm(arg) and armed != arg:
                state.confirm = (arg, time.monotonic() + CONFIRM_S)
                log.info("plug %s needs a second tap within %d s", arg, CONFIRM_S)
            else:
                await state.plugs.toggle(arg)
            await asyncio.to_thread(state.render_lights)
            lines.append("redraw")
        elif kind == "atv":
            # remote keys just flash (the Kindle puts its image back); ones that
            # change what's playing, and failures, redraw the page
            ok = await state.tv.command(arg) if state.tv else False
            if not ok or arg in TV_REDRAW or arg.startswith("app:"):
                if ok:
                    await asyncio.sleep(TV_SETTLE_S)
                await asyncio.to_thread(state.render_tv)
                lines.append("redraw")
        return "\n".join(lines) + "\n"

    @app.get("/rotate/count", response_class=PlainTextResponse, dependencies=[Kindle])
    def rotate_count():
        """Polled by the Kindle; it rotates once each time this goes up (the Rotate button on /admin)."""
        return str(state.rotate_requests)

    # ------------------------------------------------------------------ admin

    @app.get("/admin", response_class=HTMLResponse, dependencies=[Admin])
    def admin_page(done: str = ""):
        """What the Kindle shows, the plugs, and buttons to rotate the Kindle and toggle plugs."""
        return admin_html(state, done)

    @app.get("/admin/image.png", dependencies=[Admin])
    def admin_image(page: str = Query("solar"), layout: str = Query("portrait")):
        """A page upright, as the Kindle would show it (before rotation)."""
        check_view(0, page)
        if layout not in render.LAYOUTS:
            raise HTTPException(400, f"layout must be one of {', '.join(render.LAYOUTS)}")
        data, _ = state.png(layout, 0, page)
        return Response(data, media_type="image/png", headers={"Cache-Control": "no-store"})

    @app.post("/admin/rotate", dependencies=[Admin])
    def admin_rotate():
        state.rotate_requests += 1
        log.info("rotate requested from /admin (count %d)", state.rotate_requests)
        return RedirectResponse("/admin?done=rotate", status_code=303)

    @app.post("/admin/plug", dependencies=[Admin])
    async def admin_plug(host: str = Query(...)):
        if host not in settings.kasa_hosts:
            raise HTTPException(404, "unknown plug")
        await state.plugs.toggle(host)
        await asyncio.to_thread(state.render_lights)
        return RedirectResponse("/admin?done=plug", status_code=303)

    def tv_or_404() -> AppleTV:
        if state.tv is None:
            raise HTTPException(404, "no Apple TV configured (ATV_HOST)")
        return state.tv

    @app.post("/admin/tv/pair", dependencies=[Admin])
    async def admin_tv_pair(protocol: str = Query(...)):
        """Start pairing; the TV shows a PIN to enter on /admin."""
        tv = tv_or_404()
        if protocol not in TV_PROTOCOLS:
            raise HTTPException(400, f"protocol must be one of {', '.join(TV_PROTOCOLS)}")
        try:
            await tv.pair_begin(protocol)
        except Exception as exc:
            tv.error = f"pairing failed to start: {type(exc).__name__}: {exc}"
            log.warning("%s", tv.error)
            return RedirectResponse("/admin?done=tv-error#tv", status_code=303)
        await asyncio.to_thread(state.render_tv)
        return RedirectResponse("/admin#tv", status_code=303)

    @app.post("/admin/tv/pin", dependencies=[Admin])
    async def admin_tv_pin(request: Request):
        tv = tv_or_404()
        form = parse_qs((await request.body()).decode(errors="replace"))
        pin = "".join(ch for ch in form.get("pin", [""])[0] if ch.isdigit())
        try:
            ok = await tv.pair_pin(pin)
        except Exception as exc:
            tv.error, ok = f"pairing failed: {type(exc).__name__}: {exc}", False
        if ok:
            asyncio.create_task(_connect_quietly(tv))
        return RedirectResponse(f"/admin?done={'tv-paired' if ok else 'tv-error'}#tv", status_code=303)

    @app.post("/admin/tv/cancel", dependencies=[Admin])
    async def admin_tv_cancel():
        tv = tv_or_404()
        await tv.pair_cancel()
        asyncio.create_task(_connect_quietly(tv))
        await asyncio.to_thread(state.render_tv)
        return RedirectResponse("/admin#tv", status_code=303)

    @app.get("/status", dependencies=[Admin])
    def status():
        iso = lambda t: t.isoformat() if t else None  # noqa: E731
        view = state.kindle_view
        return {
            "values": state.snapshot.to_json() if state.snapshot else None,
            "last_success": iso(state.last_success),
            "stale_since": iso(state.stale_since),
            "last_error": state.last_error,
            "healthy": state.healthy(),
            "kindle": {**state.kindle._asdict(), "reported_at": iso(state.kindle_at)} if state.kindle else None,
            "kindle_view": {"page": view[0], "layout": view[1], "rotate": view[2], "at": iso(view[3])} if view else None,
            "bluetti": {
                "stations": state.bluetti.to_json() if state.bluetti else None,
                "fetched_at": iso(state.bluetti.fetched_at) if state.bluetti else None,
                "error": state.bluetti_error,
            },
            "plugs": [asdict(p) for p in state.plugs.plugs] if state.lights_polled else None,
            "tv": {
                "connected": state.tv.connected,
                "paired": [p for p in TV_PROTOCOLS if state.tv.paired(p)],
                "power_on": state.tv.power_on,
                "playing": asdict(state.tv.playing) if state.tv.playing else None,
                "error": state.tv.error,
            }
            if state.tv
            else None,
        }

    @app.get("/health")
    def health():
        """Open, for Docker's healthcheck; says only whether rendering is current."""
        ok = state.healthy()
        age = (state.now() - state.last_render).total_seconds() if state.last_render else None
        return JSONResponse({"ok": ok, "render_age_s": age}, status_code=200 if ok else 503)

    return app


async def _connect_quietly(tv: AppleTV) -> None:
    try:
        await tv.connect()
    except Exception as exc:
        tv.error = f"{type(exc).__name__}: {exc}"
        tv.on_change()


def _same(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def _basic_auth(header: str) -> tuple[str, str]:
    """(user, password) from an Authorization: Basic header, or empty strings."""
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "basic":
        return "", ""
    try:
        user, _, password = base64.b64decode(value).decode().partition(":")
    except (ValueError, UnicodeDecodeError):
        return "", ""
    return user, password


TV_PROTOCOL_NAMES = {"companion": "Remote control (buttons, apps, power)", "airplay": "Now playing"}


def _admin_tv(tv: AppleTV | None) -> str:
    """The Apple TV section: connection, pairing (with the PIN form while one runs), installed apps."""
    e = html.escape
    if tv is None:
        return "<p>Not configured: set <code>ATV_HOST</code> in <code>.env</code>.</p>"
    if tv.connected:
        power = {True: "on", False: "off"}.get(tv.power_on, "unknown")
        status = f"Connected to {e(tv.host)} &middot; TV power {power}"
    else:
        status = f"Not connected ({e(tv.host)})" + (f": {e(tv.error)}" if tv.error else "")
    out = [f"<p>{status}</p>"]
    if tv.pairing:
        out.append(
            f"<p>Pairing <b>{e(TV_PROTOCOL_NAMES[tv.pairing[0]])}</b>: enter the PIN shown on the TV.</p>"
            '<form method="post" action="/admin/tv/pin" class="row"><input name="pin" inputmode="numeric" autocomplete="one-time-code" '
            'pattern="[0-9]*" required autofocus placeholder="PIN"><button>Pair</button></form>'
            '<form method="post" action="/admin/tv/cancel"><button class="quiet">Cancel</button></form>'
        )
    else:
        for proto in TV_PROTOCOL_NAMES:
            done = tv.paired(proto)
            out.append(
                f'<form method="post" action="/admin/tv/pair?protocol={proto}" class="row"><span>{e(TV_PROTOCOL_NAMES[proto])}: '
                f'<b>{"paired" if done else "not paired"}</b></span><button{" class=quiet" if done else ""}>'
                f'{"Pair again" if done else "Pair"}</button></form>'
            )
    if tv.installed_apps:
        rows = "".join(f"<li>{e(name)} <code>{e(bundle)}</code></li>" for name, bundle in tv.installed_apps)
        out.append(
            "<details><summary>Installed apps</summary><p class=muted>For the TV page's shortcuts, set "
            "<code>ATV_APPS=Name=bundle.id,…</code> (up to 4) in <code>.env</code>.</p>"
            f"<ul>{rows}</ul></details>"
        )
    return "\n".join(out)


def _ago(t: datetime | None, now: datetime) -> str:
    if t is None:
        return "never"
    s = int((now - t).total_seconds())
    return f"{s} s ago" if s < 120 else f"{s // 60} min ago"


def admin_html(state: State, done: str) -> str:
    now, e = state.now(), html.escape
    view = state.kindle_view
    page, layout, rotate = (view[0], view[1], view[2]) if view else ("solar", "portrait", 0)
    kb = state.kindle_battery()
    kindle = (
        f"Showing <b>{e(page)}</b>, {e(layout)} (rotate {rotate}), last fetch {_ago(view[3], now)}"
        if view
        else "No image fetched since the server started"
    )
    if kb:
        kindle += f" &middot; battery {kb.level}%{' (charging)' if kb.charging else ''}"
    data = "OK" if state.stale_since is None and state.last_success else "STALE"
    if state.last_error:
        data += f" &middot; {e(state.last_error)}"
    previews = " ".join(
        f'<a href="?view={p}-{lay}" data-view="{p}-{lay}">{p} {lay}</a>' for p in PAGE_NAMES for lay in render.LAYOUTS
    )
    plugs = "".join(
        f'<form method="post" action="/admin/plug?host={e(p.host)}"><button class="plug {"on" if p.on else "off"}"'
        f'{" disabled" if p.on is None else ""}><b>{e(p.name)}</b> <span>{"offline" if p.on is None else ("ON" if p.on else "OFF")}{f" &middot; {render.fmt_watts(p.power_w)}" if p.on is not None and p.power_w is not None else ""}</span></button></form>'
        for p in state.plugs.plugs
    ) or "<p>No plugs configured (KASA_HOSTS).</p>"
    note = {
        "rotate": "Rotate requested. The Kindle turns at its next check (within a minute).",
        "plug": "Plug toggled.",
        "tv-paired": "Paired. Connecting to the Apple TV\u2026",
        "tv-error": f"Apple TV: {e(state.tv.error or 'pairing failed') if state.tv else ''}",
    }.get(done, "")
    return ADMIN_PAGE.format(
        tv=_admin_tv(state.tv),
        kindle=kindle,
        data=data,
        updated=_ago(state.last_success, now),
        note=f'<p class="note">{note}</p>' if note else "",
        img=f"/admin/image.png?page={page}&layout={layout}",
        current=f"{page}-{layout}",
        previews=previews,
        plugs=plugs,
    )


ADMIN_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kindle Dashboard Admin</title>
<style>
:root{{--bg:#fff;--fg:#111;--muted:#666;--line:#ccc;--on:#111;--on-fg:#fff}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151515;--fg:#eee;--muted:#999;--line:#444;--on:#eee;--on-fg:#111}}}}
body{{font-family:system-ui,sans-serif;margin:0 auto;padding:1rem;max-width:56rem;background:var(--bg);color:var(--fg)}}
h1{{font-size:1.4rem;margin:.2rem 0 1rem}} h2{{font-size:1.05rem;margin:1.4rem 0 .5rem}}
p{{margin:.3rem 0}} .muted{{color:var(--muted)}} .note{{padding:.6rem .8rem;border:1px solid var(--line);border-radius:.5rem}}
button{{font:inherit;padding:.7rem 1.2rem;border-radius:.5rem;border:2px solid var(--fg);background:var(--bg);color:var(--fg);cursor:pointer}}
.plugs{{display:grid;grid-template-columns:repeat(auto-fill,minmax(11rem,1fr));gap:.6rem}}
.plugs form{{margin:0}} .plug{{width:100%;display:flex;justify-content:space-between;gap:.5rem}}
.plug.on{{background:var(--on);color:var(--on-fg)}} .plug:disabled{{opacity:.45;cursor:default}}
.row{{display:flex;gap:.6rem;align-items:center;flex-wrap:wrap;margin:.4rem 0}}
input{{font:inherit;padding:.6rem;border-radius:.5rem;border:2px solid var(--fg);background:var(--bg);color:var(--fg);width:8rem}}
button.quiet{{border-width:1px;padding:.4rem .9rem}} code{{font-size:.9em}} details{{margin-top:.6rem}}
.views a{{margin-right:.8rem}} .views a.cur{{font-weight:bold;text-decoration:none}}
img{{max-width:100%;height:auto;border:1px solid var(--line);margin-top:.6rem;background:#fff}}
</style></head>
<body>
<h1>Kindle Dashboard Admin</h1>
{note}
<h2>Kindle</h2>
<p>{kindle}</p>
<form method="post" action="/admin/rotate"><button>Rotate Kindle 90&deg;</button></form>
<h2>Solar data</h2>
<p>{data} <span class="muted">&middot; last update {updated}</span></p>
<h2>Plugs</h2>
<div class="plugs">{plugs}</div>
<h2 id="tv">Apple TV</h2>
{tv}
<h2>Screen</h2>
<p class="views">{previews}</p>
<img id="shot" src="{img}" alt="The page the Kindle is showing">
<p class="muted"><a href="/status">status JSON</a></p>
<script>
// ?view=<page>-<layout> previews another page; default is what the Kindle shows
const v = new URLSearchParams(location.search).get("view") || "{current}";
const [p, l] = v.split("-");
document.getElementById("shot").src = "/admin/image.png?page=" + encodeURIComponent(p) + "&layout=" + encodeURIComponent(l);
for (const a of document.querySelectorAll(".views a")) a.classList.toggle("cur", a.dataset.view === v);
</script>
</body></html>
"""


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run(create_app(), host="0.0.0.0", port=8000, log_level="info", access_log=True)


if __name__ == "__main__":
    main()
