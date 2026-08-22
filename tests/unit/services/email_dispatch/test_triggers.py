"""
The event bus, and the subscriber that turns an event into a queued email.

Two properties are asserted harder than the rest, because both are the kind of thing that
looks fine until it is load-bearing:

*publishing can never break the publisher* — a broken template must not fail the code that
was merely reporting that a sync finished. A handler that raises is logged and the publish
still returns.

*events do not cross tenants* — a trigger fires only for its own owner's events. Getting
this wrong emails one customer about another customer's datasources, which is a data leak
rather than a bug.
"""

from __future__ import annotations

import pytest

from app.models.email_dispatch import (
    MESSAGE_QUEUED,
    SOURCE_EVENT,
    TRIGGER_EVENT,
    EmailMessage,
    EmailTrigger,
)
from app.services.email_dispatch import triggers as email_triggers
from app.utils import events
from sqlalchemy import select


EVENT = events.EVENT_INTEGRATION_RUN_SETTLED


@pytest.fixture(autouse=True)
def restore_bus():
    """
    Put the bus back the way it was.

    The registry is process-local, so a handler registered by one test firing during another
    is invisible in either of them. Snapshot and restore rather than
    ``clear_subscribers``, because the real subscription is made at import and clearing it
    would leave every later test in the session with a bus that does nothing.
    """
    saved = {name: list(handlers) for name, handlers in events._handlers.items()}
    yield
    events._handlers.clear()
    events._handlers.update(saved)


async def make_trigger(db, user, template, smtp_config, **overrides) -> EmailTrigger:
    values = {
        "user_id": user.id,
        "name": "Tell ops",
        "kind": TRIGGER_EVENT,
        "event_name": EVENT,
        "template_id": template.id,
        "smtp_config_id": smtp_config.id,
        "recipients": {"to": ["ops@example.com"]},
        "variable_bindings": {
            "WORKFLOW": {"source": "event", "path": "flow_name"},
        },
        "is_enabled": True,
    }
    values.update(overrides)
    trigger = EmailTrigger(**values)
    db.add(trigger)
    await db.commit()
    await db.refresh(trigger)
    return trigger


async def queued(db) -> list:
    return list(
        (
            await db.execute(select(EmailMessage).order_by(EmailMessage.id))
        ).scalars().all()
    )


class TestTheBus:
    async def test_a_handler_that_raises_does_not_break_the_publisher(self):
        """The property the whole module depends on. See the module docstring."""
        calls = []

        async def exploding(event):
            calls.append("exploding")
            raise RuntimeError("a broken template")

        async def working(event):
            calls.append("working")

        events.subscribe(EVENT, exploding)
        events.subscribe(EVENT, working)

        # Must not raise, and must still run the handler registered after the broken one.
        ran = await events.publish(EVENT, {"flow_name": "Nightly"}, user_id=1)

        assert "exploding" in calls
        assert "working" in calls, "one bad handler must not stop the next"
        assert ran >= 1

    async def test_an_unknown_event_is_ignored_rather_than_raised(self):
        """A publisher is usually somewhere important. Taking it down over a mistyped
        constant would trade a missing email for a broken feature."""
        assert await events.publish("nothing.happened", {}, user_id=1) == 0

    async def test_subscribing_to_an_unknown_event_is_refused(self):
        """The strict half. A silent no-op subscription is the worst outcome available —
        everything looks wired and nothing ever arrives."""
        with pytest.raises(ValueError, match="not a known event"):
            events.subscribe("nothing.happened", lambda event: None)

    async def test_the_email_handler_is_subscribed_to_every_event(self):
        """Importing app.services.email_dispatch.triggers is what wires email to events, so
        a missing import is a feature that stores triggers correctly and never fires."""
        for name, _label in events.EVENT_NAMES:
            assert events.subscriber_count(name) >= 1, name


class TestFiring:
    async def test_an_event_queues_one_email_with_the_payload_bound_in(
        self, db, user, template, smtp_config
    ):  # noqa: ANN001
        await make_trigger(db, user, template, smtp_config)

        await email_triggers.handle_event(
            events.Event(EVENT, {"flow_name": "Nightly sync"}, user_id=user.id)
        )

        messages = await queued(db)
        assert len(messages) == 1
        message = messages[0]
        assert message.status == MESSAGE_QUEUED
        assert message.source == SOURCE_EVENT
        assert message.source_ref == EVENT
        # Rendered at enqueue, from the payload, through the binding.
        assert message.subject == "Nightly sync failed"
        # The declared default filled the variable nothing was bound to.
        assert "normal" in message.body_html
        # Denormalised so the log survives the template being deleted.
        assert message.template_name == template.name
        assert message.smtp_host == smtp_config.host

    async def test_a_disabled_trigger_does_nothing(
        self, db, user, template, smtp_config
    ):  # noqa: ANN001
        await make_trigger(db, user, template, smtp_config, is_enabled=False)

        await email_triggers.handle_event(
            events.Event(EVENT, {"flow_name": "Nightly"}, user_id=user.id)
        )

        assert await queued(db) == []

    async def test_a_trigger_for_a_different_event_does_not_fire(
        self, db, user, template, smtp_config
    ):  # noqa: ANN001
        await make_trigger(
            db, user, template, smtp_config, event_name=events.EVENT_GRAPH_RUN_SETTLED
        )

        await email_triggers.handle_event(
            events.Event(EVENT, {"flow_name": "Nightly"}, user_id=user.id)
        )

        assert await queued(db) == []

    async def test_a_trigger_never_fires_for_another_users_event(
        self, db, user, make_user, template, smtp_config
    ):  # noqa: ANN001
        """The tenancy boundary. Getting this wrong emails one customer about another
        customer's data."""
        await make_trigger(db, user, template, smtp_config)
        stranger = await make_user(email="someone@else.com")

        await email_triggers.handle_event(
            events.Event(EVENT, {"flow_name": "Theirs"}, user_id=stranger.id)
        )

        assert await queued(db) == [], (
            "a trigger must only fire for events belonging to its own owner"
        )

    async def test_one_broken_trigger_does_not_stop_a_working_one(
        self, db, user, template, smtp_config
    ):  # noqa: ANN001
        """A trigger bound to a field the payload does not carry, alongside a good one."""
        await make_trigger(
            db,
            user,
            template,
            smtp_config,
            name="Broken",
            # WORKFLOW is required by the fixture template, and this path resolves to
            # nothing, so rendering refuses.
            variable_bindings={"WORKFLOW": {"source": "event", "path": "absent.field"}},
        )
        await make_trigger(db, user, template, smtp_config, name="Working")

        await email_triggers.handle_event(
            events.Event(EVENT, {"flow_name": "Nightly"}, user_id=user.id)
        )

        messages = await queued(db)
        assert len(messages) == 1, "the working trigger must still have sent"

    async def test_the_same_occurrence_only_queues_once(
        self, db, user, template, smtp_config
    ):  # noqa: ANN001
        """Two deliveries of one event — two processes, or a redelivery — produce one
        email, because the idempotency key is keyed on the occasion."""
        await make_trigger(db, user, template, smtp_config)
        payload = {"flow_name": "Nightly", "run_uuid": "abc-123"}

        await email_triggers.handle_event(
            events.Event(EVENT, payload, user_id=user.id)
        )
        await email_triggers.handle_event(
            events.Event(EVENT, payload, user_id=user.id)
        )

        assert len(await queued(db)) == 1

    async def test_two_different_occurrences_both_queue(
        self, db, user, template, smtp_config
    ):  # noqa: ANN001
        await make_trigger(db, user, template, smtp_config)

        for run in ("run-1", "run-2"):
            await email_triggers.handle_event(
                events.Event(
                    EVENT, {"flow_name": "Nightly", "run_uuid": run}, user_id=user.id
                )
            )

        assert len(await queued(db)) == 2

    async def test_a_payload_with_no_identity_is_not_deduplicated(
        self, db, user, template, smtp_config
    ):  # noqa: ANN001
        """No occasion in the payload means no key, because a weak key would make the
        second genuine firing of a recurring event collide with the first and vanish.
        Silently dropping real email is worse than an occasional duplicate."""
        await make_trigger(db, user, template, smtp_config)

        for _ in range(2):
            await email_triggers.handle_event(
                events.Event(EVENT, {"flow_name": "Nightly"}, user_id=user.id)
            )

        assert len(await queued(db)) == 2

    async def test_firing_stamps_last_fired_at(
        self, db, user, template, smtp_config
    ):  # noqa: ANN001
        trigger = await make_trigger(db, user, template, smtp_config)
        assert trigger.last_fired_at is None

        await email_triggers.handle_event(
            events.Event(EVENT, {"flow_name": "Nightly"}, user_id=user.id)
        )

        await db.refresh(trigger)
        assert trigger.last_fired_at is not None
