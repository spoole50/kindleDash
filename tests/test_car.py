import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import requests
from fastapi.testclient import TestClient

from kindle_dash import car as car_mod
from kindle_dash import health, render, touch
from kindle_dash.app import create_app
from kindle_dash.car import BlueLink, CarStatus, status_from
from kindle_dash.config import Settings

TZ = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 10, 19, 18, 0, tzinfo=timezone.utc)
KINDLE = {"X-Kindle-Token": "s3cret"}


def vehicle(**kw):
    """A stand-in for the library's Vehicle, as the US API fills it for a parked Ioniq 5."""
    v = dict(
        name="Carmen Elektra", model="IONIQ 5", last_updated_at=NOW - timedelta(hours=2),
        ev_battery_percentage=34, ev_driving_range=73, ev_driving_range_unit="mi",
        ev_battery_is_plugged_in=0, ev_battery_is_charging=False, ev_charging_power=None,
        ev_estimated_current_charge_duration=0, ev_charge_limits_ac=100, ev_charge_limits_dc=90,
        is_locked=True, air_control_is_on=False, air_temperature=None, defrost_is_on=False,
        car_battery_percentage=92, odometer=38105.2, odometer_unit="mi",
        tire_pressure_all_warning_is_on=False, tire_pressure_front_left=36, tire_pressure_front_right=36,
        tire_pressure_rear_left=35, tire_pressure_rear_right=None, tire_pressure_unit=None,
    )  # fmt: skip
    v.update(kw)
    return SimpleNamespace(**v)


def sample(**kw) -> CarStatus:
    return status_from(vehicle(**kw))


def test_status_from_vehicle():
    st = sample(trunk_is_open=True, ev_charge_port_door_is_open=True)
    assert (st.battery_pct, st.range, st.range_unit, st.plugged_in) == (34, 73, "mi", False)
    assert st.open == ("Liftgate", "Charge port")
    assert st.climate_set is None  # climate's off
    assert st.tires == (36, 36, 35, None) and st.tire_unit == "psi"
    assert sample(ev_battery_is_plugged_in=1).plugged_in is True
    assert sample(ev_battery_is_plugged_in=None).plugged_in is None
    assert sample(air_control_is_on=True, air_temperature=72).climate_set == 72


def test_car_page_renders_every_state():
    states = [
        sample(),
        sample(ev_battery_is_plugged_in=1, ev_battery_is_charging=True, ev_charging_power=7.2,
               ev_estimated_current_charge_duration=130, ev_charge_limits_ac=80),
        sample(is_locked=False, front_left_door_is_open=True, trunk_is_open=True, back_right_window_is_open=True,
               air_control_is_on=True, air_temperature=72, defrost_is_on=True, tire_pressure_all_warning_is_on=True),
        sample(**{k: None for k in ("ev_battery_percentage", "ev_driving_range", "is_locked", "air_control_is_on",
                                    "car_battery_percentage", "odometer", "tire_pressure_all_warning_is_on",
                                    "last_updated_at", "ev_charge_limits_ac", "ev_charge_limits_dc")}),
    ]  # fmt: skip
    grays = {v * 0x11 for v in range(16)}
    for layout in render.LAYOUTS:
        for st in states:
            for busy, wake_in in ((None, 0), ("wake", 0), ("read", 600), (None, 900)):
                img = render.render_car(st, NOW, TZ, layout, busy=busy, wake_in=wake_in, checked_at=NOW)
                assert img.size == render.size(layout)
                assert set(img.get_flattened_data()) <= grays
        for args in ({"st": None}, {"st": None, "error": "Login failed: bad password"}, {"st": None, "configured": False}):
            assert render.render_car(now=NOW, tz=TZ, layout=layout, **args).size == render.size(layout)


def test_six_tabs_clear_the_clock():
    for layout in render.LAYOUTS:
        clock = render.clock_target(layout)[0]
        assert all(t[0][2] <= clock[0] for t in render.tab_targets())


class FakeVM:
    """VehicleManager, offline: counts reads; `fail` makes the next call raise."""

    def __init__(self):
        self.token, self.vehicles = None, {"v1": vehicle()}
        self.cached = self.forced = 0
        self.fail_login = self.fail_read = None
        self.api = SimpleNamespace()

    def check_and_refresh_token(self):
        if self.fail_login:
            raise self.fail_login
        self.token = "t"

    def update_vehicle_with_cached_state(self, vid):
        if self.fail_read:
            raise self.fail_read
        self.cached += 1

    def force_refresh_vehicle_state(self, vid):
        self.forced += 1
        self.vehicles[vid].ev_battery_percentage = 35

    # commands: each returns a transaction id, which check_action_status resolves to `outcome`
    commands: list
    outcome = SimpleNamespace(name="SUCCESS")

    def _cmd(self, name):
        self.commands = getattr(self, "commands", []) + [name]
        return f"tid-{name}"

    lock = lambda self, vid: self._cmd("lock")  # noqa: E731
    unlock = lambda self, vid: self._cmd("unlock")  # noqa: E731
    stop_climate = lambda self, vid: self._cmd("climate_off")  # noqa: E731
    start_charge = lambda self, vid: self._cmd("charge_on")  # noqa: E731
    stop_charge = lambda self, vid: self._cmd("charge_off")  # noqa: E731

    def start_climate(self, vid, opts):
        self.climate_opts = opts
        return self._cmd("climate_on")

    def check_action_status(self, vid, tid, synchronous=False, timeout=120):
        assert synchronous and tid.startswith("tid-")
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def bluelink(wake_min_s=1800) -> tuple[BlueLink, FakeVM]:
    bl, vm = BlueLink("me@example.com", "pw", "1234", wake_min_s), FakeVM()
    bl._vm = vm
    return bl, vm


def test_wakes_at_most_every_wake_min_seconds():
    bl, vm = bluelink()
    assert bl.read() and (vm.cached, vm.forced) == (1, 0)
    assert bl.read(wake=True) and vm.forced == 1 and bl.status.battery_pct == 35
    assert bl.wake_ready_in() > 1700
    assert bl.read(wake=True) and (vm.cached, vm.forced) == (2, 1)  # too soon: a cached read instead
    bl.woken_at = time.monotonic() - 1801
    assert bl.read(wake=True) and vm.forced == 2


def test_a_refused_login_backs_off_but_a_network_error_doesnt(monkeypatch):
    bl, vm = bluelink()
    vm.fail_login = requests.ConnectionError("no route")
    assert not bl.read() and not bl.login_blocked() and "no route" in bl.error
    bl._vm = vm
    vm.fail_login = RuntimeError("Error Code: 1 - Login failed: bad credentials")
    assert not bl.read() and bl.login_blocked() and bl.error.startswith("Login failed")
    bl._vm = vm
    vm.fail_login = None
    assert not bl.read() and vm.cached == 0  # not even tried while blocked
    monkeypatch.setattr(car_mod, "LOGIN_RETRY_S", 0)
    assert bl.read() and bl.error is None and bl.login_failed_at is None


def test_a_failed_read_after_login_doesnt_block_logins():
    bl, vm = bluelink()
    vm.fail_read = RuntimeError("Service temporarily unavailable")
    assert not bl.read() and not bl.login_blocked() and bl.error == "Service temporarily unavailable"


def _app():
    settings = Settings(tz=str(TZ), control_token="s3cret", admin_password="pw", bluelink_user="me", bluelink_password="pw", bluelink_pin="1234")
    app = create_app(settings, fetch=lambda: None, poll=False)
    state = app.state.dash
    state.car._vm = FakeVM()
    return app, state


def test_tap_wakes_the_car_then_reads_hyundais_copy():
    app, state = _app()
    box = render.car_regions("portrait")["refresh"]
    x, y = (box[0] + box[2]) // 2, (box[1] + box[3]) // 2
    with TestClient(app, headers=KINDLE) as client:
        before = client.get("/portrait.png?page=car").content
        r = client.post(f"/tap?x={x}&y={y}&page=car")
        assert r.text.splitlines() == ["flash %d %d %d %d" % touch.box_to_panel(box, 0, render.size("portrait")), "redraw", "later 11"]
        assert state.armed() == "car:refresh" and state.car._vm.forced == 0  # TAP AGAIN TO WAKE THE CAR
        client.post(f"/tap?x={x}&y={y}&page=car")
        for _ in range(100):  # the read runs in the background
            if state.car.busy is None and state.car.checked_at:
                break
            time.sleep(0.02)
        vm = state.car._vm
        assert (vm.forced, vm.cached) == (1, 0)
        assert client.get("/portrait.png?page=car").content != before
        client.post(f"/tap?x={x}&y={y}&page=car")
        for _ in range(100):
            if vm.cached:
                break
            time.sleep(0.02)
        assert (vm.forced, vm.cached) == (1, 1)  # too soon to wake again
        car = client.get("/status", headers={"Authorization": "Basic YWRtaW46cHc="}).json()["car"]
        assert car["status"]["battery_pct"] == 35 and car["wake_ready_in_s"] > 1700


def test_health_reports_the_car():
    app, state = _app()
    state.refresh_car()
    row = next(c for c in health.checks(state, state.now()) if c.name == "Car (Blue Link)")
    assert row.ok and row.detail.startswith("34%, car reported")
    state.car._vm.fail_read = RuntimeError("Service temporarily unavailable")
    state.refresh_car()
    row = next(c for c in health.checks(state, state.now()) if c.name == "Car (Blue Link)")
    assert not row.ok and "last good" in row.detail
    assert "Car (Blue Link)" in state.problems


def test_no_car_configured():
    app = create_app(Settings(control_token="s3cret"), fetch=lambda: None, poll=False)
    with TestClient(app, headers=KINDLE) as client:
        assert client.get("/portrait.png?page=car").status_code == 200
        assert all(c.name != "Car (Blue Link)" for c in app.state.dash.checks)


def test_commands_show_once_the_car_confirms():
    bl, vm = bluelink()
    bl.read()
    assert bl.command("unlock") and bl.status.locked is False and bl.note() == "UNLOCK: done"
    assert bl.command("climate_on") and bl.status.climate_on and bl.status.climate_set == 72
    assert (vm.climate_opts.set_temp, vm.climate_opts.duration) == (72, 10)
    vm.outcome = SimpleNamespace(name="FAILED")
    assert not bl.command("lock") and bl.status.locked is False and bl.note() == "LOCK: the car refused"
    vm.outcome = SimpleNamespace(name="TIMEOUT")
    assert not bl.command("charge_on") and bl.note() == "START CHARGING: no answer from the car"
    assert vm.commands == ["unlock", "climate_on", "lock", "charge_on"] and bl.sending is None


def test_controls_follow_the_car():
    acts = lambda st: {k: a for k, (_, a) in render.car_controls("portrait", st).items()}  # noqa: E731
    assert acts(sample()) == {"refresh": "car:refresh", "locks": "car:unlock", "climate": "car:climate_on"}  # unplugged: no charge button
    assert acts(sample(is_locked=False, air_control_is_on=True, ev_battery_is_plugged_in=1))["locks"] == "car:lock"
    assert acts(sample(ev_battery_is_plugged_in=1))["charge"] == "car:charge_on"
    assert acts(sample(ev_battery_is_plugged_in=1, ev_battery_is_charging=True))["charge"] == "car:charge_off"
    assert acts(None) == {"refresh": "car:refresh"}
    st = sample(ev_battery_is_plugged_in=1, trunk_is_open=True, front_left_door_is_open=True)
    for layout in render.LAYOUTS:
        for confirm, sending in (("car:unlock", None), ("car:climate_on", None), ("car:charge_on", None), (None, "unlock"), (None, "charge_on")):
            img = render.render_car(st, NOW, TZ, layout, confirm=confirm, sending=sending, note="UNLOCK: done", climate_temp=72)
            assert img.size == render.size(layout)


def _tap(client, layout_box, page="car"):
    x, y = (layout_box[0] + layout_box[2]) // 2, (layout_box[1] + layout_box[3]) // 2
    return client.post(f"/tap?x={x}&y={y}&page={page}")


def test_unlock_takes_two_taps():
    app, state = _app()
    state.refresh_car()
    vm = state.car._vm
    locks = render.car_regions("portrait")["locks"]
    with TestClient(app, headers=KINDLE) as client:
        before = client.get("/portrait.png?page=car").content
        assert _tap(client, locks).text.splitlines()[-2:] == ["redraw", "later 11"]
        assert state.armed() == "car:unlock" and not getattr(vm, "commands", [])
        assert client.get("/portrait.png?page=car").content != before  # TAP AGAIN TO UNLOCK
        _tap(client, locks)
        for _ in range(100):
            if getattr(vm, "commands", None) and state.car.sending is None:
                break
            time.sleep(0.02)
        assert vm.commands == ["unlock"] and state.car.status.locked is False
        _tap(client, locks)  # now a LOCK prompt; a tap elsewhere cancels it
        assert state.armed() == "car:lock"
        _tap(client, render.car_regions("portrait")["climate"])
        assert state.armed() == "car:climate_on" and vm.commands == ["unlock"]
        assert client.get("/status", headers={"Authorization": "Basic YWRtaW46cHc="}).json()["car"]["note"] == "UNLOCK: done"


def test_unconfirmable_commands_are_checked_against_the_cars_status(monkeypatch):
    """Hyundai answers the status check for lock/unlock with INVALID DATA though they work."""
    monkeypatch.setattr(car_mod, "WATCH_EVERY_S", 0)
    bl, vm = bluelink()
    bl.read()
    vm.outcome = RuntimeError("check_action_status failed with HTTP 502: INVALID DATA")
    reads = vm.cached
    real = vm.update_vehicle_with_cached_state

    def cached(vid):  # the car reports the change on the third read
        real(vid)
        if vm.cached - reads >= 3:
            vm.vehicles[vid].is_locked = False

    vm.update_vehicle_with_cached_state = cached
    assert bl.command("unlock") and bl.status.locked is False and bl.note() == "UNLOCK: done"
    assert vm.cached - reads == 3
    monkeypatch.setattr(car_mod, "COMMAND_TIMEOUT_S", 0.05)
    assert not bl.command("climate_on") and bl.note() == "START CLIMATE: sent, not confirmed"


def test_no_wake_while_the_cars_report_is_fresh():
    bl, vm = bluelink()
    vm.vehicles["v1"].last_updated_at = datetime.now(timezone.utc) - timedelta(minutes=4)
    bl.read()
    assert 5 * 60 < bl.wake_ready_in() <= 6 * 60
    assert bl.read(wake=True) and vm.forced == 0  # a cached read instead


def test_the_real_library_never_asks_where_the_car_is():
    """findMyCar reaches the car itself; the cached read and the wake must not send it."""
    bl = BlueLink("me@example.com", "pw", "1234")
    vm = bl._manager()  # no network until a read
    assert vm.api._get_vehicle_location(None, None) is None
    assert "_get_vehicle_location" in vars(vm.api)  # the instance's, not the library's method
