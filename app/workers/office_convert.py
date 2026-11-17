"""Office document to PDF through LibreOffice (runs in a worker process).

LibreOffice is an optional external engine. ARD starts it headless with a
throw-away profile, so the user's own LibreOffice settings and macros are not
involved, waits for it with a time limit, and takes the PDF it writes.

Limits: layout is whatever LibreOffice produces and can differ from the
original application. A document that refers to remote content (linked images,
templates) may cause LibreOffice to request it; ARD cannot prevent that.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

# Formats accepted for conversion. HTML is deliberately excluded: it routinely pulls in remote content.
# Plain .txt and .csv are excluded too, so that dropping a stray text file does not quietly create a PDF.
OFFICE_EXTENSIONS = {
    ".doc", ".docx", ".odt", ".rtf",
    ".xls", ".xlsx", ".ods",
    ".ppt", ".pptx", ".odp",
}
TIMEOUT_SECONDS = 180


class ConvertError(Exception):
    pass


def _hidden(path: Path) -> bool:
    return any(part.startswith(".") and part not in (".", "..") for part in path.parts)


def convert(payload: dict[str, Any]) -> dict[str, Any]:
    """Convert ``source`` (already inside ``workdir``) to PDF. Returns the PDF path."""
    executable, source, workdir = payload["executable"], Path(payload["source"]), Path(payload["workdir"])
    profile = workdir / "profile"
    command = [
        executable, f"-env:UserInstallation=file://{profile}", "--headless", "--norestore", "--nolockcheck",
        "--nodefault", "--convert-to", "pdf", "--outdir", str(workdir), str(source),
    ]
    environment = {key: value for key, value in os.environ.items() if key in (
        "PATH", "HOME", "LANG", "LC_ALL", "USER", "XDG_RUNTIME_DIR", "SNAP", "SNAP_USER_DATA", "TMPDIR")}
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=TIMEOUT_SECONDS, cwd=str(workdir),
                                env=environment, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as exc:
        raise ConvertError(f"LibreOffice did not finish within {TIMEOUT_SECONDS} seconds and was stopped.") from exc
    except OSError as exc:
        raise ConvertError("LibreOffice could not be started.") from exc

    output = workdir / (source.stem + ".pdf")
    if not output.is_file() or output.stat().st_size == 0:
        resolved = shutil.which(executable) or executable
        if "/snap/" in os.path.realpath(resolved) + resolved and _hidden(workdir):
            raise ConvertError(
                "LibreOffice is installed as a snap, which is not allowed to read hidden folders, and ARD's data "
                f"folder is one ({workdir.parents[1]}). Set ARD_DATA_DIR to a folder that is not hidden, or install "
                "LibreOffice another way.")
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        raise ConvertError("LibreOffice could not convert this file" + (f": {detail[-1][:160]}" if detail else "."))
    return {"path": str(output)}
