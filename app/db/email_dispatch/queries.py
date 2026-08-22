"""
Email dispatch queries that ``CRUDQueryBuilder`` cannot express.

Same reason ``app/db/integrations/queries.py`` and ``app/db/downloader_agents/queries.py``
exist: a feature's own concurrency rules and joins belong with the feature, not in a
model-agnostic module.

**Every count here is a real ``select(func.count())``.** ``CRUDQueryBuilder.count()``
materialises the rows and takes their length, which is fine for a page of SMTP configs and
is not fine for a delivery log with six months of history in it. The distinction is
invisible until the day it is not.

**The claim query is the reason this module exists.** ``claim_next_email`` is a
``FOR UPDATE SKIP LOCKED`` claim with a correlated ``NOT EXISTS`` that serialises per SMTP
server, and it is the subtlest statement in the module. Read its docstring before changing
anything in it — the failure mode of getting the correlation wrong is silent and severe,
and the integrations queue has already been bitten by exactly that.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models.email_dispatch import (
    MESSAGE_FAILED,
    MESSAGE_QUEUED,
    MESSAGE_SENDING,
    EmailMessage,
    EmailTrigger,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# The queue
# ---------------------------------------------------------------------------


async def claim_next_email(
    db: AsyncSession, claimed_by: str
) -> Optional[EmailMessage]:
    """
    Take the next sendable message, or ``None``.

    ``FOR UPDATE SKIP LOCKED`` makes two workers running this identical statement at the
    same moment get two *different* messages, or one gets a message and the other gets
    nothing. Neither waits and neither can take the other's. The status flips to
    ``sending`` inside the same transaction, so by the time the lock is released the row no
    longer matches ``queued``.

    **The correlated ``NOT EXISTS`` serialises sending per SMTP server, and that is what
    actually protects the provider.** Without it, ``EMAIL_WORKER_CONCURRENCY`` connections
    open against one server at once; providers respond to that by throttling, greylisting
    or classifying the sender as a spam source, and the last of those is not something a
    retry fixes. Per-server serialisation is the control, and it is *in the claim* rather
    than in the worker because a worker-side check is a check-then-act with two workers in
    the window — which is precisely the case it exists to handle.

    **The correlation is explicit.** ``.correlate(EmailMessage)`` names the one thing the
    subquery may take from the outer query. Two levels of nesting is where SQLAlchemy's
    auto-correlation picks the wrong enclosing SELECT, and the integrations queue was
    bitten by exactly this: the condition silently stopped meaning "this server is busy"
    and started meaning "any server is busy", so one slow provider blocked every email in
    the system. The alias pair below is what keeps the two references distinguishable.

    A message with no ``smtp_config_id`` — its config was deleted after the message was
    queued — is not blocked by anything and not blocking anything. It is claimed normally
    and will fail with a clear reason at send time, which is better than a row that can
    never be claimed and never be explained.

    Ordered by priority then how long it has been due: a test somebody is sitting and
    watching should not wait behind a thousand-row overnight digest, and within one
    priority the longest-due goes first so a queue under load stays fair.

    **On SQLite this compiles to a plain SELECT.** SQLAlchemy drops the locking clause for a
    dialect that has none, which is what lets the test suite exercise this same code path —
    worth knowing rather than discovering, because a test can prove the claim works and
    cannot prove the locking does.

    Commits. A claim that is not committed is not a claim: the lock would be released when
    the session closed and the message would look available again while a worker was on it.
    """
    sending = aliased(EmailMessage)

    busy_server = (
        select(sending.id)
        .where(
            sending.status == MESSAGE_SENDING,
            sending.smtp_config_id == EmailMessage.smtp_config_id,
        )
        # Explicit, not inferred. The one thing this subquery may take from the outer query
        # is the candidate's SMTP config — see the docstring.
        .correlate(EmailMessage)
        .exists()
    )

    statement = (
        select(EmailMessage)
        .where(
            EmailMessage.status == MESSAGE_QUEUED,
            EmailMessage.next_attempt_at <= func.now(),
            # A message whose config is gone cannot contend with anything, so it is not
            # subject to the per-server rule. `is_not(None)` guards the NULL = NULL case,
            # which in SQL is unknown rather than true and would otherwise let every
            # config-less message block every other one.
            (EmailMessage.smtp_config_id.is_(None)) | (~busy_server),
        )
        .order_by(
            EmailMessage.priority.desc(),
            EmailMessage.next_attempt_at,
            EmailMessage.id,
        )
        .limit(1)
        .with_for_update(skip_locked=True, of=EmailMessage)
    )

    message = (await db.execute(statement)).scalars().first()
    if message is None:
        return None

    moment = datetime.now(timezone.utc)
    message.status = MESSAGE_SENDING
    message.attempt = (message.attempt or 0) + 1
    message.claimed_by = claimed_by[:255]
    message.claimed_at = moment
    message.heartbeat_at = moment

    await db.commit()
    await db.refresh(message)
    return message


async def email_heartbeat(db: AsyncSession, message_id: int) -> None:
    """
    Say the worker is still on this message.

    A bare ``UPDATE`` rather than loading the row: it is called on a timer for the whole
    life of a send and has nothing to read. Committed immediately, because a heartbeat held
    in an open transaction is one nobody else can see — which is the only thing it is for.
    """
    await db.execute(
        update(EmailMessage)
        .where(EmailMessage.id == message_id)
        .values(heartbeat_at=datetime.now(timezone.utc))
    )
    await db.commit()


async def finish_email(
    db: AsyncSession,
    message_id: int,
    *,
    status: str,
    error_message: str = "",
    smtp_response: str = "",
    sent_at: Optional[datetime] = None,
) -> None:
    """
    Close a message off in a terminal state.

    Clears ``claimed_by`` / ``claimed_at`` / ``heartbeat_at``: a finished message that
    still names a worker reads, to anybody scanning the table, like a send in progress —
    and ``requeue_stale_emails`` would eventually agree with them.
    """
    await db.execute(
        update(EmailMessage)
        .where(EmailMessage.id == message_id)
        .values(
            status=status,
            error_message=error_message or None,
            smtp_response=(smtp_response or None) and smtp_response[:2000],
            sent_at=sent_at,
            claimed_by=None,
            claimed_at=None,
            heartbeat_at=None,
        )
    )
    await db.commit()


async def requeue_email(
    db: AsyncSession,
    message_id: int,
    *,
    next_attempt_at: datetime,
    error_message: str = "",
    smtp_response: str = "",
) -> None:
    """
    Put a retryable failure back on the queue, due later.

    The attempt counter is **not** reset — it was incremented by the claim and is what
    ``should_retry`` bounds against. Resetting it here is how a queue retries forever.

    ``error_message`` is kept rather than cleared while the message waits, so the log page
    can say *why* something is due again in eight minutes instead of showing a queued row
    that looks like it has never been tried.
    """
    await db.execute(
        update(EmailMessage)
        .where(EmailMessage.id == message_id)
        .values(
            status=MESSAGE_QUEUED,
            next_attempt_at=next_attempt_at,
            error_message=error_message or None,
            smtp_response=(smtp_response or None) and smtp_response[:2000],
            claimed_by=None,
            claimed_at=None,
            heartbeat_at=None,
        )
    )
    await db.commit()


async def requeue_stale_emails(
    db: AsyncSession, stale_after_seconds: float
) -> List[int]:
    """
    Fail the messages whose worker stopped reporting, and return their ids.

    **Fail, not resume**, and this is the most consequential decision in the module. The
    dead worker may already have completed the SMTP conversation — the message could be in
    the recipient's mailbox right now — and there is no way from here to find out. Sending
    again would deliver it twice.

    ``requeue_stale_jobs`` in the downloader restarts an export from its last confirmation,
    which is safe because nothing outside this application has seen a part file. Email is
    the opposite: the side effect is *entirely* outside, it is irreversible, and it is
    visible to a customer. So the operator gets a message that says delivery is unknown and
    a Retry button, and pressing it is their decision with the risk stated. The same call
    ``requeue_stale_run_jobs`` makes about a half-written CRM.

    ``attempt`` is not reset, so a message that keeps killing its worker is visible as such
    rather than looping silently forever.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=stale_after_seconds)

    result = await db.execute(
        select(EmailMessage).where(
            EmailMessage.status == MESSAGE_SENDING,
            EmailMessage.heartbeat_at.is_not(None),
            EmailMessage.heartbeat_at < cutoff,
        )
    )
    stale = list(result.scalars().all())
    if not stale:
        return []

    message_text = (
        "The worker sending this email stopped responding, so whether it was "
        "delivered is unknown. Check with the recipient before pressing Retry — "
        "retrying may send it twice."
    )

    moment = datetime.now(timezone.utc)
    for row in stale:
        row.status = MESSAGE_FAILED
        row.error_message = message_text
        row.claimed_by = None
        row.claimed_at = None
        row.heartbeat_at = None
        row.sent_at = None
        row.updated_at = moment

    await db.commit()

    ids = [row.id for row in stale]
    logger.warning("Failed %d stale email message(s): %s", len(ids), ids)
    return ids


async def queued_email_count(db: AsyncSession) -> int:
    """How many messages are waiting to be sent. A real count."""
    result = await db.execute(
        select(func.count())
        .select_from(EmailMessage)
        .where(EmailMessage.status == MESSAGE_QUEUED)
    )
    return int(result.scalar_one())


# ---------------------------------------------------------------------------
# The log page
# ---------------------------------------------------------------------------


async def message_counts_by_status(
    db: AsyncSession, user_id: int
) -> Dict[str, int]:
    """
    One user's message totals per status, for the filter chips above the log.

    Grouped in the database rather than by loading the rows: the whole point of the chips
    is to be cheap enough to render on every page load of a table that only grows.

    Statuses with no rows are absent from the result rather than present as zero — the
    caller merges against ``MESSAGE_STATUSES`` so the chip order comes from the vocabulary
    and not from whatever the database happened to return.
    """
    result = await db.execute(
        select(EmailMessage.status, func.count())
        .where(EmailMessage.user_id == user_id)
        .group_by(EmailMessage.status)
    )
    return {row[0]: int(row[1]) for row in result.all()}


# ---------------------------------------------------------------------------
# Triggers
# ---------------------------------------------------------------------------


async def enabled_triggers_for_event(
    db: AsyncSession, event_name: str, *, user_id: Optional[int] = None
) -> List[EmailTrigger]:
    """
    Every enabled trigger listening for one event.

    Read on **every** published event, which is why ``ix_email_triggers_event_lookup``
    exists — without it the bus table-scans ``email_triggers`` once per event, and the
    scan gets slower as an installation adds triggers it is not firing.

    ``user_id`` narrows to one owner. Events are published with the user they concern, and
    a trigger only ever fires for its own owner's events: without that filter, one tenant
    creating a trigger on ``datasource.status_changed`` would be emailed about every other
    tenant's datasources.

    The template and SMTP config come back eagerly loaded (``lazy="selectin"`` on the
    relationships), because the caller needs both to render and is running outside a
    request where a lazy load would raise on a closed session.
    """
    filters = [
        EmailTrigger.event_name == event_name,
        EmailTrigger.is_enabled.is_(True),
    ]
    if user_id is not None:
        filters.append(EmailTrigger.user_id == user_id)

    result = await db.execute(
        select(EmailTrigger).where(*filters).order_by(EmailTrigger.id)
    )
    return list(result.scalars().all())


async def trigger_by_endpoint(
    db: AsyncSession, endpoint_id
) -> Optional[EmailTrigger]:
    """
    The trigger behind a public webhook URL, enabled or not.

    Deliberately returns a disabled trigger rather than filtering it out, so the route can
    answer 404 for "no such endpoint" and 404 for "disabled" through the same path without
    the two branches drifting apart. What must never happen is a *different* status code
    for the two: that difference tells an unauthenticated caller which endpoint ids are
    real.
    """
    result = await db.execute(
        select(EmailTrigger).where(EmailTrigger.webhook_endpoint_id == endpoint_id)
    )
    return result.scalars().first()


async def mark_trigger_fired(
    db: AsyncSession, trigger_id: int, *, moment: Optional[datetime] = None
) -> None:
    """
    Stamp ``last_fired_at``, which is what the throttle reads.

    A bare ``UPDATE`` and its own commit: the caller is about to enqueue in a different
    session, and a throttle stamp that is still inside an uncommitted transaction does not
    throttle the request arriving one millisecond later.
    """
    await db.execute(
        update(EmailTrigger)
        .where(EmailTrigger.id == trigger_id)
        .values(last_fired_at=moment or datetime.now(timezone.utc))
    )
    await db.commit()


async def templates_in_use(
    db: AsyncSession, template_ids: Sequence[int]
) -> Dict[int, int]:
    """
    How many enabled triggers point at each of these templates.

    Read before a template is deleted. ``email_triggers.template_id`` is ``ON DELETE
    RESTRICT``, so the database would refuse the delete anyway — but it would refuse it
    with an ``IntegrityError``, and "this template is used by 2 triggers, disable them
    first" is a sentence an operator can act on. Asking first is what turns a stack trace
    into an answer.
    """
    if not template_ids:
        return {}

    result = await db.execute(
        select(EmailTrigger.template_id, func.count())
        .where(EmailTrigger.template_id.in_(list(template_ids)))
        .group_by(EmailTrigger.template_id)
    )
    return {int(row[0]): int(row[1]) for row in result.all()}


async def smtp_configs_in_use(
    db: AsyncSession, config_ids: Sequence[int]
) -> Dict[int, int]:
    """How many triggers point at each of these SMTP configs. See ``templates_in_use``."""
    if not config_ids:
        return {}

    result = await db.execute(
        select(EmailTrigger.smtp_config_id, func.count())
        .where(EmailTrigger.smtp_config_id.in_(list(config_ids)))
        .group_by(EmailTrigger.smtp_config_id)
    )
    return {int(row[0]): int(row[1]) for row in result.all()}
