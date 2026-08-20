"""
Deciding whether one record matches a condition.

**The vocabulary is ``filter_algebra``'s, imported rather than restated.** That module
already owns the operator names, how many values each takes and the sentence each refusal
uses; it is deliberately polars-free and database-free, so it imports cleanly here. Two
consequences worth naming: ``validate_flow`` refuses a condition using the same functions
this evaluates with, so a filter the canvas accepted cannot be one the runner chokes on
mid-batch; and a user who has met the operator table in the aggregation panel has met this
one.

What is *not* shared is the evaluation. There, one side runs against a polars frame and
this side runs against a dictionary that came out of somebody's REST API, and the
differences are real rather than incidental:

*A missing field is not a false comparison.* ``paths.read`` returns ``None`` for a field
the record does not have, and every operator except ``is_null`` treats ``None`` as not
matching. A record without a ``shipping_address`` is not "a record whose shipping country
is not GB" — it is a record the question does not apply to, and both answers happen to be
"does not match", but only one of them is a reason.

*Comparing across types never raises.* ``"12" > 5`` is a real shape when one system stores
a quantity as text, and the alternative to coercing is a run that dies on record 4,000 of
50,000 with a ``TypeError``. Numbers are compared as numbers when both sides can be read
as numbers, and as text otherwise — stated here rather than discovered.

*``between`` is inclusive at both ends*, which is what a person means by it, and is
``filter_algebra``'s own documented reading.
"""

from typing import Any, Callable, Dict, List, Mapping, Sequence, Tuple

from app.services.agent_recursive_dataframes import filter_algebra
from app.services.integrations.errors import NodeFailure
from app.services.integrations.mapping import paths

#: How the two halves of a batch are named. The ``filter`` node's ports.
KEPT = "kept"
DROPPED = "dropped"

#: ``all`` — every condition must hold. ``any`` — one is enough.
MATCH_ALL = "all"
MATCH_ANY = "any"
MATCH_MODES = (MATCH_ALL, MATCH_ANY)


def matches(record: Any, spec: Mapping[str, Any]) -> bool:
    """
    Whether one record satisfies one condition.

    Raises ``NodeFailure`` for a condition that is not a condition — an unknown operator
    or the wrong number of values. Those are refused at save time by ``validate_flow``, so
    reaching this is a version published before the rule existed or a row edited by hand;
    it produces a readable failed step rather than a ``KeyError``.
    """
    column = filter_algebra.column_of(spec)
    operator = filter_algebra.operator_of(spec)
    values = filter_algebra.values_of(spec)

    if operator not in filter_algebra.OPERATORS:
        raise NodeFailure(filter_algebra.unsupported_operator(operator))

    arity = filter_algebra.wrong_arity(operator, values)
    if arity:
        raise NodeFailure(arity)

    value = paths.read(record, column) if column else None

    if operator == filter_algebra.OP_IS_NULL:
        return value is None
    if operator == filter_algebra.OP_IS_NOT_NULL:
        return value is not None

    # Every remaining operator asks something *about* a value, and a record that does not
    # have one cannot answer. See the module docstring.
    if value is None:
        return False

    return _COMPARISONS[operator](value, values)


def partition(
    records: Sequence[Any], specs: Sequence[Mapping[str, Any]], *, mode: str = MATCH_ALL
) -> Tuple[List[Any], List[Any]]:
    """
    Split a batch into ``(kept, dropped)``.

    **No conditions keeps everything.** A filter step somebody has not finished
    configuring should pass records through rather than silently discard the batch —
    dropping everything looks exactly like a source that returned nothing, and the run
    reports success either way.
    """
    if not specs:
        return list(records), []

    if mode not in MATCH_MODES:
        raise NodeFailure(
            f"This filter is set to match '{mode}', which is not a way of combining "
            f"conditions. Use '{MATCH_ALL}' or '{MATCH_ANY}'."
        )

    combine = all if mode == MATCH_ALL else any
    kept: List[Any] = []
    dropped: List[Any] = []

    for record in records:
        target = kept if combine(matches(record, spec) for spec in specs) else dropped
        target.append(record)

    return kept, dropped


# ---------------------------------------------------------------------------
# The comparisons
# ---------------------------------------------------------------------------


def _numbers(left: Any, right: Any) -> Tuple[Any, Any]:
    """
    Both sides as numbers when both can be, otherwise both as text.

    Not a coercion the caller asked for — a comparison across types. ``"12" > 5`` is what
    one system storing a quantity as text and another as an integer looks like, and
    refusing it means a run that dies on record 4,000 of 50,000.
    """
    try:
        return float(left), float(right)
    except (TypeError, ValueError):
        return _text(left), _text(right)


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _in(value: Any, values: Sequence[Any]) -> bool:
    # Compared as text, so a numeric id from one API matches the same id sent as a string
    # from the form the operator typed it into.
    return _text(value) in {_text(candidate) for candidate in values}


def _between(value: Any, values: Sequence[Any]) -> bool:
    low, high = values[0], values[1]
    left, low_number = _numbers(value, low)
    _, high_number = _numbers(value, high)
    # Inclusive at both ends — filter_algebra's own documented reading, and what a person
    # means by "between 1000 and 5000".
    return low_number <= left <= high_number


_COMPARISONS: Dict[str, Callable[[Any, Sequence[Any]], bool]] = {
    filter_algebra.OP_EQ: lambda v, vs: _text(v) == _text(vs[0]),
    filter_algebra.OP_NE: lambda v, vs: _text(v) != _text(vs[0]),
    filter_algebra.OP_LT: lambda v, vs: _numbers(v, vs[0])[0] < _numbers(v, vs[0])[1],
    filter_algebra.OP_LTE: lambda v, vs: _numbers(v, vs[0])[0] <= _numbers(v, vs[0])[1],
    filter_algebra.OP_GT: lambda v, vs: _numbers(v, vs[0])[0] > _numbers(v, vs[0])[1],
    filter_algebra.OP_GTE: lambda v, vs: _numbers(v, vs[0])[0] >= _numbers(v, vs[0])[1],
    filter_algebra.OP_IN: _in,
    filter_algebra.OP_NOT_IN: lambda v, vs: not _in(v, vs),
    filter_algebra.OP_CONTAINS: lambda v, vs: _text(vs[0]).lower() in _text(v).lower(),
    filter_algebra.OP_STARTS_WITH: lambda v, vs: _text(v).lower().startswith(
        _text(vs[0]).lower()
    ),
    filter_algebra.OP_BETWEEN: _between,
}
