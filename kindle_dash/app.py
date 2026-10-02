"""FastAPI app: a background poller renders; requests only serve cached bytes."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from PIL import Image

from . import render
from .config import Settings
from .data import Snapshot

log = logging.getLogger("kindle_dash")

KINDLE_REPORT_MAX_AGE_S = 15 * 60  # hide the Kindle's battery after this long without a report


class State:
    """Last good render plus everything /status and /health need."""

    def __init__(self, settings: Settings):
        self.s = settings
        self.lock = threading.Lock()
        self.render_lock = threading.Lock()  # one render at a time: poll cycle or battery change
        self.snapshot: Snapshot | None = None
        # per layout: last render from fresh data, and what's being served
        self.good_images: dict[str, Image.Image] = {}
        self.images: dict[str, Image.Image] = {
            lay: render.placeholder("Waiting for data…", self.now(), settings.zone, lay) for lay in render.LAYOUTS
        }
        self.last_render: datetime | None = None
        self.last_success: datetime | None = None
        self.stale_since: datetime | None = None
        self.last_error: str | None = None
        self._png: dict[tuple[str, int], tuple[bytes, str]] = {}
        # bumped by POST /rotate; the Kindle polls it and rotates once per increase
        self.rotate_requests = 0
        # the display's own battery, sent as query params on its image fetches
        self.kindle: render.KindleBattery | None = None
        self.kindle_at: datetime | None = None

    @staticmethod
    def now() -> datetime:
        return datetime.now(timezone.utc)

    def _publish(self, images: dict[str, Image.Image]) -> None:
        with self.lock:
            self.images = images
            self._png = {}

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
        with self.render_lock:
            if self.snapshot is None or self.stale_since is not None:
                return  # nothing fresh to redraw; the next good poll picks the report up
            self.good_images = self._render_all(self.snapshot)
            self._publish(self.good_images)

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

    def png(self, layout: str, rotate: int) -> tuple[bytes, str]:
        """Encoded once per (layout, rotation) per render; requests just read the cache."""
        with self.lock:
            cached = self._png.get((layout, rotate))
            if cached is None:
                data = render.to_png(self.images[layout], rotate)
                cached = (data, '"' + hashlib.md5(data).hexdigest() + '"')
                self._png[(layout, rotate)] = cached
            return cached

    def healthy(self) -> bool:
        return self.last_render is not None and (self.now() - self.last_render).total_seconds() < self.s.stale_after_seconds


async def _poll(state: State, fetch) -> None:
    while True:
        await asyncio.to_thread(state.update, fetch)
        await asyncio.sleep(state.s.refresh_seconds)


def create_app(settings: Settings | None = None, fetch=None, poll: bool = True) -> FastAPI:
    settings = settings or Settings.from_env()
    state = State(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        source = None
        nonlocal fetch
        if fetch is None:
            from .data import InfluxSource

            source = InfluxSource(settings)
            fetch = source.fetch
        task = asyncio.create_task(_poll(state, fetch)) if poll else None
        yield
        if task:
            task.cancel()
        if source:
            source.close()

    app = FastAPI(title="kindle-solar-dashboard", lifespan=lifespan)
    app.state.dash = state

    def serve(request: Request, layout: str, rotate: int) -> Response:
        if rotate not in (0, 90, 180, 270):
            raise HTTPException(400, "rotate must be 0, 90, 180 or 270")
        if state.report_kindle(request.query_params.get("battery"), request.query_params.get("charging")):
            state.rerender()
        data, etag = state.png(layout, rotate)
        headers = {"ETag": etag, "Cache-Control": "no-cache"}
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=headers)
        return Response(data, media_type="image/png", headers=headers)

    @app.get("/dashboard.png")
    @app.get("/portrait.png")
    def portrait(request: Request, rotate: int = Query(0)):
        """1236 x 1648 portrait layout (the Kindle's native orientation).

        The Kindle client adds ?battery=<0-100>&charging=<0|1>; the next render shows it in the header.
        """
        return serve(request, "portrait", rotate)

    @app.get("/landscape.png")
    def landscape(request: Request, rotate: int = Query(0)):
        """1648 x 1236 landscape layout; on the Kindle use ?rotate=90 or 270."""
        return serve(request, "landscape", rotate)

    @app.get("/rotate", response_class=HTMLResponse)
    def rotate_page(sent: int = 0):
        """Phone-friendly page with a button that asks the Kindle to rotate."""
        note = "<p>Requested. The Kindle rotates at its next check (within a minute).</p>" if sent else ""
        return ROTATE_PAGE.format(note=note)

    @app.post("/rotate")
    def rotate_request():
        state.rotate_requests += 1
        log.info("rotate requested (count %d)", state.rotate_requests)
        return RedirectResponse("/rotate?sent=1", status_code=303)

    @app.get("/rotate/count", response_class=PlainTextResponse)
    def rotate_count():
        """Polled by the Kindle; it rotates once each time this goes up."""
        return str(state.rotate_requests)

    @app.get("/status")
    def status():
        iso = lambda t: t.isoformat() if t else None  # noqa: E731
        return {
            "values": state.snapshot.to_json() if state.snapshot else None,
            "last_success": iso(state.last_success),
            "stale_since": iso(state.stale_since),
            "last_error": state.last_error,
            "healthy": state.healthy(),
            "kindle": {**state.kindle._asdict(), "reported_at": iso(state.kindle_at)} if state.kindle else None,
        }

    @app.get("/health")
    def health():
        ok = state.healthy()
        age = (state.now() - state.last_render).total_seconds() if state.last_render else None
        return JSONResponse({"ok": ok, "render_age_s": age}, status_code=200 if ok else 503)

    return app


ROTATE_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Rotate Kindle</title>
<style>body{{font-family:system-ui,sans-serif;margin:2rem 1rem;text-align:center}}
button{{font-size:1.4rem;padding:1rem 2rem;border-radius:.6rem}}</style></head>
<body><h1>Solar dashboard</h1>
<form method="post" action="/rotate"><button type="submit">Rotate Kindle 90&deg;</button></form>
{note}</body></html>
"""


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run(create_app(), host="0.0.0.0", port=8000, log_level="info", access_log=True)


if __name__ == "__main__":
    main()
