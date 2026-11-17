"""Phase 3, second slice: forms, cryptographic signatures, office conversion, PDF/A check."""

import io
import json
import shutil
from pathlib import Path

import pikepdf
import pytest
from fastapi.testclient import TestClient

from app.core.config.settings import Settings
from app.main import create_app
from app.workers.pdfium_lock import PDFIUM_LOCK
from tests.helpers import make_form_pdf, make_p12, make_pdf, page_texts


def current_path(client, settings, document_id):
    doc = client.get(f"/api/documents/{document_id}").json()
    version = next(v for v in doc["versions"] if v["is_current"])
    if version["number"] == 1:
        return settings.data_dir / "originals" / f"{document_id}.pdf"
    return settings.data_dir / "versions" / document_id / f"{version['id']}.pdf"


def sign(client, document_id, p12=None, passphrase="test-pass", **fields):
    return client.post(f"/api/documents/{document_id}/sign", data={"passphrase": passphrase, **fields},
                       files={"certificate": ("signer.p12", p12 if p12 is not None else make_p12(), "application/x-pkcs12")})


# ---------------------------------------------------------------- forms
def test_form_fields_are_listed_with_types_and_options(client, upload):
    doc = upload(make_form_pdf(), name="application.pdf")
    form = client.get(f"/api/documents/{doc['id']}/form").json()
    assert form["has_form"] and not form["xfa"]
    fields = {f["name"]: f for f in form["fields"]}
    assert set(fields) == {"full_name", "notes", "reference", "agree", "country", "plan"}
    assert fields["full_name"]["type"] == "text" and fields["full_name"]["required"] and fields["full_name"]["label"] == "Full name"
    assert fields["notes"]["type"] == "multiline"
    assert fields["reference"]["read_only"] and fields["reference"]["value"] == "REF-001"
    assert fields["agree"] == {**fields["agree"], "type": "checkbox", "value": False}
    assert fields["country"]["options"] == ["India", "Nepal", "Bhutan"] and fields["country"]["type"] == "choice"
    assert fields["plan"]["options"] == ["basic", "pro"] and fields["plan"]["type"] == "radio"

    plain = upload(make_pdf(1), name="plain.pdf")
    assert client.get(f"/api/documents/{plain['id']}/form").json() == {"fields": [], "has_form": False, "xfa": False}


def test_filling_a_form_saves_values_that_other_software_can_read(client, settings, upload):
    doc = upload(make_form_pdf())
    values = {"full_name": "Asha Rao", "notes": "first line\nsecond line", "agree": True, "country": "Nepal", "plan": "pro"}
    response = client.post(f"/api/documents/{doc['id']}/form", json={"values": values})
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result["report"]["filled"] == 5 and result["version_number"] == 2 and result["page_count"] == 1
    path = current_path(client, settings, doc["id"])

    # Read back with a different library than the one that wrote it.
    import pypdf
    stored = {name: field.get("/V") for name, field in pypdf.PdfReader(str(path)).get_fields().items()}
    assert stored["full_name"] == "Asha Rao" and stored["country"] == "Nepal"
    assert stored["agree"] == "/Yes" and stored["plan"] == "/pro" and stored["reference"] == "REF-001"

    # The values are actually drawn, not only stored.
    import pypdfium2 as pdfium
    with PDFIUM_LOCK:
        document = pdfium.PdfDocument(str(path))
        document.init_forms()
        image = document[0].render(scale=1, may_draw_forms=True).to_pil().convert("L")
        document.close()
    name_box = image.crop((72, 792 - 620, 300, 792 - 600))
    assert sum(1 for value in name_box.getdata() if value < 100) > 30

    listed = {f["name"]: f["value"] for f in client.get(f"/api/documents/{doc['id']}/form").json()["fields"]}
    assert listed == {**values, "reference": "REF-001"}

    # Values are document content: not in the audit log, the job list or temporary files.
    exposed = str(client.get("/api/system/audit").json()) + str(client.get("/api/jobs").json())
    assert "Asha Rao" not in exposed
    assert list((settings.data_dir / "temporary").iterdir()) == []
    assert page_texts(settings.data_dir / "originals" / f"{doc['id']}.pdf") == ["Page 1 marker"]


def test_bad_form_input_is_refused_and_creates_no_version(client, settings, upload):
    doc = upload(make_form_pdf())
    for values in ({"nope": "x"}, {"reference": "changed"}, {"agree": "yes"}, {"country": "Mars"}, {"plan": "gold"},
                   {"full_name": 12}, {"full_name": ["a"]}):
        response = client.post(f"/api/documents/{doc['id']}/form", json={"values": values})
        assert response.status_code == 400, values
    assert client.post(f"/api/documents/{doc['id']}/form", json={"values": {}}).status_code == 422
    assert client.get(f"/api/documents/{doc['id']}").json()["version_count"] == 1
    assert list((settings.data_dir / "temporary").iterdir()) == []
    plain = upload(make_pdf(1), name="plain.pdf")
    assert "no form" in client.post(f"/api/documents/{plain['id']}/form", json={"values": {"a": "b"}}).json()["detail"]


def test_form_values_export_and_reimport(client, upload):
    doc = upload(make_form_pdf())
    client.post(f"/api/documents/{doc['id']}/form", json={"values": {"full_name": "=SUM(A1)", "agree": True}})
    exported = client.get(f"/api/documents/{doc['id']}/form/values?format=json")
    assert exported.headers["content-type"] == "application/json" and "attachment" in exported.headers["content-disposition"]
    values = exported.json()
    assert values["full_name"] == "=SUM(A1)" and values["agree"] is True and values["reference"] == "REF-001"

    rows = client.get(f"/api/documents/{doc['id']}/form/values?format=csv").text.splitlines()
    assert rows[0] == "field,value" and "full_name,'=SUM(A1)" in rows and "agree,true" in rows   # formula neutralised

    # Round trip of the whole export into a fresh copy of the form: unchanged values
    # (the read-only reference, the unanswered radio group) are simply left alone.
    other = upload(make_form_pdf() + b"\n% second copy", name="copy.pdf")
    imported = client.post(f"/api/documents/{other['id']}/form", json={"values": values})
    assert imported.status_code == 200, imported.text
    assert imported.json()["result"]["report"]["filled"] == 2
    assert "Nothing to save" in client.post(f"/api/documents/{other['id']}/form", json={"values": values}).json()["detail"]
    again = client.get(f"/api/documents/{other['id']}/form/values").json()
    assert again == values
    assert client.get(f"/api/documents/{doc['id']}/form/values?format=xml").status_code == 400


def test_xfa_forms_are_reported_and_refused(client, upload):
    pdf = pikepdf.Pdf.open(io.BytesIO(make_form_pdf()))
    pdf.Root.AcroForm.XFA = pdf.make_stream(b"<xdp/>")
    buffer = io.BytesIO()
    pdf.save(buffer)
    doc = upload(buffer.getvalue(), name="xfa.pdf")
    assert client.get(f"/api/documents/{doc['id']}/form").json()["xfa"] is True
    response = client.post(f"/api/documents/{doc['id']}/form", json={"values": {"full_name": "x"}})
    assert response.status_code == 400 and "XFA" in response.json()["detail"]


# ---------------------------------------------------------------- signatures
def test_unsigned_document_reports_no_signatures(client, upload):
    doc = upload(make_pdf(1))
    report = client.get(f"/api/documents/{doc['id']}/signatures").json()
    assert report["count"] == 0 and "no cryptographic signatures" in report["note"]


def test_sign_then_validate_is_honest_about_trust(client, settings, upload):
    doc = upload(make_pdf(2))
    response = sign(client, doc["id"], reason="Approved", location="Pune")
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result["version_number"] == 2 and result["page_count"] == 2
    assert result["report"]["field"] == "ARDSignature1" and result["report"]["signer"] == "ARD Test Signer"
    # A self-signed certificate must never be reported as a valid, trusted signature.
    assert result["report"]["status"] == "Signature Unknown"

    report = client.get(f"/api/documents/{doc['id']}/signatures").json()
    assert report["count"] == 1 and "revocation was not checked" in report["note"]
    signature = report["signatures"][0]
    assert signature["status"] == "Signature Unknown"
    assert signature["intact"] and signature["cryptographically_valid"] and not signature["trusted"]
    assert signature["covers_whole_document"] and signature["self_signed"] and not signature["has_timestamp"]
    assert "cannot tell you who really signed" in signature["explanation"] and "time_note" in signature
    assert client.get(f"/api/documents/{doc['id']}").json()["has_signatures"] is True

    # The unsigned original is untouched, and signing appended to it rather than rewriting it.
    original = (settings.data_dir / "originals" / f"{doc['id']}.pdf").read_bytes()
    signed = current_path(client, settings, doc["id"]).read_bytes()
    assert signed.startswith(original) and len(signed) > len(original)

    # A second signature keeps the first one intact.
    second = sign(client, doc["id"], p12=make_p12("Second Signer"))
    assert second.json()["result"]["report"]["field"] == "ARDSignature2"
    assert second.json()["result"]["report"]["earlier_signatures_still_intact"] is True
    both = client.get(f"/api/documents/{doc['id']}/signatures").json()["signatures"]
    assert [s["subject"] for s in both] == ["ARD Test Signer", "Second Signer"]
    assert all(s["intact"] and s["status"] == "Signature Unknown" for s in both)


def test_key_and_passphrase_are_never_stored(client, settings, upload):
    doc = upload(make_pdf(1))
    p12 = make_p12(passphrase=b"very-secret-passphrase")
    assert sign(client, doc["id"], p12=p12, passphrase="very-secret-passphrase").status_code == 200
    database = (settings.data_dir / "ard.sqlite3").read_bytes()
    wal = settings.data_dir / "ard.sqlite3-wal"
    stored = database + (wal.read_bytes() if wal.exists() else b"")
    log = settings.data_dir / "logs" / "ard.log"
    logged = log.read_bytes() if log.exists() else b""
    import base64
    for secret in (b"very-secret-passphrase", base64.b64encode(p12)[:60], p12[:40]):
        assert secret not in stored and secret not in logged
    leftovers = [p for p in settings.data_dir.rglob("*") if p.is_file() and p.suffix in (".p12", ".pfx", ".key", ".pem")]
    assert leftovers == [] and list((settings.data_dir / "temporary").iterdir()) == []
    assert "very-secret" not in str(client.get("/api/jobs").json()) + str(client.get("/api/system/audit").json())


def test_wrong_passphrase_or_bad_certificate_fails_cleanly(client, upload):
    doc = upload(make_pdf(1))
    wrong = sign(client, doc["id"], passphrase="not-it")
    assert wrong.status_code == 400 and "passphrase" in wrong.json()["detail"]
    garbage = sign(client, doc["id"], p12=b"this is not a certificate")
    assert garbage.status_code == 400
    locked = upload(make_pdf(1, password="s3cret-test"), name="locked.pdf")
    assert sign(client, locked["id"]).status_code == 400
    assert client.get(f"/api/documents/{doc['id']}").json()["version_count"] == 1


def test_tampering_and_later_changes_are_reported_invalid(client, settings, upload, tmp_path):
    from app.workers import pdf_sign

    doc = upload(make_pdf(2))
    sign(client, doc["id"])
    signed = current_path(client, settings, doc["id"]).read_bytes()

    # 1. A byte inside the signed content is altered.
    tampered = bytearray(signed)
    tampered[signed.find(b"/Title") + 9] ^= 0x01
    path = tmp_path / "tampered.pdf"
    path.write_bytes(tampered)
    statuses = [s["status"] for s in pdf_sign.validate({"path": str(path)})["signatures"]]
    assert statuses == ["Signature Invalid"]

    # 2. The document is edited in ARD after signing: the new version must not look validly signed.
    client.post(f"/api/documents/{doc['id']}/operations", json={"operation": "rotate", "params": {"degrees": 90}})
    detail = client.get(f"/api/documents/{doc['id']}").json()
    assert detail["has_signatures"] is False
    after = client.get(f"/api/documents/{doc['id']}/signatures").json()
    assert all(s["status"] == "Signature Invalid" for s in after["signatures"])

    # 3. Something is appended to the signed file by other software.
    from pyhanko.pdf_utils import generic
    from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter

    writer = IncrementalPdfFileWriter(io.BytesIO(signed))
    writer.root["/Tampered"] = generic.NameObject("/Yes")
    writer.update_root()
    appended = tmp_path / "appended.pdf"
    with open(appended, "wb") as handle:
        writer.write(handle)
    result = pdf_sign.validate({"path": str(appended)})["signatures"][0]
    assert result["status"] == "Signature Invalid" and result["changed_after_signing"] and "changed after" in result["explanation"]

    # 4. A file too damaged to read is not reported as "no signatures".
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDX" + signed[4:200])
    unreadable = pdf_sign.validate({"path": str(broken)})
    assert unreadable.get("unreadable") is True and "could not be read" in unreadable["note"]


def test_expired_certificate_is_not_reported_valid(client, upload):
    doc = upload(make_pdf(1))
    response = sign(client, doc["id"], p12=make_p12("Old Signer", expired=True))
    if response.status_code == 200:
        signature = client.get(f"/api/documents/{doc['id']}/signatures").json()["signatures"][0]
        assert signature["status"] != "Signature Valid" and signature["certificate_expired"] is True
    else:
        assert response.status_code == 400      # refusing to sign with an expired certificate is also acceptable


def test_mcp_actor_cannot_sign_or_fill(context):
    from app.core.policy.engine import Actor, PolicyDenied

    document_id = context.documents.import_document(Actor(interface="cli"), io.BytesIO(make_form_pdf()), "f.pdf")["document"]["id"]
    agent = Actor(name="agent", interface="mcp")
    assert context.documents.form_fields(agent, document_id)["has_form"]          # reading is allowed
    assert context.documents.signatures(agent, document_id)["count"] == 0
    with pytest.raises(PolicyDenied):
        context.documents.fill_form(agent, document_id, {"full_name": "x"})
    with pytest.raises(PolicyDenied):
        context.documents.sign_document(agent, document_id, make_p12(), "test-pass")


# ---------------------------------------------------------------- PDF/A check
def test_pdfa_check_reports_claims_and_obstacles_without_overclaiming(client, upload):
    doc = upload(make_pdf(1))
    report = client.get(f"/api/documents/{doc['id']}/pdfa").json()
    assert report["validated"] is False and report["claim"] is None
    assert report["verdict"] == "Cannot be PDF/A as it is"
    text = " ".join(report["obstacles"])
    assert "Fonts are not embedded" in text and "Helvetica" in text and "XMP" in text
    assert "not conformance" in report["note"]

    pdf = pikepdf.Pdf.open(io.BytesIO(make_pdf(1)))
    with pdf.open_metadata() as metadata:
        metadata["pdfaid:part"] = "2"
        metadata["pdfaid:conformance"] = "B"
    buffer = io.BytesIO()
    pdf.save(buffer)
    claimed = upload(buffer.getvalue(), name="claims.pdf")
    report = client.get(f"/api/documents/{claimed['id']}/pdfa").json()
    # It claims PDF/A-2b but still has unembedded fonts: the claim is shown, never taken at face value.
    assert report["claim"] == "PDF/A-2b" and report["validated"] is False and report["verdict"] != "Conforms"


# ---------------------------------------------------------------- office conversion
def test_unsupported_and_unavailable_conversions_are_explained(client, upload, monkeypatch):
    bad = client.post("/api/documents", files=[("files", ("page.html", b"<p>hi</p>", "text/html"))]).json()
    assert bad["rejected"][0]["reason"] == "This is not a PDF file."        # HTML is never converted

    monkeypatch.setattr(client.app.state.context.capabilities, "has", lambda capability: capability != "convert.office")
    response = client.post("/api/documents", files=[("files", ("letter.docx", b"PK\x03\x04 fake", "application/octet-stream"))]).json()
    assert response["imported"] == [] and "LibreOffice" in response["rejected"][0]["reason"]


@pytest.mark.skipif(not (shutil.which("libreoffice") or shutil.which("soffice")), reason="LibreOffice is not installed")
def test_office_document_is_converted_and_imported():
    # A snap-packaged LibreOffice cannot read /tmp or hidden folders, so this test works in a visible project folder.
    base = Path(__file__).resolve().parents[2] / "application_data" / "test-convert"
    shutil.rmtree(base, ignore_errors=True)
    settings = Settings(data_dir=base, worker_mode="thread", guardian_enabled=False, log_level="WARNING", _env_file=None)
    try:
        with TestClient(create_app(settings), base_url="http://127.0.0.1:8765") as client:
            client.headers["Authorization"] = "Bearer " + settings.token_path.read_text()
            content = rb"{\rtf1\ansi Quarterly summary\par Revenue grew in the third quarter.\par}"
            response = client.post("/api/documents", files=[("files", ("../../Q3 summary.rtf", content, "application/rtf"))])
            body = response.json()
            assert body["rejected"] == [], body
            document = body["imported"][0]["document"]
            assert body["imported"][0]["converted_from"] == "rtf"
            assert document["display_name"] == "Q3 summary" and document["origin"] == "convert" and document["page_count"] == 1
            text = page_texts(base / "originals" / f"{document['id']}.pdf")[0]
            assert "Quarterly summary" in text and "third quarter" in text
            events = client.get("/api/system/audit").json()["events"]
            assert any(e["details"].get("engine") == "LibreOffice" and e["details"].get("converted_from") == "rtf" for e in events)
            assert list((base / "temporary").iterdir()) == []          # work folder and throw-away profile removed

            empty = client.post("/api/documents", files=[("files", ("empty.docx", b"", "application/octet-stream"))]).json()
            assert empty["rejected"] and list((base / "temporary").iterdir()) == []
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_everything_works_across_real_worker_processes(tmp_path):
    """Results must survive the trip back from a separate worker process (the default mode)."""
    settings = Settings(data_dir=tmp_path / "data", worker_mode="process", guardian_enabled=False,
                        log_level="WARNING", _env_file=None)
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8765") as client:
        client.headers["Authorization"] = "Bearer " + settings.token_path.read_text()
        upload = client.post("/api/documents", files=[("files", ("form.pdf", make_form_pdf(), "application/pdf"))]).json()
        document_id = upload["imported"][0]["document"]["id"]
        base = f"/api/documents/{document_id}"

        assert len(client.get(f"{base}/form").json()["fields"]) == 6
        assert client.post(f"{base}/form", json={"values": {"full_name": "Asha Rao", "agree": True}}).status_code == 200
        assert client.get(f"{base}/form/values").json()["full_name"] == "Asha Rao"
        assert client.get(f"{base}/pdfa").json()["validated"] is False
        assert client.get(f"{base}/signatures").json()["count"] == 0

        signed = sign(client, document_id)
        assert signed.status_code == 200, signed.text
        report = client.get(f"{base}/signatures")
        assert report.status_code == 200, report.text
        assert report.json()["signatures"][0]["status"] == "Signature Unknown"

        assert client.post(f"{base}/operations", json={"operation": "watermark", "params": {"text": "COPY"}}).status_code == 200
        area = {"page": 1, "box": {"tl": [60, 720], "tr": [300, 720], "bl": [60, 690]}}
        redacted = client.post(f"{base}/redactions", json={"areas": [area]})
        assert redacted.status_code == 200 and redacted.json()["result"]["report"]["verified"] is True
        assert client.get(f"{base}/export?format=txt").status_code == 200
        assert client.get(f"{base}/export?format=png").status_code == 200
        protected = client.post(f"{base}/protect", json={"owner_password": "owner-pass-1"})
        assert protected.status_code == 200, protected.text
        assert client.post(f"{base}/unlock", json={"password": "owner-pass-1"}).status_code == 200
        annotated = client.post(f"{base}/annotations", json={"items": [
            {"type": "note", "page": 1, "point": [100, 400], "text": "नमस्ते"},
            {"type": "text", "page": 1, "box": {"tl": [72, 300], "tr": [300, 300], "bl": [72, 280]}, "text": "ok"}]})
        assert annotated.status_code == 200, annotated.text
        assert client.get(f"{base}/thumbnail?page=1").status_code == 200
        assert client.get(f"{base}/integrity").json()["status"] == "Modified Since Baseline"
