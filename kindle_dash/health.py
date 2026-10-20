"""Is each subsystem working? One check per data source, device or camera.

A check that's still starting up (nothing tried yet) counts as OK, so a
restart doesn't flash the issues badge.
"""

from __future__ import annotations

from datetime import datetime
from typing import NamedTuple

VICTRON_MAX_AGE_S = 10 * 60  # the Victron logger writes every few seconds


class Check(NamedTuple):
    name: str
    ok: bool
    detail: str


def checks(state, now: datetime) -> list[Check]:
    tz = state.s.zone

    def hm(t: datetime) -> str:
        return t.astimezone(tz).strftime("%H:%M")

    def hms(t: datetime) -> str:
        return t.astimezone(tz).strftime("%H:%M:%S")

    out: list[Check] = []

    # solar: InfluxDB answering, and the Victron logger still writing to it
    name, snap = "Solar (Victron)", state.snapshot
    if state.stale_since:
        out.append(Check(name, False, f"InfluxDB failing since {hm(state.stale_since)}: {state.last_error}"))
    elif snap is None:
        out.append(Check(name, True, "Waiting for the first reading"))
    elif snap.latest_time is None:
        out.append(Check(name, False, "No Victron readings in the last day"))
    elif (now - snap.latest_time).total_seconds() > VICTRON_MAX_AGE_S:
        out.append(Check(name, False, f"No new Victron readings since {hm(snap.latest_time)}"))
    else:
        out.append(Check(name, True, f"Newest reading {hms(snap.latest_time)}"))

    # Bluetti: InfluxDB, then each station (bluetti2influx stopped or out of range)
    if state.s.bluetti_bucket:
        b = state.bluetti
        if state.bluetti_error:
            out.append(Check("Bluetti", False, f"InfluxDB: {state.bluetti_error}"))
        elif b is None:
            out.append(Check("Bluetti", True, "Waiting for the first reading"))
        else:
            for st in b.stations:
                if not st.stale(now):
                    pct = "" if st.percent is None else f"{st.percent:.0f}%, "
                    out.append(Check(f"Bluetti {st.name}", True, f"{pct}newest reading {hms(st.latest_time)}"))
                elif st.latest_time:
                    out.append(Check(f"Bluetti {st.name}", False, f"No readings since {hm(st.latest_time)}. Is bluetti2influx running?"))
                else:
                    out.append(Check(f"Bluetti {st.name}", False, "No readings in the last day. Is bluetti2influx running?"))

    if state.s.kasa_hosts:
        plugs = state.plugs.plugs
        if not state.lights_polled:
            out.append(Check("Kasa plugs", True, "Connecting"))
        else:
            offline = [p.name or p.host for p in plugs if p.on is None]
            if offline:
                out.append(Check("Kasa plugs", False, f"Offline: {', '.join(offline)}"))
            else:
                out.append(Check("Kasa plugs", True, f"All {len(plugs)} reachable"))

    if state.tv:
        tv, status = state.tv, state.tv_status()
        connecting = tv.paired() and not tv.connected and not tv.error  # still starting up
        out.append(Check("Apple TV", status is None or connecting, status or "Connected"))

    if state.car:
        car = state.car
        if car.error:
            last = f" (last good {hm(car.checked_at)})" if car.checked_at else ""
            out.append(Check("Car (Blue Link)", False, f"{car.error}{last}"))
        elif car.status is None:
            out.append(Check("Car (Blue Link)", True, "Connecting"))
        else:
            st = car.status
            pct = "" if st.battery_pct is None else f"{st.battery_pct:.0f}%, "
            seen = f"car reported {hm(st.reported_at)}, " if st.reported_at else ""
            out.append(Check("Car (Blue Link)", True, f"{pct}{seen}checked {hm(car.checked_at)}"))

    for shot in state.shots:
        name = f"Camera: {shot.label}"
        if shot.error:
            last = f" (last good {hm(shot.at)})" if shot.at else ""
            out.append(Check(name, False, f"{shot.error}{last}"))
        elif shot.at is None:
            out.append(Check(name, True, "Waiting for the first snapshot"))
        else:
            out.append(Check(name, True, f"Snapshot {hms(shot.at)}"))
    return out
