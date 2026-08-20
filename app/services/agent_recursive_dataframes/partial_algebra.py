"""
What makes a batched aggregate come out equal to a single-pass one.

This module is the whole correctness argument of the feature, written down on its
own so it can be read and tested without a database, without polars and without a
graph. Everything else in ``agent_recursive_dataframes`` is plumbing around what is
decided here.

**The rule.** A batched aggregate is exact if and only if every aggregation is an
*associative fold over a carried intermediate*. The carried intermediate is not
always the answer, and that gap is the entire subject of this file: ``avg`` cannot
be merged from averages, so what crosses a batch boundary is a ``(sum, count)``
pair and the division happens once, at the end, over the totals.

    slice 1: amount = [10, 20]        carries (30, 2)
    slice 2: amount = [60]            carries (60, 1)
    merged:                                  (90, 3)   ->  avg = 30      correct
    mean of means: (15 + 60) / 2 = 37.5                                  wrong

**What is refused.** ``median``, ``percentile``, ``mode`` and ``count_distinct``
have no bounded fold — an exact answer needs every value, or every distinct value,
resident at the same time, at which point the batching bought nothing and the
memory ceiling is gone. They are refused with a readable message rather than
approximated, because a plausible wrong figure is the one failure this application
takes most seriously. ``stddev``/``variance`` *would* be decomposable through
Chan's ``(n, mean, M2)`` merge; they are absent only because they are not in the
tool config vocabulary, and if they are ever added this is the file that gains
them.

**Three rules that are easy to get wrong and are the reason this is not inline.**

1. ``avg`` divides by the **non-null count of the averaged column**, never by the
   group's row count. SQL ``AVG`` ignores NULLs, so dividing by the row count turns
   "the average order value across the 40 orders that have one" into "…across all
   100" — a number that looks entirely reasonable and is wrong.
2. ``sum`` over an all-NULL group is **NULL in SQL and 0 in polars**. That is why
   ``sum`` carries a count it does not appear to need: without it the answer reads
   "£0 of revenue" where the database would say "no revenue recorded", and those
   are different facts.
3. **NULL is its own group**, in SQL ``GROUP BY`` and in polars ``group_by`` alike.
   The two already agree, so nothing here substitutes a sentinel — ``"null"`` would
   collide with the literal string and silently merge two groups.

Nothing in this module raises the application's exceptions. A refusal is returned
as a sentence for the caller to raise, and a ``ValueError`` here means the plan
reaching it was never validated — a programming error, not an operator's.
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence

from app.models.tool_configs import AGGREGATION_FUNCTION_VALUES

# --------------------------------------------------------------------------
# The vocabulary
# --------------------------------------------------------------------------

# The functions that have a bounded, associative fold. Written out rather than
# aliased to AGGREGATION_FUNCTION_VALUES on purpose: if a later release adds
# `count_distinct` or `median` to the tool config vocabulary, an alias would admit
# it here silently and the merge would be quietly wrong. Intersecting instead means
# a new function is refused until somebody comes here and gives it a fold.
DECOMPOSABLE_FUNCTIONS = frozenset({"count", "sum", "avg", "min", "max"})

SUPPORTED_FUNCTIONS = DECOMPOSABLE_FUNCTIONS & frozenset(AGGREGATION_FUNCTION_VALUES)

# How a carried field is combined when two slices meet. The kind is what the merge
# needs to know; the aggregation it came from is not.
KIND_ROWS = "rows"          # how many records were in the group
KIND_NON_NULL = "non_null"  # how many of them had a value in this column
KIND_SUM = "sum"
KIND_MIN = "min"
KIND_MAX = "max"

_MERGE_BY_KIND = {
    KIND_ROWS: "sum",
    KIND_NON_NULL: "sum",
    KIND_SUM: "sum",
    KIND_MIN: "min",
    KIND_MAX: "max",
}

# Suffix per kind. Part of the carried field's name rather than a separate column
# of metadata, so a partial frame can be read on its own and understood.
_SUFFIX_BY_KIND = {
    KIND_ROWS: "__n",
    KIND_NON_NULL: "__c",
    KIND_SUM: "__s",
    KIND_MIN: "__mn",
    KIND_MAX: "__mx",
}


@dataclass(frozen=True)
class CarriedField:
    """
    One column of a partial aggregate: what to compute per slice and how to merge.

    ``column`` is the source column being folded, empty for :data:`KIND_ROWS`,
    which counts records rather than values.
    """

    name: str
    kind: str
    column: str = ""

    @property
    def merge(self) -> str:
        """``"sum"``, ``"min"`` or ``"max"`` — how two slices' values combine."""
        return _MERGE_BY_KIND[self.kind]


def describe_supported() -> str:
    """The allowed function names as a sentence fragment, for refusal messages."""
    return ", ".join(sorted(SUPPORTED_FUNCTIONS))


# --------------------------------------------------------------------------
# Reading an aggregation
# --------------------------------------------------------------------------


def function_of(aggregation: Mapping[str, Any]) -> str:
    """The aggregation's function name, lowercased and trimmed."""
    return str(aggregation.get("type") or "").strip().lower()


def column_of(aggregation: Mapping[str, Any]) -> str:
    """
    The column being aggregated, trimmed. Empty means ``COUNT(*)``.

    ``count`` is the only function for which an absent column is meaningful, and
    it means something different from ``count(col)``: records rather than values.
    """
    return str(aggregation.get("column") or "").strip()


def alias_of(aggregation: Mapping[str, Any]) -> str:
    """
    The output name for this aggregation.

    Required. Aliases are generated by the planner rather than taken from a model,
    so an absent one is a validation step that did not run.
    """
    alias = str(aggregation.get("alias") or "").strip()

    if not alias:
        raise ValueError(
            "An aggregation reached partial_algebra without an alias, which means "
            "it was not validated. Aliases are assigned by aggregate_planner."
        )

    return alias


# --------------------------------------------------------------------------
# The fold
# --------------------------------------------------------------------------


def carried_fields(aggregation: Mapping[str, Any]) -> List[CarriedField]:
    """
    What one aggregation must carry across a batch boundary.

    ``sum`` and ``avg`` both carry a sum **and** a non-null count. For ``avg`` the
    reason is obvious; for ``sum`` it is rule 2 in the module docstring — the count
    is what tells "everything in this group was NULL" apart from "the values added
    up to zero", which SQL distinguishes and polars does not.
    """
    function = function_of(aggregation)
    column = column_of(aggregation)
    alias = alias_of(aggregation)

    def field(kind: str) -> CarriedField:
        return CarriedField(f"{alias}{_SUFFIX_BY_KIND[kind]}", kind, column)

    if function == "count":
        # COUNT(*) counts records and needs no column; COUNT(col) counts the
        # records where col has a value. Two different questions, so two kinds.
        return [field(KIND_ROWS) if not column else field(KIND_NON_NULL)]

    if function in {"sum", "avg"}:
        _require_column(function, column)
        return [field(KIND_SUM), field(KIND_NON_NULL)]

    if function == "min":
        _require_column(function, column)
        return [field(KIND_MIN)]

    if function == "max":
        _require_column(function, column)
        return [field(KIND_MAX)]

    raise ValueError(
        f"'{function}' has no partial fold, so it should have been refused by "
        f"validate_plan. Supported: {describe_supported()}."
    )


def plan_carried_fields(plan: Mapping[str, Any]) -> List[CarriedField]:
    """
    Every carried field the plan needs, in order, each name once.

    Two aggregations over the same column — ``sum(amount)`` and ``avg(amount)`` —
    carry the same two folds twice under different alias-derived names. That is
    deliberate: sharing them would save a trivial amount of work on a 200-record
    slice and would cost the property that every carried column's name says which
    aggregation it belongs to, which is what makes a partial frame readable.
    """
    fields: List[CarriedField] = []
    seen = set()

    for aggregation in plan.get("aggregations") or []:
        for carried in carried_fields(aggregation):
            if carried.name in seen:
                continue
            seen.add(carried.name)
            fields.append(carried)

    if not fields:
        raise ValueError("A plan reached partial_algebra with no aggregations.")

    return fields


def finalise_value(aggregation: Mapping[str, Any], carried: Mapping[str, Any]) -> Any:
    """
    Turn the merged carried fields for one group into the reported number.

    ``carried`` is the fully merged row — every slice already folded in. This is
    the only place a division happens, which is what makes ``avg`` exact.
    """
    function = function_of(aggregation)
    alias = alias_of(aggregation)
    fields = {field.kind: field.name for field in carried_fields(aggregation)}

    if function == "count":
        kind = KIND_ROWS if KIND_ROWS in fields else KIND_NON_NULL
        return int(carried.get(fields[kind]) or 0)

    total = carried.get(fields[KIND_SUM]) if KIND_SUM in fields else None
    count = int(carried.get(fields.get(KIND_NON_NULL, "")) or 0)

    if function == "sum":
        # Rule 2: no values means NULL, not zero.
        return None if count == 0 else total

    if function == "avg":
        # Rule 1: divide by the non-null count of *this* column, never by the
        # group's record count.
        return None if count == 0 or total is None else total / count

    if function == "min":
        return carried.get(fields[KIND_MIN])

    if function == "max":
        return carried.get(fields[KIND_MAX])

    raise ValueError(f"'{function}' has no finalisation, alias '{alias}'.")


def finalise_row(plan: Mapping[str, Any], carried: Mapping[str, Any]) -> Dict[str, Any]:
    """One merged row of carried fields as the row a person or a model reads."""
    row: Dict[str, Any] = {
        column: carried.get(column) for column in plan.get("group_by") or []
    }

    for aggregation in plan.get("aggregations") or []:
        row[alias_of(aggregation)] = finalise_value(aggregation, carried)

    return row


def result_columns(plan: Mapping[str, Any]) -> List[str]:
    """The finalised result's columns: the group keys, then the aggregations."""
    return [
        *(plan.get("group_by") or []),
        *(alias_of(entry) for entry in plan.get("aggregations") or []),
    ]


def sort_columns(plan: Mapping[str, Any]) -> List[tuple]:
    """
    How a finalised result is ordered: ``[(column, descending), ...]``.

    A hash ``group_by`` returns groups in arbitrary order, so without this the same
    question gives a differently-ordered answer every time — and once the 200-row
    cap bites, a different *answer*. The first aggregation descending is what makes
    "the top ones" mean something; the group keys ascending are a stable tiebreak so
    two runs over unchanged data are identical.
    """
    aggregations = plan.get("aggregations") or []
    order: List[tuple] = []

    if aggregations:
        order.append((alias_of(aggregations[0]), True))

    order.extend((column, False) for column in plan.get("group_by") or [])

    return order


# --------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------


def unsupported_function(function: str) -> Optional[str]:
    """
    Why this function cannot be merged from batches, or ``None`` if it can.

    The message names the supported set rather than only the rejected name,
    because the reader's next question is always "then what can I use".
    """
    name = (function or "").strip().lower()

    if not name:
        return "An aggregation is missing its function."

    if name in SUPPORTED_FUNCTIONS:
        return None

    return (
        f"'{name}' cannot be calculated in batches: an exact answer needs every "
        "record in memory at once, which is the thing this reads in batches to "
        f"avoid. Supported here: {describe_supported()}. For anything else, save a "
        "SQL query tool that lets the database calculate it in one pass."
    )


def preaggregated_columns(config: Optional[Mapping[str, Any]]) -> Dict[str, str]:
    """
    Output column -> the aggregation a builder-mode tool config already applied.

    Builder mode only. A SQL-mode statement is not parsed here — this application
    does not parse operator SQL anywhere — so an already-averaged column coming out
    of a SQL tool is **undetectable**, and that limitation is documented rather than
    papered over.

    The unaliased name matches ``query_executor._aggregated_columns``, which labels
    an unaliased aggregation ``function_column`` using the reflected column's own
    name — so a reference written ``orders.amount`` arrives as ``sum_amount``.
    """
    found: Dict[str, str] = {}

    for entry in (config or {}).get("aggregations") or []:
        function = function_of(entry)
        column = column_of(entry)

        if not function or not column:
            continue

        alias = str(entry.get("alias") or "").strip()
        found[alias or f"{function}_{column.rsplit('.', 1)[-1]}"] = function

    return found


def reaggregated_average(
    aggregations: Sequence[Mapping[str, Any]],
    config: Optional[Mapping[str, Any]],
) -> Optional[str]:
    """
    Why averaging this tool's output would be wrong, or ``None`` if it would not.

    If the chosen tool config already averaged a column, averaging its output
    averages averages — mean-of-means, the exact error the ``(sum, count)`` carry
    exists to prevent, arriving through the back door because the first mean
    happened in the database where nothing here can see the counts behind it.
    Unlike the batch case there is no fix available: the record counts each stored
    average was taken over are gone. So it is refused.
    """
    already = preaggregated_columns(config)

    for aggregation in aggregations:
        column = column_of(aggregation)
        if function_of(aggregation) != "avg" or not column:
            continue

        if already.get(column) == "avg":
            return (
                f"'{column}' is already an average calculated by this tool, so "
                "averaging it again would average averages and report a number "
                "that is wrong in a way nothing about it would show. Group the "
                "underlying records instead, or ask for a total."
            )

    return None


def _require_column(function: str, column: str) -> None:
    if not column:
        raise ValueError(
            f"'{function}' needs a column to aggregate; only 'count' may be asked "
            "without one."
        )
