import asyncio
import base64
import json
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from kindle_dash import app as app_mod
from kindle_dash import render
from kindle_dash.app import create_app
from kindle_dash.appletv import AppleTV, NowPlaying
from kindle_dash.config import Settings

TOKEN = "s3cret"
KINDLE = {"X-Kindle-Token": TOKEN}
ADMIN = {"Authorization": "Basic " + base64.b64encode(b"admin:pw").decode()}
APPS = (("YouTube", "com.google.ios.youtube"), ("Netflix", "com.netflix.Netflix"))


class FakeHandler:
    def __init__(self, ok=True):
        self.ok, self.pinned, self.closed = ok, None, False
        self.service = type("Svc", (), {"credentials": "CREDS"})()

    def pin(self, pin):
        self.pinned = pin

    async def finish(self):
        pass

    @property
    def has_paired(self):
        return self.ok

    async def close(self):
        self.closed = True


class FakeTV(AppleTV):
    """AppleTV with the network replaced: records commands and pairing."""

    def __init__(self, path, ok=True):
        super().__init__("10.0.0.9", path)
        self.sent, self.ok = [], ok

    async def command(self, action):
        self.sent.append(action)
        return self.ok

    async def connect(self):
        pass

    async def pair_begin(self, protocol):
        self.pairing = (protocol, FakeHandler())


def _app(tmp_path, **kw):
    s = Settings(tz="UTC", control_token=TOKEN, admin_password="pw", atv_host="10.0.0.9", atv_apps=APPS, **kw)
    app = create_app(s, fetch=lambda: None, poll=False)
    state = app.state.dash
    state.tv = FakeTV(str(tmp_path / "atv.json"))
    state.tv.on_change = state._tv_changed
    return app, state


def _panel_center(action, layout="portrait", rotate=0):
    box = next(b for b, a, _ in render.tv_buttons(layout, APPS) if a == action)
    from kindle_dash import touch

    return touch.image_to_panel((box[0] + box[2]) // 2, (box[1] + box[3]) // 2, rotate, render.size(layout))


def test_remote_keys_only_flash(tmp_path):
    app, state = _app(tmp_path)
    with TestClient(app, headers=KINDLE) as client:
        x, y = _panel_center("up")
        r = client.post(f"/tap?x={x}&y={y}&page=tv")
        assert r.status_code == 200 and r.text.splitlines()[0].startswith("flash ") and len(r.text.splitlines()) == 1
        x, y = _panel_center("volume_up", "landscape", 270)
        client.post(f"/tap?x={x}&y={y}&page=tv&rotate=270")
        assert state.tv.sent == ["up", "volume_up"]


def test_playback_apps_and_failures_redraw(tmp_path, monkeypatch):
    monkeypatch.setattr(app_mod, "TV_SETTLE_S", 0)
    app, state = _app(tmp_path)
    with TestClient(app, headers=KINDLE) as client:
        for action in ("play_pause", "power", "app:com.netflix.Netflix"):
            x, y = _panel_center(action)
            assert client.post(f"/tap?x={x}&y={y}&page=tv").text.splitlines()[1] == "redraw"
        assert state.tv.sent == ["play_pause", "power", "app:com.netflix.Netflix"]
        state.tv.ok = False  # a key that fails redraws, to show the error
        x, y = _panel_center("select")
        assert client.post(f"/tap?x={x}&y={y}&page=tv").text.splitlines()[1] == "redraw"


def test_tv_page_status_and_render(tmp_path):
    app, state = _app(tmp_path)
    assert state.tv_status() == "Pair the Apple TV at /admin"
    state.tv.creds = {"companion": "c"}
    assert state.tv_status().startswith("Connecting")
    state.tv.atv = object()
    assert "now playing" in state.tv_status()
    state.tv.creds["airplay"] = "a"
    assert state.tv_status() is None
    tz = ZoneInfo("UTC")
    now = datetime(2026, 10, 2, tzinfo=timezone.utc)
    playing = NowPlaying("YouTube", "A title that is far too long to fit on one line of the panel", "Channel", "paused", 3700, 7200)
    for layout in render.LAYOUTS:
        for args in ((playing, None, True, APPS), (None, "Pair the Apple TV at /admin", None, ())):
            img = render.render_tv(*args, now, tz, layout)
            assert img.size == render.size(layout)
            assert {v for v, n in enumerate(img.histogram()) if n} <= {i * 0x11 for i in range(16)}
    # no target overlaps another, and all are on screen
    for layout in render.LAYOUTS:
        boxes = [t.area for t in render.targets("tv", layout, [], APPS)]
        w, h = render.size(layout)
        assert all(0 <= x0 < x1 <= w and 0 <= y0 < y1 <= h for x0, y0, x1, y1 in boxes)
        for i, a in enumerate(boxes):
            for b in boxes[i + 1 :]:
                assert a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1], (a, b)


def test_admin_pairing_flow(tmp_path):
    app, state = _app(tmp_path)
    with TestClient(app, headers=ADMIN) as client:
        page = client.get("/admin").text
        assert "Remote control" in page and "not paired" in page
        assert client.post("/admin/tv/pair?protocol=bogus").status_code == 400
        r = client.post("/admin/tv/pair?protocol=companion", follow_redirects=False)
        assert r.status_code == 303 and state.tv.pairing[0] == "companion"
        assert 'name="pin"' in client.get("/admin").text
        handler = state.tv.pairing[1]
        r = client.post("/admin/tv/pin", content="pin=12+34", headers={"Content-Type": "application/x-www-form-urlencoded"}, follow_redirects=False)
        assert r.headers["location"].startswith("/admin?done=tv-paired")
        assert handler.pinned == "1234" and handler.closed and state.tv.pairing is None
        assert json.load(open(tmp_path / "atv.json")) == {"companion": "CREDS"}
        assert oct(os.stat(tmp_path / "atv.json").st_mode & 0o777) == "0o600"
        assert "<b>paired</b>" in client.get("/admin").text
    # credentials survive a restart
    assert AppleTV("10.0.0.9", str(tmp_path / "atv.json")).paired("companion")


def test_no_apple_tv_configured():
    app = create_app(Settings(control_token=TOKEN, admin_password="pw"), fetch=lambda: None, poll=False)
    with TestClient(app) as client:
        assert "Not configured" in client.get("/admin", headers=ADMIN).text
        assert client.post("/admin/tv/pair?protocol=companion", headers=ADMIN).status_code == 404
        assert client.get("/portrait.png?page=tv", headers=KINDLE).status_code == 200


class Recorder:
    """Stands in for a pyatv interface: records awaited method calls."""

    def __init__(self, log, prefix):
        self._log, self._prefix = log, prefix

    def __getattr__(self, name):
        async def call(*args):
            self._log.append((f"{self._prefix}.{name}", *args))

        return call


def test_command_dispatch():
    calls = []
    tv = AppleTV("10.0.0.9", "/nonexistent/atv.json")
    tv.atv = type("Atv", (), {})()
    for part in ("remote_control", "audio", "power", "apps"):
        setattr(tv.atv, part, Recorder(calls, part))
    tv.power_on = True

    async def run():
        for action in ("up", "play_pause", "volume_down", "power", "app:com.netflix.Netflix"):
            assert await tv.command(action)

    asyncio.run(run())
    assert calls == [
        ("remote_control.up",),
        ("remote_control.play_pause",),
        ("audio.volume_down",),
        ("power.turn_off",),
        ("apps.launch_app", "com.netflix.Netflix"),
    ]


def test_atv_apps_setting(monkeypatch):
    monkeypatch.setenv("ATV_APPS", "YouTube=com.google.ios.youtube, Broken ,Plex=com.plexapp.plex")
    assert Settings.from_env().atv_apps == (("YouTube", "com.google.ios.youtube"), ("Plex", "com.plexapp.plex"))


@pytest.fixture(autouse=True)
def _no_real_pyatv(monkeypatch):
    """Nothing here should reach the network."""

    async def boom(*a, **k):
        raise AssertionError("tried to use the network")

    import pyatv

    monkeypatch.setattr(pyatv, "scan", boom)
    monkeypatch.setattr(pyatv, "connect", boom)
    monkeypatch.setattr(pyatv, "pair", boom)
