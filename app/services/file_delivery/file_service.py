"""
Writing a file, authorising it, and taking it away again when its time is up.

One module, three jobs, and they belong together because each is the reason the next is
safe: the path is built here from the row's own uuid, so nothing a visitor or a model
supplied ever reaches the filesystem; the lookups are the only way a route resolves a
request, so every fetch is authorised against the audience that asked; and the reaper is
what makes a link that stops working stop working on disk too.

**The row is written after the bytes.** ``create_file`` writes the file first and inserts
the row second, so a ``generated_files`` row existing means a file exists. The other order
would leave a row promising a file that a crash between the two statements never produced,
and something would eventually serve a 200 for a missing file. It also means there is no
"building" state to hold: the export queue needs five statuses because a worker builds its
artifact over minutes, and here the caller is already holding the rows.

**Two audiences, two URLs, and both relative.**

    operator   /generated_files/<file-uuid>
    visitor    /public/generated_files/<file-uuid>?key=<widget-key-uuid>&session_token=…

Relative on purpose, and load-bearing: the widget script is fetched by somebody else's
website and prefixes ``API_BASE`` itself, so an absolute URL built from this server's idea
of its own hostname is how a deployed copy quietly serves links to the wrong host. The
same rule ``visitor_download_url`` keeps.

The visitor's key and token travel in the query string because there is no cookie and no
session on that route. Neither is a secret beyond the conversation it belongs to, and
together with the expiry window they are exactly what the route checks.
"""

import asyncio
import logging
import os
import shutil
import uuid as uuid_pkg
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote
from typing import Any, Optional

from litestar.exceptions import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.db_sessions import AsyncSessionLocal
from app.db.db_utils import CRUDQueryBuilder
from app.models.file_delivery import (
    FILE_EXPIRED,
    FILE_FORMAT_EXTENSIONS,
    FILE_FORMAT_MEDIA_TYPES,
    FILE_READY,
    FORMAT_CSV,
    ORIGIN_FLOW,
    GeneratedFile,
)
from app.services.file_delivery.errors import WriteError
from app.services.file_delivery.row_source import Payload
from app.utils.file_utils import GENERATED_FILE_BASE, normalize_filename

logger = logging.getLogger(__name__)

file_crud = CRUDQueryBuilder(GeneratedFile)


# How long a file a block made stays downloadable. Twenty-four hours, against the export
# queue's thirty minutes, and the difference is deliberate: an export is a sample somebody
# asked for mid-conversation and can trivially ask for again, while this is a deliverable
# an operator built into a flow — a visitor who is handed a download button, closes the
# tab and comes back after lunch should still get their file.
#
# Everything that states the figure to anybody derives it from here, so a deployment that
# changes it does not leave a sentence somewhere promising the old one.
FILE_TTL_SECONDS = int(os.getenv("NODE_FILE_TTL_SECONDS", str(24 * 3600)))

# How often the reaper sweeps: a tenth of the TTL, floored at a minute and capped at a
# quarter of an hour. Tracks the TTL rather than having to be remembered alongside it —
# the same derivation ``download_service.REAPER_INTERVAL_SECONDS`` makes.
REAPER_INTERVAL_SECONDS = max(60.0, min(900.0, FILE_TTL_SECONDS / 10))

# What anyone asking for a file they may not have is told. One sentence for a uuid that
# never existed, one that belongs to somebody else, and one whose file is gone — because
# telling them apart would confirm which uuids are real.
NOT_FOUND_MESSAGE = "That file could not be found."

# Said when the link was real and its time is simply up. A different sentence from the one
# above on purpose: "could not be found" reads as though the application lost the file and
# sends somebody looking for a link that worked yesterday.
EXPIRED_MESSAGE = "That file has expired and is no longer available."

# The name a file gets when the block's name field was left empty or normalised away to
# nothing. Not an error: a name is a convenience, and refusing to write somebody's data
# because they did not think of one would be the wrong end of the stick.
_DEFAULT_STEM = "file"


def ttl_phrase() -> str:
    """
    How long a file lasts, in words, for a sentence an operator reads.

    Derived rather than written down, for the reason ``download_service.ttl_phrase``
    gives: the TTL is configurable, and a hard-coded "24 hours" in a help page goes on
    saying so after somebody changes it.
    """
    minutes = max(1, FILE_TTL_SECONDS // 60)

    if minutes < 60:
        return _plural(minutes, "minute")

    hours = minutes // 60

    if hours < 24:
        return _plural(hours, "hour")

    return _plural(hours // 24, "day")


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def open_session() -> AsyncSession:
    """
    A session of this module's own, for code that has no request.

    The reaper uses it. An ``async with`` context manager so the caller cannot forget to
    close it — the same helper ``download_service`` keeps, for the same callers.
    """
    return AsyncSessionLocal()


# --------------------------------------------------------------------------
# Paths and names
# --------------------------------------------------------------------------

def file_dir(file_uuid: Any) -> Path:
    """
    Where one file lives: ``uploads/generated_files/<file-uuid>/``.

    A directory per file rather than a flat folder, for the reason ``part_store`` gives
    about its export directories: cleanup is then "remove this directory", a rule that
    cannot take somebody else's file with it. A flat folder would need the naming rule to
    be right in both the writer and the reaper, and one of the two would eventually be
    wrong.
    """
    return GENERATED_FILE_BASE / str(file_uuid)


def artifact_name(stem: str, file_format: str) -> str:
    """
    The finished file's name: an operator's stem, normalised, plus the format's extension.

    The stem is operator-authored and may have come through ``{{VARIABLE}}``
    interpolation, so it goes through ``normalize_filename`` — a stem of
    ``../../etc/passwd`` becomes a harmless flat name. The extension is appended from the
    **format**, never taken from the stem, so a block writing Parquet cannot produce
    ``orders.csv`` and mislead whoever opens it.

    An extension the operator typed themselves is removed first, whichever of the four it
    is. Typing ``orders.csv`` on a CSV block is the ordinary case and must not yield
    ``orders.csv.csv``; typing it on a Parquet block is a leftover from changing the format,
    and ``orders.parquet`` is what they meant — where ``orders.csv.parquet`` would carry a
    format name that is not this file's.
    """
    extension = FILE_FORMAT_EXTENSIONS.get(file_format, FILE_FORMAT_EXTENSIONS[FORMAT_CSV])
    cleaned = normalize_filename(stem or "")

    for known in FILE_FORMAT_EXTENSIONS.values():
        if cleaned.endswith(known):
            cleaned = cleaned[: -len(known)]
            break

    return f"{cleaned or _DEFAULT_STEM}{extension}"


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------

async def create_file(
    db: AsyncSession,
    *,
    user_id: int,
    payload: Payload,
    file_format: str,
    name_stem: str,
    origin: str,
    chatbot_key_id: Optional[int] = None,
    session_token: Optional[str] = None,
    source_ref: str = "",
    node_id: str = "",
) -> GeneratedFile:
    """
    Write one file and record it. Returns the row, whose ``uuid`` is the public handle.

    ``file_writer`` is imported here rather than at module scope, and that is the same
    hazard ``parquet_writer`` documents at length: it imports polars and pyarrow, whose
    compiled extensions must not be first imported on a worker thread that is later
    destroyed. This function is a coroutine on the event loop, so the import happens on
    the right thread — which it would not if this module imported it and something in a
    ``to_thread`` imported this module first.

    On a write failure the directory is removed before the exception leaves, so a failed
    block does not leave a half-written file on disk with no row pointing at it. There is
    nothing to roll back in the database: the row has not been written yet.
    """
    from app.services.file_delivery import file_writer

    file_uuid = uuid_pkg.uuid4()
    name = artifact_name(name_stem, file_format)
    directory = file_dir(file_uuid)
    path = directory / name

    try:
        if payload.rows is not None:
            row_count = await file_writer.write_rows(payload.rows, path, file_format)
        else:
            row_count = await file_writer.write_text(payload.text or "", path, file_format)
    except Exception:
        await _remove_dir(directory)
        raise

    try:
        byte_size = await asyncio.to_thread(lambda: path.stat().st_size)
    except OSError as exc:
        await _remove_dir(directory)
        raise WriteError(
            "The file could not be written. Please try again.",
        ) from exc

    return await file_crud.create(
        db,
        {
            "uuid": file_uuid,
            "user_id": int(user_id),
            "chatbot_key_id": chatbot_key_id,
            "session_token": session_token or None,
            "origin": origin,
            "source_ref": (source_ref or "")[:255] or None,
            "node_id": (node_id or "")[:100] or None,
            "file_format": file_format,
            "file_name": name,
            "file_path": str(path),
            "byte_size": int(byte_size),
            "row_count": int(row_count),
            "status": FILE_READY,
            "expires_at": datetime.now(timezone.utc)
            + timedelta(seconds=FILE_TTL_SECONDS),
        },
    )


def media_type_of(record: GeneratedFile) -> str:
    """The content type to serve one file as."""
    return FILE_FORMAT_MEDIA_TYPES.get(record.file_format, "application/octet-stream")


# --------------------------------------------------------------------------
# URLs
# --------------------------------------------------------------------------

def owner_download_url(file_uuid: Any) -> str:
    """
    Where the owner fetches one of their files.

    Built here rather than in the route so a link a node puts in its output and the
    handler that serves it cannot drift apart — the rule ``console_download_url`` states.
    """
    return f"/generated_files/{file_uuid}"


def visitor_download_url(file_uuid: Any, key_uuid: Any, session_token: str) -> str:
    """
    Where a widget visitor fetches one of their conversation's files.

    Relative, for the reason in the module docstring: the widget prefixes its own
    ``API_BASE``, and an absolute URL built here would point at whatever this process
    believes its hostname to be.
    """
    return (
        f"/public/generated_files/{file_uuid}"
        f"?key={key_uuid}&session_token={quote(session_token or '')}"
    )


# --------------------------------------------------------------------------
# Lookups — the only way a route resolves a request
# --------------------------------------------------------------------------

def _as_uuid(value: Any) -> uuid_pkg.UUID:
    """
    One public identifier as a ``UUID``, whatever the caller had.

    Both kinds of caller reach the lookups below: a route hands over a real ``UUID`` (its
    path parameter is typed ``:uuid``), while a node hands over the **string** it read out
    of a session's ``node_results`` or a graph's outputs, because that record is JSON.
    Normalising here rather than at each call site is what stops the difference mattering
    — asyncpg happens to accept a string for a uuid column and SQLite does not, so a bug
    on this path would have passed every test against one database and failed against the
    other.

    A malformed value is a 404, not a 500: it cannot name a file, and saying anything more
    specific would confirm which shapes of identifier are real.
    """
    if isinstance(value, uuid_pkg.UUID):
        return value

    try:
        return uuid_pkg.UUID(str(value))
    except (AttributeError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=NOT_FOUND_MESSAGE) from exc


async def owner_file(
    db: AsyncSession, user_id: int, file_uuid: Any,
) -> GeneratedFile:
    """
    One file belonging to this signed-in user, or a 404.

    The ownership filter is part of the lookup rather than a check after it, so there is
    no version of this function that can return somebody else's row to a caller who
    forgot to look. ``expired`` rows are returned as well as ``ready`` ones — the caller
    turns one into "that file has expired", which needs the row to exist to say.

    Either origin. A flow's file is as much this operator's data as a pipeline's: they own
    the widget, the flow and the conversation log. What the origin *does* gate is the
    public route below, where the audience is a visitor rather than an owner.
    """
    record = await file_crud.get_by_uuid(
        db, _as_uuid(file_uuid), extra_filters={"user_id": user_id},
    )

    if record is None:
        raise HTTPException(status_code=404, detail=NOT_FOUND_MESSAGE)

    return record


async def visitor_file(
    db: AsyncSession,
    chatbot_key_id: int,
    session_token: str,
    file_uuid: Any,
) -> GeneratedFile:
    """
    One file belonging to this visitor's own conversation, or a 404.

    All four facts are in the query — the file, the widget key, the session token, and
    that it came from a flow — because any one of them left out is a way to read somebody
    else's file. The origin matters as much as the rest: a graph's file has no visitor and
    must never be reachable on a public route, whatever key is presented.
    """
    if not session_token:
        raise HTTPException(status_code=404, detail=NOT_FOUND_MESSAGE)

    record = await file_crud.get_by_uuid(
        db,
        _as_uuid(file_uuid),
        extra_filters={
            "chatbot_key_id": chatbot_key_id,
            "session_token": session_token,
            "origin": ORIGIN_FLOW,
        },
    )

    if record is None:
        raise HTTPException(status_code=404, detail=NOT_FOUND_MESSAGE)

    return record


def is_expired(record: GeneratedFile, moment: Optional[datetime] = None) -> bool:
    """
    Whether this file's window has closed.

    Compared in UTC. ``expires_at`` is written with ``datetime.now(timezone.utc)``, but a
    row read back from a database column that lost its timezone would otherwise raise on
    the comparison rather than answer it — the same defence ``download_service.is_expired``
    carries, and for the same reason: this is the check that stands between a lapsed link
    and somebody's data.
    """
    if record.expires_at is None:
        return False

    expires_at = record.expires_at

    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    return expires_at < (moment or datetime.now(timezone.utc))


def assert_servable(record: GeneratedFile) -> Path:
    """
    The path to stream, or the right refusal. Called by both routes, on every request.

    Three ways it says no, and they are three different sentences because they are three
    different situations for the person holding the link: the row says expired, the clock
    says expired even though the reaper has not been round yet, or the file is missing
    from disk. The middle one is why this check exists at all — the sweep deletes bytes,
    and *this* is what enforces the rule.
    """
    if record.status == FILE_EXPIRED or is_expired(record):
        raise HTTPException(status_code=410, detail=EXPIRED_MESSAGE)

    path = Path(record.file_path)

    if not path.is_file():
        logger.warning(
            "Generated file %s is recorded at %s, which is not there",
            record.uuid,
            record.file_path,
        )
        raise HTTPException(status_code=404, detail=NOT_FOUND_MESSAGE)

    return path


# --------------------------------------------------------------------------
# Expiry
# --------------------------------------------------------------------------

async def expire_lapsed_files(db: AsyncSession) -> int:
    """
    Delete every file whose time is up and mark its row. Returns how many.

    The row is kept and marked ``expired`` rather than deleted, matching
    ``expire_lapsed_exports``: somebody coming back to a dead link should be told the file
    has expired, and a missing row can only produce "could not be found", which reads like
    the application lost it.
    """
    statement = select(GeneratedFile).where(
        GeneratedFile.status == FILE_READY,
        GeneratedFile.expires_at.is_not(None),
        GeneratedFile.expires_at < datetime.now(timezone.utc),
    )

    lapsed = list((await db.execute(statement)).scalars().all())

    for record in lapsed:
        await _remove_dir(file_dir(record.uuid))
        record.status = FILE_EXPIRED

    if lapsed:
        await db.commit()
        logger.info("Expired %d generated file(s)", len(lapsed))

    return len(lapsed)


async def run_expiry_reaper(interval_seconds: float = REAPER_INTERVAL_SECONDS) -> None:
    """
    Expire lapsed files on a timer, forever.

    Started from ``on_startup`` beside the export reaper. Every failure is logged and the
    loop carries on: a reaper that exits leaves an application quietly accumulating files
    nobody will ever download, and nothing about that state announces itself.

    A file can outlive its expiry by up to one interval, which harms nothing — every route
    calls :func:`assert_servable` on every request, so a lapsed link is refused whether or
    not the sweep has been round.
    """
    while True:
        await asyncio.sleep(interval_seconds)

        try:
            async with open_session() as db:
                await expire_lapsed_files(db)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — the reaper must outlive one bad pass
            logger.exception("The generated-file expiry reaper hit a failure")


async def _remove_dir(directory: Path) -> None:
    """
    Remove one file's directory, in a thread, never raising.

    ``ignore_errors`` because there is nothing useful a caller can do about a directory
    that is already gone or that the process cannot remove, and a reaper that raised
    partway through a sweep would leave the rest of the batch unswept.
    """
    await asyncio.to_thread(shutil.rmtree, directory, True)
