"""
Tests for ``connection_service.py``.

Three properties, and the first one is the whole reason the credential table is separate.

**No response, view or refusal contains a secret.** Asserted by searching every string a
connection produces for the plaintext key, rather than by reviewing the code that builds
them — a check that survives somebody adding a field later.

**Testing a connection sends something.** A connection that parses is not a connection
that works: the failures worth catching are a key with the wrong scope, a base URL missing
its version segment, and a gateway answering HTML with a 200. ``respx`` is used rather than
a stubbed client for the reason ``test_sender.py`` gives at length — it intercepts at the
*transport* layer, so the egress guard, the redirect refusal, the byte cap and the retry
rules that run in production are the ones running here.

**A blank secret means "leave it alone", not "clear it".** The edit form shows a masked
key, so the field arrives empty on every save where the user did not retype it. Treating
that as a deletion would silently break a working connection every time somebody fixed a
typo in its label — and nothing about it would say so until the next run.
"""

from __future__ import annotations

import socket
from typing import Any, Dict

import httpx
import pytest
import respx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from litestar.exceptions import HTTPException

from app.models.integrations import (
    AUTH_API_KEY,
    CONNECTION_DISABLED,
    CONNECTION_REVOKED,
    OPERATION_READ,
    OPERATION_WRITE,
    IntegrationConnection,
    IntegrationCredential,
    IntegrationRestOperation,
)
from app.models.user.user import User
from app.services.integrations import connection_service
from app.services.integrations.runtime import http_client, rate_limiter

BASE = "https://api.example.com"
SECRET = "sk-live-never-show-this"


@pytest.fixture(autouse=True)
async def clean_runtime(monkeypatch: pytest.MonkeyPatch):
    """
    A fresh client pool and rate limiter per test, and DNS that answers.

    Both are module state: a leaked client holds a transport ``respx`` has stopped mocking,
    and a leaked limiter makes one test's waiting another's.
    """
    import asyncio

    async def _getaddrinfo(host, port, **kwargs):  # noqa: ANN001, ANN003
        return [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", port))
        ]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", _getaddrinfo)

    await http_client.close_all_clients()
    rate_limiter.limiter.reset()

    yield

    await http_client.close_all_clients()
    rate_limiter.limiter.reset()


@pytest.fixture
async def connection(db: AsyncSession, user: User) -> IntegrationConnection:
    return await connection_service.create_connection(
        db, user.id,
        connector_id="rest_generic",
        label="Billing API",
        base_url=BASE,
        api_key=SECRET,
    )


async def add_operation(
    db: AsyncSession, connection: IntegrationConnection, **fields: Any
) -> IntegrationRestOperation:
    defaults: Dict[str, Any] = dict(
        connection_id=connection.id,
        operation_id="list_contacts",
        label="List contacts",
        kind=OPERATION_READ,
        method="GET",
        path="/contacts",
        records_path="data",
    )
    defaults.update(fields)
    row = IntegrationRestOperation(**defaults)
    db.add(row)
    await db.commit()
    return row


def operation_form(**overrides: Any) -> Dict[str, Any]:
    form: Dict[str, Any] = {
        "operation_id": "list_contacts",
        "label": "List contacts",
        "kind": OPERATION_READ,
        "method": "GET",
        "path": "/contacts",
        "records_path": "data",
    }
    form.update(overrides)
    return form


# ---------------------------------------------------------------------------
# Creating
# ---------------------------------------------------------------------------


class TestCreating:
    async def test_a_connection_stores_its_key_encrypted(
        self, db: AsyncSession, user: User, connection: IntegrationConnection
    ) -> None:
        """The column is named for ciphertext and has to hold it. A caller passing
        ``api_key_encrypted`` would be one omission away from plaintext, which is why
        ``store_credential`` owns the encryption and nothing else does."""
        result = await db.execute(
            select(IntegrationCredential).where(
                IntegrationCredential.connection_id == connection.id
            )
        )
        credential = result.scalar_one()

        assert credential.api_key_encrypted
        assert SECRET not in credential.api_key_encrypted

    async def test_the_auth_kind_comes_from_the_connector_not_the_caller(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        """A connection claiming 'none' against a connector that needs a key would send
        unauthenticated requests and get a 401 that reads like a bad key."""
        assert connection.auth_kind == AUTH_API_KEY

    async def test_an_unknown_connector_is_refused(
        self, db: AsyncSession, user: User
    ) -> None:
        with pytest.raises(HTTPException) as caught:
            await connection_service.create_connection(
                db, user.id, connector_id="netsuite", label="ERP", base_url=BASE,
            )

        assert caught.value.status_code == 400

    async def test_a_base_url_is_required_when_the_connector_asks_for_one(
        self, db: AsyncSession, user: User
    ) -> None:
        with pytest.raises(HTTPException) as caught:
            await connection_service.create_connection(
                db, user.id, connector_id="rest_generic", label="No address",
            )

        assert "address" in caught.value.detail

    async def test_a_plain_http_base_url_is_refused(
        self, db: AsyncSession, user: User
    ) -> None:
        """The shape is checked here; whether the *address* is reachable is checked at
        send time against the resolved IP, because that answer expires."""
        with pytest.raises(HTTPException) as caught:
            await connection_service.create_connection(
                db, user.id, connector_id="rest_generic", label="Insecure",
                base_url="http://api.example.com",
            )

        assert "https" in caught.value.detail

    async def test_a_url_carrying_credentials_is_refused(
        self, db: AsyncSession, user: User
    ) -> None:
        with pytest.raises(HTTPException) as caught:
            await connection_service.create_connection(
                db, user.id, connector_id="rest_generic", label="Inline creds",
                base_url="https://user:pass@api.example.com",
            )

        assert "username or password" in caught.value.detail

    async def test_two_connections_to_the_same_account_are_refused(
        self, db: AsyncSession, user: User
    ) -> None:
        await connection_service.create_connection(
            db, user.id, connector_id="rest_generic", label="Store one",
            base_url=BASE, external_account_id="acme.example.com",
        )

        with pytest.raises(HTTPException) as caught:
            await connection_service.create_connection(
                db, user.id, connector_id="rest_generic", label="Store one again",
                base_url=BASE, external_account_id="acme.example.com",
            )

        assert "Store one" in caught.value.detail

    async def test_several_connections_with_no_account_coexist(
        self, db: AsyncSession, user: User
    ) -> None:
        """
        **Many connections per connector is the point** — and a generic REST connection has
        no account concept at all, so several under one connector is correct rather than a
        loophole. This is the deliberate departure from ``ai_api_keys``' one-active-per-
        provider rule.
        """
        await connection_service.create_connection(
            db, user.id, connector_id="rest_generic", label="Billing", base_url=BASE,
        )
        await connection_service.create_connection(
            db, user.id, connector_id="rest_generic", label="Shipping", base_url=BASE,
        )

        assert len(await connection_service.list_connections(db, user.id)) == 2


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------


class TestSecretsNeverLeak:
    async def test_no_view_contains_the_key(
        self, db: AsyncSession, user: User, connection: IntegrationConnection
    ) -> None:
        """
        Searched rather than reviewed. A test that checked for the absence of a named field
        would pass the day somebody adds a differently-named one.
        """
        views = connection_service.build_views([connection])

        assert SECRET not in repr(views)

    async def test_no_test_result_contains_the_key(
        self, db: AsyncSession, user: User, connection: IntegrationConnection
    ) -> None:
        """A vendor's error body can echo the credential that was sent to it, which is why
        the result carries a status and a sentence and never the body."""
        await add_operation(db, connection)

        with respx.mock(assert_all_called=False) as router:
            router.get(f"{BASE}/contacts").mock(
                return_value=httpx.Response(
                    401, json={"error": f"invalid key {SECRET}"}
                )
            )
            result = await connection_service.test_connection(db, user.id, connection.uuid)

        assert result["ok"] is False
        assert SECRET not in repr(result)

    async def test_revoking_leaves_no_credential_row(
        self, db: AsyncSession, user: User, connection: IntegrationConnection
    ) -> None:
        """
        One row behind one foreign key is what makes revoking provably complete: it is a
        single ``DELETE``, not a hand-written cleanup that forgets a column.
        """
        await connection_service.revoke_connection(db, user.id, connection.uuid)

        remaining = await db.execute(
            select(func.count()).select_from(IntegrationCredential).where(
                IntegrationCredential.connection_id == connection.id
            )
        )
        assert int(remaining.scalar() or 0) == 0

    async def test_the_connection_survives_being_revoked(
        self, db: AsyncSession, user: User, connection: IntegrationConnection
    ) -> None:
        """
        Deliberate. Workflows point at it by uuid, and deleting it would turn 'reconnect
        this' into a step whose connection no longer exists.
        """
        revoked = await connection_service.revoke_connection(db, user.id, connection.uuid)

        assert revoked.status == CONNECTION_REVOKED


# ---------------------------------------------------------------------------
# Editing
# ---------------------------------------------------------------------------


class TestEditing:
    async def test_a_blank_key_leaves_the_stored_one_alone(
        self, db: AsyncSession, user: User, connection: IntegrationConnection
    ) -> None:
        """
        **The property that keeps working connections working.** The edit form shows a
        masked key, so this field arrives empty on every save where the user only changed
        the label.
        """
        await connection_service.update_connection(
            db, user.id, connection.uuid, label="Billing API (EU)", api_key="",
        )

        result = await db.execute(
            select(IntegrationCredential).where(
                IntegrationCredential.connection_id == connection.id
            )
        )
        assert result.scalar_one().api_key_encrypted is not None

    async def test_a_new_key_replaces_the_old_one(
        self, db: AsyncSession, user: User, connection: IntegrationConnection
    ) -> None:
        result = await db.execute(
            select(IntegrationCredential).where(
                IntegrationCredential.connection_id == connection.id
            )
        )
        before = result.scalar_one().api_key_encrypted

        await connection_service.update_connection(
            db, user.id, connection.uuid, label="Billing API", api_key="sk-rotated",
        )

        await db.refresh(connection)
        result = await db.execute(
            select(IntegrationCredential).where(
                IntegrationCredential.connection_id == connection.id
            )
        )
        assert result.scalar_one().api_key_encrypted != before

    async def test_switching_off_disables_rather_than_revoking(
        self, db: AsyncSession, user: User, connection: IntegrationConnection
    ) -> None:
        """
        'Parked by me' and 'revoked by the vendor' both stop a connection working, and only
        the first comes back by pressing a button. Conflating them makes the connections
        page unable to say which happened.
        """
        parked = await connection_service.set_connection_active(
            db, user.id, connection.uuid, False
        )

        assert parked.status == CONNECTION_DISABLED
        assert parked.is_active is False

    async def test_switching_a_revoked_connection_on_does_not_mark_it_active(
        self, db: AsyncSession, user: User, connection: IntegrationConnection
    ) -> None:
        """A revoked connection has no credential. Flipping its status green would put a
        working badge on something that 401s at the first request."""
        await connection_service.revoke_connection(db, user.id, connection.uuid)

        back = await connection_service.set_connection_active(
            db, user.id, connection.uuid, True
        )

        assert back.status == CONNECTION_REVOKED

    async def test_another_users_connection_is_not_found(
        self, db: AsyncSession, make_user, connection: IntegrationConnection
    ) -> None:
        other = await make_user(email="other@example.com")

        with pytest.raises(HTTPException) as caught:
            await connection_service.get_connection(db, other.id, connection.uuid)

        assert caught.value.status_code == 404


# ---------------------------------------------------------------------------
# Testing a connection
# ---------------------------------------------------------------------------


class TestTestConnection:
    async def test_a_working_connection_reports_the_record_count(
        self, db: AsyncSession, user: User, connection: IntegrationConnection
    ) -> None:
        await add_operation(db, connection)

        with respx.mock as router:
            router.get(f"{BASE}/contacts").mock(
                return_value=httpx.Response(200, json={"data": [{"id": 1}, {"id": 2}]})
            )
            result = await connection_service.test_connection(db, user.id, connection.uuid)

        assert result["ok"] is True
        assert result["record_count"] == 2

    async def test_it_actually_sends_a_request(
        self, db: AsyncSession, user: User, connection: IntegrationConnection
    ) -> None:
        """
        The point of the whole function. A "test" that parsed the URL and returned green
        would pass for a key with the wrong scope, a base URL missing its version segment
        and a WAF challenge — every failure worth catching.
        """
        await add_operation(db, connection)

        with respx.mock as router:
            route = router.get(f"{BASE}/contacts").mock(
                return_value=httpx.Response(200, json={"data": []})
            )
            await connection_service.test_connection(db, user.id, connection.uuid)

        assert route.call_count == 1

    async def test_the_credential_is_on_the_request(
        self, db: AsyncSession, user: User, connection: IntegrationConnection
    ) -> None:
        """A test that sent no credential would pass against an endpoint that needs one and
        then the first real run would 401."""
        await add_operation(db, connection)

        with respx.mock as router:
            route = router.get(f"{BASE}/contacts").mock(
                return_value=httpx.Response(200, json={"data": []})
            )
            await connection_service.test_connection(db, user.id, connection.uuid)

        assert SECRET in route.calls[0].request.headers.get("Authorization", "")

    async def test_a_401_is_an_answer_not_an_exception(
        self, db: AsyncSession, user: User, connection: IntegrationConnection
    ) -> None:
        """
        The page needs to show it next to a Reconnect button. Raising would make a
        perfectly informative outcome into a 500.
        """
        await add_operation(db, connection)

        with respx.mock as router:
            router.get(f"{BASE}/contacts").mock(return_value=httpx.Response(401))
            result = await connection_service.test_connection(db, user.id, connection.uuid)

        assert result["ok"] is False
        assert result["status_code"] == 401

    async def test_html_with_a_200_is_a_failure(
        self, db: AsyncSession, user: User, connection: IntegrationConnection
    ) -> None:
        """
        What a WAF challenge and an expired-session redirect look like. Parsing it as an
        empty list would report "0 records" as success, which is the worst available
        outcome: it looks like the connection works and the data is gone.
        """
        await add_operation(db, connection)

        with respx.mock as router:
            router.get(f"{BASE}/contacts").mock(
                return_value=httpx.Response(
                    200, text="<html>Checking your browser</html>",
                    headers={"content-type": "text/html"},
                )
            )
            result = await connection_service.test_connection(db, user.id, connection.uuid)

        assert result["ok"] is False

    async def test_a_connection_with_no_operations_says_so(
        self, db: AsyncSession, user: User, connection: IntegrationConnection
    ) -> None:
        """Rather than a green tick that proves nothing, which is what a test that skipped
        the request would amount to."""
        with pytest.raises(HTTPException) as caught:
            await connection_service.test_connection(db, user.id, connection.uuid)

        assert "no read operation" in caught.value.detail

    async def test_a_write_only_connection_is_not_tested_by_writing(
        self, db: AsyncSession, user: User, connection: IntegrationConnection
    ) -> None:
        """A write test would create a record in somebody's production system to prove it
        could."""
        await add_operation(
            db, connection, operation_id="create_contact", kind=OPERATION_WRITE,
            method="POST", label="Create contact",
        )

        with pytest.raises(HTTPException) as caught:
            await connection_service.test_connection(db, user.id, connection.uuid)

        assert "no read operation" in caught.value.detail

    async def test_a_disabled_connection_is_refused_before_the_request(
        self, db: AsyncSession, user: User, connection: IntegrationConnection
    ) -> None:
        """The refusal names the connection and says what to do, instead of surfacing as a
        401 from somebody else's server."""
        await add_operation(db, connection)
        await connection_service.set_connection_active(db, user.id, connection.uuid, False)

        with respx.mock(assert_all_called=False) as router:
            route = router.get(f"{BASE}/contacts").mock(
                return_value=httpx.Response(200, json={"data": []})
            )
            result = await connection_service.test_connection(db, user.id, connection.uuid)

        assert result["ok"] is False
        assert route.call_count == 0


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


class TestOperations:
    async def test_an_operation_that_saves_is_one_that_loads(
        self, db: AsyncSession, user: User, connection: IntegrationConnection
    ) -> None:
        """
        Validated by turning it into an ``OperationSpec`` — the same ``load_operation``
        ``resolve_operation`` calls at run time. Two sets of rules is how the form and the
        runtime drift, and the drift shows up as a workflow that saved fine and fails at
        3am.
        """
        await connection_service.save_operation(db, user.id, connection.uuid, operation_form())

        listed = await connection_service.list_operations(db, user.id, connection.uuid)

        assert [op["operation_id"] for op in listed] == ["list_contacts"]

    async def test_saving_the_same_id_replaces_rather_than_duplicates(
        self, db: AsyncSession, user: User, connection: IntegrationConnection
    ) -> None:
        """Matching the unique constraint. An insert-only version would leave the picker
        showing the same operation twice with different definitions."""
        await connection_service.save_operation(db, user.id, connection.uuid, operation_form())
        await connection_service.save_operation(
            db, user.id, connection.uuid, operation_form(label="Contacts, all of them"),
        )

        listed = await connection_service.list_operations(db, user.id, connection.uuid)

        assert len(listed) == 1
        assert listed[0]["label"] == "Contacts, all of them"

    async def test_an_omitted_field_is_cleared_not_carried_over(
        self, db: AsyncSession, user: User, connection: IntegrationConnection
    ) -> None:
        """
        An update that kept stale halves of two different definitions would be a request
        nobody could reproduce from the form that produced it.
        """
        await connection_service.save_operation(
            db, user.id, connection.uuid, operation_form(records_path="data.items"),
        )
        await connection_service.save_operation(
            db, user.id, connection.uuid, operation_form(records_path=""),
        )

        result = await db.execute(
            select(IntegrationRestOperation).where(
                IntegrationRestOperation.connection_id == connection.id
            )
        )
        assert result.scalar_one().records_path is None

    async def test_a_lowercase_method_is_stored_upper(
        self, db: AsyncSession, user: User, connection: IntegrationConnection
    ) -> None:
        """
        The verb decides whether the retry rules treat this as a write. A ``post`` that
        failed that comparison would be retried after a timeout — which is how a timed-out
        order becomes two orders.
        """
        await connection_service.save_operation(
            db, user.id, connection.uuid,
            operation_form(operation_id="create_contact", kind=OPERATION_WRITE, method="post"),
        )

        result = await db.execute(
            select(IntegrationRestOperation).where(
                IntegrationRestOperation.operation_id == "create_contact"
            )
        )
        assert result.scalar_one().method == "POST"

    async def test_an_unusable_operation_id_is_refused_by_name(
        self, db: AsyncSession, user: User, connection: IntegrationConnection
    ) -> None:
        """It goes into a step's ``operation_id``, into log lines and into the AI
        catalogue. Somewhere in that chain a slash stops being decorative."""
        with pytest.raises(HTTPException) as caught:
            await connection_service.save_operation(
                db, user.id, connection.uuid, operation_form(operation_id="list/contacts"),
            )

        assert "list/contacts" in caught.value.detail

    async def test_an_unknown_method_is_refused(
        self, db: AsyncSession, user: User, connection: IntegrationConnection
    ) -> None:
        with pytest.raises(HTTPException):
            await connection_service.save_operation(
                db, user.id, connection.uuid, operation_form(method="FETCH"),
            )

    async def test_a_malformed_field_is_refused_rather_than_dropped(
        self, db: AsyncSession, user: User, connection: IntegrationConnection
    ) -> None:
        """
        This used to filter non-mappings out. A form with one bad row saved green and lost
        that field, and the first anybody heard of it was a mapping panel that would not
        offer a field they had just declared — which reads as the panel being broken.

        ``OperationSaveRequest`` refuses the same input at the boundary with the entry
        number in it; this is the guarantee for the callers that never go through a form.
        """
        with pytest.raises(HTTPException) as caught:
            await connection_service.save_operation(
                db, user.id, connection.uuid,
                operation_form(inputs=[{"name": "email"}, "oops"]),
            )

        assert "Field 2" in caught.value.detail

    async def test_a_timeout_longer_than_a_node_may_run_is_refused(
        self, db: AsyncSession, user: User, connection: IntegrationConnection
    ) -> None:
        """``run_node`` caps a node at an hour. An operation asking for longer would be
        silently overruled there rather than refused here."""
        with pytest.raises(HTTPException):
            await connection_service.save_operation(
                db, user.id, connection.uuid, operation_form(timeout_seconds=7200),
            )

    async def test_deleting_removes_it_from_the_list(
        self, db: AsyncSession, user: User, connection: IntegrationConnection
    ) -> None:
        await connection_service.save_operation(db, user.id, connection.uuid, operation_form())

        await connection_service.delete_operation(
            db, user.id, connection.uuid, "list_contacts"
        )

        assert await connection_service.list_operations(db, user.id, connection.uuid) == []


class TestSchema:
    async def test_the_schema_carries_inputs_outputs_and_required(
        self, db: AsyncSession, user: User, connection: IntegrationConnection
    ) -> None:
        """
        ``outputs`` feeds the mapping grid's source column, ``inputs`` its destination
        column, and ``required`` is what the panel marks red and what ``publish_flow``
        refuses. All three from the operation's own declaration, so the picker cannot offer
        a field the request builder would reject.
        """
        await connection_service.save_operation(
            db, user.id, connection.uuid,
            operation_form(
                operation_id="create_contact",
                kind=OPERATION_WRITE,
                method="POST",
                inputs=[
                    {"name": "email", "type": "string", "required": True},
                    {"name": "note", "type": "string", "required": False},
                ],
                outputs=[{"name": "id", "type": "string"}],
            ),
        )

        schema = await connection_service.connection_schema(
            db, user.id, connection.uuid, "create_contact"
        )

        assert [f["name"] for f in schema["inputs"]] == ["email", "note"]
        assert [f["name"] for f in schema["outputs"]] == ["id"]
        assert schema["required"] == ["email"]

    async def test_an_unknown_operation_lists_what_the_connection_does_offer(
        self, db: AsyncSession, user: User, connection: IntegrationConnection
    ) -> None:
        """The registry's own sentence, passed through unchanged rather than replaced with
        a generic one that leaves somebody guessing."""
        await connection_service.save_operation(db, user.id, connection.uuid, operation_form())

        with pytest.raises(HTTPException) as caught:
            await connection_service.connection_schema(
                db, user.id, connection.uuid, "list_invoices"
            )

        assert "list_invoices" in caught.value.detail


# ---------------------------------------------------------------------------
# The escape hatch
# ---------------------------------------------------------------------------


class TestPrivateHosts:
    async def test_a_non_admin_is_refused_in_the_service(
        self, db: AsyncSession, user: User, connection: IntegrationConnection
    ) -> None:
        """
        **In the service, not the route.** A business rule a second route could skip is not
        a rule, and this is the one setting in the module that lets a request reach an
        address inside the network.
        """
        with pytest.raises(HTTPException) as caught:
            await connection_service.set_private_host_access(
                db, user.id, connection.uuid,
                is_admin=False, allow=True,
                hosts=["sap.internal:443"], cidrs=["10.42.0.0/16"],
            )

        assert caught.value.status_code == 403

    async def test_a_generic_rest_connection_can_never_be_aimed_inwards(
        self, db: AsyncSession, user: User, connection: IntegrationConnection
    ) -> None:
        """
        Even for an administrator. The connector's spec has to permit it at all, which is
        ``sap_odata`` and nothing else — so a connection whose base URL the user typed can
        never point inside the network.
        """
        with pytest.raises(HTTPException) as caught:
            await connection_service.set_private_host_access(
                db, user.id, connection.uuid,
                is_admin=True, allow=True,
                hosts=["sap.internal:443"], cidrs=["10.42.0.0/16"],
            )

        assert caught.value.status_code == 400
        assert connection.allow_private_hosts is False


class TestTheAllowlistItself:
    """
    The list rules, tested on the function that holds them.

    Not through ``set_private_host_access``, and that is a finding rather than a
    convenience: the connector gate fires **before** the list is looked at, so with the
    only Phase 1 connector — ``rest_generic``, which never permits private hosts — these
    inputs can never reach the list rules at all. That ordering is correct (refuse the
    whole capability before quibbling with its details) and it means the sole way to pin
    the rules today is to call them directly. When ``sap_odata`` lands in Phase 4 these
    grow an end-to-end sibling; until then, a test asserting the connector refusal would
    be asserting the gate twice and the list never.
    """

    def test_a_wildcard_entry_is_refused(self) -> None:
        """``*.internal`` allows whatever DNS says tomorrow, and the point of the list is
        that somebody chose each entry."""
        with pytest.raises(HTTPException) as caught:
            connection_service._validated_allowlist(["*.internal:443"], ["10.42.0.0/16"])

        assert "wildcard" in caught.value.detail

    def test_a_list_longer_than_the_cap_is_refused(self) -> None:
        """An allow-list long enough to be convenient is one nobody audits."""
        with pytest.raises(HTTPException) as caught:
            connection_service._validated_allowlist(
                [f"host{n}.internal:443" for n in range(20)], ["10.42.0.0/16"]
            )

        assert str(connection_service.MAX_ALLOWLIST_ENTRIES) in caught.value.detail

    def test_entries_are_deduplicated_and_lowercased(self) -> None:
        """Matched case-insensitively at request time, so a list holding both spellings is
        one somebody reviewing it will misread."""
        allowlist = connection_service._validated_allowlist(
            [" SAP.internal:443 ", "sap.internal:443"], ["10.42.0.0/16"]
        )

        assert allowlist["hosts"] == ["sap.internal:443"]

    def test_both_halves_are_required_by_the_caller(self) -> None:
        """
        A host alone falls to a DNS answer the operator does not control; a CIDR alone
        permits any name that happens to resolve into it. The **and** is checked in
        ``set_private_host_access`` rather than here, because an empty list is a legitimate
        intermediate state for a form being filled in.
        """
        assert connection_service._validated_allowlist(["sap.internal:443"], []) == {
            "hosts": ["sap.internal:443"],
            "cidrs": [],
        }


class TestVendorConnectionCreation:
    """
    Creating a connection to a connector that computes its own address.

    Shopify is the first of these, and it inverts the generic-REST form: no base URL, and
    a shop domain instead. The shop domain is the one field that matters, because it
    becomes the host of every request this connection makes — carrying the merchant's
    access token.
    """

    async def test_a_shop_domain_is_stored_and_no_base_url_is(
        self, db: AsyncSession, user: User
    ) -> None:
        connection = await connection_service.create_connection(
            db, user.id,
            connector_id="shopify",
            label="Demo store",
            external_account_id="demo-store.myshopify.com",
            api_key="shpat_0123456789abcdef",
        )

        assert connection.external_account_id == "demo-store.myshopify.com"
        assert connection.base_url is None

    async def test_a_typed_base_url_is_discarded_rather_than_honoured(
        self, db: AsyncSession, user: User
    ) -> None:
        """
        A vendor connector's address is computed. Storing a typed one would be a way to
        point something labelled "Shopify" at another host, so the field is dropped rather
        than trusted — silently, because the form never offers it in the first place.
        """
        connection = await connection_service.create_connection(
            db, user.id,
            connector_id="shopify",
            label="Demo store",
            base_url="https://attacker.example.com",
            external_account_id="demo-store.myshopify.com",
            api_key="shpat_0123456789abcdef",
        )

        assert connection.base_url is None

    @pytest.mark.parametrize(
        "domain",
        [
            "evil.com",
            "demo-store.myshopify.com.evil.com",
            "demo-store.myshopify.com/../admin",
            "DEMO.myshopify.com",
            "demo-store.myshopify.com:8443",
            "",
        ],
    )
    async def test_a_bad_shop_domain_is_refused_at_the_form(
        self, db: AsyncSession, user: User, domain: str
    ) -> None:
        """
        The earlier of the two checks. This one exists so the person typing gets a
        sentence; the connector re-checks the same value before it becomes a hostname, and
        that is the one that is load-bearing.
        """
        with pytest.raises(HTTPException) as caught:
            await connection_service.create_connection(
                db, user.id,
                connector_id="shopify",
                label="Demo store",
                external_account_id=domain,
                api_key="shpat_0123456789abcdef",
            )

        assert caught.value.status_code == 400

    async def test_nothing_is_stored_when_the_domain_is_refused(
        self, db: AsyncSession, user: User
    ) -> None:
        """Refusing after writing the row would leave a connection nobody can use and the
        account-uniqueness check tripping over it."""
        with pytest.raises(HTTPException):
            await connection_service.create_connection(
                db, user.id,
                connector_id="shopify", label="Bad", external_account_id="evil.com",
            )

        assert await connection_service.list_connections(db, user.id) == []

    async def test_the_refusal_says_what_a_good_domain_looks_like(
        self, db: AsyncSession, user: User
    ) -> None:
        with pytest.raises(HTTPException) as caught:
            await connection_service.create_connection(
                db, user.id,
                connector_id="shopify", label="Bad", external_account_id="evil.com",
            )

        assert "your-store.myshopify.com" in caught.value.detail

    async def test_two_connections_to_one_shop_are_refused(
        self, db: AsyncSession, user: User
    ) -> None:
        """What makes reconnecting a shop an update rather than a duplicate."""
        await connection_service.create_connection(
            db, user.id, connector_id="shopify", label="Demo",
            external_account_id="demo-store.myshopify.com", api_key="shpat_a",
        )

        with pytest.raises(HTTPException):
            await connection_service.create_connection(
                db, user.id, connector_id="shopify", label="Demo again",
                external_account_id="demo-store.myshopify.com", api_key="shpat_b",
            )

    async def test_two_different_shops_coexist(
        self, db: AsyncSession, user: User
    ) -> None:
        """Three Shopify stores under one account is the whole point of the design."""
        await connection_service.create_connection(
            db, user.id, connector_id="shopify", label="EU",
            external_account_id="eu-store.myshopify.com", api_key="shpat_a",
        )
        await connection_service.create_connection(
            db, user.id, connector_id="shopify", label="US",
            external_account_id="us-store.myshopify.com", api_key="shpat_b",
        )

        assert len(await connection_service.list_connections(db, user.id)) == 2

    async def test_its_operations_come_from_the_spec_not_the_database(
        self, db: AsyncSession, user: User
    ) -> None:
        connection = await connection_service.create_connection(
            db, user.id, connector_id="shopify", label="Demo",
            external_account_id="demo-store.myshopify.com", api_key="shpat_a",
        )

        operations = await connection_service.list_operations(db, user.id, connection.uuid)

        assert {op["operation_id"] for op in operations} == {
            "orders", "products", "customers"
        }
        assert all(op["kind"] == "read" for op in operations)
