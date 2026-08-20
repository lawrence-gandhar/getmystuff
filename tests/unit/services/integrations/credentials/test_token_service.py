"""
Tests for ``credentials/token_service.py``.

**The two-concurrent-refreshers test runs here because the lock is a compare-and-set.**
That is the concrete payoff of choosing CAS over ``FOR UPDATE SKIP LOCKED``: a row lock
would be a no-op on SQLite, so this test would either not exist or would pass without
testing anything. ``asyncio.gather`` on two ``ensure_fresh_token`` calls against a
counted token endpoint asserts exactly one exchange.

Two other assertions matter as much:

**``invalid_grant`` is never retried.** It means the refresh token is gone. Trying again
is how a provider that counts failures locks a connection out for good.

**A rotating provider that sends no new refresh token is a fault.** Keeping the old one
is right for a provider that does not rotate and catastrophic for one that does — the
old token is already dead, and the *next* refresh fails with nothing to explain it.

Nothing sleeps for real: ``sleep`` and ``now`` are both seams.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import List

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.integrations import (
    AUTH_OAUTH2,
    CONNECTION_ACTIVE,
    CONNECTION_NEEDS_REAUTH,
    IntegrationConnection,
    IntegrationCredential,
    IntegrationCredentialEvent,
)
from app.models.user.user import User
from app.services.integrations.connectors.spec import AuthSpec
from app.services.integrations.credentials import credential_service, token_service
from app.services.integrations.errors import IntegrationFailure, NodeFailure
from app.utils.crypto import decrypt_secret

NOW = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)

ROTATING = AuthSpec(
    kind=AUTH_OAUTH2, token_url="https://provider.example.com/token",
    rotates_refresh_token=True,
)
STATIC = AuthSpec(kind=AUTH_OAUTH2, token_url="https://provider.example.com/token")


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    ``now()`` is a seam so an expiry decision is testable without sleeping. Every
    expiry check in the module goes through it.
    """
    monkeypatch.setattr(token_service, "now", lambda: NOW)


async def noop_sleep(seconds: float) -> None:
    return None


@pytest.fixture
async def connection(db: AsyncSession, user: User) -> IntegrationConnection:
    row = IntegrationConnection(
        user_id=user.id,
        connector_id="gohighlevel",
        label="GHL Location 12",
        auth_kind=AUTH_OAUTH2,
        base_url="https://services.leadconnectorhq.com",
    )
    db.add(row)
    await db.commit()
    return row


async def credential_for(
    db: AsyncSession, connection: IntegrationConnection, *, expires_in: int = 3600
) -> IntegrationCredential:
    return await credential_service.store_credential(
        db,
        connection,
        access_token="access-1",
        refresh_token="refresh-1",
        expires_at=NOW + timedelta(seconds=expires_in),
    )


class TestNeedsRefresh:
    async def test_a_token_with_plenty_of_time_does_not(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        credential = await credential_for(db, connection, expires_in=3600)

        assert token_service.needs_refresh(credential, at=NOW) is False

    async def test_a_token_inside_the_margin_does(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        """
        Refreshed before it expires rather than after. Checking to the second means a
        token that expires between the check and the call, which presents as an
        intermittent 401 nobody can reproduce.
        """
        credential = await credential_for(
            db, connection, expires_in=token_service.REFRESH_MARGIN_SECONDS - 1
        )

        assert token_service.needs_refresh(credential, at=NOW) is True

    async def test_an_expired_token_does(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        credential = await credential_for(db, connection, expires_in=-60)

        assert token_service.needs_refresh(credential, at=NOW) is True

    async def test_a_credential_with_no_expiry_never_does(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        """An API key does not expire, and treating "no expiry" as "expired" would send
        every request through a refresh path with nothing to refresh."""
        credential = await credential_service.store_credential(
            db, connection, api_key="sk-1"
        )

        assert token_service.needs_refresh(credential, at=NOW) is False


class TestTheClaim:
    async def test_the_first_caller_wins(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        credential = await credential_for(db, connection)

        assert await token_service.claim_refresh(db, credential, at=NOW)

    async def test_the_second_caller_does_not(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        credential = await credential_for(db, connection)

        first = await token_service.claim_refresh(db, credential, at=NOW)
        second = await token_service.claim_refresh(db, credential, at=NOW)

        assert first is not None
        assert second is None

    async def test_an_expired_lock_may_be_taken(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        """
        A TTL is what survives a refresher that crashed. A transaction lock only manages
        that by accident — and by releasing early for one that is merely slow.
        """
        credential = await credential_for(db, connection)
        await token_service.claim_refresh(db, credential, at=NOW)

        later = NOW + timedelta(seconds=token_service.LOCK_TTL_SECONDS + 1)

        assert await token_service.claim_refresh(db, credential, at=later)

    async def test_releasing_lets_the_next_caller_in(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        credential = await credential_for(db, connection)
        claim = await token_service.claim_refresh(db, credential, at=NOW)

        await token_service.release_refresh(db, credential, claim)

        assert await token_service.claim_refresh(db, credential, at=NOW)

    async def test_releasing_somebody_elses_lock_does_nothing(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        """
        A refresh that overran its TTL has already had the lock taken by somebody else.
        Clearing it blindly would release *their* claim and let a third worker refresh
        concurrently.
        """
        credential = await credential_for(db, connection)
        await token_service.claim_refresh(db, credential, at=NOW)

        await token_service.release_refresh(db, credential, "a-different-claim")

        assert await token_service.claim_refresh(db, credential, at=NOW) is None


class TestConcurrentRefreshers:
    """
    The test the CAS design exists to make possible. See the module docstring.
    """

    async def test_two_callers_produce_exactly_one_exchange(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        credential = await credential_for(db, connection, expires_in=-60)
        exchanges: List[str] = []

        async def exchange(refresh_token: str) -> dict:
            exchanges.append(refresh_token)
            # Let the other caller reach the claim before this one commits.
            await asyncio.sleep(0)
            return {"access_token": "access-2", "refresh_token": "refresh-2",
                    "expires_in": 3600}

        results = await asyncio.gather(
            token_service.ensure_fresh_token(
                db, connection, credential, STATIC, exchange=exchange, sleep=noop_sleep
            ),
            token_service.ensure_fresh_token(
                db, connection, credential, STATIC, exchange=exchange, sleep=noop_sleep
            ),
            return_exceptions=True,
        )

        assert len(exchanges) == 1, "two workers refreshed the same credential"
        assert any(not isinstance(result, BaseException) for result in results)

    async def test_a_fresh_token_short_circuits_without_claiming(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        credential = await credential_for(db, connection, expires_in=3600)
        exchanges: List[str] = []

        async def exchange(refresh_token: str) -> dict:
            exchanges.append(refresh_token)
            return {}

        await token_service.ensure_fresh_token(
            db, connection, credential, STATIC, exchange=exchange
        )

        assert exchanges == []
        assert credential.refresh_lock_token is None


class TestRefreshing:
    async def test_the_new_tokens_are_written_and_encrypted(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        credential = await credential_for(db, connection, expires_in=-60)

        async def exchange(refresh_token: str) -> dict:
            assert refresh_token == "refresh-1"
            return {"access_token": "access-2", "refresh_token": "refresh-2",
                    "expires_in": 3600}

        refreshed = await token_service.ensure_fresh_token(
            db, connection, credential, ROTATING, exchange=exchange
        )

        assert decrypt_secret(refreshed.access_token_encrypted) == "access-2"
        assert decrypt_secret(refreshed.refresh_token_encrypted) == "refresh-2"

    async def test_the_expiry_moves_forward(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        credential = await credential_for(db, connection, expires_in=-60)

        async def exchange(refresh_token: str) -> dict:
            return {"access_token": "a", "refresh_token": "r", "expires_in": 3600}

        refreshed = await token_service.ensure_fresh_token(
            db, connection, credential, ROTATING, exchange=exchange
        )

        assert token_service.needs_refresh(refreshed, at=NOW) is False

    async def test_the_lock_is_released_afterwards(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        credential = await credential_for(db, connection, expires_in=-60)

        async def exchange(refresh_token: str) -> dict:
            return {"access_token": "a", "refresh_token": "r", "expires_in": 3600}

        await token_service.ensure_fresh_token(
            db, connection, credential, ROTATING, exchange=exchange
        )

        assert credential.refresh_lock_token is None

    async def test_the_lock_is_released_even_when_the_exchange_fails(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        """Otherwise one failed refresh blocks every later attempt for the whole TTL."""
        credential = await credential_for(db, connection, expires_in=-60)

        async def exchange(refresh_token: str) -> dict:
            raise RuntimeError("provider is down")

        with pytest.raises(NodeFailure):
            await token_service.ensure_fresh_token(
                db, connection, credential, ROTATING, exchange=exchange
            )

        await db.refresh(credential)
        assert credential.refresh_lock_token is None

    async def test_a_successful_refresh_resets_the_failure_count(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        credential = await credential_for(db, connection, expires_in=-60)
        credential.refresh_failures = 4
        await db.commit()

        async def exchange(refresh_token: str) -> dict:
            return {"access_token": "a", "refresh_token": "r", "expires_in": 3600}

        refreshed = await token_service.ensure_fresh_token(
            db, connection, credential, ROTATING, exchange=exchange
        )

        assert refreshed.refresh_failures == 0

    async def test_it_writes_an_audit_event(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        credential = await credential_for(db, connection, expires_in=-60)

        async def exchange(refresh_token: str) -> dict:
            return {"access_token": "a", "refresh_token": "r", "expires_in": 3600}

        await token_service.ensure_fresh_token(
            db, connection, credential, ROTATING, exchange=exchange
        )

        events = (await db.execute(select(IntegrationCredentialEvent))).scalars().all()
        assert "refreshed" in {event.event for event in events}


class TestRotation:
    async def test_a_rotating_provider_that_sends_no_new_token_is_a_fault(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        """
        Keeping the old one is right for a provider that does not rotate and
        catastrophic for one that does: the old token is already dead, and the *next*
        refresh fails with nothing to explain it.
        """
        credential = await credential_for(db, connection, expires_in=-60)

        async def exchange(refresh_token: str) -> dict:
            return {"access_token": "access-2", "expires_in": 3600}

        with pytest.raises(IntegrationFailure, match="new renewal token"):
            await token_service.ensure_fresh_token(
                db, connection, credential, ROTATING, exchange=exchange
            )

    async def test_a_non_rotating_provider_may_omit_it(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        credential = await credential_for(db, connection, expires_in=-60)

        async def exchange(refresh_token: str) -> dict:
            return {"access_token": "access-2", "expires_in": 3600}

        refreshed = await token_service.ensure_fresh_token(
            db, connection, credential, STATIC, exchange=exchange
        )

        assert decrypt_secret(refreshed.refresh_token_encrypted) == "refresh-1"

    async def test_a_response_with_no_access_token_is_refused(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        credential = await credential_for(db, connection, expires_in=-60)

        async def exchange(refresh_token: str) -> dict:
            return {"token_type": "Bearer"}

        with pytest.raises(IntegrationFailure, match="did not contain an access token"):
            await token_service.ensure_fresh_token(
                db, connection, credential, STATIC, exchange=exchange
            )


class TestInvalidGrantIsNeverRetried:
    """The failure that must not be retried. See the module docstring."""

    async def test_it_sets_needs_reauth(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        credential = await credential_for(db, connection, expires_in=-60)

        async def exchange(refresh_token: str) -> dict:
            raise token_service.PermanentGrantFailure("The saved access was revoked.")

        with pytest.raises(NodeFailure):
            await token_service.ensure_fresh_token(
                db, connection, credential, ROTATING, exchange=exchange
            )

        assert connection.status == CONNECTION_NEEDS_REAUTH

    async def test_the_failure_is_permanent(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        credential = await credential_for(db, connection, expires_in=-60)

        async def exchange(refresh_token: str) -> dict:
            raise token_service.PermanentGrantFailure("revoked")

        with pytest.raises(NodeFailure) as caught:
            await token_service.ensure_fresh_token(
                db, connection, credential, ROTATING, exchange=exchange
            )

        assert caught.value.permanent is True
        assert "reconnected" in str(caught.value)

    async def test_an_ordinary_failure_is_retryable_instead(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        """A provider being briefly down is not a revoked grant, and treating them alike
        would send somebody to reconnect a connection that is fine."""
        credential = await credential_for(db, connection, expires_in=-60)

        async def exchange(refresh_token: str) -> dict:
            raise RuntimeError("502 from the provider")

        with pytest.raises(NodeFailure) as caught:
            await token_service.ensure_fresh_token(
                db, connection, credential, ROTATING, exchange=exchange
            )

        assert caught.value.retryable is True
        assert connection.status == CONNECTION_ACTIVE

    async def test_an_ordinary_failure_counts_up(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        credential = await credential_for(db, connection, expires_in=-60)

        async def exchange(refresh_token: str) -> dict:
            raise RuntimeError("502")

        with pytest.raises(NodeFailure):
            await token_service.ensure_fresh_token(
                db, connection, credential, ROTATING, exchange=exchange
            )

        await db.refresh(credential)
        assert credential.refresh_failures == 1

    async def test_no_saved_refresh_token_asks_for_a_reconnect(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        credential = await credential_service.store_credential(
            db, connection, access_token="a", expires_at=NOW - timedelta(seconds=60)
        )

        async def exchange(refresh_token: str) -> dict:  # pragma: no cover - not reached
            raise AssertionError("should not be called")

        with pytest.raises(NodeFailure, match="Reconnect"):
            await token_service.ensure_fresh_token(
                db, connection, credential, ROTATING, exchange=exchange
            )


class TestWaitingForTheWinner:
    async def test_it_gives_up_rather_than_hanging(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        """
        A refresh that has taken half a minute is one that is not coming back, and a
        node that never returns looks identical to a node that is working.
        """
        credential = await credential_for(db, connection, expires_in=-60)
        await token_service.claim_refresh(db, credential, at=NOW)

        with pytest.raises(NodeFailure, match="taken longer than expected"):
            await token_service.wait_for_refresh(
                db, credential, timeout=1.0, poll=0.25, sleep=noop_sleep
            )

    async def test_the_timeout_failure_is_retryable(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        credential = await credential_for(db, connection, expires_in=-60)
        await token_service.claim_refresh(db, credential, at=NOW)

        with pytest.raises(NodeFailure) as caught:
            await token_service.wait_for_refresh(
                db, credential, timeout=1.0, poll=0.25, sleep=noop_sleep
            )

        assert caught.value.retryable is True
