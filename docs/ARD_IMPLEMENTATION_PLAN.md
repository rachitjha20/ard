# ARD V1 Implementation Plan

## 1. Delivery philosophy

The product ambition is a complete, feature-rich V1. Delivery is
controlled and phased. Do not attempt parallel implementation of every
subsystem before foundational contracts, storage, security and tests
exist.

**Current status (17 November 2026):** Implementation authorized and
started. Phase 1 (foundation) and the core of Phase 2 (viewer, registry,
page operations, compression, metadata removal) are built and tested.
Phase 0 deliverables that remain open: the dependency and licence audit,
the performance budgets and the OS compatibility plan. Phase 2 also
includes overlays (text, image, stamp) and annotations (highlight, note,
shapes, freehand). Not yet built from the Phase 2 list: resizing placed
items and annotation import/export. Phase 3 is built: forms, visual and
cryptographic signatures with validation, encryption and permissions,
redaction with verification, watermarks and headers/footers, export to
text and images, an Office conversion adapter and a PDF/A adapter. Phase 3
exit gate still open: the signature and redaction corpora are generated
test cases only (no real-world files), the veraPDF adapter is untested,
PDF-to-Office conversion is not built, and no separate security review of
document operations has been done. Phase 4 is mostly built: Tesseract
and OCRmyPDF integration, language-pack detection, searchable PDF output,
per-page OCR confidence, categories and tags, smart albums and a
timeline, duplicate detection, and full-text search (SQLite FTS5, trigram
tokenizer). Still open in Phase 4: Docling integration, entity
extraction, and an OCR review screen that allows correcting text.

## 2. Phase 0 --- Architecture and feasibility

Deliverables: - Repository and environment audit. - Confirmed technology
baseline. - Dependency and licensing audit. - Third-party binary and
model inventory. - Threat model. - Data model. - Domain boundaries. -
API contracts. - Integration interfaces. - Design system. - Test
strategy. - Performance budgets. - OS compatibility plan. - Release and
governance plan.

Exit gate: - No unresolved blocker in core dependency licenses. - Python
3.12+ environment can install the selected baseline. - CPU-only
dependency strategy is proven. - Required external binaries and optional
capabilities are documented. - Product owner approves implementation
plan.

## 3. Phase 1 --- Foundation

Build: - FastAPI application. - Jinja2 shell and vanilla frontend. -
SQLite database. - SQLAlchemy and Alembic migrations. - Configuration
and logging. - Data directory manager. - Capability registry. - Job
orchestrator. - ARD Guardian foundation. - Startup and shutdown
scripts. - Basic diagnostics.

Exit gate: - Clean install and startup. - Database migration and
recovery tests. - No mandatory internet access. - Bounded background
workers. - Resource monitoring overhead measured.

## 4. Phase 2 --- PDF foundation

Build: - PDF.js viewer. - Document import and registry. - Page
thumbnails and navigation. - Search and outline. - Tabs. - Page
operations. - Merge/split/extract/reorder/rotate/crop. - Basic
annotations and overlays. - Compression and metadata operations. -
Output validation.

Exit gate: - Original preservation. - Valid output checks. - Large-file
responsiveness. - Offline viewer and basic operations.

## 5. Phase 3 --- Advanced PDF operations

Build: - Forms. - Visual signatures. - Cryptographic signature
integration. - Encryption and permissions. - Redaction and
verification. - Watermarks and headers/footers. - Conversion. - Office
conversion adapter. - PDF/A validation adapter.

Exit gate: - Signature test corpus. - Redaction test corpus. -
Conversion limitations documented. - Security review of document
operations.

## 6. Phase 4 --- OCR and Smart Library

Build: - Tesseract integration. - OCRmyPDF integration. - Language-pack
detection. - Searchable PDF output. - OCR confidence and review UI. -
Docling integration. - Categories and tags. - Smart albums and
timeline. - Duplicate detection. - Entity extraction. - FTS5 full-text
search.

Exit gate: - CPU-only OCR tested. - English and Hindi language path
tested. - OCR source page references retained. - User category
correction works. - No automatic duplicate deletion.

## 7. Phase 5 --- Document Intelligence

Build: - Structured extraction. - Table and invoice extraction. -
Comparison. - Translation adapters. - Ollama provider. - External AI
provider adapters. - RAG ingestion and chunking. - Hybrid retrieval. -
Optional ChromaDB. - Citation and retrieved-passage UI. - Knowledge
graph foundations.

Exit gate: - AI can be disabled. - External transmission consent
enforced. - Citation resolution tests pass. - CPU and memory limits
enforced. - RAG index can be rebuilt.

## 8. Phase 6 --- ARD Trust and security

Build: - Import fingerprints. - Baseline snapshots. - Version lineage. -
Signed provenance manifests. - Hash-linked audit records. - Signature
validation reports. - Structural inspection. - Safe mode. - Policy
enforcement across all interfaces.

Exit gate: - Tamper detection tests. - Redaction verification. - Audit
chain verification. - Permission tests across UI/API/MCP/CLI. -
Threat-model review.

## 9. Phase 7 --- Developer and automation

Build: - MCP stdio server. - Scoped MCP tools. - REST API hardening. -
CLI. - Python SDK. - Plugin contracts. - Workflow engine. - Batch
processing. - Integration Hub.

Exit gate: - No policy bypass through any interface. - Write approvals
tested. - Plugin permissions and compatibility validated. - Job recovery
and cancellation verified.

## 10. Phase 8 --- Product experience

Build: - Onboarding. - Settings and Integration Hub. - Diagnostics. -
Backup and restore. - Accessibility improvements. - Keyboard shortcuts
and command palette. - Performance improvements. - Documentation and
tutorials. - Feedback flow.

Exit gate: - Usability review. - Accessibility audit. - Restore test. -
Clean user-profile test. - No blocking UI defects.

## 11. Phase 9 --- Release qualification

Test: - Linux, Windows and macOS. - CPU-only machines. - Offline
workflows. - Low-memory scenarios. - Large PDFs. - Damaged PDFs. -
Security test corpus. - Dependency and license audit. - SBOM. -
Packaging. - Signed releases. - Upgrade and migration. - Backup and
restore. - Documentation.

Exit gate: - Release blocker list closed. - Known limitations
published. - License notices complete. - Security policy published. -
Reproducible installation documented. - Product owner authorizes
release.

## 12. Definition of done

A feature is not complete merely because a UI control exists. It must
include: - Domain/service implementation. - Input validation. -
Permission checks. - Error handling. - Logging and audit where
appropriate. - Unit tests. - Integration tests. - UI tests where
applicable. - Resource behavior. - Documentation. - Accessibility. -
Offline behavior where promised. - Failure and recovery behavior.

## 13. Change control

Any proposed change to confirmed stack or core architecture must
include: - Reason. - Alternatives. - License impact. - Security
impact. - Resource impact. - Compatibility impact. - Migration impact. -
Product-owner approval.

Do not silently introduce React, Node.js, a cloud dependency, a GPU
requirement, a mandatory vector database or a microservice architecture.
