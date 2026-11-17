# Third-party notices

ARD's own code is proposed for release under Apache-2.0 (decision D-004,
subject to the Phase 0 compliance audit, which has **not** been completed).
This list covers what is bundled or required today. Transitive Python
dependencies are pinned in `requirements.lock` and still need individual
review (decision D-022).

## Bundled in this repository

| Component | Version | Licence | Location |
|---|---|---|---|
| PDF.js (Mozilla) | 6.5.438 | Apache-2.0 | `frontend/static/vendor/pdfjs/` (licence in `LICENSE` there) |
| PDF.js CMaps (Adobe) | with PDF.js | BSD-3-Clause | `frontend/static/vendor/pdfjs/cmaps/` |
| PDF.js standard fonts (Foxit, Liberation) | with PDF.js | BSD-3-Clause / SIL OFL 1.1 | `frontend/static/vendor/pdfjs/standard_fonts/` |
| PDF.js WASM decoders (OpenJPEG, QCMS, JBIG2; the QuickJS scripting sandbox is deliberately not shipped) | with PDF.js | see licence files in `frontend/static/vendor/pdfjs/wasm/` | `frontend/static/vendor/pdfjs/wasm/` |

Icons and the ARD mark were drawn for this project.

## Direct Python dependencies

| Package | Licence |
|---|---|
| FastAPI, Starlette, Uvicorn, Pydantic, pydantic-settings, Typer, platformdirs | MIT / BSD-3-Clause |
| Jinja2, python-multipart | BSD-3-Clause / Apache-2.0 |
| SQLAlchemy, Alembic | MIT |
| pypdf | BSD-3-Clause |
| pikepdf (bundles qpdf, Apache-2.0) | MPL-2.0 |
| pypdfium2 (bundles PDFium) | Apache-2.0 / BSD-3-Clause |
| Pillow | MIT-CMU |
| psutil | BSD-3-Clause |
| OCRmyPDF (with pdfminer.six, img2pdf, fpdf2, uharfbuzz, fonttools) | MPL-2.0 / MIT / LGPL-3.0 (img2pdf) / LGPL-3.0 (fpdf2) / Apache-2.0 |
| pyHanko, pyhanko-certvalidator (with cryptography, asn1crypto, oscrypto) | MIT / Apache-2.0 / BSD |

Licences in this table are as declared by each project and must be confirmed
against the exact pinned versions before any public release.
