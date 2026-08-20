"""
Where an export's files live on disk, and who is allowed to delete them.

Two roots, because the two kinds of file have different lifetimes and different
audiences. Part files are scratch, keyed by export; the finished artifact is the
deliverable, keyed by the chat session that asked for it::

    uploads/exports/<export-uuid>/
        parts/
            part-000001.csv
            part-000002.csv        <- deleted the moment the merge succeeds

    uploads/file_downloaders/<session-id>/
        inventory_items_2026-08-06.csv     <- the artifact, and what a visitor fetches

The second path is the one a visitor sees: the download URL is
``SITE_URL/file_downloaders/<session-id>/<file-name>``, served by the download route
after its session, expiry and ownership checks — never as a static directory.

**Why the artifact is keyed by session.** A session ending, and a session's files
being cleaned up, have to be one operation over one directory. Keyed by export uuid
they would be scattered across as many directories as the visitor asked for exports,
and "remove everything this conversation produced" would be a query rather than an
``rmtree``. The cost is that two exports in one session can want the same file name —
see :func:`available_artifact_name`, which is what stops the second overwriting the
first.

**Why a directory per export for the parts.** Cleanup is the reason. The
failure path has to remove every part file this export created and nothing anyone
else's export created, and "everything under this directory" is a rule that cannot get
that wrong. A flat folder with prefixed names would need the prefix to be correct in
two places — the writer and the reaper — and one of them would eventually be wrong.

**Every path is built here, from the export's uuid.** No caller passes a path in and
no path is ever assembled from a user-supplied or model-supplied string. The only
external input is the tool's table name, which becomes part of the artifact's
*filename* and goes through ``file_utils.normalize_filename`` first, so a table called
``../../etc/passwd`` becomes a harmless flat name. :func:`resolve_within_export` is the
belt to that braces: it re-checks that a path handed back to us later still sits inside
the export's own directory before anything opens it.

All filesystem work happens in a thread (``asyncio.to_thread``), matching
``file_service.upload_datasource_files``. Writing fifty rows is fast, but it is still
blocking I/O, and an export does thousands of them inside an event loop that is also
serving requests.
"""

import asyncio
import logging
import shutil
from datetime import date
from pathlib import Path
from typing import Any, List, Optional

from app.utils.file_utils import DOWNLOAD_BASE, EXPORT_BASE, normalize_filename

logger = logging.getLogger(__name__)


# The subdirectory part files go in. Named rather than inlined because the cleanup
# rule is "remove this directory", and that rule and the writer must agree.
PARTS_DIRNAME = "parts"

# Part file names are zero-padded to six digits: part-000001.csv. Six because
# MAX_EXPORT_ROWS / BATCH_SIZE is 10,000 parts at the default ceiling, and a name that
# sorts lexicographically is a name the merge can rely on without parsing it.
_PART_NUMBER_WIDTH = 6


def export_dir(export_uuid: str) -> Path:
    """The directory that holds everything for one export."""
    return EXPORT_BASE / str(export_uuid)


def parts_dir(export_uuid: str) -> Path:
    """The directory that holds one export's part files."""
    return export_dir(export_uuid) / PARTS_DIRNAME


def part_path(export_uuid: str, part_number: int, extension: str) -> Path:
    """
    The file one batch writes.

    ``extension`` includes the dot, as ``EXPORT_FORMAT_EXTENSIONS`` provides it. Part
    files carry the *target* format's extension rather than a generic ``.part``,
    because each format's merge reads its own parts back — a Parquet merge opens
    Parquet files — and a name that lies about its contents is a trap for whoever
    reads the directory next.
    """
    name = f"part-{int(part_number):0{_PART_NUMBER_WIDTH}d}{extension}"
    return parts_dir(export_uuid) / name


def artifact_name(table_name: str, extension: str, on: Optional[date] = None) -> str:
    """
    What the finished file is called when it reaches the browser.

    The tool's table name plus the date, normalised. The date is in the name because
    an export is a snapshot and a user who downloads the same tool twice in a month
    should not end up with two files called the same thing in their downloads folder.

    ``normalize_filename`` is what makes the table name safe to use here: it is a
    name from the user's own database, so it can contain anything, and this string
    ends up in a ``Content-Disposition`` header.
    """
    stem = normalize_filename(table_name or "export")
    # normalize_filename keeps a dot, so a table called "sales.2024" would otherwise
    # produce "sales.2024_2026-08-06.csv" — harmless, but the stem is a name and not
    # a path, so any extension-looking tail is folded in.
    stem = stem.replace(".", "_").strip("_") or "export"

    stamp = (on or date.today()).isoformat()

    return f"{stem}_{stamp}{extension}"


# --------------------------------------------------------------------------
# The finished artifact — one directory per chat session
# --------------------------------------------------------------------------
#
# Parts live under EXPORT_BASE, keyed by export uuid, because they are scratch that
# only the merge reads. The artifact lives under DOWNLOAD_BASE, keyed by *session*,
# because that is what the visitor downloads and what a session ending has to be able
# to clear out — and because the download URL names it:
#
#     SITE_URL/file_downloaders/<session-id>/<file-name>


def session_folder(session_id: Any) -> str:
    """
    One chat session's directory name, made safe to put in a path.

    The session token is minted by the browser and travels in a query string, so it is
    caller-supplied and must never be joined onto a path as it stands — a token of
    ``../../etc`` would otherwise write outside the download root. ``normalize_filename``
    flattens it, and the separators are stripped afterwards because a normalised name
    may still keep a dot.

    An export made from the agent console has no session at all; ``console`` is not a
    fallback so much as the truthful name for that case, and it is a reserved word
    rather than a collision risk because a normalised token cannot contain a slash and
    the console's own downloads are not served from this path.
    """
    token = normalize_filename(str(session_id or "").strip() or "console")
    token = token.replace(".", "_").strip("._-/") or "console"

    return token


def download_dir(session_id: Any) -> Path:
    """The directory holding every finished artifact for one chat session."""
    return DOWNLOAD_BASE / session_folder(session_id)


def artifact_path(session_id: Any, file_name: str) -> Path:
    """
    Where the merged artifact is written, and where the download route reads it from.

    ``file_name`` comes from :func:`artifact_name` (or :func:`available_artifact_name`),
    so it is already normalised. It is passed rather than recomputed because it is
    stored on the export row: the file on disk, the name in the database and the name
    in the URL must be the same string, and deriving it twice is how they stop being.
    """
    return download_dir(session_id) / Path(file_name).name


async def available_artifact_name(session_id: Any, file_name: str) -> str:
    """
    ``file_name``, or the next free variant of it in this session's directory.

    Grouping by session rather than by export uuid means two exports can want the same
    name: :func:`artifact_name` is the table plus the date, so asking for the same tool
    twice in one afternoon produces it twice. Writing both to one path would leave the
    first download serving the second export's bytes — the same number of records, from
    a different query, with nothing to show anything was wrong.
    """
    stem = Path(file_name).stem
    suffix = Path(file_name).suffix
    directory = download_dir(session_id)

    def _free() -> str:
        candidate = Path(file_name).name

        for attempt in range(1, 1000):
            if not (directory / candidate).exists():
                return candidate
            candidate = f"{stem}-{attempt}{suffix}"

        # A thousand exports of one table into one session is not a real case; refusing
        # is still better than overwriting somebody's file on the thousandth.
        raise RuntimeError(
            f"Could not find a free name for '{file_name}' in {directory}"
        )

    return await asyncio.to_thread(_free)


async def ensure_download_dir(session_id: Any) -> Path:
    """Create this session's download directory if it is not there, and return it."""
    target = download_dir(session_id)
    await asyncio.to_thread(target.mkdir, parents=True, exist_ok=True)
    return target


def resolve_within_downloads(session_id: Any, candidate: str) -> Path:
    """
    Check a stored artifact path still points inside this session's directory.

    The same rule as :func:`resolve_within_export`, applied to the folder the download
    route actually opens. It matters more here than there: this path is reached from a
    URL whose session segment came off the wire, so "the file this row names is inside
    the folder that URL named" is the check that keeps one session's link from ever
    resolving into another's directory.
    """
    root = download_dir(session_id).resolve()
    path = Path(candidate).resolve()

    if root != path and root not in path.parents:
        raise ValueError(
            f"'{candidate}' is not inside the download directory for session "
            f"{session_folder(session_id)}"
        )

    return path


async def delete_artifact(session_id: Any, file_name: Optional[str]) -> bool:
    """
    Delete one finished artifact, and the session's directory if it was the last.

    Returns whether a file actually went, which is what the reaper logs. Pruning the
    empty directory is not tidiness for its own sake: a session that asked for fifty
    exports would otherwise leave fifty empty folders per visitor, forever, and nothing
    else ever removes them.
    """
    if not file_name:
        return False

    directory = download_dir(session_id)
    target = directory / Path(file_name).name

    def _remove() -> bool:
        existed = target.is_file()
        target.unlink(missing_ok=True)

        if directory.is_dir() and not any(directory.iterdir()):
            # Not rmtree: only an empty directory goes, so a concurrent export that has
            # just written into it cannot be swept away by another export's cleanup.
            directory.rmdir()

        return existed

    return await asyncio.to_thread(_remove)


async def delete_session_downloads(session_id: Any) -> int:
    """
    Remove every artifact for one chat session. Returns how many files went.

    For a session that ends — its files stop being reachable the moment it does, and
    leaving them on disk until their own TTL lapses keeps one visitor's data around
    after the only conversation entitled to it is over.
    """
    directory = download_dir(session_id)

    def _remove() -> int:
        if not directory.is_dir():
            return 0

        removed = sum(1 for entry in directory.iterdir() if entry.is_file())
        shutil.rmtree(directory, ignore_errors=True)
        return removed

    removed = await asyncio.to_thread(_remove)

    if removed:
        logger.info(
            "Removed %d download(s) for session %s", removed, session_folder(session_id),
        )

    return removed


def resolve_within_export(export_uuid: str, candidate: str) -> Path:
    """
    Check a stored path still points inside this export, and return it resolved.

    The download route reads ``file_path`` off a database row and opens it. That row
    was written by this application, so this is not defence against an attacker
    editing it — it is defence against *us*: a path built wrongly, a row copied
    between exports, a relative path resolved from the wrong working directory. Any of
    those would serve one user a different user's file, and the check costs a
    ``resolve()``.

    Raises ``ValueError`` rather than an HTTPException: this is a path rule, and the
    caller decides whether a broken one is a 404 (the download route) or a log line
    (the reaper). ``db_utils._resolve_safe_path`` makes the same choice.
    """
    root = export_dir(export_uuid).resolve()
    path = Path(candidate).resolve()

    if root != path and root not in path.parents:
        raise ValueError(
            f"'{candidate}' is not inside the directory for export {export_uuid}"
        )

    return path


async def ensure_parts_dir(export_uuid: str) -> Path:
    """Create the parts directory if it is not there, and return it."""
    target = parts_dir(export_uuid)
    await asyncio.to_thread(target.mkdir, parents=True, exist_ok=True)
    return target


async def ensure_export_dir(export_uuid: str) -> Path:
    """Create the export's own directory if it is not there, and return it."""
    target = export_dir(export_uuid)
    await asyncio.to_thread(target.mkdir, parents=True, exist_ok=True)
    return target


async def list_part_paths(export_uuid: str, extension: str) -> List[Path]:
    """
    Every part file for this export, in part-number order.

    Sorted by name, which is by number because the names are zero-padded — see
    :data:`_PART_NUMBER_WIDTH`. The merge depends on this order: a CSV whose parts
    were concatenated in the order the filesystem happened to return them would be a
    valid CSV containing the right rows in the wrong sequence, which is exactly the
    kind of wrong that nothing downstream notices.
    """
    directory = parts_dir(export_uuid)

    def _listing() -> List[Path]:
        if not directory.is_dir():
            return []
        return sorted(directory.glob(f"part-*{extension}"))

    return await asyncio.to_thread(_listing)


async def delete_part(path: Path) -> None:
    """
    Delete one part file, whether or not it is there.

    ``missing_ok`` on purpose: this is called after a failed batch, and a batch that
    failed before it created anything is the normal case, not an error. The pattern is
    ``knowledge_base_service``'s.
    """
    await asyncio.to_thread(lambda: Path(path).unlink(missing_ok=True))


async def delete_parts_dir(export_uuid: str) -> int:
    """
    Remove the parts directory and everything in it. Returns how many files went.

    Called by the cleanup node on both paths — after a successful merge, because the
    parts have been folded into the artifact, and after an abort, because they are
    fragments of a file that will never exist. Counting them first is for the log: "3
    part file(s) removed" and "0 part file(s) removed" are different stories after a
    failed export, and the second one means the failure happened before any batch
    was written.
    """
    directory = parts_dir(export_uuid)

    def _remove() -> int:
        if not directory.is_dir():
            return 0

        removed = sum(1 for entry in directory.iterdir() if entry.is_file())
        shutil.rmtree(directory, ignore_errors=True)
        return removed

    removed = await asyncio.to_thread(_remove)

    if removed:
        logger.info(
            "Removed %d part file(s) for export %s", removed, export_uuid,
        )

    return removed


async def delete_export_dir(export_uuid: str) -> None:
    """
    Remove an export's whole directory — artifact, parts and all.

    For the expiry reaper, and for an abort that got as far as writing a partial
    artifact. ``ignore_errors`` because a directory that is already gone is the
    outcome this function exists to produce.
    """
    directory = export_dir(export_uuid)
    await asyncio.to_thread(shutil.rmtree, directory, True)


async def file_size(path: Path) -> int:
    """The size of a file in bytes, or 0 if it is not there."""

    def _size() -> int:
        target = Path(path)
        return target.stat().st_size if target.is_file() else 0

    return await asyncio.to_thread(_size)
