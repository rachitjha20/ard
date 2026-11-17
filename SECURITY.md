# Security

ARD is pre-release software (0.1.0). This file describes what it does today
and, just as important, what it does not.

## Reporting a problem

Until a public repository and contact address exist, report security problems
privately to the project owner. Please do not open public issues for
vulnerabilities.

## What ARD does

- **Loopback only.** The server binds to `127.0.0.1` and refuses any other
  address unless `ARD_ALLOW_NON_LOOPBACK=true` is set.
- **Session token.** A random token is created per installation
  (`session.token`, owner-readable only). The browser receives it as an
  HttpOnly, SameSite=Strict cookie; scripts send it as a bearer token.
- **Host and Origin checks.** Requests whose Host header is not a loopback
  name are refused (DNS rebinding). State-changing requests from another
  Origin are refused.
- **Content Security Policy.** Pages may load scripts, styles, fonts and data
  only from ARD itself. Nothing is fetched from the internet.
- **Untrusted documents.** PDFs are parsed in separate worker processes.
  Embedded JavaScript, actions and attachments are reported and never run or
  opened. Size and page-count limits apply at import.
- **Non-destructive storage.** Imported files and every version are stored
  read-only and are never overwritten. "Trash" hides a document; it does not
  remove files.
- **One policy engine.** UI, REST and CLI call the same services, which ask
  the same policy engine. MCP, workflow and plugin actors are read-only or
  denied by default.
- **Audit log.** Changes are written to a hash-linked log. If the log fails
  verification, ARD enters safe mode: editing is disabled, viewing and export
  continue, and nothing is repaired or deleted automatically.
- **No telemetry.** ARD sends nothing anywhere.

## Known limitations

- **Other local users.** Anyone who can reach loopback on this machine can
  load a page and obtain a session cookie. ARD is a single-user application;
  do not run it on a shared multi-user host. A stronger local authentication
  model is an open decision.
- **The audit log is evidence, not proof.** Someone with full control of the
  machine can rewrite the database and recompute the chain. Signed provenance
  manifests and external timestamps are planned, not implemented.
- **Baselines start at import.** A fingerprint shows a file has not changed
  since ARD imported it. It says nothing about the file before that.
- **Redaction is page-level.** A page with a redaction box becomes an image;
  the text under the box is gone from the new version, and so is the
  selectable text of the rest of that page. ARD verifies the result, but it
  does not search other pages, bookmarks, metadata or attachments for the
  same information, and it keeps the earlier, unredacted versions in its own
  store. Share only the redacted file you download.
- **PDF passwords.** Passwords you type to protect or unlock a document are
  sent to the local server, held in memory for that one job, and never
  written to the database, the audit log or the log file. ARD cannot recover
  them. Permission flags (no printing, no copying) are requests that PDF
  software is expected to honour, not something encryption enforces; only a
  password to open keeps content private. Removing protection requires the
  owner password.
- **The search index holds your documents' text.** Extracted and recognised
  text is stored in ARD's database on this computer so it can be searched.
  It is not copied into the audit log, job records or log files, and never
  leaves the machine. Anyone who can read the data folder can read it.
- **OCR is a best guess.** Recognised text can be wrong, and the confidence
  shown is Tesseract's own estimate, not a measure of correctness. Search
  results that come from OCR are marked. A running OCR job cannot be
  interrupted; cancelling discards its result when it finishes.
- **Categories are suggestions.** They come from keyword rules, can be wrong,
  and are shown with their evidence. Duplicate candidates are for review;
  ARD never deletes or merges on their strength.
- **Digital signatures are checked offline.** ARD verifies the mathematics
  and whether the document changed after signing, and checks the signer's
  certificate against this computer's trusted authorities. It does not
  contact revocation or timestamp services, so a revoked certificate is not
  detected. "Unknown" means intact but unvouched-for; only "Valid" means the
  signer's certificate is trusted here.
- **Signing certificates.** A .p12/.pfx file and its passphrase are sent to
  the local server for one signature, held in memory, and never written to
  disk, the database or logs. Signatures made by ARD are invisible (no
  picture on the page) and carry no trusted timestamp. Editing a signed
  document in ARD produces a new version whose signatures no longer verify.
- **Office conversion runs LibreOffice** on the file you import, headless and
  with a throw-away profile. A document that links to remote content may
  cause LibreOffice to fetch it; ARD cannot prevent that. A snap-installed
  LibreOffice cannot read hidden folders, so conversion then needs
  `ARD_DATA_DIR` set to a visible folder.
- **PDF/A is not validated by ARD itself.** Without veraPDF only the file's
  claim and certain obstacles are shown. The veraPDF adapter was written
  without veraPDF installed and has not been exercised.
- **Watermarks and visual signatures prove nothing.** They are drawn on the
  page and can be reproduced or removed by anyone with an editor.
- **Cropping, covering and metadata removal are not redaction.** Cropped
  content stays in the file, and a shape, image or stamp placed over text
  does not remove the text underneath; earlier versions keep removed metadata.
- **Editing a line removes it from the new version only.** The old wording
  stays in earlier versions, which ARD keeps. Line editing is not redaction.
- **Text overlays use the standard PDF fonts** (Latin, Windows-1252). Text in
  other scripts, including Hindi, is placed as an image: it looks right but is
  not searchable. Use Redact to remove content.
- **No malware scanning.** ARD does not scan files and never describes a file
  as clean.
- **Cancelling a running job** does not interrupt it; the job finishes and its
  output is discarded.
- **Not yet done:** AI, MCP,
  a dependency licence audit and an SBOM.
