# ARD Product Blueprint

## 1. Executive summary

ARD is an open-source, local-first document intelligence and PDF
productivity platform for individuals, students, professionals,
businesses and developers.

Its purpose is to bring document operations, discovery, understanding,
integrity and automation into one cohesive environment. Users should be
able to work with documents without creating an account, subscribing to
a cloud service, sending files to a remote provider or owning a GPU.

**Tagline:** Your documents. Your intelligence. Your control.

ARD should be powerful enough for professional document workflows while
remaining approachable for everyday users. Its default behavior is
private and local. Cloud services, external AI, model downloads,
synchronization and third-party connectors are opt-in.

## 2. Product vision

Build a trusted document workspace that can: - View, organize, edit,
convert and protect PDFs. - Turn scanned and ordinary documents into
searchable, structured information. - Automatically categorize and help
users rediscover their files. - Answer questions across personal
document collections with traceable citations. - Compare versions and
expose observable changes. - Record document provenance and integrity
evidence without making impossible forensic guarantees. - Automate
repeatable document workflows. - Expose controlled capabilities to MCP
clients, REST consumers and CLI users. - Operate on ordinary CPU-only
computers and work offline for core functions.

## 3. Target audiences

The primary audience is intentionally broad: - Individuals managing
personal documents. - Students and researchers. - Professionals and
consultants. - Small and medium businesses. - Legal, finance,
administration and operations teams. - Developers and technically
advanced users. - Users who need local processing, privacy or offline
access.

The product must not assume enterprise infrastructure or a technical
operator.

## 4. Product principles

1.  **Local-first:** Local processing and local storage are the default.
2.  **Privacy by default:** No telemetry, cloud upload, external AI
    transmission or account requirement by default.
3.  **Offline-capable:** Core viewing, editing, search and local
    processing work without internet after installation and required
    assets are provisioned.
4.  **CPU-first:** No GPU requirement. GPU acceleration is an optional
    capability.
5.  **User control:** Users choose integrations, models, folders,
    permissions and data movement.
6.  **Non-destructive:** Never silently overwrite or delete originals.
7.  **Explainable intelligence:** Classifications and AI outputs show
    evidence, confidence and source references where possible.
8.  **Replaceable engines:** Third-party engines sit behind ARD-owned
    interfaces.
9.  **Consistent policy enforcement:** UI, API, MCP, CLI, plugins and
    workflows use the same authorization and policy services.
10. **Honest integrity claims:** ARD reports evidence and limitations;
    it does not claim arbitrary PDF edits are always detectable.
11. **Accessible and efficient:** Keyboard support, reduced motion,
    resource budgets and clear feedback are baseline requirements.
12. **Open source with compliance discipline:** Every distributed
    dependency and asset receives a license review.

## 5. Product structure and naming

  -----------------------------------------------------------------------
  Product area                        Responsibility
  ----------------------------------- -----------------------------------
  ARD                                 Main application

  ARD Workspace                       PDF viewing, editing, annotations,
                                      page operations, forms and
                                      conversion

  ARD Library                         Smart document organization,
                                      search, timeline, collections and
                                      categorization

  ARD Intelligence                    OCR-assisted understanding,
                                      extraction, comparison, AI and RAG

  ARD OCR                             OCR engine management and OCR
                                      workflows

  ARD Knowledge                       Document collections, chunking,
                                      embeddings and retrieval

  ARD Trust                           Integrity, provenance, signatures,
                                      audit and security inspection

  ARD Guardian                        Resource monitoring and safe
                                      workload control

  ARD Flow                            Jobs, batch processing and workflow
                                      automation

  ARD Connect                         MCP, REST, CLI and external
                                      integrations

  ARD Developer                       SDK, extension interfaces and
                                      developer documentation
  -----------------------------------------------------------------------

The product name ARD is selected, but trademark, repository-name and
domain clearance have not been established. Complete name clearance
before public launch.

## 6. Core product pillars

### 6.1 ARD Workspace

A full PDF workspace: - PDF viewing and navigation. - Search, text
selection, bookmarks and outline. - Tabs and multi-document workspace. -
Page management. - Annotations and markups. - Overlay-based text and
image additions. - Watermarks, stamps, headers, footers and page
numbers. - Forms. - Conversion and export. - Compression and
optimization. - Encryption and permissions. - Signature workflows and
validation. - Redaction with content-removal verification.

Important distinction: editing arbitrary existing PDF text in place is
not universally reliable. ARD must distinguish original-object editing
from overlay-based additions and disclose limitations.

### 6.2 ARD Library

A visual, Google Photos-inspired document library: - Automatic document
categorization. - Smart albums. - Timeline and recent activity. -
Thumbnail grid and list views. - Tags, favorites and custom
collections. - Exact and near-duplicate detection. - Similar-document
grouping. - Entity-based search. - OCR and processing status. - Filters
for large, scanned, unclassified, modified and invalid documents. - User
correction of classifications.

No facial recognition is required. Sensitive document categories must
remain local and private.

### 6.3 ARD Intelligence

-   OCR and layout understanding.
-   Structured extraction.
-   Table and financial extraction.
-   Document summaries.
-   Question answering.
-   Document comparison.
-   Translation.
-   Local AI and optional external AI.
-   RAG across one document or a collection.
-   Knowledge graph and extracted entities.
-   Source/page citations and retrieved-chunk inspection.

AI is optional. Basic viewing, editing, search and document management
must not depend on an LLM.

### 6.4 ARD Trust

-   Import fingerprints.
-   Baseline snapshots.
-   Version lineage.
-   Signed provenance manifests.
-   Digital PDF signature validation.
-   Change reports.
-   Hash-linked audit records.
-   Structural inspection.
-   Security and integrity status indicators.
-   Redaction verification.
-   Permission enforcement across all interfaces.

### 6.5 ARD Guardian

-   CPU, RAM, process and disk monitoring.
-   Optional GPU/VRAM and temperature monitoring.
-   Workload limits.
-   Adaptive profiles.
-   Queue prioritization.
-   Pause, checkpoint, resume and safe cancellation.
-   User-facing explanations of throttling decisions.

### 6.6 ARD Flow and ARD Connect

-   Batch processing.
-   Repeatable workflows.
-   Persistent jobs and checkpoints.
-   MCP tools and resources.
-   REST API.
-   CLI. 
-   Python SDK.
-   Versioned plugin interface.

## 7. Smart Library categorization

Initial categories should include: - Invoices and receipts. - Contracts
and agreements. - Education and certificates. - Business and
operations. - Government documents. - Identity documents. - Books and
research. - Forms and applications. - Tax and finance. - Medical and
insurance documents. - Reports and presentations. - Uncategorized.

Categories are suggestions, not immutable facts. Each automatic
classification should include: - Suggested category. - Confidence
score. - Evidence, such as extracted phrases, layout or metadata. -
Classification engine and version. - Timestamp. - User override state.

User corrections should be stored locally and must not be transmitted or
used to train an external model without explicit consent.

### Smart albums

Initial smart albums: - Recently Added. - Recently Modified. -
Favorites. - Scanned Documents. - Needs OCR. - Large Files. -
Unclassified. - Similar Documents. - Duplicate Candidates. - Invalid
Signatures. - Changes Detected. - Provenance Missing.

### Timeline semantics

Do not conflate: - Import date. - Filesystem creation or modification
date. - PDF metadata creation/modification date. - Date extracted from
document content. - ARD edit/version date.

Every date shown in the UI should identify its meaning.

### Duplicate detection

-   Exact duplicate: SHA-256 file hash.
-   Near duplicate: normalized text fingerprints and page-level
    comparison.
-   Similar document: optional local embeddings.
-   Never auto-delete duplicates. Offer review and user-controlled
    cleanup.

## 8. Document intelligence and RAG

RAG should support: - One document, selected files, folders and named
collections. - OCR ingestion for scanned PDFs. - Incremental ingestion
and re-indexing. - Chunking by page, paragraph, sentence, fixed size and
optional semantic boundaries. - Page number, source offsets and source
document identity retained for every chunk. - Configurable embedding
providers. - Optional vector-store adapters. - Hybrid keyword and
semantic retrieval. - Metadata and category filters. - Top-K retrieval
and configurable thresholds. - Optional reranking. - Citation-backed
answers. - Retrieved passage inspection.

The embedding provider and LLM provider must be independently
configurable. RAG must never invent citations. If retrieval does not
support an answer, ARD should say so.

## 9. AI provider policy

Support: - Ollama for local models. - OpenAI-compatible API endpoints. -
Custom provider adapters where feasible. - Independent embedding
provider selection. - Provider health checks and model capability
detection.

Requirements: - No bundled API keys. - No automatic external
transmission of documents. - Explicit consent before sending document
content to external providers. - Store credentials using OS secure
storage where available. - Provide a clear record of which provider
processed a document. - Allow AI features to be disabled completely. -
Model downloads are user initiated and require model-license review.

## 10. Document lifecycle

Proposed lifecycle states:
`imported → classified → OCR pending/completed → reviewed → edited → signed → approved → archived → superseded/expired`

Not every document must pass through every state. Users should be able
to configure reminders for expiry, renewal and review dates.

## 11. Additional product capabilities

Include in the product plan, prioritizing according to the feature
matrix: - Visual document comparison. - Financial document intelligence
and invoice extraction. - Multilingual document studio, prioritizing
Hindi and English initially. - PDF/A and long-term archive support. -
Document health and structural inspection. - Accessibility checking. -
Template studio. - Review and comment exchange. - Backup and recovery. -
Natural-language and structured search. - Knowledge graph. - Document
expiry reminders. - Optional cloud storage connectors.

## 12. V1 scope philosophy

The ambition is a complete, feature-rich V1. Delivery must be phased and
verified. Do not attempt to implement every subsystem simultaneously.
The feature matrix and implementation plan identify sequencing and
acceptance gates.

V1 must be usable as a complete local document product even if optional
AI, vector search, cloud connectors or GPU acceleration are unavailable.
