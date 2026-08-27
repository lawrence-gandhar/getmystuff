"""
Filters in polars, and the two properties that make them safe inside a batched fold.

The first is the identity ``filter_algebra`` opens with, and it is the reason a filter may
live *inside* the pipeline rather than in front of it:

    filter(b₁ ⧺ b₂) == filter(b₁) ⧺ filter(b₂)

Asserted against SQLite rather than against a hand-written expectation — the same
standard ``test_frame_ops`` holds the fold to — because "polars and the database agree"
is a claim about the answer, while "polars matches what I typed" is a claim about my
typing.

The second is that **a filter never quietly matches nothing**. Every refusal here exists
because its alternative is an empty result, and an empty result is the one wrong answer
that reads like a right one: "there was no revenue in March" is a sentence somebody
repeats in a meeting. So a date part on a column that does not hold dates is refused with
the offending value quoted, rather than parsed loosely into nulls that match no month.

Row mode is tested here too, because it only exists through a filter: a plan with
conditions and no measures asks for the matching records themselves.
"""

from __future__ import annotations

import sqlite3
from typing import Any, List, Sequence

import pytest

from app.services.agent_recursive_dataframes import filter_algebra as fa
from app.services.agent_recursive_dataframes import frame_ops
from app.services.deep_agents.query_executor import ToolQueryError


# --- Helpers -------------------------------------------------------------


def _filter(column: str, operator: str, **kwargs: Any) -> dict:
    return {
        "column": column,
        "part": kwargs.get("part", ""),
        "operator": operator,
        "value": kwargs.get("value"),
        "values": list(kwargs.get("values") or []),
    }


def _rows_plan(*conditions: dict) -> dict:
    return {
        "mode": fa.MODE_ROWS,
        "group_by": [],
        "aggregations": [],
        "filters": list(conditions),
    }


def _groups_plan(group_by: List[str], aggregations: List[dict], *conditions) -> dict:
    return {
        "mode": fa.MODE_GROUPS,
        "group_by": group_by,
        "aggregations": aggregations,
        "filters": list(conditions),
    }


def _pipeline(plan: dict, slices: Sequence[Sequence[dict]]) -> List[dict]:
    """
    The whole pipeline over several batches: fold each, merge, finalise.

    ``keep=None`` on the merge so row mode retains everything — the truncation is the
    caller's ceiling and is tested on its own, and mixing it in here would hide a
    correctness failure behind a cap.
    """
    running = None

    for chunk in slices:
        running = frame_ops.merge_partials(
            running, [frame_ops.partial_aggregate(chunk, plan)], plan, None,
        )

    return frame_ops.finalise(running, plan, None)


def _chunked(rows: Sequence[dict], size: int) -> List[List[dict]]:
    return [list(rows[start:start + size]) for start in range(0, len(rows), size)]


def _sqlite(rows: Sequence[dict], sql: str) -> List[dict]:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        "CREATE TABLE t (department TEXT, revenue REAL, invoice_date TEXT)",
    )
    connection.executemany(
        "INSERT INTO t (department, revenue, invoice_date) "
        "VALUES (:department, :revenue, :invoice_date)",
        list(rows),
    )
    result = [dict(row) for row in connection.execute(sql)]
    connection.close()

    return result


@pytest.fixture
def ledger() -> List[dict]:
    """
    Revenue for three departments across four months, with the counts uneven.

    The unevenness is what makes a batch-boundary bug detectable: if a filter were
    applied to only the first batch, or a batch's matches were dropped, the totals would
    change — which they would not if every month held the same number of rows.
    """
    made: List[dict] = []
    plan = [
        ("Python", [(1, 100.0), (3, 250.0), (3, 75.5), (4, 300.0)]),
        ("Rust", [(2, 80.0), (3, 120.0)]),
        ("Go", [(3, 60.0), (3, 40.0), (12, 900.0)]),
    ]

    for department, entries in plan:
        for month, revenue in entries:
            made.append({
                "department": department,
                "revenue": revenue,
                "invoice_date": f"2026-{month:02d}-15",
            })

    return made


# --- The identity --------------------------------------------------------


class TestAFilterDistributesOverBatches:
    """
    The property the whole design rests on. Every assertion compares the batched
    pipeline against SQLite for the same question, at several batch sizes.
    """

    @pytest.mark.parametrize("size", [1, 2, 3, 5, 100])
    def test_a_filtered_total_matches_the_database_at_every_batch_size(
        self, ledger: List[dict], size: int,
    ) -> None:
        plan = _groups_plan(
            ["department"],
            [{"type": "sum", "column": "revenue", "alias": "sum_revenue"}],
            _filter("invoice_date", fa.OP_EQ, part="month", value=3),
        )

        got = {
            row["department"]: row["sum_revenue"]
            for row in _pipeline(plan, _chunked(ledger, size))
        }
        expected = {
            row["department"]: row["total"]
            for row in _sqlite(
                ledger,
                "SELECT department, SUM(revenue) AS total FROM t "
                "WHERE CAST(strftime('%m', invoice_date) AS INTEGER) = 3 "
                "GROUP BY department",
            )
        }

        assert got == expected

    @pytest.mark.parametrize("size", [1, 3, 100])
    def test_two_filters_are_conjunctive_and_match_the_database(
        self, ledger: List[dict], size: int,
    ) -> None:
        """
        The user's own question: one department, one month. Answered here as a total, and
        as records in the row-mode class below.
        """
        plan = _groups_plan(
            [],
            [{"type": "sum", "column": "revenue", "alias": "sum_revenue"},
             {"type": "count", "column": "", "alias": "record_count"}],
            _filter("department", fa.OP_EQ, value="Python"),
            _filter("invoice_date", fa.OP_EQ, part="month", value=3),
        )

        got = _pipeline(plan, _chunked(ledger, size))
        expected = _sqlite(
            ledger,
            "SELECT SUM(revenue) AS total, COUNT(*) AS n FROM t "
            "WHERE department = 'Python' "
            "AND CAST(strftime('%m', invoice_date) AS INTEGER) = 3",
        )

        assert got[0]["sum_revenue"] == pytest.approx(expected[0]["total"])
        assert got[0]["record_count"] == expected[0]["n"]

    def test_a_filter_matching_nothing_gives_an_empty_result_not_a_wrong_one(
        self, ledger: List[dict],
    ) -> None:
        plan = _groups_plan(
            ["department"],
            [{"type": "sum", "column": "revenue", "alias": "sum_revenue"}],
            _filter("department", fa.OP_EQ, value="Haskell"),
        )

        assert _pipeline(plan, _chunked(ledger, 2)) == []

    def test_the_filter_runs_before_the_fold_not_after_it(
        self, ledger: List[dict],
    ) -> None:
        """
        The ordering that matters. Filtering after the fold would total every month and
        then drop groups, so Python's March figure would be its whole-year figure — a
        number that looks entirely plausible.
        """
        march = _pipeline(
            _groups_plan(
                ["department"],
                [{"type": "sum", "column": "revenue", "alias": "sum_revenue"}],
                _filter("invoice_date", fa.OP_EQ, part="month", value=3),
            ),
            _chunked(ledger, 2),
        )
        everything = _pipeline(
            _groups_plan(
                ["department"],
                [{"type": "sum", "column": "revenue", "alias": "sum_revenue"}],
            ),
            _chunked(ledger, 2),
        )

        by_department = {row["department"]: row["sum_revenue"] for row in march}
        all_year = {row["department"]: row["sum_revenue"] for row in everything}

        assert by_department["Python"] == pytest.approx(325.5)
        assert all_year["Python"] == pytest.approx(725.5)


# --- Every operator ------------------------------------------------------


class TestTheOperators:
    def test_in_keeps_only_the_listed_values(self, ledger: List[dict]) -> None:
        rows = _pipeline(
            _rows_plan(_filter("department", fa.OP_IN, values=["Rust", "Go"])),
            _chunked(ledger, 2),
        )

        assert {row["department"] for row in rows} == {"Rust", "Go"}

    def test_not_in_excludes_them(self, ledger: List[dict]) -> None:
        rows = _pipeline(
            _rows_plan(_filter("department", fa.OP_NOT_IN, values=["Rust", "Go"])),
            _chunked(ledger, 2),
        )

        assert {row["department"] for row in rows} == {"Python"}

    def test_between_includes_both_ends(self) -> None:
        """
        Inclusive at both ends, which is the reading a person expects of "between 100 and
        300". Asserted on the boundaries themselves, because that is the only place an
        off-by-one shows.
        """
        rows = _pipeline(
            _rows_plan(_filter("revenue", fa.OP_BETWEEN, values=[100, 300])),
            [[
                {"revenue": 99}, {"revenue": 100}, {"revenue": 200},
                {"revenue": 300}, {"revenue": 301},
            ]],
        )

        assert [row["revenue"] for row in rows] == [100, 200, 300]

    def test_contains_is_a_literal_not_a_pattern(self) -> None:
        """
        ``literal=True``: a model writing "a.b" means those three characters, and a
        regex reading would match "axb" as well — a wider set than was asked for.
        """
        rows = _pipeline(
            _rows_plan(_filter("code", fa.OP_CONTAINS, value="a.b")),
            [[{"code": "xa.by"}, {"code": "xaxby"}]],
        )

        assert [row["code"] for row in rows] == ["xa.by"]

    def test_starts_with_anchors_at_the_start(self) -> None:
        rows = _pipeline(
            _rows_plan(_filter("code", fa.OP_STARTS_WITH, value="INV")),
            [[{"code": "INV-1"}, {"code": "X-INV-2"}]],
        )

        assert [row["code"] for row in rows] == ["INV-1"]

    def test_starts_with_works_on_a_column_a_driver_returned_as_a_number(self) -> None:
        """
        Cast rather than refused. Unlike a date parse this cannot drop a record silently:
        every non-null value has a text form.
        """
        rows = _pipeline(
            _rows_plan(_filter("reference", fa.OP_STARTS_WITH, value="20")),
            [[{"reference": 2026}, {"reference": 1999}]],
        )

        assert [row["reference"] for row in rows] == [2026]

    def test_is_null_and_is_not_null_partition_the_records(self) -> None:
        batch = [[{"closed": None}, {"closed": "yes"}, {"closed": None}]]

        empty = _pipeline(_rows_plan(_filter("closed", fa.OP_IS_NULL)), batch)
        filled = _pipeline(_rows_plan(_filter("closed", fa.OP_IS_NOT_NULL)), batch)

        assert len(empty) == 2
        assert len(filled) == 1

    def test_not_in_excludes_nulls_as_sql_does(self) -> None:
        """
        SQL's ``NOT IN`` does not return rows whose value is NULL, because NULL is in
        neither set. polars' ``is_in(...).not_()`` agrees — asserted because the obvious
        alternative, comparing ``!=`` against each value, does not.
        """
        rows = _pipeline(
            _rows_plan(_filter("d", fa.OP_NOT_IN, values=["a"])),
            [[{"d": "a"}, {"d": "b"}, {"d": None}]],
        )

        assert [row["d"] for row in rows] == ["b"]


class TestDateParts:
    """
    "In March" without any month-boundary arithmetic — which is the whole reason parts
    exist rather than ranges. February and December are where a model computing bounds
    itself gets it wrong.
    """

    def test_a_month_is_extracted_from_an_iso_text_column(
        self, ledger: List[dict],
    ) -> None:
        rows = _pipeline(
            _rows_plan(_filter("invoice_date", fa.OP_EQ, part="month", value=12)),
            _chunked(ledger, 3),
        )

        assert [row["department"] for row in rows] == ["Go"]

    def test_a_month_is_extracted_from_a_real_date_column(self) -> None:
        """
        A driver that returns ``datetime.date`` needs no parsing at all, and the branch
        that decides so is the one under test.
        """
        import datetime

        rows = _pipeline(
            _rows_plan(_filter("when", fa.OP_EQ, part="month", value=2)),
            [[
                {"when": datetime.date(2026, 2, 4)},
                {"when": datetime.date(2026, 3, 4)},
            ]],
        )

        assert len(rows) == 1

    def test_a_datetime_column_works_too(self) -> None:
        import datetime

        rows = _pipeline(
            _rows_plan(_filter("when", fa.OP_EQ, part="year", value=2025)),
            [[
                {"when": datetime.datetime(2025, 2, 4, 10, 30)},
                {"when": datetime.datetime(2026, 2, 4, 10, 30)},
            ]],
        )

        assert len(rows) == 1

    def test_a_quarter_groups_three_months(self, ledger: List[dict]) -> None:
        rows = _pipeline(
            _rows_plan(_filter("invoice_date", fa.OP_EQ, part="quarter", value=1)),
            _chunked(ledger, 4),
        )
        months = {row["invoice_date"][5:7] for row in rows}

        assert months == {"01", "02", "03"}

    def test_february_needs_no_arithmetic(self) -> None:
        """
        The case that motivated parts over ranges. A month expressed as
        ``>= 2026-02-01 AND < 2026-03-01`` is right; ``< 2026-02-30`` is not, and 30 is
        what a model reaches for. Extracting the part cannot make that mistake.
        """
        rows = _pipeline(
            _rows_plan(_filter("d", fa.OP_EQ, part="month", value=2)),
            [[
                {"d": "2026-02-01"}, {"d": "2026-02-28"},
                {"d": "2026-03-01"}, {"d": "2026-01-31"},
            ]],
        )

        assert [row["d"] for row in rows] == ["2026-02-01", "2026-02-28"]


class TestARefusalRatherThanAnEmptyResult:
    """
    Each of these would otherwise return zero records, and zero records is an answer
    somebody acts on. The refusals name the column and quote a value from the operator's
    own data.
    """

    def test_a_date_part_on_a_column_of_names_is_refused(self) -> None:
        with pytest.raises(ToolQueryError) as caught:
            _pipeline(
                _rows_plan(_filter("department", fa.OP_EQ, part="month", value=3)),
                [[{"department": "Python"}, {"department": "Rust"}]],
            )

        message = str(caught.value)

        assert "month of 'department'" in message
        assert "'Python'" in message

    def test_a_date_part_on_a_number_is_refused_by_its_type(self) -> None:
        with pytest.raises(ToolQueryError) as caught:
            _pipeline(
                _rows_plan(_filter("revenue", fa.OP_EQ, part="month", value=3)),
                [[{"revenue": 100}, {"revenue": 200}]],
            )

        assert "whole numbers rather than dates" in str(caught.value)

    def test_the_offending_value_is_quoted_not_the_first_one(self) -> None:
        """
        A column of ISO dates with one ``"n/a"`` in it fails on the ``"n/a"``. Quoting the
        first row would say "'2026-03-05' is not a date", which is false and sends the
        reader to check a column that is mostly fine.
        """
        with pytest.raises(ToolQueryError) as caught:
            _pipeline(
                _rows_plan(_filter("d", fa.OP_EQ, part="month", value=3)),
                [[{"d": "2026-03-05"}, {"d": "n/a"}, {"d": "2026-04-05"}]],
            )

        message = str(caught.value)

        assert "'n/a'" in message
        assert "2026-03-05" not in message

    def test_a_missing_column_is_refused_naming_the_condition(self) -> None:
        with pytest.raises(ToolQueryError) as caught:
            _pipeline(
                _rows_plan(_filter("nope", fa.OP_EQ, value=1)),
                [[{"department": "Python"}]],
            )

        message = str(caught.value)

        assert "nope == 1" in message
        assert "department" in message

    def test_comparing_text_against_a_number_is_refused_readably(self) -> None:
        """
        Caught while coercing, before polars sees it — so the message names the column and
        the value rather than quoting a dtype mismatch.
        """
        with pytest.raises(ToolQueryError) as caught:
            _pipeline(
                _rows_plan(_filter("revenue", fa.OP_GT, value="lots")),
                [[{"revenue": 100}]],
            )

        message = str(caught.value)

        assert "revenue" in message
        assert "lots" in message


class TestTheValueIsTypedAgainstTheColumn:
    """
    Every filter value arrives as a **string**, and this is where it becomes the type the
    column holds.

    It is a string because a plan is an LLM's structured output: the schema goes to the
    provider as ``response_format``, and a strict validator refuses both ``Any`` (which
    renders as an empty schema) and a union (``anyOf``). Cerebras rejected every planning
    call the moment filters existed, which surfaced as the agent saying it could not filter
    by month — the same apology the feature exists to remove, one layer in.

    So the typing happens against the frame's own dtype, which turns out to be the better
    place for it: ``"1000"`` on an integer column used to be a polars type error and is now
    a comparison against 1000, while ``"lots"`` is a refusal that can name both.
    """

    @pytest.mark.parametrize("given", ["1000", " 1000 "])
    def test_a_numeric_string_compares_as_a_number(self, given: str) -> None:
        rows = _pipeline(
            _rows_plan(_filter("revenue", fa.OP_GTE, value=given)),
            [[{"revenue": 999}, {"revenue": 1000}, {"revenue": 1001}]],
        )

        assert [row["revenue"] for row in rows] == [1000, 1001]

    def test_a_decimal_string_compares_against_a_float_column(self) -> None:
        rows = _pipeline(
            _rows_plan(_filter("revenue", fa.OP_GT, value="100.5")),
            [[{"revenue": 100.4}, {"revenue": 100.6}]],
        )

        assert [row["revenue"] for row in rows] == [pytest.approx(100.6)]

    @pytest.mark.parametrize(
        ("given", "kept"),
        [("true", True), ("TRUE", True), ("yes", True), ("1", True),
         ("false", False), ("no", False), ("0", False)],
    )
    def test_the_words_a_model_writes_for_true_and_false_are_understood(
        self, given: str, kept: bool,
    ) -> None:
        """
        Parsed rather than left to truthiness. ``bool("false")`` is ``True``, which would
        select the opposite of what was asked — silently, over the whole result.
        """
        rows = _pipeline(
            _rows_plan(_filter("closed", fa.OP_EQ, value=given)),
            [[{"closed": True}, {"closed": False}]],
        )

        assert [row["closed"] for row in rows] == [kept]

    def test_neither_true_nor_false_is_refused(self) -> None:
        with pytest.raises(ToolQueryError) as caught:
            _pipeline(
                _rows_plan(_filter("closed", fa.OP_EQ, value="maybe")),
                [[{"closed": True}]],
            )

        message = str(caught.value)

        assert "closed" in message
        assert "maybe" in message

    def test_an_iso_date_compares_against_a_real_date_column(self) -> None:
        import datetime

        rows = _pipeline(
            _rows_plan(_filter("when", fa.OP_GTE, value="2026-03-01")),
            [[
                {"when": datetime.date(2026, 2, 28)},
                {"when": datetime.date(2026, 3, 1)},
            ]],
        )

        assert len(rows) == 1

    def test_a_date_range_on_a_date_column_works_through_between(self) -> None:
        import datetime

        rows = _pipeline(
            _rows_plan(_filter(
                "when", fa.OP_BETWEEN, values=["2026-03-01", "2026-03-31"],
            )),
            [[
                {"when": datetime.date(2026, 2, 28)},
                {"when": datetime.date(2026, 3, 15)},
                {"when": datetime.date(2026, 4, 1)},
            ]],
        )

        assert [row["when"] for row in rows] == [datetime.date(2026, 3, 15)]

    def test_an_ambiguous_date_is_refused_rather_than_guessed(self) -> None:
        """
        ISO only, and only for a value a *model* wrote. ``01/08/2026`` has two readings and
        picking one here would be inventing a boundary — unlike a column the operator's
        driver returned, where there is existing data to accommodate.
        """
        import datetime

        with pytest.raises(ToolQueryError) as caught:
            _pipeline(
                _rows_plan(_filter("when", fa.OP_EQ, value="01/08/2026")),
                [[{"when": datetime.date(2026, 8, 1)}]],
            )

        message = str(caught.value)

        assert "YYYY-MM-DD" in message
        assert "month" in message  # the alternative it points at

    def test_a_numeric_string_is_kept_as_text_for_a_text_column(self) -> None:
        """A reference like "2026" in a text column is text, not a number."""
        rows = _pipeline(
            _rows_plan(_filter("reference", fa.OP_EQ, value="2026")),
            [[{"reference": "2026"}, {"reference": "2025"}]],
        )

        assert [row["reference"] for row in rows] == ["2026"]

    def test_a_month_is_a_number_whatever_the_column_holds(self) -> None:
        """
        The part branch runs before the dtype branch, because the month of anything is
        1–12 — the column's own type says nothing about it.
        """
        rows = _pipeline(
            _rows_plan(_filter("d", fa.OP_EQ, part="month", value="3")),
            [[{"d": "2026-03-05"}, {"d": "2026-04-05"}]],
        )

        assert len(rows) == 1

    def test_a_non_numeric_month_is_refused(self) -> None:
        with pytest.raises(ToolQueryError) as caught:
            _pipeline(
                _rows_plan(_filter("d", fa.OP_EQ, part="month", value="March")),
                [[{"d": "2026-03-05"}]],
            )

        assert "whole numbers" in str(caught.value)

    def test_a_column_of_only_nulls_is_compared_as_text_rather_than_crashing(self) -> None:
        """
        Every value in this batch is missing, so polars infers ``Null`` and there is no
        dtype to coerce against. Ordinary, not drift — and it must not raise.
        """
        rows = _pipeline(
            _rows_plan(_filter("note", fa.OP_EQ, value="anything")),
            [[{"note": None}, {"note": None}]],
        )

        assert rows == []

    @pytest.mark.parametrize(
        "operator", [fa.OP_NE, fa.OP_LT, fa.OP_LTE, fa.OP_GT, fa.OP_GTE],
    )
    def test_every_comparison_operator_coerces_its_value(self, operator: str) -> None:
        """
        Each branch of the operator dispatch, so a coercion added to one and forgotten in
        another shows up here rather than as a type error in a conversation.
        """
        rows = _pipeline(
            _rows_plan(_filter("revenue", operator, value="100")),
            [[{"revenue": 50}, {"revenue": 100}, {"revenue": 150}]],
        )

        assert rows  # every one of these matches at least one of the three


class TestRowMode:
    def test_the_records_come_back_rather_than_numbers(
        self, ledger: List[dict],
    ) -> None:
        rows = _pipeline(
            _rows_plan(_filter("department", fa.OP_EQ, value="Rust")),
            _chunked(ledger, 2),
        )

        assert len(rows) == 2
        assert set(rows[0]) == {"department", "revenue", "invoice_date"}

    def test_read_order_is_preserved(self, ledger: List[dict]) -> None:
        """
        Not sorted. "The first two hundred" only means something if the order is the
        query's, and re-sorting would answer a different question from the one the count
        beside it describes.
        """
        rows = _pipeline(
            _rows_plan(_filter("department", fa.OP_EQ, value="Python")),
            _chunked(ledger, 1),
        )

        assert [row["revenue"] for row in rows] == [100.0, 250.0, 75.5, 300.0]

    def test_an_empty_batch_does_not_end_the_run(self, ledger: List[dict]) -> None:
        """
        A batch where nothing matched returns an *empty frame*, not ``None`` — ``None``
        is how the reader says the cursor is exhausted, and confusing the two would stop
        a run at the first batch with no match in it.
        """
        plan = _rows_plan(_filter("department", fa.OP_EQ, value="Go"))
        batches = _chunked(ledger, 2)

        partial = frame_ops.partial_aggregate(batches[0], plan)

        assert partial is not None
        assert partial.height == 0
        assert len(_pipeline(plan, batches)) == 3

    def test_a_genuinely_empty_batch_is_still_none(self) -> None:
        assert frame_ops.partial_aggregate([], _rows_plan()) is None

    def test_the_retained_rows_are_capped_but_the_order_is_kept(self) -> None:
        """
        ``keep`` bounds what travels back to be shown, and the merge applies it. The rows
        kept are the *first* ones, which is what makes the count beside them meaningful.
        """
        plan = _rows_plan(_filter("n", fa.OP_GT, value=0))
        running = None

        for chunk in _chunked([{"n": index} for index in range(1, 21)], 5):
            running = frame_ops.merge_partials(
                running, [frame_ops.partial_aggregate(chunk, plan)], plan, 6,
            )

        rows = frame_ops.finalise(running, plan, None)

        assert [row["n"] for row in rows] == [1, 2, 3, 4, 5, 6]


class TestModeIsReadFromThePlan:
    def test_a_plan_with_measures_still_folds(self, ledger: List[dict]) -> None:
        """
        The compatibility case. A plan built before filters existed carries no ``mode``
        and must fold exactly as it did.
        """
        legacy = {
            "group_by": ["department"],
            "aggregations": [{"type": "count", "column": "", "alias": "record_count"}],
        }

        rows = _pipeline(legacy, _chunked(ledger, 3))

        assert {row["department"] for row in rows} == {"Python", "Rust", "Go"}
        assert sum(row["record_count"] for row in rows) == len(ledger)
