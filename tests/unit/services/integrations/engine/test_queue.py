"""
Tests for ``engine/queue.py`` and the claim under it.

**The correlated ``NOT EXISTS`` is what the file is really about.** Without it,
``overlap_policy = queue`` means nothing: a second run of the same workflow would be
claimed the moment it was enqueued, and two concurrent syncs would hit the same
destination. It is the subtlest query in the module and it gets its own class.

The rest:

**A claim is committed.** One that is not is not a claim — the lock would be released when
the session closed and the job would look available again while a worker was on it.

**A stale worker fails its run rather than resuming it.** The downloader requeues an
export because nothing outside the application has seen a part file. A sync is not that:
the dead worker may have written four hundred records into somebody's CRM.

**A ``partial`` run is a succeeded job.** Some records did not move, and the job that
carried it did its work; a red badge on the queue for something the queue got right sends
somebody looking in the wrong place.

**The loop outlives any single failure.** A worker that exited because one sync failed
would take the feature down silently until somebody restarted the application.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.integrations.queries import (
    claim_next_run_job,
    queued_run_job_count,
    requeue_stale_run_jobs,
    runs_in_flight,
)
from app.models.integrations import (
    JOB_FAILED,
    JOB_QUEUED,
    JOB_RUNNING,
    JOB_SUCCEEDED,
    RUN_CANCELLED,
    RUN_FAILED,
    RUN_PARTIAL,
    RUN_QUEUED,
    RUN_RUNNING,
    RUN_SUCCEEDED,
    TRIGGER_MANUAL,
    IntegrationFlow,
    IntegrationRun,
    IntegrationRunJob,
)
from app.models.user.user import User
from app.services.integrations.engine import queue, run_service


@pytest.fixture
async def flow(db: AsyncSession, user: User) -> IntegrationFlow:
    row = IntegrationFlow(user_id=user.id, name="Sync", graph_data={}, is_active=True)
    db.add(row)
    await db.commit()
    return row


@pytest.fixture
async def other_flow(db: AsyncSession, user: User) -> IntegrationFlow:
    row = IntegrationFlow(user_id=user.id, name="Other", graph_data={}, is_active=True)
    db.add(row)
    await db.commit()
    return row


async def queued(db: AsyncSession, flow: IntegrationFlow, **fields) -> IntegrationRunJob:
    """A run and its job, committed together the way a caller would."""
    run = await run_service.begin_run(db, flow, trigger_kind=TRIGGER_MANUAL)
    job = await queue.enqueue(db, run, **fields)
    await db.commit()
    return job


async def running(db: AsyncSession, flow: IntegrationFlow) -> IntegrationRunJob:
    """A run of this flow already in flight, in some worker."""
    job = await queued(db, flow)
    job.status = JOB_RUNNING
    run = await db.get(IntegrationRun, job.run_id)
    run.status = RUN_RUNNING
    await db.commit()
    return job


async def job_count(db: AsyncSession, **filters) -> int:
    statement = select(func.count()).select_from(IntegrationRunJob)
    for column, value in filters.items():
        statement = statement.where(getattr(IntegrationRunJob, column) == value)
    result = await db.execute(statement)
    return int(result.scalar_one())


# ---------------------------------------------------------------------------
# Enqueue
# ---------------------------------------------------------------------------


class TestEnqueue:
    async def test_the_run_and_the_job_land_together(
        self, db: AsyncSession, flow: IntegrationFlow
    ) -> None:
        """
        One transaction. A crash between two commits either loses a run or leaves a job
        pointing at nothing.
        """
        run = await run_service.begin_run(db, flow)
        await queue.enqueue(db, run)
        await db.rollback()

        assert await job_count(db) == 0
        runs = await db.execute(select(func.count()).select_from(IntegrationRun))
        assert int(runs.scalar_one()) == 0

    async def test_a_priority_puts_a_watched_run_first(
        self, db: AsyncSession, flow: IntegrationFlow, other_flow: IntegrationFlow
    ) -> None:
        """A run somebody is sitting and watching should not wait behind a nightly
        backfill."""
        await queued(db, flow)
        urgent = await queued(db, other_flow, priority=10)

        claimed = await claim_next_run_job(db, claimed_by="w1")
        assert claimed.id == urgent.id

    async def test_within_one_priority_the_oldest_goes_first(
        self, db: AsyncSession, flow: IntegrationFlow, other_flow: IntegrationFlow
    ) -> None:
        """So a queue under load stays fair rather than starving whoever asked first."""
        first = await queued(db, flow)
        await queued(db, other_flow)

        claimed = await claim_next_run_job(db, claimed_by="w1")
        assert claimed.id == first.id


# ---------------------------------------------------------------------------
# The claim
# ---------------------------------------------------------------------------


class TestPerFlowSerialisation:
    """The correlated ``NOT EXISTS``. See the module docstring."""

    async def test_a_flow_with_a_run_in_flight_is_not_claimed_again(
        self, db: AsyncSession, flow: IntegrationFlow
    ) -> None:
        """
        Without this, ``overlap_policy = queue`` means nothing: the second run would be
        claimed the moment it was enqueued and two concurrent syncs would hit the same
        destination.
        """
        await running(db, flow)
        await queued(db, flow)

        assert await claim_next_run_job(db, claimed_by="w2") is None

    async def test_a_different_flow_is_still_claimable(
        self, db: AsyncSession, flow: IntegrationFlow, other_flow: IntegrationFlow
    ) -> None:
        """
        The check is per flow, not global. A sync is HTTP-bound against many different
        destinations, and blocking every workflow because one is busy would make the
        queue useless.
        """
        await running(db, flow)
        waiting = await queued(db, other_flow)

        claimed = await claim_next_run_job(db, claimed_by="w2")
        assert claimed is not None
        assert claimed.id == waiting.id

    async def test_the_block_lifts_when_the_run_finishes(
        self, db: AsyncSession, flow: IntegrationFlow
    ) -> None:
        busy = await running(db, flow)
        waiting = await queued(db, flow)

        assert await claim_next_run_job(db, claimed_by="w2") is None

        busy.status = JOB_SUCCEEDED
        await db.commit()

        claimed = await claim_next_run_job(db, claimed_by="w2")
        assert claimed.id == waiting.id


class TestClaiming:
    async def test_a_claim_flips_the_status_and_commits(
        self, db: AsyncSession, flow: IntegrationFlow
    ) -> None:
        """
        A claim that is not committed is not a claim: the lock would be released when the
        session closed and the job would look available again while a worker was on it.
        """
        await queued(db, flow)

        job = await claim_next_run_job(db, claimed_by="worker-a")

        assert job.status == JOB_RUNNING
        assert job.attempts == 1
        assert job.claimed_by == "worker-a"
        assert job.heartbeat_at is not None

    async def test_a_claimed_job_cannot_be_claimed_again(
        self, db: AsyncSession, flow: IntegrationFlow
    ) -> None:
        await queued(db, flow)

        assert await claim_next_run_job(db, claimed_by="w1") is not None
        assert await claim_next_run_job(db, claimed_by="w2") is None

    async def test_an_empty_queue_gives_nothing(self, db: AsyncSession) -> None:
        assert await claim_next_run_job(db, claimed_by="w1") is None

    async def test_a_job_not_yet_available_is_not_claimed(
        self, db: AsyncSession, flow: IntegrationFlow
    ) -> None:
        """``available_at`` is what makes a delayed retry expressible without a second
        mechanism — a job to try again in thirty seconds is one whose date moved."""
        job = await queued(db, flow)
        job.available_at = datetime.now(timezone.utc) + timedelta(minutes=5)
        await db.commit()

        assert await claim_next_run_job(db, claimed_by="w1") is None


class TestStaleWorkers:
    async def test_a_stale_job_fails_its_run_rather_than_requeueing_it(
        self, db: AsyncSession, flow: IntegrationFlow
    ) -> None:
        """
        The departure from the downloader, and the reason for it: that worker requeues an
        export because nothing outside the application has seen a part file. A sync is not
        that — the dead worker may have written four hundred records into somebody's CRM,
        and starting again would write them twice.
        """
        job = await running(db, flow)
        job.heartbeat_at = datetime.now(timezone.utc) - timedelta(hours=1)
        await db.commit()

        failed = await requeue_stale_run_jobs(db, stale_after_seconds=60)
        await db.refresh(job)
        run = await db.get(IntegrationRun, job.run_id)
        await db.refresh(run)

        assert failed == [job.id]
        assert job.status == JOB_FAILED
        assert run.status == RUN_FAILED
        assert "stopped responding" in run.error_message
        assert "Replay" in run.error_message, (
            "the operator needs to be told what to do, not only what happened"
        )

    async def test_a_live_job_is_left_alone(
        self, db: AsyncSession, flow: IntegrationFlow
    ) -> None:
        await running(db, flow)
        assert await requeue_stale_run_jobs(db, stale_after_seconds=60) == []

    async def test_attempts_are_not_reset(
        self, db: AsyncSession, flow: IntegrationFlow
    ) -> None:
        """A run that keeps killing its worker is visible as such rather than looping
        silently forever."""
        await queued(db, flow)
        job = await claim_next_run_job(db, claimed_by="w1")
        job.heartbeat_at = datetime.now(timezone.utc) - timedelta(hours=1)
        await db.commit()

        await requeue_stale_run_jobs(db, stale_after_seconds=60)
        await db.refresh(job)

        assert job.attempts == 1


# ---------------------------------------------------------------------------
# The worker
# ---------------------------------------------------------------------------


class TestDraining:
    async def test_a_queued_run_is_claimed_and_executed(
        self, db: AsyncSession, flow: IntegrationFlow, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        job = await queued(db, flow)
        executed: list = []

        async def fake_run(run_id, *, attempt=1):  # noqa: ANN001, ANN202
            executed.append((run_id, attempt))
            return RUN_SUCCEEDED

        monkeypatch.setattr(run_service, "execute_run", fake_run)

        assert await queue.drain_once() is True
        await db.refresh(job)

        assert executed == [(job.run_id, 1)]
        assert job.status == JOB_SUCCEEDED

    async def test_an_empty_queue_is_not_work(self) -> None:
        assert await queue.drain_once() is False

    @pytest.mark.parametrize(
        "run_status,expected",
        [
            (RUN_SUCCEEDED, JOB_SUCCEEDED),
            (RUN_PARTIAL, JOB_SUCCEEDED),
            (RUN_FAILED, JOB_FAILED),
            (RUN_CANCELLED, JOB_FAILED),
        ],
    )
    async def test_the_job_status_is_not_the_run_status(
        self, db: AsyncSession, flow: IntegrationFlow, monkeypatch: pytest.MonkeyPatch,
        run_status, expected,
    ) -> None:  # noqa: ANN001
        """
        ``partial`` is a run outcome — some records did not move — and the job that carried
        it did its work. A red badge on the queue for something the queue got right sends
        somebody looking in the wrong place.
        """
        job = await queued(db, flow)

        async def fake_run(run_id, *, attempt=1):  # noqa: ANN001, ANN202
            return run_status

        monkeypatch.setattr(run_service, "execute_run", fake_run)

        await queue.drain_once()
        await db.refresh(job)

        assert job.status == expected

    async def test_a_fault_outside_the_run_fails_the_job_not_the_worker(
        self, db: AsyncSession, flow: IntegrationFlow, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``execute_run`` records its own failures and returns a status, so anything that
        reaches here is the queue's own bookkeeping going wrong."""
        job = await queued(db, flow)

        async def explode(run_id, *, attempt=1):  # noqa: ANN001, ANN202
            raise RuntimeError("the bookkeeping broke")

        monkeypatch.setattr(run_service, "execute_run", explode)

        await queue.drain_once()
        await db.refresh(job)

        assert job.status == JOB_FAILED
        assert "bookkeeping" in job.error_message


class TestTheLoopSurvives:
    async def test_a_failure_in_the_loop_does_not_stop_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        A worker that exited because one sync failed would take the feature down silently
        until somebody restarted the application — and nobody is watching at three.
        """
        calls: list = []

        async def flaky() -> bool:
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("the database went away")
            return False

        monkeypatch.setattr(queue, "drain_once", flaky)
        monkeypatch.setattr(queue, "LOOP_ERROR_BACKOFF_SECONDS", 0.01)
        monkeypatch.setattr(queue, "POLL_INTERVAL_SECONDS", 0.01)

        task = asyncio.create_task(queue.run_worker())
        await asyncio.sleep(0.15)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert len(calls) > 1, "the loop carried on past the failure"


class TestLifecycle:
    @pytest.fixture(autouse=True)
    def idle_workers(self, monkeypatch: pytest.MonkeyPatch):  # noqa: ANN001, ANN201
        """
        Workers that find nothing, without touching the database.

        These tests are about starting and stopping, not about draining — and letting the
        real ``drain_once`` run would have two tasks polling the one connection the
        in-memory SQLite database shares through ``StaticPool``. Cancelling a task mid-query
        there hangs aiosqlite's worker thread, which is a fact about the test database and
        has nothing to say about the lifecycle these tests are for.
        """
        async def nothing() -> bool:
            return False

        monkeypatch.setattr(queue, "drain_once", nothing)
        monkeypatch.setattr(queue, "POLL_INTERVAL_SECONDS", 0.05)

    async def test_starting_twice_does_not_produce_two_sets_of_workers(self) -> None:
        first = queue.start_workers(2)
        second = queue.start_workers(2)

        # The same tasks, not a second set. Asserted on identity of the members rather
        # than of the list, which is a fresh object either way — and it is the tasks that
        # would be competing for the same jobs.
        assert [id(task) for task in first] == [id(task) for task in second]
        assert queue.live_worker_count() == 2

        await queue.stop_workers()

    async def test_stopping_leaves_nothing_running(self) -> None:
        queue.start_workers(2)
        await asyncio.sleep(0)
        await queue.stop_workers()

        assert queue.live_worker_count() == 0

    async def test_a_wakeup_shortcuts_the_poll(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        What makes a manual run start immediately rather than at the next poll — and why
        the poll still exists, for a job enqueued by another process.
        """
        monkeypatch.setattr(queue, "POLL_INTERVAL_SECONDS", 30.0)
        queue.wake()

        await asyncio.wait_for(queue._wait_for_work(), timeout=1.0)

    async def test_the_flag_survives_a_busy_worker(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cleared *after* the wait, not before: a job enqueued while a worker was busy
        must not be missed."""
        monkeypatch.setattr(queue, "POLL_INTERVAL_SECONDS", 30.0)

        queue.wake()
        await asyncio.wait_for(queue._wait_for_work(), timeout=1.0)

        # The flag is spent; the next wait falls through to the poll.
        assert queue._wakeup.is_set() is False


class TestCounts:
    async def test_the_depth_is_a_real_count(
        self, db: AsyncSession, flow: IntegrationFlow, other_flow: IntegrationFlow
    ) -> None:
        await queued(db, flow)
        await queued(db, other_flow)

        assert await queued_run_job_count(db) == 2

    async def test_runs_in_flight_counts_queued_and_running(
        self, db: AsyncSession, flow: IntegrationFlow
    ) -> None:
        """The scheduler's overlap check. Counted rather than existence-tested because
        ``overlap_policy = queue`` is bounded."""
        await queued(db, flow)
        await running(db, flow)

        assert await runs_in_flight(db, flow.id) == 2
