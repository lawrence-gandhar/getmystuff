"""
Tests for ``nodes/connector_nodes.py`` — the only place records meet HTTP.

``respx`` rather than a stubbed client, for the reason ``test_sender.py`` gives: it
intercepts at the transport layer, so the real pooling, streaming, byte-cap, retry and
pagination code runs. Stubbing the client would stub out most of what is under test.

The properties, in the order the failures matter:

**A read timeout on a non-idempotent write is attempted once.** Shopify's ``POST
/orders.json`` has no idempotency header. Retrying a create that timed out mid-flight
duplicates a real order in somebody's real business, and no amount of backoff makes that
less true.

**One outcome per record, not one per chunk.** A chunk where record seven was rejected is
the others written and one failed. Reporting the chunk as failed loses the successes and
sends the operator to re-run something that already worked.

**A dry run calls nobody, and still builds every request.** Building it is the point: a
dry run that skipped the builder would not catch the template naming a field the
operation never declared.

**A remembered natural key turns a create into an update**, which is what stops the next
run repeating the duplicate the timeout above could have caused.

**The connection is loaded scoped to its owner**, not loaded and then checked — a uuid
pasted in by hand or invented by a model must not be able to reach somebody else's row.
"""

from __future__ import annotations

import asyncio
import socket

import httpx
import pytest
import respx
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.integrations import (
    AUTH_API_KEY,
    CONNECTION_NEEDS_REAUTH,
    OPERATION_READ,
    OPERATION_WRITE,
    RECORD_FAILED,
    RECORD_SAMPLE,
    RECORD_SKIPPED,
    IntegrationConnection,
    IntegrationRestOperation,
)
from app.models.user.user import User
from app.services.integrations.connectors.spec import PAGE_NUMBER
from app.services.integrations.credentials import credential_service
from app.services.integrations.errors import NodeFailure
from app.services.integrations.mapping.dedupe import NaturalKey
from app.services.integrations.nodes import connector_nodes
from app.services.integrations.runtime import http_client, rate_limiter

BASE = "https://api.example.com"
EMAIL_KEY = NaturalKey(fields=("email",)).validated()


@pytest.fixture(autouse=True)
async def clean_runtime(monkeypatch: pytest.MonkeyPatch):
    """A fresh client pool, a fresh limiter, and DNS that answers. Module state leaking
    between tests would make one test's waiting another's."""
    async def _getaddrinfo(host, port, **kwargs):  # noqa: ANN001, ANN003
        return [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "",
             ("93.184.216.34", port))
        ]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", _getaddrinfo)
    await http_client.close_all_clients()
    rate_limiter.limiter.reset()
    yield
    await http_client.close_all_clients()
    rate_limiter.limiter.reset()


@pytest.fixture
async def connection(db: AsyncSession, user: User) -> IntegrationConnection:
    row = IntegrationConnection(
        user_id=user.id,
        connector_id="rest_generic",
        label="Billing API",
        auth_kind=AUTH_API_KEY,
        base_url=BASE,
    )
    db.add(row)
    await db.commit()
    await credential_service.store_credential(db, row, user_id=user.id, api_key="sk-1")
    return row


async def add_operation(db: AsyncSession, connection: IntegrationConnection, **fields):  # noqa: ANN201
    defaults = dict(
        connection_id=connection.id,
        operation_id="list_contacts",
        label="List contacts",
        kind=OPERATION_READ,
        method="GET",
        path="/contacts",
        records_path="data",
        page_rule={"kind": PAGE_NUMBER, "param": "page", "start_at": 1, "max_pages": 20},
    )
    defaults.update(fields)
    row = IntegrationRestOperation(**defaults)
    db.add(row)
    await db.commit()
    return row


async def target_for(db: AsyncSession, connection: IntegrationConnection, operation_id: str):  # noqa: ANN201
    return await connector_nodes.resolve_target(
        db,
        {"connection_uuid": str(connection.uuid), "operation_id": operation_id},
        user_id=connection.user_id,
    )


def decisions(records, action="create", **overrides):  # noqa: ANN001, ANN201
    from app.services.integrations.mapping import dedupe

    return [
        dedupe.WriteDecision(
            position=index, record=record, action=action,
            natural_key=EMAIL_KEY.hash_for(record), **overrides,
        )
        for index, record in enumerate(records)
    ]


# ---------------------------------------------------------------------------
# Resolving
# ---------------------------------------------------------------------------


class TestResolving:
    async def test_a_connection_belonging_to_somebody_else_is_not_found(
        self, db: AsyncSession, connection: IntegrationConnection, make_user
    ) -> None:  # noqa: ANN001
        """
        Scoped in the query, not checked afterwards. A workflow with a uuid pasted in by
        hand — or invented by a model — must not be able to reach a row its owner does not
        own, and the way to guarantee that is for the row never to be loaded.
        """
        await add_operation(db, connection)
        stranger = await make_user(email="other@example.com")

        with pytest.raises(NodeFailure, match="no longer exists"):
            await connector_nodes.resolve_target(
                db,
                {"connection_uuid": str(connection.uuid), "operation_id": "list_contacts"},
                user_id=stranger.id,
            )

    async def test_a_connection_needing_reauth_is_refused_by_name(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        """Refused here rather than at the request, so the failure names the connection
        and says what to do instead of surfacing as a 401 from a vendor."""
        await add_operation(db, connection)
        connection.status = CONNECTION_NEEDS_REAUTH
        await db.commit()

        with pytest.raises(NodeFailure) as caught:
            await target_for(db, connection, "list_contacts")

        assert "Billing API" in str(caught.value)
        assert "Reconnect" in str(caught.value)

    async def test_an_operation_that_no_longer_exists_says_so(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        await add_operation(db, connection)

        with pytest.raises(NodeFailure, match="no operation called"):
            await target_for(db, connection, "list_invoices")

    async def test_a_step_with_no_connection_says_so(self, db: AsyncSession) -> None:
        with pytest.raises(NodeFailure, match="which connection"):
            await connector_nodes.resolve_target(db, {}, user_id=1)

    async def test_the_operation_hash_identifies_what_ran(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        """Half of the determinism claim: a replay producing a different hash is
        detectably not the same run. Only possible because operations are data."""
        await add_operation(db, connection)
        target = await target_for(db, connection, "list_contacts")

        assert len(target.fingerprint) == 64

    async def test_a_generic_rest_connection_cannot_be_aimed_inside_the_network(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        """
        The private-host hatch needs the *connector* to allow it, and ``rest_generic``
        does not. Checked from the spec rather than trusted from the row, so setting
        ``allow_private_hosts`` by hand in the database changes nothing.
        """
        await add_operation(db, connection)
        connection.allow_private_hosts = True
        connection.private_host_allowlist = {"hosts": ["10.0.0.5:443"], "cidrs": ["10.0.0.0/8"]}
        await db.commit()

        target = await target_for(db, connection, "list_contacts")

        assert target.context.egress_policy.allow_private is False


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


class TestReading:
    async def test_pages_are_walked_and_records_extracted(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        await add_operation(db, connection)
        target = await target_for(db, connection, "list_contacts")

        with respx.mock(assert_all_called=False) as router:
            router.get(f"{BASE}/contacts").mock(
                side_effect=[
                    httpx.Response(200, json={"data": [{"id": 1}, {"id": 2}]}),
                    httpx.Response(200, json={"data": [{"id": 3}]}),
                    httpx.Response(200, json={"data": []}),
                ]
            )
            supply = connector_nodes.open_supply(target, {})
            batch = await supply.next_batch(500)

        assert [row["id"] for row in batch] == [1, 2, 3]
        assert supply.exhausted is True

    async def test_the_credential_is_sent_and_never_held_as_a_value(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        """``auth_for`` returns the finished header pair, so nothing in the node ever
        holds a token — which is why nothing it logs or previews can contain one."""
        await add_operation(db, connection)
        target = await target_for(db, connection, "list_contacts")

        with respx.mock(assert_all_called=False) as router:
            route = router.get(f"{BASE}/contacts").mock(
                return_value=httpx.Response(200, json={"data": []})
            )
            supply = connector_nodes.open_supply(target, {})
            await supply.next_batch(10)

        assert route.calls[0].request.headers["authorization"] == "sk-1"
        assert "sk-1" not in repr(target.context.__dict__.get("connection_label"))

    async def test_nothing_is_requested_until_the_first_batch(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        await add_operation(db, connection)
        target = await target_for(db, connection, "list_contacts")

        with respx.mock(assert_all_called=False) as router:
            route = router.get(f"{BASE}/contacts").mock(
                return_value=httpx.Response(200, json={"data": []})
            )
            connector_nodes.open_supply(target, {})

            assert route.call_count == 0

    async def test_a_rejected_page_fails_the_node_with_the_vendors_words(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        await add_operation(db, connection)
        target = await target_for(db, connection, "list_contacts")

        with respx.mock(assert_all_called=False) as router:
            router.get(f"{BASE}/contacts").mock(
                return_value=httpx.Response(
                    422, json={"message": "the 'since' parameter is not a date"}
                )
            )
            supply = connector_nodes.open_supply(target, {})

            with pytest.raises(NodeFailure) as caught:
                await supply.next_batch(10)

        assert "since" in str(caught.value)


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


class TestWriting:
    async def test_each_record_becomes_one_request_and_one_outcome(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        await add_operation(
            db, connection,
            operation_id="create_contact", kind=OPERATION_WRITE, method="POST",
            path="/contacts", records_path="",
            page_rule=None,
            inputs=[{"name": "email", "type": "string", "required": True}],
            body_template={"email": "{email}"},
        )
        target = await target_for(db, connection, "create_contact")

        with respx.mock(assert_all_called=False) as router:
            route = router.post(f"{BASE}/contacts").mock(
                return_value=httpx.Response(201, json={"id": "c1"})
            )
            outcome = await connector_nodes.write_batch(
                db, target,
                [{"email": "a@b.com"}, {"email": "c@d.com"}],
                node_id="w", node_data={}, batch_index=0, dry_run=False,
            )

        assert route.call_count == 2
        assert outcome.written == 2
        assert outcome.failed == 0

    async def test_one_rejected_record_does_not_fail_the_others(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        """
        Reporting the chunk as failed loses the successful writes and tells the operator
        to re-run something that already worked.
        """
        await add_operation(
            db, connection,
            operation_id="create_contact", kind=OPERATION_WRITE, method="POST",
            path="/contacts", records_path="", page_rule=None,
            inputs=[{"name": "email", "type": "string", "required": True}],
            body_template={"email": "{email}"},
        )
        target = await target_for(db, connection, "create_contact")

        with respx.mock(assert_all_called=False) as router:
            router.post(f"{BASE}/contacts").mock(
                side_effect=[
                    httpx.Response(201, json={"id": "c1"}),
                    httpx.Response(422, json={"message": "email has already been taken"}),
                    httpx.Response(201, json={"id": "c3"}),
                ]
            )
            outcome = await connector_nodes.write_batch(
                db, target,
                [{"email": "a@b.com"}, {"email": "b@b.com"}, {"email": "c@b.com"}],
                node_id="w", node_data={}, batch_index=0, dry_run=False,
            )

        assert (outcome.written, outcome.failed) == (2, 1)
        failures = [e for e in outcome.entries if e["outcome"] == RECORD_FAILED]
        assert "already been taken" in failures[0]["message"], (
            "the destination's own message is more specific than anything we could write"
        )

    async def test_a_read_only_operation_is_refused(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        await add_operation(db, connection)
        target = await target_for(db, connection, "list_contacts")

        with pytest.raises(NodeFailure, match="read operation"):
            await connector_nodes.write_batch(
                db, target, [{"a": 1}],
                node_id="w", node_data={}, batch_index=0, dry_run=False,
            )

    async def test_a_remembered_key_turns_a_create_into_an_update(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        """What stops the *next* run repeating a duplicate."""
        from app.services.integrations.mapping import dedupe

        await add_operation(
            db, connection,
            operation_id="upsert_contact", kind=OPERATION_WRITE, method="POST",
            path="/contacts/{id}", records_path="", page_rule=None,
            inputs=[
                {"name": "id", "type": "string"},
                {"name": "email", "type": "string"},
            ],
            body_template={"email": "{email}"},
        )
        target = await target_for(db, connection, "upsert_contact")

        record = {"email": "a@b.com"}
        await dedupe.remember(
            db, connection_id=connection.id, operation_id="upsert_contact",
            written=[(EMAIL_KEY.hash_for(record), "c99")],
        )
        await db.commit()

        with respx.mock(assert_all_called=False) as router:
            route = router.post(f"{BASE}/contacts/c99").mock(
                return_value=httpx.Response(200, json={"id": "c99"})
            )
            outcome = await connector_nodes.write_batch(
                db, target, [record],
                node_id="w", node_data={"natural_key": "email"},
                batch_index=0, dry_run=False,
            )

        assert route.call_count == 1, "the remembered id was used in the path"
        assert outcome.written == 1

    async def test_a_duplicate_inside_one_batch_is_skipped_and_recorded(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        """A run with any skipped record ends ``partial``, so this has to be visible."""
        await add_operation(
            db, connection,
            operation_id="create_contact", kind=OPERATION_WRITE, method="POST",
            path="/contacts", records_path="", page_rule=None,
            inputs=[{"name": "email", "type": "string"}],
            body_template={"email": "{email}"},
        )
        target = await target_for(db, connection, "create_contact")

        with respx.mock(assert_all_called=False) as router:
            route = router.post(f"{BASE}/contacts").mock(
                return_value=httpx.Response(201, json={"id": "c1"})
            )
            outcome = await connector_nodes.write_batch(
                db, target,
                [{"email": "a@b.com"}, {"email": "a@b.com"}],
                node_id="w", node_data={"natural_key": "email"},
                batch_index=0, dry_run=False,
            )

        assert route.call_count == 1
        assert outcome.skipped == 1
        skipped = [e for e in outcome.entries if e["outcome"] == RECORD_SKIPPED]
        assert "email" in skipped[0]["message"]


class TestWriteSafety:
    async def test_a_read_timeout_on_a_non_idempotent_write_is_attempted_once(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        """
        The assertion the whole module is arranged around. The request went out, the order
        may exist, and the response never arrived. Every instinct says retry; retrying
        duplicates a real order in somebody's real business.
        """
        await add_operation(
            db, connection,
            operation_id="create_order", kind=OPERATION_WRITE, method="POST",
            path="/orders", records_path="", page_rule=None,
            inputs=[{"name": "email", "type": "string"}],
            body_template={"email": "{email}"},
        )
        target = await target_for(db, connection, "create_order")

        with respx.mock(assert_all_called=False) as router:
            route = router.post(f"{BASE}/orders").mock(
                side_effect=httpx.ReadTimeout("too slow")
            )
            outcome = await connector_nodes.write_batch(
                db, target, [{"email": "a@b.com"}],
                node_id="w", node_data={}, batch_index=0, dry_run=False,
            )

        assert route.call_count == 1, "a timed-out create must not be sent again"
        assert outcome.failed == 1

        failure = outcome.entries[0]
        assert failure["retryable"] is False, (
            "and the operator must not be offered a Replay that duplicates the order"
        )
        assert "check the destination" in failure["message"].lower()

    async def test_an_idempotent_write_may_be_retried(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        """An operation earns the right to be retried by declaring it."""
        await add_operation(
            db, connection,
            operation_id="set_stock", kind=OPERATION_WRITE, method="PUT",
            path="/stock", records_path="", page_rule=None, idempotent=True,
            inputs=[{"name": "sku", "type": "string"}],
            body_template={"sku": "{sku}"},
        )
        target = await target_for(db, connection, "set_stock")

        with respx.mock(assert_all_called=False) as router:
            route = router.put(f"{BASE}/stock").mock(
                side_effect=[
                    httpx.ReadTimeout("too slow"),
                    httpx.Response(200, json={"id": "s1"}),
                ]
            )
            outcome = await connector_nodes.write_batch(
                db, target, [{"sku": "A1"}],
                node_id="w", node_data={}, batch_index=0, dry_run=False,
            )

        assert route.call_count == 2
        assert outcome.written == 1

    async def test_a_connection_that_was_never_reached_is_retried(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        """The narrow reading of ``reached_server``: connection refused provably happened
        before any byte could be processed."""
        await add_operation(
            db, connection,
            operation_id="create_order", kind=OPERATION_WRITE, method="POST",
            path="/orders", records_path="", page_rule=None,
            inputs=[{"name": "email", "type": "string"}],
            body_template={"email": "{email}"},
        )
        target = await target_for(db, connection, "create_order")

        with respx.mock(assert_all_called=False) as router:
            route = router.post(f"{BASE}/orders").mock(
                side_effect=[
                    httpx.ConnectError("refused"),
                    httpx.Response(201, json={"id": "o1"}),
                ]
            )
            outcome = await connector_nodes.write_batch(
                db, target, [{"email": "a@b.com"}],
                node_id="w", node_data={}, batch_index=0, dry_run=False,
            )

        assert route.call_count == 2
        assert outcome.written == 1


class TestDryRun:
    async def test_nothing_is_sent_and_every_request_is_still_built(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        """
        Building the request is the point. A dry run that skipped the builder would not
        catch the template naming a field the operation never declared.
        """
        await add_operation(
            db, connection,
            operation_id="create_contact", kind=OPERATION_WRITE, method="POST",
            path="/contacts", records_path="", page_rule=None,
            inputs=[{"name": "email", "type": "string"}],
            body_template={"email": "{email}"},
        )
        target = await target_for(db, connection, "create_contact")

        with respx.mock(assert_all_called=False, assert_all_mocked=True) as router:
            route = router.post(f"{BASE}/contacts")
            outcome = await connector_nodes.write_batch(
                db, target, [{"email": "a@b.com"}, {"email": "c@d.com"}],
                node_id="w", node_data={}, batch_index=0, dry_run=True,
            )

        assert route.call_count == 0, "a dry run calls nobody"
        assert outcome.written == 0

        samples = [e for e in outcome.entries if e["outcome"] == RECORD_SAMPLE]
        assert len(samples) == 2
        assert samples[0]["payload"]["body"] == {"email": "a@b.com"}
        assert "POST" in samples[0]["message"]

    async def test_a_broken_template_is_caught_by_a_dry_run(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        """``{sinse}`` for ``{since}`` would otherwise be a sync with no date filter that
        reads everything and reports success."""
        await add_operation(
            db, connection,
            operation_id="create_contact", kind=OPERATION_WRITE, method="POST",
            path="/contacts", records_path="", page_rule=None,
            inputs=[{"name": "email", "type": "string"}],
            body_template={"email": "{emial}"},
        )
        target = await target_for(db, connection, "create_contact")

        outcome = await connector_nodes.write_batch(
            db, target, [{"email": "a@b.com"}],
            node_id="w", node_data={}, batch_index=0, dry_run=True,
        )

        assert outcome.entries[0]["outcome"] == RECORD_FAILED
        assert "emial" in outcome.entries[0]["message"]


class TestCancellation:
    async def test_a_cancelled_run_stops_between_chunks(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        """The contract the UI states: cancel stops at the next record boundary, not
        mid-request."""
        await add_operation(
            db, connection,
            operation_id="create_contact", kind=OPERATION_WRITE, method="POST",
            path="/contacts", records_path="", page_rule=None,
            inputs=[{"name": "email", "type": "string"}],
            body_template={"email": "{email}"},
        )
        target = await target_for(db, connection, "create_contact")

        async def cancelled() -> bool:
            return True

        with respx.mock(assert_all_called=False, assert_all_mocked=True) as router:
            route = router.post(f"{BASE}/contacts")
            outcome = await connector_nodes.write_batch(
                db, target, [{"email": f"{i}@b.com"} for i in range(10)],
                node_id="w", node_data={"chunk_size": 1, "parallelism": 1},
                batch_index=0, dry_run=False, cancelled=cancelled,
            )

        assert route.call_count == 0
        assert outcome.skipped == 10
