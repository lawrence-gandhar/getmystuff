"""
The bridge from the event bus to the queue: one handler, subscribed to every event.

**Importing this module is what wires email to events.** It calls ``events.subscribe`` at
the bottom, so ``main.py`` importing anything under ``app.services.email_dispatch`` is
enough to make triggers live — the same import-for-the-side-effect pattern
``connectors/registry.py`` uses for connectors and ``app/db/models.py`` for models. There is
no separate registration step to forget.

**One handler for all events, not one per event.** The handler's work is identical whatever
fired it — look up the enabled triggers for this event and this owner, render, enqueue — so
a per-event handler would be the same function registered four times with a constant baked
in. What differs between events is the *payload*, and that is data.

**The handler opens its own session and never raises.** It runs after the publisher has
committed, so it must not touch the publisher's session; and a failure here — a template
somebody broke, a server that has been deleted — must not propagate into the code that was
merely reporting that a sync finished. Every failure is logged and the loop continues to the
next trigger, because one broken trigger must not stop a working one from firing.

**Each firing gets an idempotency key built from the occasion.** The event name, the trigger,
and whatever the payload offers as its own identity — a run uuid, a datasource uuid. Two
processes handling the same publish, or a webhook a caller retried, produce one email
because the unique index refuses the second. The key never contains a timestamp: two
publishers racing would not agree on one.
"""

import logging
from typing import Any, Mapping, Optional

from sqlalchemy.exc import IntegrityError

from app.db.db_utils import CRUDQueryBuilder
from app.db.email_dispatch.queries import enabled_triggers_for_event, mark_trigger_fired
from app.models.email_dispatch import SOURCE_EVENT, EmailTrigger
from app.services.email_dispatch import dispatch_service, message_store, queue
from app.services.email_dispatch.errors import EmailFailure
from app.services.email_dispatch.variable_sources import (
    VariableContext,
    resolve_bindings,
)
from app.utils import events

logger = logging.getLogger(__name__)

trigger_crud = CRUDQueryBuilder(EmailTrigger)

#: Payload keys tried, in order, as the "which occurrence was this" half of the idempotency
#: key. A run uuid is ideal; a datasource uuid plus its new status is good enough for a
#: status change. Falling through all of them leaves the key without an occasion, and
#: `_idempotency_for` then returns None — see there.
_OCCASION_KEYS = ("run_uuid", "run_id", "job_uuid", "datasource_uuid", "event_id")


def _idempotency_for(
    event: events.Event, trigger: EmailTrigger
) -> Optional[str]:
    """
    A key identifying "this trigger, for this occurrence", or ``None``.

    ``None`` means *do not deduplicate*, and that is the right answer when the payload
    carries nothing that identifies the occasion: a key of just the event name and the
    trigger would make the second genuine firing of a recurring event collide with the
    first and vanish. Silently dropping real emails is worse than occasionally sending a
    duplicate, so the absence of an identity means no deduplication rather than a weak one.
    """
    for key in _OCCASION_KEYS:
        value = event.payload.get(key)
        if value:
            return dispatch_service.idempotency_key(
                "event", event.name, str(trigger.uuid), value,
                # The status rides along so a datasource going offline and coming back are
                # two occasions rather than one repeated.
                event.payload.get("status", ""),
            )
    return None


async def _fire(db, event: events.Event, trigger: EmailTrigger) -> bool:
    """
    Render and enqueue one trigger's email. ``True`` if a message was queued.

    ``False`` rather than an exception for a duplicate: an idempotency collision means the
    email already exists, which is success from the caller's point of view and is not worth
    a log line at warning level.
    """
    template = trigger.template
    config = trigger.smtp_config
    if template is None or config is None:
        # ON DELETE RESTRICT should make this impossible, so if it happens the constraint
        # was bypassed and the operator needs to know rather than have it pass quietly.
        logger.error(
            "Trigger %s fired but its template or SMTP server is missing.", trigger.uuid
        )
        return False

    values = resolve_bindings(
        trigger.variable_bindings,
        # A trigger has the payload and literals and nothing else — no chat session, no
        # upstream node, no record. Passing None for those is what makes
        # `VariableContext.available()` refuse a binding to them by name.
        VariableContext(
            event_payload=event.payload,
            agent_variables=None,  # type: ignore[arg-type]
            session_variables=None,  # type: ignore[arg-type]
            node_outputs=None,  # type: ignore[arg-type]
        ),
    )

    await dispatch_service.enqueue_email(
        db,
        user_id=trigger.user_id,
        template=template,
        config=config,
        recipients=trigger.recipients,
        values=values,
        source=SOURCE_EVENT,
        source_ref=event.name,
        trigger_id=trigger.id,
        trigger_kind=trigger.kind,
        workspace_id=trigger.workspace_id,
        idempotency=_idempotency_for(event, trigger),
    )
    await db.commit()
    return True


async def handle_event(event: events.Event) -> None:
    """
    Fire every enabled trigger listening for this event.

    Never raises — see the module docstring.

    **Each trigger gets its own session**, and that is load-bearing rather than tidy. Two
    things go wrong when they share one:

    *a rollback expires every object in the session.* The failing trigger's rollback would
    leave the *other* triggers' rows expired, so the next iteration's ``trigger.template``
    becomes a lazy refresh — which is IO from a synchronous attribute access, and SQLAlchemy
    raises ``MissingGreenlet`` rather than doing it. The loop that was supposed to carry on
    after one failure dies on the next trigger instead.

    *one trigger's failure would roll back another's message.* The claim that a broken
    trigger cannot stop a working one is only true if the working one's INSERT is not in the
    transaction being rolled back.

    So the ids are read once, and each is then handled and committed independently. The cost
    is one short session per trigger, against a handful of triggers per event.
    """
    async with message_store.open_session() as db:
        triggers = await enabled_triggers_for_event(
            db, event.name, user_id=event.user_id
        )
        # Only the ids cross the session boundary. Carrying the ORM objects out would make
        # every attribute read afterwards a lazy load against a closed session.
        pending = [(trigger.id, trigger.uuid, trigger.name) for trigger in triggers]

    if not pending:
        return

    queued = 0
    for trigger_id, trigger_uuid, trigger_name in pending:
        try:
            async with message_store.open_session() as db:
                trigger = await trigger_crud.get_one(db, {"id": trigger_id})
                if trigger is None:
                    # Deleted between the two reads. Nothing to do and nothing wrong.
                    continue
                if await _fire(db, event, trigger):
                    await mark_trigger_fired(db, trigger.id)
                    queued += 1
        except IntegrityError:
            # The idempotency key already exists: another process, or a redelivery, has
            # queued this exact email. The constraint doing its job, not a fault — and the
            # session is discarded with the `async with`, so there is nothing to roll back
            # by hand.
            logger.info(
                "Trigger %s already has an email for this occurrence of %s.",
                trigger_uuid,
                event.name,
            )
        except EmailFailure as exc:
            # A broken template or binding on *this* trigger. Named so an operator can find
            # it, and the next trigger still gets its go.
            logger.warning(
                "Trigger %s ('%s') could not send on %s: %s",
                trigger_uuid,
                trigger_name,
                event.name,
                exc.message,
            )
        except Exception:  # noqa: BLE001 — one trigger must not stop the others
            logger.exception(
                "Trigger %s failed unexpectedly on %s", trigger_uuid, event.name
            )

    if queued:
        # After every session has closed, so a woken worker cannot look before the commits
        # have landed.
        queue.wake()


# Subscribed to every event in the catalogue. A trigger's `event_name` column is what
# actually narrows it; subscribing to all of them means adding an event to `EVENT_NAMES` is
# the only step needed to make it available to a trigger, with no second list here to keep
# in step.
for _name, _label in events.EVENT_NAMES:
    events.subscribe(_name, handle_event)
