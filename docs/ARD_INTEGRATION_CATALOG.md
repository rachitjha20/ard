# ARD Integration Catalog

## 1. Integration philosophy

ARD owns the user experience, domain logic, permissions, document
registry, job lifecycle, data model and security policy. External
libraries are replaceable engines behind ARD-defined interfaces.

Every integration must be: - Versioned and capability-detected. -
License-reviewed. - Health-checked. - Resource-governed. -
Permission-scoped. - Disableable where optional. - Documented with
fallback behavior.

Do not install every integration as a mandatory dependency.

## 2. Confirmed baseline integrations

  Integration       Purpose                             Status
  ----------------- ----------------------------------- ----------------------------
  PDF.js            Browser PDF rendering               Confirmed
  pypdf             PDF read/manipulation               Confirmed
  pikepdf           Advanced PDF object processing      Confirmed
  Tesseract         CPU OCR                             Confirmed
  OCRmyPDF          Searchable PDF OCR pipeline         Confirmed
  Docling           Structured document understanding   Confirmed
  Ollama            Local LLM runtime                   Confirmed optional runtime
  ChromaDB          Local vector store                  Confirmed optional
  pyHanko           PDF signatures and validation       Confirmed
  cryptography      Cryptographic primitives            Confirmed
  MCP Python SDK    MCP integration                     Confirmed
  FastAPI           API                                 Confirmed
  Typer             CLI                                 Confirmed
  psutil            Resource monitoring                 Confirmed
  multiprocessing   CPU-bound worker isolation          Confirmed

## 3. Supporting dependencies to evaluate

  -----------------------------------------------------------------------
  Dependency              Purpose                 Status
  ----------------------- ----------------------- -----------------------
  pypdfium2               PDF rasterization       Recommended candidate

  qpdf                    PDF structural          Via/alongside pikepdf;
                          infrastructure          verify packaging

  fpdf2                   OCR text-layer support  Verify OCRmyPDF
                                                  dependency chain

  uharfbuzz               Font shaping            Verify OCRmyPDF
                                                  dependency chain

  Pillow                  Image processing        Recommended

  OpenCV                  Advanced image cleanup  Optional

  Ghostscript             Certain PDF operations  Optional system
                                                  dependency

  ExifTool                Metadata inspection     Optional

  veraPDF                 PDF/A validation        Optional

  ClamAV                  Malware scanning        Optional

  Noto fonts              Multilingual rendering  Recommended

  LibreOffice             Office conversion       Optional

  APScheduler             Scheduling              Evaluate; internal
                                                  scheduler may be
                                                  sufficient
  -----------------------------------------------------------------------

## 4. AI and model integrations

### Ollama

-   Optional local runtime.
-   ARD detects service availability and installed models.
-   No model auto-download.
-   Model selection considers available memory and CPU.
-   Model licenses recorded individually.
-   ARD Guardian governs resource usage.

### External AI providers

-   Provider adapter interface.
-   OpenAI-compatible endpoint support.
-   Custom HTTP adapter only with explicit configuration.
-   No bundled API credentials.
-   External document transmission requires explicit user consent.
-   Provider and model recorded for relevant operations.

### Embeddings

-   Separate from LLM provider.
-   Model metadata and license recorded.
-   Local embedding preferred for privacy.
-   Re-indexing supported.
-   Vector store is replaceable.

## 5. Vector store

ChromaDB is optional. Define a `VectorStoreAdapter` contract so ARD can
use another local implementation later.

Required operations: - Create/delete collection. - Upsert chunks and
vectors. - Query by vector. - Delete by document. - Inspect collection
health. - Rebuild index.

SQLite FTS5 remains available independently. ChromaDB must never become
the authoritative source of document metadata or original files.

## 6. MCP integration

Use the official Python SDK. Begin with stdio transport. HTTP transport
is optional and must be authenticated and explicitly enabled.

Initial tool candidates: - `ard_list_documents` -
`ard_get_document_metadata` - `ard_extract_text` -
`ard_search_documents` - `ard_get_page` - `ard_get_outline` -
`ard_summarize_document` - `ard_ocr_document` - `ard_merge_pdfs` -
`ard_split_pdf` - `ard_compress_pdf` - `ard_compare_documents` -
`ard_extract_tables` - `ard_redact_document` -
`ard_validate_signature` - `ard_rag_search`

Tools must be permission-scoped. Read-only by default. Write and
destructive operations require approval. MCP must not expose arbitrary
shell commands, Python execution or unrestricted filesystem access.

## 7. External service connectors

Optional future connectors: - Google Drive. - Dropbox. - OneDrive. -
WebDAV. - External translation services. - RFC 3161 timestamp
authorities. - External certificate validation services.

All cloud connectors are opt-in. No background synchronization or upload
without user consent. Connector tokens must be protected using OS secure
storage where available.

## 8. Integration Hub requirements

The UI should provide: - Installed integrations. - Available
integrations. - Version and compatibility. - Enable/disable. - Health
checks. - Required permissions. - License and notices. - Offline
availability. - Resource estimates. - Installation instructions. - Model
and language-pack management.

## 9. Integration fallback rules

-   No PDF.js: application should report viewer unavailable; do not
    silently substitute a remote viewer.
-   No Tesseract: basic PDF operations still work; OCR is unavailable.
-   No Docling: basic text extraction remains available where supported.
-   No Ollama: all non-AI capabilities continue.
-   No ChromaDB: keyword search and document library continue.
-   No LibreOffice: Office conversion is unavailable with an
    explanation.
-   No ClamAV: report malware scanning unavailable; do not claim files
    are clean.
-   No GPU adapter: CPU operation continues.
-   No internet: local features continue; external providers/connectors
    report offline.

## 10. License handling

Maintain an inventory for: - Direct Python dependencies. - Transitive
Python dependencies. - Bundled JavaScript and CSS assets. - PDF.js
worker and fonts. - OCR language data. - Model weights. - External
executables and shared libraries. - Documentation and icons.

Do not assume a project's main license covers every model, font,
language pack or bundled binary.
