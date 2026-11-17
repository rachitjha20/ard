"""Persistent job orchestrator.

Jobs are rows in SQLite, so the queue survives a restart. CPU-heavy work runs
in separate worker processes; a single dispatcher thread decides what may
start, guided by the Guardian.

A job kind has two halves:

* ``worker``   a picklable top-level function ``(payload) -> result`` that
               runs in a worker process and touches only the files it is given.
* ``finalize`` an optional function run in the main process afterwards, with
               a database session, to record the outcome (new versions, audit).

Guarantees:

* bounded concurrency (Guardian worker limit);
* interactive jobs go before background jobs;
* two jobs never run against the same document at once;
* jobs interrupted by a shutdown are re-queued, up to a retry limit;
* a cancelled job's output is discarded, never presented as complete.

Limitation: a job that is already running is not killed on cancel. It is
marked, allowed to finish, and its result thrown away.
"""

from __future__ import annotations

import multiprocessing
import queue
import threading
import traceback
from concurrent.futures import Executor, Future, ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from app.core.logging import get_logger, redact
from app.core.resource_guard.guardian import Guardian
from app.models import Job

log = get_logger("jobs")

PRIORITY_INTERACTIVE = 100
PRIORITY_BACKGROUND = 0

ACTIVE = ("queued", "running")
FINISHED = ("succeeded", "failed", "cancelled")

Worker = Callable[[dict[str, Any]], dict[str, Any]]
Finalizer = Callable[[Session, Job, dict[str, Any]], dict[str, Any] | None]
FailureHook = Callable[[Session, Job], None]


@dataclass(frozen=True)
class JobKind:
    name: str
    worker: Worker
    finalize: Finalizer | None = None
    on_failure: FailureHook | None = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def job_to_dict(job: Job) -> dict[str, Any]:
    return {
        "id": job.id,
        "kind": job.kind,
        "status": job.status,
        "priority": "interactive" if job.priority >= PRIORITY_INTERACTIVE else "background",
        "document_id": job.document_id,
        "progress": job.progress,
        "attempts": job.attempts,
        "cancel_requested": job.cancel_requested,
        "result": job.result,
        "error": job.error,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


class JobOrchestrator:
    def __init__(self, session_factory: sessionmaker[Session], guardian: Guardian | None,
                 mode: str = "process", max_workers: int = 2, max_attempts: int = 3) -> None:
        self._sessions = session_factory
        self._guardian = guardian
        self._mode = mode
        self._max_workers = max(1, max_workers)
        self._max_attempts = max_attempts
        self._kinds: dict[str, JobKind] = {}
        self._executor: Executor | None = None
        self._running: dict[str, str | None] = {}  # job id -> document id
        self._completions: queue.Queue[tuple[str, Future]] = queue.Queue()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._done: dict[str, threading.Event] = {}
        # Inputs that must never be written to disk (passwords). Held in memory until the job starts.
        self._transient: dict[str, dict[str, Any]] = {}
        self._done_lock = threading.Lock()

    # ---- registration and lifecycle --------------------------------------
    def register(self, kind: JobKind) -> None:
        self._kinds[kind.name] = kind

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        if self._mode == "process":
            # "spawn" gives workers a clean interpreter on every platform.
            self._executor = ProcessPoolExecutor(
                max_workers=self._max_workers, mp_context=multiprocessing.get_context("spawn")
            )
        else:
            self._executor = ThreadPoolExecutor(max_workers=self._max_workers, thread_name_prefix="ard-job")
        self._recover()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="ard-dispatcher", daemon=True)
        self._thread.start()
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=10)
        if self._executor:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None

    def _recover(self) -> None:
        """Jobs left 'running' by a previous process did not finish. Re-queue or fail them."""
        with self._sessions() as session:
            for job in session.execute(select(Job).where(Job.status == "running")).scalars():
                if job.attempts >= self._max_attempts:
                    job.status = "failed"
                    job.error = "Interrupted repeatedly; gave up after the retry limit."
                    job.finished_at = _utcnow()
                else:
                    job.status = "queued"
                    job.started_at = None
                log.info("recovered interrupted job", extra={"data": {"job": job.id, "status": job.status}})
            session.commit()

    # ---- public API -------------------------------------------------------
    def submit(self, session: Session, kind: str, payload: dict[str, Any], *, document_id: str | None = None,
               priority: int = PRIORITY_BACKGROUND, context: dict[str, Any] | None = None,
               transient: dict[str, Any] | None = None) -> Job:
        """Add a job inside the caller's transaction. Call ``notify()`` after committing.

        ``transient`` is merged into the payload when the job starts but is never
        stored in the database. If ARD restarts first, the job runs without it and
        the worker reports that it must be started again.
        """
        if kind not in self._kinds:
            raise ValueError(f"Unknown job kind: {kind}")
        job = Job(kind=kind, payload=payload, document_id=document_id, priority=priority, context=context or {})
        session.add(job)
        session.flush()
        with self._done_lock:
            self._done[job.id] = threading.Event()
            if transient:
                self._transient[job.id] = dict(transient)
        return job

    def call(self, worker: Worker, payload: dict[str, Any], timeout: float = 60.0) -> dict[str, Any]:
        """Run a short, read-only task in a worker and wait for it.

        Used for quick interactive work such as inspecting an upload or
        rendering a thumbnail, where a persistent job record adds nothing but
        the parsing should still happen outside the main process.
        """
        if self._executor is None:
            return worker(payload)
        return self._executor.submit(worker, payload).result(timeout=timeout)

    def notify(self) -> None:
        self._wake.set()

    def wait(self, job_id: str, timeout: float | None = None) -> bool:
        """Block until the job reaches a final state. Returns False on timeout."""
        with self._done_lock:
            event = self._done.get(job_id)
        if event is None:
            with self._sessions() as session:
                job = session.get(Job, job_id)
                return job is not None and job.status in FINISHED
        return event.wait(timeout)

    def cancel(self, session: Session, job_id: str) -> Job | None:
        job = session.get(Job, job_id)
        if job is None or job.status in FINISHED:
            return job
        if job.status == "queued":
            job.status = "cancelled"
            job.finished_at = _utcnow()
            self._fail_hook(session, job)
            self._signal_done(job.id)
        else:
            job.cancel_requested = True
        return job

    def stats(self) -> dict[str, Any]:
        return {
            "mode": self._mode,
            "running": len(self._running),
            "max_workers": self._max_workers,
            "worker_limit": self._limit(),
            "kinds": sorted(self._kinds),
        }

    # ---- dispatcher -------------------------------------------------------
    def _limit(self) -> int:
        limit = self._max_workers
        if self._guardian is not None:
            limit = min(limit, self._guardian.worker_limit())
        return max(1, limit)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(timeout=0.5)
            self._wake.clear()
            try:
                self._drain_completions()
                self._start_ready()
            except Exception:
                log.exception("dispatcher error")

    def _start_ready(self) -> None:
        limit = self._limit()
        if len(self._running) >= limit or self._executor is None:
            return
        background_ok = self._guardian.allows_background() if self._guardian else True
        with self._sessions() as session:
            queued = session.execute(
                select(Job).where(Job.status == "queued").order_by(Job.priority.desc(), Job.created_at)
            ).scalars().all()
            busy_documents = {d for d in self._running.values() if d}
            for job in queued:
                if len(self._running) >= limit:
                    break
                if job.priority < PRIORITY_INTERACTIVE and not background_ok:
                    continue  # paused by the Guardian; stays queued
                if job.document_id and job.document_id in busy_documents:
                    continue  # one job per document at a time
                kind = self._kinds.get(job.kind)
                if kind is None:
                    job.status = "failed"
                    job.error = f"No handler is registered for job kind '{job.kind}'."
                    job.finished_at = _utcnow()
                    session.commit()
                    self._signal_done(job.id)
                    continue
                job.status = "running"
                job.started_at = _utcnow()
                job.attempts += 1
                session.commit()
                self._running[job.id] = job.document_id
                if job.document_id:
                    busy_documents.add(job.document_id)
                with self._done_lock:
                    transient = self._transient.pop(job.id, None)
                future = self._executor.submit(kind.worker, {**job.payload, **(transient or {})})
                future.add_done_callback(lambda f, job_id=job.id: self._on_done(job_id, f))

    def _on_done(self, job_id: str, future: Future) -> None:
        self._completions.put((job_id, future))
        self._wake.set()

    def _drain_completions(self) -> None:
        while True:
            try:
                job_id, future = self._completions.get_nowait()
            except queue.Empty:
                return
            try:
                self._complete(job_id, future)
            finally:
                self._running.pop(job_id, None)
                self._signal_done(job_id)

    def _complete(self, job_id: str, future: Future) -> None:
        with self._sessions() as session:
            job = session.get(Job, job_id)
            if job is None:
                return
            kind = self._kinds.get(job.kind)
            try:
                result = future.result()
                if job.cancel_requested:
                    job.status = "cancelled"
                    self._fail_hook(session, job)
                else:
                    if kind and kind.finalize:
                        result = kind.finalize(session, job, result) or result
                    job.status = "succeeded"
                    job.result = result
                    job.progress = 1.0
            except Exception as exc:
                session.rollback()
                job = session.get(Job, job_id)
                job.status = "failed"
                job.error = redact(f"{exc.__class__.__name__}: {exc}")[:2000]
                log.error("job failed", extra={"data": {"job": job_id, "kind": job.kind,
                                                        "trace": traceback.format_exc(limit=6)}})
                self._fail_hook(session, job)
            job.finished_at = _utcnow()
            session.commit()

    def _fail_hook(self, session: Session, job: Job) -> None:
        kind = self._kinds.get(job.kind)
        if kind and kind.on_failure:
            try:
                kind.on_failure(session, job)
            except Exception:
                log.exception("failure hook error")

    def _signal_done(self, job_id: str) -> None:
        with self._done_lock:
            self._transient.pop(job_id, None)
            event = self._done.pop(job_id, None)
        if event:
            event.set()


def mark_progress(session_factory: sessionmaker[Session], job_id: str, progress: float) -> None:
    with session_factory() as session:
        session.execute(update(Job).where(Job.id == job_id).values(progress=max(0.0, min(1.0, progress))))
        session.commit()
