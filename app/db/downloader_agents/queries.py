"""
Queue queries for Downloader Agents that ``CRUDQueryBuilder`` cannot express.

One reason this module exists, and it is the claim: taking the next job off the queue has
to be atomic against every other worker doing the same thing, and that is
``SELECT … FOR UPDATE SKIP LOCKED`` — a row lock with an instruction about what to do
when it is already held. There is no generic-CRUD spelling of that, and putting it in the
shared ``db_utils`` would be putting one feature's concurrency rule in a model-agnostic
module. Same reasoning as app/db/workspaces/queries.py.

**How the claim is safe.** The select locks the row it returns and skips rows other
transactions already hold, so two workers running the identical statement at the same
moment get two *different* jobs — or one gets a job and the other gets nothing. Neither
waits, and neither can get the same job as the other. The status is then flipped to
``running`` inside the same transaction, so by the time the lock is released the row no
longer matches the ``queued`` filter and cannot be claimed again.

**On SQLite this compiles to a plain SELECT.** SQLAlchemy drops the locking clause for a
dialect that has none, which is exactly what the test suite needs: the same code path,
one connection, no concurrency to protect against. Worth knowing rather than discovering
— a test cannot prove the locking works, only that the claim does.

**Stale jobs.** :func:`requeue_stale_jobs` is the other half of claiming. A worker that
dies holds nothing — its transaction is gone, so the row is unlocked — but the row still
says ``running`` and nothing will ever pick it up again. Heartbeats are what make that
detectable, and this is what acts on it.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.downloader_agents import (
    EXPORT_QUEUED,
    JOB_QUEUED,
    JOB_RUNNING,
    DownloadExport,
    DownloadJob,
)

logger = logging.getLogger(__name__)


async def claim_next_job(
    db: AsyncSession,
    claimed_by: str,
) -> Optional[DownloadJob]:
    """
    Take the oldest queued job, or return None if there is nothing to do.

    Oldest first, so a queue under load stays fair rather than starving whoever asked
    first. The row is locked and the status flipped in one transaction — see the module
    docstring for why that is what makes two workers safe.

    Commits. A claim that is not committed is not a claim: the lock would be released
    when the session was closed and the job would go back to looking available while a
    worker was already building it.
    """
    statement = (
        select(DownloadJob)
        .where(DownloadJob.status == JOB_QUEUED)
        .order_by(DownloadJob.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )

    job = (await db.execute(statement)).scalars().first()

    if job is None:
        return None

    now = datetime.now(timezone.utc)

    job.status = JOB_RUNNING
    job.attempts = (job.attempts or 0) + 1
    job.claimed_by = claimed_by[:255]
    job.claimed_at = now
    job.heartbeat_at = now

    await db.commit()
    await db.refresh(job)

    return job


async def heartbeat(db: AsyncSession, job_id: int) -> None:
    """
    Say the worker is still alive and still on this job.

    A bare ``UPDATE`` rather than loading the row and setting an attribute: it is called
    on a timer for the whole life of an export, and it has nothing to read. Committed
    immediately, because a heartbeat held in an open transaction is a heartbeat nobody
    else can see — which is the only thing it is for.
    """
    await db.execute(
        update(DownloadJob)
        .where(DownloadJob.id == job_id)
        .values(heartbeat_at=datetime.now(timezone.utc))
    )
    await db.commit()


async def requeue_stale_jobs(db: AsyncSession, stale_after_seconds: int) -> List[int]:
    """
    Put jobs whose worker stopped reporting back on the queue. Returns their ids.

    ``running`` with a heartbeat older than the threshold means the worker died — a
    restart, an OOM, a container replaced mid-export. The job goes back to ``queued``
    and its export back to ``queued`` with it, so the next worker starts the build from
    the confirmation rather than from wherever the last one had got to.

    Starting again rather than resuming is deliberate. The dead worker's part files are
    on disk and its cursor is not; a resume would have to trust files it cannot verify
    were written completely. The checkpointed *confirmation* is what is worth keeping,
    and it survives regardless — the graph resumes from the interrupt, which is before
    any file existed.

    ``attempts`` is not reset, so a job that keeps killing its worker is visible as
    such rather than looping silently forever.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=stale_after_seconds)

    statement = select(DownloadJob).where(
        DownloadJob.status == JOB_RUNNING,
        DownloadJob.heartbeat_at.is_not(None),
        DownloadJob.heartbeat_at < cutoff,
    )

    stale = list((await db.execute(statement)).scalars().all())

    if not stale:
        return []

    for job in stale:
        job.status = JOB_QUEUED
        job.claimed_by = None
        job.claimed_at = None
        job.heartbeat_at = None
        job.error_message = (
            "The worker building this file stopped responding, so it was queued again."
        )

    await db.execute(
        update(DownloadExport)
        .where(DownloadExport.id.in_([job.export_id for job in stale]))
        .values(status=EXPORT_QUEUED)
    )

    await db.commit()

    ids = [job.id for job in stale]
    logger.warning("Requeued %d stale export job(s): %s", len(ids), ids)

    return ids


async def queued_job_count(db: AsyncSession) -> int:
    """
    How many jobs are waiting. For the worker's idle logging, and for tests.

    Not ``CRUDQueryBuilder.count`` only because it lives beside the claim it describes —
    a reader working out whether the queue drains wants both in one place.
    """
    statement = select(DownloadJob.id).where(DownloadJob.status == JOB_QUEUED)

    return len(list((await db.execute(statement)).scalars().all()))
