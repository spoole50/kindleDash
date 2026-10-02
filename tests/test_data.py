from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from kindle_dash import queries
from kindle_dash.data import build_snapshot, local_midnight

TZ = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 10, 2, 22, 30, tzinfo=timezone.utc)  # 15:30 local


def rec(result, field=None, dev=None, value=None, t=NOW):
    return {"result": result, "_field": field, "device_type": dev, "_value": value, "_time": t}


def test_latest_values_are_keyed_by_field_and_device():
    snap = build_snapshot(
        [
            rec("latest", "battery_voltage_v", "smartshunt", 53.1),
            rec("latest", "battery_voltage_v", "smartsolar_mppt", 53.3),
            rec("latest", "state_of_charge_percent", "smartshunt", 81.5),
            rec("latest", "time_to_go_s", "smartshunt", 3600.0),
            rec("latest", "pv_power_w", "smartsolar_mppt", 410.0),
            rec("latest", "unrelated_field", "smartshunt", 1.0),
        ],
        [],
        NOW,
        TZ,
        0.48,
    )
    assert snap.latest == {"shunt_v": 53.1, "mppt_v": 53.3, "soc": 81.5, "ttg_s": 3600.0, "pv_w": 410.0}
    assert snap.latest_time == NOW


def test_series_sorted_and_nulls_skipped():
    t0 = NOW - timedelta(minutes=10)
    snap = build_snapshot(
        [
            rec("series", "battery_power_w", "smartshunt", -50.0, NOW),
            rec("series", "battery_power_w", "smartshunt", 120.0, t0),
            rec("series", "battery_power_w", "smartshunt", None, t0),
        ],
        [],
        NOW,
        TZ,
        0.48,
    )
    assert snap.series["power_w"] == [(t0, 120.0), (NOW, -50.0)]


def test_energy_totals_and_cost():
    snap = build_snapshot([rec("solar_wh", value=2500.0), rec("battery_wh", value=1000.0)], [], NOW, TZ, 0.48)
    assert snap.solar_today_cost == 2500 / 1000 * 0.48
    assert snap.battery_today_cost == 0.48
    j = snap.to_json()
    assert j["solar_today_wh"] == 2500.0
    assert round(j["solar_today_usd"], 2) == 1.20


def test_daily_bars_use_local_dates_and_today_is_live():
    midnight = local_midnight(NOW, TZ)
    daily = [
        {"result": "daily_wh", "_time": midnight - timedelta(days=1), "_value": 4000.0},
        {"result": "daily_wh", "_time": midnight, "_value": 900.0},  # cached, out of date
        {"result": "daily_wh", "_time": midnight - timedelta(days=2), "_value": None},  # empty day
    ]
    snap = build_snapshot([rec("solar_wh", value=1500.0)], daily, NOW, TZ, 0.48)
    assert snap.daily_wh == [(date(2026, 9, 30), 0.0), (date(2026, 10, 1), 4000.0), (date(2026, 10, 2), 1500.0)]


def test_local_midnight_respects_tz():
    m = local_midnight(NOW, TZ)
    assert (m.hour, m.minute, m.day) == (0, 0, 2)
    assert m.utcoffset() == timedelta(hours=-7)


def test_queries_match_grafana_dashboard_filters():
    q = "\n".join(queries.live_queries("victron", 24, local_midnight(NOW, TZ)))
    for needle in (
        'from(bucket: "victron")',
        'r["_measurement"] == "victron_energy"',
        '(r["_field"] == "state_of_charge_percent" and r["device_type"] == "smartshunt")',
        '(r["_field"] == "pv_power_w" and r["device_type"] == "smartsolar_mppt")',
        "integral(unit: 1h)",
        "range(start: 2026-10-02T00:00:00-07:00)",
    ):
        assert needle in q
    d = queries.daily_query("victron", "America/Los_Angeles", local_midnight(NOW, TZ))
    assert 'timezone.location(name: "America/Los_Angeles")' in d
    assert "aggregateWindow(every: 1d" in d
