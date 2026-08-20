"""
Tests for ``mapping/record_filter.py``.

Three properties beyond the operator table itself.

**A missing field is not a false comparison.** A record without a shipping address is not
"a record whose shipping country is not GB" — it is a record the question does not apply
to. Both answers happen to be "does not match"; only one of them is a reason, and
``is_null`` is where the difference becomes visible.

**Comparing across types never raises.** One system stores a quantity as text and another
as an integer, and a run that dies on record 4,000 of 50,000 with a ``TypeError`` is worse
than one that compares them as numbers.

**No conditions keeps everything.** A filter step somebody has not finished configuring
must pass records through rather than silently discard the batch — dropping everything
looks exactly like a source that returned nothing, and the run reports success either way.
"""

from __future__ import annotations

import pytest

from app.services.integrations.errors import NodeFailure
from app.services.integrations.mapping import record_filter

ORDER = {
    "id": 17,
    "total": "125.50",
    "status": "paid",
    "customer": {"email": "Ada@Example.com", "country": "GB"},
    "tags": ["vip", "wholesale"],
    "cancelled_at": None,
}


def spec(column: str, operator: str, *values, **extra) -> dict:
    built = {"column": column, "operator": operator, "values": list(values)}
    built.update(extra)
    return built


class TestTheOperators:
    @pytest.mark.parametrize(
        "condition,expected",
        [
            (spec("status", "==", "paid"), True),
            (spec("status", "==", "refunded"), False),
            (spec("status", "!=", "refunded"), True),
            (spec("id", ">", 10), True),
            (spec("id", ">", 17), False),
            (spec("id", ">=", 17), True),
            (spec("id", "<", 20), True),
            (spec("id", "<=", 17), True),
            (spec("status", "in", "paid", "pending"), True),
            (spec("status", "not_in", "paid", "pending"), False),
            (spec("customer.email", "contains", "example"), True),
            (spec("customer.email", "starts_with", "ada"), True),
            (spec("total", "between", 100, 200), True),
            (spec("total", "between", 200, 300), False),
            (spec("cancelled_at", "is_null"), True),
            (spec("cancelled_at", "is_not_null"), False),
            (spec("status", "is_not_null"), True),
        ],
    )
    def test_the_table(self, condition, expected) -> None:  # noqa: ANN001
        assert record_filter.matches(ORDER, condition) is expected

    def test_a_nested_path_is_read_the_same_way_a_mapping_reads_it(self) -> None:
        """One path grammar for the whole module — the filter and the field mapping cannot
        disagree about what ``customer.email`` means."""
        assert record_filter.matches(ORDER, spec("customer.country", "==", "GB")) is True

    def test_between_is_inclusive_at_both_ends(self) -> None:
        """``filter_algebra``'s own documented reading, and what a person means by
        "between 1,000 and 5,000"."""
        assert record_filter.matches({"n": 5}, spec("n", "between", 5, 10)) is True
        assert record_filter.matches({"n": 10}, spec("n", "between", 5, 10)) is True

    def test_contains_and_starts_with_ignore_case(self) -> None:
        assert record_filter.matches(ORDER, spec("status", "contains", "PAID")) is True
        assert record_filter.matches(ORDER, spec("status", "starts_with", "PA")) is True


class TestAMissingFieldIsNotAFalseComparison:
    def test_an_absent_field_matches_nothing_that_asks_about_a_value(self) -> None:
        assert record_filter.matches(ORDER, spec("shipping.country", "==", "GB")) is False
        assert record_filter.matches(ORDER, spec("shipping.country", "!=", "GB")) is False

    def test_is_null_is_how_the_question_is_actually_asked(self) -> None:
        """
        Both comparisons above answer "does not match", and only one of them is a reason.
        ``is_null`` is the operator that distinguishes them, which is why it is checked
        before the ``None`` guard rather than after.
        """
        assert record_filter.matches(ORDER, spec("shipping.country", "is_null")) is True


class TestComparingAcrossTypes:
    def test_text_and_a_number_compare_as_numbers(self) -> None:
        """``"12" > 5`` is what one system storing a quantity as text looks like. The
        alternative to coercing is a run that dies on record 4,000 of 50,000."""
        assert record_filter.matches({"qty": "12"}, spec("qty", ">", 5)) is True

    def test_two_things_that_are_not_numbers_compare_as_text(self) -> None:
        assert record_filter.matches({"code": "b"}, spec("code", ">", "a")) is True

    def test_a_numeric_id_matches_the_same_id_typed_as_text(self) -> None:
        """The operator typed ``17`` into a form and the API sent an integer."""
        assert record_filter.matches(ORDER, spec("id", "in", "17", "18")) is True

    def test_nothing_raises_on_a_mismatched_shape(self) -> None:
        assert record_filter.matches({"a": {"b": 1}}, spec("a", "==", "x")) is False


class TestPartitioning:
    def test_a_batch_splits_into_kept_and_dropped(self) -> None:
        records = [{"status": "paid"}, {"status": "refunded"}, {"status": "paid"}]

        kept, dropped = record_filter.partition(records, [spec("status", "==", "paid")])

        assert len(kept) == 2
        assert len(dropped) == 1

    def test_all_means_every_condition(self) -> None:
        records = [{"a": 1, "b": 1}, {"a": 1, "b": 2}]
        kept, dropped = record_filter.partition(
            records, [spec("a", "==", 1), spec("b", "==", 1)]
        )
        assert len(kept) == 1 and len(dropped) == 1

    def test_any_means_one_is_enough(self) -> None:
        records = [{"a": 1, "b": 9}, {"a": 9, "b": 9}]
        kept, dropped = record_filter.partition(
            records,
            [spec("a", "==", 1), spec("b", "==", 1)],
            mode=record_filter.MATCH_ANY,
        )
        assert len(kept) == 1 and len(dropped) == 1

    def test_no_conditions_keeps_everything(self) -> None:
        """
        A half-configured filter must pass records through. Dropping everything looks
        exactly like a source that returned nothing, and the run reports success either
        way — which is how a sync silently stops moving data.
        """
        kept, dropped = record_filter.partition([{"a": 1}, {"a": 2}], [])
        assert len(kept) == 2 and dropped == []

    def test_order_is_preserved_within_each_half(self) -> None:
        records = [{"n": index} for index in range(6)]
        kept, dropped = record_filter.partition(records, [spec("n", "<", 3)])

        assert [row["n"] for row in kept] == [0, 1, 2]
        assert [row["n"] for row in dropped] == [3, 4, 5]


class TestRefusals:
    def test_an_unknown_operator_names_the_real_ones(self) -> None:
        """Refused at save time by ``validate_flow``; reaching this means a version
        published before the rule existed, or a row edited by hand. It produces a readable
        failed step rather than a ``KeyError``."""
        with pytest.raises(NodeFailure):
            record_filter.matches(ORDER, spec("status", "matches", "paid"))

    def test_the_wrong_number_of_values_is_refused(self) -> None:
        with pytest.raises(NodeFailure):
            record_filter.matches(ORDER, spec("total", "between", 100))

    def test_an_unknown_match_mode_is_refused(self) -> None:
        with pytest.raises(NodeFailure, match="way of combining"):
            record_filter.partition([{"a": 1}], [spec("a", "==", 1)], mode="most")
