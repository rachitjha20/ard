"""ARD's own catalogue of page operations.

This module is engine-agnostic. It validates what the user asked for and
translates it into a small set of primitive worker operations. The PDF library
behind those primitives can be replaced without touching this file.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class OperationError(ValueError):
    """The request cannot be carried out as asked. The message is shown to the user."""


@dataclass(frozen=True)
class Plan:
    worker_op: str
    params: dict[str, Any]
    summary: str
    needs_document: str | None = None  # id of a second document, for insert_pages


OPERATIONS: dict[str, str] = {
    "delete_pages": "Delete pages",
    "reorder": "Reorder pages",
    "duplicate_pages": "Duplicate pages",
    "reverse": "Reverse page order",
    "rotate": "Rotate pages",
    "crop": "Crop pages",
    "insert_blank": "Insert blank pages",
    "insert_pages": "Insert pages from another document",
    "strip_metadata": "Remove metadata",
    "compress": "Compress",
    "annotate": "Annotate",
    "watermark": "Watermark",
    "header_footer": "Headers and footers",
    "redact": "Redact",
    "protect": "Password-protect",
    "unlock": "Remove password protection",
    "revert": "Revert",
    "fill_form": "Fill form",
    "ocr": "Recognise text (OCR)",
    "sign": "Sign",
    "remove_annotations": "Remove annotations",
}


def parse_page_ranges(text: str, total: int) -> list[int]:
    """``"1-3, 5, 8-"`` -> ``[1, 2, 3, 5, 8, ..., total]``. Order and repeats are kept."""
    pages: list[int] = []
    for part in str(text).replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start_text, end_text = (piece.strip() for piece in part.split("-", 1))
            try:
                start = int(start_text) if start_text else 1
                end = int(end_text) if end_text else total
            except ValueError as exc:
                raise OperationError(f"'{part}' is not a page range.") from exc
            if start > end:
                raise OperationError(f"Range '{part}' runs backwards.")
            pages.extend(range(start, end + 1))
        else:
            try:
                pages.append(int(part))
            except ValueError as exc:
                raise OperationError(f"'{part}' is not a page number.") from exc
    _require_pages(pages, total)
    return pages


def _require_pages(pages: Any, total: int, *, allow_empty: bool = False) -> list[int]:
    if isinstance(pages, str):
        return parse_page_ranges(pages, total)
    if not isinstance(pages, list) or any(not isinstance(p, int) or isinstance(p, bool) for p in pages):
        raise OperationError("Pages must be a list of page numbers.")
    if not pages and not allow_empty:
        raise OperationError("Select at least one page.")
    for page in pages:
        if page < 1 or page > total:
            raise OperationError(f"Page {page} does not exist (the document has {total} pages).")
    return pages


def _plural(count: int, word: str = "page") -> str:
    return f"{count} {word}" + ("" if count == 1 else "s")


def plan_operation(operation: str, params: dict[str, Any], total: int) -> Plan:
    """Validate ``operation`` against a document with ``total`` pages."""
    params = params or {}

    if operation == "delete_pages":
        doomed = set(_require_pages(params.get("pages"), total))
        order = [n for n in range(1, total + 1) if n not in doomed]
        if not order:
            raise OperationError("A document needs at least one page. Move it to the trash instead.")
        return Plan("select", {"order": order}, f"Deleted {_plural(len(doomed))}")

    if operation == "reorder":
        order = _require_pages(params.get("order"), total)
        if sorted(order) != list(range(1, total + 1)):
            raise OperationError("The new order must contain every page exactly once.")
        return Plan("select", {"order": order}, "Reordered pages")

    if operation == "duplicate_pages":
        chosen = set(_require_pages(params.get("pages"), total))
        order = [n for page in range(1, total + 1) for n in ((page, page) if page in chosen else (page,))]
        return Plan("select", {"order": order}, f"Duplicated {_plural(len(chosen))}")

    if operation == "reverse":
        return Plan("select", {"order": list(range(total, 0, -1))}, "Reversed page order")

    if operation == "rotate":
        try:
            degrees = int(params.get("degrees", 90))
        except (TypeError, ValueError) as exc:
            raise OperationError("Rotation must be a number of degrees.") from exc
        if degrees % 90 != 0 or degrees % 360 == 0:
            raise OperationError("Rotate by 90, 180 or 270 degrees.")
        pages = _require_pages(params.get("pages") or list(range(1, total + 1)), total)
        return Plan("rotate", {"pages": sorted(set(pages)), "degrees": degrees % 360},
                    f"Rotated {_plural(len(set(pages)))} by {degrees % 360}°")

    if operation == "crop":
        pages = _require_pages(params.get("pages") or list(range(1, total + 1)), total)
        margins = {}
        for side in ("left", "bottom", "right", "top"):
            try:
                margins[side] = float(params.get(side, 0) or 0)
            except (TypeError, ValueError) as exc:
                raise OperationError(f"The {side} margin must be a number.") from exc
            if margins[side] < 0:
                raise OperationError("Crop margins cannot be negative.")
        if not any(margins.values()):
            raise OperationError("Give at least one crop margin.")
        return Plan("crop", {"pages": sorted(set(pages)), **margins}, f"Cropped {_plural(len(set(pages)))}")

    if operation == "insert_blank":
        try:
            after = int(params.get("after", total))
            count = int(params.get("count", 1))
        except (TypeError, ValueError) as exc:
            raise OperationError("Position and count must be whole numbers.") from exc
        if after < 0 or after > total:
            raise OperationError(f"Cannot insert after page {after}.")
        if count < 1 or count > 500:
            raise OperationError("Insert between 1 and 500 blank pages.")
        return Plan("insert_blank", {"after": after, "count": count}, f"Inserted {_plural(count, 'blank page')}")

    if operation == "insert_pages":
        other = params.get("from_document_id")
        if not isinstance(other, str) or not other:
            raise OperationError("Choose the document to take pages from.")
        try:
            after = int(params.get("after", total))
        except (TypeError, ValueError) as exc:
            raise OperationError("Position must be a whole number.") from exc
        if after < 0 or after > total:
            raise OperationError(f"Cannot insert after page {after}.")
        # Page numbers of the other document are checked by the service, which knows its length.
        return Plan("insert_pages", {"after": after, "pages": params.get("pages")}, "Inserted pages",
                    needs_document=other)

    if operation == "strip_metadata":
        return Plan("strip_metadata", {}, "Removed document metadata")

    if operation == "compress":
        preset = params.get("preset", "balanced")
        if preset not in ("quality", "balanced", "smaller"):
            raise OperationError("Choose a compression preset: quality, balanced or smaller.")
        return Plan("compress", {"preset": preset}, f"Compressed ({preset})")

    if operation == "watermark":
        text = _latin_text(params.get("text"), "The watermark text", 60).strip()
        plan_params = {"text": text, "color": _color(params.get("color"), "#b3261e"),
                       "opacity": _number(params.get("opacity", 0.25), "Opacity", 0.05, 1.0),
                       "angle": _number(params.get("angle", 45), "Angle", -90, 90)}
        if params.get("size"):
            plan_params["size"] = _number(params["size"], "Text size", 6, 300)
        if params.get("pages"):
            plan_params["pages"] = sorted(set(_require_pages(params["pages"], total)))
        return Plan("watermark", plan_params, "Added a watermark")

    if operation == "header_footer":
        plan_params = {"size": _number(params.get("size", 10), "Text size", 5, 48),
                       "margin": _number(params.get("margin", 28), "Margin", 4, 200),
                       "color": _color(params.get("color"), "#333333")}
        for key, low in (("first_page", 1), ("number_from", 0)):
            value = params.get(key, 1)
            if isinstance(value, bool) or not isinstance(value, int) or value < low or value > 100_000:
                raise OperationError("Page numbering options must be whole numbers.")
            plan_params[key] = value
        if plan_params["first_page"] > total:
            raise OperationError(f"The document has only {total} pages.")
        any_text = False
        for band in ("header", "footer"):
            slots = params.get(band) or {}
            if not isinstance(slots, dict) or set(slots) - {"left", "center", "right"}:
                raise OperationError("Headers and footers have left, center and right fields.")
            cleaned = {slot: _latin_text(text, "Header/footer text", 200, required=False).replace("\n", " ")
                       for slot, text in slots.items()}
            plan_params[band] = {slot: text for slot, text in cleaned.items() if text.strip()}
            any_text = any_text or bool(plan_params[band])
        if not any_text:
            raise OperationError("Fill in at least one header or footer field.")
        return Plan("header_footer", plan_params, "Added headers and footers")

    if operation == "remove_annotations":
        scope = params.get("scope", "all")
        if scope not in ("all", "ard"):
            raise OperationError("Scope must be 'all' or 'ard'.")
        plan_params: dict[str, Any] = {"scope": scope}
        if params.get("pages"):
            plan_params["pages"] = sorted(set(_require_pages(params["pages"], total)))
        return Plan("remove_annotations", plan_params,
                    "Removed annotations added by ARD" if scope == "ard" else "Removed annotations")

    raise OperationError(f"Unknown operation: {operation}")


def plan_split(params: dict[str, Any], total: int, max_parts: int = 500) -> list[list[int]]:
    """Return the page groups a split should produce."""
    mode = (params or {}).get("mode", "ranges")
    if mode == "every":
        try:
            size = int(params.get("n", 1))
        except (TypeError, ValueError) as exc:
            raise OperationError("Give the number of pages per part.") from exc
        if size < 1:
            raise OperationError("Each part needs at least one page.")
        groups = [list(range(start, min(start + size, total + 1))) for start in range(1, total + 1, size)]
    elif mode == "ranges":
        text = str(params.get("ranges", "")).strip()
        if not text:
            raise OperationError("Give page ranges, for example 1-3, 4-6.")
        groups = [parse_page_ranges(part, total) for part in text.replace(";", ",").split(",") if part.strip()]
    elif mode == "pages":
        groups = [_require_pages(params.get("pages"), total)]
    else:
        raise OperationError(f"Unknown split mode: {mode}")
    groups = [group for group in groups if group]
    if not groups:
        raise OperationError("Nothing to split.")
    if len(groups) > max_parts:
        raise OperationError(f"That would create {len(groups)} documents; the limit is {max_parts}.")
    return groups


# ---------------------------------------------------------------------------
# overlays and annotations
# ---------------------------------------------------------------------------
OVERLAY_TYPES = ("text", "image", "stamp")
ANNOTATION_TYPES = ("highlight", "note", "rect", "ellipse", "line", "arrow", "ink")
MAX_ITEMS = 2000
MAX_POINTS = 200_000
_COORD_LIMIT = 20_000.0
_HEX = set("0123456789abcdefABCDEF")
_STAMP_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 -")


def _number(value: Any, label: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise OperationError(f"{label} must be a number.")
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")) or number < low or number > high:
        raise OperationError(f"{label} is out of range.")
    return number


def _point(value: Any, label: str = "A coordinate") -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise OperationError(f"{label} must be an [x, y] pair.")
    return [_number(value[0], label, -_COORD_LIMIT, _COORD_LIMIT), _number(value[1], label, -_COORD_LIMIT, _COORD_LIMIT)]


def _color(value: Any, default: str | None = None) -> str:
    if value is None and default is not None:
        return default
    if not isinstance(value, str) or len(value) != 7 or value[0] != "#" or any(c not in _HEX for c in value[1:]):
        raise OperationError("Colours must look like #rrggbb.")
    return value.lower()


def _three_corner_box(value: Any) -> dict[str, list[float]]:
    if not isinstance(value, dict):
        raise OperationError("A box needs its top-left, top-right and bottom-left corners.")
    box = {key: _point(value.get(key), "A box corner") for key in ("tl", "tr", "bl")}
    width = ((box["tr"][0] - box["tl"][0]) ** 2 + (box["tr"][1] - box["tl"][1]) ** 2) ** 0.5
    height = ((box["bl"][0] - box["tl"][0]) ** 2 + (box["bl"][1] - box["tl"][1]) ** 2) ** 0.5
    if width < 1 or height < 1:
        raise OperationError("That item is too small to place.")
    return box


def _text(value: Any, label: str, limit: int, *, required: bool = True) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise OperationError(f"{label} must be text.")
    text = value.replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(ch for ch in text if ch == "\n" or ch == "\t" or ch.isprintable()).replace("\t", "    ")
    if required and not text.strip():
        raise OperationError(f"{label} is empty.")
    if len(text) > limit:
        raise OperationError(f"{label} is longer than {limit} characters.")
    return text


def _latin_text(value: Any, label: str, limit: int, *, required: bool = True) -> str:
    text = _text(value, label, limit, required=required)
    try:
        text.encode("cp1252")
    except UnicodeEncodeError as exc:
        raise OperationError(
            f"{label} contains characters the built-in PDF fonts cannot encode. Only Latin text is supported here for now."
        ) from exc
    return text


def plan_redaction(areas: Any, total: int) -> list[dict[str, Any]]:
    """Validate redaction areas: a page number and a three-corner box each."""
    if not isinstance(areas, list) or not areas:
        raise OperationError("Mark at least one area to redact.")
    if len(areas) > 1000:
        raise OperationError("Redact at most 1000 areas at a time.")
    clean = []
    for raw in areas:
        if not isinstance(raw, dict):
            raise OperationError("Each area must be an object.")
        page = raw.get("page")
        if isinstance(page, bool) or not isinstance(page, int) or page < 1 or page > total:
            raise OperationError(f"Page {page} does not exist (the document has {total} pages).")
        clean.append({"page": page, "box": _three_corner_box(raw.get("box"))})
    return clean


def plan_protection(owner_password: Any, user_password: Any, permissions: Any) -> dict[str, bool]:
    for label, value, required in (("The permissions password", owner_password, True),
                                   ("The password to open", user_password, False)):
        if value is None and not required:
            continue
        if not isinstance(value, str) or (required and not value):
            raise OperationError(f"{label} is required.")
        if len(value) > 127 or any(not ch.isprintable() for ch in value):
            raise OperationError(f"{label} must be at most 127 printable characters.")
    if isinstance(owner_password, str) and len(owner_password) < 6:
        raise OperationError("Use a permissions password of at least 6 characters.")
    names = ("print", "copy", "modify", "annotate", "fill_forms")
    if permissions is None:
        permissions = {}
    if not isinstance(permissions, dict) or set(permissions) - set(names):
        raise OperationError("Unknown permission.")
    return {name: bool(permissions.get(name, True)) for name in names}


def plan_annotations(items: Any, total: int, image_count: int = 0) -> list[dict[str, Any]]:
    """Validate and normalise overlay/annotation items. Nothing unvalidated reaches the PDF writer."""
    if not isinstance(items, list) or not items:
        raise OperationError("There is nothing to add.")
    if len(items) > MAX_ITEMS:
        raise OperationError(f"Add at most {MAX_ITEMS} items at a time.")
    clean: list[dict[str, Any]] = []
    points = 0
    for raw in items:
        if not isinstance(raw, dict):
            raise OperationError("Each item must be an object.")
        kind = raw.get("type")
        if kind not in OVERLAY_TYPES + ANNOTATION_TYPES:
            raise OperationError(f"Unknown item type: {kind}")
        page = raw.get("page")
        if isinstance(page, bool) or not isinstance(page, int) or page < 1 or page > total:
            raise OperationError(f"Page {page} does not exist (the document has {total} pages).")
        item: dict[str, Any] = {"type": kind, "page": page}
        if "opacity" in raw:
            item["opacity"] = _number(raw["opacity"], "Opacity", 0.05, 1.0)

        if kind == "text":
            item["box"] = _three_corner_box(raw.get("box"))
            item["text"] = _text(raw.get("text"), "The text", 5000)
            try:
                item["text"].encode("cp1252")
            except UnicodeEncodeError as exc:
                raise OperationError(
                    "This text contains characters the built-in PDF fonts cannot encode. "
                    "Add it as an image instead (ARD does this automatically in the editor)."
                ) from exc
            item["size"] = _number(raw.get("size", 12), "Text size", 4, 400)
            item["font"] = raw.get("font", "sans")
            if item["font"] not in ("sans", "serif", "mono"):
                raise OperationError("Font must be sans, serif or mono.")
            item["bold"] = bool(raw.get("bold", False))
            item["color"] = _color(raw.get("color"), "#000000")
        elif kind == "image":
            item["box"] = _three_corner_box(raw.get("box"))
            index = raw.get("image")
            if isinstance(index, bool) or not isinstance(index, int) or index < 0 or index >= image_count:
                raise OperationError("An image item refers to an image that was not uploaded.")
            item["image"] = index
        elif kind == "stamp":
            item["box"] = _three_corner_box(raw.get("box"))
            item["text"] = _text(raw.get("text"), "The stamp text", 40).strip().upper()
            if set(item["text"]) - _STAMP_CHARS:
                raise OperationError("Stamp text may contain only letters A-Z, digits, spaces and hyphens.")
            item["color"] = _color(raw.get("color"), "#b3261e")
        elif kind in ("rect", "ellipse"):
            item["box"] = _three_corner_box(raw.get("box"))
            item["color"] = _color(raw.get("color"), "#b3261e")
            item["width"] = _number(raw.get("width", 2), "Line width", 0.25, 40)
            if raw.get("fill"):
                item["fill"] = _color(raw["fill"])
        elif kind in ("line", "arrow"):
            item["p1"], item["p2"] = _point(raw.get("p1")), _point(raw.get("p2"))
            if abs(item["p1"][0] - item["p2"][0]) + abs(item["p1"][1] - item["p2"][1]) < 1:
                raise OperationError("That line is too short to place.")
            item["color"] = _color(raw.get("color"), "#b3261e")
            item["width"] = _number(raw.get("width", 2), "Line width", 0.25, 40)
        elif kind == "ink":
            raw_points = raw.get("points")
            if not isinstance(raw_points, list) or len(raw_points) < 2 or len(raw_points) > 5000:
                raise OperationError("A drawing needs between 2 and 5000 points.")
            item["points"] = [_point(point) for point in raw_points]
            points += len(raw_points)
            item["color"] = _color(raw.get("color"), "#b3261e")
            item["width"] = _number(raw.get("width", 2), "Line width", 0.25, 40)
        elif kind == "highlight":
            quads = raw.get("quads")
            if not isinstance(quads, list) or not quads or len(quads) > 500:
                raise OperationError("A highlight needs between 1 and 500 areas.")
            item["quads"] = []
            for quad in quads:
                if not isinstance(quad, list) or len(quad) != 4:
                    raise OperationError("Each highlight area needs four corners.")
                item["quads"].append([_point(corner) for corner in quad])
            points += 4 * len(quads)
            item["color"] = _color(raw.get("color"), "#ffd84d")
            item["text"] = _text(raw.get("text"), "The highlighted text", 5000, required=False)
        elif kind == "note":
            item["point"] = _point(raw.get("point"))
            item["text"] = _text(raw.get("text"), "The note", 5000)
            item["color"] = _color(raw.get("color"), "#ffd84d")
        if points > MAX_POINTS:
            raise OperationError("That is too much drawing to save at once. Save in smaller steps.")
        clean.append(item)
    return clean


def plan_form_values(values: Any) -> dict[str, Any]:
    """Validate the shape of form input. Field names and types are checked against the PDF by the worker."""
    if not isinstance(values, dict) or not values:
        raise OperationError("There are no form values to save.")
    if len(values) > 5000:
        raise OperationError("Too many form values at once.")
    clean: dict[str, Any] = {}
    for name, value in values.items():
        if not isinstance(name, str) or not name or len(name) > 500:
            raise OperationError("A form field name is not valid.")
        if isinstance(value, bool):
            clean[name] = value
        elif value is None:
            clean[name] = ""
        elif isinstance(value, str):
            if len(value) > 100_000:
                raise OperationError(f"The value for “{name}” is too long.")
            clean[name] = "".join(ch for ch in value.replace("\r\n", "\n").replace("\r", "\n")
                                  if ch in "\n\t" or ch.isprintable())
        else:
            raise OperationError(f"The value for “{name}” must be text, or true/false for a checkbox.")
    return clean
