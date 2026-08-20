"""
The correctness argument, tested without a database, polars or a graph.

Two claims carry this module and everything else is detail:

* folding a sequence of slices and finalising once equals aggregating the whole
  thing in one pass — asserted against **SQLite**, not against a Python
  re-implementation, because the promise is that the answer matches what the
  database would have said;
* the aggregations that cannot be folded exactly are refused rather than
  approximated.
"""

import sqlite3
from typing import Any, Dict, List, Sequence

import pytest

from app.services.agent_recursive_dataframes import partial_algebra as algebra

# --- Helpers -------------------------------------------------------------


def _plan(group_by: List[str], aggregations: List[dict]) -> dict:
    return {"group_by": group_by, "aggregations": aggregations}


def _fold_slice(plan: dict, rows: Sequence[dict]) -> Dict[tuple, Dict[str, Any]]:
    """
    A deliberately naive, obviously-correct implementation of one slice's partial
    aggregate, following :func:`partial_algebra.carried_fields` and nothing else.

    Written out longhand here rather than imported from ``frame_ops``: this module
    tests the *rules*, and checking them against the polars implementation would
    only prove the two agree, not that either is right.
    """
    groups: Dict[tuple, Dict[str, Any]] = {}

    for row in rows:
        key = tuple(row.get(column) for column in plan["group_by"])
        carried = groups.setdefault(key, {})

        for field in algebra.plan_carried_fields(plan):
            value = row.get(field.column) if field.column else None

            if field.kind == algebra.KIND_ROWS:
                carried[field.name] = (carried.get(field.name) or 0) + 1
                continue

            if value is None:
                carried.setdefault(field.name, None)
                continue

            if field.kind == algebra.KIND_NON_NULL:
                carried[field.name] = (carried.get(field.name) or 0) + 1
            elif field.kind == algebra.KIND_SUM:
                carried[field.name] = (carried.get(field.name) or 0) + value
            elif field.kind == algebra.KIND_MIN:
                seen = carried.get(field.name)
                carried[field.name] = value if seen is None else min(seen, value)
            elif field.kind == algebra.KIND_MAX:
                seen = carried.get(field.name)
                carried[field.name] = value if seen is None else max(seen, value)

    return groups


def _merge(
    plan: dict,
    left: Dict[tuple, Dict[str, Any]],
    right: Dict[tuple, Dict[str, Any]],
) -> Dict[tuple, Dict[str, Any]]:
    """Fold ``right`` into ``left`` using each field's declared merge."""
    merged = {key: dict(carried) for key, carried in left.items()}

    for key, carried in right.items():
        target = merged.setdefault(key, {})

        for field in algebra.plan_carried_fields(plan):
            incoming, existing = carried.get(field.name), target.get(field.name)

            if incoming is None:
                target.setdefault(field.name, existing)
            elif existing is None:
                target[field.name] = incoming
            elif field.merge == "sum":
                target[field.name] = existing + incoming
            elif field.merge == "min":
                target[field.name] = min(existing, incoming)
            else:
                target[field.name] = max(existing, incoming)

    return merged


def _run(plan: dict, slices: Sequence[Sequence[dict]]) -> List[dict]:
    """Fold every slice, merge them all, finalise, and sort for comparison."""
    merged: Dict[tuple, Dict[str, Any]] = {}

    for chunk in slices:
        merged = _merge(plan, merged, _fold_slice(plan, chunk))

    rows = []
    for key, carried in merged.items():
        carried = dict(carried)
        carried.update(dict(zip(plan["group_by"], key)))
        rows.append(algebra.finalise_row(plan, carried))

    return sorted(rows, key=lambda row: tuple(str(row[c]) for c in plan["group_by"]))


def _sqlite_groups(rows: Sequence[dict], sql: str) -> List[dict]:
    """The same question answered by SQLite, which is the standard being met."""
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("CREATE TABLE t (region TEXT, amount REAL)")
    connection.executemany(
        "INSERT INTO t (region, amount) VALUES (:region, :amount)", list(rows),
    )
    result = [dict(row) for row in connection.execute(sql)]
    connection.close()

    return result


def _chunked(rows: Sequence[dict], size: int) -> List[List[dict]]:
    return [list(rows[start:start + size]) for start in range(0, len(rows), size)]


# --- Fixtures ------------------------------------------------------------


@pytest.fixture
def rows() -> List[dict]:
    """
    A skewed set with the two traps in it: a group whose amounts are entirely NULL,
    and groups where some records have no amount and some do.
    """
    made: List[dict] = []

    for index in range(1, 61):
        # The group key cycles on 3 and the missing values on 4, so nullness is
        # spread across every region rather than lining up with one of them —
        # otherwise a group would be entirely NULL by accident and the avg trap
        # below would never fire.
        region = ["north", "south", "east"][index % 3]
        amount = None if index % 4 == 0 else float(index)
        made.append({"region": region, "amount": amount})

    # A whole group with nothing in it but NULLs: SUM must be NULL, not 0.
    made.extend({"region": "west", "amount": None} for _ in range(4))

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


# --- Exactness against the database --------------------------------------


class TestFoldEqualsOnePass:
    @pytest.mark.parametrize("size", [1, 2, 7, 13, 64, 200])
    def test_batched_fold_matches_sqlite_for_every_slice_size(
        self, rows: List[dict], full_plan: dict, size: int,
    ) -> None:
        expected = _sqlite_groups(
            rows,
            """
            SELECT region,
                   COUNT(*)      AS record_count,
                   COUNT(amount) AS count_amount,
                   SUM(amount)   AS sum_amount,
                   AVG(amount)   AS avg_amount,
                   MIN(amount)   AS min_amount,
                   MAX(amount)   AS max_amount
              FROM t
             GROUP BY region
             ORDER BY region
            """,
        )

        assert _run(full_plan, _chunked(rows, size)) == expected

    def test_uneven_slices_give_the_same_answer_as_even_ones(
        self, rows: List[dict], full_plan: dict,
    ) -> None:
        uneven = [rows[:3], rows[3:4], rows[4:40], rows[40:], []]

        assert _run(full_plan, uneven) == _run(full_plan, _chunked(rows, 10))

    def test_slice_order_does_not_change_the_answer(
        self, rows: List[dict], full_plan: dict,
    ) -> None:
        forward = _chunked(rows, 9)

        assert _run(full_plan, list(reversed(forward))) == _run(full_plan, forward)


class TestTheThreeTraps:
    def test_avg_divides_by_the_non_null_count_not_the_record_count(
        self, rows: List[dict],
    ) -> None:
        plan = _plan(
            ["region"],
            [
                {"type": "count", "column": "", "alias": "record_count"},
                {"type": "avg", "column": "amount", "alias": "avg_amount"},
            ],
        )
        result = {row["region"]: row for row in _run(plan, _chunked(rows, 7))}
        north = result["north"]

        expected = _sqlite_groups(
            rows,
            "SELECT region, AVG(amount) AS avg_amount FROM t GROUP BY region",
        )
        by_region = {row["region"]: row["avg_amount"] for row in expected}

        assert north["avg_amount"] == pytest.approx(by_region["north"])
        # The trap made concrete: the group has records without an amount, so
        # dividing by record_count would give a different, plausible, wrong number.
        assert north["record_count"] > 0
        total = north["avg_amount"] * north["record_count"]
        assert total != pytest.approx(sum(
            row["amount"] for row in rows
            if row["region"] == "north" and row["amount"] is not None
        ))

    def test_mean_of_means_would_have_been_wrong(self) -> None:
        """The worked example from the module docstring, asserted."""
        plan = _plan(["region"], [{"type": "avg", "column": "amount", "alias": "a"}])
        slices = [
            [{"region": "n", "amount": 10.0}, {"region": "n", "amount": 20.0}],
            [{"region": "n", "amount": 60.0}],
        ]

        assert _run(plan, slices)[0]["a"] == pytest.approx(30.0)
        # (15 + 60) / 2 — what averaging the averages would have produced.
        assert _run(plan, slices)[0]["a"] != pytest.approx(37.5)

    def test_sum_of_an_all_null_group_is_none_not_zero(
        self, rows: List[dict], full_plan: dict,
    ) -> None:
        result = {row["region"]: row for row in _run(full_plan, _chunked(rows, 5))}

        assert result["west"]["sum_amount"] is None
        assert result["west"]["avg_amount"] is None
        assert result["west"]["min_amount"] is None
        assert result["west"]["record_count"] == 4
        assert result["west"]["count_amount"] == 0

    def test_null_is_its_own_group_and_not_the_string_null(self) -> None:
        plan = _plan(["region"], [{"type": "count", "column": "", "alias": "n"}])
        slices = [
            [{"region": None}, {"region": "null"}],
            [{"region": None}, {"region": "north"}],
        ]
        result = _run(plan, slices)

        assert len(result) == 3
        assert {row["n"] for row in result} == {2, 1, 1}


# --- The carried intermediate --------------------------------------------


class TestCarriedFields:
    def test_count_without_a_column_counts_records(self) -> None:
        fields = algebra.carried_fields({"type": "count", "alias": "n"})

        assert [(f.name, f.kind, f.merge) for f in fields] == [
            ("n__n", algebra.KIND_ROWS, "sum"),
        ]

    def test_count_with_a_column_counts_values(self) -> None:
        fields = algebra.carried_fields(
            {"type": "count", "column": "amount", "alias": "c"},
        )

        assert [(f.name, f.kind, f.column) for f in fields] == [
            ("c__c", algebra.KIND_NON_NULL, "amount"),
        ]

    @pytest.mark.parametrize("function", ["sum", "avg"])
    def test_sum_and_avg_both_carry_a_sum_and_a_count(self, function: str) -> None:
        fields = algebra.carried_fields(
            {"type": function, "column": "amount", "alias": "a"},
        )

        assert {f.kind for f in fields} == {algebra.KIND_SUM, algebra.KIND_NON_NULL}
        assert all(f.merge == "sum" for f in fields)

    def test_min_and_max_merge_by_min_and_max(self) -> None:
        low = algebra.carried_fields({"type": "min", "column": "a", "alias": "l"})
        high = algebra.carried_fields({"type": "max", "column": "a", "alias": "h"})

        assert low[0].merge == "min"
        assert high[0].merge == "max"

    def test_every_carried_name_is_unique_across_a_plan(self, full_plan: dict) -> None:
        names = [field.name for field in algebra.plan_carried_fields(full_plan)]

        assert len(names) == len(set(names))

    def test_an_aggregation_without_an_alias_is_a_programming_error(self) -> None:
        with pytest.raises(ValueError, match="not validated"):
            algebra.carried_fields({"type": "sum", "column": "amount"})

    @pytest.mark.parametrize("function", ["sum", "avg", "min", "max"])
    def test_only_count_may_be_asked_without_a_column(self, function: str) -> None:
        with pytest.raises(ValueError, match="needs a column"):
            algebra.carried_fields({"type": function, "alias": "a"})


# --- Refusals ------------------------------------------------------------


class TestRefusals:
    @pytest.mark.parametrize(
        "function", ["count_distinct", "median", "percentile", "mode", "stddev"],
    )
    def test_non_decomposable_functions_are_refused_by_name(
        self, function: str,
    ) -> None:
        message = algebra.unsupported_function(function)

        assert message is not None
        assert function in message
        # The reader's next question is always "then what can I use".
        for allowed in algebra.SUPPORTED_FUNCTIONS:
            assert allowed in message

    @pytest.mark.parametrize("function", ["count", "sum", "avg", "min", "max"])
    def test_the_decomposable_five_are_allowed(self, function: str) -> None:
        assert algebra.unsupported_function(function) is None

    def test_the_supported_set_is_the_tool_config_vocabulary(self) -> None:
        from app.models.tool_configs import AGGREGATION_FUNCTION_VALUES

        # If these ever diverge, a function an operator can save is one this cannot
        # fold — which is a refusal a person must write, not one to discover live.
        assert algebra.SUPPORTED_FUNCTIONS == frozenset(AGGREGATION_FUNCTION_VALUES)

    def test_a_function_with_no_fold_never_reaches_carried_fields(self) -> None:
        with pytest.raises(ValueError, match="no partial fold"):
            algebra.carried_fields({"type": "median", "column": "a", "alias": "m"})


class TestReaggregatedAverage:
    def test_averaging_a_column_the_tool_already_averaged_is_refused(self) -> None:
        config = {"aggregations": [{"type": "avg", "column": "orders.amount"}]}

        message = algebra.reaggregated_average(
            [{"type": "avg", "column": "avg_amount", "alias": "a"}], config,
        )

        assert message is not None
        assert "avg_amount" in message

    def test_summing_a_column_the_tool_averaged_is_allowed(self) -> None:
        config = {"aggregations": [{"type": "avg", "column": "amount"}]}

        assert algebra.reaggregated_average(
            [{"type": "sum", "column": "avg_amount", "alias": "s"}], config,
        ) is None

    def test_averaging_a_column_the_tool_summed_is_allowed(self) -> None:
        config = {"aggregations": [{"type": "sum", "column": "amount"}]}

        assert algebra.reaggregated_average(
            [{"type": "avg", "column": "sum_amount", "alias": "a"}], config,
        ) is None

    def test_an_alias_is_used_in_preference_to_the_generated_name(self) -> None:
        config = {
            "aggregations": [
                {"type": "avg", "column": "orders.amount", "alias": "mean_spend"},
            ],
        }

        assert algebra.preaggregated_columns(config) == {"mean_spend": "avg"}

    def test_an_unaliased_aggregation_takes_the_executors_label(self) -> None:
        # query_executor._aggregated_columns labels it `function_column`, using the
        # reflected column's own name — so the table qualifier drops off.
        config = {"aggregations": [{"type": "avg", "column": "orders.amount"}]}

        assert algebra.preaggregated_columns(config) == {"avg_amount": "avg"}

    def test_a_sql_mode_tool_has_no_detectable_preaggregation(self) -> None:
        # Documented limitation: SQL-mode statements are not parsed anywhere in this
        # application, so an already-averaged column cannot be seen.
        assert algebra.preaggregated_columns({}) == {}
        assert algebra.reaggregated_average(
            [{"type": "avg", "column": "anything", "alias": "a"}], {},
        ) is None


# --- Result shape --------------------------------------------------------


class TestResultShape:
    def test_columns_are_the_group_keys_then_the_aggregations(
        self, full_plan: dict,
    ) -> None:
        assert algebra.result_columns(full_plan) == [
            "region", "record_count", "count_amount",
            "sum_amount", "avg_amount", "min_amount", "max_amount",
        ]

    def test_sorting_is_the_first_aggregation_descending_then_the_keys(
        self, full_plan: dict,
    ) -> None:
        assert algebra.sort_columns(full_plan) == [("record_count", True),
                                                   ("region", False)]

    def test_a_plan_with_no_aggregations_is_a_programming_error(self) -> None:
        with pytest.raises(ValueError, match="no aggregations"):
            algebra.plan_carried_fields(_plan(["region"], []))
