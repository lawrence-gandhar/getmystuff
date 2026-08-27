"""
The Create File and Download File nodes' behaviour inside a Graph Designer run.

**The implementation lives here rather than in ``app/services/graph_designer/``.** A new
module does not put its files inside another feature's folder, even when only that feature
calls them — so the graph_designer package contributes two registry entries and two
validator calls, and everything about what these nodes *do* is in this module. That keeps
the answer to "how does this application make a file" in one folder.

**No button, and the node says so at save time rather than ignoring the field.** A graph
has no visitor and no chat, so there is nothing for a coloured button to appear in. What a
pipeline gets instead is the thing a pipeline can actually use: an authorised, owner-only
URL on the node's output, visible in the run view and bindable by a later Email node — so
a graph can mail somebody the file it just built. ``graph_service`` refuses the button
fields on this canvas, because a field that is offered and then quietly ignored is worse
than one that is not offered.

**The rows are already whole here, which is why this module is the shorter of the two.** A
SQL node's output is every matching row — ``node_runners._run_sql`` passes ``max_rows=None``
and nothing on that path caps — so there is no preview-versus-total distinction to get
wrong. The flow runner's equivalent has to re-read a graph run's result precisely because
what it can reach through a chat session *is* a preview.

**Its own database session.** These nodes run on the graph's background task, which has no
request session, and the row has to commit on its own so the file outlives whatever the
rest of the graph does afterwards — including failing. The same reasoning the Email node's
graph runner states.
"""

import logging
from typing import Any, Dict, Mapping

from app.models.file_delivery import FILE_FORMAT_VALUES, ORIGIN_GRAPH, SOURCE_NODE
from app.services.file_delivery import file_service, row_source
from app.services.file_delivery.errors import FileFailure, SourceError

logger = logging.getLogger(__name__)

#: What a Create File node in a *graph* may read its rows from. One source: an earlier
#: node's output. A graph has no chat session and no agent, so there are no session or
#: agent variables to offer — and a literal is not a dataset. ``graph_service`` validates
#: against this exact set at save time.
GRAPH_DATA_SOURCES = frozenset({SOURCE_NODE})


async def run_create_file_node(
    node: dict,
    state: Mapping[str, Any],
    user_id: int,
    run_ref: str = "",
    node_label_of=None,  # noqa: ANN001 — callable(node_id) -> str, from the caller
) -> Dict[str, Any]:
    """
    Write the file this node describes. Returns what to put in ``outputs``.

    Raises :class:`FileFailure` for anything the caller should route down ``error``. The
    graph_designer runner wraps that in its own ``NodeFailure`` — this module does not
    import that class, because that would make the file module depend on the package
    calling it.

    ``node_label_of`` is passed in rather than derived here: the graph's labels live in the
    graph JSON, which the caller has and this module does not, and a refusal that names
    "the output of 'Read orders'" is worth more than one naming ``n4``.
    """
    data = node.get("data") or {}
    node_id = str(node.get("id") or "")
    label = _label(node)

    file_format = str(data.get("file_format") or "").strip().lower()

    if file_format not in FILE_FORMAT_VALUES:
        raise SourceError(f"'{label}' has no file format chosen.", block=label)

    payload = row_source.resolve_graph_data(
        outputs=(state or {}).get("outputs") or {},
        data=data.get("data") or {},
        node_label_of=node_label_of or (lambda node_id: node_id),
        block_label=label,
    )

    async with file_service.open_session() as db:
        record = await file_service.create_file(
            db,
            user_id=user_id,
            payload=payload,
            file_format=file_format,
            name_stem=str(data.get("file_name") or ""),
            origin=ORIGIN_GRAPH,
            # No key and no token: a graph has no visitor, so this file is reachable only
            # on the authenticated owner route. `visitor_file` additionally refuses any
            # file whose origin is not a flow, so a guessed uuid cannot reach it even with
            # a valid widget key.
            source_ref=f"graph run {run_ref}" if run_ref else "",
            node_id=node_id,
        )

        written = {
            # `uuid`, never the bigint id — this goes into graph state, which is previewed
            # into the run dock and is therefore something a browser sees.
            "file_uuid": str(record.uuid),
            "file_name": record.file_name,
            "file_path": record.file_path,
            "file_format": record.file_format,
            "row_count": record.row_count,
            "byte_size": record.byte_size,
        }

        await db.commit()

    logger.info(
        "Graph Create File node %s wrote %s (%d row(s)) from %s",
        node_id,
        written["file_name"],
        written["row_count"],
        payload.description,
    )

    return written


async def run_download_file_node(
    node: dict,
    state: Mapping[str, Any],
    user_id: int,
    file_uuid: str = "",
) -> Dict[str, Any]:
    """
    Turn a file this run made into a link the owner can fetch. Returns the node's output.

    ``file_uuid`` comes from the named Create File node's output, read by the caller —
    which is the half that knows the graph's shape.

    The file is resolved rather than trusted, exactly as the flow runner does it: a run can
    sit at an *Ask a human* node for longer than a file's window, and a link to a lapsed
    file is worse than a failed node. ``owner_file`` also re-checks ownership, so a node
    edited to name another person's file uuid fails here rather than exposing it.
    """
    label = _label(node)

    if not file_uuid:
        raise SourceError(
            f"The Create File node '{label}' names has not written a file in this run, "
            "so there is nothing to hand over. It has to run before this node.",
            block=label,
        )

    async with file_service.open_session() as db:
        record = await file_service.owner_file(db, user_id, file_uuid)

    if file_service.is_expired(record):
        raise SourceError(
            f"The file '{label}' offers has expired. Files last "
            f"{file_service.ttl_phrase()}.",
            block=label,
        )

    return {
        "url": file_service.owner_download_url(record.uuid),
        "file_uuid": str(record.uuid),
        "file_name": record.file_name,
        "file_format": record.file_format,
        "byte_size": record.byte_size,
        "row_count": record.row_count,
        "expires_at": record.expires_at.isoformat() if record.expires_at else None,
    }


def _label(node: Mapping[str, Any]) -> str:
    """What to call this node in a message to the operator."""
    data = node.get("data") or {}

    return (
        str(data.get("label") or "").strip()
        or str(node.get("label") or "").strip()
        or str(node.get("type") or "").replace("_", " ").strip()
        or str(node.get("id") or "this node")
    )


def wrap_failure(exc: BaseException) -> str:
    """
    The sentence a caller should put on its own node-failure exception.

    Here rather than in each canvas's runner so both read the same way in a log, and so a
    :class:`FileFailure`'s carefully written message is not replaced by ``str(exc)`` on
    some path that forgot.
    """
    if isinstance(exc, FileFailure):
        return exc.message

    return (
        "Something went wrong writing this file. Nothing was produced. Please contact "
        "support if this keeps happening."
    )
