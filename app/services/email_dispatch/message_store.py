"""
The session seam for code with no request, and the attempt log.

**``open_session`` is one patchable name, and that is the whole point.** The send worker
runs on a background task with no request behind it, so it cannot take the ``db``
dependency every route gets. It has to build its own session, and the *place* it builds it
has to be a single module-level function — because that is what a test monkeypatches to
point the worker at the same in-memory SQLite the assertions read.

Getting this wrong does not fail cleanly. The worker writes to the development Postgres
while the test looks at an in-memory SQLite and finds nothing, so the assertion fails with
"expected 1 message, got 0" and the reason is invisible. ``run_store.open_session`` and
``download_service.open_session`` exist for exactly this reason and
``tests/conftest.py``'s ``background_sessions`` fixture patches them; this module joins
them.

**Writing the log must never be what fails a send.** ``record_attempt`` swallows its own
exceptions the way ``run_store._quietly`` does. The attempt row is an *observation* of the
send, not part of it: an email that went out but whose audit row could not be written is a
delivered email, and turning that into a failure would make the queue retry it and deliver
it twice. The observation is logged at exception level so nothing is actually lost — it
moves from the table to the application log, which is the right trade when the alternative
is a duplicate in somebody's inbox.
"""

import logging
import os
import socket
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.db_sessions import AsyncSessionLocal
from app.models.email_dispatch import EmailMessageAttempt

logger = logging.getLogger(__name__)

#: How much of a server's reply is kept. Long enough for a multi-line DSN with a URL in it,
#: short enough that a chatty relay cannot fill the table.
MAX_RESPONSE_CHARS = 2000


def open_session() -> AsyncSession:
    """
    A session for code that has no request to take one from.

    Never used by a route — routes get ``db`` injected. See the module docstring for why
    this indirection is load-bearing rather than decorative.
    """
    return AsyncSessionLocal()


def worker_name() -> str:
    """
    Who is doing the work: host plus pid.

    For reading a log, never for a decision — the claim is done by ``SKIP LOCKED``, not by
    comparing this to anything. Identical to ``queue.worker_name`` in the integrations
    module so the two queues' rows read the same way in an incident.
    """
    return f"{socket.gethostname()}:{os.getpid()}"


async def record_attempt(
    *,
    message_id: int,
    attempt: int,
    status: str,
    error_message: str = "",
    smtp_response: str = "",
    retryable: bool = False,
    duration_ms: Optional[int] = None,
    worker: str = "",
) -> None:
    """
    Write one row saying what happened on this try.

    Opens and commits its own session rather than joining the caller's, for a reason worth
    stating: the caller's session may be in a failed transaction — a send that raised often
    leaves one — and an INSERT on a poisoned session raises again, which would turn the
    log write into a second, more confusing failure on top of the first.

    Never raises. See the module docstring.
    """
    try:
        async with open_session() as db:
            db.add(
                EmailMessageAttempt(
                    message_id=message_id,
                    attempt=attempt,
                    status=status,
                    error_message=(error_message or None),
                    smtp_response=(
                        smtp_response[:MAX_RESPONSE_CHARS] if smtp_response else None
                    ),
                    retryable=retryable,
                    duration_ms=duration_ms,
                    worker=(worker or worker_name())[:255],
                )
            )
            await db.commit()
    except Exception:  # noqa: BLE001 — deliberate: see the module docstring.
        logger.exception(
            "Could not record attempt %s for email message %s (status=%s). The send "
            "itself is unaffected.",
            attempt,
            message_id,
            status,
        )
