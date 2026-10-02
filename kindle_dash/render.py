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

from .data import Point, Snapshot  # noqa: E402

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


def _header(c: _Ctx) -> int:
    """Title, timestamp and rule across the top; returns the y below it."""
    w = c.img.width
    y = 26
    _text(c.d, (M, y + 70), "SOLAR", 64, bold=True, anchor="ls")
    _text(c.d, (w - M, y + 44), f"Updated {c.now:%H:%M}", 40, bold=True, anchor="rs")
    date = f"{c.now:%a %-d %b}"
    _text(c.d, (w - M, y + 80), date, 28, fill=DARK, anchor="rs")
    if c.kindle:
        _kindle_battery(c.d, w - M - c.d.textlength(date, font=font(28)) - 28, y + 80, c.kindle)
    y += 100
    c.d.rectangle((M, y, w - M, y + 4), fill=BLACK)
    return y + 4 + GAP


def _kindle_battery(d: ImageDraw.ImageDraw, right: float, baseline: float, kb: KindleBattery) -> None:
    """'Kindle [icon] 85%' ending at `right`; bold black when low and not charging."""
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
    _text(d, (x0 - 10, baseline), "Kindle", size, bold=low, fill=fill, anchor="rs")


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
    f = font(34, True)
    tw = d.textlength(text, font=f)
    cx, cy = out.width // 2 - 40, 26 + 48
    box = (cx - tw / 2 - 22, cy - 32, cx + tw / 2 + 22, cy + 32)
    d.rounded_rectangle(box, radius=12, fill=BLACK)
    d.text((cx, cy), text, font=f, fill=WHITE, anchor="mm")
    return quantize(out)


def placeholder(message: str, now: datetime, tz, layout: str = "portrait") -> Image.Image:
    w, h = (H, W) if layout == "landscape" else (W, H)
    img = Image.new("L", (w, h), WHITE)
    d = ImageDraw.Draw(img)
    _text(d, (w / 2, h / 2 - 40), "SOLAR", 96, bold=True, anchor="mm")
    _text(d, (w / 2, h / 2 + 60), message, 40, anchor="mm")
    _text(d, (w / 2, h / 2 + 120), f"{now.astimezone(tz):%H:%M}", 32, fill=DARK, anchor="mm")
    return quantize(img)


def to_png(img: Image.Image, rotate: int = 0) -> bytes:
    if rotate:
        img = img.rotate(-rotate, expand=True)  # clockwise, like a device rotation
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=False, compress_level=6)
    return buf.getvalue()
