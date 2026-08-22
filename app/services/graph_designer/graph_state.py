"""
What travels along a designed graph's edges, and how a node's contribution is merged.

Deliberately importable **without LangGraph**. The reducers and the preview caps are
the part of the runtime most worth testing and they need nothing installed;
``graph_compiler`` is the only module in this package that imports ``langgraph``. Same
split, and the same reason, as ``tool_chain_service`` / ``tool_chain_graph``.

## Why the reducers are reducers

A node returns ``{"outputs": {"<its id>": ...}}``. Without a reducer LangGraph replaces
the whole ``outputs`` mapping, so a node with two upstream nodes would see only the
second one's contribution — a graph quietly missing half its inputs. That is exactly
the bug ``tool_chain_graph._merge_values`` exists to prevent, and the fix is the same:
merge, keyed by node id.

Keyed by **node id**, not by label. Two nodes may carry the same label — nothing stops
a user naming two SQL nodes "lookup" — and a collision would silently give one node's
consumer the other node's answer.

## Why previews are capped here

``state`` is written to the checkpointer on every super-step, and a step row is written
per node pass. A graph over a two-hundred-row query would otherwise put that result set
into Postgres once per node and once per loop iteration — a log that grows faster than
the data it describes, and a checkpoint payload that grows with it.

So the full rows live in ``outputs`` only for as long as the run needs them to, and
anything *stored* goes through :func:`preview_of` first. The cap is applied at the point
of writing rather than at the point of rendering, so it is a property of the table
rather than of one renderer that has to remember.
"""

import json
import math
from typing import Annotated, Any, Dict, List, Mapping, Optional, Sequence, TypedDict

# How many rows of a node's result are kept for display. Twenty is a judgement about reading
# a log, and it is now the *only* row limit anywhere in a run: a graph's SQL nodes are
# uncapped on purpose (`node_runners._run_sql`), so a node's result is as large as its
# statement makes it and this is what keeps the log from being that size too. Changing it is
# still a one-line change and nothing depends on the value — but note that a graph called as
# an agent's tool reports from a preview, so this is also what bounds what a model reads.
PREVIEW_ROWS = 20

# How long one previewed value may be. A single text column can hold a document; a log
# row holding it makes the dock unreadable and the table large for no benefit.
PREVIEW_VALUE_CHARS = 500

# How many keys of a dictionary result are previewed. Same reasoning as the row cap.
PREVIEW_KEYS = 50

# How many entries of the `outputs` map travel in a state preview. A graph has no node
# ceiling, so a hundred-node run would otherwise write a hundred-entry state snapshot
# per node — quadratic in the size of the drawing.
PREVIEW_STATE_ENTRIES = 25


def _merge(current: Any, incoming: Any) -> Dict[str, Any]:
    """
    Accumulate what each node contributed instead of replacing the mapping.

    Written once and used by all three keyed channels, because the failure it prevents
    is identical in each: a node with two upstreams seeing one of them. ``incoming``
    wins on a key collision, which is what re-running a node inside a loop must do —
    the second pass's answer is the current one.
    """
    merged = dict(current or {})
    merged.update(dict(incoming or {}))
    return merged


class GraphState(TypedDict, total=False):
    """
    One run's state.

    ``outputs``
        What each node produced, keyed by node id. A SQL node's rows, a value node's
        parsed JSON, a tool config node's rows, a human node's answer — whatever the
        node downstream of it will read.

    ``loops``
        Per-loop bookkeeping, keyed by the loop node's id:
        ``{"items": [...], "index": int, "started": bool}``. It lives in state rather
        than in the compiled closure because the closure is built once per run and the
        cursor changes on every pass — the same reason ``agent_values`` travels in
        ``ChainState`` rather than being captured.

    ``timers``
        One stopwatch per ``timer`` node set to *start*, keyed by that node's id:
        ``{"started_at": ..., "segments": [...], "phase": ..., "carried_seconds": ...}``.
        Its own channel rather than a corner of ``outputs`` for two reasons. A *pause*
        box writing the shared record into ``outputs`` would have to write it under the
        **start** node's key — misreporting what that node produced, since a step row's
        ``output_preview`` is built from exactly that key. And the pause box needs the
        record to survive under the start node's name while each box still reports its
        own snapshot, which one map cannot do for two purposes.

        Merged like ``loops``, and last-write-wins per key is right for the same reason:
        a transition returns the whole updated record, so the newest one is the current
        one. Read as ``state.get("timers") or {}`` everywhere — a run checkpointed
        before this key existed has no such entry.

    ``answers``
        What a human supplied, keyed by the ``human`` node's id. Separate from
        ``outputs`` even though a human node also writes there, because a resumed run
        has to be able to tell "this node has already been answered" from "this node
        produced a value", and a loop containing a human node asks again on each pass.

    ``inputs``
        What the run was started with — the values a SQL node's declared parameters
        need. Replaced, not merged: it is set once when the run begins and no node
        writes to it.

    ``run_id``
        The public uuid of the run row, as a string. In state because a node runner has
        to write its step row and has no other handle on the run. A string rather than a
        ``UUID`` object because state must be JSON-serialisable for the checkpointer —
        the same note ``download_service`` makes.

    ``errors``
        A **handled** failure, keyed by the node that had it: the author drew an error
        path from that node, so the run takes it and carries on.

    ``failed_at``
        The id of the node whose failure **ends** the run — one with no error path drawn.
        Read by the router, which is what turns "this node raised" into "end the run"
        without every node having to know the graph.

    ``errors`` and ``failed_at`` are two channels rather than one flag, and that is
    load-bearing. "This node failed and we handled it" and "this run failed" are
    different facts. With a single flag, a graph that recovered from a failed node would
    still be reported as a failed run — the opposite of what drawing a recovery path
    means.

    ``failure_message``
        Why it failed, in the operator's words. Carried in state so the run row and the
        step row cannot disagree about the reason.
    """

    outputs: Annotated[Dict[str, Any], _merge]
    loops: Annotated[Dict[str, Any], _merge]
    timers: Annotated[Dict[str, Any], _merge]
    answers: Annotated[Dict[str, Any], _merge]
    errors: Annotated[Dict[str, str], _merge]
    inputs: Dict[str, Any]
    run_id: str
    failed_at: str
    failure_message: str


def initial_state(run_uuid: Any, inputs: Optional[Mapping[str, Any]] = None) -> dict:
    """
    The state a run starts from.

    Every key is present and empty rather than absent, so a runner reading
    ``state["outputs"]`` never has to guard for the first node's case.
    """
    return {
        "outputs": {},
        "loops": {},
        "timers": {},
        "answers": {},
        "errors": {},
        "inputs": dict(inputs or {}),
        "run_id": str(run_uuid),
        "failed_at": "",
        "failure_message": "",
    }


def output_of(state: Mapping[str, Any], node_id: str) -> Any:
    """
    What one node produced, or ``None`` if it has not run.

    ``None`` rather than a raised ``KeyError``: on a branch the run did not take, a
    node genuinely has no output, and asking about it is an ordinary thing for a
    condition to do.
    """
    return (state.get("outputs") or {}).get(node_id)


def preview_of(value: Any) -> Optional[dict]:
    """
    A capped, JSON-safe view of what a node produced, for a step row.

    Always a dict, never a bare list, because the column is JSONB and a consumer that
    has to handle both shapes is a consumer that will get one of them wrong. The shape
    is::

        {"kind": "rows"|"list"|"dict"|"value"|"empty",
         "count": <how many there really were>,
         "truncated": <bool>,
         "rows"|"items"|"entries"|"value": <the capped sample>}

    ``count`` is the **real** count, not the sample's length. That distinction is the
    whole reason the field exists: a dock showing twenty rows and saying "20" when
    there were two thousand is the kind of quietly wrong number
    ``documentations/DOWNLOADER_AGENTS.md`` was written about.
    """
    if value is None:
        return {"kind": "empty", "count": 0, "truncated": False}

    if isinstance(value, list):
        return _preview_list(value)

    if isinstance(value, dict):
        return _preview_dict(value)

    return {
        "kind": "value",
        "count": 1,
        "truncated": False,
        "value": _capped_scalar(value),
    }


def _preview_list(value: Sequence[Any]) -> dict:
    """A list of rows, or a list of plain values — told apart by its first entry."""
    total = len(value)
    sample = list(value[:PREVIEW_ROWS])
    truncated = total > PREVIEW_ROWS

    if sample and all(isinstance(entry, dict) for entry in sample):
        return {
            "kind": "rows",
            "count": total,
            "truncated": truncated,
            "columns": _columns_of(sample),
            "rows": [_capped_row(row) for row in sample],
        }

    return {
        "kind": "list",
        "count": total,
        "truncated": truncated,
        "items": [_capped_scalar(entry) for entry in sample],
    }


def _preview_dict(value: Mapping[str, Any]) -> dict:
    """A dictionary result, capped by key count and by value length."""
    keys = list(value.keys())

    return {
        "kind": "dict",
        "count": len(keys),
        "truncated": len(keys) > PREVIEW_KEYS,
        "entries": {
            str(key): _capped_scalar(value[key]) for key in keys[:PREVIEW_KEYS]
        },
    }


def _columns_of(rows: Sequence[Mapping[str, Any]]) -> List[str]:
    """
    The column names across the sampled rows, in first-seen order.

    Across all of them rather than off the first: a Mongo-shaped result or a union can
    have rows with different keys, and a dock that read only the first row's columns
    would silently drop a field.
    """
    columns: List[str] = []

    for row in rows:
        for key in row.keys():
            name = str(key)
            if name not in columns:
                columns.append(name)

    return columns


def _capped_row(row: Mapping[str, Any]) -> dict:
    """One row with every value capped and JSON-safe."""
    return {str(key): _capped_scalar(value) for key, value in row.items()}


def _capped_scalar(value: Any) -> Any:
    """
    One value, short enough for a log row and safe for a JSONB column.

    Anything the JSON encoder cannot take — a ``date``, a ``Decimal``, a driver's own
    type — becomes its ``str()``. That is a display artefact and is only ever used for
    display: the value that travels in ``outputs`` and reaches the next node is
    untouched.
    """
    if value is None or isinstance(value, bool) or isinstance(value, int):
        return value

    if isinstance(value, float):
        # NaN and infinities are valid Python floats and invalid JSON. Postgres would
        # reject the insert, which would turn a successful node into a failed step — the
        # log breaking the run it is only supposed to be observing.
        return value if math.isfinite(value) else str(value)

    if isinstance(value, str):
        return _capped_text(value)

    if isinstance(value, (list, tuple)):
        return _capped_text(_as_json(list(value)))

    if isinstance(value, dict):
        return _capped_text(_as_json(value))

    return _capped_text(str(value))


def _capped_text(text: str) -> str:
    """Trim to the character cap, saying so rather than ending mid-word silently."""
    if len(text) <= PREVIEW_VALUE_CHARS:
        return text

    return text[:PREVIEW_VALUE_CHARS] + f"… ({len(text)} characters)"


def _as_json(value: Any) -> str:
    """``json.dumps`` that cannot raise — a preview must never fail a node."""
    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError):
        return str(value)


def state_preview(state: Mapping[str, Any]) -> dict:
    """
    A capped snapshot of the run's state, for the dock's State tab.

    Every node's output is reduced to its *shape* — a kind and a count — rather than
    its content, with the content available per node in that node's own
    ``output_preview``. Two reasons. A state snapshot per node would otherwise repeat
    every earlier node's rows, so a ten-node run would store the first node's result
    ten times. And the question the State tab answers is "what does the run know so
    far", which is a question about shape.
    """
    outputs = dict((state.get("outputs") or {}))
    keys = list(outputs.keys())

    return {
        "outputs": {
            node_id: _shape_of(outputs[node_id])
            for node_id in keys[:PREVIEW_STATE_ENTRIES]
        },
        "outputs_truncated": len(keys) > PREVIEW_STATE_ENTRIES,
        "loops": {
            node_id: {
                "index": int((loop or {}).get("index") or 0),
                "total": len((loop or {}).get("items") or []),
            }
            for node_id, loop in (state.get("loops") or {}).items()
        },
        # Shape only, like the outputs above: whether it is still running and how long
        # it has counted. The segment list stays out — it grows with every pause and
        # is available in full on the timer node's own `output_preview`.
        "timers": {
            node_id: {
                "phase": str((timer or {}).get("phase") or ""),
                "elapsed_seconds": (timer or {}).get("elapsed_seconds"),
            }
            for node_id, timer in (state.get("timers") or {}).items()
        },
        "answers": {
            node_id: _capped_scalar(answer)
            for node_id, answer in (state.get("answers") or {}).items()
        },
        "inputs": {
            str(key): _capped_scalar(value)
            for key, value in (state.get("inputs") or {}).items()
        },
    }


def _shape_of(value: Any) -> dict:
    """What a node's output *is*, without carrying what it contains."""
    if value is None:
        return {"kind": "empty", "count": 0}

    if isinstance(value, list):
        kind = "rows" if value and all(isinstance(e, dict) for e in value) else "list"
        return {"kind": kind, "count": len(value)}

    if isinstance(value, dict):
        return {"kind": "dict", "count": len(value)}

    return {"kind": "value", "count": 1}


def rows_of(value: Any) -> List[dict]:
    """
    A node's output as rows, for a consumer that needs them.

    A list of dicts is already rows. A list of scalars becomes one single-column row
    each, so a value node holding ``[1, 2, 3]`` can feed something that expects rows
    without the author having to reshape it by hand. Anything else is one row.
    """
    if value is None:
        return []

    if isinstance(value, list):
        if all(isinstance(entry, dict) for entry in value):
            return list(value)
        return [{"value": entry} for entry in value]

    if isinstance(value, dict):
        return [dict(value)]

    return [{"value": value}]


def values_of(value: Any) -> List[Any]:
    """
    A node's output as a flat list of values — what an ``IN`` comparison takes.

    A list of scalars is already that. A list of single-column rows becomes that
    column's values, which is what makes ``SELECT id FROM …`` usable as a filter
    without an intermediate node. A list of multi-column rows is refused by returning
    nothing useful rather than guessing which column was meant — the caller reports it,
    because guessing is how a filter ends up built from the wrong column.
    """
    if value is None:
        return []

    if isinstance(value, dict):
        return list(value.values())

    if not isinstance(value, list):
        return [value]

    if all(isinstance(entry, dict) for entry in value) and value:
        columns = _columns_of(value)
        if len(columns) != 1:
            return []
        return [entry.get(columns[0]) for entry in value]

    return list(value)
