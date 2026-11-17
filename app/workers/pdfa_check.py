"""PDF/A: what a file claims, obvious obstacles, and validation if veraPDF is installed.

ARD does not implement PDF/A validation itself. Without veraPDF this module
reports only (a) whether the file *claims* to be PDF/A and (b) features that
are certain to break conformance. Passing those checks is not conformance.
"""

from __future__ import annotations

import subprocess
from typing import Any

import pikepdf
from pikepdf import Pdf


def _fonts_not_embedded(pdf: Pdf, limit: int = 200) -> list[str]:
    missing: set[str] = set()
    seen: set[tuple[int, int]] = set()
    for page in pdf.pages[:limit]:
        resources = page.obj.get("/Resources")
        fonts = resources.get("/Font") if resources is not None else None
        if fonts is None:
            continue
        for _name, font in fonts.items():
            try:
                if font.is_indirect:
                    if font.objgen in seen:
                        continue
                    seen.add(font.objgen)
                target = font
                if str(font.get("/Subtype", "")) == "/Type0":
                    target = font.DescendantFonts[0]
                if str(target.get("/Subtype", "")) == "/Type3":
                    continue
                descriptor = target.get("/FontDescriptor")
                embedded = descriptor is not None and any(key in descriptor for key in ("/FontFile", "/FontFile2", "/FontFile3"))
                if not embedded:
                    missing.add(str(font.get("/BaseFont", "unnamed")).lstrip("/"))
            except Exception:
                continue
    return sorted(missing)


def check(payload: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {"claim": None, "obstacles": [], "validated": False, "verdict": "Not validated",
                              "validator": None}
    try:
        pdf = Pdf.open(payload["path"])
    except pikepdf.PasswordError:
        result["obstacles"].append("The document is encrypted; PDF/A does not allow encryption.")
        result["locked"] = True
        return result
    with pdf:
        # Looked at first: opening the metadata below creates an XMP packet in memory.
        has_xmp = pdf.Root.get("/Metadata") is not None
        try:
            with pdf.open_metadata(set_pikepdf_as_editor=False, update_docinfo=False) as metadata:
                part, conformance = metadata.get("pdfaid:part"), metadata.get("pdfaid:conformance")
            if part:
                result["claim"] = f"PDF/A-{part}{str(conformance or '').lower()}"
        except Exception:
            pass
        root = pdf.Root
        if pdf.is_encrypted:
            result["obstacles"].append("The document is encrypted; PDF/A does not allow encryption.")
        if not has_xmp:
            result["obstacles"].append("There is no XMP metadata, which PDF/A requires.")
        names = root.get("/Names")
        if names is not None and "/JavaScript" in names:
            result["obstacles"].append("The document contains JavaScript, which PDF/A forbids.")
        try:
            if len(pdf.attachments) and not (result["claim"] or "").startswith("PDF/A-3"):
                result["obstacles"].append("The document has file attachments, allowed only in PDF/A-3.")
        except Exception:
            pass
        if root.get("/OutputIntents") is None:
            result["obstacles"].append("There is no output intent (colour profile); most PDF/A files need one.")
        fonts = _fonts_not_embedded(pdf)
        if fonts:
            shown = ", ".join(fonts[:6]) + ("…" if len(fonts) > 6 else "")
            result["obstacles"].append(f"Fonts are not embedded ({shown}); PDF/A requires every font to be embedded.")

    executable = payload.get("verapdf")
    if executable:
        result["validator"] = "veraPDF"
        try:
            run = subprocess.run([executable, "--format", "text", payload["path"]], capture_output=True, text=True,
                                 timeout=180, stdin=subprocess.DEVNULL)
            lines = [line.strip() for line in run.stdout.splitlines() if line.strip()]
            verdict = next((line.split()[0] for line in lines if line.split()[0] in ("PASS", "FAIL")), None)
            if verdict:
                result["validated"] = True
                result["verdict"] = "Conforms" if verdict == "PASS" else "Does not conform"
            else:
                result["verdict"] = "Validator output not understood"
            result["validator_output"] = "\n".join(lines[:8])[:800]
        except (OSError, subprocess.SubprocessError):
            result["verdict"] = "Validator failed to run"
    elif result["obstacles"]:
        result["verdict"] = "Cannot be PDF/A as it is"
    elif result["claim"]:
        result["verdict"] = "Claims PDF/A, not validated"
    result["note"] = ("Validated with veraPDF." if result["validated"] else
                      "ARD does not validate PDF/A by itself. This shows what the file claims and problems that are "
                      "certain to break conformance; the absence of problems here is not conformance. "
                      "Install veraPDF for real validation.")
    return result
