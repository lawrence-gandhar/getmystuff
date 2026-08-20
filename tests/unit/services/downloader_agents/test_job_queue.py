"""
Tests for base/job_queue.py and app/db/downloader_agents/queries.py.

The queue's job is to be boring, and these tests assert the ways it could fail to be:

* a claimed job is not claimable again — otherwise two workers build the same file, and
  the second one's parts land in the first one's directory;
* the worker's loop survives one export failing, because a worker that exits takes the
  whole feature down silently until somebody restarts the application;
* a job whose worker died is picked up again rather than sitting `running` forever.

``FOR UPDATE SKIP LOCKED`` cannot be *proved* on SQLite — the dialect has no locking
clause, so SQLAlchemy drops it and these tests exercise a plain SELECT. That is stated
rather than glossed over: what is tested here is the claim's bookkeeping, and the locking
itself is a PostgreSQL guarantee that only a concurrent PostgreSQL test could demonstrate.
"""

from __future__ import annotations

from typing import Callable

import pytest

from app.db.downloader_agents.queries import (
    claim_next_job,
    heartbeat,
    queued_job_count,
    requeue_stale_jobs,
)
from app.models.downloader_agents import (
    EXPORT_QUEUED,
    JOB_FAILED,
    JOB_QUEUED,
    JOB_RUNNING,
    JOB_SUCCEEDED,
)
from app.services.downloader_agents.base import download_service as svc
from app.services.downloader_agents.base import job_queue


@pytest.fixture
def make_queued_export(db, make_export_fixtures: Callable) -> Callable:  # noqa: ANN001
    """An export that has been confirmed and queued, plus its job."""

    async def _make(rows: int = 60):  # noqa: ANN202
        agent, tool = await make_export_fixtures(rows=rows)
        export = await svc.create_offer(db, agent.id, tool.id, total_rows=rows)
        await svc.mark_queued(db, export, "csv")
        job = await job_queue.enqueue_export(db, export)
        return export, job

    return _make


# ---- Claiming ----

class TestClaiming:
    async def test_an_empty_queue_claims_nothing(self, db) -> None:  # noqa: ANN001
        assert await claim_next_job(db, claimed_by="worker-1") is None

    async def test_claiming_marks_the_job_running_and_records_who(
        self, db, make_queued_export: Callable,
    ) -> None:
        _export, job = await make_queued_export()

        claimed = await claim_next_job(db, claimed_by="worker-1")

        assert claimed is not None
        assert claimed.id == job.id
        assert claimed.status == JOB_RUNNING
        assert claimed.attempts == 1
        assert claimed.claimed_by == "worker-1"
        assert claimed.claimed_at is not None
        assert claimed.heartbeat_at is not None

    async def test_a_claimed_job_cannot_be_claimed_again(
        self, db, make_queued_export: Callable,
    ) -> None:
        """
        The whole point of the claim. Two workers on one export would build one file
        twice, into the same directory, with each other's parts.
        """
        await make_queued_export()

        assert await claim_next_job(db, claimed_by="worker-1") is not None
        assert await claim_next_job(db, claimed_by="worker-2") is None

    async def test_the_oldest_job_is_claimed_first(
        self, db, make_queued_export: Callable,
    ) -> None:
        """Fair rather than arbitrary: a queue under load must not starve the first ask."""
        _first_export, first = await make_queued_export()
        _second_export, second = await make_queued_export()

        assert (await claim_next_job(db, claimed_by="w")).id == first.id
        assert (await claim_next_job(db, claimed_by="w")).id == second.id

    async def test_the_queue_depth_counts_only_waiting_jobs(
        self, db, make_queued_export: Callable,
    ) -> None:
        await make_queued_export()
        await make_queued_export()

        assert await queued_job_count(db) == 2

        await claim_next_job(db, claimed_by="w")

        assert await queued_job_count(db) == 1


# ---- Heartbeats and recovery ----

class TestRecovery:
    async def test_a_heartbeat_moves_forward(
        self, db, make_queued_export: Callable,
    ) -> None:
        await make_queued_export()
        job = await claim_next_job(db, claimed_by="w")
        first_beat = job.heartbeat_at

        await heartbeat(db, job.id)
        await db.refresh(job)

        assert job.heartbeat_at >= first_beat

    async def test_a_stale_job_is_requeued_with_its_export(
        self, db, make_queued_export: Callable,
    ) -> None:
        """
        A worker that died holds nothing — its transaction is gone — but the row still
        says `running` and nothing would ever pick it up again.

        The export goes back to `queued` too: the next worker starts the build from the
        confirmation, and an export left saying `building` would be reported to the user
        as in progress forever.
        """
        export, _job = await make_queued_export()
        job = await claim_next_job(db, claimed_by="dead-worker")
        await svc.mark_building(db, export)

        # A threshold of zero seconds makes the job just claimed count as stale, which is
        # what lets this be asserted without sleeping.
        requeued = await requeue_stale_jobs(db, stale_after_seconds=0)

        assert requeued == [job.id]

        await db.refresh(job)
        await db.refresh(export)
        assert job.status == JOB_QUEUED
        assert job.claimed_by is None
        assert job.attempts == 1        # not reset: a job that keeps killing workers shows
        assert export.status == EXPORT_QUEUED

    async def test_a_healthy_job_is_left_alone(
        self, db, make_queued_export: Callable,
    ) -> None:
        await make_queued_export()
        job = await claim_next_job(db, claimed_by="w")

        assert await requeue_stale_jobs(db, stale_after_seconds=3600) == []

        await db.refresh(job)
        assert job.status == JOB_RUNNING

    async def test_a_requeued_job_can_be_claimed_again(
        self, db, make_queued_export: Callable,
    ) -> None:
        """Otherwise the recovery would move the row and change nothing."""
        await make_queued_export()
        await claim_next_job(db, claimed_by="dead-worker")
        await requeue_stale_jobs(db, stale_after_seconds=0)

        claimed = await claim_next_job(db, claimed_by="live-worker")

        assert claimed is not None
        assert claimed.attempts == 2


# ---- The worker ----

class TestDrainOnce:
    async def test_it_reports_nothing_to_do_on_an_empty_queue(
        self, db, graph_sessions, graph_checkpointer,  # noqa: ANN001
    ) -> None:
        assert await job_queue.drain_once() is False

    async def test_it_builds_a_queued_export(
        self, db, make_queued_export: Callable, upload_root,  # noqa: ANN001
        graph_sessions, graph_checkpointer, monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
    ) -> None:
        """
        The whole worker path in one test: claim, resume the paused graph, build, finish.

        The export is taken to its confirmation interrupt first, because that is the state
        a real queued job is always in — the offer happened in a request that has ended.
        """
        from app.services.downloader_agents.base import download_graph

        monkeypatch.setattr(download_graph, "_graph", None)

        export, job = await make_queued_export(rows=60)
        await download_graph.start_export_offer(str(export.uuid), "csv")

        assert await job_queue.drain_once() is True

        await db.refresh(export)
        await db.refresh(job)
        assert export.rows_written == 60
        assert export.part_count == 2
        assert job.status == JOB_SUCCEEDED
        assert job.finished_at is not None

    async def test_a_failing_export_fails_its_job_without_raising(
        self, db, make_queued_export: Callable, upload_root,  # noqa: ANN001
        graph_sessions, graph_checkpointer, monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
    ) -> None:
        """
        A job that raised must close out as failed rather than propagating.

        ``drain_once`` propagating would reach ``run_worker``'s backoff and the job would
        sit `running` until the stale reaper found it — a slow, confusing route to the
        same place.
        """
        from app.services.downloader_agents.base import download_graph

        monkeypatch.setattr(download_graph, "_graph", None)

        export, job = await make_queued_export(rows=60)
        await download_graph.start_export_offer(str(export.uuid), "csv")

        async def explodes(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            raise RuntimeError("the graph fell over")

        monkeypatch.setattr(download_graph, "resume_export", explodes)

        assert await job_queue.drain_once() is True

        await db.refresh(job)
        await db.refresh(export)
        assert job.status == JOB_FAILED
        assert "fell over" in job.error_message
        # Marked from the worker, because the graph's own notify node never ran.
        assert export.error_message == svc.FAILURE_MESSAGE


class TestWorkerLifecycle:
    async def test_starting_twice_produces_one_worker(self) -> None:
        """Two workers in one process would compete for the same jobs for no benefit."""
        try:
            first = job_queue.start_worker()
            assert job_queue.start_worker() is first
        finally:
            await job_queue.stop_worker()

    async def test_stopping_an_unstarted_worker_is_a_no_op(self) -> None:
        """``on_shutdown`` runs whether or not startup got that far."""
        await job_queue.stop_worker()

    async def test_the_worker_name_identifies_the_process(self) -> None:
        assert ":" in job_queue.worker_name()
