"""Application configuration.

Everything is local by default: loopback binding, a per-user data directory
and no network features. Values can be overridden with ``ARD_*`` environment
variables or a ``.env`` file.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from platformdirs import user_data_dir
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

APP_NAME = "ARD"
APP_VERSION = "0.1.0"

REPO_ROOT = Path(__file__).resolve().parents[3]
FRONTEND_DIR = REPO_ROOT / "frontend"

# Sub-directories of the managed data directory (architecture doc, section 8).
DATA_AREAS = (
    "originals",
    "versions",
    "derivatives",
    "thumbnails",
    "extracted_text",
    "ocr",
    "indexes",
    "models",
    "backups",
    "temporary",
    "logs",
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ARD_", env_file=".env", extra="ignore")

    data_dir: Path = Field(default_factory=lambda: Path(user_data_dir("ard", appauthor=False)))

    # Loopback only. Binding elsewhere is refused unless explicitly allowed.
    host: str = "127.0.0.1"
    port: int = 8765
    allow_non_loopback: bool = False

    # Limits applied to untrusted documents.
    max_upload_mb: int = 300
    max_pages: int = 10_000

    # Job processing.
    worker_mode: Literal["process", "thread"] = "process"
    max_workers: int = 2
    interactive_wait_seconds: float = 120.0
    job_max_attempts: int = 3

    # ARD Guardian.
    guardian_enabled: bool = True
    guardian_profile: Literal["eco", "balanced", "performance", "custom"] = "balanced"
    guardian_interval_seconds: float = 2.0

    log_level: str = "INFO"

    @property
    def database_path(self) -> Path:
        return self.data_dir / "ard.sqlite3"

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.database_path}"

    @property
    def token_path(self) -> Path:
        return self.data_dir / "session.token"

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    def area(self, name: str) -> Path:
        if name not in DATA_AREAS:
            raise ValueError(f"Unknown data area: {name}")
        return self.data_dir / name


@lru_cache
def get_settings() -> Settings:
    return Settings()
