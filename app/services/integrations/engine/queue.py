"""
The run queue, and the worker that drains it.

**A table, not a broker.** There is no Redis, no Celery and no arq in this project, and
this does not add one. An ``integration_run_jobs`` row claimed with ``FOR UPDATE SKIP
LOCKED`` is a queue that is durable across restarts, safe across processes, and visible in
the same database as everything it is about. What a broker would add is throughput this
feature will not need and a service to operate that it would not justify.

**A manual run goes through the same queue.** Pressing Run inserts a job exactly as a
schedule does, and an ``asyncio.Event`` wakes the worker so it starts immediately rather
than at the next poll. That is what makes the run somebody tested at eleven in the morning
the same code path as the one that fires at three — and there is no second execution path
to keep in step.

**Two at a time, not one.** The downloader deliberately drains one export at a time
because an export holds a cursor open against the user's own database for its whole run. A
sync is HTTP-bound against many different destinations, and what protects any single one
is per-flow serialisation rather than a global limit. So the default is
``INTEGRATION_WORKER_CONCURRENCY = 2``, and the thing that actually bounds pressure on a
vendor is the claim.

**Per-flow serialisation is in the claim, not in the worker.** A correlated ``NOT EXISTS``
refuses a job whose flow already has a run in flight anywhere. Checking it in the worker
would be a check-then-act with two workers in the window, which is precisely when it
matters — and ``overlap_policy = queue`` would mean nothing.

**A dead worker fails its run rather than resuming it.** ``requeue_stale_run_jobs`` says
why at length: the dead worker may have written four hundred records into somebody's CRM,
and starting again would write them twice. The operator gets a run with a reason and a
Replay button, and Replay goes through the sync keys.

**Every failure inside one job is that job's failure.** The loop itself only ever stops on
cancellation. A worker that exited because one sync failed would take the feature down
silently until somebody restarted the application — and nobody is watching at three.
"""

import asyncio
import logging
import os
import socket
from typing import List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.db_utils import CRUDQueryBuilder
from app.db.integrations.queries import (
    claim_next_run_job,
    finish_run_job,
    job_heartbeat,
    queued_run_job_count,
    requeue_stale_run_jobs,
)
from app.models.integrations import (
    JOB_FAILED,
    JOB_QUEUED,
    JOB_SUCCEEDED,
    RUN_CANCELLED,
    RUN_FAILED,
    IntegrationRun,
    IntegrationRunJob,
)
from app.services.integrations.engine import run_service, run_store

logger = logging.getLogger(__name__)

job_crud = CRUDQueryBuilder(IntegrationRunJob)

#: How many runs one process drains at once. See the module docstring on why two rather
#: than the downloader's one.
WORKER_CONCURRENCY = int(os.getenv("INTEGRATION_WORKER_CONCURRENCY", "2"))

#: How long a worker waits when the queue is empty. Long, because the ``asyncio.Event``
#: below is what makes a manual run start immediately — the poll only has to catch a job
#: inserted by *another* process.
POLL_INTERVAL_SECONDS = float(os.getenv("INTEGRATION_WORKER_POLL_SECONDS", "5"))

#: How often a running job's heartbeat is written.
HEARTBEAT_INTERVAL_SECONDS = float(
    os.getenv("INTEGRATION_WORKER_HEARTBEAT_SECONDS", "10")
)

#: How quiet a running job must go before its worker is assumed dead. Six times the
#: heartbeat, so a worker briefly starved of the event loop — a large batch holding a
#: thread — is not declared dead while it is still working.
STALE_AFTER_SECONDS = float(
    os.getenv(
        "INTEGRATION_WORKER_STALE_SECONDS", str(HEARTBEAT_INTERVAL_SECONDS * 6)
    )
)

#: How long a worker waits after an unexpected failure in its own loop, as opposed to a
#: failure inside one run. Longer than the poll: whatever broke is probably the database,
#: and hammering it every five seconds does not help.
LOOP_ERROR_BACKOFF_SECONDS = 30.0

_workers: List[asyncio.Task] = []

# Set when a job is enqueued in this process, so a manual run does not wait for the poll.
# A plain flag rather than a queue of ids: the worker's job is to look, and what it finds
# is whatever the claim gives it — which may be a different job entirely, and should be.
_wakeup = asyncio.Event()


def worker_name() -> str:
    """Who claimed a job, for reading a log by. The hostname is the container id under
    Docker. Not used for any decision — the claim is done by row locking."""
    return f"{socket.gethostname()}:{os.getpid()}"


# ---------------------------------------------------------------------------
# Enqueue
# ---------------------------------------------------------------------------


async def enqueue(
    db: AsyncSession, run: IntegrationRun, *, priority: int = 0
) -> IntegrationRunJob:
    """
    Put one run on the queue, in the caller's transaction.

    ``create_pending``, not ``create``: the job and the run row — and, for a scheduled
    run, the trigger's advanced ``next_run_at`` — have to land together or not at all. A
    crash between two commits either loses a run or leaves a job pointing at nothing.

    The caller commits. :func:`wake` is what tells this process to look, and it is
    deliberately a separate call so it can happen *after* the commit — waking a worker
    before the transaction lands is how it looks for a job that is not there yet.
    """
    return await job_crud.create_pending(
        db, {"run_id": run.id, "status": JOB_QUEUED, "priority": priority}
    )


def wake() -> None:
    """
    Tell this process's workers to look now rather than at the next poll.

    Only helps in the process that enqueued, which is the single-replica case and the one
    somebody is watching. Everywhere else the poll is the mechanism, and that is why the
    poll still exists rather than being replaced by this.
    """
    _wakeup.set()


# ---------------------------------------------------------------------------
# The worker
# ---------------------------------------------------------------------------


def start_workers(count: Optional[int] = None) -> List[asyncio.Task]:
    """
    Start this process's workers, if they are not already running.

    Idempotent: calling twice does not produce two sets competing for the same jobs.
    Returns the tasks so a caller can await them and a test can cancel them.
    """
    global _workers

    live = [task for task in _workers if not task.done()]
    if live:
        return live

    how_many = max(1, int(count if count is not None else WORKER_CONCURRENCY))
    _workers = [
        asyncio.create_task(run_worker(), name=f"integration-run-worker-{index}")
        for index in range(how_many)
    ]
    logger.info("Integration run workers started (%d) as %s", how_many, worker_name())
    return _workers


async def stop_workers() -> None:
    """
    Stop the workers and wait for them to unwind.

    Cancels rather than sets a flag, and swallows the ``CancelledError``: shutdown is not
    a failure. A run cancelled mid-flight leaves its job saying ``running``, which
    ``requeue_stale_run_jobs`` fails after ``STALE_AFTER_SECONDS`` — the same recovery a
    crash gets, which is the point of having only one.
    """
    global _workers

    tasks, _workers = _workers, []
    live = [task for task in tasks if not task.done()]

    for task in live:
        task.cancel()

    for task in live:
        try:
            await task
        except asyncio.CancelledError:
            pass

    if live:
        logger.info("Integration run workers stopped")


def live_worker_count() -> int:
    return len([task for task in _workers if not task.done()])


async def run_worker() -> None:
    """
    Claim runs and execute them, forever.

    Every failure inside one run is that run's failure and is recorded on it; the loop
    only stops on cancellation. See the module docstring.
    """
    while True:
        try:
            did_work = await drain_once()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — the loop must outlive any single failure
            logger.exception("The integration run worker hit an unexpected failure")
            await asyncio.sleep(LOOP_ERROR_BACKOFF_SECONDS)
            continue

        if not did_work:
            await _wait_for_work()


async def _wait_for_work() -> None:
    """
    Sleep until something is enqueued here, or until the poll comes round.

    The event is cleared *before* the wait rather than after, so a job enqueued while a
    worker was busy is not missed — the flag set during the last run is still there and
    the wait returns immediately.
    """
    try:
        await asyncio.wait_for(_wakeup.wait(), timeout=POLL_INTERVAL_SECONDS)
    except asyncio.TimeoutError:
        return
    finally:
        _wakeup.clear()


async def drain_once() -> bool:
    """
    Fail anything stale, then claim and execute one run. ``True`` if a run was executed.

    Split out of the loop so a test can drive exactly one iteration, and so "what the
    worker does" is readable without reading the loop that repeats it.
    """
    async with run_store.open_session() as db:
        await requeue_stale_run_jobs(db, int(STALE_AFTER_SECONDS))
        job = await claim_next_run_job(db, claimed_by=worker_name())

    if job is None:
        return False

    await _execute(job.id, job.run_id, job.attempts or 1)
    return True


async def _execute(job_id: int, run_id: int, attempt: int) -> None:
    """
    Run one job, with a heartbeat alongside it.

    The heartbeat is a separate task rather than something the run calls, because the
    interesting stall is one where the run is blocked — which is exactly when it would not
    get round to calling anything.

    ``run_service.execute_run`` does not raise: it returns the status it recorded. So the
    only thing that reaches the ``except`` here is a fault in the queue's own bookkeeping,
    and a cancellation, which is shutdown rather than an error.
    """
    beat = asyncio.create_task(_beat(job_id))

    try:
        status = await run_service.execute_run(run_id, attempt=attempt)
    except asyncio.CancelledError:
        # Shutdown. The job stays `running` and is failed by the stale reaper — the same
        # recovery a crash gets, which is why there is only one.
        raise
    except Exception as exc:  # noqa: BLE001 — one bad job must not stop the worker
        logger.exception("Job %s failed outside the run", job_id)
        await _finish(job_id, JOB_FAILED, str(exc))
        return
    finally:
        beat.cancel()

    await _finish(job_id, _job_status_for(status), "")


def _job_status_for(run_status: str) -> str:
    """
    What the *job* ends as, given what the *run* ended as.

    Deliberately not the same value. ``partial`` is a run outcome — some records did not
    move — and the job that carried it did its work; calling it failed would put a red
    badge on the queue for something the queue got right. Only a run that failed outright
    or was cancelled marks its job as anything but succeeded.
    """
    if run_status == RUN_FAILED:
        return JOB_FAILED
    if run_status == RUN_CANCELLED:
        return JOB_FAILED
    return JOB_SUCCEEDED


async def _finish(job_id: int, status: str, message: str) -> None:
    try:
        async with run_store.open_session() as db:
            await finish_run_job(db, job_id, status, message)
    except Exception:  # noqa: BLE001 — the run is already recorded
        logger.exception("Could not close job %s", job_id)


async def _beat(job_id: int) -> None:
    """Write the job's heartbeat until cancelled. Swallows everything but cancellation:
    a heartbeat that raised would take down a run for a reason unrelated to it."""
    try:
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
            async with run_store.open_session() as db:
                await job_heartbeat(db, job_id)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        logger.exception("Heartbeat stopped for job %s", job_id)


async def depth(db: AsyncSession) -> int:
    """How many jobs are waiting. For the dashboard and for a shutdown log line."""
    return await queued_run_job_count(db)
