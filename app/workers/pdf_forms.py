"""AcroForm fields: list, fill (runs in worker processes).

Supports the classic AcroForm field types: text, checkbox, radio group and
dropdown/list. XFA forms (an older Adobe XML format) are detected and
reported as unsupported. Signature fields are listed but never filled here.

Appearance: after a value is set, the field needs a new picture of itself.
ARD tries to generate one; if a form's fonts make that impossible it stores
the value and sets the standard NeedAppearances flag, which asks the PDF
viewer to draw the field. The report says which happened.
"""

from __future__ import annotations

import json
from typing import Any

import pikepdf
from pikepdf import Name, Pdf, String
from pikepdf.form import (
    CheckboxField, ChoiceField, DefaultAppearanceStreamGenerator, ExtendedAppearanceStreamGenerator, Form,
    RadioButtonGroup, SignatureField, TextField,
)

MAX_FIELDS = 5000


class FormError(Exception):
    pass


def _has_xfa(pdf: Pdf) -> bool:
    acroform = pdf.Root.get("/AcroForm")
    return acroform is not None and acroform.get("/XFA") is not None


def _kind(field: Any) -> str:
    if isinstance(field, TextField):
        return "multiline" if field.is_multiline else "text"
    if isinstance(field, CheckboxField):
        return "checkbox"
    if isinstance(field, RadioButtonGroup):
        return "radio"
    if isinstance(field, ChoiceField):
        return "choice"
    if isinstance(field, SignatureField):
        return "signature"
    return "other"


def _plain(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, Name):
        return str(value).lstrip("/")
    if isinstance(value, (list, tuple, pikepdf.Array)):
        return [_plain(item) for item in value]
    return str(value)


def _raw_fields(pdf: Pdf) -> dict[str, Any]:
    try:
        return {field.fully_qualified_name: field for field in pdf.acroform.fields}
    except Exception:
        return {}


def _describe(name: str, field: Any, raw: dict[str, Any]) -> dict[str, Any] | None:
    kind = _kind(field)
    if kind == "other":
        return None                       # push buttons and proxies carry no value
    entry: dict[str, Any] = {
        "name": name, "type": kind,
        "required": bool(getattr(field, "is_required", False)),
        "read_only": bool(getattr(field, "is_read_only", False)),
        "label": "",
    }
    try:
        entry["label"] = raw[name].alternate_name or ""
    except Exception:
        pass
    if kind in ("text", "multiline"):
        secret = bool(field.is_password)
        entry["password"] = secret
        entry["value"] = "" if secret else (field.value or "")      # never echo a password field
        entry["max_length"] = field.max_length
    elif kind == "checkbox":
        entry["value"] = bool(field.checked)
    elif kind == "radio":
        entry["options"] = [str(option.on_value).lstrip("/") for option in field.options]
        value = _plain(field.value)
        entry["value"] = value if value in entry["options"] else ""
        entry["can_clear"] = bool(field.can_toggle_off)
    elif kind == "choice":
        entry["options"] = [str(option.display_value) for option in field.options]
        entry["editable"] = bool(field.allow_edit)
        entry["multiple"] = bool(field.is_multiselect)
        value = _plain(field.value)
        entry["value"] = (value[0] if value else "") if isinstance(value, list) else (value or "")
    elif kind == "signature":
        entry["value"] = None
        entry["read_only"] = True
    return entry


def list_fields(payload: dict[str, Any]) -> dict[str, Any]:
    """Describe the form in a PDF. Read-only."""
    try:
        pdf = Pdf.open(payload["path"])
    except pikepdf.PasswordError:
        return {"fields": [], "has_form": False, "xfa": False, "locked": True}
    with pdf:
        if pdf.Root.get("/AcroForm") is None:
            return {"fields": [], "has_form": False, "xfa": False}
        raw = _raw_fields(pdf)
        fields = []
        try:
            for name, field in Form(pdf).items():
                described = _describe(name, field, raw)
                if described:
                    fields.append(described)
                if len(fields) >= MAX_FIELDS:
                    break
        except Exception as exc:
            return {"fields": [], "has_form": True, "xfa": _has_xfa(pdf), "unreadable": True,
                    "problem": exc.__class__.__name__}
        return {"fields": fields, "has_form": bool(fields), "xfa": _has_xfa(pdf)}


def _set(form: Form, field: Any, kind: str, value: Any, name: str) -> None:
    if kind in ("text", "multiline"):
        if not isinstance(value, str):
            raise FormError(f"“{name}” needs text.")
        limit = field.max_length
        if limit and len(value) > limit:
            raise FormError(f"“{name}” allows at most {limit} characters.")
        field.value = value
    elif kind == "checkbox":
        if not isinstance(value, bool):
            raise FormError(f"“{name}” is a checkbox: use true or false.")
        field.checked = value
    elif kind == "radio":
        options = {str(option.on_value).lstrip("/"): option for option in field.options}
        if value in ("", None):
            if not field.can_toggle_off:
                raise FormError(f"“{name}” cannot be left without a choice.")
            field.value = Name.Off
        elif value in options:
            options[value].select()
        else:
            raise FormError(f"“{name}” has no option “{value}”.")
    elif kind == "choice":
        if not isinstance(value, str):
            raise FormError(f"“{name}” needs one of its options.")
        options = [str(option.display_value) for option in field.options]
        if value and value not in options and not field.allow_edit:
            raise FormError(f"“{name}” has no option “{value}”.")
        field.value = value
    else:
        raise FormError(f"“{name}” cannot be filled in here.")


def fill(pdf: Pdf, params: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Set field values. Returns (expected page count, report)."""
    with open(params["values_path"], encoding="utf-8") as handle:
        values: dict[str, Any] = json.load(handle)
    if pdf.Root.get("/AcroForm") is None:
        raise FormError("This document has no form fields.")
    if _has_xfa(pdf):
        raise FormError("This is an XFA form, which ARD cannot fill in. Only standard AcroForm fields are supported.")

    filled, by_viewer = 0, 0
    # Each generator is tried in turn; a form whose fonts defeat both still gets its values.
    generators = (ExtendedAppearanceStreamGenerator, DefaultAppearanceStreamGenerator)
    forms = [Form(pdf, generator) for generator in generators]
    known = dict(forms[0].items())
    unknown = sorted(set(values) - set(known))
    if unknown:
        raise FormError("Unknown field" + ("s" if len(unknown) > 1 else "") + ": " + ", ".join(unknown[:5]))

    raw_fields = _raw_fields(pdf)
    skipped = 0
    for name, value in values.items():
        kind = _kind(known[name])
        if kind in ("signature", "other"):
            raise FormError(f"“{name}” cannot be filled in here.")
        # A value that is already what the field holds is left alone. This lets a full export be
        # imported back, read-only fields and unanswered radio groups included.
        current = (_describe(name, known[name], raw_fields) or {}).get("value")
        if value == current or (value in ("", None) and current in ("", None)):
            skipped += 1
            continue
        if getattr(known[name], "is_read_only", False):
            raise FormError(f"“{name}” is read-only.")
        done = False
        for form in forms:
            field = dict(form.items())[name]
            try:
                _set(form, field, kind, value, name)
                done = True
                break
            except FormError:
                raise
            except Exception:
                continue              # appearance generation failed; try the simpler generator
        if not done:
            raw = _raw_fields(pdf).get(name)
            if raw is None or kind not in ("text", "multiline", "choice"):
                raise FormError(f"“{name}” could not be filled in.")
            raw.set_value(String(str(value)), True)      # True: ask the viewer to draw it (NeedAppearances)
            by_viewer += 1
        filled += 1

    if not filled:
        raise FormError("Nothing to save: every value is the same as in the document.")
    report: dict[str, Any] = {"filled": filled, "unchanged": skipped, "fields_drawn_by_viewer": by_viewer}
    if by_viewer:
        report["note"] = (f"{by_viewer} field{'s' if by_viewer != 1 else ''} use fonts ARD could not draw with, so the "
                          "document asks the PDF viewer to draw them. Most viewers do; check before sending.")
    return len(pdf.pages), report
