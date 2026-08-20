"""
What each kind of node actually does, and the one place that wraps all of them.

``run_node`` is the single entry point the compiler calls for every node type, so the
cancel check, the step row, the timeout, the timing and the preview caps are applied
identically to all of them. A runner that opened its own step row would eventually open
it in a slightly different shape, and the run page is only readable because they cannot.

**Runners return deltas, never totals.** ``{"counts": {node_id: {"written": 50}}}`` means
*fifty more*, and ``flow_state._accumulate`` sums them across every pass. A runner
returning a running total would make a fifty-thousand record run report the size of its
last batch — the single bug the state design exists to prevent, restated here because
this is where somebody would introduce it.

**Records never travel in the state.** A node reads its input batch out of
``record_buffer`` through a handle and writes its output back the same way. LangGraph
serialises the whole state to the checkpointer on every super-step, so a batch of five
hundred records in ``outputs`` would be written a hundred times to move it three nodes.

**Every node has one slot in the buffer, overwritten each pass.** Not a key per pass: a
thousand-pass loop would hold a thousand live batches, and nothing in pass N+1 ever reads
pass N's output. The step row carries the pass number, so nothing is lost by the key not
carrying it.

**A per-node timeout, which Graph Designer does not have.** A hung HTTP call is not a hung
query — nobody is watching at three in the morning — and without one the run sits at
``running`` with a step row that never closes. The timeout becomes a ``NodeFailure``, so it
takes the drawn error path like any other failure rather than being a special case.

**Three levels of failure, kept apart.** A record failed (a counter and a row in the
record log, the run carries on); a node failed (``errors[node_id]``, the error edge, or the
run ends); the run failed. Collapsing the first into the second is how "3 of 50,000 records
had a bad email address" becomes "the sync failed".
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set

from app.models.integrations import (
    DEFAULT_BATCH_SIZE,
    NODE_BATCH,
    NODE_BRANCH,
    NODE_CONNECTOR_READ,
    NODE_CONNECTOR_WRITE,
    NODE_FAILURE,
    NODE_FILTER,
    NODE_SUCCESS,
    NODE_TRANSFORM,
    NODE_TRIGGER,
    NODE_VALIDATE,
    PORT_BODY,
    PORT_DEFAULT,
    PORT_DONE,
    PORT_DROPPED,
    PORT_ELSE,
    PORT_INVALID,
    PORT_KEPT,
    PORT_VALID,
    RECORD_INVALID,
    STEP_COLLAPSE_AFTER,
    STEP_FAILED,
    STEP_SUCCEEDED,
)
from app.services.integrations.engine import (
    batching,
    flow_rules,
    flow_state,
    record_buffer,
    record_log,
    run_store,
)
from app.services.integrations.errors import NodeFailure, RunCancelled
from app.services.integrations.mapping import (
    dedupe,
    field_map,
    record_filter,
    record_validation,
)
from app.services.integrations.nodes import connector_nodes

logger = logging.getLogger(__name__)

#: Seconds a node may take before it is failed. Long enough for a page of a slow API,
#: short enough that a hung call does not hold a run open until somebody notices.
DEFAULT_NODE_TIMEOUT_SECONDS = 300

#: The most a node may be given, however its data is edited. An hour is already far past
#: anything a single HTTP call should take; beyond it the right answer is a smaller batch.
MAX_NODE_TIMEOUT_SECONDS = 3600

#: How many requests one run may have in flight across all of its write nodes.
DEFAULT_RUN_CONCURRENCY = 8


@dataclass
class RunContext:
    """
    Everything a runner needs that is not the node or the state.

    ``user_id`` scopes every lookup a node makes, so a workflow cannot reach a connection
    its owner does not own even if a uuid was pasted in by hand or invented by a model.

    ``run_uuid`` rather than ``run_id`` keys the record buffer: the buffer's keys appear in
    log lines, and a log line naming an internal primary key is one nobody can look up.

    ``semaphore`` bounds concurrent outbound requests **per run**, not per node. A flow
    with three write nodes must not be able to triple the rate it hits a vendor by being
    drawn differently.

    ``enclosing_batch`` is the id of the ``batch`` node this node sits inside, or ``""``.
    Set by the compiler, which is the only thing that knows the drawing's nesting — a
    runner cannot work it out from the state, and it is how a node with no explicit
    ``source_node`` finds the batch it is meant to be working on.
    """

    run_id: int
    run_uuid: str
    user_id: int
    open_session: Callable[[], Any]

    nodes: Dict[str, dict] = field(default_factory=dict)
    enclosing_batch: str = ""
    dry_run: bool = False
    redacted_fields: Sequence[str] = ()
    default_batch_size: int = DEFAULT_BATCH_SIZE
    semaphore: Optional[asyncio.Semaphore] = None

    def node(self, node_id: str) -> Optional[dict]:
        return self.nodes.get(node_id) if node_id else None

    def inside(self, batch_id: str) -> "RunContext":
        """A copy scoped to a loop, for the nodes in its body."""
        clone = RunContext(**{**self.__dict__, "enclosing_batch": batch_id})
        return clone

    def gate(self) -> asyncio.Semaphore:
        if self.semaphore is None:
            self.semaphore = asyncio.Semaphore(DEFAULT_RUN_CONCURRENCY)
        return self.semaphore


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------

_RUNNERS: Dict[str, Callable] = {}


def register_runner(node_type: str, runner: Callable) -> None:
    """
    Make a node type runnable.

    The Phase 3 ``agent`` node exists in the vocabulary and is refused by ``validate_flow``
    precisely because nothing has called this for it. Registering is therefore the single
    act that turns a named node type into a usable one, which is what lets the vocabulary
    be complete without the palette offering something that would fail at run time.
    """
    _RUNNERS[node_type] = runner


def registered_types() -> Set[str]:
    return set(_RUNNERS)


def has_runner(node_type: str) -> bool:
    return node_type in _RUNNERS


# ---------------------------------------------------------------------------
# The one wrapper
# ---------------------------------------------------------------------------


async def run_node(node: dict, state: Mapping[str, Any], context: RunContext) -> dict:
    """
    Run one node, log it, and return its state update.

    A :class:`NodeFailure` is re-raised **after** the step row is closed, because the
    compiler needs it to decide where the run goes next but a run that ends on a failure
    must not be missing the row explaining why.

    The cancel check happens before the step row is opened, so a cancelled run does not
    accumulate a ``running`` row per node it declined to execute — and
    :class:`RunCancelled` is not a failure, so it passes through without one.
    """
    node_id = flow_rules.node_id_of(node)
    node_type = flow_rules.node_type_of(node)
    label = flow_rules.label_of(node)
    batch_index = _pass_of(state, context)

    if await run_store.cancel_requested(context.run_id):
        raise RunCancelled(f"The run was stopped before '{label}' ran.")

    runner = _RUNNERS.get(node_type)
    if runner is None:
        # Unreachable through the canvas — `validate_flow` refuses an unregistered type
        # before a flow can be saved, and a run validates again before compiling. Handled
        # anyway rather than raising KeyError, because a row edited by hand in psql is
        # somebody's Tuesday and should produce a readable failed step.
        raise NodeFailure(
            f"'{label}' is a kind of step this application cannot run.", node_id=node_id
        )

    step_id = await run_store.begin_step(
        context.run_id, node_id, node_type, label, batch_index=batch_index,
    )
    started = time.monotonic()

    try:
        update = await asyncio.wait_for(
            runner(node, state, context), timeout=_timeout_for(node)
        )
    except asyncio.TimeoutError as exc:
        message = (
            f"'{label}' did not finish within {_timeout_for(node)} seconds and was "
            "stopped. If this step reads a lot at once, try a smaller batch size."
        )
        await _close(step_id, STEP_FAILED, started, message, node, context, batch_index)
        # Retryable: a timeout is about how long something took, not about whether it can
        # ever work. Whether the *records* may be re-sent is a separate decision the
        # sender already made per request.
        raise NodeFailure(message, node_id=node_id, retryable=True) from exc
    except (NodeFailure, RunCancelled) as exc:
        await _close(step_id, STEP_FAILED, started, str(exc), node, context, batch_index)
        raise
    except Exception as exc:  # noqa: BLE001 — one unexpected fault, one failed step
        # Deliberately broad and deliberately not shown. A driver or client error can name
        # internal hosts and echo values; the operator gets a fixed sentence and the real
        # reason goes to the application log — the same split the rest of the codebase
        # makes between what is raised and what is logged.
        logger.exception("Node %s (%s) failed unexpectedly", node_id, node_type)
        message = f"'{label}' could not be run. The reason has been logged."
        await _close(step_id, STEP_FAILED, started, message, node, context, batch_index)
        raise NodeFailure(message, node_id=node_id) from exc

    await _close(
        step_id,
        STEP_SUCCEEDED,
        started,
        update.pop("_message", None),
        node,
        context,
        batch_index,
        records_in=update.pop("_records_in", None),
        records_out=update.pop("_records_out", None),
        operation_hash=update.pop("_operation_hash", None),
        output=update.get("outputs", {}).get(node_id),
    )
    return update


async def _close(
    step_id: Optional[int],
    status: str,
    started: float,
    message: Optional[str],
    node: dict,
    context: RunContext,
    batch_index: int,
    *,
    records_in: Optional[int] = None,
    records_out: Optional[int] = None,
    operation_hash: Optional[str] = None,
    output: Any = None,
) -> None:
    """
    Finish the step row, or fold the pass into the rollup once the node has collapsed.

    ``step_id is None`` means one of two things: the opening write failed, or this node is
    past ``STEP_COLLAPSE_AFTER`` passes. Only the second is a collapse, and ``batch_index``
    is what tells them apart — which is why the threshold lives in ``begin_step`` and is
    read the same way here rather than being remembered between the two.

    A collapsed pass still gets its records counted. That is the whole reason the rollup
    accumulates rather than merely counting: a hundred-thousand record backfill's totals
    must not depend on how many of its passes happened to fit under the row cap.
    """
    elapsed = int((time.monotonic() - started) * 1000)
    preview = flow_state.preview_of(output, redacted_fields=context.redacted_fields)

    if step_id is not None:
        await run_store.finish_step(
            step_id, status,
            duration_ms=elapsed, message=message,
            records_in=records_in, records_out=records_out,
            output_preview=preview if isinstance(preview, dict) else {"value": preview},
            operation_hash=operation_hash,
        )
        return

    if batch_index < STEP_COLLAPSE_AFTER:
        # The opening write failed. It has already been logged by `begin_step`; inventing
        # a rollup row here would claim a pass count that is not what happened.
        return

    await run_store.roll_up_step(
        context.run_id,
        flow_rules.node_id_of(node),
        flow_rules.node_type_of(node),
        flow_rules.label_of(node),
        status=status,
        records_in=int(records_in or 0),
        records_out=int(records_out or 0),
    )


def _timeout_for(node: dict) -> int:
    """The node's timeout, clamped. See ``MAX_NODE_TIMEOUT_SECONDS``."""
    raw = flow_rules.data_of(node).get("timeout_seconds")
    try:
        seconds = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_NODE_TIMEOUT_SECONDS
    return max(1, min(seconds, MAX_NODE_TIMEOUT_SECONDS))


def _pass_of(state: Mapping[str, Any], context: RunContext) -> int:
    """Which pass of the enclosing loop this is. Zero for a node that runs once."""
    if not context.enclosing_batch:
        return 0
    counts = (state or {}).get("counts") or {}
    return int((counts.get(context.enclosing_batch) or {}).get("passes", 0))


# ---------------------------------------------------------------------------
# Reading and writing the batch a node works on
# ---------------------------------------------------------------------------


def _slot(context: RunContext, node_id: str) -> str:
    """This node's one buffer key. Overwritten each pass — see the module docstring."""
    return record_buffer.batch_key(context.run_uuid, node_id, 0)


def _emit(context: RunContext, node_id: str, records: List[Any]) -> Dict[str, Any]:
    """Stash a node's output and return the handle that travels in the state."""
    return record_buffer.put(_slot(context, node_id), records)


def _emit_ports(context: RunContext, node_id: str, **by_port: List[Any]) -> Dict[str, Any]:
    """
    A node with two record outputs — ``validate`` and ``filter``.

    Both ports always get a handle, even for an empty half. A downstream node reading a
    port that has no handle would be an error, and "nothing matched" is not an error.
    """
    handles = {}
    for port, records in by_port.items():
        key = f"{_slot(context, node_id)}:{port}"
        handles[port] = record_buffer.put(key, records)
    return {"kind": "ports", "ports": handles}


def _records_for(node: dict, state: Mapping[str, Any], context: RunContext) -> List[Any]:
    """
    The batch this node is working on.

    Explicit ``source_node`` first, then the enclosing ``batch``. That order is what lets
    the common case — a chain of steps inside a loop body — need no wiring at all, while a
    step that reads from somewhere earlier can say so.
    """
    data = flow_rules.data_of(node)
    source = str(data.get("source_node") or "").strip() or context.enclosing_batch
    port = str(data.get("source_port") or "").strip()

    if not source:
        raise NodeFailure(
            f"'{flow_rules.label_of(node)}' does not say which step's records it works "
            "on, and it is not inside a loop.",
            node_id=flow_rules.node_id_of(node),
        )

    found = ((state or {}).get("outputs") or {}).get(source)
    if found is None:
        raise NodeFailure(
            f"'{flow_rules.label_of(node)}' reads from a step that has not produced "
            "anything yet.",
            node_id=flow_rules.node_id_of(node),
        )

    if isinstance(found, dict) and found.get("kind") == "ports":
        ports = found.get("ports") or {}
        found = ports.get(port) if port else next(iter(ports.values()), None)

    if not record_buffer.is_handle(found):
        raise NodeFailure(
            f"'{flow_rules.label_of(node)}' reads from a step that does not produce "
            "records.",
            node_id=flow_rules.node_id_of(node),
        )

    return record_buffer.peek(found["key"])


# ---------------------------------------------------------------------------
# The runners
# ---------------------------------------------------------------------------


async def _run_trigger(node: dict, state: Mapping[str, Any], context: RunContext) -> dict:
    """
    Seed the run with whatever started it.

    A node with nothing to do that still gets a step row, because "what fired this and
    with what" is the first question anybody asks of a run that did something unexpected.
    """
    node_id = flow_rules.node_id_of(node)
    inputs = dict((state or {}).get("inputs") or {})
    records = inputs.pop("records", None)

    outputs: Dict[str, Any] = {}
    if isinstance(records, list):
        # A webhook body or a manual run carrying its own payload. Stashed like any other
        # recordset so a `batch` node can loop over it without a second code path.
        outputs[node_id] = _emit(context, node_id, records)
        # **Popped, not copied.** `inputs` is a plain state field, so what this returns
        # replaces it wholesale — and a payload left in there is checkpointed on every
        # super-step for the rest of the run. A webhook body of fifty thousand records
        # would be written a hundred times to move it three nodes, which is the exact
        # failure the handle design exists to prevent, arriving through the one channel
        # that does not go through the buffer.
        inputs["records_received"] = len(records)
    else:
        outputs[node_id] = {"inputs": inputs}

    return {
        "outputs": outputs,
        "inputs": inputs,
        "_message": f"Started by {inputs.get('trigger_kind') or 'a manual run'}.",
        "_records_out": len(records) if isinstance(records, list) else 0,
    }


async def _run_connector_read(
    node: dict, state: Mapping[str, Any], context: RunContext
) -> dict:
    """
    Open a paged supply and hand back a handle. **Nothing is requested here.**

    The first page goes out when the ``batch`` node asks for its first batch, which is why
    this node cannot time out on a slow API and why a run cancelled early has fetched one
    page rather than all of them.
    """
    node_id = flow_rules.node_id_of(node)
    data = flow_rules.data_of(node)

    async with context.open_session() as db:
        target = await connector_nodes.resolve_target(db, data, user_id=context.user_id)

    supply = connector_nodes.open_supply(target, dict(data.get("arguments") or {}))
    # `stash`, not `put`: a supply has no length until it has read something, and making
    # this node find out would mean issuing the request it exists to defer.
    key = record_buffer.stash(f"{context.run_uuid}:{node_id}:supply", supply)

    return {
        "outputs": {node_id: {"kind": "supply", "key": key}},
        "_message": f"Reading {target.operation_label} from {target.connection.label}.",
        "_operation_hash": target.fingerprint,
    }


async def _run_batch(node: dict, state: Mapping[str, Any], context: RunContext) -> dict:
    """
    One pass of the loop: pull the next batch, or say there is none.

    The ``batch`` node is the only place a pass is counted, and ``passes`` is the counter
    the router reads to decide ``body`` or ``done``. Both the router and this runner go
    through :func:`batch_continues`, so the log and the route cannot disagree about
    whether the loop went round again.
    """
    node_id = flow_rules.node_id_of(node)
    data = flow_rules.data_of(node)
    size = batching.batch_size_for(data, context.default_batch_size)
    passes = _passes_of(state, node_id)
    max_batches = flow_rules.max_batches_of(data)

    supply = _supply_for(node, state, context)

    if batching.loop_exhausted(passes, max_batches):
        return {
            "outputs": {node_id: {"kind": "exhausted", "reason": "limit"}},
            "counts": flow_state.delta(node_id, bounded=1),
            "_message": batching.loop_bound_message(flow_rules.label_of(node), passes),
        }

    records = await supply.next_batch(size)

    if not records:
        return {
            "outputs": {node_id: {"kind": "exhausted", "reason": "source"}},
            "_message": (
                supply.stopped_because or "there were no more records"
            ).capitalize() + ".",
        }

    return {
        "outputs": {node_id: _emit(context, node_id, records)},
        "counts": flow_state.delta(node_id, passes=1, read=len(records)),
        "_message": f"Pass {passes + 1}: {len(records):,} records.",
        "_records_out": len(records),
    }


def _passes_of(state: Mapping[str, Any], node_id: str) -> int:
    counts = (state or {}).get("counts") or {}
    return int((counts.get(node_id) or {}).get("passes", 0))


def _supply_for(
    node: dict, state: Mapping[str, Any], context: RunContext
) -> batching.RecordSupply:
    """
    The source a loop pulls from, whether it is a paged read or a list in hand.

    **The supply is created once and kept**, under the batch node's own key. A supply is
    a cursor: it remembers what it has already handed out. Rebuilding one per pass from
    the same handle resets that cursor, so the loop hands out the same first batch
    forever and only stops when it hits ``max_batches`` — a thousand passes over five
    records, which is what the bound is for and not something the bound should have to
    catch. Against a real API that is a thousand identical requests at whatever the rate
    limit allows, unattended.

    A ``connector_read``'s supply is already a live object stashed by that node, so it is
    used as it stands. A plain recordset — from a trigger's payload, or an earlier step —
    is wrapped once here.
    """
    node_id = flow_rules.node_id_of(node)
    own_key = f"{context.run_uuid}:{node_id}:supply"

    if record_buffer.has(own_key):
        return record_buffer.peek(own_key)

    data = flow_rules.data_of(node)
    source = str(data.get("source_node") or "").strip()
    found = ((state or {}).get("outputs") or {}).get(source)

    if isinstance(found, dict) and found.get("kind") == "supply":
        return record_buffer.peek(found["key"])

    if record_buffer.is_handle(found):
        supply = batching.supply_from(record_buffer.peek(found["key"]))
        record_buffer.stash(own_key, supply)
        return supply

    raise NodeFailure(
        f"'{flow_rules.label_of(node)}' is not connected to a step that produces "
        "records to loop over.",
        node_id=flow_rules.node_id_of(node),
    )


async def _run_transform(
    node: dict, state: Mapping[str, Any], context: RunContext
) -> dict:
    """
    Map every record in the batch into the destination's shape.

    A record whose mapping failed is **counted and logged, not raised**. Three bad email
    addresses in fifty thousand records is a fact about the data, and failing the node
    would turn it into a failed sync.
    """
    node_id = flow_rules.node_id_of(node)
    data = flow_rules.data_of(node)
    records = _records_for(node, state, context)

    try:
        mappings = field_map.load_mappings(data.get("mappings"))
    except ValueError as exc:
        # The mapping list itself is wrong, not one record. That is the author's mistake
        # and every record would fail the same way, so it fails the node.
        raise NodeFailure(str(exc), node_id=node_id) from exc

    mapped: List[Any] = []
    entries: List[dict] = []

    for position, outcome in enumerate(field_map.apply_to_batch(mappings, records)):
        if outcome.ok:
            mapped.append(outcome.record)
            continue
        entries.append(
            record_log.entry(
                node_id=node_id,
                outcome=RECORD_INVALID,
                batch_index=_pass_of(state, context),
                message=outcome.message(),
                source_key=str(position),
                payload=outcome.record,
            )
        )

    await record_log.write(context.run_id, entries, redacted_fields=context.redacted_fields)

    return {
        "outputs": {node_id: _emit(context, node_id, mapped)},
        "counts": flow_state.delta(node_id, mapped=len(mapped), failed=len(entries)),
        "_message": _mapped_message(len(mapped), len(entries)),
        "_records_in": len(records),
        "_records_out": len(mapped),
    }


def _mapped_message(mapped: int, failed: int) -> str:
    if not failed:
        return f"{mapped:,} records mapped."
    return f"{mapped:,} records mapped, {failed:,} could not be."


async def _run_validate(
    node: dict, state: Mapping[str, Any], context: RunContext
) -> dict:
    """
    Split the batch into what the destination will accept and what it will not.

    Both halves get a handle. The ``invalid`` port exists so an author can route those
    somewhere — a report, a second destination — rather than having them vanish.
    """
    node_id = flow_rules.node_id_of(node)
    data = flow_rules.data_of(node)
    records = _records_for(node, state, context)

    fields = flow_rules.field_specs_of(data.get("rules"))
    split = record_validation.partition(records, fields)

    entries = [
        record_log.entry(
            node_id=node_id,
            outcome=RECORD_INVALID,
            batch_index=_pass_of(state, context),
            message=outcome.message(),
            source_key=str(position),
            payload=outcome.record,
        )
        for position, outcome in enumerate(split.invalid)
    ]
    await record_log.write(context.run_id, entries, redacted_fields=context.redacted_fields)

    return {
        "outputs": {
            node_id: _emit_ports(
                context, node_id,
                valid=split.valid,
                invalid=[outcome.record for outcome in split.invalid],
            )
        },
        "counts": flow_state.delta(node_id, **split.counts()),
        "_message": f"{len(split.valid):,} valid, {len(split.invalid):,} not.",
        "_records_in": len(records),
        "_records_out": len(split.valid),
    }


async def _run_filter(node: dict, state: Mapping[str, Any], context: RunContext) -> dict:
    """
    Keep the records that match. Both halves get a handle, for the same reason.

    A dropped record is **not** logged. Filtering is what the author asked for, and a
    hundred thousand rows saying "this record did not match your filter" is a log nobody
    reads hiding the rows somebody needs.
    """
    node_id = flow_rules.node_id_of(node)
    data = flow_rules.data_of(node)
    records = _records_for(node, state, context)

    kept, dropped = record_filter.partition(
        records,
        list(data.get("specs") or []),
        mode=str(data.get("match") or record_filter.MATCH_ALL),
    )

    return {
        "outputs": {
            node_id: _emit_ports(context, node_id, kept=kept, dropped=dropped)
        },
        "counts": flow_state.delta(node_id, kept=len(kept), dropped=len(dropped)),
        "_message": f"{len(kept):,} kept, {len(dropped):,} dropped.",
        "_records_in": len(records),
        "_records_out": len(kept),
    }


async def _run_branch(node: dict, state: Mapping[str, Any], context: RunContext) -> dict:
    """
    Choose a port. **First match wins**, in the order the author drew them.

    A scalar decision about the run, not about a record — "did this batch have anything
    in it", "is this a full sync". Per-record decisions are what ``filter`` is for, and
    keeping the two apart is what stops a branch node quietly meaning "some records".
    """
    node_id = flow_rules.node_id_of(node)
    chosen = branch_port(node, state)
    return {
        "outputs": {node_id: {"kind": "branch", "port": chosen}},
        "_message": f"Took the '{chosen}' path.",
    }


async def _run_connector_write(
    node: dict, state: Mapping[str, Any], context: RunContext
) -> dict:
    """
    Send the batch, one outcome per record.

    The node **succeeds** when some records fail — that is the second level of the three,
    and the counters plus the record log carry the detail. It fails only when nothing
    could be attempted at all: an unusable connection, an operation that no longer exists,
    a mapping list that could not be read.
    """
    node_id = flow_rules.node_id_of(node)
    data = flow_rules.data_of(node)
    records = _records_for(node, state, context)
    batch_index = _pass_of(state, context)

    async def cancelled() -> bool:
        return await run_store.cancel_requested(context.run_id)

    async with context.open_session() as db:
        target = await connector_nodes.resolve_target(db, data, user_id=context.user_id)
        outcome = await connector_nodes.write_batch(
            db, target, records,
            node_id=node_id, node_data=data, batch_index=batch_index,
            dry_run=context.dry_run, semaphore=context.gate(), cancelled=cancelled,
        )
        if outcome.sync_keys:
            # In the same transaction as nothing else, but deliberately after the writes
            # succeeded: a key remembered for a create that failed turns the next run's
            # create into an update against an id that does not exist.
            await dedupe.remember(
                db,
                connection_id=target.connection.id,
                operation_id=target.operation.operation_id,
                written=outcome.sync_keys,
            )
            await db.commit()

    await record_log.write(
        context.run_id, outcome.entries, redacted_fields=context.redacted_fields
    )
    await run_store.bump_counts(
        context.run_id,
        records_written=outcome.written,
        records_failed=outcome.failed,
        records_skipped=outcome.skipped,
    )

    return {
        "outputs": {node_id: {"kind": "written", "count": outcome.written}},
        "counts": flow_state.delta(
            node_id,
            written=outcome.written, failed=outcome.failed, skipped=outcome.skipped,
        ),
        "_message": _written_message(target, outcome, context.dry_run),
        "_records_in": len(records),
        "_records_out": outcome.written,
        "_operation_hash": target.fingerprint,
    }


def _written_message(target, outcome, dry_run: bool) -> str:  # noqa: ANN001
    if dry_run:
        return (
            f"Dry run — nothing was sent to {target.connection.label}. "
            f"{len(outcome.entries):,} records were prepared."
        )
    parts = [f"{outcome.written:,} written to {target.connection.label}"]
    if outcome.failed:
        parts.append(f"{outcome.failed:,} failed")
    if outcome.skipped:
        parts.append(f"{outcome.skipped:,} skipped")
    return ", ".join(parts) + "."


async def _run_success(node: dict, state: Mapping[str, Any], context: RunContext) -> dict:
    node_id = flow_rules.node_id_of(node)
    return {
        "outputs": {node_id: {"kind": "end", "totals": flow_state.totals(state)}},
        "_message": flow_rules.data_of(node).get("message") or "Finished.",
    }


async def _run_failure(node: dict, state: Mapping[str, Any], context: RunContext) -> dict:
    """
    An end the author drew for the paths they expect to go wrong.

    Raises rather than returning, because reaching this node *is* the run failing — and
    ``run_service`` decides the run's status from what came out of the graph, so a failure
    end that returned quietly would be recorded as a success.
    """
    message = (
        flow_rules.data_of(node).get("message")
        or f"The workflow ended at '{flow_rules.label_of(node)}'."
    )
    raise NodeFailure(message, node_id=flow_rules.node_id_of(node))


# ---------------------------------------------------------------------------
# The router's questions, answered by the same code the runners used
# ---------------------------------------------------------------------------


def branch_port(node: dict, state: Mapping[str, Any]) -> str:
    """
    Which port a ``branch`` takes. First match wins; ``else`` when none do.

    Called by the runner *and* by the compiler's router, so the port the log records and
    the edge the run follows cannot disagree.
    """
    for condition in flow_rules.data_of(node).get("conditions") or []:
        if record_filter.matches({"state": _scalars(state)}, _rooted(condition)):
            return str(condition.get("port") or PORT_DEFAULT)
    return PORT_ELSE


def _rooted(condition: Mapping[str, Any]) -> Dict[str, Any]:
    """A branch condition, rewritten to read out of the state rather than a record."""
    spec = dict(condition)
    spec["column"] = f"state.{condition.get('column') or condition.get('field') or ''}"
    return spec


def _scalars(state: Mapping[str, Any]) -> Dict[str, Any]:
    """
    The small values a branch may test.

    Totals and inputs only — never a recordset. A branch that could reach into a batch
    would be a per-record decision wearing a scalar's clothes, and ``filter`` is the node
    for that.
    """
    scalars = dict(flow_state.totals(state))
    scalars.update({"inputs": dict((state or {}).get("inputs") or {})})
    return scalars


def validate_port(node: dict, state: Mapping[str, Any]) -> str:
    """``valid`` unless the batch produced nothing valid and something invalid."""
    counts = ((state or {}).get("counts") or {}).get(flow_rules.node_id_of(node)) or {}
    if not counts.get("valid") and counts.get("invalid"):
        return PORT_INVALID
    return PORT_VALID


def filter_port(node: dict, state: Mapping[str, Any]) -> str:
    """``kept`` unless nothing was kept and something was dropped."""
    counts = ((state or {}).get("counts") or {}).get(flow_rules.node_id_of(node)) or {}
    if not counts.get("kept") and counts.get("dropped"):
        return PORT_DROPPED
    return PORT_KEPT


def batch_continues(node: dict, state: Mapping[str, Any]) -> bool:
    """Whether the loop goes round again. The router's half of :func:`_run_batch`."""
    node_id = flow_rules.node_id_of(node)
    produced = ((state or {}).get("outputs") or {}).get(node_id)
    return record_buffer.is_handle(produced)


def batch_port(node: dict, state: Mapping[str, Any]) -> str:
    return PORT_BODY if batch_continues(node, state) else PORT_DONE


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

register_runner(NODE_TRIGGER, _run_trigger)
register_runner(NODE_CONNECTOR_READ, _run_connector_read)
register_runner(NODE_CONNECTOR_WRITE, _run_connector_write)
register_runner(NODE_TRANSFORM, _run_transform)
register_runner(NODE_VALIDATE, _run_validate)
register_runner(NODE_FILTER, _run_filter)
register_runner(NODE_BRANCH, _run_branch)
register_runner(NODE_BATCH, _run_batch)
register_runner(NODE_SUCCESS, _run_success)
register_runner(NODE_FAILURE, _run_failure)

# The vocabulary and the runners, checked against each other at import.
#
# `flow_rules.IMPLEMENTED_NODE_TYPES` is what the validator refuses against and what the
# palette is built from. If the two lists could drift, the canvas would offer a node that
# fails at run time or refuse one that works — and both would be discovered by a user
# rather than by a test. Asserted here rather than in a test because a test can be
# skipped and an import cannot.
assert registered_types() == set(flow_rules.IMPLEMENTED_NODE_TYPES), (
    "the runners and flow_rules.IMPLEMENTED_NODE_TYPES disagree: "
    f"{registered_types() ^ set(flow_rules.IMPLEMENTED_NODE_TYPES)}"
)
