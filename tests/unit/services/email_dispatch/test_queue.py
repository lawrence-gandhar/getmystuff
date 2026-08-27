"""
The send queue: claiming, per-server serialisation, retry backoff and the stale reaper.

Two behaviours are asserted harder than the rest because getting either wrong is invisible
until it matters:

*per-SMTP-config serialisation* — ``claim_next_email``'s correlated ``NOT EXISTS``. The
failure mode of a wrong correlation is not an error, it is the condition silently changing
meaning from "this server is busy" to "any server is busy", which serialises the entire
queue behind one slow provider. The integrations queue has already been bitten by exactly
this, so it is pinned from both sides: two configs must run in parallel, one config must
not.

*a dead worker fails rather than resumes* — the most consequential decision in the module.
A resumed send may deliver a second copy of an email that already arrived, so the reaper
must leave the message ``failed`` with delivery declared unknown, and must never put it
back on the queue by itself.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


from app.db.email_dispatch.queries import (
    claim_next_email,
    queued_email_count,
    requeue_stale_emails,
)
from app.models.email_dispatch import (
    MESSAGE_FAILED,
    MESSAGE_QUEUED,
    MESSAGE_SENDING,
    MESSAGE_SENT,
    EmailMessage,
    EmailMessageAttempt,
    EmailSmtpConfig,
)
from app.services.email_dispatch import queue, retry, sender
from app.services.email_dispatch.errors import DispatchError, SendError
from app.utils.crypto import encrypt_secret
from sqlalchemy import select


def aware(moment: datetime) -> datetime:
    """
    A datetime off a row, as UTC-aware.

    ``DateTime(timezone=True)`` gives back an aware value on PostgreSQL and a **naive** one
    on SQLite, which drops the offset at storage — so comparing a stored value to
    ``datetime.now(timezone.utc)`` raises ``TypeError`` in the test suite and not in
    production. Same helper, same reasoning, as ``scheduler._aware``. Assuming UTC is right
    rather than convenient: every write in this module is ``datetime.now(timezone.utc)``, so
    a naive value is a UTC value that lost its label.
    """
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


async def make_message(db, user, config, **overrides) -> EmailMessage:  # noqa: ANN001
    """A queued, fully-rendered message. Rendering is tested elsewhere; this is queue
    mechanics, so the content is already finished exactly as `enqueue_email` leaves it."""
    values = {
        "user_id": user.id,
        "smtp_config_id": config.id if config else None,
        "source": "manual",
        "from_email": "alerts@example.com",
        "to_addresses": ["ops@example.com"],
        "cc_addresses": [],
        "bcc_addresses": [],
        "subject": "Nightly sync failed",
        "body_html": "<p>It failed.</p>",
        "body_text": "It failed.",
        "status": MESSAGE_QUEUED,
        "smtp_host": config.host if config else None,
    }
    values.update(overrides)
    message = EmailMessage(**values)
    db.add(message)
    await db.commit()
    await db.refresh(message)
    return message


async def second_config(db, user) -> EmailSmtpConfig:  # noqa: ANN001
    config = EmailSmtpConfig(
        user_id=user.id,
        name="Bulk relay",
        host="bulk.example.com",
        port=587,
        security="starttls",
        username="bulk",
        password_encrypted=encrypt_secret("s3cret"),
        from_email="bulk@example.com",
    )
    db.add(config)
    await db.commit()
    await db.refresh(config)
    return config


class TestClaim:
    async def test_claims_a_due_message_and_marks_it_sending(self, db, user, smtp_config):  # noqa: ANN001
        message = await make_message(db, user, smtp_config)

        claimed = await claim_next_email(db, claimed_by="host:1")

        assert claimed is not None
        assert claimed.id == message.id
        assert claimed.status == MESSAGE_SENDING
        # The claim is what increments the attempt counter, so `should_retry` has something
        # to bound against without the worker having to remember to count.
        assert claimed.attempt == 1
        assert claimed.claimed_by == "host:1"
        assert claimed.heartbeat_at is not None

    async def test_will_not_claim_a_message_that_is_not_due_yet(self, db, user, smtp_config):  # noqa: ANN001
        await make_message(
            db,
            user,
            smtp_config,
            next_attempt_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )

        assert await claim_next_email(db, claimed_by="host:1") is None

    async def test_a_second_claim_skips_the_message_already_sending(self, db, user, smtp_config):  # noqa: ANN001
        """Two workers, one message: the second gets nothing rather than the same row."""
        await make_message(db, user, smtp_config)

        first = await claim_next_email(db, claimed_by="host:1")
        second = await claim_next_email(db, claimed_by="host:2")

        assert first is not None
        assert second is None

    async def test_highest_priority_goes_first(self, db, user, smtp_config):  # noqa: ANN001
        await make_message(db, user, smtp_config, subject="digest", priority=0)
        await make_message(db, user, smtp_config, subject="test send", priority=10)

        claimed = await claim_next_email(db, claimed_by="host:1")

        assert claimed is not None
        assert claimed.subject == "test send"


class TestPerServerSerialisation:
    """The correlated NOT EXISTS. See the module docstring."""

    async def test_one_server_sends_one_at_a_time(self, db, user, smtp_config):  # noqa: ANN001
        await make_message(db, user, smtp_config, subject="first")
        await make_message(db, user, smtp_config, subject="second")

        first = await claim_next_email(db, claimed_by="host:1")
        blocked = await claim_next_email(db, claimed_by="host:2")

        assert first is not None
        assert blocked is None, (
            "a second message for the same SMTP config must not be claimed while one "
            "is already sending"
        )

    async def test_two_servers_send_in_parallel(self, db, user, smtp_config):  # noqa: ANN001
        """The other half of the same rule, and the half a broken correlation breaks: one
        busy server must not block a different one."""
        other = await second_config(db, user)
        await make_message(db, user, smtp_config, subject="via relay one")
        await make_message(db, user, other, subject="via relay two")

        first = await claim_next_email(db, claimed_by="host:1")
        second = await claim_next_email(db, claimed_by="host:2")

        assert first is not None
        assert second is not None
        assert {first.smtp_config_id, second.smtp_config_id} == {smtp_config.id, other.id}

    async def test_a_message_with_no_config_is_not_blocked_by_anything(self, db, user, smtp_config):  # noqa: ANN001
        """Its config was deleted after it was queued. It contends with nothing, so it is
        claimed normally and fails with a clear reason rather than sitting unclaimable
        forever — which is what a naive `NULL = NULL` comparison would produce."""
        await make_message(db, user, smtp_config, subject="has a server")
        await make_message(db, user, None, subject="orphaned")

        first = await claim_next_email(db, claimed_by="host:1")
        second = await claim_next_email(db, claimed_by="host:2")

        assert first is not None
        assert second is not None
        assert second.smtp_config_id is None


class TestSending:
    async def test_a_successful_send_records_sent_and_one_attempt(
        self, db, user, smtp_config, no_smtp
    ):  # noqa: ANN001
        message = await make_message(db, user, smtp_config)

        assert await queue.drain_once() is True

        await db.refresh(message)
        assert message.status == MESSAGE_SENT
        assert message.sent_at is not None
        assert message.error_message is None
        # Cleared on finish: a sent message still naming a worker reads like one in flight,
        # and the stale reaper would eventually agree.
        assert message.claimed_by is None

        attempts = (
            await db.execute(
                select(EmailMessageAttempt).where(
                    EmailMessageAttempt.message_id == message.id
                )
            )
        ).scalars().all()
        assert len(attempts) == 1
        assert attempts[0].status == MESSAGE_SENT
        assert attempts[0].attempt == 1

    async def test_the_stored_text_is_what_is_sent(self, db, user, smtp_config, no_smtp):  # noqa: ANN001
        """The worker sends the rendered bytes off the row, never re-renders. This is what
        makes a retry provably identical to the first attempt."""
        await make_message(
            db, user, smtp_config, subject="Exact subject", body_html="<p>Exact body</p>"
        )

        await queue.drain_once()

        assert no_smtp.last["subject"] == "Exact subject"
        assert no_smtp.last["body_html"] == "<p>Exact body</p>"
        assert no_smtp.last["to_addresses"] == ["ops@example.com"]

    async def test_the_decrypted_password_reaches_the_transport(
        self, db, user, smtp_config, no_smtp
    ):  # noqa: ANN001
        await make_message(db, user, smtp_config)

        await queue.drain_once()

        assert no_smtp.last["target"].password == "hunter2"

    async def test_a_retryable_failure_goes_back_on_the_queue_with_a_backoff(
        self, db, user, smtp_config, no_smtp
    ):  # noqa: ANN001
        no_smtp.error = SendError(
            "smtp.example.com did not respond. It will be tried again.",
            retryable=True,
        )
        message = await make_message(db, user, smtp_config)
        before = datetime.now(timezone.utc)

        await queue.drain_once()

        await db.refresh(message)
        assert message.status == MESSAGE_QUEUED
        assert message.attempt == 1
        assert message.error_message is not None
        # Due later, by the first backoff step, so the worker does not spin on it.
        assert aware(message.next_attempt_at) > before + timedelta(
            seconds=retry.BASE_DELAY_SECONDS - 1
        )

    async def test_a_permanent_failure_stops_immediately(
        self, db, user, smtp_config, no_smtp
    ):  # noqa: ANN001
        """A rejected recipient must not burn five attempts and twenty minutes."""
        no_smtp.error = SendError(
            "smtp.example.com refused every recipient address on this email.",
            retryable=False,
            permanent=True,
            smtp_code=550,
        )
        message = await make_message(db, user, smtp_config)

        await queue.drain_once()

        await db.refresh(message)
        assert message.status == MESSAGE_FAILED
        assert message.attempt == 1
        assert "refused" in message.error_message

    async def test_retries_stop_at_max_attempts_and_the_message_stops_promising_more(
        self, db, user, smtp_config, no_smtp
    ):  # noqa: ANN001
        no_smtp.error = SendError(
            "smtp.example.com did not respond. It will be tried again.",
            retryable=True,
        )
        message = await make_message(
            db, user, smtp_config, max_attempts=2, attempt=1
        )

        # Second and final attempt.
        await queue.drain_once()

        await db.refresh(message)
        assert message.status == MESSAGE_FAILED
        assert message.attempt == 2
        # The sender's "will be tried again" must not be left standing on a row that never
        # will be — the small lie that costs somebody an afternoon.
        assert "will be tried again" not in message.error_message
        assert "gave up" in message.error_message

    async def test_a_dispatch_error_is_never_retried(
        self, db, user, smtp_config, no_smtp
    ):  # noqa: ANN001
        """A misconfiguration needs a person. Retrying it just writes the same failure
        five times."""
        no_smtp.error = DispatchError(
            "This application will not connect to smtp.example.com."
        )
        message = await make_message(db, user, smtp_config)

        await queue.drain_once()

        await db.refresh(message)
        assert message.status == MESSAGE_FAILED

    async def test_a_deleted_smtp_config_fails_with_a_sentence_naming_the_cause(
        self, db, user, no_smtp
    ):  # noqa: ANN001
        message = await make_message(db, user, None)

        await queue.drain_once()

        await db.refresh(message)
        assert message.status == MESSAGE_FAILED
        assert "no longer exists" in message.error_message
        assert no_smtp.call_count == 0, "nothing should have been sent"

    async def test_an_inactive_smtp_config_fails_without_sending(
        self, db, user, smtp_config, no_smtp
    ):  # noqa: ANN001
        smtp_config.is_active = False
        await db.commit()
        message = await make_message(db, user, smtp_config)

        await queue.drain_once()

        await db.refresh(message)
        assert message.status == MESSAGE_FAILED
        assert "switched off" in message.error_message
        assert no_smtp.call_count == 0

    async def test_a_partial_acceptance_is_recorded_as_sent_but_names_the_refusals(
        self, db, user, smtp_config, no_smtp
    ):  # noqa: ANN001
        """SMTP can accept a message while refusing some recipients, without raising.
        Recording that as an unqualified success is the half-truth this module exists to
        avoid."""
        no_smtp.result = sender.SendResult(
            response="250 Ok",
            message_id="<x@y>",
            rejected=("gone@example.com",),
        )
        message = await make_message(db, user, smtp_config)

        await queue.drain_once()

        await db.refresh(message)
        assert message.status == MESSAGE_SENT
        assert "gone@example.com" in message.smtp_response

    async def test_drain_reports_no_work_when_the_queue_is_empty(self, db):  # noqa: ANN001
        assert await queue.drain_once() is False


class TestStaleReaper:
    async def test_a_dead_worker_fails_the_message_and_declares_delivery_unknown(
        self, db, user, smtp_config
    ):  # noqa: ANN001
        """The most consequential decision in the module. See the module docstring."""
        message = await make_message(
            db,
            user,
            smtp_config,
            status=MESSAGE_SENDING,
            attempt=1,
            claimed_by="dead-host:99",
            heartbeat_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )

        reaped = await requeue_stale_emails(db, stale_after_seconds=60)

        assert reaped == [message.id]
        await db.refresh(message)
        assert message.status == MESSAGE_FAILED
        assert message.status != MESSAGE_QUEUED, "it must NOT be silently retried"
        assert "unknown" in message.error_message
        assert "twice" in message.error_message, (
            "the operator has to be told what retrying risks"
        )
        # Not reset, so a message that keeps killing its worker is visible as such.
        assert message.attempt == 1

    async def test_a_live_worker_is_left_alone(self, db, user, smtp_config):  # noqa: ANN001
        message = await make_message(
            db,
            user,
            smtp_config,
            status=MESSAGE_SENDING,
            heartbeat_at=datetime.now(timezone.utc),
        )

        assert await requeue_stale_emails(db, stale_after_seconds=60) == []

        await db.refresh(message)
        assert message.status == MESSAGE_SENDING

    async def test_a_queued_message_is_not_reaped(self, db, user, smtp_config):  # noqa: ANN001
        """Only `sending` rows can be stale. A queued one has no worker to have died."""
        await make_message(db, user, smtp_config, heartbeat_at=None)

        assert await requeue_stale_emails(db, stale_after_seconds=0) == []


class TestDepth:
    async def test_counts_only_queued_messages(self, db, user, smtp_config):  # noqa: ANN001
        await make_message(db, user, smtp_config, subject="waiting")
        await make_message(db, user, smtp_config, subject="done", status=MESSAGE_SENT)

        assert await queued_email_count(db) == 1
