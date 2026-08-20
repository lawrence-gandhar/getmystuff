"""
The polars half: records in, narrowed, partial aggregates out, merged, finalised.

Every rule this module applies comes from
:mod:`app.services.agent_recursive_dataframes.partial_algebra` — which carried
field a fold produces, how two of them combine, and what the finalised number is —
and from :mod:`app.services.agent_recursive_dataframes.filter_algebra`, which owns
which conditions may narrow a batch and why only row-wise ones may. Nothing decides
that here; this file only knows how to say it in polars. That split is the point: the
correctness argument is testable without a DataFrame library, and this file can be
replaced without reopening it.

**polars is imported here and nowhere else in the application, at module scope.**
Both halves of that sentence matter. Module scope, because
``downloader_agents/parquet/parquet_writer.py`` documents what happens when a
compiled extension is first imported on a pool thread that is later destroyed, and
this module is reached from ``asyncio.to_thread``. One place, because a second
import site is a second chance to get the first one wrong.

**Why polars and not the pandas already in the project.** ``group_by``/``agg`` run
in Rust with the GIL released, so several slices genuinely aggregate at once;
pandas holds the GIL through ``from_records`` over dicts and through string-key
factorisation, which would serialise the fan-out and leave the wave pattern doing
nothing at all.

**Where the null rules live.** polars and SQL disagree in exactly one place —
``sum`` over an all-null group is ``0`` in polars and ``NULL`` in SQL — and this
module does not paper over it in the fold. The disagreement is resolved in
:func:`finalise`, using the non-null count that ``partial_algebra`` insists every
``sum`` carries. Reading the two functions together is how that is meant to be
checked.
"""

import logging
from typing import Any, Dict, List, Mapping, Optional, Sequence

import polars as pl

from app.services.agent_recursive_dataframes import filter_algebra as filters
from app.services.agent_recursive_dataframes import partial_algebra as algebra
from app.services.deep_agents.query_executor import (
    NEEDS_RECONFIGURING,
    ToolQueryError,
)

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# One slice
# --------------------------------------------------------------------------


def partial_aggregate(
    rows: Sequence[Mapping[str, Any]],
    plan: Mapping[str, Any],
) -> Optional["pl.DataFrame"]:
    """
    One batch of records narrowed by the plan's filters and folded. Runs in a thread.

    The returned frame has one row per group present *in this batch*, the group
    key columns, and the carried fields — never the finalised numbers. A partial
    average would be a wrong number waiting for somebody to read it, so the
    division does not happen until :func:`finalise`.

    **The filters are applied here, per batch, and that is exact rather than
    convenient.** Every operator in the vocabulary is row-wise, so
    ``filter(b₁ ⧺ b₂) == filter(b₁) ⧺ filter(b₂)`` — the identity
    ``filter_algebra`` opens with. Filtering in front of the pipeline instead would
    mean reading the whole set into memory first, which is the thing the batching
    exists to avoid.

    In :data:`MODE_ROWS` there is no fold: the filtered records themselves are the
    answer, and the frame is returned as it is. Its size is already bounded by the
    batch it came from, so nothing is truncated at this level — the running total is
    where the retained rows are capped, because that is the only place that knows how
    many have accumulated.

    ``None`` means the batch was empty, which is how the reader signals the end.
    """
    if not rows:
        return None

    frame = pl.from_dicts(list(rows), infer_schema_length=None)
    frame = _checked(frame, plan)
    frame = apply_filters(frame, plan)

    if filters.mode_of(plan) == filters.MODE_ROWS:
        # Every record that matched, unfolded. An empty frame is a real answer here
        # ("nothing in this batch matched") and is returned rather than turned into
        # None, which the reader uses to mean the cursor is exhausted.
        return frame

    group_by = list(plan.get("group_by") or [])
    aggregations = [_fold_expression(f) for f in algebra.plan_carried_fields(plan)]

    if not group_by:
        # No grouping at all — a single total over everything. Still a fold, still
        # merged the same way; the "group" is simply the whole result set.
        return frame.select(aggregations)

    return frame.group_by(group_by).agg(aggregations)


def _fold_expression(field: algebra.CarriedField) -> "pl.Expr":
    """The polars expression that produces one carried field from raw records."""
    if field.kind == algebra.KIND_ROWS:
        # len() counts records; count() would count values and is the other field.
        return pl.len().alias(field.name)

    column = pl.col(field.column)

    if field.kind == algebra.KIND_NON_NULL:
        # polars' count() already excludes nulls, which is exactly SQL's COUNT(col).
        return column.count().alias(field.name)

    if field.kind == algebra.KIND_SUM:
        # Returns 0 for an all-null group where SQL would say NULL. Deliberately
        # left alone: the count carried alongside is what finalise() uses to tell
        # "nothing here" from "the values summed to zero".
        return column.sum().alias(field.name)

    if field.kind == algebra.KIND_MIN:
        return column.min().alias(field.name)

    if field.kind == algebra.KIND_MAX:
        return column.max().alias(field.name)

    raise ValueError(f"No polars fold for carried kind '{field.kind}'.")


# --------------------------------------------------------------------------
# Narrowing, before the fold
# --------------------------------------------------------------------------


def apply_filters(
    frame: "pl.DataFrame",
    plan: Mapping[str, Any],
) -> "pl.DataFrame":
    """
    Keep the records the plan's conditions hold for. Runs on every batch.

    Every predicate is built from the *validated* plan — the columns are the probed
    spellings, the operators are in the vocabulary, the arity is right — so nothing
    here re-argues any of that. What this function does own is the two places polars
    and a plan can still disagree, and both of them produce a wrong-looking-right
    answer rather than an error:

    * a **date part on a column polars did not read as a date.** SQLite hands dates
      back as text and so do several drivers, so ``.dt.month()`` would fail on a column
      that plainly holds dates. Coerced by :func:`_temporal`, which **refuses** rather
      than parsing loosely — see its docstring, the failure it prevents is a filter
      that matches nothing.
    * a **comparison against the wrong kind of value** — ``amount > "lots"``. polars
      raises for that, which is right, and the message it raises with names a dtype
      rather than a column, so it is caught and rewritten.

    Conditions are combined with AND, in the order the plan lists them. That is not a
    style choice: each one narrows what the last left, which is what makes them
    applicable one at a time and what ``describe_filters`` says out loud.
    """
    specs = filters.specs_of(plan)

    if not specs:
        return frame

    for spec in specs:
        try:
            frame = frame.filter(_predicate(frame.schema, spec))
        except ToolQueryError:
            raise
        except Exception as exc:  # noqa: BLE001 — one sentence per polars complaint
            raise _unappliable(frame, spec, exc) from exc

    return frame


def _unappliable(
    frame: "pl.DataFrame",
    spec: Mapping[str, Any],
    exc: Exception,
) -> ToolQueryError:
    """
    A polars complaint, rewritten as something the reader can act on.

    The date case is separated out because polars' own message for it — "could not find
    an appropriate format to parse dates" — describes the library's difficulty rather
    than the operator's mistake, which is usually that a ``month`` was asked of a column
    holding names or reference codes. Quoting one offending value is what turns it into
    a sentence somebody can check against their data.
    """
    part = filters.part_of(spec)
    column = filters.column_of(spec)

    if part and column in frame.schema:
        return ToolQueryError(
            f"The {part} of '{column}' cannot be read, because {_unreadable(frame, column)} "
            f"is not a date. Filter on a date column, or compare '{column}' directly "
            "instead of part of it.",
            advice=NEEDS_RECONFIGURING,
        )

    return ToolQueryError(
        f"The condition '{filters.describe_filter(spec)}' could not be applied to "
        f"these records: {exc}",
        advice=NEEDS_RECONFIGURING,
    )


def _unreadable(frame: "pl.DataFrame", column: str) -> str:
    """
    The value that actually failed to parse as a date, quoted.

    **The first value is the wrong one to show.** A column of ISO dates with one
    ``"n/a"`` in it fails on the ``"n/a"``, and a message quoting the first row would
    read "'2026-03-05' is not a date" — which is false, and sends the operator to check
    a column that is mostly fine. So the offending value is found: parse the column
    loosely, and the offender is a row that came back empty from a value that was not.

    Loose parsing is safe *here* precisely because this is the failure path — nothing
    is being filtered with it, and the null it produces is the thing being looked for
    rather than a record being quietly dropped.
    """
    values = frame.get_column(column)

    try:
        parsed = values.str.to_datetime(strict=False)
        offenders = values.filter(parsed.is_null() & values.is_not_null()).drop_nulls()

        if offenders.len():
            return f"'{offenders[0]}' in '{column}'"
    except Exception:  # noqa: BLE001 — a column with no text form at all; fall through
        pass

    present = values.drop_nulls()

    return f"'{present[0]}' in '{column}'" if present.len() else f"'{column}'"


def _predicate(schema: Mapping[str, Any], spec: Mapping[str, Any]) -> "pl.Expr":
    """One validated condition as a polars expression."""
    column = filters.column_of(spec)
    operator = filters.operator_of(spec)
    part = filters.part_of(spec)
    raw = filters.values_of(spec)

    subject = pl.col(column)

    if part:
        subject = _date_part(subject, schema, column, part)

    if operator == filters.OP_IS_NULL:
        return subject.is_null()
    if operator == filters.OP_IS_NOT_NULL:
        return subject.is_not_null()

    # Text operators compare against the column's text form, so the value stays a string
    # and no coercion is wanted — see `_as_text`.
    if operator == filters.OP_CONTAINS:
        return _as_text(subject, schema, column).str.contains(
            str(raw[0]), literal=True,
        )
    if operator == filters.OP_STARTS_WITH:
        return _as_text(subject, schema, column).str.starts_with(str(raw[0]))

    values = [_coerced(item, schema, column, part) for item in raw]

    if operator == filters.OP_IN:
        return subject.is_in(values)
    if operator == filters.OP_NOT_IN:
        # `is_in(...).not_()` rather than `!= each`: a null is in neither, and negating
        # the membership keeps a null out of the result exactly as SQL's NOT IN does.
        return subject.is_in(values).not_()
    if operator == filters.OP_BETWEEN:
        return subject.is_between(values[0], values[1], closed="both")

    value = values[0]

    if operator == filters.OP_EQ:
        return subject == value
    if operator == filters.OP_NE:
        return subject != value
    if operator == filters.OP_LT:
        return subject < value
    if operator == filters.OP_LTE:
        return subject <= value
    if operator == filters.OP_GT:
        return subject > value

    return subject >= value


def _coerced(
    value: Any,
    schema: Mapping[str, Any],
    column: str,
    part: str,
) -> Any:
    """
    One filter value, as the type the column it is compared against actually holds.

    **Every value arrives as a string**, because a plan is an LLM's structured output and
    a provider's strict ``response_format`` validator refuses both ``Any`` (an empty
    schema) and a union (``anyOf``) — see ``PlannedFilter``. So the typing that a JSON
    number would have carried has to happen here instead.

    That turns out to be the better place for it. Trusting the model's own types meant
    ``"1000"`` against an integer column was a polars type error surfaced as a refusal;
    coercing against the column's real dtype makes it a comparison against 1000, and makes
    ``"lots"`` a refusal that can name the column and the value. The check is the same
    either way — what changes is that the *common* case now works.

    A date part is always a whole number, whatever the underlying column holds, because
    ``month`` of anything is 1–12.
    """
    if part:
        return _as_int(value, column, f"the {part} of")

    dtype = schema.get(column)

    if dtype is None or dtype == pl.String or dtype == pl.Null:
        return str(value)

    if dtype == pl.Boolean:
        return _as_bool(value, column)

    if dtype.is_integer():
        return _as_int(value, column, "")

    if dtype.is_float():
        return _as_float(value, column)

    if dtype.is_temporal():
        return _as_date(value, column)

    # Anything else — a list, a struct — is compared as text rather than guessed at. The
    # comparison will refuse in polars, and `_unappliable` phrases it with the condition.
    return str(value)


def _as_int(value: Any, column: str, role: str) -> int:
    """A whole number, or a refusal naming the column and what was given."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        raise ToolQueryError(
            f"{role} '{column}' is compared against whole numbers, and '{value}' is not "
            "one.".strip().capitalize(),
            advice=NEEDS_RECONFIGURING,
        ) from None


def _as_float(value: Any, column: str) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        raise ToolQueryError(
            f"'{column}' holds numbers, and '{value}' is not one, so the two cannot be "
            "compared.",
            advice=NEEDS_RECONFIGURING,
        ) from None


def _as_bool(value: Any, column: str) -> bool:
    """
    A true/false value from the words a model actually writes.

    Refused rather than defaulted, because Python's truthiness would read ``"false"`` as
    ``True`` — a filter that silently selects the opposite of what was asked.
    """
    text = str(value).strip().lower()

    if text in ("true", "yes", "1", "y", "t"):
        return True
    if text in ("false", "no", "0", "n", "f"):
        return False

    raise ToolQueryError(
        f"'{column}' holds true/false values, and '{value}' is neither.",
        advice=NEEDS_RECONFIGURING,
    )


def _as_date(value: Any, column: str) -> Any:
    """
    A date or datetime from an ISO string, or a refusal.

    ISO only, and deliberately: this is a value a *model* produced rather than a column
    the operator's driver returned, so there is no legacy format to accommodate and no
    reason to accept an ambiguous one. ``2026-08-01`` has one reading; ``01/08/2026`` has
    two, and guessing between them here would be inventing a boundary.
    """
    import datetime

    text = str(value).strip()

    for parse in (datetime.datetime.fromisoformat, datetime.date.fromisoformat):
        try:
            return parse(text)
        except ValueError:
            continue

    raise ToolQueryError(
        f"'{column}' holds dates, and '{value}' is not one this can read. Write it as "
        "YYYY-MM-DD — or compare part of the date instead, such as its month.",
        advice=NEEDS_RECONFIGURING,
    )


def _date_part(
    subject: "pl.Expr",
    schema: Mapping[str, Any],
    column: str,
    part: str,
) -> "pl.Expr":
    """
    The year, month, quarter or day of a date column, as a whole number.

    The column may not be a date as far as polars is concerned — see
    :func:`_temporal` — so it is coerced first and the extraction happens on the
    result.
    """
    temporal = _temporal(subject, schema, column, part)

    if part == filters.PART_YEAR:
        return temporal.dt.year()
    if part == filters.PART_MONTH:
        return temporal.dt.month()
    if part == filters.PART_QUARTER:
        return temporal.dt.quarter()

    return temporal.dt.day()


def _temporal(
    subject: "pl.Expr",
    schema: Mapping[str, Any],
    column: str,
    part: str,
) -> "pl.Expr":
    """
    A column read as a date, or a refusal naming it.

    **This is the one place in the feature where a loose parse would be worse than a
    failure.** ``str.to_datetime(strict=False)`` turns anything it cannot read into
    null, and a null has no month — so a column of dates in a format polars did not
    recognise becomes a filter that matches **no records at all**. "There was no
    revenue in March" is a sentence somebody would repeat in a meeting.

    So the parse is strict, and the refusal names the column, the part being asked for
    and what the column actually holds. A caller told that ``invoice_date`` is text is
    told something they can act on; a caller shown an empty result is not.

    **One limit is worth knowing rather than guarding.** Text dates are read by polars'
    format inference, and an ambiguous format is genuinely ambiguous: ``05/03/2026``
    is read **day-first**, so it is the 5th of March and not the 3rd of May. ISO dates
    — what SQLite and every migration in this project write — have no such reading.
    Refusing every ambiguous format would refuse most text date columns outright, so
    the mitigation is elsewhere: ``plan_summary`` names the condition that ran, so a
    filtered answer always says what it was filtered by.
    """
    dtype = schema.get(column)

    if dtype is not None and dtype.is_temporal():
        return subject

    if dtype == pl.String:
        # strict=True: an unreadable value raises, which apply_filters turns into a
        # sentence. Deliberately not `strict=False`, which would return null and
        # silently exclude the record.
        return subject.str.to_datetime(strict=True)

    raise ToolQueryError(
        f"The {part} of '{column}' cannot be compared, because '{column}' holds "
        f"{_describe(dtype)} rather than dates. Filter on a date column, or compare "
        f"'{column}' directly.",
        advice=NEEDS_RECONFIGURING,
    )


def _as_text(
    subject: "pl.Expr",
    schema: Mapping[str, Any],
    column: str,
) -> "pl.Expr":
    """
    A column as text, for ``contains`` / ``starts_with``.

    Cast rather than refused, because "the reference starts with INV" is a reasonable
    thing to ask of a column a driver happened to return as a number, and a cast of one
    value to text loses nothing. Unlike a date parse, it cannot silently drop a record:
    every non-null value has a text form.
    """
    return subject if schema.get(column) == pl.String else subject.cast(pl.String)


# --------------------------------------------------------------------------
# Merging
# --------------------------------------------------------------------------


def merge_partials(
    running: Optional["pl.DataFrame"],
    partials: Sequence[Optional["pl.DataFrame"]],
    plan: Mapping[str, Any],
    keep: Optional[int] = None,
) -> Optional["pl.DataFrame"]:
    """
    Fold this wave's partial aggregates into the running one. Runs in a thread.

    ``keep`` applies to :data:`MODE_ROWS` only, and bounds the rows carried forward to
    be shown — see :func:`_kept_rows`. It is ignored for a fold, where what bounds
    memory is the number of groups and the caller checks that separately.

    Stack and re-aggregate, rather than join: a group present in one frame and
    absent from another is the ordinary case — most batches contain most groups,
    none contains all of them — and a join would have to decide what an absent
    side means for every carried field separately. Stacking makes that a
    non-question, because a group simply contributes the rows it has.

    ``vertical_relaxed`` because a batch whose values all happened to be whole
    numbers infers ``Int64`` where another infers ``Float64``. That is not drift
    in the data, it is drift in the inference, and promoting to the common numeric
    type is right. Anything with no common numeric type has already been refused
    by :func:`_checked` at the slice that introduced it, where the column can still
    be named.
    """
    frames = [
        frame for frame in [running, *partials]
        if frame is not None and frame.height
    ]

    if not frames:
        return running

    if filters.mode_of(plan) == filters.MODE_ROWS:
        return _kept_rows(frames, keep)

    if len(frames) == 1:
        return frames[0]

    stacked = pl.concat(frames, how="vertical_relaxed")
    merges = [_merge_expression(f) for f in algebra.plan_carried_fields(plan)]
    group_by = list(plan.get("group_by") or [])

    if not group_by:
        return stacked.select(merges)

    return stacked.group_by(group_by).agg(merges)


def _kept_rows(
    frames: Sequence["pl.DataFrame"],
    keep: Optional[int],
) -> "pl.DataFrame":
    """
    Row mode's merge: the matching records so far, in read order, capped at ``keep``.

    **Truncating here is not the cap this application refuses.** Every matching record
    is still read and counted — ``matched_rows`` in the graph's state is the exact
    total — and what is bounded is only how many are *carried* to be shown, which is
    the prompt's limit and not the query's. That is the distinction ``query_executor``
    draws with ``PROMPT_ROW_LIMIT``: the answer says "200 of 4,317", so the reader
    knows what they have. A cap that changed the total would be the other thing.

    Read order is preserved because the reader's cursor order is the query's order, and
    "the first two hundred" only means something if it is stable.
    """
    stacked = pl.concat(frames, how="vertical_relaxed") if len(frames) > 1 else frames[0]

    if keep is None or stacked.height <= keep:
        return stacked

    return stacked.head(keep)


def _merge_expression(field: algebra.CarriedField) -> "pl.Expr":
    """
    How two slices' values for one carried field combine.

    Taken from ``field.merge`` rather than from the aggregation it came from,
    because the merge is a property of the carried intermediate: an ``avg``'s sum
    merges by addition exactly as a ``sum``'s does, and the two are the same
    operation once the division has been deferred.
    """
    column = pl.col(field.name)

    if field.merge == "sum":
        return column.sum().alias(field.name)

    if field.merge == "min":
        return column.min().alias(field.name)

    if field.merge == "max":
        return column.max().alias(field.name)

    raise ValueError(f"No polars merge for '{field.merge}'.")


def group_count(frame: Optional["pl.DataFrame"]) -> int:
    """How many groups the running aggregate holds."""
    return 0 if frame is None else int(frame.height)


# --------------------------------------------------------------------------
# Finalising
# --------------------------------------------------------------------------


def finalise(
    frame: Optional["pl.DataFrame"],
    plan: Mapping[str, Any],
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    The merged aggregate as the rows a person or a model reads.

    Three things happen here and the order is load-bearing. The carried fields
    become the reported numbers — the only place ``avg`` divides, and the only
    place a group with no values becomes NULL rather than zero. Then the result is
    **sorted**, because a hash ``group_by`` returns groups in arbitrary order and
    without this the same question gives a differently-ordered answer each time.
    Only then is ``limit`` applied, if there is one, so "the top ``limit``" means
    something rather than "whichever ``limit`` the hash happened to put first".

    ``limit=None`` — what the aggregate service passes — returns **every group**. The
    sort still matters without it: it is what makes the order of the answer repeatable.

    ``nulls_last`` on every key: a group whose measure is NULL is the least
    interesting one, and it is certainly not the largest.

    In :data:`MODE_ROWS` none of that applies: there are no carried fields to divide,
    and the rows are returned **in read order** rather than sorted. Sorting them would
    be inventing an order — the plan asked for the matching records, not for the
    largest of them, and "the first two hundred" of a re-sorted set answers a
    different question from the one the count beside it describes.
    """
    if frame is None or not frame.height:
        return []

    if filters.mode_of(plan) == filters.MODE_ROWS:
        return frame.to_dicts()

    finalised = frame.select([
        *(pl.col(column) for column in plan.get("group_by") or []),
        *_finalise_expressions(plan),
    ])

    order = algebra.sort_columns(plan)

    if order:
        finalised = finalised.sort(
            by=[column for column, _ in order],
            descending=[descending for _, descending in order],
            nulls_last=True,
        )

    if limit is None:
        return finalised.to_dicts()

    return finalised.head(max(1, int(limit))).to_dicts()


def _finalise_expressions(plan: Mapping[str, Any]) -> List["pl.Expr"]:
    """
    One expression per aggregation, mirroring ``partial_algebra.finalise_value``.

    The two are checked against each other by
    ``test_frame_ops.test_polars_finalisation_matches_the_reference_rules``. They
    exist separately because sorting has to happen on the finalised values inside
    polars — you cannot order by an average that has not been divided yet — while
    the rules themselves must stay readable without a DataFrame library.
    """
    expressions: List["pl.Expr"] = []

    for aggregation in plan.get("aggregations") or []:
        function = algebra.function_of(aggregation)
        alias = algebra.alias_of(aggregation)
        fields = {f.kind: f.name for f in algebra.carried_fields(aggregation)}

        if function == "count":
            name = fields.get(algebra.KIND_ROWS) or fields[algebra.KIND_NON_NULL]
            expressions.append(pl.col(name).cast(pl.Int64).alias(alias))
            continue

        if function in {"sum", "avg"}:
            total = pl.col(fields[algebra.KIND_SUM])
            count = pl.col(fields[algebra.KIND_NON_NULL])
            # Rules 1 and 2 from partial_algebra, said in polars: divide by the
            # values that existed, and report nothing where nothing existed.
            value = total if function == "sum" else total.cast(pl.Float64) / count
            expressions.append(
                pl.when(count > 0).then(value).otherwise(None).alias(alias),
            )
            continue

        kind = algebra.KIND_MIN if function == "min" else algebra.KIND_MAX
        expressions.append(pl.col(fields[kind]).alias(alias))

    return expressions


# --------------------------------------------------------------------------
# Checking a slice before it is folded
# --------------------------------------------------------------------------


def _checked(frame: "pl.DataFrame", plan: Mapping[str, Any]) -> "pl.DataFrame":
    """
    Refuse a slice the plan cannot be applied to, naming what is wrong.

    Run on **every** slice rather than only the first, and that is what makes it
    the schema-drift guard as well as the plan check: a column that arrived as a
    number in the first thousand records and as text in the next is caught at the
    batch that introduced it, where the column can still be named. The alternative
    — coercing it to text, as the parquet writer reasonably does for a file — would
    drop the value out of a SUM and understate the total with nothing about the
    result saying so.

    Filter columns are checked here as well as measures and keys, and for the same
    reason as the others rather than for a new one: a condition on a column this batch
    does not have would otherwise surface as a polars ``ColumnNotFound`` three frames
    later, where nothing knows which of the plan's parts named it.
    """
    schema = frame.schema

    for column in plan.get("group_by") or []:
        _checked_group_key(schema, column)

    for spec in filters.specs_of(plan):
        _require_column(
            schema,
            filters.column_of(spec),
            f"used in the condition '{filters.describe_filter(spec)}'",
        )

    for aggregation in plan.get("aggregations") or []:
        frame = _checked_measure(frame, schema, aggregation)

    return frame


def _checked_group_key(schema: Mapping[str, Any], column: str) -> None:
    """A group key exists and is not a float."""
    dtype = _require_column(schema, column, "grouped by")

    if dtype in (pl.Float32, pl.Float64):
        raise ToolQueryError(
            f"'{column}' holds decimal numbers, which cannot be used to group "
            "records: two values that display identically are not necessarily "
            "equal, so the groups would not be trustworthy. Group by a text, "
            "whole-number or date column instead.",
            advice=NEEDS_RECONFIGURING,
        )


def _checked_measure(
    frame: "pl.DataFrame",
    schema: Mapping[str, Any],
    aggregation: Mapping[str, Any],
) -> "pl.DataFrame":
    """One measure's column exists and holds something that function can work on."""
    column = algebra.column_of(aggregation)
    function = algebra.function_of(aggregation)

    if not column:
        return frame

    dtype = _require_column(schema, column, f"used for the {function}")

    if dtype == pl.Null:
        # Every record in *this* batch happens to have no value for the column.
        # Ordinary, not drift: cast so the fold produces the empty carry the merge
        # expects rather than failing on a type that holds nothing.
        return frame.with_columns(pl.col(column).cast(pl.Float64))

    if function in {"sum", "avg"} and not dtype.is_numeric():
        raise ToolQueryError(
            f"'{column}' holds {_describe(dtype)}, which cannot be "
            f"{'totalled' if function == 'sum' else 'averaged'}. Choose a "
            "numeric column, or count the records instead.",
            advice=NEEDS_RECONFIGURING,
        )

    if function in {"min", "max"} and not (
        dtype.is_numeric() or dtype.is_temporal() or dtype == pl.String
    ):
        raise ToolQueryError(
            f"'{column}' holds {_describe(dtype)}, which has no smallest or "
            "largest value. Choose a numeric, date or text column.",
            advice=NEEDS_RECONFIGURING,
        )

    return frame


def _require_column(schema: Mapping[str, Any], column: str, role: str) -> Any:
    """The column's dtype, or a refusal naming what the tool actually returns."""
    if column in schema:
        return schema[column]

    available = ", ".join(sorted(schema)) or "nothing at all"

    raise ToolQueryError(
        f"This tool does not return a column called '{column}', so it cannot be "
        f"{role}. It returns: {available}.",
        advice=NEEDS_RECONFIGURING,
    )


def _describe(dtype: Any) -> str:
    """A polars dtype in words an operator reads, not a repr."""
    if dtype == pl.String:
        return "text"
    if dtype == pl.Boolean:
        return "true/false values"
    if dtype.is_temporal():
        return "dates"
    if dtype in (pl.List, pl.Struct) or isinstance(dtype, (pl.List, pl.Struct)):
        return "structured values"
    if dtype is not None and dtype.is_integer():
        return "whole numbers"
    if dtype is not None and dtype.is_float():
        return "decimal numbers"

    return f"{dtype} values"
