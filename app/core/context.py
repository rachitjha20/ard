"""Application context: the one place where the pieces are wired together.

The web app, the CLI and (later) MCP all build the same context and call the
same services through it, so no interface can carry its own business logic or
skip the policy engine.
"""

from __future__ import annotations

import platform
import sys
import threading
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.capabilities import CapabilityRegistry
from app.core.config.settings import APP_VERSION, Settings, get_settings
from app.core.jobs.orchestrator import JobOrchestrator
from app.core.logging import configure_logging, get_logger
from app.core.policy.engine import PolicyEngine
from app.core.resource_guard.guardian import Guardian
from app.database.session import make_engine, make_session_factory, run_migrations
from app.models import AppSetting
from app.services.documents.storage import Storage
from app.services.integrity.audit import verify_chain

log = get_logger("context")


@dataclass
class AppContext:
    settings: Settings
    engine: Engine
    sessions: sessionmaker[Session]
    storage: Storage
    policy: PolicyEngine
    guardian: Guardian
    capabilities: CapabilityRegistry
    jobs: JobOrchestrator
    documents: Any = None  # DocumentService, attached in build_context
    library: Any = None    # LibraryService, attached in build_context
    # Called with (session, document_id, version_id, storage_key) whenever a version becomes current.
    version_ready_hooks: list[Any] = field(default_factory=list)

    def start(self) -> None:
        if self.settings.guardian_enabled:
            self.guardian.start()
        self.jobs.start()
        # Engine detection runs external commands; do it off the request path.
        threading.Thread(target=self.capabilities.refresh, name="ard-capabilities", daemon=True).start()
        if self.library is not None:
            try:
                self.library.queue_missing()      # index anything imported before search existed
            except Exception:
                log.exception("could not queue indexing")

    def stop(self) -> None:
        self.jobs.stop()
        self.guardian.stop()
        self.engine.dispose()

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self.sessions() as session:
            row = session.get(AppSetting, key)
            return row.value if row else default

    def set_setting(self, session: Session, key: str, value: Any) -> None:
        row = session.get(AppSetting, key)
        if row:
            row.value = value
        else:
            session.add(AppSetting(key=key, value=value))

    def diagnostics(self) -> dict[str, Any]:
        with self.sessions() as session:
            integrity = session.execute(text("PRAGMA quick_check")).scalar()
            audit = verify_chain(session)
        return {
            "ard_version": APP_VERSION,
            "python": sys.version.split()[0],
            "os": f"{platform.system()} {platform.release()}",
            "machine": platform.machine(),
            "data_dir": str(self.settings.data_dir),
            "bind": f"{self.settings.host}:{self.settings.port}",
            "database": {"path": str(self.settings.database_path), "quick_check": integrity},
            "storage_bytes": self.storage.usage(),
            "audit": audit.as_dict(),
            "safe_mode": {"active": self.policy.safe_mode, "reason": self.policy.safe_mode_reason},
            "jobs": self.jobs.stats(),
            "guardian_state": self.guardian.state,
            "capabilities": self.capabilities.available_capabilities(),
            "telemetry": "none",
        }


def build_context(settings: Settings | None = None) -> AppContext:
    from app.services.documents.service import DocumentService

    settings = settings or get_settings()
    storage = Storage(settings)
    storage.ensure()
    configure_logging(settings.log_level, settings.area("logs"))

    run_migrations(settings.database_url)
    engine = make_engine(settings.database_url)
    sessions = make_session_factory(engine)

    policy = PolicyEngine()
    with sessions() as session:
        # The audit lock row gives writers something to lock before reading the chain head.
        if session.get(AppSetting, "__audit_lock__") is None:
            session.add(AppSetting(key="__audit_lock__", value=0))
            session.commit()
        chain = verify_chain(session)
        profile_row = session.get(AppSetting, "guardian.profile")
        custom_row = session.get(AppSetting, "guardian.custom")
    if not chain.ok:
        # Safe behaviour only: stop sensitive edits, keep everything, repair nothing.
        policy.enter_safe_mode(f"The audit log failed verification at record {chain.first_bad_seq}: {chain.problem}")
        log.error("audit verification failed; entering safe mode", extra={"data": chain.as_dict()})

    profile = profile_row.value if profile_row else settings.guardian_profile
    try:
        guardian = Guardian(settings.data_dir, profile, settings.guardian_interval_seconds,
                            custom_row.value if custom_row else None)
    except ValueError:
        guardian = Guardian(settings.data_dir, "balanced", settings.guardian_interval_seconds)

    jobs = JobOrchestrator(sessions, guardian if settings.guardian_enabled else None, settings.worker_mode,
                           settings.max_workers, settings.job_max_attempts)
    context = AppContext(settings, engine, sessions, storage, policy, guardian, CapabilityRegistry(), jobs)
    context.documents = DocumentService(context)
    from app.services.library.service import LibraryService

    context.library = LibraryService(context)
    # Only stale leftovers: another ARD process (the CLI) may be mid-upload.
    storage.cleanup_temporary(older_than_seconds=3600)
    return context
