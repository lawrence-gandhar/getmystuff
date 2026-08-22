"""
The send queue, and the worker that drains it.

**A table, not a broker.** There is no Redis, no Celery and no arq in this project, and
this does not add one. An ``email_messages`` row claimed with ``FOR UPDATE SKIP LOCKED`` is
a queue that is durable across restarts, safe across processes, and visible in the same
database as everything it is about. What a broker would add is throughput this feature will
never need and a service to operate that it would not justify. ``ENGINEERING_TECHNOLOGY.md``
§29 records the same decision twice already, for the export queue and the sync queue; this
is the third time and the reasoning has not changed.

**A hand-sent test goes through the same queue.** Pressing Send test inserts a message
exactly as an event trigger does, and an ``asyncio.Event`` wakes the worker so it starts
immediately rather than at the next poll. That is what makes the send somebody tested at
eleven in the morning the same code path as the one that fires at three — and there is no
second execution path to keep in step.

**Two at a time, and the real limit is in the claim.** ``EMAIL_WORKER_CONCURRENCY`` bounds
how many messages this process sends at once, but what protects any single mail server is
``claim_next_email``'s refusal to take a second message for a config that already has one
in flight. Providers respond to a burst of parallel connections by throttling, greylisting
or classifying the sender as a spam source, and the last of those is not something a retry
fixes. So the global limit is a courtesy and the per-server serialisation is the control.

**A dead worker fails its message rather than resuming it.** ``requeue_stale_emails`` says
why at length: the dead worker may already have completed the SMTP conversation, so the mail
could be in somebody's inbox right now, and sending again would deliver it twice. The
operator gets a message that says delivery is unknown and a Retry button — and pressing it
is their decision with the risk stated.

**Every failure inside one send is that message's failure.** The loop itself only ever stops
on cancellation. A worker that exited because one relay refused one address would take
email down silently until somebody restarted the application — and nobody is watching at
three.
"""

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.db_utils import CRUDQueryBuilder
from app.db.email_dispatch.queries import (
    claim_next_email,
    email_heartbeat,
    finish_email,
    queued_email_count,
    requeue_email,
    requeue_stale_emails,
)
from app.models.email_dispatch import (
    MESSAGE_FAILED,
    MESSAGE_SENT,
    EmailMessage,
    EmailSmtpConfig,
)
from app.services.email_dispatch import message_store, retry, sender
from app.services.email_dispatch.errors import DispatchError, EmailFailure, SendError
from app.utils.crypto import decrypt_secret

logger = logging.getLogger(__name__)

message_crud = CRUDQueryBuilder(EmailMessage)
config_crud = CRUDQueryBuilder(EmailSmtpConfig)

#: How many messages one process sends at once. Two, matching the integrations queue —
#: see the module docstring on why this is not the interesting limit.
WORKER_CONCURRENCY = int(os.getenv("EMAIL_WORKER_CONCURRENCY", "2"))

#: How long a worker waits when the queue is empty. Long, because the ``asyncio.Event``
#: below is what makes a hand-sent test start immediately — the poll only has to catch a
#: message inserted by *another* process, or one whose retry backoff has come due.
POLL_INTERVAL_SECONDS = float(os.getenv("EMAIL_WORKER_POLL_SECONDS", "5"))

#: How often a sending message's heartbeat is written.
HEARTBEAT_INTERVAL_SECONDS = float(os.getenv("EMAIL_WORKER_HEARTBEAT_SECONDS", "10"))

#: How quiet a sending message must go before its worker is assumed dead. Six times the
#: heartbeat, so a worker briefly starved of the event loop is not declared dead while it
#: is still waiting on a slow relay.
STALE_AFTER_SECONDS = float(
    os.getenv("EMAIL_WORKER_STALE_SECONDS", str(HEARTBEAT_INTERVAL_SECONDS * 6))
)

#: How long the loop waits after an unexpected failure, so a persistent fault logs at a
#: readable rate instead of filling the log as fast as it can.
LOOP_ERROR_BACKOFF_SECONDS = 30.0

_workers: List[asyncio.Task] = []

# Set when a message is enqueued in this process, so a hand-sent test does not wait for the
# poll. A plain flag rather than a queue of ids: the worker's job is to look, and what it
# finds is whatever the claim gives it — which may be a different message entirely, and
# should be.
_wakeup = asyncio.Event()


def worker_name() -> str:
    """Who claimed a message, for reading a log by. Not used for any decision — the claim
    is done by row locking."""
    return message_store.worker_name()


# ---------------------------------------------------------------------------
# Enqueue
# ---------------------------------------------------------------------------


async def enqueue(db: AsyncSession, values: dict) -> EmailMessage:
    """
    Put one already-rendered message on the queue, in the caller's transaction.

    ``create_pending``, not ``create``: the message and whatever else the caller is writing
    in the same breath — a trigger's ``last_fired_at``, a node's step row — have to land
    together or not at all. A crash between two commits either loses an email or records
    one that was never queued.

    The caller commits. :func:`wake` is deliberately a separate call so it can happen
    *after* that commit — waking a worker before the transaction lands is how it looks for a
    message that is not there yet, finds nothing, and goes back to sleep for the full poll
    interval.
    """
    return await message_crud.create_pending(db, values)


def wake() -> None:
    """
    Tell this process's workers to look now rather than at the next poll.

    Only helps in the process that enqueued, which is the single-replica case and the one
    somebody is watching. Everywhere else the poll is the mechanism, and that is why the
    poll still exists rather than being replaced by this.
    """
    _wakeup.set()


# ---------------------------------------------------------------------------
# The worker
# ---------------------------------------------------------------------------


def start_workers(count: Optional[int] = None) -> List[asyncio.Task]:
    """
    Start this process's workers, if they are not already running.

    Idempotent: calling twice does not produce two sets competing for the same messages.
    Returns the tasks so a caller can await them and a test can cancel them.
    """
    global _workers

    live = [task for task in _workers if not task.done()]
    if live:
        return live

    how_many = max(1, int(count if count is not None else WORKER_CONCURRENCY))
    _workers = [
        asyncio.create_task(run_worker(), name=f"email-send-worker-{index}")
        for index in range(how_many)
    ]
    logger.info("Email send workers started (%d) as %s", how_many, worker_name())
    return _workers


async def stop_workers() -> None:
    """
    Stop the workers and wait for them to unwind.

    Cancels rather than sets a flag, and swallows the ``CancelledError``: shutdown is not a
    failure. A message cancelled mid-send stays ``sending``, which ``requeue_stale_emails``
    fails after ``STALE_AFTER_SECONDS`` — the same recovery a crash gets, which is the point
    of having only one. That it is *failed* rather than retried is deliberate even on a
    clean shutdown: we still cannot tell whether the relay took it.
    """
    global _workers

    tasks, _workers = _workers, []
    live = [task for task in tasks if not task.done()]

    for task in live:
        task.cancel()

    for task in live:
        try:
            await task
        except asyncio.CancelledError:
            pass

    if live:
        logger.info("Email send workers stopped")


def live_worker_count() -> int:
    return len([task for task in _workers if not task.done()])


async def run_worker() -> None:
    """
    Claim messages and send them, forever.

    Every failure inside one send is that message's failure and is recorded on it; the loop
    only stops on cancellation. See the module docstring.
    """
    while True:
        try:
            did_work = await drain_once()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — the loop must outlive any single failure
            logger.exception("The email send worker hit an unexpected failure")
            await asyncio.sleep(LOOP_ERROR_BACKOFF_SECONDS)
            continue

        if not did_work:
            await _wait_for_work()


async def _wait_for_work() -> None:
    """
    Sleep until something is enqueued here, or until the poll comes round.

    The event is cleared in a ``finally`` rather than before the wait, so a message enqueued
    while a worker was busy is not missed — the flag set during the last send is still there
    and the wait returns immediately.
    """
    try:
        await asyncio.wait_for(_wakeup.wait(), timeout=POLL_INTERVAL_SECONDS)
    except asyncio.TimeoutError:
        return
    finally:
        _wakeup.clear()


async def drain_once() -> bool:
    """
    Fail anything stale, then claim and send one message. ``True`` if one was sent.

    Split out of the loop so a test can drive exactly one iteration, and so "what the worker
    does" is readable without reading the loop that repeats it.
    """
    async with message_store.open_session() as db:
        await requeue_stale_emails(db, STALE_AFTER_SECONDS)
        message = await claim_next_email(db, claimed_by=worker_name())

    if message is None:
        return False

    await _send_claimed(message.id)
    return True


async def _send_claimed(message_id: int) -> None:
    """
    Send one claimed message, with a heartbeat alongside it.

    The heartbeat is a separate task rather than something the send calls, because the
    interesting stall is one where the send is blocked on a socket — which is exactly when
    it would not get round to calling anything.
    """
    beat = asyncio.create_task(_beat(message_id))
    try:
        await _attempt_send(message_id)
    except asyncio.CancelledError:
        # Shutdown. The message stays `sending` and is failed by the stale reaper — the same
        # recovery a crash gets, which is why there is only one.
        raise
    except Exception:  # noqa: BLE001 — one bad message must not stop the worker
        logger.exception("Email message %s failed outside the send", message_id)
        await _finish_quietly(
            message_id,
            status=MESSAGE_FAILED,
            error_message=(
                "Something went wrong preparing this email to be sent. It has not "
                "been delivered. Please contact support if this keeps happening."
            ),
        )
    finally:
        beat.cancel()


async def _attempt_send(message_id: int) -> None:
    """
    One attempt: build the target, hand it to the sender, record what happened.

    Re-reads the message inside its own session rather than trusting the row the claim
    returned. The claim committed and closed; the object it handed back is detached, and
    reading a column off it later is either stale or an error depending on the expiry
    settings. Cheap, and it removes a whole class of "why is this field empty" question.
    """
    async with message_store.open_session() as db:
        message = await message_crud.get_one(db, {"id": message_id})
        if message is None:
            # Deleted between the claim and now. Nothing to send and nothing to record.
            logger.warning("Email message %s disappeared after being claimed", message_id)
            return

        attempt = int(message.attempt or 1)
        max_attempts = int(message.max_attempts or 1)
        config = (
            await config_crud.get_one(db, {"id": message.smtp_config_id})
            if message.smtp_config_id
            else None
        )

        payload = {
            "subject": message.subject,
            "body_html": message.body_html,
            "body_text": message.body_text,
            "from_email": message.from_email,
            "from_name": message.from_name,
            "reply_to": message.reply_to,
            "to_addresses": list(message.to_addresses or []),
            "cc_addresses": list(message.cc_addresses or []),
            "bcc_addresses": list(message.bcc_addresses or []),
        }

    if config is None:
        # The config was deleted after the message was queued. Not retryable: no later
        # attempt finds it again. Failed with a sentence naming the actual problem, rather
        # than letting it fail obscurely at connect time on an empty hostname.
        await _record_and_finish(
            message_id,
            attempt=attempt,
            status=MESSAGE_FAILED,
            error_message=(
                "The SMTP server this email was queued for no longer exists. Point the "
                "template at a current server and send it again."
            ),
            retryable=False,
        )
        return

    if not config.is_active:
        await _record_and_finish(
            message_id,
            attempt=attempt,
            status=MESSAGE_FAILED,
            error_message=(
                f"The SMTP server '{config.name}' is switched off, so this email was "
                "not sent. Switch it back on and press Retry."
            ),
            retryable=False,
        )
        return

    target = sender.SmtpTarget(
        host=config.host,
        port=int(config.port),
        security=config.security,
        username=config.username,
        # Decrypted here and held only for the duration of the call. Never written back to
        # the message, never logged, never in a SendResult.
        password=(
            decrypt_secret(config.password_encrypted)
            if config.password_encrypted
            else None
        ),
        timeout_seconds=int(config.timeout_seconds or 30),
    )

    started = datetime.now(timezone.utc)
    try:
        result = await sender.send_message(target=target, **payload)
    except (SendError, DispatchError) as exc:
        await _handle_failure(
            message_id,
            attempt=attempt,
            max_attempts=max_attempts,
            exc=exc,
            started=started,
        )
        return

    duration_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)

    # A partial acceptance is recorded as sent — the message did go — but the refused
    # addresses go in the log line, because "sent" with three of five recipients silently
    # dropped is the kind of half-truth this module exists to avoid.
    note = (
        f" Refused by the server: {', '.join(result.rejected)}."
        if result.rejected
        else ""
    )

    await message_store.record_attempt(
        message_id=message_id,
        attempt=attempt,
        status=MESSAGE_SENT,
        smtp_response=f"{result.response}{note}",
        retryable=False,
        duration_ms=duration_ms,
        worker=worker_name(),
    )
    await _finish_quietly(
        message_id,
        status=MESSAGE_SENT,
        error_message="",
        smtp_response=f"{result.response}{note}",
        sent_at=datetime.now(timezone.utc),
    )


async def _handle_failure(
    message_id: int,
    *,
    attempt: int,
    max_attempts: int,
    exc: EmailFailure,
    started: datetime,
) -> None:
    """
    Record the attempt, then either back off or give up.

    ``DispatchError`` is never retried — it means a person has to change something, and a
    queue that retries a misconfiguration just writes the same failure five times.
    """
    duration_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)

    retryable = getattr(exc, "retryable", False)
    permanent = getattr(exc, "permanent", False)
    smtp_response = getattr(exc, "smtp_response", "") or ""

    await message_store.record_attempt(
        message_id=message_id,
        attempt=attempt,
        status=MESSAGE_FAILED,
        error_message=exc.message,
        smtp_response=smtp_response,
        retryable=bool(retryable),
        duration_ms=duration_ms,
        worker=worker_name(),
    )

    will_retry = retry.should_retry(
        attempt=attempt,
        max_attempts=max_attempts,
        retryable=bool(retryable),
        permanent=bool(permanent),
    )

    if not will_retry:
        await _finish_quietly(
            message_id,
            status=MESSAGE_FAILED,
            error_message=_final_message(
                exc.message,
                attempt=attempt,
                retryable=bool(retryable),
                permanent=bool(permanent),
            ),
            smtp_response=smtp_response,
        )
        return

    due = retry.next_attempt_at(attempt)
    try:
        async with message_store.open_session() as db:
            await requeue_email(
                db,
                message_id,
                next_attempt_at=due,
                error_message=exc.message,
                smtp_response=smtp_response,
            )
    except Exception:  # noqa: BLE001 — the attempt is already recorded
        logger.exception("Could not requeue email message %s", message_id)


def _final_message(
    message: str,
    *,
    attempt: int,
    retryable: bool,
    permanent: bool,
) -> str:
    """
    The sentence that ends up on a message nothing further will happen to.

    The sender writes "It will be tried again." while it still might be, because at the
    moment of failure that is true and is what the log should say. Once the attempts are
    spent it is no longer true, and leaving it standing on a row that will never move again
    is the kind of small lie that costs somebody an afternoon. So it is replaced rather than
    appended to.

    A ``permanent`` failure never carried that clause in the first place, so it is returned
    untouched — the server has said the thing will not work and repeating the attempt count
    adds nothing.
    """
    if permanent or not retryable:
        return message

    exhausted = f"It was tried {attempt} time{'s' if attempt != 1 else ''} and gave up."
    if "It will be tried again." in message:
        return message.replace("It will be tried again.", exhausted)
    return f"{message} {exhausted}"


async def _beat(message_id: int) -> None:
    """
    Write the message's heartbeat until cancelled.

    Swallows everything but cancellation: a heartbeat that raised would fail a send for a
    reason unrelated to it, and the consequence of a missing heartbeat is already handled —
    ``requeue_stale_emails`` fails the message, which is strictly better than failing it
    here for a transient database blip while the mail was going out fine.
    """
    try:
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
            async with message_store.open_session() as db:
                await email_heartbeat(db, message_id)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        logger.exception("Heartbeat stopped for email message %s", message_id)


async def _finish_quietly(
    message_id: int,
    *,
    status: str,
    error_message: str = "",
    smtp_response: str = "",
    sent_at: Optional[datetime] = None,
) -> None:
    """Close a message off, logging rather than raising if the close itself fails — the send
    has already happened or already failed, and the attempt row records it either way."""
    try:
        async with message_store.open_session() as db:
            await finish_email(
                db,
                message_id,
                status=status,
                error_message=error_message,
                smtp_response=smtp_response,
                sent_at=sent_at,
            )
    except Exception:  # noqa: BLE001
        logger.exception("Could not close email message %s", message_id)


async def _record_and_finish(
    message_id: int,
    *,
    attempt: int,
    status: str,
    error_message: str,
    retryable: bool,
) -> None:
    """Write the attempt row and the terminal status together, for the failures that never
    reached a socket."""
    await message_store.record_attempt(
        message_id=message_id,
        attempt=attempt,
        status=status,
        error_message=error_message,
        retryable=retryable,
        worker=worker_name(),
    )
    await _finish_quietly(message_id, status=status, error_message=error_message)


async def depth(db: AsyncSession) -> int:
    """How many messages are waiting. For the log page and for a shutdown log line."""
    return await queued_email_count(db)
