"""
What each kind of node actually does.

One ``async`` function per node type behind the :data:`_RUNNERS` registry, dispatched by
:func:`run_node`. That is the shape ``engine_service``'s ``_step_*`` table has, and it is
what makes adding a node type a matter of writing a function and naming it in one dict
rather than extending a chain of ``if``s in three places.

**Logging is written once.** :func:`run_node` opens the step row, times the body, and
closes the row with the outcome and the capped previews. A new node type therefore
cannot forget to log, and cannot log differently — which is the failure a per-runner
``begin/finish`` pair invites. It is the same reasoning that put the retry loop inside
``download_graph``'s ``write_batch`` node rather than around it.

**Nothing here imports LangGraph.** A runner takes a node dict and the state and returns
the state update; the compiler is what wires them into a graph. That keeps every runner
testable by calling it, which is how the interesting cases — a SQL node against a real
SQLite file, a loop cursor advancing, a tool config chain short-circuiting — are actually
asserted.

**What a failure is.** A runner raises :class:`NodeFailure` for anything the operator
caused or can fix, carrying a sentence written for them. The compiler catches it, records
the failed step, and routes to the node's ``error`` port if the author drew one or ends
the run if they did not. A runner never returns a sentinel for failure: a node that
returned ``None`` on failure and ``None`` on "no rows" would make those two
indistinguishable, and they are the two states most worth telling apart.
"""

import asyncio
import logging
import time
import uuid as uuid_pkg
from typing import Any, Callable, Dict, List, Mapping, Optional, Set, Tuple

from litestar.exceptions import HTTPException

from app.models.graph_designer import (
    BINDING_MODE_IN_LIST,
    BINDING_MODE_ONE,
    BINDING_MODE_VALUES,
    NODE_BRANCH,
    NODE_DO_UNTIL,
    NODE_EMAIL,
    NODE_FAILURE,
    NODE_FOR_EACH,
    NODE_HUMAN,
    NODE_SQL,
    NODE_SQL_UNION,
    NODE_START,
    NODE_SUCCESS,
    NODE_TIMER,
    NODE_TOOL_CONFIG,
    NODE_TYPE_VALUES,
    NODE_VALUE,
    NODE_WAIT,
    STEP_FAILED,
    STEP_SUCCEEDED,
    TIMER_PAUSE,
    TIMER_RESUME,
    TIMER_START,
    TIMER_STOP,
)
from app.services.graph_designer import (
    graph_state,
    node_variables,
    run_store,
    timers,
)
from app.services.graph_designer.graph_service import (
    DEFAULT_MAX_ITERATIONS,
    VALUELESS_OPERATORS,
    bindings_of,
    node_label,
)

logger = logging.getLogger(__name__)


class NodeFailure(Exception):
    """
    A node could not do its work, for a reason the operator can read.

    Carries the sentence and nothing else. The node it happened on is added by
    :func:`run_node`, which knows the node — a runner should not have to remember to
    name itself in every message it raises.
    """


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------

async def run_node(
    node: dict,
    state: Mapping[str, Any],
    context: "RunContext",
) -> dict:
    """
    Run one node, log it, and return its state update.

    The single entry point the compiler calls for every node type, so the timing, the
    step row and the preview caps are applied identically to all of them.

    A :class:`NodeFailure` is re-raised after the step row is closed, because the
    compiler needs it to decide where the run goes next — but the *log* has to be
    written first, or a run that ends on a failure would be missing the row explaining
    why.
    """
    node_id = str(node.get("id") or "")
    node_type = str(node.get("type") or "")
    label = node_label(node)
    iteration = _current_iteration(state, context)

    runner = _RUNNERS.get(node_type)

    if runner is None:
        # Unreachable through the designer — `validate_graph` refuses an unknown type
        # before it can be saved, and the run validates again before compiling. Handled
        # anyway rather than raising KeyError, because a row hand-edited in psql is
        # somebody's Tuesday and should produce a readable failed step.
        raise NodeFailure(
            f"'{label}' has a node type this application cannot run.",
        )

    step_id = await run_store.begin_step(
        context.run_id, node_id, node_type, label, iteration,
    )
    started = time.monotonic()

    try:
        # Inside the try, and after the step row is open, so an unmet dependency is a
        # *logged* failed step rather than only a sentence on the run. A run that says
        # why it failed but whose log does not show which node failed is a log with a
        # hole exactly where somebody would look.
        _require_available_sources(node, context.available)
        # After the dependency check, so "you left this node out of the selection" beats
        # "that node produced nothing" — the first is the truth and the second is only
        # its symptom. `render_node` returns a *copy*; the drawing must not be edited,
        # because the compiler captures each node in a closure once per run and a loop
        # body re-enters the same closure on every pass.
        prepared = node_variables.render_node(node, state)
        update = await runner(prepared, state, context)
    except NodeFailure as exc:
        await run_store.finish_step(
            step_id,
            STEP_FAILED,
            duration_ms=_elapsed_ms(started),
            message=str(exc),
            state_preview=graph_state.state_preview(state),
        )
        raise
    except asyncio.CancelledError:
        # Somebody pressed Stop while this node was still going.
        #
        # `CancelledError` derives from `BaseException`, so without this it sails past
        # the `except Exception` below, past `_guarded`, out of `ainvoke`, and the step
        # row it opened is never closed — leaving a node spinning forever in the dock
        # underneath a run the list already shows as cancelled. That was true of every
        # node before the `wait` node existed; a node that sleeps just makes it easy to
        # hit rather than a matter of timing.
        #
        # Written as a failed step rather than a new `cancelled` status: the row is
        # honest either way, and a sixth step status would need every consumer of the
        # log to learn it for no extra information. The row is closed and the
        # cancellation is re-raised untouched — swallowing it would tell asyncio the
        # task declined to stop.
        await run_store.finish_step(
            step_id,
            STEP_FAILED,
            duration_ms=_elapsed_ms(started),
            message="The run was stopped while this step was running.",
            state_preview=graph_state.state_preview(state),
        )
        raise
    except HTTPException as exc:
        # A validator this node's data no longer passes — a stored graph edited by hand,
        # or a datasource whose type changed under it. The validators speak to an
        # operator filling in a form and this operator is one, so the detail is shown
        # rather than swallowed.
        await run_store.finish_step(
            step_id,
            STEP_FAILED,
            duration_ms=_elapsed_ms(started),
            message=str(exc.detail),
            state_preview=graph_state.state_preview(state),
        )
        raise NodeFailure(str(exc.detail)) from exc
    except Exception as exc:  # noqa: BLE001 — one unexpected fault, one failed step
        # Deliberately broad, and deliberately *not* passed through to the user. A
        # driver error can name schema objects and echo values; the operator gets a
        # fixed sentence and the real reason goes to the log, the same split
        # `query_executor` makes between `execute_tool_query` and `probe_tool_query`.
        logger.exception("Node %s (%s) failed unexpectedly", node_id, node_type)
        await run_store.finish_step(
            step_id,
            STEP_FAILED,
            duration_ms=_elapsed_ms(started),
            message=f"'{label}' could not be run. The reason has been logged.",
            state_preview=graph_state.state_preview(state),
        )
        raise NodeFailure(
            f"'{label}' could not be run. The reason has been logged.",
        ) from exc

    merged = graph_state._merge(state.get("outputs"), update.get("outputs"))

    await run_store.finish_step(
        step_id,
        STEP_SUCCEEDED,
        duration_ms=_elapsed_ms(started),
        message=update.pop("_message", None),
        output_preview=graph_state.preview_of(merged.get(node_id)),
        state_preview=graph_state.state_preview({**state, "outputs": merged}),
    )

    return update


def _elapsed_ms(started: float) -> int:
    """How long a node took, in whole milliseconds."""
    return int((time.monotonic() - started) * 1000)


def _current_iteration(state: Mapping[str, Any], context: "RunContext") -> int:
    """
    Which pass of the enclosing loop this node is on.

    Read from the innermost enclosing loop's cursor. Zero for a node in no loop, which
    is most of them. Without this, a loop body's rows are indistinguishable from one
    another and the dock cannot group them.
    """
    if not context.enclosing_loop:
        return 0

    loop = (state.get("loops") or {}).get(context.enclosing_loop) or {}
    return int(loop.get("index") or 0)


def _require_available_sources(node: dict, available: Optional[Set[str]]) -> None:
    """
    Refuse a node whose inputs were left out of a tested selection.

    This is what stops a selection run from being quietly meaningless. A ``for_each``
    whose source is not in the selection would read ``None``, loop zero times and report
    success — a green tick on a test that ran nothing.

    ``available`` of ``None`` means the whole graph is running, so there is nothing to
    check.
    """
    if available is None:
        return

    missing = sorted(
        source for source in referenced_nodes(node) if source not in available
    )

    if not missing:
        return

    label = node_label(node)
    names = ", ".join(f"'{source}'" for source in missing)
    verb = "is" if len(missing) == 1 else "are"
    pronoun = "it" if len(missing) == 1 else "them"

    raise NodeFailure(
        f"'{label}' reads {names}, which {verb} not part of this test. Include "
        f"{pronoun} in the selection, or run the whole graph."
    )


def referenced_nodes(node: dict) -> Set[str]:
    """
    Every other node this node's settings read a value from.

    Collected in one place because six node types do it in six different fields, and a
    seventh would otherwise be able to introduce a reference that nothing checks. Public
    because the compiler needs the same answer when it decides what a selection covers.
    """
    from app.models.graph_designer import LOOP_NODE_TYPES

    data = node.get("data") or {}
    node_type = str(node.get("type") or "")
    referenced: Set[str] = set()

    if node_type in LOOP_NODE_TYPES:
        source = str(data.get("source_node") or "").strip()
        if source:
            referenced.add(source)

        condition = data.get("condition")
        if isinstance(condition, dict):
            source = str(condition.get("source_node") or "").strip()
            if source:
                referenced.add(source)

        # The node whose rows the loop unions. It sits in the loop's own body, so a
        # selection covering the loop covers it too — but a selection of *just* the loop
        # would not, and reading `None` there would union nothing and report success.
        collect_from = str(data.get("collect_from") or "").strip()
        if collect_from:
            referenced.add(collect_from)

    if node_type == NODE_BRANCH:
        for condition in data.get("conditions") or []:
            if isinstance(condition, dict):
                source = str(condition.get("source_node") or "").strip()
                if source:
                    referenced.add(source)

    # Read through `bindings_of`, not off the raw dict: a binding is a string on older
    # graphs and an object on new ones, and a reader that knew only one shape would
    # report no dependency at all for the other.
    for binding in bindings_of(data).values():
        referenced.add(str(binding["node"]))

    # The timer a pause, resume or stop acts on. A `start` names nothing — it *is* the
    # timer — so this is empty for it, which is what makes a lone `start` testable on
    # its own.
    if node_type == NODE_TIMER:
        timer_node = str(data.get("timer_node") or "").strip()
        if timer_node:
            referenced.add(timer_node)

    # Both variable maps: a node's own `variables`, and an Email node's
    # `variable_bindings`. The second was missing here, which made an Email node's
    # upstream invisible to a selection run — it passed this check and then failed
    # inside the resolver claiming the node had been "deleted, or skipped by a branch",
    # which is the wrong thing to tell somebody who simply did not tick that box.
    referenced |= node_variables.source_nodes(data)

    # A node never counts as reading itself. `do_until` conditions routinely test their
    # own cursor (`index`), and flagging that as a missing dependency would make every
    # such loop untestable in isolation.
    referenced.discard(str(node.get("id") or ""))

    return referenced


class RunContext:
    """
    Everything a runner needs that is not the node or the state.

    ``user_id`` is what scopes every lookup a node does — a datasource, a tool config —
    so a graph cannot reach a row its owner does not own even if a uuid was pasted in
    by hand.

    ``enclosing_loop`` is the id of the innermost loop node this node sits inside, or
    ``""``. Set by the compiler, which is the only thing that knows the drawing's
    nesting; a runner cannot work it out from the state.

    ``nodes`` is the whole drawing, by id. A runner is handed its own node, but two
    things it must do need somebody else's: filling a parameter named after the enclosing
    loop's item needs that loop's ``item_name``, and binding a parameter wired *to* a loop
    needs to know it is a loop at all. Set once by the compiler and carried by every copy
    below — a copy that dropped it would make those look like "no loop here", which reads
    as a graph that simply does not use the feature.

    ``available`` is the set of node ids this run actually covers — every node for a
    full run, the chosen ones for a tested selection. ``None`` means "everything", so a
    caller that is not testing a selection does not have to enumerate the graph.
    """

    __slots__ = ("run_id", "user_id", "enclosing_loop", "nodes", "available")

    def __init__(
        self,
        run_id: int,
        user_id: int,
        enclosing_loop: str = "",
        available: Optional[Set[str]] = None,
        nodes: Optional[Dict[str, dict]] = None,
    ) -> None:
        self.run_id = run_id
        self.user_id = user_id
        self.enclosing_loop = enclosing_loop
        self.nodes = nodes or {}
        self.available = available

    def node(self, node_id: str) -> Optional[dict]:
        """One node of the drawing by id, or ``None`` if it is not there."""
        return self.nodes.get(node_id) if node_id else None

    def for_loop(self, loop_id: str) -> "RunContext":
        """A copy scoped to a loop, for the nodes inside its body."""
        return RunContext(
            self.run_id, self.user_id, loop_id, self.available, self.nodes,
        )

    def covering(
        self,
        available: Optional[Set[str]],
        nodes: Optional[Dict[str, dict]] = None,
    ) -> "RunContext":
        """A copy that knows which nodes this run covers, and what they are."""
        return RunContext(
            self.run_id,
            self.user_id,
            self.enclosing_loop,
            available,
            nodes if nodes is not None else self.nodes,
        )


# --------------------------------------------------------------------------
# The runners
# --------------------------------------------------------------------------

async def _run_start(
    node: dict,
    state: Mapping[str, Any],
    context: RunContext,
) -> dict:
    """
    The start node does nothing, and does it on purpose.

    It exists so a drawing can say where the run begins — a graph is not a list and has
    no reading order — and it gets a step row like everything else so the log opens with
    the run starting rather than with whatever happened to be first.
    """
    return {"outputs": {str(node.get("id")): {"started": True}}}


async def _run_value(
    node: dict,
    state: Mapping[str, Any],
    context: RunContext,
) -> dict:
    """
    A literal: the parsed JSON the author typed.

    Re-parsed here rather than trusting what validation saw, because a graph row can be
    edited outside the designer and this function's guarantee has to hold for whatever
    is in the column — the same reason ``query_executor`` re-validates a stored query on
    every run instead of trusting that it passed once.
    """
    from app.services.graph_designer.graph_service import _parsed_value

    data = node.get("data") or {}
    value = _parsed_value(data, node_label(node))

    return {"outputs": {str(node.get("id")): value}}


async def _run_sql(
    node: dict,
    state: Mapping[str, Any],
    context: RunContext,
) -> dict:
    """
    Run this node's statement against its datasource and hand the rows on.

    Everything about *how* it runs is ``query_executor``'s: the connection, the bound
    parameters, the read-only guard and the active-table check. Nothing here assembles
    SQL, and nothing here concatenates a value into a statement — the values a downstream
    node supplies arrive as bound parameters exactly as a nested tool config's do.

    **Every matching row comes back — there is no cap here.** That is no longer a graph's
    exemption from anything: ``query_executor`` caps nothing either, so a node and a tool
    read alike. ``max_rows=None`` is still passed explicitly, because a node's guarantee
    about the operator's data should not be a default somebody could change elsewhere.
    **A ``LIMIT`` in the statement is the only thing that bounds the result**, which puts
    the size of the answer where the author can see it.

    Two consequences worth being explicit about. The rows land in the run's ``state``, so
    they are serialised to the checkpointer at every superstep — an unfiltered select over a
    large table is a large checkpoint. And a graph run as an agent's tool is unaffected,
    because that path reports through ``result_preview``, which ``graph_state.preview_of``
    caps at twenty rows with the real total beside it.

    Where the parameters come from, and in this order of precedence: an upstream node
    the author wired to the parameter, then the run's ``inputs`` (what the test panel or
    a calling model supplied), then the item of the loop this node sits inside. See
    :func:`_param_bindings`, which decides all three. A parameter with no value is
    refused by ``query_executor`` rather than defaulted, because a statement run with a
    missing filter returns more rows than it should and nothing about the result says so.

    A parameter set to take a list travels by ``value_bindings`` and is bound as an
    expanding parameter — ``IN (?, ?, ?)`` — while a single value travels through its
    declaration in ``sql_params``. Both lists go to ``assemble_sql_statement``, which
    already binds them together; the statement is not rewritten either way.
    """
    from app.services.deep_agents.query_executor import (
        ToolQueryError,
        execute_tool_query,
    )
    from app.services.tool_configs.tool_config_service import (
        validated_tables,
        validated_tool_sql,
    )

    data = node.get("data") or {}
    label = node_label(node)

    datasource = await _resolve_datasource(data, context, label)
    primary_table, extra_tables = validated_tables(data.get("table_names"))
    sql_query = validated_tool_sql(data.get("sql_query"))

    bindings = _param_bindings(data, state, context, _declared_params(data))

    try:
        rows = await execute_tool_query(
            datasource,
            {},
            primary_table,
            row_limit=None,
            max_rows=None,
            sql_query=sql_query,
            table_names=[primary_table, *extra_tables],
            value_bindings=bindings.lists,
            agent_values=bindings.scalars,
            sql_params=bindings.declared,
        )
    except ToolQueryError as exc:
        # The message is already written for an operator — this is the audience
        # `probe_tool_query` exists for — so it is shown rather than replaced. The
        # `advice` field is deliberately not shown: it is instructions for a model
        # about what to tell a visitor, and there is no visitor here.
        raise NodeFailure(f"'{label}' could not run: {exc}") from exc

    return {
        "outputs": {str(node.get("id")): rows},
        "_message": f"{len(rows)} row(s).",
    }


async def _run_sql_union(
    node: dict,
    state: Mapping[str, Any],
    context: RunContext,
) -> dict:
    """
    Append this pass's copy of one statement, and on the last pass run the lot as one query.

    The other way to union a loop — ``for_each``'s ``collect_from`` — runs the body's
    statement on every pass and concatenates the rows in Python: N round trips, no ceiling
    on the total text. This node is the opposite trade: it composes **one** statement and
    goes to the database once. ``documentations/GRAPH_DESIGNER.md`` has the table saying
    which to reach for.

    **The values are still bound.** This is the only place in the application that writes
    SQL, so it is the only place that has to say how it keeps SQL mode's guarantee. Each
    pass's copy of the fragment has its placeholders *renamed* — pass 7's ``:id`` becomes
    ``:id__p7`` (``sql_guard.suffixed_placeholders``, which steps over string literals so a
    LIKE pattern holding a colon survives) — and pass 7's value is bound under that name.
    Eighty-two fragments therefore carry eighty-two bind parameters, and no value is ever
    rendered into text. Concatenating the values instead would have been three lines
    shorter and would have made every looped statement an injection site.

    **Where the fragments live between passes.** In this node's own ``outputs`` entry.
    ``outputs`` is merged per node id, so reading what it wrote last pass and writing the
    extension needs no new state channel and survives the checkpointer like everything else.
    On the executing pass that entry is **replaced by the rows**, exactly as
    ``_run_for_each`` replaces its item envelope with the collected union on its last visit,
    and for the same reason: it makes the node after ``execute`` an ordinary consumer, so
    ``rows_of``, ``preview_of``, a branch condition and a further loop all work unchanged.

    Which pass is the last is :func:`union_executes`' answer, and the router asks the same
    function — one decision, so the log and the route cannot disagree.
    """
    from app.services.deep_agents.query_executor import (
        ToolQueryError,
        execute_tool_query,
    )
    from app.services.tool_configs.tool_config_service import (
        validated_tables,
        validated_tool_sql,
    )
    from app.utils.sql_guard import MAX_BUILT_SQL_LENGTH, suffixed_placeholders

    data = node.get("data") or {}
    label = node_label(node)
    node_id = str(node.get("id") or "")

    loop_id = context.enclosing_loop
    loop_node = context.node(loop_id)

    if not loop_id or not loop_node:
        # Refused at save time, so reaching this means a graph stored before the rule
        # existed or edited outside the application. Either way it cannot work: with no
        # loop there is no last pass, so the statement would be built and never run.
        raise NodeFailure(
            f"'{label}' builds a union of one statement per pass, so it has to sit inside "
            "a For each loop's body. This one does not."
        )

    envelope = graph_state.output_of(state, loop_id)
    index, total = _pass_position(envelope, loop_node, label)

    datasource = await _resolve_datasource(data, context, label)
    primary_table, extra_tables = validated_tables(data.get("table_names"))

    built = _extended_union(
        state, node_id, data, context, label, loop_node, suffix=f"__p{index + 1}",
    )

    if len(built.sql) > MAX_BUILT_SQL_LENGTH:
        raise NodeFailure(
            f"'{label}' has built a statement of {len(built.sql)} characters over "
            f"{built.passes} pass(es), which is more than the {MAX_BUILT_SQL_LENGTH} one "
            f"query may be. Set '{node_label(loop_node)}' to collect this node's rows "
            "instead — that unions the results rather than the text, and has no length to "
            "run out of."
        )

    if not union_executes(node, state, loop_id):
        return {
            "outputs": {node_id: built.as_output()},
            "_message": f"Added pass {index + 1} of {total} to the union.",
        }

    try:
        rows = await execute_tool_query(
            datasource,
            {},
            primary_table,
            row_limit=None,
            max_rows=None,
            sql_query=built.sql,
            table_names=[primary_table, *extra_tables],
            value_bindings=built.lists,
            agent_values=built.values,
            sql_params=built.params,
            max_length=MAX_BUILT_SQL_LENGTH,
        )
    except ToolQueryError as exc:
        raise NodeFailure(f"'{label}' could not run: {exc}") from exc

    return {
        "outputs": {node_id: rows},
        "_message": (
            f"Ran one query built from {built.passes} pass(es) — {len(rows)} row(s)."
        ),
    }


async def _run_tool_config(
    node: dict,
    state: Mapping[str, Any],
    context: RunContext,
) -> dict:
    """
    Run an existing tool config, exactly as an agent calling it would.

    Including its chain: a nested tool is resolved through
    ``tool_chain_service.chain_for_tool`` and run through
    ``tool_chain_graph.run_chain``, so a tool behaves identically here and in a
    conversation. Nothing in ``tool_chain_graph`` is modified or bypassed.

    **A chain that short-circuits is a success, not a failure.** An inner tool matching
    nothing means nothing matched, which is an answer — ``describe_stop`` phrases it —
    and reporting it as a failed node would send an operator looking for a broken query
    that is working correctly.
    """
    from app.db.db_utils import CRUDQueryBuilder
    from app.models.datasource import DataSource
    from app.services.tool_configs import tool_chain_service
    from app.services.tool_configs.tool_chain_graph import (
        build_chain_graph,
        describe_stop,
        run_chain,
    )
    from app.services.tool_configs.tool_config_service import get_tool_config

    data = node.get("data") or {}
    label = node_label(node)

    tool_uuid = _required_uuid(data.get("tool_config_id"), label, "tool config")

    async with run_store.open_session() as db:
        try:
            tool_config = await get_tool_config(db, context.user_id, tool_uuid)
        except HTTPException as exc:
            raise NodeFailure(
                f"'{label}' points at a tool config that is no longer available: "
                f"{exc.detail}"
            ) from exc

        if not tool_config.is_enabled:
            raise NodeFailure(
                f"'{label}' points at the tool config '{tool_config.tool_name}', which "
                "is switched off. Enable it in Tool Configs, or point this node at "
                "another tool."
            )

        # The tool's *own* datasource, not the node's: a tool config reads what it was
        # configured to read, and a graph node pointing at it does not get to redirect
        # that. Loaded here rather than lazily because building the chain needs it and
        # the session closes at the end of this block.
        datasource = await CRUDQueryBuilder(DataSource).get_one(
            db, filters={"id": tool_config.datasource_id},
        )

        if datasource is None:
            raise NodeFailure(
                f"'{label}' points at the tool config '{tool_config.tool_name}', whose "
                "datasource no longer exists."
            )

        chain = await tool_chain_service.chain_for_tool(db, tool_config, datasource)

    from app.services.deep_agents.query_executor import ToolQueryError

    try:
        result = await run_chain(chain, build_chain_graph(chain), _run_inputs(state))
    except ToolQueryError as exc:
        raise NodeFailure(f"'{label}' could not run: {exc}") from exc

    stopped = describe_stop(result)

    return {
        "outputs": {str(node.get("id")): list(result.rows)},
        "_message": stopped or f"{len(result.rows)} row(s).",
    }


async def _run_human(
    node: dict,
    state: Mapping[str, Any],
    context: RunContext,
) -> dict:
    """
    Ask a person, and stop until they answer.

    The pause itself is not here — it is a LangGraph ``interrupt()`` in the compiler,
    because that is the only place that can suspend a run. This runner is what happens
    **after** the answer comes back: it validates it against what the node said it
    expects and records it.

    Split that way because ``interrupt()`` re-runs the node it is in when the graph
    resumes, so anything before the interrupt happens twice. Keeping the pause in the
    compiler and the handling here means the step row is written once, for the pass
    that actually got an answer.
    """
    node_id = str(node.get("id") or "")
    answer = (state.get("answers") or {}).get(node_id)

    return {
        "outputs": {node_id: answer},
        "_message": f"Answered: {_short(answer)}",
    }


async def _run_branch(
    node: dict,
    state: Mapping[str, Any],
    context: RunContext,
) -> dict:
    """
    Decide which way the run goes, and record the decision.

    The decision itself is taken by :func:`branch_port`, which the compiler also calls
    as the router — one function, so the port recorded in the log and the port the run
    actually takes cannot differ.
    """
    port = branch_port(node, state)

    return {
        "outputs": {str(node.get("id")): {"outcome": port}},
        "_message": f"Took the '{port}' path.",
    }


async def _run_for_each(
    node: dict,
    state: Mapping[str, Any],
    context: RunContext,
) -> dict:
    """
    Load the list on the first pass, then advance the cursor on every pass after it.

    One runner for both, rather than a separate "setup" node, because the loop has to
    be re-entered by the back edge and a graph cannot have two nodes at one place in
    the drawing. ``started`` is what tells the two apart.

    The ceiling is checked here and **refuses rather than truncates**: a loop that
    quietly stopped early would produce a partial result that looks exactly like a
    complete one, which is the failure ``MAX_CHAIN_ITERATIONS`` is written about.

    If the author named a node to collect, each pass's rows are gathered here as the
    loop comes back round — see :func:`_collect_pass` — and the union replaces this
    node's own output once the cursor is spent.
    """
    data = node.get("data") or {}
    node_id = str(node.get("id") or "")
    label = node_label(node)

    loops = dict(state.get("loops") or {})
    loop = dict(loops.get(node_id) or {})

    ceiling = _iteration_ceiling(data)

    if not loop.get("started"):
        source = str(data.get("source_node") or "")
        items = graph_state.rows_of(graph_state.output_of(state, source))

        if len(items) > ceiling:
            raise NodeFailure(
                f"'{label}' would run {len(items)} times, which is more than the "
                f"{ceiling} passes it allows. Raise its limit, or narrow what it loops "
                "over."
            )

        loop = {"items": items, "index": 0, "started": True, "collected": []}
        message = (
            f"Looping over {len(items)} item(s)."
            if items else "Nothing to loop over."
        )
    else:
        # Collected **before** the cursor moves, while `index` still points at the pass
        # that has just finished — that is the pass whose rows are now in the state, and
        # the item that identifies them.
        loop["collected"] = _collect_pass(node, state, loop, label)

        loop["index"] = int(loop.get("index") or 0) + 1
        total = len(loop.get("items") or [])
        # The cursor is advanced first and *then* tested by `loop_continues`, so the
        # last visit to this node is the one that finds it exhausted. Saying "pass 3 of
        # 2" there would report a pass that is not going to happen — the log has to
        # describe what the run did, not what the cursor says.
        message = (
            f"Pass {loop['index'] + 1} of {total}."
            if loop["index"] < total
            else f"Finished all {total} pass(es)."
        )

    loops[node_id] = loop
    index = int(loop.get("index") or 0)
    items = loop.get("items") or []
    current = items[index] if index < len(items) else None

    item_name = str(data.get("item_name") or "").strip() or "item"
    collecting = bool(str(data.get("collect_from") or "").strip())
    finished = index >= len(items)

    if collecting and finished:
        # The union, and the loop's output *is* it once the loop is over. A node wired to
        # this one from the `done` port therefore reads every pass's rows, and reads them
        # as rows — so `rows_of`, a downstream loop, a branch condition, a parameter and
        # the dock's preview all work with no further arrangement.
        #
        # The body cannot observe this: the body does not run again after the cursor is
        # spent, so nothing inside the loop ever sees anything but the item envelope.
        collected = list(loop.get("collected") or [])
        return {
            "loops": {node_id: loop},
            "outputs": {node_id: collected},
            "_message": f"{message} {len(collected)} row(s) collected.",
        }

    return {
        "loops": {node_id: loop},
        "outputs": {node_id: {item_name: current, "index": index, "total": len(items)}},
        "_message": message,
    }


def _collect_pass(
    node: dict,
    state: Mapping[str, Any],
    loop: Mapping[str, Any],
    label: str,
) -> List[dict]:
    """
    The rows collected so far, plus the pass that has just finished.

    This is what makes a loop able to union its passes. The body runs *after* the loop
    node, so the rows of pass *k* are in the state by the time the loop node is
    re-entered for pass *k+1* — which is why the collecting happens here, on the way
    round, rather than needing a node of its own at the end of the body.

    ``label_item_as`` records which item produced each row, through
    ``query_executor.labelled_rows``: rows from twenty passes of one statement are
    indistinguishable once concatenated, and a statement that filters on a department
    without *selecting* it is perfectly ordinary SQL. It is optional because a query
    that already returns the value needs no second copy — and asking for one anyway is
    refused there as a column collision rather than silently overwriting a real value
    from the database.

    **There is no cap on the union**, for the same reason a SQL node has none: the only
    bound on how many rows there are is what the author wrote in SQL. This replaced a
    refusal that fired past 200 rows — a number two passes could reach, in a feature whose
    whole purpose is to put every pass together.

    What remains true, and is the property to preserve if this is ever changed, is that a
    union is only ever *whole* or *refused*: nothing here truncates one and reports
    success, because a union short of its last passes is short of whole **passes** — four
    departments missing, not four employees — and no row count says so.
    """
    from app.services.deep_agents.query_executor import labelled_rows

    data = node.get("data") or {}
    collect_from = str(data.get("collect_from") or "").strip()

    collected = list(loop.get("collected") or [])

    if not collect_from:
        return collected

    index = int(loop.get("index") or 0)
    items = loop.get("items") or []

    # A `for_each` collects while `index` still names the finished pass, so the item is
    # always there. A `do_until` walks no list at all, and the pass number is the only
    # thing that distinguishes one of its passes from another.
    label_value = _item_label(items[index]) if index < len(items) else index

    rows = graph_state.rows_of(graph_state.output_of(state, collect_from))
    alias = str(data.get("label_item_as") or "").strip()

    try:
        labelled = labelled_rows(rows, {alias: label_value} if alias else None)
    except Exception as exc:  # ToolQueryError — a column collision, named by the label
        raise NodeFailure(
            f"'{label}' could not record the item as '{alias}': {exc}"
        ) from exc

    collected.extend(labelled)

    return collected


def _item_label(item: Any) -> Any:
    """
    The value recorded against a collected row, for one item of the loop.

    The same one-value rule a binding follows: a single-column row is that column's
    value, so ``select id from departments`` labels with the id rather than with
    ``{"id": 7}``. Anything with no single value is written as it is and left for the
    author to see — a label is a description of the pass, so an unhelpful one is a
    cosmetic problem, unlike a *binding*, where guessing would filter the statement on
    the wrong thing.
    """
    resolved = graph_state.values_of(item)

    return resolved[0] if len(resolved) == 1 else item


async def _run_do_until(
    node: dict,
    state: Mapping[str, Any],
    context: RunContext,
) -> dict:
    """
    Count the passes, and refuse past the ceiling.

    Whether to go round again is :func:`loop_continues`' decision, which the compiler
    calls as the router — the same one-function rule the branch follows. This runner
    only advances and bounds the counter.

    A ``do_until`` is where an unbounded run would actually happen, so the ceiling here
    is the one that matters most. It refuses out loud and names the node, rather than
    letting LangGraph raise ``GraphRecursionError`` somewhere the author cannot connect
    to their drawing.

    **A ``do_until`` does not collect its passes, and ``for_each`` does.** Not an
    oversight: a loop can only publish a union on the visit it knows is its last, and for
    a ``for_each`` that is a fact about the cursor this runner holds, while for a
    ``do_until`` it is :func:`loop_continues`' decision — taken by the compiler as a
    router, *after* this returns. Working it out here as well would put the same decision
    in two places, and the pass where they disagreed is the pass whose rows go missing.
    Refused when the graph is saved, so the field is never offered and then ignored.
    """
    data = node.get("data") or {}
    node_id = str(node.get("id") or "")
    label = node_label(node)

    loops = dict(state.get("loops") or {})
    loop = dict(loops.get(node_id) or {})

    ceiling = _iteration_ceiling(data)

    index = 0 if not loop.get("started") else int(loop.get("index") or 0) + 1

    if index >= ceiling:
        raise NodeFailure(
            f"'{label}' has gone round {index} times without its condition being met, "
            f"which is the {ceiling} passes it allows. Check the condition, or raise "
            "the limit."
        )

    loop = {"started": True, "index": index, "items": []}

    return {
        "loops": {node_id: loop},
        "outputs": {node_id: {"index": index}},
        "_message": f"Pass {index + 1}.",
    }


async def _run_email(
    node: dict,
    state: Mapping[str, Any],
    context: RunContext,
) -> dict:
    """
    Queue one email, and put what was queued in the outputs.

    The implementation is in ``app/services/email_dispatch/nodes/graph_designer_runner.py``
    — a new module does not put its files inside another feature's folder, so what lives
    here is the dispatch and the failure translation and nothing else.

    **Queues; does not send.** Waiting on SMTP inside a node would make the run's
    wall-clock depend on somebody else's mail server and turn a greylisting relay into a
    failed graph. The queue already owns retrying. So the output says ``queued``, and
    whether it arrived is a question for the delivery log.

    Imported inside the function rather than at module scope, the same call this module
    already makes for ``graph_service``: the email module imports the integrations path
    reader, and a module-scope import here would put that whole chain behind every import
    of the graph runners — including in tests that never touch email.
    """
    from app.services.email_dispatch.nodes import graph_designer_runner

    try:
        queued = await graph_designer_runner.run_email_node(
            node, state, user_id=context.user_id, run_ref=str(context.run_id),
        )
    except Exception as exc:  # noqa: BLE001 — translated into this module's own failure
        raise NodeFailure(graph_designer_runner.wrap_failure(exc)) from exc

    return {"outputs": {str(node.get("id")): queued}}


async def _run_timer(
    node: dict,
    state: Mapping[str, Any],
    context: RunContext,
) -> dict:
    """
    Start, pause, resume or stop a stopwatch, and report where it has got to.

    Two things are written. The shared record goes into the ``timers`` channel under the
    **starting** node's id, so all four boxes act on one stopwatch. The reportable
    snapshot goes into ``outputs`` under **this** node's id, so a later node — usually an
    email — can bind to ``elapsed_human`` or ``started_at`` the way it binds to anything
    else.

    The clock is read **once**, at the top, and threaded through. Reading it again for
    the snapshot would let the record and the log disagree by the microseconds between
    two calls, which is the kind of difference nobody can explain a week later.
    """
    data = node.get("data") or {}
    node_id = str(node.get("id") or "")
    label = node_label(node)
    action = str(data.get("action") or "").strip().lower()

    key = node_id if action == TIMER_START else str(data.get("timer_node") or "").strip()
    record = (state.get("timers") or {}).get(key)
    moment = timers.now()

    try:
        updated = _timer_transition(action, record, label, node_id, moment, context)
    except timers.TimerError as exc:
        raise NodeFailure(str(exc)) from exc

    if updated is None:
        raise NodeFailure(
            f"'{label}' has no action chosen. A Timer node must Start, Pause, Resume "
            "or Stop a timer."
        )

    snapshot = timers.snapshot(updated, action)

    return {
        "timers": {key: updated},
        "outputs": {node_id: snapshot},
        "_message": _timer_message(action, snapshot),
    }


def _timer_transition(
    action: str,
    record: Optional[Mapping[str, Any]],
    label: str,
    node_id: str,
    moment: Any,
    context: RunContext,
) -> Optional[dict]:
    """
    Which transition this action is, and whether the timer's phase allows it.

    A second *start* is the one case that depends on where the box sits. Inside a loop
    body it means "this pass begins now" and the timer restarts, carrying what earlier
    passes measured; anywhere else it is the author starting a timer twice, which is a
    mistake with no sensible reading. The difference is not guessed — ``enclosing_loop``
    is set by the compiler, which is the only thing that knows the drawing's nesting.
    """
    if action == TIMER_START:
        if record is None:
            return timers.started(label, node_id, moment)

        if context.enclosing_loop:
            return timers.restarted(record, moment)

        raise timers.TimerError(
            f"The timer '{label}' has already been started. A timer is started once — "
            "use a Timer set to Stop to end it, or Resume if you paused it."
        )

    if record is None:
        raise timers.TimerError(
            f"'{label}' acts on a timer that has not been started on this path. It may "
            "be on a branch this run did not take."
        )

    if action == TIMER_PAUSE:
        return timers.paused(record, moment)

    if action == TIMER_RESUME:
        return timers.resumed(record, moment)

    if action == TIMER_STOP:
        return timers.stopped(record, moment)

    return None


def _timer_message(action: str, snapshot: Mapping[str, Any]) -> str:
    """What the log says this box did. The elapsed time where there is one worth reading."""
    name = snapshot.get("timer") or "timer"

    if action == TIMER_START:
        restarts = int(snapshot.get("restarts") or 0)
        return f"Started '{name}' (pass {restarts + 1})." if restarts else f"Started '{name}'."

    if action == TIMER_PAUSE:
        return f"Paused '{name}' at {snapshot.get('elapsed_human')}."

    if action == TIMER_RESUME:
        return f"Resumed '{name}'."

    return f"Stopped '{name}' at {snapshot.get('elapsed_human')}."


async def _run_wait(
    node: dict,
    state: Mapping[str, Any],
    context: RunContext,
) -> dict:
    """
    Pause the run for a fixed number of seconds.

    **The wait does not survive a restart.** ``stop_all_runs`` cancels every live run on
    shutdown, so a deploy landing inside one leaves the run cancelled and nothing resumes
    it. That is the price of not building a second scheduler for a node whose common case
    is thirty seconds, and it is why the ceiling is fifteen minutes rather than hours —
    anything longer belongs in an Integrations schedule, which is persisted.

    The duration is re-validated rather than trusted from the save, the same call
    ``_run_value`` makes about its JSON: ``graph_data`` is JSONB and can be edited by
    hand, and there is no ``asyncio.wait_for`` around a runner in this package to catch
    it if this number is wrong.

    Cancellation is not caught here. ``run_node`` closes the step row and re-raises, so
    a stopped run leaves a log that says how far it got.
    """
    data = node.get("data") or {}
    label = node_label(node)

    try:
        seconds = timers.validated_wait_seconds(data.get("seconds"), label)
    except timers.TimerError as exc:
        raise NodeFailure(str(exc)) from exc

    started_at = timers.now()
    monotonic = time.monotonic()

    await timers.sleep(seconds)

    waited = round(time.monotonic() - monotonic, 3)

    return {
        "outputs": {
            str(node.get("id")): {
                "waited_seconds": waited,
                "started_at": started_at.isoformat(),
                "ended_at": timers.now().isoformat(),
            },
        },
        "_message": f"Waited {seconds}s.",
    }


async def _run_success(
    node: dict,
    state: Mapping[str, Any],
    context: RunContext,
) -> dict:
    """End the run, having worked. The author's message if they wrote one."""
    data = node.get("data") or {}
    message = str(data.get("message") or "").strip()

    return {
        "outputs": {str(node.get("id")): {"succeeded": True}},
        "_message": message or "Finished.",
    }


async def _run_failure(
    node: dict,
    state: Mapping[str, Any],
    context: RunContext,
) -> dict:
    """
    End the run as failed, on purpose.

    A failure node is not an error — it is the author saying "if we get here, that is a
    bad outcome". So it writes a *succeeded* step (the node did its job) and sets
    ``failed_at``, which is what makes the **run** fail. Recording the step as failed
    would say the node malfunctioned, which it did not.
    """
    data = node.get("data") or {}
    label = node_label(node)
    message = str(data.get("message") or "").strip()

    return {
        "outputs": {str(node.get("id")): {"failed": True}},
        "failed_at": str(node.get("id") or ""),
        "failure_message": message or f"The run reached '{label}'.",
        "_message": message or "Reached the failure path.",
    }


_RUNNERS: Dict[str, Callable] = {
    NODE_START: _run_start,
    NODE_SQL: _run_sql,
    NODE_SQL_UNION: _run_sql_union,
    NODE_VALUE: _run_value,
    NODE_TOOL_CONFIG: _run_tool_config,
    NODE_HUMAN: _run_human,
    NODE_BRANCH: _run_branch,
    NODE_FOR_EACH: _run_for_each,
    NODE_DO_UNTIL: _run_do_until,
    NODE_EMAIL: _run_email,
    NODE_TIMER: _run_timer,
    NODE_WAIT: _run_wait,
    NODE_SUCCESS: _run_success,
    NODE_FAILURE: _run_failure,
}

# A node type in the vocabulary with no runner would save, validate, compile, and then
# fail at the one moment it matters. Asserted at import so the mistake stops the
# application rather than one graph — the same call `variable_sources` makes about its
# resolvers and `node_variables` about its field table.
assert set(_RUNNERS) == set(NODE_TYPE_VALUES), (
    "the node runners and the node vocabulary disagree: "
    f"{set(_RUNNERS) ^ set(NODE_TYPE_VALUES)}"
)


# --------------------------------------------------------------------------
# Conditions — compared, never evaluated
# --------------------------------------------------------------------------

def branch_port(node: dict, state: Mapping[str, Any]) -> str:
    """
    Which of a branch's outcomes this state takes.

    The conditions are tried **in the order the author listed them** and the first
    match wins, which is what makes the list an ordered set of rules rather than an
    unordered set whose overlap is undefined. Nothing matching takes ``else``.
    """
    from app.services.graph_designer.graph_service import PORT_ELSE

    data = node.get("data") or {}

    for condition in data.get("conditions") or []:
        if not isinstance(condition, dict):
            continue

        if _condition_holds(condition, state):
            return str(condition.get("port") or PORT_ELSE)

    return PORT_ELSE


def loop_continues(node: dict, state: Mapping[str, Any]) -> bool:
    """
    Whether a loop goes round again.

    ``for_each`` continues while its cursor is inside its list. ``do_until`` continues
    **until** its condition holds — so it is the negation, and the name says so.
    """
    node_id = str(node.get("id") or "")
    data = node.get("data") or {}
    loop = (state.get("loops") or {}).get(node_id) or {}

    if str(node.get("type")) == NODE_FOR_EACH:
        return int(loop.get("index") or 0) < len(loop.get("items") or [])

    condition = data.get("condition")

    if not isinstance(condition, dict):
        # Unreachable through validation, which refuses a `do_until` with no condition.
        # Treated as "stop" rather than "continue" because a loop with no way out is
        # the one outcome that must not be the default.
        return False

    return not _condition_holds(condition, state)


def _condition_holds(condition: Mapping[str, Any], state: Mapping[str, Any]) -> bool:
    """
    One comparison.

    **Compared, never evaluated.** The operator is a name from a fixed table and the
    comparison happens in Python; there is no expression language on this path and
    nothing reaches ``eval``. That is the same decision
    ``engine_service._evaluate_condition`` made, and it is why a graph cannot be used to
    run arbitrary code even by its own author.
    """
    operator = str(condition.get("operator") or "")
    source = str(condition.get("source_node") or "")
    actual = graph_state.output_of(state, source)

    field = str(condition.get("field") or "").strip()
    if field:
        actual = _field_of(actual, field)

    if operator in VALUELESS_OPERATORS:
        empty = _is_empty(actual)
        return empty if operator == "is_empty" else not empty

    expected = condition.get("value")

    if operator == "equals":
        return _as_text(actual) == _as_text(expected)

    if operator == "not_equals":
        return _as_text(actual) != _as_text(expected)

    if operator == "contains":
        return _as_text(expected) in _as_text(actual)

    if operator == "not_contains":
        return _as_text(expected) not in _as_text(actual)

    if operator in ("greater_than", "less_than"):
        left, right = _as_numbers(actual, expected)

        if left is None or right is None:
            # Not comparable as numbers. False rather than a raise: a condition is a
            # question, and "is this text greater than 5" is answered by "no" rather
            # than by failing the run.
            return False

        return left > right if operator == "greater_than" else left < right

    return False


def _field_of(value: Any, field: str) -> Any:
    """
    One named field out of a node's output.

    A dict's key, or the same key on the first of a list of rows — so a condition can
    read ``total`` from a one-row aggregate without the author adding a node to unwrap
    it. The first row rather than all of them because a condition is one comparison;
    testing every row is what a branch inside a ``for_each`` is for.
    """
    if isinstance(value, dict):
        return value.get(field)

    if isinstance(value, list) and value and isinstance(value[0], dict):
        return value[0].get(field)

    return None


def _is_empty(value: Any) -> bool:
    """
    Whether a node produced nothing.

    ``0`` and ``False`` are **not** empty. That distinction is the whole reason this is
    a function rather than ``not value``: a SQL node returning a count of zero has
    produced a real answer, and treating it as empty would send a graph down its
    "nothing found" path when the thing it found was zero.
    """
    if value is None:
        return True

    if isinstance(value, (str, list, dict, tuple)):
        return len(value) == 0

    return False


def _as_text(value: Any) -> str:
    """A value as text, for the comparisons that are textual."""
    if value is None:
        return ""

    if isinstance(value, bool):
        return "true" if value else "false"

    return str(value)


def _as_numbers(left: Any, right: Any) -> tuple:
    """Both sides as floats, or ``(None, None)`` if either will not convert."""
    try:
        return float(left), float(right)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None, None


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _iteration_ceiling(data: Mapping[str, Any]) -> int:
    """
    How many passes a loop allows.

    Falls back to :data:`DEFAULT_MAX_ITERATIONS` for a node saved before the field
    existed, rather than to "unbounded" — the fallback for a missing limit has to be a
    limit.
    """
    try:
        ceiling = int(data.get("max_iterations") or DEFAULT_MAX_ITERATIONS)
    except (TypeError, ValueError):
        ceiling = DEFAULT_MAX_ITERATIONS

    return max(1, ceiling)


async def _resolve_datasource(
    data: Mapping[str, Any],
    context: RunContext,
    label: str,
) -> Any:
    """
    This node's datasource row, scoped to the graph's owner.

    Scoped, which is the point: a ``datasource_id`` pasted into a saved graph by hand
    cannot reach a datasource belonging to somebody else. The ORM row is returned rather
    than a view because ``query_executor`` needs its encrypted password to connect.
    """
    from app.db.db_utils import CRUDQueryBuilder
    from app.models.datasource import DataSource

    datasource_uuid = _required_uuid(data.get("datasource_id"), label, "datasource")

    async with run_store.open_session() as db:
        datasource = await CRUDQueryBuilder(DataSource).get_by_uuid(
            db, datasource_uuid, extra_filters={"user_id": context.user_id},
        )

    if not datasource:
        raise NodeFailure(
            f"'{label}' points at a datasource that is no longer available.",
        )

    if not datasource.is_active:
        raise NodeFailure(
            f"'{label}' reads '{datasource.datasource_name}', which is switched off in "
            "Data Sources."
        )

    return datasource


def _required_uuid(raw: Any, label: str, what: str) -> uuid_pkg.UUID:
    """One of a node's uuid references, parsed."""
    text = str(raw or "").strip()

    if not text:
        raise NodeFailure(f"'{label}' has no {what} selected.")

    try:
        return uuid_pkg.UUID(text)
    except ValueError as exc:
        raise NodeFailure(
            f"The {what} '{label}' points at is not a valid selection.",
        ) from exc


def _declared_params(data: Mapping[str, Any]) -> List[dict]:
    """
    A SQL node's declared parameters, in the shape ``query_executor`` binds.

    The same ``{param, type, required, description}`` shape a SQL-mode tool config
    stores, so the binding, the coercion and the "declared but not filled" refusal are
    all the code that already exists.
    """
    raw = data.get("params")
    entries = raw if isinstance(raw, list) else []

    return [entry for entry in entries if isinstance(entry, dict) and entry.get("param")]


class BuiltUnion:
    """
    A union under construction: the text so far, and everything needed to bind it.

    Carried as an object rather than the dict it is stored as so the runner reads
    ``built.sql`` and ``built.passes`` instead of indexing a mapping in six places, and so
    :meth:`as_output` is the single definition of the stored shape — the accumulator is
    read back next pass by the same code that wrote it, and a key spelled two ways would
    lose a pass's values in silence.
    """

    __slots__ = ("fragments", "params", "values", "lists")

    def __init__(
        self,
        fragments: List[str],
        params: List[dict],
        values: Dict[str, Any],
        lists: List[dict],
    ) -> None:
        self.fragments = fragments
        self.params = params
        self.values = values
        self.lists = lists

    @property
    def sql(self) -> str:
        """The fragments as one statement.

        ``UNION`` and not ``UNION ALL``: the author asked for the operator they wrote, and
        it is the one that removes rows appearing in more than one pass.

        No parentheses around the members. They would let a fragment carry its own
        ``ORDER BY``, and they are invalid around a compound-select operand in SQLite — so
        the fragment is refused at save time for holding one instead, which fails on the
        form rather than on two of the three supported databases.
        """
        return " UNION ".join(self.fragments)

    @property
    def passes(self) -> int:
        return len(self.fragments)

    def as_output(self) -> dict:
        """The shape this node writes to ``outputs`` while it is still accumulating."""
        return {
            "sql": self.sql,
            "fragments": list(self.fragments),
            "params": list(self.params),
            "values": dict(self.values),
            "lists": list(self.lists),
            "passes": self.passes,
        }

    @classmethod
    def stored(cls, value: Any) -> "BuiltUnion":
        """
        What a previous pass left in ``outputs``, or an empty union.

        Anything that is not this node's own accumulator — the rows from a previous run of
        the same graph, ``None`` on the first pass — starts again from nothing rather than
        being coerced. A half-read accumulator would build a statement whose text and whose
        values came from different passes.
        """
        if not isinstance(value, Mapping) or "fragments" not in value:
            return cls([], [], {}, [])

        return cls(
            [str(fragment) for fragment in value.get("fragments") or []],
            [dict(entry) for entry in value.get("params") or [] if isinstance(entry, Mapping)],
            dict(value.get("values") or {}),
            [dict(entry) for entry in value.get("lists") or [] if isinstance(entry, Mapping)],
        )


def _pass_position(
    envelope: Any,
    loop_node: dict,
    label: str,
) -> Tuple[int, int]:
    """
    Which pass of how many, off the loop's ``{item, index, total}`` envelope.

    Read rather than counted from the fragments already gathered, because the loop's cursor
    is the only thing that knows how many items there are — and "is this the last pass" has
    to be answerable *before* the fragment is added, by both this runner and the router.
    """
    if not isinstance(envelope, Mapping) or "index" not in envelope:
        raise NodeFailure(
            f"'{label}' reads which pass it is on from '{node_label(loop_node)}', which has "
            "not produced one. Check that it is inside that loop's body."
        )

    try:
        return int(envelope.get("index") or 0), int(envelope.get("total") or 0)
    except (TypeError, ValueError) as exc:
        raise NodeFailure(
            f"'{label}' could not read the pass number from '{node_label(loop_node)}'."
        ) from exc


def _extended_union(
    state: Mapping[str, Any],
    node_id: str,
    data: Mapping[str, Any],
    context: "RunContext",
    label: str,
    loop_node: dict,
    suffix: str,
) -> BuiltUnion:
    """
    The union so far, plus this pass's fragment, with this pass's values bound to it.

    The values come from :func:`_param_bindings` — unchanged, so a fragment's parameter is
    filled from a wiring, the run's inputs or the loop's item in exactly the order a plain
    SQL node's is. All this adds is the rename that keeps one pass's value off another
    pass's fragment.

    ``lists`` is carried as well as ``scalars``: a parameter set to take a whole list is
    already expressible on this form, and quietly dropping it here would leave a filter out
    of one member of the union while the query still ran.
    """
    from app.services.tool_configs.tool_config_service import validated_tool_sql
    from app.utils.sql_guard import suffixed_placeholders

    previous = BuiltUnion.stored(graph_state.output_of(state, node_id))
    bindings = _param_bindings(data, state, context, _declared_params(data))

    fragment = suffixed_placeholders(validated_tool_sql(data.get("sql_query")), suffix)

    if fragment in previous.fragments:
        # Two passes producing the same text means the suffix did not change, which would
        # bind one pass's value over another's. Nothing should be able to cause it — the
        # suffix is the loop's index — so it is a guard rather than a message about
        # something the author did.
        raise NodeFailure(
            f"'{label}' built the same fragment twice on '{node_label(loop_node)}'."
        )

    return BuiltUnion(
        [*previous.fragments, fragment],
        [
            *previous.params,
            *[{**entry, "param": str(entry["param"]) + suffix} for entry in bindings.declared],
        ],
        {
            **previous.values,
            **{name + suffix: value for name, value in bindings.scalars.items()},
        },
        [
            *previous.lists,
            *[
                {**entry, "reference": str(entry.get("reference") or "") + suffix}
                for entry in bindings.lists
            ],
        ],
    )


def union_executes(node: dict, state: Mapping[str, Any], loop_id: str) -> bool:
    """
    Whether this visit to a union node is the one that runs the statement.

    True on the pass where the enclosing loop has handed over its **last** item, which is
    what makes an ``execute`` port possible at all: the node itself decides, so the loop's
    ``done`` port is left for the empty-list case and the drawing stays a loop.

    Called twice per visit and deliberately so — by :func:`_run_sql_union` to decide whether
    to run, and by the compiler's router to decide whether to leave by ``execute``. The same
    arrangement ``branch_port`` and ``loop_continues`` have, and for the same reason: a
    second copy of this comparison could disagree with the first, and the log would then
    describe a route the run did not take.

    A pure function of the loop's published envelope, so it answers the same after the
    runner has written its output as before — the runner does not touch the loop's entry.
    """
    if not loop_id:
        return False

    envelope = graph_state.output_of(state, loop_id)

    if not isinstance(envelope, Mapping) or "index" not in envelope:
        return False

    try:
        index = int(envelope.get("index") or 0)
        total = int(envelope.get("total") or 0)
    except (TypeError, ValueError):
        return False

    return total > 0 and index + 1 >= total


class ParamBindings:
    """
    How a SQL node's declared parameters reach its statement.

    Two lists rather than one dict, because the two binding shapes travel by different
    arguments and must not both carry the same name — a parameter bound twice is bound
    once by whichever SQLAlchemy saw last:

    * ``scalars`` → ``execute_tool_query(agent_values=…)``, read *through* ``declared``
      and coerced by the type the author stated.
    * ``lists`` → ``execute_tool_query(value_bindings=…)``, each an expanding parameter
      that renders as ``IN (?, ?, ?)``.
    * ``declared`` → the ``sql_params`` to pass on: the declarations for the scalars
      only, so a list-bound name is not also bound as one value.
    """

    __slots__ = ("scalars", "lists", "declared")

    def __init__(
        self,
        scalars: Dict[str, Any],
        lists: List[dict],
        declared: List[dict],
    ) -> None:
        self.scalars = scalars
        self.lists = lists
        self.declared = declared


def _param_bindings(
    data: Mapping[str, Any],
    state: Mapping[str, Any],
    context: "RunContext",
    sql_params: List[dict],
) -> ParamBindings:
    """
    What to bind each declared parameter to, and how.

    Three sources, and the first that has a value wins:

    1. **A wiring** — an upstream node the author connected to this parameter. The most
       specific statement available: they drew that line about this parameter.
    2. **The run's ``inputs``** — what the test panel or a calling model supplied.
    3. **The enclosing loop's item** — a parameter whose name is the loop's ``item_name``
       is filled with the item of the current pass, with nothing to wire. This is what
       makes ``for_each`` over ``select id from departments`` feed
       ``where id = :item`` directly, which is the whole point of a loop body.

    The loop comes last so that a wiring or an explicit input always overrides it. A
    graph that names a parameter after the item and *also* wires it is not ambiguous —
    the drawn connection is the author being specific.

    A parameter with no value from any of the three is **left out**, not defaulted to
    ``None`` or to an empty string. ``query_executor`` refuses a declared parameter it
    was not given, and that refusal is the correct outcome: a statement run with a
    filter missing returns more rows than it should, and nothing about the result says
    so.
    """
    wired = bindings_of(data)
    inputs = dict(state.get("inputs") or {})

    scalars: Dict[str, Any] = {}
    lists: List[dict] = []
    declared: List[dict] = []

    for entry in sql_params:
        name = str(entry.get("param") or "")
        binding = wired.get(name)

        if binding:
            _bind_wired(name, binding, state, context, scalars, lists, declared, entry)
            continue

        if name in inputs:
            scalars[name] = inputs[name]
            declared.append(entry)
            continue

        item = _loop_item_for(name, state, context)
        if item is not _NO_VALUE:
            scalars[name] = item

        declared.append(entry)

    return ParamBindings(scalars, lists, declared)


def _bind_wired(
    name: str,
    binding: Mapping[str, Any],
    state: Mapping[str, Any],
    context: "RunContext",
    scalars: Dict[str, Any],
    lists: List[dict],
    declared: List[dict],
    declaration: dict,
) -> None:
    """One wired parameter, placed in whichever list its mode belongs to."""
    source_id = str(binding.get("node") or "")
    value = _bound_value(source_id, state, context)

    field = str(binding.get("field") or "")
    if field:
        _require_field_present(name, field, value, source_id, context)
        value = _field_of(value, field)

    resolved = graph_state.values_of(value)

    if binding.get("mode") == BINDING_MODE_IN_LIST:
        if not resolved:
            # `IN ()` is a syntax error in most dialects and an always-false filter in
            # the rest. Neither is what the author drew, and both are worse than saying
            # so: an empty result that reads as "nothing matched" hides that the filter
            # itself was never built.
            raise NodeFailure(
                f"The parameter ':{name}' is set to take a list of values, but the "
                "node wired to it produced none."
            )

        lists.append({"reference": name, "values": resolved, "expanding": True})
        return

    if len(resolved) == 1:
        scalars[name] = resolved[0]
        declared.append(declaration)
        return

    if resolved:
        # A single-value parameter given several is a wiring mistake, and binding the
        # first would run a statement filtered on an arbitrary one of them.
        raise NodeFailure(
            f"The parameter ':{name}' takes one value, but the node wired to it "
            f"produced {len(resolved)}. Name the column it should read, or set it to "
            "take a list of values."
        )

    declared.append(declaration)


def _require_field_present(
    name: str,
    field: str,
    value: Any,
    source_id: str,
    context: "RunContext",
) -> None:
    """
    Refuse a wiring that names a field its source has not got.

    ``_field_of`` answers ``None`` for "no such key" and for "the key is there and null",
    which is right for a *condition* — both compare as empty — and wrong here, because a
    parameter that resolves to nothing is dropped, and what the author then reads is
    ``query_executor``'s "this tool needs a value for 'id' and none was given". That
    sentence describes an input nobody supplied, when in fact a line *was* drawn and it
    read the wrong key. Worse, an **optional** parameter in that state takes the filter
    out of the statement altogether and the run succeeds over every row.

    So the field is checked against what the source actually produced, and the refusal
    lists the fields there are — which is the whole diagnosis in one sentence. The
    commonest way to get here is a field left behind from an earlier wiring: renaming a
    loop's item, or re-pointing the parameter, does not empty the box.
    """
    source = context.node(source_id)
    label = node_label(source) if source else source_id

    if _is_empty(value):
        raise NodeFailure(
            f"':{name}' reads the field '{field}' from '{label}', which produced nothing "
            "on this pass."
        )

    available = _fields_of(value)

    if available is None:
        raise NodeFailure(
            f"':{name}' reads the field '{field}' from '{label}', which produced a "
            "single value rather than rows. Clear the field to use that value as it is."
        )

    if field not in available:
        listed = ", ".join(f"'{key}'" for key in available) or "nothing"
        raise NodeFailure(
            f"':{name}' reads the field '{field}' from '{label}', which has no field of "
            f"that name. It has: {listed}. Clear the field to use the value itself."
        )


def _fields_of(value: Any) -> Optional[List[str]]:
    """
    The field names a wiring could read out of a node's output.

    ``None`` — rather than an empty list — when the output is not made of fields at all,
    so the caller can tell "this has no field called that" from "this has no fields".
    Reads the first row of a list for the same reason :func:`_field_of` does.
    """
    if isinstance(value, Mapping):
        return [str(key) for key in value]

    if isinstance(value, list) and value and isinstance(value[0], Mapping):
        return [str(key) for key in value[0]]

    return None


def _bound_value(
    source_id: str,
    state: Mapping[str, Any],
    context: "RunContext",
) -> Any:
    """
    What a parameter wired to ``source_id`` reads.

    An ordinary node's output, as it is — except for a **loop**, where wiring a parameter
    to it means the item it is on, not the ``{item, index, total}`` envelope the loop
    publishes. That envelope is right for a branch condition, which may well want to test
    ``index``; it is never what somebody wiring a value into a statement meant, and
    binding it whole fails with "produced 3 values" for a reason nobody would guess from
    the drawing.

    So a loop is unwrapped here, and a ``field`` on the binding therefore names a column
    of the *item* — which is how one column of a multi-column row is reached.

    While the loop is finished and collecting, its output is already the union rather than
    an envelope; that is a list, has no ``item_name`` key, and falls through unchanged, so
    a node after ``done`` wired to the loop reads the collected rows.
    """
    value = graph_state.output_of(state, source_id)

    source = context.node(source_id)
    if not source or str(source.get("type") or "") not in _LOOP_TYPES:
        return value

    item_name = str((source.get("data") or {}).get("item_name") or "").strip() or "item"

    if isinstance(value, Mapping) and item_name in value:
        return value[item_name]

    return value


# Imported lazily elsewhere in this module to keep the model import at the bottom of the
# dependency order; named here because `_bound_value` runs on every wired parameter.
_LOOP_TYPES = frozenset({NODE_FOR_EACH, NODE_DO_UNTIL})


# Distinguishes "the loop had no value for this name" from "the value is None", which a
# plain `None` return could not: an item genuinely may be NULL.
_NO_VALUE = object()


def _loop_item_for(
    name: str,
    state: Mapping[str, Any],
    context: "RunContext",
) -> Any:
    """
    The enclosing loop's current item, if this parameter is named after it.

    Resolved by the same one-value rule a wiring follows, so the three sources cannot
    disagree about what an item *is*:

    * a scalar item is itself;
    * a single-column row — ``{"id": 7}``, which is what ``select id from departments``
      yields — is that column's value, so the common case needs no wiring at all;
    * a row with several columns has no single value, and is refused rather than guessed
      at. Binding an arbitrary column would filter the statement on the wrong thing and
      the result would look entirely normal.
    """
    loop_id = context.enclosing_loop
    loop_node = context.node(loop_id)

    if not loop_id or not loop_node:
        return _NO_VALUE

    item_name = str((loop_node.get("data") or {}).get("item_name") or "").strip()

    if not item_name or item_name != name:
        return _NO_VALUE

    envelope = graph_state.output_of(state, loop_id)
    if not isinstance(envelope, Mapping) or item_name not in envelope:
        return _NO_VALUE

    resolved = graph_state.values_of(envelope.get(item_name))

    if len(resolved) == 1:
        return resolved[0]

    if not resolved:
        return None

    raise NodeFailure(
        f"'{node_label(loop_node)}' walks rows with {len(resolved)} columns, so "
        f"':{name}' cannot be filled from the item on its own. Wire the parameter to "
        "the loop and name the column it should read."
    )


def _run_inputs(state: Mapping[str, Any]) -> Dict[str, Any]:
    """The run's inputs, for a tool config node whose tool declares parameters."""
    return dict(state.get("inputs") or {})


def _short(value: Any) -> str:
    """A one-line rendering of a value, for a log message."""
    text = _as_text(value)
    return text if len(text) <= 80 else text[:80] + "…"
