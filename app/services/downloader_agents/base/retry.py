"""
Retrying one batch, and giving up honestly when it will not work.

Three attempts per batch, with the part file deleted before each retry. That is the
whole rule, and it is small enough that the interesting part is not the loop but what
happens either side of it.

**Why the file is deleted first.** A batch fails somewhere inside writing its part —
after the header, after twenty rows, mid-row. What is on disk is then a fragment that
looks exactly like a part file and is not one. If the retry simply wrote again, either
it appends to the fragment (a file with a header in the middle) or it overwrites and
the fragment's size is silently wrong. Deleting is what makes an attempt an attempt
rather than an edit, and it is the caller's ``on_discard`` that does it, because only
the caller knows which file this batch owns.

**Why it retries at all.** A batch reads from *someone else's* database over a
connection this application does not control. A dropped connection, a lock timeout, a
failover — those are transient, and a whole export abandoned because of one of them is
a worse answer than trying again. What is not transient is a query that no longer
validates or a table that was switched off, and those raise
:class:`~app.services.deep_agents.query_executor.ToolQueryError`, which this module
does **not** retry: three attempts at a permanent failure is just three times the
wait before the same answer.

**Why it gives up out loud.** After the third failure the export stops. There is no
partial file, no "here are the first 2,000 records" — an export that silently contains
some of the data is the one outcome worse than no export, because nothing about the
file says so. The failure travels as :class:`BatchRetriesExhausted`, and the graph's
notify node turns it into the sentence the user hears.
"""

import asyncio
import logging
from typing import Any, Awaitable, Callable, Optional

from app.services.deep_agents.query_executor import ToolQueryError

logger = logging.getLogger(__name__)


# How many times one batch may be attempted, in total — not in addition to the first
# try. The user's number: "retry for 3 times" and then stop.
MAX_BATCH_ATTEMPTS = 3

# Wait before attempt 2, doubled before attempt 3. Short on purpose: the failures
# worth retrying here are connection-level, which either clear immediately or do not
# clear at all, and an export holding a cursor open is not a good place to sleep.
_BACKOFF_BASE_SECONDS = 0.5


class BatchRetriesExhausted(Exception):
    """
    One batch failed every attempt it was allowed.

    Carries the batch number, how many attempts were made and the last underlying
    failure, because all three appear in what is written to the export row and the
    log — and none of them appear in what the *user* is told, which is deliberately
    just "the file cannot be created at the moment".
    """

    def __init__(
        self,
        batch_number: int,
        attempts: int,
        last_error: BaseException,
    ) -> None:
        super().__init__(
            f"Batch {batch_number} failed after {attempts} attempt(s): {last_error}"
        )
        self.batch_number = batch_number
        self.attempts = attempts
        self.last_error = last_error


async def run_batch_with_retries(
    operation: Callable[[int], Awaitable[Any]],
    batch_number: int,
    on_discard: Optional[Callable[[int, int, BaseException], Awaitable[None]]] = None,
    max_attempts: int = MAX_BATCH_ATTEMPTS,
) -> Any:
    """
    Run ``operation`` for one batch, retrying up to ``max_attempts`` times.

    ``operation`` is called with the attempt number (1-based) — not because it needs
    to behave differently, but because it records the attempt on the part row, and
    passing it is cheaper than making the caller thread a counter through a closure.

    ``on_discard`` is awaited after every failed attempt, including the last, and is
    where the part file is deleted and the discarded attempt recorded. It runs on the
    final failure too: the fragment from attempt three is exactly as much of a lie as
    the fragment from attempt one, and the cleanup node should not be the first thing
    to notice it. A failure inside ``on_discard`` is logged and swallowed — it is
    running because something already went wrong, and replacing that failure with a
    tidying-up failure would lose the only useful diagnosis.

    Raises :class:`BatchRetriesExhausted` when every attempt failed, and re-raises a
    :class:`ToolQueryError` immediately without retrying — see the module docstring.
    """
    attempts = max(1, int(max_attempts))
    last_error: Optional[BaseException] = None

    for attempt in range(1, attempts + 1):
        try:
            return await operation(attempt)
        except ToolQueryError:
            # Permanent by construction: the stored query no longer validates, a
            # table was switched off, the datasource is not relational. Retrying
            # changes nothing except how long the user waits to be told.
            raise
        except asyncio.CancelledError:
            # Shutdown, not a batch failure. Must never be swallowed into a retry
            # loop, or a stopping worker keeps hammering a database on the way out.
            raise
        except Exception as exc:  # noqa: BLE001 — anything else is worth one more try
            last_error = exc
            logger.warning(
                "Export batch %d failed on attempt %d of %d: %s",
                batch_number,
                attempt,
                attempts,
                exc,
            )

            if on_discard is not None:
                try:
                    await on_discard(batch_number, attempt, exc)
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "Failed to discard part file for batch %d attempt %d",
                        batch_number,
                        attempt,
                    )

            if attempt < attempts:
                await asyncio.sleep(_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))

    raise BatchRetriesExhausted(
        batch_number=batch_number,
        attempts=attempts,
        # Unreachable with a None last_error: the loop either returned or set it.
        last_error=last_error or RuntimeError("unknown failure"),
    )
