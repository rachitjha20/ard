"""OCR (runs in worker processes).

OCRmyPDF adds an invisible text layer to pages that have none; pages that
already have text are left alone, and page images are not re-encoded. A
second, quick Tesseract pass over the same pages yields word confidences,
which is how ARD knows which pages deserve a human look.

OCR is a best guess. The recognised text can be wrong, and a confidence
score is Tesseract's own estimate, not a measure of correctness.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
import threading
from typing import Any

for _noisy in ("ocrmypdf", "pikepdf", "PIL", "fontTools", "pdfminer"):
    logging.getLogger(_noisy).setLevel(logging.ERROR)

from app.workers.pdfium_lock import PDFIUM_LOCK as _pdfium_lock  # noqa: E402
LOW_CONFIDENCE = 70.0
CONFIDENCE_DPI = 150


class OcrError(Exception):
    pass


def _page_texts(path: str) -> list[str]:
    import pypdfium2 as pdfium

    with _pdfium_lock:
        document = pdfium.PdfDocument(path)
        try:
            texts = []
            for index in range(len(document)):
                page = document[index]
                text_page = page.get_textpage()
                texts.append((text_page.get_text_range() or "").strip())
                text_page.close()
                page.close()
            return texts
        finally:
            document.close()


def _confidence(source: str, page_number: int, languages: list[str], workdir: str) -> tuple[float | None, int]:
    """Mean word confidence (0-100) and word count for one page, from Tesseract's own report."""
    import pypdfium2 as pdfium

    image_path = os.path.join(workdir, f"conf-{page_number}.png")
    with _pdfium_lock:
        document = pdfium.PdfDocument(source)
        try:
            page = document[page_number - 1]
            width, height = page.get_size()
            scale = min(CONFIDENCE_DPI / 72, (25_000_000 / max(1.0, width * height)) ** 0.5)
            page.render(scale=scale, grayscale=True).to_pil().save(image_path)
            page.close()
        finally:
            document.close()
    try:
        result = subprocess.run(["tesseract", image_path, "-", "-l", "+".join(languages), "--dpi", str(CONFIDENCE_DPI), "tsv"],
                                capture_output=True, text=True, timeout=180, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None, 0
    finally:
        if os.path.exists(image_path):
            os.remove(image_path)
    total, count = 0.0, 0
    for line in result.stdout.splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) < 12 or not parts[11].strip():
            continue
        try:
            confidence = float(parts[10])
        except ValueError:
            continue
        if confidence >= 0:
            total += confidence
            count += 1
    return (round(total / count, 1) if count else None), count


def run(source: str, output: str, params: dict[str, Any]) -> dict[str, Any]:
    import ocrmypdf
    from ocrmypdf import exceptions as ocr_errors

    languages = list(params.get("languages") or ["eng"])
    before = _page_texts(source)
    part = output + ".part"
    os.makedirs(os.path.dirname(output), exist_ok=True)
    try:
        try:
            # OCRmyPDF may rasterise with PDFium itself, so it runs under the shared lock.
            with _pdfium_lock:
                code = ocrmypdf.ocr(
                    source, part, language=languages, skip_text=True, output_type="pdf", optimize=0,
                    progress_bar=False, jobs=int(params.get("jobs", 1)), use_threads=True, tesseract_timeout=300,
                )
        except ocr_errors.EncryptedPdfError as exc:
            raise OcrError("This PDF is password-protected. Remove the protection before running OCR.") from exc
        except ocr_errors.MissingDependencyError as exc:
            raise OcrError("OCR could not start because a required program or language pack is missing.") from exc
        except ocr_errors.DigitalSignatureError as exc:
            raise OcrError("This PDF is digitally signed. OCR would invalidate the signature, so it was not run.") from exc
        except ocr_errors.ExitCodeException as exc:
            raise OcrError(f"OCR failed ({exc.__class__.__name__}). Nothing was changed.") from exc
        if int(code) != 0 or not os.path.exists(part):
            raise OcrError("OCR did not finish. Nothing was changed.")

        after = _page_texts(part)
        if len(after) != len(before):
            raise OcrError("Output check failed: the page count changed. Nothing was saved.")
        # Verify the text layer by reading it back: a page counts only if text can now be extracted from it.
        recognised = [index + 1 for index, (old, new) in enumerate(zip(before, after)) if len(old) < 12 and len(new) >= 3]
        if not recognised:
            raise OcrError("No text was recognised. The pages may be blank, or in a language that was not selected.")

        pages = []
        with tempfile.TemporaryDirectory(dir=os.path.dirname(output)) as workdir:
            for number in recognised:
                confidence, words = _confidence(source, number, languages, workdir)
                pages.append({"page": number, "confidence": confidence, "words": words,
                              "low": confidence is not None and confidence < LOW_CONFIDENCE})
        os.replace(part, output)
    finally:
        if os.path.exists(part):
            os.remove(part)

    known = [p["confidence"] for p in pages if p["confidence"] is not None]
    low = [p["page"] for p in pages if p["low"]]
    return {
        "languages": languages, "pages_recognised": recognised, "pages": pages,
        "pages_left_alone": len(before) - len(recognised),
        "mean_confidence": round(sum(known) / len(known), 1) if known else None,
        "low_confidence_pages": low,
        "note": "Recognised text is a best guess and can contain mistakes. Confidence is Tesseract's own estimate. "
                + (f"{len(low)} page{'s' if len(low) != 1 else ''} scored below {int(LOW_CONFIDENCE)}% and should be checked. "
                   if low else "")
                + "The page images were not changed; only an invisible text layer was added.",
    }
