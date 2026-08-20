"""
The state a run carries between nodes, and the two reducers that combine it.

LangGraph merges what each node returns into the running state using the reducer
annotated on each key. Getting those annotations right is most of what this module is,
and one of them is the single most consequential function in the engine.

**Records never travel in state.** ``outputs`` holds handles
(``record_buffer.handle()``) and small scalars, never rows. The whole state is
serialised to the checkpointer on every super-step, so a batch of 500 records in
``outputs`` is written to the checkpoint table once per pass — a hundred times over a
50,000-record sync. ``graph_designer``'s ``_run_sql`` already concedes this problem at
its own much smaller scale; here it is fatal rather than untidy. The test suite pins it
directly: ``len(json.dumps(final_state)) < 32_768`` after a 50,000-record run.

**:func:`_accumulate` must sum, and this is the bug it exists to prevent.** ``counts``
is per-node, per-metric, and a node inside a batch body is visited once per pass. With
an ordinary last-wins merge, pass 100 returning ``{"written": 500}`` *replaces* pass
99's, and a run that moved 50,000 records reports 500. It would look entirely plausible
in the dock, and nothing would contradict it. So:

    **Runners return deltas, never totals.**

That sentence is repeated in ``run_node``'s docstring and asserted in
``test_flow_state.py`` over a hundred simulated passes, because it is a contract between
two files that no type can express.

**:func:`redact` runs inside :func:`preview_of`, before anything is stored.** Graph
Designer previews the operator's own query results. This previews webhook bodies and
third-party API responses, either of which routinely carries a bearer token nobody chose
to store. Redacting at write time makes it a property of the ``output_preview`` column
rather than of whichever template happens to render it — which is the only version of
that guarantee worth having.
"""

import json
import re
from typing import Annotated, Any, Dict, Iterable, List, Optional, TypedDict

# ---------------------------------------------------------------------------
# Preview and redaction limits
# ---------------------------------------------------------------------------
# A preview exists so somebody can see the shape of what moved. Beyond a screenful it
# stops answering that question and starts being a copy of the data in a log table.
MAX_PREVIEW_ITEMS = 5
MAX_PREVIEW_CHARS = 4096
MAX_PREVIEW_DEPTH = 6
REDACTED = "***"

# Field names whose *values* never appear in a preview, a log row or an audit detail.
# A deny-list rather than an allow-list because the shape of a third-party response is
# not ours to enumerate — an allow-list would redact almost everything and make the
# feature useless, which is how redaction gets switched off.
#
# Matched against the *key*, case-insensitively, as a substring: `x-api-key`,
# `Authorization`, `refresh_token` and `stripe_secret_key` all have to be caught, and
# an exact-match list would miss every one of them.
_SENSITIVE_KEY = re.compile(
    r"authorization|token|password|secret|api[_-]?key|client[_-]?secret|"
    r"credential|cookie|session|card|cvv|ssn",
    re.IGNORECASE,
)


class FlowState(TypedDict, total=False):
    """
    Everything a run carries between super-steps.

    Split into four merged channels and a handful of scalars, because the merge
    behaviour is what differs — not the subject.

    ``outputs``
        What each node produced, keyed by node id. Handles and small values only; see
        the module docstring.

    ``batches``
        The loop bookkeeping for each ``batch`` node: which pass it is on, whether the
        source is exhausted. Separate from ``outputs`` so a batch node's own progress is
        not something a downstream node can read as data.

    ``counts``
        Per node, per metric, **summed**. See :func:`_accumulate`.

    ``errors``
        The handled failures, keyed by node id. A node with an entry here takes its
        ``error`` edge. Distinct from ``failed_at``, which is the unhandled case that
        ends the run — the difference between a failure the author drew a path for and
        one they did not.
    """

    outputs: Annotated[Dict[str, Any], lambda a, b: _merge(a, b)]
    batches: Annotated[Dict[str, Any], lambda a, b: _merge(a, b)]
    counts: Annotated[Dict[str, Dict[str, int]], lambda a, b: _accumulate(a, b)]
    errors: Annotated[Dict[str, str], lambda a, b: _merge(a, b)]

    # Set once at the start and never merged.
    inputs: Dict[str, Any]
    run_id: str
    version_hash: str
    attempt: int
    dry_run: bool

    # Set by the engine as the run progresses. The router reads all four, in the order
    # they appear here — see flow_compiler's routing precedence.
    cancelled: bool
    failed_at: str
    failure_message: str


def _merge(left: Optional[Dict[str, Any]], right: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Last write wins, per key.

    Right for ``outputs``: a node visited twice in a loop genuinely replaces its own
    previous output, because the second batch is not an addition to the first — it is
    what that node is holding now.
    """
    combined = dict(left or {})
    combined.update(right or {})
    return combined


def _accumulate(
    left: Optional[Dict[str, Dict[str, int]]],
    right: Optional[Dict[str, Dict[str, int]]],
) -> Dict[str, Dict[str, int]]:
    """
    Sum, per node, per metric.

    **The most consequential function in the engine.** See the module docstring: with a
    last-wins merge here, a fifty-thousand-record run reports whatever its final pass
    happened to write. Runners therefore return deltas.

    A non-integer value is summed as zero rather than raising. This is a merge running
    inside LangGraph's own reduction, and a ``TypeError`` here would surface as a graph
    execution error with no useful attribution — losing a counter is bad, losing the run
    to bookkeeping is worse.
    """
    combined: Dict[str, Dict[str, int]] = {
        node_id: dict(metrics) for node_id, metrics in (left or {}).items()
    }

    for node_id, metrics in (right or {}).items():
        target = combined.setdefault(node_id, {})
        for metric, delta in (metrics or {}).items():
            target[metric] = target.get(metric, 0) + _as_int(delta)

    return combined


def _as_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return 0


def initial_state(
    *,
    run_id: str,
    version_hash: str,
    inputs: Optional[Dict[str, Any]] = None,
    dry_run: bool = False,
    attempt: int = 1,
) -> FlowState:
    """
    The state a run starts from.

    Every merged channel is present and empty rather than absent. A reducer that has to
    cope with ``None`` on its left is a reducer with a branch nobody exercises, and the
    first time it runs is in production.
    """
    return FlowState(
        outputs={},
        batches={},
        counts={},
        errors={},
        inputs=dict(inputs or {}),
        run_id=run_id,
        version_hash=version_hash,
        attempt=attempt,
        dry_run=bool(dry_run),
        cancelled=False,
        failed_at="",
        failure_message="",
    )


# ---------------------------------------------------------------------------
# Totals
# ---------------------------------------------------------------------------


def total(state: Any, metric: str) -> int:
    """
    One metric summed across every node.

    ``records_written`` for the whole run is the sum of what each write node wrote, and
    computing it here rather than keeping a second running total is what keeps the two
    from disagreeing.
    """
    counts = (state or {}).get("counts") or {}
    return sum(_as_int((metrics or {}).get(metric)) for metrics in counts.values())


def totals(state: Any) -> Dict[str, int]:
    """Every metric, summed across every node. What the run row's counters are set from."""
    summed: Dict[str, int] = {}
    counts = (state or {}).get("counts") or {}
    for metrics in counts.values():
        for metric, value in (metrics or {}).items():
            summed[metric] = summed.get(metric, 0) + _as_int(value)
    return summed


def delta(node_id: str, **metrics: int) -> Dict[str, Dict[str, int]]:
    """
    Build a ``counts`` fragment for one node.

    A tiny helper with one job: reading ``delta(node_id, written=len(batch))`` at a call
    site says *delta* out loud, where ``{"counts": {node_id: {"written": ...}}}`` reads
    equally like a total. That is the whole contract this module rests on, so it is
    worth spelling.
    """
    return {node_id: {name: _as_int(value) for name, value in metrics.items()}}


# ---------------------------------------------------------------------------
# Redaction and previews
# ---------------------------------------------------------------------------


def redact(value: Any, extra_fields: Optional[Iterable[str]] = None) -> Any:
    """
    Replace the value of every sensitive-looking key, at any depth.

    ``extra_fields`` is the flow's own ``redacted_fields`` — the field names its author
    knows are sensitive in *their* data, which no general pattern could guess. Matched
    the same way as the built-in list: case-insensitive substring against the key.

    The structure is preserved. A preview that dropped the key entirely would leave
    somebody debugging a mapping unable to see that the field was sent at all, which is
    exactly the question a preview is for.
    """
    extra = tuple(str(field).lower() for field in (extra_fields or ()) if str(field).strip())
    return _redact(value, extra, 0)


def _redact(value: Any, extra: tuple, depth: int) -> Any:
    if depth > MAX_PREVIEW_DEPTH:
        return "…"

    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            if _is_sensitive(str(key), extra):
                cleaned[key] = REDACTED
            else:
                cleaned[key] = _redact(item, extra, depth + 1)
        return cleaned

    if isinstance(value, (list, tuple)):
        return [_redact(item, extra, depth + 1) for item in value]

    return value


def _is_sensitive(key: str, extra: tuple) -> bool:
    if _SENSITIVE_KEY.search(key):
        return True
    lowered = key.lower()
    return any(field in lowered for field in extra)


def preview_of(
    value: Any,
    *,
    redacted_fields: Optional[Iterable[str]] = None,
    max_items: int = MAX_PREVIEW_ITEMS,
    max_chars: int = MAX_PREVIEW_CHARS,
) -> Dict[str, Any]:
    """
    What is safe and small enough to store on a step or run row.

    Returns ``{"sample": ..., "count": n, "truncated": bool}``.

    **``count`` is the real count**, not the length of the sample. A preview that
    reported five when fifty thousand moved would be a log actively lying about the
    volume, and the number in the dock has to be the number that happened.

    Redaction runs *before* truncation, so a token cannot survive by being past the
    cutoff and a truncation cannot leave a half-redacted string behind.
    """
    cleaned = redact(value, redacted_fields)

    if isinstance(cleaned, list):
        count = len(cleaned)
        sample = cleaned[:max_items]
        truncated = count > len(sample)
    else:
        count = 1
        sample = cleaned
        truncated = False

    encoded = _safe_json(sample)
    if len(encoded) > max_chars:
        sample = _shrink(sample, max_chars)
        truncated = True

    return {"sample": sample, "count": count, "truncated": truncated}


def _shrink(sample: Any, max_chars: int) -> Any:
    """
    Drop items until the encoded form fits, then give up and describe it.

    Encoding after each drop rather than estimating: one record with a large text field
    can be bigger than fifty small ones, so an item count is not a size.
    """
    if isinstance(sample, list):
        shrunk: List[Any] = list(sample)
        while shrunk and len(_safe_json(shrunk)) > max_chars:
            shrunk.pop()
        if shrunk:
            return shrunk

    return f"(too large to preview — {len(_safe_json(sample))} characters)"


def _safe_json(value: Any) -> str:
    """
    Encode for measurement, never for storage.

    ``default=str`` because a preview may hold a ``datetime`` or a ``Decimal`` that
    arrived from a database driver, and failing to *measure* something is not a reason
    to fail the node that produced it.
    """
    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError):
        return str(value)
