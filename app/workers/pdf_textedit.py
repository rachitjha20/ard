"""Editing a line of existing text (runs in worker processes).

What "edit" means here, precisely:

1. The drawing instructions for the chosen line are removed from the page's
   content stream. The old text is gone from the new version; it is not
   covered up.
2. The new text is drawn at the same starting point, baseline, size and
   colour.
3. Font: the line's own font is reused when that is safe, which means the
   font is a simple Latin font and already contains every character of the
   new text. Otherwise the closest standard font (same family, weight and
   slant) is used, and the report says so. A subset font embedded in a PDF
   usually holds only the characters the document already uses, so new
   characters cannot be drawn with it.

Limits: text inside a graphic or form object, text drawn as outlines, and
lines whose layout depends on text that follows them on the same line are not
edited; the operation stops and says why. Nothing reflows: a longer line
simply extends further to the right.
"""

from __future__ import annotations

import math
from typing import Any

import pikepdf
from pikepdf import Array, Name, Operator, Pdf

from app.workers.pdf_geometry import _HELVETICA, inherited

Matrix = tuple[float, float, float, float, float, float]
IDENTITY: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)

STANDARD_14 = {
    "Helvetica", "Helvetica-Bold", "Helvetica-Oblique", "Helvetica-BoldOblique", "Times-Roman", "Times-Bold",
    "Times-Italic", "Times-BoldItalic", "Courier", "Courier-Bold", "Courier-Oblique", "Courier-BoldOblique",
    "Arial", "Arial-Bold", "Arial-Italic", "Arial-BoldItalic", "ArialMT", "Arial-BoldMT", "TimesNewRoman",
    "TimesNewRomanPSMT", "CourierNew", "CourierNewPSMT",
}


class TextEditError(Exception):
    pass


def _mul(m: Matrix, n: Matrix) -> Matrix:
    """m followed by n (PDF row-vector convention)."""
    return (m[0] * n[0] + m[1] * n[2], m[0] * n[1] + m[1] * n[3],
            m[2] * n[0] + m[3] * n[2], m[2] * n[1] + m[3] * n[3],
            m[4] * n[0] + m[5] * n[2] + n[4], m[4] * n[1] + m[5] * n[3] + n[5])


def _numbers(operands: Any) -> list[float]:
    return [float(value) for value in operands]


# ---------------------------------------------------------------------------
# fonts
# ---------------------------------------------------------------------------
class FontInfo:
    """What ARD needs to know about one font resource: glyph widths and what it looks like."""

    def __init__(self, resource_name: str, font: pikepdf.Object | None) -> None:
        self.resource_name = resource_name
        self.font = font
        self.base_name = ""
        self.subtype = ""
        self.composite = False
        self.first_char = 0
        self.widths: list[float] = []
        self.cid_widths: dict[int, float] = {}
        self.default_width = 500.0
        self.has_widths = False
        self.flags = 0
        self.weight = 0.0
        self.encoding = None
        if font is None:
            return
        try:
            self.base_name = str(font.get("/BaseFont", "")).lstrip("/")
            self.subtype = str(font.get("/Subtype", ""))
            self.composite = self.subtype == "/Type0"
            self.encoding = font.get("/Encoding")
            descriptor = font.get("/FontDescriptor")
            if self.composite:
                descendant = font.DescendantFonts[0]
                descriptor = descendant.get("/FontDescriptor")
                self.default_width = float(descendant.get("/DW", 1000))
                self._read_cid_widths(descendant.get("/W"))
                self.has_widths = True
            else:
                self.first_char = int(font.get("/FirstChar", 0))
                widths = font.get("/Widths")
                if widths is not None:
                    self.widths = [float(value) for value in widths]
                    self.has_widths = True
                if descriptor is not None:
                    self.default_width = float(descriptor.get("/MissingWidth", 0)) or 500.0
            if descriptor is not None:
                self.flags = int(descriptor.get("/Flags", 0))
                self.weight = float(descriptor.get("/FontWeight", 0))
        except Exception:
            pass

    def _read_cid_widths(self, table: Any) -> None:
        if table is None:
            return
        items = list(table)
        index = 0
        try:
            while index < len(items):
                first = int(items[index])
                following = items[index + 1]
                if isinstance(following, Array):
                    for offset, width in enumerate(following):
                        self.cid_widths[first + offset] = float(width)
                    index += 2
                else:
                    last, width = int(following), float(items[index + 2])
                    if last - first < 70_000:
                        for cid in range(first, last + 1):
                            self.cid_widths[cid] = width
                    index += 3
        except Exception:
            return

    @property
    def plain_name(self) -> str:
        """Font name without a subset prefix such as ``ABCDEF+``."""
        name = self.base_name
        return name.split("+", 1)[1] if len(name) > 7 and name[6] == "+" else name

    @property
    def is_subset(self) -> bool:
        return len(self.base_name) > 7 and self.base_name[6] == "+"

    def glyph_widths(self, data: bytes) -> list[tuple[float, bool]]:
        """(width in 1/1000 em, is a single-byte space) for each character code in a string."""
        if self.composite:
            return [(self.cid_widths.get((data[i] << 8) | data[i + 1], self.default_width), False)
                    for i in range(0, len(data) - 1, 2)]
        result = []
        for code in data:
            offset = code - self.first_char
            if self.has_widths and 0 <= offset < len(self.widths):
                width = self.widths[offset]
            elif not self.has_widths and 32 <= code <= 126 and "Courier" not in self.plain_name:
                width = float(_HELVETICA[code - 32])           # a standard font without a width table
            elif not self.has_widths and "Courier" in self.plain_name:
                width = 600.0
            else:
                width = self.default_width
            result.append((width, code == 32))
        return result

    # ---- how it looks -------------------------------------------------------
    @property
    def bold(self) -> bool:
        name = self.plain_name.lower()
        return "bold" in name or "black" in name or "heavy" in name or self.weight >= 600 or bool(self.flags & (1 << 18))

    @property
    def italic(self) -> bool:
        name = self.plain_name.lower()
        return "italic" in name or "oblique" in name or bool(self.flags & (1 << 6))

    @property
    def family(self) -> str:
        name = self.plain_name.lower()
        if any(key in name for key in ("courier", "mono", "consol", "menlo", "typewriter")) or self.flags & 1:
            return "mono"
        if any(key in name for key in ("times", "serif", "georgia", "garamond", "cambria", "palatino", "book",
                                       "minion", "caslon", "century", "roman")) and "sans" not in name:
            return "serif"
        if self.flags & 2 and "sans" not in name and not any(key in name for key in ("arial", "helvet", "calibri", "verdana")):
            return "serif"
        return "sans"

    def standard_equivalent(self, family: str | None = None, bold: bool | None = None, italic: bool | None = None) -> str:
        family = family or self.family
        bold = self.bold if bold is None else bold
        italic = self.italic if italic is None else italic
        if family == "serif":
            return {(False, False): "Times-Roman", (True, False): "Times-Bold",
                    (False, True): "Times-Italic", (True, True): "Times-BoldItalic"}[(bold, italic)]
        stem = "Courier" if family == "mono" else "Helvetica"
        return stem + {(False, False): "", (True, False): "-Bold", (False, True): "-Oblique",
                       (True, True): "-BoldOblique"}[(bold, italic)]

    def can_draw(self, text: str) -> bool:
        """True when this very font can safely draw ``text`` (so the edit keeps the original font)."""
        if self.font is None or self.composite or self.subtype not in ("/Type1", "/TrueType", "/MMType1"):
            return False
        if self.flags & 4 and not self.flags & 32:        # symbolic font: its codes are not Latin letters
            return False
        try:
            encoded = text.encode("cp1252")
        except UnicodeEncodeError:
            return False
        encoding = self.encoding
        win_ansi = False
        if isinstance(encoding, Name):
            win_ansi = str(encoding) == "/WinAnsiEncoding"
        elif encoding is not None:
            try:
                win_ansi = str(encoding.get("/BaseEncoding", "")) == "/WinAnsiEncoding" and "/Differences" not in encoding
            except Exception:
                win_ansi = False
        standard = self.plain_name in STANDARD_14 and not self.is_subset
        if not win_ansi:
            # Without WinAnsi only a standard font and plain ASCII are certain to mean the same letters.
            return standard and encoding is None and all(32 <= code <= 126 for code in encoded)
        if self.has_widths:
            # A subset font lists a width only for the characters it actually contains.
            for code in encoded:
                offset = code - self.first_char
                if not (0 <= offset < len(self.widths)) or self.widths[offset] <= 0:
                    return False
            return True
        return standard


def _page_fonts(page: pikepdf.Page) -> dict[str, FontInfo]:
    resources = inherited(page.obj, "/Resources")
    fonts = resources.get("/Font") if resources is not None else None
    result: dict[str, FontInfo] = {}
    if fonts is not None:
        for name, font in fonts.items():
            result[str(name)] = FontInfo(str(name), font)
    return result


# ---------------------------------------------------------------------------
# colour
# ---------------------------------------------------------------------------
def _hex(values: list[float]) -> str:
    if len(values) == 1:
        red = green = blue = values[0]
    elif len(values) == 3:
        red, green, blue = values
    elif len(values) == 4:
        cyan, magenta, yellow, black = values
        red, green, blue = (1 - min(1, cyan + black)), (1 - min(1, magenta + black)), (1 - min(1, yellow + black))
    else:
        return "#000000"
    return "#" + "".join(f"{max(0, min(255, round(channel * 255))):02x}" for channel in (red, green, blue))


# ---------------------------------------------------------------------------
# the edit
# ---------------------------------------------------------------------------
SHOW_OPERATORS = ("Tj", "TJ", "'", '"')
POSITION_OPERATORS = ("Td", "TD", "Tm", "T*", "'", '"', "BT")


def remove_line(pdf: Pdf, page: pikepdf.Page, target: dict[str, Any]) -> dict[str, Any]:
    """Remove the text-drawing instructions that start inside ``target`` and describe what was there.

    ``target``: ``origin`` (start of the baseline), ``direction`` (unit vector along the line),
    ``width`` and ``height``, all in PDF user space.
    """
    origin, direction = target["origin"], target["direction"]
    width, height = float(target["width"]), float(target["height"])
    normal = (-direction[1], direction[0])
    slack_along = max(1.5, 0.12 * height)
    slack_across = max(1.0, 0.28 * height)

    def inside(point: tuple[float, float]) -> bool:
        dx, dy = point[0] - origin[0], point[1] - origin[1]
        along = dx * direction[0] + dy * direction[1]
        across = dx * normal[0] + dy * normal[1]
        # Text that starts exactly where this run ends belongs to the next run, so the right edge is exclusive.
        return -slack_along <= along < width - min(0.3, width * 0.1) and abs(across) <= slack_across

    fonts = _page_fonts(page)
    try:
        instructions = list(pikepdf.parse_content_stream(page))
    except Exception as exc:
        raise TextEditError("This page's content could not be read for editing.") from exc

    ctm: Matrix = IDENTITY
    fill = "#000000"
    stack: list[tuple[Matrix, str]] = []
    text_matrix = line_matrix = IDENTITY
    font_name, font_size = "", 0.0
    char_space = word_space = leading = rise = 0.0
    horizontal = 1.0
    output: list[Any] = []
    removed: list[dict[str, Any]] = []
    positioned_since_removal = True

    def advance(operands: Any, operator: str) -> tuple[float, bool]:
        """Displacement (text space) of a show operator, and whether every width was known."""
        info = fonts.get(font_name) or FontInfo(font_name, None)
        known = info.font is not None and (info.has_widths or info.plain_name in STANDARD_14)
        pieces = list(operands[0]) if operator == "TJ" else [operands[-1]]
        total = 0.0
        for piece in pieces:
            if isinstance(piece, pikepdf.String):
                for glyph_width, is_space in info.glyph_widths(bytes(piece)):
                    total += (glyph_width / 1000 * font_size + char_space + (word_space if is_space else 0.0)) * horizontal
            else:
                try:
                    total -= float(piece) / 1000 * font_size * horizontal
                except (TypeError, ValueError):
                    pass
        return total, known

    for instruction in instructions:
        operands, operator = instruction.operands, str(instruction.operator)
        keep = True
        try:
            if operator == "q":
                stack.append((ctm, fill))
            elif operator == "Q":
                if stack:
                    ctm, fill = stack.pop()
            elif operator == "cm":
                ctm = _mul(tuple(_numbers(operands)), ctm)        # type: ignore[arg-type]
            elif operator in ("g", "rg", "k"):
                fill = _hex(_numbers(operands))
            elif operator in ("sc", "scn"):
                values = [float(v) for v in operands if not isinstance(v, (Name, pikepdf.String))]
                if len(values) in (1, 3, 4) and len(values) == len(operands):
                    fill = _hex(values)
            elif operator == "BT":
                text_matrix = line_matrix = IDENTITY
            elif operator == "Tf":
                font_name, font_size = str(operands[0]), float(operands[1])
            elif operator == "Tc":
                char_space = float(operands[0])
            elif operator == "Tw":
                word_space = float(operands[0])
            elif operator == "Tz":
                horizontal = float(operands[0]) / 100
            elif operator == "TL":
                leading = float(operands[0])
            elif operator == "Ts":
                rise = float(operands[0])
            elif operator in ("Td", "TD"):
                tx, ty = _numbers(operands)
                if operator == "TD":
                    leading = -ty
                line_matrix = _mul((1, 0, 0, 1, tx, ty), line_matrix)
                text_matrix = line_matrix
            elif operator == "Tm":
                text_matrix = line_matrix = tuple(_numbers(operands))      # type: ignore[assignment]
            elif operator == "T*":
                line_matrix = _mul((1, 0, 0, 1, 0, -leading), line_matrix)
                text_matrix = line_matrix

            if operator in POSITION_OPERATORS and operator not in ("'", '"'):
                positioned_since_removal = True

            if operator in SHOW_OPERATORS:
                if operator in ("'", '"'):
                    if operator == '"':
                        word_space, char_space = float(operands[0]), float(operands[1])
                    line_matrix = _mul((1, 0, 0, 1, 0, -leading), line_matrix)
                    text_matrix = line_matrix
                    positioned_since_removal = True
                full = _mul(text_matrix, ctm)
                start = (rise * full[2] + full[4], rise * full[3] + full[5])
                displacement, known = advance(operands, operator)
                if inside(start):
                    info = fonts.get(font_name) or FontInfo(font_name, None)
                    scale = math.hypot(full[2], full[3])
                    removed.append({"font": info, "size": font_size * scale, "color": fill, "start": start,
                                    "matrix": full, "known_advance": known})
                    # Keep the pen movement the removed text caused, so anything after it stays where it was.
                    replacement = []
                    if operator == '"':
                        replacement += [pikepdf.ContentStreamInstruction([operands[0]], Operator("Tw")),
                                        pikepdf.ContentStreamInstruction([operands[1]], Operator("Tc"))]
                    if operator in ("'", '"'):
                        replacement.append(pikepdf.ContentStreamInstruction([], Operator("T*")))
                    if font_size and horizontal:
                        shift = -displacement * 1000 / (font_size * horizontal)
                        replacement.append(pikepdf.ContentStreamInstruction([Array([round(shift, 3)])], Operator("TJ")))
                    output.extend(replacement)
                    positioned_since_removal = False
                    keep = False
                elif removed and not positioned_since_removal and not removed[-1]["known_advance"]:
                    # Text that is staying is placed relative to the end of text we removed, and we do not
                    # know that text's exact width. Removing it would shift what remains.
                    raise TextEditError(
                        "This line cannot be edited in place: the text after it on the same line is positioned "
                        "relative to it, and the font does not say how wide its characters are. "
                        "Nothing was changed.")
                text_matrix = _mul((1, 0, 0, 1, displacement, 0), text_matrix)
        except TextEditError:
            raise
        except Exception:
            keep = True                    # an instruction we do not understand is passed through untouched
        if keep:
            output.append(instruction)

    if not removed:
        raise TextEditError(
            "ARD could not find this text in the page's own content. It may be part of a graphic, a form "
            "object or an image. Nothing was changed; you can cover it and add new text instead.")

    page.obj.Contents = pdf.make_stream(pikepdf.unparse_content_stream(output))
    first = min(removed, key=lambda item: (item["start"][0] - origin[0]) * direction[0] + (item["start"][1] - origin[1]) * direction[1])
    return {"font": first["font"], "size": first["size"], "color": first["color"], "start": first["start"],
            "matrix": first["matrix"], "fragments": len(removed)}


def replacement_ops(info: dict[str, Any], item: dict[str, Any], resources: Any) -> tuple[str, dict[str, Any]]:
    """Content-stream text for the new line, plus a description of the font decision."""
    font: FontInfo = info["font"]
    text = item["text"]
    choice = item.get("font", "original")
    size = float(item.get("size") or info["size"])
    color = item.get("color") or info["color"]
    bold, italic = item.get("bold"), item.get("italic")

    matrix: Matrix = info["matrix"]
    scale = math.hypot(matrix[2], matrix[3]) or 1.0
    # Unit vectors of the original line in user space; the new text uses the same baseline and slant.
    ex = (matrix[0] / (math.hypot(matrix[0], matrix[1]) or 1.0), matrix[1] / (math.hypot(matrix[0], matrix[1]) or 1.0))
    ey = (matrix[2] / scale, matrix[3] / scale)
    start = info["start"]

    reused = choice == "original" and bold is None and italic is None and font.can_draw(text)
    if reused:
        resource = font.resource_name
        used_name = font.plain_name
    else:
        family = None if choice == "original" else choice
        used_name = font.standard_equivalent(family, bold, italic)
        resource = resources.font(used_name)

    red, green, blue = (int(color[i:i + 2], 16) / 255 for i in (1, 3, 5))
    encoded = text.encode("cp1252")
    literal = "(" + "".join("\\" + chr(b) if b in (0x28, 0x29, 0x5C) else chr(b) if 32 <= b < 127 else f"\\{b:03o}"
                            for b in encoded) + ")"
    number = lambda value: f"{value:.4f}".rstrip("0").rstrip(".") or "0"      # noqa: E731
    ops = "\n".join([
        "BT", f"{resource} {number(size)} Tf", f"{number(red)} {number(green)} {number(blue)} rg",
        f"{number(ex[0])} {number(ex[1])} {number(ey[0])} {number(ey[1])} {number(start[0])} {number(start[1])} Tm",
        f"{literal} Tj", "ET",
    ])
    original = font.plain_name or "unknown"
    if reused:
        reason = f"Kept the original font ({original})."
    elif choice != "original" or bold is not None or italic is not None:
        reason = f"Used {used_name}, as you chose."
    elif used_name == original:
        reason = (f"Used the standard {used_name} font, the same typeface as the original, so that every "
                  "character you typed can be drawn.")
    elif font.composite or font.font is None:
        reason = (f"The original font ({original}) is stored in a form ARD cannot add new text to, "
                  f"so the closest standard font, {used_name}, was used.")
    else:
        reason = (f"The original font ({original}) does not contain every character you typed, or is not a "
                  f"plain Latin font, so the closest standard font, {used_name}, was used.")
    return ops, {"page": item["page"], "original_font": original, "font_used": used_name, "kept_original_font": reused,
                 "size": round(size, 2), "color": color, "fragments_removed": info["fragments"], "explanation": reason}


def describe_line(payload: dict[str, Any]) -> dict[str, Any]:
    """Report the font, size and colour of a line without changing anything. Used before editing."""
    try:
        pdf = Pdf.open(payload["path"])
    except pikepdf.PasswordError:
        return {"found": False, "problem": "This PDF is password-protected. Remove the protection before editing."}
    with pdf:
        number = int(payload["page"])
        if number < 1 or number > len(pdf.pages):
            return {"found": False, "problem": "That page does not exist."}
        try:
            # Works on the in-memory copy only; the file is never written.
            info = remove_line(pdf, pdf.pages[number - 1], payload["target"])
        except TextEditError as exc:
            return {"found": False, "problem": str(exc)}
        font: FontInfo = info["font"]
        return {
            "found": True, "font": font.plain_name or "", "family": font.family, "bold": font.bold,
            "italic": font.italic, "subset": font.is_subset, "size": round(info["size"], 2), "color": info["color"],
            "standard_equivalent": font.standard_equivalent(),
            # Whether new Latin text can ever keep this font; the exact answer depends on the characters typed.
            "can_keep_font": font.can_draw("e") or font.can_draw("a") or font.can_draw(" "),
        }
