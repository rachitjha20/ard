"""Cryptographic PDF signatures: validate and sign (runs in worker processes).

Honest reporting is the point of this module:

* A signature is **Valid** only when its cryptography checks out, the document
  has not been changed in a disallowed way since, *and* the signer's
  certificate chains to a certificate this computer trusts.
* **Invalid** means the signed bytes do not match, or the document was changed
  after signing in a way the signature does not permit.
* **Unknown** means the mathematics is intact but ARD cannot vouch for who the
  signer is (self-signed or unknown issuer, expired certificate, and so on).

Validation is offline. No certificate, revocation list or timestamp service
is contacted, so revocation is not checked; the report says so.

Signing uses a PKCS#12 (.p12/.pfx) file supplied for that one operation. The
key and its passphrase are held in memory and never written anywhere.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

# These libraries log a full traceback for every untrusted certificate, which is an expected
# outcome here and is reported to the user properly instead.
for _noisy in ("pyhanko", "pyhanko_certvalidator", "pyhanko.sign", "pyhanko.sign.validation"):
    logging.getLogger(_noisy).setLevel(logging.CRITICAL)

STATUS_VALID = "Signature Valid"
STATUS_INVALID = "Signature Invalid"
STATUS_UNKNOWN = "Signature Unknown"


class SignError(Exception):
    pass


def _name(certificate: Any) -> dict[str, str]:
    try:
        subject = certificate.subject.native
        issuer = certificate.issuer.native
    except Exception:
        return {"subject": "", "issuer": "", "organization": ""}

    def text(value: Any) -> str:
        return ", ".join(value) if isinstance(value, list) else str(value or "")

    return {"subject": text(subject.get("common_name")) or certificate.subject.human_friendly,
            "organization": text(subject.get("organization_name")),
            "issuer": text(issuer.get("common_name")) or certificate.issuer.human_friendly}


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def validate(payload: dict[str, Any]) -> dict[str, Any]:
    """Report on every signature in a PDF. Read-only and offline."""
    from pyhanko.pdf_utils.reader import PdfFileReader
    from pyhanko.sign.validation import validate_pdf_signature
    from pyhanko_certvalidator import ValidationContext

    results: list[dict[str, Any]] = []
    with open(payload["path"], "rb") as handle:
        try:
            reader = PdfFileReader(handle, strict=False)
            if reader.encrypted:
                return {"signatures": [], "count": 0, "locked": True,
                        "note": "This document is password-protected; its signatures cannot be examined until it is unlocked."}
            embedded = list(reader.embedded_signatures)
        except Exception as exc:
            return {"signatures": [], "count": 0, "unreadable": True,
                    "note": f"The document's signature data could not be read ({exc.__class__.__name__})."}

        for signature in embedded:
            entry: dict[str, Any] = {"field": str(signature.field_name or ""), "status": STATUS_UNKNOWN}
            try:
                certificate = signature.signer_cert
                entry.update(_name(certificate))
                entry["certificate_valid_from"] = _iso(certificate.not_valid_before)
                entry["certificate_valid_until"] = _iso(certificate.not_valid_after)
                entry["self_signed"] = certificate.self_signed in ("yes", "maybe")
            except Exception:
                certificate = None
            try:
                # A fresh context per signature; fetching is off, so nothing leaves this computer.
                status = validate_pdf_signature(signature, ValidationContext(allow_fetching=False))
            except Exception as exc:
                entry.update(status=STATUS_INVALID, intact=False,
                             explanation=f"The signature could not be validated ({exc.__class__.__name__}). Treat it as invalid.")
                results.append(entry)
                continue

            intact, valid, trusted = bool(status.intact), bool(status.valid), bool(status.trusted)
            coverage = getattr(status.coverage, "name", str(status.coverage))
            modification = getattr(status.modification_level, "name", None)
            changes_ok = bool(status.docmdp_ok) if status.docmdp_ok is not None else coverage == "ENTIRE_FILE"
            now = datetime.now(timezone.utc)
            expired = bool(certificate is not None and certificate.not_valid_after < now)
            entry.update(
                intact=intact, cryptographically_valid=valid, trusted=trusted,
                covers_whole_document=coverage == "ENTIRE_FILE",
                changed_after_signing=coverage != "ENTIRE_FILE",
                changes_allowed=changes_ok, modification_level=modification,
                signing_time_claimed=_iso(status.signer_reported_dt),
                has_timestamp=status.timestamp_validity is not None,
                certificate_expired=expired,
            )
            if not intact or not valid:
                entry["status"] = STATUS_INVALID
                entry["explanation"] = "The signed content does not match the signature. The document or the signature was altered."
            elif not changes_ok:
                entry["status"] = STATUS_INVALID
                entry["explanation"] = "The document was changed after it was signed, in a way this signature does not allow."
            elif status.bottom_line:
                entry["status"] = STATUS_VALID
                entry["explanation"] = ("The signature is intact and the signer's certificate is trusted by this computer."
                                        + (" Later additions to the document are of a kind the signature allows."
                                           if coverage != "ENTIRE_FILE" else ""))
            else:
                entry["status"] = STATUS_UNKNOWN
                reasons = []
                if not trusted:
                    reasons.append("the signer's certificate is self-signed" if entry.get("self_signed")
                                   else "the signer's certificate does not lead to an authority this computer trusts")
                if expired:
                    reasons.append("the certificate has expired")
                entry["explanation"] = ("The signature is mathematically intact, so the signed content has not changed, but "
                                        + (" and ".join(reasons) or "the signer's identity could not be confirmed")
                                        + ". ARD cannot tell you who really signed it.")
            if not entry["has_timestamp"]:
                entry["time_note"] = "The signing time comes from the signer's own clock and is not independently proven."
            results.append(entry)

    # The PDF library's strings keep a reference to the open file. Convert everything to plain
    # values so the report can be handed back from the worker process.
    results = json.loads(json.dumps(results, default=str))
    return {
        "signatures": results, "count": len(results),
        "note": "Checked offline: certificate revocation was not checked, and a signature field with no signature "
                "in it is not a signature." if results else "This document has no cryptographic signatures. "
                "A drawn or pasted signature picture is not one.",
    }


def _load_signer(p12: bytes, passphrase: str) -> Any:
    from asn1crypto import keys, x509
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.serialization import pkcs12
    from pyhanko.sign import signers
    from pyhanko_certvalidator.registry import SimpleCertificateStore

    try:
        key, certificate, chain = pkcs12.load_key_and_certificates(p12, passphrase.encode("utf-8") if passphrase else None)
    except Exception as exc:
        raise SignError("The certificate file could not be opened. Check that it is a .p12/.pfx file and that "
                        "the passphrase is right.") from exc
    if key is None or certificate is None:
        raise SignError("The certificate file does not contain both a private key and a certificate.")
    store = SimpleCertificateStore()
    store.register_multiple([x509.Certificate.load(extra.public_bytes(serialization.Encoding.DER)) for extra in chain or []])
    return signers.SimpleSigner(
        signing_cert=x509.Certificate.load(certificate.public_bytes(serialization.Encoding.DER)),
        signing_key=keys.PrivateKeyInfo.load(key.private_bytes(
            serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())),
        cert_registry=store,
    )


def sign(source: str, output: str, params: dict[str, Any], secrets: dict[str, str]) -> dict[str, Any]:
    """Add an (invisible) signature as an incremental update, which keeps earlier signatures valid."""
    from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
    from pyhanko.pdf_utils.reader import PdfFileReader
    from pyhanko.sign import signers

    if "p12" not in secrets:
        raise SignError("The certificate for this job is no longer available. Start the operation again.")
    signer = _load_signer(base64.b64decode(secrets["p12"]), secrets.get("passphrase") or "")

    with open(source, "rb") as handle:
        data = handle.read()
    try:
        check = PdfFileReader(io.BytesIO(data), strict=False)
        if check.encrypted:
            raise SignError("This PDF is password-protected. Remove the protection before signing.")
        taken = {str(signature.field_name) for signature in check.embedded_signatures}
    except SignError:
        raise
    except Exception as exc:
        raise SignError("This PDF could not be prepared for signing.") from exc
    index = 1
    while f"ARDSignature{index}" in taken:
        index += 1
    field_name = f"ARDSignature{index}"

    part = output + ".part"
    try:
        writer = IncrementalPdfFileWriter(io.BytesIO(data), strict=False)
        metadata = signers.PdfSignatureMetadata(field_name=field_name, reason=params.get("reason") or None,
                                                location=params.get("location") or None)
        try:
            signed = signers.sign_pdf(writer, metadata, signer=signer)
        except Exception as exc:
            raise SignError(f"Signing failed ({exc.__class__.__name__}). Nothing was changed.") from exc
        with open(part, "wb") as handle:
            handle.write(signed.getvalue())

        # Check our own work before accepting it.
        report = validate({"path": part})
        ours = next((entry for entry in report["signatures"] if entry["field"] == field_name), None)
        if ours is None or not ours.get("intact") or not ours.get("cryptographically_valid"):
            raise SignError("Output check failed: the new signature did not verify. Nothing was saved.")
        if any(entry["status"] == STATUS_INVALID for entry in report["signatures"] if entry["field"] in taken):
            earlier_ok = False
        else:
            earlier_ok = True
        os.replace(part, output)
    finally:
        if os.path.exists(part):
            os.remove(part)
    return {"field": field_name, "signer": ours.get("subject"), "status": ours["status"],
            "explanation": ours.get("explanation"), "signatures_in_document": report["count"],
            "earlier_signatures_still_intact": earlier_ok,
            "note": "The signature is invisible: it is recorded in the file, not drawn on a page. "
                    "Any later change made in ARD creates a new version in which this signature no longer verifies; "
                    "the signed version itself is kept."}
