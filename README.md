# ARD

**Your documents. Your intelligence. Your control.**

ARD is an open-source, local-first PDF and document workspace. It runs on your
own computer, in your browser, with no account, no upload and no telemetry.

> **Status: 0.1.0, early development.** Phase 1 (foundation) and the core of
> Phase 2 (PDF viewing, page operations, overlays and annotations) and the
> Phase 3 (redaction, protection, forms, signatures, conversion) and most of
> Phase 4 (OCR, search, categories, duplicates) of the
> [implementation plan](docs/ARD_IMPLEMENTATION_PLAN.md) are built. OCR, the
> smart library, AI, signatures, redaction, MCP and the rest are **not built
> yet**. See [What works](#what-works-today) and [What does not](#not-built-yet).

## Quick start

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/). No Node.js, no
Docker, no GPU, no internet connection after installation. OCR additionally
needs Tesseract (`sudo apt install tesseract-ocr tesseract-ocr-eng
tesseract-ocr-hin`); everything else works without it.

```bash
uv sync
```

```bash
uv run ard serve --open
```

ARD starts at <http://127.0.0.1:8765>. Import a PDF from the Home or Library
page, or drop files onto the window.

Your documents are copied into ARD's data folder (Linux:
`~/.local/share/ard`); the files you import are never modified. Set
`ARD_DATA_DIR` to use a different folder. See [.env.example](.env.example).

## What works today

**Workspace**
- PDF viewer (bundled PDF.js, no CDN): page navigation, zoom, fit width/page,
  view rotation, text selection, in-document search with highlights,
  bookmarks, page thumbnails, multiple document tabs, keyboard navigation.
- Page operations: delete, reorder (drag thumbnails), duplicate, reverse,
  rotate, crop, insert blank pages, insert pages from another document.
- New documents from existing ones: merge, split by ranges or every N pages,
  extract selected pages.
- Annotate mode, saved as one new version when you choose:
  - *Drawn onto the page* (part of the page afterwards): text, PNG/JPEG
    images, stamps. This adds on top; it never edits or removes existing
    content.
  - *Annotations* (standard PDF objects, removable later): text highlight,
    notes, rectangle, ellipse, line, arrow, freehand drawing.
  - Undo/redo, move and delete before saving; "Remove annotations" afterwards.
  - Works on rotated pages. Comments on existing annotations can be read.
- Redaction with verification: marked pages are replaced by an image with the
  areas blacked out, so the content underneath is removed rather than covered.
  ARD re-opens the result and checks it before accepting it. Redacted pages
  lose their selectable text.
- Password protection (AES-256) with optional password-to-open and permission
  requests, and removal of protection with the owner password. Passwords are
  never stored.
- Watermarks, headers, footers and page numbers (Latin text).
- Visual signature: draw it and place it as a picture. This is not a
  cryptographic signature.
- Export to plain text, or to PNG/JPEG page images in a zip.
- Forms: list and fill standard PDF form fields (text, checkbox, radio,
  dropdown), and export or import the values as JSON or CSV. XFA forms are
  detected and refused.
- Cryptographic signatures: sign with your own .p12/.pfx certificate (used for
  that one signature, never stored), and check existing signatures offline.
  Results are reported as Valid, Invalid or Unknown; a self-signed or
  untrusted signer is Unknown, never Valid.
- Office documents (Word, Excel, PowerPoint, OpenDocument, RTF) are converted
  to PDF on import when LibreOffice is installed.
- PDF/A: shows what a file claims and what is certain to break conformance.
  Real validation needs veraPDF, which ARD uses if it is installed.
- Compression with three presets and honest before/after sizes; removal of
  document-level metadata.
- Password-protected PDFs can be viewed, and edited once unlocked.

**Safety**
- Every change creates a new version. The imported file is stored read-only
  and never overwritten; any version can be downloaded or made current again.
- Output is re-opened and its page count checked before it is accepted. A
  failed operation leaves nothing behind.
- "Delete" for a document means move to trash; files stay on disk.

**Library**
- Thumbnail grid, sorting, favourites, trash and restore, multi-select merge.
  Exact duplicates are detected at import by SHA-256 and not copied twice.
- Search inside documents: every hit names the page it was found on and opens
  there. Works for any script, including Hindi, and for parts of words.
- OCR for scanned pages (Tesseract through OCRmyPDF): adds a searchable text
  layer as a new version without changing the page images, records
  Tesseract's confidence per page and flags low-scoring pages for review.
  English and Hindi language packs are supported; others work if installed.
- Category suggestions from simple, explainable rules, shown with the words
  that decided them and a confidence. You can override any of them, and your
  choice is never replaced.
- Tags, smart albums (recently added, needs OCR, scanned, unclassified, large
  files, password-protected), a timeline by import month, and duplicate
  candidates (identical file, same text, very similar text) for review.
  Nothing is ever deleted or merged automatically.
- No embeddings, vector store or AI model is involved in any of this.

**Foundation**
- Central policy engine used by the UI, REST API and CLI.
- Hash-linked audit log with verification and a safe mode.
- ARD Guardian: CPU, memory and disk monitoring with Eco / Balanced /
  Performance profiles; throttles only ARD's own background work.
- Persistent job queue; PDF work runs in separate worker processes.
- Capability registry: shows which optional engines are installed and how to
  enable the rest. Missing engines never block startup.
- Light and dark themes (Obsidian Silver), keyboard accessible.

## Not built yet

Docling layout and table extraction, entity extraction and entity search ·
collections, correcting OCR text, cancelling a running OCR job · editing
existing PDF text · resizing placed items, annotation
import/export · visible signature appearances, trusted timestamps, revocation
checks · filling forms directly on the page · redaction that keeps the rest
of the page as text, sensitive-data detection · conversion from PDF to
DOCX/HTML/spreadsheets · converting a file to PDF/A · document intelligence, RAG and any AI feature · MCP server, Python SDK, plugins,
workflows · backup and restore · installers and packaging.

Known limitations of what *is* built are listed in [SECURITY.md](SECURITY.md).

## Command line

```bash
uv run ard --help
```

`serve`, `import`, `list`, `capabilities`, `verify-audit` and `diagnostics`.
Overlays and annotations are also available over REST:
`POST /api/documents/{id}/annotations` (see `/docs` while ARD is running).
The CLI uses the same services, policy engine and audit log as the web app.

## Development

```bash
uv run pytest
```

The suite covers the policy engine, Guardian state machine, audit chain,
operation planning, the REST API end to end, overlays and annotations (the
output is rendered and checked pixel by pixel), the job orchestrator, and the
security behaviours required by the specification (path traversal, safe mode,
scope enforcement, output validation, secrets in logs). There are no browser
(Playwright) tests yet.

```
app/
  main.py                 FastAPI application
  api/routes/             thin HTTP layer
  core/                   config, policy, jobs, Guardian, capabilities, security
  services/               document registry, PDF operations, audit, system
  workers/                PDF work that runs in worker processes
  integrations/cli/       Typer CLI
  database/migrations/    Alembic migrations (applied automatically at startup)
frontend/
  templates/              Jinja2 pages
  static/                 vanilla CSS and ES modules, bundled PDF.js
tests/                    unit, integration, security
docs/                     product and architecture specifications
```

To author a schema change: edit `app/models`, then

```bash
uv run alembic revision --autogenerate -m "describe the change"
```

## Design rules

The specifications in [docs/](docs) are binding. In short: local-first, no
mandatory cloud, GPU, Node.js, Docker or vector database; Jinja2 with vanilla
CSS and JavaScript; a modular monolith; every interface goes through the same
policy engine; originals are never overwritten; integrity claims state their
limits. Changes to the confirmed stack need a recorded decision
([decision log](docs/ARD_DECISION_LOG.md)).

## Licence

Proposed: Apache-2.0 (see [LICENSE](LICENSE)), pending the dependency and
licence audit described in the decision log. Bundled third-party components
are listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). The name
"ARD" has not been cleared for trademark, domain or repository use.
