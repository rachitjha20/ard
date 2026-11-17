"""System-level services: status, Guardian, capabilities, jobs and the audit log.

Like the document service, these are shared by every interface and go through
the policy engine.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select

from app.core.config.settings import APP_VERSION
from app.core.jobs.orchestrator import ACTIVE, job_to_dict
from app.core.policy.engine import Actor
from app.models import AuditEvent, Document, Job
from app.services.integrity.audit import append_event, verify_chain

if TYPE_CHECKING:
    from app.core.context import AppContext


class SystemService:
    def __init__(self, context: "AppContext") -> None:
        self.ctx = context

    def status(self, actor: Actor) -> dict[str, Any]:
        self.ctx.policy.authorize(actor, "system.read")
        with self.ctx.sessions() as session:
            documents = session.scalar(select(func.count()).select_from(Document).where(Document.trashed_at.is_(None)))
            trashed = session.scalar(select(func.count()).select_from(Document).where(Document.trashed_at.is_not(None)))
            stored = session.scalar(select(func.coalesce(func.sum(Document.size_bytes), 0)).where(
                Document.trashed_at.is_(None)))
            active = session.scalar(select(func.count()).select_from(Job).where(Job.status.in_(ACTIVE)))
        return {
            "version": APP_VERSION,
            "documents": documents,
            "trashed": trashed,
            "original_bytes": stored,
            "active_jobs": active,
            "guardian_state": self.ctx.guardian.state,
            "safe_mode": {"active": self.ctx.policy.safe_mode, "reason": self.ctx.policy.safe_mode_reason},
            "viewer_available": self.ctx.capabilities.has("pdf.render"),
            "thumbnails_available": self.ctx.capabilities.has("pdf.rasterize"),
        }

    def capabilities(self, actor: Actor, refresh: bool = False) -> dict[str, Any]:
        self.ctx.policy.authorize(actor, "system.read")
        integrations = self.ctx.capabilities.refresh() if refresh else self.ctx.capabilities.all()
        return {"integrations": integrations, "capabilities": self.ctx.capabilities.available_capabilities()}

    def guardian(self, actor: Actor) -> dict[str, Any]:
        self.ctx.policy.authorize(actor, "system.read")
        data = self.ctx.guardian.snapshot()
        data["enabled"] = self.ctx.settings.guardian_enabled
        data["jobs"] = self.ctx.jobs.stats()
        return data

    def set_guardian_profile(self, actor: Actor, profile: str, custom: dict[str, Any] | None = None) -> dict[str, Any]:
        self.ctx.policy.authorize(actor, "system.configure")
        applied = self.ctx.guardian.set_profile(profile, custom)  # raises ValueError on bad input
        with self.ctx.sessions() as session:
            self.ctx.set_setting(session, "guardian.profile", profile)
            if profile == "custom":
                self.ctx.set_setting(session, "guardian.custom", custom or {})
            append_event(session, actor=actor.name, interface=actor.interface, action="system.configure",
                         details={"guardian_profile": applied.name})
            session.commit()
        self.ctx.jobs.notify()
        return self.guardian(actor)

    def diagnostics(self, actor: Actor) -> dict[str, Any]:
        self.ctx.policy.authorize(actor, "system.read")
        data = self.ctx.diagnostics()
        data["integrations"] = [
            {"id": e["id"], "available": e["available"], "version": e["version"]} for e in self.ctx.capabilities.all()
        ]
        return data

    def audit(self, actor: Actor, limit: int = 100, document_id: str | None = None) -> dict[str, Any]:
        self.ctx.policy.authorize(actor, "audit.read")
        with self.ctx.sessions() as session:
            statement = select(AuditEvent).order_by(AuditEvent.seq.desc()).limit(max(1, min(limit, 1000)))
            if document_id:
                statement = statement.where(AuditEvent.document_id == document_id)
            events = [
                {"seq": e.seq, "timestamp": e.timestamp, "actor": e.actor, "interface": e.interface,
                 "action": e.action, "outcome": e.outcome, "document_id": e.document_id, "details": e.details,
                 "hash": e.hash}
                for e in session.execute(statement).scalars()
            ]
            return {"verification": verify_chain(session).as_dict(), "events": events}

    def verify_audit(self, actor: Actor) -> dict[str, Any]:
        """Re-check the chain. A failure switches on safe mode; it never repairs or deletes anything."""
        self.ctx.policy.authorize(actor, "audit.verify")
        with self.ctx.sessions() as session:
            result = verify_chain(session)
        if not result.ok and not self.ctx.policy.safe_mode:
            self.ctx.policy.enter_safe_mode(
                f"The audit log failed verification at record {result.first_bad_seq}: {result.problem}")
        return {**result.as_dict(), "safe_mode": self.ctx.policy.safe_mode}

    def list_jobs(self, actor: Actor, active_only: bool = False, limit: int = 50) -> dict[str, Any]:
        self.ctx.policy.authorize(actor, "job.read")
        with self.ctx.sessions() as session:
            statement = select(Job).order_by(Job.created_at.desc()).limit(max(1, min(limit, 200)))
            if active_only:
                statement = statement.where(Job.status.in_(ACTIVE))
            jobs = []
            for job in session.execute(statement).scalars():
                data = job_to_dict(job)
                data["operation"] = (job.context or {}).get("operation")
                jobs.append(data)
            return {"jobs": jobs, "queue": self.ctx.jobs.stats()}

    def get_job(self, actor: Actor, job_id: str) -> dict[str, Any] | None:
        self.ctx.policy.authorize(actor, "job.read")
        with self.ctx.sessions() as session:
            job = session.get(Job, job_id)
            return job_to_dict(job) if job else None

    def cancel_job(self, actor: Actor, job_id: str) -> dict[str, Any] | None:
        self.ctx.policy.authorize(actor, "job.cancel")
        with self.ctx.sessions() as session:
            job = self.ctx.jobs.cancel(session, job_id)
            session.commit()
            return job_to_dict(job) if job else None
