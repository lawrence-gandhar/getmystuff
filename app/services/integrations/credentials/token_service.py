"""
Keeping an OAuth token fresh, with a compare-and-set lock rather than a row lock.

Phase 1 has no OAuth connector. This lands now anyway, because Phase 2 and Phase 3 both
build on it and retrofitting a locking strategy under working code is how a subtle
concurrency bug gets introduced by a refactor nobody thought was risky.

**Why compare-and-set and not ``FOR UPDATE SKIP LOCKED``.** ``claim_next_job`` uses a row
lock and is right to: claiming a queue row is instantaneous. A token refresh is an
*outbound HTTP call taking seconds*, and a row lock would hold an open transaction and a
pooled database connection for that whole time, across every concurrent node in every
run. Three consequences follow, and all three matter:

* A TTL column survives a refresher that crashed. A transaction lock does too, but only
  because the transaction died — which means it also survives a refresher that is merely
  slow, by releasing early.
* Nothing holds a connection from the pool while somebody else's server thinks.
* **It works on SQLite**, so the two-concurrent-refreshers test runs in the ordinary
  suite instead of not existing.

**Rotation ordering is load-bearing.** GoHighLevel and Shopify online tokens issue a new
refresh token on every use and invalidate the old one. So the sequence is

    exchange → write → commit → *then* use

If the exchange succeeds and the write fails, the stored refresh token is already dead
and the connection is locked out permanently — there is no recovery but a human
reconnecting. Every other ordering has that failure in it.

**``invalid_grant`` is never retried.** It means the refresh token is gone: revoked,
already rotated, or expired. Trying again is how a provider that counts failures locks
the connection out for good. It sets ``needs_reauth`` and raises permanently.
"""

import asyncio
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Optional

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.integrations import (
    CONNECTION_NEEDS_REAUTH,
    CREDENTIAL_REAUTH_REQUIRED,
    CREDENTIAL_REFRESH_FAILED,
    CREDENTIAL_REFRESHED,
    IntegrationConnection,
    IntegrationCredential,
)
from app.services.integrations.connectors.spec import AuthSpec
from app.services.integrations.credentials import credential_service
from app.services.integrations.errors import IntegrationFailure, NodeFailure
from app.utils.crypto import decrypt_secret, encrypt_secret

logger = logging.getLogger(__name__)


#: Refresh this long before the token actually expires. Two minutes covers the round
#: trip plus a clock that is a little out — an expiry checked to the second means a
#: token that expires between the check and the call, which presents as an intermittent
#: 401 nobody can reproduce.
REFRESH_MARGIN_SECONDS = 120

#: How long a claimed refresh may hold the lock before another worker may take it. Long
#: enough for a slow provider, short enough that a crashed refresher does not block a
#: run for minutes.
LOCK_TTL_SECONDS = 60

#: How long a worker that lost the race waits for the winner. Beyond this it gives up
#: rather than queueing indefinitely — a refresh that has taken half a minute is one
#: that is not coming back, and failing the node is more honest than hanging it.
WAIT_TIMEOUT_SECONDS = 30
WAIT_POLL_SECONDS = 0.25


def now() -> datetime:
    """
    The clock, as a seam.

    A function rather than an inline ``datetime.now`` so a test can freeze it. Every
    expiry decision in this module goes through here, which is what makes "a token one
    second from expiry is refreshed" testable without sleeping.
    """
    return datetime.now(timezone.utc)


def needs_refresh(credential: IntegrationCredential, *, at: Optional[datetime] = None) -> bool:
    """
    Whether this token should be refreshed before it is used.

    A credential with no expiry never needs one — an API key does not expire, and
    treating "no expiry" as "expired" would send every request through a refresh path
    that has nothing to refresh with.
    """
    if credential.expires_at is None:
        return False

    moment = at or now()
    expires = _aware(credential.expires_at)
    return expires - timedelta(seconds=REFRESH_MARGIN_SECONDS) <= moment


def _aware(value: datetime) -> datetime:
    """
    A timestamp with a timezone, whatever the driver returned.

    SQLite gives naive datetimes back even for a ``DateTime(timezone=True)`` column, so
    comparing one against an aware ``now()`` raises. Treating a naive value as UTC is
    correct here because that is what was written.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# The lock
# ---------------------------------------------------------------------------


async def claim_refresh(
    db: AsyncSession, credential: IntegrationCredential, *, at: Optional[datetime] = None
) -> Optional[str]:
    """
    Try to become the one worker that refreshes this credential.

    Returns a token identifying the claim, or ``None`` if somebody else holds it.

    The whole claim is one conditional ``UPDATE``: it sets the lock only where the lock
    is free or expired, and the row count says whether this caller won. Two workers
    running it at the same moment cannot both match, because the second one's ``WHERE``
    no longer holds by the time it runs — which is the same guarantee ``SKIP LOCKED``
    gives, without holding a transaction open across an HTTP call.
    """
    moment = at or now()
    claim = secrets.token_urlsafe(24)

    result = await db.execute(
        update(IntegrationCredential)
        .where(
            IntegrationCredential.id == credential.id,
            (IntegrationCredential.refresh_lock_expires_at.is_(None))
            | (IntegrationCredential.refresh_lock_expires_at <= moment),
        )
        .values(
            refresh_lock_token=claim,
            refresh_lock_expires_at=moment + timedelta(seconds=LOCK_TTL_SECONDS),
        )
    )
    await db.commit()

    return claim if result.rowcount == 1 else None


async def release_refresh(
    db: AsyncSession, credential: IntegrationCredential, claim: str
) -> None:
    """
    Give the lock back, but only if it is still ours.

    The ``refresh_lock_token`` check is the point: a refresh that overran its TTL has
    already had the lock taken by somebody else, and clearing it blindly would release
    *their* claim and let a third worker refresh concurrently.
    """
    await db.execute(
        update(IntegrationCredential)
        .where(
            IntegrationCredential.id == credential.id,
            IntegrationCredential.refresh_lock_token == claim,
        )
        .values(refresh_lock_token=None, refresh_lock_expires_at=None)
    )
    await db.commit()


async def wait_for_refresh(
    db: AsyncSession,
    credential: IntegrationCredential,
    *,
    timeout: float = WAIT_TIMEOUT_SECONDS,
    poll: float = WAIT_POLL_SECONDS,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> IntegrationCredential:
    """
    Wait for whoever won the claim to finish, then re-read the row.

    Gives up rather than queueing indefinitely. A refresh that has taken half a minute
    is one that is not coming back, and failing the node is more honest than hanging it
    — a node that never returns looks identical to a node that is working.
    """
    waited = 0.0

    while waited < timeout:
        await sleep(poll)
        waited += poll

        await db.refresh(credential)
        if not credential.refresh_lock_token and not needs_refresh(credential):
            return credential

    raise NodeFailure(
        "Another part of this run is still refreshing this connection's access, and it "
        "has taken longer than expected. Try running it again.",
        retryable=True,
    )


# ---------------------------------------------------------------------------
# The refresh
# ---------------------------------------------------------------------------


async def ensure_fresh_token(
    db: AsyncSession,
    connection: IntegrationConnection,
    credential: IntegrationCredential,
    auth: AuthSpec,
    *,
    exchange: Callable[[str], Awaitable[dict]],
    at: Optional[datetime] = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> IntegrationCredential:
    """
    Return a credential whose access token is good to use.

    ``exchange`` performs the provider call and returns its parsed response. Injected
    rather than imported so this module does no HTTP and the concurrency can be tested
    without a server — the real one goes through ``runtime/sender`` like every other
    outbound call, so it is pooled and egress-checked.

    A caller that loses the claim waits for the winner rather than refreshing too. Two
    concurrent refreshes against a provider that rotates its refresh token means one of
    them writes a token the other has already invalidated.
    """
    if not needs_refresh(credential, at=at):
        return credential

    claim = await claim_refresh(db, credential, at=at)

    if claim is None:
        return await wait_for_refresh(db, credential, sleep=sleep)

    try:
        return await _refresh(db, connection, credential, auth, exchange=exchange)
    finally:
        await release_refresh(db, credential, claim)


async def _refresh(
    db: AsyncSession,
    connection: IntegrationConnection,
    credential: IntegrationCredential,
    auth: AuthSpec,
    *,
    exchange: Callable[[str], Awaitable[dict]],
) -> IntegrationCredential:
    if not credential.refresh_token_encrypted:
        raise await _needs_reauth(
            db, connection,
            "There is no saved way to renew this connection's access. Reconnect it.",
        )

    try:
        refresh_token = decrypt_secret(credential.refresh_token_encrypted)
    except Exception as exc:  # noqa: BLE001
        raise await _needs_reauth(
            db, connection,
            "This connection's saved access could not be read. Reconnect it.",
        ) from exc

    try:
        response = await exchange(refresh_token)
    except PermanentGrantFailure as exc:
        # invalid_grant. See the module docstring: never retried.
        raise await _needs_reauth(db, connection, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        credential.refresh_failures = (credential.refresh_failures or 0) + 1
        await db.commit()
        await credential_service.record_event(
            db, connection, CREDENTIAL_REFRESH_FAILED,
            detail={"failures": credential.refresh_failures},
        )
        raise NodeFailure(
            f"'{connection.label}' could not renew its access ({exc}). This is usually "
            "temporary.",
            retryable=True,
        ) from exc

    # exchange → write → commit → then use. See the module docstring; every other
    # ordering can leave a dead refresh token in the database.
    _apply(credential, response, auth)
    credential.refresh_failures = 0
    credential.refreshed_at = now()
    await db.commit()
    await db.refresh(credential)

    await credential_service.record_event(
        db, connection, CREDENTIAL_REFRESHED,
        detail={"expires_at": str(credential.expires_at or "")},
    )

    return credential


def _apply(credential: IntegrationCredential, response: dict, auth: AuthSpec) -> None:
    """
    Write a token response onto the credential row.

    **A rotating provider that sent no new refresh token is a fault, not a default.**
    Keeping the old one would be right for a provider that does not rotate and
    catastrophic for one that does — the old token is already dead, and the next refresh
    would fail with nothing to explain it. So the connector's ``rotates_refresh_token``
    decides, and the absence is refused rather than papered over.
    """
    access = str(response.get("access_token") or "")
    if not access:
        raise IntegrationFailure(
            "The provider's response did not contain an access token, so there is "
            "nothing to use."
        )

    credential.access_token_encrypted = encrypt_secret(access)

    refresh = response.get("refresh_token")
    if refresh:
        credential.refresh_token_encrypted = encrypt_secret(str(refresh))
    elif auth.rotates_refresh_token:
        raise IntegrationFailure(
            "This provider issues a new renewal token every time and did not send one, "
            "so the saved one is no longer usable. Reconnect this connection."
        )

    if response.get("token_type"):
        credential.token_type = str(response["token_type"])
    if response.get("scope"):
        credential.scope = str(response["scope"])

    expires_in = response.get("expires_in")
    if expires_in is not None:
        try:
            credential.expires_at = now() + timedelta(seconds=int(expires_in))
        except (TypeError, ValueError):
            # A provider sending something unparseable is one whose expiry we do not
            # know. Clearing it means "never refresh proactively", which is worse than
            # leaving the previous value and letting a 401 trigger a reconnect.
            logger.warning("Ignoring unparseable expires_in: %r", expires_in)


class PermanentGrantFailure(Exception):
    """
    The refresh token is gone — revoked, already rotated, or expired.

    Raised by the ``exchange`` callable when the provider says ``invalid_grant``. A
    distinct type because it is the one token failure that must never be retried, and a
    string match on a message is not a thing to make that decision with.
    """


async def _needs_reauth(
    db: AsyncSession, connection: IntegrationConnection, message: str
) -> NodeFailure:
    """
    Mark the connection and build the failure to raise.

    Returns rather than raises so the call site reads ``raise await _needs_reauth(...)``
    — which keeps the ``raise`` visible where the control flow leaves, instead of hiding
    it inside a helper.

    The audit event carries no detail beyond the fact. Which token failed and why is in
    the log; this table is read by somebody asking "when did this stop working".
    """
    connection.status = CONNECTION_NEEDS_REAUTH
    await db.commit()

    await credential_service.record_event(
        db, connection, CREDENTIAL_REAUTH_REQUIRED,
    )

    return NodeFailure(
        f"'{connection.label}' needs to be reconnected. {message}",
        permanent=True,
    )
