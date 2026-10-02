"""FastAPI app: a background poller renders; requests only serve cached bytes."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from PIL import Image

from . import render
from .config import Settings
from .data import Snapshot

log = logging.getLogger("kindle_dash")


class State:
    """Last good render plus everything /status and /health need."""

    def __init__(self, settings: Settings):
        self.s = settings
        self.lock = threading.Lock()
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

    @staticmethod
    def now() -> datetime:
        return datetime.now(timezone.utc)

    def _publish(self, images: dict[str, Image.Image]) -> None:
        with self.lock:
            self.images = images
            self._png = {}

    def update(self, fetch) -> None:
        """One poll cycle. `fetch` returns a Snapshot or raises."""
        try:
            snap = fetch()
            imgs = {
                lay: render.render(snap, self.s.zone, self.s.chart_hours, self.s.spark_hours, layout=lay)
                for lay in render.LAYOUTS
            }
        except Exception as exc:  # keep serving the last good image
            log.warning("refresh failed: %s", exc)
            self.last_error = f"{type(exc).__name__}: {exc}"
            if self.stale_since is None:
                self.stale_since = self.last_success or self.now()
                if self.good_images:
                    self._publish({lay: render.stale_badge(im, self.stale_since, self.s.zone) for lay, im in self.good_images.items()})
                else:
                    self._publish(
                        {lay: render.placeholder("InfluxDB unavailable", self.now(), self.s.zone, lay) for lay in render.LAYOUTS}
                    )
            return
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
        data, etag = state.png(layout, rotate)
        headers = {"ETag": etag, "Cache-Control": "no-cache"}
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=headers)
        return Response(data, media_type="image/png", headers=headers)

    @app.get("/dashboard.png")
    @app.get("/portrait.png")
    def portrait(request: Request, rotate: int = Query(0)):
        """1236 x 1648 portrait layout (the Kindle's native orientation)."""
        return serve(request, "portrait", rotate)

    @app.get("/landscape.png")
    def landscape(request: Request, rotate: int = Query(0)):
        """1648 x 1236 landscape layout; on the Kindle use ?rotate=90 or 270."""
        return serve(request, "landscape", rotate)

    @app.get("/status")
    def status():
        iso = lambda t: t.isoformat() if t else None  # noqa: E731
        return {
            "values": state.snapshot.to_json() if state.snapshot else None,
            "last_success": iso(state.last_success),
            "stale_since": iso(state.stale_since),
            "last_error": state.last_error,
            "healthy": state.healthy(),
        }

    @app.get("/health")
    def health():
        ok = state.healthy()
        age = (state.now() - state.last_render).total_seconds() if state.last_render else None
        return JSONResponse({"ok": ok, "render_age_s": age}, status_code=200 if ok else 503)

    return app


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run(create_app(), host="0.0.0.0", port=8000, log_level="info", access_log=True)


if __name__ == "__main__":
    main()
