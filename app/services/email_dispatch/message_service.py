"""
The delivery log: reading it, and the two actions available from it.

**This module reads the message row and nothing it points at.** ``template_name``,
``smtp_host`` and ``trigger_kind`` were copied onto the message at enqueue precisely so
that the log renders without joining three tables that may no longer have the rows. A view
built by following ``template_id`` would show blanks for exactly the history somebody is
most likely to be investigating.

**Retry and cancel live here rather than in ``dispatch_service``** even though they mutate a
message, because both are *operator actions on the log* with their own preconditions, and
because keeping them out of the enqueue path keeps that path to one job. ``dispatch_service``
answers "how does an email get queued"; this answers "what can somebody do about one that
is already there".

**Sending a test email is the one thing here that creates a message**, and it goes through
``dispatch_service.enqueue_email`` like everything else. A test that used a shortcut would
be a test of the shortcut.
"""

import logging
import uuid as uuid_pkg
from typing import Any, Dict, Optional

from litestar.exceptions import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.db_utils import CRUDQueryBuilder
from app.db.email_dispatch.queries import message_counts_by_status
from app.models.email_dispatch import (
    MESSAGE_QUEUED,
    MESSAGE_SOURCE_LABELS,
    MESSAGE_SOURCE_VALUES,
    MESSAGE_STATUS_LABELS,
    MESSAGE_STATUS_VALUES,
    MESSAGE_STATUSES,
    SOURCE_MANUAL,
    TERMINAL_MESSAGE_STATUSES,
    EmailMessage,
    EmailMessageAttempt,
)
from app.services.email_dispatch import dispatch_service, queue, template_service
from app.services.email_dispatch.errors import EmailFailure

logger = logging.getLogger(__name__)

message_crud = CRUDQueryBuilder(EmailMessage)

#: Rows per page. The log only grows, so it is always paged.
PAGE_SIZE = 50


# --------------------------------------------------------------------------
# Read
# --------------------------------------------------------------------------


def build_view(message: EmailMessage) -> Dict[str, Any]:
    """
    One log row.

    ``can_retry`` / ``can_cancel`` are computed here rather than in the template, so the
    button the operator sees and the precondition the service enforces come from one
    expression. A template deciding for itself is how a visible button starts returning 400.
    """
    return {
        "uuid": str(message.uuid),
        "subject": message.subject,
        "to_addresses": list(message.to_addresses or []),
        "cc_addresses": list(message.cc_addresses or []),
        "bcc_addresses": list(message.bcc_addresses or []),
        "from_email": message.from_email or "",
        "status": message.status,
        "status_label": MESSAGE_STATUS_LABELS.get(message.status, message.status),
        "source": message.source or "",
        "source_label": MESSAGE_SOURCE_LABELS.get(message.source, message.source or ""),
        "source_ref": message.source_ref or "",
        "template_name": message.template_name or "",
        "smtp_host": message.smtp_host or "",
        "attempt": message.attempt or 0,
        "max_attempts": message.max_attempts or 0,
        "error_message": message.error_message or "",
        "smtp_response": message.smtp_response or "",
        "created_at": message.created_at,
        "sent_at": message.sent_at,
        "next_attempt_at": message.next_attempt_at,
        "can_retry": message.status in TERMINAL_MESSAGE_STATUSES,
        "can_cancel": message.status == MESSAGE_QUEUED,
    }


def build_attempt_view(attempt: EmailMessageAttempt) -> Dict[str, Any]:
    """One attempt, for the detail pane's timeline."""
    return {
        "uuid": str(attempt.uuid),
        "attempt": attempt.attempt,
        "status": attempt.status,
        "status_label": MESSAGE_STATUS_LABELS.get(attempt.status, attempt.status),
        "error_message": attempt.error_message or "",
        "smtp_response": attempt.smtp_response or "",
        "retryable": attempt.retryable,
        "duration_ms": attempt.duration_ms,
        "worker": attempt.worker or "",
        "started_at": attempt.started_at,
    }


def _validated_choice(value: Any, allowed: frozenset, label: str) -> Optional[str]:
    """
    A filter value that has to be in a vocabulary, or ``None`` for "no filter".

    Refused rather than ignored. A mistyped ``?status=snet`` silently showing every message
    is worse than a sentence: the operator reads an unfiltered table as an answer to the
    question they thought they asked.
    """
    cleaned = str(value or "").strip().lower()
    if not cleaned:
        return None
    if cleaned not in allowed:
        offered = ", ".join(sorted(allowed))
        raise HTTPException(
            status_code=400,
            detail=f"'{cleaned}' is not a {label}. Choose one of: {offered}.",
        )
    return cleaned


async def list_views(
    db: AsyncSession,
    user_id: int,
    *,
    status: Any = None,
    source: Any = None,
    page: Any = 1,
) -> Dict[str, Any]:
    """
    A page of the log, newest first, with the counts for the filter chips.

    The chips are merged against ``MESSAGE_STATUSES`` rather than built from whatever the
    database returned, so their order comes from the vocabulary and a status with no rows
    still shows as zero instead of vanishing — a chip that appears and disappears as mail
    flows is harder to use than one that reads nought.
    """
    clean_status = _validated_choice(status, MESSAGE_STATUS_VALUES, "status")
    clean_source = _validated_choice(source, MESSAGE_SOURCE_VALUES, "source")

    try:
        page_number = max(1, int(page or 1))
    except (TypeError, ValueError):
        page_number = 1

    filters: Dict[str, Any] = {"user_id": user_id}
    if clean_status:
        filters["status"] = clean_status
    if clean_source:
        filters["source"] = clean_source

    messages = await message_crud.get_many(
        db,
        filters=filters,
        skip=(page_number - 1) * PAGE_SIZE,
        limit=PAGE_SIZE,
        order_by="created_at",
        desc=True,
    )

    counts = await message_counts_by_status(db, user_id)

    return {
        "messages": [build_view(message) for message in messages],
        "chips": [
            {
                "value": value,
                "label": label,
                "count": counts.get(value, 0),
                "selected": clean_status == value,
            }
            for value, label in MESSAGE_STATUSES
        ],
        "total": sum(counts.values()),
        "status": clean_status or "",
        "source": clean_source or "",
        "page": page_number,
        # There is no total-pages count on purpose: it would need a second COUNT over the
        # filtered set on every page load of a table that only grows. A full page means
        # there may be another one, which is all the pager needs to know.
        "has_more": len(messages) == PAGE_SIZE,
    }


async def get_detail(
    db: AsyncSession, user_id: int, message_id: uuid_pkg.UUID
) -> Dict[str, Any]:
    """
    One message with its full body and every attempt.

    404 rather than 403 for somebody else's message — a 403 would confirm the uuid is real.
    """
    message = await message_crud.get_by_uuid(
        db, message_id, extra_filters={"user_id": user_id}
    )
    if not message:
        raise HTTPException(status_code=404, detail="Email not found")

    attempts = (
        await db.execute(
            select(EmailMessageAttempt)
            .where(EmailMessageAttempt.message_id == message.id)
            .order_by(EmailMessageAttempt.attempt)
        )
    ).scalars().all()

    view = build_view(message)
    view.update(
        {
            "body_html": message.body_html or "",
            "body_text": message.body_text or "",
            "attempts": [build_attempt_view(attempt) for attempt in attempts],
        }
    )
    return view


# --------------------------------------------------------------------------
# Actions
# --------------------------------------------------------------------------


async def retry(
    db: AsyncSession, user_id: int, message_id: uuid_pkg.UUID
) -> EmailMessage:
    """
    Send a failed message again, unchanged. Commits, then wakes the worker.

    Commits here rather than leaving it to the route because :func:`queue.wake` must happen
    *after* the commit — a woken worker looks immediately, and if the transaction has not
    landed it finds nothing and sleeps for the full poll interval. Keeping the pair together
    is what stops a caller getting the order wrong.
    """
    message = await dispatch_service.retry_message(db, user_id, message_id)
    await db.commit()
    queue.wake()
    return message


async def cancel(
    db: AsyncSession, user_id: int, message_id: uuid_pkg.UUID
) -> EmailMessage:
    """Stop a queued message. Nothing to wake — this removes work rather than adding it."""
    message = await dispatch_service.cancel_message(db, user_id, message_id)
    await db.commit()
    return message


async def send_test(
    db: AsyncSession,
    user_id: int,
    *,
    template_id: uuid_pkg.UUID,
    config_id: uuid_pkg.UUID,
    to_address: str,
) -> EmailMessage:
    """
    Queue one real email to a chosen address, using the template's own sample values.

    **This sends actual mail**, unlike the SMTP Send-test button, which only opens a
    connection. The two are deliberately separate: one answers "can I reach the server", the
    other answers "does my template look right when it arrives", and conflating them would
    mean every connection check spammed somebody.

    Sample values, not real ones — ``«CUSTOMER»`` where nothing is bound. A test that
    invented plausible data would let an operator believe the wiring works before anything
    is wired.
    """
    template = await dispatch_service.resolve_template(db, user_id, template_id)
    config = await dispatch_service.resolve_config(db, user_id, config_id)

    try:
        message = await dispatch_service.enqueue_email(
            db,
            user_id=user_id,
            template=template,
            config=config,
            recipients={"to": [to_address]},
            values=template_service.sample_values(template.variables),
            source=SOURCE_MANUAL,
            source_ref="test send",
            # Ahead of any queued digest: somebody is standing there waiting for it.
            priority=10,
        )
    except EmailFailure as exc:
        # Rollback before raising: the HTMX route re-renders in this same session, and a
        # failed transaction would make that render fail instead of showing the message.
        await db.rollback()
        raise HTTPException(status_code=400, detail=exc.message) from exc

    await db.commit()
    queue.wake()
    return message
