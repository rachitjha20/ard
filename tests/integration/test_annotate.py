"""Overlays and annotations: the output is rendered and checked pixel by pixel."""

import base64
import io

import pikepdf
import pypdf
import pypdfium2 as pdfium
import pytest
from PIL import Image

from app.services.pdf.operations import OperationError, plan_annotations
from app.workers.pdfium_lock import PDFIUM_LOCK
from tests.helpers import make_pdf, page_texts


def box(x0, y0, x1, y1):
    """A box given by PDF coordinates of its lower-left and upper-right corners (unrotated page)."""
    return {"tl": [x0, y1], "tr": [x1, y1], "bl": [x0, y0]}


def png(color=(0, 160, 0, 255), size=(40, 20)):
    buffer = io.BytesIO()
    Image.new("RGBA", size, color).save(buffer, "PNG")
    return base64.b64encode(buffer.getvalue()).decode()


def annotate(client, document_id, items, images=()):
    return client.post(f"/api/documents/{document_id}/annotations", json={"items": items, "images": list(images)})


def version_path(settings, document_id, version_id):
    return settings.data_dir / "versions" / document_id / f"{version_id}.pdf"


def render(path, page=0):
    with PDFIUM_LOCK:          # background indexing uses PDFium too, and PDFium is not thread-safe
        document = pdfium.PdfDocument(str(path))
        try:
            return document[page].render(scale=1).to_pil().convert("RGB")
        finally:
            document.close()


def pixel(image, x, y):
    """Colour at PDF point (x, y) on a 612x792 page rendered at scale 1."""
    return image.getpixel((round(x), round(792 - y)))


def near(color, target, tolerance=40):
    return all(abs(a - b) <= tolerance for a, b in zip(color, target))


def test_every_item_type_is_drawn_where_asked(client, settings, upload):
    doc = upload(make_pdf(2))
    items = [
        {"type": "text", "page": 1, "box": box(72, 560, 300, 600), "text": "Overlay café\nsecond line", "size": 18, "color": "#0000ff"},
        {"type": "image", "page": 1, "box": box(300, 300, 400, 350), "image": 0},
        {"type": "stamp", "page": 1, "box": box(72, 400, 252, 450), "text": "approved"},
        {"type": "rect", "page": 1, "box": box(420, 600, 520, 680), "color": "#ff0000", "width": 4, "fill": "#00ffff"},
        {"type": "ellipse", "page": 1, "box": box(420, 480, 520, 560), "color": "#008800", "width": 4},
        {"type": "arrow", "page": 1, "p1": [72, 200], "p2": [250, 200], "color": "#ff00ff", "width": 4},
        {"type": "ink", "page": 1, "points": [[300, 100], [360, 100], [360, 160]], "color": "#000000", "width": 4},
        {"type": "highlight", "page": 1, "quads": [[[72, 730], [400, 730], [72, 690], [400, 690]]], "text": "Page 1 marker"},
        {"type": "note", "page": 2, "point": [100, 600], "text": "नमस्ते — please review"},
    ]
    response = annotate(client, doc["id"], items, [png()])
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result["page_count"] == 2 and result["version_number"] == 2
    assert result["report"]["overlays"] == 3 and result["report"]["annotations"] == 6
    path = version_path(settings, doc["id"], result["version_id"])

    image = render(path)
    assert near(pixel(image, 350, 325), (0, 160, 0))            # image
    assert near(pixel(image, 470, 640), (0, 255, 255))          # rectangle fill
    assert near(pixel(image, 420, 640), (255, 0, 0))            # rectangle border
    assert near(pixel(image, 470, 520), (255, 255, 255))        # ellipse is not filled
    assert near(pixel(image, 420, 520), (0, 136, 0), 60)        # ellipse outline
    assert near(pixel(image, 150, 200), (255, 0, 255))          # arrow shaft
    assert near(pixel(image, 330, 100), (0, 0, 0))              # drawing
    assert near(pixel(image, 380, 710), (255, 235, 165), 45)    # highlight tint on white
    assert near(pixel(image, 74, 425), (179, 38, 30), 50)       # stamp border
    assert near(pixel(image, 550, 60), (255, 255, 255))         # untouched area stays white

    # Overlay text is real text; the original text is still there; the stamp is upper-cased.
    text = page_texts(path)[0]
    assert "Page 1 marker" in text and "Overlay café" in text and "second line" in text and "APPROVED" in text

    with pikepdf.Pdf.open(path) as pdf:
        subtypes = sorted(str(a.Subtype) for a in pdf.pages[0].Annots)
        assert subtypes == ["/Circle", "/Highlight", "/Ink", "/Line", "/Square"]
        note = pdf.pages[1].Annots[0]
        assert str(note.Subtype) == "/Text" and str(note.Contents) == "नमस्ते — please review"
        assert all(str(a.NM).startswith("ard-") and "/AP" in a for a in pdf.pages[0].Annots)

    # Item text and images were not left in temporary files or copied into the audit log.
    assert list((settings.data_dir / "temporary").iterdir()) == []
    audit = str(client.get("/api/system/audit").json())
    assert "please review" not in audit and "Overlay" not in audit
    jobs = str(client.get("/api/jobs").json())
    assert "please review" not in jobs

    # The original is untouched.
    assert page_texts(settings.data_dir / "originals" / f"{doc['id']}.pdf") == ["Page 1 marker", "Page 2 marker"]


def test_overlay_is_upright_on_a_rotated_page(client, settings, upload):
    """Boxes are given as seen on screen, so the same code works whatever the page rotation."""
    doc = upload(make_pdf(1))
    client.post(f"/api/documents/{doc['id']}/operations", json={"operation": "rotate", "params": {"degrees": 90}})
    # On a page rotated 90 degrees clockwise, screen-right is PDF-down and screen-down is PDF-left.
    # A box that looks 200 wide and 50 tall on screen:
    items = [{"type": "image", "page": 1, "image": 0, "box": {"tl": [300, 700], "tr": [300, 500], "bl": [250, 700]}}]
    result = annotate(client, doc["id"], items, [png(size=(200, 50))]).json()["result"]
    image = render(version_path(settings, doc["id"], result["version_id"]))
    assert image.size == (792, 612)
    green = [(x, y) for x in range(0, 792, 4) for y in range(0, 612, 4) if near(image.getpixel((x, y)), (0, 160, 0))]
    xs, ys = [p[0] for p in green], [p[1] for p in green]
    assert 190 <= max(xs) - min(xs) <= 204 and 40 <= max(ys) - min(ys) <= 54     # wide, not tall


def test_inherited_page_resources_survive_an_overlay(client, settings, upload):
    """A page whose fonts are inherited from its parent must still show its own text afterwards."""
    pdf = pikepdf.Pdf.open(io.BytesIO(make_pdf(1)))
    page = pdf.pages[0]
    pdf.Root.Pages.Resources = page.obj.Resources
    del page.obj["/Resources"]
    buffer = io.BytesIO()
    pdf.save(buffer)
    doc = upload(buffer.getvalue(), name="inherited.pdf")
    result = annotate(client, doc["id"], [{"type": "text", "page": 1, "box": box(72, 300, 300, 330), "text": "Added"}]).json()["result"]
    assert page_texts(version_path(settings, doc["id"], result["version_id"])) == ["Page 1 marker\nAdded"]


def test_remove_annotations_keeps_links_and_overlays(client, settings, upload):
    pdf = pikepdf.Pdf.open(io.BytesIO(make_pdf(1)))
    pdf.pages[0].obj.Annots = pikepdf.Array([pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name.Annot, Subtype=pikepdf.Name.Link, Rect=[10, 10, 50, 50])),
        pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.Annot, Subtype=pikepdf.Name.Square, Rect=[60, 60, 90, 90]))])
    buffer = io.BytesIO()
    pdf.save(buffer)
    doc = upload(buffer.getvalue(), name="links.pdf")
    annotate(client, doc["id"], [
        {"type": "text", "page": 1, "box": box(72, 300, 300, 330), "text": "Stays"},
        {"type": "rect", "page": 1, "box": box(100, 100, 200, 200)},
    ])

    def op(scope):
        return client.post(f"/api/documents/{doc['id']}/operations", json={"operation": "remove_annotations", "params": {"scope": scope}})

    first = op("ard").json()["result"]
    assert first["report"]["removed"] == 1
    path = version_path(settings, doc["id"], first["version_id"])
    with pikepdf.Pdf.open(path) as check:
        assert sorted(str(a.Subtype) for a in check.pages[0].Annots) == ["/Link", "/Square"]
    assert "Stays" in page_texts(path)[0]

    assert op("ard").status_code == 400                      # nothing of ARD's left: no empty version
    second = op("all").json()["result"]
    with pikepdf.Pdf.open(version_path(settings, doc["id"], second["version_id"])) as check:
        assert [str(a.Subtype) for a in check.pages[0].Annots] == ["/Link"]
    assert client.get(f"/api/documents/{doc['id']}").json()["version_count"] == 4


def test_bad_requests_change_nothing(client, settings, upload):
    doc = upload(make_pdf(1))
    good_box = box(10, 10, 100, 100)
    bad = [
        [],
        [{"type": "rect", "page": 2, "box": good_box}],
        [{"type": "laser", "page": 1}],
        [{"type": "rect", "page": 1, "box": box(10, 10, 10.2, 10.2)}],
        [{"type": "rect", "page": 1, "box": {"tl": [0, float("1e9")], "tr": [1, 1], "bl": [2, 2]}}],
        [{"type": "rect", "page": 1, "box": good_box, "color": "red"}],
        [{"type": "rect", "page": 1, "box": good_box, "color": "#ff0000) Tj ET"}],
        [{"type": "text", "page": 1, "box": good_box, "text": "नमस्ते"}],
        [{"type": "text", "page": 1, "box": good_box, "text": "   "}],
        [{"type": "stamp", "page": 1, "box": good_box, "text": "PAID (1)"}],
        [{"type": "image", "page": 1, "box": good_box, "image": 0}],
        [{"type": "ink", "page": 1, "points": [[1, 1]]}],
        [{"type": "note", "page": 1, "point": [1, 2, 3], "text": "x"}],
        [{"type": "line", "page": 1, "p1": [5, 5], "p2": [5, 5]}],
    ]
    for items in bad:
        response = annotate(client, doc["id"], items)
        assert response.status_code in (400, 422), items
    assert annotate(client, doc["id"], [{"type": "image", "page": 1, "box": good_box, "image": 0}],
                    [base64.b64encode(b"GIF89a not allowed").decode()]).status_code == 400
    assert annotate(client, doc["id"], [{"type": "image", "page": 1, "box": good_box, "image": 0}],
                    ["%%% not base64"]).status_code == 400
    # A PNG signature followed by garbage is caught in the worker and fails cleanly.
    broken = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"garbage" * 20).decode()
    failed = annotate(client, doc["id"], [{"type": "image", "page": 1, "box": good_box, "image": 0}], [broken])
    assert failed.status_code == 400 and "image" in failed.json()["detail"].lower()

    assert client.get(f"/api/documents/{doc['id']}").json()["version_count"] == 1
    assert not (settings.data_dir / "versions" / doc["id"]).exists() or not list((settings.data_dir / "versions" / doc["id"]).iterdir())
    assert list((settings.data_dir / "temporary").iterdir()) == []


def test_text_cannot_inject_pdf_operators(client, settings, upload):
    doc = upload(make_pdf(1))
    hostile = "safe) Tj ET 1 0 0 rg 0 0 612 792 re f BT (x \\ still text"
    result = annotate(client, doc["id"], [{"type": "text", "page": 1, "box": box(72, 300, 500, 330), "text": hostile}]).json()["result"]
    path = version_path(settings, doc["id"], result["version_id"])
    assert near(pixel(render(path), 550, 60), (255, 255, 255))      # the page was not painted red
    assert hostile in pypdf.PdfReader(str(path)).pages[0].extract_text()


def test_validation_limits():
    good = {"type": "rect", "page": 1, "box": box(0, 0, 10, 10)}
    assert plan_annotations([good], 1)[0]["color"] == "#b3261e"
    with pytest.raises(OperationError):
        plan_annotations([good] * 2001, 1)
    with pytest.raises(OperationError):
        plan_annotations([{"type": "ink", "page": 1, "points": [[0, 0]] * 5001}], 1)
    with pytest.raises(OperationError):
        plan_annotations([{**good, "opacity": 0}], 1)
    with pytest.raises(OperationError):
        plan_annotations([{**good, "page": True}], 1)
    # Unknown keys are dropped rather than passed to the PDF writer.
    assert "evil" not in plan_annotations([{**good, "evil": "/JS (app.alert(1))"}], 1)[0]


def test_annotating_respects_policy_and_encryption(client, upload):
    locked = upload(make_pdf(1, password="s3cret-test"), name="locked.pdf")
    assert annotate(client, locked["id"], [{"type": "rect", "page": 1, "box": box(0, 0, 50, 50)}]).status_code == 400
    doc = upload(make_pdf(1))
    client.delete(f"/api/documents/{doc['id']}", headers={"X-ARD-Confirm": "yes"})
    assert annotate(client, doc["id"], [{"type": "rect", "page": 1, "box": box(0, 0, 50, 50)}]).status_code == 403
