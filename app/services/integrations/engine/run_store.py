"""
Every read and write of a run and its steps, and the session a node runs in.

The one place that touches ``integration_runs`` and ``integration_run_steps``. Both the
node runners and the orchestrator need those writes, and if each opened its own sessions
and built its own rows they would be two answers to "what does a step row look like" —
which shows up as a run page with gaps in it.

**The session seam.** ``open_session`` is called through this module rather than
``AsyncSessionLocal`` being imported directly, for the reason ``graph_designer/run_store``
and ``download_service`` both document: a LangGraph node has no injected session, and a
test has to be able to point one at the test database. One patchable name. Getting this
wrong does not fail cleanly — the nodes write to the development Postgres while the
assertions look at an in-memory SQLite and pass — so the autouse fixture that patches it
is load-bearing rather than convenient.

**A node opens a short session per write** rather than holding one across its work. A
node's work is an HTTP call to somebody else's server; holding this application's own
pooled connection open for the length of that is a pool slot bought for nothing.

**Logging must never fail the node.** Every write here swallows and logs. The log is an
observation of the run, not part of it, and a sync that moved fifty thousand records into
a CRM must not be reported as failed because the row describing it could not be written.
The cost is stated plainly rather than hidden: a counter bump that fails leaves the run's
total *smaller* than the truth, and the application log is the only place that says so.

**Three things this module bounds, and the reason each cap is on the log rather than on
the numbers.**

*Step rows collapse.* After ``STEP_COLLAPSE_AFTER`` passes of one ``(run_id, node_id)``,
``finish_step`` stops inserting and folds into a rollup row, accumulating the record
counts and counting the passes. A ten-thousand-pass backfill would otherwise write a log
larger than the data it describes. The pass count is *kept*, so the page can say "one row
standing for 9,500 passes" rather than quietly implying there were 500.

*The frame carries the tail.* ``run_view`` returns the last hundred step rows plus
``steps_total``; the rest is paginated. Whole-state for the numbers, because a client that
missed a frame must not be left holding a wrong total; pagination for the append-only list,
because a fifty-thousand row log must not arrive on every one-second poll.

*Cancellation is polled, and the poll is cached.* Checking a database row at the top of
every node and between every chunk would be thousands of round trips per run to read a
boolean that changes at most once. ``CANCEL_CACHE_SECONDS`` is what makes the contract
stated in the UI honest and cheap at the same time: **cancel stops at the next record
boundary, not mid-request.**
"""

import logging
import time
import uuid as uuid_pkg
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.db_sessions import AsyncSessionLocal
from app.db.db_utils import CRUDQueryBuilder
from app.db.integrations.queries import (
    bump_run_counts,
    count_records_by_outcome,
    count_run_steps,
    fetch_recent_steps,
    fetch_run_steps,
    fetch_run_with_flow,
    find_rollup_step,
    next_step_sequence,
)
from app.models.integrations import (
    RUN_CANCELLED,
    RUN_PARTIAL,
    RUN_QUEUED,
    RUN_RUNNING,
    RUN_SUCCEEDED,
    STEP_COLLAPSE_AFTER,
    STEP_FAILED,
    STEP_RUNNING,
    TERMINAL_RUN_STATUSES,
    IntegrationFlow,
    IntegrationRun,
    IntegrationRunStep,
)

logger = logging.getLogger(__name__)

run_crud = CRUDQueryBuilder(IntegrationRun)
step_crud = CRUDQueryBuilder(IntegrationRunStep)

#: How long a ``cancel_requested`` read is trusted for. Two seconds is the contract: a
#: cancelled run stops within about that, and a run that is not cancelled costs one query
#: every two seconds rather than one per chunk.
CANCEL_CACHE_SECONDS = 2.0

#: How many step rows the live frame carries. The rest is paginated.
FRAME_STEP_LIMIT = 100

# run_id -> (checked_at_monotonic, cancel_requested). Process-local, like the buffer, and
# for the same reason: a run executes inside one worker task, so there is nothing to
# share and nothing to invalidate across processes.
_cancel_cache: Dict[int, Tuple[float, bool]] = {}


def open_session() -> AsyncSession:
    """
    A session of this module's own, for code that has no request.

    An ``async with`` context manager, so a caller cannot forget to close it — which
    matters more here than in a route, where Litestar closes it either way.
    """
    return AsyncSessionLocal()


def new_thread_id() -> str:
    """
    A fresh checkpointer thread for one run.

    Its own uuid rather than the run's, and that is not redundancy: the run's uuid is
    public and appears in a URL, while a checkpointer thread is the handle on a parked
    graph's entire state. Two different things, kept separate on principle.
    """
    return str(uuid_pkg.uuid4())


def now() -> datetime:
    """The clock, as a seam. A scheduler test must never ``asyncio.sleep``."""
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


async def create_run(
    db: AsyncSession,
    *,
    flow_id: int,
    flow_version_id: Optional[int],
    thread_id: str,
    trigger_id: Optional[int] = None,
    trigger_kind: str = "manual",
    mode: str = "live",
    idempotency_key: Optional[str] = None,
    scheduled_for: Optional[datetime] = None,
    replay_of_run_id: Optional[int] = None,
    status: str = RUN_QUEUED,
) -> IntegrationRun:
    """
    Record a run before anything is compiled or claimed.

    Before, deliberately. A compilation that fails is then a run somebody can open and
    read a reason from, rather than a button that appeared to do nothing.

    Created ``queued`` rather than ``running`` because a run goes through the queue even
    when somebody pressed the button — the run tested at eleven in the morning has to be
    the run that fires at three.

    Takes a caller-supplied ``db`` and does **not** commit — hence ``create_pending``.
    The run row, its queue job and, for a scheduled run, the trigger's advanced
    ``next_run_at`` belong in one transaction: a crash between them either loses a run or
    fires a slot twice, and there is no "between" if there is one commit. The row is
    flushed, so the caller has its id for the job's foreign key immediately.
    """
    return await run_crud.create_pending(db, {
        "flow_id": flow_id,
        "flow_version_id": flow_version_id,
        "trigger_id": trigger_id,
        "trigger_kind": trigger_kind,
        "status": status,
        "mode": mode,
        "idempotency_key": idempotency_key,
        "thread_id": thread_id,
        "scheduled_for": scheduled_for,
        "replay_of_run_id": replay_of_run_id,
    })


async def get_run_and_flow(
    db: AsyncSession, run_uuid: Any, user_id: int
) -> Optional[Tuple[IntegrationRun, IntegrationFlow]]:
    """
    One run with its flow, only if this user owns the flow.

    Ownership runs run → flow → ``user_id``. A run whose flow belongs to somebody else
    comes back as ``None`` and the caller turns that into the same "not found" a missing
    run gets — answering differently would confirm the uuid is real.
    """
    found = await fetch_run_with_flow(db, run_uuid)
    if not found:
        return None

    run, flow = found
    return (run, flow) if flow.user_id == user_id else None


async def mark_running(db: AsyncSession, run_id: int, attempt: int = 1) -> None:
    """Claimed by a worker and starting. ``started_at`` moves to now because a queued run
    that waited an hour did not start an hour ago, and a duration measured from enqueue
    would describe the queue rather than the sync."""
    await run_crud.update(db, run_id, {
        "status": RUN_RUNNING,
        "attempt": attempt,
        "started_at": now(),
        "heartbeat_at": now(),
    })


async def mark_finished(
    db: AsyncSession,
    run_id: int,
    status: str,
    *,
    result_preview: Optional[Mapping[str, Any]] = None,
    error_message: Optional[str] = None,
) -> None:
    """
    Close a run off.

    ``interrupt_payload`` is cleared on every terminal path including the failing one: a
    finished run with a question still attached renders a prompt nobody can answer.
    """
    await run_crud.update(db, run_id, {
        "status": status,
        "interrupt_payload": None,
        "result_preview": dict(result_preview) if result_preview else None,
        "error_message": error_message or None,
        "finished_at": now(),
    })
    _cancel_cache.pop(run_id, None)


def final_status(
    *, failed: int, skipped: int, invalid: int = 0, cancelled: bool = False
) -> str:
    """
    What a run that reached the end should be recorded as.

    **A run with any failed, invalid or skipped record ends ``partial``, never
    ``succeeded``.** This is the rule the three-levels-of-failure design exists for, and
    it is the same argument ``downloader_agents`` makes about part files: an export that
    silently contains some of the data is worse than no export, because nothing about it
    says so. A green tick over "49,997 of 50,000" is a lie the operator has no way to
    catch.

    It is a pure function of four numbers so that it can be asserted as a table, and so
    that the queue, the orchestrator and a replay cannot each decide it differently.
    """
    if cancelled:
        return RUN_CANCELLED
    if failed or invalid or skipped:
        return RUN_PARTIAL
    return RUN_SUCCEEDED


async def heartbeat(run_id: int) -> None:
    """
    Say the worker is still alive.

    Swallowed like every other write here. A missed heartbeat eventually gets the run
    requeued and failed, which is a worse outcome than a lost beat — but raising would
    kill the run *now*, for a write that describes it rather than does it.
    """
    await _quietly("heartbeat", run_id, lambda db: run_crud.update(
        db, run_id, {"heartbeat_at": now()}
    ))


async def bump_counts(run_id: int, **deltas: int) -> None:
    """
    Add to the run's record counters. Deltas, never totals.

    Delegates to ``queries.bump_run_counts``, which issues ``SET x = x + :n`` in one
    statement. Two chunks of a write node add at the same moment by design, and a
    read-modify-write would lose one silently.
    """
    await _quietly("counters", run_id, lambda db: bump_run_counts(db, run_id, **deltas))


async def request_cancel(db: AsyncSession, run_id: int) -> None:
    """
    Ask a run to stop, durably.

    The row is marked **before** the local task is cancelled — the other half of the
    mechanism. Cancelling first races the write, and the page then shows a run that
    stopped with nothing on it saying why.

    The cache entry is dropped so the running node sees this on its very next check
    rather than up to ``CANCEL_CACHE_SECONDS`` later. That only helps in the single
    process where both happen; the timeout is what makes it correct everywhere else.
    """
    await run_crud.update(db, run_id, {"cancel_requested": True})
    _cancel_cache.pop(run_id, None)


async def cancel_requested(run_id: int) -> bool:
    """
    Whether somebody has asked this run to stop, read at most every two seconds.

    Returns ``False`` when the read fails. A database blip must not cancel a run that
    nobody cancelled — the failure mode of guessing wrong in this direction is a sync
    that keeps going, and in the other it is a sync that stops for no reason and reports
    a cancellation the operator did not request.
    """
    cached = _cancel_cache.get(run_id)
    moment = time.monotonic()
    if cached and moment - cached[0] < CANCEL_CACHE_SECONDS:
        return cached[1]

    try:
        async with open_session() as db:
            run = await run_crud.get_one(db, filters={"id": run_id})
            requested = bool(run and run.cancel_requested)
    except Exception:  # noqa: BLE001 — see the docstring
        logger.exception("Could not read the cancel flag for run %s", run_id)
        return False

    _cancel_cache[run_id] = (moment, requested)
    return requested


def forget_run(run_id: int) -> None:
    """Drop a finished run's cached cancel flag. Called on every terminal path, so a
    long-lived worker does not accumulate one entry per run it has ever executed."""
    _cancel_cache.pop(run_id, None)


def cached_runs() -> int:
    """How many runs the cancel cache is holding. For the test that asserts it empties —
    a leak here is invisible until it is a memory profile."""
    return len(_cancel_cache)


def clear_cancel_cache() -> None:
    """Drop every cached cancel flag. For tests, and for a worker shutting down."""
    _cancel_cache.clear()


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


async def begin_step(
    run_id: int,
    node_id: str,
    node_type: str,
    node_label: str,
    *,
    batch_index: int = 0,
    attempt: int = 1,
    records_in: int = 0,
) -> Optional[int]:
    """
    Write the ``running`` row for a node that has just started, and return its id.

    Written **before** the node does its work, which is why a step is two writes rather
    than one: a node that hangs or whose worker dies is then visible as a step that never
    finished. Recorded only on completion, it would be indistinguishable from a node the
    run never reached.

    Returns ``None`` when the write failed, or when this node has already collapsed into
    a rollup — in the second case there is deliberately no ``running`` row to open, and
    :func:`finish_step` folds the pass into the rollup instead. A caller treats ``None``
    the same either way, which is what keeps the collapse invisible to the runner.
    """
    if batch_index >= STEP_COLLAPSE_AFTER:
        return None

    try:
        async with open_session() as db:
            sequence = await next_step_sequence(db, run_id)
            step = await step_crud.create(db, {
                "run_id": run_id,
                "sequence": sequence,
                "node_id": node_id,
                "node_type": node_type,
                "node_label": node_label,
                "batch_index": batch_index,
                "attempt": attempt,
                "status": STEP_RUNNING,
                "records_in": records_in,
            })
            return step.id
    except Exception:  # noqa: BLE001 — logging must not break the run
        logger.exception(
            "Could not record the start of node %s on run %s", node_id, run_id,
        )
        return None


async def finish_step(
    step_id: Optional[int],
    status: str,
    *,
    duration_ms: Optional[int] = None,
    message: Optional[str] = None,
    records_in: Optional[int] = None,
    records_out: Optional[int] = None,
    output_preview: Optional[Mapping[str, Any]] = None,
    state_preview: Optional[Mapping[str, Any]] = None,
    operation_hash: Optional[str] = None,
    egress_policy: Optional[str] = None,
    resolved_ip: Optional[str] = None,
) -> None:
    """
    Complete the row :func:`begin_step` opened.

    A ``None`` ``step_id`` means either the opening write failed or this node has
    collapsed; the node still ran either way, and the pass is accounted for by
    :func:`roll_up_step`. Same swallow, same reason.

    Every preview handed here is already capped **and redacted** by
    ``flow_state.preview_of`` — this table is one an API response body reaches, so
    redaction happening before the write is what makes it a property of the data rather
    than of whoever renders it.
    """
    if step_id is None:
        return

    updates: Dict[str, Any] = {
        "status": status,
        "duration_ms": duration_ms,
        "message": message,
        "output_preview": dict(output_preview) if output_preview else None,
        "state_preview": dict(state_preview) if state_preview else None,
        "operation_hash": operation_hash,
        "egress_policy": egress_policy,
        "resolved_ip": resolved_ip,
        "finished_at": now(),
    }
    if records_in is not None:
        updates["records_in"] = records_in
    if records_out is not None:
        updates["records_out"] = records_out

    try:
        async with open_session() as db:
            await step_crud.update(db, step_id, updates)
    except Exception:  # noqa: BLE001 — logging must not break the run
        logger.exception("Could not record the end of step %s", step_id)


async def roll_up_step(
    run_id: int,
    node_id: str,
    node_type: str,
    node_label: str,
    *,
    status: str,
    records_in: int = 0,
    records_out: int = 0,
    message: Optional[str] = None,
) -> None:
    """
    Account for a pass that is past the collapse threshold.

    One row per node holds the tail of a long loop: ``rollup_count`` counts the passes it
    stands for and the two record counts accumulate. The count is kept rather than
    dropped so the page can say "one row standing for 9,500 passes" — a log that silently
    stops at 500 implies there were 500, which is a worse answer than no log.

    ``status`` is *sticky towards failure*: once a rolled-up pass has failed, the row
    stays failed however many succeed afterwards. A rollup that reported the last pass
    would hide the only interesting thing in it.
    """
    try:
        async with open_session() as db:
            existing = await find_rollup_step(db, run_id, node_id)

            if existing is None:
                sequence = await next_step_sequence(db, run_id)
                await step_crud.create(db, {
                    "run_id": run_id,
                    "sequence": sequence,
                    "node_id": node_id,
                    "node_type": node_type,
                    "node_label": node_label,
                    "batch_index": STEP_COLLAPSE_AFTER,
                    "status": status,
                    "is_rollup": True,
                    "rollup_count": 1,
                    "records_in": records_in,
                    "records_out": records_out,
                    "message": message or _rollup_message(1),
                    "finished_at": now(),
                })
                return

            passes = int(existing.rollup_count or 0) + 1
            await step_crud.update(db, existing.id, {
                "rollup_count": passes,
                "records_in": int(existing.records_in or 0) + records_in,
                "records_out": int(existing.records_out or 0) + records_out,
                "status": _stickier(existing.status, status),
                "message": message or _rollup_message(passes),
                "finished_at": now(),
            })
    except Exception:  # noqa: BLE001 — logging must not break the run
        logger.exception("Could not roll up node %s on run %s", node_id, run_id)


def _rollup_message(passes: int) -> str:
    return f"One row standing for {passes:,} further passes of this step."


def _stickier(existing: Optional[str], incoming: str) -> str:
    """A rollup keeps the worse of the two statuses. See :func:`roll_up_step`."""
    return STEP_FAILED if STEP_FAILED in (existing, incoming) else incoming


async def record_step(
    run_id: int,
    node_id: str,
    node_type: str,
    node_label: str,
    status: str,
    *,
    message: Optional[str] = None,
    batch_index: int = 0,
) -> None:
    """
    One complete step row in a single write, for an outcome with no duration.

    Used for a node that was never reached, or one skipped because the run was cancelled
    before it. It gets a row at all because **a node missing from the log is
    indistinguishable from a node the run never reached** — and telling those two apart is
    most of what somebody reading a cancelled run wants.
    """
    try:
        async with open_session() as db:
            sequence = await next_step_sequence(db, run_id)
            await step_crud.create(db, {
                "run_id": run_id,
                "sequence": sequence,
                "node_id": node_id,
                "node_type": node_type,
                "node_label": node_label,
                "batch_index": batch_index,
                "status": status,
                "message": message,
                "finished_at": now(),
            })
    except Exception:  # noqa: BLE001 — logging must not break the run
        logger.exception("Could not record node %s on run %s", node_id, run_id)


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------


async def run_view(db: AsyncSession, run: IntegrationRun, flow: IntegrationFlow) -> dict:
    """
    One run as the SSE frame and the polling body both read it.

    The same shape for both on purpose: a client whose stream dropped and fell back to
    polling must not have to understand a second payload.

    **Whole state for the numbers, a window for the list.** Every counter is absolute, so
    a consumer that missed a frame is not left holding a wrong total — the reason
    ``progress.py`` gives, and the reason a delta-based frame is wrong for anything
    somebody bills on. The step list is the last hundred rows plus ``steps_total``,
    because a fifty-thousand step run must not arrive on every one-second poll; the rest
    is at ``/runs/{id}/steps?after=``.
    """
    steps = await fetch_recent_steps(db, run.id, FRAME_STEP_LIMIT)
    steps_total = await count_run_steps(db, run.id)

    return {
        "uuid": str(run.uuid),
        "flow_uuid": str(flow.uuid),
        "flow_name": flow.name,
        "status": run.status,
        "mode": run.mode,
        "trigger_kind": run.trigger_kind,
        "attempt": run.attempt,
        "cancel_requested": bool(run.cancel_requested),
        "counts": {
            "read": int(run.records_read or 0),
            "written": int(run.records_written or 0),
            "failed": int(run.records_failed or 0),
            "skipped": int(run.records_skipped or 0),
        },
        "records_log_truncated": bool(run.records_log_truncated),
        "interrupt_payload": dict(run.interrupt_payload) if run.interrupt_payload else None,
        "result_preview": dict(run.result_preview) if run.result_preview else None,
        "error_message": run.error_message,
        "steps": [step_view(step) for step in steps],
        "steps_total": steps_total,
        "scheduled_for": run.scheduled_for,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
    }


def step_view(step: IntegrationRunStep) -> dict:
    """One step row as the run page reads it. No bigint id, here or anywhere."""
    return {
        "uuid": str(step.uuid),
        "sequence": step.sequence,
        "node_id": step.node_id,
        "node_type": step.node_type,
        "node_label": step.node_label,
        "batch_index": step.batch_index,
        "attempt": step.attempt,
        "status": step.status,
        "records_in": int(step.records_in or 0),
        "records_out": int(step.records_out or 0),
        "is_rollup": bool(step.is_rollup),
        "rollup_count": int(step.rollup_count or 0),
        "duration_ms": step.duration_ms,
        "message": step.message,
        "output_preview": dict(step.output_preview) if step.output_preview else None,
        "state_preview": dict(step.state_preview) if step.state_preview else None,
        "operation_hash": step.operation_hash,
        "egress_policy": step.egress_policy,
        "resolved_ip": step.resolved_ip,
        "started_at": step.started_at,
        "finished_at": step.finished_at,
    }


async def steps_page(
    db: AsyncSession, run_id: int, *, after_sequence: int = -1, limit: int = 200
) -> List[dict]:
    """The paginated log behind the frame's hundred-row window."""
    steps = await fetch_run_steps(db, run_id, after_sequence=after_sequence, limit=limit)
    return [step_view(step) for step in steps]


async def node_rollup(db: AsyncSession, run_id: int) -> Dict[str, dict]:
    """
    The latest state of each node, for repainting the canvas.

    Keyed by node id because the canvas draws one ring per node however many passes it
    made: the ring shows where that node stands and the log lists the passes.
    """
    latest: Dict[str, dict] = {}
    for step in await fetch_run_steps(db, run_id):
        latest[step.node_id] = step_view(step)
    return latest


async def record_outcome_counts(db: AsyncSession, run_id: int) -> Dict[str, int]:
    """
    How many rows of each outcome the record log holds.

    Separate from the run's counters and deliberately so: these are how many were
    *logged*, the counters are how many there were. When the log truncates, the run page
    shows both, and the difference is the honest statement that some detail is missing.
    """
    return await count_records_by_outcome(db, run_id)


def is_terminal(status: Optional[str]) -> bool:
    """Whether a run is over and the progress stream should stop."""
    return str(status or "") in TERMINAL_RUN_STATUSES


async def reload_run(db: AsyncSession, run_id: int) -> Optional[IntegrationRun]:
    """
    Re-read one run row.

    Used by the poll loop, which must see writes made by the *other* task driving the
    run. A session caches identity-mapped rows, so the loop opens a fresh session per
    poll rather than re-reading through one it has held.
    """
    return await run_crud.get_one(db, filters={"id": run_id})


# ---------------------------------------------------------------------------
# The swallow
# ---------------------------------------------------------------------------


async def _quietly(what: str, run_id: int, operation) -> None:  # noqa: ANN001
    """
    Run one short-session write, logging rather than raising if it fails.

    Factored out because the alternative is the same six lines of ``try``/``except
    Exception``/``logger.exception`` at every call site, and the day one of them is
    written without the swallow is the day a counter bump fails a run that had already
    finished its work.
    """
    try:
        async with open_session() as db:
            await operation(db)
            await db.commit()
    except Exception:  # noqa: BLE001 — logging must not break the run
        logger.exception("Could not write %s for run %s", what, run_id)
