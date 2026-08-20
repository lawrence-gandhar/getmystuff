"""
Running a nested tool config as a LangGraph.

:mod:`app.services.tool_configs.tool_chain_service` decides what a chain *is*; this
runs it. One tool becomes one **node**, the edges run deepest-first, and a
**conditional edge** after every inner node is where "if the deepest tool returns
nothing, stop and return nothing" actually lives.

    START → paid_invoices → active_clients → projects_by_client → END
                  │                │
                  └── no values ───┴──────────────────────────→ END

Why a graph rather than a loop. The behaviour asked for — evaluate inside-out,
propagate outward, stop the moment a level produces nothing — is a control-flow
graph whichever way it is written, and writing it as one makes the control flow the
thing you read instead of something you reconstruct from `if`s and `break`s. It also
puts the chain on the same footing as the agent that calls it: both are LangGraph
runs, both can be traced by the same tooling.

**What crosses an edge is values, never rows.** An inner node returns one column of
its result (``execute_value_query``); that list becomes an ``IN`` comparison on the
node above it, bound as parameters. The rows an inner tool read are discarded at the
edge — they are not returned to the agent, not logged, and not carried up the chain.
Only the root's rows are the tool's answer, exactly as a sub-query's inner rows are
not part of an outer query's result.

**An inner node may be a drawn graph instead of a tool config.** It produces values the
same way and is indistinguishable from there upward, which is the property that made it
cheap to add — but it brings one thing no tool config can do: a graph containing an *Ask
a human* node stops mid-run and waits. So a chain has a **third outcome** besides rows
and "nothing matched": ``ChainResult.asked``, carrying the question, the run it is parked
on and the node that asked. Nothing failed and nothing matched nothing; the chain is
waiting, and the caller relays the question and comes back with ``resolved``. See
``_graph_values`` and ``run_chain``.

Values from a graph are read with ``graph_runner.full_result``, **never** off
``GraphOutcome.rows``. That distinction is load-bearing: ``rows`` is a twenty-row preview
meant for describing a result to somebody, and these values become a filter — one built
from the first twenty of five hundred ids answers a different question than the one asked
while looking exactly like an answer.

**A link may iterate instead of matching a list.** With ``binding_mode`` ``each``
the root is run once per value the child returned and the rows are concatenated —
the shape needed whenever the value is not on the right-hand side of an ``IN``
(``dd.id = :x``, or a pattern the database assembles around it). The loop is
sequential and lives inside the root node rather than being a LangGraph ``Send``
fan-out, for the reason siblings are sequenced too: the first sibling to return
nothing ends the run, so sequencing means the queries whose answer cannot matter are
never run at all.

**Nothing here caps rows.** Not the values an inner node hands up, and not the root's
result. Both used to be capped, and both caps did the same damage from opposite ends:
an ``IN`` list cut to 2,000 built a filter that answered a different question than the
one asked, and a root result cut to 200 was a sample of somebody's data reported as
their answer. What the run still refuses is :data:`MAX_CHAIN_ITERATIONS` — how many
times the root may be **re-run**, which bounds round trips inside one chat turn rather
than how much data exists — and it refuses rather than truncating, because rows from
the first fifty departments are indistinguishable from rows for every department and a
total taken over them is a plausible number that is wrong.

**Each tool is still run in full, as itself.** The chain is not compiled into one
nested SQL statement. Every node goes through ``query_executor`` with its own
validation and its own active-table and active-column checks, so a tool behaves
identically whether it was called directly or embedded — which is what makes "the
child works on its own too" true rather than approximately true.

LangGraph is imported here and nowhere else in this feature, which is why the
validation and tree-building live next door: those rules are testable without the
container, and only this file needs it.
"""

import logging
from dataclasses import dataclass, field
from typing import Annotated, Any, Callable, Dict, List, Optional, TypedDict

from langgraph.graph import END, START, StateGraph

from app.services.deep_agents.query_executor import (
    MAX_CHAIN_ITERATIONS,
    ToolQueryError,
    execute_tool_query,
    execute_value_query,
    labelled_rows,
)
from app.services.tool_configs.tool_chain_service import ChainNode
from app.services.tool_configs.tool_config_service import tables_read

logger = logging.getLogger(__name__)


def _merge_values(
    current: Dict[str, List[Any]],
    incoming: Dict[str, List[Any]],
) -> Dict[str, List[Any]]:
    """
    Accumulate what each node produced instead of replacing it.

    Without a reducer a node returning ``{"values": {...}}`` would overwrite the
    whole mapping and a parent with two children would only ever see the second
    one's values — a query silently missing half its restriction.
    """
    return {**(current or {}), **(incoming or {})}


class ChainState(TypedDict, total=False):
    """
    What travels along the edges.

    ``values`` is keyed by node key, not by tool name: the same tool embedded twice
    for two different columns is two nodes with two answers, and a name would
    collide.
    """

    values: Annotated[Dict[str, List[Any]], _merge_values]
    rows: List[Dict[str, Any]]
    stopped_by: str
    # Set when an embedded **graph** stopped to ask a person a question. Carries the
    # question, the run id it is parked on, and the key of the node that asked, because
    # resuming needs all three: the text to relay, the thread to answer, and which
    # node's values the answer will finally produce.
    #
    # It ends the run the same way `stopped_by` does — via `_continue_or_stop` — for the
    # same reason: nothing above it can run without the values it did not produce.
    asked: Dict[str, Any]
    # Values a node does not have to produce because they are already known, keyed the
    # same way `values` is. One caller sets it: the tool that resumes a chain an embedded
    # graph paused. The graph's question has been answered and its run finished on that
    # turn, so re-running it would ask again — the same question, of somebody who has
    # already answered it.
    resolved: Dict[str, List[Any]]
    # What the model supplied for the **root's** declared parameters. It travels in
    # state rather than in the node closures because the closures are compiled once
    # per tool and reused by every call, while these change with every call. Only the
    # root reads it: an inner tool is never called by the model, so a child that
    # needs an argument is refused when it is embedded rather than left to fail here.
    agent_values: Dict[str, Any]


@dataclass
class ChainResult:
    """
    The outcome of one chain run — one of three things, and a caller has to tell them
    apart because they call for three different sentences.

    ``rows`` and nothing else is the ordinary answer.

    ``stopped_by`` names the child that returned nothing, and is what turns a bare
    "0 rows" into an answer the agent can give: *no clients matched, so there were
    no projects to total*. Empty when the chain ran to the end.

    ``asked`` is the third, and it exists because a child may now be a **graph** — the
    one kind of child that can stop mid-run to ask a person something. Nothing failed and
    nothing matched nothing; the chain is *waiting*. It carries the question to relay
    verbatim, the run id to answer it on, and which node asked. See
    :func:`app.services.graph_designer.graph_runner.run_graph`, where the same decision is
    documented: a pause is an outcome, not an error.
    """

    rows: List[Dict[str, Any]]
    stopped_by: str = ""
    asked: Optional[Dict[str, Any]] = None

    @property
    def short_circuited(self) -> bool:
        return bool(self.stopped_by)

    @property
    def waiting(self) -> bool:
        """Whether an embedded graph is holding this chain open for an answer."""
        return bool(self.asked)


def build_chain_graph(
    chain: ChainNode,
    row_limit: Optional[int] = None,
    include_root: bool = True,
):
    """
    Compile ``chain`` into a runnable graph, deepest node first.

    ``row_limit`` bounds only the **root** — what the tool returns — and ``None``, the
    default, means every row it matches. It survives as a parameter for the one caller
    that wants a specific number rather than a ceiling: **Test Query** compiles the same
    graph with a single row, because a test needs to know the chain runs, not to move a
    result set. Inner nodes are unbounded either way; they read values, not rows.

    ``include_root=False`` compiles the same graph with its last node left out, so
    the run ends with the inner nodes' values in state and the root's query never
    executed. That is what :func:`resolve_chain_bindings` needs — a caller that
    intends to read the root's whole result set itself must not have the root run
    first — and it is the same graph either way, which is the point of the flag
    existing rather than a second traversal.

    Built **once** per tool — ``tool_factory`` keeps the compiled graph in the
    tool's closure — so calling a nested tool costs one ``ainvoke``, not a rebuild.

    Nodes are de-duplicated by key, so a tool embedded twice for the same column
    runs once and both parents read the same answer. Two different columns of the
    same tool are two nodes, because they are two different questions.

    Siblings are chained rather than fanned out. LangGraph would happily run them in
    parallel, and it is the wrong trade here: the first sibling to return nothing
    ends the whole run, so running them in sequence means the second one is never
    executed at all. Chains are short by construction (``MAX_CHAIN_DEPTH``), so what
    parallelism would buy is a fraction of one query's latency, against always
    paying for queries whose answer cannot matter.
    """
    order = _ordered_nodes(chain)

    if not include_root:
        order = order[:-1]

    graph = StateGraph(ChainState)

    for position, node in enumerate(order):
        is_root = include_root and position == len(order) - 1
        graph.add_node(
            _name(node), _node_runner(node, is_root=is_root, row_limit=row_limit),
        )

    graph.add_edge(START, _name(order[0]))

    for position, node in enumerate(order[:-1]):
        following = _name(order[position + 1])
        # The conditional edge *is* the propagation rule: a node that produced no
        # values sends the run to END, so nothing above it is ever executed.
        graph.add_conditional_edges(
            _name(node),
            _continue_or_stop,
            {"continue": following, "stop": END},
        )

    graph.add_edge(_name(order[-1]), END)

    return graph.compile()


async def run_chain(
    chain: ChainNode,
    graph=None,
    agent_values: Optional[Dict[str, Any]] = None,
    resolved: Optional[Dict[str, List[Any]]] = None,
) -> ChainResult:
    """
    Run a chain and return the root's rows, or the reason there are none.

    ``graph`` is the compiled graph when the caller kept one (the tool factory
    does); otherwise it is compiled here, which is the convenient path for a one-off
    run such as **Test Query**.

    ``agent_values`` is what the model supplied for the root tool's declared
    parameters. It is passed per run rather than baked into the graph because the
    graph is compiled once per tool and every call fills its parameters differently.

    ``resolved`` supplies values a node would otherwise produce, keyed by node key. One
    caller uses it: the tool that resumes a chain an embedded graph paused. By then the
    graph's question has been answered and its run has finished, so letting that node run
    again would ask the same question of somebody who has already answered it.

    Failures are not caught. A ``ToolQueryError`` from any node is the chain's
    failure and belongs to whoever called it — the agent's tool wrapper phrases it
    for a model, the test panel shows it to an operator. A **pause** is not a failure and
    does come back, as ``ChainResult.asked``.
    """
    compiled = graph or build_chain_graph(chain)

    state = await compiled.ainvoke({
        "values": {},
        "rows": [],
        "stopped_by": "",
        "asked": {},
        "resolved": dict(resolved or {}),
        "agent_values": dict(agent_values or {}),
    })

    return ChainResult(
        rows=list(state.get("rows") or []),
        stopped_by=str(state.get("stopped_by") or ""),
        asked=dict(state.get("asked") or {}) or None,
    )


@dataclass
class ChainBindings:
    """
    A chain resolved down to what the root would be run with, without running it.

    The aggregate path needs this and cannot use :func:`run_chain`: it does not want
    the root's first two hundred rows, it wants to read the root's whole result set
    itself, in batches, which means it needs the *bindings* rather than the answer.
    Producing them here rather than there is what stops the propagation rules being
    written twice.

    ``iteration_values`` is empty for a chain with no iterating child, which is the
    ordinary case and means "one run, one result set". ``stopped_by`` names the tool
    that returned nothing, exactly as :class:`ChainResult` does.
    """

    bindings: List[dict] = field(default_factory=list)
    iteration_reference: str = ""
    iteration_values: List[Any] = field(default_factory=list)
    iteration_alias: str = ""
    stopped_by: str = ""

    @property
    def short_circuited(self) -> bool:
        return bool(self.stopped_by)

    @property
    def iterates(self) -> bool:
        return bool(self.iteration_reference)


async def resolve_chain_bindings(chain: ChainNode) -> ChainBindings:
    """
    Run every node **except** the root, and report what the root would be bound to.

    The same graph, compiled without the root: the inner nodes run in the same order,
    stop on the same condition and produce the same values, so a caller reading a
    chain's whole result set is reading the query the agent's tool would have run and
    not an approximation of it.

    A chain with no children resolves to no bindings, which is the honest answer:
    the root's query stands on its own.
    """
    if not chain.children:
        return ChainBindings()

    compiled = build_chain_graph(chain, include_root=False)
    state = await compiled.ainvoke({
        "values": {}, "rows": [], "stopped_by": "", "agent_values": {},
    })

    if state.get("stopped_by"):
        return ChainBindings(stopped_by=str(state["stopped_by"]))

    iterating = chain.iterating_child
    values = state.get("values") or {}

    return ChainBindings(
        bindings=[
            {"reference": child.parent_reference, "values": values[_name(child)]}
            for child in chain.children
            if child is not iterating and values.get(_name(child))
        ],
        iteration_reference=iterating.parent_reference if iterating else "",
        iteration_values=list(values.get(_name(iterating)) or []) if iterating else [],
        iteration_alias=iterating.value_alias if iterating else "",
    )


def _ordered_nodes(chain: ChainNode) -> List[ChainNode]:
    """Every node deepest-first, each key kept once, the root last."""
    ordered: List[ChainNode] = []
    seen = set()

    for node in chain.walk():
        key = _name(node)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(node)

    return ordered


def _name(node: ChainNode) -> str:
    """
    A node's name in the graph.

    The node's uuid plus the column being read: one tool asked for two different
    columns is two nodes producing two different lists, and naming them alike would
    silently give one parent the other's values.
    """
    return f"{node.key}#{node.child_column}" if node.child_column else node.key


#: The same function, public, because ``resolved`` is keyed by it and the caller that
#: fills that mapping is in another module. Exported as an alias rather than by renaming
#: ``_name`` so the twenty call sites in here keep reading as internal.
chain_node_name = _name


def _continue_or_stop(state: ChainState) -> str:
    """
    Whether the run carries on upward, or ends here.

    Two reasons to end, and both end it the same way: a child produced no values, or an
    embedded graph stopped to ask a question. Nothing above either one can run — in the
    first case because an empty filter would match nothing and call it an answer, in the
    second because the values do not exist yet. What they *mean* differs completely, and
    that difference is carried in state rather than in the routing.
    """
    return "stop" if state.get("stopped_by") or state.get("asked") else "continue"


def _node_runner(
    node: ChainNode, is_root: bool, row_limit: Optional[int],
) -> Callable:
    """
    The function one node runs.

    An inner node reads its column and hands the values up; the root runs the query
    the agent actually asked for and hands up its rows. Both collect what their own
    children left in ``state["values"]`` first, which is how a three-level chain
    propagates without any node knowing more than its own children.
    """

    async def run(state: ChainState) -> dict:
        bindings = _bindings_for(node, state)

        if is_root:
            return {"rows": await _root_rows(node, state, bindings, row_limit)}

        # An answer already known for this node — see `run_chain`'s `resolved`. Read
        # before anything runs, because the whole point of it is that this node's work
        # has already been done and paid for on an earlier turn.
        known = (state.get("resolved") or {}).get(_name(node))

        if known is not None:
            return {"values": {_name(node): list(known)}}

        if node.is_graph:
            return await _graph_values(node)

        # Every value the inner tool produced, however many that is. It used to be
        # refused past 2,000 — the alternative to a truncated IN list, which would have
        # built a filter that answered a different question and said nothing about it —
        # and the refusal was reached by tools that were simply about a lot of records.
        values = await execute_value_query(
            node.datasource,
            dict(node.tool.config or {}),
            node.tool.table_name,
            node.child_column,
            sql_query=node.tool.sql_query,
            table_names=tables_read(node.tool.table_name, node.tool.extra_tables),
            value_bindings=bindings,
        )

        if not values:
            logger.info(
                "Tool chain stopped at %s: no values to pass upward",
                node.tool.tool_name,
            )
            return {"stopped_by": node.tool.tool_name}

        return {"values": {_name(node): values}}

    return run


async def _graph_values(node: ChainNode) -> dict:
    """
    Run an embedded graph and collect one named key of its result as values.

    Three outcomes, and each is a different kind of thing rather than a different degree
    of success — which is why this returns three different pieces of state:

    * **it finished with values** — indistinguishable from a tool-config child from here
      up, which is the property that made a graph child cheap to add at all;
    * **it finished with nothing** — the ordinary short circuit, ``stopped_by``. A graph
      that matched nothing is an answer;
    * **it stopped to ask** — ``asked``, carrying the question verbatim, the run id it is
      parked on and this node's key. The chain ends there and the caller relays it.

    A **failure raises**, unlike the other two: ``graph_runner`` returns failures rather
    than raising, and this is the boundary where that has to change back, because every
    other node in this graph signals failure with ``ToolQueryError`` and the wrapper above
    catches exactly that. A graph that could not run is a chain that could not run.

    The graph is run **as its author** (``graph.user_id``), never as whoever owns the
    calling agent. Its nodes read datasources scoped to that author, so anything else
    would be an authorisation decision made by accident.
    """
    from app.services.graph_designer import graph_runner

    outcome = await graph_runner.run_graph(
        int(node.graph.user_id), str(node.graph.uuid),
    )

    if outcome.asks:
        question = str((outcome.question or {}).get("prompt") or "").strip()
        logger.info(
            "Tool chain paused at graph %s: waiting on an answer", node.graph.name,
        )
        return {
            "asked": {
                "graph_name": str(node.graph.name),
                "question": question,
                "run_id": outcome.run_id,
                "node": _name(node),
            },
        }

    if not outcome.finished:
        raise ToolQueryError(
            f"The embedded graph '{node.graph.name}' could not be run: "
            f"{outcome.reason or 'it did not complete.'}",
            advice=_ITERATION_ADVICE,
        )

    values = graph_values(
        await graph_runner.full_result(int(node.graph.user_id), outcome.run_id),
        node.child_column,
    )

    if not values:
        logger.info(
            "Tool chain stopped at graph %s: no values to pass upward", node.graph.name,
        )
        return {"stopped_by": str(node.graph.name)}

    return {"values": {_name(node): values}}


def graph_values(result: Any, column: str) -> List[Any]:
    """
    One named key of a finished graph's whole result, as a de-duplicated list of values.

    ``result`` is what ``graph_runner.full_result`` returned — the **uncapped** output,
    deliberately not ``GraphOutcome.rows``, which is a twenty-row sample. These values
    become a parent tool's ``IN`` filter, and a filter built from a sample answers a
    different question than the one asked while looking exactly like an answer.

    Public because the answering path needs it too: a resumed run's values are read the
    same way as a first run's, and two copies of this would be two answers to "what did
    the graph produce".

    A graph's last data-producing node returns rows, a bare list, or a single value, so
    all three are read here. Rows are read by ``column``; a list or a value has no columns
    and *is* the answer, so the name is not required to match anything. That asymmetry is
    deliberate: requiring a column name against a bare list would refuse a perfectly
    ordinary graph whose last node is a Value node holding the ids to filter on.

    ``NULL`` is dropped and duplicates collapse, exactly as ``execute_value_query`` does
    it, and for the same reasons: a NULL never matches an ``IN`` so carrying it forward
    only inflates the list, and a value repeated restricts a query once.
    """
    if isinstance(result, dict):
        # A dict output — one row, or a mapping a Value node holds. Read by name, and the
        # whole thing is one value's worth rather than a list.
        raw = [result.get(column)]
    elif isinstance(result, list):
        raw = [
            row.get(column) if isinstance(row, dict) else row
            for row in result
        ]
    elif result is None:
        raw = []
    else:
        raw = [result]

    values: List[Any] = []
    seen = set()

    for value in raw:
        if value is None:
            continue

        marker = (type(value).__name__, value)

        if marker in seen:
            continue

        seen.add(marker)
        values.append(value)

    return values


async def _root_rows(
    node: ChainNode,
    state: ChainState,
    bindings: List[dict],
    row_limit: Optional[int],
) -> List[Dict[str, Any]]:
    """
    The root's rows: one run, or one run per value of an iterating child.

    The ordinary case is the first line and always has been. The loop below exists for
    a link whose value cannot be a list — see the module docstring.

    With no ``row_limit`` — every caller but one — the loop runs every value and
    concatenates every row, so the answer covers all of them. The loop used to spend a
    row budget in order and refuse the moment the union crossed it, which meant a
    chain over fifty departments failed outright once their projects came to more than
    200 rows.

    A caller that *does* name a limit gets it as a stopping point rather than a
    refusal, and there is exactly one: **Test Query**, asking for a single row to prove
    the chain executes. It reports the column names and the count and never a value, so
    stopping early costs it nothing — where refusing would have made a chain with an
    iterating link untestable.
    """
    iterating = node.iterating_child

    if iterating is None:
        return await _run_root(node, state, bindings, row_limit)

    # Rebuilt excluding the iterating child by **identity**, not by target name. Two
    # children may legitimately restrict the same column — one narrowing it to a set,
    # one iterating over it — and filtering by name would drop the sibling's
    # restriction and silently widen every run.
    list_bindings = _bindings_for(node, state, exclude=iterating)
    values = (state.get("values") or {}).get(_name(iterating)) or []

    if len(values) > MAX_CHAIN_ITERATIONS:
        # Refused rather than truncated, and rather than run. Fifty departments'
        # worth of rows looks exactly like every department's, and the tool has no
        # way to say otherwise once the rows are in a prompt.
        raise ToolQueryError(
            f"'{iterating.tool.tool_name}' returned {len(values)} values and this "
            f"query runs once per value, which is more than the {MAX_CHAIN_ITERATIONS} "
            "runs allowed in one answer.",
            advice=_ITERATION_ADVICE,
        )

    rows: List[Dict[str, Any]] = []

    for value in values:
        # The iterating child's binding is rebuilt per value; every other child's
        # binding is the list it produced and is the same on every pass.
        per_value = [
            *list_bindings,
            {
                "reference": iterating.parent_reference,
                "values": [value],
                "expanding": False,
            },
        ]

        # Whatever is left of a named budget, or no budget at all.
        got = await _run_root(
            node,
            state,
            per_value,
            None if row_limit is None else max(1, row_limit - len(rows)),
        )

        rows.extend(
            labelled_rows(
                got,
                {iterating.value_alias: value} if iterating.value_alias else None,
            ),
        )

        if row_limit is not None and len(rows) >= row_limit:
            # Only a probe sets a budget, and it has what it came for.
            break

    return rows


async def _run_root(
    node: ChainNode,
    state: ChainState,
    bindings: List[dict],
    row_limit: Optional[int],
) -> List[Dict[str, Any]]:
    """One run of the root's own query, with whatever bindings this pass carries."""
    return await execute_tool_query(
        node.datasource,
        dict(node.tool.config or {}),
        node.tool.table_name,
        row_limit=None if row_limit is None else max(1, row_limit),
        sql_query=node.tool.sql_query,
        table_names=tables_read(node.tool.table_name, node.tool.extra_tables),
        value_bindings=bindings,
        agent_values=dict(state.get("agent_values") or {}),
        sql_params=list(node.tool.sql_params or []),
    )


# What the agent is to do when an iterating chain is refused for running too many
# times. Worded around one fact: a tool takes no arguments the visitor could narrow, so
# any suggestion that they rephrase sends the conversation back to the same tool and the
# same refusal, forever. Observed telling visitors to "specify a date range" when there
# was no date range to specify.
_ITERATION_ADVICE = (
    "Tell the user this cannot be answered at the moment and that the tool needs "
    "reconfiguring by whoever set it up. Do NOT ask them to narrow, filter or "
    "rephrase the question — you cannot pass a filter to a tool, so no rewording of "
    "theirs can change this."
)


def _bindings_for(
    node: ChainNode,
    state: ChainState,
    exclude: Optional[ChainNode] = None,
) -> List[dict]:
    """
    What this node's children produced, in the shape ``query_executor`` binds.

    A child with no entry in ``values`` has not run — which cannot happen on a path
    that reached this node, because the conditional edge ends the run instead. It is
    skipped rather than defaulted to an empty list: an empty list would build a
    query matching nothing and quietly call it an answer.

    Every binding here is a list. An iterating child's binding is rebuilt one value
    at a time by :func:`_root_rows`, which passes that child as ``exclude`` — **by
    identity, not by target name**, because two children may restrict the same
    column and dropping one of them by name would silently widen the query.
    """
    values = state.get("values") or {}

    return [
        {"reference": child.parent_reference, "values": values[_name(child)]}
        for child in node.children
        if child is not exclude and values.get(_name(child))
    ]


def describe_stop(result: ChainResult) -> Optional[str]:
    """
    One line explaining a short circuit, for the tool output.

    A nested tool returning nothing is ordinary and meaningful — *no clients
    matched, so there is nothing to total* — and a bare "0 rows" leaves a model
    unable to tell that from a broken query or missing data. Only the fact and the
    tool's name; no values, no rows.
    """
    if not result.short_circuited:
        return None

    return (
        f"The inner tool '{result.stopped_by}' returned nothing, so this tool has "
        "no rows to report. That is an answer, not a failure: nothing matched."
    )


def describe_question(result: ChainResult, tool_name: str) -> Optional[str]:
    """
    What to tell the model when an embedded graph stopped to ask something.

    The third outcome, and the one that needs the most care, because it is the only one
    where the conversation has to continue before there is any data at all. Three things
    are said and each has a failure behind it:

    * **it is not a failure.** A model told a tool failed apologises and stops, and here
      nothing is wrong — somebody is being asked a question.
    * **the question is repeated word for word.** The operator wrote it into the graph;
      ``download_service.offer_sentence`` gives the reason in full, and it is the same
      reason ``graph_tool_factory`` gives: a model rewording a question asks the user the
      wrong thing, and a paraphrase makes the next turn's answer unmatchable.
    * **how to come back.** A question the model cannot resume is a conversation that
      cannot continue, so the run id and the answering tool are both named.

    ``None`` when the chain was not waiting, so a caller can use it as the condition.
    """
    if not result.waiting:
        return None

    asked = result.asked or {}

    return (
        f"'{tool_name}' needs the user's answer before it can return anything. This is "
        f"not a failure. Ask them exactly this, word for word, and nothing else: "
        f"\"{asked.get('question') or ''}\"\n"
        f"When they reply, call answer_{tool_name} with run_id "
        f"\"{asked.get('run_id') or ''}\" and what they said."
    )
