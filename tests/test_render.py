import base64
from datetime import datetime, timezone
from io import BytesIO
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from kindle_dash import render
from kindle_dash.app import create_app
from kindle_dash.config import Settings
from kindle_dash.data import Snapshot
from kindle_dash.sample import sample_snapshot

TZ = ZoneInfo("America/Los_Angeles")
# the Kindle's token and the admin login, for routes that need either
AUTH = {"X-Kindle-Token": "t", "Authorization": "Basic " + base64.b64encode(b"admin:pw").decode()}
NOW = datetime(2026, 10, 2, 22, 30, tzinfo=timezone.utc)


def _grays(img):
    return {v for v, n in enumerate(img.histogram()) if n}


def test_render_is_portrait_grayscale_with_eink_levels():
    img = render.render(sample_snapshot(NOW, TZ), TZ)
    assert img.mode == "L"
    assert img.size == (1236, 1648)
    assert _grays(img) <= {i * 0x11 for i in range(16)}


def test_landscape_layout():
    img = render.render(sample_snapshot(NOW, TZ), TZ, layout="landscape")
    assert img.mode == "L" and img.size == (1648, 1236)
    assert _grays(img) <= {i * 0x11 for i in range(16)}


@pytest.mark.parametrize("layout,size", [("portrait", (1236, 1648)), ("landscape", (1648, 1236))])
def test_render_survives_empty_snapshot(layout, size):
    assert render.render(Snapshot(fetched_at=NOW), TZ, layout=layout).size == size


def test_stale_badge_marks_header_only():
    img = render.render(sample_snapshot(NOW, TZ), TZ)
    stale = render.stale_badge(img, NOW, TZ)
    diff = [(x, y) for x in range(0, 1236, 4) for y in range(0, 1648, 4) if img.getpixel((x, y)) != stale.getpixel((x, y))]
    assert diff and max(y for _, y in diff) < 130


@pytest.mark.parametrize(
    "seconds,expected",
    [(None, "--"), (-1, "--"), (59, "0m"), (3 * 3600 + 5 * 60, "3h 05m"), (2 * 86400 + 7200, "2d 2h"), (864000, "> 10 days")],
)
def test_fmt_duration(seconds, expected):
    assert render.fmt_duration(seconds) == expected


def test_formatting_helpers():
    assert render.fmt_num(-12.34, 1) == "−12.3"
    assert render.fmt_num(5, 0, signed=True) == "+5"
    assert render.fmt_energy(3184.6) == ("3,185", "Wh")
    assert render.fmt_energy(12345) == ("12.3", "kWh")
    assert render.fmt_money(1.234) == "$1.23"


def test_battery_state():
    snap = Snapshot(fetched_at=NOW, latest={"current_a": -4.0, "ttg_s": 7200.0})
    assert render.battery_state(snap) == ("DISCHARGING", "2h 00m")
    snap.latest["current_a"] = 6.0
    assert render.battery_state(snap)[0] == "CHARGING"


def test_endpoints_serve_cached_png_and_go_stale():
    calls = {"fail": False}

    def fetch():
        if calls["fail"]:
            raise ConnectionError("influx down")
        return sample_snapshot(datetime.now(timezone.utc), TZ)

    app = create_app(Settings(tz="America/Los_Angeles", control_token="t", admin_password="pw"), fetch=fetch, poll=False)
    state = app.state.dash
    with TestClient(app, headers=AUTH) as client:
        assert client.get("/health").status_code == 503
        state.update(fetch)
        r = client.get("/dashboard.png")
        assert r.status_code == 200 and r.headers["content-type"] == "image/png"
        assert Image.open(BytesIO(r.content)).size == (1236, 1648)
        assert client.get("/dashboard.png", headers={"If-None-Match": r.headers["etag"]}).status_code == 304
        assert Image.open(BytesIO(client.get("/dashboard.png?rotate=90").content)).size == (1648, 1236)
        assert client.get("/portrait.png").content == r.content
        land = client.get("/landscape.png")
        assert Image.open(BytesIO(land.content)).size == (1648, 1236)
        # rotated for the Kindle's portrait framebuffer
        assert Image.open(BytesIO(client.get("/landscape.png?rotate=90").content)).size == (1236, 1648)
        assert client.get("/dashboard.png?rotate=45").status_code == 400
        assert client.get("/health").status_code == 200
        status = client.get("/status").json()
        assert status["values"]["battery_soc_pct"] is not None and status["stale_since"] is None

        calls["fail"] = True
        state.update(fetch)
        r2 = client.get("/dashboard.png")
        assert r2.status_code == 200 and r2.content != r.content  # old image + badge
        assert client.get("/landscape.png").content != land.content
        status = client.get("/status").json()
        assert status["stale_since"] == status["last_success"]
        assert "influx down" in status["last_error"]


def test_rotate_requests():
    app = create_app(Settings(tz="America/Los_Angeles", control_token="t", admin_password="pw"), fetch=lambda: None, poll=False)
    with TestClient(app, headers=AUTH) as client:
        assert client.get("/rotate/count").text == "0"
        assert "Rotate Kindle" in client.get("/admin").text
        r = client.post("/admin/rotate", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/admin?done=rotate"
        assert client.get("/rotate/count").text == "1"
        assert "Rotate requested" in client.get("/admin?done=rotate").text


def test_kindle_battery_report_reaches_header_and_status():
    snap = sample_snapshot(NOW, TZ)
    app = create_app(Settings(tz="America/Los_Angeles", control_token="t", admin_password="pw"), fetch=lambda: snap, poll=False)
    state = app.state.dash
    with TestClient(app, headers=AUTH) as client:
        state.update(lambda: snap)
        plain = state.images[("solar", "portrait")]
        plain_land = client.get("/landscape.png?rotate=270").content
        assert client.get("/status").json()["kindle"] is None
        # malformed reports are ignored, never break the image
        assert client.get("/portrait.png?rotate=0&battery=&charging=").status_code == 200
        assert client.get("/portrait.png?battery=abc").status_code == 200
        assert state.kindle is None
        r = client.get("/landscape.png?rotate=270&battery=64&charging=1")
        assert client.get("/status").json()["kindle"]["level"] == 64
        # the change is in the image served with the report, not a poll later
        assert r.content != plain_land
        assert client.get("/landscape.png?rotate=270&battery=64&charging=1").content == r.content
        assert state.images[("solar", "portrait")].tobytes() != plain.tobytes()
        state.update(lambda: snap)
        # header only: the rest of the image is unchanged
        assert state.images[("solar", "portrait")].crop((0, 130, 1236, 1648)).tobytes() == plain.crop((0, 130, 1236, 1648)).tobytes()
        state.kindle_at = datetime(2020, 1, 1, tzinfo=timezone.utc)  # long ago -> hidden again
        state.update(lambda: snap)
        assert state.images[("solar", "portrait")].tobytes() == plain.tobytes()
