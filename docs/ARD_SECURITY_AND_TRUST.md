# ARD Security, Trust and Document Integrity

## 1. Security goals

ARD must protect: - Original user documents. - Extracted and derived
document content. - API and connector credentials. - Local model and
integration configuration. - Document provenance and audit history. -
User-selected file and folder scopes. - Integrity of security-sensitive
operations.

ARD is not a forensic laboratory and must not claim that every
modification to an arbitrary PDF can be detected.

## 2. Threat model

Consider: - Malicious or malformed PDFs. - Embedded files and
attachments. - Embedded JavaScript and actions. - External links and
hostile URLs. - Prompt injection in document text. - Malicious OCR or
parser inputs. - Path traversal and unsafe filenames. - Archive bombs
and oversized files. - Resource exhaustion. - Malicious plugins or
compromised dependencies. - Unauthorized MCP/API clients. - Local user
or administrator tampering. - Compromised external AI provider. -
Supply-chain and model-license risks.

## 3. Untrusted document handling

-   Treat all imported documents as untrusted.
-   Never execute PDF JavaScript or embedded actions.
-   Do not automatically open embedded attachments.
-   Inspect suspicious links and attachments without following/executing
    them.
-   Apply file-size, page-count, processing-time and memory limits.
-   Use worker isolation for risky parsing and conversion.
-   Use safe temporary directories.
-   Validate output before presenting it as complete.
-   Avoid path traversal and symlink escape.
-   Keep parser and conversion dependencies updated.

## 4. ARD Trust baseline

On import, optionally establish a trusted baseline: - SHA-256 whole-file
hash. - File size. - Page count. - Page-level fingerprints. - Normalized
text fingerprints. - Metadata snapshot. - Import timestamp. - ARD
version. - Optional original file preservation.

The baseline should be clearly marked as created by ARD at a specific
time. It does not prove the file was authentic before import.

## 5. Version lineage

Every ARD edit should create a new version by default: - Parent
version. - Input hash. - Output hash. - Operation name and parameters. -
Timestamp. - Application version. - Actor/interface. - Relevant audit
event. - Optional signed provenance manifest.

Do not overwrite original files automatically.

## 6. Provenance and audit

-   Hash-linked audit records.
-   Signed provenance manifests.
-   Signing keys protected using OS key storage where available.
-   Optional external timestamp anchoring in a future release.
-   Verification command and report.
-   Clear status when records are missing or unverifiable.

A local hash chain is not impossible to rewrite if an attacker controls
the whole machine, database and signing keys. State this limitation
clearly.

## 7. Digital signature semantics

Distinguish: - Visual signature: image or drawing placed on a page. -
Cryptographic digital signature: certificate-backed signature over PDF
content.

Where supported, validate: - Signature integrity. - Certificate chain. -
Certificate validity period. - Timestamp. - Signed revision. - Changes
after signing. - Certification permissions.

Do not state that a signature is trusted merely because a signature
field exists. Trust depends on certificate and validation policy.

## 8. Integrity status vocabulary

Use evidence-based labels: - Original Baseline Verified. - Modified
Since Baseline. - Signature Valid. - Signature Invalid. - Signature
Unknown. - Provenance Unavailable. - Audit Verification Failed. -
Integrity Check Incomplete.

A modification is not automatically fraud. Report what changed and what
evidence is available.

## 9. Redaction

Redaction must remove underlying content, not merely place a visual
black rectangle.

After redaction: - Re-open the output. - Extract text and inspect page
objects where feasible. - Search for redacted strings. - Verify expected
output behavior. - Record the operation and result. - Preserve original
separately unless the user explicitly deletes it.

Explain limitations around OCR layers, hidden objects, attachments,
incremental revisions and external copies.

## 10. Central policy engine

All operations from: - UI. - REST API. - MCP. - CLI. - Plugins. - AI
agents. - Workflows.

must pass through one policy service.

Policy considers actor, action, document scope, permissions, risk,
approval, resource state and audit requirements.

AI output must never grant itself permissions. Text in a PDF must never
override application policy.

## 11. MCP security

-   stdio first.
-   Read-only default permissions.
-   Explicit directory allowlists.
-   Write scopes separated from read scopes.
-   Approval for destructive operations.
-   No arbitrary shell or Python execution.
-   No unrestricted filesystem access.
-   HTTP transport disabled by default.
-   If enabled: authentication, origin checks, rate limits, size limits
    and audit records.

## 12. Safe mode

If a critical Trust or policy component is unavailable or integrity
checks fail: - Enter a clearly explained safe mode. - Disable sensitive
editing, signing, redaction or provenance operations as appropriate. -
Preserve originals and logs. - Permit safe viewing and exports only
where the risk assessment allows. - Offer recovery from a verified
installation package.

Never self-delete the application, sabotage the codebase, delete user
files or silently destroy data as an anti-tampering measure.

## 13. Secrets and privacy

-   No hardcoded API keys.
-   OS keyring where available.
-   Redact secrets from logs and diagnostics.
-   No telemetry by default.
-   No external AI transmission without explicit consent.
-   Local document and OCR data remain local unless user initiates an
    external action.
-   Provide clear data-flow disclosures for each connector.

## 14. Malware scanning

ClamAV may be offered as an optional scanning integration. If it is not
installed or definitions are unavailable, report scanning as
unavailable. Never label a document clean merely because a scanner did
not run.

## 15. Security verification

Required tests: - Path traversal. - Malformed and oversized PDFs. -
Embedded attachment handling. - Prompt injection in document text. - MCP
scope enforcement. - Unauthorized write attempts. - Redaction
verification. - Signature validation cases. - Audit chain tampering
detection. - Secret leakage in logs. - Safe mode behavior. - Dependency
and SBOM review.
