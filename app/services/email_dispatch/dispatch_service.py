"""
The one way an email gets queued.

**Every send in the application goes through :func:`enqueue_email`.** The three canvas
nodes, the event-bus subscriber, the inbound webhook and the Send-test button all call this
function and nothing else. That is the single most important property of the module: there
is no second path that renders differently, validates less, or forgets to write the log
row. When something is wrong with how mail goes out, there is exactly one function to read.

**Rendering happens here, before the row exists.** The alternative — store the template id
plus a variable map, render in the worker — fails twice over. The values come from a live
run whose state is gone by the time the worker looks, and a log holding a template
reference cannot answer "what did we actually send" once the template has been edited. So
the message row carries finished text, and :func:`enqueue_email` is where a
:class:`RenderError` stops the whole thing before anything is queued. A queued row is a row
a worker will send; there must never be a half-rendered one.

**The caller commits.** ``enqueue`` uses ``create_pending``, so the message lands in the
caller's transaction alongside whatever else they are writing — a trigger's
``last_fired_at``, a graph's step row. The caller then commits and calls
:func:`queue.wake`, in that order. Waking first is how a worker looks for a message that is
not there yet.

**Ownership is resolved from the config and the template, never taken on trust.** Both are
fetched by uuid with ``extra_filters={"user_id": ...}``, so a node in somebody else's flow
cannot name this user's SMTP server. A missing row is a 404 with the same sentence whether
it does not exist or belongs to another account — a different answer for the two would
confirm which uuids are real.
"""

import hashlib
import logging
import uuid as uuid_pkg
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from litestar.exceptions import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.db_utils import CRUDQueryBuilder
from app.models.email_dispatch import (
    DEFAULT_MAX_ATTEMPTS,
    MESSAGE_CANCELLED,
    MESSAGE_QUEUED,
    MESSAGE_SENDING,
    MESSAGE_SOURCE_VALUES,
    SOURCE_MANUAL,
    TERMINAL_MESSAGE_STATUSES,
    EmailMessage,
    EmailSmtpConfig,
    EmailTemplate,
)
from app.services.email_dispatch import queue, rendering
from app.services.email_dispatch.errors import DispatchError

logger = logging.getLogger(__name__)

message_crud = CRUDQueryBuilder(EmailMessage)
config_crud = CRUDQueryBuilder(EmailSmtpConfig)
template_crud = CRUDQueryBuilder(EmailTemplate)


def idempotency_key(*parts: Any) -> str:
    """
    A stable key from whatever identifies "this send, once".

    Hashed rather than concatenated because the parts are arbitrary — an event name, a run
    uuid, a node id, a recipient address — and a 255-character column cannot hold them
    joined. sha256 of the joined parts is fixed-width, collision-free for this purpose, and
    reproducible across processes, which is what makes it work as a unique constraint.

    Keyed on the *occasion*, never on the clock. Two workers handling the same webhook
    retry must produce the same key, and they will not agree on a timestamp.
    """
    joined = "\x1f".join("" if part is None else str(part) for part in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _as_uuid(value: Any, *, label: str) -> uuid_pkg.UUID:
    """
    A public identifier as a real ``UUID``, or a 404.

    **Parsing here rather than at each call site is load-bearing.** A route gets a parsed
    ``uuid.UUID`` from Litestar's ``:uuid`` path type, but a *node* holds its template's id
    as a **string** inside the graph's JSONB — client-minted, never a foreign key — and a
    trigger holds one the same way. Handing that string to ``get_by_uuid`` reaches the
    ``PGUUID → CHAR(36)`` shim in the test harness, which calls ``.hex`` on it and raises a
    ``StatementError`` about ``'str' object has no attribute 'hex'``: a database error where
    a "not found" belonged, and one that says nothing about the actual problem.

    A malformed id answers the same 404 as a missing one, deliberately. To the caller both
    mean "that is not a thing you can send with", and a different answer for the two would
    let somebody probe which ids are well-formed.
    """
    if isinstance(value, uuid_pkg.UUID):
        return value
    try:
        return uuid_pkg.UUID(str(value or "").strip())
    except (TypeError, ValueError):
        raise HTTPException(status_code=404, detail=f"{label} not found") from None


async def resolve_template(
    db: AsyncSession, user_id: int, template_uuid: Any
) -> EmailTemplate:
    """The user's own template, or a 404 with a sentence."""
    template = await template_crud.get_by_uuid(
        db,
        _as_uuid(template_uuid, label="Email template"),
        extra_filters={"user_id": user_id},
    )
    if template is None:
        raise HTTPException(status_code=404, detail="Email template not found")
    return template


async def resolve_config(
    db: AsyncSession, user_id: int, config_uuid: Any
) -> EmailSmtpConfig:
    """The user's own SMTP config, or a 404 with a sentence."""
    config = await config_crud.get_by_uuid(
        db,
        _as_uuid(config_uuid, label="SMTP server"),
        extra_filters={"user_id": user_id},
    )
    if config is None:
        raise HTTPException(status_code=404, detail="SMTP server not found")
    return config


async def enqueue_email(
    db: AsyncSession,
    *,
    user_id: int,
    template: EmailTemplate,
    config: EmailSmtpConfig,
    recipients: Mapping[str, Any],
    values: Mapping[str, str],
    source: str = SOURCE_MANUAL,
    source_ref: str = "",
    trigger_id: Optional[int] = None,
    trigger_kind: str = "",
    workspace_id: Optional[int] = None,
    priority: int = 0,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    idempotency: Optional[str] = None,
) -> EmailMessage:
    """
    Render one email and put it on the queue. Does **not** commit.

    Raises :class:`RenderError` if the template and the values do not make a whole email,
    and :class:`DispatchError` if the configuration cannot send one. Neither leaves a row
    behind — see the module docstring on why a half-rendered message must not exist.

    ``values`` are the resolved variable values, already flattened to strings by
    ``variable_sources.resolve_bindings``. This function does not know where they came from,
    which is what lets the same call serve a graph node, a chat session and a webhook.
    """
    if source not in MESSAGE_SOURCE_VALUES:
        # A programming error rather than an operator one, so it does not get a friendly
        # sentence — but it is still refused rather than stored, because `source` is what
        # the log filters by and an unknown value makes a message invisible.
        raise ValueError(f"Unknown email source '{source}'")

    if not template.is_active:
        raise DispatchError(
            f"The template '{template.name}' is switched off, so nothing was sent. "
            "Switch it back on to use it."
        )
    if not config.is_active:
        raise DispatchError(
            f"The SMTP server '{config.name}' is switched off, so nothing was sent. "
            "Switch it back on to use it."
        )

    declared = list(template.variables or [])

    # Recipients first. A missing address is the most common mistake and it is cheaper to
    # report before rendering three bodies — and its error message names the recipient
    # field, which is where the fix is.
    resolved_recipients = rendering.render_recipients(recipients, dict(values))

    subject, body_html, body_text = rendering.render_message(
        subject_template=template.subject_template,
        body_html_template=template.body_html_template,
        body_text_template=template.body_text_template,
        variables=declared,
        values=dict(values),
    )

    message = await queue.enqueue(
        db,
        {
            "user_id": user_id,
            # Falls back to the template's workspace, so a message inherits the sharing of
            # the thing it was written from unless a caller says otherwise.
            "workspace_id": workspace_id
            if workspace_id is not None
            else template.workspace_id,
            "trigger_id": trigger_id,
            "template_id": template.id,
            "smtp_config_id": config.id,
            "source": source,
            "source_ref": (source_ref or None) and str(source_ref)[:255],
            # Denormalised now so the log stays readable after any of the three is deleted.
            "template_name": template.name,
            "trigger_kind": trigger_kind or None,
            "smtp_host": config.host,
            "from_email": config.from_email,
            "from_name": config.from_name,
            "reply_to": config.reply_to,
            "to_addresses": resolved_recipients["to"],
            "cc_addresses": resolved_recipients["cc"],
            "bcc_addresses": resolved_recipients["bcc"],
            "subject": subject,
            "body_html": body_html,
            "body_text": body_text,
            "status": MESSAGE_QUEUED,
            "priority": priority,
            "max_attempts": max(1, int(max_attempts)),
            "idempotency_key": idempotency or None,
        },
    )

    logger.info(
        "Queued email to %d recipient(s) via '%s' (source=%s, template='%s')",
        len(resolved_recipients["to"])
        + len(resolved_recipients["cc"])
        + len(resolved_recipients["bcc"]),
        config.name,
        source,
        template.name,
    )
    return message


async def retry_message(
    db: AsyncSession, user_id: int, message_uuid: Any
) -> EmailMessage:
    """
    Put a failed message back on the queue, unchanged.

    **Re-sends the stored text rather than re-rendering.** The template may have been
    edited since, and an operator pressing Retry is asking for *this* email to go, not for
    a new one built from whatever the template says now. That the bytes are identical is the
    property that makes Retry safe to reason about.

    Only a terminal message can be retried. A ``queued`` one is already going and a
    ``sending`` one is in flight; offering Retry on either would produce two sends of one
    message, which is the thing the whole claim mechanism exists to prevent.

    ``idempotency_key`` is cleared. It has already done its job — it stopped a *duplicate
    occasion* from producing a second message — and leaving it set would make a second
    manual retry collide with the first on the unique index. The operator's explicit press
    is a new occasion by definition.

    Does not commit; the route does, then calls ``queue.wake()``.
    """
    message = await message_crud.get_by_uuid(
        db, message_uuid, extra_filters={"user_id": user_id}
    )
    if message is None:
        raise HTTPException(status_code=404, detail="Email not found")

    if message.status not in TERMINAL_MESSAGE_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=(
                "This email is already on its way, so there is nothing to retry. "
                "Wait for it to finish."
            ),
        )

    message.status = MESSAGE_QUEUED
    message.attempt = 0
    message.error_message = None
    message.smtp_response = None
    message.sent_at = None
    message.claimed_by = None
    message.claimed_at = None
    message.heartbeat_at = None
    message.idempotency_key = None
    # Due immediately: the operator is standing there.
    message.next_attempt_at = _now()

    return message


async def cancel_message(
    db: AsyncSession, user_id: int, message_uuid: Any
) -> EmailMessage:
    """
    Stop a queued message from being sent.

    Only a ``queued`` message can be cancelled. A ``sending`` one cannot: the SMTP
    conversation may be past ``DATA``, and marking it cancelled would claim we stopped
    something that has already arrived. That is a lie the log must not tell, so the refusal
    says what is actually happening instead.
    """
    message = await message_crud.get_by_uuid(
        db, message_uuid, extra_filters={"user_id": user_id}
    )
    if message is None:
        raise HTTPException(status_code=404, detail="Email not found")

    if message.status == MESSAGE_SENDING:
        raise HTTPException(
            status_code=400,
            detail=(
                "This email is being sent right now and can no longer be stopped. "
                "It may already have arrived."
            ),
        )
    if message.status != MESSAGE_QUEUED:
        raise HTTPException(
            status_code=400,
            detail="This email is not waiting to be sent, so there is nothing to cancel.",
        )

    message.status = MESSAGE_CANCELLED
    message.error_message = "Cancelled before it was sent."
    return message


def _now() -> datetime:
    """Wrapped so a test can freeze it without patching ``datetime`` globally — the clock
    seam ``scheduler.now()`` exposes, for the same reason."""
    return datetime.now(timezone.utc)
