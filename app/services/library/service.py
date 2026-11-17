"""Smart Library: text index, search, categories, tags, duplicates and OCR.

Everything here works without embeddings, a vector store or any AI model.
Classification and duplicate detection produce suggestions with their
evidence; nothing is changed or removed on the strength of them.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from app.core.jobs.orchestrator import PRIORITY_BACKGROUND, JobKind, job_to_dict
from app.core.logging import get_logger
from app.core.policy.engine import Actor
from app.models import Document, DocumentTag, DocumentVersion, Job, PageText, Tag
from app.services.documents.service import DocumentError, NotFound
from app.services.integrity.audit import append_event
from app.services.library import classifier, fingerprint
from app.workers import pdf_text

if TYPE_CHECKING:
    from app.core.context import AppContext

log = get_logger("library")

ALBUMS: dict[str, str] = {
    "recent_added": "Recently added",
    "recent_modified": "Recently modified",
    "favorites": "Favorites",
    "scanned": "Scanned documents",
    "needs_ocr": "Needs OCR",
    "large": "Large files",
    "unclassified": "Unclassified",
    "protected": "Password-protected",
}
LARGE_BYTES = 10 * 1024 * 1024
RECENT_DAYS = 30
SIMILAR_MAX_DISTANCE = 6
_TAG = re.compile(r"^[\w][\w .&+/-]{0,39}$", re.UNICODE)


def album_condition(album: str) -> Any:
    """SQL condition for a smart album. Shared with the document listing."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=RECENT_DAYS)
    return {
        "recent_added": Document.imported_at >= cutoff,
        "recent_modified": Document.edited_at >= cutoff,
        "favorites": Document.favorite.is_(True),
        "scanned": Document.ocr_status.in_(("needed", "partial", "done")),
        "needs_ocr": Document.ocr_status.in_(("needed", "partial")),
        "large": Document.size_bytes >= LARGE_BYTES,
        "unclassified": Document.category == "uncategorized",
        "protected": Document.is_encrypted.is_(True),
    }.get(album)


class LibraryService:
    def __init__(self, context: "AppContext") -> None:
        self.ctx = context
        context.jobs.register(JobKind("library.index", pdf_text.extract, self._finalize_index))
        context.version_ready_hooks.append(self.queue_index)

    # ------------------------------------------------------------------ indexing
    def queue_index(self, session: Session, document_id: str, version_id: str, storage_key: str) -> None:
        """Queue text extraction for a new version. Runs in the background and never blocks editing."""
        self.ctx.jobs.submit(
            session, "library.index", {"path": str(self.ctx.storage.resolve(storage_key))},
            priority=PRIORITY_BACKGROUND,          # no document lock: it only reads an immutable file
            context={"operation": "index", "document_id": document_id, "version_id": version_id},
        )

    def queue_missing(self) -> int:
        """Queue indexing for documents whose current version has not been indexed (first run, or after an upgrade)."""
        queued = 0
        with self.ctx.sessions() as session:
            pending = {(job.context or {}).get("version_id") for job in session.execute(
                select(Job).where(Job.kind == "library.index", Job.status.in_(("queued", "running")))).scalars()}
            rows = session.execute(select(Document).where(
                Document.current_version_id.is_not(None),
                (Document.indexed_version_id.is_(None)) | (Document.indexed_version_id != Document.current_version_id)))
            for document in rows.scalars():
                if document.current_version_id in pending:
                    continue
                version = session.get(DocumentVersion, document.current_version_id)
                if version is not None:
                    self.queue_index(session, document.id, version.id, version.storage_key)
                    queued += 1
            session.commit()
        if queued:
            self.ctx.jobs.notify()
        return queued

    def _finalize_index(self, session: Session, job: Job, result: dict[str, Any]) -> dict[str, Any]:
        context = job.context or {}
        document = session.get(Document, context.get("document_id"))
        version_id = context.get("version_id")
        if document is None or document.current_version_id != version_id:
            return {"summary": "Skipped: a newer version exists", "skipped": True}
        version = session.get(DocumentVersion, version_id)

        session.execute(delete(PageText).where(PageText.document_id == document.id))
        session.execute(text("DELETE FROM page_fts WHERE document_id = :d"), {"d": document.id})
        document.indexed_version_id = version_id
        if result.get("locked"):
            document.ocr_status, document.text_chars = "locked", 0
            document.text_fingerprint = document.text_simhash = None
            return {"summary": "Not indexed: password-protected", "pages": 0}

        # Pages whose text came from OCR in this very version carry Tesseract's confidence.
        ocr_pages: dict[int, float | None] = {}
        if version is not None and version.operation == "ocr":
            ocr_pages = {entry["page"]: entry.get("confidence") for entry in (version.report or {}).get("pages", [])}

        pages = result.get("pages") or []
        parts: list[str] = []
        for entry in pages:
            body = entry["text"] or ""
            number = entry["page"]
            from_ocr = number in ocr_pages
            session.add(PageText(
                document_id=document.id, version_id=version_id, page=number, text=body,
                source="ocr" if from_ocr else ("pdf" if body.strip() else "none"),
                needs_ocr=bool(entry.get("needs_ocr")), confidence=ocr_pages.get(number),
            ))
            if body.strip():
                session.execute(text("INSERT INTO page_fts(text, document_id, page) VALUES (:t, :d, :p)"),
                                {"t": body, "d": document.id, "p": number})
                parts.append(body)

        full = "\n".join(parts)
        document.text_chars = len(full)
        document.text_fingerprint = fingerprint.text_fingerprint(full)
        document.text_simhash = fingerprint.simhash(full)

        needing = sum(1 for entry in pages if entry.get("needs_ocr"))
        had_ocr = session.scalar(select(func.count()).select_from(DocumentVersion).where(
            DocumentVersion.document_id == document.id, DocumentVersion.operation == "ocr")) > 0
        document.ocr_status = ("partial" if had_ocr else "needed") if needing else ("done" if had_ocr else "not_needed")

        if document.category_source != "user":
            self._apply_suggestion(document, full)
        return {"summary": f"Indexed {len(pages)} page{'s' if len(pages) != 1 else ''}", "pages": len(pages),
                "characters": len(full), "pages_needing_ocr": needing}

    @staticmethod
    def _apply_suggestion(document: Document, full_text: str) -> None:
        suggestion = classifier.classify(full_text, document.display_name)
        document.category = suggestion.category
        document.category_confidence = suggestion.confidence if suggestion.category != "uncategorized" else None
        document.category_evidence = suggestion.evidence
        document.category_source = "auto"
        document.category_engine = classifier.ENGINE
        document.categorized_at = datetime.now(timezone.utc)

    # ------------------------------------------------------------------ search
    @staticmethod
    def _terms(query: str) -> list[str]:
        return [term for term in re.split(r"\s+", query.strip()) if term]

    @staticmethod
    def _snippet(body: str, terms: list[str], width: int = 70) -> dict[str, str] | None:
        lower = body.lower()
        best = None
        for term in terms:
            index = lower.find(term.lower())
            if index != -1 and (best is None or index < best[0]):
                best = (index, len(term))
        if best is None:
            return None
        start, length = best
        left = max(0, start - width)
        squash = lambda value: re.sub(r"\s+", " ", value)          # noqa: E731
        return {"before": ("…" if left else "") + squash(body[left:start]),
                "match": body[start:start + length],
                "after": squash(body[start + length:start + length + width]) + "…"}

    def search(self, actor: Actor, query: str, limit: int = 60) -> dict[str, Any]:
        """Find documents by words in their text or name. Every text hit carries the page it was found on."""
        self.ctx.policy.authorize(actor, "document.list")
        terms = self._terms(query)[:12]
        if not terms:
            return {"query": query, "results": [], "total": 0}
        with self.ctx.sessions() as session:
            hits: dict[str, list[int]] = {}
            indexable = [term for term in terms if len(term) >= 3]
            if indexable:
                match = " AND ".join('"' + term.replace('"', '""') + '"' for term in indexable)
                rows = session.execute(text(
                    "SELECT document_id, page FROM page_fts WHERE page_fts MATCH :m ORDER BY document_id, page LIMIT 5000"),
                    {"m": match}).all()
                for document_id, page in rows:
                    hits.setdefault(document_id, []).append(int(page))
            else:
                # Very short words cannot use the index; fall back to a plain scan.
                like = "%" + terms[0].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                for row in session.execute(select(PageText.document_id, PageText.page).where(
                        PageText.text.ilike(like, escape="\\")).order_by(PageText.document_id, PageText.page).limit(5000)):
                    hits.setdefault(row.document_id, []).append(row.page)

            name_like = "%" + query.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            named = {row.id for row in session.execute(select(Document.id).where(
                Document.display_name.ilike(name_like, escape="\\")))}

            results = []
            for document_id in set(hits) | named:
                document = session.get(Document, document_id)
                if document is None or document.trashed_at is not None:
                    continue
                pages = sorted(set(hits.get(document_id, [])))
                snippet = None
                if pages:
                    row = session.execute(select(PageText).where(
                        PageText.document_id == document_id, PageText.page == pages[0])).scalars().first()
                    if row is not None:
                        snippet = self._snippet(row.text, indexable or terms)
                        if snippet:
                            snippet.update(page=row.page, from_ocr=row.source == "ocr")
                results.append({
                    "document": self.ctx.documents.document_dict(session, document),
                    "name_match": document_id in named, "pages": pages[:50], "page_hits": len(pages), "snippet": snippet,
                })
            results.sort(key=lambda item: (-(item["page_hits"] + (5 if item["name_match"] else 0)),
                                           item["document"]["display_name"].lower()))
            return {"query": query, "results": results[:limit], "total": len(results)}

    # ------------------------------------------------------------------ overview, categories, tags
    def overview(self, actor: Actor) -> dict[str, Any]:
        self.ctx.policy.authorize(actor, "document.list")
        with self.ctx.sessions() as session:
            live = Document.trashed_at.is_(None)
            by_category = dict(session.execute(
                select(Document.category, func.count()).where(live).group_by(Document.category)).all())
            albums = [{"id": key, "label": label,
                       "count": session.scalar(select(func.count()).select_from(Document).where(live, album_condition(key)))}
                      for key, label in ALBUMS.items()]
            tags = [{"name": name, "count": count} for name, count in session.execute(
                select(Tag.name, func.count(DocumentTag.document_id)).join(DocumentTag, DocumentTag.tag_id == Tag.id)
                .join(Document, Document.id == DocumentTag.document_id).where(live).group_by(Tag.id).order_by(Tag.name))]
            waiting = session.scalar(select(func.count()).select_from(Job).where(
                Job.kind == "library.index", Job.status.in_(("queued", "running"))))
        languages = self._languages()
        return {
            "categories": [{"id": key, "label": label, "count": by_category.get(key, 0)}
                           for key, label in classifier.CATEGORIES.items()],
            "albums": albums, "tags": tags, "indexing": waiting,
            "ocr": {"available": bool(languages) and self.ctx.capabilities.has("ocr.searchable_pdf"),
                    "languages": languages},
            "classifier": classifier.ENGINE,
        }

    def set_category(self, actor: Actor, document_id: str, category: str) -> dict[str, Any]:
        """Set the category by hand, or pass "auto" to go back to ARD's suggestion."""
        if category != "auto" and category not in classifier.CATEGORIES:
            raise DocumentError("Unknown category.")
        with self.ctx.sessions() as session:
            document = session.get(Document, document_id)
            if document is None:
                raise NotFound("Document not found.")
            self.ctx.documents._authorize(session, actor, "document.update", document)
            previous = {"category": document.category, "source": document.category_source}
            if category == "auto":
                full = "\n".join(row.text for row in session.execute(select(PageText).where(
                    PageText.document_id == document.id).order_by(PageText.page)).scalars())
                self._apply_suggestion(document, full)
            else:
                # The user's choice is stored locally and is never replaced by a later automatic suggestion.
                document.category, document.category_source = category, "user"
                document.category_confidence, document.category_evidence = None, []
                document.categorized_at = datetime.now(timezone.utc)
            append_event(session, actor=actor.name, interface=actor.interface, action="document.update",
                         document_id=document.id,
                         details={"category": {"from": previous, "to": document.category, "source": document.category_source}})
            session.commit()
            return self.ctx.documents.document_dict(session, document)

    def set_tags(self, actor: Actor, document_id: str, names: Any) -> dict[str, Any]:
        if not isinstance(names, list) or len(names) > 30:
            raise DocumentError("Give up to 30 tags.")
        clean: list[str] = []
        for name in names:
            if not isinstance(name, str):
                raise DocumentError("Tags must be text.")
            tag = re.sub(r"\s+", " ", name).strip()
            if not tag:
                continue
            if not _TAG.match(tag):
                raise DocumentError(f"“{tag[:40]}” is not a usable tag. Use up to 40 letters, digits and spaces.")
            if tag.lower() not in [existing.lower() for existing in clean]:
                clean.append(tag)
        with self.ctx.sessions() as session:
            document = session.get(Document, document_id)
            if document is None:
                raise NotFound("Document not found.")
            self.ctx.documents._authorize(session, actor, "document.update", document)
            session.execute(delete(DocumentTag).where(DocumentTag.document_id == document.id))
            for name in clean:
                tag = session.execute(select(Tag).where(func.lower(Tag.name) == name.lower())).scalars().first()
                if tag is None:
                    tag = Tag(name=name)
                    session.add(tag)
                    session.flush()
                session.add(DocumentTag(document_id=document.id, tag_id=tag.id))
            session.flush()
            # Tags nobody uses any more are removed so the tag list stays meaningful.
            session.execute(delete(Tag).where(Tag.id.not_in(select(DocumentTag.tag_id))))
            append_event(session, actor=actor.name, interface=actor.interface, action="document.update",
                         document_id=document.id, details={"tags": clean})
            session.commit()
            return self.ctx.documents.document_dict(session, document)

    # ------------------------------------------------------------------ duplicates
    def duplicates(self, actor: Actor) -> dict[str, Any]:
        """Groups of documents that look like copies of each other. For review only; nothing is deleted."""
        self.ctx.policy.authorize(actor, "document.list")
        with self.ctx.sessions() as session:
            rows = session.execute(
                select(Document, DocumentVersion.sha256).join(DocumentVersion, DocumentVersion.id == Document.current_version_id)
                .where(Document.trashed_at.is_(None)).order_by(Document.imported_at).limit(5000)).all()
            documents = {document.id: document for document, _sha in rows}
            groups: list[dict[str, Any]] = []
            paired: set[frozenset[str]] = set()

            def add(kind: str, label: str, ids: list[str], similarity: int | None = None) -> None:
                key = frozenset(ids)
                if len(ids) < 2 or key in paired:
                    return
                paired.add(key)
                for a in ids:
                    for b in ids:
                        if a < b:
                            paired.add(frozenset((a, b)))
                groups.append({"kind": kind, "label": label, "similarity": similarity,
                               "documents": [self.ctx.documents.document_dict(session, documents[i]) for i in ids]})

            by_file: dict[str, list[str]] = {}
            for document, sha in rows:
                by_file.setdefault(sha, []).append(document.id)
            for ids in by_file.values():
                add("identical_file", "Identical files", ids)

            by_text: dict[str, list[str]] = {}
            for document, _sha in rows:
                if document.text_fingerprint:
                    by_text.setdefault(document.text_fingerprint, []).append(document.id)
            for ids in by_text.values():
                add("identical_text", "Same text, different file", ids)

            candidates = [d for d, _sha in rows if d.text_simhash and d.text_chars >= 400][:3000]
            for index, first in enumerate(candidates):
                for second in candidates[index + 1:]:
                    if frozenset((first.id, second.id)) in paired:
                        continue
                    distance = fingerprint.hamming(first.text_simhash, second.text_simhash)
                    if distance <= SIMILAR_MAX_DISTANCE:
                        add("similar_text", "Very similar text", [first.id, second.id], round(100 * (64 - distance) / 64))
            return {"groups": groups,
                    "note": "These are candidates to review. ARD never deletes or merges documents on its own."}

    # ------------------------------------------------------------------ page text (OCR review)
    def page_text(self, actor: Actor, document_id: str, page: int | None = None) -> dict[str, Any]:
        with self.ctx.sessions() as session:
            document = session.get(Document, document_id)
            if document is None:
                raise NotFound("Document not found.")
            self.ctx.documents._authorize(session, actor, "document.read", document)
            rows = session.execute(select(PageText).where(PageText.document_id == document.id).order_by(PageText.page)).scalars().all()
            data: dict[str, Any] = {
                "indexed": document.indexed_version_id == document.current_version_id, "ocr_status": document.ocr_status,
                "pages": [{"page": row.page, "characters": len(row.text), "source": row.source,
                           "needs_ocr": row.needs_ocr, "confidence": row.confidence,
                           "low_confidence": row.confidence is not None and row.confidence < 70} for row in rows],
            }
            if page is not None:
                row = next((r for r in rows if r.page == page), None)
                if row is None:
                    raise NotFound("That page has not been indexed.")
                data["text"] = {"page": row.page, "text": row.text, "source": row.source, "confidence": row.confidence}
            return data

    # ------------------------------------------------------------------ OCR
    def _languages(self) -> list[str]:
        for entry in self.ctx.capabilities.all():
            if entry["id"] == "tesseract" and entry["available"]:
                return [code for code in entry.get("languages", []) if code != "osd"]
        return []

    def run_ocr(self, actor: Actor, document_id: str, languages: Any = None) -> dict[str, Any]:
        """Queue OCR for a document. Returns at once; the job runs in the background under the Guardian."""
        installed = self._languages()
        if not installed:
            raise DocumentError("OCR needs Tesseract, which is not installed.")
        if not self.ctx.capabilities.has("ocr.searchable_pdf"):
            raise DocumentError("OCR needs the OCRmyPDF package, which is not installed.")
        wanted = languages or (["eng"] if "eng" in installed else installed[:1])
        if not isinstance(wanted, list) or not wanted or len(wanted) > 4 or any(not isinstance(c, str) for c in wanted):
            raise DocumentError("Choose between one and four languages.")
        missing = [code for code in wanted if code not in installed]
        if missing:
            raise DocumentError(f"The language pack for “{missing[0]}” is not installed. Installed: {', '.join(installed)}.")

        with self.ctx.sessions() as session:
            document = session.get(Document, document_id)
            if document is None:
                raise NotFound("Document not found.")
            documents = self.ctx.documents
            documents._authorize(session, actor, "document.operate", document)
            version = documents._version(session, document)
            documents._require_editable(document, version)
            if document.indexed_version_id == document.current_version_id and document.ocr_status in ("not_needed", "done"):
                raise DocumentError("Every page of this document already has text, so there is nothing to recognise.")
            active = session.scalar(select(func.count()).select_from(Job).where(
                Job.document_id == document.id, Job.status.in_(("queued", "running")), Job.kind == "pdf.transform"))
            if active:
                raise DocumentError("Another operation on this document is still running. Try again when it has finished.")
            from app.models import new_id

            new_version_id = new_id()
            output_key = self.ctx.storage.version_key(document.id, new_version_id)
            job = self.ctx.jobs.submit(
                session, "pdf.transform",
                {"op": "ocr", "params": {"languages": wanted, "jobs": 1},
                 "source": str(self.ctx.storage.resolve(version.storage_key)),
                 "output": str(self.ctx.storage.resolve(output_key))},
                document_id=document.id, priority=PRIORITY_BACKGROUND,
                context={"actor": actor.name, "interface": actor.interface, "operation": "ocr",
                         "parameters": {"languages": wanted}, "summary": "Recognised text (OCR)",
                         "parent_version_id": version.id, "new_version_id": new_version_id, "output_key": output_key},
            )
            session.commit()
            data = job_to_dict(job)
        self.ctx.jobs.notify()
        return data
