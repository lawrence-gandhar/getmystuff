"""
Turning "total and average spend by region" into something safe to execute.

Two decisions and then a lot of checking. Which tool holds the records, and what to
group and measure. The first is usually free — the agent's routing prompt already
lists every tool by name, so a model naming one is the ordinary path — and the
second costs exactly one LLM call, never more.

**The model sees column names and nothing else.** The columns come from
``query_executor.probe_tool_query``, which fetches one row, reports the column
names and reports no values. So "the rows a tool returns are the only data the
model ever sees" stays true here by construction rather than by discipline. It is
also the only way to know the columns at all: a builder config with an empty
selection means *every active column*, and a SQL-mode statement is not parsed
anywhere in this application.

**Validation is the load-bearing half, and it runs whatever produced the plan.**
A model that names ``regoin`` is the expected case. Every column is matched against
the probed names and then *replaced by the probed spelling*, so a later frame
lookup is exact rather than nearly right. Every function is checked against
``partial_algebra``'s foldable set, which is what makes an inexact merge
unreachable rather than merely unlikely. And the output names are assigned here,
never taken from the model, because a model-chosen alias could collide with a group
key and quietly overwrite it.

**There is no internal retry.** A refusal names the tool's real columns and goes
back as a tool failure; the agent's own loop is the correction path, and it already
exists. Re-asking here would spend a second call to make the same mistake more
expensively.
"""

import logging
from typing import Any, Dict, List, Mapping, Optional, Sequence

from app.schemas.agent_recursive_dataframes import (
    MAX_GROUP_BY_COLUMNS,
    MAX_PLAN_AGGREGATIONS,
    MAX_PLAN_FILTERS,
    AggregationPlan,
)
from app.services.agent_recursive_dataframes import filter_algebra as filters
from app.services.agent_recursive_dataframes import partial_algebra as algebra
from app.services.agent_recursive_dataframes.filter_algebra import (
    MODE_GROUPS,
    MODE_ROWS,
)
from app.services.deep_agents.prompt_builder import INTERNAL_CALL_TAG
from app.services.deep_agents.query_executor import (
    NEEDS_RECONFIGURING,
    NOT_AVAILABLE,
    ToolQueryError,
    probe_tool_query,
)
from app.services.downloader_agents.base.record_reader import RecordSource

logger = logging.getLogger(__name__)


# How many tools are described to the model when it has to choose one. Past this
# the catalogue is longer than the question and the choice gets worse, not better;
# an agent with more tools than this wants the tool named in the instruction.
MAX_CATALOGUE_TOOLS = 40

_SYSTEM_PROMPT = """\
You turn a request into a plan over one table of records. A plan has three parts: \
which records to keep (filters), how to group them, and what to measure.

You will be given the columns that table has. Every column name you use MUST be \
copied exactly from that list. Do not invent columns, do not guess at names that \
"should" exist, and do not use a name from the request if it is not in the list.

FILTERS - each one is a condition a record must satisfy. They are combined with \
AND: every filter must hold. For "March or April" use one filter with `in` and \
both values, never two filters.
- operator is one of: {operators}
- `between` takes exactly two values and includes both ends.
- `in` and `not_in` take a list in `values`; every other operator takes one `value`.
- `is_null` and `is_not_null` take no value at all.
- To filter on part of a date, set `part` to one of {parts} and compare it to a \
whole number: the March of any year is part="month", operator="==", value=3. \
NEVER write date arithmetic yourself and never turn a month into a date range.

MEASURES - the only ones available are: {functions}.
- count with no column counts records.
- count with a column counts the records that have a value in it.

If the request only asks to see the matching records - "show me", "list", "which \
ones" - give the filters and NO measures, and the records themselves come back. \
Ask for measures only when a total, average, count, smallest or largest is wanted. \
Grouping requires at least one measure.

Limits: at most {max_filters} filters, at most {max_group_by} columns to group by, \
at most {max_aggregations} measures.

If the request needs something this cannot express - a median, a percentile, a \
count of distinct values, a ranking, a percentage of a total, a comparison against \
an average or another total, the "top N", or anything calculated from more than one \
column - set unsupported to true and give one short sentence saying why. Do not \
substitute a different measure, do not drop a condition you cannot express, and do \
not answer a nearby question instead.\
"""


def validated_instruction(instruction: str) -> str:
    """The request, or a refusal. Nothing to group is not a plan, it is a mistake."""
    text = (instruction or "").strip()

    if not text:
        raise ToolQueryError(
            "No instruction was given, so there is nothing to group.",
            advice=NOT_AVAILABLE,
        )

    return text


async def plan(
    entry: Mapping[str, Any],
    columns: Sequence[str],
    instruction: str,
    model: Any,
) -> Dict[str, Any]:
    """
    Produce a validated plan against a known set of columns.

    Returns the plan as plain JSON — plain because it travels through graph state,
    and a pydantic model there would be one more thing every node has to know the
    type of.

    **The columns are passed in rather than probed here**, because the two kinds of
    source learn them differently and neither should be the other's special case. A
    tool config is *probed*: one row fetched, names reported, no values — see
    :func:`probe_columns`. A graph has already been run by the time a plan is wanted,
    because there is nothing to probe, so its columns come off the result it produced.
    Deciding that here would put a graph-shaped branch inside the planner; deciding it
    in ``aggregate_service`` keeps this function about turning words into a plan.
    """
    proposed = await propose_plan(model, instruction, entry, columns)

    return validate_plan(proposed, columns, entry).payload()


# --------------------------------------------------------------------------
# Choosing the tool
# --------------------------------------------------------------------------


async def choose_tool(
    tools: Sequence[Mapping[str, Any]],
    instruction: str,
    model: Any,
    tool_name: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Which tool's records to group. Cheapest answer first.

    Named and resolvable, or only one available, means no model call at all — and
    between them those two cover nearly every real request, because the agent was
    told the tool names before it was asked anything.
    """
    available = [dict(entry) for entry in tools]

    if not available:
        raise ToolQueryError(
            "No tool is set up to have its records grouped this way. Whoever "
            "configured this agent needs to allow it on one of its tools first.",
            advice=NOT_AVAILABLE,
        )

    if tool_name:
        wanted = tool_name.strip().lower()
        for entry in available:
            if str(entry.get("tool_name") or "").strip().lower() == wanted:
                return entry

        raise ToolQueryError(
            f"There is no tool called '{tool_name}' that can have its records "
            f"grouped. Available: {_tool_names(available)}.",
            advice=NEEDS_RECONFIGURING,
        )

    if len(available) == 1:
        return available[0]

    return await _ask_which_tool(available, instruction, model)


async def _ask_which_tool(
    available: List[Dict[str, Any]],
    instruction: str,
    model: Any,
) -> Dict[str, Any]:
    """
    One call over a catalogue of names, descriptions and tables — and no data.

    The same fields ``prompt_builder`` already puts in the routing prompt, so a
    model choosing here sees nothing it was not already shown when it was choosing
    whether to call this at all.
    """
    if model is None:
        raise ToolQueryError(
            "More than one tool could be grouped and none was named, so there is "
            f"no way to tell which was meant. Name one of: {_tool_names(available)}.",
            advice=NEEDS_RECONFIGURING,
        )

    catalogue = available[:MAX_CATALOGUE_TOOLS]
    listing = "\n".join(
        f"- {entry.get('tool_name')}: {entry.get('description') or 'no description'} "
        f"(reads {', '.join(entry.get('table_names') or [entry.get('table_name')])})"
        for entry in catalogue
    )

    try:
        reply = await model.ainvoke(
            [
                (
                    "system",
                    "Pick the one tool whose records answer the request. Reply with "
                    "its name and nothing else.",
                ),
                ("human", f"Request: {instruction}\n\nTools:\n{listing}"),
            ],
            # Tagged for the same reason the planning call is: this is the machinery
            # choosing a tool, not the agent talking, and a bare tool name streamed into
            # the answer reads as the assistant having said it.
            config={"tags": [INTERNAL_CALL_TAG]},
        )
    except Exception as exc:  # noqa: BLE001 - one refusal for every provider failure
        logger.exception("Choosing a tool to aggregate failed")
        raise ToolQueryError(
            f"Which tool to group could not be decided: {exc}",
            advice=NOT_AVAILABLE,
        ) from exc

    chosen = _reply_text(reply).strip().strip("'\"`").lower()

    for entry in catalogue:
        if str(entry.get("tool_name") or "").strip().lower() == chosen:
            return entry

    raise ToolQueryError(
        f"No tool matched '{chosen}'. Available: {_tool_names(catalogue)}.",
        advice=NEEDS_RECONFIGURING,
    )


def _tool_names(entries: Sequence[Mapping[str, Any]]) -> str:
    return ", ".join(str(entry.get("tool_name") or "?") for entry in entries)


def _reply_text(reply: Any) -> str:
    """The text of a chat reply, whichever shape the provider returned it in."""
    content = getattr(reply, "content", reply)

    if isinstance(content, list):
        return " ".join(
            str(part.get("text", "")) if isinstance(part, Mapping) else str(part)
            for part in content
        )

    return str(content or "")


# --------------------------------------------------------------------------
# The columns
# --------------------------------------------------------------------------


async def probe_columns(
    entry: Mapping[str, Any],
    source: RecordSource,
) -> List[str]:
    """
    The column names the tool actually returns, from the database.

    ``probe_tool_query`` fetches one row and reports the names, applying every
    validator, active-table and active-column rule the real run will — so a plan
    can never be made against a column that was switched off after the tool was
    saved. It reports no values, which is what keeps this step data-free.

    **One source is probed, not all of them.** An iterating chain runs the same
    statement once per value, so every source returns the same columns by
    construction — probing the rest would be N round trips to be told the same thing.
    The source's ``label`` is added on top, because those keys are real columns of
    the rows the fold will see even though no database produced them.
    """
    try:
        probed = await probe_tool_query(
            entry["datasource"],
            dict(source.config or {}),
            source.table_name,
            sql_query=source.sql_query,
            table_names=list(source.table_names or []),
            value_bindings=list(source.value_bindings or []),
            agent_values=dict(source.agent_values or {}),
            sql_params=list(source.sql_params or []),
        )
    except ToolQueryError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("Probing columns for '%s' failed", entry.get("tool_name"))
        raise ToolQueryError(
            f"The columns this tool returns could not be read: {exc}",
            advice=NEEDS_RECONFIGURING,
        ) from exc

    columns = [str(name) for name in probed.get("columns") or []]
    columns.extend(name for name in (source.label or {}) if name not in columns)

    if not columns:
        raise ToolQueryError(
            "This tool returns no columns, so there is nothing to group by or "
            "measure.",
            advice=NEEDS_RECONFIGURING,
        )

    return columns


# --------------------------------------------------------------------------
# The plan
# --------------------------------------------------------------------------


async def propose_plan(
    model: Any,
    instruction: str,
    entry: Mapping[str, Any],
    columns: Sequence[str],
) -> AggregationPlan:
    """
    One structured-output call: the instruction and the column names, in, a plan out.

    A provider that cannot produce structured output fails here with a readable
    message rather than falling back to parsing JSON out of prose — a hand-rolled
    parse succeeding on a malformed plan is how a wrong column reaches the data.
    """
    if model is None:
        raise ToolQueryError(
            "No language model is configured, so the instruction could not be "
            "turned into a grouping.",
            advice=NOT_AVAILABLE,
        )

    system = _SYSTEM_PROMPT.format(
        functions=algebra.describe_supported(),
        operators=filters.describe_operators(),
        parts=", ".join(sorted(filters.PARTS)),
        max_filters=MAX_PLAN_FILTERS,
        max_group_by=MAX_GROUP_BY_COLUMNS,
        max_aggregations=MAX_PLAN_AGGREGATIONS,
    )
    human = (
        f"Request: {instruction}\n\n"
        f"Tool: {entry.get('tool_name')} — "
        f"{entry.get('description') or 'no description'}\n"
        f"Columns: {', '.join(columns)}"
    )

    try:
        proposed = await model.with_structured_output(AggregationPlan).ainvoke(
            [("system", system), ("human", human)],
            config={"tags": [INTERNAL_CALL_TAG]},
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Planning the aggregation failed")
        raise ToolQueryError(
            f"The instruction could not be turned into a grouping: {exc}",
            advice=NOT_AVAILABLE,
        ) from exc

    if isinstance(proposed, AggregationPlan):
        return proposed

    # Some providers hand back a plain dict rather than the model instance.
    return AggregationPlan.parse(dict(proposed or {}))


def validate_plan(
    proposed: AggregationPlan,
    columns: Sequence[str],
    entry: Mapping[str, Any],
) -> AggregationPlan:
    """
    Check a plan against the tool it will run on, whatever produced it.

    Runs for every plan, including one a person typed. The checks are in the order
    a reader would want them: is this expressible at all, is there anything to do,
    do the columns exist, and finally the two that are specific to folding in
    batches — the function must have an exact fold, and the tool must not already
    have averaged what is about to be averaged again.

    **The mode is decided here and never inferred again.** A plan with filters and no
    measures asks for the matching records; one with measures asks for numbers over
    them. Recording that as :data:`MODE_ROWS` / :data:`MODE_GROUPS` on the plan means
    the four places that behave differently read one field instead of each deciding
    for themselves what an empty ``aggregations`` list meant.
    """
    if proposed.unsupported:
        raise ToolQueryError(
            proposed.reason.strip()
            or "This cannot be answered by grouping and counting records.",
            advice=NOT_AVAILABLE,
        )

    # The grouping case comes first, and the order is the message rather than the rule:
    # a plan that grouped and measured nothing *did* ask for something, so naming that
    # mistake is more use than the generic "nothing was asked for" below.
    if not proposed.aggregations and proposed.group_by:
        raise ToolQueryError(
            "Records were grouped but nothing was measured over them, so there "
            "would be nothing to report per group. Ask for one of: "
            f"{algebra.describe_supported()}, or drop the grouping to see the "
            "matching records themselves.",
            advice=NEEDS_RECONFIGURING,
        )

    if not proposed.aggregations and not proposed.filters:
        raise ToolQueryError(
            "Nothing was asked for — no condition to narrow the records by and no "
            "measure to report over them. Ask for a condition, or for one of: "
            f"{algebra.describe_supported()}.",
            advice=NEEDS_RECONFIGURING,
        )

    known = {str(name).strip().lower(): str(name) for name in columns}

    group_by = [_resolved(name, known, entry) for name in proposed.group_by]

    if len(set(group_by)) != len(group_by):
        raise ToolQueryError(
            "The same column was given twice to group by.",
            advice=NEEDS_RECONFIGURING,
        )

    conditions = [
        _validated_filter(item, known, entry) for item in proposed.filters
    ]

    aggregations = [
        _validated_aggregation(entry_, known, entry, group_by)
        for entry_ in proposed.aggregations
    ]

    _assign_aliases(aggregations, group_by)

    reaggregated = algebra.reaggregated_average(
        [item.model_dump() for item in aggregations], entry.get("config"),
    )

    if reaggregated:
        raise ToolQueryError(reaggregated, advice=NEEDS_RECONFIGURING)

    return proposed.model_copy(
        update={
            "group_by": group_by,
            "aggregations": aggregations,
            "filters": conditions,
            "mode": MODE_GROUPS if aggregations else MODE_ROWS,
        },
    )


def _validated_filter(item, known, entry):  # noqa: ANN001, ANN201
    """
    One condition, with its column, operator, arity and date part checked.

    The operator and the part were already checked by the schema; they are asked again
    here because ``validate_plan`` runs for **every** plan including one a person
    typed, and the schema is only in the path of the ones a model produced.

    The arity check is the one worth reading. ``between`` with a single value has no
    second bound, and a filter that quietly became "greater than" would narrow the set
    differently from what was asked with nothing in the answer saying so.
    """
    refusal = (
        filters.unsupported_operator(item.operator)
        or filters.unsupported_part(item.part)
    )

    if refusal:
        raise ToolQueryError(refusal, advice=NEEDS_RECONFIGURING)

    values = filters.values_of(item.model_dump())

    refusal = (
        filters.wrong_arity(item.operator, values)
        or filters.out_of_range_part(item.part, values)
    )

    if refusal:
        raise ToolQueryError(refusal, advice=NEEDS_RECONFIGURING)

    return item.model_copy(
        update={"column": _resolved(item.column, known, entry)},
    )


def _validated_aggregation(item, known, entry, group_by):  # noqa: ANN001, ANN201
    """One measure, with its function and column checked against reality."""
    refusal = algebra.unsupported_function(item.type)

    if refusal:
        raise ToolQueryError(refusal, advice=NEEDS_RECONFIGURING)

    if not item.column:
        if item.type != "count":
            raise ToolQueryError(
                f"'{item.type}' needs a column to work on. Only counting records "
                "can be asked for on its own.",
                advice=NEEDS_RECONFIGURING,
            )
        return item.model_copy(update={"column": ""})

    return item.model_copy(update={"column": _resolved(item.column, known, entry)})


def _resolved(name: str, known: Mapping[str, str], entry: Mapping[str, Any]) -> str:
    """
    A column name matched case-insensitively and returned in the tool's spelling.

    Returning the *probed* spelling rather than the one that was asked for is what
    makes every later frame lookup exact: polars matches column names byte for
    byte, so "Region" resolved to "region" here is the difference between a
    grouping and a refusal three nodes later, where the column can no longer be
    explained.
    """
    resolved = known.get(str(name).strip().lower())

    if resolved:
        return resolved

    raise ToolQueryError(
        f"'{entry.get('tool_name')}' does not return a column called '{name}'. "
        f"It returns: {', '.join(known.values())}.",
        advice=NEEDS_RECONFIGURING,
    )


def _assign_aliases(aggregations, group_by) -> None:  # noqa: ANN001
    """
    Name every measure's output column, here and nowhere else.

    Never taken from the model: an alias is an output column name, and one that
    collided with a group key would overwrite it — the grouping would still look
    like a grouping and the key column would hold a total. Uniqueness is forced by
    suffixing rather than by refusing, because two ``sum``s of the same column is a
    silly plan, not a dangerous one.
    """
    taken = {str(name).strip().lower() for name in group_by}

    for item in aggregations:
        base = "record_count" if item.type == "count" and not item.column else (
            f"{item.type}_{item.column}"
        )
        alias, suffix = base, 2

        while alias.lower() in taken:
            alias, suffix = f"{base}_{suffix}", suffix + 1

        taken.add(alias.lower())
        # model_copy would return a new object the list no longer holds; the alias
        # is the one field assigned after validation, so it is set in place.
        object.__setattr__(item, "alias", alias)


def plan_summary(plan_data: Mapping[str, Any], entry: Mapping[str, Any]) -> str:
    """
    One sentence saying what was calculated, for the console and the tool output.

    Written from the validated plan rather than from the instruction, so what is
    shown is what actually ran — the point of failure this catches is a plan that
    quietly answered a nearby question.

    **The conditions are always named.** A filtered answer that does not say what it was
    filtered by is the same failure as a capped result that does not say it was capped:
    the number is right about a set the reader has to guess at. So "£48,200" becomes
    "sum of revenue from 'x', where department == Python and the month of invoice_date
    == 3" — and if the model narrowed the set in a way nobody asked for, that sentence
    is where it shows.
    """
    tool = entry.get("tool_name") or "the tool"
    conditions = filters.describe_filters(filters.specs_of(plan_data))
    where = f", where {conditions}" if conditions else ""

    if filters.mode_of(plan_data) == MODE_ROWS:
        return f"The records from '{tool}'{where}."

    measures = ", ".join(
        "the number of records" if algebra.function_of(item) == "count"
        and not algebra.column_of(item)
        else f"{algebra.function_of(item)} of {algebra.column_of(item)}"
        for item in plan_data.get("aggregations") or []
    )
    grouping = ", ".join(plan_data.get("group_by") or [])

    if grouping:
        return f"{measures} from '{tool}', grouped by {grouping}{where}."

    return f"{measures} from '{tool}', over every record{where}."
