"""
Building one export, as the graph it is.

    START → count_records ──too large──→ notify_failure ──┐
                  │                                       │
                 ask                                      │
                  ↓                                       │
        await_confirmation ──declined──────────────────────┼──→ cleanup → END
                  │                                       │
              confirmed                                   │
                  ↓                                       │
          ┌── write_batch ──more batches──┐               │
          └───────────────────────────────┘               │
                  │            │                          │
               finished      failed ────────────→ notify_failure
                  ↓
            merge_parts ──failed──────────────────→ notify_failure
                  │
              publish_artifact ──────────────────────→ cleanup → END

**Why a graph rather than a function with a loop.** Two of the edges above are the
feature, not decoration. ``await_confirmation`` is a genuine pause — the run stops
inside a chat turn, its state is written to PostgreSQL, and it is resumed by a different
task after a different HTTP request. That is what a checkpointed graph and
``interrupt()`` are for, and a function would have to reinvent both. The second is that
every terminal path — refused, declined, built, failed — passes through ``cleanup``,
which is visible here as one node with several inbound edges rather than as a
``finally`` block that has to be right in five places.

**Where the interrupt goes and where it comes back.** :func:`start_export_offer` runs the
graph in the *request* that answered the user's question: it counts, it pauses, and the
payload it gets back is the sentence the agent says out loud. The user's "yes" enqueues a
job, and the worker calls :func:`resume_export` — same ``thread_id``, same checkpoint,
the run continues from the pause as if nothing had happened in between. The request side
never builds anything and the worker side never asks anything.

**The retry loop is inside ``write_batch``, not around it.** Three attempts per batch with
the part file deleted between them is ``retry.run_batch_with_retries``, called by the
node. It was tempting to make the retry an edge — ``write_batch → discard_part →
write_batch`` — and it would be worse: every attempt would be a checkpoint write, the
conditional router would have to distinguish "retry this batch" from "go to the next
one" from "give up", and a crash mid-retry would resume into a state the reader's cursor
no longer matches. A worker that dies is already handled, one level up, by the job being
requeued.

**The recursion limit is computed, not defaulted.** ``write_batch`` loops back to itself
once per batch, and every hop counts against LangGraph's recursion limit — which
defaults to 25, or one and a quarter thousand records. See :data:`_RECURSION_LIMIT`.
"""

import logging
from pathlib import Path
from typing import Any, Optional

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from app.services.deep_agents.query_executor import ToolQueryError
from app.services.downloader_agents.base import download_service as svc
from app.services.downloader_agents.base import part_store
from app.services.downloader_agents.base.checkpointer import get_checkpointer
# `svc` above and these names are both deliberate: the pure helpers read better unqualified,
# and `open_session` is called through the module so the session factory is one patchable
# seam — a graph node has no injected session, and a test has to be able to point it at the
# test database. See tests/unit/services/downloader_agents/conftest.graph_sessions.
from app.services.downloader_agents.base.download_service import (
    FAILURE_MESSAGE,
    ExportContext,
    forget_context,
    load_context,
    mark_declined,
    mark_failed,
    mark_parts_merged,
    mark_ready,
    offer_sentence,
    require_export,
    record_discarded_part,
    record_part,
    thread_id_for,
    too_large_message,
)
from app.services.downloader_agents.base.download_state import (
    DownloadState,
    initial_state,
)
from app.services.downloader_agents.base.part_writer import forget_export_caches
from app.services.downloader_agents.base import record_reader
from app.services.downloader_agents.base.record_reader import (
    BATCH_SIZE,
    MAX_EXPORT_ROWS,
    count_records,
    get_reader,
    release_reader,
)
from app.services.downloader_agents.base.retry import (
    MAX_BATCH_ATTEMPTS,
    BatchRetriesExhausted,
    run_batch_with_retries,
)
from app.utils.file_utils import compute_checksum

logger = logging.getLogger(__name__)


# One hop per batch, plus the fixed nodes either side, plus room to spare. The default
# of 25 would stop an export at 1,250 records — and it would stop it by raising
# GraphRecursionError, which reads like a bug in the graph rather than like a limit
# being hit.
_RECURSION_LIMIT = (MAX_EXPORT_ROWS // BATCH_SIZE) + 50

# Node names. Constants because they appear in the edge wiring, in the progress log and
# in the tests, and a typo in any one of them is a silent misroute.
COUNT_RECORDS = "count_records"
AWAIT_CONFIRMATION = "await_confirmation"
WRITE_BATCH = "write_batch"
MERGE_PARTS = "merge_parts"
PUBLISH_ARTIFACT = "publish_artifact"
NOTIFY_FAILURE = "notify_failure"
CLEANUP = "cleanup"

# Compiled once per process. Building a StateGraph is cheap but not free, and every
# export uses the identical graph — only the state and the thread differ.
_graph = None


# --------------------------------------------------------------------------
# Nodes
# --------------------------------------------------------------------------

async def _count_records(state: DownloadState) -> dict:
    """
    Establish the exact number of records, and refuse a set too large to export.

    The count is the offer's whole substance, so it happens before the user is asked
    anything. Refusing here rather than after the confirmation is deliberate: asking
    someone whether they would like a file and then telling them it was never possible
    is worse than telling them straight away.
    """
    export_uuid = state["export_id"]

    async with svc.open_session() as db:
        export = await require_export(db, export_uuid)
        context = await load_context(db, export)

        counted = await count_records(context.source)

        export.total_rows = counted.total
        export.count_is_lower_bound = counted.is_lower_bound
        await db.commit()

        if counted.exceeds_ceiling:
            # Read through the module, not from the name imported above: the ceiling is
            # configurable, and a copy taken at import time would let this message and
            # the check that produced it disagree about what the limit is.
            ceiling = record_reader.MAX_EXPORT_ROWS

            await mark_failed(
                db,
                export,
                reason=f"{counted.total} record(s) is past the {ceiling} ceiling",
                user_message=too_large_message(counted.total, ceiling),
            )

            return {
                "total_rows": counted.total,
                "count_is_lower_bound": counted.is_lower_bound,
                "failure": "result set exceeds the export ceiling",
            }

    return {
        "total_rows": counted.total,
        "count_is_lower_bound": counted.is_lower_bound,
    }


async def _await_confirmation(state: DownloadState) -> dict:
    """
    Stop, and ask whether to build the file.

    ``interrupt()`` suspends the whole run here. What it returns is whatever the resume
    passes back, and what it *raises* on the way out carries the payload below to the
    caller — which is how the offer sentence reaches the agent.

    The sentence is built here and sent as a value rather than being composed by
    whoever displays it: it contains the record count, and there is exactly one place
    that knows the count is exact.
    """
    total_rows = int(state.get("total_rows") or 0)
    file_format = state.get("file_format") or "csv"

    answer = interrupt(
        {
            "export_id": state["export_id"],
            "total_rows": total_rows,
            "file_format": file_format,
            "question": offer_sentence(total_rows, file_format),
        }
    )

    confirmed, chosen_format = _read_answer(answer, file_format)

    async with svc.open_session() as db:
        export = await require_export(db, state["export_id"])

        if not confirmed:
            await mark_declined(db, export)
            return {"confirmed": False}

        # The chosen format is written to the row here, not only by whoever called the
        # resume. The offer promised CSV and the answer may have been "yes, as a
        # spreadsheet", and every node after this one reloads its context *from the row* —
        # so a format that lived only in graph state would produce a CSV for someone who
        # asked for a workbook. Persisting it in the node that learns it means the graph
        # is right regardless of what the caller remembered to do.
        if export.file_format != chosen_format:
            export.file_format = chosen_format
            await db.commit()

    return {"confirmed": True, "file_format": chosen_format}


async def _write_batch(state: DownloadState) -> dict:
    """
    Read one batch of :data:`BATCH_SIZE` records and write it as one part file.

    Both halves are inside the retry, because both can fail for the same transient
    reasons and a batch is only done when its file is on disk. Up to
    :data:`MAX_BATCH_ATTEMPTS` attempts; the part file is deleted before each retry and
    the discarded attempt is recorded, so a retried export can be told apart from a
    clean one afterwards.

    Returning ``finished_reading`` is how the loop ends: an empty read means the cursor
    is exhausted, and that is the only thing that means it.
    """
    export_uuid = state["export_id"]
    batch_number = int(state.get("batch_number") or 1)

    async with svc.open_session() as db:
        export = await require_export(db, export_uuid)
        context = await load_context(db, export)

    await part_store.ensure_parts_dir(export_uuid)

    reader = get_reader(export_uuid, context.source)
    path = part_store.part_path(export_uuid, batch_number, context.extension)

    try:
        written = await run_batch_with_retries(
            lambda attempt: _attempt_batch(context, reader, batch_number, attempt, path),
            batch_number=batch_number,
            on_discard=_discard_after(context, path),
            max_attempts=MAX_BATCH_ATTEMPTS,
        )
    except (BatchRetriesExhausted, ToolQueryError) as exc:
        # Not raised onward. A failed export is an outcome this graph handles — the
        # notify and cleanup nodes exist for it — and raising here would abandon the
        # part files and leave the export row saying "building" forever.
        return {"failure": str(exc)}

    if written is None:
        return {"finished_reading": True}

    return {
        "batch_number": batch_number + 1,
        "attempts": 0,
        "part_paths": [str(path)],
        "rows_written": int(state.get("rows_written") or 0) + written,
    }


async def _merge_parts(state: DownloadState) -> dict:
    """
    Fold every part into the finished artifact.

    Format-specific work, done by the format's own writer — a byte concatenation for
    CSV, appended row groups for Parquet, a re-written workbook for XLSX. The count
    that comes back is counted from the *files*, so ``rows_written`` after this node is
    a fact about the artifact rather than a total of what each batch reported.
    """
    export_uuid = state["export_id"]

    async with svc.open_session() as db:
        export = await require_export(db, export_uuid)
        context = await load_context(db, export)
        session_token = export.session_token

    # The artifact goes to the session's own folder, not the export's — that is the
    # path the download URL names. The name is resolved against what is already in
    # there, because two exports of one tool on one day want the same name and the
    # second must not land on top of the first. See part_store.available_artifact_name.
    await part_store.ensure_download_dir(session_token)

    file_name = await part_store.available_artifact_name(
        session_token, context.file_name,
    )
    destination = part_store.artifact_path(session_token, file_name)
    paths = [Path(entry) for entry in state.get("part_paths") or []]

    try:
        rows_written = await context.writer.merge_parts(paths, destination)
        byte_size = await part_store.file_size(destination)
        checksum = await compute_checksum(destination)
    except Exception as exc:  # noqa: BLE001 — every merge failure is one outcome
        logger.exception("Merging export %s failed", export_uuid)
        return {"failure": f"the parts could not be combined: {exc}"}

    return {
        "file_path": str(destination),
        "file_name": file_name,
        "byte_size": byte_size,
        "checksum": checksum,
        "rows_written": rows_written,
    }


async def _publish_artifact(state: DownloadState) -> dict:
    """
    Mark the export ready and set when it expires.

    The last thing that happens before cleanup, and the point at which a download URL
    starts working: the route serves ``ready`` exports and nothing else, so until this
    node has run there is no way to fetch a half-written file.
    """
    export_uuid = state["export_id"]

    async with svc.open_session() as db:
        export = await require_export(db, export_uuid)

        await mark_ready(
            db,
            export,
            file_path=str(state.get("file_path") or ""),
            file_name=str(state.get("file_name") or ""),
            byte_size=int(state.get("byte_size") or 0),
            checksum=str(state.get("checksum") or ""),
            part_count=len(state.get("part_paths") or []),
            rows_written=int(state.get("rows_written") or 0),
        )

        await mark_parts_merged(db, export.id)

    logger.info(
        "Export %s is ready: %s record(s) in %s part(s), %s byte(s)",
        export_uuid,
        state.get("rows_written"),
        len(state.get("part_paths") or []),
        state.get("byte_size"),
    )

    return {}


async def _notify_failure(state: DownloadState) -> dict:
    """
    Record the failure so the agent can relay it, and stop.

    The user hears :data:`FAILURE_MESSAGE` — one fixed sentence — unless an earlier node
    already stored something more useful, which the too-large refusal does. The real
    reason is logged and kept in the graph state for the operator; it is not shown,
    because a driver's words are not a visitor's business and "try again" is the only
    actionable part of any of them.
    """
    export_uuid = state["export_id"]
    reason = str(state.get("failure") or "unknown failure")

    async with svc.open_session() as db:
        export = await require_export(db, export_uuid)

        if export.error_message:
            # A node that already phrased this — the ceiling refusal names the limit,
            # which is more use than the generic sentence. Not overwritten.
            logger.info(
                "Export %s already carries a failure message; leaving it", export_uuid,
            )
        else:
            await mark_failed(db, export, reason=reason, user_message=FAILURE_MESSAGE)

    return {}


async def _cleanup(state: DownloadState) -> dict:
    """
    Delete every part file, close the cursor, and forget the caches.

    Reached by every terminal path, which is the point of it being a node. What it
    removes differs by outcome:

    * a successful export keeps its artifact and loses its parts, which have been
      folded into it;
    * a failed one loses the whole directory — the parts *and* any partial artifact.
      An export that half exists is the one outcome worse than no export, because
      nothing about the file says which half is missing.
    """
    export_uuid = state["export_id"]
    failed = bool(state.get("failure"))

    await release_reader(export_uuid)

    if failed:
        await part_store.delete_export_dir(export_uuid)
        logger.info("Removed every file for failed export %s", export_uuid)
    else:
        await part_store.delete_parts_dir(export_uuid)

    forget_export_caches(part_store.parts_dir(export_uuid))
    forget_context(export_uuid)

    return {}


# --------------------------------------------------------------------------
# Routers
# --------------------------------------------------------------------------

def _after_count(state: DownloadState) -> str:
    """Ask the user, or refuse a set too large to export."""
    return "refuse" if state.get("failure") else "ask"


def _after_confirmation(state: DownloadState) -> str:
    """Build it, or end the run having built nothing."""
    return "build" if state.get("confirmed") else "done"


def _after_batch(state: DownloadState) -> str:
    """
    The read loop's only decision.

    Order matters: a failure is checked before completion, because a batch that failed
    on its last attempt has neither read anything nor finished, and treating it as
    finished would merge the parts written so far into a file missing its tail.
    """
    if state.get("failure"):
        return "failed"

    if state.get("finished_reading"):
        return "finished"

    return "continue"


def _after_merge(state: DownloadState) -> str:
    """Publish it, or fall through to the failure path."""
    return "failed" if state.get("failure") else "publish"


# --------------------------------------------------------------------------
# Graph
# --------------------------------------------------------------------------

async def build_download_graph():
    """
    Compile the export graph, once per process.

    Compiled with the checkpointer from
    :mod:`app.services.downloader_agents.base.checkpointer` — without one,
    ``interrupt()`` has nowhere to write the paused run and the confirmation could not
    outlive the request that made the offer.
    """
    global _graph

    if _graph is not None:
        return _graph

    graph = StateGraph(DownloadState)

    graph.add_node(COUNT_RECORDS, _count_records)
    graph.add_node(AWAIT_CONFIRMATION, _await_confirmation)
    graph.add_node(WRITE_BATCH, _write_batch)
    graph.add_node(MERGE_PARTS, _merge_parts)
    graph.add_node(PUBLISH_ARTIFACT, _publish_artifact)
    graph.add_node(NOTIFY_FAILURE, _notify_failure)
    graph.add_node(CLEANUP, _cleanup)

    graph.add_edge(START, COUNT_RECORDS)

    graph.add_conditional_edges(
        COUNT_RECORDS,
        _after_count,
        {"ask": AWAIT_CONFIRMATION, "refuse": NOTIFY_FAILURE},
    )
    graph.add_conditional_edges(
        AWAIT_CONFIRMATION,
        _after_confirmation,
        {"build": WRITE_BATCH, "done": CLEANUP},
    )
    graph.add_conditional_edges(
        WRITE_BATCH,
        _after_batch,
        # The self-edge is the read loop. See _RECURSION_LIMIT for why the limit has
        # to be raised for it.
        {"continue": WRITE_BATCH, "finished": MERGE_PARTS, "failed": NOTIFY_FAILURE},
    )
    graph.add_conditional_edges(
        MERGE_PARTS,
        _after_merge,
        {"publish": PUBLISH_ARTIFACT, "failed": NOTIFY_FAILURE},
    )

    graph.add_edge(PUBLISH_ARTIFACT, CLEANUP)
    graph.add_edge(NOTIFY_FAILURE, CLEANUP)
    graph.add_edge(CLEANUP, END)

    _graph = graph.compile(checkpointer=await get_checkpointer())

    return _graph


async def start_export_offer(export_uuid: Any, file_format: str) -> Optional[dict]:
    """
    Run the graph up to the confirmation, and return what the user should be asked.

    Called from the *request* that answered the question. Returns the interrupt payload
    — ``{"question": ..., "total_rows": ..., "export_id": ...}`` — or ``None`` when the
    run finished without pausing, which happens when the count came back past the
    ceiling. In that case the export row carries the refusal and the caller reads it
    from there.
    """
    graph = await build_download_graph()

    state = await graph.ainvoke(
        initial_state(export_uuid, file_format),
        config=_config_for(export_uuid),
    )

    return _interrupt_payload(state)


async def resume_export(export_uuid: Any, confirmed: bool, file_format: str) -> dict:
    """
    Resume a paused export from the worker, and run it to the end.

    ``Command(resume=...)`` hands the answer back to the waiting ``interrupt()`` — the
    run continues from inside ``_await_confirmation`` rather than starting again, which
    is what makes the count established during the request still valid here.

    Returns the final state, whose ``failure`` key is how the worker knows whether to
    mark the job succeeded or failed.
    """
    graph = await build_download_graph()

    return await graph.ainvoke(
        Command(resume={"confirmed": bool(confirmed), "file_format": file_format}),
        config=_config_for(export_uuid),
    )


def _config_for(export_uuid: Any) -> dict:
    """
    The run configuration for one export.

    The thread id is derived from the export's uuid, so the request that pauses the run
    and the worker that resumes it arrive at the same checkpoint without having to pass
    anything between them but the export id.
    """
    return {
        "configurable": {"thread_id": thread_id_for(export_uuid)},
        "recursion_limit": _RECURSION_LIMIT,
    }


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------

async def _attempt_batch(
    context: ExportContext,
    reader: Any,
    batch_number: int,
    attempt: int,
    path: Path,
) -> Optional[int]:
    """
    One attempt at one batch: read it, write it, record it.

    Returns the number of records written, or ``None`` for the end of the result set.
    ``None`` rather than ``0`` because they are different facts — a batch of zero
    records cannot happen, and conflating "no more data" with "wrote nothing" would end
    the loop on a batch that merely failed quietly.
    """
    rows = await reader.read(batch_number)

    if not rows:
        return None

    written = await context.writer.write_part(rows, path)
    byte_size = await part_store.file_size(path)

    async with svc.open_session() as db:
        await record_part(
            db,
            export_id=context.export_id,
            part_number=batch_number,
            attempt=attempt,
            row_count=written,
            path=str(path),
            byte_size=byte_size,
        )

    return written


def _discard_after(context: ExportContext, path: Path):
    """
    The callback that deletes a failed attempt's part file and records the attempt.

    Built as a closure so ``retry`` needs to know nothing about part files, contexts or
    the database — its only job is counting attempts and waiting between them.
    """

    async def discard(batch_number: int, attempt: int, exc: BaseException) -> None:
        await part_store.delete_part(path)

        async with svc.open_session() as db:
            await record_discarded_part(
                db,
                export_id=context.export_id,
                part_number=batch_number,
                attempt=attempt,
                path=str(path),
                reason=str(exc),
            )

    return discard


def _read_answer(answer: Any, default_format: str) -> tuple:
    """
    ``(confirmed, file_format)`` out of whatever the resume passed.

    Tolerant on purpose. The resume value comes from
    :func:`resume_export`, which sends a dict — but a bare ``True``/``False`` is what
    anyone driving this graph by hand would send, and a paused run is not the place to
    be pedantic about the shape of a yes.
    """
    if isinstance(answer, dict):
        return bool(answer.get("confirmed")), str(
            answer.get("file_format") or default_format
        )

    return bool(answer), default_format


def _interrupt_payload(state: Any) -> Optional[dict]:
    """
    The payload of the interrupt a paused run stopped on, if it paused.

    LangGraph reports pauses under ``__interrupt__`` in the returned state. Read
    defensively — as a list of objects with ``.value``, and as plain dicts — because
    that key's exact shape is langgraph's to change and an export offer is not worth
    breaking over it.
    """
    if not isinstance(state, dict):
        return None

    interrupts = state.get("__interrupt__") or []

    for entry in interrupts:
        value = getattr(entry, "value", None)

        if value is None and isinstance(entry, dict):
            value = entry.get("value")

        if isinstance(value, dict):
            return value

    return None
