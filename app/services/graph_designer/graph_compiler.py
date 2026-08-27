"""
Turning a drawing into a runnable LangGraph.

The only module in this package that imports ``langgraph``. Everything it needs to make a
decision — the rules, the runners, the conditions, the previews — lives next door and is
testable without it, which is the same split ``tool_chain_service`` / ``tool_chain_graph``
makes and the reason ``pytest.importorskip("langgraph")`` only has to guard this file's
tests.

## The shape of the compiled graph

One LangGraph node per authored node, and **every node gets a conditional edge** rather
than a plain one. That is the central decision here and it is what makes the rest simple:
a single router per node answers one question — where does the run go from here — and it
answers it for the ordinary case, the branch case, the loop case and the failure case in
the same place. Mixing ``add_edge`` for "simple" nodes with ``add_conditional_edges`` for
the rest would mean a node that gained an error path had to change edge *kind*, and the
failure path would be the one that never got tested.

    START → start ─→ sql ─→ for_each ──body──→ tool_config ─┐
                       │        │                            │
                     error      └──done──→ success            └──→ (back to for_each)
                       ↓
                    failure

## How a failure travels

A runner raises ``NodeFailure``. The wrapper catches it and writes one of two things into
state, and **which one depends on whether the author drew an error path for that node** —
a fact known at compile time, so the wrapper is told it rather than working it out:

* an error path exists → ``errors[node_id]``, and the router takes that path. The run is
  *not* marked failed, because the author said what to do about it.
* no error path → ``failed_at`` / ``failure_message``, and the router ends the run.

Two channels rather than one, because "this node failed and we handled it" and "this run
failed" are different facts and a single flag cannot hold both. With one flag, a graph
that recovered from a failed node would still report the whole run as failed — which is
the opposite of what drawing a recovery path means.

## Loops

A loop node is entered by ``START``-side flow once and then re-entered by its own back
edge. Its runner tells the two apart by ``started`` on the cursor, which is why loading
the list and advancing the cursor are one function rather than two nodes: a drawing has
one box there, so the compiled graph has one node there.

``recursion_limit`` is **computed**, not defaulted. LangGraph's default is 25 super-steps,
which would stop a perfectly valid loop over 30 rows with ``GraphRecursionError`` — an
internal exception, raised a long way from the two edges that caused it. That is the exact
mistake ``download_graph._RECURSION_LIMIT`` documents, so the ceiling here is derived from
the drawing: the nodes, multiplied by what the loops are allowed to do, plus slack.

## Testing a selection

A selection compiles as the **induced subgraph** — the chosen nodes and the authored edges
between them. Choosing nodes that are not connected is an ordinary thing to do ("does this
query work, and does that one"), so the disconnected pieces are chained in the drawing's
topological order: each piece's dead ends lead into the next piece's entry. The chaining
is worked out at compile time, so nothing about it is decided while the run is in flight.

A node in the selection that reads a node *outside* it fails with a step row naming what
is missing, rather than reading ``None`` and carrying on. A ``for_each`` over an absent
list would otherwise loop zero times and report success, which is a green tick on a test
that tested nothing.
"""

import logging
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from litestar.exceptions import HTTPException

from app.models.graph_designer import (
    HUMAN_EXPECTS_CHOICE,
    HUMAN_EXPECTS_CONFIRM,
    LOOP_NODE_TYPES,
    NODE_BRANCH,
    NODE_HUMAN,
    NODE_SQL_UNION,
    NODE_START,
    STEP_SKIPPED,
    TERMINAL_NODE_TYPES,
)
from app.services.downloader_agents.base.checkpointer import get_checkpointer
from app.services.graph_designer import (
    graph_state,
    node_runners,
    node_variables,
    run_store,
)
from app.services.graph_designer.graph_service import (
    DEFAULT_MAX_ITERATIONS,
    PORT_BODY,
    PORT_DEFAULT,
    PORT_DONE,
    PORT_ERROR,
    PORT_EXECUTE,
    node_label,
)
from app.services.graph_designer.node_runners import NodeFailure, RunContext

logger = logging.getLogger(__name__)

# Slack on top of the computed super-step budget. A loop costs more than one super-step
# per pass — the loop node, the body's nodes, the router — and this is what keeps a graph
# that is merely large from being mistaken for one that is looping forever.
_RECURSION_SLACK = 50

# The hard ceiling on the computed limit. A run that needs more super-steps than this is
# one nobody is waiting for the end of, and LangGraph's own error is then the right
# backstop rather than something to engineer around.
_MAX_RECURSION_LIMIT = 500_000


class CompiledGraph:
    """
    A compiled graph and what the caller needs to know about it.

    ``skipped`` is the nodes a selection left out. They are returned rather than written
    here because writing them is the orchestrator's job — this class is the product of
    compiling, and compiling touches no rows.
    """

    __slots__ = ("graph", "recursion_limit", "skipped", "node_by_id")

    def __init__(
        self,
        graph: Any,
        recursion_limit: int,
        skipped: Sequence[dict],
        node_by_id: Mapping[str, dict],
    ) -> None:
        self.graph = graph
        self.recursion_limit = recursion_limit
        self.skipped = list(skipped)
        self.node_by_id = dict(node_by_id)


async def compile_graph(
    graph_data: Mapping[str, Any],
    context: RunContext,
    selection: Optional[Sequence[str]] = None,
) -> CompiledGraph:
    """
    Build the runnable graph for one run.

    ``selection`` limits it to those node ids; ``None`` compiles the whole drawing. The
    caller has already validated ``graph_data`` — ``graph_run_service`` calls
    ``graph_service.validate_graph`` first, so a run cannot execute a drawing looser than
    one the designer would have saved.
    """
    all_nodes = [n for n in (graph_data.get("nodes") or []) if isinstance(n, dict)]
    all_edges = [e for e in (graph_data.get("edges") or []) if isinstance(e, dict)]

    node_by_id = {str(node.get("id")): node for node in all_nodes}

    chosen, skipped = _partition(all_nodes, selection)
    chosen_ids = {str(node.get("id")) for node in chosen}

    edges = [
        edge for edge in all_edges
        if str(edge.get("source")) in chosen_ids
        and str(edge.get("target")) in chosen_ids
    ]

    targets = _target_index(edges)
    entry, fallthrough = _entry_and_chaining(chosen, edges, node_by_id, selection)
    loop_of = _enclosing_loops(chosen, all_edges, node_by_id)

    builder = StateGraph(graph_state.GraphState)

    # `None` for a whole-graph run rather than the full id set, so the dependency check
    # inside `run_node` is skipped outright instead of always passing. A selection is
    # the only case where a node can be asked to read something that is not running.
    # The drawing travels with the context: a runner needs its *own* node's settings, but
    # filling a parameter from a loop needs the loop's, and only the compiler has the map.
    covering = context.covering(chosen_ids if selection else None, node_by_id)

    for node in chosen:
        node_id = str(node.get("id"))
        has_error_path = (node_id, PORT_ERROR) in targets

        builder.add_node(
            node_id,
            _node_function(
                node,
                covering.for_loop(loop_of.get(node_id, "")),
                has_error_path=has_error_path,
            ),
        )

    builder.add_edge(START, entry)

    for node in chosen:
        _wire(builder, node, targets, fallthrough, loop_of.get(str(node.get("id")), ""))

    graph = builder.compile(checkpointer=await get_checkpointer())

    return CompiledGraph(
        graph=graph,
        recursion_limit=_recursion_limit(chosen),
        skipped=skipped,
        node_by_id=node_by_id,
    )


# --------------------------------------------------------------------------
# Node functions
# --------------------------------------------------------------------------

def _node_function(
    node: dict,
    context: RunContext,
    has_error_path: bool,
) -> Callable:
    """
    The callable one LangGraph node runs.

    A ``human`` node gets its own wrapper because it has to pause, and pausing is the one
    thing that cannot be done inside ``run_node``: ``interrupt()`` unwinds the whole
    call, and on resume LangGraph **re-runs the node from the top**. So everything
    expensive — and specifically the step row — has to sit *after* the interrupt, or it
    would be written twice and the log would show the question being asked twice.
    """
    if str(node.get("type")) == NODE_HUMAN:
        return _human_function(node, context, has_error_path)

    async def run(state: Mapping[str, Any]) -> dict:
        return await _guarded(node, state, context, has_error_path)

    return run


def _human_function(
    node: dict,
    context: RunContext,
    has_error_path: bool,
) -> Callable:
    """
    A node that stops the run and waits for a person.

    ``interrupt()`` suspends everything here and the state is written to the
    checkpointer. What it returns, when the run is resumed with ``Command(resume=…)``, is
    the answer — and only then does the node do any work.

    Nothing is written before the interrupt. The run row is marked ``awaiting_input`` by
    the orchestrator, which reads the pause off ``ainvoke``'s result: doing it here would
    run again on resume and leave the row saying "waiting" while the run carried on.
    """
    node_id = str(node.get("id") or "")

    async def run(state: Mapping[str, Any]) -> dict:
        # The question is substituted here as well as inside `run_node`, because
        # `interrupt()` puts it in front of a person *before* the runner is ever reached
        # — without this, a question written "Approve {{TOTAL}}?" would be shown with the
        # braces still in it.
        #
        # Rendering twice is free and deterministic: `interrupt()` re-runs this node from
        # the top on resume anyway, and the renderer reads only `outputs`, which no
        # answer changes.
        try:
            asked = node_variables.render_node(node, state)
        except HTTPException:
            # A variable the question needs did not resolve. Pausing on a half-written
            # question would strand the run on something nobody can sensibly answer, so
            # fall through to the guarded path instead — which opens the step row, records
            # the reason, and takes the error port if the author drew one.
            return await _guarded(node, state, context, has_error_path)

        answer = interrupt(_question(asked))

        # The answer belongs in state before the runner reads it, and the runner reads it
        # from state rather than from an argument so that a resumed run and a replayed
        # checkpoint behave identically.
        answered = {
            **dict(state),
            "answers": graph_state._merge(state.get("answers"), {node_id: answer}),
        }

        update = await _guarded(node, answered, context, has_error_path)
        update.setdefault("answers", {})[node_id] = answer
        return update

    return run


async def _guarded(
    node: dict,
    state: Mapping[str, Any],
    context: RunContext,
    has_error_path: bool,
) -> dict:
    """
    Run a node and turn a failure into state rather than an exception.

    An exception escaping here would abort ``ainvoke`` and lose the run's own record of
    why — so the failure becomes a value, and the router reads it. Which channel it goes
    into is the whole subject of the module docstring.
    """
    try:
        return await node_runners.run_node(node, state, context)
    except NodeFailure as exc:
        message = str(exc)

        if has_error_path:
            logger.info(
                "Node %s failed and is taking its error path: %s",
                node.get("id"), message,
            )
            return {"errors": {str(node.get("id")): message}}

        return {
            "failed_at": str(node.get("id")),
            "failure_message": message,
        }


def _question(node: dict) -> dict:
    """
    The payload a paused run puts in front of a person.

    Stored on the run row and rendered by the dock. It carries the node's id so the dock
    can highlight *which* box is waiting — a graph with three human nodes otherwise
    presents three identical prompts.
    """
    data = node.get("data") or {}

    return {
        "node_id": str(node.get("id") or ""),
        "node_label": node_label(node),
        "prompt": str(data.get("prompt") or "").strip(),
        "expects": str(data.get("expects") or "").strip(),
        "choices": [
            str(choice).strip()
            for choice in (data.get("choices") or [])
            if str(choice).strip()
        ],
    }


# --------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------

def _wire(
    builder: StateGraph,
    node: dict,
    targets: Mapping[Tuple[str, str], str],
    fallthrough: Mapping[str, str],
    loop_id: str = "",
) -> None:
    """
    Give one node its outgoing conditional edge.

    Every node, uniformly — see the module docstring, and now with no exceptions at all.
    A terminal node used to be one: it got a plain ``add_edge(node_id, END)``, on the
    reasoning that it has nothing to decide. It does have one thing to decide, which is
    whether the author drew anything after it, and ``_router`` answers that for terminals
    the same way it answers it for every other node.

    Dropping the special case fixed a quiet bug in **tested selections**, too. A selection
    made of two disconnected pieces chains each piece's dead ends into the next piece's
    entry (:func:`_entry_and_chaining`), and a terminal node at the end of the first piece
    is such a dead end — but the plain edge to ``END`` ignored ``fallthrough``, so the
    second piece never ran and nothing in the log said why. Its nodes were simply absent.

    ``loop_id`` is the enclosing loop from :func:`_enclosing_loops`, passed on to the
    router because a union node's outcome is a question about that loop's cursor rather
    than about anything in its own settings. Nothing else uses it, and it is a compile-time
    fact, so closing over it here beats making every router re-derive it per visit.
    """
    node_id = str(node.get("id"))

    builder.add_conditional_edges(
        node_id,
        _router(node, targets, fallthrough, loop_id),
        _destinations(node, targets, fallthrough),
    )


def _destinations(
    node: dict,
    targets: Mapping[Tuple[str, str], str],
    fallthrough: Mapping[str, str],
) -> List[str]:
    """
    Every node this one can reach, plus ``END``.

    Declared explicitly rather than left for LangGraph to infer from the router's return
    value. Two reasons: the compiled graph can then be drawn and validated by LangGraph
    itself, and a router that returned a name nobody wired fails at compile time instead
    of mid-run.
    """
    node_id = str(node.get("id"))

    reachable = {
        target for (source, _port), target in targets.items() if source == node_id
    }

    if node_id in fallthrough:
        reachable.add(fallthrough[node_id])

    reachable.add(END)
    return sorted(reachable)


def _router(
    node: dict,
    targets: Mapping[Tuple[str, str], str],
    fallthrough: Mapping[str, str],
    loop_id: str = "",
) -> Callable:
    """
    Where the run goes after this node.

    **A terminal node is answered here, at compile time, and asks nothing of state.** It
    has one thing to decide — whether the author drew anything after it — and that is a fact
    about the drawing. Answering it up here is not only cheaper; it is the only correct
    place, because a ``failure`` node sets ``failed_at`` to *its own id* as its entire job
    (``node_runners._run_failure``), which is indistinguishable inside ``route`` from a node
    whose runner blew up. Falling through to the generic failure check would therefore send
    every ``failure`` node to ``END`` and silently drop whatever the author drew after it —
    no error, no step row, nothing in the log. Kept out of ``route`` rather than ordered
    first within it, so that no later edit can reorder it back into being a bug.

    Everything else gets one function per node, answering in a fixed order of precedence:

    1. **A handled failure** takes the error path. First, because a node that failed has
       not produced the output a branch or a loop would go on to read, so asking those
       questions of it would compare against nothing.
    2. **An unhandled failure** ends the run.
    3. **A branch** takes the port its conditions chose.
    4. **A loop** goes round or moves on.
    5. **A union** leaves by ``execute`` on the pass it ran, and by ``default`` — back
       round the loop — on every pass it only appended to.
    6. **Anything else** follows its ``default`` port, or the selection's chaining, or
       ends the run.

    The branch, the loop and the union delegate to ``node_runners.branch_port``,
    ``loop_continues`` and ``union_executes`` — the same functions the runners use to write
    what they did into the log. One function per decision, so the log and the route cannot
    disagree.

    The union is **after** the two failure cases and not before, which is what makes a
    failed query take the error path rather than ``execute``: it did not run, so there is
    nothing for the next node to read.
    """
    node_id = str(node.get("id"))
    node_type = str(node.get("type") or "")

    if node_type in TERMINAL_NODE_TYPES:
        settled = _default_of(node_id, targets, fallthrough)

        def leave(_state: Mapping[str, Any]) -> str:
            return settled

        return leave

    def route(state: Mapping[str, Any]) -> str:
        if (state.get("errors") or {}).get(node_id):
            return targets[(node_id, PORT_ERROR)]

        if str(state.get("failed_at") or "") == node_id:
            return END

        if node_type == NODE_BRANCH:
            port = node_runners.branch_port(node, state)
            return targets.get((node_id, port)) or _default_of(
                node_id, targets, fallthrough,
            )

        if node_type in LOOP_NODE_TYPES:
            port = PORT_BODY if node_runners.loop_continues(node, state) else PORT_DONE
            return targets.get((node_id, port)) or _default_of(
                node_id, targets, fallthrough,
            )

        if node_type == NODE_SQL_UNION and node_runners.union_executes(
            node, state, loop_id,
        ):
            return targets.get((node_id, PORT_EXECUTE)) or _default_of(
                node_id, targets, fallthrough,
            )

        return _default_of(node_id, targets, fallthrough)

    return route


def _default_of(
    node_id: str,
    targets: Mapping[Tuple[str, str], str],
    fallthrough: Mapping[str, str],
) -> str:
    """
    Where a node with nothing special to decide goes.

    Its ``default`` edge if it has one; otherwise the next disconnected piece of a tested
    selection; otherwise the run ends. A node with no outgoing edge ending the run is the
    right default — it is what the drawing says, and the alternative would be to guess a
    successor the author did not draw.
    """
    target = targets.get((node_id, PORT_DEFAULT))

    if target:
        return target

    return fallthrough.get(node_id, END)


def _target_index(edges: Sequence[Mapping[str, Any]]) -> Dict[Tuple[str, str], str]:
    """
    ``(source, port) -> target``.

    A dict is safe here only because ``validate_graph`` has already refused two edges on
    one port; without that rule this would silently keep the last one and the run would
    take an edge the author could not predict.
    """
    return {
        (
            str(edge.get("source")),
            str(edge.get("source_port") or PORT_DEFAULT) or PORT_DEFAULT,
        ): str(edge.get("target"))
        for edge in edges
    }


# --------------------------------------------------------------------------
# Selections
# --------------------------------------------------------------------------

def _partition(
    nodes: Sequence[dict],
    selection: Optional[Sequence[str]],
) -> Tuple[List[dict], List[dict]]:
    """
    Split the drawing into what this run covers and what it leaves out.

    An empty or absent selection means the whole graph. A selection naming nothing that
    exists is refused by the caller, not silently widened to everything — "run these
    three nodes" and "run all of them" must never be the same request.
    """
    if not selection:
        return list(nodes), []

    wanted = set(selection)
    chosen = [node for node in nodes if str(node.get("id")) in wanted]
    skipped = [node for node in nodes if str(node.get("id")) not in wanted]

    return chosen, skipped


def _entry_and_chaining(
    chosen: Sequence[dict],
    edges: Sequence[Mapping[str, Any]],
    node_by_id: Mapping[str, dict],
    selection: Optional[Sequence[str]],
) -> Tuple[str, Dict[str, str]]:
    """
    Where the run begins, and how disconnected pieces of a selection are joined.

    For a whole-graph run this is simply the Start node and no chaining — the drawing
    already says where it begins.

    For a selection it is a real question, because choosing three unconnected nodes is an
    ordinary way to ask "do these three work". The pieces are ordered by where they sit in
    the drawing, and each piece's dead ends lead into the next piece's entry, so the run
    covers everything that was chosen in an order that matches the picture. Worked out
    here, at compile time, so nothing about it is being decided while the run is in
    flight.
    """
    chosen_ids = [str(node.get("id")) for node in chosen]

    if not selection:
        start = next(
            (
                node_id for node_id in chosen_ids
                if str(node_by_id[node_id].get("type")) == NODE_START
            ),
            chosen_ids[0],
        )
        return start, {}

    order = _topological(chosen_ids, edges)
    components = _components(chosen_ids, edges, order)

    entries = [component[0] for component in components]
    chaining: Dict[str, str] = {}

    outgoing = {str(edge.get("source")) for edge in edges}

    for position, component in enumerate(components):
        following = entries[position + 1] if position + 1 < len(entries) else None

        if following is None:
            continue

        for node_id in component:
            if node_id not in outgoing:
                chaining[node_id] = following

    return entries[0], chaining


def _topological(
    node_ids: Sequence[str],
    edges: Sequence[Mapping[str, Any]],
) -> List[str]:
    """
    The chosen nodes in the order the drawing runs them, cycles tolerated.

    Kahn's algorithm, and whatever a cycle leaves behind is appended in its original
    order. Tolerated rather than refused because a loop *is* a cycle and this ordering is
    only used to pick a sensible entry point — a question that has an acceptable answer
    even when part of the graph goes round.
    """
    indegree = {node_id: 0 for node_id in node_ids}
    adjacency: Dict[str, List[str]] = {node_id: [] for node_id in node_ids}

    for edge in edges:
        source = str(edge.get("source"))
        target = str(edge.get("target"))

        if source in adjacency and target in indegree:
            adjacency[source].append(target)
            indegree[target] += 1

    queue = [node_id for node_id in node_ids if indegree[node_id] == 0]
    ordered: List[str] = []

    while queue:
        node_id = queue.pop(0)
        ordered.append(node_id)

        for neighbour in adjacency[node_id]:
            indegree[neighbour] -= 1
            if indegree[neighbour] == 0:
                queue.append(neighbour)

    ordered.extend(node_id for node_id in node_ids if node_id not in ordered)
    return ordered


def _components(
    node_ids: Sequence[str],
    edges: Sequence[Mapping[str, Any]],
    order: Sequence[str],
) -> List[List[str]]:
    """
    The connected pieces of a selection, each in topological order.

    Connectivity is **undirected** here: two nodes joined by an edge belong to the same
    piece whichever way the arrow points, because they are one thing the user selected
    together. The pieces themselves come out in the order their first node appears in the
    drawing's flow.
    """
    neighbours: Dict[str, Set[str]] = {node_id: set() for node_id in node_ids}

    for edge in edges:
        source = str(edge.get("source"))
        target = str(edge.get("target"))

        if source in neighbours and target in neighbours:
            neighbours[source].add(target)
            neighbours[target].add(source)

    position = {node_id: index for index, node_id in enumerate(order)}
    seen: Set[str] = set()
    components: List[List[str]] = []

    for node_id in order:
        if node_id in seen:
            continue

        stack = [node_id]
        seen.add(node_id)
        member: List[str] = []

        while stack:
            current = stack.pop()
            member.append(current)

            for neighbour in neighbours[current]:
                if neighbour not in seen:
                    seen.add(neighbour)
                    stack.append(neighbour)

        components.append(sorted(member, key=lambda name: position[name]))

    return components


def _enclosing_loops(
    chosen: Sequence[dict],
    all_edges: Sequence[Mapping[str, Any]],
    node_by_id: Mapping[str, dict],
) -> Dict[str, str]:
    """
    For each node, the innermost loop whose body it sits in.

    Used only to label a step row with which pass it belongs to. Worked out by walking
    forward from each loop's ``body`` port and stopping when the loop itself is reached
    again — so the nodes on the way round are the nodes inside it. Later (more deeply
    nested) loops overwrite earlier ones, which is what "innermost" means: a node inside
    two loops is labelled with the pass of the one closest to it.
    """
    adjacency: Dict[str, List[Tuple[str, str]]] = {}

    for edge in all_edges:
        source = str(edge.get("source"))
        port = str(edge.get("source_port") or PORT_DEFAULT) or PORT_DEFAULT
        adjacency.setdefault(source, []).append((port, str(edge.get("target"))))

    chosen_ids = {str(node.get("id")) for node in chosen}
    enclosing: Dict[str, str] = {}

    loops = [
        str(node.get("id")) for node in node_by_id.values()
        if str(node.get("type")) in LOOP_NODE_TYPES
    ]

    for loop_id in loops:
        body = [
            target for port, target in adjacency.get(loop_id, [])
            if port == PORT_BODY
        ]

        stack = list(body)
        walked: Set[str] = set()

        while stack:
            node_id = stack.pop()

            if node_id in walked or node_id == loop_id:
                continue

            walked.add(node_id)

            if node_id in chosen_ids:
                enclosing[node_id] = loop_id

            stack.extend(target for _port, target in adjacency.get(node_id, []))

    return enclosing


# --------------------------------------------------------------------------
# The recursion limit
# --------------------------------------------------------------------------

def _recursion_limit(chosen: Sequence[dict]) -> int:
    """
    How many super-steps this graph is allowed.

    Derived from the drawing rather than left at LangGraph's default of 25, which would
    stop a valid loop over 30 rows. The sum of the loops' ceilings is what a run can
    legitimately cost in passes; multiplying by the node count covers the body each pass
    walks through, and the slack covers the routers.

    Capped, because the point of the number is to distinguish "large" from "forever" —
    past the cap, LangGraph's own error is the right backstop and there is nothing to be
    gained by computing a bigger one.
    """
    node_count = max(1, len(chosen))

    passes = sum(
        _ceiling_of(node) for node in chosen
        if str(node.get("type")) in LOOP_NODE_TYPES
    )

    if not passes:
        return min(_MAX_RECURSION_LIMIT, node_count + _RECURSION_SLACK)

    return min(_MAX_RECURSION_LIMIT, node_count * passes + _RECURSION_SLACK)


def _ceiling_of(node: Mapping[str, Any]) -> int:
    """One loop's iteration ceiling, defaulting rather than becoming unbounded."""
    data = node.get("data") or {}

    try:
        return max(1, int(data.get("max_iterations") or DEFAULT_MAX_ITERATIONS))
    except (TypeError, ValueError):
        return DEFAULT_MAX_ITERATIONS


# --------------------------------------------------------------------------
# Running
# --------------------------------------------------------------------------

def run_config(compiled: CompiledGraph, thread_id: str) -> dict:
    """The invoke configuration: which thread, and how many super-steps."""
    return {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": compiled.recursion_limit,
    }


def interrupt_payload(state: Any) -> Optional[dict]:
    """
    The question a paused run stopped on, if it paused.

    LangGraph reports pauses under ``__interrupt__`` in the returned state. Read
    defensively — as objects with ``.value`` and as plain dicts — because that key's exact
    shape is langgraph's to change and a paused run is not worth breaking over it. Same
    function, same reasoning, as ``download_graph._interrupt_payload``.
    """
    if not isinstance(state, Mapping):
        return None

    for entry in state.get("__interrupt__") or []:
        value = getattr(entry, "value", None)

        if value is None and isinstance(entry, Mapping):
            value = entry.get("value")

        if isinstance(value, Mapping):
            return dict(value)

    return None


def resume_command(answer: Any) -> Command:
    """
    Hand an answer back to the ``interrupt()`` that is waiting for it.

    A thin wrapper so ``graph_run_service`` does not import ``langgraph.types`` — this
    module is the boundary, and keeping it that way is what lets everything else in the
    package be imported without the library.
    """
    return Command(resume=answer)


async def record_skipped(run_id: int, skipped: Sequence[dict]) -> None:
    """
    Write a ``skipped`` row for every node a selection left out.

    A row rather than nothing, because **a node missing from the log is
    indistinguishable from a node the run never reached** — and "I deliberately only
    tested these two" is exactly what somebody reading a selection run needs to be told.
    """
    for node in skipped:
        await run_store.record_step(
            run_id,
            str(node.get("id") or ""),
            str(node.get("type") or ""),
            node_label(node),
            STEP_SKIPPED,
            message="Not part of this test.",
        )


def validated_answer(payload: Mapping[str, Any], answer: Any) -> Any:
    """
    Check a human's answer against what the node said it expects.

    Here rather than in the runner because the *resume endpoint* has to refuse a bad
    answer while the person is still on the page — telling them at that point is useful,
    whereas resuming the run and failing a node three steps later is not.

    A ``confirm`` node's answer becomes a real boolean, so a branch comparing it to
    ``true`` works without the author knowing whether the browser sent ``"yes"`` or
    ``"true"``.
    """
    expects = str(payload.get("expects") or "")
    text = "" if answer is None else str(answer).strip()

    if expects == HUMAN_EXPECTS_CONFIRM:
        if text.lower() in ("true", "yes", "y", "1", "on"):
            return True
        if text.lower() in ("false", "no", "n", "0", "off"):
            return False
        raise ValueError("Please answer yes or no.")

    if expects == HUMAN_EXPECTS_CHOICE:
        choices = [str(choice) for choice in (payload.get("choices") or [])]

        if text not in choices:
            offered = ", ".join(choices) or "nothing"
            raise ValueError(f"Please pick one of: {offered}.")

        return text

    if not text:
        raise ValueError("Please give an answer before continuing.")

    return text
