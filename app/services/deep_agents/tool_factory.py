"""
Turning tool configs into the tools a Deep Agent can call.

One enabled ``ToolConfig`` becomes one LangChain tool, named exactly as the
operator named it. The tool's whole behaviour is fixed by the stored config:
calling it runs that query and nothing else.

**A tool's arguments are values, never query structure.** By default a tool takes
no arguments at all: the config declares its filters, columns and grouping, and the
model's only decision is *which* tool to call. An operator can open one filter's
*value* to the agent (``agent_supplied`` on that filter), which adds one string field
to this tool's schema — see :func:`_arguments_schema`. What that never adds is a way
to choose a column, an operator or a table, or to relax any other filter: those still
come from the stored config and are resolved against the reflected schema, and the
supplied value is bound as a parameter exactly as a stored one is. So the property
that matters is unchanged — no model-written text reaches the query — while a tool
scoped to ``status = 'paid'`` stays scoped to it.

**A nested tool is still one tool with no arguments.** A tool that embeds others
runs them first — as a LangGraph, see
:mod:`app.services.tool_configs.tool_chain_graph` — and its own query is restricted
to what they returned. The model neither supplies nor sees any of that: it calls one
name, the chain is fixed by the operator exactly as a filter is, and what comes back
is the outermost query's rows. The children remain tools in their own right, so the
agent can also call one directly when that is the question being asked.

The rows a tool returns are the only data the model ever sees.
"""

import logging
from typing import Any, Dict, List, Optional, Type

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field, create_model

from app.services.agent_recursive_dataframes.aggregate_tools import (
    AggregateContext,
    build_aggregate_tools,
)
from app.services.deep_agents.query_executor import (
    ToolQueryError,
    describe_result,
    execute_tool_query,
)
from app.services.downloader_agents.base.download_tools import (
    DownloadContext,
    build_download_tools,
    describe_tool_result,
)
from app.services.tool_configs.tool_chain_graph import (
    build_chain_graph,
    chain_node_name,
    describe_question,
    describe_stop,
    graph_values,
    run_chain,
)
from app.utils.query_joins import RDBMS_DB_TYPES

logger = logging.getLogger(__name__)


class _NoArguments(BaseModel):
    """
    The argument schema for a data tool with no agent-supplied filter: empty.

    Declared explicitly rather than left for LangChain to infer, so the schema
    advertised to the model is unambiguously "no parameters" across all three
    providers' tool-calling formats. Still the default and still the common case —
    a tool only grows parameters when an operator opens a filter for one.
    """


def _arguments_schema(
    tool_name: str,
    config: Dict[str, Any],
    sql_params: Optional[List[dict]] = None,
) -> Type[BaseModel]:
    """
    The argument schema for one tool: one field per value the operator opened.

    Two sources, one schema. Builder mode opens a *filter* — ``config["filters"]``
    with ``agent_supplied`` set — because there the parameter has a column and an
    operator the operator chose. SQL mode has no filters to open, so it declares the
    values separately (``sql_params``) and the statement says what they compare
    against. Exactly one of the two is populated for any tool, because
    ``tool_config_service._validated_fields`` writes both columns on every save.

    **What the model may decide, and what it may not.** A field here carries a
    *value* and nothing else. The column it is compared against, the operator, the
    table and every other filter come from the stored config and are resolved
    against the reflected schema in ``query_executor._filter_conditions``, which
    binds the value as a parameter exactly as it binds a stored one. So a model
    cannot reach a column an operator did not open, cannot change ``>`` to ``<``,
    and cannot widen a filter the operator left fixed — the whole of its influence
    is the right-hand side of one comparison the operator chose to open.

    **Every field is a string, and the coercion happens at the database.** A schema
    typed from the reflected column would need a reflection here, at prompt-build
    time, for a tool that may never be called — and would still have to be re-checked
    at execution, because the column can change underneath a saved config.
    ``_coerced_value`` already types the value against the live column; declaring a
    string keeps one answer to "what type is this" instead of two that can disagree.

    Optional fields are ``None`` by default, which is what
    ``_filter_conditions`` reads as "omit this clause".
    """
    fields: Dict[str, Any] = {}

    for entry in config.get("filters") or []:
        if not entry.get("agent_supplied"):
            continue

        name = str(entry.get("param") or "").strip()

        if not name:
            continue

        _add_field(
            fields,
            name,
            entry,
            fallback=(
                f"Value to compare {entry.get('column')} against "
                f"with {entry.get('operator')}."
            ),
        )

    for entry in sql_params or []:
        name = str((entry or {}).get("param") or "").strip()

        if not name:
            continue

        # No fallback worth writing: nothing here knows what the statement compares
        # this value against, which is the whole reason a SQL parameter has to be
        # described by the operator. A field with no description is worse than a good
        # sentence and better than a wrong one — the same bargain the filter path
        # makes when the operator leaves the description blank.
        _add_field(fields, name, entry, fallback=f"Value for '{name}'.")

    if not fields:
        return _NoArguments

    return create_model(f"{tool_name}_Arguments", **fields)


def _add_field(
    fields: Dict[str, Any],
    name: str,
    entry: dict,
    fallback: str,
) -> None:
    """
    One field on the arguments model, shared by both kinds of opened value.

    Every field is a ``str``, and the coercion happens later — against the reflected
    column for a builder filter, against the operator's declared type for a SQL
    parameter. Declaring a type here would need a reflection at prompt-build time for
    a tool that may never be called, and would still have to be re-checked at
    execution because the column can change underneath a saved config.
    """
    required = bool(entry.get("required", True))
    description = str(entry.get("description") or "").strip() or fallback

    fields[name] = (
        str if required else Optional[str],
        Field(..., description=description) if required
        else Field(default=None, description=description),
    )


def build_agent_tools(
    tools: List[dict],
    download_context: Optional["DownloadContext"] = None,
    aggregate_context: Optional["AggregateContext"] = None,
) -> List[StructuredTool]:
    """
    Build a callable tool per entry in ``tools``.

    ``tools`` is what
    :func:`app.services.deep_agents.prompt_sync_service.collect_agent_tools`
    returns — the same list the routing prompt is built from, so the prompt can
    never describe a tool that was not created.

    ``download_context`` says who is asking, and switches on the export feature: a
    result larger than ``DISPLAY_ROW_LIMIT`` gets an exact count and an offer to send
    the whole set as a file, and two further tools are added for confirming that offer
    and reporting on it. Without it the tools behave exactly as they did before — which
    is what a caller with no conversation to scope an export to should get, rather than
    an offer nobody could act on.

    ``aggregate_context`` adds one further tool, for grouping a tool's whole result
    set rather than the capped rows it returns — see
    ``app/services/agent_recursive_dataframes/``. It is ``None`` unless at least one
    of ``tools`` was opted in, so an agent with none gets exactly the tool list it
    got before that feature existed.

    An entry marked ``kind: "graph"`` is an attached Graph Designer graph rather than a
    tool config, and is built by ``app.services.graph_designer.graph_tool_factory``. It is
    dispatched here rather than inside :func:`_build_tool` because a graph shares none of
    that function's assumptions — no datasource row, no table, no chain — and threading a
    second shape through it would make every line there conditional. An agent with no
    graph is unaffected: the list simply contains no such entry.
    """
    built: List[StructuredTool] = []

    for entry in tools:
        if entry.get("kind") == "graph":
            # Lazy import: graph_designer reads query_executor from this package, so a
            # module-scope import would be a cycle. Same call `aggregate_service` and
            # `query_test_service` make in the other direction.
            from app.services.graph_designer.graph_tool_factory import build_graph_tools

            built.extend(build_graph_tools(entry))
            continue

        built.append(_build_tool(entry, download_context))

        # A tool whose chain embeds a graph that can ask something needs a companion to
        # resume it, for the same reason a graph tool does: a tool that can pause is
        # useless to an agent that cannot carry on. Offered only when the chain actually
        # contains such a graph, so no ordinary tool gains a second entry.
        if _asking_node(entry.get("chain")) is not None:
            built.append(_answer_chain_tool(entry, download_context))

    if download_context is not None:
        built.extend(build_download_tools(download_context))

    if aggregate_context is not None:
        built.extend(build_aggregate_tools(aggregate_context))

    return built


def _build_tool(
    entry: dict,
    download_context: Optional["DownloadContext"] = None,
) -> StructuredTool:
    """
    One tool.

    The datasource row is captured in the closure rather than re-fetched per call:
    a single agent run may call several tools, and re-reading (and re-decrypting)
    the same datasource each time buys nothing. Connection pooling still happens
    in ``db_utils.get_engine``, keyed by URL.
    """
    datasource = entry["datasource"]
    config: Dict[str, Any] = entry.get("config") or {}
    table_name: str = entry["table_name"]
    # Every table the tool reads, primary first. Only a SQL-mode tool needs it — a
    # built query's tables are its base table plus its joins, which the executor
    # already knows — and there it is the only record of what the statement reads.
    table_names: List[str] = list(entry.get("table_names") or [])
    tool_name: str = entry["tool_name"]
    # Non-empty for a SQL-mode tool, which the executor runs as written instead of
    # rebuilding from `config`. Passed as the stored value rather than as a mode
    # flag so the two can never disagree.
    sql_query: Optional[str] = entry.get("sql_query")
    # The values the operator declared for a SQL-mode statement. Empty for every
    # builder-mode tool, whose equivalent lives inside `config["filters"]`.
    sql_params: List[dict] = list(entry.get("sql_params") or [])
    # The tools this one embeds, resolved into a tree. Present and childless for an
    # ordinary tool, which is why the graph is only built when it has children:
    # compiling one per call for every tool would cost every tool something to buy
    # nothing.
    chain = entry.get("chain")
    nested = bool(chain is not None and chain.children)
    # Compiled once, here, and reused by every call — a nested tool call is then an
    # `ainvoke`, not a rebuild.
    graph = build_chain_graph(chain) if nested else None

    arguments = _arguments_schema(tool_name, config, sql_params)

    async def run_tool(**agent_values: Any) -> str:
        """
        Execute this tool's stored query and describe the rows.

        ``agent_values`` is empty for every tool whose values are all fixed, which
        is the common case and the default. Where an operator opened one — a builder
        filter or a declared SQL parameter — the value arrives here already checked
        against ``arguments`` by LangChain and is handed to the executor, which binds
        it. See ``_arguments_schema``.
        """
        try:
            if nested:
                result = await run_chain(chain, graph, agent_values)

                waiting = describe_question(result, tool_name)

                if waiting:
                    # An embedded graph stopped to ask somebody something. Not a
                    # failure and not an empty result — the chain is open, and this
                    # returns the question plus how to come back to it.
                    return waiting

                if result.short_circuited:
                    # Not a failure: an inner tool matched nothing, so there is
                    # nothing to report. Said out loud, because "0 rows" alone
                    # leaves the model unable to tell that from missing data.
                    return f"{describe_result([])} {describe_stop(result)}"

                # A nested tool's rows are the outermost query's rows, so they are as
                # exportable as any other tool's — the export re-runs the chain the
                # same way this call did.
                return await describe_tool_result(entry, result.rows, download_context)

            rows = await execute_tool_query(
                datasource,
                config,
                table_name,
                sql_query=sql_query,
                table_names=table_names,
                agent_values=agent_values,
                sql_params=sql_params,
            )
        except ToolQueryError as exc:
            # Returned as tool output, not raised: the agent has to be told the
            # tool failed so it can say so. Raising would abort the whole turn and
            # give the visitor a 500 for what is a recoverable, explainable state.
            logger.warning("Tool %s could not run: %s", tool_name, exc)
            # `for_agent` and not `exc` itself: the fault is stated the same way to
            # everyone, and this is the one caller that also needs telling the model
            # what to do about it (query_executor.ToolQueryError).
            return f"TOOL FAILED: {exc.for_agent}"

        return await describe_tool_result(entry, rows, download_context)

    return StructuredTool.from_function(
        coroutine=run_tool,
        name=tool_name,
        description=_tool_description(entry),
        args_schema=arguments,
    )


def _asking_node(chain):  # noqa: ANN001, ANN201
    """
    The graph node in this chain that can stop to ask a person something, or ``None``.

    One function for both callers — whether to offer an answering tool at all, and which
    node an answer finally belongs to — because two walks over the same tree looking for
    the same thing is two chances to disagree about what "can ask" means.

    Read from the **drawing**, not from a flag on the row: a graph that stops asking after
    an edit should stop offering an answering tool without anybody remembering to update a
    column.

    The first such node wins. A chain with two asking graphs would need the run id matched
    to a node, which means storing which run belongs to which — state this path
    deliberately does not keep, since the run id the model carries *is* that state. It is
    also a shape nothing can currently produce: a graph child is a leaf and a parent's
    children are validated when they are saved.
    """
    from app.models.graph_designer import NODE_HUMAN

    if chain is None:
        return None

    for node in chain.walk():
        if not node.is_graph:
            continue

        nodes = (getattr(node.graph, "graph_data", None) or {}).get("nodes") or []

        if any(
            isinstance(item, dict) and str(item.get("type")) == NODE_HUMAN
            for item in nodes
        ):
            return node

    return None


def _answer_chain_tool(
    entry: dict,
    download_context: Optional["DownloadContext"] = None,
) -> StructuredTool:
    """
    The companion that answers a question an embedded graph asked, and finishes the tool.

    Two steps, and the second is the point: resuming the graph is not the answer the user
    asked for. So this hands the answer to the paused run, waits for it, reads its values,
    and then **re-runs the chain with those values supplied** — through ``run_chain``'s
    ``resolved``, so the graph is not run a second time and nobody is asked the same
    question twice. What comes back is the rows the user originally wanted.

    Named after the tool rather than being one generic answering tool, exactly as
    ``graph_tool_factory`` names its own: an agent holding two such tools has two
    unambiguous ways to answer, instead of one that has to be told which chain it means.
    """
    tool_name = str(entry.get("tool_name") or "tool")
    chain = entry.get("chain")
    compiled = build_chain_graph(chain) if chain is not None else None

    class _Answer(BaseModel):
        run_id: str = Field(
            description="The run id the question came with. Copy it exactly.",
        )
        answer: str = Field(description="What the user replied, as they said it.")

    async def answer_chain_question(run_id: str, answer: str) -> str:
        """Answer the paused graph, then finish the tool it was holding open."""
        from app.services.graph_designer import graph_runner

        asking = _asking_node(chain)

        if asking is None:
            # The graph stopped asking between this tool being built and being called.
            return (
                "This tool no longer asks anything, so there is nothing to answer. Call "
                f"'{tool_name}' again."
            )

        user_id = int(asking.graph.user_id)
        outcome = await graph_runner.answer_graph_run(user_id, run_id, answer)

        if outcome.asks and outcome.reason:
            # The answer did not fit the question — "maybe" to a yes/no. The one failure
            # here the user can fix, so the model asks again rather than reporting that
            # the tool is broken. Same distinction `graph_tool_factory` draws.
            return (
                f"That answer was not accepted: {outcome.reason} Ask the user again for "
                "an answer of that kind, then call this tool with the same run id."
            )

        if outcome.asks:
            question = str((outcome.question or {}).get("prompt") or "").strip()
            return (
                f"There is another question. Ask the user exactly this, word for word: "
                f"\"{question}\"\nThen call this tool again with run_id "
                f"\"{outcome.run_id}\" and what they said."
            )

        if not outcome.finished:
            return (
                f"TOOL FAILED: {outcome.reason or 'The run could not be completed.'} "
                "Tell the user this cannot be answered at the moment and that it needs "
                "looking at by whoever set it up. Do NOT ask them to rephrase."
            )

        values = graph_values(
            await graph_runner.full_result(user_id, outcome.run_id),
            str(asking.child_column or ""),
        )

        try:
            result = await run_chain(
                chain, compiled, {}, resolved={chain_node_name(asking): values},
            )
        except ToolQueryError as exc:
            logger.warning("Tool %s could not finish after an answer: %s", tool_name, exc)
            return f"TOOL FAILED: {exc.for_agent}"

        if result.short_circuited:
            return f"{describe_result([])} {describe_stop(result)}"

        return await describe_tool_result(entry, result.rows, download_context)

    return StructuredTool.from_function(
        coroutine=answer_chain_question,
        name=f"answer_{tool_name}",
        description=(
            f"Give an answer to a question that '{tool_name}' asked. Call this only "
            "after the user has answered that question, passing the run id the question "
            "came with and what they said."
        ),
        args_schema=_Answer,
    )


def _tool_description(entry: dict) -> str:
    """
    What the model is shown for this tool in the tool-calling schema.

    Kept short and behavioural. The full account of what the tool returns lives in
    the routing prompt (prompt_builder), which the model reads once, rather than
    being repeated in every tool schema on every request — the same information in
    both places would just cost tokens.
    """
    description = (entry.get("description") or "").strip()
    table_name = entry.get("table_name") or ""

    if description:
        return f"{description} Runs a fixed pre-approved query over {table_name}."

    return (
        f"Runs a fixed pre-approved query over {table_name}. See the tool list in "
        "your instructions for the fields it returns."
    )


def tool_names(tools: List[dict]) -> List[str]:
    """The tool names for one agent — used by the console and for logging."""
    return [str(entry.get("tool_name")) for entry in tools if entry.get("tool_name")]


def find_unsupported_tools(tools: List[dict]) -> List[str]:
    """
    Names of tools that cannot run, with the reason.

    Two causes, both permanent until the operator changes the config: a non-relational
    datasource, and a RIGHT JOIN (which ``query_executor._apply_joins`` refuses rather
    than approximating). Surfaced on the agent's console up front, so neither is
    discovered only when a visitor happens to ask a question that routes to one.

    The RIGHT JOIN check is a *builder-mode* limitation — it comes from assembling
    the query out of SQLAlchemy join operands — so it is not applied to a SQL-mode
    tool, whose statement is run exactly as written and may right-join freely.

    An attached graph is skipped entirely. Neither check means anything for one: it has no
    single datasource and no assembled query — its own nodes each hold those, and each
    node is validated when the graph is saved. Without this skip a graph would be reported
    as "not a relational datasource" on every agent console, which is both wrong and
    alarming.
    """
    unsupported = []

    for entry in tools:
        if entry.get("kind") == "graph":
            continue

        name = str(entry.get("tool_name"))

        if (entry.get("db_type") or "").strip().lower() not in RDBMS_DB_TYPES:
            unsupported.append(f"{name} (not a relational datasource)")
            continue

        if (entry.get("sql_query") or "").strip():
            continue

        joins = (entry.get("config") or {}).get("joins") or []
        if any((join.get("type") or "").lower() == "right" for join in joins):
            unsupported.append(f"{name} (uses a RIGHT JOIN)")

    return unsupported
