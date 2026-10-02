import base64
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from PIL import Image

from kindle_dash import render, touch
from kindle_dash.app import create_app
from kindle_dash.camera import Shot
from kindle_dash.config import Settings
from kindle_dash.sample import sample_bluetti

TZ = ZoneInfo("UTC")
NOW = datetime(2026, 10, 2, 22, 30, tzinfo=timezone.utc)
KINDLE = {"X-Kindle-Token": "t"}
ADMIN = {"Authorization": "Basic " + base64.b64encode(b"admin:pw").decode()}


def _grays(img):
    return {v for v, n in enumerate(img.histogram()) if n}


def _gradient(w=1600, h=900):
    return Image.linear_gradient("L").resize((w, h))


class FakeProtect:
    def __init__(self):
        self.calls, self.fail = [], False

    def snapshot(self, name):
        self.calls.append(name)
        if self.fail:
            raise ConnectionError("camera offline")
        return _gradient()


def _app(**kw):
    s = Settings(tz="UTC", control_token="t", admin_password="pw", protect_cameras=(("garageCam", "Outer garage"),), **kw)
    app = create_app(s, fetch=lambda: None, poll=False)
    state = app.state.dash
    state.protect = FakeProtect()
    return app, state


def test_tabs_clear_the_clock():
    # the Kindle's battery (icon + bold "100%", ~140 px) sits left of the time (~140 px) in portrait
    assert render.TABS_RIGHT < render.W - render.M - 140 - 20 - 140
    assert [a for _, a in render.tab_boxes()] == ["page:solar", "page:bluetti", "page:lights", "page:tv", "page:cameras"]


def test_photo_is_dithered_to_panel_grays():
    pic = render.photo(_gradient(), 1164, 800)
    assert pic.size == (1164, 655)  # 16:9 kept
    assert _grays(pic) <= {i * 0x11 for i in range(16)} and len(_grays(pic)) > 8


def test_camera_page_renders_every_state():
    shots = [
        Shot("a", "Outer garage", _gradient(), NOW),
        Shot("b", "Patio", None, None, "timed out"),
        Shot("c", "Drive", _gradient(), NOW - timedelta(minutes=9), "camera offline and a very long reason that has to be cut down"),
    ]
    for layout in render.LAYOUTS:
        for n in (0, 1, 2, 3):
            img = render.render_cameras(shots[:n], NOW, TZ, layout)
            assert img.size == render.size(layout) and _grays(img) <= {i * 0x11 for i in range(16)}
        boxes = render.camera_boxes(layout, 3)
        w, h = render.size(layout)
        assert all(render.HEADER_RULE_Y < y0 and x1 <= w and y1 <= h - 40 for _, y0, x1, y1 in boxes)


def test_tap_on_the_still_takes_a_new_one():
    app, state = _app()
    with TestClient(app, headers=KINDLE) as client:
        box = render.camera_boxes("portrait", 1)[0]
        x, y = touch.image_to_panel((box[0] + box[2]) // 2, (box[1] + box[3]) // 2, 0, render.size("portrait"))
        r = client.post(f"/tap?x={x}&y={y}&page=cameras")
        assert r.text.splitlines()[1] == "redraw" and state.protect.calls == ["garageCam"]
        assert state.shots[0].at is not None and state.shots[0].error is None
        assert client.get("/portrait.png?page=cameras").status_code == 200


def test_failures_show_on_the_health_page_and_badge_every_page():
    app, state = _app()
    with TestClient(app, headers=KINDLE) as client:
        state.refresh_cameras()
        assert state.problems == ()
        clean = client.get("/portrait.png?page=tv").content
        state.protect.fail = True
        state.refresh_cameras()  # keeps the last still, marked failed
        assert state.shots[0].image is not None and "offline" in state.shots[0].error
        assert state.problems == ("Camera: Outer garage",)
        assert client.get("/portrait.png?page=tv").content != clean  # badged
        # the clock opens the health page
        cx, cy = touch.image_to_panel(render.W - render.M - 60, 60, 0, render.size("portrait"))
        assert client.post(f"/tap?x={cx}&y={cy}&page=tv").text.splitlines()[1] == "page health"
        assert client.get("/portrait.png?page=health").status_code == 200
        admin = client.get("/admin", headers=ADMIN).text
        assert "PROBLEM" in admin and "Outer garage" in admin
        status = client.get("/status", headers=ADMIN).json()
        assert status["cameras"][0]["error"] and any(not c["ok"] for c in status["health"])
        state.protect.fail = False
        state.refresh_cameras()
        assert state.problems == () and client.get("/portrait.png?page=tv").content == clean


def test_health_checks_cover_each_subsystem():
    app, state = _app()
    state.bluetti = sample_bluetti(datetime.now(timezone.utc) - timedelta(minutes=10), TZ)  # bluetti2influx stopped
    state.update_health()
    names = {c.name: c for c in state.checks}
    assert not names["Bluetti APEX 300"].ok and "bluetti2influx" in names["Bluetti APEX 300"].detail
    assert names["Solar (Victron)"].ok  # still starting: no badge
    for layout in render.LAYOUTS:
        img = render.render_health(state.checks, NOW, TZ, layout)
        assert img.size == render.size(layout) and _grays(img) <= {i * 0x11 for i in range(16)}
    badged = render.issues_badge(render.placeholder("x", NOW, TZ), 2)
    assert _grays(badged) <= {i * 0x11 for i in range(16)}


def test_protect_settings(monkeypatch):
    monkeypatch.setenv("PROTECT_CAMERAS", "garageCam=Outer garage,livingRoomCam")
    assert Settings.from_env().protect_cameras == (("garageCam", "Outer garage"), ("livingRoomCam", "livingRoomCam"))
