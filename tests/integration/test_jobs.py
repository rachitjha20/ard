"""Job orchestrator behaviour: isolation, recovery, cancellation, document locking."""

import threading
import time

from fastapi.testclient import TestClient

from app.core.config.settings import Settings
from app.core.jobs.orchestrator import JobKind, JobOrchestrator
from app.core.context import build_context
from app.main import create_app
from app.models import Job
from tests.helpers import make_pdf

_gate = threading.Event()
_running = []


def slow_worker(payload):
    _running.append(payload["name"])
    _gate.wait(5)
    return {"name": payload["name"]}


def failing_worker(payload):
    raise RuntimeError("password=supersecret123 boom")


def test_operations_run_in_separate_processes(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", worker_mode="process", guardian_enabled=False, _env_file=None)
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8765") as client:
        client.headers["Authorization"] = "Bearer " + settings.token_path.read_text()
        doc = client.post("/api/documents", files=[("files", ("a.pdf", make_pdf(3), "application/pdf"))]).json()
        document_id = doc["imported"][0]["document"]["id"]
        result = client.post(f"/api/documents/{document_id}/operations", json={"operation": "reverse", "params": {}}).json()
        assert result["done"] and result["result"]["page_count"] == 3
        assert client.get("/api/jobs").json()["queue"]["mode"] == "process"


def test_one_job_per_document_and_bounded_workers(context):
    _gate.clear(); _running.clear()
    context.jobs.register(JobKind("test.slow", slow_worker))
    with context.sessions() as session:
        first = context.jobs.submit(session, "test.slow", {"name": "first"}, document_id="d" * 32).id
        second = context.jobs.submit(session, "test.slow", {"name": "second"}, document_id="d" * 32).id
        session.commit()
    context.jobs.notify()
    time.sleep(0.8)
    assert _running == ["first"]          # the second waits for the same document
    _gate.set()
    assert context.jobs.wait(first, 5) and context.jobs.wait(second, 5)
    assert _running == ["first", "second"]


def test_failures_are_recorded_with_secrets_redacted(context):
    context.jobs.register(JobKind("test.fail", failing_worker))
    with context.sessions() as session:
        job_id = context.jobs.submit(session, "test.fail", {}).id
        session.commit()
    context.jobs.notify()
    assert context.jobs.wait(job_id, 5)
    with context.sessions() as session:
        job = session.get(Job, job_id)
        assert job.status == "failed" and "boom" in job.error and "supersecret123" not in job.error


def test_cancel_queued_and_running_jobs(context):
    _gate.clear(); _running.clear()
    context.jobs.register(JobKind("test.slow", slow_worker))
    with context.sessions() as session:
        running = context.jobs.submit(session, "test.slow", {"name": "r"}, document_id="e" * 32).id
        queued = context.jobs.submit(session, "test.slow", {"name": "q"}, document_id="e" * 32).id
        session.commit()
    context.jobs.notify()
    time.sleep(0.6)
    with context.sessions() as session:
        assert context.jobs.cancel(session, queued).status == "cancelled"
        assert context.jobs.cancel(session, running).cancel_requested is True
        session.commit()
    _gate.set()
    assert context.jobs.wait(running, 5)
    with context.sessions() as session:
        assert session.get(Job, running).status == "cancelled"   # result discarded
        assert session.get(Job, running).result is None
    assert _running == ["r"]


def test_interrupted_jobs_are_requeued_then_given_up(settings):
    context = build_context(settings)
    with context.sessions() as session:
        session.add(Job(id="a" * 32, kind="test.slow", status="running", attempts=1, payload={"name": "x"}))
        session.add(Job(id="b" * 32, kind="test.slow", status="running", attempts=3, payload={"name": "y"}))
        session.commit()
    context.engine.dispose()

    restarted = build_context(settings)       # a fresh process would do exactly this
    orchestrator = JobOrchestrator(restarted.sessions, None, "thread", 1, max_attempts=3)
    orchestrator._recover()
    with restarted.sessions() as session:
        assert session.get(Job, "a" * 32).status == "queued"
        assert session.get(Job, "b" * 32).status == "failed"
    restarted.engine.dispose()


def test_guardian_pause_holds_background_but_not_interactive(settings, tmp_path):
    from app.core.resource_guard.guardian import Guardian, Sample
    from app.core.jobs.orchestrator import PRIORITY_INTERACTIVE

    _gate.set(); _running.clear()
    context = build_context(settings)     # not started: this test drives its own orchestrator
    guardian = Guardian(tmp_path)
    for _ in range(guardian.profile.sustain_samples):
        guardian.observe(Sample("t", 5, 99, 10, 50_000, 10, 1))
    assert guardian.state == "paused"
    orchestrator = JobOrchestrator(context.sessions, guardian, "thread", 2)
    orchestrator.register(JobKind("test.slow", slow_worker))
    orchestrator.start()
    try:
        with context.sessions() as session:
            background = orchestrator.submit(session, "test.slow", {"name": "bg"}).id
            interactive = orchestrator.submit(session, "test.slow", {"name": "ui"}, priority=PRIORITY_INTERACTIVE).id
            session.commit()
        orchestrator.notify()
        assert orchestrator.wait(interactive, 5)
        assert not orchestrator.wait(background, 0.8)      # still queued, not lost
        with context.sessions() as session:
            assert session.get(Job, background).status == "queued"
    finally:
        orchestrator.stop()
        context.engine.dispose()
