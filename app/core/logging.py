"""Structured logging with secret redaction.

Logs are JSON lines. Document content, extracted text and credentials must
never be passed to the logger; as a second line of defence, values that look
like tokens or keys are masked before they are written.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

_SECRET_PATTERNS = [
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)((?:api[_-]?key|token|password|secret)\s*[\"']?\s*[:=]\s*[\"']?)[^\s\"',;&]{4,}"),
    re.compile(r"\b(sk-[A-Za-z0-9]{6})[A-Za-z0-9_-]{10,}"),
]


def redact(text: str) -> str:
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda m: m.group(1) + "[REDACTED]", text)
    return text


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        extra = getattr(record, "data", None)
        if isinstance(extra, dict):
            payload["data"] = json.loads(redact(json.dumps(extra, default=str)))
        if record.exc_info:
            payload["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str = "INFO", log_dir: Path | None = None) -> None:
    root = logging.getLogger()
    root.setLevel(level.upper())
    for handler in list(root.handlers):
        if getattr(handler, "_ard", False):
            root.removeHandler(handler)

    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(
            RotatingFileHandler(log_dir / "ard.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8")
        )
    for handler in handlers:
        handler.setFormatter(JsonFormatter())
        handler._ard = True  # type: ignore[attr-defined]
        root.addHandler(handler)


    for noisy in ("alembic", "httpx", "httpcore", "multipart", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"ard.{name}")
