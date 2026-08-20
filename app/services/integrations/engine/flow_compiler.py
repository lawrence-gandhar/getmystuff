"""
Turning a published workflow into a runnable LangGraph.

**The only module in this package that imports ``langgraph``.** Everything it needs to
make a decision — the rules, the runners, the ports, the previews — lives next door and is
testable without it, which is why ``pytest.importorskip("langgraph", …)`` has to guard
exactly two test modules rather than the whole feature.

## One conditional edge per node, uniformly

Every non-terminal node gets ``add_conditional_edges``, including the ones with a single
plain successor. Mixing ``add_edge`` for "simple" nodes with conditional edges for the
rest would mean a node that gains an error path has to change edge *kind* — and the error
path is the one that never gets tested. One router per node, answering one question.

    START → trigger → read → batch ──body──→ transform → validate ──valid──→ write ─┐
                                │                            │                       │
                              done                        invalid                    │
                                ↓                            ↓                       │
                             success                      failure       (back to batch)

## The router's order of precedence, and why it is this order

1. **Cancelled → ``END``.** Above the error channels, deliberately. A cancelled run must
   not take an error edge into a notification node and do more work on the way out;
   cancellation is a request to stop, not a kind of failure to handle.
2. **A handled failure** — ``errors[node_id]`` — takes the drawn error path. The run is
   *not* marked failed, because the author said what to do about it.
3. **An unhandled failure** — ``failed_at`` — ends the run.
4. ``validate`` → valid/invalid · ``filter`` → kept/dropped · ``branch`` → the first
   matching condition, else ``else`` · ``batch`` → body/done.
5. The ``default`` edge, or ``END``.

Steps 2 and 3 come before step 4 because a node that failed has not produced the records
the port question would be asked about. A failed read routed by "were there any records"
would answer "no" and take the ``done`` edge, and the run would report that it finished.

Every question in step 4 delegates to the same function in ``node_runners`` that the
runner used to write what it did into the log. One function per decision, so the log and
the route cannot disagree.

## Two failure channels, not one

Which one a failure goes into depends on whether the author drew an error path for that
node — a fact known at **compile** time, so the wrapper is told it rather than working it
out. With a single flag, a workflow that recovered from a failed step would still report
the whole run as failed, which is the opposite of what drawing a recovery path means.

## The recursion limit is computed

LangGraph's default is 25 super-steps. A hundred-pass loop over fifty thousand records
needs several hundred, and hitting the default produces ``GraphRecursionError`` — an
internal exception raised a long way from the two edges that caused it. So the ceiling is
derived from the drawing: the nodes, multiplied by what the loops are allowed to do, plus
slack. The same mistake ``download_graph._RECURSION_LIMIT`` documents, in a module where
the loops are much longer.

## What is deliberately absent

**No selection, no partial runs.** Graph Designer has them and they are right there —
running one query against a database somebody owns is a reasonable thing to test. Running
one *write* node against a live CRM with no upstream data writes garbage into somebody's
production system. The equivalent affordance is ``mode: dry_run``, which compiles the
whole workflow and calls nobody.
"""

import logging
from typing import Any, Callable, Dict, List, Mapping, Sequence, Set, Tuple

from langgraph.graph import END, START, StateGraph

from app.models.integrations import (
    NODE_BATCH,
    NODE_BRANCH,
    NODE_FILTER,
    NODE_VALIDATE,
    PORT_BODY,
    PORT_DEFAULT,
    PORT_ERROR,
    TERMINAL_NODE_TYPES,
)
from app.services.downloader_agents.base.checkpointer import get_checkpointer
from app.services.integrations.engine import flow_rules, flow_state, node_runners
from app.services.integrations.engine.node_runners import RunContext
from app.services.integrations.errors import FlowValidationError, NodeFailure, RunCancelled

logger = logging.getLogger(__name__)

#: Slack on top of the computed super-step budget. A loop pass costs more than one
#: super-step — the batch node, the body's nodes, the routers — and this is what keeps a
#: workflow that is merely large from being mistaken for one that is looping forever.
RECURSION_SLACK = 100

#: The hard ceiling on the computed limit. A run needing more super-steps than this is one
#: nobody is waiting for the end of, and LangGraph's own error is then the right backstop
#: rather than something to engineer around.
MAX_RECURSION_LIMIT = 1_000_000


class CompiledFlow:
    """
    A compiled workflow and what the caller needs to know about it.

    ``node_by_id`` travels with it because the orchestrator needs a node's label and type
    to write a step row for something the graph never reached, and re-parsing
    ``graph_data`` to find them would be a second reader of the drawing.
    """

    __slots__ = ("graph", "recursion_limit", "node_by_id", "enclosing_batch")

    def __init__(
        self,
        graph: Any,
        recursion_limit: int,
        node_by_id: Mapping[str, dict],
        enclosing_batch: Mapping[str, str],
    ) -> None:
        self.graph = graph
        self.recursion_limit = recursion_limit
        self.node_by_id = dict(node_by_id)
        self.enclosing_batch = dict(enclosing_batch)


async def compile_flow(
    graph_data: Mapping[str, Any], context: RunContext
) -> CompiledFlow:
    """
    Build the runnable graph for one run.

    The caller has already validated ``graph_data`` — ``run_service`` calls
    ``validate_flow`` first, so a run cannot execute a workflow looser than one its author
    could have saved. What is checked *here* is only what compiling itself needs: that
    there is somewhere to start.
    """
    nodes = flow_rules.nodes_of(graph_data)
    edges = flow_rules.edges_of(graph_data)

    node_by_id = {flow_rules.node_id_of(node): node for node in nodes}
    targets = _target_index(edges)
    entry = _entry_of(nodes)
    enclosing = enclosing_batches(nodes, edges)

    builder = StateGraph(flow_state.FlowState)

    for node in nodes:
        node_id = flow_rules.node_id_of(node)
        builder.add_node(
            node_id,
            _node_function(
                node,
                # The context is scoped to the loop this node sits in, which is how a step
                # inside a body finds the batch it works on without the author drawing a
                # second connection for it. Only the compiler knows the nesting.
                context.inside(enclosing.get(node_id, "")),
                has_error_path=(node_id, PORT_ERROR) in targets,
            ),
        )

    builder.add_edge(START, entry)

    for node in nodes:
        _wire(builder, node, targets)

    return CompiledFlow(
        graph=builder.compile(checkpointer=await get_checkpointer()),
        recursion_limit=recursion_limit_for(nodes),
        node_by_id=node_by_id,
        enclosing_batch=enclosing,
    )


# ---------------------------------------------------------------------------
# Node functions
# ---------------------------------------------------------------------------


def _node_function(node: dict, context: RunContext, *, has_error_path: bool) -> Callable:
    async def run(state: Mapping[str, Any]) -> dict:
        return await _guarded(node, state, context, has_error_path=has_error_path)

    return run


async def _guarded(
    node: dict,
    state: Mapping[str, Any],
    context: RunContext,
    *,
    has_error_path: bool,
) -> dict:
    """
    Run a node and turn a failure into state rather than an exception.

    An exception escaping here aborts ``ainvoke`` and loses the run's own record of why —
    so the failure becomes a value the router reads. Which channel it goes into is the
    subject of the module docstring, and it is decided by ``has_error_path``, which was
    computed from the drawing at compile time.

    :class:`RunCancelled` goes into ``cancelled`` rather than either failure channel. A
    stopped run is not a failed one, and a red badge on something somebody asked for is
    the sort of thing that sends an operator looking for a fault that does not exist.
    """
    node_id = flow_rules.node_id_of(node)

    try:
        return await node_runners.run_node(node, state, context)
    except RunCancelled as exc:
        logger.info("Run %s stopped at node %s", context.run_id, node_id)
        return {"cancelled": True, "failure_message": str(exc)}
    except NodeFailure as exc:
        message = str(exc)

        if has_error_path:
            logger.info(
                "Node %s failed and is taking its error path: %s", node_id, message
            )
            return {"errors": {node_id: message}}

        return {"failed_at": node_id, "failure_message": message}


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def _wire(builder: StateGraph, node: dict, targets: Mapping[Tuple[str, str], str]) -> None:
    """
    Give one node its outgoing edge.

    Every node conditionally, with one exception: a terminal node gets a plain edge to
    ``END`` because it has nothing to decide. ``failure`` is terminal *and* raises inside
    its runner, so it reaches this edge only in the sense that the graph is well-formed —
    the run has already ended by the time the router would have been asked.
    """
    node_id = flow_rules.node_id_of(node)

    if flow_rules.node_type_of(node) in TERMINAL_NODE_TYPES:
        builder.add_edge(node_id, END)
        return

    builder.add_conditional_edges(
        node_id, _router(node, targets), _destinations(node_id, targets)
    )


def _destinations(node_id: str, targets: Mapping[Tuple[str, str], str]) -> List[str]:
    """
    Every node this one can reach, plus ``END``.

    Declared explicitly rather than left for LangGraph to infer. Two reasons: the compiled
    graph can then be drawn and validated by LangGraph itself, and a router that returns a
    name nobody wired fails at compile time rather than mid-run at three in the morning.
    """
    reachable = {
        target for (source, _port), target in targets.items() if source == node_id
    }
    reachable.add(END)
    return sorted(reachable)


def _router(node: dict, targets: Mapping[Tuple[str, str], str]) -> Callable:
    """
    Where the run goes after this node. See the module docstring for the order.

    One closure per node, built once at compile time. The port questions delegate to
    ``node_runners``, which is what makes "the log and the route cannot disagree" a
    property of the code rather than a convention.
    """
    node_id = flow_rules.node_id_of(node)
    node_type = flow_rules.node_type_of(node)

    def route(state: Mapping[str, Any]) -> str:
        # 1. Above everything, including the error channels. A cancelled run taking an
        #    error edge into a notification node would do more work on the way out, which
        #    is precisely what cancellation asks not to happen.
        if state.get("cancelled"):
            return END

        # 2 and 3 come before the port questions because a node that failed has not
        #    produced the records those questions would be about. A failed read routed by
        #    "were there any records" answers "no", takes `done`, and the run reports that
        #    it finished.
        if (state.get("errors") or {}).get(node_id):
            # `_by_port` rather than a direct lookup. The wrapper only writes this channel
            # when the drawing has an error edge, so the edge is there — except when the
            # state came from somewhere else, which a resumed checkpoint and a replayed
            # run both are. A missing edge there should end the run, not raise a KeyError
            # from inside a router where nothing can turn it into a sentence.
            return _by_port(node_id, PORT_ERROR, targets)

        if str(state.get("failed_at") or "") == node_id:
            return END

        chooser = _PORT_CHOOSERS.get(node_type)
        if chooser is not None:
            return _by_port(node_id, chooser(node, state), targets)

        return targets.get((node_id, PORT_DEFAULT), END)

    return route


#: The four node types whose exit is a question rather than a fact, and the function that
#: answers each. **The same function the runner used**, which is what makes "the log and
#: the route cannot disagree" a property of the code rather than a convention. A table
#: rather than a chain of ``if``s so that adding a Phase 2 type is one line here and none
#: inside the router.
_PORT_CHOOSERS: Dict[str, Callable[[dict, Mapping[str, Any]], str]] = {
    NODE_VALIDATE: node_runners.validate_port,
    NODE_FILTER: node_runners.filter_port,
    NODE_BRANCH: node_runners.branch_port,
    NODE_BATCH: node_runners.batch_port,
}


def _by_port(
    node_id: str, port: str, targets: Mapping[Tuple[str, str], str]
) -> str:
    """
    The edge on a named port, falling back to ``default`` and then to ``END``.

    A port with nothing drawn on it ends the run rather than raising. That is what the
    drawing says: an author who wired ``valid`` and left ``invalid`` bare has said the
    invalid records go nowhere, and they are already counted and logged. Guessing a
    successor they did not draw would be inventing a step.
    """
    return targets.get((node_id, port)) or targets.get((node_id, PORT_DEFAULT), END)


def _target_index(edges: Sequence[Mapping[str, Any]]) -> Dict[Tuple[str, str], str]:
    """``(source, port) -> target``. ``validate_flow`` has already refused two edges on
    one port, so a later edge silently winning is not a case that can arise here."""
    return {
        (
            str(edge.get("source") or ""),
            flow_rules.source_port_of(edge),
        ): str(edge.get("target") or "")
        for edge in edges
        if edge.get("source") and edge.get("target")
    }


def _entry_of(nodes: Sequence[Mapping[str, Any]]) -> str:
    """
    Where the run starts: the one trigger.

    ``validate_flow`` refuses a workflow without exactly one, so this raising is a
    backstop for a version published before that rule existed or a row edited by hand —
    and it raises the validator's own exception type so the caller has one thing to catch.
    """
    for node in nodes:
        if flow_rules.node_type_of(node) == flow_rules.NODE_TRIGGER:
            return flow_rules.node_id_of(node)

    raise FlowValidationError(
        "This workflow has no trigger, so there is nowhere for a run to start."
    )


# ---------------------------------------------------------------------------
# Nesting and bounds
# ---------------------------------------------------------------------------


def enclosing_batches(
    nodes: Sequence[Mapping[str, Any]], edges: Sequence[Mapping[str, Any]]
) -> Dict[str, str]:
    """
    ``node_id -> the batch node whose body it sits in``, or absent for a node outside any.

    Walked from each ``batch`` node along its ``body`` edge and onwards, stopping when the
    walk returns to that batch — which it must, because ``validate_flow`` refuses a body
    that never comes back. Only the compiler can work this out: a runner sees one node and
    the state, and the state says nothing about the shape of the drawing.

    An inner loop wins over an outer one. The walk assigns on first visit and does not
    overwrite, and the inner batch's own walk runs over a strictly smaller set — so a node
    in a nested body is attributed to the loop it is actually in. Phase 1's validator does
    not refuse nesting, so this is not hypothetical.
    """
    by_source: Dict[str, List[Tuple[str, str]]] = {}
    for edge in edges:
        source = str(edge.get("source") or "")
        by_source.setdefault(source, []).append(
            (flow_rules.source_port_of(edge), str(edge.get("target") or ""))
        )

    batch_ids = [
        flow_rules.node_id_of(node)
        for node in nodes
        if flow_rules.node_type_of(node) == NODE_BATCH
    ]

    enclosing: Dict[str, str] = {}

    # Innermost first is not knowable before walking, so every batch walks and the
    # *shorter* walk wins — an inner body is a subset of the outer one, so its members are
    # reassigned to it. Ordering by walk size rather than by drawing order is what makes
    # that deterministic.
    walks = {batch_id: _body_of(batch_id, by_source) for batch_id in batch_ids}

    for batch_id in sorted(walks, key=lambda key: len(walks[key]), reverse=True):
        for member in walks[batch_id]:
            enclosing[member] = batch_id

    return enclosing


def _body_of(batch_id: str, by_source: Mapping[str, List[Tuple[str, str]]]) -> Set[str]:
    """Every node reachable from a batch's ``body`` port before the walk returns to it."""
    start = next(
        (target for port, target in by_source.get(batch_id, ()) if port == PORT_BODY),
        "",
    )
    if not start:
        return set()

    seen: Set[str] = set()
    queue = [start]

    while queue:
        current = queue.pop()
        if current in seen or current == batch_id:
            continue
        seen.add(current)
        queue.extend(target for _port, target in by_source.get(current, ()))

    return seen


def recursion_limit_for(nodes: Sequence[Mapping[str, Any]]) -> int:
    """
    How many super-steps this workflow is allowed.

    ``nodes × Σ max_batches + slack``. Computed rather than defaulted, because LangGraph's
    default of 25 would stop a perfectly ordinary hundred-pass loop with an internal
    exception raised a long way from the two edges that caused it.

    Multiplied rather than added because a loop re-runs its whole body every pass: a
    six-node body over a thousand passes is six thousand super-steps, and a limit derived
    from the node count alone would be off by three orders of magnitude.
    """
    passes = sum(
        flow_rules.max_batches_of(flow_rules.data_of(node))
        for node in nodes
        if flow_rules.node_type_of(node) == NODE_BATCH
    ) or 1

    computed = len(nodes) * passes + RECURSION_SLACK
    return min(computed, MAX_RECURSION_LIMIT)
