"""Hash-linked audit log.

Each record stores the hash of the previous record, so removing or editing a
record breaks every hash after it.

Limitation, stated plainly: this detects accidental damage and casual
tampering. Someone who controls the whole machine can rewrite the database and
recompute the chain. It is evidence, not proof.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models import AuditEvent

GENESIS_HASH = "0" * 64
_append_lock = threading.Lock()


def _digest(prev_hash: str, fields: dict[str, Any]) -> str:
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(f"{prev_hash}\n{canonical}".encode("utf-8")).hexdigest()


def _fields(event: AuditEvent) -> dict[str, Any]:
    return {
        "timestamp": event.timestamp,
        "actor": event.actor,
        "interface": event.interface,
        "action": event.action,
        "outcome": event.outcome,
        "document_id": event.document_id,
        "details": event.details or {},
    }


def append_event(
    session: Session,
    *,
    actor: str,
    interface: str,
    action: str,
    outcome: str = "success",
    document_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> AuditEvent:
    """Append one record. The caller's transaction commits it."""
    with _append_lock:
        # Take SQLite's write lock before reading the chain head, so two
        # writers (threads or processes) can never link to the same record.
        session.execute(text("UPDATE app_settings SET value = value WHERE key = '__audit_lock__'"))
        last = session.execute(select(AuditEvent).order_by(AuditEvent.seq.desc()).limit(1)).scalar_one_or_none()
        prev_hash = last.hash if last else GENESIS_HASH
        event = AuditEvent(
            timestamp=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            actor=actor,
            interface=interface,
            action=action,
            outcome=outcome,
            document_id=document_id,
            details=details or {},
            prev_hash=prev_hash,
            hash="",
        )
        event.hash = _digest(prev_hash, _fields(event))
        session.add(event)
        session.flush()
        return event


@dataclass(frozen=True)
class ChainVerification:
    ok: bool
    records: int
    first_bad_seq: int | None = None
    problem: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "records": self.records,
            "first_bad_seq": self.first_bad_seq,
            "problem": self.problem,
            "status": "Audit Chain Verified" if self.ok else "Audit Verification Failed",
        }


def verify_chain(session: Session) -> ChainVerification:
    prev_hash = GENESIS_HASH
    prev_seq = 0
    count = 0
    for event in session.execute(select(AuditEvent).order_by(AuditEvent.seq)).scalars():
        count += 1
        if event.seq != prev_seq + 1:
            return ChainVerification(False, count, event.seq, f"Record {prev_seq + 1} is missing.")
        if event.prev_hash != prev_hash:
            return ChainVerification(False, count, event.seq, "Link to the previous record does not match.")
        if event.hash != _digest(prev_hash, _fields(event)):
            return ChainVerification(False, count, event.seq, "Record contents do not match its hash.")
        prev_hash = event.hash
        prev_seq = event.seq
    return ChainVerification(True, count)
