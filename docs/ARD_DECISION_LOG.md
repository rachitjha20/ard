# ARD Decision Log

This document records product and architecture decisions made during
planning. Confirmed decisions should not be changed silently.

## D-001 --- Product name

**Decision:** ARD.\
**Status:** Confirmed by product owner.\
**Note:** Trademark, domain and repository-name clearance remain
pending.

## D-002 --- Product category

**Decision:** Local-first document intelligence and PDF productivity
platform, not merely a PDF editor.\
**Status:** Confirmed.

## D-003 --- Audience

**Decision:** Broad audience: individuals, students, professionals,
businesses and developers.\
**Status:** Confirmed.

## D-004 --- Distribution

**Decision:** Open-source product.\
**Status:** Confirmed.\
**Proposed core license:** Apache-2.0, subject to compliance audit.

## D-005 --- Release ambition

**Decision:** Complete, feature-rich V1 delivered through controlled
phases.\
**Status:** Confirmed.

## D-006 --- Privacy and operating model

**Decision:** Local-first, offline-capable, privacy-focused. No
mandatory cloud, account, external API or GPU.\
**Status:** Confirmed.

## D-007 --- Frontend constraints

**Decision:** Jinja2, HTML, vanilla CSS and vanilla JavaScript.\
**Explicit exclusions:** React, Next.js, Vue, Angular, Svelte, Tailwind,
Bootstrap and mandatory Node/npm.\
**Status:** Confirmed.

## D-008 --- Architecture

**Decision:** Python-first modular monolith with isolated processing
workers.\
**Status:** Confirmed.

## D-009 --- Technology stack

**Decision:** Python 3.12+ / FastAPI; Jinja2 / Vanilla CSS / Vanilla JS;
SQLite / SQLAlchemy / Alembic; PDF.js / pypdf / pikepdf; Tesseract /
OCRmyPDF / Docling; Ollama / optional ChromaDB; pyHanko / cryptography /
ARD Trust; MCP SDK / FastAPI / Typer; psutil / multiprocessing / ARD
Guardian.\
**Status:** Confirmed 17 November 2026.

## D-010 --- Optional integrations

**Decision:** Third-party engines must be capability-detected,
replaceable and optional wherever possible.\
**Status:** Confirmed principle.

## D-011 --- AI

**Decision:** Ollama and external APIs are optional. No bundled API keys
or automatic external document transmission.\
**Status:** Confirmed.

## D-012 --- RAG

**Decision:** Hybrid retrieval with citations; embeddings and LLM
providers independently configurable. ChromaDB optional.\
**Status:** Confirmed direction.

## D-013 --- Resource management

**Decision:** ARD Guardian monitors resources and throttles ARD-owned
work progressively. It must never kill unrelated processes, shut down
the OS or delete code/data as an anti-tampering response.\
**Status:** Confirmed.

## D-014 --- Integrity model

**Decision:** ARD Trust uses baselines, hashes, version lineage,
provenance, signatures and audit evidence. It must not claim arbitrary
edits are always detectable.\
**Status:** Confirmed.

## D-015 --- Safe mode

**Decision:** Disable sensitive capabilities and preserve data if
critical trust controls fail. No self-deletion or destructive
anti-tampering.\
**Status:** Confirmed.

## D-016 --- Initial deployment

**Decision:** Local browser-based application for V1. Native desktop
shell may be evaluated later.\
**Status:** Recommended baseline; confirm packaging details during Phase
0.

## D-017 --- Database

**Decision:** SQLite for V1. PostgreSQL is not required for the local
edition; may be considered for a future server/multi-user edition.\
**Status:** Confirmed direction.

## D-018 --- PDF.js

**Decision:** Bundle locally; no remote CDN dependency.\
**Status:** Confirmed.

## D-019 --- Smart categorization

**Decision:** Google Photos-inspired visual document library with
categories, smart albums, timeline, duplicates, similar docs, entity
search and user correction. No facial recognition.\
**Status:** Confirmed product direction.

## D-020 --- Core application layout

**Decision:** Home, Library, Workspace, Intelligence, Automation, Trust
& Settings.\
**Status:** Confirmed design direction.

## D-021 --- Visual identity

**Decision:** Obsidian Silver palette: Obsidian `#171A1F`, Silver
`#DDE2E8`, Ice Blue `#B9DDF5`, Marigold `#D4A74F`; dark and light
themes.\
**Status:** Confirmed design direction.

## D-022 --- Dependency compliance

**Decision:** Exact versions, transitive dependencies, native binaries,
fonts, OCR language packs and models require individual review.\
**Status:** Pending Phase 0.

## D-023 --- Implementation

**Decision:** Do not start implementation until explicitly authorized.\
**Status:** Authorized by the product owner on 17 November 2026 for Phase
1 and Phase 2, and on the same day for Phases 3 and 4. Later phases still
need authorization.

## D-024 --- Full-text search tokenizer

**Decision:** SQLite FTS5 with the trigram tokenizer for the page-text
index.\
**Reason:** The default tokenizer splits Devanagari words at vowel signs,
which breaks Hindi search; trigram works for any script and matches parts
of words. Cost: a larger index, and words shorter than three characters
fall back to a plain scan.\
**Status:** Implemented 17 November 2026.

## D-025 --- OCR confidence

**Decision:** OCRmyPDF's public API produces the searchable PDF; a second
Tesseract pass at 150 dpi over the same pages supplies word confidence.\
**Reason:** OCRmyPDF's public API does not expose confidence, and its
two-step hOCR functions are private in the installed version.\
**Status:** Implemented 17 November 2026. Revisit if OCRmyPDF publishes
the hOCR pipeline.

## D-026 --- Editing existing text

**Decision:** Existing text is edited one line at a time by removing that
line's drawing instructions from the page content and drawing the
replacement at the same origin, size and colour. The original font is
reused only when it is a simple Latin font that already contains every
character of the new text; otherwise the closest standard font is used and
the report says so. Lines that cannot be edited safely are refused with a
reason rather than approximated.\
**Reason:** The blueprint requires ARD to distinguish original-object
editing from overlays and to disclose limits. Subset fonts in PDFs lack
glyphs for new characters, so "always the same font" cannot be promised.\
**Status:** Implemented 17 November 2026. Embedding a matching system font
and paragraph reflow are not built.

## Open decisions

1.  Final trademark/domain/repository clearance for ARD.
2.  Exact Python dependency versions and lock strategy.
3.  Exact bundled versus system-installed binary policy.
4.  Final offline installer contents and distribution size.
5.  Default local authentication/session model.
6.  Final OS support matrix after tests.
7.  Whether a native desktop shell is justified after V1.
8.  Exact initial Hindi/English OCR language-pack distribution.
9.  Final ChromaDB version and adapter contract.
10. Final model list and model redistribution policy.
