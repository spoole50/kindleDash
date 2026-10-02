"""Still snapshots from UniFi Protect cameras, for the CAMERA page.

Logs in to UniFi OS once with a local account (a Protect "View Only" user is
enough) and keeps the session cookie; logs in again only when Protect says the
session expired, since UniFi OS rate-limits logins. Protect's certificate is
self-signed, so it isn't verified (LAN only).
"""

from __future__ import annotations

import http.cookiejar
import io
import json
import ssl
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime

from PIL import Image

SNAPSHOT_WIDTH = 1600  # wide enough for landscape (1576 px); stills are scaled down to this


@dataclass
class Shot:
    """One camera's latest still."""

    name: str  # the camera's name in Protect
    label: str  # shown on the page
    image: Image.Image | None = None  # grayscale, at most SNAPSHOT_WIDTH wide
    at: datetime | None = None
    error: str | None = None  # why the last attempt failed; None once one succeeds


class Protect:
    def __init__(self, host: str, username: str, password: str, timeout: float = 15):
        self.base = f"https://{host}"
        self.username, self.password, self.timeout = username, password, timeout
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ctx), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )
        self._csrf: str | None = None
        self._ids: dict[str, str] = {}  # camera name -> id
        self._path_index = 0  # into PATHS: which snapshot API this Protect has
        self._lock = threading.Lock()  # one request sequence at a time (login state is shared)

    def _open(self, path: str, data: bytes | None = None):
        headers = {"Content-Type": "application/json"} if data else {}
        if self._csrf:
            headers["X-CSRF-Token"] = self._csrf
        return self._opener.open(urllib.request.Request(self.base + path, data=data, headers=headers), timeout=self.timeout)

    def _login(self) -> None:
        body = json.dumps({"username": self.username, "password": self.password, "rememberMe": True}).encode()
        try:
            with self._open("/api/auth/login", body) as r:
                self._csrf = r.headers.get("X-CSRF-Token") or r.headers.get("X-Updated-CSRF-Token")
        except urllib.error.HTTPError as exc:
            raise PermissionError(f"Protect login refused (HTTP {exc.code})") from None

    def _get(self, path: str) -> bytes:
        """GET, logging in first if needed and once more if the session expired."""
        if self._csrf is None:
            self._login()
        try:
            with self._open(path) as r:
                return r.read()
        except urllib.error.HTTPError as exc:
            if exc.code not in (401, 403):
                raise
        self._login()
        with self._open(path) as r:
            return r.read()

    def _camera_id(self, name: str, refresh: bool = False) -> str:
        if refresh or name not in self._ids:
            boot = json.loads(self._get("/proxy/protect/api/bootstrap"))
            self._ids = {c["name"]: c["id"] for c in boot.get("cameras", [])}
        if name not in self._ids:
            raise LookupError(f"no camera named {name!r} in Protect (have: {', '.join(sorted(self._ids)) or 'none'})")
        return self._ids[name]

    # The integration API gives a full-resolution still (the private one only the
    # 640 x 360 "Low" stream); older Protect versions only have the private one.
    PATHS = ("/proxy/protect/integration/v1/cameras/{}/snapshot?highQuality=true", "/proxy/protect/api/cameras/{}/snapshot?force=true")

    def _snapshot_bytes(self, camera_id: str) -> bytes:
        for path in self.PATHS[self._path_index :]:
            try:
                return self._get(path.format(camera_id))
            except urllib.error.HTTPError as exc:
                if exc.code != 404 or path == self.PATHS[-1]:
                    raise
                # 404 from the integration API: no such API here (or no such camera; the next path tells)
                self._path_index += 1
        raise AssertionError("unreachable")

    def snapshot(self, name: str) -> Image.Image:
        """The camera's current view, grayscale."""
        with self._lock:
            try:
                data = self._snapshot_bytes(self._camera_id(name))
            except urllib.error.HTTPError as exc:
                if exc.code != 404:  # a re-added camera gets a new id
                    raise
                self._path_index = 0
                data = self._snapshot_bytes(self._camera_id(name, refresh=True))
        img = Image.open(io.BytesIO(data))
        img.draft("L", (SNAPSHOT_WIDTH, SNAPSHOT_WIDTH))
        img = img.convert("L")
        if img.width > SNAPSHOT_WIDTH:
            img = img.resize((SNAPSHOT_WIDTH, round(img.height * SNAPSHOT_WIDTH / img.width)), Image.LANCZOS)
        return img
