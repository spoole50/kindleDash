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
NOW = datetime(2026, 10, 2, 22, 30, tzinfo=timezone.utc)


def _grays(img):
    return {v for v, n in enumerate(img.histogram()) if n}


def test_render_is_portrait_grayscale_with_eink_levels():
    img = render.render(sample_snapshot(NOW, TZ), TZ)
    assert img.mode == "L"
    assert img.size == (1236, 1648)
    assert _grays(img) <= {i * 0x11 for i in range(16)}


def test_render_survives_empty_snapshot():
    img = render.render(Snapshot(fetched_at=NOW), TZ)
    assert img.size == (1236, 1648)


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

    app = create_app(Settings(tz="America/Los_Angeles"), fetch=fetch, poll=False)
    state = app.state.dash
    with TestClient(app) as client:
        assert client.get("/health").status_code == 503
        state.update(fetch)
        r = client.get("/dashboard.png")
        assert r.status_code == 200 and r.headers["content-type"] == "image/png"
        assert Image.open(BytesIO(r.content)).size == (1236, 1648)
        assert client.get("/dashboard.png", headers={"If-None-Match": r.headers["etag"]}).status_code == 304
        assert Image.open(BytesIO(client.get("/dashboard.png?rotate=90").content)).size == (1648, 1236)
        assert client.get("/dashboard.png?rotate=45").status_code == 400
        assert client.get("/health").status_code == 200
        status = client.get("/status").json()
        assert status["values"]["battery_soc_pct"] is not None and status["stale_since"] is None

        calls["fail"] = True
        state.update(fetch)
        r2 = client.get("/dashboard.png")
        assert r2.status_code == 200 and r2.content != r.content  # old image + badge
        status = client.get("/status").json()
        assert status["stale_since"] == status["last_success"]
        assert "influx down" in status["last_error"]
