"""End-to-end tests through the REST API: import, operations, versions, originals."""

import hashlib

from tests.helpers import make_pdf, page_texts


def op(client, document_id, operation, **params):
    response = client.post(f"/api/documents/{document_id}/operations", json={"operation": operation, "params": params})
    return response


def current_file(client, settings, document_id):
    doc = client.get(f"/api/documents/{document_id}").json()
    version = next(v for v in doc["versions"] if v["is_current"])
    if version["number"] == 1:
        return settings.data_dir / "originals" / f"{document_id}.pdf"
    return settings.data_dir / "versions" / document_id / f"{version['id']}.pdf"


def markers(client, settings, document_id):
    return [text.replace("Page ", "").replace(" marker", "") for text in page_texts(current_file(client, settings, document_id))]


def test_import_records_fingerprint_and_separate_dates(client, upload):
    data = make_pdf(4)
    doc = upload(data, name="My Report.pdf")
    assert doc["display_name"] == "My Report"
    assert doc["page_count"] == 4
    assert doc["sha256"] == hashlib.sha256(data).hexdigest()
    assert doc["dates"]["pdf_created_at"] == "2024-01-31T12:00:00+05:30"
    assert doc["dates"]["imported_at"] and doc["dates"]["edited_at"] is None
    listing = client.get("/api/documents").json()
    assert listing["total"] == 1


def test_same_file_is_not_imported_twice(client, upload):
    data = make_pdf(2)
    first = upload(data)
    response = client.post("/api/documents", files=[("files", ("copy.pdf", data, "application/pdf"))]).json()
    assert response["imported"][0]["already_imported"] is True
    assert response["imported"][0]["document"]["id"] == first["id"]
    assert client.get("/api/documents").json()["total"] == 1


def test_bad_files_are_rejected_with_reasons_and_leave_nothing_behind(client, settings):
    response = client.post("/api/documents", files=[
        ("files", ("notes.txt", b"hello world", "text/plain")),
        ("files", ("empty.pdf", b"", "application/pdf")),
        ("files", ("broken.pdf", b"%PDF-1.7\nthis is not really a pdf", "application/pdf")),
        ("files", ("good.pdf", make_pdf(1), "application/pdf")),
    ])
    body = response.json()
    assert [r["filename"] for r in body["rejected"]] == ["notes.txt", "empty.pdf", "broken.pdf"]
    assert all(r["reason"] for r in body["rejected"])
    assert len(body["imported"]) == 1
    assert len(list((settings.data_dir / "originals").iterdir())) == 1
    assert list((settings.data_dir / "temporary").iterdir()) == []


def test_page_operations_create_versions_and_keep_the_original(client, settings, upload):
    data = make_pdf(5, outline=True)
    doc = upload(data)
    original = settings.data_dir / "originals" / f"{doc['id']}.pdf"

    result = op(client, doc["id"], "delete_pages", pages=[2]).json()
    assert result["done"] and result["result"]["page_count"] == 4 and result["result"]["version_number"] == 2
    assert result["result"]["report"]["bookmarks_removed"] == 1
    assert markers(client, settings, doc["id"]) == ["1", "3", "4", "5"]

    assert op(client, doc["id"], "reorder", order=[4, 3, 2, 1]).status_code == 200
    assert markers(client, settings, doc["id"]) == ["5", "4", "3", "1"]

    assert op(client, doc["id"], "duplicate_pages", pages=[1]).status_code == 200
    assert markers(client, settings, doc["id"]) == ["5", "5", "4", "3", "1"]

    assert op(client, doc["id"], "reverse").status_code == 200
    assert markers(client, settings, doc["id"]) == ["1", "3", "4", "5", "5"]

    assert op(client, doc["id"], "insert_blank", after=1, count=2).json()["result"]["page_count"] == 7
    assert markers(client, settings, doc["id"])[:4] == ["1", "", "", "3"]

    # The imported file is byte-for-byte unchanged and read-only.
    assert hashlib.sha256(original.read_bytes()).hexdigest() == doc["sha256"]
    assert not (original.stat().st_mode & 0o222)

    detail = client.get(f"/api/documents/{doc['id']}").json()
    assert [v["number"] for v in detail["versions"]] == [1, 2, 3, 4, 5, 6]
    assert detail["versions"][2]["parent_version_id"] == detail["versions"][1]["id"]
    assert detail["dates"]["edited_at"] is not None
    assert client.get(f"/api/documents/{doc['id']}/integrity").json()["status"] == "Modified Since Baseline"


def test_rotate_and_crop(client, settings, upload):
    import pypdf

    doc = upload(make_pdf(3))
    assert op(client, doc["id"], "rotate", pages=[2], degrees=90).status_code == 200
    reader = pypdf.PdfReader(str(current_file(client, settings, doc["id"])))
    assert [page.rotation for page in reader.pages] == [0, 90, 0]

    assert op(client, doc["id"], "crop", pages=[1], left=50, top=100).status_code == 200
    reader = pypdf.PdfReader(str(current_file(client, settings, doc["id"])))
    assert float(reader.pages[0].cropbox.width) == 562 and float(reader.pages[0].cropbox.height) == 692
    assert op(client, doc["id"], "crop", pages=[1], left=400, right=400).status_code == 400


def test_invalid_operations_fail_cleanly_without_a_new_version(client, settings, upload):
    doc = upload(make_pdf(2))
    for operation, params in [("delete_pages", {"pages": [1, 2]}), ("delete_pages", {"pages": [9]}),
                              ("rotate", {"degrees": 45}), ("nonsense", {})]:
        response = op(client, doc["id"], operation, **params)
        assert response.status_code == 400 and response.json()["detail"]
    assert client.get(f"/api/documents/{doc['id']}").json()["version_count"] == 1
    assert not (settings.data_dir / "versions" / doc["id"]).exists()
    assert op(client, "0" * 32, "reverse").status_code == 404


def test_strip_metadata(client, settings, upload):
    import pypdf

    doc = upload(make_pdf(1))
    result = op(client, doc["id"], "strip_metadata").json()["result"]
    assert "Title" in result["report"]["removed_fields"]
    reader = pypdf.PdfReader(str(current_file(client, settings, doc["id"])))
    assert not reader.metadata or reader.metadata.title is None
    # The original still has it; nothing claims otherwise.
    assert pypdf.PdfReader(str(settings.data_dir / "originals" / f"{doc['id']}.pdf")).metadata.title == "Test document"


def test_compress_reports_real_sizes_and_stays_valid(client, settings, upload):
    doc = upload(make_pdf(2, image=True))
    lossless = op(client, doc["id"], "compress", preset="quality").json()["result"]
    assert lossless["report"]["lossy"] is False and lossless["page_count"] == 2
    size_lossless = lossless["report"]["size_after"]

    smaller = op(client, doc["id"], "compress", preset="smaller").json()["result"]
    report = smaller["report"]
    assert report["lossy"] is True and report["images_recompressed"] == 1
    path = current_file(client, settings, doc["id"])
    assert report["size_after"] == path.stat().st_size < size_lossless
    assert len(page_texts(path)) == 2


def test_merge_split_extract_create_new_documents(client, settings, upload):
    a = upload(make_pdf(2, label="A"), name="a.pdf")
    b = upload(make_pdf(3, label="B"), name="b.pdf")

    merged = client.post("/api/documents/merge", json={"document_ids": [b["id"], a["id"]], "name": "Both"}).json()
    new = merged["result"]["documents"][0]
    assert new["display_name"] == "Both" and new["page_count"] == 5
    texts = page_texts(settings.data_dir / "originals" / f"{new['id']}.pdf")
    assert texts == ["B 1 marker", "B 2 marker", "B 3 marker", "A 1 marker", "A 2 marker"]

    parts = client.post(f"/api/documents/{new['id']}/split", json={"mode": "every", "n": 2}).json()["result"]["documents"]
    assert [p["page_count"] for p in parts] == [2, 2, 1]

    extracted = client.post(f"/api/documents/{b['id']}/split", json={"mode": "pages", "pages": [3, 1]}).json()
    extract_id = extracted["result"]["documents"][0]["id"]
    assert page_texts(settings.data_dir / "originals" / f"{extract_id}.pdf") == ["B 3 marker", "B 1 marker"]

    # Sources are untouched: still one version each.
    assert client.get(f"/api/documents/{a['id']}").json()["version_count"] == 1
    assert client.get(f"/api/documents/{b['id']}").json()["version_count"] == 1
    assert client.get("/api/documents").json()["total"] == 7
    assert client.post("/api/documents/merge", json={"document_ids": [a["id"], "f" * 32]}).status_code == 404


def test_insert_pages_from_another_document(client, settings, upload):
    a = upload(make_pdf(2, label="A"), name="a.pdf")
    b = upload(make_pdf(3, label="B"), name="b.pdf")
    response = op(client, a["id"], "insert_pages", from_document_id=b["id"], pages="2-3", after=1)
    assert response.json()["result"]["page_count"] == 4
    assert page_texts(current_file(client, settings, a["id"])) == ["A 1 marker", "B 2 marker", "B 3 marker", "A 2 marker"]
    assert op(client, a["id"], "insert_pages", from_document_id=b["id"], pages="9").status_code == 400


def test_revert_copies_forward_and_keeps_history(client, settings, upload):
    doc = upload(make_pdf(3))
    op(client, doc["id"], "delete_pages", pages=[1])
    detail = client.get(f"/api/documents/{doc['id']}").json()
    first = detail["versions"][0]
    response = client.post(f"/api/documents/{doc['id']}/versions/{first['id']}/revert").json()
    assert response["result"]["version_number"] == 3 and response["result"]["page_count"] == 3
    detail = client.get(f"/api/documents/{doc['id']}").json()
    assert [v["number"] for v in detail["versions"]] == [1, 2, 3]
    assert detail["versions"][2]["sha256"] == first["sha256"]


def test_trash_needs_confirmation_keeps_files_and_can_be_restored(client, settings, upload):
    doc = upload(make_pdf(1))
    denied = client.delete(f"/api/documents/{doc['id']}")
    assert denied.status_code == 403 and denied.json()["requires_approval"] is True
    assert client.delete(f"/api/documents/{doc['id']}", headers={"X-ARD-Confirm": "yes"}).json()["trashed"] is True
    assert (settings.data_dir / "originals" / f"{doc['id']}.pdf").is_file()
    assert client.get("/api/documents").json()["total"] == 0
    assert client.get("/api/documents?view=trash").json()["total"] == 1
    assert op(client, doc["id"], "reverse").status_code == 403
    assert client.post(f"/api/documents/{doc['id']}/restore").json()["trashed"] is False


def test_rename_favorite_search_and_download(client, upload):
    doc = upload(make_pdf(1), name="invoice 2024.pdf")
    upload(make_pdf(2), name="contract.pdf")
    updated = client.patch(f"/api/documents/{doc['id']}", json={"display_name": "Invoice: March/April", "favorite": True}).json()
    assert updated["display_name"] == "Invoice MarchApril" and updated["favorite"] is True
    assert [d["display_name"] for d in client.get("/api/documents?q=invoice").json()["documents"]] == ["Invoice MarchApril"]
    assert client.get("/api/documents?q=100%25").json()["total"] == 0
    assert client.get("/api/documents?view=favorites").json()["total"] == 1

    inline = client.get(f"/api/documents/{doc['id']}/file")
    assert inline.headers["content-type"] == "application/pdf" and inline.content.startswith(b"%PDF")
    assert inline.headers["content-disposition"].startswith("inline")
    partial = client.get(f"/api/documents/{doc['id']}/file", headers={"Range": "bytes=0-7"})
    assert partial.status_code == 206 and partial.content.startswith(b"%PDF-")
    download = client.get(f"/api/documents/{doc['id']}/file?download=true")
    assert download.headers["content-disposition"].startswith("attachment")
    actions = [e["action"] for e in client.get("/api/system/audit").json()["events"]]
    assert "document.export" in actions and "document.update" in actions


def test_thumbnails(client, settings, upload):
    doc = upload(make_pdf(2))
    response = client.get(f"/api/documents/{doc['id']}/thumbnail?page=2")
    assert response.status_code == 200 and response.content.startswith(b"\x89PNG")
    assert client.get(f"/api/documents/{doc['id']}/thumbnail?page=3").status_code == 404


def test_system_endpoints(client):
    assert client.get("/api/health").json()["status"] == "ok"
    status = client.get("/api/system/status").json()
    assert status["viewer_available"] and status["safe_mode"]["active"] is False
    caps = client.get("/api/system/capabilities").json()
    assert "pdf.merge" in caps["capabilities"]
    assert all(item["license"] for item in caps["integrations"])
    diagnostics = client.get("/api/system/diagnostics").json()
    assert diagnostics["database"]["quick_check"] == "ok" and diagnostics["telemetry"] == "none"
    assert client.put("/api/system/guardian", json={"profile": "eco"}).json()["profile"]["name"] == "eco"
    assert client.put("/api/system/guardian", json={"profile": "warp"}).status_code == 400
    for path in ("/", "/library", "/workspace", "/intelligence", "/automation", "/trust"):
        page = client.get(path)
        assert page.status_code == 200 and "<title>" in page.text
        assert "http://" not in page.text.replace("http://www.w3.org", "") and "https://" not in page.text, path
