"""Tests for app/utils/validators.py — the shared input-validation rules."""

from __future__ import annotations

import uuid as uuid_pkg

import pytest
from litestar.exceptions import HTTPException

from app.utils import validators


class TestRequireText:
    def test_trims_surrounding_whitespace(self) -> None:
        assert validators.require_text("  Sales  ", "Name", 50) == "Sales"

    @pytest.mark.parametrize("value", [None, "", "   ", "\t\n"])
    def test_rejects_blank(self, value) -> None:
        with pytest.raises(HTTPException) as exc:
            validators.require_text(value, "Name", 50)
        assert exc.value.status_code == 400
        assert exc.value.detail == "Name is required"

    def test_accepts_value_exactly_at_the_limit(self) -> None:
        assert validators.require_text("a" * 10, "Name", 10) == "a" * 10

    def test_rejects_value_over_the_limit(self) -> None:
        with pytest.raises(HTTPException) as exc:
            validators.require_text("a" * 11, "Name", 10)
        assert exc.value.detail == "Name cannot be longer than 10 characters"

    def test_length_is_measured_after_trimming(self) -> None:
        """Padding must not push an otherwise valid value over the limit."""
        assert validators.require_text("   abc   ", "Name", 3) == "abc"


class TestOptionalText:
    @pytest.mark.parametrize("value", [None, "", "   "])
    def test_blank_becomes_none(self, value) -> None:
        """A cleared textarea must null the column, not store an empty string."""
        assert validators.optional_text(value, "Description", 50) is None

    def test_trims_a_present_value(self) -> None:
        assert validators.optional_text("  hi  ", "Description", 50) == "hi"

    def test_rejects_value_over_the_limit(self) -> None:
        with pytest.raises(HTTPException) as exc:
            validators.optional_text("a" * 51, "Description", 50)
        assert exc.value.detail == "Description cannot be longer than 50 characters"


class TestRequireIdentifier:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("total_units", "total_units"),
            ("TOTAL_UNITS", "total_units"),
            ("Total_Units_2", "total_units_2"),
            ("a", "a"),
        ],
    )
    def test_accepts_and_lowercases(self, raw, expected) -> None:
        assert validators.require_identifier(raw, "Tool name") == expected

    @pytest.mark.parametrize(
        "raw",
        [
            "1total",       # must start with a letter
            "_total",       # must start with a letter
            "total-units",  # dash not allowed
            "total units",  # space not allowed
            "total.units",  # dot not allowed
            "totàl",        # non-ASCII
            "total;drop",   # SQL punctuation
        ],
    )
    def test_rejects_non_identifiers(self, raw) -> None:
        with pytest.raises(HTTPException) as exc:
            validators.require_identifier(raw, "Tool name")
        assert exc.value.status_code == 400
        assert "must start with a letter" in exc.value.detail

    def test_blank_reports_required_not_format(self) -> None:
        with pytest.raises(HTTPException) as exc:
            validators.require_identifier("", "Tool name")
        assert exc.value.detail == "Tool name is required"


class TestRequireObjectName:
    @pytest.mark.parametrize(
        "raw",
        ["users", "sales_data.csv", "Order Items", "table-1", "A1", "_leading_underscore"],
    )
    def test_accepts_real_object_names(self, raw) -> None:
        assert validators.require_object_name(raw, "Table") == raw.strip()

    @pytest.mark.parametrize(
        "raw",
        [
            'users"; DROP TABLE users; --',
            "users`",
            "users'",
            "users(1)",
            "-leading-dash",
            ".leading-dot",
        ],
    )
    def test_rejects_names_that_could_break_out_of_an_identifier(self, raw) -> None:
        with pytest.raises(HTTPException):
            validators.require_object_name(raw, "Table")

    def test_leading_whitespace_is_trimmed_not_rejected(self) -> None:
        assert validators.require_object_name("  users", "Table") == "users"

    def test_rejects_unicode_homoglyphs(self) -> None:
        """The pattern spells out [A-Za-z0-9_] rather than \\w for exactly this."""
        with pytest.raises(HTTPException):
            validators.require_object_name("uѕers", "Table")  # Cyrillic 's'

    @pytest.mark.parametrize("value", [None, "", "   "])
    def test_rejects_blank(self, value) -> None:
        with pytest.raises(HTTPException) as exc:
            validators.require_object_name(value, "Table")
        assert exc.value.detail == "Table is required"

    def test_rejects_over_255_characters(self) -> None:
        with pytest.raises(HTTPException) as exc:
            validators.require_object_name("a" * 256, "Table")
        assert exc.value.detail == "Table cannot be longer than 255 characters"

    def test_invalid_name_is_quoted_back_to_the_user(self) -> None:
        with pytest.raises(HTTPException) as exc:
            validators.require_object_name("bad(name)", "Table")
        assert "'bad(name)'" in exc.value.detail


class TestUuidParsing:
    def test_require_uuid_parses(self) -> None:
        value = uuid_pkg.uuid4()
        assert validators.require_uuid(str(value), "Workspace") == value

    @pytest.mark.parametrize("value", [None, "", "   "])
    def test_require_uuid_rejects_blank(self, value) -> None:
        with pytest.raises(HTTPException) as exc:
            validators.require_uuid(value, "Workspace")
        assert exc.value.detail == "Workspace is required"

    def test_require_uuid_rejects_garbage(self) -> None:
        with pytest.raises(HTTPException) as exc:
            validators.require_uuid("not-a-uuid", "Workspace")
        assert exc.value.detail == "Workspace is not a valid selection"

    @pytest.mark.parametrize("value", [None, "", "   "])
    def test_optional_uuid_blank_is_none(self, value) -> None:
        """An unselected <option value=""> means 'nothing chosen'."""
        assert validators.parse_optional_uuid(value, "Workspace") is None

    def test_optional_uuid_rejects_garbage(self) -> None:
        with pytest.raises(HTTPException) as exc:
            validators.parse_optional_uuid("12345", "Workspace")
        assert exc.value.detail == "Workspace is not a valid selection"

    def test_optional_uuid_accepts_unhyphenated_form(self) -> None:
        value = uuid_pkg.uuid4()
        assert validators.parse_optional_uuid(value.hex, "Workspace") == value


class TestParseJsonObject:
    @pytest.mark.parametrize("value", [None, "", "   "])
    def test_blank_is_empty_dict(self, value) -> None:
        assert validators.parse_json_object(value, "Query") == {}

    def test_parses_an_object(self) -> None:
        assert validators.parse_json_object('{"a": 1}', "Query") == {"a": 1}

    def test_rejects_malformed_json(self) -> None:
        """
        The sentence names the field and says what to do, without naming a page. It used
        to end "rebuild the query below" — wording written for the tool config builder,
        which was its first caller — and this validator is now shared with forms that have
        no query below them. Matches ``schemas/base._json_array``, which has always said
        this.
        """
        with pytest.raises(HTTPException) as exc:
            validators.parse_json_object("{not json", "Query")
        assert exc.value.detail == "Query could not be read — please rebuild it and try again"

    @pytest.mark.parametrize("value", ["[1, 2]", '"a string"', "42", "true", "null"])
    def test_rejects_valid_json_that_is_not_an_object(self, value) -> None:
        with pytest.raises(HTTPException) as exc:
            validators.parse_json_object(value, "Query")
        assert exc.value.detail == "Query is not in the expected format"
