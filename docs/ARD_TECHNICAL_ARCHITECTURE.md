# ARD Technical Architecture

## 1. Confirmed technology baseline

  Layer                    Technology
  ------------------------ ---------------------------------------------------
  Runtime                  Python 3.12+
  Backend                  FastAPI
  Templates                Jinja2
  Frontend                 HTML5, vanilla CSS, vanilla JavaScript ES modules
  Database                 SQLite
  ORM                      SQLAlchemy 2.x
  Migrations               Alembic
  PDF rendering            PDF.js
  PDF manipulation         pypdf and pikepdf
  OCR                      Tesseract and OCRmyPDF
  Document understanding   Docling
  Local AI                 Ollama
  Vector database          Optional ChromaDB
  Digital signatures       pyHanko
  Cryptography             cryptography
  MCP                      Official Python MCP SDK
  CLI                      Typer
  Monitoring               psutil
  CPU-bound workers        Python multiprocessing
  Testing                  pytest, HTTPX; Playwright for UI acceptance tests

This baseline is confirmed. Do not substitute frameworks without an
explicit architecture decision.

## 2. Architectural style

Use a modular monolith with isolated worker processes.

Do not introduce microservices, Kubernetes, Redis, Celery, RabbitMQ or
Docker as mandatory infrastructure. These are unnecessary for the
default single-user local application.

The application should have: - One local FastAPI process. - A central
domain/service layer. - A persistent SQLite database. - A managed local
data directory. - Separate processes for CPU-heavy workloads. - Optional
external engine processes such as Ollama. - Optional adapters for vector
stores and external services.

## 3. High-level flow

`Browser UI → FastAPI routes → application services → domain services → repositories/adapters → SQLite and managed files`

MCP, CLI, REST and workflows must call the same application service
layer. They must not implement separate business logic or bypass policy
enforcement.

## 4. Layer responsibilities

### Presentation layer

-   Jinja2 templates.
-   Vanilla CSS and JavaScript.
-   PDF.js viewer.
-   Accessible controls and keyboard navigation.
-   Local static assets only.
-   No React, Vue, Angular, Svelte, Tailwind, Bootstrap or Node/npm
    build requirement.

### API layer

-   FastAPI routers.
-   Pydantic request/response schemas.
-   Input validation.
-   Authentication/session controls appropriate to local deployment.
-   Rate and size limits for sensitive endpoints.
-   OpenAPI contract.

### Application services

-   Document import and registry.
-   Document operations.
-   Library and categorization.
-   OCR orchestration.
-   Conversion orchestration.
-   Search and retrieval.
-   AI provider orchestration.
-   Trust and provenance.
-   Job orchestration.
-   Resource policy.
-   Integration and plugin management.

### Domain layer

Owns rules, entities, state transitions and invariants. Avoid binding
domain rules directly to a specific OCR, AI or PDF engine.

### Infrastructure layer

-   SQLite repositories.
-   Filesystem storage.
-   Engine adapters.
-   OS keyring.
-   Process management.
-   Logging.
-   Backup and recovery.

## 5. Proposed repository layout

``` text
ard/
├── app/
│   ├── main.py
│   ├── api/
│   │   ├── routes/
│   │   └── dependencies/
│   ├── core/
│   │   ├── config/
│   │   ├── security/
│   │   ├── policy/
│   │   ├── jobs/
│   │   ├── resource_guard/
│   │   └── events/
│   ├── database/
│   ├── models/
│   ├── schemas/
│   ├── repositories/
│   ├── services/
│   │   ├── documents/
│   │   ├── pdf/
│   │   ├── ocr/
│   │   ├── conversion/
│   │   ├── compression/
│   │   ├── forms/
│   │   ├── signatures/
│   │   ├── security/
│   │   ├── integrity/
│   │   ├── library/
│   │   ├── search/
│   │   ├── extraction/
│   │   ├── comparison/
│   │   ├── ai/
│   │   ├── rag/
│   │   ├── automation/
│   │   └── hardware/
│   ├── integrations/
│   │   ├── mcp/
│   │   ├── cli/
│   │   ├── ai/
│   │   ├── vector_stores/
│   │   ├── storage/
│   │   └── providers/
│   ├── workers/
│   └── utils/
├── frontend/
│   ├── templates/
│   └── static/
│       ├── css/
│       ├── js/
│       ├── icons/
│       └── vendor/
├── extensions/
├── application_data/
├── scripts/
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── security/
│   ├── performance/
│   └── e2e/
├── docs/
├── examples/
├── packaging/
├── pyproject.toml
├── requirements.lock
├── .env.example
├── README.md
├── LICENSE
└── SECURITY.md
```

`application_data/` is a development placeholder. Production data paths
must be selected using platform-appropriate user data directories. Do
not store user documents inside the source repository by default.

## 6. Module boundaries

### Document Registry

Owns document identity, paths, metadata, lifecycle and version
references.

### PDF Engine Adapter

Provides a stable ARD interface for PDF reading, manipulation, rendering
coordination, metadata and exports. pypdf and pikepdf remain replaceable
implementations.

### OCR Engine Adapter

Detects scanned pages, prepares images, runs Tesseract/OCRmyPDF or
optional engines, captures confidence and produces searchable output.

### Document Understanding Adapter

Uses Docling to extract structured text, tables, layout and reading
order. Preserve page and source provenance.

### Library Service

Owns categories, tags, collections, smart albums, timeline and
duplicate/similarity relationships.

### Intelligence Service

Owns summaries, structured extraction, comparisons, translation and AI
tasks. Providers are adapters.

### RAG Service

Owns ingestion, chunking, embedding, indexing, retrieval, citations and
index lifecycle.

### Trust Service

Owns hashes, baselines, provenance, signatures, audit events and
integrity reports.

### Job Orchestrator

Owns persistent jobs, queue state, retries, checkpoints, cancellation
and recovery.

### Resource Guardian

Owns resource measurements, workload budgets, scheduler decisions and
safe throttling.

### Integration Hub

Owns engine detection, version checks, capability state, permissions,
health checks and optional integration lifecycle.

## 7. Capability registry

Every optional engine should declare: - Integration identifier and
version. - Detected executable/package version. - License identifier and
notices location. - Supported platforms. - Required and optional
capabilities. - Resource estimate or profile. - Availability state. -
Health-check result. - Data access scope. - Whether internet access is
required. - Whether model or language data must be downloaded. -
Fallback behavior.

Capabilities should be explicit, for example: `pdf.render`, `pdf.merge`,
`ocr.tesseract`, `ocr.searchable_pdf`, `document.understand`,
`ai.local`, `rag.vector`, `signature.validate`, `pdfa.validate`.

The UI should show unavailable capabilities and explain how to enable
them. Missing optional engines must not prevent application startup.

## 8. Storage strategy

Use SQLite as the authoritative metadata, job, audit and configuration
store.

Store original files and derived artifacts in a managed filesystem
directory. Keep database records pointing to stable internal file
identifiers rather than relying on display names.

Suggested data areas: - originals/ - versions/ - derivatives/ -
thumbnails/ - extracted_text/ - ocr/ - indexes/ - models/ - backups/ -
temporary/

Use atomic writes where possible. Temporary files must be cleaned
safely. Original files should be immutable by default.

## 9. Concurrency and job processing

-   Use asynchronous I/O for network and suitable filesystem operations.
-   Use multiprocessing for CPU-heavy rendering, OCR, embedding and
    conversion where needed.
-   Do not assume `async` makes CPU-bound processing faster.
-   Keep bounded worker counts and memory budgets.
-   Persist job state and checkpoints.
-   Prioritize interactive work over background indexing.
-   Support cancellation and safe resume.
-   Prevent concurrent jobs from corrupting the same document version.
-   Use locks or explicit document-operation coordination.

## 10. Security architecture

Every operation passes through the policy engine. Policy evaluation
should include: - Actor and interface. - Requested action. - Document
identity and state. - File/folder scope. - Read/write/delete
permissions. - Risk level. - User approval requirement. - Resource
constraints. - Audit requirements.

PDF contents are untrusted data. Text inside a PDF must never be treated
as instructions that override ARD policy or AI system instructions.

## 11. Local network posture

Default to loopback-only binding. Do not expose the service to a LAN or
public network by default.

If authenticated HTTP MCP or remote API access is enabled, require: -
Explicit user activation. - Authentication. - Origin validation. - Rate
and request-size limits. - Permission scopes. - Audit logging. - Clear
network binding and exposure warnings.

## 12. Observability

Use structured logs and operational events. Avoid logging full document
content, secrets, API keys or sensitive extracted text by default.

Diagnostics should report: - ARD version. - Python and OS versions. -
Integration versions and health. - Resource state. - Job failures and
relevant stack traces with secrets redacted. - Database and storage
health. - Model and language-pack availability.

## 13. Architecture constraints

-   No mandatory Node.js.
-   No mandatory GPU.
-   No mandatory Docker.
-   No mandatory cloud services.
-   No mandatory external LLM.
-   No mandatory vector database.
-   No microservices in the default V1.
-   No arbitrary shell execution exposed to plugins or MCP.
-   No direct filesystem bypass around the document registry and policy
    layer.
