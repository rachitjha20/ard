"""Editing existing lines of text: the old line is removed, the new one keeps font, size and colour."""

import io

import pikepdf
import pypdfium2 as pdfium
import pytest

from app.workers.pdfium_lock import PDFIUM_LOCK
from tests.helpers import make_pdf, page_texts

CONTENT = (b"BT /F1 36 Tf 72 700 Td (Page heading) Tj ET "
           b"BT /F1 14 Tf 0.8 0 0 rg 72 600 Td 18 TL (First line of a paragraph) Tj T* (Second line stays) Tj "
           b"T* [(Thi)-20(rd li)10(ne too)] TJ ET "
           b"BT /F2 12 Tf 0 0 0 rg 72 500 Td (Invoce total) Tj ET "
           b"BT /F1 12 Tf 72 450 Td (Left part) Tj ( and right part) Tj ET")


def build(extra_fonts=None, content=CONTENT, rotate=0):
    pdf = pikepdf.Pdf.open(io.BytesIO(make_pdf(1)))
    page = pdf.pages[0]
    fonts = page.obj.Resources.Font
    # F2: an embedded-style subset font that only "contains" the letters of "Invoce total".
    widths = [0] * 224
    for char in "Invoce total":
        widths[ord(char) - 32] = 500
    fonts.F2 = pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name.Font, Subtype=pikepdf.Name.TrueType, BaseFont=pikepdf.Name("/ABCDEF+Georgia-Bold"),
        Encoding=pikepdf.Name.WinAnsiEncoding, FirstChar=32, LastChar=255, Widths=widths,
        FontDescriptor=pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.FontDescriptor,
                                                            FontName=pikepdf.Name("/ABCDEF+Georgia-Bold"), Flags=34))))
    for name, font in (extra_fonts or {}).items():
        fonts[name] = font(pdf) if callable(font) else font
    page.obj.Contents = pdf.make_stream(content)
    if rotate:
        page.obj.Rotate = rotate
    buffer = io.BytesIO()
    pdf.save(buffer)
    return buffer.getvalue()


def line(x, y, width, height, text, **extra):
    return {"type": "edit_text", "page": 1, "origin": [x, y], "direction": [1, 0], "width": width, "height": height,
            "text": text, **extra}


def edit(client, document_id, items):
    return client.post(f"/api/documents/{document_id}/annotations", json={"items": items, "images": []})


def output_path(client, settings, document_id):
    doc = client.get(f"/api/documents/{document_id}").json()
    version = next(v for v in doc["versions"] if v["is_current"])
    return settings.data_dir / "versions" / document_id / f"{version['id']}.pdf"


def char_positions(path):
    """{text of each run: (x, y) of its first character} read back from the saved file."""
    with PDFIUM_LOCK:
        document = pdfium.PdfDocument(str(path))
        page = document[0]
        text_page = page.get_textpage()
        text = text_page.get_text_range()
        boxes = [text_page.get_charbox(i) for i in range(text_page.count_chars())]
        document.close()
    return text, boxes


def first_char_of(path, phrase):
    text, boxes = char_positions(path)
    index = text.index(phrase)
    return boxes[index]


def test_line_is_replaced_in_place_with_the_same_font_size_and_colour(client, settings, upload):
    doc = upload(build())
    original = settings.data_dir / "originals" / f"{doc['id']}.pdf"
    stays_before = first_char_of(original, "Second line stays")
    old_first = first_char_of(original, "First line of a paragraph")

    response = edit(client, doc["id"], [line(72, 600, 170, 14, "Replaced first line")])
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result["summary"] == "Edited 1 line of text"
    detail = result["report"]["text_edits"][0]
    assert detail == {**detail, "kept_original_font": True, "font_used": "Helvetica", "size": 14.0, "color": "#cc0000",
                      "fragments_removed": 1}

    path = output_path(client, settings, doc["id"])
    text = page_texts(path)[0]
    assert "Replaced first line" in text and "First line of a paragraph" not in text
    assert "Second line stays" in text and "Third line too" in text.replace("  ", " ")

    # The old words are gone from the file's content, not merely covered.
    with pikepdf.Pdf.open(path) as pdf:
        streams = b"".join(pdf.pages[0].Contents.read_bytes() if not isinstance(pdf.pages[0].Contents, pikepdf.Array)
                           else b"".join(part.read_bytes() for part in pdf.pages[0].Contents) for _ in [0])
        assert b"First line of a paragraph" not in streams and b"Replaced first line" in streams
        assert b"re f" not in streams                       # no cover-up rectangle was drawn

    # The new line starts exactly where the old one did, and the lines below did not move.
    new_first = first_char_of(path, "Replaced first line")
    assert new_first[0] == pytest.approx(old_first[0], abs=1.0) and new_first[1] == pytest.approx(old_first[1], abs=1.0)
    assert first_char_of(path, "Second line stays") == pytest.approx(stays_before, abs=0.2)

    # Rendered in the original red.
    with PDFIUM_LOCK:
        image = pdfium.PdfDocument(str(path))[0].render(scale=2).to_pil().convert("RGB")
    reds = sum(1 for x in range(144, 400, 2) for y in range(2 * (792 - 612), 2 * (792 - 598), 2)
               if image.getpixel((x, y))[0] > 150 and image.getpixel((x, y))[1] < 80)
    assert reds > 20
    assert page_texts(original)[0].count("First line of a paragraph") == 1      # the original file is untouched


def test_text_after_a_removed_piece_keeps_its_place(client, settings, upload):
    """'Left part' and ' and right part' are drawn one after the other with no repositioning between them."""
    doc = upload(build())
    original = settings.data_dir / "originals" / f"{doc['id']}.pdf"
    right_before = first_char_of(original, "and right part")
    # Edit only the left piece. Its region is exactly as wide as "Left part", so it ends where the right piece begins.
    response = edit(client, doc["id"], [line(72, 450, 44.03, 12, "LEFT")])
    assert response.status_code == 200, response.text
    path = output_path(client, settings, doc["id"])
    assert first_char_of(path, "and right part") == pytest.approx(right_before, abs=0.3)
    assert "LEFT" in page_texts(path)[0] and "Left part" not in page_texts(path)[0]


def test_subset_font_falls_back_to_the_matching_standard_font(client, settings, upload):
    doc = upload(build())
    # Every letter of the correction already exists in the font: the original font is kept.
    kept = edit(client, doc["id"], [line(72, 500, 70, 12, "Invoice total".replace("i", "o", 0)[:0] + "Invoce tota")])
    assert kept.json()["result"]["report"]["text_edits"][0]["kept_original_font"] is True

    other = upload(build() + b"\n% copy", name="copy.pdf")
    # "i" is not in the subset, so the same font cannot draw it: a serif bold standard font is chosen and explained.
    response = edit(client, other["id"], [line(72, 500, 70, 12, "Invoice total")])
    detail = response.json()["result"]["report"]["text_edits"][0]
    assert detail["kept_original_font"] is False and detail["original_font"] == "Georgia-Bold"
    assert detail["font_used"] == "Times-Bold" and detail["size"] == 12.0
    assert "does not contain every character" in detail["explanation"]
    path = output_path(client, settings, other["id"])
    assert "Invoice total" in page_texts(path)[0]
    with pikepdf.Pdf.open(path) as pdf:
        names = {str(font.get("/BaseFont")) for font in pdf.pages[0].Resources.Font.values()}
        assert "/Times-Bold" in names


def test_user_can_override_font_size_and_colour(client, settings, upload):
    doc = upload(build())
    response = edit(client, doc["id"], [line(72, 600, 170, 14, "Bigger and blue", font="mono", size=20, color="#0000ff", bold=True)])
    detail = response.json()["result"]["report"]["text_edits"][0]
    assert detail["font_used"] == "Courier-Bold" and detail["size"] == 20.0 and detail["color"] == "#0000ff"
    assert "as you chose" in detail["explanation"]


def test_text_on_a_rotated_page_and_several_edits_at_once(client, settings, upload):
    doc = upload(build(rotate=90))
    items = [line(72, 600, 170, 14, "One"), line(72, 564, 100, 14, "Three"), line(72, 700, 250, 36, "New heading")]
    response = edit(client, doc["id"], items)
    assert response.status_code == 200, response.text
    assert response.json()["result"]["summary"] == "Edited 3 lines of text"
    text = page_texts(output_path(client, settings, doc["id"]))[0]
    assert all(word in text for word in ("One", "Three", "New heading", "Second line stays"))
    assert "Page heading" not in text and "First line" not in text


def test_edits_that_cannot_be_done_safely_change_nothing(client, settings, upload):
    # 1. Text that lives inside a form object, not in the page's own content.
    def with_form():
        pdf = pikepdf.Pdf.open(io.BytesIO(make_pdf(1)))
        page = pdf.pages[0]
        form = pdf.make_stream(b"BT /F1 14 Tf 72 600 Td (Inside a form object) Tj ET", Type=pikepdf.Name.XObject,
                               Subtype=pikepdf.Name.Form, BBox=[0, 0, 612, 792],
                               Resources=pikepdf.Dictionary(Font=page.obj.Resources.Font))
        page.obj.Resources.XObject = pikepdf.Dictionary(Fm0=form)
        page.obj.Contents = pdf.make_stream(b"/Fm0 Do")
        buffer = io.BytesIO()
        pdf.save(buffer)
        return buffer.getvalue()

    doc = upload(with_form(), name="form.pdf")
    response = edit(client, doc["id"], [line(72, 600, 150, 14, "Changed")])
    assert response.status_code == 400 and "could not find this text" in response.json()["detail"]

    # 2. Kept text placed right after removed text whose width is unknown (a font with no width table).
    unknown = lambda pdf: pdf.make_indirect(pikepdf.Dictionary(                      # noqa: E731
        Type=pikepdf.Name.Font, Subtype=pikepdf.Name.TrueType, BaseFont=pikepdf.Name("/Mystery")))
    tricky = upload(build(extra_fonts={"/F3": unknown},
                          content=b"BT /F3 12 Tf 72 450 Td (Left part) Tj ( and right part) Tj ET"), name="tricky.pdf")
    response = edit(client, tricky["id"], [line(72, 450, 3, 12, "LEFT")])
    assert response.status_code == 400 and "cannot be edited in place" in response.json()["detail"]

    # 3. Clicking where there is no text, non-Latin replacement text, and malformed requests.
    plain = upload(build(), name="plain.pdf")
    assert edit(client, plain["id"], [line(300, 300, 50, 12, "Nothing here")]).status_code == 400
    assert "Latin" in edit(client, plain["id"], [line(72, 600, 170, 14, "नमस्ते")]).json()["detail"]
    for bad in ({"direction": [0, 0]}, {"width": -1}, {"height": 0}, {"font": "comic"}, {"size": 9999}, {"bold": "yes"},
                {"color": "red"}):
        assert edit(client, plain["id"], [{**line(72, 600, 170, 14, "x"), **bad}]).status_code == 400, bad
    assert edit(client, plain["id"], [line(72, 600, 170, 14, "safe) Tj ET 1 0 0 rg 0 0 612 792 re f BT (x")]).status_code == 200

    for document in (doc, tricky):
        assert client.get(f"/api/documents/{document['id']}").json()["version_count"] == 1
    assert list((settings.data_dir / "temporary").iterdir()) == []


def test_font_family_detection():
    from app.workers.pdf_textedit import FontInfo

    def info(name, flags=32, subtype="/TrueType"):
        font = pikepdf.Dictionary(Subtype=pikepdf.Name(subtype), BaseFont=pikepdf.Name("/" + name),
                                  FontDescriptor=pikepdf.Dictionary(Flags=flags))
        return FontInfo("/F", font)

    assert info("ABCDEF+Calibri").standard_equivalent() == "Helvetica"
    assert info("ABCDEF+Calibri-BoldItalic").standard_equivalent() == "Helvetica-BoldOblique"
    assert info("TimesNewRomanPS-ItalicMT").standard_equivalent() == "Times-Italic"
    assert info("Cambria", flags=34).standard_equivalent() == "Times-Roman"
    assert info("Consolas-Bold").standard_equivalent() == "Courier-Bold"
    assert info("SomethingOdd", flags=33).standard_equivalent() == "Courier"        # fixed-pitch flag
    assert info("ABCDEF+Calibri").plain_name == "Calibri" and info("ABCDEF+Calibri").is_subset
    assert info("NotoSans-Regular").standard_equivalent() == "Helvetica"
    assert not info("Wingdings", flags=4).can_draw("abc")                              # symbol font
    assert not FontInfo("/F", None).can_draw("abc")


def test_line_font_size_and_colour_are_reported_before_editing(client, upload):
    doc = upload(build())
    probe = lambda x, y, width, height: client.post(f"/api/documents/{doc['id']}/text-line", json={      # noqa: E731
        "page": 1, "line": {"origin": [x, y], "direction": [1, 0], "width": width, "height": height}}).json()
    red = probe(72, 600, 170, 14)
    assert red == {**red, "found": True, "font": "Helvetica", "family": "sans", "bold": False, "size": 14.0,
                   "color": "#cc0000", "can_keep_font": True}
    subset = probe(72, 500, 70, 12)
    assert subset["font"] == "Georgia-Bold" and subset["family"] == "serif" and subset["bold"] and subset["subset"]
    assert subset["standard_equivalent"] == "Times-Bold" and subset["size"] == 12.0 and subset["color"] == "#000000"
    heading = probe(72, 700, 250, 36)
    assert heading["size"] == 36.0
    nothing = probe(300, 300, 50, 12)
    assert nothing["found"] is False and "could not find this text" in nothing["problem"]
    # Looking is read-only: no version, no job record, nothing in the audit log.
    assert client.get(f"/api/documents/{doc['id']}").json()["version_count"] == 1
    assert not [j for j in client.get("/api/jobs").json()["jobs"] if j["operation"] not in ("index", None)]
    bad = client.post(f"/api/documents/{doc['id']}/text-line", json={"page": 9, "line": {}})
    assert bad.status_code == 400
