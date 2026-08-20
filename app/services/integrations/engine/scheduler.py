"""
Firing workflows on a schedule.

No scheduler exists anywhere else in this codebase, so this is built in the shape
``download_service.run_expiry_reaper`` uses — ``while True: tick(); sleep()``, registered
as an asyncio task in ``on_startup`` — with ``job_queue.run_worker``'s error discipline:
**every failure inside a tick is logged and the loop continues.** A scheduler that exited
over one misconfigured flow would take the feature down silently, and nobody is watching
at three in the morning to notice that nothing fired.

**Nothing about the schedule lives in memory.** ``next_run_at`` is a column, backfilled
whenever a trigger is enabled or edited. That is the whole point of it: a fresh process
started against a database with a due row fires it, and a restart mid-schedule lands on
the persisted time rather than an interval measured from boot. There is a test that
constructs a scheduler with no history and proves exactly that.

**The claim is one transaction.** Lock the due triggers, set ``last_fired_at``, compute
and store the next ``next_run_at``, insert the run row and insert the queue job — then
commit once. A crash between advancing the clock and enqueueing the work cannot happen
because there is no "between". The ``idempotency_key`` unique index is the second line of
defence, not the first.

**``catch_up = false`` is the only Phase 1 behaviour.** A trigger an hour stale fires once
and jumps to the next slot. Firing twelve missed hourly slots costs twelve times the API
quota for zero extra data, because an incremental sync's single catch-up run reads
everything those twelve would have. The column exists; the behaviour does not, and
``validate_flow`` refuses ``catch_up = true`` rather than accepting a setting that does
nothing.

**``overlap_policy = skip`` writes a run row saying it was skipped**, rather than doing
nothing quietly. One row per skipped tick is the only way an operator ever discovers that
their five-minute sync takes seven minutes — silence looks identical to a schedule that is
working.

**The clock is a seam.** :func:`now` is patched by tests, which is what lets a scheduler
test assert that twelve missed slots produce one run without waiting twelve intervals.
Never ``asyncio.sleep`` in a scheduler test.
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.db_utils import CRUDQueryBuilder
from app.db.integrations.queries import claim_due_triggers, runs_in_flight
from app.models.integrations import (
    MIN_INTERVAL_SECONDS,
    OVERLAP_CANCEL_PREVIOUS,
    OVERLAP_QUEUE,
    OVERLAP_QUEUE_LIMIT,
    OVERLAP_SKIP,
    RUN_SKIPPED,
    TRIGGER_SCHEDULE,
    IntegrationFlow,
    IntegrationFlowVersion,
    IntegrationRun,
    IntegrationTrigger,
)
from app.services.integrations.engine import queue, run_service, run_store

logger = logging.getLogger(__name__)

flow_crud = CRUDQueryBuilder(IntegrationFlow)
version_crud = CRUDQueryBuilder(IntegrationFlowVersion)
trigger_crud = CRUDQueryBuilder(IntegrationTrigger)
run_crud = CRUDQueryBuilder(IntegrationRun)

#: How often the scheduler looks. Fifteen seconds against a sixty-second minimum interval
#: means a schedule fires within a quarter of its shortest possible period — close enough
#: that nobody notices, cheap enough that N replicas ticking is still a trivial query.
TICK_SECONDS = float(os.getenv("INTEGRATION_SCHEDULER_TICK_SECONDS", "15"))

#: How many due triggers one tick handles. A cap rather than a limit anybody should reach:
#: it bounds the transaction, and whatever is left is due again in fifteen seconds.
BATCH_LIMIT = 50

#: How long the loop waits after an unexpected failure, as opposed to a failure firing one
#: trigger. Whatever broke is probably the database.
LOOP_ERROR_BACKOFF_SECONDS = 30.0

_task: Optional[asyncio.Task] = None


def now() -> datetime:
    """The clock, as a seam. Patched by tests — never ``asyncio.sleep`` in one."""
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# next_run_at
# ---------------------------------------------------------------------------


def _aware(moment: Optional[datetime]) -> Optional[datetime]:
    """
    A datetime off a row, as UTC-aware.

    ``DateTime(timezone=True)`` gives back an aware value on PostgreSQL and a **naive**
    one on SQLite, which drops the offset at storage. Comparing the two raises
    ``TypeError``, so every datetime that has been through the database goes through here
    before any arithmetic. Assuming UTC is right rather than convenient: every write in
    this module is ``datetime.now(timezone.utc)``, so a naive value is a UTC value that
    lost its label.
    """
    if moment is None:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def next_slot_after(
    moment: datetime, interval_seconds: int, *, last: Optional[datetime] = None
) -> datetime:
    """
    The next time a trigger should fire.

    **Jumps past every missed slot rather than firing them.** A trigger whose worker was
    down for an hour comes back due once, at the next slot from now — not twelve times.
    That is ``catch_up = false``, and it is the only Phase 1 behaviour; see the module
    docstring for why firing the missed ones is worse than useless.

    Anchored to ``last`` where there is one, so a schedule keeps its phase — an hourly sync
    set up at 09:07 stays at :07 rather than drifting by however long each run took. Slots
    are advanced by whole intervals from that anchor, which is what makes "the 09:00 run"
    a name a late run can still answer to.
    """
    interval = max(MIN_INTERVAL_SECONDS, int(interval_seconds or 0))
    moment = _aware(moment)
    anchor = _aware(last) or moment

    if anchor > moment:
        return anchor

    elapsed = (moment - anchor).total_seconds()
    whole = int(elapsed // interval) + 1
    return anchor + timedelta(seconds=whole * interval)


def backfill_next_run_at(trigger: IntegrationTrigger, *, moment: Optional[datetime] = None) -> None:
    """
    Set or clear ``next_run_at`` on a trigger, in the caller's session.

    Called whenever a trigger is enabled, disabled or has its interval edited — which is
    what keeps the column true and therefore what lets the scheduler hold nothing in
    memory. A disabled trigger has it cleared rather than left stale, so the column can be
    read as "when this is next due" without a second condition.
    """
    at = moment or now()

    if not trigger.is_enabled or trigger.kind != TRIGGER_SCHEDULE:
        trigger.next_run_at = None
        return

    trigger.next_run_at = next_slot_after(
        at,
        trigger.interval_seconds or MIN_INTERVAL_SECONDS,
        last=_aware(trigger.last_fired_at),
    )


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


def start_scheduler() -> Optional[asyncio.Task]:
    """Start this process's scheduler, if it is not already running. Idempotent."""
    global _task

    if _task is not None and not _task.done():
        return _task

    _task = asyncio.create_task(run_scheduler(), name="integration-scheduler")
    logger.info("Integration scheduler started")
    return _task


async def stop_scheduler() -> None:
    """Stop the scheduler and wait for it to unwind. Shutdown is not a failure."""
    global _task

    task, _task = _task, None
    if task is None or task.done():
        return

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    logger.info("Integration scheduler stopped")


def is_running() -> bool:
    return _task is not None and not _task.done()


async def run_scheduler() -> None:
    """Tick forever. Every failure inside a tick is logged and the loop continues."""
    while True:
        try:
            await tick()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — one bad trigger must not stop the schedule
            logger.exception("The integration scheduler hit an unexpected failure")
            await asyncio.sleep(LOOP_ERROR_BACKOFF_SECONDS)
            continue

        await asyncio.sleep(TICK_SECONDS)


async def tick() -> List[int]:
    """
    Fire everything that is due. Returns the run ids created, for tests and logging.

    Split out of the loop so a test can drive exactly one iteration against a database
    with a due row — including a *fresh* scheduler with no history, which is the assertion
    that proves nothing about the schedule lives in memory.
    """
    moment = now()
    fired: List[int] = []

    async with run_store.open_session() as db:
        triggers = await claim_due_triggers(db, moment, BATCH_LIMIT)

        for trigger in triggers:
            try:
                run_id = await _fire(db, trigger, moment)
            except Exception:  # noqa: BLE001 — one trigger, not the tick
                logger.exception("Could not fire trigger %s", trigger.uuid)
                # The clock still advances. A trigger that fails to fire and keeps its old
                # `next_run_at` is due again on the next tick, and again, forever — a
                # scheduler spinning on one broken workflow every fifteen seconds.
                _advance(trigger, moment)
                continue

            if run_id is not None:
                fired.append(run_id)

        await db.commit()

    if fired:
        # After the commit, never before: waking a worker for a job whose transaction has
        # not landed sends it looking for something that is not there.
        queue.wake()

    return fired


async def _fire(
    db: AsyncSession, trigger: IntegrationTrigger, moment: datetime
) -> Optional[int]:
    """
    One trigger: advance its clock, then create the run and the job — or record a skip.

    The clock is advanced **first and unconditionally**. A slot that was skipped because
    the last run is still going is still a slot that has passed, and leaving the trigger
    due would fire it again in fifteen seconds and every fifteen seconds after that.
    """
    scheduled_for = _aware(trigger.next_run_at) or moment
    _advance(trigger, moment)

    flow = await flow_crud.get_one(db, filters={"id": trigger.flow_id})
    if flow is None or not flow.is_active:
        return None

    policy = trigger.overlap_policy or OVERLAP_SKIP
    in_flight = await runs_in_flight(db, flow.id)

    if in_flight and policy == OVERLAP_SKIP:
        await _record_skip(db, flow, trigger, scheduled_for, in_flight)
        return None

    if in_flight >= OVERLAP_QUEUE_LIMIT and policy == OVERLAP_QUEUE:
        # Bounded, then degrades to skip. An unbounded queue for a sync that takes longer
        # than its interval grows forever, and the first anybody hears of it is a thousand
        # pending runs.
        await _record_skip(db, flow, trigger, scheduled_for, in_flight)
        return None

    if in_flight and policy == OVERLAP_CANCEL_PREVIOUS:
        await _cancel_previous(db, flow.id)

    version = await _published_version(db, flow.id)
    if version is None:
        # A schedule can only be enabled on a published flow, so this is a flow whose
        # version was withdrawn underneath it. Recorded as a skip rather than a failure:
        # nothing went wrong, there is simply nothing to run.
        await _record_skip(db, flow, trigger, scheduled_for, in_flight, unpublished=True)
        return None

    run = await run_service.begin_run(
        db,
        flow,
        version=version,
        trigger_kind=TRIGGER_SCHEDULE,
        trigger_id=trigger.id,
        idempotency_key=_slot_key(trigger, scheduled_for),
        scheduled_for=scheduled_for,
    )
    await queue.enqueue(db, run)
    return run.id


def _advance(trigger: IntegrationTrigger, moment: datetime) -> None:
    trigger.last_fired_at = moment
    trigger.next_run_at = next_slot_after(
        moment, trigger.interval_seconds or MIN_INTERVAL_SECONDS, last=moment
    )


def _slot_key(trigger: IntegrationTrigger, scheduled_for: datetime) -> str:
    """
    The idempotency key for one slot of one schedule.

    The *slot*, not the moment of firing — so two schedulers racing on the same tick
    produce one run, and a run that started eleven minutes late because it waited in the
    queue is still recognisably the 09:00 run.
    """
    from app.services.integrations.engine.idempotency import schedule_run_key

    return schedule_run_key(trigger.uuid, scheduled_for)


async def _record_skip(
    db: AsyncSession,
    flow: IntegrationFlow,
    trigger: IntegrationTrigger,
    scheduled_for: datetime,
    in_flight: int,
    *,
    unpublished: bool = False,
) -> None:
    """
    Write a run row saying this tick was skipped, and why.

    **A row rather than silence.** One row per skipped tick is the only way an operator
    discovers that their five-minute sync takes seven minutes; doing nothing quietly looks
    exactly like a schedule that is working. It carries the same ``idempotency_key`` as a
    real run would, so a slot cannot produce both a skip and a run.
    """
    reason = (
        "This workflow has no published version, so the scheduled run was skipped."
        if unpublished
        else (
            f"The previous run was still going, so this scheduled run was skipped "
            f"({in_flight} already in progress)."
        )
    )

    run = await run_service.begin_run(
        db,
        flow,
        trigger_kind=TRIGGER_SCHEDULE,
        trigger_id=trigger.id,
        idempotency_key=_slot_key(trigger, scheduled_for),
        scheduled_for=scheduled_for,
    )
    run.status = RUN_SKIPPED
    run.error_message = reason
    run.finished_at = now()


async def _cancel_previous(db: AsyncSession, flow_id: int) -> None:
    """
    Ask every in-flight run of this flow to stop, so the new one can go.

    Marks the rows only. The local task, if the run is in this process, is cancelled by
    ``run_service.request_stop`` — but a run in another worker can only be reached through
    its row, and that is the mechanism this relies on.
    """
    running = await run_crud.get_many(db, filters={"flow_id": flow_id})
    for run in running:
        if run.status in ("queued", "running"):
            await run_service.request_stop(db, run)


async def _published_version(
    db: AsyncSession, flow_id: int
) -> Optional[IntegrationFlowVersion]:
    versions = await version_crud.get_many(
        db, filters={"flow_id": flow_id, "status": "published"}, limit=1
    )
    return versions[0] if versions else None
