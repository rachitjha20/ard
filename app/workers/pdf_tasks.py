"""PDF work that runs in worker processes.

Functions here are pure with respect to ARD: they read the input files they
are given, write only to the output paths they are given, and know nothing
about the database. That keeps untrusted-PDF parsing out of the main process.

Rules every task follows:

* inputs are opened read-only and never modified;
* output is written to ``<name>.part``, re-opened and checked, and only then
  renamed into place, so a failure never leaves a half-written file that
  looks complete;
* embedded JavaScript and actions are never executed (none of the libraries
  used here can execute them).
"""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import threading
from pathlib import Path
from typing import Any

import pikepdf
from pikepdf import Array, Dictionary, Name, Pdf

from app.workers.pdfium_lock import PDFIUM_LOCK as _pdfium_lock  # noqa: E402


class PdfTaskError(Exception):
    """A problem worth showing to the user as-is."""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _open(path: str) -> Pdf:
    try:
        pdf = Pdf.open(path)
    except pikepdf.PasswordError as exc:
        raise PdfTaskError("This PDF is password-protected. ARD cannot change it yet.") from exc
    except pikepdf.PdfError as exc:
        raise PdfTaskError("This PDF is damaged or not a valid PDF file.") from exc
    if pdf.is_encrypted:
        pdf.close()
        raise PdfTaskError("This PDF is encrypted. ARD cannot change encrypted PDFs yet.")
    return pdf


def _save_validated(pdf: Pdf, output: str, expected_pages: int, *, check_password: str = "",
                    **save_kwargs: Any) -> dict[str, Any]:
    part = output + ".part"
    try:
        pdf.save(part, **save_kwargs)
        with Pdf.open(part, password=check_password) as check:
            actual = len(check.pages)
        if actual != expected_pages:
            raise PdfTaskError(f"Output check failed: expected {expected_pages} pages but the file has {actual}.")
        os.replace(part, output)
    finally:
        if os.path.exists(part):
            os.remove(part)
    return {"path": output, "page_count": expected_pages, "size_bytes": os.path.getsize(output),
            "sha256": sha256_file(output)}


def _check_pages(pages: list[int], total: int, *, allow_empty: bool = False) -> None:
    if not pages and not allow_empty:
        raise PdfTaskError("No pages were selected.")
    for number in pages:
        if not isinstance(number, int) or number < 1 or number > total:
            raise PdfTaskError(f"Page {number} does not exist (the document has {total} pages).")


def _select_in_place(pdf: Pdf, order: list[int]) -> int:
    """Rebuild the page tree as ``order`` (1-based, repeats allowed).

    The first use of a page keeps the original page object, so bookmarks that
    point at it keep working. Bookmarks to removed pages are pruned.
    Returns the number of bookmarks removed.
    """
    originals = list(pdf.pages)
    _check_pages(order, len(originals))
    while len(pdf.pages):
        del pdf.pages[-1]
    for number in order:
        pdf.pages.append(originals[number - 1])  # qpdf copies a page that is already in the tree
    kept = {originals[number - 1].obj.objgen for number in set(order)}
    return _prune_outline(pdf, kept)


def _prune_outline(pdf: Pdf, kept: set[tuple[int, int]]) -> int:
    removed = 0
    try:
        with pdf.open_outline() as outline:
            def prune(items: list) -> None:
                nonlocal removed
                for item in list(items):
                    prune(item.children)
                    dest = item.destination
                    target = None
                    if isinstance(dest, Array) and len(dest) and isinstance(dest[0], pikepdf.Object):
                        try:
                            target = dest[0].objgen
                        except Exception:
                            target = None
                    if target is not None and target != (0, 0) and target not in kept and not item.children:
                        items.remove(item)
                        removed += 1

            prune(outline.root)
    except Exception:
        # A malformed outline must not block the page operation itself.
        return removed
    return removed


def _page_size(page: pikepdf.Page) -> tuple[float, float]:
    box = page.mediabox
    return float(box[2]) - float(box[0]), float(box[3]) - float(box[1])


# ---------------------------------------------------------------------------
# inspection and rendering
# ---------------------------------------------------------------------------
def inspect(payload: dict[str, Any]) -> dict[str, Any]:
    """Describe a PDF without changing it. Used at import and for the metadata panel."""
    path = payload["path"]
    info: dict[str, Any] = {
        "valid": True, "encrypted": False, "needs_password": False, "page_count": None, "pdf_version": None,
        "metadata": {}, "has_javascript": False, "has_open_action": False, "attachment_count": 0,
        "has_forms": False, "has_signature_fields": False, "has_outline": False, "first_page_size": None,
    }
    try:
        pdf = Pdf.open(path)
    except pikepdf.PasswordError:
        info.update(encrypted=True, needs_password=True)
        return info
    except pikepdf.PdfError as exc:
        info.update(valid=False, error=str(exc).splitlines()[0][:200] if str(exc) else "Not a valid PDF.")
        return info

    with pdf:
        info["encrypted"] = bool(pdf.is_encrypted)
        info["pdf_version"] = pdf.pdf_version
        info["page_count"] = len(pdf.pages)
        try:
            for key, value in pdf.docinfo.items():
                text = str(value)
                info["metadata"][str(key).lstrip("/")] = text[:500]
        except Exception:
            pass
        root = pdf.Root
        try:
            names = root.get("/Names")
            info["has_javascript"] = bool(names is not None and "/JavaScript" in names)
            open_action = root.get("/OpenAction")
            info["has_open_action"] = open_action is not None
            if isinstance(open_action, Dictionary) and open_action.get("/S") == Name.JavaScript:
                info["has_javascript"] = True
            if root.get("/AA") is not None:
                info["has_open_action"] = True
            info["attachment_count"] = len(pdf.attachments)
            acroform = root.get("/AcroForm")
            if acroform is not None:
                fields = acroform.get("/Fields")
                info["has_forms"] = fields is not None and len(fields) > 0
                info["has_signature_fields"] = bool(int(acroform.get("/SigFlags", 0)) & 1)
            info["has_outline"] = root.get("/Outlines") is not None
            info["has_xmp"] = root.get("/Metadata") is not None
        except Exception:
            info["inspection_incomplete"] = True
        if info["page_count"]:
            width, height = _page_size(pdf.pages[0])
            info["first_page_size"] = [round(width, 2), round(height, 2)]
    return info


def render_thumbnail(payload: dict[str, Any]) -> dict[str, Any]:
    """Rasterise one page to PNG with PDFium."""
    import pypdfium2 as pdfium

    path, output = payload["path"], payload["output"]
    page_number, width = int(payload["page"]), int(payload.get("width", 220))
    with _pdfium_lock:
        document = pdfium.PdfDocument(path)
        try:
            if page_number < 1 or page_number > len(document):
                raise PdfTaskError(f"Page {page_number} does not exist.")
            page = document[page_number - 1]
            page_width = page.get_width() or 612
            scale = max(0.05, min(4.0, width / page_width))
            image = page.render(scale=scale).to_pil().convert("RGB")
        finally:
            document.close()
    part = output + ".part"
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    image.save(part, format="PNG", optimize=True)
    os.replace(part, output)
    return {"path": output, "width": image.width, "height": image.height}


# ---------------------------------------------------------------------------
# operations that produce one new file from one document
# ---------------------------------------------------------------------------
def _op_select(pdf: Pdf, params: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    order = list(params["order"])
    removed = _select_in_place(pdf, order)
    return len(order), {"bookmarks_removed": removed}


def _op_rotate(pdf: Pdf, params: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    degrees = int(params["degrees"])
    if degrees % 90 != 0:
        raise PdfTaskError("Rotation must be a multiple of 90 degrees.")
    pages = list(params.get("pages") or range(1, len(pdf.pages) + 1))
    _check_pages(pages, len(pdf.pages))
    for number in set(pages):
        pdf.pages[number - 1].rotate(degrees, relative=True)
    return len(pdf.pages), {"rotated": len(set(pages))}


def _op_crop(pdf: Pdf, params: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    pages = list(params.get("pages") or range(1, len(pdf.pages) + 1))
    _check_pages(pages, len(pdf.pages))
    left, bottom, right, top = (float(params.get(k, 0)) for k in ("left", "bottom", "right", "top"))
    if min(left, bottom, right, top) < 0:
        raise PdfTaskError("Crop margins cannot be negative.")
    for number in set(pages):
        page = pdf.pages[number - 1]
        box = [float(v) for v in page.mediabox]
        new = [box[0] + left, box[1] + bottom, box[2] - right, box[3] - top]
        if new[2] - new[0] < 10 or new[3] - new[1] < 10:
            raise PdfTaskError(f"Crop margins leave nothing visible on page {number}.")
        # Cropping hides content outside the box; it does not remove it from the file.
        page.cropbox = new
    return len(pdf.pages), {"cropped": len(set(pages)),
                            "note": "Cropping hides the outer area. The hidden content is still in the file."}


def _op_insert_blank(pdf: Pdf, params: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    total = len(pdf.pages)
    after = int(params.get("after", total))
    count = int(params.get("count", 1))
    if after < 0 or after > total:
        raise PdfTaskError(f"Cannot insert after page {after}.")
    if count < 1 or count > 500:
        raise PdfTaskError("Insert between 1 and 500 blank pages.")
    reference = pdf.pages[max(0, min(total - 1, after - 1))] if total else None
    size = _page_size(reference) if reference is not None else (595.0, 842.0)
    if params.get("width") and params.get("height"):
        size = (float(params["width"]), float(params["height"]))
    for offset in range(count):
        pdf.add_blank_page(page_size=size)
        page = pdf.pages[-1]
        del pdf.pages[-1]
        pdf.pages.insert(after + offset, page)
    return total + count, {"inserted": count}


def _op_strip_metadata(pdf: Pdf, params: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    removed: list[str] = []
    try:
        removed.extend(str(key).lstrip("/") for key in pdf.docinfo.keys())
    except Exception:
        pass
    if "/Info" in pdf.trailer:
        del pdf.trailer["/Info"]
    if "/Metadata" in pdf.Root:
        del pdf.Root["/Metadata"]
        removed.append("XMP")
    return len(pdf.pages), {
        "removed_fields": removed,
        "note": "Document-level metadata was removed. Metadata inside embedded images or attachments is not touched.",
    }


# Compression presets. "quality" only restructures the file and is lossless.
COMPRESSION_PRESETS: dict[str, dict[str, Any]] = {
    "quality": {"label": "Maximum quality", "lossy": False, "max_dim": None, "jpeg_quality": None},
    "balanced": {"label": "Balanced", "lossy": True, "max_dim": 2000, "jpeg_quality": 75},
    "smaller": {"label": "Smaller file", "lossy": True, "max_dim": 1400, "jpeg_quality": 55},
}


def _recompress_images(pdf: Pdf, max_dim: int, quality: int) -> dict[str, int]:
    """Downsample and re-encode ordinary RGB/greyscale images as JPEG.

    Deliberately conservative: anything unusual (masks, CMYK, indexed colour,
    1-bit scans, custom decode arrays) is left exactly as it is, and an image
    is only replaced when the result is actually smaller.
    """
    from PIL import Image

    stats = {"images_seen": 0, "images_recompressed": 0, "images_skipped": 0, "bytes_saved": 0}
    seen: set[tuple[int, int]] = set()
    for page in pdf.pages:
        try:
            images = page.get_images()   # also finds images nested in form XObjects
        except Exception:
            continue
        for raw in (images.values() if hasattr(images, "values") else images):
            if raw.objgen in seen:
                continue
            seen.add(raw.objgen)
            stats["images_seen"] += 1
            try:
                if raw.get("/ImageMask") or "/Mask" in raw or "/Decode" in raw:
                    raise ValueError("unsupported image feature")
                if int(raw.get("/BitsPerComponent", 8)) != 8:
                    raise ValueError("not 8-bit")
                image = pikepdf.PdfImage(raw)
                if image.mode not in ("RGB", "L"):
                    raise ValueError("unsupported colour mode")
                original_size = len(raw.read_raw_bytes())
                pil = image.as_pil_image()
                if pil.mode not in ("RGB", "L"):
                    raise ValueError("unsupported colour mode")
                if max(pil.size) > max_dim:
                    ratio = max_dim / max(pil.size)
                    pil = pil.resize((max(1, round(pil.width * ratio)), max(1, round(pil.height * ratio))),
                                     Image.LANCZOS)
                buffer = io.BytesIO()
                pil.save(buffer, format="JPEG", quality=quality, optimize=True)
                data = buffer.getvalue()
                if len(data) >= original_size * 0.95:
                    stats["images_skipped"] += 1
                    continue
                raw.write(data, filter=Name.DCTDecode)
                raw.Width, raw.Height = pil.width, pil.height
                raw.ColorSpace = Name.DeviceRGB if pil.mode == "RGB" else Name.DeviceGray
                raw.BitsPerComponent = 8
                if "/DecodeParms" in raw:
                    del raw["/DecodeParms"]
                stats["images_recompressed"] += 1
                stats["bytes_saved"] += original_size - len(data)
            except Exception:
                stats["images_skipped"] += 1
    return stats


def _run_compress(source: str, output: str, params: dict[str, Any]) -> dict[str, Any]:
    preset_name = params.get("preset", "balanced")
    preset = COMPRESSION_PRESETS.get(preset_name)
    if preset is None:
        raise PdfTaskError(f"Unknown compression preset: {preset_name}")
    with _open(source) as pdf:
        expected = len(pdf.pages)
        report: dict[str, Any] = {"preset": preset_name, "lossy": preset["lossy"]}
        if preset["lossy"]:
            report.update(_recompress_images(pdf, preset["max_dim"], preset["jpeg_quality"]))
        pdf.remove_unreferenced_resources()
        result = _save_validated(
            pdf, output, expected, compress_streams=True, recompress_flate=True,
            object_stream_mode=pikepdf.ObjectStreamMode.generate,
        )
    before = os.path.getsize(source)
    report.update(size_before=before, size_after=result["size_bytes"],
                  saved_bytes=before - result["size_bytes"],
                  saved_percent=round(100 * (before - result["size_bytes"]) / before, 1) if before else 0.0)
    if result["size_bytes"] >= before:
        report["note"] = "The file could not be made smaller with this preset."
    result["report"] = report
    return result


def _op_annotate(pdf: Pdf, params: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    from app.workers import pdf_annotate

    try:
        return pdf_annotate.apply_items(pdf, params)
    except pdf_annotate.AnnotateError as exc:
        raise PdfTaskError(str(exc)) from exc


def _op_remove_annotations(pdf: Pdf, params: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    from app.workers import pdf_annotate

    try:
        return pdf_annotate.remove_annotations(pdf, params)
    except pdf_annotate.AnnotateError as exc:
        raise PdfTaskError(str(exc)) from exc


def _op_watermark(pdf: Pdf, params: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    from app.workers import pdf_decorate

    return pdf_decorate.watermark(pdf, params)


def _op_header_footer(pdf: Pdf, params: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    from app.workers import pdf_decorate

    try:
        return pdf_decorate.header_footer(pdf, params)
    except ValueError as exc:
        raise PdfTaskError(str(exc)) from exc


PERMISSION_NAMES = ("print", "copy", "modify", "annotate", "fill_forms")


def _run_protect(source: str, output: str, params: dict[str, Any], secrets: dict[str, str]) -> dict[str, Any]:
    """Encrypt with AES-256. Passwords arrive in memory only and are never written to the job record."""
    owner, user = secrets.get("owner_password") or "", secrets.get("user_password") or ""
    if not owner:
        raise PdfTaskError("The passwords for this job are no longer available. Start the operation again.")
    allowed = params.get("permissions") or {}
    permissions = pikepdf.Permissions(
        accessibility=True,                                   # never block assistive technology
        extract=bool(allowed.get("copy", True)),
        print_lowres=bool(allowed.get("print", True)),
        print_highres=bool(allowed.get("print", True)),
        modify_other=bool(allowed.get("modify", True)),
        modify_assembly=bool(allowed.get("modify", True)),
        modify_annotation=bool(allowed.get("annotate", True)),
        modify_form=bool(allowed.get("fill_forms", True)),
    )
    with _open(source) as pdf:
        expected = len(pdf.pages)
        result = _save_validated(pdf, output, expected, check_password=owner,
                                 encryption=pikepdf.Encryption(owner=owner, user=user, R=6, allow=permissions))
    with Pdf.open(output, password=owner) as check:
        if not check.is_encrypted or check.encryption.bits != 256:
            os.remove(output)
            raise PdfTaskError("Output check failed: the file was not encrypted as requested.")
    result["encrypted"] = True
    result["report"] = {
        "encryption": "AES-256", "needs_password_to_open": bool(user),
        "permissions": {name: bool(allowed.get(name, True)) for name in PERMISSION_NAMES},
        "note": "Permission settings are requests that PDF software is expected to honour; they are not "
                "enforced by encryption. Only the password to open actually keeps content private. "
                "ARD does not store the passwords and cannot recover them.",
    }
    return result


def _run_unlock(source: str, output: str, secrets: dict[str, str]) -> dict[str, Any]:
    """Write an unencrypted copy. Requires the owner password, so restrictions are not bypassed."""
    if "password" not in secrets:
        raise PdfTaskError("The password for this job is no longer available. Start the operation again.")
    try:
        pdf = Pdf.open(source, password=secrets["password"])
    except pikepdf.PasswordError as exc:
        raise PdfTaskError("That password is not correct.") from exc
    except pikepdf.PdfError as exc:
        raise PdfTaskError("This PDF is damaged or not a valid PDF file.") from exc
    with pdf:
        if not pdf.is_encrypted:
            raise PdfTaskError("This document is not password-protected.")
        if not pdf.owner_password_matched:
            raise PdfTaskError("That password opens the document but is not its owner (permissions) password, "
                               "so ARD will not remove the protection.")
        expected = len(pdf.pages)
        result = _save_validated(pdf, output, expected)
    with Pdf.open(output) as check:
        if check.is_encrypted:
            os.remove(output)
            raise PdfTaskError("Output check failed: the file is still encrypted.")
    result["encrypted"] = False
    result["report"] = {"note": "This version is not password-protected. Earlier versions still are."}
    return result


def _run_sign(source: str, output: str, params: dict[str, Any], secrets: dict[str, str]) -> dict[str, Any]:
    from app.workers import pdf_sign

    try:
        report = pdf_sign.sign(source, output, params, secrets)
    except pdf_sign.SignError as exc:
        raise PdfTaskError(str(exc)) from exc
    with Pdf.open(output) as check:
        pages = len(check.pages)
    return {"path": output, "page_count": pages, "size_bytes": os.path.getsize(output), "sha256": sha256_file(output),
            "signed": True, "report": report}


def _op_fill_form(pdf: Pdf, params: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    from app.workers import pdf_forms

    try:
        return pdf_forms.fill(pdf, params)
    except pdf_forms.FormError as exc:
        raise PdfTaskError(str(exc)) from exc


def _run_redact(source: str, output: str, params: dict[str, Any]) -> dict[str, Any]:
    from app.workers import pdf_redact

    areas = params["areas"]
    try:
        with _open(source) as pdf:
            expected = len(pdf.pages)
            report = pdf_redact.apply(pdf, source, areas)
            pdf.remove_unreferenced_resources()
            result = _save_validated(pdf, output, expected)
        try:
            report["checks"] = pdf_redact.verify(output, areas)
        except Exception:
            # An unverified redaction must never be presented as done.
            if os.path.exists(output):
                os.remove(output)
            raise
    except pdf_redact.RedactionError as exc:
        raise PdfTaskError(str(exc)) from exc
    report["verified"] = True
    result["report"] = report
    return result


def export(payload: dict[str, Any]) -> dict[str, Any]:
    """Write a text file or page images for a document. Conversion is one-way and loses information."""
    import zipfile

    source, output, fmt = payload["path"], payload["output"], payload["format"]
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    part = output + ".part"
    try:
        if fmt == "txt":
            import pypdf

            reader = pypdf.PdfReader(source)
            if reader.is_encrypted:
                raise PdfTaskError("This PDF is password-protected. Remove the protection before exporting.")
            empty = 0
            with open(part, "w", encoding="utf-8", newline="\n") as handle:
                for number, page in enumerate(reader.pages, start=1):
                    try:
                        text = (page.extract_text() or "").strip()
                    except Exception:
                        text = ""
                    if not text:
                        empty += 1
                    handle.write(f"--- Page {number} ---\n{text}\n\n")
            report = {"pages": len(reader.pages), "pages_without_text": empty}
        else:
            import pypdfium2 as pdfium

            scale = float(payload.get("dpi", 150)) / 72
            with _pdfium_lock:
                try:
                    document = pdfium.PdfDocument(source)
                except pdfium.PdfiumError as exc:
                    raise PdfTaskError("This PDF could not be opened for export. It may be password-protected.") from exc
                try:
                    count = len(document)
                    if count > int(payload.get("max_pages", 300)):
                        raise PdfTaskError(f"Image export is limited to {payload.get('max_pages', 300)} pages at a time.")
                    digits = len(str(count))
                    with zipfile.ZipFile(part, "w", zipfile.ZIP_STORED) as archive:
                        for index in range(count):
                            page = document[index]
                            width, height = page.get_size()
                            factor = min(scale, (40_000_000 / max(1.0, width * height)) ** 0.5)
                            image = page.render(scale=factor).to_pil().convert("RGB")
                            page.close()
                            buffer = io.BytesIO()
                            if fmt == "jpg":
                                image.save(buffer, format="JPEG", quality=88)
                            else:
                                image.save(buffer, format="PNG")
                            archive.writestr(f"page-{str(index + 1).zfill(digits)}.{fmt}", buffer.getvalue())
                finally:
                    document.close()
            report = {"pages": count}
        os.replace(part, output)
    finally:
        if os.path.exists(part):
            os.remove(part)
    return {"path": output, "report": report}


_SINGLE_OPS = {
    "fill_form": _op_fill_form,
    "watermark": _op_watermark,
    "header_footer": _op_header_footer,
    "annotate": _op_annotate,
    "remove_annotations": _op_remove_annotations,
    "select": _op_select,
    "rotate": _op_rotate,
    "crop": _op_crop,
    "insert_blank": _op_insert_blank,
    "strip_metadata": _op_strip_metadata,
}


def transform(payload: dict[str, Any]) -> dict[str, Any]:
    """Apply one operation to one document, producing one new file.

    payload: ``{"op", "source", "output", "params", "extra_source"?}``
    """
    op, source, output = payload["op"], payload["source"], payload["output"]
    params = payload.get("params") or {}
    Path(output).parent.mkdir(parents=True, exist_ok=True)

    if op == "compress":
        return {"outputs": [_run_compress(source, output, params)]}

    if op == "protect":
        return {"outputs": [_run_protect(source, output, params, payload.get("secrets") or {})]}

    if op == "unlock":
        return {"outputs": [_run_unlock(source, output, payload.get("secrets") or {})]}

    if op == "redact":
        return {"outputs": [_run_redact(source, output, params)]}

    if op == "ocr":
        from app.workers import pdf_ocr

        try:
            report = pdf_ocr.run(source, output, params)
        except pdf_ocr.OcrError as exc:
            raise PdfTaskError(str(exc)) from exc
        with Pdf.open(output) as check:
            pages = len(check.pages)
        return {"outputs": [{"path": output, "page_count": pages, "size_bytes": os.path.getsize(output),
                             "sha256": sha256_file(output), "report": report}]}

    if op == "sign":
        return {"outputs": [_run_sign(source, output, params, payload.get("secrets") or {})]}

    if op == "copy":
        part = output + ".part"
        shutil.copyfile(source, part)
        try:
            try:
                with Pdf.open(part) as check:
                    pages, encrypted = len(check.pages), bool(check.is_encrypted)
            except pikepdf.PasswordError:
                pages, encrypted = None, True       # a protected version is copied as it is
            os.replace(part, output)
        finally:
            if os.path.exists(part):
                os.remove(part)
        return {"outputs": [{"path": output, "page_count": pages, "size_bytes": os.path.getsize(output),
                             "sha256": sha256_file(output), "encrypted": encrypted, "report": {}}]}

    if op == "insert_pages":
        with _open(source) as pdf, _open(payload["extra_source"]) as other:
            total = len(pdf.pages)
            after = int(params.get("after", total))
            if after < 0 or after > total:
                raise PdfTaskError(f"Cannot insert after page {after}.")
            pages = list(params.get("pages") or range(1, len(other.pages) + 1))
            _check_pages(pages, len(other.pages))
            for offset, number in enumerate(pages):
                pdf.pages.insert(after + offset, other.pages[number - 1])
            result = _save_validated(pdf, output, total + len(pages))
        result["report"] = {"inserted": len(pages)}
        return {"outputs": [result]}

    handler = _SINGLE_OPS.get(op)
    if handler is None:
        raise PdfTaskError(f"Unknown operation: {op}")
    with _open(source) as pdf:
        expected, report = handler(pdf, params)
        result = _save_validated(pdf, output, expected)
    result["report"] = report
    return {"outputs": [result]}


# ---------------------------------------------------------------------------
# operations that produce new documents
# ---------------------------------------------------------------------------
def merge(payload: dict[str, Any]) -> dict[str, Any]:
    """Join several documents, in the order given, into one new file."""
    sources, output = payload["sources"], payload["output"]
    if len(sources) < 2:
        raise PdfTaskError("Choose at least two documents to merge.")
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    merged = Pdf.new()
    expected = 0
    opened: list[Pdf] = []
    try:
        for source in sources:
            pdf = _open(source)
            opened.append(pdf)
            merged.pages.extend(pdf.pages)
            expected += len(pdf.pages)
        result = _save_validated(merged, output, expected)
    finally:
        for pdf in opened:
            pdf.close()
        merged.close()
    result["report"] = {"sources": len(sources),
                        "note": "Bookmarks and form fields from the source documents are not carried over."}
    return {"outputs": [result]}


def split(payload: dict[str, Any]) -> dict[str, Any]:
    """Write one new file per page group. ``groups`` is a list of 1-based page lists."""
    source, outputs, groups = payload["source"], payload["outputs"], payload["groups"]
    if len(outputs) != len(groups) or not groups:
        raise PdfTaskError("Nothing to split.")
    results = []
    written: list[str] = []
    try:
        for output, group in zip(outputs, groups):
            Path(output).parent.mkdir(parents=True, exist_ok=True)
            with _open(source) as pdf:
                removed = _select_in_place(pdf, list(group))
                result = _save_validated(pdf, output, len(group))
            result["report"] = {"pages": list(group), "bookmarks_removed": removed}
            results.append(result)
            written.append(output)
    except Exception:
        # All or nothing: do not leave some parts behind looking like a finished split.
        for path in written:
            if os.path.exists(path):
                os.remove(path)
        raise
    return {"outputs": results}
