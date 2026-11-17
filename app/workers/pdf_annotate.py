"""Overlays and annotations (runs in worker processes).

Two different things happen here, and ARD keeps them apart everywhere:

* **Overlays** (text, image, stamp) are drawn onto the page by appending to
  its content stream. They become part of the page in the new version. They
  do not edit or replace anything already on the page; whatever is underneath
  is still in the file.
* **Annotations** (highlight, note, rectangle, ellipse, line, arrow, freehand)
  are standard PDF annotation objects with their own appearance streams. Other
  PDF software can show, edit or remove them, and so can ARD.

All coordinates arrive in PDF user space (points). Boxes are given as three
corners as they appear on screen (top-left, top-right, bottom-left), which
makes rotated pages work without this module having to reason about rotation.
Input has already been validated by ``app.services.pdf.operations``.
"""

from __future__ import annotations

import io
import json
import math
import uuid
from datetime import datetime, timezone
from typing import Any

import pikepdf
from pikepdf import Array, Dictionary, Name, Pdf, String

AUTHOR = "ARD"
ID_PREFIX = "ard-"
MAX_IMAGE_DIMENSION = 4000

FONTS = {
    "sans": ("Helvetica", "Helvetica-Bold"),
    "serif": ("Times-Roman", "Times-Bold"),
    "mono": ("Courier", "Courier-Bold"),
}

# Helvetica-Bold advance widths (1/1000 em) for the characters a stamp may contain.
_BOLD_WIDTHS = {
    **dict.fromkeys("ABCDHKNRU", 722), **dict.fromkeys("EPSVXY", 667), **dict.fromkeys("FLTZ", 611),
    **dict.fromkeys("GOQ", 778), "I": 278, "J": 556, "M": 833, "W": 944,
    **dict.fromkeys("0123456789", 556), " ": 278, "-": 333,
}

# Annotation subtypes that "remove annotations" may delete. Links, form
# widgets and file attachments are deliberately not in this list.
MARKUP_SUBTYPES = {
    "/Text", "/FreeText", "/Line", "/Square", "/Circle", "/Polygon", "/PolyLine", "/Highlight", "/Underline",
    "/Squiggly", "/StrikeOut", "/Stamp", "/Caret", "/Ink",
}


class AnnotateError(Exception):
    pass


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def _n(value: float) -> str:
    text = f"{value:.3f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def _rgb(color: str) -> tuple[float, float, float]:
    return tuple(int(color[i:i + 2], 16) / 255 for i in (1, 3, 5))  # type: ignore[return-value]


def _color_ops(color: str, *, stroke: bool) -> str:
    r, g, b = _rgb(color)
    return f"{_n(r)} {_n(g)} {_n(b)} {'RG' if stroke else 'rg'}"


def _sub(a: list[float], b: list[float]) -> tuple[float, float]:
    return a[0] - b[0], a[1] - b[1]


def _length(v: tuple[float, float]) -> float:
    return math.hypot(v[0], v[1])


def _unit(v: tuple[float, float]) -> tuple[float, float]:
    size = _length(v) or 1.0
    return v[0] / size, v[1] / size


def _bounds(points: list[list[float]], pad: float = 0.0) -> list[float]:
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return [min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad]


def _pdf_literal(text: str) -> str:
    """Text as a PDF literal string in WinAnsi (cp1252)."""
    out = []
    for byte in text.encode("cp1252"):
        if byte in (0x28, 0x29, 0x5C):
            out.append("\\" + chr(byte))
        elif 32 <= byte < 127:
            out.append(chr(byte))
        else:
            out.append(f"\\{byte:03o}")
    return "(" + "".join(out) + ")"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("D:%Y%m%d%H%M%SZ")


def _box(item: dict[str, Any]) -> dict[str, Any]:
    """Derive the geometry of a three-corner box."""
    tl, tr, bl = item["box"]["tl"], item["box"]["tr"], item["box"]["bl"]
    across, down = _sub(tr, tl), _sub(bl, tl)
    return {
        "tl": tl, "tr": tr, "bl": bl, "br": [tr[0] + down[0], tr[1] + down[1]],
        "across": across, "down": down, "w": _length(across), "h": _length(down),
        "ex": _unit(across), "ed": _unit(down),
    }


def _own_resources(pdf: Pdf, page: pikepdf.Page) -> Dictionary:
    """Give the page its own /Resources, copying inherited ones first.

    Adding a resource to a page that inherits its resources from a parent node
    would otherwise hide the inherited fonts and images and break the page.
    """
    obj = page.obj
    if "/Resources" not in obj:
        inherited = None
        node = obj.get("/Parent")
        for _ in range(64):
            if node is None:
                break
            if "/Resources" in node:
                inherited = node["/Resources"]
                break
            node = node.get("/Parent")
        obj["/Resources"] = Dictionary({key: value for key, value in inherited.items()}) if inherited is not None \
            else Dictionary()
    resources = obj["/Resources"]
    return resources


def _add_resource(pdf: Pdf, resources: Dictionary, kind: str, value: pikepdf.Object, prefix: str) -> str:
    """Register ``value`` under a fresh name in a resources sub-dictionary, copying that sub-dictionary if shared."""
    if kind not in resources:
        resources[kind] = Dictionary()
    elif resources[kind].is_indirect:
        # The sub-dictionary may be shared with other pages; do not write into it.
        resources[kind] = Dictionary({key: item for key, item in resources[kind].items()})
    group = resources[kind]
    index = 0
    while f"/{prefix}{index}" in group:
        index += 1
    name = f"/{prefix}{index}"
    group[name] = value
    return name


class _Resources:
    """Per-target resource bookkeeping (a page, or one appearance stream)."""

    def __init__(self, pdf: Pdf, resources: Dictionary, fonts: dict[str, pikepdf.Object]) -> None:
        self.pdf, self.resources, self._font_objects = pdf, resources, fonts
        self._fonts: dict[str, str] = {}
        self._states: dict[tuple, str] = {}

    def font(self, base: str) -> str:
        if base not in self._fonts:
            if base not in self._font_objects:
                self._font_objects[base] = self.pdf.make_indirect(Dictionary(
                    Type=Name.Font, Subtype=Name.Type1, BaseFont=Name("/" + base), Encoding=Name.WinAnsiEncoding))
            self._fonts[base] = _add_resource(self.pdf, self.resources, "/Font", self._font_objects[base], "ArdF")
        return self._fonts[base]

    def opacity(self, alpha: float, *, multiply: bool = False) -> str:
        key = (round(alpha, 3), multiply)
        if key not in self._states:
            state = Dictionary(Type=Name.ExtGState, CA=alpha, ca=alpha)
            if multiply:
                state.BM = Name.Multiply
            self._states[key] = _add_resource(self.pdf, self.resources, "/ExtGState", state, "ArdG")
        return self._states[key]

    def xobject(self, value: pikepdf.Object) -> str:
        return _add_resource(self.pdf, self.resources, "/XObject", value, "ArdIm")


# ---------------------------------------------------------------------------
# overlays: drawn into the page content
# ---------------------------------------------------------------------------
def _text_ops(item: dict[str, Any], res: _Resources) -> str:
    box = _box(item)
    size = float(item["size"])
    regular, bold = FONTS[item.get("font", "sans")]
    font = res.font(bold if item.get("bold") else regular)
    ex, ed = box["ex"], box["ed"]
    ops = ["BT", f"{font} {_n(size)} Tf", _color_ops(item["color"], stroke=False)]
    for index, line in enumerate(item["text"].split("\n")):
        offset = size * (0.92 + 1.2 * index)          # baseline of this line, measured down from the top edge
        x, y = box["tl"][0] + ed[0] * offset, box["tl"][1] + ed[1] * offset
        ops.append(f"{_n(ex[0])} {_n(ex[1])} {_n(-ed[0])} {_n(-ed[1])} {_n(x)} {_n(y)} Tm")
        ops.append(f"{_pdf_literal(line)} Tj")
    ops.append("ET")
    return "\n".join(ops)


def _stamp_ops(item: dict[str, Any], res: _Resources) -> str:
    box = _box(item)
    text = item["text"]
    ex, ed, w, h = box["ex"], box["ed"], box["w"], box["h"]
    border = max(1.0, h * 0.07)
    units = sum(_BOLD_WIDTHS.get(ch, 700) for ch in text) / 1000
    size = min(h * 0.52, (w - 4 * border) / units if units else h)
    width = units * size
    # Work in a local frame: origin at the box's top-left, x across, y down the screen.
    frame = f"{_n(ex[0])} {_n(ex[1])} {_n(ed[0])} {_n(ed[1])} {_n(box['tl'][0])} {_n(box['tl'][1])} cm"
    inset = border / 2
    font = res.font("Helvetica-Bold")
    baseline = h / 2 + size * 0.36
    return "\n".join([
        "q", frame, _color_ops(item["color"], stroke=True), _color_ops(item["color"], stroke=False),
        f"{_n(border)} w", f"{_n(inset)} {_n(inset)} {_n(w - border)} {_n(h - border)} re S",
        # Text needs y up, so flip the frame's y axis for the text matrix.
        "BT", f"{font} {_n(size)} Tf", f"1 0 0 -1 {_n((w - width) / 2)} {_n(baseline)} Tm", f"{_pdf_literal(text)} Tj", "ET",
        "Q",
    ])


def _load_image(pdf: Pdf, path: str) -> pikepdf.Object:
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = 60_000_000      # refuse decompression bombs
    try:
        with Image.open(path) as source:
            source.load()
            image = source.copy()
            original_format = source.format
    except Exception as exc:
        raise AnnotateError("The image could not be read. Use a PNG or JPEG file.") from exc

    if max(image.size) > MAX_IMAGE_DIMENSION:
        ratio = MAX_IMAGE_DIMENSION / max(image.size)
        image = image.resize((max(1, round(image.width * ratio)), max(1, round(image.height * ratio))), Image.LANCZOS)
        original_format = None

    has_alpha = image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info)
    if has_alpha:
        rgba = image.convert("RGBA")
        mask = pdf.make_stream(rgba.getchannel("A").tobytes(), Type=Name.XObject, Subtype=Name.Image,
                               Width=rgba.width, Height=rgba.height, ColorSpace=Name.DeviceGray, BitsPerComponent=8)
        return pdf.make_stream(rgba.convert("RGB").tobytes(), Type=Name.XObject, Subtype=Name.Image, Width=rgba.width,
                               Height=rgba.height, ColorSpace=Name.DeviceRGB, BitsPerComponent=8, SMask=mask)
    if original_format == "JPEG" and image.mode in ("RGB", "L"):
        with open(path, "rb") as handle:       # keep the JPEG as it is: no re-encoding loss
            data = handle.read()
        return _jpeg_stream(pdf, data, image)
    plain = image.convert("L" if image.mode in ("L", "1") else "RGB")
    return pdf.make_stream(plain.tobytes(), Type=Name.XObject, Subtype=Name.Image, Width=plain.width,
                           Height=plain.height, BitsPerComponent=8,
                           ColorSpace=Name.DeviceGray if plain.mode == "L" else Name.DeviceRGB)


def _jpeg_stream(pdf: Pdf, data: bytes, image: Any) -> pikepdf.Object:
    stream = pdf.make_stream(b"")
    stream.write(data, filter=Name.DCTDecode)
    stream.Type, stream.Subtype = Name.XObject, Name.Image
    stream.Width, stream.Height, stream.BitsPerComponent = image.width, image.height, 8
    stream.ColorSpace = Name.DeviceGray if image.mode == "L" else Name.DeviceRGB
    return stream


def _image_ops(item: dict[str, Any], res: _Resources, images: dict[int, pikepdf.Object]) -> str:
    box = _box(item)
    name = res.xobject(images[item["image"]])
    across, up = box["across"], (-box["down"][0], -box["down"][1])
    bl = box["bl"]
    return f"q {_n(across[0])} {_n(across[1])} {_n(up[0])} {_n(up[1])} {_n(bl[0])} {_n(bl[1])} cm {name} Do Q"


# ---------------------------------------------------------------------------
# annotations: separate objects with appearance streams
# ---------------------------------------------------------------------------
def _path(points: list[list[float]], *, close: bool = False) -> str:
    ops = [f"{_n(points[0][0])} {_n(points[0][1])} m"]
    ops += [f"{_n(x)} {_n(y)} l" for x, y in points[1:]]
    if close:
        ops.append("h")
    return " ".join(ops)


def _ellipse_path(box: dict[str, Any]) -> str:
    cx, cy = (box["tr"][0] + box["bl"][0]) / 2, (box["tr"][1] + box["bl"][1]) / 2
    ax, ay = box["across"][0] / 2, box["across"][1] / 2
    bx, by = box["down"][0] / 2, box["down"][1] / 2
    k = 0.5523

    def at(u: float, v: float) -> str:
        return f"{_n(cx + ax * u + bx * v)} {_n(cy + ay * u + by * v)}"

    return " ".join([
        f"{at(1, 0)} m",
        f"{at(1, k)} {at(k, 1)} {at(0, 1)} c", f"{at(-k, 1)} {at(-1, k)} {at(-1, 0)} c",
        f"{at(-1, -k)} {at(-k, -1)} {at(0, -1)} c", f"{at(k, -1)} {at(1, -k)} {at(1, 0)} c", "h",
    ])


def _annotation(pdf: Pdf, subtype: str, rect: list[float], content: str, item: dict[str, Any],
                fonts: dict[str, pikepdf.Object], extra: dict[str, Any] | None = None,
                *, alpha: float = 1.0, multiply: bool = False) -> pikepdf.Object:
    resources = Dictionary()
    res = _Resources(pdf, resources, fonts)
    body = content
    if alpha < 1 or multiply:
        body = f"{res.opacity(alpha, multiply=multiply)} gs\n{content}"
    appearance = pdf.make_stream(body.encode("ascii"), Type=Name.XObject, Subtype=Name.Form,
                                 BBox=Array(rect), Resources=resources)
    if multiply:
        appearance.Group = Dictionary(S=Name.Transparency)
    r, g, b = _rgb(item["color"])
    annotation = Dictionary(
        Type=Name.Annot, Subtype=Name("/" + subtype), Rect=Array(rect), F=4,          # 4 = print
        C=Array([r, g, b]), T=String(AUTHOR), M=String(_now()), NM=String(ID_PREFIX + uuid.uuid4().hex),
        AP=Dictionary(N=appearance), Contents=String(item.get("text") or ""),
    )
    if alpha < 1:
        annotation.CA = alpha
    for key, value in (extra or {}).items():
        annotation[key] = value
    return pdf.make_indirect(annotation)


def _build_annotation(pdf: Pdf, item: dict[str, Any], fonts: dict[str, pikepdf.Object]) -> pikepdf.Object:
    kind = item["type"]
    width = float(item.get("width", 2))
    alpha = float(item.get("opacity", 1))
    stroke = _color_ops(item["color"], stroke=True)

    if kind in ("rect", "ellipse"):
        box = _box(item)
        corners = [box["tl"], box["tr"], box["br"], box["bl"]]
        path = _path(corners, close=True) if kind == "rect" else _ellipse_path(box)
        fill = item.get("fill")
        paint = "B" if fill else "S"
        content = f"{stroke} {_color_ops(fill, stroke=False) if fill else ''} {_n(width)} w 1 j {path} {paint}"
        extra: dict[str, Any] = {"/BS": Dictionary(W=width)}
        if fill:
            extra["/IC"] = Array(list(_rgb(fill)))
        return _annotation(pdf, "Square" if kind == "rect" else "Circle", _bounds(corners, width), content, item,
                           fonts, extra, alpha=alpha)

    if kind in ("line", "arrow"):
        p1, p2 = item["p1"], item["p2"]
        content = f"{stroke} {_n(width)} w 1 J {_path([p1, p2])} S"
        pad = width
        extra = {"/L": Array([*p1, *p2]), "/BS": Dictionary(W=width)}
        if kind == "arrow":
            direction = _unit(_sub(p2, p1))
            head = max(8.0, width * 4)
            normal = (-direction[1], direction[0])
            base = (p2[0] - direction[0] * head, p2[1] - direction[1] * head)
            wing = head * 0.45
            tip = [[base[0] + normal[0] * wing, base[1] + normal[1] * wing], p2,
                   [base[0] - normal[0] * wing, base[1] - normal[1] * wing]]
            content += f" {_color_ops(item['color'], stroke=False)} {_path(tip, close=True)} f"
            pad = head
            extra["/LE"] = Array([Name("/None"), Name.ClosedArrow])
        return _annotation(pdf, "Line", _bounds([p1, p2], pad), content, item, fonts, extra, alpha=alpha)

    if kind == "ink":
        points = item["points"]
        content = f"{stroke} {_n(width)} w 1 J 1 j {_path(points)} S"
        flat = [value for point in points for value in point]
        return _annotation(pdf, "Ink", _bounds(points, width), content, item, fonts,
                           {"/InkList": Array([Array(flat)]), "/BS": Dictionary(W=width)}, alpha=alpha)

    if kind == "highlight":
        quads = item["quads"]
        fill = _color_ops(item["color"], stroke=False)
        content = fill + " " + " ".join(f"{_path([q[0], q[1], q[3], q[2]], close=True)} f" for q in quads)
        every = [point for quad in quads for point in quad]
        # /QuadPoints order per quad: top-left, top-right, bottom-left, bottom-right.
        flat = [value for quad in quads for point in quad for value in point]
        return _annotation(pdf, "Highlight", _bounds(every), content, item, fonts, {"/QuadPoints": Array(flat)},
                           alpha=0.45, multiply=True)

    if kind == "note":
        x, y = item["point"]
        radius = 9.0
        box = {"tr": [x + radius, y + radius], "bl": [x - radius, y - radius],
               "across": (2 * radius, 0.0), "down": (0.0, -2 * radius)}
        # A round marker looks the same at any page rotation.
        content = (f"{_color_ops(item['color'], stroke=False)} 0.2 0.2 0.2 RG 1 w {_ellipse_path(box)} B "
                   f"0.2 0.2 0.2 rg {_n(x - 4.5)} {_n(y + 2)} 9 1.4 re f {_n(x - 4.5)} {_n(y - 0.7)} 9 1.4 re f "
                   f"{_n(x - 4.5)} {_n(y - 3.4)} 6 1.4 re f")
        rect = [x - radius - 1, y - radius - 1, x + radius + 1, y + radius + 1]
        return _annotation(pdf, "Text", rect, content, item, fonts, {"/Name": Name.Comment, "/Open": False})

    raise AnnotateError(f"Unknown item type: {kind}")


# ---------------------------------------------------------------------------
# entry points
# ---------------------------------------------------------------------------
OVERLAY_TYPES = ("text", "image", "stamp", "edit_text")


def apply_items(pdf: Pdf, params: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Add overlays and annotations. Returns (expected page count, report)."""
    with open(params["items_path"], encoding="utf-8") as handle:
        items = json.load(handle)
    images = {int(index): _load_image(pdf, path) for index, path in (params.get("image_paths") or {}).items()}
    return draw_items(pdf, items, images)


def draw_items(pdf: Pdf, items: list[dict[str, Any]], images: dict[int, pikepdf.Object] | None = None
               ) -> tuple[int, dict[str, Any]]:
    """Draw already-validated items. Shared by annotate, watermarks and headers/footers."""
    total = len(pdf.pages)
    images = images or {}
    fonts: dict[str, pikepdf.Object] = {}
    counts: dict[str, int] = {}
    overlays: dict[int, list[str]] = {}
    page_resources: dict[int, _Resources] = {}
    edits: list[dict[str, Any]] = []

    for item in items:
        number = item["page"]
        if number < 1 or number > total:
            raise AnnotateError(f"Page {number} does not exist (the document has {total} pages).")
        page = pdf.pages[number - 1]
        kind = item["type"]
        counts[kind] = counts.get(kind, 0) + 1
        if kind in OVERLAY_TYPES:
            if number not in page_resources:
                page_resources[number] = _Resources(pdf, _own_resources(pdf, page), fonts)
            res = page_resources[number]
            alpha = float(item.get("opacity", 1))
            if kind == "edit_text":
                from app.workers import pdf_textedit

                try:
                    found = pdf_textedit.remove_line(pdf, page, item)
                except pdf_textedit.TextEditError as exc:
                    raise AnnotateError(str(exc)) from exc
                ops, detail = pdf_textedit.replacement_ops(found, item, res)
                edits.append(detail)
            elif kind == "text":
                ops = _text_ops(item, res)
            elif kind == "stamp":
                ops = _stamp_ops(item, res)
            else:
                ops = _image_ops(item, res, images)
            if alpha < 1:
                ops = f"q {res.opacity(alpha)} gs\n{ops}\nQ"
            overlays.setdefault(number, []).append(ops)
        else:
            annotation = _build_annotation(pdf, item, fonts)
            annotation.P = page.obj
            if "/Annots" not in page.obj:
                page.obj.Annots = Array()
            elif page.obj.Annots.is_indirect:
                page.obj.Annots = Array(list(page.obj.Annots))     # do not write into a shared array
            page.obj.Annots.append(annotation)

    for number, operations in overlays.items():
        page = pdf.pages[number - 1]
        # Isolate the existing content's graphics state so the overlay starts from a clean one.
        page.contents_add(pdf.make_stream(b"q\n"), prepend=True)
        page.contents_add(pdf.make_stream(("Q\nq\n" + "\n".join(operations) + "\nQ\n").encode("ascii")))

    overlay_count = sum(counts.get(kind, 0) for kind in OVERLAY_TYPES)
    annotation_count = sum(counts.values()) - overlay_count
    overlay_count -= len(edits)
    return total, {
        "added": counts,
        "text_edits": edits,
        "overlays": overlay_count,
        "annotations": annotation_count,
        "pages": sorted({item["page"] for item in items}),
        "note": "Text, images and stamps are drawn on top of the page and are part of it in this version; "
                "they do not change or remove what is underneath. Highlights, notes, shapes and drawings are "
                "annotations and can be removed later.",
    }


def remove_annotations(pdf: Pdf, params: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Remove markup annotations. Links, form fields and attachments are kept."""
    only_ard = params.get("scope", "all") == "ard"
    wanted = set(params.get("pages") or range(1, len(pdf.pages) + 1))
    removed = 0
    for number, page in enumerate(pdf.pages, start=1):
        if number not in wanted or "/Annots" not in page.obj:
            continue
        keep, dropped = [], []
        for annotation in page.obj.Annots:
            subtype = str(annotation.get("/Subtype", ""))
            ours = str(annotation.get("/NM", "")).startswith(ID_PREFIX)
            if subtype in MARKUP_SUBTYPES and (ours or not only_ard):
                dropped.append(annotation)
            else:
                keep.append(annotation)
        if not dropped:
            continue
        gone = {a.objgen for a in dropped if a.is_indirect}
        # A popup belongs to its parent annotation; remove it with the parent.
        keep = [a for a in keep if not (str(a.get("/Subtype", "")) == "/Popup" and a.get("/Parent") is not None
                                        and a.get("/Parent").is_indirect and a.get("/Parent").objgen in gone)]
        removed += len(dropped)
        if keep:
            page.obj.Annots = Array(keep)
        else:
            del page.obj["/Annots"]
    if not removed:
        raise AnnotateError("There are no annotations to remove." if not only_ard
                            else "There are no annotations added by ARD to remove.")
    return len(pdf.pages), {
        "removed": removed,
        "note": "Links, form fields and attachments were kept. Text, images and stamps drawn onto pages are part "
                "of the page and are not affected; use Versions to go back to before they were added.",
    }
