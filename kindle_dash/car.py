"""The car, via Hyundai Blue Link (US), with hyundai_kia_connect_api.

Two kinds of read:

- cached: what the car last reported to Hyundai's servers (it reports after
  driving, charging, or a wake). Cheap, and the car stays asleep.
- wake: Hyundai asks the car for fresh numbers. That runs the car's modem off
  the 12 V battery and counts against Hyundai's daily limits, so it only
  happens on two taps, at most every CAR_WAKE_MIN_SECONDS, and not while the
  car's last report is under FRESH_S old.

Nothing on a timer reaches the car: the poll only reads Hyundai's copy.

Commands (lock, unlock, climate, charging) go to the car and wait for it to
confirm, up to about a minute.

The library's cached read also sends a findMyCar request (to the car itself)
on the first read and after every drive. The page doesn't show where the car
is, so that's switched off: the car stays asleep and its location isn't kept.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone

log = logging.getLogger("kindle_dash.car")

LOGIN_RETRY_S = 6 * 3600  # Hyundai locks the account after a few bad logins: don't keep trying a wrong password

OPENINGS = (  # (Vehicle attribute, how the page names it when open)
    ("front_left_door_is_open", "Driver door"),
    ("front_right_door_is_open", "Passenger door"),
    ("back_left_door_is_open", "Rear left door"),
    ("back_right_door_is_open", "Rear right door"),
    ("trunk_is_open", "Liftgate"),
    ("hood_is_open", "Frunk"),
    ("front_left_window_is_open", "Driver window"),
    ("front_right_window_is_open", "Passenger window"),
    ("back_left_window_is_open", "Rear left window"),
    ("back_right_window_is_open", "Rear right window"),
    ("sunroof_is_open", "Sunroof"),
    ("ev_charge_port_door_is_open", "Charge port"),
)
TIRES = ("front_left", "front_right", "rear_left", "rear_right")


@dataclass(frozen=True)
class CarStatus:
    """What the car page shows. None means the car didn't say."""

    name: str
    model: str
    reported_at: datetime | None  # when the car last reported to Hyundai
    battery_pct: float | None
    range: float | None
    range_unit: str
    plugged_in: bool | None
    charging: bool | None
    charge_kw: float | None
    charge_minutes: float | None  # until the charge limit
    limit_ac: int | None
    limit_dc: int | None
    locked: bool | None
    open: tuple[str, ...]  # doors, windows etc. that are open, from OPENINGS
    climate_on: bool | None
    climate_set: float | None  # the temperature climate is heading for, while it's on
    defrost_on: bool | None
    aux_battery_pct: float | None  # the 12 V battery
    odometer: float | None
    odometer_unit: str
    tire_warning: bool | None
    tires: tuple[float | None, ...]  # pressures: front left, front right, rear left, rear right
    tire_unit: str


def _num(v) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _unit(v) -> str:
    """The library's unit strings ("mi", "km"), or its PressureUnit enum."""
    if v is None:
        return ""
    return str(getattr(v, "value", v))


def status_from(v) -> CarStatus:
    """A CarStatus from the library's Vehicle."""
    plugged = getattr(v, "ev_battery_is_plugged_in", None)  # 0 unplugged; other numbers say what kind of charger
    return CarStatus(
        name=(v.name or "").strip() or "Car",
        model=(v.model or "").strip(),
        reported_at=v.last_updated_at,
        battery_pct=_num(v.ev_battery_percentage),
        range=_num(v.ev_driving_range),
        range_unit=_unit(v.ev_driving_range_unit) or "mi",
        plugged_in=None if plugged is None else bool(plugged),
        charging=v.ev_battery_is_charging,
        charge_kw=_num(getattr(v, "ev_charging_power", None)),
        charge_minutes=_num(v.ev_estimated_current_charge_duration),
        limit_ac=v.ev_charge_limits_ac,
        limit_dc=v.ev_charge_limits_dc,
        locked=v.is_locked,
        open=tuple(label for attr, label in OPENINGS if getattr(v, attr, None)),
        climate_on=v.air_control_is_on,
        climate_set=_num(v.air_temperature) if v.air_control_is_on else None,
        defrost_on=getattr(v, "defrost_is_on", None),
        aux_battery_pct=_num(v.car_battery_percentage),
        odometer=_num(v.odometer),
        odometer_unit=_unit(v.odometer_unit) or "mi",
        tire_warning=v.tire_pressure_all_warning_is_on,
        tires=tuple(_num(getattr(v, f"tire_pressure_{t}", None)) for t in TIRES),
        tire_unit=_unit(getattr(v, "tire_pressure_unit", None)) or "psi",
    )


def _network_error(exc: Exception) -> bool:
    """Couldn't reach Hyundai at all (no answer to judge the password by)."""
    import requests

    return isinstance(exc, (requests.ConnectionError, requests.Timeout, ConnectionError, TimeoutError))


class LoginRefused(Exception):
    """Hyundai answered and refused the login."""


# Remote commands: (what the button does, the change to show once the car confirms it)
COMMANDS = {
    "lock": ("LOCK", {"locked": True}),
    "unlock": ("UNLOCK", {"locked": False}),
    "climate_on": ("START CLIMATE", {"climate_on": True}),
    "climate_off": ("STOP CLIMATE", {"climate_on": False, "climate_set": None}),
    "charge_on": ("START CHARGING", {"charging": True}),
    "charge_off": ("STOP CHARGING", {"charging": False}),
}
COMMAND_TIMEOUT_S = 70  # how long to wait for the car to confirm a command
WATCH_EVERY_S = 10  # while confirming from Hyundai's copy of the status (see BlueLink._confirm)
FRESH_S = 10 * 60  # Hyundai's copy is this new or newer: a tap re-reads it instead of waking the car
NOTE_S = 10 * 60  # how long a command's outcome stays in the footer


class BlueLink:
    """One car on a Blue Link account. Reads and commands block (the library uses requests): run them in a thread."""

    def __init__(self, username: str, password: str, pin: str, wake_min_s: int = 1800, climate_temp: float = 72, climate_minutes: int = 10):
        self._creds = (username, password, pin)
        self.wake_min_s = wake_min_s
        self.climate_temp, self.climate_minutes = climate_temp, climate_minutes
        self._vm = None
        self._io = threading.Lock()  # one request to Hyundai at a time
        self.status: CarStatus | None = None
        self.error: str | None = None  # the last read failed: why
        self.checked_at: datetime | None = None  # last good read
        self.busy: str | None = None  # "wake" or "read" while one runs
        self.sending: str | None = None  # the command (a COMMANDS key) on its way to the car
        self._note: tuple[str, float] | None = None  # (how the last command went, monotonic time)
        self.woken_at: float | None = None  # monotonic time of the last wake (tried, not necessarily worked)
        self.login_failed_at: float | None = None

    def _manager(self):
        if self._vm is None:
            from hyundai_kia_connect_api import VehicleManager

            user, password, pin = self._creds
            vm = VehicleManager(region=3, brand=2, username=user, password=password, pin=pin)  # USA, Hyundai
            vm.api._get_vehicle_location = lambda token, vehicle: None  # findMyCar wakes the car: see the module docstring
            self._vm = vm
        return self._vm

    def _vehicle(self):
        """Logged in: (manager, vehicle id). Raises LoginRefused, after which logins wait LOGIN_RETRY_S."""
        try:
            vm = self._manager()
            vm.check_and_refresh_token()  # logs in again when the token's expired
        except Exception as exc:
            if _network_error(exc):
                raise
            self.login_failed_at = time.monotonic()
            self._vm = None
            msg = f"Login failed: {str(exc)[:120] or type(exc).__name__}"
            log.warning("car: %s (next try in %d h)", msg, LOGIN_RETRY_S // 3600)
            raise LoginRefused(msg) from exc
        if not vm.vehicles:
            raise RuntimeError("no vehicles on this Blue Link account")
        self.login_failed_at = None
        return vm, next(iter(vm.vehicles))

    def wake_ready_in(self) -> float:
        """Seconds until a tap may wake the car again (0: now).

        Not within wake_min_s of the last wake, and not within FRESH_S of the car's
        last report to Hyundai: then Hyundai's copy is already new (the car's just
        been driven, charged, woken or sent a command).
        """
        wait = 0.0 if self.woken_at is None else self.wake_min_s - (time.monotonic() - self.woken_at)
        reported = self.status.reported_at if self.status else None
        if reported is not None:
            wait = max(wait, FRESH_S - (datetime.now(timezone.utc) - reported).total_seconds())
        return max(0.0, wait)

    def login_blocked(self) -> bool:
        return self.login_failed_at is not None and time.monotonic() - self.login_failed_at < LOGIN_RETRY_S

    def note(self) -> str | None:
        """How the last command went, for a while after it."""
        if self._note and time.monotonic() - self._note[1] < NOTE_S:
            return self._note[0]
        return None

    def read(self, wake: bool = False) -> bool:
        """A cached read, or a wake if `wake` and one's allowed; True on success."""
        if self.login_blocked():
            return False
        with self._io:
            wake = wake and self.wake_ready_in() == 0
            self.busy = "wake" if wake else "read"
            if wake:
                self.woken_at = time.monotonic()
            try:
                vm, vid = self._vehicle()
                if wake:
                    vm.force_refresh_vehicle_state(vid)
                    log.info("car woken for fresh data")
                else:
                    vm.update_vehicle_with_cached_state(vid)
                self.status = status_from(vm.vehicles[vid])
                self.error = None
                self.checked_at = datetime.now(timezone.utc)
                return True
            except Exception as exc:
                if not isinstance(exc, LoginRefused):
                    log.warning("car %s failed: %s: %s", "wake" if wake else "read", type(exc).__name__, exc)
                self.error = str(exc)[:120] or type(exc).__name__
                return False
            finally:
                self.busy = None

    def command(self, action: str) -> bool:
        """Send a COMMANDS action and wait for the car to confirm it; True once it has."""
        label, change = COMMANDS[action]
        if self.login_blocked():
            self._note = (f"{label}: not sent, login failing", time.monotonic())
            return False
        with self._io:
            self.sending = action
            try:
                vm, vid = self._vehicle()
                tid = self._send(vm, vid, action)
                log.info("car: %s sent", label.lower())
            except Exception as exc:
                self.sending = None
                log.warning("car %s failed: %s: %s", action, type(exc).__name__, exc)
                self._note = (f"{label} FAILED: {str(exc)[:80] or type(exc).__name__}", time.monotonic())
                return False
            try:
                result = self._confirm(vm, vid, tid, change)
            finally:
                self.sending = None
            log.info("car: %s -> %s", label.lower(), result)
            if result == "SUCCESS":
                self._note = (f"{label}: done", time.monotonic())
                return True
            self._note = (
                {"FAILED": f"{label}: the car refused", "SENT": f"{label}: sent, not confirmed"}.get(result, f"{label}: no answer from the car"),
                time.monotonic(),
            )
            return False

    def _confirm(self, vm, vid: str, tid: str | None, change: dict) -> str:
        """Did the car do it? "SUCCESS", "FAILED", "SENT" (couldn't tell) or the library's TIMEOUT etc.

        Hyundai's action-status check answers some commands (lock and unlock on a
        2023 Ioniq 5) with "INVALID DATA" even though they work, so when it can't
        answer, watch Hyundai's copy of the car's status (cached reads: the car
        stays asleep) until it shows the change, which takes about 40 s.
        """
        if tid:
            try:
                result = vm.check_action_status(vid, tid, synchronous=True, timeout=COMMAND_TIMEOUT_S).name
            except Exception as exc:
                log.info("car: action status unavailable (%s: %s); watching the car's status instead", type(exc).__name__, str(exc)[:100])
            else:
                if result == "SUCCESS" and self.status:
                    if change.get("climate_on"):
                        change = {**change, "climate_set": self.climate_temp}
                    self.status = replace(self.status, **change)
                return result
        expect = {k: v for k, v in change.items() if k != "climate_set"}
        deadline = time.monotonic() + COMMAND_TIMEOUT_S
        while time.monotonic() < deadline:
            time.sleep(WATCH_EVERY_S)
            try:
                vm.update_vehicle_with_cached_state(vid)
            except Exception as exc:
                log.info("car: status read while confirming failed: %s", exc)
                continue
            st = status_from(vm.vehicles[vid])
            self.status, self.checked_at = st, datetime.now(timezone.utc)
            if all(getattr(st, k) == v for k, v in expect.items()):
                return "SUCCESS"
        return "SENT"

    def _send(self, vm, vid: str, action: str) -> str | None:
        """The library call for `action`; returns its transaction id."""
        if action == "climate_on":
            from hyundai_kia_connect_api import ClimateRequestOptions

            opts = ClimateRequestOptions(set_temp=self.climate_temp, duration=self.climate_minutes, climate=True, defrost=False, heating=0)
            return vm.start_climate(vid, opts)
        return {
            "lock": vm.lock,
            "unlock": vm.unlock,
            "climate_off": vm.stop_climate,
            "charge_on": vm.start_charge,
            "charge_off": vm.stop_charge,
        }[action](vid)
