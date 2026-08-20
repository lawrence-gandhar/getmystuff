"""
The export queue, and the worker that drains it.

An export is not something a chat turn can wait for. A hundred thousand records is
thousands of batches, and the visitor's turn times out after two minutes — so the
confirmation enqueues the work and returns, and this module is what picks it up.

**A table, not a broker.** There is no Redis, no Celery and no arq in this project, and
this does not add one. A ``download_jobs`` row claimed with ``FOR UPDATE SKIP LOCKED``
(app/db/downloader_agents/queries.py) is a queue that is durable across restarts, safe
across processes, and visible in the same database as everything else it is about. What a
broker would add over that is throughput this feature will never need and a service to
operate that it would not justify.

**The worker is an asyncio task in the application process.** Started from ``on_startup``,
stopped from ``on_shutdown``. It is the shape ``db_utils.cleanup_idle_connections``
describes and — unlike those two — it is actually started. Running it in-process means a
deployment is one container, and it means the worker sees the same code the requests do;
running it separately would be a second image to build, tag and keep in step for a
feature whose work is I/O-bound anyway.

**One job at a time, deliberately.** An export holds a database cursor open against the
user's own database for its whole run. Draining two at once would double that against a
server this application does not own, to finish a background job sooner than anybody is
waiting for. The claim is concurrency-safe regardless, so raising this later is a
constant and not a rewrite.

**A dead worker is recovered, not resumed.** ``heartbeat_at`` is written while a job runs;
a job whose heartbeat has gone stale is requeued by :func:`requeue_stale_jobs`, and the
next worker starts the build again from the confirmation. See that function for why
starting again is the honest choice.
"""

import asyncio
import logging
import os
import socket
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.db_utils import CRUDQueryBuilder
from app.db.downloader_agents.queries import (
    claim_next_job,
    heartbeat,
    requeue_stale_jobs,
)
from app.models.downloader_agents import (
    JOB_FAILED,
    JOB_QUEUED,
    JOB_SUCCEEDED,
    DownloadExport,
    DownloadJob,
)
from app.services.downloader_agents.base import download_service as svc
from app.services.downloader_agents.base.download_service import (
    mark_building,
    mark_failed,
    require_export,
)

logger = logging.getLogger(__name__)

job_crud = CRUDQueryBuilder(DownloadJob)


# How long the worker waits when the queue is empty. Five seconds: an export is a
# background job nobody is watching to the second, and a shorter poll would be a query
# per second forever to learn that there is still nothing to do.
POLL_INTERVAL_SECONDS = float(os.getenv("DOWNLOAD_WORKER_POLL_SECONDS", "5"))

# How often the running job's heartbeat is written.
HEARTBEAT_INTERVAL_SECONDS = float(os.getenv("DOWNLOAD_WORKER_HEARTBEAT_SECONDS", "10"))

# How quiet a running job must go before it is assumed dead. Six times the heartbeat, so
# a worker briefly starved of the event loop — a big merge holding a thread — is not
# declared dead while it is still working.
STALE_AFTER_SECONDS = float(
    os.getenv("DOWNLOAD_WORKER_STALE_SECONDS", str(HEARTBEAT_INTERVAL_SECONDS * 6))
)

# How long the worker waits after an unexpected failure in its own loop, as opposed to a
# failure inside one export. Longer than the poll: whatever broke is probably the
# database, and hammering it every five seconds does not help.
_LOOP_ERROR_BACKOFF_SECONDS = 30.0

# The running worker task, if this process has one.
_worker_task: Optional[asyncio.Task] = None


def worker_name() -> str:
    """
    Who claimed a job, for reading a log by.

    The hostname is the container id under Docker, which is what identifies a replica.
    Not used for any decision — the claim is done by row locking, not by this string.
    """
    return f"{socket.gethostname()}:{os.getpid()}"


# --------------------------------------------------------------------------
# Enqueue
# --------------------------------------------------------------------------

async def enqueue_export(db: AsyncSession, export: DownloadExport) -> DownloadJob:
    """
    Put one confirmed export on the queue.

    Called from the request that handled the user's "yes", so it takes the caller's
    session — the job must be committed by the same transaction that moved the export to
    ``queued``, or a worker could claim a job whose export still says ``offered``.
    """
    job = await job_crud.create(
        db, {"export_id": export.id, "status": JOB_QUEUED},
    )

    logger.info("Queued export %s as job %s", export.uuid, job.uuid)

    return job


async def job_for_export(
    db: AsyncSession,
    export_id: int,
) -> Optional[DownloadJob]:
    """The most recent job for one export, if it has been queued at all."""
    jobs = await job_crud.get_many(
        db, filters={"export_id": export_id}, order_by="id", desc=True, limit=1,
    )

    return jobs[0] if jobs else None


# --------------------------------------------------------------------------
# The worker
# --------------------------------------------------------------------------

def start_worker() -> Optional[asyncio.Task]:
    """
    Start the queue worker for this process, if it is not already running.

    Returns the task so a caller can await it, and so a test can cancel it. Idempotent:
    calling twice does not produce two workers competing for the same jobs.
    """
    global _worker_task

    if _worker_task is not None and not _worker_task.done():
        return _worker_task

    _worker_task = asyncio.create_task(run_worker(), name="download-export-worker")
    logger.info("Export queue worker started as %s", worker_name())

    return _worker_task


async def stop_worker() -> None:
    """
    Stop the worker and wait for it to finish the batch it is on.

    Cancels rather than sets a flag, and swallows the ``CancelledError``: shutdown is not
    a failure. A job cancelled mid-export leaves its row saying ``running``, which the
    stale-job reaper requeues after :data:`STALE_AFTER_SECONDS` — the same recovery a
    crash gets, which is the point of having only one.
    """
    global _worker_task

    task, _worker_task = _worker_task, None

    if task is None or task.done():
        return

    task.cancel()

    try:
        await task
    except asyncio.CancelledError:
        pass

    logger.info("Export queue worker stopped")


async def run_worker() -> None:
    """
    Claim jobs and build them, forever.

    Every failure inside one job is that job's failure and is recorded on it; the loop
    itself only ever stops on cancellation. A worker that exited because one export
    failed would take the whole feature down with it, silently, until someone restarted
    the application.
    """
    while True:
        try:
            did_work = await drain_once()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — the loop must outlive any single failure
            logger.exception("The export queue worker hit an unexpected failure")
            await asyncio.sleep(_LOOP_ERROR_BACKOFF_SECONDS)
            continue

        if not did_work:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)


async def drain_once() -> bool:
    """
    Requeue anything stale, then claim and run one job. True if a job was run.

    Split out of the loop so a test can drive exactly one iteration, and so "what the
    worker does" is readable without reading the loop that repeats it.
    """
    async with svc.open_session() as db:
        await requeue_stale_jobs(db, int(STALE_AFTER_SECONDS))
        job = await claim_next_job(db, claimed_by=worker_name())

        if job is None:
            return False

        export = await require_export_by_id(db, job.export_id)
        export_uuid = str(export.uuid)
        file_format = export.file_format

        await mark_building(db, export)

    await _run_job(job.id, export_uuid, file_format)

    return True


async def require_export_by_id(db: AsyncSession, export_id: int) -> DownloadExport:
    """
    One export by its internal bigint id, or a raise.

    A job with no export cannot happen — the foreign key cascades — but it is resolved
    through a raising helper anyway, so that if it ever does the worker fails here with a
    clear message rather than on an attribute access three functions later.
    """
    export = await db.get(DownloadExport, export_id)

    if export is None:
        raise ValueError(f"Export id={export_id} no longer exists")

    return export


async def _run_job(job_id: int, export_uuid: str, file_format: str) -> None:
    """
    Resume the export's graph, with a heartbeat running alongside it.

    The heartbeat is a separate task rather than something the graph calls, because the
    graph must not know it is being watched — and because the interesting stall is one
    where the graph is blocked, which is exactly when it would not get round to calling
    anything.

    Imported here rather than at module scope: ``download_graph`` imports langgraph, and
    a process that never runs an export should not pay for that at startup.
    """
    from app.services.downloader_agents.base.download_graph import resume_export

    beat = asyncio.create_task(_heartbeat_loop(job_id), name=f"export-heartbeat-{job_id}")

    try:
        state = await resume_export(export_uuid, confirmed=True, file_format=file_format)
        failure = (state or {}).get("failure")
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — one job's failure, recorded as one
        logger.exception("Export %s failed while building", export_uuid)
        await _finish(job_id, export_uuid, failure=str(exc), record_on_export=True)
        return
    finally:
        beat.cancel()

    await _finish(job_id, export_uuid, failure=failure, record_on_export=False)


async def _heartbeat_loop(job_id: int) -> None:
    """Write this job's heartbeat until cancelled."""
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)

        try:
            async with svc.open_session() as db:
                await heartbeat(db, job_id)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            # A missed heartbeat is recoverable — the reaper's threshold is six of
            # them — and a heartbeat that killed the export it was reporting on would
            # not be.
            logger.warning("Could not write a heartbeat for job %s", job_id)


async def _finish(
    job_id: int,
    export_uuid: str,
    failure: Optional[str],
    record_on_export: bool,
) -> None:
    """
    Close the job out, and the export with it if the graph never got the chance.

    ``record_on_export`` is the distinction that matters. The graph's own notify node
    already marks the export failed for every failure it handles, and overwriting that
    would replace a specific message (the too-large refusal) with the generic one. It is
    only when the graph *raised* — so its notify node never ran — that the export needs
    marking from out here.
    """
    async with svc.open_session() as db:
        job = await db.get(DownloadJob, job_id)

        if job is not None:
            job.status = JOB_FAILED if failure else JOB_SUCCEEDED
            job.error_message = str(failure)[:2000] if failure else None
            job.finished_at = datetime.now(timezone.utc)
            await db.commit()

        if failure and record_on_export:
            try:
                export = await require_export(db, export_uuid)
            except Exception:  # noqa: BLE001 — a deleted export is not a worker failure
                logger.warning(
                    "Export %s could not be marked failed; it may have been deleted",
                    export_uuid,
                )
            else:
                await mark_failed(db, export, reason=str(failure))

    logger.info(
        "Export %s finished as %s",
        export_uuid,
        "failed" if failure else "ready",
    )


def worker_task() -> Optional[Any]:
    """The running worker task, for tests and for a health check to look at."""
    return _worker_task
