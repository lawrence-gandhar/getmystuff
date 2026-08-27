"""
app/services/canvas_layout/layout_service.py

Where the boxes go: a layered top-down layout for any node-and-edge drawing.

Two canvases in this application let somebody draw a graph — the Flow Builder's
conversation flows and the Graph Designer's pipelines — and neither of them ever placed a
block. Each dropped a new one on a fixed stagger (``x = 40 + (count % 6) * 40``), so every
readable canvas in the product was a canvas somebody had dragged into shape by hand, and
every unreadable one was a canvas they had not got round to yet. This module is what
places them.

**Why this is Python and not JavaScript.** ``tool_graph_service`` already made this exact
call and wrote down the reason: *layout is the part of a drawing that can be wrong without
looking wrong, and only the Python side of this repository has a test harness.* That is
still true — there is no JS test runner here at all; even the chatbot widget script is
asserted on by Python tests. So the arithmetic that decides a picture lives where it can
be tested, and the browser multiplies the answer by a gap and draws it. Same division of
labour ``static/js/tool_graphs.js`` states at the top of its own file.

**What this module deliberately does not know.** Node *types*. It is handed ids and edges
and gives back a layer and a column, which is why one copy serves two canvases whose node
vocabularies have nothing in common. The one piece of type knowledge either canvas needs —
which block is the Start — is passed in as ``entry_ids`` by the caller that knows.

**Columns are fractional on purpose.** A parent sitting at 1.5 is centred over children at
1 and 2, which is what makes a fan-out look drawn rather than stacked. The browser turns a
column into pixels; nothing here rounds.

**Everything is deterministic.** Every iteration is over an input-ordered list or a sorted
key, never over a dict's insertion order, and ties break on a node's position in the input.
A canvas that rearranged itself slightly on every reload would be worse than one that
never arranged itself at all — the same rule ``tool_graph_service._rows_of`` states for its
own row assignment.
"""

from __future__ import annotations

from collections import deque
from typing import Any, Dict, List, Mapping, Sequence, Set, Tuple

#: Empty columns left between two disconnected parts of one drawing. A block that has just
#: been added and not yet wired up is its own part, and it needs to read as "not attached
#: to anything" rather than as a step of the chain beside it.
COMPONENT_GAP = 1.0

#: How many times to alternate centring parents over their children and children under
#: their parents. Four is well past the point where either canvas's graphs stop moving —
#: the loop exits early when a pass changes nothing — and it is a bound rather than a
#: convergence test so a pathological drawing cannot spin here.
CENTRING_PASSES = 4


def layered_layout(
    nodes: Sequence[Mapping[str, Any]],
    edges: Sequence[Mapping[str, Any]],
    entry_ids: Sequence[str] = (),
) -> Dict[str, Any]:
    """
    Place every node on a layer and a column, top down.

    ``nodes`` need only carry an ``id``; ``edges`` a ``source`` and a ``target``. Anything
    else on either — position, type, data, ports — is ignored, so both canvases can post
    what they already hold without reshaping it.

    ``entry_ids`` are the blocks a reader starts from, which is the Start block on both
    canvases. Ids that are not in ``nodes`` are dropped, and an empty list falls back to
    "every node nothing points at" — so a drawing whose Start has been deleted still lays
    out instead of refusing.

    Returns::

        {
            "positions":  {node_id: {"layer": int, "column": float}},
            "back_edges": [index, ...],   # indices into the `edges` argument
        }

    ``back_edges`` are the edges that point *upward* — a Goto's return jump, or any cycle.
    They are excluded from the layering (otherwise there are no layers) and reported so the
    caller can draw them as returns rather than as steps, which is the only honest way to
    show a loop on a top-down canvas.
    """
    ids = _node_ids(nodes)

    if not ids:
        return {"positions": {}, "back_edges": []}

    rank = {node_id: position for position, node_id in enumerate(ids)}
    adjacency = _adjacency(ids, edges)
    entries = _entries(ids, adjacency, entry_ids)
    back_edges = _back_edges(ids, entries, adjacency)
    children_of, parents_of = _forward(ids, adjacency, back_edges)
    layers = _layers(ids, children_of, parents_of)
    columns = _columns(
        _components(ids, adjacency, entries, rank), layers, children_of, parents_of, rank,
    )

    return {
        "positions": {
            node_id: {"layer": layers[node_id], "column": columns[node_id]}
            for node_id in ids
        },
        "back_edges": sorted(back_edges),
    }


# --------------------------------------------------------------------------
# Reading the drawing
# --------------------------------------------------------------------------

def _node_ids(nodes: Sequence[Mapping[str, Any]]) -> List[str]:
    """
    Every node's id, in the order given, first occurrence winning.

    A blank id is skipped rather than raising: this module is handed a canvas somebody is
    still editing, and one malformed node should cost that node its place in the picture,
    not the whole picture. The save-time validators are where a bad graph is refused.
    """
    ids: List[str] = []
    seen: Set[str] = set()

    for node in nodes:
        node_id = str((node or {}).get("id") or "").strip()

        if node_id and node_id not in seen:
            seen.add(node_id)
            ids.append(node_id)

    return ids


def _adjacency(
    ids: Sequence[str], edges: Sequence[Mapping[str, Any]],
) -> Dict[str, List[Tuple[str, int]]]:
    """
    Source id → the targets it reaches, each with its index in the input edge list.

    The index is carried because that is how a back edge is reported: the caller's own
    array order is the one identifier every edge has, whether or not the canvas gave it an
    id.

    An edge with an end that is not a node in this drawing is dropped. A canvas mid-edit
    genuinely has those — a node deleted a moment ago, an edge posted from a stale
    client — and a layout that raised on one would blank a picture over a stray line.
    """
    known = set(ids)
    adjacency: Dict[str, List[Tuple[str, int]]] = {node_id: [] for node_id in ids}

    for index, edge in enumerate(edges):
        source = str((edge or {}).get("source") or "").strip()
        target = str((edge or {}).get("target") or "").strip()

        if source in known and target in known:
            adjacency[source].append((target, index))

    return adjacency


def _entries(
    ids: Sequence[str],
    adjacency: Mapping[str, List[Tuple[str, int]]],
    entry_ids: Sequence[str],
) -> List[str]:
    """
    Where reading starts: the callers' declared entries, else everything unpointed-at.

    Falls back rather than refusing for the reason the module docstring gives — a drawing
    with no Start block is a drawing somebody is part-way through, and it still has to
    appear on screen. A graph that is one closed loop has nothing unpointed-at either, and
    the first node in input order stands in, so the tangle is drawn instead of nothing.
    """
    known = set(ids)
    declared = [node_id for node_id in entry_ids if node_id in known]

    if declared:
        return declared

    pointed_at = {
        target for targets in adjacency.values() for target, _index in targets
    }
    roots = [node_id for node_id in ids if node_id not in pointed_at]

    return roots or [ids[0]]


# --------------------------------------------------------------------------
# Back edges
# --------------------------------------------------------------------------

def _back_edges(
    ids: Sequence[str],
    entries: Sequence[str],
    adjacency: Mapping[str, List[Tuple[str, int]]],
) -> Set[int]:
    """
    The edges that close a loop, by their index in the input.

    A depth-first walk keeping the current path: an edge into a node that is *on the path*
    is the edge that made the cycle, and taking it out is what leaves a graph that can be
    layered at all.

    Iterative rather than recursive, because the recursion depth would be the length of the
    longest chain a user has drawn and there is no bound on that but the node cap.

    Every node is walked, not only the ones the entries reach: a part of the drawing that
    hangs off nothing still has its own loops, and an unwalked cycle would send ``_layers``
    to its pass bound with numbers that mean nothing.
    """
    back: Set[int] = set()
    finished: Set[str] = set()
    on_path: Set[str] = set()

    # (node, iterator position) — the position is an index into the node's target list, so
    # a node is revisited exactly as many times as it has children and no state is
    # rebuilt.
    for start in list(entries) + list(ids):
        if start in finished:
            continue

        stack: List[Tuple[str, int]] = [(start, 0)]
        on_path.add(start)

        while stack:
            node_id, cursor = stack[-1]
            targets = adjacency.get(node_id) or []

            if cursor >= len(targets):
                stack.pop()
                on_path.discard(node_id)
                finished.add(node_id)
                continue

            stack[-1] = (node_id, cursor + 1)
            target, edge_index = targets[cursor]

            if target in on_path:
                back.add(edge_index)
            elif target not in finished:
                on_path.add(target)
                stack.append((target, 0))

    return back


def _forward(
    ids: Sequence[str],
    adjacency: Mapping[str, List[Tuple[str, int]]],
    back_edges: Set[int],
) -> Tuple[Dict[str, List[str]], Dict[str, List[str]]]:
    """
    Children and parents over the downward edges only, each list in edge order.

    Both directions are built here rather than one being derived later because the two
    passes that place a column need one each, and deriving the reverse of a dict of lists
    twice is the kind of duplication that drifts.

    A repeated edge between the same two nodes contributes once: two connectors drawn
    between one pair should not pull a node twice as hard toward its parent.
    """
    children_of: Dict[str, List[str]] = {node_id: [] for node_id in ids}
    parents_of: Dict[str, List[str]] = {node_id: [] for node_id in ids}

    for node_id in ids:
        for target, edge_index in adjacency.get(node_id) or []:
            if edge_index in back_edges or target in children_of[node_id]:
                continue

            children_of[node_id].append(target)
            parents_of[target].append(node_id)

    return children_of, parents_of


# --------------------------------------------------------------------------
# Layers
# --------------------------------------------------------------------------

def _layers(
    ids: Sequence[str],
    children_of: Mapping[str, List[str]],
    parents_of: Mapping[str, List[str]],
) -> Dict[str, int]:
    """
    Each node's row: one below its deepest parent, so every drawn edge points down.

    The *longest* path rather than the shortest, which is what puts a node that two
    branches of different lengths both reach below the end of both — on shortest paths one
    of those edges would run past a whole row of blocks on its way.

    Settled in **one pass in topological order**, not by relaxing to a fixed point.
    ``tool_graph_service._layers`` relaxes because it is bounded to twenty nodes and the
    difference is not measurable there; here it is. Taking the back edges out first leaves
    a graph with no cycles, so every node can be given its final layer the moment its last
    parent has one — where relaxing a 500-block chain costs a pass per block, and the Graph
    Designer puts no cap on how many blocks a pipeline may have.

    The tail of this function is the insurance the relaxation used to provide. A surviving
    cycle would leave nodes that never reach in-degree zero; they are given a layer in input
    order rather than left out, so a graph this module has somehow mis-read still draws.
    """
    layers = dict.fromkeys(ids, 0)
    remaining = {node_id: len(parents_of.get(node_id, [])) for node_id in ids}
    ready = deque(node_id for node_id in ids if not remaining[node_id])
    settled: Set[str] = set()

    while ready:
        node_id = ready.popleft()
        settled.add(node_id)

        for child in children_of.get(node_id, []):
            layers[child] = max(layers[child], layers[node_id] + 1)
            remaining[child] -= 1

            if not remaining[child]:
                ready.append(child)

    for node_id in ids:
        if node_id in settled:
            continue

        layers[node_id] = 1 + max(
            (layers[parent] for parent in parents_of.get(node_id, []) if parent in settled),
            default=-1,
        )
        settled.add(node_id)

    return layers


# --------------------------------------------------------------------------
# Columns
# --------------------------------------------------------------------------

def _components(
    ids: Sequence[str],
    adjacency: Mapping[str, List[Tuple[str, int]]],
    entries: Sequence[str],
    rank: Mapping[str, int],
) -> List[List[str]]:
    """
    The drawing split into its disconnected parts, the part holding the Start first.

    Connectivity is read **undirected** and **including** back edges: a block joined to the
    chain only by a Goto's return is part of that chain, and putting it in a band of its own
    would say it was stranded.

    Each part is laid out beside the last rather than below it, so a block added and not yet
    wired appears at the top of a fresh column — visibly not part of the chain, and without
    pushing the whole canvas down.
    """
    parent: Dict[str, str] = {node_id: node_id for node_id in ids}

    def find(node_id: str) -> str:
        while parent[node_id] != node_id:
            parent[node_id] = parent[parent[node_id]]
            node_id = parent[node_id]
        return node_id

    for node_id in ids:
        for target, _index in adjacency.get(node_id) or []:
            left, right = find(node_id), find(target)
            if left != right:
                parent[right] = left

    grouped: Dict[str, List[str]] = {}
    for node_id in ids:
        grouped.setdefault(find(node_id), []).append(node_id)

    entry_roots = {find(node_id) for node_id in entries}

    # The part a reader starts in comes first, and the rest follow in the order their
    # earliest node appears in the input — so adding a block never reshuffles the bands
    # that were already there.
    return [
        members for _key, members in sorted(
            grouped.items(),
            key=lambda item: (
                0 if item[0] in entry_roots else 1,
                min(rank[node_id] for node_id in item[1]),
            ),
        )
    ]


def _columns(
    components: Sequence[Sequence[str]],
    layers: Mapping[str, int],
    children_of: Mapping[str, List[str]],
    parents_of: Mapping[str, List[str]],
    rank: Mapping[str, int],
) -> Dict[str, float]:
    """Every node's column, one component at a time, left to right."""
    columns: Dict[str, float] = {}
    offset = 0.0

    for members in components:
        width = _place_component(
            members, layers, children_of, parents_of, rank, columns, offset,
        )
        offset += width + 1.0 + COMPONENT_GAP

    return columns


def _place_component(
    members: Sequence[str],
    layers: Mapping[str, int],
    children_of: Mapping[str, List[str]],
    parents_of: Mapping[str, List[str]],
    rank: Mapping[str, int],
    columns: Dict[str, float],
    offset: float,
) -> float:
    """
    Column every node of one component, starting at ``offset``. Returns its width.

    Two stages, which is the shape every readable layered drawing has:

    1. **A first pass down**, giving each node the average column of its parents and
       shoving it right if the node before it in the same row is already there. This alone
       draws a straight chain straight and a fan-out fanned.
    2. **Centring passes**, alternating "put a parent over the middle of its children" with
       "put a child under the middle of its parents". This is what closes a fan back up:
       six error branches converging on one End Flow leave that block centred under all six
       rather than under whichever one happened to reach it first.

    A node never moves past its neighbours in its own row, so no pass can make two blocks
    overlap — the bound is checked on every move rather than repaired afterwards.
    """
    rows = _rows(members, layers, children_of, parents_of, rank)

    for layer in sorted(rows):
        cursor = offset - 1.0

        for node_id in rows[layer]:
            placed = [
                columns[parent] for parent in parents_of.get(node_id, [])
                if parent in columns
            ]
            desired = sum(placed) / len(placed) if placed else cursor + 1.0
            columns[node_id] = max(desired, cursor + 1.0)
            cursor = columns[node_id]

    for _pass in range(CENTRING_PASSES):
        moved = _centre(rows, columns, children_of, offset, sorted(rows, reverse=True))
        moved = _centre(rows, columns, parents_of, offset, sorted(rows)) or moved

        if not moved:
            break

    return max(columns[node_id] for node_id in members) - offset


def _rows(
    members: Sequence[str],
    layers: Mapping[str, int],
    children_of: Mapping[str, List[str]],
    parents_of: Mapping[str, List[str]],
    rank: Mapping[str, int],
) -> Dict[int, List[str]]:
    """
    One component's nodes grouped by layer, each row in left-to-right order.

    The order within a row is a depth-first walk from the component's roots, which is what
    keeps one parent's children next to each other instead of interleaved with a sibling
    branch's. Without it the columns still avoid overlapping and the connectors still cross
    everything.
    """
    order = _traversal_order(members, children_of, parents_of, rank)
    rows: Dict[int, List[str]] = {}

    for node_id in members:
        rows.setdefault(layers[node_id], []).append(node_id)

    for layer in rows:
        rows[layer].sort(key=lambda node_id: order[node_id])

    return rows


def _traversal_order(
    members: Sequence[str],
    children_of: Mapping[str, List[str]],
    parents_of: Mapping[str, List[str]],
    rank: Mapping[str, int],
) -> Dict[str, int]:
    """
    A depth-first index per node, from this component's roots in input order.

    Children are pushed in reverse so the walk takes a node's first-drawn connector first:
    the order an operator wired the blocks up in is the order their branches read
    left-to-right, which is the one ordering they can predict.
    """
    roots = [node_id for node_id in members if not parents_of.get(node_id)]
    roots.sort(key=lambda node_id: rank[node_id])

    order: Dict[str, int] = {}
    stack = list(reversed(roots or sorted(members, key=lambda n: rank[n])[:1]))

    while stack:
        node_id = stack.pop()

        if node_id in order:
            continue

        order[node_id] = len(order)

        for child in reversed(children_of.get(node_id, [])):
            if child not in order:
                stack.append(child)

    # Only reachable through a back edge, so the forward walk above never saw it. Appended
    # in input order rather than left out: a node with no index would fail the row sort.
    for node_id in sorted(members, key=lambda n: rank[n]):
        order.setdefault(node_id, len(order))

    return order


def _centre(
    rows: Mapping[int, List[str]],
    columns: Dict[str, float],
    relatives_of: Mapping[str, List[str]],
    offset: float,
    layer_order: Sequence[int],
) -> bool:
    """
    Slide each node toward the middle of its relatives, without passing its neighbours.

    One function for both directions — ``relatives_of`` is ``children_of`` going up the
    canvas and ``parents_of`` coming down — because the move and the bounds are identical
    and only the set being averaged differs. Reports whether anything moved, so the caller
    can stop early.
    """
    moved = False

    for layer in layer_order:
        row = rows[layer]

        for position, node_id in enumerate(row):
            placed = [
                columns[other] for other in relatives_of.get(node_id, [])
                if other in columns
            ]

            if not placed:
                continue

            lower = columns[row[position - 1]] + 1.0 if position else offset
            upper = (
                columns[row[position + 1]] - 1.0
                if position + 1 < len(row) else float("inf")
            )
            target = min(max(sum(placed) / len(placed), lower), max(lower, upper))

            if abs(target - columns[node_id]) > 1e-9:
                columns[node_id] = target
                moved = True

    return moved
