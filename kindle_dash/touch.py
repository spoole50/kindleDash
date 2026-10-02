"""Coordinates for taps: raw touch -> panel pixels -> the (rotated) image, and back.

The panel is portrait, 1236 x 1648. Images are drawn upright and rotated
clockwise by `rotate` degrees to fit it (render.to_png), so a panel pixel maps
back into the image by undoing that rotation.
"""

from __future__ import annotations

from typing import NamedTuple

from .config import Settings

PANEL_W, PANEL_H = 1236, 1648

Box = tuple[int, int, int, int]  # x0, y0, x1, y1 (exclusive), image pixels


def raw_to_panel(x: int, y: int, s: Settings) -> tuple[int, int]:
    """Raw touch coordinates to panel pixels, per the TOUCH_* settings."""
    max_x, max_y = s.touch_max_x, s.touch_max_y
    if s.touch_swap_xy:
        x, y, max_x, max_y = y, x, max_y, max_x
    if s.touch_flip_x:
        x = max_x - x
    if s.touch_flip_y:
        y = max_y - y
    px = round(x * (PANEL_W - 1) / max(max_x, 1))
    py = round(y * (PANEL_H - 1) / max(max_y, 1))
    return min(max(px, 0), PANEL_W - 1), min(max(py, 0), PANEL_H - 1)


def panel_to_image(px: int, py: int, rotate: int, size: tuple[int, int]) -> tuple[int, int]:
    """A panel pixel to the pixel of the upright image (size = its width, height)."""
    iw, ih = size
    if rotate == 90:
        return py, ih - 1 - px
    if rotate == 180:
        return iw - 1 - px, ih - 1 - py
    if rotate == 270:
        return iw - 1 - py, px
    return px, py


def image_to_panel(x: int, y: int, rotate: int, size: tuple[int, int]) -> tuple[int, int]:
    iw, ih = size
    if rotate == 90:
        return ih - 1 - y, x
    if rotate == 180:
        return iw - 1 - x, ih - 1 - y
    if rotate == 270:
        return y, iw - 1 - x
    return x, y


def box_to_panel(box: Box, rotate: int, size: tuple[int, int]) -> tuple[int, int, int, int]:
    """An image box as panel (left, top, width, height), for FBInk."""
    x0, y0, x1, y1 = box
    ax, ay = image_to_panel(x0, y0, rotate, size)
    bx, by = image_to_panel(x1 - 1, y1 - 1, rotate, size)
    left, top = min(ax, bx), min(ay, by)
    return left, top, max(ax, bx) - left + 1, max(ay, by) - top + 1


class Target(NamedTuple):
    """A tappable area: where a tap counts, the box to flash, and what it does."""

    area: Box
    flash: Box
    action: str  # "page:<name>" or "plug:<host>"


def hit(targets: list[Target], x: int, y: int) -> Target | None:
    """The first target whose area contains image pixel (x, y)."""
    for t in targets:
        x0, y0, x1, y1 = t.area
        if x0 <= x < x1 and y0 <= y < y1:
            return t
    return None
