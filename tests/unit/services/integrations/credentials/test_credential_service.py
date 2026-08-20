"""
Tests for ``credentials/credential_service.py``.

The properties worth pinning are all about what *cannot* happen rather than what does.

**No caller holds a secret.** ``auth_for`` returns the finished ``(name, value)`` header
pair, so a token never exists as a value anything else can put somewhere. The last class
asserts the negative directly: no plaintext secret appears anywhere in a connection view
or an audit row.

**The view cannot leak.** ``build_connection_views`` reads the connection row and never
joins the credential table, which is the structural reason it is safe — adding a secret
to that payload would take a join somebody would have to write on purpose.

**Revoking leaves nothing.** One ``DELETE``, asserted by counting rows rather than by
checking that the columns are null.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.integrations import (
    AUTH_API_KEY,
    CONNECTION_ACTIVE,
    CONNECTION_NEEDS_REAUTH,
    IntegrationConnection,
    IntegrationCredential,
    IntegrationCredentialEvent,
)
from app.models.user.user import User
from app.services.integrations.connectors.spec import (
    PLACEMENT_HEADER,
    PLACEMENT_QUERY,
    AuthSpec,
    ConnectorSpec,
)
from app.services.integrations.credentials import credential_service
from app.services.integrations.errors import IntegrationFailure
from app.utils.crypto import decrypt_secret

SECRET = "sk-live-abcdef123456"


def connector(**overrides) -> ConnectorSpec:
    defaults = dict(
        connector_id="rest_generic",
        label="REST API",
        base_url_is_user_supplied=True,
        auth=AuthSpec(kind=AUTH_API_KEY, placement=PLACEMENT_HEADER,
                      name="Authorization", value_template="{api_key}"),
    )
    defaults.update(overrides)
    return ConnectorSpec(**defaults).validated()


@pytest.fixture
async def connection(db: AsyncSession, user: User) -> IntegrationConnection:
    row = IntegrationConnection(
        user_id=user.id,
        connector_id="rest_generic",
        label="Billing API",
        auth_kind=AUTH_API_KEY,
        base_url="https://api.example.com",
    )
    db.add(row)
    await db.commit()
    return row


class TestStoring:
    async def test_a_secret_is_encrypted_on_the_way_in(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        """
        The caller passes ``api_key``, not ``api_key_encrypted``. Making them encrypt
        would leave every call site one omission away from writing plaintext into a
        column named for ciphertext.
        """
        credential = await credential_service.store_credential(
            db, connection, api_key=SECRET
        )

        assert credential.api_key_encrypted != SECRET
        assert decrypt_secret(credential.api_key_encrypted) == SECRET

    async def test_storing_twice_replaces_rather_than_duplicating(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        await credential_service.store_credential(db, connection, api_key="first")
        await credential_service.store_credential(db, connection, api_key="second")

        rows = await db.scalar(select(func.count()).select_from(IntegrationCredential))
        assert rows == 1

    async def test_plaintext_fields_are_stored_as_they_are(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        """
        ``client_id`` is public by definition and ``scope`` has to be queryable to
        decide whether a refresh is due. Encrypting them would cost a decrypt on every
        read and buy nothing.
        """
        credential = await credential_service.store_credential(
            db, connection, client_id="app-123", scope="read_orders write_customers"
        )

        assert credential.client_id == "app-123"
        assert credential.scope == "read_orders write_customers"

    async def test_an_unknown_field_is_refused(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        """
        A caller passing ``clientId`` and getting no error would produce a connection
        that cannot authenticate, for no visible reason.
        """
        with pytest.raises(IntegrationFailure, match="clientId"):
            await credential_service.store_credential(db, connection, clientId="app-123")

    async def test_supplying_a_credential_clears_needs_reauth(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        """Leaving the red badge up until the next run happened to succeed would make
        the Reconnect button look like it did nothing."""
        connection.status = CONNECTION_NEEDS_REAUTH
        await db.commit()

        await credential_service.store_credential(db, connection, api_key=SECRET)

        assert connection.status == CONNECTION_ACTIVE

    async def test_it_writes_an_audit_event(
        self, db: AsyncSession, connection: IntegrationConnection, user: User
    ) -> None:
        await credential_service.store_credential(
            db, connection, api_key=SECRET, user_id=user.id
        )

        event = await db.scalar(select(IntegrationCredentialEvent))
        assert event.event == "connected"
        assert event.user_id == user.id


class TestRevoking:
    async def test_it_leaves_nothing(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        """
        Counted rather than checked for nulls. "Null the columns" leaves whatever a
        partial write or an earlier migration put there.
        """
        await credential_service.store_credential(db, connection, api_key=SECRET)

        await credential_service.revoke(db, connection)

        rows = await db.scalar(select(func.count()).select_from(IntegrationCredential))
        assert rows == 0

    async def test_it_marks_the_connection(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        await credential_service.store_credential(db, connection, api_key=SECRET)

        await credential_service.revoke(db, connection)

        assert connection.status == "revoked"

    async def test_the_audit_event_survives_the_delete(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        """
        Written before, in the same transaction. Written after, a failure between the
        two leaves a connection with no credential and no record of why — the state
        somebody investigating an outage least wants to find.
        """
        await credential_service.store_credential(db, connection, api_key=SECRET)

        await credential_service.revoke(db, connection, reason="user asked")

        events = (await db.execute(select(IntegrationCredentialEvent))).scalars().all()
        assert "revoked" in {event.event for event in events}


class TestAuthFor:
    async def test_it_returns_a_finished_header(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        await credential_service.store_credential(db, connection, api_key=SECRET)

        header, query = await credential_service.auth_for(db, connection, connector())

        assert header == ("Authorization", SECRET)
        assert query is None

    async def test_the_connectors_template_shapes_it(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        """
        An API wanting ``X-Api-Key: abc`` and one wanting ``Authorization: Bearer abc``
        are the same connection with different strings.
        """
        await credential_service.store_credential(db, connection, api_key=SECRET)
        spec = connector(
            auth=AuthSpec(kind=AUTH_API_KEY, name="X-Api-Key", value_template="Bearer {api_key}")
        )

        header, _ = await credential_service.auth_for(db, connection, spec)

        assert header == ("X-Api-Key", f"Bearer {SECRET}")

    async def test_a_query_placement_returns_a_query_pair(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        await credential_service.store_credential(db, connection, api_key=SECRET)
        spec = connector(
            auth=AuthSpec(kind=AUTH_API_KEY, placement=PLACEMENT_QUERY,
                          name="api_key", value_template="{api_key}")
        )

        header, query = await credential_service.auth_for(db, connection, spec)

        assert header is None
        assert query == ("api_key", SECRET)

    async def test_a_connector_needing_nothing_returns_nothing(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        spec = connector(auth=AuthSpec(kind="none"))

        assert await credential_service.auth_for(db, connection, spec) == (None, None)

    async def test_basic_auth_is_encoded(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        import base64

        await credential_service.store_credential(
            db, connection, username="jane", password="hunter2"
        )
        spec = connector(
            auth=AuthSpec(kind="basic", name="Authorization", value_template="Basic {basic}")
        )

        header, _ = await credential_service.auth_for(db, connection, spec)

        assert header[1] == "Basic " + base64.b64encode(b"jane:hunter2").decode()

    async def test_no_credential_at_all_says_what_to_do(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        with pytest.raises(IntegrationFailure, match="no saved credentials"):
            await credential_service.auth_for(db, connection, connector())

    async def test_a_missing_field_names_the_connection(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        await credential_service.store_credential(db, connection, client_id="x")

        with pytest.raises(IntegrationFailure, match="Billing API"):
            await credential_service.auth_for(db, connection, connector())

    async def test_a_credential_that_cannot_be_read_says_re_enter(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        """
        Almost always ``FERNET_KEY`` changed without ``FERNET_KEY_OLD``. The person
        seeing this is the connection's owner, not the operator, so the message says
        re-enter rather than naming an environment variable.
        """
        credential = await credential_service.store_credential(
            db, connection, api_key=SECRET
        )
        credential.api_key_encrypted = "not-a-real-token"
        await db.commit()

        with pytest.raises(IntegrationFailure, match="enter them again"):
            await credential_service.auth_for(db, connection, connector())

    async def test_a_template_that_names_no_credential_is_refused(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        """It would send a request with no credential in it and get a 401 that reads
        like a bad key."""
        await credential_service.store_credential(db, connection, api_key=SECRET)
        spec = connector(auth=AuthSpec(kind=AUTH_API_KEY, value_template="Bearer"))

        with pytest.raises(IntegrationFailure, match="nowhere to put"):
            await credential_service.auth_for(db, connection, spec)


class TestMasking:
    def test_it_shows_enough_to_recognise_and_no_more(self) -> None:
        assert credential_service.mask_secret("sk-live-abcdef123456") == "••••••••3456"

    @pytest.mark.parametrize("short", ["", "a", "abcd"])
    def test_something_too_short_is_masked_entirely(self, short: str) -> None:
        masked = credential_service.mask_secret(short)

        assert set(masked) <= {"*"}
        assert len(masked) == len(short)

    async def test_the_owner_sees_masked_secrets_not_plain_ones(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        """
        Unlike a chatbot action's headers, which come back to be edited as a whole.
        A connection's key is either kept or replaced, so no workflow here needs the
        plaintext on screen.
        """
        await credential_service.store_credential(db, connection, api_key=SECRET)

        revealed = await credential_service.reveal_for_owner(db, connection)

        assert revealed == {"api_key": "••••••••3456"}
        assert SECRET not in str(revealed)

    async def test_an_unreadable_secret_says_so_rather_than_looking_absent(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        """An empty field reads as "there is nothing saved" and invites a duplicate."""
        credential = await credential_service.store_credential(
            db, connection, api_key=SECRET
        )
        credential.api_key_encrypted = "not-a-real-token"
        await db.commit()

        revealed = await credential_service.reveal_for_owner(db, connection)

        assert revealed["api_key"] == "(cannot be read)"


class TestNothingLeaks:
    """The negative assertions the whole module is arranged around."""

    async def test_a_connection_view_holds_no_secret(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        await credential_service.store_credential(db, connection, api_key=SECRET)

        views = credential_service.build_connection_views([connection])

        assert SECRET not in str(views)
        assert not ({"api_key", "api_key_encrypted", "credential"} & set(views[0]))

    async def test_a_connection_view_holds_no_bigint_id(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        """The house rule: only ``uuid`` ever leaves the module."""
        views = credential_service.build_connection_views([connection])

        assert "id" not in views[0]
        assert views[0]["uuid"] == str(connection.uuid)

    async def test_an_audit_detail_is_redacted_even_when_a_caller_slips(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        """
        Callers are told not to put a secret in ``detail``; this makes it true anyway.
        The cost is one function call and the failure mode is a credential in a table
        built for reading.
        """
        await credential_service.record_event(
            db, connection, "connected", detail={"api_key": SECRET, "shop": "acme"}
        )

        event = await db.scalar(select(IntegrationCredentialEvent))
        assert SECRET not in str(event.detail)
        assert event.detail["shop"] == "acme"

    async def test_a_failing_audit_write_does_not_fail_the_run(
        self, db: AsyncSession, connection: IntegrationConnection, monkeypatch
    ) -> None:
        """A run must not fail because its audit row could not be written, and a lost
        row is visible as a gap where the neighbouring rows are not."""
        async def _boom() -> None:
            raise RuntimeError("database went away")

        monkeypatch.setattr(db, "commit", _boom)

        await credential_service.record_event(db, connection, "connected")
