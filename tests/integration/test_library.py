"""Phase 4: text index, search, categories, tags, smart albums, duplicates and OCR."""

import io
import shutil
import time
from pathlib import Path

import pikepdf
import pytest

from app.workers.pdfium_lock import PDFIUM_LOCK
from tests.helpers import make_pdf, make_scanned_pdf, page_texts

HAS_TESSERACT = shutil.which("tesseract") is not None
DEVANAGARI_FONT = next((str(p) for p in Path("/usr/share/fonts").rglob("NotoSansDevanagari-Regular.ttf")), None)


def text_pdf(pages: list[str], title: str | None = None) -> bytes:
    """A PDF whose pages contain the given (Latin) text, one line per sentence."""
    pdf = pikepdf.Pdf.new()
    font = pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1,
                                                BaseFont=pikepdf.Name.Helvetica, Encoding=pikepdf.Name.WinAnsiEncoding))
    for body in pages:
        pdf.add_blank_page(page_size=(612, 792))
        page = pdf.pages[-1]
        page.obj.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font))
        lines = [line.strip() for line in body.split(".") if line.strip()]
        content = "BT /F1 12 Tf 14 TL 72 720 Td " + " ".join(
            "(" + line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") + ".) Tj T*" for line in lines) + " ET"
        page.obj.Contents = pdf.make_stream(content.encode("cp1252"))
    if title:
        pdf.docinfo["/Title"] = title
    buffer = io.BytesIO()
    pdf.save(buffer)
    return buffer.getvalue()


def settle(client, timeout=120):
    """Wait until background jobs (indexing, OCR) have finished."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not client.get("/api/jobs?active=true").json()["jobs"]:
            return
        time.sleep(0.05)
    raise AssertionError("background jobs did not finish")


INVOICE = ("Tax Invoice. Invoice number 4471. Bill to Acme Traders. Consulting services rendered in March. "
           "Subtotal 1000. Amount due 1250 rupees. Payment terms thirty days")
CONTRACT = ("This Agreement is made between the parties. Whereas the supplier agrees to deliver goods. "
            "Hereinafter called the Supplier. Governing law of India applies. Termination by written notice")
PAPER = ("Abstract. This paper studies river sediment. Introduction. Prior work by Rao et al is reviewed. "
         "Methodology. Samples were collected monthly. References. Journal of Hydrology")


# ---------------------------------------------------------------- indexing and search
def test_search_finds_words_inside_documents_with_page_references(client, upload):
    invoice = upload(text_pdf([INVOICE, "Second page. Delivery address is Pune"]), name="march-bill.pdf")
    contract = upload(text_pdf([CONTRACT]), name="supply.pdf")
    settle(client)

    found = client.get("/api/search?q=sediment").json()
    assert found["total"] == 0

    result = client.get("/api/search?q=consulting").json()
    assert [r["document"]["id"] for r in result["results"]] == [invoice["id"]]
    hit = result["results"][0]
    assert hit["pages"] == [1] and hit["snippet"]["page"] == 1
    assert hit["snippet"]["match"].lower() == "consulting" and "services" in hit["snippet"]["after"]

    assert client.get("/api/search?q=Pune").json()["results"][0]["pages"] == [2]            # the citation is the real page
    assert client.get("/api/search?q=CONSULT").json()["total"] == 1                         # part of a word, any case
    both = client.get("/api/search?q=supplier goods").json()                                # every word must be present
    assert [r["document"]["id"] for r in both["results"]] == [contract["id"]]
    assert client.get("/api/search?q=supplier rupees").json()["total"] == 0
    by_name = client.get("/api/search?q=march-bill").json()["results"][0]
    assert by_name["name_match"] is True and by_name["document"]["id"] == invoice["id"]
    for odd in ('"', "a*b OR (", "%", "NEAR(", "'; DROP TABLE documents;--"):                # never a query error
        assert client.get("/api/search", params={"q": odd}).status_code == 200
    assert client.get("/api/search?q=").json()["results"] == []


def test_index_follows_the_current_version_and_skips_trash(client, upload):
    doc = upload(text_pdf(["First page mentions walrus", "Second page mentions pelican"]))
    settle(client)
    assert client.get("/api/search?q=walrus").json()["total"] == 1
    client.post(f"/api/documents/{doc['id']}/operations", json={"operation": "delete_pages", "params": {"pages": [1]}})
    settle(client)
    assert client.get("/api/search?q=walrus").json()["total"] == 0          # gone from the current version
    hit = client.get("/api/search?q=pelican").json()["results"][0]
    assert hit["pages"] == [1]                                              # and the page number followed the edit
    client.delete(f"/api/documents/{doc['id']}", headers={"X-ARD-Confirm": "yes"})
    assert client.get("/api/search?q=pelican").json()["total"] == 0
    client.post(f"/api/documents/{doc['id']}/restore")
    assert client.get("/api/search?q=pelican").json()["total"] == 1


def test_documents_imported_before_search_existed_are_indexed_on_start(settings):
    from fastapi.testclient import TestClient
    from sqlalchemy import text

    from app.core.context import build_context
    from app.main import create_app

    with TestClient(create_app(settings), base_url="http://127.0.0.1:8765") as client:
        client.headers["Authorization"] = "Bearer " + settings.token_path.read_text()
        client.post("/api/documents", files=[("files", ("a.pdf", text_pdf(["Contains the word ocelot"]), "application/pdf"))])
        settle(client)
    context = build_context(settings)          # simulate the state before the index existed
    with context.sessions() as session:
        session.execute(text("UPDATE documents SET indexed_version_id = NULL"))
        session.execute(text("DELETE FROM page_fts"))
        session.execute(text("DELETE FROM page_texts"))
        session.commit()
    context.engine.dispose()
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8765") as client:
        client.headers["Authorization"] = "Bearer " + settings.token_path.read_text()
        time.sleep(0.2)
        settle(client)
        assert client.get("/api/search?q=ocelot").json()["total"] == 1


def test_search_text_is_not_copied_into_jobs_or_audit(client, upload):
    upload(text_pdf(["The secret codename is BLUEHERON"]))
    settle(client)
    assert client.get("/api/search?q=blueheron").json()["total"] == 1
    exposed = str(client.get("/api/jobs?limit=200").json()) + str(client.get("/api/system/audit").json())
    assert "BLUEHERON" not in exposed and "codename" not in exposed


# ---------------------------------------------------------------- categories
def test_categories_are_suggested_with_evidence_and_can_be_overridden(client, upload):
    invoice = upload(text_pdf([INVOICE]), name="scan0001.pdf")
    contract = upload(text_pdf([CONTRACT]), name="doc2.pdf")
    plain = upload(make_pdf(1), name="misc.pdf")
    settle(client)

    get = lambda d: client.get(f"/api/documents/{d['id']}").json()
    first = get(invoice)
    assert first["category"] == "invoices_receipts" and first["category_label"] == "Invoices and receipts"
    assert first["category_source"] == "auto" and 0.5 < first["category_confidence"] <= 0.99
    phrases = [item["phrase"] for item in first["category_evidence"]]
    assert "invoice" in phrases and all(item["count"] >= 1 for item in first["category_evidence"])
    assert get(contract)["category"] == "contracts"
    assert get(plain)["category"] == "uncategorized" and get(plain)["category_confidence"] is None

    # The user's correction wins, is recorded, and survives re-indexing.
    changed = client.put(f"/api/documents/{invoice['id']}/category", json={"category": "tax_finance"}).json()
    assert changed["category"] == "tax_finance" and changed["category_source"] == "user"
    client.post(f"/api/documents/{invoice['id']}/operations", json={"operation": "rotate", "params": {"degrees": 90}})
    settle(client)
    assert get(invoice)["category"] == "tax_finance" and get(invoice)["category_source"] == "user"
    # Going back to automatic re-derives the suggestion.
    back = client.put(f"/api/documents/{invoice['id']}/category", json={"category": "auto"}).json()
    assert back["category"] == "invoices_receipts" and back["category_source"] == "auto"
    assert client.put(f"/api/documents/{invoice['id']}/category", json={"category": "nonsense"}).status_code == 400

    listed = client.get("/api/documents?category=contracts").json()
    assert [d["id"] for d in listed["documents"]] == [contract["id"]]
    overview = client.get("/api/library/overview").json()
    counts = {c["id"]: c["count"] for c in overview["categories"]}
    assert counts["invoices_receipts"] == 1 and counts["contracts"] == 1 and counts["uncategorized"] == 1
    assert {a["id"]: a["count"] for a in overview["albums"]}["unclassified"] == 1
    actions = [e["details"] for e in client.get("/api/system/audit").json()["events"] if "category" in e["details"]]
    assert len(actions) == 2


def test_classifier_rules():
    from app.services.library.classifier import CATEGORIES, classify

    assert classify("", "").category == "uncategorized"
    assert classify("hello world this is a short note", "").category == "uncategorized"
    hindi = classify("यह एक चालान है। कुल राशि 500 रुपये। रसीद संलग्न है।", "")
    assert hindi.category == "invoices_receipts" and "चालान" in [e["phrase"] for e in hindi.evidence]
    assert classify("", "Rental Agreement 2024 contract.pdf").category == "contracts"       # the name alone can decide
    # A word inside another word must not count: "invoiced" is not "invoice", "tds" is not in "outdsx".
    assert classify("we invoiced them and reinvoiced again " * 5, "").category == "uncategorized"
    medical = classify("Patient name. Diagnosis: fracture. Prescription and dosage. Hospital discharge summary", "")
    assert medical.category == "medical_insurance" and medical.label == CATEGORIES["medical_insurance"]
    assert 0 < medical.confidence <= 0.99


# ---------------------------------------------------------------- tags and albums
def test_tags(client, upload):
    doc = upload(text_pdf([INVOICE]))
    other = upload(text_pdf([CONTRACT]), name="b.pdf")
    tagged = client.put(f"/api/documents/{doc['id']}/tags", json={"tags": ["Clients", " 2024 ", "clients", ""]}).json()
    assert tagged["tags"] == ["2024", "Clients"]                       # trimmed, de-duplicated without regard to case
    client.put(f"/api/documents/{other['id']}/tags", json={"tags": ["clients"]})
    assert client.get("/api/documents?tag=CLIENTS").json()["total"] == 2
    overview = {t["name"]: t["count"] for t in client.get("/api/library/overview").json()["tags"]}
    assert overview == {"2024": 1, "Clients": 2}
    client.put(f"/api/documents/{doc['id']}/tags", json={"tags": []})
    assert [t["name"] for t in client.get("/api/library/overview").json()["tags"]] == ["Clients"]   # unused tag removed
    for bad in (["<script>"], ["x" * 41], [5], "nope"):
        assert client.put(f"/api/documents/{doc['id']}/tags", json={"tags": bad}).status_code in (400, 422)


def test_duplicate_candidates_are_grouped_and_nothing_is_deleted(client, settings, upload):
    long_text = " ".join(f"Clause {n} states that the supplier shall deliver item {n} within {n} days of the order."
                         for n in range(1, 60))
    original = upload(text_pdf([long_text]), name="original.pdf")
    # Same text, different bytes (as after re-saving in another program).
    resaved = upload(text_pdf([long_text], title="Resaved copy"), name="resaved.pdf")
    # Nearly the same text.
    edited = upload(text_pdf([long_text.replace("Clause 7 states", "Clause 7 now states")]), name="edited.pdf")
    upload(text_pdf([PAPER]), name="unrelated.pdf")
    # Identical file: an extract of the whole single page is byte-different, so copy via merge of the same doc instead.
    settle(client)

    groups = client.get("/api/library/duplicates").json()
    kinds = {group["kind"]: {d["id"] for d in group["documents"]} for group in groups["groups"]}
    assert kinds["identical_text"] == {original["id"], resaved["id"]}
    similar = [g for g in groups["groups"] if g["kind"] == "similar_text"]
    assert similar and edited["id"] in {d["id"] for g in similar for d in g["documents"]}
    assert all(g["similarity"] >= 90 for g in similar)
    assert all("unrelated" not in d["display_name"] for g in groups["groups"] for d in g["documents"])
    assert "never deletes" in groups["note"]
    assert client.get("/api/documents").json()["total"] == 4
    assert len(list((settings.data_dir / "originals").iterdir())) == 4


def test_smart_albums(client, upload):
    upload(text_pdf([INVOICE]), name="a.pdf")
    locked = upload(make_pdf(1, password="s3cret-test"), name="locked.pdf")
    settle(client)
    albums = {a["id"]: a["count"] for a in client.get("/api/library/overview").json()["albums"]}
    assert albums["recent_added"] == 2 and albums["protected"] == 1 and albums["needs_ocr"] == 0 and albums["large"] == 0
    assert [d["id"] for d in client.get("/api/documents?view=protected").json()["documents"]] == [locked["id"]]
    assert client.get(f"/api/documents/{locked['id']}").json()["ocr_status"] == "locked"
    assert client.get("/api/documents?view=recent_modified").json()["total"] == 0


# ---------------------------------------------------------------- OCR
pytestmark_ocr = pytest.mark.skipif(not HAS_TESSERACT, reason="Tesseract is not installed")


def run_ocr(client, document_id, languages=None):
    response = client.post(f"/api/documents/{document_id}/ocr", json={"languages": languages})
    assert response.status_code == 202, response.text
    job_id = response.json()["job"]["id"]
    settle(client, timeout=300)
    return client.get(f"/api/jobs/{job_id}").json()


@pytestmark_ocr
def test_scanned_document_is_detected_then_made_searchable(client, settings, upload):
    scan = make_scanned_pdf([["Invoice number 4471", "Total amount due 1250 rupees", "Payment within thirty days"],
                             ["Second page of the scan", "Delivered to the warehouse"]])
    doc = upload(scan, name="scan.pdf")
    settle(client)
    before = client.get(f"/api/documents/{doc['id']}").json()
    assert before["ocr_status"] == "needed" and before["category"] == "uncategorized"
    assert client.get("/api/search?q=warehouse").json()["total"] == 0
    assert {a["id"]: a["count"] for a in client.get("/api/library/overview").json()["albums"]}["needs_ocr"] == 1
    pages = client.get(f"/api/documents/{doc['id']}/text").json()["pages"]
    assert [p["needs_ocr"] for p in pages] == [True, True]

    job = run_ocr(client, doc["id"], ["eng"])
    assert job["status"] == "succeeded", job
    report = job["result"]["report"]
    assert report["pages_recognised"] == [1, 2] and report["languages"] == ["eng"]
    assert report["mean_confidence"] > 70 and all(p["words"] >= 3 for p in report["pages"])

    after = client.get(f"/api/documents/{doc['id']}").json()
    assert after["ocr_status"] == "done" and after["version_count"] == 2 and after["page_count"] == 2
    # The recognised text is searchable, with the right page, and it re-classified the document.
    hit = client.get("/api/search?q=warehouse").json()["results"][0]
    assert hit["pages"] == [2] and hit["snippet"]["from_ocr"] is True
    assert after["category"] == "invoices_receipts"
    review = client.get(f"/api/documents/{doc['id']}/text?page=1").json()
    assert review["pages"][0]["source"] == "ocr" and review["pages"][0]["confidence"] > 70
    assert "4471" in review["text"]["text"]

    # The text layer really is in the file, the scan itself is unchanged, and the original is kept.
    version = next(v for v in after["versions"] if v["is_current"])
    output = settings.data_dir / "versions" / doc["id"] / f"{version['id']}.pdf"
    assert "Invoice number 4471" in page_texts(output)[0]
    with pikepdf.Pdf.open(output) as result, pikepdf.Pdf.open(io.BytesIO(scan)) as source:
        assert result.pages[0].Resources.XObject.Im0.read_raw_bytes() == source.pages[0].Resources.XObject.Im0.read_raw_bytes()
    assert page_texts(settings.data_dir / "originals" / f"{doc['id']}.pdf") == ["", ""]

    again = client.post(f"/api/documents/{doc['id']}/ocr", json={"languages": ["eng"]})
    assert again.status_code == 400 and "already has text" in again.json()["detail"]
    exposed = str(client.get("/api/jobs?limit=200").json()) + str(client.get("/api/system/audit").json())
    assert "warehouse" not in exposed and "4471" not in exposed


@pytestmark_ocr
def test_ocr_requests_are_validated(client, upload):
    doc = upload(text_pdf([INVOICE]))
    settle(client)
    nothing = client.post(f"/api/documents/{doc['id']}/ocr", json={"languages": ["eng"]})
    assert nothing.status_code == 400 and "already has text" in nothing.json()["detail"]
    scan = upload(make_scanned_pdf([["Hello"]]), name="s.pdf")
    settle(client)
    missing = client.post(f"/api/documents/{scan['id']}/ocr", json={"languages": ["xyz"]})
    assert missing.status_code == 400 and "not installed" in missing.json()["detail"]
    assert client.post(f"/api/documents/{scan['id']}/ocr", json={"languages": ["eng; rm -rf /"]}).status_code == 400
    locked = upload(make_pdf(1, password="s3cret-test"), name="l.pdf")
    assert client.post(f"/api/documents/{locked['id']}/ocr", json={}).status_code == 400


@pytestmark_ocr
def test_blank_scan_creates_no_version(client, upload):
    doc = upload(make_scanned_pdf([[]]), name="blank.pdf")
    settle(client)
    job = run_ocr(client, doc["id"], ["eng"])
    assert job["status"] == "failed" and "No text was recognised" in job["error"]
    assert client.get(f"/api/documents/{doc['id']}").json()["version_count"] == 1


@pytest.mark.skipif(not HAS_TESSERACT or DEVANAGARI_FONT is None or "hin" not in
                    (shutil.which("tesseract") and __import__("subprocess").run(
                        ["tesseract", "--list-langs"], capture_output=True, text=True).stdout or ""),
                    reason="Hindi OCR needs Tesseract's hin pack and a Devanagari font")
def test_hindi_ocr_and_search(client, upload):
    doc = upload(make_scanned_pdf([["यह एक चालान है", "कुल राशि पांच सौ रुपये"]], font_path=DEVANAGARI_FONT), name="hindi.pdf")
    settle(client)
    job = run_ocr(client, doc["id"], ["hin", "eng"])
    assert job["status"] == "succeeded", job
    review = client.get(f"/api/documents/{doc['id']}/text?page=1").json()["text"]["text"]
    assert "चालान" in review
    hit = client.get("/api/search", params={"q": "चालान"}).json()
    assert hit["total"] == 1 and hit["results"][0]["snippet"]["match"] == "चालान"
    assert client.get(f"/api/documents/{doc['id']}").json()["category"] == "invoices_receipts"


def test_mcp_actor_can_search_but_not_change_the_library(context):
    from app.core.policy.engine import Actor, PolicyDenied

    document_id = context.documents.import_document(Actor(interface="cli"), io.BytesIO(text_pdf([INVOICE])), "i.pdf")["document"]["id"]
    agent = Actor(name="agent", interface="mcp")
    assert "results" in context.library.search(agent, "invoice")
    assert context.library.overview(agent)["categories"]
    for call in (lambda: context.library.set_category(agent, document_id, "contracts"),
                 lambda: context.library.set_tags(agent, document_id, ["x"]),
                 lambda: context.library.run_ocr(agent, document_id, ["eng"])):
        with pytest.raises((PolicyDenied, Exception)) as error:
            call()
        assert isinstance(error.value, PolicyDenied) or "Tesseract" in str(error.value) or "already has text" in str(error.value)
    with pytest.raises(PolicyDenied):
        context.library.set_category(agent, document_id, "contracts")
