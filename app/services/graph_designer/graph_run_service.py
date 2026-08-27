"""
Starting, watching, answering and stopping a run.

The orchestration layer: it owns the run's lifecycle and the background task that drives
it, and it is the only place that knows a run is asynchronous at all. ``graph_compiler``
builds the graph, ``node_runners`` do the work, ``run_store`` writes the rows; this
decides when.

## Why the run is a background task and not the request

A graph runs queries against somebody else's database and may pause on a question for as
long as a person takes to answer it. A request that held the run would either time out or
hold a worker for minutes, and a paused run has no request to belong to at all — the
answer arrives in a *different* one. So the request starts a task and returns a handle,
and the browser watches the run through the progress stream.

That is the same division ``downloader_agents`` makes between the request that offers an
export and the worker that builds it, arrived at from the same constraint. It does **not**
add a queue: an export is background work nobody is waiting for, whereas a run is watched
live by the person who pressed the button, so there is nothing to gain by making them
queue behind each other.

## Why the log is written by the nodes and read from the table

The task driving the run and the request streaming the dock are different tasks — and
behind more than one replica, different processes. So the nodes write step rows and the
stream polls them. See ``run_store`` and ``progress.py``: an in-memory bus would work
only in the configuration this application is not guaranteed to run in, and a browser
that reconnected mid-run would see half the story.

## One execution path

Testing one node, testing a group, and running the whole graph are the **same** function
with a different ``scope``. That is deliberate and it is the guarantee the feature rests
on: a node that passes a test is the node that will run, for the same reason
``documentations/QUERY_TEST.md`` insists the query that is tested is the query that will
be saved.
"""

import asyncio
import logging
import uuid as uuid_pkg
from typing import Any, AsyncIterator, Dict, List, Mapping, Optional, Sequence

from litestar.exceptions import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.graph_designer import (
    NODE_SQL,
    NODE_SQL_UNION,
    NODE_TOOL_CONFIG,
    NODE_VALUE,
    RUN_AWAITING_INPUT,
    RUN_CANCELLED,
    RUN_FAILED,
    RUN_SUCCEEDED,
    SCOPE_FULL,
    SCOPE_SELECTION,
)
from app.db.graph_designer.queries import fetch_run_with_graph
from app.services.graph_designer import graph_service, graph_state, run_store
from app.services.graph_designer.node_runners import RunContext
from app.utils import events

logger = logging.getLogger(__name__)

# How often the progress stream re-reads the run and its steps. One second: fast enough
# that a per-node dock moves as the run does, slow enough that watching a two-minute run
# costs a hundred and twenty small queries rather than twelve thousand. The same interval,
# for the same reason, as `progress.POLL_INTERVAL_SECONDS`.
POLL_INTERVAL_SECONDS = 1.0

# How long a stream stays open before it gives up. A browser tab left open on a paused run
# should not hold a connection forever, and a run waiting on a human may wait all day.
MAX_STREAM_SECONDS = 3600.0

# The node types whose output is a *result* rather than bookkeeping. A branch records
# which way it went, a loop records its cursor, a Success node records that it was
# reached — none of that is what the graph was built to produce, so none of it should be
# reported as the run's result. See `_result_preview`.
#
# A `sql_union` node **is** here. Its output is the builder dict while it accumulates and the
# rows on the pass it runs them, and the last thing it writes on a successful run is the rows —
# so leaving it out did not mean "no result", it meant *an earlier node's* result reported as
# this run's, which is the same silent wrong answer the `human` note below describes.
#
# A `human` node is deliberately **not** here, and that is not an oversight. Its output is
# the answer somebody gave, which is an input *to* the graph rather than a result *from*
# it — and because it usually runs late, counting it would let a yes/no shadow the rows a
# SQL node read earlier. Observed doing exactly that: a graph that read two departments
# reported "no rows" because the last data-ish output was `True`.
_DATA_NODE_TYPES = frozenset(
    {NODE_SQL, NODE_SQL_UNION, NODE_VALUE, NODE_TOOL_CONFIG},
)

# The sentence for a run that is not this user's, or is not there at all. One constant
# because four handlers say it, and they must say the same thing: a run belonging to
# somebody else is refused exactly as a missing one is, so the answer cannot be used to
# find out which uuids are real.
_NOT_FOUND = "That run could not be found."

# The tasks driving live runs, keyed by run uuid. Held so a run can be cancelled and so
# `on_shutdown` can stop them; a task nobody holds a reference to can also be garbage
# collected mid-run, which would stop a graph with no record of why.
_RUNNING: Dict[str, asyncio.Task] = {}


# --------------------------------------------------------------------------
# Starting
# --------------------------------------------------------------------------

async def start_run(
    db: AsyncSession,
    user_id: int,
    graph_id: uuid_pkg.UUID,
    scope: str = SCOPE_FULL,
    node_ids: Optional[Sequence[str]] = None,
    inputs: Optional[Mapping[str, Any]] = None,
) -> str:
    """
    Validate, record the run, and start driving it. Returns the run's public uuid.

    The drawing is validated **here**, before anything is compiled, using
    ``graph_service.validate_graph`` — the same function the save and the publish call. A
    run that validated more loosely than the save would be a run of a graph its author
    could not have stored, and a run that validated more strictly would refuse one they
    already had.

    A selection naming nothing that exists is refused rather than quietly widened to the
    whole graph: "run these three nodes" and "run everything" must never be the same
    request.
    """
    graph = await graph_service.get_graph(db, user_id, graph_id)

    graph_service.validate_graph(graph.graph_data)

    selection = _validated_selection(graph.graph_data, scope, node_ids)

    thread_id = run_store.new_thread_id()

    run = await run_store.create_run(
        db,
        graph_id=graph.id,
        scope=SCOPE_SELECTION if selection else SCOPE_FULL,
        selected_nodes=selection,
        inputs=inputs,
        thread_id=thread_id,
    )

    run_uuid = str(run.uuid)

    # Read off the row now, while there is a session: the background task opens its own
    # and must not touch a detached ORM instance from this one.
    task = asyncio.create_task(
        _drive(
            run_uuid=run_uuid,
            run_id=run.id,
            user_id=user_id,
            thread_id=thread_id,
            graph_data=dict(graph.graph_data or {}),
            selection=selection,
            inputs=dict(inputs or {}),
        ),
        name=f"graph-run-{run_uuid}",
    )

    _RUNNING[run_uuid] = task
    task.add_done_callback(lambda _task: _RUNNING.pop(run_uuid, None))

    return run_uuid


def _validated_selection(
    graph_data: Mapping[str, Any],
    scope: str,
    node_ids: Optional[Sequence[str]],
) -> Optional[List[str]]:
    """
    The node ids this run covers, checked against the graph, or ``None`` for all of them.

    Membership is checked here because this is the first point at which both the request
    and the drawing are in hand — the schema could only check the shape.
    """
    if scope != SCOPE_SELECTION:
        return None

    wanted = [str(node_id) for node_id in (node_ids or [])]

    if not wanted:
        raise HTTPException(
            status_code=400,
            detail="Select at least one node to test, or run the whole graph.",
        )

    present = {
        str(node.get("id")) for node in (graph_data.get("nodes") or [])
        if isinstance(node, dict)
    }

    missing = [node_id for node_id in wanted if node_id not in present]

    if missing:
        raise HTTPException(
            status_code=400,
            detail=(
                "Some of the selected nodes are no longer in this graph. Reload the "
                "page and select them again."
            ),
        )

    return wanted


# --------------------------------------------------------------------------
# Driving
# --------------------------------------------------------------------------

async def _drive(
    run_uuid: str,
    run_id: int,
    user_id: int,
    thread_id: str,
    graph_data: Mapping[str, Any],
    selection: Optional[Sequence[str]],
    inputs: Mapping[str, Any],
) -> None:
    """
    Compile the graph and run it until it finishes or pauses.

    Nothing raises out of here. This is a background task, so an exception would be
    swallowed by asyncio and the run would sit at ``running`` forever with no reason
    recorded — the worst outcome available, because the dock would spin indefinitely. So
    every failure is written onto the run row and the task ends normally.
    """
    from app.services.graph_designer import graph_compiler

    try:
        compiled = await graph_compiler.compile_graph(
            graph_data, RunContext(run_id, user_id), selection,
        )

        await graph_compiler.record_skipped(run_id, compiled.skipped)

        state = await compiled.graph.ainvoke(
            graph_state.initial_state(run_uuid, inputs),
            config=graph_compiler.run_config(compiled, thread_id),
        )

        await _settle(run_id, state, compiled.node_by_id)
    except asyncio.CancelledError:
        # Cancellation is a user pressing Stop. Recorded rather than swallowed, then
        # re-raised so asyncio sees the task as cancelled rather than as one that
        # returned — `cancel_run` writes the row, so this only has to not overwrite it.
        raise
    except Exception as exc:  # noqa: BLE001 — a run must never end with no reason
        logger.exception("Graph run %s failed", run_uuid)

        async with run_store.open_session() as db:
            await run_store.mark_finished(
                db,
                run_id,
                RUN_FAILED,
                error_message=_readable_failure(exc),
            )


async def _settle(
    run_id: int,
    state: Any,
    node_by_id: Optional[Mapping[str, Any]] = None,
) -> None:
    """
    Record how a finished ``ainvoke`` ended: paused, failed, or succeeded.

    The pause is read off the returned state rather than caught as an exception, because
    that is how LangGraph reports it — see ``graph_compiler.interrupt_payload``, and
    ``download_graph.start_export_offer``, which reads it the same way.
    """
    from app.services.graph_designer import graph_compiler

    payload = graph_compiler.interrupt_payload(state)

    settled_status = ""
    settled_message = ""

    async with run_store.open_session() as db:
        if payload is not None:
            await run_store.mark_awaiting(db, run_id, payload)
            return

        failed_at = str((state or {}).get("failed_at") or "")

        if failed_at:
            settled_status = RUN_FAILED
            settled_message = (
                str((state or {}).get("failure_message") or "")
                or "The run stopped at a node that could not complete."
            )
            await run_store.mark_finished(
                db,
                run_id,
                RUN_FAILED,
                error_message=settled_message,
            )
        else:
            settled_status = RUN_SUCCEEDED
            await run_store.mark_finished(
                db,
                run_id,
                RUN_SUCCEEDED,
                result_preview=_result_preview(state, node_by_id),
            )

    # Announce it, outside the session and after the commit. A subscriber — an email trigger,
    # today — opens its own session, so telling it while this transaction was still open
    # would have it read a database that does not yet agree with the event. `publish` never
    # raises, so a broken template cannot turn a finished run into a failed one.
    #
    # A paused run returns above and announces nothing: "awaiting an answer" is not an
    # outcome, and a trigger firing on it would email somebody every time a graph asked a
    # question.
    await _announce_settled(run_id, settled_status, settled_message)


async def _announce_settled(run_id: int, status: str, message: str) -> None:
    """
    Publish ``graph_run.settled`` for a run that has just reached a terminal state.

    Reads the run again for the two things a subscriber needs and this function does not
    already hold: whose run it was, and its public uuid. Cheap, once per run, and it keeps
    ``_settle`` from having to thread them through.

    Swallows its own failures. The run is already recorded; an event that could not be
    published is worth a log line and nothing more.
    """
    try:
        async with run_store.open_session() as db:
            run = await run_store.reload_run(db, run_id)
            if run is None:
                return
            found = await fetch_run_with_graph(db, run.uuid)
            if not found:
                return
            _, graph = found

            await events.publish(
                events.EVENT_GRAPH_RUN_SETTLED,
                {
                    "run_uuid": str(run.uuid),
                    "status": status,
                    "graph_name": graph.name,
                    "graph_uuid": str(graph.uuid),
                    "failure_message": message,
                },
                # Ownership runs run -> graph -> user, which is why the graph is read at all
                # rather than the run's own column being trusted for it.
                user_id=graph.user_id,
                workspace_id=graph.workspace_id,
            )
    except Exception:  # noqa: BLE001 — announcing must not fail a finished run
        logger.exception("Could not announce that graph run %s settled", run_id)


def _result_preview(
    state: Any,
    node_by_id: Optional[Mapping[str, Any]] = None,
) -> Optional[dict]:
    """
    What the run produced, as one capped preview.

    **The last node that produced data**, not simply the last node to run. That
    distinction is the whole function: a graph almost always ends at a Success node, whose
    output is the bookkeeping ``{"succeeded": true}``, so "the last output" reports a graph
    that read two hundred rows as having returned nothing. Observed doing exactly that —
    the SQL node's rows were in state and the result said "no rows".

    So the newest output belonging to a node in ``_DATA_NODE_TYPES`` is taken. An
    **allow-list**, deliberately, rather than a list of types to skip: every node type added
    since has been one whose output is bookkeeping rather than data — a queued email's
    receipt, a person's answer, a loop's cursor — and each would have had to be remembered
    and excluded. An Email node drawn after a Success node is the current case in point;
    with a deny-list it would silently become "what this graph returned".

    ``node_by_id`` is how the types are known; without it this falls back to the last
    output, which is the best guess available and is only reached if a caller has nothing to
    identify the nodes with.

    Capped by ``graph_state.preview_of`` like every other stored preview, so a run over a
    large result set does not put that result set on the run row.
    """
    outputs = (state or {}).get("outputs") or {}

    if not outputs:
        return None

    chosen = None

    for node_id in outputs:
        if node_by_id is None:
            chosen = node_id
            continue

        node = node_by_id.get(node_id) or {}

        if str(node.get("type")) in _DATA_NODE_TYPES:
            chosen = node_id

    if chosen is None:
        # Every node in the run was control flow — a graph of nothing but a Start and a
        # Success, say. Reported as the last output rather than as nothing, because the
        # run did happen and "no result" is different from "no run".
        chosen = list(outputs.keys())[-1]

    return {
        "node_id": chosen,
        "output": graph_state.preview_of(outputs[chosen]),
    }


def _readable_failure(exc: Exception) -> str:
    """
    A sentence about a run that fell over outside any node.

    ``GraphRecursionError`` is called out by name because it is the one failure here whose
    default message would send an operator looking in entirely the wrong place: it reads
    as an internal limit, and what it actually means is that a loop did not finish. The
    limit is computed from the drawing (see ``graph_compiler._recursion_limit``), so
    reaching it really does mean the graph did not converge.
    """
    name = type(exc).__name__

    if name == "GraphRecursionError":
        return (
            "The run went round more times than this graph allows without finishing. "
            "Check that every loop's condition can be met, and that a loop's body "
            "leads back to it."
        )

    if isinstance(exc, HTTPException):
        return str(exc.detail)

    return "The run could not be completed. The reason has been logged."


# --------------------------------------------------------------------------
# Watching
# --------------------------------------------------------------------------

async def get_run(
    db: AsyncSession,
    user_id: int,
    run_uuid: uuid_pkg.UUID,
) -> dict:
    """
    One run's whole state — the polling response, and what the page opens on.

    A run whose graph belongs to somebody else is a 404 with the same sentence a missing
    one gets. Answering differently would confirm the uuid is real.
    """
    found = await run_store.get_run_and_graph(db, run_uuid, user_id)

    if not found:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)

    run, graph = found
    return await run_store.run_view(db, run, graph)


async def full_result(
    db: AsyncSession,
    user_id: int,
    run_uuid: uuid_pkg.UUID,
) -> Any:
    """
    A finished run's last data-producing output **in full**, or ``None``.

    The uncapped counterpart of ``result_preview``, and the only reason it exists is that
    the preview is a *sample*. ``graph_state.preview_of`` caps a stored preview at twenty
    rows with the real total beside it, which is exactly right for a log somebody reads
    and exactly wrong for a caller that is going to *use* the values: a graph embedded in
    a tool config supplies that tool's filter, and a filter built from the first twenty of
    five hundred ids answers a different question than the one asked, silently. That is
    the failure the row caps were removed to prevent, and reading a preview here would
    have reintroduced it one layer up.

    So the full output is read from the **checkpointer**, where it already is. The graph is
    recompiled from the stored drawing to get at it, which is the same move
    :func:`_resume` makes and for the same reason: a compiled graph is a set of closures
    that cannot outlive a request, while the checkpointer holds the state and the persisted
    ``thread_id`` is the handle to it. Nothing is re-run — ``aget_state`` reads.

    The alternative was storing the full output on the run row, which is the thing
    ``preview_of`` exists to prevent: a run over a large result set would put that result
    set in a log table, once per loop pass.

    Returns ``None`` when there is nothing to read — no checkpoint, no outputs, or a run
    that never got as far as producing data. A caller distinguishes "no values" from
    "could not read" by the run's status, which it already has.
    """
    from app.services.graph_designer import graph_compiler

    found = await run_store.get_run_and_graph(db, run_uuid, user_id)

    if not found:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)

    run, graph = found

    if not run.thread_id:
        return None

    compiled = await graph_compiler.compile_graph(
        graph.graph_data or {},
        RunContext(run.id, user_id),
        list(run.selected_nodes or []) or None,
    )

    snapshot = await compiled.graph.aget_state(
        graph_compiler.run_config(compiled, run.thread_id),
    )
    state = getattr(snapshot, "values", None) or {}

    return _last_data_output(state, compiled.node_by_id)


def _last_data_output(
    state: Any,
    node_by_id: Optional[Mapping[str, Any]] = None,
) -> Any:
    """
    The newest output of a node that produces data, uncapped.

    The same choice :func:`_result_preview` makes — skip the terminal, branch and loop
    nodes, take the newest genuine result — without the capping. Split out rather than
    parameterised because the two callers want different *types* back: a preview dict for
    a log row, and the value itself for something that is going to use it.
    """
    outputs = (state or {}).get("outputs") or {}

    if not outputs:
        return None

    chosen = None

    for node_id in outputs:
        if node_by_id is None:
            chosen = node_id
            continue

        if str((node_by_id.get(node_id) or {}).get("type")) in _DATA_NODE_TYPES:
            chosen = node_id

    return outputs.get(chosen) if chosen is not None else None


async def stream_run(
    user_id: int,
    run_uuid: uuid_pkg.UUID,
) -> AsyncIterator[dict]:
    """
    Frames describing the run, until it ends.

    **Every frame is a whole state, not a delta.** A client that missed one is not left
    with a wrong picture, and a browser that reconnects sees the run's whole history
    rather than only what happened after it arrived. That is
    ``progress.stream_progress``' contract and it is worth keeping identical, because the
    dock's polling fallback consumes the same shape.

    **A short session per poll.** The task driving the run writes through its own
    sessions, and a session held open here would keep serving the rows it first read —
    the dock would sit still while the run moved. Polling rather than LISTEN/NOTIFY is
    one small query a second against a page somebody has deliberately opened to watch
    something happen.

    The stream ends on a terminal status, or when it has been open too long. It does
    **not** end when a run pauses on a question: that is exactly when somebody is
    watching, and the frame carrying the question is the one the dock needs.
    """
    waited = 0.0

    while waited <= MAX_STREAM_SECONDS:
        async with run_store.open_session() as db:
            found = await run_store.get_run_and_graph(db, run_uuid, user_id)

            if not found:
                # Deleted, or never theirs. One frame saying so beats a stream that
                # simply stops, which a browser would reconnect to.
                yield {"uuid": str(run_uuid), "status": RUN_FAILED,
                       "error_message": _NOT_FOUND}
                return

            run, graph = found
            frame = await run_store.run_view(db, run, graph)

        yield frame

        if run_store.is_terminal(frame.get("status")):
            return

        await asyncio.sleep(POLL_INTERVAL_SECONDS)
        waited += POLL_INTERVAL_SECONDS


# --------------------------------------------------------------------------
# Answering
# --------------------------------------------------------------------------

async def resume_run(
    db: AsyncSession,
    user_id: int,
    run_uuid: uuid_pkg.UUID,
    answer: Any,
) -> dict:
    """
    Give a paused run its answer and set it going again.

    The answer is validated against the node's own ``expects`` **before** the run is
    resumed, so a bad answer is refused while the person is still looking at the prompt.
    Resuming and failing a node three steps later would be technically equivalent and
    much less useful.

    The resumed run continues *from inside* the waiting ``interrupt()`` rather than
    starting again, which is what makes everything the run had already established still
    valid. That is what the persisted ``thread_id`` buys.
    """
    from app.services.graph_designer import graph_compiler

    found = await run_store.get_run_and_graph(db, run_uuid, user_id)

    if not found:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)

    run, graph = found

    if run.status != RUN_AWAITING_INPUT or not run.interrupt_payload:
        raise HTTPException(
            status_code=400,
            detail="That run is not waiting for an answer.",
        )

    try:
        value = graph_compiler.validated_answer(run.interrupt_payload, answer)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    await run_store.mark_resumed(db, run.id)

    run_id = run.id
    thread_id = run.thread_id
    graph_data = dict(graph.graph_data or {})
    selection = list(run.selected_nodes or []) or None
    identifier = str(run.uuid)

    task = asyncio.create_task(
        _resume(
            run_uuid=identifier,
            run_id=run_id,
            user_id=user_id,
            thread_id=thread_id,
            graph_data=graph_data,
            selection=selection,
            answer=value,
        ),
        name=f"graph-run-{identifier}",
    )

    _RUNNING[identifier] = task
    task.add_done_callback(lambda _task: _RUNNING.pop(identifier, None))

    return await run_store.run_view(db, run, graph)


async def _resume(
    run_uuid: str,
    run_id: int,
    user_id: int,
    thread_id: str,
    graph_data: Mapping[str, Any],
    selection: Optional[Sequence[str]],
    answer: Any,
) -> None:
    """
    Carry a paused run on from its ``interrupt()``.

    The graph is **recompiled**, which sounds wasteful and is the correct thing to do: a
    compiled graph is a set of closures and cannot be kept across requests, and the
    checkpointer holds the *state*, not the topology. Recompiling from the same stored
    drawing produces the same graph, and the thread id is what makes the run continue
    rather than restart.

    Skipped rows are **not** written again — they were written when the run started, and
    a second set would double every skipped node in the log.
    """
    from app.services.graph_designer import graph_compiler

    try:
        compiled = await graph_compiler.compile_graph(
            graph_data, RunContext(run_id, user_id), selection,
        )

        state = await compiled.graph.ainvoke(
            graph_compiler.resume_command(answer),
            config=graph_compiler.run_config(compiled, thread_id),
        )

        await _settle(run_id, state, compiled.node_by_id)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — see _drive
        logger.exception("Graph run %s failed after resuming", run_uuid)

        async with run_store.open_session() as db:
            await run_store.mark_finished(
                db, run_id, RUN_FAILED, error_message=_readable_failure(exc),
            )


# --------------------------------------------------------------------------
# Stopping
# --------------------------------------------------------------------------

async def cancel_run(
    db: AsyncSession,
    user_id: int,
    run_uuid: uuid_pkg.UUID,
) -> dict:
    """
    Stop a run.

    The row is marked **before** the task is cancelled, so the frame the dock is about to
    read already says ``cancelled`` — otherwise the task's own teardown could race the
    write and the dock would show a run that stopped for no stated reason.

    Steps already recorded are left alone. A cancelled run's log is the most useful thing
    about it: it says how far it got.
    """
    found = await run_store.get_run_and_graph(db, run_uuid, user_id)

    if not found:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)

    run, graph = found

    if run_store.is_terminal(run.status):
        # Already over. Returned rather than refused: pressing Stop on a run that has
        # just finished is an ordinary race, not a mistake to report.
        return await run_store.run_view(db, run, graph)

    await run_store.mark_finished(
        db,
        run.id,
        RUN_CANCELLED,
        error_message="Stopped before it finished.",
    )

    task = _RUNNING.get(str(run.uuid))

    if task is not None and not task.done():
        task.cancel()

    return await run_store.run_view(db, run, graph)


async def stop_all_runs() -> None:
    """
    Cancel every live run, for ``on_shutdown``.

    A run in flight when the process stops is a run whose remaining nodes will not
    happen. Cancelling deliberately at least lets the tasks unwind their sessions;
    without this they are torn down mid-query with the event loop.
    """
    tasks = [task for task in _RUNNING.values() if not task.done()]

    for task in tasks:
        task.cancel()

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def live_run_count() -> int:
    """How many runs are in flight. Used by tests and by shutdown logging."""
    return len([task for task in _RUNNING.values() if not task.done()])
