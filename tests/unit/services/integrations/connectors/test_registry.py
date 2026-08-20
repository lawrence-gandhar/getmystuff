"""
Tests for ``connectors/registry.py`` and the generic REST connector.

``resolve_operation`` holds the one branch in the connector layer — user-defined
operations come from ``integration_rest_operations`` rows, declared ones from the
connector's own module — and the assertion that matters is that the **caller cannot
tell**. The two paths return the same frozen dataclass, so a vendor connector and a
form-authored one go through the same request builder, pagination and retry code.

The generic REST connector's own tests are mostly about what it refuses to be. It is the
one connector whose base URL a user types, which makes ``allows_private_hosts = False``
a security property rather than a default: a typed base URL plus a private-host
allowance is a server-side request forgery with a Save button.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.integrations import IntegrationConnection, IntegrationRestOperation
from app.models.user.user import User
from app.services.integrations.connectors import registry
from app.services.integrations.connectors.rest_generic.connector import CONNECTOR_ID, SPEC
from app.services.integrations.connectors.spec import (
    ConnectorSpec,
    OperationSpec,
    PageRule,
)
from app.services.integrations.errors import IntegrationFailure


@pytest.fixture
async def connection(db: AsyncSession, user: User) -> IntegrationConnection:
    row = IntegrationConnection(
        user_id=user.id,
        connector_id=CONNECTOR_ID,
        label="Billing API",
        base_url="https://api.example.com",
    )
    db.add(row)
    await db.commit()
    return row


@pytest.fixture
async def operation_row(
    db: AsyncSession, connection: IntegrationConnection
) -> IntegrationRestOperation:
    row = IntegrationRestOperation(
        connection_id=connection.id,
        operation_id="list_invoices",
        label="List invoices",
        kind="read",
        method="GET",
        path="/invoices",
        records_path="data",
        page_rule={"kind": "page_number", "param": "page", "start_at": 1},
        outputs=[{"name": "total", "type": "number", "path": "amount_due"}],
    )
    db.add(row)
    await db.commit()
    return row


class TestWhatIsRegistered:
    def test_generic_rest_is_there(self) -> None:
        assert CONNECTOR_ID in registry.connector_ids()
        assert registry.get(CONNECTOR_ID) is SPEC

    def test_an_unknown_id_is_none(self) -> None:
        assert registry.get("stripe") is None

    def test_require_names_what_is_available(self) -> None:
        """
        A connection can outlive the connector it names — a spec removed in a later
        version, a database restored from elsewhere — so the person reading this needs
        to know which connection to fix, not a ``KeyError``.
        """
        with pytest.raises(IntegrationFailure) as caught:
            registry.require("stripe")

        assert "stripe" in str(caught.value)
        assert CONNECTOR_ID in str(caught.value)

    def test_registering_the_same_id_twice_is_refused(self) -> None:
        """A connection stores that id and nothing else, so a duplicate is ambiguous."""
        with pytest.raises(ValueError, match="Two connectors"):
            registry.register(
                ConnectorSpec(
                    connector_id=CONNECTOR_ID,
                    label="Something else",
                    base_url_template="https://x.example.com",
                )
            )

    def test_a_malformed_connector_is_refused_at_registration(self) -> None:
        """
        Validated here rather than at first use. A malformed spec is a programming error
        in this repository, and the moment to find out is at import — not when
        somebody's scheduled sync fires at three in the morning.
        """
        with pytest.raises(ValueError):
            registry.register(ConnectorSpec(connector_id="broken", label="Broken"))

    def test_registering_a_broken_connector_does_not_leave_it_behind(self) -> None:
        assert registry.get("broken") is None


class TestDescribeConnectors:
    def test_it_carries_what_the_connections_page_needs(self) -> None:
        described = next(
            entry
            for entry in registry.describe_connectors()
            if entry["connector_id"] == CONNECTOR_ID
        )

        assert described["auth_kind"] == "api_key"
        assert described["asks_for_base_url"] is True
        assert described["operations_are_user_defined"] is True

    def test_it_leaks_no_urls_or_auth_templates(self) -> None:
        """
        This payload reaches a browser. A base URL template in it is an internal
        endpoint in somebody's devtools.
        """
        for entry in registry.describe_connectors():
            assert not (
                {"base_url_template", "value_template", "token_url", "authorize_url"}
                & set(entry)
            )

    def test_it_is_json_serialisable(self) -> None:
        import json

        json.dumps(registry.describe_connectors())


class TestResolveOperation:
    """
    The one branch in the module. See the module docstring.
    """

    async def test_a_user_defined_operation_comes_from_its_row(
        self, db: AsyncSession, connection: IntegrationConnection, operation_row
    ) -> None:
        spec, operation = await registry.resolve_operation(
            db, connection, "list_invoices"
        )

        assert spec.connector_id == CONNECTOR_ID
        assert isinstance(operation, OperationSpec)
        assert operation.path == "/invoices"
        assert operation.page_rule.kind == "page_number"
        assert operation.outputs[0].path == "amount_due"

    async def test_it_returns_the_same_type_a_declared_connector_would(
        self, db: AsyncSession, connection: IntegrationConnection, operation_row
    ) -> None:
        """
        The assertion the whole design rests on: whatever comes back is an
        ``OperationSpec``, so the request builder cannot behave differently for a
        form-authored operation than for a vendor's.
        """
        _, from_row = await registry.resolve_operation(db, connection, "list_invoices")

        declared = OperationSpec(
            operation_id="list_invoices",
            label="List invoices",
            kind="read",
            method="GET",
            path="/invoices",
            records_path="data",
            page_rule=PageRule(kind="page_number", param="page", start_at=1),
        )

        assert from_row.canonical() == declared.canonical()
        assert from_row.fingerprint() == declared.fingerprint()

    async def test_an_operation_on_another_connection_is_not_found(
        self, db: AsyncSession, user: User, operation_row
    ) -> None:
        """
        Operations are scoped to the connection that owns them. Without this, an id
        somebody chose on one connection would silently resolve against another's
        credentials.
        """
        other = IntegrationConnection(
            user_id=user.id,
            connector_id=CONNECTOR_ID,
            label="Warehouse API",
            base_url="https://warehouse.example.com",
        )
        db.add(other)
        await db.commit()

        with pytest.raises(IntegrationFailure, match="no operation called"):
            await registry.resolve_operation(db, other, "list_invoices")

    async def test_a_missing_operation_says_what_to_do(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        """
        The likely cause is that it was renamed since the workflow was drawn, so the
        message says that rather than merely reporting an absence.
        """
        with pytest.raises(IntegrationFailure) as caught:
            await registry.resolve_operation(db, connection, "list_invoices")

        assert "renamed or removed" in str(caught.value)

    async def test_a_step_naming_no_operation(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        with pytest.raises(IntegrationFailure, match="which operation"):
            await registry.resolve_operation(db, connection, "")

    async def test_a_connection_whose_connector_is_gone(
        self, db: AsyncSession, user: User
    ) -> None:
        orphan = IntegrationConnection(
            user_id=user.id, connector_id="stripe", label="Stripe", base_url="https://x"
        )
        db.add(orphan)
        await db.commit()

        with pytest.raises(IntegrationFailure, match="not available in this version"):
            await registry.resolve_operation(db, orphan, "charge")


class TestTheGenericRestConnector:
    def test_it_declares_no_operations(self) -> None:
        """The user writes them, so there are none to declare."""
        assert SPEC.operations == ()
        assert SPEC.operations_are_user_defined is True

    def test_it_asks_for_a_base_url(self) -> None:
        assert SPEC.base_url_is_user_supplied is True
        assert SPEC.base_url_template == ""

    def test_it_can_never_reach_a_private_host(self) -> None:
        """
        The security property, pinned. This is the one connector whose base URL a user
        types; combining that with a private-host allowance would be a form field that
        reaches inside the network. The on-premise escape hatch belongs to
        ``sap_odata``, whose base URL nobody types.
        """
        assert SPEC.allows_private_hosts is False

    def test_it_requires_https(self) -> None:
        assert SPEC.requires_https is True

    def test_the_api_key_goes_in_a_header_the_user_names(self) -> None:
        """
        ``{api_key}`` rather than ``Bearer {api_key}``: an API wanting
        ``X-Api-Key: abc`` and one wanting ``Authorization: Bearer abc`` are the same
        connection with different strings, and hard-coding the prefix would make the
        second impossible to express.
        """
        assert SPEC.auth.kind == "api_key"
        assert SPEC.auth.placement == "header"
        assert SPEC.auth.value_template == "{api_key}"

    def test_it_has_a_conservative_rate_limit(self) -> None:
        """We know nothing about the far end, so the default must not trip a limit
        nobody told us about."""
        assert SPEC.rate_limits.requests_per_second <= 5

    def test_it_validates(self) -> None:
        SPEC.validated()
