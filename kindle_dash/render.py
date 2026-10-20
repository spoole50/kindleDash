"""Render a Snapshot to an 8-bit grayscale PNG for e-ink.

Every gray is a multiple of 0x11 (the 16 levels the Kindle panel can show),
and nothing relies on colour: charging is a solid dark fill, discharging is
hatched, today's partial bar is hatched.
"""

from __future__ import annotations

import io
import math
import os
from datetime import datetime, timedelta
from functools import lru_cache
from typing import NamedTuple

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from .bluetti import BluettiSnapshot, Station  # noqa: E402
from .car import COMMANDS, CarStatus  # noqa: E402
from .data import Point, Snapshot  # noqa: E402
from .plugs import Plug  # noqa: E402
from .touch import Target  # noqa: E402

BLACK, INK, DARK, MID, LIGHT, FAINT, WHITE = 0x00, 0x22, 0x55, 0x88, 0xBB, 0xDD, 0xFF
HEX = {v: f"#{v:02x}{v:02x}{v:02x}" for v in (BLACK, INK, DARK, MID, LIGHT, FAINT, WHITE)}

W, H = 1236, 1648
M = 36  # outer margin
GAP = 20

_FONT_DIR = os.path.join(matplotlib.get_data_path(), "fonts", "ttf")


@lru_cache(maxsize=None)
def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    return ImageFont.truetype(os.path.join(_FONT_DIR, name), size)


# --------------------------------------------------------------------------- formatting


def fmt_num(v: float | None, decimals: int = 0, signed: bool = False) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "--"
    s = f"{v:+,.{decimals}f}" if signed else f"{v:,.{decimals}f}"
    return s.replace("-", "−")  # true minus sign reads better at size


def fmt_duration(seconds: float | None) -> str:
    if seconds is None or seconds <= 0 or math.isnan(seconds):
        return "--"
    if seconds >= 10 * 86400:
        return "> 10 days"
    mins = int(seconds // 60)
    d, rem = divmod(mins, 1440)
    h, m = divmod(rem, 60)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m"


def fmt_energy(wh: float | None) -> tuple[str, str]:
    if wh is None:
        return "--", "Wh"
    if abs(wh) >= 10_000:
        return fmt_num(wh / 1000, 1), "kWh"
    return fmt_num(wh, 0), "Wh"


def fmt_money(v: float | None) -> str:
    return "--" if v is None else f"${v:,.2f}"


def battery_state(snap: Snapshot) -> tuple[str, str]:
    """(state word, time-left text) for the hero tile."""
    current = snap.get("current_a")
    if current is not None and current > 0.1:
        return "CHARGING", "\u2014 charging"
    if current is not None and abs(current) <= 0.1:
        return "IDLE", fmt_duration(snap.get("ttg_s"))
    return "DISCHARGING", fmt_duration(snap.get("ttg_s"))


def quantize(img: Image.Image) -> Image.Image:
    """Snap every pixel to the nearest multiple of 0x11."""
    lut = [min(255, round(v / 17) * 17) for v in range(256)]
    return img.point(lut)


# --------------------------------------------------------------------------- primitives


def _text(d: ImageDraw.ImageDraw, xy, s, size, bold=False, fill=BLACK, anchor="la"):
    d.text(xy, s, font=font(size, bold), fill=fill, anchor=anchor)
    return d.textlength(s, font=font(size, bold))


def _value_unit(d, x, y, value, unit, vsize, usize, anchor="ls", fill=BLACK):
    """Big value followed by a smaller unit on the same baseline."""
    vw = d.textlength(value, font=font(vsize, True))
    uw = d.textlength(unit, font=font(usize, True)) if unit else 0
    gap = 8 if unit else 0
    if anchor == "rs":
        x -= vw + gap + uw
    d.text((x, y), value, font=font(vsize, True), fill=fill, anchor="ls")
    if unit:
        d.text((x + vw + gap, y), unit, font=font(usize, True), fill=fill, anchor="ls")
    return vw + gap + uw


def _tile(d, box, label):
    d.rounded_rectangle(box, radius=14, outline=BLACK, width=3)
    _text(d, (box[0] + 22, box[1] + 16), label, 28, bold=True, fill=DARK)


def sparkline(d, box, pts: list[Point], since: datetime | None = None, baseline: float | None = None):
    """Polyline in `box` with a dot on the latest value. Draws nothing if <2 points."""
    if since is not None:
        pts = [p for p in pts if p[0] >= since]
    if len(pts) < 2:
        _text(d, ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2), "no data", 22, fill=MID, anchor="mm")
        return
    x0, y0, x1, y1 = box
    t0, t1 = pts[0][0].timestamp(), pts[-1][0].timestamp()
    vals = [v for _, v in pts]
    lo, hi = min(vals), max(vals)
    if baseline is not None:
        lo, hi = min(lo, baseline), max(hi, baseline)
    if hi - lo < 1e-9:
        lo, hi = lo - 1, hi + 1
    span_t = max(t1 - t0, 1)

    def xy(p):
        return (
            x0 + (p[0].timestamp() - t0) / span_t * (x1 - x0),
            y1 - (p[1] - lo) / (hi - lo) * (y1 - y0),
        )

    if baseline is not None:
        by = y1 - (baseline - lo) / (hi - lo) * (y1 - y0)
        for xx in range(int(x0), int(x1), 12):
            d.line([(xx, by), (min(xx + 6, x1), by)], fill=MID, width=2)
    line = [xy(p) for p in pts]
    d.line(line, fill=BLACK, width=4, joint="curve")
    ex, ey = line[-1]
    d.ellipse((ex - 8, ey - 8, ex + 8, ey + 8), fill=BLACK)


# --------------------------------------------------------------------------- matplotlib charts


def _setup_mpl():
    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": 17,
            "font.weight": "bold",
            "axes.edgecolor": HEX[BLACK],
            "axes.linewidth": 2,
            "axes.labelcolor": HEX[BLACK],
            "xtick.color": HEX[BLACK],
            "ytick.color": HEX[BLACK],
            "xtick.major.width": 2,
            "ytick.major.width": 0,
            "xtick.major.size": 6,
            "hatch.linewidth": 2.2,
            "hatch.color": HEX[BLACK],
            "svg.fonttype": "none",
        }
    )


_setup_mpl()


def _fig(w: int, h: int):
    fig = plt.figure(figsize=(w / 100, h / 100), dpi=100, facecolor="white")
    # fixed pixel margins so tick labels never clip, whatever the height
    left, bottom, top, right = 96, 40, 8, 36  # right: room for a tick label at the edge
    ax = fig.add_axes((left / w, bottom / h, 1 - (left + right) / w, 1 - (bottom + top) / h))
    ax.set_facecolor("white")
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.grid(axis="y", color=HEX[FAINT], linewidth=1.5)
    ax.set_axisbelow(True)
    return fig, ax


def _to_image(fig) -> Image.Image:
    fig.canvas.draw()
    arr = np.asarray(fig.canvas.buffer_rgba())
    plt.close(fig)
    return Image.fromarray(arr[:, :, :3]).convert("L")


def _with_gaps(pts: list[Point], max_gap: timedelta) -> tuple[list[datetime], np.ndarray]:
    """Insert NaN where samples are missing so lines don't bridge outages."""
    xs: list[datetime] = []
    ys: list[float] = []
    for i, (t, v) in enumerate(pts):
        if i and t - pts[i - 1][0] > max_gap:
            xs.append(pts[i - 1][0] + (t - pts[i - 1][0]) / 2)
            ys.append(float("nan"))
        xs.append(t)
        ys.append(v)
    return xs, np.array(ys, dtype=float)


def _time_axis(ax, tz, start: datetime, end: datetime):
    ax.set_xlim(start, end)
    ax.xaxis.set_major_locator(mdates.HourLocator(byhour=range(0, 24, 6), tz=tz))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=tz))
    ax.tick_params(axis="y", pad=2)
    ax.yaxis.set_major_locator(plt.MaxNLocator(4))


def _no_data(ax):
    ax.text(0.5, 0.5, "no data", transform=ax.transAxes, ha="center", va="center", color=HEX[MID], fontsize=22)


def power_chart(pts: list[Point], tz, start, end, w, h) -> Image.Image:
    fig, ax = _fig(w, h)
    _time_axis(ax, tz, start, end)
    if pts:
        xs, ys = _with_gaps(pts, timedelta(minutes=20))
        pos = np.where(ys > 0, ys, 0.0)
        neg = np.where(ys < 0, ys, 0.0)
        ax.fill_between(xs, 0, pos, facecolor=HEX[DARK], linewidth=0, step=None)
        ax.fill_between(xs, 0, neg, facecolor="white", edgecolor=HEX[BLACK], hatch="///", linewidth=0)
        ax.plot(xs, ys, color=HEX[BLACK], linewidth=2.2)
        lo, hi = np.nanmin(ys), np.nanmax(ys)
        pad = max(hi - lo, 50) * 0.08
        ax.set_ylim(min(lo, 0) - pad, max(hi, 0) + pad)
    else:
        _no_data(ax)
    ax.axhline(0, color=HEX[BLACK], linewidth=2.5)
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: fmt_num(v, 0)))
    return _to_image(fig)


def solar_chart(pts: list[Point], tz, start, end, w, h) -> Image.Image:
    fig, ax = _fig(w, h)
    _time_axis(ax, tz, start, end)
    if pts:
        xs, ys = _with_gaps(pts, timedelta(minutes=20))
        ax.fill_between(xs, 0, ys, facecolor=HEX[LIGHT], linewidth=0)
        ax.plot(xs, ys, color=HEX[BLACK], linewidth=2.2)
        ax.set_ylim(0, max(np.nanmax(ys) * 1.1, 50))
    else:
        _no_data(ax)
        ax.set_ylim(0, 100)
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: fmt_num(v, 0)))
    return _to_image(fig)


def daily_chart(days, today, w, h) -> Image.Image:
    fig = plt.figure(figsize=(w / 100, h / 100), dpi=100, facecolor="white")
    ax = fig.add_axes((0.02, 44 / h, 0.96, 1 - 50 / h))
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.set_yticks([])
    if not days:
        _no_data(ax)
        return _to_image(fig)
    labels = [d.strftime("%a") if d != today else "Today" for d, _ in days]
    vals = [v / 1000 for _, v in days]
    x = np.arange(len(days))
    is_today = [d == today for d, _ in days]
    for xi, v, t in zip(x, vals, is_today):
        if t:  # today is still accumulating: outlined + hatched
            ax.bar(xi, v, width=0.72, color="white", edgecolor=HEX[BLACK], hatch="///", linewidth=2.5)
        else:
            ax.bar(xi, v, width=0.72, color=HEX[DARK], linewidth=0)
    top = max(max(vals), 0.1)
    ax.set_ylim(0, top * 1.22)
    for xi, v in zip(x, vals):
        ax.text(xi, v + top * 0.03, f"{v:.2f}", ha="center", va="bottom", fontsize=17, fontweight="bold")
    ax.set_xticks(x, labels)
    ax.tick_params(axis="x", length=0, pad=8)
    return _to_image(fig)


# --------------------------------------------------------------------------- sections
# Each section draws into a box (x0, y0, x1, y1), so both layouts share them.


class KindleBattery(NamedTuple):
    """The display's own battery, as reported by the Kindle client."""

    level: int  # percent
    charging: bool


LOW_KINDLE_BATTERY = 20


class _Ctx:
    def __init__(self, img, snap: Snapshot, tz, chart_hours: int, spark_hours: int, kindle: KindleBattery | None = None):
        self.img, self.d, self.snap, self.tz = img, ImageDraw.Draw(img), snap, tz
        self.kindle = kindle
        self.chart_hours, self.spark_hours = chart_hours, spark_hours
        self.now = snap.fetched_at.astimezone(tz)
        self.spark_since = snap.fetched_at - timedelta(hours=spark_hours)
        self.end = snap.fetched_at
        self.start = self.end - timedelta(hours=chart_hours)


# Pages, in tab order: (name used in URLs and taps, tab label)
PAGES = (("solar", "SOLAR"), ("bluetti", "BLUETTI"), ("car", "CAR"), ("lights", "PLUGS"), ("tv", "TV"), ("cameras", "CAMERA"))  # "lights" stays the URL name; Kindles remember it
HIDDEN_PAGES = ("health",)  # no tab: opened by tapping the clock
HEADER_RULE_Y = 126  # the rule under the header; tabs sit above it
TAB_SIZE, TAB_PAD, TAB_GAP = 29, 14, 12  # six tabs, clear of the Kindle battery, clock and date in portrait
CLOCK_W = 200  # the date, or the issues badge over it, at the top right; the Kindle's battery sits left of the time above


def tab_boxes() -> list[tuple[tuple[int, int, int, int], str]]:
    """Each tab's drawn box and its tap action ("page:<name>"); the same in both layouts."""
    out, x = [], M
    for name, label in PAGES:
        w = round(font(TAB_SIZE, True).getlength(label)) + 2 * TAB_PAD
        out.append(((x, 30, x + w, 114), f"page:{name}"))
        x += w + TAB_GAP
    return out


def clock_target(layout: str) -> Target:
    """The clock and date: opens the health page."""
    w = size(layout)[0]
    return Target((w - M - CLOCK_W - 40, 0, w, HEADER_RULE_Y + 4), (w - M - CLOCK_W, 26, w - M, 116), "page:health")


def tab_targets() -> list[Target]:
    """Tap areas for the tabs: the full header height, split halfway across the gaps."""
    return [
        Target((x0 - (TAB_GAP // 2 if i else M), 0, x1 + TAB_GAP // 2, HEADER_RULE_Y + 4), (x0, y0, x1, y1), action)
        for i, ((x0, y0, x1, y1), action) in enumerate(tab_boxes())
    ]


TABS_RIGHT = tab_boxes()[-1][0][2]


def header(img: Image.Image, page: str, now: datetime, kindle: KindleBattery | None) -> int:
    """Page tabs, timestamp, date and rule across the top; returns the y below it."""
    d, w = ImageDraw.Draw(img), img.width
    for (x0, y0, x1, y1), action in tab_boxes():
        name = action.split(":", 1)[1]
        label = dict(PAGES)[name]
        active = name == page
        d.rounded_rectangle((x0, y0, x1, y1), radius=14, fill=BLACK if active else WHITE, outline=BLACK, width=3)
        _text(d, ((x0 + x1) / 2, (y0 + y1) / 2), label, TAB_SIZE, bold=True, fill=WHITE if active else BLACK, anchor="mm")
    y = 26
    clock_w = _text(d, (w - M, y + 44), f"{now:%H:%M}", 44, bold=True, anchor="rs")
    _text(d, (w - M, y + 80), f"{now:%a %-d %b}", 28, fill=DARK, anchor="rs")
    if kindle:
        _kindle_battery(d, w - M - clock_w - 20, y + 40, kindle)
    d.rectangle((M, HEADER_RULE_Y, w - M, HEADER_RULE_Y + 4), fill=BLACK)
    return HEADER_RULE_Y + 4 + GAP


def _header(c: _Ctx) -> int:
    return header(c.img, "solar", c.now, c.kindle)


def _kindle_battery(d: ImageDraw.ImageDraw, right: float, baseline: float, kb: KindleBattery) -> None:
    """'[icon] 85%' ending at `right`; bold black when low and not charging."""
    low = kb.level < LOW_KINDLE_BATTERY and not kb.charging
    fill, size = (BLACK, 28) if low else (DARK, 24)
    pct = f"{kb.level}%"
    tw = d.textlength(pct, font=font(size, low))
    _text(d, (right, baseline), pct, size, bold=low, fill=fill, anchor="rs")
    # icon: body + nub, filled to the level; a bolt while charging
    bw, bh = 44, 22
    x1 = right - tw - 10 - 4
    x0, y1 = x1 - bw, baseline - 1
    y0 = y1 - bh
    d.rectangle((x1, y0 + 6, x1 + 4, y1 - 6), fill=fill)
    d.rectangle((x0, y0, x1, y1), outline=fill, width=3)
    inner = bw - 10
    fw = round(inner * max(0, min(kb.level, 100)) / 100)
    if fw:
        d.rectangle((x0 + 5, y0 + 5, x0 + 5 + fw, y1 - 5), fill=fill)
    if kb.charging:
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        bolt = [(cx + 3, y0 - 3), (cx - 9, cy + 2), (cx - 1, cy + 2), (cx - 4, y1 + 3), (cx + 9, cy - 2), (cx + 1, cy - 2)]
        d.polygon(bolt, fill=WHITE, outline=BLACK, width=2)


def _footer(c: _Ctx) -> None:
    """Right-aligned; the left side stays empty for the Kindle client's error marker."""
    w, h = c.img.size
    data_at = c.snap.latest_time.astimezone(c.tz).strftime("%H:%M:%S") if c.snap.latest_time else "--"
    _text(c.d, (w - M, h - 16), f"Victron via InfluxDB  \u00b7  newest sample {data_at}", 22, fill=DARK, anchor="rs")


def _battery_tile(c: _Ctx, box) -> None:
    d, soc = c.d, c.snap.get("soc")
    _, left = battery_state(c.snap)
    _tile(d, box, "BATTERY")
    _value_unit(d, box[0] + 22, box[1] + 150, fmt_num(soc, 1), "%", 120, 60)
    bx0, by0, bx1, by1 = box[0] + 22, box[1] + 168, box[2] - 40, box[1] + 200
    d.rectangle((bx0, by0, bx1, by1), outline=BLACK, width=3)
    d.rectangle((bx1, by0 + 9, bx1 + 10, by1 - 9), fill=BLACK)
    if soc is not None:
        fill_w = (bx1 - bx0 - 10) * max(0.0, min(soc, 100.0)) / 100
        d.rectangle((bx0 + 5, by0 + 5, bx0 + 5 + fill_w, by1 - 5), fill=INK)
    _text(d, (box[0] + 22, box[3] - 18), "Time left", 30, fill=DARK, anchor="ls")
    _text(d, (box[2] - 22, box[3] - 18), left, 34, bold=True, anchor="rs")


def _net_power_tile(c: _Ctx, box) -> None:
    d = c.d
    state, _ = battery_state(c.snap)
    _tile(d, box, "NET POWER")
    _value_unit(d, box[0] + 22, box[1] + 140, fmt_num(c.snap.get("power_w"), 0, signed=True), "W", 92, 48)
    arrow = {"CHARGING": "\u25b2", "DISCHARGING": "\u25bc"}.get(state, "\u25cf")
    _text(d, (box[2] - 22, box[1] + 16), f"{arrow} {state}", 28, bold=True, anchor="ra")
    sparkline(d, (box[0] + 22, box[1] + 168, box[2] - 30, box[3] - 46), c.snap.series.get("soc", []), c.spark_since)
    _text(d, (box[0] + 22, box[3] - 14), f"SoC, last {c.spark_hours} h", 24, fill=DARK, anchor="ls")


def _stat_tiles(c: _Ctx, box) -> None:
    """Voltage, battery current and solar input side by side."""
    d, snap = c.d, c.snap
    x0, y0, x1, y1 = box
    tw = (x1 - x0 - 2 * GAP) // 3
    vs, us = (72, 40) if tw >= 360 else (62, 34)  # narrower tiles in landscape
    b0, b1, b2 = [(x0 + i * (tw + GAP), y0, x0 + i * (tw + GAP) + tw, y1) for i in range(3)]

    _tile(d, b0, "VOLTAGE")
    for row, (lab, key) in enumerate((("Shunt", "shunt_v"), ("MPPT", "mppt_v"))):
        by = b0[1] + 96 + row * 54
        _text(d, (b0[0] + 22, by), lab, 30, fill=DARK, anchor="ls")
        _value_unit(d, b0[2] - 22, by, fmt_num(snap.get(key), 1), "V", 50, 30, anchor="rs")
    sparkline(d, (b0[0] + 22, b0[3] - 42, b0[2] - 30, b0[3] - 16), snap.series.get("shunt_v", []), c.spark_since)

    for b, label, key, val, unit in (
        (b1, "BATTERY CURRENT", "current_a", fmt_num(snap.get("current_a"), 1, signed=True), "A"),
        (b2, "SOLAR INPUT", "pv_w", fmt_num(snap.get("pv_w"), 0), "W"),
    ):
        _tile(d, b, label)
        _value_unit(d, b[0] + 22, b[1] + 124, val, unit, vs, us)
        sparkline(d, (b[0] + 22, b[1] + 146, b[2] - 30, b[3] - 18), snap.series.get(key, []), c.spark_since, baseline=0)


def _power_chart_section(c: _Ctx, box) -> None:
    d, (x0, y0, x1, y1) = c.d, box
    _text(d, (x0, y0 + 30), f"BATTERY POWER (W), {c.chart_hours} h", 30, bold=True, anchor="ls")
    # key: solid = charge, hatched = discharge
    kx = x1
    kx -= _text(d, (kx, y0 + 30), "Discharge", 26, bold=True, anchor="rs") + 12
    _hatch_swatch(c.img, (kx - 34, y0 + 6, kx, y0 + 32))
    kx -= 34 + 30
    kx -= _text(d, (kx, y0 + 30), "Charge", 26, bold=True, anchor="rs") + 12
    d.rectangle((kx - 34, y0 + 6, kx, y0 + 32), fill=DARK)
    chart = power_chart(c.snap.series.get("power_w", []), c.tz, c.start, c.end, x1 - x0, y1 - y0 - 44)
    c.img.paste(chart, (x0, y0 + 44))


def _solar_chart_section(c: _Ctx, box) -> None:
    x0, y0, x1, y1 = box
    _text(c.d, (x0, y0 + 30), f"MPPT SOLAR INPUT (W), {c.chart_hours} h", 30, bold=True, anchor="ls")
    chart = solar_chart(c.snap.series.get("pv_w", []), c.tz, c.start, c.end, x1 - x0, y1 - y0 - 44)
    c.img.paste(chart, (x0, y0 + 44))


def _today_tiles(c: _Ctx, box) -> None:
    """Solar today and battery used today, stacked."""
    snap, (x0, y0, x1, y1) = c.snap, box
    th = (y1 - y0 - GAP) // 2
    for i, (label, wh, cost) in enumerate(
        (
            ("SOLAR TODAY", snap.solar_today_wh, snap.solar_today_cost),
            ("BATTERY USED", snap.battery_today_wh, snap.battery_today_cost),
        )
    ):
        b = (x0, y0 + i * (th + GAP), x1, y0 + i * (th + GAP) + th)
        _tile(c.d, b, label)
        v, u = fmt_energy(wh)
        tall = th >= 200  # landscape: bigger numbers instead of a gap
        _value_unit(c.d, b[0] + 22, b[1] + (128 if tall else 108), v, u, 76 if tall else 60, 38 if tall else 32)
        _text(c.d, (b[0] + 22, b[3] - 20), fmt_money(cost), 60 if tall else 44, bold=True, anchor="ls")
        _text(c.d, (b[2] - 22, b[3] - 22), f"@ ${snap.price_per_kwh:.2f}/kWh", 22, fill=DARK, anchor="rs")


def _daily_section(c: _Ctx, box) -> None:
    x0, y0, x1, y1 = box
    _text(c.d, (x0, y0 + 30), "PRODUCTION PER DAY (kWh)", 30, bold=True, anchor="ls")
    c.img.paste(daily_chart(c.snap.daily_wh, c.now.date(), x1 - x0, y1 - y0 - 44), (x0, y0 + 44))


# --------------------------------------------------------------------------- layouts

LAYOUTS = ("portrait", "landscape")


def size(layout: str) -> tuple[int, int]:
    return (H, W) if layout == "landscape" else (W, H)


def render(
    snap: Snapshot, tz, chart_hours: int = 24, spark_hours: int = 6, layout: str = "portrait", kindle: KindleBattery | None = None
) -> Image.Image:
    if layout == "landscape":
        return render_landscape(snap, tz, chart_hours, spark_hours, kindle)
    return render_portrait(snap, tz, chart_hours, spark_hours, kindle)


def render_portrait(snap: Snapshot, tz, chart_hours: int = 24, spark_hours: int = 6, kindle: KindleBattery | None = None) -> Image.Image:
    """1236 x 1648: hero row, stat tiles, two 24 h charts, today + per-day bars."""
    c = _Ctx(Image.new("L", (W, H), WHITE), snap, tz, chart_hours, spark_hours, kindle)
    y = _header(c)
    half = (W - 2 * M - GAP) // 2
    _battery_tile(c, (M, y, M + half, y + 256))
    _net_power_tile(c, (M + half + GAP, y, W - M, y + 256))
    y += 256 + GAP
    _stat_tiles(c, (M, y, W - M, y + 214))
    y += 214 + GAP
    _power_chart_section(c, (M, y, W - M, y + 300))
    y += 300 + GAP
    _solar_chart_section(c, (M, y, W - M, y + 214))
    y += 214 + GAP
    bottom, lw = H - 50, 380
    _today_tiles(c, (M, y, M + lw, bottom))
    _daily_section(c, (M + lw + GAP, y, W - M, bottom))
    _footer(c)
    return quantize(c.img)


def render_landscape(snap: Snapshot, tz, chart_hours: int = 24, spark_hours: int = 6, kindle: KindleBattery | None = None) -> Image.Image:
    """1648 x 1236: battery + today down the left, stats and charts on the right."""
    w, h = H, W
    c = _Ctx(Image.new("L", (w, h), WHITE), snap, tz, chart_hours, spark_hours, kindle)
    y0 = _header(c)
    bottom, lw = h - 50, 520
    rx = M + lw + GAP

    # left column: battery, net power, today's totals
    y = y0
    _battery_tile(c, (M, y, M + lw, y + 250))
    y += 250 + GAP
    _net_power_tile(c, (M, y, M + lw, y + 250))
    y += 250 + GAP
    _today_tiles(c, (M, y, M + lw, bottom))

    # right column: stat tiles, charts, per-day bars
    y = y0
    _stat_tiles(c, (rx, y, w - M, y + 214))
    y += 214 + GAP
    rest = bottom - y - 2 * GAP
    ph, sh = int(rest * 0.37), int(rest * 0.29)
    _power_chart_section(c, (rx, y, w - M, y + ph))
    y += ph + GAP
    _solar_chart_section(c, (rx, y, w - M, y + sh))
    y += sh + GAP
    _daily_section(c, (rx, y, w - M, bottom))
    _footer(c)
    return quantize(c.img)


# --------------------------------------------------------------------------- lights page


def plug_boxes(layout: str, n: int) -> list[tuple[int, int, int, int]]:
    """One big tile per plug: 2 columns in portrait, 3 in landscape."""
    w, h = size(layout)
    cols = 3 if layout == "landscape" else 2
    rows = max(1, math.ceil(n / cols))
    top, bottom = HEADER_RULE_Y + 4 + GAP, h - 50
    tw = (w - 2 * M - (cols - 1) * GAP) // cols
    th = min(400, (bottom - top - (rows - 1) * GAP) // rows)
    return [
        (M + (i % cols) * (tw + GAP), top + (i // cols) * (th + GAP), M + (i % cols) * (tw + GAP) + tw, top + (i // cols) * (th + GAP) + th)
        for i in range(n)
    ]


def targets(
    page: str, layout: str, plugs: list[Plug], tv_apps: tuple[tuple[str, str], ...] = (), n_cameras: int = 0, car: CarStatus | None = None
) -> list[Target]:
    """Everything tappable on a page: the tabs and clock, plus a tile per plug, the TV remote's buttons, the camera stills or the car's controls."""
    out = tab_targets() + [clock_target(layout)]
    if page == "cameras":
        out += [Target(box, box, "camera:refresh") for box in camera_boxes(layout, n_cameras)]
    elif page == "lights":
        out += [Target(box, box, f"plug:{p.host}") for box, p in zip(plug_boxes(layout, len(plugs)), plugs)]
    elif page == "tv":
        out += [Target(box, box, f"atv:{action}") for box, action, _ in tv_buttons(layout, tv_apps)]
    elif page == "car":
        out += [Target(box, box, action) for box, action in car_controls(layout, car).values()]
    return out


def _fit(d: ImageDraw.ImageDraw, s: str, size_: int, width: float) -> str:
    """`s`, cut with an ellipsis to fit `width` at bold `size_`."""
    f = font(size_, True)
    if d.textlength(s, font=f) <= width:
        return s
    while s and d.textlength(s + "…", font=f) > width:
        s = s[:-1]
    return s.rstrip() + "…"


def _wrap(d: ImageDraw.ImageDraw, s: str, size_: int, width: float, max_lines: int = 2) -> list[str]:
    """`s` broken at spaces into at most `max_lines` lines of bold `size_`; the last is cut with an ellipsis if needed."""
    f = font(size_, True)
    lines: list[str] = []
    words = s.split()
    while words and len(lines) < max_lines:
        line = words.pop(0)
        while words and d.textlength(f"{line} {words[0]}", font=f) <= width:
            line += " " + words.pop(0)
        lines.append(line)
    if words:
        lines[-1] = _fit(d, f"{lines[-1]} {' '.join(words)}", size_, width)
    return [_fit(d, line, size_, width) for line in lines] or [""]


def fmt_watts(w: float | None) -> str:
    if w is None:
        return "--"
    return f"{w / 1000:.2f} kW" if w >= 1000 else f"{w:.0f} W"


def fmt_kwh(v: float) -> str:
    return f"{v:.2f} kWh" if v < 10 else f"{v:,.1f} kWh"


def _plug_tile(d: ImageDraw.ImageDraw, box, plug: Plug, confirm: bool = False) -> None:
    """ON is solid black, OFF is outlined, unreachable is pale gray.

    Top: what it powers, then the name (two lines). Bottom: energy today and
    over the last 30 days, then ON/OFF and the power now. While
    `confirm`, the bottom asks for a second tap instead.
    """
    x0, y0, x1, y1 = box
    on = plug.on is True
    if plug.on is None:
        bg, fg, sub = FAINT, DARK, DARK
    elif confirm:
        bg, fg, sub = WHITE, BLACK, DARK
    else:
        bg, fg, sub = (BLACK, WHITE, LIGHT) if on else (WHITE, BLACK, DARK)
    d.rounded_rectangle(box, radius=18, fill=bg, outline=BLACK, width=12 if confirm else 4)
    y = y0 + 24
    if plug.kind:
        _text(d, (x0 + 28, y), plug.kind.upper(), 24, bold=True, fill=sub)
        y += 36
    for line in _wrap(d, plug.name, 40, x1 - x0 - 56):
        _text(d, (x0 + 28, y), line, 40, bold=True, fill=fg)
        y += 48
    if plug.on is None:
        _text(d, (x0 + 28, y1 - 30), "OFFLINE", 60, bold=True, fill=fg, anchor="ls")
        return
    if confirm:
        _text(d, (x0 + 28, y1 - 104), "TAP AGAIN TO", 36, bold=True, fill=fg, anchor="ls")
        _text(d, (x0 + 28, y1 - 30), f"TURN {'OFF' if on else 'ON'}", 72, bold=True, fill=fg, anchor="ls")
        return
    # energy over the state line, so long figures never meet the big ON/OFF
    for i, (v, when) in enumerate(((plug.today_kwh, "today"), (plug.last30_kwh, "last 30 days"))):
        if v is not None:
            _text(d, (x0 + 28, y1 - 150 + i * 34), f"{fmt_kwh(v)} {when}", 26, fill=sub, anchor="ls")
    _text(d, (x0 + 28, y1 - 30), "ON" if on else "OFF", 84, bold=True, fill=fg, anchor="ls")
    if plug.power_w is not None:
        _text(d, (x1 - 28, y1 - 30), fmt_watts(plug.power_w), 56, bold=True, fill=fg, anchor="rs")


def render_lights(
    plugs: list[Plug], now: datetime, tz, layout: str = "portrait", kindle: KindleBattery | None = None, confirm: str | None = None
) -> Image.Image:
    """A tile per plug; the plug whose host is `confirm` asks for a second tap."""
    img = Image.new("L", size(layout), WHITE)
    w, h = img.size
    header(img, "lights", now.astimezone(tz), kindle)
    d = ImageDraw.Draw(img)
    if not plugs:
        _text(d, (w / 2, h / 2), "No plugs configured (KASA_HOSTS)", 40, bold=True, anchor="mm")
    for box, plug in zip(plug_boxes(layout, len(plugs)), plugs):
        _plug_tile(d, box, plug, plug.host == confirm)
    reachable = sum(p.on is not None for p in plugs)
    total = sum(p.power_w for p in plugs if p.on is not None and p.power_w is not None)
    _text(d, (w - M, h - 16), f"Kasa plugs  \u00b7  {fmt_watts(total)} total  \u00b7  {reachable} of {len(plugs)} reachable", 22, fill=DARK, anchor="rs")
    return quantize(img)


# --------------------------------------------------------------------------- TV page

TV_PAD = (("menu", "BACK"), ("up", ""), ("home", "HOME"), ("left", ""), ("select", "OK"), ("right", ""),
          ("volume_down", "VOL \u2212"), ("down", ""), ("volume_up", "VOL +"))  # fmt: skip
TV_TRANSPORT = (("skip_backward", ""), ("play_pause", ""), ("skip_forward", ""), ("power", "POWER"))
MAX_TV_APPS = 4


def _grid(box, cols: int, rows: int, n: int | None = None) -> list[tuple[int, int, int, int]]:
    x0, y0, x1, y1 = box
    cw, ch = (x1 - x0 - (cols - 1) * GAP) // cols, (y1 - y0 - (rows - 1) * GAP) // rows
    cells = [(x0 + c * (cw + GAP), y0 + r * (ch + GAP), x0 + c * (cw + GAP) + cw, y0 + r * (ch + GAP) + ch) for r in range(rows) for c in range(cols)]
    return cells[: len(cells) if n is None else n]


def _tv_regions(layout: str) -> dict[str, tuple[int, int, int, int]]:
    """Now playing, the 3x3 pad, transport and app rows."""
    top = HEADER_RULE_Y + 4 + GAP
    if layout == "landscape":
        w, h = size(layout)
        rx = M + 840
        return {
            "playing": (rx, top, w - M, top + 250),
            "pad": (M, top, rx - GAP, h - 50),
            "transport": (rx, top + 270, w - M, top + 270 + 320),
            "apps": (rx, top + 610, w - M, h - 50),
        }
    w = W
    return {
        "playing": (M, top, w - M, top + 230),
        "pad": (M, top + 250, w - M, top + 250 + 700),
        "transport": (M, top + 970, w - M, top + 970 + 180),
        "apps": (M, top + 1170, w - M, top + 1170 + 180),
    }


def tv_buttons(layout: str, apps: tuple[tuple[str, str], ...] = ()) -> list[tuple[tuple[int, int, int, int], str, str]]:
    """(box, action, label) for every remote button; actions are AppleTV.command() names."""
    r = _tv_regions(layout)
    out = [(b, a, lab) for b, (a, lab) in zip(_grid(r["pad"], 3, 3), TV_PAD)]
    cols, rows = (2, 2) if layout == "landscape" else (4, 1)
    out += [(b, a, lab) for b, (a, lab) in zip(_grid(r["transport"], cols, rows), TV_TRANSPORT)]
    shown = apps[:MAX_TV_APPS]
    out += [(b, f"app:{bundle}", name) for b, (name, bundle) in zip(_grid(r["apps"], cols, rows, len(shown)), shown)]
    return out


def _tri(d, cx, cy, s, direction, fill):
    """A solid triangle of size `s` pointing up/down/left/right."""
    pts = {
        "up": [(cx, cy - s), (cx - s, cy + s * 0.7), (cx + s, cy + s * 0.7)],
        "down": [(cx, cy + s), (cx - s, cy - s * 0.7), (cx + s, cy - s * 0.7)],
        "left": [(cx - s, cy), (cx + s * 0.7, cy - s), (cx + s * 0.7, cy + s)],
        "right": [(cx + s, cy), (cx - s * 0.7, cy - s), (cx - s * 0.7, cy + s)],
    }[direction]
    d.polygon(pts, fill=fill)


def _tv_icon(d, action: str, cx: float, cy: float, fill) -> bool:
    """Draw the icon for `action` centred at (cx, cy); False if it has none (use the label)."""
    if action in ("up", "down", "left", "right"):
        _tri(d, cx, cy, 44, action, fill)
    elif action == "play_pause":
        _tri(d, cx - 30, cy, 30, "right", fill)
        d.rectangle((cx + 12, cy - 30, cx + 24, cy + 30), fill=fill)
        d.rectangle((cx + 34, cy - 30, cx + 46, cy + 30), fill=fill)
    elif action in ("skip_backward", "skip_forward"):
        way = "left" if action == "skip_backward" else "right"
        _tri(d, cx - 22, cy, 28, way, fill)
        _tri(d, cx + 22, cy, 28, way, fill)
    else:
        return False
    return True


def _tv_playing(d, box, playing, status: str | None) -> None:
    x0, y0, x1, y1 = box
    d.rounded_rectangle(box, radius=14, outline=BLACK, width=3)
    if status:  # not paired / not connected
        _text(d, ((x0 + x1) / 2, (y0 + y1) / 2), _fit(d, status, 34, x1 - x0 - 56), 34, bold=True, fill=DARK, anchor="mm")
        return
    if playing is None or not (playing.title or playing.app):
        _text(d, (x0 + 28, y0 + 24), "NOW PLAYING", 24, bold=True, fill=DARK)
        _text(d, (x0 + 28, y0 + 70), "Nothing playing", 44, bold=True, fill=MID)
        return
    _text(d, (x0 + 28, y0 + 24), _fit(d, (playing.app or "Now playing").upper(), 24, (x1 - x0) / 2), 24, bold=True, fill=DARK)
    if playing.state:
        _text(d, (x1 - 28, y0 + 24), playing.state.upper(), 24, bold=True, anchor="ra")
    _text(d, (x0 + 28, y0 + 64), _fit(d, playing.title or "\u2014", 44, x1 - x0 - 56), 44, bold=True)
    if playing.subtitle:
        _text(d, (x0 + 28, y0 + 120), _fit(d, playing.subtitle, 30, x1 - x0 - 56), 30, fill=DARK)
    if playing.total:
        bx0, by0, bx1, by1 = x0 + 28, y1 - 46, x1 - 210, y1 - 26
        d.rectangle((bx0, by0, bx1, by1), outline=BLACK, width=2)
        frac = max(0.0, min(1.0, (playing.position or 0) / playing.total))
        if frac:
            d.rectangle((bx0 + 3, by0 + 3, bx0 + 3 + (bx1 - bx0 - 6) * frac, by1 - 3), fill=INK)
        clock = lambda s: f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"  # noqa: E731
        _text(d, (x1 - 28, y1 - 24), f"{clock(playing.position or 0)} / {clock(playing.total)}", 24, fill=DARK, anchor="rs")


def render_tv(
    playing,
    status: str | None,
    power_on: bool | None,
    apps: tuple[tuple[str, str], ...],
    now: datetime,
    tz,
    layout: str = "portrait",
    kindle: KindleBattery | None = None,
) -> Image.Image:
    """The remote: now playing, a 3x3 pad, transport + power, and app shortcuts.

    `status` replaces now playing with a message (not paired, not connected).
    """
    img = Image.new("L", size(layout), WHITE)
    w, h = img.size
    header(img, "tv", now.astimezone(tz), kindle)
    d = ImageDraw.Draw(img)
    _tv_playing(d, _tv_regions(layout)["playing"], playing, status)
    for box, action, label in tv_buttons(layout, apps):
        x0, y0, x1, y1 = box
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        lit = action == "power" and power_on  # the power key is solid while the TV is on
        d.rounded_rectangle(box, radius=16, fill=BLACK if lit else WHITE, outline=BLACK, width=4)
        fg = WHITE if lit else BLACK
        if action == "power":
            _text(d, (cx, cy - 16), "POWER", 36, bold=True, fill=fg, anchor="mm")
            state = "unknown" if power_on is None else ("on" if power_on else "off")
            _text(d, (cx, cy + 26), state, 26, fill=LIGHT if lit else DARK, anchor="mm")
        elif not _tv_icon(d, action, cx, cy, fg):
            _text(d, (cx, cy), _fit(d, label, 40, x1 - x0 - 24), 40, bold=True, fill=fg, anchor="mm")
    if not apps:
        x0, y0, x1, y1 = _tv_regions(layout)["apps"]
        _text(d, ((x0 + x1) / 2, (y0 + y1) / 2), "Add app shortcuts with ATV_APPS", 26, fill=MID, anchor="mm")
    _text(d, (w - M, h - 16), "Apple TV", 22, fill=DARK, anchor="rs")
    return quantize(img)


# --------------------------------------------------------------------------- Bluetti page


def bluetti_power_chart(pts_in: list[Point], pts_out: list[Point], tz, start, end, w, h) -> Image.Image:
    """Input as a pale filled area, output as a heavy black line."""
    fig, ax = _fig(w, h)
    _time_axis(ax, tz, start, end)
    top = 0.0
    if pts_in:
        xs, ys = _with_gaps(pts_in, timedelta(minutes=20))
        ax.fill_between(xs, 0, ys, facecolor=HEX[LIGHT], edgecolor=HEX[DARK], linewidth=1.5)
        top = max(top, float(np.nanmax(ys)))
    if pts_out:
        xs, ys = _with_gaps(pts_out, timedelta(minutes=20))
        ax.plot(xs, ys, color=HEX[BLACK], linewidth=3)
        top = max(top, float(np.nanmax(ys)))
    if not pts_in and not pts_out:
        _no_data(ax)
    ax.set_ylim(0, max(top * 1.1, 50))
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: fmt_num(v, 0)))
    return _to_image(fig)


def percent_chart(pts: list[Point], tz, start, end, w, h) -> Image.Image:
    fig, ax = _fig(w, h)
    _time_axis(ax, tz, start, end)
    if pts:
        xs, ys = _with_gaps(pts, timedelta(minutes=20))
        ax.fill_between(xs, 0, ys, facecolor=HEX[FAINT], linewidth=0)
        ax.plot(xs, ys, color=HEX[BLACK], linewidth=2.5)
    else:
        _no_data(ax)
    ax.set_ylim(0, 100)
    ax.yaxis.set_major_locator(plt.FixedLocator([0, 50, 100]))
    return _to_image(fig)


def _shrink(d: ImageDraw.ImageDraw, parts, width: float, bold: bool = True) -> float:
    """The scale (<= 1) at which text `parts` [(s, size)] fit in `width`."""
    total = sum(d.textlength(s, font=font(size, bold)) for s, size in parts) + 8 * (len(parts) - 1)
    return min(1.0, width / total) if total else 1.0


def _fitted_value(d, x, y, value, unit, vsize, usize, width, anchor="ls", fill=BLACK) -> float:
    """_value_unit, scaled down until it fits in `width`."""
    k = _shrink(d, ((value, vsize), (unit, usize)), width)
    return _value_unit(d, x, y, value, unit, int(vsize * k), int(usize * k), anchor, fill)


def _fitted_text(d, xy, s, size, width, bold=False, fill=BLACK, anchor="la") -> float:
    return _text(d, xy, s, int(size * _shrink(d, ((s, size),), width, bold)), bold, fill, anchor)


def _watts_parts(w: float | None) -> tuple[str, str]:
    """('1.84', 'kW') or ('352', 'W') for a big number and its unit."""
    if w is None:
        return "--", "W"
    return (f"{w / 1000:.2f}", "kW") if w >= 1000 else (f"{w:.0f}", "W")


def _bluetti_card(d: ImageDraw.ImageDraw, box, st: Station, now: datetime, tz) -> None:
    """Name and state, battery %, time to empty or full, power in and out, and today's energy.

    Narrow boxes (portrait) put IN and OUT side by side under the battery; wide
    ones (landscape) stack them beside it. Every number shrinks to fit its space.
    """
    x0, y0, x1, y1 = box
    wide = x1 - x0 >= 700
    stale = st.stale(now)
    d.rounded_rectangle(box, radius=14, outline=BLACK, width=3)
    _text(d, (x0 + 22, y0 + 16), st.name, 34, bold=True)
    if stale:
        msg = f"NO DATA SINCE {st.latest_time.astimezone(tz):%H:%M}" if st.latest_time else "NO DATA"
        f = font(24, True)
        tw = d.textlength(msg, font=f)
        d.rounded_rectangle((x1 - 22 - tw - 24, y0 + 14, x1 - 22, y0 + 54), radius=10, fill=BLACK)
        d.text((x1 - 22 - tw / 2 - 12, y0 + 34), msg, font=f, fill=WHITE, anchor="mm")
    else:
        arrow = {"CHARGING": "\u25b2", "DISCHARGING": "\u25bc"}.get(st.state, "\u25cf")
        _text(d, (x1 - 22, y0 + 20), f"{arrow} {st.state}", 28, bold=True, anchor="ra")

    # battery: percent, gauge, time to empty or full
    lx1 = x0 + (x1 - x0) * 9 // 20 if wide else x1
    lw = lx1 - x0 - 44
    pct = st.percent
    _fitted_value(d, x0 + 22, y0 + 176, fmt_num(pct, 0), "%", 120, 60, lw)
    bx0, by0, bx1, by1 = x0 + 22, y0 + 194, lx1 - 32, y0 + 226
    d.rectangle((bx0, by0, bx1, by1), outline=BLACK, width=3)
    d.rectangle((bx1, by0 + 9, bx1 + 10, by1 - 9), fill=BLACK)
    if pct is not None:
        d.rectangle((bx0 + 5, by0 + 5, bx0 + 5 + (bx1 - bx0 - 10) * max(0.0, min(pct, 100.0)) / 100, by1 - 5), fill=INK)
    charging = st.state == "CHARGING"
    left = None if stale else st.time_left()
    label = "Full in" if charging else "Time left"
    if charging and pct is not None and pct >= 99.5:
        label, value = "Battery", "Full"
    else:
        value = f"\u2248 {fmt_duration(left)}" if left else "--"
    used = _text(d, (x0 + 22, y0 + 284), label, 30, fill=DARK, anchor="ls")
    _fitted_text(d, (lx1 - 22, y0 + 284), value, 34, lw - used - 16, bold=True, anchor="rs")

    blocks = (
        ("IN", st.power_in, (("AC", "ac_input_power", None), ("DC", "dc_input_power", None))),
        ("OUT", st.power_out, (("AC", "ac_output_power", "ctrl_ac"), ("DC", "dc_output_power", "ctrl_dc"))),
    )

    def part(lab, key, switch):
        off = switch is not None and st.latest.get(switch) is False  # the output is switched off
        return lab, "OFF" if off else fmt_watts(st.watts(key))

    if wide:
        # stacked beside the battery: "IN   1.84 kW" over "AC 1.2 kW · DC 640 W"
        fx0, fx1 = lx1 + 24, x1 - 22
        for i, (title, total, rows) in enumerate(blocks):
            by = y0 + 136 + i * 112
            used = _text(d, (fx0, by), title, 30, bold=True, fill=DARK, anchor="ls")
            _fitted_value(d, fx1, by, *_watts_parts(total), 56, 30, fx1 - fx0 - used - 20, anchor="rs")
            sub = "  \u00b7  ".join(f"{lab} {val}" for lab, val in (part(*r) for r in rows))
            _fitted_text(d, (fx1, by + 40), sub, 26, fx1 - fx0, fill=DARK, anchor="rs")
    else:
        # side by side under the battery: a big total, then AC and DC rows
        fx0, fy = x0 + 22, y0 + 320
        bw = (x1 - 22 - fx0 - GAP) // 2
        for i, (title, total, rows) in enumerate(blocks):
            bx = fx0 + i * (bw + GAP)
            _text(d, (bx, fy), title, 26, bold=True, fill=DARK)
            _fitted_value(d, bx, fy + 84, *_watts_parts(total), 60, 32, bw - 8)
            for r, row in enumerate(rows):
                lab, val = part(*row)
                ry = fy + 128 + r * 40
                used = _text(d, (bx, ry), lab, 28, fill=DARK, anchor="ls")
                _fitted_text(d, (bx + bw, ry), val, 28, bw - used - 12, bold=True, anchor="rs")

    today_in, today_out = st.today_in_wh, st.today_out_wh
    if today_in is not None or today_out is not None:
        kwh = lambda v: "--" if v is None else fmt_kwh(v / 1000)  # noqa: E731
        line = f"Today: {kwh(today_in)} in \u00b7 {kwh(today_out)} out"
        _fitted_text(d, (x0 + 22, y1 - 20), line, 26, x1 - x0 - 44, fill=DARK, anchor="ls")


def _legend(d: ImageDraw.ImageDraw, right: int, baseline: int) -> None:
    """'[fill] In  [line] Out', ending at `right`."""
    x = right
    x -= _text(d, (x, baseline), "Out", 26, bold=True, anchor="rs") + 10
    d.rectangle((x - 34, baseline - 15, x, baseline - 10), fill=BLACK)
    x -= 34 + 26
    x -= _text(d, (x, baseline), "In", 26, bold=True, anchor="rs") + 10
    d.rectangle((x - 34, baseline - 24, x, baseline), fill=LIGHT, outline=DARK, width=2)


def render_bluetti(
    snap: BluettiSnapshot | None, now: datetime, tz, layout: str = "portrait", kindle: KindleBattery | None = None, chart_hours: int = 24
) -> Image.Image:
    """A column per station: its card, power in/out and battery % over `chart_hours`."""
    img = Image.new("L", size(layout), WHITE)
    w, h = img.size
    header(img, "bluetti", now.astimezone(tz), kindle)
    d = ImageDraw.Draw(img)
    stations = snap.stations if snap else []
    if not stations:
        _text(d, (w / 2, h / 2), "No Bluetti data (bluetti2influx)", 40, bold=True, anchor="mm")
        return quantize(img)
    top, bottom, n = HEADER_RULE_Y + 4 + GAP, h - 50, len(stations)
    cw = (w - 2 * M - (n - 1) * GAP) // n
    card_h = 360 if cw >= 700 else 546
    rest = bottom - top - card_h - 2 * GAP
    ph = int(rest * 0.55)
    start, end = snap.fetched_at - timedelta(hours=chart_hours), snap.fetched_at
    for i, st in enumerate(stations):
        x0 = M + i * (cw + GAP)
        x1, y = x0 + cw, top
        _bluetti_card(d, (x0, y, x1, y + card_h), st, now, tz)
        y += card_h + GAP
        _text(d, (x0, y + 30), f"POWER (W), {chart_hours} h", 30, bold=True, anchor="ls")
        _legend(d, x1, y + 30)
        img.paste(bluetti_power_chart(st.watts_in, st.watts_out, tz, start, end, cw, ph - 44), (x0, y + 44))
        y += ph + GAP
        _text(d, (x0, y + 30), f"BATTERY (%), {chart_hours} h", 30, bold=True, anchor="ls")
        img.paste(percent_chart(st.soc, tz, start, end, cw, bottom - y - 44), (x0, y + 44))
    newest = snap.latest_time.astimezone(tz).strftime("%H:%M:%S") if snap.latest_time else "--"
    _text(d, (w - M, h - 16), f"Bluetti via InfluxDB  \u00b7  newest sample {newest}", 22, fill=DARK, anchor="rs")
    return quantize(img)


# --------------------------------------------------------------------------- camera page

CAPTION_H = 56  # under each still: its label and when it was taken


def camera_boxes(layout: str, n: int) -> list[tuple[int, int, int, int]]:
    """Each camera's cell (still + caption): stacked in portrait, a 2-column grid in landscape."""
    w, h = size(layout)
    top, bottom = HEADER_RULE_Y + 4 + GAP, h - 50
    if n <= 0:
        return []
    cols = 2 if layout == "landscape" and n > 1 else 1
    rows = math.ceil(n / cols)
    cw = (w - 2 * M - (cols - 1) * GAP) // cols
    # a 16:9 still at full width, unless that's taller than its share of the page
    ch = min((bottom - top - (rows - 1) * GAP) // rows, cw * 9 // 16 + CAPTION_H)
    top += (bottom - top - rows * ch - (rows - 1) * GAP) // 2  # centred in what's left
    return [
        (M + (i % cols) * (cw + GAP), top + (i // cols) * (ch + GAP), M + (i % cols) * (cw + GAP) + cw, top + (i // cols) * (ch + GAP) + ch)
        for i in range(n)
    ]


GRAYS_16 = Image.new("P", (1, 1))
GRAYS_16.putpalette([v for i in range(16) for v in (i * 0x11,) * 3])


def photo(img: Image.Image, w: int, h: int) -> Image.Image:
    """A grayscale photo scaled to fit w x h, contrast stretched and dithered
    to the panel's 16 grays (dithering keeps smooth shades on e-ink)."""
    from PIL import ImageOps

    img = ImageOps.contain(img.convert("L"), (w, h), Image.LANCZOS)
    img = ImageOps.autocontrast(img, cutoff=1)
    return img.convert("RGB").quantize(palette=GRAYS_16, dither=Image.Dither.FLOYDSTEINBERG).convert("L")


def render_cameras(shots, now: datetime, tz, layout: str = "portrait", kindle: KindleBattery | None = None) -> Image.Image:
    """Each camera's latest still with its label and age; tapping one takes new ones."""
    img = Image.new("L", size(layout), WHITE)
    w, h = img.size
    header(img, "cameras", now.astimezone(tz), kindle)
    d = ImageDraw.Draw(img)
    if not shots:
        _text(d, (w / 2, h / 2), "No cameras configured (PROTECT_CAMERAS)", 40, bold=True, anchor="mm")
        return quantize(img)
    for box, shot in zip(camera_boxes(layout, len(shots)), shots):
        x0, y0, x1, y1 = box
        iy1 = y1 - CAPTION_H
        if shot.image is not None:
            pic = photo(shot.image, x1 - x0, iy1 - y0)
            px, py = x0 + (x1 - x0 - pic.width) // 2, y0 + (iy1 - y0 - pic.height) // 2
            img.paste(pic, (px, py))
            d.rectangle((px - 2, py - 2, px + pic.width + 1, py + pic.height + 1), outline=BLACK, width=2)
        else:
            d.rounded_rectangle((x0, y0, x1, iy1), radius=14, fill=FAINT, outline=BLACK, width=3)
            msg = "Can't reach the camera" if shot.error else "Taking a snapshot\u2026"
            _text(d, ((x0 + x1) / 2, (y0 + iy1) / 2), msg, 40, bold=True, anchor="mm")
        used = _text(d, (x0, y1 - 14), shot.label, 34, bold=True, anchor="ls")
        if shot.error:
            when = f"FAILED: {shot.error}"
            if shot.at:
                when = f"{when}  \u00b7  last {shot.at.astimezone(tz):%H:%M}"
            _fitted_text(d, (x1, y1 - 14), when, 26, x1 - x0 - used - 24, bold=True, anchor="rs")
        elif shot.at:
            _text(d, (x1, y1 - 14), f"{shot.at.astimezone(tz):%H:%M:%S}", 28, bold=True, fill=DARK, anchor="rs")
    _text(d, (w - M, h - 16), "UniFi Protect  \u00b7  tap a picture for a new one", 22, fill=DARK, anchor="rs")
    return quantize(img)


# --------------------------------------------------------------------------- car page


def car_regions(layout: str) -> dict[str, tuple[int, int, int, int]]:
    """The battery card, locks, climate, the row of small tiles, and the charge and refresh buttons."""
    w, h = size(layout)
    top, bottom = HEADER_RULE_Y + 4 + GAP, h - 50
    if layout == "landscape":
        rx = M + 780 + GAP
        y = top + 600 + GAP
        mid = (y + bottom - GAP) // 2
        return {
            "hero": (M, top, rx - GAP, top + 600),
            "charge": (M, y, rx - GAP, mid),
            "refresh": (M, mid + GAP, rx - GAP, bottom),
            "locks": (rx, top, w - M, top + 320),
            "climate": (rx, top + 320 + GAP, w - M, top + 640 + GAP),
            "small": (rx, top + 640 + 2 * GAP, w - M, bottom),
        }
    half = (w - 2 * M - GAP) // 2
    y = top + 480 + GAP
    yb = y + 580 + 2 * GAP
    return {
        "hero": (M, top, w - M, top + 480),
        "locks": (M, y, M + half, y + 320),
        "climate": (M + half + GAP, y, w - M, y + 320),
        "small": (M, y + 320 + GAP, w - M, y + 580 + GAP),
        "charge": (M, yb, M + half, bottom),
        "refresh": (M + half + GAP, yb, w - M, bottom),
    }


def car_controls(layout: str, st: CarStatus | None) -> dict[str, tuple[tuple[int, int, int, int], str]]:
    """Each tappable region's box and what a tap does now ("car:refresh", or "car:<command>" from car.COMMANDS)."""
    r = car_regions(layout)
    out = {"refresh": (r["refresh"], "car:refresh")}
    if st is not None:
        out["locks"] = (r["locks"], "car:unlock" if st.locked else "car:lock")
        out["climate"] = (r["climate"], "car:climate_off" if st.climate_on else "car:climate_on")
        if st.plugged_in:  # nothing to start or stop otherwise
            out["charge"] = (r["charge"], "car:charge_off" if st.charging else "car:charge_on")
    return out


def fmt_ago(seconds: float) -> str:
    if seconds < 90:
        return "just now"
    if seconds < 90 * 60:
        return f"{seconds / 60:.0f} min ago"
    if seconds < 48 * 3600:
        return f"{seconds / 3600:.0f} h ago"
    return f"{seconds / 86400:.0f} days ago"


def _car_state(st: CarStatus) -> str:
    if st.charging:
        return "▲ CHARGING"
    if st.plugged_in:
        return "● PLUGGED IN"
    return "UNPLUGGED" if st.plugged_in is False else ""


def _car_hero(d: ImageDraw.ImageDraw, box, st: CarStatus) -> None:
    """Name and charging state, battery % and range, a gauge marked at the charge limit, then charging or the limits."""
    x0, y0, x1, y1 = box
    extra = (y1 - y0 - 480) // 2  # taller in landscape: spread out
    d.rounded_rectangle(box, radius=14, outline=BLACK, width=3)
    _text(d, (x0 + 22, y0 + 16), (st.model or "CAR").upper(), 28, bold=True, fill=DARK)
    _fitted_text(d, (x0 + 22, y0 + 52), st.name, 40, (x1 - x0) * 0.55, bold=True)
    _text(d, (x1 - 22, y0 + 20), _car_state(st), 30, bold=True, anchor="ra")

    base = y0 + 260 + extra
    half = (x1 - x0) // 2
    _fitted_value(d, x0 + 22, base, fmt_num(st.battery_pct, 0), "%", 170, 80, half - 30)
    _text(d, (x1 - 22, base - 120), "RANGE", 26, bold=True, fill=DARK, anchor="rs")
    _fitted_value(d, x1 - 22, base, fmt_num(st.range, 0), st.range_unit, 110, 50, half - 30, anchor="rs")

    bx0, by0, bx1, by1 = x0 + 22, base + 30, x1 - 36, base + 80
    d.rectangle((bx0, by0, bx1, by1), outline=BLACK, width=3)
    d.rectangle((bx1, by0 + 12, bx1 + 12, by1 - 12), fill=BLACK)
    inner = bx1 - bx0 - 10
    if st.battery_pct is not None:
        d.rectangle((bx0 + 5, by0 + 5, bx0 + 5 + inner * max(0.0, min(st.battery_pct, 100.0)) / 100, by1 - 5), fill=INK)
    limit = st.limit_ac
    if limit is not None and 0 < limit < 100:  # where charging at home stops
        lx = bx0 + 5 + inner * limit / 100
        d.rectangle((lx - 3, by0 - 12, lx + 3, by1 + 12), fill=BLACK)
        _text(d, (lx, by1 + 16), f"limit {limit}%", 22, bold=True, anchor="ma")

    foot = y1 - 26
    if st.charging:
        left = "≈ " + fmt_duration(st.charge_minutes * 60) if st.charge_minutes else "--"
        used = _text(d, (x0 + 22, foot), f"To {limit}%" if limit else "Full in", 30, fill=DARK, anchor="ls")
        _text(d, (x0 + 22 + used + 14, foot), left, 36, bold=True, anchor="ls")
        if st.charge_kw:
            _value_unit(d, x1 - 22, foot, fmt_num(st.charge_kw, 1), "kW", 44, 28, anchor="rs")
    else:
        used = _text(d, (x0 + 22, foot), "Charge limits", 30, fill=DARK, anchor="ls")
        lim = lambda v: "--" if v is None else f"{v}%"  # noqa: E731
        _fitted_text(d, (x1 - 22, foot), f"AC {lim(st.limit_ac)}  ·  DC {lim(st.limit_dc)}", 34, x1 - x0 - used - 64, bold=True, anchor="rs")


def _command_label(action: str) -> str:
    """"car:unlock" -> "UNLOCK"; "car:refresh" -> "WAKE THE CAR"."""
    name = action.split(":", 1)[1]
    return "WAKE THE CAR" if name == "refresh" else COMMANDS[name][0]


def _car_control(d: ImageDraw.ImageDraw, box, action: str | None, confirm: str | None, sending: str | None) -> bool:
    """A control tile's prompt ("TAP AGAIN TO ...") or "SENDING..."; False if neither applies (draw the tile as usual)."""
    if action is None or action not in (confirm, sending):
        return False
    x0, y0, x1, y1 = box
    label, width = _command_label(action), x1 - x0 - 56
    if action == sending:
        d.rounded_rectangle(box, radius=14, fill=BLACK, outline=BLACK, width=3)
        _fitted_text(d, (x0 + 28, y1 - 104), f"{label}:", 36, width, bold=True, fill=LIGHT, anchor="ls")
        _fitted_text(d, (x0 + 28, y1 - 30), "SENDING…", 64, width, bold=True, fill=WHITE, anchor="ls")
    else:
        d.rounded_rectangle(box, radius=14, fill=WHITE, outline=BLACK, width=12)
        _text(d, (x0 + 28, y1 - 104), "TAP AGAIN TO", 36, bold=True, anchor="ls")
        _fitted_text(d, (x0 + 28, y1 - 30), label, 64, width, bold=True, anchor="ls")
    return True


def _hint(d, box, action: str | None, fill) -> None:
    """'TAP TO UNLOCK' at a control tile's bottom right."""
    if action:
        x0, _, x1, y1 = box
        _fitted_text(d, (x1 - 22, y1 - 20), f"TAP TO {_command_label(action)}", 24, (x1 - x0) / 2, bold=True, fill=fill, anchor="rs")


def _car_locks(d: ImageDraw.ImageDraw, box, st: CarStatus, action: str | None = None) -> None:
    """LOCKED, or UNLOCKED in white on black; then what's open."""
    x0, y0, x1, y1 = box
    alarm = st.locked is False
    fg, sub = (WHITE, LIGHT) if alarm else (BLACK, DARK)
    d.rounded_rectangle(box, radius=14, fill=BLACK if alarm else WHITE, outline=BLACK, width=3)
    _text(d, (x0 + 22, y0 + 16), "LOCKS", 28, bold=True, fill=sub)
    word = "--" if st.locked is None else ("LOCKED" if st.locked else "UNLOCKED")
    _fitted_text(d, (x0 + 22, y0 + 150), word, 76, x1 - x0 - 44, bold=True, fill=fg, anchor="ls")
    if st.open:
        _text(d, (x0 + 22, y0 + 200), "OPEN", 26, bold=True, fill=sub, anchor="ls")
        lines = _wrap(d, ", ".join(st.open), 32, x1 - x0 - 44, max_lines=1 if action else 2)
        for i, line in enumerate(lines):
            _text(d, (x0 + 22, y0 + 244 + i * 42), line, 32, bold=True, fill=fg, anchor="ls")
    else:
        _fitted_text(d, (x0 + 22, y0 + 210), "Doors and windows closed", 28, x1 - x0 - 44, fill=sub, anchor="ls")
    _hint(d, box, action, sub)


def _car_climate(d: ImageDraw.ImageDraw, box, st: CarStatus, action: str | None = None, temp: float | None = None) -> None:
    x0, y0, x1, y1 = box
    _tile(d, box, "CLIMATE")
    word = "--" if st.climate_on is None else ("ON" if st.climate_on else "OFF")
    _text(d, (x0 + 22, y0 + 150), word, 76, bold=True, anchor="ls")
    notes = []
    if st.climate_on and st.climate_set is not None:
        notes.append(f"Set to {st.climate_set:.0f}°")
    if st.defrost_on:
        notes.append("Defrost on")
    if notes:
        _fitted_text(d, (x0 + 22, y0 + 210), "  ·  ".join(notes), 34, x1 - x0 - 44, bold=True, anchor="ls")
    elif not st.climate_on and temp is not None and action:
        _text(d, (x0 + 22, y0 + 210), f"Starts at {temp:.0f}°", 28, fill=DARK, anchor="ls")
    _hint(d, box, action, DARK)


def _car_small(d: ImageDraw.ImageDraw, box, st: CarStatus) -> None:
    """12 V battery, tires and odometer side by side."""
    x0, y0, x1, y1 = box
    tw = (x1 - x0 - 2 * GAP) // 3
    b = [(x0 + i * (tw + GAP), y0, x0 + i * (tw + GAP) + tw, y1) for i in range(3)]
    tires = "--" if st.tire_warning is None else ("LOW" if st.tire_warning else "OK")
    values = (
        ("12 V BATTERY", fmt_num(st.aux_battery_pct, 0), "%"),
        ("TIRES", tires, ""),
        ("ODOMETER", fmt_num(st.odometer, 0), st.odometer_unit),
    )
    for (bx0, by0, bx1, by1), (label, value, unit) in zip(b, values):
        d.rounded_rectangle((bx0, by0, bx1, by1), radius=14, outline=BLACK, width=3)  # _tile, with the label fitted to narrow tiles
        _fitted_text(d, (bx0 + 22, by0 + 16), label, 28, bx1 - bx0 - 44, bold=True, fill=DARK)
        _fitted_value(d, bx0 + 22, by0 + 140, value, unit, 76, 36, bx1 - bx0 - 44)
    if any(p is not None for p in st.tires):
        bx0, _, bx1, by1 = b[1]
        psi = "  ".join("--" if p is None else f"{p:.0f}" for p in st.tires)
        _fitted_text(d, (bx0 + 22, by1 - 52), psi, 30, bx1 - bx0 - 44, bold=True, anchor="ls")
        _text(d, (bx0 + 22, by1 - 18), f"{st.tire_unit}: front, rear", 22, fill=DARK, anchor="ls")


def _button(d: ImageDraw.ImageDraw, box, title: str, lines: list[str], lit: bool = False, off: bool = False) -> None:
    """A big centred button: solid while `lit`, pale gray while `off` (nothing to tap)."""
    x0, y0, x1, y1 = box
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    bg, fg, sub = (BLACK, WHITE, LIGHT) if lit else ((FAINT, DARK, DARK) if off else (WHITE, BLACK, DARK))
    d.rounded_rectangle(box, radius=18, fill=bg, outline=BLACK, width=4)
    _fitted_text(d, (cx, cy - 20 * len(lines)), title, 52, x1 - x0 - 48, bold=True, fill=fg, anchor="ms")
    for i, line in enumerate(lines):
        _fitted_text(d, (cx, cy + 30 + 40 * i - 20 * (len(lines) - 1)), line, 28, x1 - x0 - 48, bold=i == 0 and not lit, fill=fg if i == 0 else sub, anchor="ms")


def _car_refresh(d: ImageDraw.ImageDraw, box, st: CarStatus | None, busy: str | None, wake_in: float, now: datetime, tz) -> None:
    """Wakes the car for fresh numbers, or (too soon since the last wake) re-reads Hyundai's copy."""
    if busy:
        wake = busy == "wake"
        _button(d, box, "ASKING THE CAR…" if wake else "CHECKING…", ["Takes up to a minute" if wake else "Reading Hyundai's copy"], lit=wake)
        return
    lines = []
    if st and st.reported_at:
        at = st.reported_at.astimezone(tz)
        day = "" if at.date() == now.date() else f" {at:%a %-d %b}"
        lines.append(f"Car reported {at:%H:%M}{day}  ·  {fmt_ago((now - st.reported_at).total_seconds())}")
    lines.append(f"Can wake the car again in {math.ceil(wake_in / 60)} min" if wake_in else "Tap twice to wake the car for new numbers")
    _button(d, box, "CHECK AGAIN" if wake_in else "REFRESH FROM CAR", lines)


def _car_charge(d: ImageDraw.ImageDraw, box, st: CarStatus | None, action: str | None) -> None:
    if action is None:
        _button(d, box, "CHARGING", ["Not plugged in" if st and st.plugged_in is False else "--"], off=True)
    elif action == "car:charge_off":
        _button(d, box, "STOP CHARGING", [f"Charging to {st.limit_ac}%" if st.limit_ac else "Charging"])
    else:
        _button(d, box, "START CHARGING", [f"To the {st.limit_ac}% limit" if st.limit_ac else "Plugged in"])


def render_car(
    st: CarStatus | None,
    now: datetime,
    tz,
    layout: str = "portrait",
    kindle: KindleBattery | None = None,
    busy: str | None = None,
    wake_in: float = 0.0,
    error: str | None = None,
    checked_at: datetime | None = None,
    configured: bool = True,
    confirm: str | None = None,
    sending: str | None = None,
    note: str | None = None,
    climate_temp: float | None = None,
) -> Image.Image:
    """The car: battery and range, locks, climate, 12 V battery, tires, odometer, and charge and refresh buttons.

    Locks, climate and charging are controls: `confirm` is the action ("car:unlock") waiting for its
    second tap, and `sending` the command on its way to the car. `busy` is "wake" or "read" while one
    runs; `wake_in` is how long until a tap may wake the car again; `note` says how the last command went.
    """
    img = Image.new("L", size(layout), WHITE)
    w, h = img.size
    now = now.astimezone(tz)
    header(img, "car", now, kindle)
    d = ImageDraw.Draw(img)
    if not configured:
        _text(d, (w / 2, h / 2), "Set BLUELINK_USER, _PASSWORD and _PIN", 40, bold=True, anchor="mm")
        return quantize(img)
    r, controls = car_regions(layout), car_controls(layout, st)
    action = {name: a for name, (_, a) in controls.items()}
    sending = f"car:{sending}" if sending else None
    if st is None:
        x0, y0, x1, _ = r["hero"]
        msg = error or "Connecting to Blue Link…"
        _fitted_text(d, ((x0 + x1) / 2, (y0 + r["charge"][1]) / 2), msg, 40, x1 - x0, bold=True, anchor="mm")
    else:
        _car_hero(d, r["hero"], st)
        if not _car_control(d, r["locks"], action.get("locks"), confirm, sending):
            _car_locks(d, r["locks"], st, action.get("locks"))
        if not _car_control(d, r["climate"], action.get("climate"), confirm, sending):
            _car_climate(d, r["climate"], st, action.get("climate"), climate_temp)
        _car_small(d, r["small"], st)
    if not _car_control(d, r["charge"], action.get("charge"), confirm, sending):
        _car_charge(d, r["charge"], st, action.get("charge"))
    if busy or not _car_control(d, r["refresh"], "car:refresh", confirm, None):
        _car_refresh(d, r["refresh"], st, busy, wake_in, now, tz)
    parts = ["Hyundai Blue Link"] + ([f"checked {checked_at.astimezone(tz):%H:%M}"] if checked_at else [])
    if error and st is not None:
        parts.append(f"FAILED: {error}")
    if note:
        parts.append(note)
    loud = bool(error and st is not None) or bool(note)
    _fitted_text(d, (w - M, h - 16), "  ·  ".join(parts), 22, w - 2 * M - 80, bold=loud, fill=BLACK if loud else DARK, anchor="rs")
    return quantize(img)


# --------------------------------------------------------------------------- health


def issues_badge(img: Image.Image, n: int) -> Image.Image:
    """A copy of `img` with a black "! N ISSUES" badge over the date (tap it for the health page)."""
    out = img.copy()
    d = ImageDraw.Draw(out)
    box = (out.width - M - CLOCK_W, 80, out.width - M, 118)
    d.rectangle((box[0], 78, box[2], 120), fill=WHITE)
    d.rounded_rectangle(box, radius=10, fill=BLACK)
    text = f"\u26a0 {n} ISSUE{'S' if n != 1 else ''}"
    k = _shrink(d, ((text, 26),), CLOCK_W - 20)
    d.text(((box[0] + box[2]) / 2, (box[1] + box[3]) / 2), text, font=font(int(26 * k), True), fill=WHITE, anchor="mm")
    return quantize(out)


def render_health(checks, now: datetime, tz, layout: str = "portrait", kindle: KindleBattery | None = None) -> Image.Image:
    """A row per subsystem: OK or PROBLEM, and what it last did."""
    img = Image.new("L", size(layout), WHITE)
    w, h = img.size
    header(img, "health", now.astimezone(tz), kindle)
    d = ImageDraw.Draw(img)
    bad = sum(not c.ok for c in checks)
    y = HEADER_RULE_Y + 4 + GAP
    _text(d, (M, y + 50), "All systems OK" if not bad else f"{bad} problem{'s' if bad != 1 else ''}", 48, bold=True, anchor="ls")
    y += 80
    row_h = min(120, (h - 50 - y) // max(1, len(checks)))
    pill_w = 210
    for c in checks:
        mid = y + row_h // 2
        pill = (M, mid - 26, M + pill_w, mid + 26)
        d.rounded_rectangle(pill, radius=12, fill=WHITE if c.ok else BLACK, outline=BLACK, width=3)
        _text(d, ((pill[0] + pill[2]) / 2, mid), "OK" if c.ok else "PROBLEM", 30, bold=True, fill=BLACK if c.ok else WHITE, anchor="mm")
        tx = M + pill_w + 28
        _fitted_text(d, (tx, mid - 6), c.name, 36, w - M - tx, bold=True, anchor="ls")
        _fitted_text(d, (tx, mid + 32), c.detail, 26, w - M - tx, bold=not c.ok, fill=BLACK if not c.ok else DARK, anchor="ls")
        y += row_h
        d.line((M, y - 1, w - M, y - 1), fill=FAINT, width=2)
    _text(d, (w - M, h - 16), "Tap a tab to go back", 22, fill=DARK, anchor="rs")
    return quantize(img)


def _hatch_swatch(img: Image.Image, box):
    """Legend swatch matching the chart's /// hatch."""
    x0, y0, x1, y1 = (int(v) for v in box)
    sw = Image.new("L", (x1 - x0, y1 - y0), WHITE)
    sd = ImageDraw.Draw(sw)
    h = y1 - y0
    for off in range(-h, x1 - x0, 9):
        sd.line([(off, h), (off + h, 0)], fill=BLACK, width=2)
    sd.rectangle((0, 0, x1 - x0 - 1, h - 1), outline=BLACK, width=2)
    img.paste(sw, (x0, y0))


def stale_badge(img: Image.Image, since: datetime, tz) -> Image.Image:
    """Return a copy of `img` with a high-contrast STALE badge in the header."""
    out = img.copy()
    d = ImageDraw.Draw(out)
    text = f"STALE since {since.astimezone(tz):%H:%M}"
    f = font(30, True)
    tw = d.textlength(text, font=f)
    # over the clock, which it replaces; the date line below stays
    box = (out.width - M - tw - 32, 24, out.width - M, 80)
    d.rectangle((box[0] - 8, 20, out.width - M, 82), fill=WHITE)
    d.rounded_rectangle(box, radius=12, fill=BLACK)
    d.text(((box[0] + box[2]) / 2, (box[1] + box[3]) / 2), text, font=f, fill=WHITE, anchor="mm")
    return quantize(out)


def placeholder(message: str, now: datetime, tz, layout: str = "portrait", page: str = "solar") -> Image.Image:
    """A message under the usual header, so the tabs still work."""
    img = Image.new("L", size(layout), WHITE)
    w, h = img.size
    header(img, page, now.astimezone(tz), None)
    d = ImageDraw.Draw(img)
    _text(d, (w / 2, h / 2 - 20), message, 44, bold=True, anchor="mm")
    _text(d, (w / 2, h / 2 + 50), f"{now.astimezone(tz):%H:%M}", 32, fill=DARK, anchor="mm")
    return quantize(img)


def to_png(img: Image.Image, rotate: int = 0) -> bytes:
    if rotate:
        img = img.rotate(-rotate, expand=True)  # clockwise, like a device rotation
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=False, compress_level=6)
    return buf.getvalue()
