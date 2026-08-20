"""
Tests for filter_algebra — which conditions may narrow a batch, and the refusals.

The module's whole claim is one identity:

    filter(b₁ ⧺ b₂) == filter(b₁) ⧺ filter(b₂)

Every operator in the vocabulary satisfies it, which is what lets a filter live *inside*
the fold instead of in front of it. That property is asserted against real polars in
``test_frame_ops_filters.py``; what is asserted here is everything decidable without a
DataFrame library — the arity rules, the date-part ranges, and the wording.

**Why the arity rules get this much attention.** Each one prevents a filter that would
still run and narrow the set differently from what was asked. ``between`` with one value
has no second bound; a date part compared against 13 matches nothing; ``in`` with no
values matches nothing. None of those is an error a *person* makes — they are all errors
a model makes, and each produces an empty or wrong result that reads like an answer.
"""

from __future__ import annotations

import pytest

from app.services.agent_recursive_dataframes import filter_algebra as fa


class TestTheVocabularyIsRowWise:
    """
    Every operator decides one record on its own — the identity the module opens with.

    Asserted as a property of the *list* rather than of each entry: the failure this
    catches is somebody adding ``above_average`` or ``top_n`` to ``OPERATORS`` without
    reading the docstring, at which point the fold silently starts filtering each batch
    against its own average.
    """

    def test_every_operator_falls_into_exactly_one_arity_class(self) -> None:
        for operator in fa.OPERATORS:
            assert fa.needs_values(operator) in (0, 1, 2, -1)

    def test_no_operator_references_the_rest_of_the_set(self) -> None:
        """
        A set-referencing operator would have a name from this list. None of these do —
        and the point of the assertion is that adding one makes this test fail, which is
        where the docstring gets read.
        """
        set_referencing = {
            "above_average", "below_average", "top", "top_n", "rank",
            "percentile_of", "latest_per", "first_per", "in_query",
        }

        assert not (fa.OPERATORS & set_referencing)


class TestArity:
    def test_between_needs_exactly_two_values(self) -> None:
        assert fa.wrong_arity(fa.OP_BETWEEN, [1, 10]) == ""
        assert "two values" in fa.wrong_arity(fa.OP_BETWEEN, [1])
        assert "two values" in fa.wrong_arity(fa.OP_BETWEEN, [1, 2, 3])

    def test_a_comparison_needs_exactly_one_value(self) -> None:
        assert fa.wrong_arity(fa.OP_GT, [5]) == ""
        assert "exactly one value" in fa.wrong_arity(fa.OP_GT, [])
        assert "exactly one value" in fa.wrong_arity(fa.OP_GT, [1, 2])

    def test_is_null_takes_no_value(self) -> None:
        assert fa.wrong_arity(fa.OP_IS_NULL, []) == ""
        assert "takes no value" in fa.wrong_arity(fa.OP_IS_NULL, [1])

    def test_in_needs_at_least_one_value(self) -> None:
        assert fa.wrong_arity(fa.OP_IN, ["a"]) == ""
        assert fa.wrong_arity(fa.OP_IN, ["a", "b", "c"]) == ""
        assert "at least one value" in fa.wrong_arity(fa.OP_IN, [])

    def test_an_absurdly_long_in_list_is_refused(self) -> None:
        """
        Not a correctness bound — a long list is still row-wise. A model that emitted
        every matching id has answered the question instead of asking it, and the
        predicate is then slower than the query that produced the ids.
        """
        refusal = fa.wrong_arity(fa.OP_IN, list(range(fa.MAX_IN_VALUES + 1)))

        assert str(fa.MAX_IN_VALUES) in refusal.replace(",", "")


class TestDateParts:
    """
    The reason parts exist at all: a model asked for "March" must not be doing
    month-boundary arithmetic. So the part is compared as a number, and the number is
    range-checked — because month 13 matches nothing, and "no revenue in that month" is
    a sentence somebody repeats.
    """

    def test_a_month_outside_one_to_twelve_is_refused(self) -> None:
        assert fa.out_of_range_part(fa.PART_MONTH, [3]) == ""
        assert "no month 13" in fa.out_of_range_part(fa.PART_MONTH, [13])
        assert "no month 0" in fa.out_of_range_part(fa.PART_MONTH, [0])

    def test_a_quarter_outside_one_to_four_is_refused(self) -> None:
        assert fa.out_of_range_part(fa.PART_QUARTER, [4]) == ""
        assert "no quarter 5" in fa.out_of_range_part(fa.PART_QUARTER, [5])

    def test_a_month_name_is_refused_rather_than_translated(self) -> None:
        """
        ``month == "March"`` is a mistake worth naming. Translating it would mean owning
        a month-name table per locale, and a filter that silently understood "Mar" but
        not "Mrz" is worse than one that says what it wants.
        """
        refusal = fa.out_of_range_part(fa.PART_MONTH, ["March"])

        assert "whole number" in refusal
        assert "1 to 12" in refusal

    def test_any_year_is_allowed(self) -> None:
        """No range on a year: refusing 1900 or 2400 would be inventing a calendar."""
        assert fa.out_of_range_part(fa.PART_YEAR, [1900]) == ""
        assert fa.out_of_range_part(fa.PART_YEAR, [2400]) == ""

    def test_a_numeric_string_is_accepted(self) -> None:
        """A model returning "3" rather than 3 is not making a mistake about anything."""
        assert fa.out_of_range_part(fa.PART_MONTH, ["3"]) == ""

    def test_no_part_means_no_range_check(self) -> None:
        assert fa.out_of_range_part("", ["anything"]) == ""

    def test_an_invented_part_is_refused_by_name(self) -> None:
        assert fa.unsupported_part(fa.PART_MONTH) == ""
        assert fa.unsupported_part("") == ""
        assert "week" in fa.unsupported_part("week")


class TestOperators:
    def test_an_invented_operator_is_refused_and_the_real_ones_listed(self) -> None:
        refusal = fa.unsupported_operator("like")

        assert "like" in refusal
        assert fa.OP_CONTAINS in refusal

    def test_every_declared_operator_is_accepted(self) -> None:
        for operator in fa.OPERATORS:
            assert fa.unsupported_operator(operator) == ""

    def test_the_described_list_matches_the_vocabulary(self) -> None:
        """
        The prompt is built from ``describe_operators``. A described operator the code
        does not implement is a model told it may use something that will be refused;
        an implemented one left undescribed is a capability nothing will ever reach.
        """
        described = {item.strip() for item in fa.describe_operators().split(",")}

        assert described == set(fa.OPERATORS)


class TestValues:
    def test_values_wins_over_value_when_both_are_given(self) -> None:
        assert fa.values_of({"value": 1, "values": [2, 3]}) == [2, 3]

    def test_a_scalar_becomes_a_one_item_list(self) -> None:
        assert fa.values_of({"value": 7}) == [7]

    def test_nothing_at_all_is_an_empty_list(self) -> None:
        assert fa.values_of({}) == []
        assert fa.values_of({"value": None, "values": []}) == []
        assert fa.values_of({"value": "", "values": []}) == []

    def test_a_zero_is_kept_but_an_empty_string_is_not(self) -> None:
        """
        The schema types these as strings — a provider's strict ``response_format``
        validator is the reason, see ``PlannedFilter`` — so an absent value arrives as
        ``""``. A literal ``"0"`` is a real comparison and must survive that.
        """
        assert fa.values_of({"value": "0"}) == ["0"]
        assert fa.values_of({"value": ""}) == []

    def test_a_blank_inside_a_list_is_dropped(self) -> None:
        assert fa.values_of({"values": ["a", "", "b"]}) == ["a", "b"]


class TestMode:
    def test_a_plan_saying_nothing_folds(self) -> None:
        """
        The default matters for compatibility: every plan built before filters existed
        has no ``mode``, and every one of them meant "group".
        """
        assert fa.mode_of({}) == fa.MODE_GROUPS

    def test_a_rows_plan_is_read_as_rows(self) -> None:
        assert fa.mode_of({"mode": "rows"}) == fa.MODE_ROWS

    def test_nonsense_falls_back_to_folding(self) -> None:
        assert fa.mode_of({"mode": "sideways"}) == fa.MODE_GROUPS


class TestWording:
    """
    The summary sentence. It exists because a filtered figure that does not say what it
    was filtered by is the same failure as a capped list that does not say it was
    capped: right about a set the reader has to guess at.
    """

    def test_a_date_part_reads_as_a_part(self) -> None:
        described = fa.describe_filter({
            "column": "invoice_date", "part": "month",
            "operator": "==", "value": 3,
        })

        assert described == "the month of invoice_date == 3"

    def test_between_names_both_ends(self) -> None:
        described = fa.describe_filter({
            "column": "amount", "operator": "between", "values": [100, 500],
        })

        assert described == "amount is between 100 and 500"

    def test_in_lists_the_values(self) -> None:
        described = fa.describe_filter({
            "column": "region", "operator": "in", "values": ["EU", "UK"],
        })

        assert described == "region is one of EU, UK"

    def test_not_in_says_not(self) -> None:
        described = fa.describe_filter({
            "column": "region", "operator": "not_in", "values": ["EU"],
        })

        assert described == "region is not one of EU"

    def test_is_null_reads_as_english(self) -> None:
        assert fa.describe_filter(
            {"column": "closed_at", "operator": "is_null"},
        ) == "closed_at is empty"

    @pytest.mark.parametrize("operator", ["==", "!=", "<", "<=", ">", ">="])
    def test_a_comparison_shows_its_symbol(self, operator: str) -> None:
        described = fa.describe_filter(
            {"column": "amount", "operator": operator, "value": 5},
        )

        assert described == f"amount {operator} 5"

    def test_several_filters_join_with_and_not_or(self) -> None:
        """
        Conjunctive, and the sentence has to say so. A reader shown "department Python or
        March" would understand a wider set than the one that was measured.
        """
        described = fa.describe_filters([
            {"column": "department", "operator": "==", "value": "Python"},
            {"column": "d", "part": "month", "operator": "==", "value": 3},
        ])

        assert described == "department == Python and the month of d == 3"
        assert " or " not in described

    def test_no_filters_describe_as_nothing(self) -> None:
        assert fa.describe_filters([]) == ""


class TestSpecsOf:
    def test_plain_dicts_pass_through(self) -> None:
        specs = fa.specs_of({"filters": [{"column": "a", "operator": "=="}]})

        assert specs == [{"column": "a", "operator": "=="}]

    def test_pydantic_models_are_dumped(self) -> None:
        """
        A plan travels through graph state as JSON, but ``validate_plan`` returns model
        instances — so both shapes reach this module and neither may be the one that
        breaks it.
        """
        from app.schemas.agent_recursive_dataframes import PlannedFilter

        specs = fa.specs_of({
            "filters": [PlannedFilter(column="a", operator="==", value="1")],
        })

        assert specs[0]["column"] == "a"
        assert specs[0]["operator"] == "=="

    def test_no_filters_is_an_empty_list(self) -> None:
        assert fa.specs_of({}) == []
