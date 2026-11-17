"""Text extraction for search and classification (runs in worker processes)."""

from __future__ import annotations

import threading
from typing import Any

from app.workers.pdfium_lock import PDFIUM_LOCK as _pdfium_lock  # noqa: E402
MAX_PAGE_CHARS = 200_000
MAX_TOTAL_CHARS = 20_000_000


def extract(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the text of every page, and whether a page looks like a scan without a text layer."""
    import pypdfium2 as pdfium
    import pypdfium2.raw as raw

    with _pdfium_lock:
        try:
            document = pdfium.PdfDocument(payload["path"])
        except pdfium.PdfiumError:
            return {"pages": [], "locked": True}
        pages: list[dict[str, Any]] = []
        total = 0
        try:
            for index in range(len(document)):
                page = document[index]
                text = ""
                try:
                    text_page = page.get_textpage()
                    text = (text_page.get_text_range() or "").replace("\r\n", "\n").replace("\x00", "")
                    text_page.close()
                except Exception:
                    text = ""
                text = text[:MAX_PAGE_CHARS] if total < MAX_TOTAL_CHARS else ""
                total += len(text)

                # A page with (almost) no text but a picture covering much of it is treated as a scan.
                needs_ocr = False
                if len(text.strip()) < 12:
                    try:
                        width, height = page.get_size()
                        area = max(1.0, width * height)
                        for obj in page.get_objects(filter=[raw.FPDF_PAGEOBJ_IMAGE], max_depth=3):
                            left, bottom, right, top = obj.get_bounds()
                            if (right - left) * (top - bottom) >= 0.3 * area:
                                needs_ocr = True
                                break
                    except Exception:
                        needs_ocr = False
                pages.append({"page": index + 1, "text": text, "needs_ocr": needs_ocr})
                page.close()
        finally:
            document.close()
    return {"pages": pages, "locked": False}
