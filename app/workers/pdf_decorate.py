"""Watermarks, headers, footers and page numbers (runs in worker processes).

These are overlays: text drawn on top of each page, positioned relative to the
page as the reader sees it. Like all overlays they become part of the page in
the new version and do not alter what is underneath.
"""

from __future__ import annotations

import math
from datetime import date
from typing import Any

from pikepdf import Pdf

from app.workers.pdf_annotate import draw_items
from app.workers.pdf_geometry import page_frame, text_width


def _fill(template: str, page: int, pages: int, name: str) -> str:
    return (template.replace("{page}", str(page)).replace("{pages}", str(pages))
            .replace("{date}", date.today().isoformat()).replace("{name}", name))


def watermark(pdf: Pdf, params: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    text, angle = params["text"], math.radians(params.get("angle", 45))
    wanted = set(params.get("pages") or range(1, len(pdf.pages) + 1))
    items = []
    for number, page in enumerate(pdf.pages, start=1):
        if number not in wanted:
            continue
        frame = page_frame(page)
        size = params.get("size")
        if not size:
            # Fit the text along the direction it runs, leaving a margin.
            available = min(frame.width / max(abs(math.cos(angle)), 0.2), frame.height / max(abs(math.sin(angle)), 0.2))
            size = max(12.0, min(160.0, 0.8 * available / max(text_width(text, 1, bold=True), 0.5)))
        width = text_width(text, size, bold=True)
        across = (math.cos(angle), -math.sin(angle))          # reading direction on screen (y down)
        down = (math.sin(angle), math.cos(angle))
        # Put the optical centre of the capitals at the centre of the page.
        left = frame.width / 2 - across[0] * width / 2 - down[0] * size * 0.56
        top = frame.height / 2 - across[1] * width / 2 - down[1] * size * 0.56
        items.append({
            "type": "text", "page": number, "text": text, "size": size, "font": "sans", "bold": True,
            "color": params.get("color", "#b3261e"), "opacity": params.get("opacity", 0.25),
            "box": {"tl": frame.to_pdf(left, top),
                    "tr": frame.to_pdf(left + across[0] * width, top + across[1] * width),
                    "bl": frame.to_pdf(left + down[0] * size, top + down[1] * size)},
        })
    total, _ = draw_items(pdf, items)
    return total, {"pages_marked": len(items),
                   "note": "The watermark is drawn on top of each page. It is a visible mark, not a security feature: "
                           "anyone with the earlier version or an editor can produce a copy without it."}


def header_footer(pdf: Pdf, params: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    size, margin = float(params.get("size", 10)), float(params.get("margin", 28))
    color, name = params.get("color", "#333333"), params.get("name", "")
    first = int(params.get("first_page", 1))
    number_from = int(params.get("number_from", 1))
    pages = len(pdf.pages)
    shown_total = pages - first + number_from
    items = []
    for number, page in enumerate(pdf.pages, start=1):
        if number < first:
            continue
        frame = page_frame(page)
        shown = number - first + number_from
        for band, top in (("header", margin), ("footer", frame.height - margin - size * 1.2)):
            for slot, template in (params.get(band) or {}).items():
                text = _fill(template, shown, shown_total, name).strip()
                if not text:
                    continue
                width = text_width(text, size)
                left = {"left": margin, "center": (frame.width - width) / 2, "right": frame.width - margin - width}[slot]
                items.append({"type": "text", "page": number, "text": text, "size": size, "font": "sans",
                              "color": color, "box": frame.box(left, top, max(width, 1.0), size * 1.2)})
    if not items:
        raise ValueError("Nothing to add: every header and footer field is empty.")
    total, _ = draw_items(pdf, items)
    return total, {"pages_marked": len({item["page"] for item in items}), "texts_added": len(items),
                   "note": "Headers and footers are drawn on top of each page. If the page already has content in "
                           "the margin, they will overlap it."}
