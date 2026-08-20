"""
The polars implementation, checked against the rules it is supposed to implement.

The claim under test is narrow and total: folding slices and merging them gives
the same numbers as aggregating the whole batch at once, and the same numbers
SQLite gives. Everything else here is a refusal that has to name the column.
"""

import sqlite3
from typing import Any, Dict, List, Sequence

import polars as pl
import pytest

from app.services.agent_recursive_dataframes import frame_ops
from app.services.agent_recursive_dataframes import partial_algebra as algebra
from app.services.deep_agents.query_executor import ToolQueryError

# --- Helpers -------------------------------------------------------------


def _plan(group_by: List[str], aggregations: List[dict]) -> dict:
    return {"group_by": group_by, "aggregations": aggregations}


def _run(plan: dict, slices: Sequence[Sequence[dict]], limit: int = 1000) -> List[dict]:
    """The whole pipeline: fold each slice, merge them all, finalise."""
    running = None

    for chunk in slices:
        running = frame_ops.merge_partials(
            running, [frame_ops.partial_aggregate(chunk, plan)], plan,
        )

    return frame_ops.finalise(running, plan, limit)


def _chunked(rows: Sequence[dict], size: int) -> List[List[dict]]:
    return [list(rows[start:start + size]) for start in range(0, len(rows), size)]


def _sqlite(rows: Sequence[dict], sql: str) -> List[dict]:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("CREATE TABLE t (region TEXT, amount REAL, sold_on TEXT)")
    connection.executemany(
        "INSERT INTO t (region, amount, sold_on) VALUES (:region, :amount, :sold_on)",
        list(rows),
    )
    result = [dict(row) for row in connection.execute(sql)]
    connection.close()

    return result


@pytest.fixture
def rows() -> List[dict]:
    """Skewed groups, missing values spread across them, and one all-null group."""
    made: List[dict] = [
        {
            "region": ["north", "south", "east"][index % 3],
            "amount": None if index % 4 == 0 else float(index),
            "sold_on": f"2026-01-{(index % 28) + 1:02d}",
        }
        for index in range(1, 61)
    ]
    made.extend(
        {"region": "west", "amount": None, "sold_on": "2026-02-01"} for _ in range(4)
    )

    return made


@pytest.fixture
def full_plan() -> dict:
    return _plan(
        ["region"],
        [
            {"type": "count", "column": "", "alias": "record_count"},
            {"type": "count", "column": "amount", "alias": "count_amount"},
            {"type": "sum", "column": "amount", "alias": "sum_amount"},
            {"type": "avg", "column": "amount", "alias": "avg_amount"},
            {"type": "min", "column": "amount", "alias": "min_amount"},
            {"type": "max", "column": "amount", "alias": "max_amount"},
        ],
    )


# --- The claim -----------------------------------------------------------


class TestFoldAndMergeAreExact:
    @pytest.mark.parametrize("size", [1, 2, 7, 13, 200])
    def test_every_slice_size_matches_sqlite(
        self, rows: List[dict], full_plan: dict, size: int,
    ) -> None:
        expected = _sqlite(
            rows,
            """
            SELECT region,
                   COUNT(*)      AS record_count,
                   COUNT(amount) AS count_amount,
                   SUM(amount)   AS sum_amount,
                   AVG(amount)   AS avg_amount,
                   MIN(amount)   AS min_amount,
                   MAX(amount)   AS max_amount
              FROM t GROUP BY region
            """,
        )
        by_region = {row["region"]: row for row in expected}
        result = {row["region"]: row for row in _run(full_plan, _chunked(rows, size))}

        assert set(result) == set(by_region)

        for region, actual in result.items():
            for column, value in by_region[region].items():
                if isinstance(value, float):
                    assert actual[column] == pytest.approx(value), (region, column)
                else:
                    assert actual[column] == value, (region, column)

    def test_one_slice_equals_many_slices(
        self, rows: List[dict], full_plan: dict,
    ) -> None:
        assert _run(full_plan, [rows]) == _run(full_plan, _chunked(rows, 6))

    def test_a_wave_of_several_partials_merges_in_one_call(
        self, rows: List[dict], full_plan: dict,
    ) -> None:
        """merge_partials takes the whole wave at once — the graph's actual shape."""
        wave = [frame_ops.partial_aggregate(c, full_plan) for c in _chunked(rows, 8)]
        merged = frame_ops.merge_partials(None, wave, full_plan)

        assert frame_ops.finalise(merged, full_plan, 1000) == _run(full_plan, [rows])

    def test_slice_order_does_not_matter(
        self, rows: List[dict], full_plan: dict,
    ) -> None:
        forward = _chunked(rows, 9)

        assert _run(full_plan, list(reversed(forward))) == _run(full_plan, forward)

    def test_polars_finalisation_matches_the_reference_rules(
        self, rows: List[dict], full_plan: dict,
    ) -> None:
        """
        The one place the rules are written twice — once in partial_algebra as
        prose-shaped Python, once in frame_ops as polars expressions, because
        sorting has to happen on finalised values inside polars. This asserts they
        agree, which is what makes the duplication a checked invariant rather than
        two chances to be wrong.
        """
        merged = None
        for chunk in _chunked(rows, 11):
            merged = frame_ops.merge_partials(
                merged, [frame_ops.partial_aggregate(chunk, full_plan)], full_plan,
            )

        reference = sorted(
            (algebra.finalise_row(full_plan, row) for row in merged.to_dicts()),
            key=lambda row: row["region"],
        )
        actual = sorted(
            frame_ops.finalise(merged, full_plan, 1000), key=lambda row: row["region"],
        )

        assert actual == pytest.approx(reference)


class TestNullRules:
    def test_sum_of_an_all_null_group_is_none_not_zero(
        self, rows: List[dict], full_plan: dict,
    ) -> None:
        west = {r["region"]: r for r in _run(full_plan, _chunked(rows, 5))}["west"]

        assert west["sum_amount"] is None
        assert west["avg_amount"] is None
        assert west["min_amount"] is None
        assert west["record_count"] == 4
        assert west["count_amount"] == 0

    def test_null_is_its_own_group(self) -> None:
        plan = _plan(["region"], [{"type": "count", "column": "", "alias": "n"}])
        result = _run(plan, [
            [{"region": None}, {"region": "null"}],
            [{"region": None}, {"region": "north"}],
        ])

        assert sorted((r["region"] or "<null>", r["n"]) for r in result) == [
            ("<null>", 2), ("north", 1), ("null", 1),
        ]

    def test_a_slice_where_the_column_is_entirely_null_is_not_drift(self) -> None:
        """A batch that happens to hold no value at all must fold, not fail."""
        plan = _plan(["region"], [{"type": "sum", "column": "amount", "alias": "s"}])
        result = _run(plan, [
            [{"region": "n", "amount": None}, {"region": "n", "amount": None}],
            [{"region": "n", "amount": 5.0}],
        ])

        assert result == [{"region": "n", "s": 5.0}]

    def test_integer_and_float_slices_of_the_same_column_merge(self) -> None:
        """Inference drift, not data drift: promote rather than refuse."""
        plan = _plan(["region"], [{"type": "sum", "column": "amount", "alias": "s"}])
        result = _run(plan, [
            [{"region": "n", "amount": 2}],       # infers Int64
            [{"region": "n", "amount": 0.5}],     # infers Float64
        ])

        assert result[0]["s"] == pytest.approx(2.5)


class TestGroupingAndOrdering:
    def test_a_plan_with_no_group_by_gives_one_total_row(
        self, rows: List[dict], full_plan: dict,
    ) -> None:
        plan = _plan([], [{"type": "sum", "column": "amount", "alias": "s"}])
        result = _run(plan, _chunked(rows, 7))

        expected = _sqlite(rows, "SELECT SUM(amount) AS s FROM t")

        assert len(result) == 1
        assert result[0]["s"] == pytest.approx(expected[0]["s"])

    def test_several_group_keys_are_combined(self, rows: List[dict]) -> None:
        plan = _plan(
            ["region", "sold_on"], [{"type": "count", "column": "", "alias": "n"}],
        )
        result = _run(plan, _chunked(rows, 5))
        expected = _sqlite(
            rows,
            "SELECT region, sold_on, COUNT(*) AS n FROM t GROUP BY region, sold_on",
        )

        assert sorted(map(tuple, (r.values() for r in result))) == sorted(
            map(tuple, (r.values() for r in expected))
        )

    def test_the_result_is_sorted_by_the_first_aggregation_descending(
        self, rows: List[dict], full_plan: dict,
    ) -> None:
        result = _run(full_plan, _chunked(rows, 6))
        counts = [row["record_count"] for row in result]

        assert counts == sorted(counts, reverse=True)

    def test_the_cap_is_applied_after_sorting_so_it_keeps_the_largest(
        self, rows: List[dict], full_plan: dict,
    ) -> None:
        everything = _run(full_plan, _chunked(rows, 6))
        capped = _run(full_plan, _chunked(rows, 6), limit=2)

        assert capped == everything[:2]

    def test_groups_whose_measure_is_null_sort_last(
        self, rows: List[dict],
    ) -> None:
        plan = _plan(["region"], [{"type": "sum", "column": "amount", "alias": "s"}])
        result = _run(plan, _chunked(rows, 6))

        assert result[-1]["region"] == "west"
        assert result[-1]["s"] is None

    def test_the_same_data_gives_byte_identical_results_across_runs(
        self, rows: List[dict], full_plan: dict,
    ) -> None:
        assert _run(full_plan, _chunked(rows, 4)) == _run(full_plan, _chunked(rows, 4))


class TestGroupCount:
    def test_counts_the_groups_not_the_records(
        self, rows: List[dict], full_plan: dict,
    ) -> None:
        merged = frame_ops.merge_partials(
            None,
            [frame_ops.partial_aggregate(c, full_plan) for c in _chunked(rows, 9)],
            full_plan,
        )

        assert frame_ops.group_count(merged) == 4
        assert frame_ops.group_count(None) == 0


# --- Refusals ------------------------------------------------------------


class TestRefusals:
    def test_an_unknown_group_column_names_what_the_tool_does_return(self) -> None:
        plan = _plan(["nope"], [{"type": "count", "column": "", "alias": "n"}])

        with pytest.raises(ToolQueryError) as caught:
            frame_ops.partial_aggregate([{"region": "n", "amount": 1.0}], plan)

        assert "nope" in str(caught.value)
        assert "region" in str(caught.value) and "amount" in str(caught.value)

    def test_an_unknown_aggregation_column_is_refused(self) -> None:
        plan = _plan([], [{"type": "sum", "column": "nope", "alias": "s"}])

        with pytest.raises(ToolQueryError, match="nope"):
            frame_ops.partial_aggregate([{"amount": 1.0}], plan)

    def test_grouping_by_a_decimal_number_column_is_refused(self) -> None:
        plan = _plan(["amount"], [{"type": "count", "column": "", "alias": "n"}])

        with pytest.raises(ToolQueryError) as caught:
            frame_ops.partial_aggregate([{"amount": 1.5}, {"amount": 2.5}], plan)

        assert "amount" in str(caught.value)
        assert "group" in str(caught.value)

    def test_summing_text_is_refused_and_never_coerced(self) -> None:
        plan = _plan([], [{"type": "sum", "column": "name", "alias": "s"}])

        with pytest.raises(ToolQueryError) as caught:
            frame_ops.partial_aggregate([{"name": "a"}, {"name": "b"}], plan)

        assert "name" in str(caught.value)
        assert "text" in str(caught.value)

    def test_drift_to_text_is_caught_at_the_batch_that_introduces_it(self) -> None:
        """The whole reason the check runs on every slice, not just the first."""
        plan = _plan(["region"], [{"type": "sum", "column": "amount", "alias": "s"}])

        assert frame_ops.partial_aggregate(
            [{"region": "n", "amount": 1.0}], plan,
        ) is not None

        with pytest.raises(ToolQueryError, match="amount"):
            frame_ops.partial_aggregate([{"region": "n", "amount": "oops"}], plan)

    def test_averaging_dates_is_refused(self) -> None:
        import datetime

        plan = _plan([], [{"type": "avg", "column": "d", "alias": "a"}])

        with pytest.raises(ToolQueryError, match="dates"):
            frame_ops.partial_aggregate([{"d": datetime.date(2026, 1, 1)}], plan)

    def test_min_and_max_of_dates_are_allowed(self) -> None:
        import datetime

        plan = _plan([], [{"type": "min", "column": "d", "alias": "earliest"}])
        result = _run(plan, [
            [{"d": datetime.date(2026, 3, 1)}],
            [{"d": datetime.date(2026, 1, 9)}],
        ])

        assert result == [{"earliest": datetime.date(2026, 1, 9)}]

    def test_min_and_max_of_text_are_allowed(self) -> None:
        plan = _plan([], [{"type": "max", "column": "name", "alias": "last"}])

        assert _run(plan, [[{"name": "alpha"}], [{"name": "zulu"}]]) == [
            {"last": "zulu"},
        ]


class TestEmptyInput:
    def test_an_empty_slice_folds_to_nothing(self, full_plan: dict) -> None:
        assert frame_ops.partial_aggregate([], full_plan) is None

    def test_merging_nothing_leaves_the_running_aggregate_alone(
        self, rows: List[dict], full_plan: dict,
    ) -> None:
        running = frame_ops.partial_aggregate(rows[:10], full_plan)

        assert frame_ops.merge_partials(running, [None], full_plan) is running

    def test_finalising_nothing_gives_no_rows(self, full_plan: dict) -> None:
        assert frame_ops.finalise(None, full_plan, 200) == []


class TestDecimals:
    def test_a_decimal_column_totals_without_becoming_approximate(self) -> None:
        from decimal import Decimal

        plan = _plan(["region"], [{"type": "sum", "column": "amount", "alias": "s"}])
        result = _run(plan, [
            [{"region": "n", "amount": Decimal("0.10")}],
            [{"region": "n", "amount": Decimal("0.20")}],
        ])

        # The float answer would be 0.30000000000000004; money must not do that.
        assert result[0]["s"] == Decimal("0.30")

    def test_averaging_a_decimal_column_yields_a_float(self) -> None:
        from decimal import Decimal

        plan = _plan([], [{"type": "avg", "column": "amount", "alias": "a"}])
        result = _run(plan, [[{"amount": Decimal("1.00")}, {"amount": Decimal("2.00")}]])

        assert isinstance(result[0]["a"], float)
        assert result[0]["a"] == pytest.approx(1.5)


class TestOneImportSite:
    def test_polars_is_imported_only_by_frame_ops(self) -> None:
        """
        A second import site is a second chance to import the extension first on a
        worker thread — the hazard parquet_writer.py documents. Asserted rather
        than trusted, because it is the sort of thing a later edit undoes quietly.
        """
        import pathlib

        root = pathlib.Path(frame_ops.__file__).parents[2]
        importers = sorted(
            path.relative_to(root).as_posix()
            for path in root.rglob("*.py")
            if "polars" in path.read_text()
            and any(
                line.strip().startswith(("import polars", "from polars"))
                for line in path.read_text().splitlines()
            )
        )

        assert importers == ["services/agent_recursive_dataframes/frame_ops.py"]
