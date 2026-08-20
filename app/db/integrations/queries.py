"""
Integration queries that ``CRUDQueryBuilder`` cannot express.

Same reason ``app/db/downloader_agents/queries.py`` and ``app/db/workspaces/queries.py``
exist: a feature's own concurrency rules and joins belong with the feature, not in a
model-agnostic module.

**One rule this module keeps throughout: every count is a real ``select(func.count())``.**
``CRUDQueryBuilder.count()`` materialises the rows and takes their length, which is fine
for a page of tool configs and is not fine for a run that produced eight hundred thousand
record rows. The distinction is invisible until the day it is not.

**Counters are bumped, never read-modify-written.** ``UPDATE … SET x = x + :n`` in one
statement. Two nodes of the same run add at the same moment by design — the write node
fans its chunks out — and a read-then-write would lose one of them silently, which is the
same class of bug ``flow_state._accumulate`` exists to prevent one layer up.

Phase 1 fills this in as the engine needs it. The job-claim query — the correlated ``NOT
EXISTS`` under ``FOR UPDATE SKIP LOCKED`` that makes ``overlap_policy = queue`` mean
something — arrives with the queue, because it is the queue's own concurrency rule and
belongs beside the worker that depends on it.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import aliased
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.integrations import (
    JOB_FAILED,
    JOB_QUEUED,
    JOB_RUNNING,
    RUN_FAILED,
    RUN_QUEUED,
    RUN_RUNNING,
    TRIGGER_SCHEDULE,
    VERSION_ARCHIVED,
    VERSION_PUBLISHED,
    IntegrationFlow,
    IntegrationFlowVersion,
    IntegrationRestOperation,
    IntegrationRun,
    IntegrationRunJob,
    IntegrationRunRecord,
    IntegrationRunStep,
    IntegrationSyncKey,
    IntegrationTrigger,
)

logger = logging.getLogger(__name__)

#: How many hashes go into one ``IN`` list. Postgres allows far more parameters than
#: this, but a batch is capped at 5000 records and a single statement carrying 5000 bound
#: values is slower to plan than five carrying a thousand.
_LOOKUP_CHUNK = 1000

#: The only columns :func:`bump_run_counts` will add to. Named explicitly rather than
#: checked with ``hasattr``, which would happily accept ``attempt`` or ``flow_id`` and
#: turn a typo into a corrupted row.
_RUN_COUNTER_COLUMNS = frozenset(
    {"records_read", "records_written", "records_failed", "records_skipped"}
)


async def get_rest_operation(
    db: AsyncSession, connection_id: int, operation_id: str
) -> Optional[IntegrationRestOperation]:
    """
    One user-defined operation on one connection.

    Takes the bigint ``connection_id`` rather than the uuid: the caller has already
    resolved the connection it is acting on, and re-resolving here would be a second
    query for a row that is in hand. The house rule is that a *route* accepts a uuid,
    which is upstream of this.

    Returns ``None`` rather than raising. The caller —
    ``registry.resolve_operation`` — has the connector's label and the list of what it
    does offer, so it can say something useful; this layer has neither.
    """
    result = await db.execute(
        select(IntegrationRestOperation).where(
            IntegrationRestOperation.connection_id == connection_id,
            IntegrationRestOperation.operation_id == operation_id,
        )
    )
    return result.scalar_one_or_none()


async def list_rest_operations(
    db: AsyncSession, connection_id: int
) -> list[IntegrationRestOperation]:
    """
    Every operation on one connection, for the picker and the AI catalogue.

    Ordered by label so the list a user sees is stable between page loads — an
    unordered list from Postgres is not random, but it is not promised either, and a
    picker whose entries move is a picker people misclick.
    """
    result = await db.execute(
        select(IntegrationRestOperation)
        .where(IntegrationRestOperation.connection_id == connection_id)
        .order_by(IntegrationRestOperation.label, IntegrationRestOperation.operation_id)
    )
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Sync keys — what a record from one system became in another
# ---------------------------------------------------------------------------


async def find_sync_keys(
    db: AsyncSession,
    connection_id: int,
    operation_id: str,
    hashes: Sequence[str],
) -> Dict[str, str]:
    """
    ``{natural_key_sha256: target_record_id}`` for whichever of ``hashes`` are known.

    **One query per chunk, not one per record.** A batch of five hundred records asking
    "have I written you before?" individually is five hundred round trips against the
    application's own database in the middle of a node that is already waiting on
    somebody else's API. The whole reason the unit of a loop pass is a batch is to stop
    doing exactly this.

    A hash that is absent from the result is a record this connection has never written
    for this operation — the caller creates it. There is no distinction between "never
    seen" and "seen and deleted at the far end", and that is a real limitation stated
    plainly: a record deleted in the destination is created again on the next run only if
    the caller also clears its sync key, which nothing in Phase 1 does.
    """
    if not hashes:
        return {}

    unique = list(dict.fromkeys(hashes))
    found: Dict[str, str] = {}

    for start in range(0, len(unique), _LOOKUP_CHUNK):
        chunk = unique[start : start + _LOOKUP_CHUNK]
        result = await db.execute(
            select(
                IntegrationSyncKey.natural_key_sha256,
                IntegrationSyncKey.target_record_id,
            ).where(
                IntegrationSyncKey.connection_id == connection_id,
                IntegrationSyncKey.operation_id == operation_id,
                IntegrationSyncKey.natural_key_sha256.in_(chunk),
            )
        )
        found.update({row[0]: row[1] for row in result.all()})

    return found


async def remember_sync_keys(
    db: AsyncSession,
    connection_id: int,
    operation_id: str,
    pairs: Iterable[Tuple[str, str]],
) -> int:
    """
    Record what each natural key became, as one ``INSERT … ON CONFLICT DO UPDATE``.

    **Upsert rather than select-then-insert**, for the reason that applies to every
    check-then-act in this module: two chunks of the same write node run concurrently by
    design, and the window between the select and the insert is exactly where the same
    natural key arrives twice. The unique constraint is the arbiter, and letting it
    decide costs one statement instead of two plus a retry.

    The statement is built for whichever dialect is underneath because the two spell the
    conflict target the same way but SQLAlchemy will not compile one dialect's
    ``insert()`` against the other. Both are real: Postgres runs this in production and
    SQLite runs it in every test, and a dedupe path that is only exercised in production
    is not one anybody should trust.

    Returns how many pairs were written, so a caller can log it. Does **not** commit —
    the sync key and whatever else the chunk recorded belong in one transaction, since a
    key remembered for a write that was rolled back would suppress a create that never
    happened.
    """
    rows = [
        {
            "connection_id": connection_id,
            "operation_id": operation_id,
            "natural_key_sha256": natural_key,
            "target_record_id": str(target_record_id),
        }
        for natural_key, target_record_id in pairs
        if natural_key and target_record_id is not None
    ]
    if not rows:
        return 0

    # A duplicate natural key inside one call would make Postgres refuse the whole
    # statement — "ON CONFLICT DO UPDATE command cannot affect row a second time" — so
    # the last write for a key wins here rather than taking the batch down.
    deduplicated = {row["natural_key_sha256"]: row for row in rows}

    insert = (
        sqlite_insert
        if db.get_bind().dialect.name == "sqlite"
        else postgres_insert
    )
    statement = insert(IntegrationSyncKey).values(list(deduplicated.values()))
    await db.execute(
        statement.on_conflict_do_update(
            index_elements=[
                IntegrationSyncKey.connection_id,
                IntegrationSyncKey.operation_id,
                IntegrationSyncKey.natural_key_sha256,
            ],
            set_={"target_record_id": statement.excluded.target_record_id},
        )
    )
    return len(deduplicated)


async def forget_sync_keys(
    db: AsyncSession, connection_id: int, operation_id: Optional[str] = None
) -> None:
    """
    Drop the remembered keys for a connection, or for one operation on it.

    The deliberate escape hatch for the limitation :func:`find_sync_keys` names: if the
    destination was wiped and everything needs creating again, the operator says so once
    rather than the engine guessing from a 404 — a 404 during a sync is far more often a
    permissions change than a deletion, and guessing wrong duplicates every record.
    """
    statement = delete(IntegrationSyncKey).where(
        IntegrationSyncKey.connection_id == connection_id
    )
    if operation_id:
        statement = statement.where(IntegrationSyncKey.operation_id == operation_id)
    await db.execute(statement)


# ---------------------------------------------------------------------------
# Runs — the reads a page and a worker make
# ---------------------------------------------------------------------------


async def fetch_run_with_flow(
    db: AsyncSession, run_uuid: Any
) -> Optional[Tuple[IntegrationRun, IntegrationFlow]]:
    """
    One run and the flow it belongs to, in one statement.

    A join rather than two queries because every caller needs both: ownership lives on
    the flow (a run has no ``user_id`` of its own, deliberately — one place to change
    when a run becomes shareable) and the page needs the flow's name in its heading.

    Returns ``None`` for a run that does not exist. The *caller* decides what a run
    belonging to somebody else looks like, and the answer is the same "not found" — a
    different answer would confirm that the uuid is real.
    """
    result = await db.execute(
        select(IntegrationRun, IntegrationFlow)
        .join(IntegrationFlow, IntegrationFlow.id == IntegrationRun.flow_id)
        .where(IntegrationRun.uuid == run_uuid)
    )
    row = result.first()
    return (row[0], row[1]) if row else None


async def fetch_run_steps(
    db: AsyncSession, run_id: int, *, after_sequence: int = -1, limit: Optional[int] = None
) -> List[IntegrationRunStep]:
    """
    One run's log, in the order the nodes ran.

    ``after_sequence`` is what makes the step list paginable. The SSE frame carries only
    the last hundred rows — see :func:`fetch_recent_steps` — and a dock that wants the
    rest asks for everything after the highest sequence it already has. A run of a
    hundred thousand steps must not arrive as one payload, and a client that reconnects
    must not have to re-fetch what it already drew.
    """
    statement = (
        select(IntegrationRunStep)
        .where(IntegrationRunStep.run_id == run_id)
        .order_by(IntegrationRunStep.sequence)
    )
    if after_sequence >= 0:
        statement = statement.where(IntegrationRunStep.sequence > after_sequence)
    if limit:
        statement = statement.limit(limit)

    result = await db.execute(statement)
    return list(result.scalars().all())


async def fetch_recent_steps(
    db: AsyncSession, run_id: int, limit: int = 100
) -> List[IntegrationRunStep]:
    """
    The tail of the log, oldest-first, for the live frame.

    Selected newest-first and reversed in Python rather than ordered ascending with an
    offset: an offset has to count every row it skips, and a fifty-thousand step run
    would pay that on every one-second poll.
    """
    result = await db.execute(
        select(IntegrationRunStep)
        .where(IntegrationRunStep.run_id == run_id)
        .order_by(IntegrationRunStep.sequence.desc())
        .limit(limit)
    )
    return list(reversed(result.scalars().all()))


async def count_run_steps(db: AsyncSession, run_id: int) -> int:
    """
    How many step rows this run has. A real ``COUNT(*)``.

    ``CRUDQueryBuilder.count()`` would select the rows and take their length, which is
    fine for a page of tool configs and is not fine here — the whole point of the number
    is that the caller is *not* fetching them all.
    """
    result = await db.execute(
        select(func.count())
        .select_from(IntegrationRunStep)
        .where(IntegrationRunStep.run_id == run_id)
    )
    return int(result.scalar_one())


async def next_step_sequence(db: AsyncSession, run_id: int) -> int:
    """
    The next position in this run's log.

    ``MAX(sequence) + 1`` rather than a count, so a gap left by a failed insert does not
    make two rows claim the same position. Read-then-write, and that is acceptable here
    where it is not elsewhere in this module: two steps racing for the same sequence
    produces a log that renders in a slightly odd order, whereas two counter bumps
    racing loses records from a total somebody bills on. Cheap correctness where it
    matters, cheap code where it does not.
    """
    result = await db.execute(
        select(func.max(IntegrationRunStep.sequence)).where(
            IntegrationRunStep.run_id == run_id
        )
    )
    return int(result.scalar() or 0) + 1


async def find_rollup_step(
    db: AsyncSession, run_id: int, node_id: str
) -> Optional[IntegrationRunStep]:
    """
    The row standing for many passes of one node, if this node has collapsed yet.

    Read once per pass of a long loop, which makes it the hottest read in the module —
    hence ``ix_integration_run_steps_run_node``.
    """
    result = await db.execute(
        select(IntegrationRunStep).where(
            IntegrationRunStep.run_id == run_id,
            IntegrationRunStep.node_id == node_id,
            IntegrationRunStep.is_rollup.is_(True),
        )
    )
    return result.scalars().first()


async def count_node_steps(db: AsyncSession, run_id: int, node_id: str) -> int:
    """How many rows one node of one run has written. Decides when to start collapsing."""
    result = await db.execute(
        select(func.count())
        .select_from(IntegrationRunStep)
        .where(
            IntegrationRunStep.run_id == run_id,
            IntegrationRunStep.node_id == node_id,
        )
    )
    return int(result.scalar_one())


# ---------------------------------------------------------------------------
# Counters — bumped, never read-modify-written
# ---------------------------------------------------------------------------


async def bump_run_counts(db: AsyncSession, run_id: int, **deltas: int) -> None:
    """
    Add to a run's record counters in one statement per call.

    ``UPDATE … SET records_written = records_written + :n``. **Never** read the value
    and write it back: the write node fans its chunks out with ``asyncio.gather`` by
    design, so two additions land at the same moment and a read-modify-write silently
    loses one of them. It is the same class of bug ``flow_state._accumulate`` prevents
    one layer up, and it is silent in exactly the same way — the number is merely
    smaller than the truth, with nothing to say so.

    Zero and negative deltas are dropped rather than issued. A batch pass that failed
    nothing should not write to the row at all; issuing ``+ 0`` a hundred times per run
    is a hundred row locks bought for nothing.
    """
    values = {
        column: getattr(IntegrationRun, column) + amount
        for column, amount in deltas.items()
        if amount and column in _RUN_COUNTER_COLUMNS
    }
    unknown = set(deltas) - _RUN_COUNTER_COLUMNS
    if unknown:
        # Raised rather than ignored: a typo'd counter name that silently does nothing
        # is a run reporting zero written for a sync that worked.
        raise ValueError(
            f"{', '.join(sorted(unknown))} is not a run counter. The counters are: "
            f"{', '.join(sorted(_RUN_COUNTER_COLUMNS))}."
        )
    if not values:
        return

    values["heartbeat_at"] = func.now()
    await db.execute(
        update(IntegrationRun).where(IntegrationRun.id == run_id).values(**values)
    )


async def mark_log_truncated(db: AsyncSession, run_id: int) -> None:
    """
    Say that the record log stopped accepting rows, without touching the counters.

    The two are deliberately independent. A run page that says "50,000 records, 1,000 of
    the failures listed" is honest; one that capped the count as well would say 1,000
    and give the reader nothing to notice.
    """
    await db.execute(
        update(IntegrationRun)
        .where(IntegrationRun.id == run_id, IntegrationRun.records_log_truncated.is_(False))
        .values(records_log_truncated=True)
    )


async def count_run_records(
    db: AsyncSession, run_id: int, outcome: Optional[str] = None
) -> int:
    """How many record rows this run logged, optionally of one outcome. A real count."""
    statement = (
        select(func.count())
        .select_from(IntegrationRunRecord)
        .where(IntegrationRunRecord.run_id == run_id)
    )
    if outcome:
        statement = statement.where(IntegrationRunRecord.outcome == outcome)

    result = await db.execute(statement)
    return int(result.scalar_one())


async def count_records_by_outcome(db: AsyncSession, run_id: int) -> Dict[str, int]:
    """
    ``{outcome: count}`` for one run, in one grouped statement.

    One query rather than four, because the run page shows all of them together and four
    round trips for four integers is three too many.
    """
    result = await db.execute(
        select(IntegrationRunRecord.outcome, func.count())
        .where(IntegrationRunRecord.run_id == run_id)
        .group_by(IntegrationRunRecord.outcome)
    )
    return {outcome: int(count) for outcome, count in result.all()}


async def fetch_run_records(
    db: AsyncSession,
    run_id: int,
    *,
    outcome: Optional[str] = None,
    retryable_only: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> List[IntegrationRunRecord]:
    """
    The dead-letter list, paginated.

    ``retryable_only`` is what the "replay the failures" button selects on. Deciding
    retryability at *write* time rather than here is deliberate and load-bearing: a
    ``ReadTimeout`` on a non-idempotent write may well have reached the server, and only
    the code that made the call knows that. Re-deriving it later from a stored message
    is how a merchant ends up with two of everything.
    """
    statement = (
        select(IntegrationRunRecord)
        .where(IntegrationRunRecord.run_id == run_id)
        .order_by(IntegrationRunRecord.id)
        .limit(limit)
        .offset(offset)
    )
    if outcome:
        statement = statement.where(IntegrationRunRecord.outcome == outcome)
    if retryable_only:
        statement = statement.where(IntegrationRunRecord.retryable.is_(True))

    result = await db.execute(statement)
    return list(result.scalars().all())


async def step_node_ids(db: AsyncSession, run_id: int) -> set:
    """
    The distinct nodes this run has written a step row for.

    Answers "which nodes did the run actually reach?", and the answer has to come from the
    log rather than from the final state. A node that **failed** contributes nothing to
    ``counts`` or ``outputs`` — it raised — so a state-derived answer calls it unreached
    and writes it a second, ``skipped`` row on top of its ``failed`` one. Two rows for one
    node, contradicting each other, in the log somebody reads to find out what went wrong.
    """
    result = await db.execute(
        select(IntegrationRunStep.node_id)
        .where(IntegrationRunStep.run_id == run_id)
        .distinct()
    )
    return {row[0] for row in result.all()}


# ---------------------------------------------------------------------------
# The queue
# ---------------------------------------------------------------------------


async def claim_next_run_job(
    db: AsyncSession, claimed_by: str
) -> Optional[IntegrationRunJob]:
    """
    Take the next runnable job, or ``None``.

    ``FOR UPDATE SKIP LOCKED`` makes two workers running this identical statement at the
    same moment get two *different* jobs, or one gets a job and the other gets nothing.
    Neither waits and neither can take the other's. The status flips inside the same
    transaction, so by the time the lock is released the row no longer matches ``queued``.

    **The correlated ``NOT EXISTS`` is what makes ``overlap_policy = queue`` mean
    anything.** Without it, "queue this run behind the last one" would put a second run of
    the same workflow on the queue and a second worker would start it immediately — two
    concurrent syncs against the same destination, which is exactly what the policy exists
    to prevent. It is the subtlest query in the module and it is the reason this function
    is not ``claim_next_job`` with a different table name.

    Ordered by priority then availability: a run somebody is sitting and watching should
    not wait behind a nightly backfill, and within one priority the oldest goes first so a
    queue under load stays fair.

    **On SQLite this compiles to a plain SELECT.** SQLAlchemy drops the locking clause for
    a dialect that has none, which is what lets the test suite exercise the same code path
    — worth knowing rather than discovering, because a test can prove the claim works and
    cannot prove the locking does.

    Commits. A claim that is not committed is not a claim: the lock would be released when
    the session closed and the job would look available again while a worker was on it.
    """
    # The candidate's own run is **joined into the outer query** rather than looked up in
    # a nested scalar subquery, so the ``EXISTS`` below has one unambiguous thing to
    # correlate against. Two levels of nesting is where auto-correlation picks the wrong
    # enclosing SELECT, and the failure is silent and severe: the condition stops meaning
    # "this flow is busy" and starts meaning "any flow is busy", so one running sync
    # blocks every workflow in the system.
    running_job = aliased(IntegrationRunJob)
    running_run = aliased(IntegrationRun)

    busy_flow = (
        select(running_job.id)
        .join(running_run, running_run.id == running_job.run_id)
        .where(
            running_job.status == JOB_RUNNING,
            running_run.flow_id == IntegrationRun.flow_id,
        )
        # Explicit, not inferred. The one thing this subquery may take from the outer
        # query is the candidate's flow.
        .correlate(IntegrationRun)
        .exists()
    )

    statement = (
        select(IntegrationRunJob)
        .join(IntegrationRun, IntegrationRun.id == IntegrationRunJob.run_id)
        .where(
            IntegrationRunJob.status == JOB_QUEUED,
            IntegrationRunJob.available_at <= func.now(),
            ~busy_flow,
        )
        .order_by(
            IntegrationRunJob.priority.desc(),
            IntegrationRunJob.available_at,
            IntegrationRunJob.id,
        )
        .limit(1)
        # ``of`` names the job: the run is joined for its ``flow_id`` and must not be
        # locked, or a scheduler advancing the same flow would block on a claim.
        .with_for_update(skip_locked=True, of=IntegrationRunJob)
    )

    job = (await db.execute(statement)).scalars().first()
    if job is None:
        return None

    moment = datetime.now(timezone.utc)
    job.status = JOB_RUNNING
    job.attempts = (job.attempts or 0) + 1
    job.claimed_by = claimed_by[:255]
    job.claimed_at = moment
    job.heartbeat_at = moment

    await db.commit()
    await db.refresh(job)
    return job


async def job_heartbeat(db: AsyncSession, job_id: int) -> None:
    """
    Say the worker is still on this job.

    A bare ``UPDATE`` rather than loading the row: it is called on a timer for the whole
    life of a run and has nothing to read. Committed immediately, because a heartbeat held
    in an open transaction is one nobody else can see — which is the only thing it is for.
    """
    await db.execute(
        update(IntegrationRunJob)
        .where(IntegrationRunJob.id == job_id)
        .values(heartbeat_at=datetime.now(timezone.utc))
    )
    await db.commit()


async def finish_run_job(
    db: AsyncSession, job_id: int, status: str, error_message: str = ""
) -> None:
    """Close a job off. The run row was already written by ``run_service``; this is the
    queue's own bookkeeping and is deliberately separate from it."""
    await db.execute(
        update(IntegrationRunJob)
        .where(IntegrationRunJob.id == job_id)
        .values(
            status=status,
            finished_at=datetime.now(timezone.utc),
            error_message=error_message or None,
        )
    )
    await db.commit()


async def requeue_stale_run_jobs(
    db: AsyncSession, stale_after_seconds: int
) -> List[int]:
    """
    Fail the runs whose worker stopped reporting, and return their job ids.

    **Requeue-and-fail, not resume**, and in Phase 1 that means the *run* is failed while
    the job is closed. ``requeue_stale_jobs`` in the downloader starts an export again
    from its confirmation, which is safe because nothing outside this application has seen
    a part file. A sync is not that: the dead worker may have written four hundred records
    into somebody's CRM, and starting again would write them twice.

    So the operator gets a run that says the worker stopped responding, and a Replay
    button — which goes through the sync keys and turns the repeats into updates. Automatic
    resume is Phase 4 work, gated on every write node carrying an idempotency template.

    ``attempts`` is not reset, so a run that keeps killing its worker is visible as such
    rather than looping silently forever.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=stale_after_seconds)

    result = await db.execute(
        select(IntegrationRunJob).where(
            IntegrationRunJob.status == JOB_RUNNING,
            IntegrationRunJob.heartbeat_at.is_not(None),
            IntegrationRunJob.heartbeat_at < cutoff,
        )
    )
    stale = list(result.scalars().all())
    if not stale:
        return []

    message = (
        "The worker running this sync stopped responding, so it was not completed. "
        "Check the destination and press Replay if you want to run it again."
    )

    for job in stale:
        job.status = JOB_FAILED
        job.finished_at = datetime.now(timezone.utc)
        job.error_message = message

    await db.execute(
        update(IntegrationRun)
        .where(IntegrationRun.id.in_([job.run_id for job in stale]))
        .values(
            status=RUN_FAILED,
            error_message=message,
            finished_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()

    ids = [job.id for job in stale]
    logger.warning("Failed %d stale integration run job(s): %s", len(ids), ids)
    return ids


async def queued_run_job_count(db: AsyncSession) -> int:
    """How many jobs are waiting. A real count."""
    result = await db.execute(
        select(func.count())
        .select_from(IntegrationRunJob)
        .where(IntegrationRunJob.status == JOB_QUEUED)
    )
    return int(result.scalar_one())


async def runs_in_flight(db: AsyncSession, flow_id: int) -> int:
    """
    How many runs of one flow are queued or running.

    The scheduler's overlap check. Counted rather than existence-tested because
    ``overlap_policy = queue`` is bounded at ``OVERLAP_QUEUE_LIMIT`` — an unbounded queue
    for a sync that takes longer than its interval grows forever, and the first anybody
    hears of it is a thousand pending runs.
    """
    result = await db.execute(
        select(func.count())
        .select_from(IntegrationRun)
        .where(
            IntegrationRun.flow_id == flow_id,
            IntegrationRun.status.in_((RUN_QUEUED, RUN_RUNNING)),
        )
    )
    return int(result.scalar_one())


# ---------------------------------------------------------------------------
# The scheduler
# ---------------------------------------------------------------------------


async def claim_due_triggers(
    db: AsyncSession, moment: datetime, limit: int = 50
) -> List[IntegrationTrigger]:
    """
    Every enabled schedule that is due, locked for this tick.

    ``FOR UPDATE SKIP LOCKED`` again, and for a sharper reason than the queue's: two
    schedulers seeing the same due trigger without it would each advance ``next_run_at``
    and each insert a run, and the flow would fire twice for one slot. The
    ``idempotency_key`` unique index is the second line of defence; this is the first.

    Does **not** commit. The caller advances ``next_run_at``, inserts the run and inserts
    the job in the same transaction, so a crash between the advance and the enqueue cannot
    happen — there is no "between".
    """
    result = await db.execute(
        select(IntegrationTrigger)
        .where(
            IntegrationTrigger.is_enabled.is_(True),
            IntegrationTrigger.kind == TRIGGER_SCHEDULE,
            IntegrationTrigger.next_run_at.is_not(None),
            IntegrationTrigger.next_run_at <= moment,
        )
        .order_by(IntegrationTrigger.next_run_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Flows and versions
# ---------------------------------------------------------------------------


async def flow_named(
    db: AsyncSession,
    user_id: int,
    name: str,
    *,
    exclude_id: Optional[int] = None,
) -> Optional[IntegrationFlow]:
    """
    The user's flow with this name, compared the way the index compares it.

    ``lower(name)`` in SQL rather than loading every flow and comparing in Python, which
    is what the graph library does and what stops being acceptable at a few hundred rows.
    Matches ``uq_integration_flows_user_name_lower``, so the sentence this feeds and the
    constraint that would otherwise raise an ``IntegrityError`` agree about what a
    duplicate is.
    """
    statement = select(IntegrationFlow).where(
        IntegrationFlow.user_id == user_id,
        func.lower(IntegrationFlow.name) == (name or "").strip().lower(),
    )
    if exclude_id is not None:
        statement = statement.where(IntegrationFlow.id != exclude_id)

    result = await db.execute(statement.limit(1))
    return result.scalar_one_or_none()


async def published_version(
    db: AsyncSession, flow_id: int
) -> Optional[IntegrationFlowVersion]:
    """
    The one version of this flow that runs, or ``None`` while it is a draft.

    Ordered newest-first so that a database which somehow holds two published rows —
    the state the partial unique index and :func:`app.services.integrations.flow_service.publish_flow`
    both exist to prevent — resolves to the most recent one rather than to whichever the
    planner happened to return. A silent wrong answer here would run a topology nobody
    published.
    """
    result = await db.execute(
        select(IntegrationFlowVersion)
        .where(
            IntegrationFlowVersion.flow_id == flow_id,
            IntegrationFlowVersion.status == VERSION_PUBLISHED,
        )
        .order_by(IntegrationFlowVersion.version_number.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def count_published_versions(db: AsyncSession, flow_id: int) -> int:
    """How many published versions this flow has. Should only ever be 0 or 1 — see
    :func:`published_version`."""
    result = await db.execute(
        select(func.count())
        .select_from(IntegrationFlowVersion)
        .where(
            IntegrationFlowVersion.flow_id == flow_id,
            IntegrationFlowVersion.status == VERSION_PUBLISHED,
        )
    )
    return int(result.scalar() or 0)


async def next_version_number(db: AsyncSession, flow_id: int) -> int:
    """
    One past the highest version this flow has ever had.

    Deliberately **not** ``count + 1``. Versions are never deleted today, but the day one
    is, ``count + 1`` reuses a number that a run row already points at by id and a person
    already knows by name — and "version 3" would then mean two different topologies.
    """
    result = await db.execute(
        select(func.max(IntegrationFlowVersion.version_number)).where(
            IntegrationFlowVersion.flow_id == flow_id
        )
    )
    return int(result.scalar() or 0) + 1


async def archive_published_versions(db: AsyncSession, flow_id: int) -> int:
    """
    Archive every published version of this flow, in the caller's transaction.

    A bare ``UPDATE … WHERE status = 'published'``, not a load-and-loop: publishing must
    leave exactly one published row, and the way to guarantee that against a second
    request arriving at the same moment is for the archive and the insert to be one
    transaction ending in a unique index. Returns how many rows it archived so the caller
    can tell a first publish from a re-publish.
    """
    result = await db.execute(
        update(IntegrationFlowVersion)
        .where(
            IntegrationFlowVersion.flow_id == flow_id,
            IntegrationFlowVersion.status == VERSION_PUBLISHED,
        )
        .values(status=VERSION_ARCHIVED)
    )
    return int(result.rowcount or 0)


async def latest_runs_for_flows(
    db: AsyncSession, flow_ids: Sequence[int]
) -> Dict[int, IntegrationRun]:
    """
    The most recent run of each of these flows, in one statement.

    The flow list shows "last run: failed, 20 minutes ago" against every row, and doing
    that with a query per flow is the N+1 that makes a page with forty workflows on it
    take a second. Ordered so the dictionary comprehension keeps the newest.
    """
    if not flow_ids:
        return {}

    result = await db.execute(
        select(IntegrationRun)
        .where(IntegrationRun.flow_id.in_(list(flow_ids)))
        .order_by(IntegrationRun.flow_id, IntegrationRun.started_at.desc(), IntegrationRun.id.desc())
    )

    latest: Dict[int, IntegrationRun] = {}
    for run in result.scalars().all():
        latest.setdefault(run.flow_id, run)
    return latest


async def published_versions_for_flows(
    db: AsyncSession, flow_ids: Sequence[int]
) -> Dict[int, IntegrationFlowVersion]:
    """
    The published version of each of these flows, in one statement.

    The list page shows a Draft or Published badge on every row; asking per flow is the
    same N+1 :func:`latest_runs_for_flows` avoids, and for the same reason.
    """
    if not flow_ids:
        return {}

    result = await db.execute(
        select(IntegrationFlowVersion)
        .where(
            IntegrationFlowVersion.flow_id.in_(list(flow_ids)),
            IntegrationFlowVersion.status == VERSION_PUBLISHED,
        )
        .order_by(IntegrationFlowVersion.version_number.desc())
    )

    published: Dict[int, IntegrationFlowVersion] = {}
    for version in result.scalars().all():
        published.setdefault(version.flow_id, version)
    return published
