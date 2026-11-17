"""Capability registry.

Every engine ARD can use declares what it provides, how it is licensed, what
it needs and what happens without it. Detection never raises and never
downloads anything: a missing optional engine is reported, and the
application starts regardless.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from app.core.config.settings import FRONTEND_DIR


@dataclass
class Integration:
    id: str
    name: str
    purpose: str
    capabilities: list[str]
    license: str
    required: bool
    phase: int
    requires_internet: bool = False
    needs_download: str | None = None
    data_scope: str = "Local files handed to it by ARD."
    fallback: str = ""
    enable_hint: str = ""
    detector: Callable[[], tuple[bool, str | None, str | None]] = field(default=lambda: (False, None, None), repr=False)

    def detect(self) -> dict[str, Any]:
        try:
            available, version, note = self.detector()
        except Exception as exc:  # detection must not break startup
            available, version, note = False, None, f"Detection failed: {exc.__class__.__name__}"
        data = asdict(self)
        data.pop("detector")
        data.update(available=available, version=version, note=note,
                    state="available" if available else "unavailable")
        return data


def _package(dist: str, module: str | None = None) -> Callable[[], tuple[bool, str | None, str | None]]:
    def check() -> tuple[bool, str | None, str | None]:
        if importlib.util.find_spec(module or dist) is None:
            return False, None, None
        try:
            return True, importlib.metadata.version(dist), None
        except importlib.metadata.PackageNotFoundError:
            return True, None, None

    return check


def _binary(*names: str, version_args: tuple[str, ...] | None = ("--version",)) -> Callable[[], tuple[bool, str | None, str | None]]:
    def check() -> tuple[bool, str | None, str | None]:
        for name in names:
            path = shutil.which(name)
            if not path:
                continue
            version = None
            if version_args is None:  # some launchers take seconds just to print a version
                return True, None, path
            try:
                out = subprocess.run([path, *version_args], capture_output=True, text=True, timeout=3)
                lines = (out.stdout or out.stderr).strip().splitlines()
                version = lines[0][:80] if lines else None
            except (OSError, subprocess.SubprocessError):
                pass
            return True, version, path
        return False, None, None

    return check


def _pdfjs() -> tuple[bool, str | None, str | None]:
    build = FRONTEND_DIR / "static" / "vendor" / "pdfjs" / "build"
    if not (build / "pdf.mjs").is_file() or not (build / "pdf.worker.mjs").is_file():
        return False, None, "Bundled viewer files are missing."
    version = None
    with (build / "pdf.mjs").open(encoding="utf-8", errors="ignore") as handle:
        head = handle.read(4000)
    marker = "pdfjsVersion = "
    if marker in head:
        version = head.split(marker, 1)[1].split("\n", 1)[0].strip(" ;'\"")
    return True, version, "Bundled locally; no CDN."


def _tesseract_languages() -> list[str]:
    path = shutil.which("tesseract")
    if not path:
        return []
    try:
        out = subprocess.run([path, "--list-langs"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return []
    return [line.strip() for line in out.stdout.splitlines()[1:] if line.strip()]


INTEGRATIONS: list[Integration] = [
    Integration("pdfjs", "PDF.js", "Renders PDFs in the browser.", ["pdf.render"], "Apache-2.0", True, 2,
                fallback="The viewer is reported as unavailable. ARD never substitutes a remote viewer.",
                enable_hint="Run scripts/fetch_pdfjs.py to restore the bundled files.", detector=_pdfjs),
    Integration("pypdf", "pypdf", "Reads PDF text, outline and metadata.", ["pdf.read", "pdf.text"], "BSD-3-Clause", True, 2,
                detector=_package("pypdf")),
    Integration("pikepdf", "pikepdf (qpdf)", "Page operations, optimisation and structural inspection.",
                ["pdf.merge", "pdf.split", "pdf.pages", "pdf.compress", "pdf.inspect"], "MPL-2.0", True, 2,
                detector=_package("pikepdf")),
    Integration("pypdfium2", "pypdfium2 (PDFium)", "Rasterises pages for thumbnails.", ["pdf.rasterize"],
                "Apache-2.0 / BSD-3-Clause", False, 2,
                fallback="Thumbnails are unavailable; the viewer and page operations still work.",
                enable_hint="uv sync", detector=_package("pypdfium2")),
    Integration("tesseract", "Tesseract", "CPU OCR for scanned pages.", ["ocr.tesseract"], "Apache-2.0", False, 4,
                needs_download="Language packs (English and Hindi first).",
                fallback="Basic PDF operations still work; OCR is unavailable.",
                enable_hint="sudo apt install tesseract-ocr tesseract-ocr-eng tesseract-ocr-hin",
                detector=_binary("tesseract")),
    Integration("ocrmypdf", "OCRmyPDF", "Adds a searchable text layer to scanned PDFs.", ["ocr.searchable_pdf"],
                "MPL-2.0", False, 4, fallback="Searchable-PDF output is unavailable.",
                enable_hint="uv sync (requires Tesseract)", detector=_package("ocrmypdf")),
    Integration("docling", "Docling", "Structured text, tables and layout extraction.", ["document.understand"], "MIT",
                False, 4, needs_download="Layout models, downloaded only when you ask.",
                fallback="Basic text extraction remains available.",
                enable_hint="Installed with the Smart Library phase.", detector=_package("docling")),
    Integration("ollama", "Ollama", "Runs local language models.", ["ai.local"], "MIT (runtime); models vary", False, 5,
                needs_download="Models, chosen and downloaded by you. Each has its own licence.",
                fallback="Everything that does not need AI keeps working.",
                enable_hint="Install Ollama and pull a model. ARD never downloads models itself.",
                detector=_binary("ollama")),
    Integration("chromadb", "ChromaDB", "Optional local vector store for semantic search.", ["rag.vector"], "Apache-2.0",
                False, 5, fallback="Keyword search and the library continue without it.",
                enable_hint="Installed with the Intelligence phase.", detector=_package("chromadb")),
    Integration("pyhanko", "pyHanko", "Creates and validates cryptographic PDF signatures.",
                ["signature.sign", "signature.validate"], "MIT", False, 3,
                fallback="Signature validation is unavailable; signature status is shown as unknown.",
                enable_hint="uv sync", detector=_package("pyHanko", "pyhanko")),
    Integration("ghostscript", "Ghostscript", "Some PDF conversions and PDF/A work.", ["pdf.ghostscript"], "AGPL-3.0",
                False, 3, fallback="Those conversions are unavailable.", enable_hint="sudo apt install ghostscript",
                detector=_binary("gs")),
    Integration("libreoffice", "LibreOffice", "Converts Office files to PDF.", ["convert.office"], "MPL-2.0", False, 3,
                fallback="Office conversion is unavailable, with an explanation.",
                enable_hint="Install LibreOffice.", detector=_binary("libreoffice", "soffice", version_args=None)),
    Integration("verapdf", "veraPDF", "PDF/A validation.", ["pdfa.validate"], "GPL-3.0+ / MPL-2.0", False, 3,
                fallback="PDF/A validation is unavailable.", enable_hint="Install veraPDF.", detector=_binary("verapdf", version_args=None)),
    Integration("clamav", "ClamAV", "Optional malware scanning.", ["malware.scan"], "GPL-2.0", False, 6,
                needs_download="Virus definitions.",
                fallback="Scanning is reported as unavailable. A file is never called clean because no scan ran.",
                enable_hint="sudo apt install clamav", detector=_binary("clamscan")),
]


class CapabilityRegistry:
    def __init__(self) -> None:
        self._cache: list[dict[str, Any]] | None = None

    def refresh(self) -> list[dict[str, Any]]:
        self._cache = [integration.detect() for integration in INTEGRATIONS]
        for entry in self._cache:
            if entry["id"] == "tesseract" and entry["available"]:
                entry["languages"] = _tesseract_languages()
        return self._cache

    def all(self) -> list[dict[str, Any]]:
        return self._cache if self._cache is not None else self.refresh()

    def has(self, capability: str) -> bool:
        return any(capability in e["capabilities"] and e["available"] for e in self.all())

    def available_capabilities(self) -> list[str]:
        return sorted({c for e in self.all() if e["available"] for c in e["capabilities"]})
