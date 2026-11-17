"""System, Guardian, job and audit endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.api.dependencies import get_actor, get_system
from app.core.config.settings import APP_VERSION
from app.core.policy.engine import Actor
from app.services.system import SystemService

router = APIRouter(prefix="/api", tags=["system"])


class GuardianProfile(BaseModel):
    profile: str = Field(max_length=16)
    custom: dict[str, float] | None = None


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": APP_VERSION}


@router.get("/system/status")
def status(actor: Actor = Depends(get_actor), system: SystemService = Depends(get_system)) -> dict[str, Any]:
    return system.status(actor)


@router.get("/system/capabilities")
def capabilities(refresh: bool = False, actor: Actor = Depends(get_actor),
                 system: SystemService = Depends(get_system)) -> dict[str, Any]:
    return system.capabilities(actor, refresh)


@router.get("/system/guardian")
def guardian(actor: Actor = Depends(get_actor), system: SystemService = Depends(get_system)) -> dict[str, Any]:
    return system.guardian(actor)


@router.put("/system/guardian")
def set_guardian(body: GuardianProfile, actor: Actor = Depends(get_actor),
                 system: SystemService = Depends(get_system)) -> dict[str, Any]:
    try:
        return system.set_guardian_profile(actor, body.profile, body.custom)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/system/diagnostics")
def diagnostics(actor: Actor = Depends(get_actor), system: SystemService = Depends(get_system)) -> dict[str, Any]:
    return system.diagnostics(actor)


@router.get("/system/audit")
def audit(limit: int = Query(100, ge=1, le=1000), document_id: str | None = Query(None, max_length=32),
          actor: Actor = Depends(get_actor), system: SystemService = Depends(get_system)) -> dict[str, Any]:
    return system.audit(actor, limit, document_id)


@router.post("/system/audit/verify")
def verify_audit(actor: Actor = Depends(get_actor), system: SystemService = Depends(get_system)) -> dict[str, Any]:
    return system.verify_audit(actor)


@router.get("/jobs")
def list_jobs(active: bool = False, limit: int = Query(50, ge=1, le=200), actor: Actor = Depends(get_actor),
              system: SystemService = Depends(get_system)) -> dict[str, Any]:
    return system.list_jobs(actor, active, limit)


@router.get("/jobs/{job_id}")
def get_job(job_id: str, actor: Actor = Depends(get_actor),
            system: SystemService = Depends(get_system)) -> dict[str, Any]:
    job = system.get_job(actor, job_id)
    if job is None:
        raise HTTPException(404, "Job not found.")
    return job


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str, actor: Actor = Depends(get_actor),
               system: SystemService = Depends(get_system)) -> dict[str, Any]:
    job = system.cancel_job(actor, job_id)
    if job is None:
        raise HTTPException(404, "Job not found.")
    return job
