"""Smart Library endpoints: search, overview, categories, tags, duplicates, OCR."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from app.api.dependencies import get_actor
from app.core.policy.engine import Actor
from app.services.library.service import LibraryService

router = APIRouter(prefix="/api", tags=["library"])


def get_library(request: Request) -> LibraryService:
    return request.app.state.context.library


class CategoryUpdate(BaseModel):
    category: str = Field(max_length=32)


class TagsUpdate(BaseModel):
    tags: list[str] = Field(max_length=30)


class OcrRequest(BaseModel):
    languages: list[str] | None = Field(default=None, max_length=4)


@router.get("/search")
def search(q: str = Query("", max_length=200), limit: int = Query(60, ge=1, le=200), actor: Actor = Depends(get_actor),
           library: LibraryService = Depends(get_library)) -> dict[str, Any]:
    return library.search(actor, q, limit)


@router.get("/library/overview")
def overview(actor: Actor = Depends(get_actor), library: LibraryService = Depends(get_library)) -> dict[str, Any]:
    return library.overview(actor)


@router.get("/library/duplicates")
def duplicates(actor: Actor = Depends(get_actor), library: LibraryService = Depends(get_library)) -> dict[str, Any]:
    return library.duplicates(actor)


@router.put("/documents/{document_id}/category")
def set_category(document_id: str, body: CategoryUpdate, actor: Actor = Depends(get_actor),
                 library: LibraryService = Depends(get_library)) -> dict[str, Any]:
    return library.set_category(actor, document_id, body.category)


@router.put("/documents/{document_id}/tags")
def set_tags(document_id: str, body: TagsUpdate, actor: Actor = Depends(get_actor),
             library: LibraryService = Depends(get_library)) -> dict[str, Any]:
    return library.set_tags(actor, document_id, body.tags)


@router.get("/documents/{document_id}/text")
def page_text(document_id: str, page: int | None = Query(None, ge=1), actor: Actor = Depends(get_actor),
              library: LibraryService = Depends(get_library)) -> dict[str, Any]:
    return library.page_text(actor, document_id, page)


@router.post("/documents/{document_id}/ocr", status_code=202)
def run_ocr(document_id: str, body: OcrRequest, actor: Actor = Depends(get_actor),
            library: LibraryService = Depends(get_library)) -> dict[str, Any]:
    """Queue OCR. Poll /api/jobs/{id} for the result."""
    job = library.run_ocr(actor, document_id, body.languages)
    return {"job": job, "done": False, "result": None}
