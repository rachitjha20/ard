import pytest

from app.core.logging import redact
from app.services.documents.service import _describe_pages, clean_display_name, parse_pdf_date
from app.services.pdf.operations import OperationError, parse_page_ranges, plan_operation, plan_split


def test_parse_page_ranges():
    assert parse_page_ranges("1-3, 5, 8-", 10) == [1, 2, 3, 5, 8, 9, 10]
    assert parse_page_ranges("-2", 10) == [1, 2]
    for bad in ("0", "11", "3-1", "abc", "1-x"):
        with pytest.raises(OperationError):
            parse_page_ranges(bad, 10)


def test_delete_cannot_remove_every_page():
    assert plan_operation("delete_pages", {"pages": [2]}, 3).params == {"order": [1, 3]}
    with pytest.raises(OperationError):
        plan_operation("delete_pages", {"pages": [1, 2, 3]}, 3)


def test_reorder_must_be_a_permutation():
    assert plan_operation("reorder", {"order": [3, 1, 2]}, 3).params["order"] == [3, 1, 2]
    for bad in ([1, 2], [1, 1, 2], [1, 2, 4]):
        with pytest.raises(OperationError):
            plan_operation("reorder", {"order": bad}, 3)


def test_duplicate_reverse_rotate():
    assert plan_operation("duplicate_pages", {"pages": [2]}, 3).params["order"] == [1, 2, 2, 3]
    assert plan_operation("reverse", {}, 3).params["order"] == [3, 2, 1]
    assert plan_operation("rotate", {"degrees": -90}, 2).params == {"pages": [1, 2], "degrees": 270}
    for bad in (45, 360, "x"):
        with pytest.raises(OperationError):
            plan_operation("rotate", {"degrees": bad}, 2)


def test_bad_input_is_rejected():
    for operation, params in [
        ("delete_pages", {"pages": [True]}), ("delete_pages", {"pages": "nope"}), ("delete_pages", {}),
        ("crop", {}), ("crop", {"left": -5}), ("insert_blank", {"after": 9}), ("insert_blank", {"count": 9999}),
        ("compress", {"preset": "ultra"}), ("insert_pages", {}), ("format_disk", {}),
    ]:
        with pytest.raises(OperationError):
            plan_operation(operation, params, 3)


def test_plan_split():
    assert plan_split({"mode": "every", "n": 2}, 5) == [[1, 2], [3, 4], [5]]
    assert plan_split({"mode": "ranges", "ranges": "1-2, 4"}, 5) == [[1, 2], [4]]
    assert plan_split({"mode": "pages", "pages": [2, 3]}, 5) == [[2, 3]]
    with pytest.raises(OperationError):
        plan_split({"mode": "every", "n": 1}, 5, max_parts=3)
    with pytest.raises(OperationError):
        plan_split({"mode": "ranges", "ranges": ""}, 5)


def test_display_names_are_sanitised():
    assert clean_display_name("../../etc/passwd.pdf") == "passwd"
    assert clean_display_name("C:\\Users\\me\\evil\x00name.PDF") == "evilname"
    assert clean_display_name("") == "Untitled"
    assert clean_display_name("  रिपोर्ट 2024.pdf ") == "रिपोर्ट 2024"
    assert len(clean_display_name("a" * 500 + ".pdf")) == 200


def test_pdf_dates():
    assert parse_pdf_date("D:20240131120000+05'30'") == "2024-01-31T12:00:00+05:30"
    assert parse_pdf_date("D:20240131120000Z") == "2024-01-31T12:00:00Z"
    assert parse_pdf_date("D:2024") == "2024-01-01T00:00:00"
    assert parse_pdf_date("yesterday") is None
    assert parse_pdf_date("D:20241345") is None


def test_describe_pages():
    assert _describe_pages([1, 2, 3, 7]) == "1-3, 7"
    assert _describe_pages([4]) == "4"


def test_log_redaction():
    assert "abc123secret" not in redact("Authorization: Bearer abc123secretTOKEN")
    assert "hunter22" not in redact('password="hunter22"')
    assert "sk-abcdef" in redact("sk-abcdefGHIJKLMNOPQRSTUV") and "GHIJKLMNOP" not in redact("sk-abcdefGHIJKLMNOPQRSTUV")
    assert redact("page 3 of 10") == "page 3 of 10"
