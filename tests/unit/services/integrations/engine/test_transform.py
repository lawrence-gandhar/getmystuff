"""
Tests for ``engine/transform.py``.

The property the table exists to hold: **nothing is guessed.** A transform that cannot
do its job raises, and the record is recorded as failed with the reason. It never falls
back to the original value and never substitutes a default — a record written into
somebody's CRM with a silently-zeroed amount is a wrong record with nothing in the log
to find it by.

The other property is that it is a *closed* table. There is no expression evaluator
here, and the last class asserts that the palette, the validator and the runner all read
the same list — because the moment they do not, the canvas offers a transform that fails
at three in the morning.
"""

from __future__ import annotations

import pytest

from app.services.integrations.engine.transform import (
    TRANSFORM_NAMES,
    TRANSFORMS,
    apply_all,
    apply_transform,
    describe_transforms,
    is_known,
)


class TestAbsentIsNotMalformed:
    @pytest.mark.parametrize("name", TRANSFORM_NAMES)
    def test_none_passes_through_every_transform(self, name: str) -> None:
        """
        Required-ness is a separate rule that runs first. Uppercasing a field that was
        simply not sent must not produce ``"NONE"``, and must not raise either.
        """
        assert apply_transform(name, None) is None


class TestNothingIsGuessed:
    @pytest.mark.parametrize(
        ("name", "value"),
        [
            ("to_number", "abc"),
            ("to_integer", "10.5"),
            ("to_boolean", "maybe"),
            ("to_date", "not-a-date"),
            ("to_datetime", "2026-13-45"),
            ("json_decode", "not json"),
        ],
    )
    def test_a_value_that_does_not_fit_is_refused(self, name: str, value: str) -> None:
        with pytest.raises(ValueError):
            apply_transform(name, value)

    def test_a_fraction_is_never_truncated(self) -> None:
        """Truncating 10.5 to 10 loses half a unit of whatever this is, silently."""
        with pytest.raises(ValueError):
            apply_transform("to_integer", "10.5")

        assert apply_transform("to_integer", "10.0") == 10

    def test_a_failure_does_not_fall_back_to_the_original(self) -> None:
        """
        The whole argument, as one assertion: there is no return path from a failed
        transform, so the value cannot arrive at the destination unconverted.
        """
        with pytest.raises(ValueError):
            apply_transform("to_number", "twelve pounds")


class TestTextTransforms:
    def test_trim(self) -> None:
        assert apply_transform("trim", "  hello  ") == "hello"

    def test_collapse_whitespace(self) -> None:
        assert apply_transform("collapse_whitespace", " a \n\t b  c ") == "a b c"

    def test_case(self) -> None:
        assert apply_transform("lower", "HeLLo") == "hello"
        assert apply_transform("upper", "HeLLo") == "HELLO"
        assert apply_transform("title", "jane doe") == "Jane Doe"

    def test_digits_only_keeps_a_leading_zero(self) -> None:
        """
        Not a phone parser. It has no idea which country this is from, and a parser
        that guesses is one that silently drops the zero.
        """
        assert apply_transform("digits_only", "+44 (0)20 7946 0958") == "440207946095" + "8"

    def test_a_dict_stringifies_as_json_not_a_python_repr(self) -> None:
        """
        ``str({"a": True})`` gives ``{'a': True}`` — single quotes, capital True —
        which is not JSON and not what any API accepts.
        """
        assert apply_transform("to_string", {"a": True}) == '{"a": true}'


class TestTypeTransforms:
    def test_to_number(self) -> None:
        assert apply_transform("to_number", " 2.5 ") == 2.5

    def test_to_boolean(self) -> None:
        assert apply_transform("to_boolean", "YES") is True
        assert apply_transform("to_boolean", "off") is False

    def test_to_date_keeps_only_the_date(self) -> None:
        assert apply_transform("to_date", "2026-08-14T14:30:00") == "2026-08-14"

    def test_to_datetime_accepts_a_trailing_z(self) -> None:
        """Legal ISO 8601, and every vendor API in scope emits it."""
        assert apply_transform("to_datetime", "2026-08-14T14:30:00Z").startswith(
            "2026-08-14T14:30:00"
        )

    def test_json_round_trip(self) -> None:
        encoded = apply_transform("json_encode", {"b": 1, "a": 2})
        assert apply_transform("json_decode", encoded) == {"b": 1, "a": 2}


class TestListTransforms:
    def test_first_and_last(self) -> None:
        assert apply_transform("first", ["a", "b", "c"]) == "a"
        assert apply_transform("last", ["a", "b", "c"]) == "c"

    def test_an_empty_list_yields_nothing_rather_than_raising(self) -> None:
        """
        "This record had no addresses" is a fact about the record, not a fault in the
        mapping — and required-ness is decided elsewhere.
        """
        assert apply_transform("first", []) is None

    def test_a_scalar_passes_through(self) -> None:
        """``a[*].email`` yields a list of one for most records and a bare value for
        some sources; both have to reach the same destination field."""
        assert apply_transform("first", "a@b.com") == "a@b.com"


class TestUnknownTransforms:
    def test_the_refusal_names_the_alternatives(self) -> None:
        """
        "There is no transform called 'uppercase'" is considerably less useful without
        the list that contains ``upper``.
        """
        with pytest.raises(ValueError) as caught:
            apply_transform("uppercase", "x")

        message = str(caught.value)
        assert "uppercase" in message
        assert "upper" in message

    def test_is_known_is_what_the_validator_asks(self) -> None:
        assert is_known("trim")
        assert not is_known("eval")


class TestChaining:
    def test_transforms_run_left_to_right(self) -> None:
        assert apply_all(["trim", "lower"], "  HELLO  ") == "hello"

    def test_order_is_the_users_and_it_matters(self) -> None:
        """``digits_only`` then ``to_integer`` works; the reverse does not."""
        assert apply_all(["digits_only", "to_integer"], "no. 0042") == 42

        with pytest.raises(ValueError):
            apply_all(["to_integer", "digits_only"], "no. 0042")

    @pytest.mark.parametrize("names", [None, [], ()])
    def test_no_transforms_is_the_identity(self, names: object) -> None:
        assert apply_all(names, "unchanged") == "unchanged"


class TestTheTableIsTheOnlySource:
    def test_the_palette_is_built_from_the_table(self) -> None:
        """
        One list, so the canvas cannot offer a transform the runner does not have. A
        second hand-written list is exactly how that stops being true.
        """
        described = {entry["name"] for entry in describe_transforms()}

        assert described == set(TRANSFORMS)

    def test_every_transform_has_a_label_and_a_sentence(self) -> None:
        for entry in describe_transforms():
            assert entry["label"].strip()
            assert entry["description"].strip()

    def test_there_is_no_expression_transform(self) -> None:
        """
        The posture, pinned. Anything with an evaluator in it is a remote code
        execution waiting for the first person who pastes something clever into a text
        box, and this is a form field a language model also writes into.
        """
        forbidden = {"eval", "expression", "expr", "python", "script", "template", "exec"}

        assert not (forbidden & set(TRANSFORM_NAMES))
