"""Phase 3, first slice: watermark, header/footer, protection, redaction, export."""

import io
import zipfile

import pikepdf
import pypdf
import pypdfium2 as pdfium
import pytest
from PIL import Image

from app.workers.pdf_geometry import Frame, page_frame, text_width
from app.workers.pdfium_lock import PDFIUM_LOCK
from tests.helpers import make_pdf, page_texts


def op(client, document_id, operation, **params):
    return client.post(f"/api/documents/{document_id}/operations", json={"operation": operation, "params": params})


def current_path(client, settings, document_id):
    doc = client.get(f"/api/documents/{document_id}").json()
    version = next(v for v in doc["versions"] if v["is_current"])
    if version["number"] == 1:
        return settings.data_dir / "originals" / f"{document_id}.pdf"
    return settings.data_dir / "versions" / document_id / f"{version['id']}.pdf"


def render(path, page=0, password=None):
    with PDFIUM_LOCK:          # background indexing uses PDFium too, and PDFium is not thread-safe
        document = pdfium.PdfDocument(str(path), password=password)
        try:
            return document[page].render(scale=1).to_pil().convert("RGB")
        finally:
            document.close()


def dark(image, box):
    """Fraction of dark pixels in a screen-space box (left, top, right, bottom)."""
    crop = image.crop(box).convert("L")
    data = crop.getdata()
    return sum(1 for value in data if value < 100) / max(1, len(data))


# ---------------------------------------------------------------- geometry
def test_frame_round_trips_for_every_rotation():
    for rotation in (0, 90, 180, 270):
        frame = Frame(10, 20, 310, 420, rotation)
        assert (frame.width, frame.height) == ((400, 300) if rotation in (90, 270) else (300, 400))
        for point in ((0, 0), (frame.width, 0), (37.5, 120.25), (frame.width, frame.height)):
            back = frame.to_display(*frame.to_pdf(*point))
            assert back == pytest.approx(list(point))
        # The top-left of the screen is a corner of the box.
        assert frame.to_pdf(0, 0)[0] in (10, 310) and frame.to_pdf(0, 0)[1] in (20, 420)


def test_helvetica_widths_match_the_renderer(tmp_path):
    """Our width table must agree with how a real PDF engine lays the text out."""
    text = "Confidential 2024 - Page 12 of 340 (draft) Wj@%"
    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page(page_size=(612, 792))
    font = pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1,
                                                BaseFont=pikepdf.Name.Helvetica, Encoding=pikepdf.Name.WinAnsiEncoding))
    pdf.pages[0].obj.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font))
    literal = text.replace("(", "\\(").replace(")", "\\)")
    pdf.pages[0].obj.Contents = pdf.make_stream(f"BT /F1 20 Tf 50 400 Td ({literal}) Tj ET".encode())
    path = tmp_path / "w.pdf"
    pdf.save(path)
    with PDFIUM_LOCK:
        document = pdfium.PdfDocument(str(path))
        text_page = document[0].get_textpage()
        count = text_page.count_chars()
        right = max(text_page.get_charbox(i)[2] for i in range(count))
        document.close()
    # The last glyph's ink ends slightly before its advance; allow that much.
    assert right - 50 == pytest.approx(text_width(text, 20), abs=20 * 0.12)


# ---------------------------------------------------------------- watermark / header / footer
def test_watermark_is_centred_on_normal_and_rotated_pages(client, settings, upload):
    doc = upload(make_pdf(2))
    op(client, doc["id"], "rotate", pages=[2], degrees=90)
    result = op(client, doc["id"], "watermark", text="DRAFT", opacity=1, angle=0, color="#000000")
    assert result.status_code == 200, result.text
    assert result.json()["result"]["report"]["pages_marked"] == 2
    path = current_path(client, settings, doc["id"])
    for page_index, (width, height) in enumerate([(612, 792), (792, 612)]):
        image = render(path, page_index)
        assert image.size == (width, height)
        centre = dark(image, (width / 2 - 60, height / 2 - 25, width / 2 + 60, height / 2 + 25))
        assert centre > 0.2, (page_index, centre)
        # Upright and horizontal: ink spreads sideways from the centre, not up and down.
        assert dark(image, (width / 2 - 150, height / 2 - 12, width / 2 - 90, height / 2 + 12)) > 0.05
        assert dark(image, (width / 2 - 20, height / 2 - 250, width / 2 + 20, height / 2 - 120)) == 0
    assert "DRAFT" in page_texts(path)[0]
    assert op(client, doc["id"], "watermark", text="गोपनीय").status_code == 400
    assert op(client, doc["id"], "watermark", text="  ").status_code == 400


def test_headers_footers_and_page_numbers(client, settings, upload):
    doc = upload(make_pdf(3), name="Annual Report.pdf")
    result = op(client, doc["id"], "header_footer", header={"left": "{name}", "right": "Internal"},
                footer={"center": "Page {page} of {pages}"}, first_page=2, number_from=1)
    assert result.status_code == 200, result.text
    path = current_path(client, settings, doc["id"])
    texts = page_texts(path)
    assert "Page 1 of 2" not in texts[0] and "Annual Report" not in texts[0]       # first page skipped
    assert "Annual Report" in texts[1] and "Internal" in texts[1] and "Page 1 of 2" in texts[1]
    assert "Page 2 of 2" in texts[2]

    image = render(path, 1)
    assert dark(image, (20, 20, 200, 50)) > 0.002                   # header, left
    assert dark(image, (480, 20, 600, 50)) > 0.002                  # header, right
    assert dark(image, (240, 740, 372, 775)) > 0.002                # footer, centred
    assert dark(image, (20, 740, 200, 775)) == 0                    # nothing bottom-left
    assert op(client, doc["id"], "header_footer", header={}, footer={}).status_code == 400
    assert op(client, doc["id"], "header_footer", footer={"middle": "x"}).status_code == 400
    assert op(client, doc["id"], "header_footer", footer={"center": "x"}, first_page=9).status_code == 400


# ---------------------------------------------------------------- protection
def test_protect_then_unlock(client, settings, upload):
    doc = upload(make_pdf(2))
    weak = client.post(f"/api/documents/{doc['id']}/protect", json={"owner_password": "abc"})
    assert weak.status_code == 400

    response = client.post(f"/api/documents/{doc['id']}/protect", json={
        "owner_password": "owner-pass-1", "user_password": "open-pass-1", "permissions": {"copy": False, "print": False}})
    assert response.status_code == 200, response.text
    report = response.json()["result"]["report"]
    assert report["encryption"] == "AES-256" and report["permissions"]["copy"] is False
    path = current_path(client, settings, doc["id"])

    with pytest.raises(pikepdf.PasswordError):
        pikepdf.Pdf.open(path)
    with pikepdf.Pdf.open(path, password="open-pass-1") as pdf:
        assert pdf.encryption.bits == 256 and pdf.allow.extract is False and pdf.allow.print_highres is False
        assert pdf.allow.accessibility is True and not pdf.owner_password_matched

    detail = client.get(f"/api/documents/{doc['id']}").json()
    assert detail["is_encrypted"] is True
    assert op(client, doc["id"], "reverse").status_code == 400            # protected: no edits
    assert client.get(f"/api/documents/{doc['id']}/export?format=txt").status_code == 400

    # Passwords never reach the database, the audit log, the job list or the log file.
    database = (settings.data_dir / "ard.sqlite3").read_bytes()
    wal = settings.data_dir / "ard.sqlite3-wal"
    stored = database + (wal.read_bytes() if wal.exists() else b"")
    log = settings.data_dir / "logs" / "ard.log"
    for secret in (b"owner-pass-1", b"open-pass-1"):
        assert secret not in stored
        assert secret not in (log.read_bytes() if log.exists() else b"")
    assert "owner-pass-1" not in str(client.get("/api/jobs").json()) + str(client.get("/api/system/audit").json())

    unlock = lambda password: client.post(f"/api/documents/{doc['id']}/unlock", json={"password": password})
    assert "not correct" in unlock("wrong").json()["detail"]
    # The open password alone must not strip the owner's restrictions.
    assert "owner" in unlock("open-pass-1").json()["detail"]
    assert client.get(f"/api/documents/{doc['id']}").json()["version_count"] == 2

    done = unlock("owner-pass-1")
    assert done.status_code == 200, done.text
    with pikepdf.Pdf.open(current_path(client, settings, doc["id"])) as pdf:
        assert not pdf.is_encrypted and len(pdf.pages) == 2
    detail = client.get(f"/api/documents/{doc['id']}").json()
    assert detail["is_encrypted"] is False and detail["version_count"] == 3
    assert op(client, doc["id"], "reverse").status_code == 200
    assert b"owner-pass-1" not in (settings.data_dir / "ard.sqlite3").read_bytes()


def test_imported_protected_pdf_can_be_unlocked_and_reverted(client, settings, upload):
    doc = upload(make_pdf(2, password="s3cret-test"), name="locked.pdf")
    assert doc["is_encrypted"] and doc["page_count"] is None
    done = client.post(f"/api/documents/{doc['id']}/unlock", json={"password": "s3cret-test"})
    assert done.status_code == 200, done.text
    detail = client.get(f"/api/documents/{doc['id']}").json()
    assert detail["page_count"] == 2 and detail["is_encrypted"] is False
    assert client.post(f"/api/documents/{doc['id']}/unlock", json={"password": "x"}).status_code == 400

    # Going back to the protected original copies it as it is and restores the protected state.
    first = detail["versions"][0]["id"]
    assert client.post(f"/api/documents/{doc['id']}/versions/{first}/revert").status_code == 200
    detail = client.get(f"/api/documents/{doc['id']}").json()
    assert detail["is_encrypted"] is True and detail["page_count"] is None and detail["version_count"] == 3


# ---------------------------------------------------------------- redaction
def area(page, x0, y0, x1, y1):
    return {"page": page, "box": {"tl": [x0, y1], "tr": [x1, y1], "bl": [x0, y0]}}


def make_secret_pdf():
    pdf = pikepdf.Pdf.open(io.BytesIO(make_pdf(2)))
    font = pdf.pages[0].obj.Resources.Font.F1
    pdf.pages[0].obj.Contents = pdf.make_stream(
        b"BT /F1 20 Tf 72 700 Td (Public heading) Tj ET BT /F1 20 Tf 72 500 Td (SECRET-ACCOUNT-99812) Tj ET")
    pdf.pages[0].obj.Annots = pikepdf.Array([pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name.Annot, Subtype=pikepdf.Name.Text, Rect=[72, 480, 92, 500],
        Contents=pikepdf.String("note mentions SECRET-ACCOUNT-99812")))])
    assert font is not None
    buffer = io.BytesIO()
    pdf.save(buffer)
    return buffer.getvalue()


def test_redaction_removes_the_content_and_is_verified(client, settings, upload):
    data = make_secret_pdf()
    doc = upload(data, name="statement.pdf")
    response = client.post(f"/api/documents/{doc['id']}/redactions", json={"areas": [area(1, 60, 490, 400, 525)]})
    assert response.status_code == 200, response.text
    report = response.json()["result"]["report"]
    assert report["verified"] is True and all(report["checks"].values())
    assert report["pages"] == [1] and report["annotations_removed"] == 1
    assert any("earlier versions" in warning for warning in report["warnings"])
    assert any("metadata" in warning.lower() for warning in report["warnings"])

    path = current_path(client, settings, doc["id"])
    raw = path.read_bytes()
    assert b"SECRET-ACCOUNT" not in raw
    with pikepdf.Pdf.open(path) as pdf:
        # Not in any stream of the file either, compressed or not.
        for obj in pdf.objects:
            if isinstance(obj, pikepdf.Stream):
                try:
                    data = obj.read_bytes()
                except pikepdf.PdfError:
                    continue                      # image data that is not decodable as bytes
                assert b"SECRET-ACCOUNT" not in data
            elif isinstance(obj, pikepdf.Dictionary) and "/Contents" in obj:
                assert "SECRET-ACCOUNT" not in str(obj["/Contents"])
        assert "/Annots" not in pdf.pages[0].obj and "/Font" not in pdf.pages[0].obj.Resources
    texts = page_texts(path)
    assert texts[0] == "" and texts[1] == "Page 2 marker"          # page 2 untouched and still text

    image = render(path)
    assert dark(image, (70, 792 - 520, 390, 792 - 495)) > 0.99     # the area is black
    assert dark(image, (72, 792 - 720, 300, 792 - 695)) > 0.02     # the public heading is still visible
    assert dark(image, (450, 100, 600, 200)) == 0

    # The original is untouched and still contains the text: ARD says so rather than hiding it.
    with pikepdf.Pdf.open(settings.data_dir / "originals" / f"{doc['id']}.pdf") as original:
        assert b"SECRET-ACCOUNT" in original.pages[0].Contents.read_bytes()
    assert "SECRET" not in str(client.get("/api/system/audit").json())


def test_redaction_on_a_rotated_page_lands_where_asked(client, settings, upload):
    doc = upload(make_secret_pdf())
    op(client, doc["id"], "rotate", pages=[1], degrees=90)
    # Same user-space box as before; on screen the page is now landscape.
    response = client.post(f"/api/documents/{doc['id']}/redactions", json={"areas": [area(1, 60, 490, 400, 525)]})
    assert response.status_code == 200, response.text
    path = current_path(client, settings, doc["id"])
    image = render(path)
    assert image.size == (792, 612)
    with pikepdf.Pdf.open(path) as check:
        frame = page_frame(check.pages[0])
    left, top = frame.to_display(60, 490)
    right, bottom = frame.to_display(400, 525)
    box = (min(left, right) + 3, min(top, bottom) + 3, max(left, right) - 3, max(top, bottom) - 3)
    assert dark(image, box) > 0.99
    assert page_texts(path)[0] == ""


def test_redaction_failures_create_nothing(client, settings, upload, monkeypatch):
    from app.workers import pdf_redact

    doc = upload(make_secret_pdf())
    for areas in ([], [area(9, 0, 0, 50, 50)], [{"page": 1}], [area(1, 5, 5, 5.1, 5.1)]):
        assert client.post(f"/api/documents/{doc['id']}/redactions", json={"areas": areas}).status_code in (400, 422)

    def failing_verify(output, areas):
        raise pdf_redact.RedactionError("Redaction could not be verified (simulated). Nothing was saved.")

    monkeypatch.setattr(pdf_redact, "verify", failing_verify)
    response = client.post(f"/api/documents/{doc['id']}/redactions", json={"areas": [area(1, 60, 490, 400, 525)]})
    assert response.status_code == 400 and "could not be verified" in response.json()["detail"]
    assert client.get(f"/api/documents/{doc['id']}").json()["version_count"] == 1
    folder = settings.data_dir / "versions" / doc["id"]
    assert not folder.exists() or list(folder.iterdir()) == []


def test_verification_really_detects_a_cosmetic_redaction(tmp_path):
    """A black box drawn over text (the wrong way) must fail verification."""
    from app.workers import pdf_redact

    pdf = pikepdf.Pdf.open(io.BytesIO(make_secret_pdf()))
    page = pdf.pages[0]
    del page.obj["/Annots"]
    page.contents_add(pdf.make_stream(b"q 0 0 0 rg 60 490 340 35 re f Q"))
    path = tmp_path / "cosmetic.pdf"
    pdf.save(path)
    with pytest.raises(pdf_redact.RedactionError) as error:
        pdf_redact.verify(str(path), [area(1, 60, 490, 400, 525)])
    assert "text removed" in str(error.value)


# ---------------------------------------------------------------- export
def test_export_text_and_images(client, settings, upload):
    doc = upload(make_pdf(3), name="notes.pdf")
    text = client.get(f"/api/documents/{doc['id']}/export?format=txt")
    assert text.status_code == 200 and text.headers["content-type"].startswith("text/plain")
    assert "attachment" in text.headers["content-disposition"] and "notes.txt" in text.headers["content-disposition"]
    body = text.text
    assert body.index("--- Page 1 ---") < body.index("Page 1 marker") < body.index("--- Page 2 ---") < body.index("Page 3 marker")

    for fmt, signature in (("png", b"\x89PNG"), ("jpg", b"\xff\xd8")):
        response = client.get(f"/api/documents/{doc['id']}/export?format={fmt}")
        assert response.status_code == 200 and response.headers["content-type"] == "application/zip"
        archive = zipfile.ZipFile(io.BytesIO(response.content))
        assert archive.namelist() == [f"page-1.{fmt}", f"page-2.{fmt}", f"page-3.{fmt}"]
        first = archive.read(f"page-1.{fmt}")
        assert first.startswith(signature)
        width, height = Image.open(io.BytesIO(first)).size
        assert abs(width - 1275) <= 1 and abs(height - 1650) <= 1        # US Letter at 150 dpi

    assert client.get(f"/api/documents/{doc['id']}/export?format=docx").status_code == 400
    exports = [e for e in client.get("/api/system/audit").json()["events"] if e["action"] == "document.export"]
    assert sorted(e["details"]["format"] for e in exports) == ["jpg", "png", "txt"]
    # The document itself did not change.
    assert client.get(f"/api/documents/{doc['id']}").json()["version_count"] == 1
