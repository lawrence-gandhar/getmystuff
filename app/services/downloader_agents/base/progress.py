"""
Turning a build in progress into a stream of events somebody can watch.

An export of a hundred thousand records is two thousand batches. Without this the only
honest thing the interface can say is "it is being prepared", indefinitely — and a
stalled export and a slow one look identical, which is the worst possible thing for the
one question a person actually has.

**Read from the database, not from a bus.** The worker writing the part files and the
request streaming this feed are different tasks, and under more than one replica they are
different *processes*. An in-memory event bus would work in exactly the configuration this
application is not guaranteed to run in. The ``download_export_parts`` rows are already
written per batch for their own reasons, so the progress feed reads them — which also
means a browser that reconnects halfway through sees the whole story rather than only what
happened after it arrived.

**Polling, and why that is not a compromise here.** :func:`stream_progress` re-reads the
export and its parts on an interval. The alternative — LISTEN/NOTIFY — would be one more
database driver behaviour to depend on, to save a small query every second or two on a
page somebody has deliberately opened to watch a slow job. The interval is the cost and it
is measured in seconds against a job measured in minutes.

**Every frame is a state, not a delta.** A frame carries the counters as they are, so a
client that missed one is not left with a wrong total; and the stream always ends with
either ``ready`` or ``failed``, so a consumer knows to stop rather than inferring it from
silence.
"""

import asyncio
import logging
from typing import Any, AsyncIterator, Callable, Dict, List, Optional

from app.models.downloader_agents import (
    EXPORT_FAILED,
    EXPORT_READY,
    PART_DISCARDED,
)
from app.schemas.downloader_agents import DownloadProgressEvent
from app.services.downloader_agents.base import download_service as svc
from app.services.downloader_agents.base.record_reader import BATCH_SIZE

logger = logging.getLogger(__name__)


# How often the export and its parts are re-read. One second: fast enough that a
# per-batch progress bar moves, slow enough that watching a ten-minute export costs six
# hundred small queries rather than sixty thousand.
POLL_INTERVAL_SECONDS = 1.0

# How long a stream stays open before it gives up. A browser tab left open on a failed
# build should not hold a connection forever, and an export that has not finished in an
# hour is not going to.
MAX_STREAM_SECONDS = 3600.0

# The states that end the stream. Both are terminal on the export row, so there is
# nothing further to report once one is reached.
_TERMINAL = {EXPORT_READY, EXPORT_FAILED}


def expected_parts(total_rows: Optional[int]) -> Optional[int]:
    """
    How many part files an export of ``total_rows`` records will produce.

    Derived rather than stored, because it is only ever used to render "part 12 of 97" —
    and a stored copy would be a second number that could disagree with the batch size
    the reader actually used.
    """
    if not total_rows:
        return None

    return max(1, -(-int(total_rows) // BATCH_SIZE))


def frames_for(
    export: Any,
    parts: List[Any],
    download_url: Optional[str] = None,
) -> List[DownloadProgressEvent]:
    """
    The complete event history of one export, as frames.

    Pure: given a row and its parts it produces the same frames every time, which is what
    makes both the stream and its tests straightforward. The stream calls this on each
    poll and emits only the frames it has not sent yet.

    A discarded part becomes a ``retry`` frame rather than being hidden. A retry is
    precisely what somebody watching a slow export wants to see — it is the difference
    between "this is big" and "this is struggling".
    """
    total_parts = expected_parts(export.total_rows)
    frames: List[DownloadProgressEvent] = []
    written = 0

    for part in parts:
        if part.status == PART_DISCARDED:
            frames.append(
                DownloadProgressEvent.build(
                    {
                        "event": DownloadProgressEvent.RETRY,
                        "export_id": export.uuid,
                        "status": export.status,
                        "part": part.part_number,
                        "of": total_parts,
                        "attempt": part.attempts,
                        "rows_written": written,
                        "total_rows": export.total_rows,
                        "message": (
                            f"Part {part.part_number} failed on attempt "
                            f"{part.attempts} and is being retried."
                        ),
                    }
                )
            )
            continue

        written += part.row_count or 0

        frames.append(
            DownloadProgressEvent.build(
                {
                    "event": DownloadProgressEvent.PROGRESS,
                    "export_id": export.uuid,
                    "status": export.status,
                    "part": part.part_number,
                    "of": total_parts,
                    "attempt": part.attempts,
                    "rows_written": written,
                    "total_rows": export.total_rows,
                }
            )
        )

    terminal = _terminal_frame(export, total_parts, download_url)

    if terminal is not None:
        frames.append(terminal)

    return frames


def _terminal_frame(
    export: Any,
    total_parts: Optional[int],
    download_url: Optional[str],
) -> Optional[DownloadProgressEvent]:
    """The closing ``ready`` or ``failed`` frame, or None while it is still running."""
    if export.status == EXPORT_READY:
        return DownloadProgressEvent.build(
            {
                "event": DownloadProgressEvent.READY,
                "export_id": export.uuid,
                "status": export.status,
                "of": total_parts,
                "rows_written": export.rows_written or 0,
                "total_rows": export.total_rows,
                "message": "The file is ready to download.",
                "download_url": download_url,
                "file_name": export.file_name,
                "byte_size": export.byte_size,
            }
        )

    if export.status == EXPORT_FAILED:
        return DownloadProgressEvent.build(
            {
                "event": DownloadProgressEvent.FAILED,
                "export_id": export.uuid,
                "status": export.status,
                "of": total_parts,
                "rows_written": export.rows_written or 0,
                "total_rows": export.total_rows,
                "message": export.error_message or svc.FAILURE_MESSAGE,
            }
        )

    return None


async def stream_progress(
    export_uuid: Any,
    download_url_for: Callable[[Any], Optional[str]],
    poll_interval: float = POLL_INTERVAL_SECONDS,
    max_seconds: float = MAX_STREAM_SECONDS,
) -> AsyncIterator[Dict[str, Any]]:
    """
    Yield one dict per new progress frame until the export finishes.

    Opens a **short session per poll** rather than holding one for the life of the stream.
    A stream can be open for the whole of a long export, and a session held that long is a
    connection out of the pool for the whole of it — for a reader that is idle between
    polls.

    ``download_url_for`` is a callback rather than a string because the link differs by
    audience (an operator and a widget visitor fetch the same file from two different
    paths) and is only known once the export is ready.

    The caller wraps each dict in an SSE message; this generator deliberately knows
    nothing about SSE, so it can be tested by iterating it.
    """
    sent = 0
    loop = asyncio.get_running_loop()
    # A real deadline off the loop's clock, not a counter of intervals. Two reasons: a poll
    # that takes longer than its interval would otherwise stretch the budget without
    # accounting for it, and an interval of zero would never advance the counter at all —
    # which is an infinite loop rather than a fast one.
    deadline = loop.time() + max_seconds

    while True:
        async with svc.open_session() as db:
            export = await svc.get_export(db, export_uuid)

            if export is None:
                # Deleted mid-stream. Nothing further can be said about it, and an
                # invented failure frame would be a claim we cannot support.
                logger.info("Export %s vanished while streaming progress", export_uuid)
                return

            parts = await svc.part_progress(db, export.id)
            frames = frames_for(export, parts, download_url_for(export))
            status = export.status

        for frame in frames[sent:]:
            yield frame.payload()

        sent = max(sent, len(frames))

        if status in _TERMINAL:
            return

        if loop.time() >= deadline:
            logger.info(
                "Stopped streaming progress for export %s after %ss",
                export_uuid,
                max_seconds,
            )
            return

        await asyncio.sleep(poll_interval)
