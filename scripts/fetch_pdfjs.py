"""Restore the bundled PDF.js files.

ARD never loads PDF.js from a CDN. The viewer files live in
frontend/static/vendor/pdfjs and are part of the repository. Run this script
only to restore them or to move to a new, reviewed PDF.js release:

    uv run python scripts/fetch_pdfjs.py            # the pinned version
    uv run python scripts/fetch_pdfjs.py 6.5.438    # a specific version

It downloads the official release archive from the Mozilla PDF.js GitHub
releases and copies only the files ARD uses.
"""

from __future__ import annotations

import io
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

PINNED = "6.5.438"
TARGET = Path(__file__).resolve().parents[1] / "frontend" / "static" / "vendor" / "pdfjs"
KEEP_DIRS = ("web/cmaps/", "web/standard_fonts/", "web/wasm/", "web/iccs/")


def main(version: str) -> None:
    url = f"https://github.com/mozilla/pdf.js/releases/download/v{version}/pdfjs-{version}-dist.zip"
    print(f"Downloading {url}")
    with urllib.request.urlopen(url, timeout=120) as response:  # noqa: S310 (fixed https origin)
        archive = zipfile.ZipFile(io.BytesIO(response.read()))

    staging = TARGET.with_name("pdfjs.new")
    shutil.rmtree(staging, ignore_errors=True)
    for name in archive.namelist():
        if name.endswith("/") or ".." in Path(name).parts:
            continue
        # ARD never runs JavaScript embedded in PDFs, so the scripting sandbox is not shipped.
        if "sandbox" in name or "quickjs" in name.lower():
            continue
        if name == "LICENSE":
            destination = staging / "LICENSE"
        elif name.startswith("build/") and name.endswith(".mjs"):
            destination = staging / name
        elif name.startswith(KEEP_DIRS):
            destination = staging / name[len("web/"):]
        else:
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(archive.read(name))

    if not (staging / "build" / "pdf.mjs").is_file():
        raise SystemExit("The archive did not contain build/pdf.mjs; nothing was changed.")
    shutil.rmtree(TARGET, ignore_errors=True)
    staging.rename(TARGET)
    print(f"PDF.js {version} installed in {TARGET}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else PINNED)
