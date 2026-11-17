"""Page geometry shared by worker tasks.

A PDF page is drawn in "user space" (origin bottom-left, y up) but shown on
screen after applying its crop box and its /Rotate entry. ``Frame`` converts
between the two, so features that think in terms of "top of the page as the
reader sees it" (headers, watermarks, redaction) work on rotated pages too.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pikepdf

# Helvetica advance widths (1/1000 em) for ASCII 32..126, from the standard font metrics.
_HELVETICA = [
    278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278,          # space .. /
    556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556,          # 0 .. ?
    1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778,         # @ .. O
    667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556,          # P .. _
    333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,          # ` .. o
    556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584,               # p .. ~
]
_HELVETICA_BOLD = [
    278, 333, 474, 556, 556, 889, 722, 238, 333, 333, 389, 584, 278, 333, 278, 278,
    556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 333, 333, 584, 584, 584, 611,
    975, 722, 722, 722, 722, 667, 611, 778, 722, 278, 556, 722, 611, 833, 722, 778,
    667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 333, 278, 333, 584, 556,
    333, 556, 611, 556, 611, 556, 333, 611, 611, 278, 278, 556, 278, 889, 611, 611,
    611, 611, 389, 556, 333, 611, 556, 778, 556, 556, 500, 389, 280, 389, 584,
]


def text_width(text: str, size: float, *, bold: bool = False) -> float:
    """Width of ``text`` in Helvetica at ``size`` points. Non-ASCII Latin characters are approximated."""
    table = _HELVETICA_BOLD if bold else _HELVETICA
    units = 0
    for char in text:
        code = ord(char)
        units += table[code - 32] if 32 <= code <= 126 else (611 if bold else 556)
    return units * size / 1000


def inherited(obj: pikepdf.Object, key: str) -> Any:
    """Look up a page attribute that may be inherited from a parent Pages node."""
    node = obj
    for _ in range(64):
        if node is None:
            return None
        if key in node:
            return node[key]
        node = node.get("/Parent")
    return None


@dataclass(frozen=True)
class Frame:
    """The visible page as the reader sees it: ``width`` x ``height`` points, origin top-left, y down."""

    x0: float
    y0: float
    x1: float
    y1: float
    rotation: int

    @property
    def width(self) -> float:
        return (self.y1 - self.y0) if self.rotation in (90, 270) else (self.x1 - self.x0)

    @property
    def height(self) -> float:
        return (self.x1 - self.x0) if self.rotation in (90, 270) else (self.y1 - self.y0)

    def to_pdf(self, dx: float, dy: float) -> list[float]:
        if self.rotation == 90:
            return [self.x0 + dy, self.y0 + dx]
        if self.rotation == 180:
            return [self.x1 - dx, self.y0 + dy]
        if self.rotation == 270:
            return [self.x1 - dy, self.y1 - dx]
        return [self.x0 + dx, self.y1 - dy]

    def to_display(self, x: float, y: float) -> list[float]:
        if self.rotation == 90:
            return [y - self.y0, x - self.x0]
        if self.rotation == 180:
            return [self.x1 - x, y - self.y0]
        if self.rotation == 270:
            return [self.y1 - y, self.x1 - x]
        return [x - self.x0, self.y1 - y]

    def box(self, left: float, top: float, width: float, height: float) -> dict[str, list[float]]:
        """A three-corner box (as used by the overlay code) for an upright rectangle on screen."""
        return {"tl": self.to_pdf(left, top), "tr": self.to_pdf(left + width, top), "bl": self.to_pdf(left, top + height)}


def page_frame(page: pikepdf.Page) -> Frame:
    media = [float(v) for v in (inherited(page.obj, "/MediaBox") or [0, 0, 612, 792])]
    crop_raw = inherited(page.obj, "/CropBox")
    crop = [float(v) for v in crop_raw] if crop_raw is not None else media
    # The visible area is the crop box clipped to the media box.
    x0, y0 = max(min(crop[0], crop[2]), min(media[0], media[2])), max(min(crop[1], crop[3]), min(media[1], media[3]))
    x1, y1 = min(max(crop[0], crop[2]), max(media[0], media[2])), min(max(crop[1], crop[3]), max(media[1], media[3]))
    if x1 - x0 < 1 or y1 - y0 < 1:
        x0, y0, x1, y1 = min(media[0], media[2]), min(media[1], media[3]), max(media[0], media[2]), max(media[1], media[3])
    try:
        rotation = int(inherited(page.obj, "/Rotate") or 0) % 360
    except (TypeError, ValueError):
        rotation = 0
    if rotation not in (0, 90, 180, 270):
        rotation = 0
    return Frame(x0, y0, x1, y1, rotation)
