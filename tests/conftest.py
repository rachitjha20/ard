from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config.settings import Settings
from app.core.context import build_context
from app.main import create_app
from tests.helpers import make_pdf


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(data_dir=tmp_path / "data", worker_mode="thread", guardian_enabled=False,
                    interactive_wait_seconds=30, log_level="WARNING", _env_file=None)


@pytest.fixture
def app(settings):
    return create_app(settings)


@pytest.fixture
def client(app, settings):
    """An authenticated REST client (bearer token, like a script would use)."""
    with TestClient(app, base_url="http://127.0.0.1:8765") as test_client:
        token = settings.token_path.read_text().strip()
        test_client.headers["Authorization"] = f"Bearer {token}"
        yield test_client


@pytest.fixture
def context(settings):
    ctx = build_context(settings)
    ctx.start()
    yield ctx
    ctx.stop()


@pytest.fixture
def upload(client):
    def _upload(data: bytes | None = None, name: str = "sample.pdf", **kwargs) -> dict:
        data = data if data is not None else make_pdf(**kwargs)
        response = client.post("/api/documents", files=[("files", (name, data, "application/pdf"))])
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["imported"], body
        return body["imported"][0]["document"]

    return _upload
