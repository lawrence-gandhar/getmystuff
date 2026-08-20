"""
Business logic for Downloader Agents — the offer, the record, and the sentences.

Everything in this feature that touches the application's own database goes through
here. The graph nodes next door decide *what happens*; this module decides what is
written down about it, and what anyone is told.

**Sessions are opened here, not passed in.** Almost every function takes ``db`` as its
first argument, in the house style — but the graph's nodes have no request and therefore
no injected session, so :func:`open_session` exists and the nodes use it. That is the
documented background pattern (``prompt_sync_service.sync_tool_routing_prompt``): the
request that made the offer has long since returned by the time the worker builds the
file.

**The user-facing sentences are constants.** :func:`offer_sentence` and
:data:`FAILURE_MESSAGE` are the two things a visitor actually reads, and they are here
rather than composed by a model on purpose:

* the offer carries the record count, and a model rewording it is how a user gets told
  the wrong number;
* the failure is a fixed sentence because the real reason — a dropped connection, a
  lock timeout, a driver error — is not something to put in front of a visitor, and
  "try again" is the only useful instruction either way. The real reason goes to
  ``error_message`` and the log, for the operator.

**What this module will not do.** It does not build files (``part_writer`` and the format
packages), does not read the user's data (``record_reader``), does not decide control
flow (``download_graph``) and does not authorise anything for HTTP
(``download_routes``). It owns the ``download_exports`` and ``download_export_parts``
rows and the words.
"""

import logging
import os
import uuid as uuid_pkg
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from litestar.exceptions import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.db_sessions import AsyncSessionLocal
from app.db.db_utils import CRUDQueryBuilder
from app.models.data_agents import DataAgent
from app.models.datasource import DataSource
from app.models.downloader_agents import (
    EXPORT_BUILDING,
    EXPORT_DECLINED,
    EXPORT_EXPIRED,
    EXPORT_FAILED,
    EXPORT_FORMAT_LABELS,
    EXPORT_OFFERED,
    EXPORT_QUEUED,
    EXPORT_READY,
    FORMAT_CSV,
    PART_DISCARDED,
    PART_MERGED,
    PART_WRITTEN,
    DownloadExport,
    DownloadExportPart,
)
from app.models.tool_configs import ToolConfig
from app.services.downloader_agents.base import part_store
from app.services.downloader_agents.base.part_writer import extension_for, writer_for
from app.services.downloader_agents.base.record_reader import RecordSource
from app.services.tool_configs.tool_config_service import tables_read

logger = logging.getLogger(__name__)

export_crud = CRUDQueryBuilder(DownloadExport)
part_crud = CRUDQueryBuilder(DownloadExportPart)


# How long a finished artifact stays downloadable. Half an hour: long enough for the
# conversation it belongs to, short enough that a server is not an archive of everything
# anybody ever asked for. The reaper honours it and so does the download route, so a
# lapsed link cannot be served even while the file is still on disk.
#
# It is short deliberately, and that has a consequence worth stating: a visitor who closes
# the tab and comes back an hour later has no file. That is the intended trade — they can
# ask again, and asking again is cheap. Raise DOWNLOAD_EXPORT_TTL_SECONDS if a deployment
# wants otherwise; everything that states the figure to a user derives it from here.
EXPORT_TTL_SECONDS = int(os.getenv("DOWNLOAD_EXPORT_TTL_SECONDS", str(30 * 60)))


def ttl_phrase() -> str:
    """
    How long a file lasts, in words, for a sentence the agent reads out.

    Derived rather than written down. The TTL is configurable and was 24 hours before it
    was 30 minutes; a hard-coded "available for the next 24 hours" in the tool output
    would have gone on saying so for a file the reaper deletes in half that many minutes,
    and the user would believe it.
    """
    minutes = max(1, EXPORT_TTL_SECONDS // 60)

    if minutes < 60:
        return _plural(minutes, "minute")

    hours = minutes // 60

    if hours < 24:
        return _plural(hours, "hour")

    return _plural(hours // 24, "day")


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"

# What a visitor is told when the export could not be built. Fixed, and the same for
# every underlying cause — see the module docstring.
FAILURE_MESSAGE = (
    "The file cannot be created at the moment. Please try again."
)

# What anyone asking for an export they may not have is told. The same sentence for a
# uuid that never existed, one that belongs to another user and one whose file has been
# deleted — because distinguishing them would confirm which uuids are real.
NOT_FOUND_MESSAGE = "That download could not be found."

# What a visitor is told when the link was real and its time is simply up. A different
# sentence from the one above on purpose: "could not be found" reads like the application
# lost the file, and invites the user to go looking for a link that worked yesterday. This
# one says what happened and what to do about it.
EXPIRED_MESSAGE = "That download has expired. Please ask for it again."

# What a visitor is told when their result set is too large to export at all. Names the
# ceiling, because "too large" with no number is not something anyone can act on.
def too_large_message(total_rows: int, ceiling: int) -> str:
    """The refusal for a result set past :data:`record_reader.MAX_EXPORT_ROWS`."""
    return (
        f"There are {total_rows:,} records, which is more than the {ceiling:,} this "
        "application can put into one file. Please narrow the question down and ask "
        "again."
    )


def offer_sentence(total_rows: int, file_format: str = FORMAT_CSV) -> str:
    """
    The offer the agent must repeat word for word.

    The wording is the specified one and is not to be improved: it states the count and
    it asks a plain yes/no question, which is what makes a bare "yes" an answer the
    next turn can act on. The format name is substituted so an offer for a Parquet
    export does not promise a CSV, and defaults to CSV because that is what an
    unprompted offer is for.
    """
    label = EXPORT_FORMAT_LABELS.get(file_format, EXPORT_FORMAT_LABELS[FORMAT_CSV])

    return (
        f"There are {total_rows} records. Do you want me to create a downloadable "
        f"{label} file containing the list of all the records."
    )


def console_download_url(export_uuid: Any) -> str:
    """
    Where an operator fetches a finished export.

    Built here rather than in the route so the sentence the agent says and the handler
    that serves it cannot drift apart — a link the agent invents is a link that 404s.
    """
    return f"/downloads/{export_uuid}"


def visitor_download_url(export: DownloadExport, key_uuid: Any) -> str:
    """
    Where a widget visitor fetches a finished export.

    Named for the file rather than the export::

        SITE_URL/file_downloaders/<session-id>/<file-name>?key=<widget-key-uuid>

    The session is in the path because that is how the artifact is stored — one folder
    per conversation — so the URL and the directory are the same two facts and cannot
    drift. The key travels in the query string because there is no session and no cookie
    on this route: a widget runs on somebody else's site. Neither is a secret beyond the
    conversation it belongs to, and together they are what the download route checks,
    along with the file still being inside its window.

    Returns "" for an export with no file yet. A link to a file that does not exist is
    a link that 404s, and the callers all treat "no URL" as "not ready".
    """
    if not export.file_name:
        return ""

    session = part_store.session_folder(export.session_token)

    return (
        f"/file_downloaders/{session}/{quote(export.file_name)}"
        f"?key={key_uuid}"
    )


def console_progress_url(export_uuid: Any) -> str:
    """Where an operator watches one export being built."""
    return f"/downloads/{export_uuid}/events"


def console_status_url(export_uuid: Any) -> str:
    """Where an operator asks once where one export has got to."""
    return f"/downloads/{export_uuid}/status"


def visitor_progress_url(
    export_uuid: Any,
    key_uuid: Any,
    session_token: str,
) -> str:
    """
    Where a widget visitor watches their own export being built.

    Separate from :func:`visitor_download_url` rather than that URL with ``/events``
    appended: the scope travels in the query string, so the suffix belongs on the path
    before it, and appending would produce ``…?key=…/events``.
    """
    return _visitor_url(export_uuid, key_uuid, session_token, suffix="/events")


def visitor_status_url(
    export_uuid: Any,
    key_uuid: Any,
    session_token: str,
) -> str:
    """
    Where a widget visitor asks once where their own export has got to.

    The progress stream is the live view; this is what a client falls back to when that
    stream drops — a build can outlast one SSE connection, and a card that stopped
    updating because a proxy timed out must not read as a build that stopped.
    """
    return _visitor_url(export_uuid, key_uuid, session_token, suffix="/status")


def site_url() -> str:
    """
    This application's own public base URL, without a trailing slash.

    Read at call time rather than at import so a deployment that sets it after this
    module is first imported — a test, a reload — still gets it.

    **Not used for the URLs handed to the widget.** See :func:`_visitor_url` for why
    that ends badly; this is here for anything server-side that needs to name this
    application in full, where no request is available to derive the host from.
    """
    return os.getenv("SITE_URL", "").strip().rstrip("/")


def _visitor_url(
    export_uuid: Any,
    key_uuid: Any,
    session_token: str,
    suffix: str = "",
) -> str:
    """
    One shape for the visitor's progress and status URLs, so they cannot disagree.

    Still keyed by export uuid rather than by file, because these two are asked
    *while the export is being built* — there is no file name yet, and the whole point
    of the call is to find out when there will be.

    **Every URL this module hands the widget is a path, and must stay one.** The widget
    script is *downloaded* and hosted on the operator's own website
    (``chatbot_settings_routes.download_widget``), so at any moment the copy running in
    a visitor's browser may be months older than this server. Every version of it does
    ``API_BASE + url``, which is correct for a path and produces
    ``https://api.example.com/https://api.example.com/…`` for an absolute URL — a string
    the browser never sends, so nothing reaches the access log and nothing is thrown.

    That is not hypothetical. Prefixing ``SITE_URL`` here to fix a download link that
    resolved against the embedding page left every progress card stuck on "Gathering the
    records…" forever: the export finished, the file was written, and the browser never
    asked. Naming the host is the *embed snippet's* job, via ``apiBase`` — the one piece
    of configuration that lives next to the script actually running.
    """
    return (
        f"/public/downloads/{export_uuid}{suffix}"
        f"?key={key_uuid}&session_token={session_token}"
    )


def open_session() -> AsyncSession:
    """
    A session of this module's own, for code that has no request.

    Used by the graph nodes and the queue worker. An ``async with`` context manager, so
    the caller cannot forget to close it — which matters more here than in a route,
    where Litestar closes it either way.
    """
    return AsyncSessionLocal()


# --------------------------------------------------------------------------
# The offer
# --------------------------------------------------------------------------

async def create_offer(
    db: AsyncSession,
    data_agent_id: int,
    tool_config_id: int,
    total_rows: int,
    count_is_lower_bound: bool = False,
    file_format: str = FORMAT_CSV,
    chatbot_key_id: Optional[int] = None,
    session_token: Optional[str] = None,
) -> DownloadExport:
    """
    Record that an export has been offered, and return the row.

    Takes internal bigint ids because both callers already resolved them — the tool has
    the ``ToolConfig`` row it just ran, and the agent id came off the same lookup. The
    row it returns is what carries the public uuid onward.

    ``thread_id`` is derived from the export's own uuid rather than being a second
    random value. One export, one graph run, one checkpoint thread: making them the
    same string means a stale thread cannot be resumed against a fresh export, and
    there is nothing to keep in step.
    """
    export = await export_crud.create(
        db,
        {
            "data_agent_id": data_agent_id,
            "tool_config_id": tool_config_id,
            "chatbot_key_id": chatbot_key_id,
            "session_token": session_token,
            "thread_id": "",  # replaced below, once the uuid exists
            "file_format": file_format,
            "total_rows": total_rows,
            "count_is_lower_bound": bool(count_is_lower_bound),
            "status": EXPORT_OFFERED,
        },
    )

    export.thread_id = thread_id_for(export.uuid)
    await db.commit()
    await db.refresh(export)

    logger.info(
        "Offered an export of %s record(s) for tool_config id=%s (export %s)",
        total_rows,
        tool_config_id,
        export.uuid,
    )

    return export


def thread_id_for(export_uuid: Any) -> str:
    """
    The checkpointer thread for one export.

    Prefixed rather than the bare uuid so a thread id is self-describing in
    langgraph's own tables, which this feature is not the only possible user of.
    """
    return f"download-export:{export_uuid}"


async def latest_open_offer(
    db: AsyncSession,
    data_agent_id: int,
    session_token: Optional[str] = None,
) -> Optional[DownloadExport]:
    """
    The newest offer this conversation could still act on, if there is one.

    This is what makes a bare "yes" work. ``session_token`` narrows it to one visitor's
    conversation when there is one; on the operator console there is no token and the
    agent is the whole scope, which is correct there — the console has a single user.

    Only ``offered`` rows. An export already queued or built is not something to
    confirm again, and returning one would make a second "yes" silently produce a
    second identical file.
    """
    statement = (
        select(DownloadExport)
        .where(
            DownloadExport.data_agent_id == data_agent_id,
            DownloadExport.status == EXPORT_OFFERED,
        )
        .order_by(DownloadExport.created_at.desc())
        .limit(1)
    )

    if session_token:
        statement = statement.where(DownloadExport.session_token == session_token)

    return (await db.execute(statement)).scalars().first()


async def latest_export(
    db: AsyncSession,
    data_agent_id: int,
    session_token: Optional[str] = None,
) -> Optional[DownloadExport]:
    """
    The newest export of any status for this conversation.

    Used by ``download_status``, which is asked "is my file ready?" and must be able to
    answer for one that is building, ready or failed — not only for one still on offer.
    """
    statement = (
        select(DownloadExport)
        .where(DownloadExport.data_agent_id == data_agent_id)
        .order_by(DownloadExport.created_at.desc())
        .limit(1)
    )

    if session_token:
        statement = statement.where(DownloadExport.session_token == session_token)

    return (await db.execute(statement)).scalars().first()


def is_expired(export: DownloadExport, now: Optional[datetime] = None) -> bool:
    """
    Whether this export's time is up.

    Here rather than in the download route for two reasons. It is a business rule, and
    routes in this application hold none — but also because the comparison is not as
    simple as it looks: ``timestamptz`` comes back from PostgreSQL timezone-aware and from
    SQLite timezone-naive, and comparing one to the other raises ``TypeError`` rather than
    returning an answer. A naive value is read as UTC, which is what it is: every
    ``expires_at`` in this module is written with :func:`datetime.now(timezone.utc)`.

    An export with no expiry has not got one yet, and is not expired.
    """
    if export.expires_at is None:
        return False

    moment = now or datetime.now(timezone.utc)
    expires_at = export.expires_at

    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    return expires_at < moment


def has_lapsed(export: DownloadExport, now: Optional[datetime] = None) -> bool:
    """
    Whether this export's window has closed, by either of the two ways it can.

    An export goes out of date in two steps that are minutes apart: its ``expires_at``
    passes, and then the reaper's next sweep marks the row ``expired`` and deletes the
    bytes. Both are "the file's time is up", and a caller that checks only one gets the
    other wrong.

    That is not hypothetical. The download route used to test the status first and the
    clock second, so an export the reaper had already swept fell into the *not found*
    branch and the visitor was told the download "could not be found" — the exact reading
    that keeping the row was meant to prevent. It was nearly invisible while the TTL was a
    day and the correct message covered most of the window; at thirty minutes with a
    three-minute sweep the wrong message is what almost everybody would have seen.
    """
    return export.status == EXPORT_EXPIRED or is_expired(export, now)


async def get_export(db: AsyncSession, export_uuid: Any) -> Optional[DownloadExport]:
    """
    One export by its public uuid, with no ownership check.

    Accepts a string as well as a ``UUID``, because the graph's state carries the id as a
    string — state has to be JSON-serialisable for the checkpointer, so a ``UUID`` object
    cannot travel in it. Coerced here rather than at each of the ten call sites, and
    coerced rather than relied upon: asyncpg happens to accept a string for a UUID column,
    SQLAlchemy's own ``Uuid`` type does not, and leaning on one driver's leniency is how a
    module works against Postgres and fails against anything else.
    """
    parsed = _as_uuid(export_uuid)

    if parsed is None:
        return None

    return await export_crud.get_by_uuid(db, parsed)


def _as_uuid(value: Any) -> Optional[uuid_pkg.UUID]:
    """
    ``value`` as a ``UUID``, or None if it is not one.

    None rather than a raise: the callers all treat "no such export" as an ordinary
    outcome, and a malformed id is a case of that — not something worth a different code
    path.
    """
    if isinstance(value, uuid_pkg.UUID):
        return value

    try:
        return uuid_pkg.UUID(str(value).strip())
    except (ValueError, AttributeError, TypeError):
        return None


async def require_export(db: AsyncSession, export_uuid: Any) -> DownloadExport:
    """
    One export by uuid, or a 404.

    Separate from :func:`get_export` because the graph nodes want the exception (a node
    that cannot find its own export has nothing left to do) and the routes want to
    decide the status code themselves after an ownership check.
    """
    export = await get_export(db, export_uuid)

    if export is None:
        raise HTTPException(status_code=404, detail=NOT_FOUND_MESSAGE)

    return export


# --------------------------------------------------------------------------
# Status transitions
# --------------------------------------------------------------------------

async def mark_declined(db: AsyncSession, export: DownloadExport) -> DownloadExport:
    """Record that the user said no, so the offer is not re-used by a later 'yes'."""
    return await _set(db, export, {"status": EXPORT_DECLINED})


async def mark_queued(
    db: AsyncSession,
    export: DownloadExport,
    file_format: str,
) -> DownloadExport:
    """
    Record that the user said yes, in the format they asked for.

    The format is written here rather than at offer time because this is the first
    moment it is actually known: the offer says CSV, and the user may answer "yes, as a
    spreadsheet".
    """
    return await _set(db, export, {"status": EXPORT_QUEUED, "file_format": file_format})


async def mark_building(db: AsyncSession, export: DownloadExport) -> DownloadExport:
    """Record that a worker has started on it."""
    return await _set(db, export, {"status": EXPORT_BUILDING})


async def mark_ready(
    db: AsyncSession,
    export: DownloadExport,
    file_path: str,
    file_name: str,
    byte_size: int,
    checksum: str,
    part_count: int,
    rows_written: int,
) -> DownloadExport:
    """
    Record the finished artifact, and when it stops being available.

    ``expires_at`` is set here — at the only moment the file exists — so an export can
    never be ready with no expiry, which would be a file nothing ever deletes.
    """
    return await _set(
        db,
        export,
        {
            "status": EXPORT_READY,
            "file_path": file_path,
            "file_name": file_name,
            "byte_size": byte_size,
            "checksum": checksum,
            "part_count": part_count,
            "rows_written": rows_written,
            "error_message": None,
            "expires_at": datetime.now(timezone.utc)
            + timedelta(seconds=EXPORT_TTL_SECONDS),
        },
    )


async def mark_failed(
    db: AsyncSession,
    export: DownloadExport,
    reason: str,
    user_message: str = FAILURE_MESSAGE,
) -> DownloadExport:
    """
    Record that the export could not be built.

    Two messages, one row: ``reason`` goes to the log for the operator, and
    ``user_message`` — the fixed sentence, unless a caller has something more useful
    like the too-large refusal — is what is stored for the agent to relay.
    """
    logger.warning("Export %s failed: %s", export.uuid, reason)

    return await _set(
        db, export, {"status": EXPORT_FAILED, "error_message": user_message},
    )


async def mark_expired(db: AsyncSession, export: DownloadExport) -> DownloadExport:
    """Record that a finished artifact has been deleted, and why it is gone."""
    return await _set(
        db,
        export,
        {
            "status": EXPORT_EXPIRED,
            "file_path": None,
            "byte_size": None,
            "error_message": "That download has expired. Please ask for it again.",
        },
    )


async def _set(
    db: AsyncSession,
    export: DownloadExport,
    values: Dict[str, Any],
) -> DownloadExport:
    """Apply and commit a set of column values on one export."""
    for key, value in values.items():
        setattr(export, key, value)

    await db.commit()
    await db.refresh(export)

    return export


# --------------------------------------------------------------------------
# Parts
# --------------------------------------------------------------------------

async def record_part(
    db: AsyncSession,
    export_id: int,
    part_number: int,
    attempt: int,
    row_count: int,
    path: str,
    byte_size: int,
) -> DownloadExportPart:
    """Record a part file that was written cleanly."""
    return await part_crud.create(
        db,
        {
            "export_id": export_id,
            "part_number": part_number,
            "attempts": attempt,
            "row_count": row_count,
            "status": PART_WRITTEN,
            "path": path,
            "byte_size": byte_size,
        },
    )


async def record_discarded_part(
    db: AsyncSession,
    export_id: int,
    part_number: int,
    attempt: int,
    path: str,
    reason: str,
) -> DownloadExportPart:
    """
    Record an attempt whose part file was deleted.

    A row for something that no longer exists, deliberately: three rows for one part
    number is what "this batch failed twice before it worked" looks like afterwards,
    and without them a retried export is indistinguishable from a clean one.
    """
    return await part_crud.create(
        db,
        {
            "export_id": export_id,
            "part_number": part_number,
            "attempts": attempt,
            "row_count": 0,
            "status": PART_DISCARDED,
            "path": path,
            "error_message": reason[:2000],
        },
    )


async def mark_parts_merged(db: AsyncSession, export_id: int) -> None:
    """
    Flag every written part as folded into the artifact.

    Which is also the record that their files are expected to be gone: the cleanup node
    deletes them straight after, and a part still marked ``written`` with no file is a
    cleanup that did not run.
    """
    parts = await part_crud.get_many(
        db, filters={"export_id": export_id, "status": PART_WRITTEN},
    )

    for part in parts:
        part.status = PART_MERGED

    await db.commit()


async def written_parts(db: AsyncSession, export_id: int) -> List[DownloadExportPart]:
    """The parts of one export that were written cleanly, in order."""
    return await part_crud.get_many(
        db,
        filters={"export_id": export_id, "status": PART_WRITTEN},
        order_by="part_number",
    )


async def part_progress(db: AsyncSession, export_id: int) -> List[DownloadExportPart]:
    """
    Every part row for one export, oldest first — written and discarded alike.

    This is what the progress stream reads. Discarded rows are included because a retry
    is exactly the thing a person watching a slow export wants to see; hiding them would
    make a stalled export look identical to a fast one that is nearly done.
    """
    return await part_crud.get_many(db, filters={"export_id": export_id}, order_by="id")


# --------------------------------------------------------------------------
# Context
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ExportContext:
    """
    Everything a graph node needs that is not in the state.

    Assembled from the database by :func:`load_context` and cached per export, because
    the alternative is re-reading the tool config and the datasource once per batch —
    ten thousand times for a large export, to learn the same three things.

    Holds the ``RecordSource`` (which query, which datasource), the format writer, and
    the artifact's name. The name is decided once, here, rather than at merge time: it
    is stored on the export row, and a name derived twice is a name that can differ
    between the file on disk and the header the browser is sent.
    """

    export_uuid: str
    export_id: int
    source: RecordSource
    file_format: str
    extension: str
    file_name: str
    writer: Any
    table_name: str


_contexts: Dict[str, ExportContext] = {}


async def load_context(
    db: AsyncSession,
    export: DownloadExport,
    refresh: bool = False,
) -> ExportContext:
    """
    The context for one export, loaded once per process.

    ``refresh`` forces a reload, which the worker uses when it picks up a job: the
    format may have changed between the offer and the confirmation, and a cached
    context would build a CSV for someone who asked for a spreadsheet.

    Raises the project's 404 when the tool or the datasource has been deleted since the
    offer was made. That is a real state — an operator can remove a tool while a visitor
    is deciding — and it has to fail loudly here rather than produce an empty file.
    """
    key = str(export.uuid)

    cached = _contexts.get(key)
    if cached is not None and not refresh and cached.file_format == export.file_format:
        return cached

    tool = await db.get(ToolConfig, export.tool_config_id)

    if tool is None:
        raise HTTPException(
            status_code=404,
            detail="The tool this download came from no longer exists.",
        )

    datasource = await db.get(DataSource, tool.datasource_id)

    if datasource is None:
        raise HTTPException(
            status_code=404,
            detail="The data source this download came from no longer exists.",
        )

    extension = extension_for(export.file_format)

    context = ExportContext(
        export_uuid=key,
        export_id=export.id,
        source=RecordSource(
            datasource=datasource,
            config=dict(tool.config or {}),
            table_name=tool.table_name,
            sql_query=tool.sql_query,
            table_names=tables_read(tool.table_name, tool.extra_tables),
        ),
        file_format=export.file_format,
        extension=extension,
        file_name=export.file_name
        or part_store.artifact_name(tool.table_name, extension),
        writer=writer_for(export.file_format),
        table_name=tool.table_name,
    )

    _contexts[key] = context

    return context


def forget_context(export_uuid: Any) -> None:
    """
    Drop one export's cached context.

    Called by the cleanup node. The cache has no eviction of its own, and it holds a
    detached ORM row per export — small, but it is a row from a session that is closed,
    and keeping it past the run it belongs to invites somebody to use it.
    """
    _contexts.pop(str(export_uuid), None)


# --------------------------------------------------------------------------
# Expiry
# --------------------------------------------------------------------------

async def expire_lapsed_exports(db: AsyncSession) -> int:
    """
    Delete every artifact whose time is up, and mark its row. Returns how many.

    An export is a snapshot of somebody's data sitting on a disk, and the honest default
    for that is that it does not sit there forever. :data:`EXPORT_TTL_SECONDS` decides how
    long; this is what acts on it.

    The row is kept and marked ``expired`` rather than deleted. A visitor coming back to a
    dead link should be told the file has expired and that they can ask again — which
    needs a row to say so. A missing row would produce "that download could not be
    found", which reads like the application lost it.
    """
    statement = select(DownloadExport).where(
        DownloadExport.status == EXPORT_READY,
        DownloadExport.expires_at.is_not(None),
        DownloadExport.expires_at < datetime.now(timezone.utc),
    )

    lapsed = list((await db.execute(statement)).scalars().all())

    for export in lapsed:
        # Both roots. The artifact is what the visitor could still fetch, so it is the
        # one that has to go; the export directory is the parts' home and is normally
        # already empty by now, but an export that failed after writing parts and
        # before merging leaves it behind and nothing else comes back for it.
        await part_store.delete_artifact(export.session_token, export.file_name)
        await part_store.delete_export_dir(export.uuid)
        await mark_expired(db, export)

    if lapsed:
        logger.info("Expired %d finished export(s)", len(lapsed))

    return len(lapsed)


#: How often the reaper sweeps. A tenth of the TTL, floored at a minute and capped at a
#: quarter of an hour, so the sweep tracks the TTL instead of having to be remembered
#: alongside it — a fixed quarter-hour interval against a thirty-minute TTL would leave a
#: file on disk for up to forty-five minutes.
REAPER_INTERVAL_SECONDS = max(60.0, min(900.0, EXPORT_TTL_SECONDS / 10))


async def run_expiry_reaper(
    interval_seconds: float = REAPER_INTERVAL_SECONDS,
) -> None:
    """
    Expire lapsed exports on a timer, forever.

    Started from ``on_startup`` beside the queue worker. Every failure is logged and the
    loop carries on: a reaper that exits leaves an application quietly accumulating files
    nobody will ever download, and nothing about that state announces itself.

    An artifact can outlive its expiry by up to one interval, which harms nothing: the
    download route checks :func:`is_expired` on every request, so a lapsed export is
    refused whether or not the reaper has been round yet. The sweep deletes bytes; the
    route is what enforces the rule.
    """
    import asyncio

    while True:
        await asyncio.sleep(interval_seconds)

        try:
            async with open_session() as db:
                await expire_lapsed_exports(db)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — the reaper must outlive one bad pass
            logger.exception("The export expiry reaper hit a failure")


# --------------------------------------------------------------------------
# Ownership
# --------------------------------------------------------------------------

async def owned_export(
    db: AsyncSession,
    user_id: int,
    export_uuid: uuid_pkg.UUID,
) -> DownloadExport:
    """
    One export belonging to ``user_id``, or a 404.

    Resolved export -> data agent -> ``user_id``, because an export has no owner column
    of its own: it belongs to whoever owns the agent whose tool produced it.

    The 404 for someone else's export is deliberate and matches the rest of the
    application — a 403 there would confirm that the uuid names a real file.
    """
    statement = (
        select(DownloadExport)
        .join(DataAgent, DataAgent.id == DownloadExport.data_agent_id)
        .where(DownloadExport.uuid == export_uuid, DataAgent.user_id == user_id)
        .limit(1)
    )

    export = (await db.execute(statement)).scalars().first()

    if export is None:
        raise HTTPException(status_code=404, detail=NOT_FOUND_MESSAGE)

    return export


async def visitor_export(
    db: AsyncSession,
    chatbot_key_id: int,
    session_token: str,
    export_uuid: uuid_pkg.UUID,
) -> DownloadExport:
    """
    One export belonging to a widget visitor's own conversation, or a 404.

    Both the key and the token are required, and the token is why: a widget is public,
    so its key identifies the site rather than the person. Without the token, any
    visitor could enumerate every export ever produced for that widget.
    """
    statement = (
        select(DownloadExport)
        .where(
            DownloadExport.uuid == export_uuid,
            DownloadExport.chatbot_key_id == chatbot_key_id,
            DownloadExport.session_token == session_token,
        )
        .limit(1)
    )

    export = (await db.execute(statement)).scalars().first()

    if export is None:
        raise HTTPException(status_code=404, detail=NOT_FOUND_MESSAGE)

    return export


async def session_download(
    db: AsyncSession,
    chatbot_key_id: int,
    session_folder: str,
    file_name: str,
) -> DownloadExport:
    """
    The export behind ``/file_downloaders/<session>/<file>``, or a 404.

    Matched on the widget key, the session and the file name together. The session is
    matched on its *folder* form rather than on the raw token, because the folder is
    what the URL carries and what the file is stored under — comparing the URL's
    segment against the raw column would fail for any token the folder rules changed,
    and comparing the other way round would let two tokens that normalise to one folder
    read each other's files. Normalising both sides is the only version of this that is
    true in both directions.

    ``expired`` rows are matched as well as ``ready`` ones, and deliberately: the
    caller turns one into "that download has expired. Please ask for it again", which
    needs the row to exist. Refusing them here would produce "could not be found",
    which reads like the application lost the file.
    """
    statement = (
        select(DownloadExport)
        .where(
            DownloadExport.chatbot_key_id == chatbot_key_id,
            DownloadExport.file_name == file_name,
            DownloadExport.status.in_([EXPORT_READY, EXPORT_EXPIRED]),
        )
        .order_by(DownloadExport.created_at.desc())
    )

    for export in (await db.execute(statement)).scalars().all():
        if part_store.session_folder(export.session_token) == session_folder:
            return export

    raise HTTPException(status_code=404, detail=NOT_FOUND_MESSAGE)
