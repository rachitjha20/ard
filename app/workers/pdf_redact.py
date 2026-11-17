"""Redaction (runs in worker processes).

Method: every page that has a redaction area is replaced by a picture of
itself with the areas blacked out. The page's original content stream, fonts,
images and annotations are dropped from the new file, so the text under a
redaction box is not merely covered; it is no longer there.

Cost, stated plainly: a redacted page becomes an image. Its remaining text
can no longer be selected or searched until OCR is run on it, and it is
rendered at a fixed resolution.

After saving, the output is re-opened and checked. If any check fails the
output is discarded and no version is created.

What this does not do: it does not look for the same information elsewhere
(other pages, bookmarks, metadata, attachments), and it cannot reach copies
of the document outside this file, including the earlier versions ARD keeps.
"""

from __future__ import annotations

import io
import threading
from typing import Any

import pikepdf
from pikepdf import Dictionary, Name, Pdf

from app.workers.pdf_geometry import page_frame

from app.workers.pdfium_lock import PDFIUM_LOCK as _pdfium_lock  # noqa: E402
RENDER_DPI = 200
MAX_PIXELS = 14_000_000
# Page-level entries that can hold or reference the original content.
_PAGE_KEYS_TO_DROP = ("/Annots", "/Thumb", "/PieceInfo", "/Metadata", "/B", "/AA", "/StructParents", "/Group")


class RedactionError(Exception):
    pass


def _corners(area: dict[str, Any]) -> list[list[float]]:
    tl, tr, bl = area["box"]["tl"], area["box"]["tr"], area["box"]["bl"]
    br = [tr[0] + bl[0] - tl[0], tr[1] + bl[1] - tl[1]]
    return [tl, tr, br, bl]


def apply(pdf: Pdf, source: str, areas: list[dict[str, Any]]) -> dict[str, Any]:
    """Replace each affected page with a blacked-out picture of itself."""
    import pypdfium2 as pdfium
    from PIL import ImageDraw

    by_page: dict[int, list[dict[str, Any]]] = {}
    for area in areas:
        by_page.setdefault(area["page"], []).append(area)
    total = len(pdf.pages)
    for number in by_page:
        if number < 1 or number > total:
            raise RedactionError(f"Page {number} does not exist (the document has {total} pages).")

    annotations_removed = 0
    with _pdfium_lock:
        document = pdfium.PdfDocument(source)
        try:
            for number, page_areas in sorted(by_page.items()):
                page = pdf.pages[number - 1]
                frame = page_frame(page)
                scale = RENDER_DPI / 72
                pixels = frame.width * frame.height * scale * scale
                if pixels > MAX_PIXELS:
                    scale *= (MAX_PIXELS / pixels) ** 0.5
                source_page = document[number - 1]
                image = source_page.render(scale=scale).to_pil().convert("RGB")
                source_page.close()
                # Scale from the rendered size, so a rounding difference cannot misplace a box.
                sx, sy = image.width / frame.width, image.height / frame.height
                draw = ImageDraw.Draw(image)
                for area in page_areas:
                    polygon = [(frame.to_display(x, y)[0] * sx, frame.to_display(x, y)[1] * sy) for x, y in _corners(area)]
                    draw.polygon(polygon, fill=(0, 0, 0))

                buffer = io.BytesIO()
                image.save(buffer, format="JPEG", quality=88, optimize=True)
                picture = pdf.make_stream(b"")
                picture.write(buffer.getvalue(), filter=Name.DCTDecode)
                picture.Type, picture.Subtype = Name.XObject, Name.Image
                picture.Width, picture.Height = image.width, image.height
                picture.ColorSpace, picture.BitsPerComponent = Name.DeviceRGB, 8

                # Place the picture so it fills the visible page in the page's own orientation.
                tl, tr, bl = frame.to_pdf(0, 0), frame.to_pdf(frame.width, 0), frame.to_pdf(0, frame.height)
                matrix = [tr[0] - tl[0], tr[1] - tl[1], tl[0] - bl[0], tl[1] - bl[1], bl[0], bl[1]]
                content = "q " + " ".join(f"{value:.4f}" for value in matrix) + " cm /ArdRedacted Do Q"
                if "/Annots" in page.obj:
                    annotations_removed += len(page.obj.Annots)
                for key in _PAGE_KEYS_TO_DROP:
                    if key in page.obj:
                        del page.obj[key]
                page.obj.Contents = pdf.make_stream(content.encode("ascii"))
                page.obj.Resources = Dictionary(XObject=Dictionary(ArdRedacted=picture))
        finally:
            document.close()

    warnings = [
        "Redacted pages are now images: their remaining text cannot be selected or searched until OCR is run.",
        "ARD still stores the earlier versions of this document, which contain the original content. "
        "Share only the redacted version you download.",
        "Other pages were not examined. The same information may appear elsewhere in the document.",
    ]
    root = pdf.Root
    if root.get("/Outlines") is not None:
        warnings.append("The document has bookmarks, which were not changed and may contain redacted wording.")
    try:
        has_info = len(pdf.docinfo.keys()) > 0
    except Exception:
        has_info = False
    if has_info or root.get("/Metadata") is not None:
        warnings.append("Document metadata (title, author and similar) was not changed. Use Remove metadata as well.")
    try:
        if len(pdf.attachments):
            warnings.append("The document has file attachments, which were not changed.")
    except Exception:
        pass
    return {"pages": sorted(by_page), "areas": len(areas), "annotations_removed": annotations_removed,
            "warnings": warnings}


def verify(output: str, areas: list[dict[str, Any]]) -> dict[str, Any]:
    """Re-open the saved file and confirm the redaction took effect. Raises RedactionError if not."""
    import pypdfium2 as pdfium

    by_page: dict[int, list[dict[str, Any]]] = {}
    for area in areas:
        by_page.setdefault(area["page"], []).append(area)

    checks = {"text_removed": True, "annotations_removed": True, "areas_black": True}
    with Pdf.open(output) as pdf:
        frames = {number: page_frame(pdf.pages[number - 1]) for number in by_page}
        for number in by_page:
            page = pdf.pages[number - 1]
            if "/Annots" in page.obj:
                checks["annotations_removed"] = False
            xobjects = page.obj.get("/Resources", Dictionary()).get("/XObject", Dictionary())
            if "/Font" in page.obj.get("/Resources", Dictionary()) or list(xobjects.keys()) != ["/ArdRedacted"]:
                checks["text_removed"] = False

    with _pdfium_lock:
        document = pdfium.PdfDocument(output)
        try:
            for number, page_areas in by_page.items():
                page = document[number - 1]
                text_page = page.get_textpage()
                if text_page.count_chars() != 0:
                    checks["text_removed"] = False
                text_page.close()
                image = page.render(scale=1).to_pil().convert("L")
                page.close()
                frame = frames[number]
                sx, sy = image.width / frame.width, image.height / frame.height
                for area in page_areas:
                    corners = [frame.to_display(x, y) for x, y in _corners(area)]
                    cx = sum(p[0] for p in corners) / 4
                    cy = sum(p[1] for p in corners) / 4
                    # Sample the centre and points part-way to each corner.
                    samples = [(cx, cy)] + [(cx + (p[0] - cx) * 0.6, cy + (p[1] - cy) * 0.6) for p in corners]
                    for x, y in samples:
                        px, py = min(image.width - 1, max(0, int(x * sx))), min(image.height - 1, max(0, int(y * sy)))
                        if image.getpixel((px, py)) > 60:
                            checks["areas_black"] = False
        finally:
            document.close()

    if not all(checks.values()):
        failed = ", ".join(name.replace("_", " ") for name, ok in checks.items() if not ok)
        raise RedactionError(f"Redaction could not be verified ({failed}). Nothing was saved.")
    return checks
