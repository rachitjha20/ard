# ARD Feature Matrix and Acceptance Criteria

## Priority definitions

-   **P0 --- Core V1:** Required for a coherent, useful V1.
-   **P1 --- Advanced V1:** Expected in the feature-rich V1, delivered
    after core stability.
-   **P2 --- Optional/extended:** Optional integration or capability;
    must not block core operation.
-   **Later:** Candidate for post-V1 expansion.

## 1. PDF viewer --- P0

-   Render PDF pages locally using bundled PDF.js.
-   Page navigation, zoom, fit modes and rotation.
-   Text selection and in-document search.
-   Thumbnails, outline/bookmarks and page count.
-   Multiple document tabs.
-   Print and presentation mode where browser/platform support allows.

Acceptance: - Opens supported PDFs without network access. - Handles
large documents without blocking the interface. - Reports
password-protected, damaged or unsupported documents clearly. - Does not
silently modify the source file.

## 2. Page operations --- P0

-   Merge, split, extract, delete, duplicate and reorder pages.
-   Rotate, crop, resize and reverse pages.
-   Insert blank pages.
-   Insert or replace pages from another document.

Acceptance: - Operations produce a new output/version. - Page order and
count are verified after processing. - Original source remains intact. -
Failures leave no partial output presented as complete.

## 3. Editing and annotation --- P0/P1

-   Text and image overlays.
-   Shapes, highlights, notes and freehand annotation.
-   Stamps and watermarks.
-   Headers, footers and page numbers.
-   Annotation import/export where supported.
-   Undo/redo for supported operations.

Acceptance: - Clearly distinguish overlay additions from editing
existing PDF objects. - Preserve source version. - Render output and
compare expected page count and content.

## 4. Compression and optimization --- P0/P1

-   Presets such as maximum quality, balanced and smaller file.
-   Image downsampling and recompression.
-   Metadata inspection and removal.
-   Structural optimization where safe.
-   Before/after size reporting.

Acceptance: - Never overwrite original automatically. - Report actual
output size and settings. - Validate resulting PDF can be opened. - Warn
when quality loss is expected.

## 5. Conversion --- P1

-   PDF to TXT, HTML, PNG, JPG and TIFF.
-   PDF table extraction to CSV/XLSX.
-   PDF to DOCX where feasible.
-   Office to PDF through optional LibreOffice.
-   PPTX-related conversion where supported.

Acceptance: - Clearly report conversion fidelity limitations. - Preserve
page order. - Provide per-page and batch error reports. - Do not promise
exact layout preservation for formats that cannot guarantee it.

## 6. OCR --- P0/P1

-   Tesseract CPU OCR.
-   OCRmyPDF searchable PDF generation.
-   Detect likely scanned pages.
-   Orientation correction, deskew, denoise and contrast options.
-   Language selection and language-pack detection.
-   Confidence and review interface.
-   Text export and batch processing.
-   Optional OCR engines behind adapters.

Initial language priority: English and Hindi, with expandable language
packs.

Acceptance: - CPU-only operation works. - Missing language packs are
identified. - OCR text retains page association. - Low-confidence output
is marked for review. - Searchable PDF text layer is verified.

## 7. Forms and signatures --- P1

-   Detect and fill supported AcroForm fields.
-   Text, checkbox, radio, dropdown and date fields where supported.
-   Form data import/export to JSON/CSV.
-   Visual signature drawing or image placement.
-   Cryptographic PDF signature workflows where supported.
-   Certificate and signature validation.

Acceptance: - Clearly distinguish visual signatures from cryptographic
digital signatures. - Explain unsupported signature types. - Verify
signed revision and later changes where possible.

## 8. Security and redaction --- P1

-   Password protection and encryption.
-   Permission inspection.
-   Metadata removal.
-   Sensitive-data detection.
-   Redaction.
-   Embedded attachment and action inspection.
-   Structural anomaly reporting.

Acceptance: - Redaction removes underlying text and relevant content
objects, not just draws a black rectangle. - Re-open and extract output
to verify redacted content is absent. - Warn that ARD cannot guarantee
complete forensic sanitization of every PDF. - Never execute embedded
PDF scripts or actions.

## 9. Smart Library --- P0/P1

-   Automatic categorization and user correction.
-   Categories, tags, favorites and collections.
-   Smart albums and timeline.
-   Thumbnail grid and list.
-   Full-text search.
-   Entity search.
-   Exact and near duplicate detection.
-   Similar-document grouping.
-   Filters for OCR status, file size, integrity and classification.

Acceptance: - User can override any suggested category. - Classification
confidence and evidence are visible. - Dates identify their source and
meaning. - Duplicate detection never deletes files automatically. -
Works without embeddings or cloud AI.

## 10. Document Intelligence --- P1

-   Structured document extraction.
-   Invoice and receipt extraction.
-   Table extraction.
-   Summaries and key-point extraction.
-   Document comparison.
-   Translation.
-   Action items and date extraction.
-   Knowledge graph and entity relationships.

Acceptance: - Extracted values retain source references. - Confidence
and uncertainty are shown. - AI output is distinguishable from source
facts. - External provider transmission requires explicit consent.

## 11. RAG --- P1

-   Collections and ingestion.
-   OCR-aware ingestion.
-   Configurable chunking.
-   Embedding adapter.
-   Keyword and semantic hybrid retrieval.
-   Source/page citations.
-   Retrieved passage inspector.
-   Incremental indexing and rebuild.

Acceptance: - Citations resolve to source documents/pages. - No
fabricated references. - Search remains usable without vector store. -
Embeddings and LLMs can be configured independently.

## 12. ARD Trust --- P1

-   SHA-256 import fingerprint.
-   Optional trusted baseline.
-   Page-level fingerprints.
-   Metadata snapshot.
-   Version lineage.
-   Signed provenance manifest.
-   Hash-linked audit events.
-   Digital signature validation.
-   Integrity report.

Acceptance: - Status reflects evidence, not assumptions. - No claim that
edits to arbitrary files can always be detected without a trusted
baseline. - Local administrator threat limitations are documented. -
Audit verification failure triggers safe behavior, not destructive
self-repair.

## 13. ARD Guardian --- P0

-   CPU and RAM monitoring.
-   Process-level resource measurements.
-   Disk space monitoring.
-   Optional GPU and temperature monitoring.
-   Eco, Balanced, Performance and Custom profiles.
-   Bounded job queue.
-   Background throttling and pause.
-   Checkpoints and recovery.

Acceptance: - Never kill unrelated processes. - Never shut down the
operating system. - Never delete source files or codebase. - Use
sustained thresholds, hysteresis and recovery thresholds. - Explain each
intervention.

## 14. Automation and developer platform --- P1

-   Persistent jobs and batch processing.
-   Workflow builder.
-   MCP server.
-   REST API and OpenAPI.
-   CLI and Python SDK.
-   Versioned plugin interfaces.

Acceptance: - All interfaces use the same domain services and policy
engine. - MCP writes and destructive actions require explicit scopes and
approval. - No unrestricted shell or filesystem access.

## 15. Additional product features

Prioritize after core stability: - PDF/A validation and archival. -
Accessibility checking. - Document lifecycle and expiry reminders. -
Template studio. - Review/comment exchange. - Cloud storage
connectors. - Advanced visual similarity. - External timestamp
authorities. - More advanced multilingual translation.
