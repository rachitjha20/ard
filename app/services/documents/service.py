"""Document registry and document operations.

This is the single entry point for anything that touches a document. The UI,
REST API and CLI all call these methods; each one asks the policy engine
first and writes an audit record afterwards.

Non-destructive by design: an imported file is stored once and never changed.
Every edit writes a new version file and records its parent, so the original
can always be recovered.
"""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, Any, BinaryIO

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.config.settings import APP_VERSION
from app.core.jobs.orchestrator import PRIORITY_INTERACTIVE, JobKind, job_to_dict
from app.core.logging import get_logger
from app.core.policy.engine import Actor, PolicyDenied
from app.models import Document, DocumentTag, DocumentVersion, Job, Tag, new_id
from app.services.library.classifier import CATEGORIES as CATEGORY_LABELS
from app.services.integrity.audit import append_event
from app.services.pdf.operations import (
    OPERATIONS, OperationError, parse_page_ranges, plan_annotations, plan_operation, plan_protection,
    plan_form_values, plan_redaction, plan_split,
)
from app.workers import office_convert, pdf_forms, pdf_sign, pdf_tasks, pdfa_check

if TYPE_CHECKING:
    from app.core.context import AppContext

log = get_logger("documents")


class DocumentError(Exception):
    """A request that cannot be fulfilled; the message is safe to show."""

    status = 400


class NotFound(DocumentError):
    status = 404


class ImportRejected(DocumentError):
    status = 422


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def clean_display_name(filename: str, *, from_path: bool = True) -> str:
    """Turn an untrusted name into a display name. Never used as a path.

    Upload names may carry directories, which are dropped. A name the user
    typed (rename) is not a path, so only the unsafe characters are removed.
    """
    name = PureWindowsPath(PurePosixPath(filename or "").name).name if from_path else (filename or "")
    name = unicodedata.normalize("NFC", name)
    name = "".join(ch for ch in name if ch.isprintable() and ch not in '<>:"/\\|?*')
    name = re.sub(r"\s+", " ", name).strip(" .")
    if name.lower().endswith(".pdf"):
        name = name[:-4].rstrip(" .")
    return name[:200] or "Untitled"


_PDF_DATE = re.compile(r"^D?:?(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?\s*([Zz]|[+-]\d{2}'?:?\d{2}'?)?")


def parse_pdf_date(value: str | None) -> str | None:
    """``D:20240131120000+05'30'`` -> ISO 8601, or None if it cannot be read."""
    if not value:
        return None
    match = _PDF_DATE.match(value.strip())
    if not match:
        return None
    year, month, day, hour, minute, second, zone = match.groups()
    try:
        parsed = datetime(int(year), int(month or 1), int(day or 1), int(hour or 0), int(minute or 0), int(second or 0))
    except ValueError:
        return None
    text = parsed.isoformat()
    if zone:
        zone = zone.replace("'", "").replace(":", "")
        text += "Z" if zone.upper() == "Z" else f"{zone[:3]}:{zone[3:5]}"
    return text


class DocumentService:
    def __init__(self, context: "AppContext") -> None:
        self.ctx = context
        context.jobs.register(JobKind("pdf.transform", pdf_tasks.transform, self._finalize_version, self._discard_outputs))
        context.jobs.register(JobKind("pdf.merge", pdf_tasks.merge, self._finalize_new_documents, self._discard_outputs))
        context.jobs.register(JobKind("pdf.split", pdf_tasks.split, self._finalize_new_documents, self._discard_outputs))

    # ------------------------------------------------------------------ helpers
    def _authorize(self, session: Session, actor: Actor, action: str, document: Document | None = None) -> None:
        try:
            self.ctx.policy.authorize(actor, action, document_trashed=bool(document and document.trashed_at))
        except PolicyDenied as denied:
            # Denials are evidence too. Record them, except for plain missing confirmation.
            if not denied.decision.requires_approval:
                append_event(session, actor=actor.name, interface=actor.interface, action=action, outcome="denied",
                             document_id=document.id if document else None, details={"reason": denied.decision.reason})
                session.commit()
            raise

    def _version_ready(self, session: Session, document_id: str, version_id: str, storage_key: str) -> None:
        """Tell interested services (the search index) that a version became current."""
        for hook in self.ctx.version_ready_hooks:
            try:
                hook(session, document_id, version_id, storage_key)
            except Exception:
                log.exception("version hook failed")

    def _get(self, session: Session, document_id: str) -> Document:
        document = session.get(Document, document_id) if isinstance(document_id, str) else None
        if document is None:
            raise NotFound("Document not found.")
        return document

    def _version(self, session: Session, document: Document, version_id: str | None = None) -> DocumentVersion:
        version = session.get(DocumentVersion, version_id or document.current_version_id)
        if version is None or version.document_id != document.id:
            raise NotFound("Version not found.")
        return version

    def _require_editable(self, document: Document, version: DocumentVersion) -> int:
        if document.is_encrypted or version.page_count is None:
            raise DocumentError("This PDF is password-protected. Remove the protection first; ARD keeps the protected "
                                "version as it is.")
        return version.page_count

    def version_dict(self, version: DocumentVersion, current_id: str | None = None) -> dict[str, Any]:
        return {
            "id": version.id,
            "number": version.number,
            "parent_version_id": version.parent_version_id,
            "operation": version.operation,
            "label": OPERATIONS.get(version.operation, version.operation.replace("_", " ").capitalize()),
            "parameters": version.parameters,
            "report": version.report,
            "sha256": version.sha256,
            "size_bytes": version.size_bytes,
            "page_count": version.page_count,
            "actor": version.actor,
            "interface": version.interface,
            "app_version": version.app_version,
            "created_at": version.created_at.isoformat(),
            "is_current": version.id == current_id,
            "is_original": version.number == 1,
        }

    def document_dict(self, session: Session, document: Document) -> dict[str, Any]:
        current = session.get(DocumentVersion, document.current_version_id)
        count = session.scalar(select(func.count()).select_from(DocumentVersion).where(
            DocumentVersion.document_id == document.id))
        return {
            "id": document.id,
            "display_name": document.display_name,
            "original_filename": document.original_filename,
            "sha256": document.sha256,
            "original_size_bytes": document.size_bytes,
            "size_bytes": current.size_bytes if current else document.size_bytes,
            "page_count": current.page_count if current else None,
            "is_encrypted": document.is_encrypted,
            "favorite": document.favorite,
            "origin": document.origin,
            "lifecycle_state": document.lifecycle_state,
            "current_version_id": document.current_version_id,
            "current_version_number": current.number if current else None,
            "version_count": count,
            "trashed": document.trashed_at is not None,
            "category": document.category,
            "category_label": CATEGORY_LABELS.get(document.category, "Uncategorized"),
            "category_source": document.category_source,
            "category_confidence": document.category_confidence,
            "category_evidence": document.category_evidence or [],
            "ocr_status": document.ocr_status,
            "indexed": document.indexed_version_id == document.current_version_id,
            "tags": [name for (name,) in session.execute(
                select(Tag.name).join(DocumentTag, DocumentTag.tag_id == Tag.id)
                .where(DocumentTag.document_id == document.id).order_by(Tag.name))],
            # True when this version was signed in ARD, or is an imported file that carries signatures.
            "has_signatures": bool(current and ((current.report or {}).get("signed") or (
                current.number == 1 and ((current.report or {}).get("inspection") or {}).get("has_signature_fields")))),
            # Each date says what it means; they are never merged into one "date".
            "dates": {
                "imported_at": document.imported_at.isoformat(),
                "pdf_created_at": document.pdf_created_at,
                "pdf_modified_at": document.pdf_modified_at,
                "edited_at": document.edited_at.isoformat() if document.edited_at else None,
                "trashed_at": document.trashed_at.isoformat() if document.trashed_at else None,
            },
        }

    # ------------------------------------------------------------------ import
    def import_document(self, actor: Actor, stream: BinaryIO, filename: str) -> dict[str, Any]:
        with self.ctx.sessions() as session:
            self._authorize(session, actor, "document.import")

        temp, sha256, size = self.ctx.storage.receive(stream, self.ctx.settings.max_upload_bytes)
        try:
            if size == 0:
                raise ImportRejected("The file is empty.")
            with open(temp, "rb") as handle:
                if b"%PDF-" not in handle.read(1024):
                    raise ImportRejected("This is not a PDF file.")

            with self.ctx.sessions() as session:
                existing = session.execute(
                    select(Document).where(Document.sha256 == sha256, Document.trashed_at.is_(None),
                                           Document.origin == "import")
                ).scalars().first()
                if existing is not None:
                    return {"document": self.document_dict(session, existing), "already_imported": True}

            try:
                info = self.ctx.jobs.call(pdf_tasks.inspect, {"path": str(temp)})
            except Exception as exc:
                log.warning("inspection failed", extra={"data": {"error": exc.__class__.__name__}})
                raise ImportRejected("This PDF could not be read. It may be damaged.") from exc
            if not info.get("valid"):
                raise ImportRejected("This PDF is damaged or not a valid PDF file.")
            pages = info.get("page_count")
            if pages is not None and pages > self.ctx.settings.max_pages:
                raise ImportRejected(f"This PDF has {pages} pages; the limit is {self.ctx.settings.max_pages}.")
            if pages == 0:
                raise ImportRejected("This PDF has no pages.")

            document_id, version_id = new_id(), new_id()
            key = self.ctx.storage.commit_original(temp, document_id)
        except BaseException:
            temp.unlink(missing_ok=True)
            raise

        metadata = info.get("metadata") or {}
        with self.ctx.sessions() as session:
            document = Document(
                id=document_id, display_name=clean_display_name(filename),
                original_filename=(filename or "")[:255], sha256=sha256, size_bytes=size,
                is_encrypted=bool(info.get("encrypted")), origin="import",
                pdf_created_at=parse_pdf_date(metadata.get("CreationDate")),
                pdf_modified_at=parse_pdf_date(metadata.get("ModDate")),
                current_version_id=version_id,
            )
            session.add(document)
            session.add(DocumentVersion(
                id=version_id, document_id=document_id, number=1, storage_key=key, sha256=sha256, size_bytes=size,
                page_count=pages, operation="import", parameters={}, report={"inspection": info},
                actor=actor.name, interface=actor.interface, app_version=APP_VERSION,
            ))
            session.flush()
            append_event(session, actor=actor.name, interface=actor.interface, action="document.import",
                         document_id=document_id, details={"sha256": sha256, "size_bytes": size, "page_count": pages})
            self._version_ready(session, document_id, version_id, key)
            session.commit()
            result = {"document": self.document_dict(session, document), "already_imported": False}
        self.ctx.jobs.notify()
        return result

    # ------------------------------------------------------------------ queries
    def list_documents(self, actor: Actor, *, query: str = "", view: str = "all", sort: str = "imported",
                       limit: int = 200, offset: int = 0, category: str = "", tag: str = "") -> dict[str, Any]:
        with self.ctx.sessions() as session:
            self._authorize(session, actor, "document.list")
            statement = select(Document)
            if view == "trash":
                statement = statement.where(Document.trashed_at.is_not(None))
            else:
                statement = statement.where(Document.trashed_at.is_(None))
                if view == "favorites":
                    statement = statement.where(Document.favorite.is_(True))
                elif view == "edited":
                    statement = statement.where(Document.edited_at.is_not(None))
                elif view == "protected":
                    statement = statement.where(Document.is_encrypted.is_(True))
                elif view not in ("all", ""):
                    from app.services.library.service import album_condition

                    condition = album_condition(view)
                    if condition is not None:
                        statement = statement.where(condition)
                if category:
                    statement = statement.where(Document.category == category)
                if tag:
                    statement = statement.where(Document.id.in_(
                        select(DocumentTag.document_id).join(Tag, Tag.id == DocumentTag.tag_id)
                        .where(func.lower(Tag.name) == tag.lower())))
            if query.strip():
                like = "%" + query.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                statement = statement.where(or_(Document.display_name.ilike(like, escape="\\"),
                                                Document.original_filename.ilike(like, escape="\\")))
            total = session.scalar(select(func.count()).select_from(statement.subquery()))
            ordering = {
                "imported": Document.imported_at.desc(),
                "name": func.lower(Document.display_name),
                "edited": func.coalesce(Document.edited_at, Document.imported_at).desc(),
                "size": Document.size_bytes.desc(),
            }.get(sort, Document.imported_at.desc())
            rows = session.execute(statement.order_by(ordering).limit(max(1, min(limit, 500))).offset(max(0, offset)))
            return {"total": total, "documents": [self.document_dict(session, d) for d in rows.scalars()]}

    def get_document(self, actor: Actor, document_id: str) -> dict[str, Any]:
        with self.ctx.sessions() as session:
            document = self._get(session, document_id)
            self._authorize(session, actor, "document.read", document)
            data = self.document_dict(session, document)
            data["versions"] = [self.version_dict(v, document.current_version_id) for v in document.versions]
            current = self._version(session, document)
            data["inspection"] = (document.versions[0].report or {}).get("inspection") if document.versions else None
            data["current_report"] = current.report
            return data

    def file_path(self, actor: Actor, document_id: str, version_id: str | None = None, *,
                  export: bool = False) -> tuple[Path, str]:
        """Path of a stored version and a safe download name."""
        with self.ctx.sessions() as session:
            document = self._get(session, document_id)
            self._authorize(session, actor, "document.export" if export else "document.download", document)
            version = self._version(session, document, version_id)
            path = self.ctx.storage.resolve(version.storage_key)
            if not path.is_file():
                raise NotFound("The stored file for this version is missing.")
            if export:
                append_event(session, actor=actor.name, interface=actor.interface, action="document.export",
                             document_id=document.id, details={"version": version.number, "sha256": version.sha256})
                session.commit()
            suffix = "" if version.number == 1 else f" (v{version.number})"
            return path, f"{document.display_name}{suffix}.pdf"

    def thumbnail(self, actor: Actor, document_id: str, page: int, version_id: str | None = None,
                  width: int = 220) -> Path:
        if not self.ctx.capabilities.has("pdf.rasterize"):
            raise NotFound("Thumbnails are unavailable: the PDF rasteriser is not installed.")
        width = 440 if width > 300 else 220  # two cached sizes only
        with self.ctx.sessions() as session:
            document = self._get(session, document_id)
            self._authorize(session, actor, "document.read", document)
            version = self._version(session, document, version_id)
            if version.page_count is None:
                raise NotFound("Thumbnails are not available for password-protected PDFs.")
            if page < 1 or page > version.page_count:
                raise NotFound("Page does not exist.")
            source = self.ctx.storage.resolve(version.storage_key)
            target = self.ctx.storage.thumbnail_path(version.id, page, width)
        if not target.is_file():
            try:
                self.ctx.jobs.call(pdf_tasks.render_thumbnail,
                                   {"path": str(source), "output": str(target), "page": page, "width": width})
            except Exception as exc:
                raise NotFound("This page could not be rendered.") from exc
        return target

    def integrity(self, actor: Actor, document_id: str) -> dict[str, Any]:
        """Re-hash stored files and compare with the fingerprints recorded when they were written."""
        with self.ctx.sessions() as session:
            document = self._get(session, document_id)
            self._authorize(session, actor, "document.read", document)
            checks = []
            for version in document.versions:
                path = self.ctx.storage.resolve(version.storage_key)
                if not path.is_file():
                    state = "missing"
                else:
                    state = "match" if pdf_tasks.sha256_file(path) == version.sha256 else "mismatch"
                checks.append({"version": version.number, "state": state, "sha256": version.sha256})
            if any(c["state"] != "match" for c in checks):
                status = "Integrity Check Incomplete" if all(c["state"] != "mismatch" for c in checks) \
                    else "Stored File Changed Outside ARD"
            elif len(checks) > 1:
                status = "Modified Since Baseline"
            else:
                status = "Original Baseline Verified"
            return {
                "status": status,
                "checks": checks,
                "baseline_created_at": document.imported_at.isoformat(),
                "note": "The baseline was recorded by ARD when the file was imported. It shows the file has not "
                        "changed since then; it says nothing about the file before import.",
            }

    # ------------------------------------------------------------------ simple updates
    def update_document(self, actor: Actor, document_id: str, *, display_name: str | None = None,
                        favorite: bool | None = None) -> dict[str, Any]:
        with self.ctx.sessions() as session:
            document = self._get(session, document_id)
            self._authorize(session, actor, "document.update", document)
            changes: dict[str, Any] = {}
            if display_name is not None:
                cleaned = clean_display_name(display_name, from_path=False)
                if cleaned != document.display_name:
                    changes["display_name"] = {"from": document.display_name, "to": cleaned}
                    document.display_name = cleaned
            if favorite is not None and favorite != document.favorite:
                changes["favorite"] = favorite
                document.favorite = favorite
            if changes:
                append_event(session, actor=actor.name, interface=actor.interface, action="document.update",
                             document_id=document.id, details=changes)
            session.commit()
            return self.document_dict(session, document)

    def trash_document(self, actor: Actor, document_id: str) -> dict[str, Any]:
        """Move to the trash. Files stay on disk and the document can be restored."""
        with self.ctx.sessions() as session:
            document = self._get(session, document_id)
            self._authorize(session, actor, "document.trash", document)
            if document.trashed_at is None:
                document.trashed_at = _utcnow()
                append_event(session, actor=actor.name, interface=actor.interface, action="document.trash",
                             document_id=document.id, details={"files_kept": True})
            session.commit()
            return self.document_dict(session, document)

    def restore_document(self, actor: Actor, document_id: str) -> dict[str, Any]:
        with self.ctx.sessions() as session:
            document = self._get(session, document_id)
            self._authorize(session, actor, "document.restore", document)
            if document.trashed_at is not None:
                document.trashed_at = None
                append_event(session, actor=actor.name, interface=actor.interface, action="document.restore",
                             document_id=document.id)
            session.commit()
            return self.document_dict(session, document)

    # ------------------------------------------------------------------ operations
    def _run(self, job_id: str) -> dict[str, Any]:
        """Wake the dispatcher and wait a while. A slow job keeps running and is polled by the client."""
        self.ctx.jobs.notify()
        self.ctx.jobs.wait(job_id, self.ctx.settings.interactive_wait_seconds)
        with self.ctx.sessions() as session:
            job = session.get(Job, job_id)
            data = job_to_dict(job)
        if data["status"] == "failed":
            raise DocumentError(self._friendly_error(data["error"]))
        return data

    @staticmethod
    def _friendly_error(error: str | None) -> str:
        if error and error.startswith("PdfTaskError: "):
            return error[len("PdfTaskError: "):]
        if error and "TimeoutError" in error:
            return "The operation took too long and was stopped. Nothing was changed."
        return "The operation failed and nothing was changed. The original document is intact."

    def apply_operation(self, actor: Actor, document_id: str, operation: str, params: dict[str, Any]) -> dict[str, Any]:
        with self.ctx.sessions() as session:
            document = self._get(session, document_id)
            self._authorize(session, actor, "document.operate", document)
            version = self._version(session, document)
            total = self._require_editable(document, version)
            try:
                plan = plan_operation(operation, params, total)
            except OperationError as exc:
                raise DocumentError(str(exc)) from exc

            new_version_id = new_id()
            output_key = self.ctx.storage.version_key(document.id, new_version_id)
            payload: dict[str, Any] = {
                "op": plan.worker_op, "params": plan.params,
                "source": str(self.ctx.storage.resolve(version.storage_key)),
                "output": str(self.ctx.storage.resolve(output_key)),
            }
            recorded = dict(params or {})
            if plan.worker_op == "header_footer":
                payload["params"] = {**plan.params, "name": document.display_name}
            if plan.needs_document:
                other = self._get(session, plan.needs_document)
                self._authorize(session, actor, "document.read", other)
                other_version = self._version(session, other)
                other_total = self._require_editable(other, other_version)
                wanted = plan.params.get("pages")
                try:
                    pages = parse_page_ranges(wanted, other_total) if isinstance(wanted, str) else \
                        (wanted or list(range(1, other_total + 1)))
                    if not isinstance(pages, list) or any(
                            not isinstance(n, int) or isinstance(n, bool) or n < 1 or n > other_total for n in pages):
                        raise OperationError(f"Those pages do not exist in the other document ({other_total} pages).")
                except OperationError as exc:
                    raise DocumentError(str(exc)) from exc
                if total + len(pages) > self.ctx.settings.max_pages:
                    raise DocumentError(f"The result would exceed the {self.ctx.settings.max_pages}-page limit.")
                payload["params"] = {**plan.params, "pages": pages}
                payload["extra_source"] = str(self.ctx.storage.resolve(other_version.storage_key))
                recorded["from_document_sha256"] = other_version.sha256

            job = self.ctx.jobs.submit(
                session, "pdf.transform", payload, document_id=document.id, priority=PRIORITY_INTERACTIVE,
                context={"actor": actor.name, "interface": actor.interface, "operation": operation,
                         "parameters": recorded, "summary": plan.summary, "parent_version_id": version.id,
                         "new_version_id": new_version_id, "output_key": output_key},
            )
            session.commit()
            job_id = job.id
        return self._run(job_id)

    MAX_IMAGES = 20
    MAX_IMAGE_BYTES = 10 * 1024 * 1024

    def annotate_document(self, actor: Actor, document_id: str, items: Any, images: list[bytes] | None = None) -> dict[str, Any]:
        """Add overlays (text, image, stamp) and annotations (highlight, note, shapes, drawing) as a new version."""
        images = images or []
        if len(images) > self.MAX_IMAGES:
            raise DocumentError(f"Add at most {self.MAX_IMAGES} images at a time.")
        for data in images:
            if len(data) > self.MAX_IMAGE_BYTES:
                raise DocumentError("An image is larger than the 10 MB limit.")
            if not (data.startswith(b"\x89PNG\r\n\x1a\n") or data.startswith(b"\xff\xd8")):
                raise DocumentError("Images must be PNG or JPEG files.")

        temp_files: list[str] = []
        try:
            with self.ctx.sessions() as session:
                document = self._get(session, document_id)
                self._authorize(session, actor, "document.operate", document)
                version = self._version(session, document)
                total = self._require_editable(document, version)
                try:
                    clean = plan_annotations(items, total, len(images))
                except OperationError as exc:
                    raise DocumentError(str(exc)) from exc

                # Item text and image bytes go to temporary files, not into the job record or the audit log.
                items_path = self.ctx.storage.temp_path(".items.json")
                items_path.write_text(json.dumps(clean), encoding="utf-8")
                temp_files.append(str(items_path))
                image_paths: dict[str, str] = {}
                for index in sorted({item["image"] for item in clean if item["type"] == "image"}):
                    path = self.ctx.storage.temp_path(".img")
                    path.write_bytes(images[index])
                    temp_files.append(str(path))
                    image_paths[str(index)] = str(path)

                counts: dict[str, int] = {}
                for item in clean:
                    counts[item["type"]] = counts.get(item["type"], 0) + 1
                count = len(clean)
                new_version_id = new_id()
                output_key = self.ctx.storage.version_key(document.id, new_version_id)
                job = self.ctx.jobs.submit(
                    session, "pdf.transform",
                    {"op": "annotate", "params": {"items_path": str(items_path), "image_paths": image_paths},
                     "source": str(self.ctx.storage.resolve(version.storage_key)),
                     "output": str(self.ctx.storage.resolve(output_key)), "cleanup": list(temp_files)},
                    document_id=document.id, priority=PRIORITY_INTERACTIVE,
                    context={"actor": actor.name, "interface": actor.interface, "operation": "annotate",
                             "parameters": {"added": counts, "pages": sorted({item["page"] for item in clean})},
                             "summary": f"Added {count} item{'s' if count != 1 else ''}",
                             "parent_version_id": version.id, "new_version_id": new_version_id,
                             "output_key": output_key},
                )
                session.commit()
                job_id = job.id
        except BaseException:
            for path in temp_files:
                self.ctx.storage.remove_quietly(path)
            raise
        return self._run(job_id)

    def _submit_version_job(self, session: Session, actor: Actor, document: Document, version: DocumentVersion,
                            op: str, params: dict[str, Any], *, operation: str, recorded: dict[str, Any],
                            summary: str, transient: dict[str, Any] | None = None) -> str:
        new_version_id = new_id()
        output_key = self.ctx.storage.version_key(document.id, new_version_id)
        job = self.ctx.jobs.submit(
            session, "pdf.transform",
            {"op": op, "params": params, "source": str(self.ctx.storage.resolve(version.storage_key)),
             "output": str(self.ctx.storage.resolve(output_key))},
            document_id=document.id, priority=PRIORITY_INTERACTIVE, transient=transient,
            context={"actor": actor.name, "interface": actor.interface, "operation": operation,
                     "parameters": recorded, "summary": summary, "parent_version_id": version.id,
                     "new_version_id": new_version_id, "output_key": output_key},
        )
        session.commit()
        return job.id

    def redact_document(self, actor: Actor, document_id: str, areas: Any) -> dict[str, Any]:
        """Black out areas by replacing their pages with images, then verify the result."""
        with self.ctx.sessions() as session:
            document = self._get(session, document_id)
            self._authorize(session, actor, "document.operate", document)
            version = self._version(session, document)
            total = self._require_editable(document, version)
            if not self.ctx.capabilities.has("pdf.rasterize"):
                raise DocumentError("Redaction needs the PDF rasteriser, which is not installed.")
            try:
                clean = plan_redaction(areas, total)
            except OperationError as exc:
                raise DocumentError(str(exc)) from exc
            pages = sorted({area["page"] for area in clean})
            job_id = self._submit_version_job(
                session, actor, document, version, "redact", {"areas": clean}, operation="redact",
                recorded={"pages": pages, "areas": len(clean)},
                summary=f"Redacted {len(clean)} area{'s' if len(clean) != 1 else ''} on {len(pages)} page{'s' if len(pages) != 1 else ''}")
        return self._run(job_id)

    def protect_document(self, actor: Actor, document_id: str, owner_password: Any, user_password: Any = None,
                         permissions: Any = None) -> dict[str, Any]:
        """Encrypt as a new version. Passwords are held in memory for the job only and never stored or logged."""
        with self.ctx.sessions() as session:
            document = self._get(session, document_id)
            self._authorize(session, actor, "document.operate", document)
            version = self._version(session, document)
            self._require_editable(document, version)
            try:
                allowed = plan_protection(owner_password, user_password, permissions)
            except OperationError as exc:
                raise DocumentError(str(exc)) from exc
            job_id = self._submit_version_job(
                session, actor, document, version, "protect", {"permissions": allowed}, operation="protect",
                recorded={"permissions": allowed, "password_to_open": bool(user_password)},
                summary="Password-protected (AES-256)",
                transient={"secrets": {"owner_password": owner_password, "user_password": user_password or ""}})
        return self._run(job_id)

    def unlock_document(self, actor: Actor, document_id: str, password: Any) -> dict[str, Any]:
        """Write an unprotected version of a protected document. Needs its owner password."""
        if not isinstance(password, str) or len(password) > 1000:
            raise DocumentError("Enter the document's password.")
        with self.ctx.sessions() as session:
            document = self._get(session, document_id)
            self._authorize(session, actor, "document.operate", document)
            version = self._version(session, document)
            if not document.is_encrypted:
                raise DocumentError("This document is not password-protected.")
            job_id = self._submit_version_job(
                session, actor, document, version, "unlock", {}, operation="unlock", recorded={},
                summary="Removed password protection", transient={"secrets": {"password": password}})
        return self._run(job_id)

    # ------------------------------------------------------------------ forms
    def _readable_source(self, session: Session, actor: Actor, document_id: str) -> tuple[Document, DocumentVersion, Path]:
        document = self._get(session, document_id)
        self._authorize(session, actor, "document.read", document)
        version = self._version(session, document)
        return document, version, self.ctx.storage.resolve(version.storage_key)

    def form_fields(self, actor: Actor, document_id: str) -> dict[str, Any]:
        with self.ctx.sessions() as session:
            _document, _version, path = self._readable_source(session, actor, document_id)
        try:
            return self.ctx.jobs.call(pdf_forms.list_fields, {"path": str(path)})
        except Exception as exc:
            log.warning("worker call failed", extra={"data": {"error": f"{exc.__class__.__name__}: {exc}"[:300]}})
            raise DocumentError("The form in this document could not be read.") from exc

    def fill_form(self, actor: Actor, document_id: str, values: Any) -> dict[str, Any]:
        """Save form values as a new version. Values are document content: they go to a temporary file, not the job record."""
        temp: Path | None = None
        try:
            with self.ctx.sessions() as session:
                document = self._get(session, document_id)
                self._authorize(session, actor, "document.operate", document)
                version = self._version(session, document)
                self._require_editable(document, version)
                try:
                    clean = plan_form_values(values)
                except OperationError as exc:
                    raise DocumentError(str(exc)) from exc
                temp = self.ctx.storage.temp_path(".form.json")
                temp.write_text(json.dumps(clean), encoding="utf-8")
                new_version_id = new_id()
                output_key = self.ctx.storage.version_key(document.id, new_version_id)
                count = len(clean)
                job = self.ctx.jobs.submit(
                    session, "pdf.transform",
                    {"op": "fill_form", "params": {"values_path": str(temp)},
                     "source": str(self.ctx.storage.resolve(version.storage_key)),
                     "output": str(self.ctx.storage.resolve(output_key)), "cleanup": [str(temp)]},
                    document_id=document.id, priority=PRIORITY_INTERACTIVE,
                    context={"actor": actor.name, "interface": actor.interface, "operation": "fill_form",
                             "parameters": {"fields": sorted(clean)[:200]},
                             "summary": f"Filled in {count} form field{'s' if count != 1 else ''}",
                             "parent_version_id": version.id, "new_version_id": new_version_id,
                             "output_key": output_key},
                )
                session.commit()
                job_id = job.id
        except BaseException:
            if temp is not None:
                self.ctx.storage.remove_quietly(temp)
            raise
        return self._run(job_id)

    def export_form_values(self, actor: Actor, document_id: str) -> tuple[dict[str, Any], str]:
        """Current form values as {name: value}. Password fields are left out."""
        fields = self.form_fields(actor, document_id)
        if not fields.get("fields"):
            raise DocumentError("This document has no form fields.")
        with self.ctx.sessions() as session:
            document = self._get(session, document_id)
            append_event(session, actor=actor.name, interface=actor.interface, action="document.export",
                         document_id=document.id, details={"format": "form values"})
            session.commit()
            name = document.display_name
        values = {f["name"]: f["value"] for f in fields["fields"] if f["type"] != "signature" and not f.get("password")}
        return values, name

    # ------------------------------------------------------------------ signatures
    def signatures(self, actor: Actor, document_id: str) -> dict[str, Any]:
        with self.ctx.sessions() as session:
            _document, _version, path = self._readable_source(session, actor, document_id)
        try:
            return self.ctx.jobs.call(pdf_sign.validate, {"path": str(path)}, timeout=120)
        except Exception as exc:
            log.warning("worker call failed", extra={"data": {"error": f"{exc.__class__.__name__}: {exc}"[:300]}})
            raise DocumentError("The signatures in this document could not be examined.") from exc

    def sign_document(self, actor: Actor, document_id: str, p12: bytes, passphrase: str, reason: str = "",
                      location: str = "") -> dict[str, Any]:
        """Sign with a PKCS#12 certificate supplied for this one operation. The key is never stored."""
        import base64

        if not p12 or len(p12) > 1_000_000:
            raise DocumentError("Choose a .p12 or .pfx certificate file (up to 1 MB).")
        if not isinstance(passphrase, str) or len(passphrase) > 1000:
            raise DocumentError("The passphrase is not valid.")
        details = {}
        for label, value in (("reason", reason), ("location", location)):
            text = "".join(ch for ch in (value or "") if ch.isprintable()).strip()
            if len(text) > 200:
                raise DocumentError(f"The {label} is too long.")
            details[label] = text
        with self.ctx.sessions() as session:
            document = self._get(session, document_id)
            self._authorize(session, actor, "document.operate", document)
            version = self._version(session, document)
            self._require_editable(document, version)
            job_id = self._submit_version_job(
                session, actor, document, version, "sign", details, operation="sign", recorded=details,
                summary="Signed with a digital certificate",
                transient={"secrets": {"p12": base64.b64encode(p12).decode("ascii"), "passphrase": passphrase}})
        return self._run(job_id)

    def pdfa_report(self, actor: Actor, document_id: str) -> dict[str, Any]:
        import shutil

        with self.ctx.sessions() as session:
            _document, _version, path = self._readable_source(session, actor, document_id)
        validator = shutil.which("verapdf") if self.ctx.capabilities.has("pdfa.validate") else None
        try:
            return self.ctx.jobs.call(pdfa_check.check, {"path": str(path), "verapdf": validator}, timeout=240)
        except Exception as exc:
            log.warning("worker call failed", extra={"data": {"error": f"{exc.__class__.__name__}: {exc}"[:300]}})
            raise DocumentError("The PDF/A check could not be completed.") from exc

    # ------------------------------------------------------------------ office conversion
    def import_office(self, actor: Actor, stream: BinaryIO, filename: str) -> dict[str, Any]:
        """Convert an office document to PDF with LibreOffice and import the result."""
        import shutil

        with self.ctx.sessions() as session:
            self._authorize(session, actor, "document.import")
        extension = Path(PurePosixPath(filename or "").name).suffix.lower()
        if extension not in office_convert.OFFICE_EXTENSIONS:
            raise ImportRejected("This file type cannot be converted.")
        if not self.ctx.capabilities.has("convert.office"):
            raise ImportRejected("Converting office documents needs LibreOffice, which is not installed.")
        executable = shutil.which("libreoffice") or shutil.which("soffice")
        if not executable:
            raise ImportRejected("Converting office documents needs LibreOffice, which is not installed.")

        upload, source_sha256, size = self.ctx.storage.receive(stream, self.ctx.settings.max_upload_bytes)
        workdir = self.ctx.storage.resolve(f"temporary/convert-{new_id()}")
        try:
            if size == 0:
                raise ImportRejected("The file is empty.")
            workdir.mkdir(parents=True)
            source = workdir / f"input{extension}"          # a fixed name: the upload's own name is never used as a path
            upload.replace(source)
            try:
                result = self.ctx.jobs.call(office_convert.convert,
                                            {"executable": executable, "source": str(source), "workdir": str(workdir)},
                                            timeout=office_convert.TIMEOUT_SECONDS + 30)
            except office_convert.ConvertError as exc:
                raise ImportRejected(str(exc)) from exc
            except Exception as exc:
                raise ImportRejected("The conversion failed.") from exc
            stem = clean_display_name(Path(PurePosixPath(filename).name).stem + ".pdf")
            with open(result["path"], "rb") as handle:
                imported = self.import_document(actor, handle, f"{stem}.pdf")
        finally:
            upload.unlink(missing_ok=True)
            shutil.rmtree(workdir, ignore_errors=True)

        with self.ctx.sessions() as session:
            document = session.get(Document, imported["document"]["id"])
            if document is not None and not imported["already_imported"]:
                document.origin = "convert"
                document.original_filename = (filename or "")[:255]
                append_event(session, actor=actor.name, interface=actor.interface, action="document.import",
                             document_id=document.id,
                             details={"converted_from": extension.lstrip("."), "source_sha256": source_sha256,
                                      "engine": "LibreOffice"})
                session.commit()
                imported["document"] = self.document_dict(session, document)
        imported["converted_from"] = extension.lstrip(".")
        return imported

    EXPORT_FORMATS = {"txt": ("txt", "text/plain; charset=utf-8"), "png": ("zip", "application/zip"),
                      "jpg": ("zip", "application/zip")}

    def export_document(self, actor: Actor, document_id: str, fmt: str, version_id: str | None = None
                        ) -> tuple[Path, str, str]:
        """Convert a version to text or page images. Returns (path, download name, media type)."""
        if fmt not in self.EXPORT_FORMATS:
            raise DocumentError("Export format must be txt, png or jpg.")
        extension, media_type = self.EXPORT_FORMATS[fmt]
        with self.ctx.sessions() as session:
            document = self._get(session, document_id)
            self._authorize(session, actor, "document.export", document)
            version = self._version(session, document, version_id)
            if document.is_encrypted and version.id == document.current_version_id or version.page_count is None:
                raise DocumentError("This PDF is password-protected. Remove the protection before exporting.")
            if fmt != "txt" and not self.ctx.capabilities.has("pdf.rasterize"):
                raise DocumentError("Image export needs the PDF rasteriser, which is not installed.")
            source = self.ctx.storage.resolve(version.storage_key)
            target = self.ctx.storage.resolve(f"derivatives/{version.id}.{fmt}.{extension}")
            name = f"{document.display_name}{'' if version.number == 1 else f' (v{version.number})'}"
            name += ".txt" if fmt == "txt" else f" ({fmt} pages).zip"
            if not target.is_file():
                try:
                    self.ctx.jobs.call(pdf_tasks.export, {"path": str(source), "output": str(target), "format": fmt},
                                       timeout=600)
                except pdf_tasks.PdfTaskError as exc:
                    raise DocumentError(str(exc)) from exc
                except Exception as exc:
                    raise DocumentError("The export failed. The document was not changed.") from exc
            append_event(session, actor=actor.name, interface=actor.interface, action="document.export",
                         document_id=document.id, details={"version": version.number, "format": fmt})
            session.commit()
            return target, name, media_type

    def _cleanup_inputs(self, job: Job) -> None:
        for path in (job.payload or {}).get("cleanup") or []:
            self.ctx.storage.remove_quietly(path)

    def revert_to_version(self, actor: Actor, document_id: str, version_id: str) -> dict[str, Any]:
        """Make an older version current again by copying it forward. History is never rewritten."""
        with self.ctx.sessions() as session:
            document = self._get(session, document_id)
            self._authorize(session, actor, "document.revert", document)
            target = self._version(session, document, version_id)
            current = self._version(session, document)
            if target.id == current.id:
                raise DocumentError("That version is already the current one.")
            new_version_id = new_id()
            output_key = self.ctx.storage.version_key(document.id, new_version_id)
            job = self.ctx.jobs.submit(
                session, "pdf.transform",
                {"op": "copy", "params": {}, "source": str(self.ctx.storage.resolve(target.storage_key)),
                 "output": str(self.ctx.storage.resolve(output_key))},
                document_id=document.id, priority=PRIORITY_INTERACTIVE,
                context={"actor": actor.name, "interface": actor.interface, "operation": "revert",
                         "parameters": {"to_version": target.number}, "summary": f"Reverted to version {target.number}",
                         "parent_version_id": current.id, "new_version_id": new_version_id, "output_key": output_key},
            )
            session.commit()
            job_id = job.id
        return self._run(job_id)

    def merge_documents(self, actor: Actor, document_ids: list[str], name: str | None = None) -> dict[str, Any]:
        if not isinstance(document_ids, list) or len(document_ids) < 2:
            raise DocumentError("Choose at least two documents to merge.")
        if len(document_ids) > 200:
            raise DocumentError("Merge at most 200 documents at a time.")
        with self.ctx.sessions() as session:
            self._authorize(session, actor, "document.merge")
            sources, hashes, names, pages = [], [], [], 0
            for document_id in document_ids:
                document = self._get(session, document_id)
                self._authorize(session, actor, "document.read", document)
                version = self._version(session, document)
                pages += self._require_editable(document, version)
                sources.append(str(self.ctx.storage.resolve(version.storage_key)))
                hashes.append(version.sha256)
                names.append(document.display_name)
            if pages > self.ctx.settings.max_pages:
                raise DocumentError(f"The merged document would have {pages} pages; the limit is "
                                    f"{self.ctx.settings.max_pages}.")
            new_document_id = new_id()
            output_key = self.ctx.storage.original_key(new_document_id)
            job = self.ctx.jobs.submit(
                session, "pdf.merge", {"sources": sources, "output": str(self.ctx.storage.resolve(output_key))},
                priority=PRIORITY_INTERACTIVE,
                context={"actor": actor.name, "interface": actor.interface, "operation": "merge", "origin": "merge",
                         "parameters": {"source_documents": document_ids, "source_sha256": hashes},
                         "documents": [{"id": new_document_id, "output_key": output_key,
                                        "name": clean_display_name(name, from_path=False) if name else f"{names[0]} (merged)"}]},
            )
            session.commit()
            job_id = job.id
        return self._run(job_id)

    def split_document(self, actor: Actor, document_id: str, params: dict[str, Any]) -> dict[str, Any]:
        with self.ctx.sessions() as session:
            document = self._get(session, document_id)
            self._authorize(session, actor, "document.split", document)
            version = self._version(session, document)
            total = self._require_editable(document, version)
            try:
                groups = plan_split(params, total)
            except OperationError as exc:
                raise DocumentError(str(exc)) from exc
            extracting = (params or {}).get("mode") == "pages"
            documents = []
            for index, group in enumerate(groups, start=1):
                new_document_id = new_id()
                label = _describe_pages(group)
                suffix = f"pages {label}" if extracting or len(groups) == 1 else f"part {index}, pages {label}"
                documents.append({"id": new_document_id, "output_key": self.ctx.storage.original_key(new_document_id),
                                  "name": f"{document.display_name} ({suffix})"[:200]})
            job = self.ctx.jobs.submit(
                session, "pdf.split",
                {"source": str(self.ctx.storage.resolve(version.storage_key)), "groups": groups,
                 "outputs": [str(self.ctx.storage.resolve(d["output_key"])) for d in documents]},
                document_id=document.id, priority=PRIORITY_INTERACTIVE,
                context={"actor": actor.name, "interface": actor.interface,
                         "operation": "extract" if extracting else "split",
                         "origin": "extract" if extracting else "split",
                         "parameters": {"source_document": document.id, "source_sha256": version.sha256,
                                        "groups": groups},
                         "documents": documents},
            )
            session.commit()
            job_id = job.id
        return self._run(job_id)

    # ------------------------------------------------------------------ job finalizers (main process)
    def _finalize_version(self, session: Session, job: Job, result: dict[str, Any]) -> dict[str, Any]:
        context, output = job.context, result["outputs"][0]
        self._cleanup_inputs(job)
        document = session.get(Document, job.document_id)
        if document is None:
            raise RuntimeError("Document disappeared while the job was running.")
        if document.current_version_id != context["parent_version_id"]:
            # Another change landed first. Do not silently build on a stale version.
            raise RuntimeError("The document changed while this operation was running. Nothing was saved.")
        number = (session.scalar(select(func.max(DocumentVersion.number)).where(
            DocumentVersion.document_id == document.id)) or 0) + 1
        parent = session.get(DocumentVersion, context["parent_version_id"])
        report = dict(output.get("report") or {})
        report["summary"] = context.get("summary")
        report["signed"] = bool(output.get("signed", False))
        version = DocumentVersion(
            id=context["new_version_id"], document_id=document.id, parent_version_id=context["parent_version_id"],
            number=number, storage_key=context["output_key"], sha256=output["sha256"],
            size_bytes=output["size_bytes"], page_count=output["page_count"], operation=context["operation"],
            parameters=context.get("parameters") or {}, report=report, actor=context["actor"],
            interface=context["interface"], app_version=APP_VERSION,
        )
        session.add(version)
        document.current_version_id = version.id
        # Protection belongs to a version: protect, unlock and revert can each change it.
        document.is_encrypted = bool(output.get("encrypted", False))
        document.edited_at = _utcnow()
        document.lifecycle_state = "edited"
        session.flush()
        self.ctx.storage.make_read_only(self.ctx.storage.resolve(context["output_key"]))
        self._version_ready(session, document.id, version.id, context["output_key"])
        append_event(session, actor=context["actor"], interface=context["interface"], action="document.operate",
                     document_id=document.id,
                     details={"operation": context["operation"], "parameters": context.get("parameters") or {},
                              "input_sha256": parent.sha256 if parent else None, "output_sha256": output["sha256"],
                              "version": number, "job": job.id})
        return {"document_id": document.id, "version_id": version.id, "version_number": number,
                "page_count": output["page_count"], "size_bytes": output["size_bytes"], "report": report,
                "summary": context.get("summary")}

    def _finalize_new_documents(self, session: Session, job: Job, result: dict[str, Any]) -> dict[str, Any]:
        context = job.context
        created = []
        for spec, output in zip(context["documents"], result["outputs"]):
            version_id = new_id()
            document = Document(
                id=spec["id"], display_name=spec["name"], original_filename=f"{spec['name']}.pdf"[:255],
                sha256=output["sha256"], size_bytes=output["size_bytes"], origin=context["origin"],
                current_version_id=version_id,
            )
            session.add(document)
            session.add(DocumentVersion(
                id=version_id, document_id=spec["id"], number=1, storage_key=spec["output_key"],
                sha256=output["sha256"], size_bytes=output["size_bytes"], page_count=output["page_count"],
                operation=context["operation"], parameters=context.get("parameters") or {},
                report=output.get("report") or {}, actor=context["actor"], interface=context["interface"],
                app_version=APP_VERSION,
            ))
            session.flush()
            self.ctx.storage.make_read_only(self.ctx.storage.resolve(spec["output_key"]))
            self._version_ready(session, spec["id"], version_id, spec["output_key"])
            created.append({"id": spec["id"], "display_name": spec["name"], "page_count": output["page_count"]})
        append_event(session, actor=context["actor"], interface=context["interface"],
                     action=f"document.{context['operation'] if context['operation'] == 'merge' else 'split'}",
                     document_id=job.document_id,
                     details={"operation": context["operation"], "created": [c["id"] for c in created],
                              "output_sha256": [o["sha256"] for o in result["outputs"]],
                              "parameters": context.get("parameters") or {}, "job": job.id})
        count = len(created)
        summary = "Merged into a new document" if context["operation"] == "merge" else \
            f"Created {count} new document{'s' if count != 1 else ''}"
        return {"documents": created, "summary": summary}

    def _discard_outputs(self, session: Session, job: Job) -> None:
        """A failed or cancelled job must leave no output behind."""
        payload = job.payload or {}
        self._cleanup_inputs(job)
        for path in [payload.get("output"), *(payload.get("outputs") or [])]:
            if path:
                self.ctx.storage.remove_quietly(path)
        context = job.context or {}
        append_event(session, actor=context.get("actor", "system"), interface=context.get("interface", "system"),
                     action="document.operate", outcome=job.status, document_id=job.document_id,
                     details={"operation": context.get("operation"), "job": job.id, "error": job.error})


def _describe_pages(pages: list[int]) -> str:
    """``[1, 2, 3, 7]`` -> ``"1-3, 7"``."""
    parts: list[str] = []
    start = previous = pages[0]
    for page in pages[1:]:
        if page == previous + 1:
            previous = page
            continue
        parts.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = page
    parts.append(str(start) if start == previous else f"{start}-{previous}")
    text = ", ".join(parts)
    return text if len(text) <= 40 else text[:37] + "..."
