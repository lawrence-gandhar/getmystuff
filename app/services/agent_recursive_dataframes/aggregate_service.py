"""
The bounds a run is held to, the words it fails in, and the one call that runs it.

Everything an operator can tune lives here, and so does every sentence a person
reads when a run is refused. Both for the same reason: a ceiling and the message
explaining it have to change together, and keeping the number in one file and the
apology in another is how they drift apart.

**Why there are ceilings at all.** A run holds one database cursor open for its
whole length and happens inside a chat turn — 120 seconds for a visitor, 900 for
the console. A run nobody can finish is refused before it starts rather than
abandoned halfway, which is the same judgement ``record_reader``'s export ceiling
makes and for the same reason.

**Why a cap refuses instead of truncating.** Every ceiling here ends a run with
nothing rather than with a partial answer. A total assembled from three quarters of
the records is a plausible number that is wrong, with nothing about it saying so —
and this application's whole guarantee is the figures it reports.
"""

import logging
import os
import uuid as uuid_pkg
from dataclasses import dataclass, field as dc_field, replace
from typing import Any, Dict, List, Mapping, Optional

from litestar.exceptions import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.agent_recursive_dataframes import (
    aggregate_graph,
    aggregate_planner,
    filter_algebra,
    partial_algebra,
    row_supply,
)
from app.services.data_agents import data_agent_service
from app.services.deep_agents.prompt_sync_service import collect_agent_tools
from app.services.deep_agents.query_executor import (
    MAX_CHAIN_ITERATIONS,
    NEEDS_RECONFIGURING,
    NOT_AVAILABLE,
    PROMPT_ROW_LIMIT,
    ToolQueryError,
)
from app.services.downloader_agents.base import record_reader
from app.services.downloader_agents.base.record_reader import RecordSource

logger = logging.getLogger(__name__)


def _int_env(name: str, default: int) -> int:
    """An operator-set integer, falling back rather than failing on nonsense."""
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        logger.warning("%s is not a whole number; using %d", name, default)
        return default

    return value if value > 0 else default


# How many records one slice holds — one batch, one worker, one partial aggregate.
# A slice is an internal unit of work and nothing but memory depends on its size: the
# fold is associative, so the same groups and the same numbers come out whatever it is.
#
# It is small, and that has a cost worth stating: 200,000 records is 250 waves and
# a thousand round trips, and at this size polars' fixed setup per slice dominates
# the aggregation itself, so the fan-out buys little and the run is round-trip
# bound. The answer is exact either way. This is an env var so the trade can be
# measured on real hardware rather than argued about.
AGGREGATE_CHUNK_ROWS = _int_env("AGGREGATE_CHUNK_ROWS", 200)

# How many slices are read and then aggregated together. The reader is one
# sequential cursor and cannot be shared, so this is not read concurrency — it is
# how many folds overlap while the next batch is being fetched.
AGGREGATE_WAVE_WIDTH = _int_env("AGGREGATE_WAVE_WIDTH", 4)

# The largest result set that will be aggregated this way. Never above the export
# ceiling: past MAX_EXPORT_ROWS `count_records` stops counting and reports a lower
# bound, so a higher number here would be a ceiling this application cannot check
# it is under.
AGGREGATE_MAX_SOURCE_ROWS = min(
    _int_env("AGGREGATE_MAX_SOURCE_ROWS", 200_000),
    record_reader.MAX_EXPORT_ROWS,
)

# How many distinct groups the running aggregate may hold. This is the memory
# ceiling: batching bounds the records held at once, but nothing bounds the groups
# except the grouping itself, and grouping by something near-unique turns a fold
# into a copy of the table.
MAX_GROUPS = _int_env("AGGREGATE_MAX_GROUPS", 100_000)

# How many finalised group rows are returned. ``None`` — every group.
#
# It was 200, matching the cap the rest of the tool path used, and it was the worst
# place in the application to have one: this feature exists to read *every* record and
# report an exact figure, and then returned the first 200 groups of however many there
# were. An operator grouping 40,000 projects by client got 200 clients and no
# indication that the other clients existed. ``MAX_GROUPS`` still bounds what the fold
# may hold in memory and refuses past it, which is a different question with a
# different answer: how much can be computed, not how much may be reported.
MAX_RESULT_ROWS: Optional[int] = None

# How many *matching records* are carried forward to be shown, when the answer is the
# records rather than numbers over them.
#
# This is the prompt's limit, not the query's, and the difference is the whole reason
# the row mode is honest. Every matching record is still read and counted — the answer
# says "200 of 4,317" — and what is bounded is only how many travel back, because a
# context window is a fixed size and the alternative to shortening is a failed turn.
# Taken from `query_executor.PROMPT_ROW_LIMIT` rather than set here, so a filtered result
# is shortened to exactly the same length as any other tool result.
KEEP_MATCHED_ROWS = PROMPT_ROW_LIMIT


@dataclass(frozen=True)
class SourceSet:
    """
    What a tool's whole result set is, as things the reader can read.

    Two fields rather than a bare list because an empty list has two meanings and
    they need different sentences: a chain that short-circuited (an inner tool
    matched nothing — an answer) and, in principle, nothing at all. ``stopped_by``
    names the tool, exactly as ``tool_chain_graph.ChainResult`` does.
    """

    sources: List[RecordSource] = dc_field(default_factory=list)
    stopped_by: str = ""

    @property
    def short_circuited(self) -> bool:
        return bool(self.stopped_by)


async def record_sources(entry: Mapping[str, Any]) -> SourceSet:
    """
    A tool entry from ``collect_agent_tools``, as the sources the reader can read.

    Reuses the export path's own source type rather than defining a second one, so
    an aggregation reads exactly what an export would: the same statement, the same
    re-validation, the same active-table and active-column checks. Passing the
    stored ``sql_query`` rather than a mode flag is what makes the two modes
    impossible to disagree about.

    **A list, because a nested tool may not be one query.** An ordinary tool is one
    source. A tool with children is still one source, but a *restricted* one — its
    chain is resolved first and the values it produced are carried on the source, so
    the totals are over the rows the tool actually returns. A tool with an iterating
    child is one source **per value**: the same statement, a different bind, and the
    fold across all of them is exactly the fold across one query's batches, because
    a partial aggregate does not care which cursor its rows came from.

    Short-circuits to no sources at all when an inner tool matched nothing: there
    are no records, and no query worth running to prove it.
    """
    base = RecordSource(
        datasource=entry["datasource"],
        config=dict(entry.get("config") or {}),
        table_name=entry["table_name"],
        sql_query=entry.get("sql_query"),
        table_names=list(entry.get("table_names") or []),
        sql_params=list(entry.get("sql_params") or []),
    )

    chain = entry.get("chain")

    if chain is None or not chain.children:
        return SourceSet(sources=[base])

    # Imported here rather than at module scope: tool_chain_graph pulls LangGraph,
    # and the rest of this module — the ceilings, the messages — is useful and
    # testable without it. The same reason model_factory is imported inside
    # `run_for_agent`.
    from app.services.tool_configs.tool_chain_graph import resolve_chain_bindings

    resolved = await resolve_chain_bindings(chain)

    if resolved.short_circuited:
        return SourceSet(stopped_by=resolved.stopped_by)

    if not resolved.iterates:
        return SourceSet(
            sources=[replace(base, value_bindings=list(resolved.bindings))],
        )

    if len(resolved.iteration_values) > MAX_CHAIN_ITERATIONS:
        raise ToolQueryError(
            f"This tool runs its query once for each of "
            f"{len(resolved.iteration_values)} values, which is more than the "
            f"{MAX_CHAIN_ITERATIONS} runs allowed in one answer.",
            advice=NEEDS_RECONFIGURING,
        )

    return SourceSet(sources=[
        replace(
            base,
            value_bindings=[
                *resolved.bindings,
                {
                    "reference": resolved.iteration_reference,
                    "values": [value],
                    "expanding": False,
                },
            ],
            label=(
                {resolved.iteration_alias: value}
                if resolved.iteration_alias else None
            ),
        )
        for value in resolved.iteration_values
    ])


# --------------------------------------------------------------------------
# The words
# --------------------------------------------------------------------------


def too_large_message(
    total: int,
    is_lower_bound: bool,
    subject: str = "tool",
) -> str:
    """
    Why a result set is too big to aggregate this way, and what to do instead.

    Said before a single record is read. Both numbers are named because "too large"
    on its own tells the reader nothing about how much too large, and the advice is
    concrete because the good answer here — let the database group it — is better
    than this feature anyway.

    ``subject`` is "tool" or "graph", from the supply. Not cosmetic: the advice differs,
    because a graph's own SQL nodes are where its result set is narrowed and telling
    somebody to edit "the tool's filters" would send them to the wrong page.
    """
    how_many = (
        f"more than {AGGREGATE_MAX_SOURCE_ROWS:,}" if is_lower_bound
        else f"{total:,}"
    )
    remedy = (
        "Narrow the graph's own query nodes so it returns less."
        if subject == "graph" else
        "Narrow the tool's filters, or save a SQL query tool that lets the database "
        "do the grouping, which has no such limit."
    )

    return (
        f"This {subject} returns {how_many} records in total, and at most "
        f"{AGGREGATE_MAX_SOURCE_ROWS:,} can be read this way — every record has "
        "to be read to be counted, and a run that cannot finish inside one "
        f"conversation is refused rather than abandoned halfway. {remedy}"
    )


def too_many_groups_message(groups: int) -> str:
    """
    Why a run stopped once the grouping stopped being a grouping.

    Nothing partial is reported. A list of the first hundred thousand groups looks
    exactly like a complete answer.
    """
    return (
        f"Grouping this data produced more than {MAX_GROUPS:,} groups, which is "
        "more than can be held at once — usually a sign of grouping by something "
        "nearly unique, such as an id or a timestamp. Nothing has been reported, "
        "because a partial list of groups reads as a complete one. Group by "
        "something coarser and try again."
    )


def no_records_message() -> str:
    """A run that read nothing. An answer, not a failure — see the graph."""
    return "This tool returned no records, so there was nothing to group."


def nothing_matched_message(read: int) -> str:
    """
    A filtered run where records were read and none of them matched.

    Worth saying separately from :func:`no_records_message`, and the difference is the
    whole point: "there are no records" and "there are 4,317 records and none is in
    March" are different facts, and a bare empty result is neither. Naming how many were
    read is what makes it a finding rather than a shrug.
    """
    return (
        f"All {read:,} record(s) were read and none of them matched, so the answer is "
        "that there are none. That is a result, not a failure."
    )


def graph_asked_message(tool_name: str, question: str) -> str:
    """
    Why a graph that stops to ask something cannot be read this way.

    A graph with a Human node pauses mid-run and waits for an answer. Every other owner
    of a graph carries that pause — ``graph_tool_factory`` relays the question and offers
    an ``answer_`` tool, a flow ends the turn on it — and this one deliberately does not:
    resuming would mean holding a half-read result set across two conversation turns,
    which is a second kind of state for a feature whose whole shape is "read it all now".

    Refused with the question quoted and the graph's own tool named, because the fix is
    real and one step away: call the graph directly, answer it, and the graph gives its
    result without this feature involved.
    """
    asked = (question or "").strip()
    quoted = f' It asks: "{asked}"' if asked else ""

    return (
        f"The graph '{tool_name}' stops part-way through to ask a question, so its "
        f"records cannot be read and filtered in one step.{quoted} Call "
        f"'{tool_name}' directly and answer it instead."
    )


async def graph_rows(entry: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """
    A published graph's whole result, run now, as the records to be filtered.

    **``full_result`` and never ``outcome.rows``.** The latter is off ``result_preview``,
    which is capped at twenty rows — filtering a twenty-row sample and reporting the
    count of what matched in it would be a wrong number with nothing about it saying so.
    That is the same distinction ``graph_runner.full_result`` documents, and this is
    exactly the kind of caller its docstring is addressed to: one that *uses* the values
    rather than describing them.

    Run as the graph's **author**, not as whoever is asking. A graph shared with a
    workspace reads datasources scoped to whoever built it, and ``entry["user_id"]``
    is what ``_graph_entry`` recorded for that reason.

    Every failure and the pause are raised as ``ToolQueryError``, because that is what
    the layer above catches — the graph runner returns them as outcomes so that each of
    its four owners can phrase them, and this owner's phrasing is a refusal.
    """
    from app.services.graph_designer import graph_runner

    tool_name = str(entry.get("tool_name") or "the graph")

    outcome = await graph_runner.run_graph(
        int(entry["user_id"]), str(entry["graph_uuid"]),
    )

    if outcome.asks:
        raise ToolQueryError(
            graph_asked_message(tool_name, str((outcome.question or {}).get("prompt") or "")),
            advice=NOT_AVAILABLE,
        )

    if not outcome.finished:
        raise ToolQueryError(
            f"The graph '{tool_name}' did not finish, so it produced no records to "
            f"read: {outcome.reason or 'no reason was given'}",
            advice=NEEDS_RECONFIGURING,
        )

    result = await graph_runner.full_result(
        int(entry["user_id"]), outcome.run_id,
    )
    rows = _as_records(result)

    logger.info(
        "Graph '%s' produced %d record(s) for filtering (run %s)",
        tool_name, len(rows), outcome.run_id,
    )

    return rows


def _as_records(result: Any) -> List[Dict[str, Any]]:
    """
    A graph's last output as a list of records, whatever shape it ended in.

    A graph's nodes produce four shapes — rows, a list of values, a dict, a scalar — and
    only the first is already records. The other three are lifted into one-column
    records rather than refused, because "the departments a graph picked, filtered to
    the ones starting with P" is a reasonable request and a list is what that graph
    returns.

    The column is named ``value`` for a list or a scalar. That name then appears in the
    columns the planner is given, so a model filtering on it is filtering on something
    it was actually shown.
    """
    if isinstance(result, Mapping):
        rows = result.get("rows")

        if isinstance(rows, list):
            return [dict(row) for row in rows if isinstance(row, Mapping)]

        return [dict(result)]

    if isinstance(result, (list, tuple)):
        return [
            dict(item) if isinstance(item, Mapping) else {"value": item}
            for item in result
        ]

    return [] if result is None else [{"value": result}]


def chain_stopped_message(stopped_by: str) -> str:
    """
    A run whose inner tool matched nothing.

    An answer and not a failure, exactly as ``tool_chain_graph.describe_stop`` says
    for the agent's own tool call — and worth saying separately from
    :func:`no_records_message`, because "no clients matched" and "those clients have
    no invoices" are two different things and a bare zero is neither.
    """
    return (
        f"The inner tool '{stopped_by}' returned nothing, so this tool has no "
        "records to group. That is an answer, not a failure: nothing matched."
    )


# --------------------------------------------------------------------------
# The one call
# --------------------------------------------------------------------------


async def aggregate(
    tools: List[Dict[str, Any]],
    instruction: str,
    model: Any,
    tool_name: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Turn an instruction into an aggregate: choose a tool, plan it, run it.

    The single entry point both callers use — the console route and the agent tool
    — so the two cannot end up doing this differently. Returns the view described
    in ``AggregationResultView``.

    ``model`` may be ``None`` when ``tool_name`` names the tool and there is only
    one plausible plan to make, but in practice the planner needs it; the planner
    is what decides whether it is called, and it avoids the call whenever the
    answer is already determined.

    Failures are not caught here. A ``ToolQueryError`` belongs to whoever called:
    the tool wrapper phrases it for a model, the route renders it into an alert.

    The order is deliberate: the tool is chosen, then its **records** are resolved,
    and only then is the plan made. Planning needs the columns, and for a tool config
    the columns come from probing — probing a nested tool without its chain resolved
    would ask a wider question than the totals will answer.

    **A graph resolves further than a tool config does, and that is unavoidable.** There
    is nothing to probe on a drawing: the only way to know what columns it returns is to
    run it. So a graph is run first and planned against the result it produced, while a
    tool config is probed for one row and planned before anything is read. Both end up
    at the same place — a validated plan and a supply of records — which is why
    everything after this function is indifferent to which it had.
    """
    text = aggregate_planner.validated_instruction(instruction)
    entry = await aggregate_planner.choose_tool(
        tools, text, model, tool_name=tool_name,
    )

    run_id = uuid_pkg.uuid4().hex

    if entry.get("kind") == "graph":
        supply, columns = await _graph_supply(entry)
    else:
        resolved = await record_sources(entry)

        if resolved.short_circuited:
            # An inner tool matched nothing, so there is nothing to read — and no plan
            # worth an LLM call to describe rows that do not exist. Reported as an empty
            # result with a sentence saying why, rather than as a failure.
            return _nothing_to_read(entry, chain_stopped_message(resolved.stopped_by))

        supply = row_supply.for_sources(resolved.sources)
        columns = await aggregate_planner.probe_columns(entry, resolved.sources[0])

    plan = await aggregate_planner.plan(entry, columns, text, model)

    logger.info(
        "Reading '%s' as run %s (%s): %s",
        entry.get("tool_name"),
        run_id,
        filter_algebra.mode_of(plan),
        aggregate_planner.plan_summary(plan, entry),
    )

    outcome = await aggregate_graph.run_aggregation(supply, plan, run_id)

    return {
        "tool_name": entry.get("tool_name") or "",
        "tool_id": entry.get("uuid") or entry.get("graph_uuid"),
        "datasource_name": entry.get("datasource_name") or "",
        # In row mode the answer's columns are the source's own, because the records
        # come back as they were. `result_columns` describes a fold, and asking it about
        # a plan that does not fold would name the group keys and no measures at all.
        "columns": (
            list(columns) if filter_algebra.mode_of(plan) == filter_algebra.MODE_ROWS
            else partial_algebra.result_columns(plan)
        ),
        "summary": aggregate_planner.plan_summary(plan, entry),
        **outcome,
    }


async def _graph_supply(entry: Mapping[str, Any]):  # noqa: ANN202
    """
    A published graph, run, as a supply of records and the columns it produced.

    The columns come off the **first record** rather than a union across all of them. A
    graph's last data-producing node is one statement or one value, so its records are
    uniform by construction; scanning 200,000 of them to confirm that would cost a pass
    over the whole result to learn what the first row already says.
    """
    rows = await graph_rows(entry)
    columns = [str(name) for name in (rows[0].keys() if rows else [])]

    return row_supply.for_rows(rows), columns


def _nothing_to_read(entry: Mapping[str, Any], summary: str) -> Dict[str, Any]:
    """An empty result carrying the sentence explaining why it is empty."""
    return {
        "tool_name": entry.get("tool_name") or "",
        "tool_id": entry.get("uuid") or entry.get("graph_uuid"),
        "datasource_name": entry.get("datasource_name") or "",
        "mode": filter_algebra.MODE_GROUPS,
        "columns": [],
        "summary": summary,
        "rows": [],
        "group_count": 0,
        "records_read": 0,
        "total_records": 0,
    }


# --------------------------------------------------------------------------
# The console
# --------------------------------------------------------------------------


async def aggregatable_tools(
    db: AsyncSession,
    user_id: int,
    agent_id: uuid_pkg.UUID,
) -> List[Dict[str, Any]]:
    """
    One agent's tools that may have their whole result set read.

    Reuses ``collect_agent_tools`` rather than querying afresh, so the console
    offers exactly the set the agent itself would be given — including tools
    inherited by being embedded in another, which are just as callable, and any
    published graph the agent can call.
    """
    agent = await data_agent_service.get_data_agent(db, user_id, agent_id)
    tools = await collect_agent_tools(db, agent.id)

    return readable_tools(tools)


def readable_tools(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    The entries of one agent's tool list whose whole result may be read and filtered.

    One flag for both kinds. A tool config carries
    ``tool_configs.allow_recursive_aggregate``; a graph carries
    ``tool_graphs.allow_recursive_aggregate``, and ``_graph_entry`` puts it under the
    same key — so the filter is one expression rather than two, and a third kind of
    source added later opts in the same way or not at all.
    """
    return [entry for entry in tools if entry.get("allow_recursive_aggregate")]


def public_id(entry: Mapping[str, Any]) -> str:
    """
    The public identifier of a tool entry, whichever kind it is.

    A tool config's is ``uuid``; a graph's is ``graph_uuid``. One function so a caller
    cannot reach for the wrong key — the failure that does is a ``KeyError`` at best and
    a blank identifier at worst.
    """
    return str(entry.get("uuid") or entry.get("graph_uuid") or "")


async def get_console_view(
    db: AsyncSession,
    user_id: int,
    agent_id: Optional[uuid_pkg.UUID] = None,
) -> Dict[str, Any]:
    """
    What the console page needs: the agents to choose from and, once one is chosen,
    its aggregatable tools.

    The tool list is exposed by public uuid and name only — the console never sees
    a query, and never needs to.

    ``uuid`` falls back to ``graph_uuid`` because a graph entry has no ``uuid`` key:
    it holds the graph's public id under its own name, so that an entry carrying both
    a graph and the agent it was collected for cannot be ambiguous about which a bare
    ``uuid`` meant. Reading ``tool["uuid"]`` off one is the mistake that took the
    agent console down, and it is worth not repeating twice.
    """
    agents = await data_agent_service.get_agent_views(db, user_id)
    tools: List[Dict[str, Any]] = []

    if agent_id is not None:
        tools = [
            {
                "uuid": public_id(tool),
                "tool_name": tool["tool_name"],
                "description": tool.get("description") or "",
                "datasource_name": (
                    tool.get("datasource_name")
                    or ("a designed graph" if tool.get("kind") == "graph" else "")
                ),
            }
            for tool in await aggregatable_tools(db, user_id, agent_id)
        ]

    return {
        "agents": [agent for agent in agents if agent.get("is_active")],
        "tools": tools,
        "selected_agent": str(agent_id) if agent_id else "",
    }


async def run_for_agent(
    db: AsyncSession,
    user_id: int,
    agent_id: Optional[uuid_pkg.UUID],
    instruction: str,
    tool_id: Optional[uuid_pkg.UUID] = None,
) -> Dict[str, Any]:
    """
    The console's own entry point: resolve the agent, then aggregate.

    Ownership is resolved here rather than in the route, and a tool that is not the
    agent's own is simply not in the list — so a uuid guessed from another agent
    resolves to nothing rather than to somebody else's records.
    """
    if agent_id is None:
        raise HTTPException(status_code=400, detail="Choose a data agent first.")

    tools = await aggregatable_tools(db, user_id, agent_id)

    if not tools:
        raise HTTPException(
            status_code=400,
            detail=(
                "None of this agent's tools allow whole-result grouping. Switch "
                "'Allow whole-result grouping' on for one of them in Tool Configs."
            ),
        )

    tool_name = None

    if tool_id is not None:
        chosen = next(
            (tool for tool in tools if public_id(tool) == str(tool_id)), None,
        )
        if chosen is None:
            raise HTTPException(
                status_code=404,
                detail="That tool was not found, or does not allow grouping.",
            )
        tool_name = chosen["tool_name"]

    # Imported here rather than at module scope: model_factory pulls the provider
    # SDKs, and everything else in this module — the ceilings, the messages, the
    # graph — is useful and testable without them.
    from app.services.deep_agents import model_factory

    model = await model_factory.build_chat_model(db, user_id)

    try:
        return await aggregate(tools, instruction, model, tool_name=tool_name)
    except ToolQueryError as exc:
        # An operator is reading this, not a model, so the advice — which is
        # addressed to a model talking to a visitor — is dropped rather than shown.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
