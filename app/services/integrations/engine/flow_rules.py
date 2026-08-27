"""
What a workflow may be, and every reason one is refused.

**One source, three readers.** :func:`node_specs` is what ``/integrations/vocabulary``
serves the canvas, what the AI prompt renderer lists for the model, and what
:func:`validate_flow` checks against. Graph Designer keeps its port table in
``graph_canvas.js`` as well as in Python, and the two can drift — a palette offering an
exit the validator refuses. Here the canvas is told, so adding a node type touches no
JavaScript and the drift has nowhere to happen.

**Split out of ``flow_service`` deliberately.** ``graph_designer/graph_service.py`` is
2079 lines because its validator shares a file with its CRUD. The validator has three
importers — the compiler, the runners and the routes — and the CRUD has one. That is a
real seam, taken from the start rather than after the file gets long.

**Validation is identical for save, publish and run**, which is Graph Designer's rule
for Graph Designer's reason: a run that validated more loosely than the save would be a
run of a workflow its author could not have stored. :func:`validate_for_publish` is
*additional*, never alternative — it adds the one rule a draft is allowed to break.

**Refusals are exceptions carrying a node id**, not returned sentences. The opposite
call from ``filter_algebra``, and for a different situation: there, a refusal is
feedback to a language model mid-plan and raising would mean the plan was never
validated. Here the reader is a person looking at a canvas, the answer is to edit one
node, and the id is what lets the canvas highlight it instead of showing a banner about
a graph they have to search by hand.

Every message names the node the way the canvas labels it, and says what to do. "Invalid
graph" is not a sentence anybody can act on.
"""

import uuid as uuid_pkg
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from app.models.integrations import (
    CONNECTOR_NODE_TYPES,
    DEFAULT_BATCH_SIZE,
    LOOP_NODE_TYPES,
    MAX_BATCH_SIZE,
    MIN_BATCH_SIZE,
    MIN_INTERVAL_SECONDS,
    NODE_BATCH,
    NODE_BRANCH,
    NODE_EMAIL,
    NODE_CONNECTOR_READ,
    NODE_CONNECTOR_WRITE,
    NODE_FAILURE,
    NODE_FILTER,
    NODE_PORTS,
    NODE_SUCCESS,
    NODE_TRANSFORM,
    NODE_TRIGGER,
    NODE_TYPE_LABELS,
    NODE_TYPE_VALUES,
    NODE_VALIDATE,
    OVERLAP_POLICY_VALUES,
    PORT_BODY,
    PORT_DEFAULT,
    PORT_DONE,
    PORT_ELSE,
    PORT_ERROR,
    TERMINAL_NODE_TYPES,
    TRIGGER_KIND_VALUES,
    TRIGGER_SCHEDULE,
)
from app.services.agent_recursive_dataframes import filter_algebra
from app.services.integrations.engine import transform
from app.services.integrations.connectors.spec import FieldSpec
from app.services.integrations.errors import FlowValidationError
from app.services.integrations.mapping import field_map

# The value kinds a `validate` rule may require. The same seven
# ``app/utils/type_coercion.py`` coerces to, imported rather than restated because the
# rule and the coercion are the same question asked twice — a second list would answer
# it differently the first time somebody adds a type to one of them.
from app.utils.type_coercion import TYPES as _KNOWN_TYPES

# ---------------------------------------------------------------------------
# What is actually runnable
# ---------------------------------------------------------------------------
# The node types this phase has a runner for. ``models.NODE_TYPES`` is the whole
# vocabulary including the Phase 2 and Phase 3 entries; a type named there but absent
# here is refused by `validate_flow` and omitted from the palette, which is what lets
# the vocabulary be complete without the canvas offering something that would fail at
# three in the morning.
#
# ``node_runners`` asserts at import that it registers exactly this set. That assertion,
# rather than an import from there, is what keeps the dependency running one way: the
# compiler and the runners import these rules, and the rules import nothing that runs.
IMPLEMENTED_NODE_TYPES = frozenset(
    {
        NODE_TRIGGER,
        NODE_CONNECTOR_READ,
        NODE_CONNECTOR_WRITE,
        NODE_TRANSFORM,
        NODE_VALIDATE,
        NODE_FILTER,
        NODE_BRANCH,
        NODE_BATCH,
        NODE_EMAIL,
        NODE_SUCCESS,
        NODE_FAILURE,
    }
)

# A `batch` node will not loop more times than this, whatever it is set to. The bound is
# on *work* rather than on the size of the drawing, which is why there is no cap on node
# or edge count anywhere in this module: a ten-node workflow can read a million records
# and a forty-node one can read none.
DEFAULT_MAX_BATCHES = 1000
MAX_MAX_BATCHES = 100_000


# ---------------------------------------------------------------------------
# Palette presentation
# ---------------------------------------------------------------------------
# The one sentence under each palette entry, and the order the entries appear in.
#
# Here rather than in ``models.py`` because these two answer a different question than
# ``NODE_TYPE_LABELS`` does. The labels are the vocabulary the validator refuses things
# by name in; these are how the palette *reads*, and only the implemented types have one
# — a Phase 3 type nobody can place needs no sales pitch. This module already serves the
# palette, so it is also the module that owns how the palette looks.
#
# In JavaScript would be worse for the reason the module docstring gives: a description
# there is a second place to edit when a node type changes, and the one that gets missed.
NODE_DESCRIPTIONS: Dict[str, str] = {
    NODE_TRIGGER: "Starts the workflow, on a schedule or when somebody presses Run.",
    NODE_CONNECTOR_READ: "Fetches records from a connected app.",
    NODE_CONNECTOR_WRITE: "Sends records to a connected app.",
    NODE_TRANSFORM: "Renames and reshapes fields so they match the destination.",
    NODE_VALIDATE: "Checks each record and routes the bad ones somewhere else.",
    NODE_FILTER: "Keeps the records that match and drops the rest.",
    NODE_BRANCH: "Sends records down different paths depending on what they hold.",
    NODE_BATCH: "Works through records a few at a time instead of all at once.",
    NODE_EMAIL: "Sends an email — a summary of the run, or one per record.",
    NODE_SUCCESS: "Ends the workflow, reporting success.",
    NODE_FAILURE: "Ends the workflow, reporting failure.",
}

# Read, write, shape, check, route, loop, tell somebody, finish. The order somebody
# builds a workflow in, which is the order both other canvases use — `flow_builder.js`
# by its object's key order and `graph_designer` by ``models.NODE_TYPES``' tuple order.
# Alphabetical by slug, which this replaces, put `batch` and `branch` first and buried
# the two entries every workflow starts with in the middle of the list.
#
# `trigger` is here for completeness even though the palette skips it: a workflow has
# exactly one and it arrives with the drawing, so offering a second would be offering a
# save that cannot succeed.
PALETTE_ORDER: Tuple[str, ...] = (
    NODE_TRIGGER,
    NODE_CONNECTOR_READ,
    NODE_CONNECTOR_WRITE,
    NODE_TRANSFORM,
    NODE_VALIDATE,
    NODE_FILTER,
    NODE_BRANCH,
    NODE_BATCH,
    NODE_EMAIL,
    NODE_SUCCESS,
    NODE_FAILURE,
)

# The same shape of guarantee ``node_runners`` gives at its own import: a node type added
# to `IMPLEMENTED_NODE_TYPES` without a description or a place in the order is a palette
# button with no subtitle, sorted last by accident. Caught here, at import, rather than
# noticed by whoever opens the canvas next.
assert set(PALETTE_ORDER) == set(IMPLEMENTED_NODE_TYPES), (
    "flow_rules.PALETTE_ORDER and IMPLEMENTED_NODE_TYPES disagree: "
    f"{set(PALETTE_ORDER) ^ set(IMPLEMENTED_NODE_TYPES)}"
)
assert set(NODE_DESCRIPTIONS) == set(IMPLEMENTED_NODE_TYPES), (
    "flow_rules.NODE_DESCRIPTIONS and IMPLEMENTED_NODE_TYPES disagree: "
    f"{set(NODE_DESCRIPTIONS) ^ set(IMPLEMENTED_NODE_TYPES)}"
)


# ---------------------------------------------------------------------------
# Reading a drawing
# ---------------------------------------------------------------------------


def nodes_of(graph_data: Any) -> List[dict]:
    return [n for n in (graph_data or {}).get("nodes") or [] if isinstance(n, dict)]


def edges_of(graph_data: Any) -> List[dict]:
    return [e for e in (graph_data or {}).get("edges") or [] if isinstance(e, dict)]


def node_id_of(node: Mapping[str, Any]) -> str:
    return str(node.get("id") or "").strip()


def node_type_of(node: Mapping[str, Any]) -> str:
    return str(node.get("type") or "").strip()


def data_of(node: Mapping[str, Any]) -> dict:
    data = node.get("data")
    return data if isinstance(data, dict) else {}


def label_of(node: Mapping[str, Any]) -> str:
    """
    What to call this node in a message.

    The author's own label if they set one, the node type's name if not, and the id as a
    last resort — because a sentence saying "the node called ''" helps nobody.
    """
    label = str(data_of(node).get("label") or "").strip()
    if label:
        return label
    return NODE_TYPE_LABELS.get(node_type_of(node)) or node_id_of(node) or "a node"


def source_port_of(edge: Mapping[str, Any]) -> str:
    return str(edge.get("source_port") or PORT_DEFAULT).strip() or PORT_DEFAULT


def ports_of(node: Mapping[str, Any]) -> Tuple[str, ...]:
    """
    The exits this node offers.

    Static for every type but ``branch``, whose ports are authored — one per condition
    the user wrote, plus ``else``. That is why it is absent from ``NODE_PORTS``: a
    static list for it would be a lie, and deriving it here is the one place that knows
    the node's own data.
    """
    node_type = node_type_of(node)

    if node_type == NODE_BRANCH:
        ports = [
            str(condition.get("port") or "").strip()
            for condition in _conditions_of(node)
            if isinstance(condition, dict)
        ]
        return tuple([p for p in ports if p] + [PORT_ELSE, PORT_ERROR])

    return NODE_PORTS.get(node_type, ())


def _conditions_of(node: Mapping[str, Any]) -> List[Any]:
    conditions = data_of(node).get("conditions")
    return conditions if isinstance(conditions, list) else []


# ---------------------------------------------------------------------------
# The vocabulary, as served
# ---------------------------------------------------------------------------


def node_specs() -> List[Dict[str, Any]]:
    """
    Every node type the palette may offer, with its exits and its defaults.

    Only the implemented ones. A type in the model's vocabulary with no runner is not
    offered, so the promise "the palette can never offer what the validator refuses"
    holds for ports as well as for types.

    Ordered by :data:`PALETTE_ORDER` rather than by name, because the palette renders
    this list in the order it arrives and the order somebody builds a workflow in is not
    alphabetical.
    """
    specs: List[Dict[str, Any]] = []

    for node_type, label in [
        (t, NODE_TYPE_LABELS[t]) for t in NODE_TYPE_VALUES if t in IMPLEMENTED_NODE_TYPES
    ]:
        specs.append(
            {
                "type": node_type,
                "label": label,
                # The palette's subtitle, and what a step with no settings of its own
                # falls back to on the canvas. "Write to" on its own tells nobody what
                # the step does.
                "description": NODE_DESCRIPTIONS.get(node_type, ""),
                # `branch` reports its fixed exits; the canvas adds one per condition as
                # the user writes them, using the same `else`/`error` tail.
                "ports": list(NODE_PORTS.get(node_type, (PORT_ELSE, PORT_ERROR))),
                "dynamic_ports": node_type == NODE_BRANCH,
                "terminal": node_type in TERMINAL_NODE_TYPES,
                "loop": node_type in LOOP_NODE_TYPES,
                "needs_connection": node_type in CONNECTOR_NODE_TYPES,
            }
        )

    # A type absent from the order sorts last rather than raising: the import-time
    # assertion above is where that is caught, and a palette missing its curation is
    # still a usable palette.
    order = {node_type: index for index, node_type in enumerate(PALETTE_ORDER)}
    return sorted(specs, key=lambda spec: (order.get(spec["type"], len(order)), spec["type"]))


def vocabulary() -> Dict[str, Any]:
    """
    The whole payload behind ``GET /integrations/vocabulary``.

    Assembled from the model constants, this module and ``filter_algebra``'s own
    operator table — never written out by hand. A canvas built from this cannot offer
    an operator the runner does not implement, which is the same guarantee
    ``describe_operators`` already gives the aggregation planner.
    """
    return {
        "nodes": node_specs(),
        "operators": sorted(filter_algebra.OPERATORS),
        "date_parts": sorted(filter_algebra.PARTS),
        "transforms": transform.describe_transforms(),
        "defaults": {
            "batch_size": DEFAULT_BATCH_SIZE,
            "min_batch_size": MIN_BATCH_SIZE,
            "max_batch_size": MAX_BATCH_SIZE,
            "max_batches": DEFAULT_MAX_BATCHES,
            "min_interval_seconds": MIN_INTERVAL_SECONDS,
        },
    }


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_flow(graph_data: Any) -> None:
    """
    Refuse a drawing that is not a runnable workflow, naming the node at fault.

    Called identically by save, publish and run. See the module docstring.
    """
    if not isinstance(graph_data, dict):
        raise FlowValidationError("This workflow could not be read.")

    nodes = graph_data.get("nodes")
    edges = graph_data.get("edges")

    if not isinstance(nodes, list) or not isinstance(edges, list):
        raise FlowValidationError(
            "A workflow needs a list of steps and a list of connections between them."
        )

    if not nodes:
        raise FlowValidationError(
            "This workflow is empty. Start by adding a trigger to say what sets it off."
        )

    by_id = _index_nodes(nodes)
    trigger = _require_one_trigger(nodes)

    for node in nodes:
        _validate_node(node)

    _validate_edges(edges, by_id)
    _refuse_edges_into_the_trigger(edges, by_id)
    _require_bounded_cycles(edges, by_id)
    _require_batch_bodies_that_return(edges, by_id)
    _require_sources_upstream(edges, by_id)
    _require_writes_reachable(trigger, edges, by_id)
    _require_writes_inside_their_body(edges, by_id)


def validate_for_publish(graph_data: Any) -> None:
    """
    Everything :func:`validate_flow` refuses, **plus** the rule a draft may break.

    A draft with an unmapped required input is a workflow somebody is halfway through
    building, and refusing to save it would make the canvas unusable. A *published* one
    is a workflow a schedule is about to run unattended, and the mapping panel's red
    warning would be decorative if publishing ignored it.
    """
    validate_flow(graph_data)

    for node in nodes_of(graph_data):
        if node_type_of(node) != NODE_CONNECTOR_WRITE:
            continue

        missing = _unmapped_required_inputs(node)
        if missing:
            raise FlowValidationError(
                f"'{label_of(node)}' has required fields with nothing mapped to them: "
                f"{', '.join(missing)}. Map them, or remove them from the operation, "
                "before publishing — a scheduled run has nobody to ask.",
                node_id=node_id_of(node),
            )


def _unmapped_required_inputs(node: Mapping[str, Any]) -> List[str]:
    """
    Required destination fields with no mapping and no constant.

    The operation's own input list is resolved at publish time by ``flow_service``,
    which stamps it onto the node as ``required_inputs``. Doing the lookup here would
    make this module need a database, and the whole value of it is that it does not.
    """
    data = data_of(node)
    required = [str(name) for name in (data.get("required_inputs") or []) if str(name)]
    if not required:
        return []

    satisfied = set()
    for mapping in data.get("mappings") or []:
        if not isinstance(mapping, dict):
            continue
        target = str(mapping.get("target") or "").strip()
        if not target:
            continue
        if mapping.get("source") or mapping.get("const") is not None or mapping.get("default") is not None:
            satisfied.add(target)

    return [name for name in required if name not in satisfied]


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------


def _index_nodes(nodes: Sequence[Any]) -> Dict[str, dict]:
    indexed: Dict[str, dict] = {}

    for node in nodes:
        if not isinstance(node, dict):
            raise FlowValidationError("One of the steps could not be read.")

        node_id = node_id_of(node)
        if not node_id:
            raise FlowValidationError("Every step needs an id, and one of them has none.")

        if node_id in indexed:
            raise FlowValidationError(
                f"Two steps share the id '{node_id}'. Every connection names a step by "
                "its id, so a duplicate makes the workflow ambiguous.",
                node_id=node_id,
            )

        indexed[node_id] = node

    return indexed


def _require_one_trigger(nodes: Sequence[Mapping[str, Any]]) -> dict:
    triggers = [node for node in nodes if node_type_of(node) == NODE_TRIGGER]

    if len(triggers) == 1:
        return triggers[0]

    if not triggers:
        raise FlowValidationError(
            "This workflow has no trigger, so nothing would ever start it. Add one to "
            "say whether it runs on a schedule, on a webhook, or by hand."
        )

    raise FlowValidationError(
        f"A workflow has exactly one trigger and this one has {len(triggers)}. Two "
        "triggers would mean two different things starting the same run, and the run "
        "could not say which.",
        node_id=node_id_of(triggers[1]),
    )


def _validate_edges(edges: Sequence[Mapping[str, Any]], by_id: Dict[str, dict]) -> None:
    seen_ports: Set[Tuple[str, str]] = set()

    for edge in edges:
        source = str(edge.get("source") or "").strip()
        target = str(edge.get("target") or "").strip()
        edge_id = str(edge.get("id") or "").strip()
        port = source_port_of(edge)

        for end, node_id in (("from", source), ("to", target)):
            if not node_id:
                raise FlowValidationError(
                    "One of the connections does not say which step it goes "
                    f"{end}.", edge_id=edge_id,
                )
            if node_id not in by_id:
                raise FlowValidationError(
                    f"A connection points {end} a step that is not in this workflow "
                    f"('{node_id}'). Delete the connection and draw it again.",
                    edge_id=edge_id,
                )

        source_node = by_id[source]

        if node_type_of(source_node) in TERMINAL_NODE_TYPES:
            raise FlowValidationError(
                f"'{label_of(source_node)}' is where the workflow ends, so nothing can "
                "come after it.",
                node_id=source,
                edge_id=edge_id,
            )

        available = ports_of(source_node)
        if port not in available:
            raise FlowValidationError(
                f"'{label_of(source_node)}' has no '{port}' exit. It offers: "
                f"{', '.join(available) or 'none'}.",
                node_id=source,
                edge_id=edge_id,
            )

        if (source, port) in seen_ports:
            raise FlowValidationError(
                f"'{label_of(source_node)}' has two connections leaving its '{port}' "
                "exit. A run follows one path, so it could not say which.",
                node_id=source,
                edge_id=edge_id,
            )
        seen_ports.add((source, port))


def _refuse_edges_into_the_trigger(
    edges: Sequence[Mapping[str, Any]], by_id: Dict[str, dict]
) -> None:
    for edge in edges:
        target = str(edge.get("target") or "").strip()
        if target in by_id and node_type_of(by_id[target]) == NODE_TRIGGER:
            raise FlowValidationError(
                "Nothing can lead back into the trigger — it is where the run begins. "
                "To repeat work, use a Batch step.",
                node_id=target,
                edge_id=str(edge.get("id") or ""),
            )


def _require_bounded_cycles(
    edges: Sequence[Mapping[str, Any]], by_id: Dict[str, dict]
) -> None:
    """
    A cycle is legal exactly when it passes through a ``batch``.

    Implemented by cutting every ``body`` edge and requiring what remains to be acyclic.
    That is the whole rule: a batch node is the only thing that counts its own passes and
    stops, so a loop that avoids one has nothing to end it, and an unattended workflow
    that never ends is a worker held forever and an API quota spent on nothing.
    """
    adjacency: Dict[str, List[str]] = {node_id: [] for node_id in by_id}

    for edge in edges:
        source = str(edge.get("source") or "").strip()
        target = str(edge.get("target") or "").strip()
        if source not in by_id or target not in by_id:
            continue
        if (
            node_type_of(by_id[source]) in LOOP_NODE_TYPES
            and source_port_of(edge) == PORT_BODY
        ):
            continue
        adjacency[source].append(target)

    cycle = _find_cycle(adjacency)
    if cycle:
        node = by_id[cycle]
        raise FlowValidationError(
            f"'{label_of(node)}' is part of a loop that has nothing to stop it. A "
            "workflow may only repeat through a Batch step, which counts its own "
            "passes and finishes.",
            node_id=cycle,
        )


def _find_cycle(adjacency: Mapping[str, Sequence[str]]) -> str:
    """The id of one node on a cycle, or ``""``. Iterative, so a wide graph cannot
    exhaust the recursion limit before the validator has said anything."""
    WHITE, GREY, BLACK = 0, 1, 2
    colour = dict.fromkeys(adjacency, WHITE)

    for root in adjacency:
        if colour[root] != WHITE:
            continue

        stack: List[Tuple[str, int]] = [(root, 0)]
        colour[root] = GREY

        while stack:
            node_id, index = stack[-1]
            neighbours = adjacency.get(node_id) or ()

            if index >= len(neighbours):
                colour[node_id] = BLACK
                stack.pop()
                continue

            stack[-1] = (node_id, index + 1)
            neighbour = neighbours[index]

            if colour.get(neighbour) == GREY:
                return neighbour
            if colour.get(neighbour) == WHITE:
                colour[neighbour] = GREY
                stack.append((neighbour, 0))

    return ""


def _require_batch_bodies_that_return(
    edges: Sequence[Mapping[str, Any]], by_id: Dict[str, dict]
) -> None:
    """
    A batch body must exist and must come back.

    Both halves are the same failure wearing different clothes: **one batch of a
    hundred, reported as success.** A body wired to nothing processes no records and
    finishes green; a body that never returns to the batch processes the first page and
    finishes green. Neither says anything is wrong, and a nightly sync that quietly moves
    the first five hundred records of fifty thousand is the worst outcome this module
    has.
    """
    for node_id, node in by_id.items():
        if node_type_of(node) not in LOOP_NODE_TYPES:
            continue

        body_targets = [
            str(edge.get("target") or "").strip()
            for edge in edges
            if str(edge.get("source") or "").strip() == node_id
            and source_port_of(edge) == PORT_BODY
        ]

        if not body_targets:
            raise FlowValidationError(
                f"'{label_of(node)}' has nothing wired to its body, so it would read "
                "the records and do nothing with them. Connect the body to the first "
                "step that should run for each batch.",
                node_id=node_id,
            )

        if not _reaches(body_targets[0], node_id, edges, by_id):
            raise FlowValidationError(
                f"The body of '{label_of(node)}' never comes back to it, so only the "
                "first batch would be processed and the run would still report "
                "success. Connect the last step of the body back to this one.",
                node_id=node_id,
            )


def _reaches(
    start: str,
    goal: str,
    edges: Sequence[Mapping[str, Any]],
    by_id: Mapping[str, dict],
) -> bool:
    return goal in _reachable_from(start, edges, by_id)


def _reachable_from(
    start: str,
    edges: Sequence[Mapping[str, Any]],
    by_id: Mapping[str, dict],
    *,
    skip_port: Optional[Tuple[str, str]] = None,
) -> Set[str]:
    """
    Every node reachable from ``start``, following edges forwards.

    ``skip_port`` removes one ``(node_id, port)`` exit from the walk — used to ask "what
    is inside this batch's body" by walking from the body and refusing to leave through
    ``done``.
    """
    outgoing: Dict[str, List[str]] = {}
    for edge in edges:
        source = str(edge.get("source") or "").strip()
        target = str(edge.get("target") or "").strip()
        if source not in by_id or target not in by_id:
            continue
        if skip_port is not None and (source, source_port_of(edge)) == skip_port:
            continue
        outgoing.setdefault(source, []).append(target)

    seen: Set[str] = set()
    stack = [start]
    while stack:
        node_id = stack.pop()
        if node_id in seen or node_id not in by_id:
            continue
        seen.add(node_id)
        stack.extend(outgoing.get(node_id, ()))

    return seen


def _ancestors_of(
    node_id: str, edges: Sequence[Mapping[str, Any]], by_id: Mapping[str, dict]
) -> Set[str]:
    """Every node from which ``node_id`` can be reached, following edges backwards."""
    incoming: Dict[str, List[str]] = {}
    for edge in edges:
        source = str(edge.get("source") or "").strip()
        target = str(edge.get("target") or "").strip()
        if source in by_id and target in by_id:
            incoming.setdefault(target, []).append(source)

    seen: Set[str] = set()
    stack = list(incoming.get(node_id, ()))
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack.extend(incoming.get(current, ()))

    return seen


def _require_sources_upstream(
    edges: Sequence[Mapping[str, Any]], by_id: Dict[str, dict]
) -> None:
    """
    A node reading another node's records must be downstream of it.

    ``source_node`` is a data reference rather than a drawn edge — a write node reads
    the transform's output, which may be three steps back. But if the source cannot
    reach this node along the drawn path, then at the moment this node runs the source
    has not run, and it would read either nothing or the *previous* pass's records. The
    second is worse: it produces a plausible result that is one batch stale.
    """
    for node_id, node in by_id.items():
        source_id = str(data_of(node).get("source_node") or "").strip()
        if not source_id:
            continue

        if source_id not in by_id:
            raise FlowValidationError(
                f"'{label_of(node)}' reads from a step that is not in this workflow. "
                "Choose which step its records come from.",
                node_id=node_id,
            )

        if source_id == node_id:
            raise FlowValidationError(
                f"'{label_of(node)}' is set to read from itself.", node_id=node_id,
            )

        if source_id not in _ancestors_of(node_id, edges, by_id):
            raise FlowValidationError(
                f"'{label_of(node)}' reads from '{label_of(by_id[source_id])}', but "
                "there is no path from that step to this one — so it would not have run "
                "yet. Connect them, or pick a step that comes earlier.",
                node_id=node_id,
            )


def _require_writes_reachable(
    trigger: Mapping[str, Any],
    edges: Sequence[Mapping[str, Any]],
    by_id: Dict[str, dict],
) -> None:
    """
    Every write must be reachable from the trigger.

    A write node stranded off to one side is almost always half-finished work rather
    than an intention, and publishing it produces a workflow whose author believes it
    writes somewhere it never reaches. Only writes are checked: an orphaned read or
    transform costs nothing and is a normal state to leave a canvas in overnight.
    """
    reachable = _reachable_from(node_id_of(trigger), edges, by_id)

    for node_id, node in by_id.items():
        if node_type_of(node) == NODE_CONNECTOR_WRITE and node_id not in reachable:
            raise FlowValidationError(
                f"Nothing leads to '{label_of(node)}', so it would never run. Connect "
                "it to the workflow or remove it.",
                node_id=node_id,
            )


def _require_writes_inside_their_body(
    edges: Sequence[Mapping[str, Any]], by_id: Dict[str, dict]
) -> None:
    """
    A write inside a batch body may not read from outside it.

    Its source would hold the same records on every pass, so a hundred-pass loop would
    write the first batch a hundred times. The counters would say 50,000 written and the
    destination would hold 500 records repeated — a discrepancy nobody discovers until a
    customer asks why they got the same order confirmation a hundred times.
    """
    for batch_id, batch in by_id.items():
        if node_type_of(batch) not in LOOP_NODE_TYPES:
            continue

        body_start = next(
            (
                str(edge.get("target") or "").strip()
                for edge in edges
                if str(edge.get("source") or "").strip() == batch_id
                and source_port_of(edge) == PORT_BODY
            ),
            "",
        )
        if not body_start:
            continue  # already refused by _require_batch_bodies_that_return

        body = _reachable_from(
            body_start, edges, by_id, skip_port=(batch_id, PORT_DONE)
        )
        body.discard(batch_id)

        for node_id in body:
            node = by_id[node_id]
            if node_type_of(node) != NODE_CONNECTOR_WRITE:
                continue

            source_id = str(data_of(node).get("source_node") or "").strip()
            if source_id and source_id not in body and source_id != batch_id:
                raise FlowValidationError(
                    f"'{label_of(node)}' runs inside the body of "
                    f"'{label_of(batch)}' but reads from "
                    f"'{label_of(by_id[source_id])}', which is outside it — so it "
                    "would write the same records on every pass. Read from a step "
                    "inside the batch instead.",
                    node_id=node_id,
                )


# ---------------------------------------------------------------------------
# One node at a time
# ---------------------------------------------------------------------------


def _validate_node(node: Mapping[str, Any]) -> None:
    node_type = node_type_of(node)
    node_id = node_id_of(node)

    if node_type not in NODE_TYPE_VALUES:
        raise FlowValidationError(
            f"'{node_type or 'a step'}' is not a kind of step this workflow "
            f"understands. Available: {', '.join(sorted(IMPLEMENTED_NODE_TYPES))}.",
            node_id=node_id,
        )

    if node_type not in IMPLEMENTED_NODE_TYPES:
        raise FlowValidationError(
            f"'{NODE_TYPE_LABELS[node_type]}' steps are not available yet. Remove this "
            "one to save the workflow.",
            node_id=node_id,
        )

    validator = _NODE_VALIDATORS.get(node_type)
    if validator is not None:
        validator(node)


def _validate_trigger(node: Mapping[str, Any]) -> None:
    data = data_of(node)
    node_id = node_id_of(node)
    kind = str(data.get("kind") or "").strip()

    if kind and kind not in TRIGGER_KIND_VALUES:
        raise FlowValidationError(
            f"'{kind}' is not a way of starting a workflow. Available: "
            f"{', '.join(sorted(TRIGGER_KIND_VALUES))}.",
            node_id=node_id,
        )

    policy = str(data.get("overlap_policy") or "").strip()
    if policy and policy not in OVERLAP_POLICY_VALUES:
        raise FlowValidationError(
            f"'{policy}' is not a way of handling a run that is still going when the "
            f"next one is due. Available: {', '.join(sorted(OVERLAP_POLICY_VALUES))}.",
            node_id=node_id,
        )

    if kind != TRIGGER_SCHEDULE:
        return

    if data.get("catch_up"):
        # Firing twelve missed hourly slots costs twelve times the API quota for zero
        # extra data, because an incremental sync's single catch-up run reads
        # everything those twelve would have. Refused rather than defaulted, so nobody
        # switches it on and finds out at the end of the month.
        raise FlowValidationError(
            "Catching up on missed runs is not available. A workflow that has fallen "
            "behind runs once and moves on — the next run reads everything the missed "
            "ones would have.",
            node_id=node_id,
        )

    if data.get("cron_expression"):
        raise FlowValidationError(
            "Cron schedules are not available yet. Set an interval in seconds instead.",
            node_id=node_id,
        )

    interval = data.get("interval_seconds")
    if interval is None:
        raise FlowValidationError(
            "This trigger runs on a schedule but does not say how often. Set an "
            f"interval of at least {MIN_INTERVAL_SECONDS} seconds.",
            node_id=node_id,
        )

    if not isinstance(interval, int) or isinstance(interval, bool):
        raise FlowValidationError(
            "How often this runs must be a whole number of seconds.", node_id=node_id,
        )

    if interval < MIN_INTERVAL_SECONDS:
        raise FlowValidationError(
            f"A workflow cannot run more often than every {MIN_INTERVAL_SECONDS} "
            "seconds. Every system this connects to limits how fast it may be called, "
            "and polling faster spends that allowance without finding anything new.",
            node_id=node_id,
        )


def _validate_connector_node(node: Mapping[str, Any]) -> None:
    data = data_of(node)
    node_id = node_id_of(node)

    _validate_connection_uuid(node, data.get("connection_uuid"))

    if not str(data.get("operation_id") or "").strip():
        raise FlowValidationError(
            f"'{label_of(node)}' does not say what to do with that connection.",
            node_id=node_id,
        )

    _validate_batch_size(node, data.get("batch_size"))

    if node_type_of(node) == NODE_CONNECTOR_WRITE:
        _validate_mappings(node, data.get("mappings"))


def _validate_connection_uuid(node: Mapping[str, Any], raw: Any) -> None:
    """
    The connection a step names, checked for shape but not for existence.

    **``connection_uuid``, spelled exactly as the runner reads it.** These two rules used
    to disagree — the validator asked for ``connection_id`` while
    ``connector_nodes.resolve_target`` read ``connection_uuid`` — which meant a workflow
    could save green and fail at the first record, or save red while being perfectly
    runnable. One name, and it is the public one: a field called ``connection_id`` invites
    somebody to put the internal bigint in it, and CLAUDE.md's rule is that the bigint
    never reaches a payload at all.

    The uuid is *parsed* here rather than merely required, because the shape a
    hallucination takes is a plausible word — a model writing ``"shopify-prod"`` where an
    identifier belongs. Caught at save time, that is a red node on an open canvas; caught
    at run time it is a line in a log at 3am.

    Whether the connection **exists** is deliberately not checked. This module has no
    database, which is what makes it importable by the palette endpoint and the AI prompt
    renderer, and existence is resolved at publish and at run by the same function.
    """
    text = str(raw or "").strip()

    if not text:
        raise FlowValidationError(
            f"'{label_of(node)}' does not say which connection to use.",
            node_id=node_id_of(node),
        )

    try:
        uuid_pkg.UUID(text)
    except (ValueError, AttributeError, TypeError):
        raise FlowValidationError(
            f"'{label_of(node)}' names '{text}', which is not a connection. Open the step "
            "and choose one from the list.",
            node_id=node_id_of(node),
        )


def max_batches_of(node_data: Mapping[str, Any]) -> int:
    """
    The pass limit on a loop, clamped.

    Read by the runner as well as the validator, so a bound the canvas accepted is the
    bound the loop actually honours. Clamped rather than refused here — ``validate_flow``
    already refused an out-of-range value at save time, and a version published before
    that rule existed must still run bounded rather than not at all. Unbounded is not an
    option for something that runs unattended at three in the morning against a
    rate-limited API: the failure mode is a suspended account, not a slow run.
    """
    raw = node_data.get("max_batches")
    if raw in (None, "") or isinstance(raw, bool):
        return DEFAULT_MAX_BATCHES
    try:
        limit = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_BATCHES
    return max(1, min(limit, MAX_MAX_BATCHES))


def field_specs_of(rules: Any) -> Tuple[FieldSpec, ...]:
    """
    A ``validate`` node's rules as the :class:`FieldSpec` tuple ``record_validation``
    checks against.

    One translation, here, rather than the runner building its own: the node's rules and a
    connector operation's declared inputs are the same question — *what does this field
    have to be?* — and ``record_validation`` should not have to know which of the two it
    was handed. A rule with an unknown type has already been refused by
    ``_validate_validate_node``; an unrecognised one here defaults to ``string``, which
    checks presence and nothing else rather than failing every record.
    """
    specs = []
    for rule in rules or ():
        if not isinstance(rule, Mapping):
            continue
        name = str(rule.get("field") or "").strip()
        if not name:
            continue
        declared = str(rule.get("type") or "").strip()
        specs.append(
            FieldSpec(
                name=name,
                label=str(rule.get("label") or "").strip(),
                type=declared if declared in _KNOWN_TYPES else "string",
                required=bool(rule.get("required")),
            )
        )
    return tuple(specs)


def _validate_batch_node(node: Mapping[str, Any]) -> None:
    data = data_of(node)
    _validate_batch_size(node, data.get("batch_size"))

    max_batches = data.get("max_batches")
    if max_batches is None:
        return

    if not isinstance(max_batches, int) or isinstance(max_batches, bool):
        raise FlowValidationError(
            f"The pass limit on '{label_of(node)}' must be a whole number.",
            node_id=node_id_of(node),
        )

    if not 1 <= max_batches <= MAX_MAX_BATCHES:
        raise FlowValidationError(
            f"The pass limit on '{label_of(node)}' must be between 1 and "
            f"{MAX_MAX_BATCHES}.",
            node_id=node_id_of(node),
        )


def _validate_batch_size(node: Mapping[str, Any], batch_size: Any) -> None:
    if batch_size is None:
        return

    node_id = node_id_of(node)

    if not isinstance(batch_size, int) or isinstance(batch_size, bool):
        raise FlowValidationError(
            f"The batch size on '{label_of(node)}' must be a whole number.",
            node_id=node_id,
        )

    if not MIN_BATCH_SIZE <= batch_size <= MAX_BATCH_SIZE:
        # Enforced here rather than clamped, because a batch is held in process memory:
        # this is a bound on how much of somebody's data one worker holds at once, and
        # silently reducing it would make a workflow behave differently from what its
        # author wrote.
        raise FlowValidationError(
            f"The batch size on '{label_of(node)}' must be between {MIN_BATCH_SIZE} and "
            f"{MAX_BATCH_SIZE}. Each batch is held in memory while it is processed.",
            node_id=node_id,
        )


def _validate_transform_node(node: Mapping[str, Any]) -> None:
    _validate_mappings(node, data_of(node).get("mappings"), required=True)


def _validate_mappings(
    node: Mapping[str, Any], mappings: Any, *, required: bool = False
) -> None:
    """
    The mappings, checked by **loading them exactly as the runner loads them**.

    ``field_map.load_mappings`` is called rather than re-implemented, for the reason
    ``_validate_filter_node`` calls ``filter_algebra``'s own refusal functions: a mapping
    the validator accepted and the runner refused would be a fault that first appears
    mid-batch, in a step the author already saw a green tick on. That module refuses an
    unknown transform, an unreadable source path, a ``const`` sitting alongside a
    ``source``, an unknown declared type and a duplicated target — all of them here, at
    save time, each naming the field.

    What this adds is the node's label. ``field_map`` is handed a list of mappings and
    has no idea which step on the canvas produced it.
    """
    node_id = node_id_of(node)

    if mappings is None or mappings == []:
        if required:
            raise FlowValidationError(
                f"'{label_of(node)}' does not map any fields, so it would pass records "
                "through unchanged. Map at least one field or remove the step.",
                node_id=node_id,
            )
        return

    try:
        field_map.load_mappings(mappings)
    except ValueError as exc:
        raise FlowValidationError(f"'{label_of(node)}': {exc}", node_id=node_id) from exc


def _validate_filter_node(node: Mapping[str, Any]) -> None:
    """
    The conditions, checked with ``filter_algebra``'s own functions.

    Imported rather than re-implemented: the runner evaluates these with the same
    ``needs_values`` and the same operator set, so a condition the validator accepted and
    the runner refused would be a bug that only appears mid-run. It is also the operator
    vocabulary a user has already met in the aggregation panel, which is worth more than
    a bespoke one would be.
    """
    node_id = node_id_of(node)
    specs = data_of(node).get("specs")

    if not isinstance(specs, list) or not specs:
        raise FlowValidationError(
            f"'{label_of(node)}' has no conditions, so it would let every record "
            "through. Add a condition or remove the step.",
            node_id=node_id,
        )

    for index, spec in enumerate(specs, start=1):
        if not isinstance(spec, dict):
            raise FlowValidationError(
                f"Condition {index} on '{label_of(node)}' could not be read.",
                node_id=node_id,
            )

        if not str(spec.get("column") or "").strip():
            raise FlowValidationError(
                f"Condition {index} on '{label_of(node)}' does not say which field to "
                "look at.",
                node_id=node_id,
            )

        operator = str(spec.get("operator") or "").strip()

        for refusal in (
            filter_algebra.unsupported_operator(operator),
            filter_algebra.unsupported_part(str(spec.get("part") or "")),
            filter_algebra.wrong_arity(operator, filter_algebra.values_of(spec)),
        ):
            if refusal:
                raise FlowValidationError(
                    f"Condition {index} on '{label_of(node)}': {refusal}",
                    node_id=node_id,
                )


def _validate_validate_node(node: Mapping[str, Any]) -> None:
    node_id = node_id_of(node)
    rules = data_of(node).get("rules")

    if not isinstance(rules, list) or not rules:
        raise FlowValidationError(
            f"'{label_of(node)}' has no rules, so every record would count as valid. "
            "Add a rule or remove the step.",
            node_id=node_id,
        )

    for index, rule in enumerate(rules, start=1):
        if not isinstance(rule, dict) or not str(rule.get("field") or "").strip():
            raise FlowValidationError(
                f"Rule {index} on '{label_of(node)}' does not say which field it "
                "checks.",
                node_id=node_id,
            )

        expected = str(rule.get("type") or "").strip()
        if expected and expected not in _KNOWN_TYPES:
            raise FlowValidationError(
                f"Rule {index} on '{label_of(node)}' expects a '{expected}', which is "
                f"not a kind of value. Available: {', '.join(_KNOWN_TYPES)}.",
                node_id=node_id,
            )


def _validate_branch_node(node: Mapping[str, Any]) -> None:
    node_id = node_id_of(node)
    conditions = _conditions_of(node)

    if not conditions:
        raise FlowValidationError(
            f"'{label_of(node)}' has no conditions, so every record would take the same "
            "path. Add a condition or remove the step.",
            node_id=node_id,
        )

    seen_ports: Set[str] = set()

    for index, condition in enumerate(conditions, start=1):
        if not isinstance(condition, dict):
            raise FlowValidationError(
                f"Condition {index} on '{label_of(node)}' could not be read.",
                node_id=node_id,
            )

        port = str(condition.get("port") or "").strip()
        if not port:
            raise FlowValidationError(
                f"Condition {index} on '{label_of(node)}' has no name, so its exit "
                "cannot be drawn.",
                node_id=node_id,
            )

        if port in (PORT_ELSE, PORT_ERROR):
            raise FlowValidationError(
                f"Condition {index} on '{label_of(node)}' cannot be called '{port}' — "
                "that exit already means something here.",
                node_id=node_id,
            )

        if port in seen_ports:
            raise FlowValidationError(
                f"'{label_of(node)}' has two conditions called '{port}'. Each one is an "
                "exit, so their names have to differ.",
                node_id=node_id,
            )
        seen_ports.add(port)

        operator = str(condition.get("operator") or "").strip()
        for refusal in (
            filter_algebra.unsupported_operator(operator),
            filter_algebra.wrong_arity(operator, filter_algebra.values_of(condition)),
        ):
            if refusal:
                raise FlowValidationError(
                    f"Condition {index} on '{label_of(node)}': {refusal}",
                    node_id=node_id,
                )


def _validate_email_node(node: Mapping[str, Any]) -> None:
    """
    An Email step: a template, a server, somebody to send to, and legal bindings.

    Checked by asking the runner's own module what a mode allows, rather than restating it —
    ``binding_sources_for`` is the one definition, so a validator that accepted a ``record``
    binding in ``once`` mode cannot drift away from a runner that refuses it. Same reason
    ``_validate_mappings`` calls ``field_map.load_mappings`` instead of re-implementing it.

    The template's declared variables are not available here: this function is synchronous
    and offline like every validator in this module, and ``validate_flow`` runs on save,
    publish *and* run. A binding naming a variable the template no longer declares is caught
    at enqueue, with a sentence naming it.
    """
    from app.services.email_dispatch.errors import EmailFailure, RenderError
    from app.services.email_dispatch.nodes import integration_runner
    from app.services.email_dispatch import variable_sources

    node_id = node_id_of(node)
    label = label_of(node)
    data = data_of(node)

    if not str(data.get("template_id") or "").strip():
        raise FlowValidationError(
            f"'{label}' has no email template chosen.", node_id=node_id
        )
    if not str(data.get("smtp_config_id") or "").strip():
        raise FlowValidationError(
            f"'{label}' has no SMTP server chosen.", node_id=node_id
        )

    recipients = data.get("recipients") or {}
    if not isinstance(recipients, dict) or not (recipients.get("to") or []):
        raise FlowValidationError(
            f"'{label}' has nobody to email. Add at least one TO address — it may be a "
            "{{VARIABLE}}.",
            node_id=node_id,
        )

    try:
        mode = integration_runner.mode_of(data)
    except EmailFailure as exc:
        raise FlowValidationError(f"On '{label}': {exc.message}", node_id=node_id) from exc

    allowed = integration_runner.binding_sources_for(mode)
    bindings = data.get("variable_bindings") or {}
    if not isinstance(bindings, dict):
        raise FlowValidationError(
            f"The variable bindings on '{label}' could not be read.", node_id=node_id
        )

    for name, binding in bindings.items():
        shown = str(name).upper()
        if not isinstance(binding, Mapping):
            raise FlowValidationError(
                f"The binding for {{{{{shown}}}}} on '{label}' could not be read.",
                node_id=node_id,
            )
        source = str(binding.get("source") or "").strip().lower()
        if source not in allowed:
            raise FlowValidationError(
                f"{{{{{shown}}}}} on '{label}' is bound to '{source}', which is not "
                f"available when sending {'one email per record' if mode == integration_runner.MODE_PER_RECORD else 'one email for the whole batch'}.",
                node_id=node_id,
            )
        path = str(binding.get("path") or "").strip()
        if path:
            try:
                variable_sources.assert_path(path, name=shown)
            except RenderError as exc:
                raise FlowValidationError(
                    f"On '{label}': {exc.message}", node_id=node_id
                ) from exc


_NODE_VALIDATORS = {
    NODE_TRIGGER: _validate_trigger,
    NODE_CONNECTOR_READ: _validate_connector_node,
    NODE_CONNECTOR_WRITE: _validate_connector_node,
    NODE_TRANSFORM: _validate_transform_node,
    NODE_FILTER: _validate_filter_node,
    NODE_VALIDATE: _validate_validate_node,
    NODE_BRANCH: _validate_branch_node,
    NODE_BATCH: _validate_batch_node,
    NODE_EMAIL: _validate_email_node,
}
