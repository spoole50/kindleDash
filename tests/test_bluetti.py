from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from kindle_dash import bluetti, render
from kindle_dash.app import create_app
from kindle_dash.config import Settings
from kindle_dash.sample import sample_bluetti

TZ = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 10, 2, 22, 30, tzinfo=timezone.utc)
STATIONS = (("AP300", 2764.8), ("AC180", 1152.0))
KINDLE = {"X-Kindle-Token": "t"}


def rec(result, dev, field, value, t=NOW):
    return {"result": result, "device": dev, "_field": field, "_value": value, "_time": t}


def _grays(img):
    return {v for v, n in enumerate(img.histogram()) if n}


def test_build_snapshot_orders_sums_and_keeps_switches():
    t0 = NOW - timedelta(minutes=5)
    snap = bluetti.build_snapshot(
        [
            rec("latest", "AC180", "total_battery_percent", 80.0),
            rec("latest", "AC180", "dc_output_power", 40.0),
            rec("latest", "AC180", "ctrl_ac", False),
            rec("latest", "EB3A", "total_battery_percent", 50.0),
            rec("series", "AC180", "ac_output_power", 10.0, t0),
            rec("series", "AC180", "dc_output_power", 30.0, t0),
            rec("series", "AC180", "dc_output_power", 40.0),
            rec("series", "AC180", "total_battery_percent", 81.0, t0),
            rec("today_wh", "AC180", "dc_output_power", 120.0),
            rec("today_wh", "AC180", "ac_output_power", -0.5),
            rec("latest", "AC180", "ac_input_power", None),
        ],
        NOW,
        STATIONS,
    )
    assert [s.model for s in snap.stations] == ["AP300", "AC180", "EB3A"]  # listed first, then any other
    ap, ac, eb = snap.stations
    assert ap.latest == {} and ap.stale(NOW) and ap.percent is None
    assert ac.latest == {"total_battery_percent": 80.0, "dc_output_power": 40.0, "ctrl_ac": False}
    assert ac.watts_out == [(t0, 40.0), (NOW, 40.0)] and ac.soc == [(t0, 81.0)]
    assert ac.power_in is None and ac.power_out == 40.0 and ac.net is None
    assert ac.today_out_wh == 120.0 and ac.today_in_wh is None
    assert eb.capacity_wh is None and eb.name == "EB3A" and ap.name == "APEX 300"
    assert snap.latest_time == NOW


def test_state_and_time_left():
    st = bluetti.Station("AC180", 1000.0, {"total_battery_percent": 50.0, "ac_input_power": 0.0, "dc_input_power": 0.0,
                                           "ac_output_power": 0.0, "dc_output_power": 100.0}, NOW)  # fmt: skip
    assert st.state == "DISCHARGING" and st.time_left() == 5 * 3600  # 500 Wh at 100 W
    st.latest["dc_input_power"] = 350.0  # 250 W in: the other 500 Wh in 2 h
    assert st.state == "CHARGING" and st.time_left() == 2 * 3600
    st.latest["dc_input_power"] = 102.0
    assert st.state == "IDLE" and st.time_left() is None
    st.capacity_wh = None
    st.latest["dc_input_power"] = 0.0
    assert st.time_left() is None
    assert not st.stale(NOW + timedelta(seconds=60)) and st.stale(NOW + timedelta(minutes=5))


def test_queries_filter_the_bluetti_measurement():
    latest, series, today = bluetti.queries("bluetti", 24, NOW)
    assert 'r["_measurement"] == "bluetti"' in latest and "last()" in latest and '"ctrl_ac"' in latest
    assert "aggregateWindow(every: 5m" in series and '"ctrl_ac"' not in series
    assert NOW.isoformat() in today and "integral(unit: 1h)" in today


def test_render_layouts_and_eink_grays():
    snap = sample_bluetti(NOW, TZ)
    for layout in render.LAYOUTS:
        for s, now in ((snap, NOW), (snap, NOW + timedelta(hours=1)), (bluetti.BluettiSnapshot(NOW), NOW), (None, NOW)):
            img = render.render_bluetti(s, now, TZ, layout)
            assert img.size == render.size(layout)
            assert _grays(img) <= {i * 0x11 for i in range(16)}


def test_page_served_and_badged_when_influx_fails():
    calls = []

    def fetch():
        calls.append(1)
        if len(calls) > 1:
            raise ConnectionError("influx down")
        return sample_bluetti(datetime.now(timezone.utc), TZ)

    app = create_app(Settings(tz="UTC", control_token="t", admin_password="pw"), fetch=lambda: None, poll=False, fetch_bluetti=fetch)
    state = app.state.dash
    with TestClient(app, headers=KINDLE) as client:
        waiting = client.get("/portrait.png?page=bluetti").content
        state.update_bluetti(fetch)
        fresh = client.get("/portrait.png?page=bluetti").content
        assert fresh != waiting
        state.update_bluetti(fetch)  # fails: same data, with a STALE badge
        assert state.bluetti_stale_since and "influx down" in state.bluetti_error
        assert client.get("/portrait.png?page=bluetti").content != fresh
        assert client.get("/landscape.png?page=bluetti&rotate=270").status_code == 200
        status = client.get("/status", auth=("admin", "pw")).json()["bluetti"]
        assert [s["model"] for s in status["stations"]] == ["AP300", "AC180"] and status["error"]


def test_bluetti_settings(monkeypatch):
    assert Settings.from_env().bluetti_stations == STATIONS
    monkeypatch.setenv("BLUETTI_STATIONS", "AC180=1152, AP300=5529.6 ,EB3A")
    assert Settings.from_env().bluetti_stations == (("AC180", 1152.0), ("AP300", 5529.6), ("EB3A", None))


def test_big_numbers_switch_to_kw_and_render_at_any_size():
    assert render._watts_parts(352.4) == ("352", "W") and render._watts_parts(1840.0) == ("1.84", "kW")
    assert render._watts_parts(None) == ("--", "W")
    snap = sample_bluetti(NOW, TZ)
    for st in snap.stations:
        st.latest.update(total_battery_percent=100.0, ac_input_power=9999.0, dc_input_power=9999.0, ac_output_power=9999.0, dc_output_power=9999.0)
        st.today_wh = {"dc_input_power": 99999.0, "ac_output_power": 99999.0}
    for layout in render.LAYOUTS:
        assert render.render_bluetti(snap, NOW, TZ, layout).size == render.size(layout)
