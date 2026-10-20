import json
from datetime import datetime, timezone
from io import BytesIO
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from PIL import Image

from kindle_dash import health, prefs, render
from kindle_dash.app import create_app
from kindle_dash.camera import Shot
from kindle_dash.config import Settings
from kindle_dash.prefs import KindleDevice, Prefs

TOKEN = "s3cret"
KINDLE = {"X-Kindle-Token": TOKEN}
TZ = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 10, 19, 20, 0, tzinfo=timezone.utc)
GRAYS = {i * 0x11 for i in range(16)}
DEVICE_QS = "&battery=80&charging=0&light=12&lightmax=24&warm=16&wifi=-56&up=3600&ver=" + prefs.KINDLE_SCRIPT_VERSION


def _app(tmp_path, **kw):
    s = Settings(tz=str(TZ), control_token=TOKEN, admin_password="pw", prefs_file=str(tmp_path / "settings.json"), **kw)
    app = create_app(s, fetch=lambda: None, poll=False)
    return app, app.state.dash


def _center(box):
    return (box[0] + box[2]) // 2, (box[1] + box[3]) // 2


def _box(name, action, layout="portrait"):
    return next(b for b, a in render.settings_rows(layout)[name][2] if a == action)


def _tap(client, box, page="settings", rotate=0):
    x, y = _center(box)
    return client.post(f"/tap?x={x}&y={y}&rotate={rotate}&page={page}")


def test_prefs_round_trip_and_bad_files(tmp_path):
    path = str(tmp_path / "s.json")
    assert prefs.load(path, ("solar",)) == Prefs()  # no file yet
    prefs.save(path, Prefs("dark", "car", 900, False))
    assert prefs.load(path, ("solar", "car")) == Prefs("dark", "car", 900, False)
    assert prefs.load(path, ("solar",)).home == "solar"  # a tab that's gone
    (tmp_path / "s.json").write_text(json.dumps({"theme": "neon", "idle_return": 7, "extra": 1}))
    assert prefs.load(path, ("solar",)) == Prefs()
    (tmp_path / "s.json").write_text("{half")
    assert prefs.load(path, ("solar",)) == Prefs()
    prefs.save("", Prefs("dark"))  # not saved, no error
    prefs.save(str(tmp_path / "missing" / "s.json"), Prefs("dark"))  # logged, no error


def test_auto_theme_follows_the_dark_hours():
    at = lambda h, m=0: datetime(2026, 10, 19, h, m)  # noqa: E731
    assert prefs.is_dark("dark", at(12), "21:00", "07:00") and not prefs.is_dark("light", at(23), "21:00", "07:00")
    assert [prefs.is_dark("auto", at(h), "21:00", "07:00") for h in (20, 21, 0, 6, 7)] == [False, True, True, True, False]
    assert [prefs.is_dark("auto", at(h), "13:00", "15:30") for h in (12, 13, 15, 16)] == [False, True, True, False]


def test_device_report_drops_anything_malformed():
    dev = prefs.device_from({"light": "12", "lightmax": "24", "warm": "x", "wifi": "-56", "up": "-3", "ver": prefs.KINDLE_SCRIPT_VERSION}, "10.0.0.9", 90)
    assert dev == KindleDevice(12, 24, None, -56, None, prefs.KINDLE_SCRIPT_VERSION, "10.0.0.9", 90)
    assert dev.scripts_current
    assert prefs.device_from({"ver": "<script>"}, None, 0).version is None
    assert not prefs.device_from({}, None, 0).scripts_current


def test_settings_page_renders_every_state():
    devices = [None, KindleDevice(rotate=270), KindleDevice(0, 24, None, -80, 90000, prefs.KINDLE_SCRIPT_VERSION, "10.0.0.9", 0),
               KindleDevice(24, 24, 24, -40, 60, prefs.KINDLE_SCRIPT_VERSION, "10.0.0.9", 180)]  # fmt: skip
    for layout in render.LAYOUTS:
        for dev in devices:
            for p, confirm in ((Prefs(), None), (Prefs("dark", "cameras", 0, False), "set:exit"), (Prefs("auto", "car", 1800), None)):
                img = render.render_settings(p, dev, NOW, TZ, layout, render.KindleBattery(9, True), confirm)
                assert img.size == render.size(layout) and set(img.get_flattened_data()) <= GRAYS


def test_settings_buttons_fit_and_never_overlap():
    for layout in render.LAYOUTS:
        w, h = render.size(layout)
        boxes = [b for _, _, choices in render.settings_rows(layout).values() for b, _ in choices]
        assert all(render.HEADER_RULE_Y < y0 and 0 <= x0 and x1 <= w and y1 <= h - 40 for x0, y0, x1, y1 in boxes)
        for i, a in enumerate(boxes):
            for b in boxes[i + 1 :]:
                assert a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1], (layout, a, b)
        taps = render.targets("settings", layout, [])
        actions = {t.action for t in taps}
        assert {"page:health", "page:settings", "set:rotate:270", "set:theme:dark", "set:light:0", "set:light:12", "set:warm:12", "set:exit"} <= actions
        assert {"set:idle:0", "set:idle:1800", "set:idlelight:1", "set:home:cameras", "set:clean"} <= actions


def test_dark_mode_keeps_photos_as_they_are():
    pic = Image.linear_gradient("L").resize((640, 360))
    img = render.render_cameras([Shot("a", "Garage", pic, NOW)], NOW, TZ, "portrait")
    (x0, y0, x1, y1), = img.info["photos"]
    dark = render.invert(img)
    assert dark.crop((x0, y0, x1, y1)).tobytes() == img.crop((x0, y0, x1, y1)).tobytes()
    assert dark.getpixel((5, 5)) == 255 - img.getpixel((5, 5)) == 0  # the page around it turns black
    assert set(dark.get_flattened_data()) <= GRAYS


def test_the_clock_menu_switches_between_status_and_settings(tmp_path):
    app, _ = _app(tmp_path)
    status, settings = (box for box, _ in render.menu_boxes("portrait"))
    with TestClient(app, headers=KINDLE) as client:
        assert _tap(client, render.clock_target("portrait")[0], page="solar").text.splitlines()[1] == "page health"
        assert _tap(client, settings, page="health").text.splitlines()[1] == "page settings"
        assert _tap(client, status, page="settings").text.splitlines()[1] == "page health"
        assert _tap(client, settings, page="solar").status_code == 204  # only on those two pages
        for page in ("health", "settings"):
            assert client.get(f"/portrait.png?page={page}").status_code == 200


def test_theme_tap_saves_and_darkens_every_page(tmp_path):
    app, state = _app(tmp_path)
    with TestClient(app, headers=KINDLE) as client:
        r = client.get("/portrait.png?page=solar")
        assert r.headers["x-kindle-settings"] == "home=solar idle=300 idlelight=1 theme=light"
        assert Image.open(BytesIO(r.content)).getpixel((5, 5)) == 255
        assert _tap(client, _box("theme", "set:theme:dark")).text.splitlines()[1:] == ["redraw"]
        assert json.loads((tmp_path / "settings.json").read_text())["theme"] == "dark"
        r = client.get("/portrait.png?page=solar")
        assert r.headers["x-kindle-settings"] == f"home=solar idle=300 idlelight=1 theme=dark full={prefs.DARK_FULL_REFRESH_EVERY}"
        assert Image.open(BytesIO(r.content)).getpixel((5, 5)) == 0
        assert _tap(client, _box("theme", "set:theme:light")).text.split("\n")[0].endswith(" white")  # flashed on the dark page
    assert create_app(state.s, fetch=lambda: None, poll=False).state.dash.prefs.theme == "light"  # kept across restarts


def test_kindle_side_settings_reach_the_kindle(tmp_path):
    app, state = _app(tmp_path)
    with TestClient(app, headers=KINDLE) as client:
        client.get("/portrait.png?page=settings" + DEVICE_QS)
        assert state.device.light == 12 and state.device.ip
        assert _tap(client, _box("light", "set:light:6")).text.splitlines()[1:] == ["light 12", "redraw"]
        assert _tap(client, _box("light", "set:light:12")).text.splitlines()[1:] == ["light 24", "redraw"]
        assert _tap(client, _box("light", "set:light:0")).text.splitlines()[1:] == ["light 0", "redraw"]
        assert _tap(client, _box("warm", "set:warm:3")).text.splitlines()[1:] == ["warm 6", "redraw"]
        assert _tap(client, _box("rotate", "set:rotate:90")).text.splitlines()[1:] == ["rotate 90", "redraw"]
        assert _tap(client, _box("theme", "set:clean")).text.splitlines()[1:] == ["clean", "redraw"]
        _tap(client, _box("home", "set:home:car"))
        _tap(client, _box("idle", "set:idle:900"))
        _tap(client, _box("idlelight", "set:idlelight:0"))
        r = client.get("/portrait.png?page=settings" + DEVICE_QS)
        assert r.headers["x-kindle-settings"] == "home=car idle=900 idlelight=0 theme=light"
        assert client.get("/status", headers={"Authorization": "Basic YWRtaW46cHc="}).json()["settings"]["home"] == "car"


def test_landscape_taps_land_on_the_rotated_buttons(tmp_path):
    from kindle_dash import touch

    app, _ = _app(tmp_path)
    box = _box("theme", "set:theme:dark", "landscape")
    px, py = touch.image_to_panel(*_center(box), 270, render.size("landscape"))
    with TestClient(app, headers=KINDLE) as client:
        assert client.post(f"/tap?x={px}&y={py}&rotate=270&page=settings").status_code == 200
    assert app.state.dash.prefs.theme == "dark"


def test_exit_takes_two_taps(tmp_path):
    app, state = _app(tmp_path)
    exit_box = _box("kindle", "set:exit")
    with TestClient(app, headers=KINDLE) as client:
        assert _tap(client, exit_box).text.splitlines()[1:] == ["redraw", "later 11"]  # redraw when the prompt runs out
        assert state.armed() == "set:exit"
        assert _tap(client, exit_box).text.splitlines()[1:] == ["exit"]
        _tap(client, exit_box)
        _tap(client, _box("theme", "set:clean"))  # another tap cancels it
        assert state.armed() is None
        assert "exit" not in _tap(client, exit_box).text.splitlines()


def test_health_flags_old_kindle_scripts(tmp_path):
    app, state = _app(tmp_path)
    assert not [c for c in health.checks(state, NOW) if c.name == "Kindle scripts"]  # no fetch yet
    with TestClient(app, headers=KINDLE) as client:
        client.get("/portrait.png?page=solar&battery=50")  # an older Kindle: no version
        check = next(c for c in health.checks(state, NOW) if c.name == "Kindle scripts")
        assert not check.ok and "kindledash.sh" in check.detail
        client.get("/portrait.png?page=solar" + DEVICE_QS)
        assert next(c for c in health.checks(state, NOW) if c.name == "Kindle scripts").ok


def test_an_expired_prompt_leaves_the_screen_at_the_next_fetch(tmp_path):
    app, state = _app(tmp_path)
    exit_box = _box("kindle", "set:exit")
    with TestClient(app, headers=KINDLE) as client:
        _tap(client, exit_box)
        prompt = client.get("/portrait.png?page=settings").content
        state.confirm = (state.confirm[0], 0.0)  # run out
        assert client.get("/portrait.png?page=settings").content != prompt
        assert state.confirm is None
        assert _tap(client, exit_box).text.splitlines()[1:] == ["redraw", "later 11"]  # starts again, as the screen now says
