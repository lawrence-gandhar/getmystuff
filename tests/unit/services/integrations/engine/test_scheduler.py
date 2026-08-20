"""
Tests for ``engine/scheduler.py``.

**Never ``asyncio.sleep``.** ``scheduler.now`` is a seam and every test here patches it,
which is what lets "twelve missed hourly slots fire once" be an assertion rather than a
twelve-hour wait.

The properties, in the order the failures matter:

**Nothing about the schedule lives in memory.** A *fresh* scheduler — one that has never
seen this trigger — fires a due row on its first tick. That is the whole reason
``next_run_at`` is a column, and it is what makes a restart mid-schedule land on the
persisted time rather than an interval measured from boot.

**A trigger due twice in one tick fires once**, and the clock advances inside the same
transaction as the run and the job, so a crash between advancing and enqueueing cannot
happen.

**``catch_up = false``.** An hour-stale trigger jumps to the next slot rather than firing
twelve times. Twelve missed hourly slots is twelve times the API quota for zero extra
data.

**``overlap_policy = skip`` writes a row saying so.** Doing nothing quietly looks exactly
like a schedule that is working, and one row per skipped tick is the only way an operator
discovers their five-minute sync takes seven minutes.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.integrations import (
    MIN_INTERVAL_SECONDS,
    OVERLAP_CANCEL_PREVIOUS,
    OVERLAP_QUEUE,
    OVERLAP_QUEUE_LIMIT,
    OVERLAP_SKIP,
    RUN_QUEUED,
    RUN_RUNNING,
    RUN_SKIPPED,
    TRIGGER_MANUAL,
    TRIGGER_SCHEDULE,
    IntegrationFlow,
    IntegrationFlowVersion,
    IntegrationRun,
    IntegrationRunJob,
    IntegrationTrigger,
)
from app.models.user.user import User
from app.services.integrations.engine import scheduler
from app.services.integrations.engine.scheduler import _aware

NOON = datetime(2026, 3, 4, 12, 0, 0, tzinfo=timezone.utc)
HOURLY = 3600


@pytest.fixture
def frozen_clock(monkeypatch: pytest.MonkeyPatch):  # noqa: ANN001, ANN201
    """
    A clock the test moves by hand.

    The alternative is a scheduler test that sleeps, which is a test that is slow when it
    passes and flaky when it does not.
    """
    holder = {"now": NOON}
    monkeypatch.setattr(scheduler, "now", lambda: holder["now"])
    return holder


@pytest.fixture
async def flow(db: AsyncSession, user: User) -> IntegrationFlow:
    # `is_active` defaults to False — a new flow is a draft, and a draft's triggers do
    # not fire. A scheduler test needs one somebody has switched on.
    row = IntegrationFlow(
        user_id=user.id, name="Nightly sync", graph_data={}, is_active=True
    )
    db.add(row)
    await db.commit()
    return row


@pytest.fixture
async def version(db: AsyncSession, flow: IntegrationFlow) -> IntegrationFlowVersion:
    row = IntegrationFlowVersion(
        flow_id=flow.id, version_number=1, graph_data={},
        graph_hash="h1", status="published",
    )
    db.add(row)
    await db.commit()
    return row


async def make_trigger(
    db: AsyncSession, flow: IntegrationFlow, *, due_at: datetime = NOON, **fields
) -> IntegrationTrigger:
    defaults = dict(
        flow_id=flow.id,
        node_id="start",
        kind=TRIGGER_SCHEDULE,
        is_enabled=True,
        interval_seconds=HOURLY,
        next_run_at=due_at,
        overlap_policy=OVERLAP_SKIP,
    )
    defaults.update(fields)
    row = IntegrationTrigger(**defaults)
    db.add(row)
    await db.commit()
    return row


async def run_count(db: AsyncSession, **filters) -> int:
    statement = select(func.count()).select_from(IntegrationRun)
    for column, value in filters.items():
        statement = statement.where(getattr(IntegrationRun, column) == value)
    result = await db.execute(statement)
    return int(result.scalar_one())


async def job_count(db: AsyncSession) -> int:
    result = await db.execute(select(func.count()).select_from(IntegrationRunJob))
    return int(result.scalar_one())


# ---------------------------------------------------------------------------
# next_run_at as arithmetic
# ---------------------------------------------------------------------------


class TestNextSlot:
    def test_the_next_slot_is_one_interval_on(self) -> None:
        assert scheduler.next_slot_after(NOON, HOURLY, last=NOON) == NOON + timedelta(
            hours=1
        )

    def test_twelve_missed_slots_produce_one(self) -> None:
        """
        ``catch_up = false``, as arithmetic. Firing twelve missed hourly slots costs
        twelve times the API quota for zero extra data — an incremental sync's single
        catch-up run reads everything those twelve would have.
        """
        stale = NOON - timedelta(hours=12)
        assert scheduler.next_slot_after(NOON, HOURLY, last=stale) == NOON + timedelta(
            hours=1
        )

    def test_the_phase_is_kept(self) -> None:
        """An hourly sync set up at 09:07 stays at :07 rather than drifting by however
        long each run took."""
        anchor = NOON.replace(minute=7)
        moment = anchor + timedelta(hours=3, minutes=2)

        assert scheduler.next_slot_after(moment, HOURLY, last=anchor).minute == 7

    def test_an_interval_below_the_floor_is_raised_to_it(self) -> None:
        """The floor is a validation rule; this is the second line, so a row edited by
        hand cannot make a workflow fire every second."""
        assert scheduler.next_slot_after(NOON, 1, last=NOON) == NOON + timedelta(
            seconds=MIN_INTERVAL_SECONDS
        )

    def test_a_future_anchor_is_left_alone(self) -> None:
        later = NOON + timedelta(hours=2)
        assert scheduler.next_slot_after(NOON, HOURLY, last=later) == later


class TestBackfill:
    def test_enabling_a_trigger_gives_it_a_next_run(self, frozen_clock) -> None:  # noqa: ANN001
        trigger = IntegrationTrigger(
            flow_id=1, node_id="start", kind=TRIGGER_SCHEDULE,
            is_enabled=True, interval_seconds=HOURLY,
        )
        scheduler.backfill_next_run_at(trigger)

        assert trigger.next_run_at == NOON + timedelta(hours=1)

    def test_disabling_one_clears_it_rather_than_leaving_it_stale(
        self, frozen_clock
    ) -> None:  # noqa: ANN001
        """So the column reads as "when this is next due" without a second condition."""
        trigger = IntegrationTrigger(
            flow_id=1, node_id="start", kind=TRIGGER_SCHEDULE,
            is_enabled=False, interval_seconds=HOURLY, next_run_at=NOON,
        )
        scheduler.backfill_next_run_at(trigger)

        assert trigger.next_run_at is None

    def test_a_manual_trigger_never_gets_one(self, frozen_clock) -> None:  # noqa: ANN001
        trigger = IntegrationTrigger(
            flow_id=1, node_id="start", kind=TRIGGER_MANUAL, is_enabled=True,
        )
        scheduler.backfill_next_run_at(trigger)

        assert trigger.next_run_at is None


# ---------------------------------------------------------------------------
# The tick
# ---------------------------------------------------------------------------


class TestFiring:
    async def test_a_fresh_scheduler_fires_a_due_row(
        self, db: AsyncSession, flow: IntegrationFlow, version, frozen_clock
    ) -> None:  # noqa: ANN001
        """
        The assertion that proves nothing about the schedule lives in memory. This
        scheduler has never seen this trigger — it is the process that just started — and
        it fires it because ``next_run_at`` is a column.
        """
        await make_trigger(db, flow, due_at=NOON - timedelta(minutes=1))

        fired = await scheduler.tick()

        assert len(fired) == 1
        assert await job_count(db) == 1, "the run and its job land together"

    async def test_the_clock_advances_in_the_same_transaction(
        self, db: AsyncSession, flow: IntegrationFlow, version, frozen_clock
    ) -> None:  # noqa: ANN001
        """A crash between advancing the clock and enqueueing the work cannot happen
        because there is no "between"."""
        trigger = await make_trigger(db, flow, due_at=NOON - timedelta(minutes=1))

        await scheduler.tick()
        await db.refresh(trigger)

        assert trigger.last_fired_at is not None
        assert _aware(trigger.next_run_at) > NOON

    async def test_a_trigger_due_twice_in_one_tick_fires_once(
        self, db: AsyncSession, flow: IntegrationFlow, version, frozen_clock
    ) -> None:  # noqa: ANN001
        """Two ticks with no time passing between them. The second finds nothing due,
        because the first advanced the row."""
        await make_trigger(db, flow, due_at=NOON - timedelta(hours=5))

        first = await scheduler.tick()
        second = await scheduler.tick()

        assert len(first) == 1
        assert second == []

    async def test_a_run_is_pinned_to_the_published_version(
        self, db: AsyncSession, flow: IntegrationFlow, version, frozen_clock
    ) -> None:  # noqa: ANN001
        """A scheduled run executes the snapshot, not whatever the drawing has become
        since — which is what makes an audit trail one."""
        await make_trigger(db, flow)

        fired = await scheduler.tick()
        run = await db.get(IntegrationRun, fired[0])

        assert run.flow_version_id == version.id
        assert run.trigger_kind == TRIGGER_SCHEDULE

    async def test_the_run_carries_the_slot_as_its_idempotency_key(
        self, db: AsyncSession, flow: IntegrationFlow, version, frozen_clock
    ) -> None:  # noqa: ANN001
        """
        The slot, not the moment of firing. Two schedulers racing on the same tick produce
        one run, and a run that started eleven minutes late is still recognisably the
        09:00 run.
        """
        trigger = await make_trigger(db, flow, due_at=NOON)

        fired = await scheduler.tick()
        run = await db.get(IntegrationRun, fired[0])

        assert str(trigger.uuid) in run.idempotency_key
        assert NOON.isoformat() in run.idempotency_key

    async def test_a_disabled_trigger_is_not_due(
        self, db: AsyncSession, flow: IntegrationFlow, version, frozen_clock
    ) -> None:  # noqa: ANN001
        await make_trigger(db, flow, is_enabled=False)
        assert await scheduler.tick() == []

    async def test_a_trigger_due_later_is_left_alone(
        self, db: AsyncSession, flow: IntegrationFlow, version, frozen_clock
    ) -> None:  # noqa: ANN001
        await make_trigger(db, flow, due_at=NOON + timedelta(hours=1))
        assert await scheduler.tick() == []

    async def test_an_inactive_flow_does_not_fire(
        self, db: AsyncSession, flow: IntegrationFlow, version, frozen_clock
    ) -> None:  # noqa: ANN001
        await make_trigger(db, flow)
        flow.is_active = False
        await db.commit()

        assert await scheduler.tick() == []
        assert await run_count(db) == 0, "not even a skip row — the flow is parked"

    async def test_a_flow_with_no_published_version_records_a_skip(
        self, db: AsyncSession, flow: IntegrationFlow, frozen_clock
    ) -> None:  # noqa: ANN001
        """
        A version withdrawn underneath a live schedule. Recorded as a skip rather than a
        failure: nothing went wrong, there is simply nothing to run — and a red badge
        would send somebody looking for a fault.
        """
        await make_trigger(db, flow)

        assert await scheduler.tick() == []
        assert await run_count(db, status=RUN_SKIPPED) == 1


class TestOverlap:
    async def test_skip_writes_a_row_saying_why(
        self, db: AsyncSession, flow: IntegrationFlow, version, frozen_clock
    ) -> None:  # noqa: ANN001
        """
        **A row rather than silence.** One row per skipped tick is the only way an
        operator discovers their five-minute sync takes seven minutes; doing nothing
        quietly looks exactly like a schedule that is working.
        """
        db.add(IntegrationRun(
            flow_id=flow.id, status=RUN_RUNNING, thread_id="t", trigger_kind=TRIGGER_MANUAL,
        ))
        await db.commit()

        await make_trigger(db, flow, overlap_policy=OVERLAP_SKIP)

        assert await scheduler.tick() == []

        skipped = await db.execute(
            select(IntegrationRun).where(IntegrationRun.status == RUN_SKIPPED)
        )
        row = skipped.scalars().first()
        assert row is not None
        assert "still going" in row.error_message
        assert row.finished_at is not None, "a skipped run is over, not pending"

    async def test_skip_still_advances_the_clock(
        self, db: AsyncSession, flow: IntegrationFlow, version, frozen_clock
    ) -> None:  # noqa: ANN001
        """
        A slot skipped because the last run is still going is still a slot that has
        passed. Leaving the trigger due would fire it again in fifteen seconds, and every
        fifteen seconds after that.
        """
        db.add(IntegrationRun(
            flow_id=flow.id, status=RUN_RUNNING, thread_id="t", trigger_kind=TRIGGER_MANUAL,
        ))
        await db.commit()
        trigger = await make_trigger(db, flow, overlap_policy=OVERLAP_SKIP)

        await scheduler.tick()
        await db.refresh(trigger)

        assert _aware(trigger.next_run_at) > NOON

    async def test_queue_allows_a_bounded_backlog(
        self, db: AsyncSession, flow: IntegrationFlow, version, frozen_clock
    ) -> None:  # noqa: ANN001
        db.add(IntegrationRun(
            flow_id=flow.id, status=RUN_RUNNING, thread_id="t", trigger_kind=TRIGGER_MANUAL,
        ))
        await db.commit()
        await make_trigger(db, flow, overlap_policy=OVERLAP_QUEUE)

        assert len(await scheduler.tick()) == 1, "one in flight is under the limit"

    async def test_queue_degrades_to_skip_at_the_limit(
        self, db: AsyncSession, flow: IntegrationFlow, version, frozen_clock
    ) -> None:  # noqa: ANN001
        """
        Bounded, then it stops. An unbounded queue for a sync that takes longer than its
        interval grows forever, and the first anybody hears of it is a thousand pending
        runs.
        """
        for _ in range(OVERLAP_QUEUE_LIMIT):
            db.add(IntegrationRun(
                flow_id=flow.id, status=RUN_QUEUED, thread_id="t",
                trigger_kind=TRIGGER_MANUAL,
            ))
        await db.commit()
        await make_trigger(db, flow, overlap_policy=OVERLAP_QUEUE)

        assert await scheduler.tick() == []
        assert await run_count(db, status=RUN_SKIPPED) == 1

    async def test_cancel_previous_stops_what_is_running_and_fires(
        self, db: AsyncSession, flow: IntegrationFlow, version, frozen_clock
    ) -> None:  # noqa: ANN001
        previous = IntegrationRun(
            flow_id=flow.id, status=RUN_RUNNING, thread_id="t", trigger_kind=TRIGGER_MANUAL,
        )
        db.add(previous)
        await db.commit()
        await make_trigger(db, flow, overlap_policy=OVERLAP_CANCEL_PREVIOUS)

        fired = await scheduler.tick()
        await db.refresh(previous)

        assert len(fired) == 1
        assert previous.cancel_requested is True


class TestTheLoopSurvives:
    async def test_one_broken_trigger_does_not_stop_the_tick(
        self, db: AsyncSession, flow: IntegrationFlow, version, frozen_clock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:  # noqa: ANN001
        """
        A scheduler that exited over one misconfigured flow would take the feature down
        silently, and nobody is watching at three in the morning to notice that nothing
        fired.
        """
        first = await make_trigger(db, flow, due_at=NOON - timedelta(minutes=2))
        second = await make_trigger(db, flow, due_at=NOON - timedelta(minutes=1))

        original = scheduler._fire
        calls: list = []

        async def explode_once(session, trigger, moment):  # noqa: ANN001, ANN202
            calls.append(trigger.id)
            if trigger.id == first.id:
                raise RuntimeError("this one is broken")
            return await original(session, trigger, moment)

        monkeypatch.setattr(scheduler, "_fire", explode_once)

        fired = await scheduler.tick()

        assert len(calls) == 2, "the tick carried on to the second trigger"
        assert len(fired) == 1

    async def test_a_broken_trigger_still_has_its_clock_advanced(
        self, db: AsyncSession, flow: IntegrationFlow, version, frozen_clock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:  # noqa: ANN001
        """
        Otherwise it is due again on the next tick, and again — a scheduler spinning on
        one broken workflow every fifteen seconds forever.
        """
        trigger = await make_trigger(db, flow, due_at=NOON - timedelta(minutes=1))

        async def explode(session, trigger_row, moment):  # noqa: ANN001, ANN202
            raise RuntimeError("broken")

        monkeypatch.setattr(scheduler, "_fire", explode)

        await scheduler.tick()
        await db.refresh(trigger)

        assert _aware(trigger.next_run_at) > NOON


class TestLifecycle:
    async def test_starting_twice_does_not_produce_two_schedulers(self) -> None:
        first = scheduler.start_scheduler()
        second = scheduler.start_scheduler()

        assert first is second
        await scheduler.stop_scheduler()

    async def test_stopping_is_not_a_failure(self) -> None:
        scheduler.start_scheduler()
        await scheduler.stop_scheduler()

        assert scheduler.is_running() is False
