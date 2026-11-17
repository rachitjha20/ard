"""Document endpoints. Thin: validation in, service call, JSON out."""

from __future__ import annotations

import base64
import binascii
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from app.api.dependencies import get_actor, get_context, get_documents
from app.core.context import AppContext
from app.core.policy.engine import Actor
from app.services.documents.service import DocumentError, DocumentService
from app.services.documents.storage import UploadTooLarge
from app.services.pdf.operations import OPERATIONS
from app.workers.office_convert import OFFICE_EXTENSIONS

router = APIRouter(prefix="/api/documents", tags=["documents"])


class DocumentUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=255)
    favorite: bool | None = None


class OperationRequest(BaseModel):
    operation: str = Field(max_length=64)
    params: dict[str, Any] = Field(default_factory=dict)


class MergeRequest(BaseModel):
    document_ids: list[str] = Field(min_length=2, max_length=200)
    name: str | None = Field(default=None, max_length=255)


class SplitRequest(BaseModel):
    mode: str = Field(default="ranges", max_length=16)
    ranges: str | None = Field(default=None, max_length=4000)
    n: int | None = Field(default=None, ge=1)
    pages: list[int] | None = Field(default=None, max_length=20000)


class AnnotationRequest(BaseModel):
    items: list[dict[str, Any]] = Field(min_length=1, max_length=2000)
    # PNG or JPEG files, base64-encoded; image items refer to them by position.
    images: list[str] = Field(default_factory=list, max_length=20)


class RedactionRequest(BaseModel):
    areas: list[dict[str, Any]] = Field(min_length=1, max_length=1000)


class ProtectRequest(BaseModel):
    owner_password: str = Field(min_length=1, max_length=127)
    user_password: str | None = Field(default=None, max_length=127)
    permissions: dict[str, bool] | None = None


class UnlockRequest(BaseModel):
    password: str = Field(max_length=1000)


class TextLineRequest(BaseModel):
    page: int = Field(ge=1)
    line: dict[str, Any]


class FormValues(BaseModel):
    values: dict[str, Any] = Field(min_length=1, max_length=5000)


def _job_response(job: dict[str, Any]) -> dict[str, Any]:
    """A job that finished in time carries its result; otherwise the client polls /api/jobs/{id}."""
    return {"job": job, "done": job["status"] == "succeeded", "result": job.get("result")}


@router.post("", status_code=201)
def upload_documents(request: Request, files: list[UploadFile] = File(...), actor: Actor = Depends(get_actor),
                     documents: DocumentService = Depends(get_documents),
                     context: AppContext = Depends(get_context)) -> dict[str, Any]:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > context.settings.max_upload_bytes * len(files) + 1_000_000:
        raise HTTPException(413, "The upload is larger than the import limit.")
    imported, rejected = [], []
    for upload in files:
        name = upload.filename or "Untitled.pdf"
        office = "." in name and "." + name.rsplit(".", 1)[1].lower() in OFFICE_EXTENSIONS
        try:
            imported.append(documents.import_office(actor, upload.file, name) if office
                            else documents.import_document(actor, upload.file, name))
        except (DocumentError, UploadTooLarge) as exc:
            # One bad file must not hide the others; each gets its own explanation.
            rejected.append({"filename": upload.filename, "reason": str(exc)})
        finally:
            upload.file.close()
    return {"imported": imported, "rejected": rejected}


@router.get("")
def list_documents(q: str = Query("", max_length=200), view: str = Query("all", max_length=24),
                   sort: str = Query("imported", max_length=16), limit: int = Query(200, ge=1, le=500),
                   offset: int = Query(0, ge=0), category: str = Query("", max_length=32),
                   tag: str = Query("", max_length=64), actor: Actor = Depends(get_actor),
                   documents: DocumentService = Depends(get_documents)) -> dict[str, Any]:
    return documents.list_documents(actor, query=q, view=view, sort=sort, limit=limit, offset=offset,
                                    category=category, tag=tag)


@router.get("/operations")
def list_operations() -> dict[str, Any]:
    return {"operations": OPERATIONS}


@router.post("/merge")
def merge(body: MergeRequest, actor: Actor = Depends(get_actor),
          documents: DocumentService = Depends(get_documents)) -> dict[str, Any]:
    return _job_response(documents.merge_documents(actor, body.document_ids, body.name))


@router.get("/{document_id}")
def get_document(document_id: str, actor: Actor = Depends(get_actor),
                 documents: DocumentService = Depends(get_documents)) -> dict[str, Any]:
    return documents.get_document(actor, document_id)


@router.patch("/{document_id}")
def update_document(document_id: str, body: DocumentUpdate, actor: Actor = Depends(get_actor),
                    documents: DocumentService = Depends(get_documents)) -> dict[str, Any]:
    return documents.update_document(actor, document_id, display_name=body.display_name, favorite=body.favorite)


@router.delete("/{document_id}")
def trash_document(document_id: str, actor: Actor = Depends(get_actor),
                   documents: DocumentService = Depends(get_documents)) -> dict[str, Any]:
    """Moves the document to the trash. Nothing is removed from disk."""
    return documents.trash_document(actor, document_id)


@router.post("/{document_id}/restore")
def restore_document(document_id: str, actor: Actor = Depends(get_actor),
                     documents: DocumentService = Depends(get_documents)) -> dict[str, Any]:
    return documents.restore_document(actor, document_id)


@router.get("/{document_id}/file")
def get_file(document_id: str, version: str | None = Query(None, max_length=32), download: bool = False,
             actor: Actor = Depends(get_actor), documents: DocumentService = Depends(get_documents)) -> FileResponse:
    path, filename = documents.file_path(actor, document_id, version, export=download)
    disposition = "attachment" if download else "inline"
    ascii_name = filename.encode("ascii", "ignore").decode().replace('"', "") or "document.pdf"
    headers = {
        "Content-Disposition": f"{disposition}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}",
        # A version never changes, so a versioned URL may be cached; "current" may not.
        "Cache-Control": "private, max-age=86400, immutable" if version else "private, no-cache",
    }
    return FileResponse(path, media_type="application/pdf", headers=headers)


@router.get("/{document_id}/thumbnail")
def get_thumbnail(document_id: str, page: int = Query(1, ge=1), version: str | None = Query(None, max_length=32),
                  width: int = Query(220, ge=50, le=600), actor: Actor = Depends(get_actor),
                  documents: DocumentService = Depends(get_documents)) -> FileResponse:
    path = documents.thumbnail(actor, document_id, page, version, width)
    cache = "private, max-age=86400, immutable" if version else "private, no-cache"
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": cache})


@router.get("/{document_id}/integrity")
def get_integrity(document_id: str, actor: Actor = Depends(get_actor),
                  documents: DocumentService = Depends(get_documents)) -> dict[str, Any]:
    return documents.integrity(actor, document_id)


@router.post("/{document_id}/operations")
def apply_operation(document_id: str, body: OperationRequest, actor: Actor = Depends(get_actor),
                    documents: DocumentService = Depends(get_documents)) -> dict[str, Any]:
    return _job_response(documents.apply_operation(actor, document_id, body.operation, body.params))


@router.post("/{document_id}/split")
def split_document(document_id: str, body: SplitRequest, actor: Actor = Depends(get_actor),
                   documents: DocumentService = Depends(get_documents)) -> dict[str, Any]:
    return _job_response(documents.split_document(actor, document_id, body.model_dump(exclude_none=True)))


@router.post("/{document_id}/versions/{version_id}/revert")
def revert(document_id: str, version_id: str, actor: Actor = Depends(get_actor),
           documents: DocumentService = Depends(get_documents)) -> dict[str, Any]:
    return _job_response(documents.revert_to_version(actor, document_id, version_id))


@router.post("/{document_id}/annotations")
def annotate(document_id: str, body: AnnotationRequest, request: Request, actor: Actor = Depends(get_actor),
             documents: DocumentService = Depends(get_documents)) -> dict[str, Any]:
    """Add overlays and annotations as a new version. Coordinates are PDF points; see the README."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > 60 * 1024 * 1024:
        raise HTTPException(413, "That is too much to save at once.")
    try:
        images = [base64.b64decode(data.split(",", 1)[-1], validate=True) for data in body.images]
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(400, "An image was not valid base64.") from exc
    return _job_response(documents.annotate_document(actor, document_id, body.items, images))


@router.post("/{document_id}/redactions")
def redact(document_id: str, body: RedactionRequest, actor: Actor = Depends(get_actor),
           documents: DocumentService = Depends(get_documents)) -> dict[str, Any]:
    """Black out areas. Affected pages become images; the result is verified before it is accepted."""
    return _job_response(documents.redact_document(actor, document_id, body.areas))


@router.post("/{document_id}/protect")
def protect(document_id: str, body: ProtectRequest, actor: Actor = Depends(get_actor),
            documents: DocumentService = Depends(get_documents)) -> dict[str, Any]:
    return _job_response(documents.protect_document(actor, document_id, body.owner_password, body.user_password,
                                                    body.permissions))


@router.post("/{document_id}/unlock")
def unlock(document_id: str, body: UnlockRequest, actor: Actor = Depends(get_actor),
           documents: DocumentService = Depends(get_documents)) -> dict[str, Any]:
    return _job_response(documents.unlock_document(actor, document_id, body.password))


@router.get("/{document_id}/export")
def export(document_id: str, format: str = Query("txt", max_length=8), version: str | None = Query(None, max_length=32),
           actor: Actor = Depends(get_actor), documents: DocumentService = Depends(get_documents)) -> FileResponse:
    path, filename, media_type = documents.export_document(actor, document_id, format, version)
    ascii_name = filename.encode("ascii", "ignore").decode().replace('"', "") or "export"
    headers = {"Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}",
               "Cache-Control": "private, no-store"}
    return FileResponse(path, media_type=media_type, headers=headers)


@router.get("/{document_id}/form")
def get_form(document_id: str, actor: Actor = Depends(get_actor),
             documents: DocumentService = Depends(get_documents)) -> dict[str, Any]:
    return documents.form_fields(actor, document_id)


@router.post("/{document_id}/form")
def fill_form(document_id: str, body: FormValues, actor: Actor = Depends(get_actor),
              documents: DocumentService = Depends(get_documents)) -> dict[str, Any]:
    return _job_response(documents.fill_form(actor, document_id, body.values))


@router.get("/{document_id}/form/values")
def export_form(document_id: str, format: str = Query("json", max_length=8), actor: Actor = Depends(get_actor),
                documents: DocumentService = Depends(get_documents)) -> Response:
    import csv
    import io
    import json

    values, name = documents.export_form_values(actor, document_id)
    if format == "csv":
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["field", "value"])
        for key, value in values.items():
            text = "" if value is None else ("true" if value is True else "false" if value is False else str(value))
            # A leading =, +, - or @ would be run as a formula by spreadsheet software.
            writer.writerow([key, "'" + text if text[:1] in "=+-@" else text])
        content, media_type, extension = buffer.getvalue(), "text/csv; charset=utf-8", "csv"
    elif format == "json":
        content, media_type, extension = json.dumps(values, indent=2, ensure_ascii=False), "application/json", "json"
    else:
        raise HTTPException(400, "Format must be json or csv.")
    filename = f"{name} (form values).{extension}"
    ascii_name = filename.encode("ascii", "ignore").decode().replace('"', "") or f"form.{extension}"
    return Response(content, media_type=media_type, headers={
        "Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}",
        "Cache-Control": "private, no-store"})


@router.get("/{document_id}/signatures")
def get_signatures(document_id: str, actor: Actor = Depends(get_actor),
                   documents: DocumentService = Depends(get_documents)) -> dict[str, Any]:
    return documents.signatures(actor, document_id)


@router.post("/{document_id}/sign")
def sign(document_id: str, certificate: UploadFile = File(...), passphrase: str = Form("", max_length=1000),
         reason: str = Form("", max_length=200), location: str = Form("", max_length=200),
         actor: Actor = Depends(get_actor), documents: DocumentService = Depends(get_documents)) -> dict[str, Any]:
    """Sign with a PKCS#12 certificate. The certificate and passphrase are used for this request only."""
    try:
        data = certificate.file.read(1_000_001)
    finally:
        certificate.file.close()
    return _job_response(documents.sign_document(actor, document_id, data, passphrase, reason, location))


@router.get("/{document_id}/pdfa")
def get_pdfa(document_id: str, actor: Actor = Depends(get_actor),
             documents: DocumentService = Depends(get_documents)) -> dict[str, Any]:
    return documents.pdfa_report(actor, document_id)


@router.post("/{document_id}/text-line")
def text_line(document_id: str, body: TextLineRequest, actor: Actor = Depends(get_actor),
              documents: DocumentService = Depends(get_documents)) -> dict[str, Any]:
    """Look up the font, size and colour of a line of text. Changes nothing."""
    return documents.text_line_info(actor, document_id, body.page, body.line)
