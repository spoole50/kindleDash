import asyncio
import time
import base64
from datetime import datetime, timezone
from io import BytesIO
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from kindle_dash import render, touch
from kindle_dash import app as app_mod
from kindle_dash.app import create_app
from kindle_dash.config import Settings
from kindle_dash.plugs import KasaPlugs, Plug

TOKEN = "s3cret"
KINDLE = {"X-Kindle-Token": TOKEN}
ADMIN = {"Authorization": "Basic " + base64.b64encode(b"admin:pw").decode()}


@pytest.mark.parametrize("rotate", [0, 90, 180, 270])
@pytest.mark.parametrize("layout", ["portrait", "landscape"])
def test_panel_mapping_matches_to_png(rotate, layout):
    """A pixel set in the upright image lands where panel_to_image/image_to_panel say."""
    size = render.size(layout)
    img = Image.new("L", size, 255)
    x, y = 100, 40
    img.putpixel((x, y), 0)
    panel = Image.open(BytesIO(render.to_png(img, rotate)))
    found = next((px, py) for py in range(panel.height) for px in range(panel.width) if panel.getpixel((px, py)) == 0)
    assert touch.image_to_panel(x, y, rotate, size) == found
    assert touch.panel_to_image(*found, rotate, size) == (x, y)


def test_box_to_panel_is_left_top_width_height():
    assert touch.box_to_panel((10, 20, 110, 70), 0, (1236, 1648)) == (10, 20, 100, 50)
    # landscape image rotated 270: image x runs up the panel
    left, top, w, h = touch.box_to_panel((10, 20, 110, 70), 270, (1648, 1236))
    assert (left, top, w, h) == (20, 1648 - 110, 50, 100)


def test_raw_to_panel_transforms():
    assert touch.raw_to_panel(100, 200, Settings()) == (100, 200)
    s = Settings(touch_swap_xy=True, touch_flip_x=True, touch_max_x=1647, touch_max_y=1235)
    # raw x runs along the panel's long axis; after the swap, x is flipped
    assert touch.raw_to_panel(0, 0, s) == (1235, 0)
    assert touch.raw_to_panel(1647, 1235, s) == (0, 1647)
    scaled = Settings(touch_max_x=4095, touch_max_y=4095)
    assert touch.raw_to_panel(4095, 2048, scaled) == (1235, 824)


class FakePlugs:
    def __init__(self, plugs):
        self.plugs = plugs
        self.toggled = []

    async def refresh(self):
        return False

    async def toggle(self, host):
        self.toggled.append(host)
        self.plugs = [Plug(p.host, p.name, not p.on) if p.host == host else p for p in self.plugs]
        return True

    async def close(self):
        pass


def _app(**kw):
    kw = {"control_token": TOKEN, "admin_password": "pw", "kasa_hosts": ("10.0.0.1", "10.0.0.2"), **kw}
    app = create_app(Settings(tz="America/Los_Angeles", **kw), fetch=lambda: None, poll=False)
    state = app.state.dash
    state.plugs = FakePlugs([Plug("10.0.0.1", "Lamp", False), Plug("10.0.0.2", "Fan", True)])
    asyncio.run(state.poll_lights())
    return app, state


def _center(box):
    return (box[0] + box[2]) // 2, (box[1] + box[3]) // 2


def test_tap_needs_token():
    off = create_app(Settings(), fetch=lambda: None, poll=False)
    with TestClient(off) as client:
        assert client.post("/tap?x=1&y=1").status_code == 503  # no CONTROL_TOKEN: controls are off
    app, _ = _app()
    with TestClient(app) as client:
        assert client.post("/tap?x=1&y=1").status_code == 401
        assert client.post("/tap?x=1&y=1", headers={"X-Kindle-Token": "nope"}).status_code == 401
    app, _ = _app(control_allow_ips=("192.168.113.66",))
    with TestClient(app) as client:  # the test client's address isn't on the list
        assert client.post("/tap?x=1&y=1", headers={"X-Kindle-Token": TOKEN}).status_code == 403


def test_tap_tab_switches_page():
    app, _ = _app()
    lights_tab = next(box for box, action in render.tab_boxes() if action == "page:lights")
    with TestClient(app) as client:
        x, y = _center(lights_tab)
        r = client.post(f"/tap?x={x}&y={y}&rotate=0&page=solar", headers={"X-Kindle-Token": TOKEN})
        assert r.status_code == 200
        left, top, w, h = lights_tab[0], lights_tab[1], lights_tab[2] - lights_tab[0], lights_tab[3] - lights_tab[1]
        assert r.text.splitlines() == [f"flash {left} {top} {w} {h}", "page lights"]
        # nothing there
        assert client.post("/tap?x=600&y=900&page=solar", headers={"X-Kindle-Token": TOKEN}).status_code == 204


def test_tap_plug_toggles_and_redraws_rotated():
    app, state = _app()
    with TestClient(app, headers=KINDLE) as client:
        before = client.get("/landscape.png?rotate=270&page=lights").content
        # the Fan tile, in landscape on a panel turned 270: convert its centre to panel pixels
        box = render.plug_boxes("landscape", 2)[1]
        px, py = touch.image_to_panel(*_center(box), 270, render.size("landscape"))
        r = client.post(f"/tap?x={px}&y={py}&rotate=270&page=lights", headers={"X-Kindle-Token": TOKEN})
        assert r.text.splitlines()[0] == "flash %d %d %d %d" % touch.box_to_panel(box, 270, render.size("landscape"))
        assert r.text.splitlines()[1] == "redraw"
        assert state.plugs.toggled == ["10.0.0.2"]
        assert client.get("/landscape.png?rotate=270&page=lights").content != before
        assert client.get("/status", headers=ADMIN).json()["plugs"][1]["on"] is False


def test_pages_are_served_and_validated():
    app, _ = _app()
    with TestClient(app, headers=KINDLE) as client:
        lights = client.get("/portrait.png?page=lights")
        assert lights.status_code == 200 and lights.content != client.get("/portrait.png").content
        assert client.get("/portrait.png?page=nope").status_code == 400


def test_lights_page_has_eink_grays_and_offline_tiles():
    plugs = [
        Plug("a", "Lamp", True, "light", 7, 0.05, 0.8),
        Plug("b", "A very long plug name indeed, longer than two lines can hold", False, "ev charger", 0, 6.8, 123.4),
        Plug("c", "Gone", None, "fan"),
    ]
    for layout in render.LAYOUTS:
        img = render.render_lights(plugs, datetime(2026, 10, 2, tzinfo=timezone.utc), ZoneInfo("UTC"), layout)
        assert img.size == render.size(layout)
        assert {v for v, n in enumerate(img.histogram()) if n} <= {i * 0x11 for i in range(16)}


def test_kasa_hosts_with_kinds(monkeypatch):
    monkeypatch.setenv("KASA_HOSTS", "10.0.0.1=light, 10.0.0.2 , 10.0.0.3=ev charger")
    s = Settings.from_env()
    assert s.kasa_hosts == ("10.0.0.1", "10.0.0.2", "10.0.0.3")
    assert dict(s.kasa_kinds) == {"10.0.0.1": "light", "10.0.0.3": "ev charger"}
    plugs = KasaPlugs(s.kasa_hosts, dict(s.kasa_kinds))
    assert [p.kind for p in plugs.plugs] == ["light", "", "ev charger"]


def test_long_names_wrap_to_two_lines():
    d = ImageDraw.Draw(Image.new("L", (10, 10)))
    assert render._wrap(d, "Living Room Back Door", 40, 460) == ["Living Room Back", "Door"]
    lines = render._wrap(d, "Bedroom Desk LEDs With An Extremely Long Name", 40, 460)
    assert len(lines) == 2 and lines[1].endswith("\u2026")
    assert render._wrap(d, "Lamp", 40, 460) == ["Lamp"]
    assert render.fmt_watts(1452) == "1.45 kW" and render.fmt_watts(32) == "32 W" and render.fmt_watts(None) == "--"


def test_refresh_keeps_a_toggle_made_while_it_ran():
    plugs = KasaPlugs(("h1",))
    plugs.plugs = [Plug("h1", "Lamp", False)]

    async def slow_read(host):
        await asyncio.sleep(0.05)
        return Plug(host, "Lamp", False)  # read before the toggle landed

    async def run():
        plugs._read = slow_read
        task = asyncio.create_task(plugs.refresh())
        await asyncio.sleep(0)
        plugs._toggles["h1"] = 1  # what toggle() does first...
        plugs.plugs = [Plug("h1", "Lamp", True)]  # ...and the state it reads back
        await task

    asyncio.run(run())
    assert plugs.plugs == [Plug("h1", "Lamp", True)]


def test_kindle_routes_need_the_token():
    app, _ = _app(control_allow_ips=("testclient",))
    with TestClient(app) as client:
        for path in ("/portrait.png", "/landscape.png?rotate=90", "/dashboard.png", "/rotate/count"):
            assert client.get(path).status_code == 401
            assert client.get(path, headers=KINDLE).status_code == 200
        assert client.get("/health").status_code in (200, 503)  # open, for Docker's healthcheck
        assert client.get("/status").status_code == 401  # admin only
        assert client.get("/status", headers=KINDLE).status_code == 401
    app, _ = _app(control_allow_ips=("192.168.113.66",))
    with TestClient(app) as client:  # not the Kindle's address; /admin isn't limited to it
        assert client.get("/portrait.png", headers=KINDLE).status_code == 403
        assert client.get("/admin", headers=ADMIN).status_code == 200


def test_admin_needs_login():
    off = create_app(Settings(control_token=TOKEN), fetch=lambda: None, poll=False)
    with TestClient(off) as client:
        assert client.get("/admin", headers=ADMIN).status_code == 503  # no ADMIN_PASSWORD: off
    app, _ = _app()
    with TestClient(app) as client:
        r = client.get("/admin")
        assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Basic")
        wrong = {"Authorization": "Basic " + base64.b64encode(b"admin:nope").decode()}
        assert client.get("/admin", headers=wrong).status_code == 401
        assert client.get("/admin", headers={"Authorization": "Basic !!!"}).status_code == 401
        assert client.get("/admin", headers=KINDLE).status_code == 401
        # a form on another site can't use the browser's saved login
        evil = {**ADMIN, "Origin": "http://evil.example"}
        assert client.post("/admin/rotate", headers=evil, follow_redirects=False).status_code == 403
        same = {**ADMIN, "Origin": "http://testserver"}
        assert client.post("/admin/rotate", headers=same, follow_redirects=False).status_code == 303


def test_admin_page_shows_the_kindle_view_and_toggles_plugs():
    app, state = _app()
    with TestClient(app) as client:
        assert "No image fetched" in client.get("/admin", headers=ADMIN).text
        client.get("/landscape.png?rotate=270&page=lights&battery=55", headers=KINDLE)
        page = client.get("/admin", headers=ADMIN).text
        assert "Showing <b>lights</b>, landscape (rotate 270)" in page and "battery 55%" in page
        assert "/admin/image.png?page=lights&layout=landscape" in page
        assert "Lamp" in page and "Fan" in page
        img = client.get("/admin/image.png?page=lights&layout=landscape", headers=ADMIN)
        assert Image.open(BytesIO(img.content)).size == (1648, 1236)
        assert client.get("/admin/image.png?layout=sideways", headers=ADMIN).status_code == 400
        r = client.post("/admin/plug?host=10.0.0.1", headers=ADMIN, follow_redirects=False)
        assert r.status_code == 303 and state.plugs.toggled == ["10.0.0.1"]
        assert client.post("/admin/plug?host=8.8.8.8", headers=ADMIN).status_code == 404


def test_ev_charger_needs_a_second_tap(monkeypatch):
    app, state = _app(kasa_kinds=(("10.0.0.2", "EV Charger"),))
    box = render.plug_boxes("portrait", 2)[1]
    x, y = _center(box)
    tap = lambda c: c.post(f"/tap?x={x}&y={y}&page=lights", headers=KINDLE)  # noqa: E731
    with TestClient(app, headers=KINDLE) as client:
        plain = client.get("/portrait.png?page=lights").content
        assert tap(client).text.splitlines()[1:] == ["redraw", "later 11"]
        assert state.plugs.toggled == [] and state.armed() == "10.0.0.2"
        prompt = client.get("/portrait.png?page=lights").content
        assert prompt != plain
        tap(client)  # the second tap switches it
        assert state.plugs.toggled == ["10.0.0.2"] and state.armed() is None

        # a prompt expires...
        tap(client)
        now = time.monotonic()
        monkeypatch.setattr(time, "monotonic", lambda: now + app_mod.CONFIRM_S + 1)
        tap(client)  # ...so this only asks again
        assert state.plugs.toggled == ["10.0.0.2"] and state.armed() == "10.0.0.2"

        # tapping anything else cancels it; the plain Lamp switches at once
        lx, ly = _center(render.plug_boxes("portrait", 2)[0])
        client.post(f"/tap?x={lx}&y={ly}&page=lights", headers=KINDLE)
        assert state.plugs.toggled == ["10.0.0.2", "10.0.0.1"] and state.armed() is None


class FakeEnergy:
    def __init__(self, months):
        self.months, self.calls = months, 0

    async def get_daily_stats(self, *, year, month, kwh=True):
        self.calls += 1
        return self.months.get((year, month), {})


def test_last_30_days_spans_the_month_boundary(monkeypatch):
    import kindle_dash.plugs as plugs_mod

    class Today(plugs_mod.date):
        @classmethod
        def today(cls):
            return cls(2026, 10, 2)

    monkeypatch.setattr(plugs_mod, "date", Today)
    # Sept 2..30 (29 days) + Oct 1 count; Sept 1 and today (Oct 2) don't
    energy = FakeEnergy({(2026, 9): {d: 1.0 for d in range(1, 31)}, (2026, 10): {1: 0.5, 2: 9.0}})
    dev = type("Dev", (), {"modules": {"Energy": energy}})()
    plugs = KasaPlugs(("h",))
    assert asyncio.run(plugs._last30("h", dev)) == 29.5
    assert asyncio.run(plugs._last30("h", dev)) == 29.5 and energy.calls == 2  # cached
    nomon = type("Dev", (), {"modules": {}})()
    assert asyncio.run(KasaPlugs(("h",))._last30("h", nomon)) is None
