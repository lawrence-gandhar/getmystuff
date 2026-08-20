"""
Which predicates may narrow a result set before it is folded, and why only those.

The companion to :mod:`app.services.agent_recursive_dataframes.partial_algebra`, and
the same split for the same reason: the rule that makes batching correct is written
here, without polars, without a database and without a graph, so it can be read and
tested on its own. ``frame_ops`` says it in polars and decides nothing.

**The rule, and it is one sentence.** A predicate may be applied per batch if and only
if it is **row-wise** — decidable from a single record, with no reference to any other
record in the set.

    filter(concat(b₁, b₂, …)) == concat(filter(b₁), filter(b₂), …)

That identity is what lets a filter live inside the fold rather than in front of it.
Every operator below satisfies it. What that excludes is worth naming, because each
one is a thing somebody will eventually ask for:

| Asked for | Why it is not here |
|---|---|
| ``amount > the average amount`` | needs the average of the whole set, which is not known until every batch has been read — so batch one would be filtered against batch one's average |
| ``the ten largest`` | a rank is a fact about the set; per batch it would return the ten largest *of each batch* |
| ``the latest row per department`` | same, with a group boundary; it is a window function, and this is not one |
| ``rows whose id also appears in …`` | a semi-join against another result set, which this pipeline does not have |

None of those is refused by accident. A model asked for "above average" and given an
operator list without it will say so through ``unsupported``, which is why the planner
gives it that word.

**Dates get parts rather than arithmetic.** "In March" is expressed as
``part=month, operator===, value=3`` on a date column, not as a range the model
computes. That is a deliberate narrowing: a model producing ``>= 2026-03-01`` and
``< 2026-04-01`` is doing month-boundary arithmetic, and February, December and leap
years are exactly where it gets that wrong — silently, as a smaller result set that
still looks like an answer. Extracting the part is the same question with no arithmetic
in it.

**A refusal is a returned sentence, never an exception.** Same contract
``partial_algebra`` keeps: a ``ValueError`` raised from here would mean the plan that
reached it was never validated, which is a programming error rather than an operator's
mistake.
"""

from typing import Any, Dict, List, Mapping, Optional, Sequence

# --------------------------------------------------------------------------
# The two shapes an answer takes
# --------------------------------------------------------------------------

#: Numbers over the records — the fold. What this feature did before filters existed.
MODE_GROUPS = "groups"

#: The matching records themselves. Only reachable through a filter, which is why the
#: pair lives in this module rather than beside the fold rules: a plan with no
#: conditions and no measures asks for nothing, so "return the records" is a shape that
#: exists *because* a condition can be expressed.
MODE_ROWS = "rows"

MODES = frozenset({MODE_GROUPS, MODE_ROWS})


# --------------------------------------------------------------------------
# The vocabulary
# --------------------------------------------------------------------------

#: Comparisons. Every one of them decides a record on its own.
OP_EQ = "=="
OP_NE = "!="
OP_LT = "<"
OP_LTE = "<="
OP_GT = ">"
OP_GTE = ">="
OP_IN = "in"
OP_NOT_IN = "not_in"
OP_CONTAINS = "contains"
OP_STARTS_WITH = "starts_with"
OP_BETWEEN = "between"
OP_IS_NULL = "is_null"
OP_IS_NOT_NULL = "is_not_null"

#: Operators taking exactly one value.
_ONE_VALUE = frozenset({OP_EQ, OP_NE, OP_LT, OP_LTE, OP_GT, OP_GTE,
                        OP_CONTAINS, OP_STARTS_WITH})

#: Operators taking a list of values.
_MANY_VALUES = frozenset({OP_IN, OP_NOT_IN})

#: Operators taking no value at all.
_NO_VALUE = frozenset({OP_IS_NULL, OP_IS_NOT_NULL})

OPERATORS = _ONE_VALUE | _MANY_VALUES | _NO_VALUE | frozenset({OP_BETWEEN})

#: ``between`` is **inclusive at both ends**, which is the reading a person expects of
#: "between 1000 and 5000". It is safe to be inclusive here precisely because a month
#: is not expressed as a range — see the module docstring. A half-open range would be
#: right for dates and surprising for numbers, and one operator cannot be both.
BETWEEN_VALUES = 2

#: How many values one ``in`` list may hold. Not a correctness bound — a long list is
#: still row-wise — but a model that emitted four thousand ids has misunderstood the
#: question, and the resulting predicate is slower than the query that produced it.
MAX_IN_VALUES = 500

# --------------------------------------------------------------------------
# Date parts
# --------------------------------------------------------------------------

PART_YEAR = "year"
PART_MONTH = "month"
PART_QUARTER = "quarter"
PART_DAY = "day"

#: A part, and the range its value must fall in. The range is checked because a model
#: asking for month 13 would otherwise produce a filter that matches nothing, and an
#: empty result set is the one wrong answer that reads as a right one: "there was no
#: revenue in that month" is a sentence somebody would believe.
PART_RANGES: Dict[str, Optional[tuple]] = {
    PART_YEAR: None,            # any year; a four-digit check would refuse 0203 typos
    PART_MONTH: (1, 12),
    PART_QUARTER: (1, 4),
    PART_DAY: (1, 31),
}

PARTS = frozenset(PART_RANGES)

#: Parts whose value is a whole number. All of them — a part is a number, which is
#: what makes ``month == 3`` comparable and ``month == "March"`` a mistake worth
#: naming rather than coercing.
_NUMERIC_PARTS = PARTS


# --------------------------------------------------------------------------
# Reading one filter
# --------------------------------------------------------------------------


def mode_of(plan: Mapping[str, Any]) -> str:
    """
    Which shape of answer a plan asks for.

    Read from the plan rather than re-derived from ``not plan["aggregations"]``, so the
    four places that behave differently cannot each reach their own conclusion. Defaults
    to :data:`MODE_GROUPS`, which is what every plan made before filters existed meant.
    """
    mode = str(plan.get("mode") or "").strip().lower()

    return mode if mode in MODES else MODE_GROUPS


def specs_of(plan: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    """A plan's filters as plain mappings, whatever they arrived as."""
    return [
        item if isinstance(item, Mapping) else item.model_dump()
        for item in plan.get("filters") or []
    ]


def column_of(spec: Mapping[str, Any]) -> str:
    return str(spec.get("column") or "").strip()


def operator_of(spec: Mapping[str, Any]) -> str:
    return str(spec.get("operator") or "").strip()


def part_of(spec: Mapping[str, Any]) -> str:
    return str(spec.get("part") or "").strip().lower()


def values_of(spec: Mapping[str, Any]) -> List[Any]:
    """
    A filter's values as a list, whichever field they arrived in.

    ``value`` and ``values`` are two fields rather than one because an LLM handed a
    single field for both a scalar and a list will put a one-element list where a
    scalar belongs about as often as not, and ``"in": 5`` and ``"==": [5]`` are then
    indistinguishable from typos. Two fields make the arity check meaningful.

    ``None`` **and** ``""`` both mean "no value given". The empty string is in there
    because the schema types these as strings — see ``PlannedFilter``, where a provider's
    strict ``response_format`` validator is the reason — so an absent value arrives as
    ``""`` rather than as null. Comparing against a genuinely empty string is asked for
    with ``is_null`` instead, which is the question somebody actually means.
    """
    if spec.get("values"):
        return [item for item in spec["values"] if item not in (None, "")]

    value = spec.get("value")

    return [] if value in (None, "") else [value]


def needs_values(operator: str) -> int:
    """
    How many values an operator wants: 0, 1, 2, or ``-1`` for "one or more".

    One function so the schema, the planner and the polars layer cannot disagree
    about what ``between`` means.
    """
    if operator in _NO_VALUE:
        return 0
    if operator in _ONE_VALUE:
        return 1
    if operator == OP_BETWEEN:
        return BETWEEN_VALUES

    return -1


# --------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------


def unsupported_operator(operator: str) -> str:
    """A sentence naming what this operator is not, or ``""`` when it is fine."""
    cleaned = str(operator or "").strip()

    if cleaned in OPERATORS:
        return ""

    return (
        f"'{cleaned or 'nothing'}' is not a way of comparing values here. "
        f"Available: {describe_operators()}."
    )


def unsupported_part(part: str) -> str:
    """A sentence naming what this date part is not, or ``""``."""
    cleaned = str(part or "").strip().lower()

    if not cleaned or cleaned in PARTS:
        return ""

    return (
        f"'{cleaned}' is not a part of a date that can be compared. "
        f"Available: {', '.join(sorted(PARTS))}."
    )


def wrong_arity(operator: str, values: Sequence[Any]) -> str:
    """
    A sentence for a filter carrying the wrong number of values, or ``""``.

    Checked rather than tolerated. ``between`` with one value has no second bound and
    would have to guess at an open-ended range, and a guess here narrows or widens a
    result set with nothing about the answer saying which happened.
    """
    wanted = needs_values(operator)
    given = len(values)

    if wanted == 0 and given:
        return (
            f"'{operator}' asks whether a value is there at all, so it takes no "
            "value to compare against."
        )

    if wanted == 1 and given != 1:
        return f"'{operator}' needs exactly one value to compare against, not {given}."

    if wanted == BETWEEN_VALUES and operator == OP_BETWEEN and given != BETWEEN_VALUES:
        return (
            f"'{OP_BETWEEN}' needs exactly two values — the low and the high end, "
            f"both included — not {given}."
        )

    if wanted == -1:
        if not given:
            return f"'{operator}' needs at least one value to compare against."
        if given > MAX_IN_VALUES:
            return (
                f"'{operator}' was given {given:,} values, which is more than the "
                f"{MAX_IN_VALUES:,} allowed. A list that long is usually a sign the "
                "filter should be a condition rather than a list of every match."
            )

    return ""


def out_of_range_part(part: str, values: Sequence[Any]) -> str:
    """
    A sentence for a date part compared against an impossible number, or ``""``.

    The reason this is checked at all is in :data:`PART_RANGES`: month 13 matches no
    record, and "no records" is an answer a person acts on.
    """
    bounds = PART_RANGES.get(str(part or "").strip().lower())

    if not bounds:
        return ""

    low, high = bounds

    for value in values:
        number = _as_number(value)

        if number is None:
            return (
                f"The {part} of a date is a whole number, so it cannot be compared "
                f"against '{value}'. Use {low} to {high}."
            )

        if not low <= number <= high:
            return (
                f"There is no {part} {number} — it runs from {low} to {high}."
            )

    return ""


def _as_number(value: Any) -> Optional[int]:
    """``value`` as a whole number, or ``None`` when it is not one."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value

    try:
        text = str(value).strip()
        return int(text) if text else None
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# Words
# --------------------------------------------------------------------------


def describe_operators() -> str:
    """The operator list, for a model's system prompt and for a refusal."""
    return ", ".join([
        OP_EQ, OP_NE, OP_LT, OP_LTE, OP_GT, OP_GTE,
        OP_IN, OP_NOT_IN, OP_CONTAINS, OP_STARTS_WITH,
        OP_BETWEEN, OP_IS_NULL, OP_IS_NOT_NULL,
    ])


def describe_filter(spec: Mapping[str, Any]) -> str:
    """
    One filter as a phrase a person reads, for the summary sentence.

    Built from the *validated* filter and not from the instruction, for the reason
    ``aggregate_planner.plan_summary`` gives: what is shown has to be what ran, and the
    failure worth catching is a plan that quietly answered a nearby question.
    """
    column = column_of(spec)
    part = part_of(spec)
    operator = operator_of(spec)
    values = values_of(spec)
    subject = f"the {part} of {column}" if part else column

    if operator == OP_IS_NULL:
        return f"{subject} is empty"
    if operator == OP_IS_NOT_NULL:
        return f"{subject} has a value"
    if operator == OP_BETWEEN:
        return f"{subject} is between {values[0]} and {values[1]}"
    if operator in _MANY_VALUES:
        listed = ", ".join(str(value) for value in values)
        return f"{subject} is {'not ' if operator == OP_NOT_IN else ''}one of {listed}"
    if operator == OP_CONTAINS:
        return f"{subject} contains {values[0]}"
    if operator == OP_STARTS_WITH:
        return f"{subject} starts with {values[0]}"

    return f"{subject} {operator} {values[0]}"


def describe_filters(specs: Sequence[Mapping[str, Any]]) -> str:
    """
    Every filter as one phrase, joined by "and".

    **And, never or.** Filters are conjunctive: each one narrows what the previous left,
    which is what makes them applicable one at a time inside the fold. A model wanting
    "March or April" says ``in [3, 4]`` on one filter, not two filters — and the
    operator list is what tells it so.
    """
    return " and ".join(describe_filter(spec) for spec in specs)
