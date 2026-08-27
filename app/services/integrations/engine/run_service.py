"""
Driving one run from start to finish, and stopping one that is already going.

This is the module that owns a run's *lifecycle*. The compiler builds the graph, the
runners do the work and the store writes it down; what happens here is the ordering
around all of that — validate, compile, invoke, settle, and release everything the run
was holding whether it succeeded, failed or was stopped.

**The compiler is imported inside functions**, not at the top. It is the only langgraph
importer in the feature, and importing it here would make langgraph a hard dependency of
the worker, the routes and every test that touches a run. Same call
``graph_run_service`` makes, for the same reason.

**Validation happens again before compiling.** The flow was validated when it was saved
and again when it was published, and it is validated a third time here. Not paranoia: the
run executes a *pinned version*, and a version published by an older build of this
application may not satisfy a rule added since. Discovering that as a readable refusal
before anything is sent is better than discovering it as a half-completed sync.

**Cleanup is in a ``finally`` and it is not optional.** The record buffer holds real
records in process memory and the record log holds a per-run budget; a run that fails to
release them leaks for the lifetime of the worker. The ``finally`` is the path that
matters most, because a cancelled task does not route to a cleanup node — there is no
graph edge that runs when somebody presses Stop.

**Cancellation is two mechanisms and both are needed.** The durable one is
``integration_runs.cancel_requested``, polled at the top of every node and between chunks;
it is what makes a run stoppable from another worker or after a restart. The fast one is
the local task, cancelled directly, so a run in *this* process stops within milliseconds
rather than within the poll interval. **The row is marked first**: cancelling the task
first races the write, and the page then shows a run that stopped with nothing on it
saying why.

**Crash recovery in Phase 1 is requeue-and-fail, not resume.** A worker that dies leaves a
run whose heartbeat goes stale; the queue marks it failed with a sentence saying so and
the operator presses Replay. That is ``requeue_stale_jobs``' own reasoning — starting
again rather than resuming is deliberate — applied to a harder case, because half-resuming
a write into somebody's CRM is worse than a clear failure with a button next to it.
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict, Mapping, Optional, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.db_utils import CRUDQueryBuilder
from app.utils import events
from app.models.integrations import (
    RUN_CANCELLED,
    RUN_FAILED,
    RUN_MODE_DRY_RUN,
    RUN_MODE_LIVE,
    STEP_SKIPPED,
    TRIGGER_MANUAL,
    IntegrationFlow,
    IntegrationFlowVersion,
    IntegrationRun,
)
from app.db.integrations.queries import step_node_ids
from app.services.integrations.engine.idempotency import graph_hash
from app.services.integrations.engine import (
    flow_rules,
    flow_state,
    node_runners,
    record_buffer,
    record_log,
    run_store,
)
from app.services.integrations.errors import (
    FlowValidationError,
    IntegrationFailure,
    NodeFailure,
    RunCancelled,
)

logger = logging.getLogger(__name__)

flow_crud = CRUDQueryBuilder(IntegrationFlow)
version_crud = CRUDQueryBuilder(IntegrationFlowVersion)
run_crud = CRUDQueryBuilder(IntegrationRun)

#: How often the worker says it is still alive while a run is in flight. Short enough that
#: the queue's stale threshold can be tight, long enough that a run is not writing to its
#: own row more often than it does anything else.
HEARTBEAT_SECONDS = 10

#: How often :func:`watch_run` re-reads the run. One second, matching the dock's own poll:
#: a person watching records climb reads this as continuous, and anything faster is queries
#: nobody can perceive.
STREAM_POLL_SECONDS = 1.0

#: How long one stream may stay open. An hour, after which the browser reconnects and gets
#: a fresh one — a bound on a forgotten tab holding a connection, not on how long a sync
#: may take, because every frame is whole state and a reconnect loses nothing.
MAX_STREAM_SECONDS = 3600.0

#: Said for a run that is missing and for one that is somebody else's. The same sentence
#: for both, so guessing uuids confirms nothing.
NO_SUCH_RUN = "That run does not exist."

# run_id -> the task driving it. Process-local by definition: this is the *fast* half of
# cancellation, and the durable half is the row. A run in another worker is stopped by
# marking the row and waiting for its next poll.
_RUNNING: Dict[int, asyncio.Task] = {}


# ---------------------------------------------------------------------------
# Creating
# ---------------------------------------------------------------------------


async def begin_run(
    db: AsyncSession,
    flow: IntegrationFlow,
    *,
    version: Optional[IntegrationFlowVersion] = None,
    mode: str = RUN_MODE_LIVE,
    trigger_kind: str = TRIGGER_MANUAL,
    trigger_id: Optional[int] = None,
    idempotency_key: Optional[str] = None,
    scheduled_for: Any = None,
    replay_of_run_id: Optional[int] = None,
) -> IntegrationRun:
    """
    Record a run, without committing and without queueing it.

    Both omissions are deliberate. The caller — the queue for a manual run, the scheduler
    for a timed one — inserts the job row in the *same* transaction, so a crash between
    the two cannot lose a run or leave a job pointing at nothing. And the run exists
    before anything is compiled, so a workflow that fails to compile is a run somebody can
    open and read a reason from rather than a button that appeared to do nothing.

    ``mode`` is checked here rather than trusted, because a dry run that quietly became a
    live one would write to somebody's production system on the strength of a typo.
    """
    if mode not in (RUN_MODE_LIVE, RUN_MODE_DRY_RUN):
        raise IntegrationFailure(
            f"'{mode}' is not a way to run a workflow. Use '{RUN_MODE_LIVE}' or "
            f"'{RUN_MODE_DRY_RUN}'."
        )

    return await run_store.create_run(
        db,
        flow_id=flow.id,
        flow_version_id=version.id if version else None,
        thread_id=run_store.new_thread_id(),
        trigger_id=trigger_id,
        trigger_kind=trigger_kind,
        mode=mode,
        idempotency_key=idempotency_key,
        scheduled_for=scheduled_for,
        replay_of_run_id=replay_of_run_id,
    )


async def graph_for(
    db: AsyncSession, run: IntegrationRun, flow: IntegrationFlow
) -> Mapping[str, Any]:
    """
    The drawing this run executes: the **pinned version**, or the live flow if it has none.

    A run with a ``flow_version_id`` runs that snapshot however the flow has been edited
    since — which is what the versions table is for, and what makes a replay a repeat
    rather than a different workflow that happens to share a name.

    A run with no version is a manual run of a flow nobody has published. It runs the
    drawing as it stands, and is also precisely the run that cannot be replayed, because
    there is nothing to replay it *as*.
    """
    if run.flow_version_id:
        version = await version_crud.get_one(db, filters={"id": run.flow_version_id})
        if version is None:
            raise IntegrationFailure(
                "The published version this run was pinned to no longer exists, so it "
                "cannot be run. Publish the workflow again."
            )
        return version.graph_data or {}

    return flow.graph_data or {}


# ---------------------------------------------------------------------------
# Executing
# ---------------------------------------------------------------------------


@dataclass
class RunPlan:
    """
    Everything one run needs, read in a single session before it starts.

    Assembled up front rather than fetched as it goes. Three of these values are wanted
    by three different functions, and reading each where it is needed would be three
    extra round trips per run against the application's own database — for values that
    cannot change once the run is under way.
    """

    run_id: int
    run_uuid: str
    user_id: int
    thread_id: str
    graph_data: Mapping[str, Any]
    inputs: Dict[str, Any]
    dry_run: bool
    redacted_fields: Sequence[str]
    default_batch_size: Optional[int]
    attempt: int = 1


async def execute_run(run_id: int, *, attempt: int = 1) -> str:
    """
    Run one workflow to completion and return the status it ended with.

    Called by the queue worker. Everything it needs is on the row, so a run can be
    executed by a worker that knows nothing about how it was created — which is what
    makes a manual run and a scheduled one the same code path, and therefore what makes
    the run tested at eleven in the morning the run that fires at three.

    Exceptions do not escape. A run that failed is a *recorded* fact, and the queue needs
    a status rather than a traceback to decide what to do with the job.
    """
    task = asyncio.current_task()
    if task is not None:
        _RUNNING[run_id] = task

    heartbeat: Optional[asyncio.Task] = None
    plan: Optional[RunPlan] = None

    try:
        plan = await _plan_for(run_id, attempt)
        if plan is None:
            return RUN_FAILED

        heartbeat = asyncio.create_task(_beat(run_id))
        return await _drive(plan)
    except asyncio.CancelledError:
        # The fast half of cancellation reached this task. The row was already marked by
        # `request_stop` — see the module docstring on why that order and not the other.
        await _settle_cancelled(run_id)
        raise
    except Exception as exc:  # noqa: BLE001 — a run must not take the worker down
        logger.exception("Run %s failed unexpectedly", run_id)
        await _fail_by_id(run_id, _readable(exc))
        return RUN_FAILED
    finally:
        if heartbeat is not None:
            heartbeat.cancel()
        _RUNNING.pop(run_id, None)
        # Not optional, and not on a graph edge. A cancelled task never routes anywhere,
        # so this is the only place a stopped run's memory comes back. Keyed by the run's
        # **uuid**, because that is what the buffer keys are built from — releasing by the
        # bigint id would match nothing and leak silently.
        if plan is not None:
            record_buffer.release_run(plan.run_uuid)
        record_log.release_run(run_id)
        run_store.forget_run(run_id)


async def _plan_for(run_id: int, attempt: int) -> Optional[RunPlan]:
    """
    Read the run, its flow and its pinned drawing, and mark it running.

    Returns ``None`` when there is nothing to run, having already recorded why. A run
    whose flow was deleted mid-queue is a real sequence — somebody removes a workflow
    while its 3am job is waiting — and it has to end as a run with a reason rather than
    as an exception in a worker log.
    """
    async with run_store.open_session() as db:
        run = await run_crud.get_one(db, filters={"id": run_id})
        if run is None:
            logger.warning("Run %s no longer exists", run_id)
            return None

        flow = await flow_crud.get_one(db, filters={"id": run.flow_id})
        if flow is None:
            await _fail(db, run, "The workflow this run belongs to has been deleted.")
            return None

        try:
            graph_data = await graph_for(db, run, flow)
        except IntegrationFailure as exc:
            await _fail(db, run, str(exc))
            return None

        await run_store.mark_running(db, run.id, attempt)

        return RunPlan(
            run_id=run.id,
            run_uuid=str(run.uuid),
            user_id=flow.user_id,
            thread_id=str(run.thread_id),
            graph_data=graph_data,
            inputs={
                "trigger_kind": run.trigger_kind,
                "scheduled_for": (
                    run.scheduled_for.isoformat() if run.scheduled_for else None
                ),
                **(dict(run.interrupt_payload or {})),
            },
            dry_run=run.mode == RUN_MODE_DRY_RUN,
            redacted_fields=list(flow.redacted_fields or []),
            default_batch_size=int(flow.default_batch_size or 0) or None,
            attempt=attempt,
        )


async def _drive(plan: RunPlan) -> str:
    """
    Validate, compile, invoke, settle.

    The compiler is imported here rather than at module scope — see the module docstring.
    """
    from app.services.integrations.engine import flow_compiler

    try:
        flow_rules.validate_flow(plan.graph_data)
    except FlowValidationError as exc:
        # A version published by an older build, or a row edited by hand. A readable
        # refusal before anything is sent beats a half-completed sync.
        await _fail_by_id(plan.run_id, str(exc))
        return RUN_FAILED

    context = node_runners.RunContext(
        run_id=plan.run_id,
        run_uuid=plan.run_uuid,
        user_id=plan.user_id,
        open_session=run_store.open_session,
        nodes={
            flow_rules.node_id_of(node): node
            for node in flow_rules.nodes_of(plan.graph_data)
        },
        dry_run=plan.dry_run,
        redacted_fields=list(plan.redacted_fields),
        **(
            {"default_batch_size": plan.default_batch_size}
            if plan.default_batch_size
            else {}
        ),
    )

    compiled = await flow_compiler.compile_flow(plan.graph_data, context)
    state = flow_state.initial_state(
        run_id=plan.run_uuid,
        version_hash=graph_hash(plan.graph_data),
        inputs=plan.inputs,
        dry_run=plan.dry_run,
        attempt=plan.attempt,
    )

    final = await compiled.graph.ainvoke(
        state,
        config={
            "configurable": {"thread_id": plan.thread_id},
            "recursion_limit": compiled.recursion_limit,
        },
    )

    return await _settle(plan.run_id, final, compiled)


async def _settle(run_id: int, final: Mapping[str, Any], compiled: Any) -> str:  # noqa: ANN401
    """
    Write down how the run ended.

    The status comes from :func:`run_store.final_status`, which is a pure function of the
    counters — so the queue, the orchestrator and a replay cannot each decide it
    differently, and **a run with any failed, invalid or skipped record ends ``partial``**
    rather than succeeded.

    Nodes the graph never reached get a ``skipped`` step row. A node missing from the log
    is indistinguishable from a node the run never got to, and telling those two apart is
    most of what somebody reading a cancelled or failed run wants to know.
    """
    totals = flow_state.totals(final)
    cancelled = bool(final.get("cancelled"))
    failed_at = str(final.get("failed_at") or "")

    if failed_at and not cancelled:
        status = RUN_FAILED
        message = str(final.get("failure_message") or "")
    else:
        status = run_store.final_status(
            failed=totals.get("failed", 0),
            skipped=totals.get("skipped", 0),
            invalid=totals.get("invalid", 0),
            cancelled=cancelled,
        )
        message = str(final.get("failure_message") or "") if cancelled else ""

    await _record_unreached(run_id, final, compiled)

    async with run_store.open_session() as db:
        await run_store.mark_finished(
            db, run_id, status,
            result_preview=_preview(final, totals),
            error_message=message or None,
        )

    # Announce it, outside the session and after the commit, so a subscriber opening its own
    # session reads a database that agrees with the event. `publish` never raises, so an
    # email trigger with a broken template cannot change how this run is recorded.
    await _announce_settled(run_id, status, message, totals)

    return status


async def _announce_settled(
    run_id: int,
    status: str,
    message: str,
    totals: Mapping[str, int],
) -> None:
    """
    Publish ``integration_run.settled``.

    Carries the record counters as well as the status, because "the sync finished" and "the
    sync finished having failed 3 of 50,000 records" are the two different things somebody
    would want an email about, and ``partial`` alone does not say which.

    Swallows its own failures: the run is already recorded, and an event that could not be
    published is worth a log line and nothing more.
    """
    try:
        async with run_store.open_session() as db:
            run = await run_crud.get_one(db, {"id": run_id})
            if run is None:
                return
            flow = await flow_crud.get_one(db, {"id": run.flow_id})

            await events.publish(
                events.EVENT_INTEGRATION_RUN_SETTLED,
                {
                    "run_uuid": str(run.uuid),
                    "status": status,
                    "flow_name": flow.name if flow else "",
                    "flow_uuid": str(flow.uuid) if flow else "",
                    "failure_message": message,
                    "written": totals.get("written", 0),
                    "failed": totals.get("failed", 0),
                    "skipped": totals.get("skipped", 0),
                    "invalid": totals.get("invalid", 0),
                },
                user_id=run.user_id,
                workspace_id=flow.workspace_id if flow else None,
            )
    except Exception:  # noqa: BLE001 — announcing must not fail a finished run
        logger.exception("Could not announce that integration run %s settled", run_id)


def _preview(final: Mapping[str, Any], totals: Mapping[str, int]) -> dict:
    """
    What the run page shows as the outcome, already capped and redacted.

    The totals rather than the outputs. ``outputs`` holds handles into a buffer that is
    about to be released, so storing it would put keys into a table that resolve to
    nothing — a preview whose every value is "gone" is worse than none.
    """
    return {
        "totals": dict(totals),
        "ended_at_node": str(final.get("failed_at") or ""),
    }


async def _record_unreached(run_id: int, final: Mapping[str, Any], compiled: Any) -> None:  # noqa: ANN401
    """A ``skipped`` row for every node the run never got to. See :func:`_settle`."""
    counts = (final or {}).get("counts") or {}
    outputs = (final or {}).get("outputs") or {}

    async with run_store.open_session() as db:
        logged = await step_node_ids(db, run_id)

    # The log, not just the state. A node that failed raised rather than returning, so it
    # contributes nothing to `counts` or `outputs` — and calling it unreached would write
    # it a `skipped` row on top of its `failed` one, two rows for one node contradicting
    # each other in the log somebody reads to find out what went wrong.
    reached = set(counts) | set(outputs) | logged

    for node_id, node in (compiled.node_by_id or {}).items():
        if node_id in reached:
            continue
        await run_store.record_step(
            run_id,
            node_id,
            flow_rules.node_type_of(node),
            flow_rules.label_of(node),
            STEP_SKIPPED,
            message="The run ended before this step.",
        )


# ---------------------------------------------------------------------------
# Stopping
# ---------------------------------------------------------------------------


async def request_stop(db: AsyncSession, run: IntegrationRun) -> None:
    """
    Ask a run to stop: the row first, then the local task.

    **That order, not the other.** Cancelling the task first races the write, and the page
    then shows a run that stopped with nothing on it saying why. Marking first also means
    a run executing in another worker stops at its next poll, which is the only mechanism
    available across processes.
    """
    await run_store.request_cancel(db, run.id)

    task = _RUNNING.get(run.id)
    if task is not None and not task.done():
        task.cancel()


async def stop_all_runs() -> None:
    """
    Cancel every live run, for ``on_shutdown``.

    A run in flight when the process stops is a run whose remaining steps will not happen.
    Cancelling deliberately at least lets the tasks unwind their sessions and release
    their buffers; without this they are torn down mid-request with the event loop, and
    the run row is left saying ``running`` forever until the queue reaps it.
    """
    tasks = [task for task in _RUNNING.values() if not task.done()]

    for task in tasks:
        task.cancel()

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def live_run_count() -> int:
    """How many runs are in flight in this process. For shutdown logging and for tests."""
    return len([task for task in _RUNNING.values() if not task.done()])


# ---------------------------------------------------------------------------
# Watching
# ---------------------------------------------------------------------------


async def watch_run(user_id: int, run_uuid: Any) -> AsyncIterator[dict]:
    """
    Frames describing one run, until it ends.

    **Every frame is a whole state, not a delta.** A client that missed one is not left
    holding a wrong total — which matters more here than it does for a query tool, because
    these numbers are records written into somebody's production system and a drifting
    counter is a number people would act on. The dock's polling fallback consumes the same
    shape for the same reason.

    **A short session per poll.** The worker driving the run writes through its own
    sessions, and a session held open here would keep serving the rows it first read: the
    page would sit still while the run moved. Polling rather than ``LISTEN/NOTIFY`` is one
    small query a second against a page somebody has deliberately opened to watch something
    happen.

    **It polls the database, not this process's task table.** A run may be executing in
    another worker entirely — that is the whole point of the queue — and a stream that read
    ``_RUNNING`` would show nothing for the commonest case in a multi-worker deployment.

    A run that has vanished yields one frame saying so rather than simply stopping, because
    a stream that stops is one the browser reconnects to.
    """
    waited = 0.0

    while waited <= MAX_STREAM_SECONDS:
        async with run_store.open_session() as db:
            found = await run_store.get_run_and_flow(db, run_uuid, user_id)

            if not found:
                yield {
                    "uuid": str(run_uuid),
                    "status": RUN_FAILED,
                    "error_message": NO_SUCH_RUN,
                }
                return

            run, flow = found
            frame = await run_store.run_view(db, run, flow)

        yield frame

        if run_store.is_terminal(frame.get("status")):
            return

        await asyncio.sleep(STREAM_POLL_SECONDS)
        waited += STREAM_POLL_SECONDS


# ---------------------------------------------------------------------------
# The heartbeat
# ---------------------------------------------------------------------------


async def _beat(run_id: int) -> None:
    """
    Say the worker is alive, every ``HEARTBEAT_SECONDS``, until cancelled.

    Every failure inside the loop is swallowed. A heartbeat that raised would take the run
    down for a reason entirely unrelated to what the run is doing — and a *missed* beat is
    already handled, by the queue eventually requeueing a job that looks dead.
    """
    try:
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            await run_store.heartbeat(run_id)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — a heartbeat must not fail a run
        logger.exception("Heartbeat stopped for run %s", run_id)


# ---------------------------------------------------------------------------
# Ending badly
# ---------------------------------------------------------------------------


async def _fail(db: AsyncSession, run: IntegrationRun, message: str) -> None:
    await run_store.mark_finished(db, run.id, RUN_FAILED, error_message=message)


async def _fail_by_id(run_id: int, message: str) -> None:
    async with run_store.open_session() as db:
        await run_store.mark_finished(db, run_id, RUN_FAILED, error_message=message)


async def _settle_cancelled(run_id: int) -> None:
    """
    Close a run whose task was cancelled from outside.

    Written in the ``except`` rather than left to the ``finally``, because the row has to
    say ``cancelled`` and not stay at ``running`` — a run that stopped and never said so
    is one the queue will eventually reap as a dead worker, which is a different and
    misleading story.
    """
    try:
        async with run_store.open_session() as db:
            await run_store.mark_finished(
                db, run_id, RUN_CANCELLED,
                error_message="This run was stopped.",
            )
    except Exception:  # noqa: BLE001 — already unwinding
        logger.exception("Could not record the cancellation of run %s", run_id)


def _readable(exc: Exception) -> str:
    """
    One sentence for a failure nobody drew a path for.

    A ``NodeFailure`` and an ``IntegrationFailure`` already speak to an operator, so they
    are shown. Anything else is a fault in this application — a driver error, a client
    error — and can name internal hosts or echo values, so the operator gets a fixed
    sentence and the detail goes to the application log. The same split the rest of the
    codebase makes between what is raised and what is logged.
    """
    if isinstance(exc, (NodeFailure, IntegrationFailure, RunCancelled)):
        return str(exc)
    return "This run could not be completed. The reason has been logged."
