"""
Tests for ``app.utils.type_coercion``.

The property this module exists to hold: **nothing is guessed.** A value that does
not honour its declared type is refused, never quietly defaulted. A record written
into somebody's CRM with a silently-zeroed amount is a wrong record with nothing in
the log to find it by, which is strictly worse than a record that failed.

``coerce_to_url_and_body`` is tested separately because it is the pre-existing
chatbot-action behaviour and its wording is asserted by that feature's own tests;
this file pins it so a change to the general path cannot alter it by accident.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from app.utils.type_coercion import (
    TYPES,
    coerce_to_url_and_body,
    coerce_value,
    describe_type,
)


class TestNothingIsGuessed:
    """The headline property, stated as the failures it prevents."""

    @pytest.mark.parametrize(
        ("value", "target"),
        [
            ("abc", "number"),
            ("", "number"),
            ("   ", "integer"),
            ("10.5", "integer"),
            ("maybe", "boolean"),
            ("not-a-date", "date"),
            ("2026-13-45", "datetime"),
            ("not json", "json"),
        ],
    )
    def test_a_value_that_does_not_fit_is_refused(self, value: str, target: str) -> None:
        with pytest.raises(ValueError):
            coerce_value(value, target)

    def test_a_fraction_is_never_truncated_to_an_integer(self) -> None:
        """Truncating 10.5 to 10 loses half a unit of whatever this is, silently."""
        with pytest.raises(ValueError, match="whole number"):
            coerce_value("10.5", "integer")
        assert coerce_value("10.0", "integer") == 10

    def test_a_boolean_is_not_a_quantity(self) -> None:
        """
        ``bool`` is a subclass of ``int``, so ``float(True)`` is 1.0. Almost
        certainly a mapping mistake rather than an intent, so it is refused rather
        than becoming a quantity of one.
        """
        with pytest.raises(ValueError, match="true/false"):
            coerce_value(True, "number")
        with pytest.raises(ValueError, match="true/false"):
            coerce_value(False, "integer")

    def test_a_json_scalar_is_not_a_json_object(self) -> None:
        assert coerce_value('{"a": 1}', "json") == {"a": 1}
        assert coerce_value("[1, 2]", "json") == [1, 2]
        with pytest.raises(ValueError, match="JSON object or array"):
            coerce_value('"just a string"', "json")


class TestAbsentIsNotMalformed:
    @pytest.mark.parametrize("target", TYPES)
    def test_none_passes_through_at_every_type(self, target: str) -> None:
        """
        Required-ness is a separate rule that runs first. Conflating them reports
        an optional field that was simply not sent as a type error.
        """
        assert coerce_value(None, target) is None


class TestConversions:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [("1", 1.0), (" 2.5 ", 2.5), (3, 3.0), (4.5, 4.5), ("-0.25", -0.25)],
    )
    def test_number(self, value: object, expected: float) -> None:
        assert coerce_value(value, "number") == expected

    @pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes", "y", "on", True, 1])
    def test_truthy(self, value: object) -> None:
        assert coerce_value(value, "boolean") is True

    @pytest.mark.parametrize("value", ["false", "FALSE", "0", "no", "n", "off", False, 0])
    def test_falsy(self, value: object) -> None:
        assert coerce_value(value, "boolean") is False

    def test_date_keeps_only_the_date(self) -> None:
        assert coerce_value("2026-08-13T14:30:00", "date") == "2026-08-13"
        assert coerce_value(date(2026, 8, 13), "date") == "2026-08-13"

    def test_datetime_accepts_a_trailing_z(self) -> None:
        """
        ``Z`` is legal ISO 8601 and ``fromisoformat`` did not accept it before
        Python 3.11 — and every vendor API in scope emits it.
        """
        assert coerce_value("2026-08-13T14:30:00Z", "datetime").startswith("2026-08-13T14:30:00")
        assert coerce_value(datetime(2026, 8, 13, 14, 30), "datetime") == "2026-08-13T14:30:00"

    def test_a_dict_becomes_json_not_a_python_repr(self) -> None:
        """
        ``str({"a": True})`` gives ``{'a': True}`` — single quotes, capital True —
        which is not JSON and not what any API accepts.
        """
        assert coerce_value({"a": True}, "string") == '{"a": true}'
        assert coerce_value([1, 2], "string") == "[1, 2]"

    def test_a_bool_stringifies_as_json(self) -> None:
        assert coerce_value(True, "string") == "true"

    def test_an_unknown_type_is_refused_by_name(self) -> None:
        with pytest.raises(ValueError, match="unknown type 'currency'"):
            coerce_value("5", "currency")


class TestDescribeType:
    @pytest.mark.parametrize("target", TYPES)
    def test_every_type_has_a_human_phrase(self, target: str) -> None:
        phrase = describe_type(target)
        assert phrase and phrase != target or target == "string"


class TestChatbotActionForm:
    """
    ``coerce_to_url_and_body`` is the pre-existing behaviour, unchanged. A string
    is JSON-escaped *without* its surrounding quotes, so a template writes
    ``"{{param.id}}"`` with quotes and ``{{param.qty}}`` bare.
    """

    def test_a_string_is_escaped_without_quotes(self) -> None:
        assert coerce_to_url_and_body('say "hi"', "string") == ('say "hi"', 'say \\"hi\\"')

    def test_a_newline_is_escaped_for_the_body(self) -> None:
        url_text, body = coerce_to_url_and_body("a\nb", "string")
        assert url_text == "a\nb"
        assert body == "a\\nb"

    def test_a_number_is_bare_in_both(self) -> None:
        assert coerce_to_url_and_body(" 42 ", "number") == ("42", "42")

    def test_a_boolean_is_lowercased(self) -> None:
        assert coerce_to_url_and_body("TRUE", "boolean") == ("true", "true")

    def test_the_refusal_still_blames_the_ai(self) -> None:
        """This path only ever coerces a value a model produced, and the wording
        is asserted by the chatbot action tests."""
        with pytest.raises(ValueError, match="expected a number but the AI supplied"):
            coerce_to_url_and_body("abc", "number")
        with pytest.raises(ValueError, match="expected true or false but the AI supplied"):
            coerce_to_url_and_body("maybe", "boolean")
