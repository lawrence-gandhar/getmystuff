"""
Every read and write of a run and its steps, and the session a graph node runs in.

This module exists to be the one place that touches ``tool_graph_runs`` and
``tool_graph_run_steps``. Both the node runners and the orchestrator need those writes,
and if each opened its own sessions and built its own rows they would be two answers to
"what does a step row look like" — which is the sort of disagreement that shows up as a
dock with gaps in it.

**The session seam.** ``open_session`` is called through this module rather than
``AsyncSessionLocal`` being imported directly, for the reason
``download_service.open_session`` documents: a LangGraph node has no injected session,
and a test has to be able to point one at the test database. One patchable name.

**Why a node opens its own short session per write** instead of holding one for the
duration of its work. A node's work is a query against *somebody else's* database that
can take a while; holding this application's own connection open across it would tie up
a pool slot for the length of an external call. It is the same shape
``progress.stream_progress`` uses for its poll loop, and the reason is the same.
"""

import logging
import uuid as uuid_pkg
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.db_sessions import AsyncSessionLocal
from app.db.db_utils import CRUDQueryBuilder
from app.db.graph_designer.queries import (
    fetch_run_steps,
    fetch_run_with_graph,
    next_step_sequence,
)
from app.models.graph_designer import (
    RUN_AWAITING_INPUT,
    RUN_RUNNING,
    STEP_RUNNING,
    TERMINAL_RUN_STATUSES,
    ToolGraph,
    ToolGraphRun,
    ToolGraphRunStep,
)

logger = logging.getLogger(__name__)

run_crud = CRUDQueryBuilder(ToolGraphRun)
step_crud = CRUDQueryBuilder(ToolGraphRunStep)


def open_session() -> AsyncSession:
    """
    A session of this module's own, for code that has no request.

    Used by every graph node and by the background task that drives a run. An
    ``async with`` context manager, so a caller cannot forget to close it — which
    matters more here than in a route, where Litestar closes it either way.
    """
    return AsyncSessionLocal()


def new_thread_id() -> str:
    """
    A fresh checkpointer thread for one run.

    Its own uuid rather than the run's, and that is not redundancy. The run's uuid is
    public — it appears in the dock's URL — and a checkpointer thread is the handle on
    a parked graph's whole state, so the two are kept separate on principle.
    """
    return str(uuid_pkg.uuid4())


# --------------------------------------------------------------------------
# Runs
# --------------------------------------------------------------------------

async def create_run(
    db: AsyncSession,
    graph_id: int,
    scope: str,
    selected_nodes: Optional[Sequence[str]],
    inputs: Optional[Mapping[str, Any]],
    thread_id: str,
) -> ToolGraphRun:
    """
    Record a run before the graph is compiled.

    Before, deliberately. A compilation that fails is then a run somebody can open and
    read a reason from, rather than a button that appeared to do nothing. The failure
    is written onto this row by ``mark_failed``.
    """
    return await run_crud.create(db, {
        "tool_graph_id": graph_id,
        "status": RUN_RUNNING,
        "scope": scope,
        "selected_nodes": list(selected_nodes) if selected_nodes else None,
        "inputs": dict(inputs or {}) or None,
        "thread_id": thread_id,
    })


async def get_run_and_graph(
    db: AsyncSession,
    run_uuid: Any,
    user_id: int,
) -> Optional[tuple]:
    """
    One run with its graph, only if this user owns the graph.

    Ownership runs run → graph → ``user_id``. A run whose graph belongs to somebody
    else comes back as ``None`` — the caller turns that into the same "not found" a
    missing run gets, because answering differently would confirm the uuid is real.
    """
    found = await fetch_run_with_graph(db, run_uuid)

    if not found:
        return None

    run, graph = found

    if graph.user_id != user_id:
        return None

    return run, graph


async def mark_awaiting(
    db: AsyncSession,
    run_id: int,
    payload: Mapping[str, Any],
) -> None:
    """
    Park a run on a question.

    The payload is stored rather than only sent, because the dock may be opened by a
    browser that was not watching when the interrupt fired — and because the resume
    endpoint has to know what question is outstanding before it accepts an answer.
    """
    await run_crud.update(db, run_id, {
        "status": RUN_AWAITING_INPUT,
        "interrupt_payload": dict(payload),
    })


async def mark_resumed(db: AsyncSession, run_id: int) -> None:
    """
    Clear the outstanding question and put the run back to work.

    Clearing matters: a payload left in place is a question the dock would keep
    offering, and an answer accepted twice would resume a thread that has already
    moved on.
    """
    await run_crud.update(db, run_id, {
        "status": RUN_RUNNING,
        "interrupt_payload": None,
    })


async def mark_finished(
    db: AsyncSession,
    run_id: int,
    status: str,
    result_preview: Optional[Mapping[str, Any]] = None,
    error_message: Optional[str] = None,
) -> None:
    """
    Close a run off — succeeded, failed or cancelled.

    ``interrupt_payload`` is cleared on every terminal path, including the failure one:
    a finished run with a question still attached would render a prompt nobody can
    answer.
    """
    await run_crud.update(db, run_id, {
        "status": status,
        "interrupt_payload": None,
        "result_preview": dict(result_preview) if result_preview else None,
        "error_message": error_message or None,
        "finished_at": datetime.now(timezone.utc),
    })


# --------------------------------------------------------------------------
# Steps
# --------------------------------------------------------------------------

async def begin_step(
    run_id: int,
    node_id: str,
    node_type: str,
    node_label: str,
    iteration: int = 0,
) -> Optional[int]:
    """
    Write the ``running`` row for a node that has just started, and return its id.

    Written **before** the node does its work, which is the whole reason a step is two
    writes instead of one. A node that hangs or whose process dies is then visible as a
    step that never finished; recorded only on completion, it would be indistinguishable
    from a node the run never reached.

    Returns ``None`` if the row could not be written. A failure to log must not fail the
    node — the log is an observation of the run, not part of it — so this is the one
    place in the module that swallows an exception, and it says so in the application
    log instead.
    """
    try:
        async with open_session() as db:
            sequence = await next_step_sequence(db, run_id)
            step = await step_crud.create(db, {
                "run_id": run_id,
                "sequence": sequence,
                "node_id": node_id,
                "node_type": node_type,
                "node_label": node_label,
                "iteration": iteration,
                "status": STEP_RUNNING,
            })
            return step.id
    except Exception:  # noqa: BLE001 — logging must not break the run
        logger.exception(
            "Could not record the start of node %s on run %s", node_id, run_id,
        )
        return None


async def finish_step(
    step_id: Optional[int],
    status: str,
    duration_ms: Optional[int] = None,
    message: Optional[str] = None,
    output_preview: Optional[Mapping[str, Any]] = None,
    state_preview: Optional[Mapping[str, Any]] = None,
) -> None:
    """
    Complete the row ``begin_step`` opened.

    A ``None`` ``step_id`` means the opening write failed and there is nothing to
    update; the node still ran, and its outcome is still in the application log. Same
    swallow, same reason as above.
    """
    if step_id is None:
        return

    try:
        async with open_session() as db:
            await step_crud.update(db, step_id, {
                "status": status,
                "duration_ms": duration_ms,
                "message": message,
                "output_preview": dict(output_preview) if output_preview else None,
                "state_preview": dict(state_preview) if state_preview else None,
                "finished_at": datetime.now(timezone.utc),
            })
    except Exception:  # noqa: BLE001 — logging must not break the run
        logger.exception("Could not record the end of step %s", step_id)


async def record_step(
    run_id: int,
    node_id: str,
    node_type: str,
    node_label: str,
    status: str,
    message: Optional[str] = None,
    iteration: int = 0,
) -> None:
    """
    One complete step row in a single write, for an outcome with no duration.

    Used for a ``skipped`` node — one outside a tested selection — where there is no
    work to time and nothing to observe part-way through. A skipped node gets a row at
    all because **a node missing from the log is indistinguishable from a node the run
    never reached**, and "I only tested these two" is exactly what somebody reading a
    selection run needs the log to tell them.
    """
    try:
        async with open_session() as db:
            sequence = await next_step_sequence(db, run_id)
            await step_crud.create(db, {
                "run_id": run_id,
                "sequence": sequence,
                "node_id": node_id,
                "node_type": node_type,
                "node_label": node_label,
                "iteration": iteration,
                "status": status,
                "message": message,
                "finished_at": datetime.now(timezone.utc),
            })
    except Exception:  # noqa: BLE001 — logging must not break the run
        logger.exception("Could not record node %s on run %s", node_id, run_id)


# --------------------------------------------------------------------------
# Views
# --------------------------------------------------------------------------

async def run_view(db: AsyncSession, run: ToolGraphRun, graph: ToolGraph) -> dict:
    """
    One run and every step it has taken — the SSE frame and the polling body alike.

    The same shape for both on purpose: a client whose stream dropped and fell back to
    polling must not have to understand a second payload. **A whole state, never a
    delta**, for the reason ``progress.py`` gives — a consumer that missed a frame is
    not left holding a wrong total.
    """
    steps = await fetch_run_steps(db, run.id)

    return {
        "uuid": str(run.uuid),
        "graph_uuid": str(graph.uuid),
        "status": run.status,
        "scope": run.scope,
        "selected_nodes": list(run.selected_nodes or []),
        "interrupt_payload": dict(run.interrupt_payload) if run.interrupt_payload else None,
        "result_preview": dict(run.result_preview) if run.result_preview else None,
        "error_message": run.error_message,
        "steps": [_step_view(step) for step in steps],
        "started_at": run.started_at,
        "finished_at": run.finished_at,
    }


def _step_view(step: ToolGraphRunStep) -> dict:
    """One step row as the dock reads it."""
    return {
        "uuid": str(step.uuid),
        "sequence": step.sequence,
        "node_id": step.node_id,
        "node_type": step.node_type,
        "node_label": step.node_label,
        "iteration": step.iteration,
        "status": step.status,
        "duration_ms": step.duration_ms,
        "message": step.message,
        "output_preview": dict(step.output_preview) if step.output_preview else None,
        "state_preview": dict(step.state_preview) if step.state_preview else None,
        "started_at": step.started_at,
        "finished_at": step.finished_at,
    }


def is_terminal(status: Optional[str]) -> bool:
    """Whether a run is over, and the progress stream should stop."""
    return str(status or "") in TERMINAL_RUN_STATUSES


async def reload_run(db: AsyncSession, run_id: int) -> Optional[ToolGraphRun]:
    """
    Re-read one run row.

    Used by the poll loop, which must see writes made by the *other* task driving the
    run. A session caches identity-mapped rows, so the loop opens a fresh session per
    poll rather than re-reading through one it has held — see ``open_session``'s note.
    """
    return await run_crud.get_one(db, filters={"id": run_id})


async def steps_map(db: AsyncSession, run_id: int) -> Dict[str, List[dict]]:
    """
    One run's steps grouped by node id, for repainting the canvas.

    Grouped because a node inside a loop has one row per pass and the canvas draws one
    ring per node: the ring shows the latest pass, and the dock lists them all.
    """
    grouped: Dict[str, List[dict]] = {}

    for step in await fetch_run_steps(db, run_id):
        grouped.setdefault(step.node_id, []).append(_step_view(step))

    return grouped
