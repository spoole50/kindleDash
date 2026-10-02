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
        self.good_image: Image.Image | None = None  # last render from fresh data
        self.image: Image.Image = render.placeholder("Waiting for data…", self.now(), settings.zone)
        self.last_render: datetime | None = None
        self.last_success: datetime | None = None
        self.stale_since: datetime | None = None
        self.last_error: str | None = None
        self._png: dict[int, tuple[bytes, str]] = {}

    @staticmethod
    def now() -> datetime:
        return datetime.now(timezone.utc)

    def _publish(self, img: Image.Image) -> None:
        with self.lock:
            self.image = img
            self._png = {}

    def update(self, fetch) -> None:
        """One poll cycle. `fetch` returns a Snapshot or raises."""
        try:
            snap = fetch()
            img = render.render(snap, self.s.zone, self.s.chart_hours, self.s.spark_hours)
        except Exception as exc:  # keep serving the last good image
            log.warning("refresh failed: %s", exc)
            self.last_error = f"{type(exc).__name__}: {exc}"
            if self.stale_since is None:
                self.stale_since = self.last_success or self.now()
                if self.good_image is not None:
                    self._publish(render.stale_badge(self.good_image, self.stale_since, self.s.zone))
                else:
                    self._publish(render.placeholder("InfluxDB unavailable", self.now(), self.s.zone))
            return
        self.snapshot = snap
        self.good_image = img
        self.last_success = self.last_render = snap.fetched_at
        self.stale_since = None
        self.last_error = None
        self._publish(img)

    def png(self, rotate: int) -> tuple[bytes, str]:
        with self.lock:
            cached = self._png.get(rotate)
            if cached is None:
                data = render.to_png(self.image, rotate)
                cached = (data, '"' + hashlib.md5(data).hexdigest() + '"')
                self._png[rotate] = cached
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

    @app.get("/dashboard.png")
    def dashboard(request: Request, rotate: int = Query(0)):
        if rotate not in (0, 90, 180, 270):
            raise HTTPException(400, "rotate must be 0, 90, 180 or 270")
        data, etag = state.png(rotate)
        headers = {"ETag": etag, "Cache-Control": "no-cache"}
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=headers)
        return Response(data, media_type="image/png", headers=headers)

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
