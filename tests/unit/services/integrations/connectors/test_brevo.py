"""
Tests for the Brevo connector.

Three claims carry this suite, and each of them is a bug the connector would otherwise
have.

**``updateEnabled`` really goes out, and ``create_contact`` really is idempotent.** Those
are one fact, not two: without the literal in the body, Brevo answers 400
``duplicate_parameter`` for an address it already holds, so a re-run of yesterday's sync
fails on every contact it already sent — and ``idempotent=True``, which permits a retry
after a timeout, becomes a claim the request cannot support. Asserted on the sent body
rather than on the template, because the substituter is what decides whether a non-string
literal survives.

**Offset paging starts at zero and steps by what came back.** Brevo counts from zero and
``PageRule.start_at`` defaults to one, so a default here would silently skip the first
contact of every sync — the shape of paging bug nobody notices. The step comes from the
response for the reason ``pagination.advance`` gives: a vendor may return fewer than it was
asked for, and an offset computed from the request skips the difference.

**The address is fixed and cannot be talked out of.** One hostname for every Brevo account
means a stored ``base_url`` must not override it, and a connection that arrived with one —
from a connector change, a restored database — must not become a Brevo-token request to
somebody else's host.

``respx`` rather than a stubbed client, for the reason ``test_sender.py`` gives: it
intercepts at the transport layer, so the real pooling, byte-cap, paging and retry code
runs.
"""

from __future__ import annotations

import json
import socket
from typing import Any, Dict, List

import httpx
import pytest
import respx

from app.models.integrations import AUTH_API_KEY, OPERATION_READ, OPERATION_WRITE
from app.services.integrations.connectors import registry
from app.services.integrations.connectors.brevo import connector as brevo
from app.services.integrations.connectors.brevo import ecommerce
from app.services.integrations.connectors.spec import PAGE_OFFSET, describe_operation
from app.services.integrations.runtime import (
    http_client,
    pagination,
    rate_limiter,
    request_builder,
    sender,
)

BASE = brevo.BASE_URL
CONTACTS = f"{BASE}/contacts"
LISTS = f"{BASE}/contacts/lists"


@pytest.fixture(autouse=True)
async def clean_runtime(monkeypatch: pytest.MonkeyPatch):
    """A fresh pool and limiter per test, and DNS that answers. See ``test_sender.py``."""
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


class Connection:
    """A connection row's duck type."""

    def __init__(self, base_url: Any = None, account: str = "") -> None:
        self.base_url = base_url
        self.external_account_id = account
        self.label = "Marketing account"


def context(**overrides) -> sender.SendContext:
    defaults = dict(
        connection_key="brevo-1",
        connection_label="Marketing account",
        connector=brevo.SPEC,
        auth_header=("api-key", "xkeysib-0123456789abcdef"),
    )
    defaults.update(overrides)
    return sender.SendContext(**defaults)


def contacts_page(count: int = 2, *, start: int = 0) -> Dict[str, Any]:
    return {
        "contacts": [
            {
                "id": start + n,
                "email": f"person{start + n}@example.com",
                "attributes": {"FIRSTNAME": "Ada", "LASTNAME": "Lovelace"},
                "listIds": [3],
                "emailBlacklisted": False,
                "smsBlacklisted": False,
                "createdAt": "2026-08-01T09:00:00.000Z",
                "modifiedAt": "2026-08-02T09:00:00.000Z",
            }
            for n in range(count)
        ],
        "count": 4,
    }


# ---------------------------------------------------------------------------
# The spec
# ---------------------------------------------------------------------------


class TestTheSpec:
    def test_it_is_registered(self) -> None:
        assert "brevo" in registry.connector_ids()
        assert registry.require("brevo") is brevo.SPEC

    def test_it_reads_and_writes(self) -> None:
        """Unlike Shopify. The palette for a ``connector_write`` node is built from
        ``writable_operations()``, so this tuple being non-empty is what makes Brevo
        selectable as a destination at all."""
        assert [op.operation_id for op in brevo.SPEC.readable_operations()] == [
            "contacts",
            "lists",
            "orders",
            "products",
            "categories",
        ]
        assert [op.operation_id for op in brevo.SPEC.writable_operations()] == [
            "create_contact",
            "add_to_list",
            "upsert_order",
            "upsert_product",
            "upsert_category",
        ]

    def test_contacts_is_the_operation_the_test_button_calls(self) -> None:
        """``connection_service._operation_to_test`` takes the first read positionally.

        It has to be a contacts one. Every Brevo account has that endpoint, whereas the
        eCommerce endpoints answer 403 until the account switches the eCommerce app on —
        so a Test that happened to call ``orders`` would tell somebody with a perfectly
        good key that their connection does not work.
        """
        assert brevo.SPEC.readable_operations()[0].operation_id == "contacts"

    def test_the_key_goes_in_brevos_own_header_with_no_prefix(self) -> None:
        """``Bearer xkeysib-…`` would be sent as part of the key and answered with a 401
        that reads like a bad key."""
        assert brevo.SPEC.auth.kind == AUTH_API_KEY
        assert brevo.SPEC.auth.name == "api-key"
        assert brevo.SPEC.auth.value_template == "{api_key}"

    def test_the_address_is_neither_typed_nor_computed(self) -> None:
        assert brevo.SPEC.base_url_is_user_supplied is False
        assert brevo.SPEC.account_id_required is False
        assert brevo.SPEC.requires_https is True
        assert brevo.SPEC.allows_private_hosts is False
        assert brevo.SPEC.render_base_url(Connection()) == BASE

    def test_a_stored_url_cannot_override_the_fixed_one(self) -> None:
        """One hostname for every account. A connection carrying a base URL — from a
        restored database, or a connector that used to ask for one — must not become a
        Brevo-token request to somebody else's host."""
        connection = Connection(base_url="https://attacker.example.com")

        assert brevo.SPEC.render_base_url(connection) == BASE

    def test_both_reads_page_by_offset_from_zero(self) -> None:
        for operation in brevo.SPEC.readable_operations():
            assert operation.page_rule.kind == PAGE_OFFSET
            assert operation.page_rule.param == "offset"
            assert operation.page_rule.size_param == "limit"
            # Brevo counts from zero; PageRule defaults to one. A default here skips the
            # first record of every sync.
            assert operation.page_rule.start_at == 0

    def test_each_read_asks_for_what_its_endpoint_allows(self) -> None:
        """Asking for more than the endpoint's maximum is a 400, not a clamp."""
        assert brevo.SPEC.operation("contacts").page_rule.size == brevo.MAX_CONTACT_PAGE
        assert brevo.SPEC.operation("lists").page_rule.size == brevo.MAX_LIST_PAGE
        assert brevo.SPEC.operation("orders").page_rule.size == ecommerce.MAX_ORDER_PAGE
        assert brevo.SPEC.operation("products").page_rule.size == ecommerce.MAX_PRODUCT_PAGE
        assert (
            brevo.SPEC.operation("categories").page_rule.size == ecommerce.MAX_CATEGORY_PAGE
        )

    def test_no_read_declares_the_paging_parameters_as_inputs(self) -> None:
        """Offset paging supplies ``limit`` and ``offset`` as request parameters. Declaring
        them as inputs as well puts the page size in the query string twice."""
        for operation in brevo.SPEC.readable_operations():
            assert operation.input_named("limit") is None
            assert operation.input_named("offset") is None

    def test_the_writes_declare_what_they_need(self) -> None:
        assert brevo.SPEC.operation("create_contact").required_inputs == ("email",)
        assert set(brevo.SPEC.operation("add_to_list").required_inputs) == {
            "list_id",
            "emails",
        }

    def test_the_kinds_match_the_methods(self) -> None:
        """A write declared as a GET slips past every rule that keys off ``kind``."""
        for operation in brevo.SPEC.operations:
            if operation.kind == OPERATION_WRITE:
                assert operation.method == "POST"
            else:
                assert operation.kind == OPERATION_READ
                assert operation.method == "GET"

    def test_the_upsert_is_in_the_fingerprint(self) -> None:
        """``updateEnabled`` decides what the request does, so a replay that dropped it
        must not claim to be the same operation."""
        canonical = brevo.SPEC.operation("create_contact").canonical()

        assert canonical["body"]["updateEnabled"] is True


class TestWhatTheBrowserSees:
    def test_no_operation_leaks_the_address_or_the_header(self) -> None:
        """This payload reaches a browser. See ``describe_operation``."""
        for operation in brevo.SPEC.operations:
            described = json.dumps(describe_operation(operation))

            assert "api.brevo.com" not in described
            assert "/contacts" not in described

    def test_the_connector_description_asks_for_nothing_but_a_key(self) -> None:
        entry = next(
            c for c in registry.describe_connectors() if c["connector_id"] == "brevo"
        )

        assert entry["asks_for_base_url"] is False
        assert entry["asks_for_account_id"] is False
        assert entry["operations_are_user_defined"] is False
        assert entry["auth_kind"] == "api_key"

    def test_it_carries_an_icon_for_the_apps_page(self) -> None:
        entry = next(
            c for c in registry.describe_connectors() if c["connector_id"] == "brevo"
        )

        assert entry["icon"] == brevo.SPEC.icon
        assert entry["accent"] == brevo.SPEC.accent


# ---------------------------------------------------------------------------
# Building the request
# ---------------------------------------------------------------------------


class TestReadRequests:
    def test_contacts_is_a_get_with_no_filters_by_default(self) -> None:
        built = request_builder.build_request(
            brevo.SPEC.operation("contacts"), {}, base_url=BASE
        )

        assert built.method == "GET"
        assert built.url == CONTACTS
        # An unmapped filter is omitted, not sent empty: `?modifiedSince=` and no
        # `modifiedSince` mean different things to Brevo.
        assert built.params == {}

    def test_a_changed_since_filter_goes_out_as_a_timestamp(self) -> None:
        built = request_builder.build_request(
            brevo.SPEC.operation("contacts"),
            {"modified_since": "2026-08-01T00:00:00"},
            base_url=BASE,
        )

        assert built.params["modifiedSince"] == "2026-08-01T00:00:00"

    def test_lists_is_a_get_to_its_own_path(self) -> None:
        built = request_builder.build_request(
            brevo.SPEC.operation("lists"), {}, base_url=BASE
        )

        assert built.url == LISTS


class TestWriteRequests:
    def test_the_create_always_sends_the_upsert_flag(self) -> None:
        """
        The most consequential value in the connector. Asserted on the built body rather
        than on the template, because a non-string literal has to survive substitution —
        and without it a re-run fails on every contact Brevo already holds.
        """
        built = request_builder.build_request(
            brevo.SPEC.operation("create_contact"),
            {"email": "ada@example.com"},
            base_url=BASE,
        )

        assert built.json_body["updateEnabled"] is True
        assert built.json_body["email"] == "ada@example.com"

    def test_unmapped_contact_fields_are_omitted_not_nulled(self) -> None:
        """Brevo reads an explicit null as "clear this", which is a destructive reading of
        a field nobody mapped."""
        built = request_builder.build_request(
            brevo.SPEC.operation("create_contact"),
            {"email": "ada@example.com"},
            base_url=BASE,
        )

        assert set(built.json_body) == {"email", "updateEnabled"}

    def test_attributes_and_lists_keep_their_json_shape(self) -> None:
        built = request_builder.build_request(
            brevo.SPEC.operation("create_contact"),
            {
                "email": "ada@example.com",
                "attributes": {"FIRSTNAME": "Ada"},
                "list_ids": [3, 7],
            },
            base_url=BASE,
        )

        assert built.json_body["attributes"] == {"FIRSTNAME": "Ada"}
        assert built.json_body["listIds"] == [3, 7]

    def test_a_create_with_no_email_is_refused_before_it_is_sent(self) -> None:
        """A 400 from Brevo is a worse sentence than the one this can compose."""
        with pytest.raises(ValueError, match="Email"):
            request_builder.build_request(
                brevo.SPEC.operation("create_contact"), {}, base_url=BASE
            )

    def test_the_list_id_lands_in_the_path(self) -> None:
        built = request_builder.build_request(
            brevo.SPEC.operation("add_to_list"),
            {"list_id": 12, "emails": ["ada@example.com"]},
            base_url=BASE,
        )

        assert built.url == f"{BASE}/contacts/lists/12/contacts/add"
        assert built.json_body == {"emails": ["ada@example.com"]}

    def test_a_missing_list_id_is_never_a_url_with_a_hole_in_it(self) -> None:
        with pytest.raises(ValueError):
            request_builder.build_request(
                brevo.SPEC.operation("add_to_list"),
                {"emails": ["ada@example.com"]},
                base_url=BASE,
            )


# ---------------------------------------------------------------------------
# Sending and paging
# ---------------------------------------------------------------------------


class TestSending:
    @respx.mock
    async def test_the_key_goes_out_in_the_api_key_header(self) -> None:
        sent: List[httpx.Request] = []

        def record(request: httpx.Request) -> httpx.Response:
            sent.append(request)
            return httpx.Response(200, json=contacts_page())

        respx.get(CONTACTS).mock(side_effect=record)

        operation = brevo.SPEC.operation("contacts")
        built = request_builder.build_request(
            operation, pagination.first_page_arguments(operation.page_rule), base_url=BASE
        )
        read = await sender.send(built, operation, context())

        assert read.ok is True
        assert sent[0].headers["api-key"] == "xkeysib-0123456789abcdef"
        assert "authorization" not in sent[0].headers

    @respx.mock
    async def test_the_records_come_out_of_the_contacts_key(self) -> None:
        from app.services.integrations.mapping import paths

        respx.get(CONTACTS).mock(return_value=httpx.Response(200, json=contacts_page()))

        operation = brevo.SPEC.operation("contacts")
        built = request_builder.build_request(operation, {}, base_url=BASE)
        read = await sender.send(built, operation, context())

        records = paths.read_records(read.payload, operation.records_path)

        assert len(records) == 2
        assert paths.read(records[0], "attributes.FIRSTNAME") == "Ada"

    @respx.mock
    async def test_the_first_page_asks_from_zero(self) -> None:
        """Brevo counts from zero. Page one starting at one loses a contact per sync."""
        operation = brevo.SPEC.operation("contacts")
        walk = pagination.begin(operation.page_rule, CONTACTS)

        assert walk.params == {"limit": brevo.MAX_CONTACT_PAGE, "offset": 0}

    @respx.mock
    async def test_the_offset_steps_by_what_came_back(self) -> None:
        """
        Not by the page size. A vendor is free to return fewer than it was asked for, and
        an offset computed from the request skips the difference — silently, as a gap in
        the middle of a sync.
        """
        operation = brevo.SPEC.operation("contacts")
        walk = pagination.begin(operation.page_rule, CONTACTS)

        pagination.advance(
            walk, payload=contacts_page(2), headers={}, records_in_page=2
        )

        assert walk.finished is False
        assert walk.params["offset"] == 2

    @respx.mock
    async def test_an_empty_page_ends_the_walk(self) -> None:
        operation = brevo.SPEC.operation("contacts")
        walk = pagination.begin(operation.page_rule, CONTACTS)

        pagination.advance(
            walk, payload={"contacts": []}, headers={}, records_in_page=0
        )

        assert walk.finished is True

    @respx.mock
    async def test_a_rejected_key_is_a_failure_and_not_an_empty_read(self) -> None:
        """Brevo reports a bad key with a 401 rather than inside a 200 — so unlike Shopify
        this connector needs no hook, and ``sender`` raising is the whole behaviour."""
        from app.services.integrations.errors import NodeFailure

        respx.get(CONTACTS).mock(
            return_value=httpx.Response(
                401, json={"code": "unauthorized", "message": "Key not found"}
            )
        )

        operation = brevo.SPEC.operation("contacts")
        built = request_builder.build_request(operation, {}, base_url=BASE)

        with pytest.raises(NodeFailure):
            await sender.send(built, operation, context())


# ---------------------------------------------------------------------------
# The eCommerce section
# ---------------------------------------------------------------------------
# Orders, products and categories. Three claims carry this half of the suite:
#
#   * `updateEnabled` goes out on the two writes that need it and is *absent* from the one
#     that does not — Brevo's order endpoint upserts by itself, and a flag added there for
#     symmetry would be a field it does not document.
#   * A line-item array survives as an array. It is the only nested structure this
#     connector sends, and a `products` that arrived as the string "[{...}]" would be
#     rejected by Brevo for a reason no message here would explain.
#   * The three families draw on the buckets Brevo actually meters, which for the reads
#     and the category write means *one* bucket between four operations.

ORDERS_URL = f"{BASE}/orders"
PRODUCTS_URL = f"{BASE}/products"
CATEGORIES_URL = f"{BASE}/categories"

#: A whole, valid order. Every required input, so a test that wants to prove one thing is
#: missing can remove exactly that one.
ORDER_RECORD: Dict[str, Any] = {
    "id": "ORD-1001",
    "created_at": "2026-08-20T09:30:00Z",
    "updated_at": "2026-08-20T11:00:00Z",
    "status": "completed",
    "amount": "129.98",
    "products": [
        {"productId": "SKU-1", "price": 49.99, "quantity": 2},
        {"productId": "SKU-2", "price": 30.00, "quantity": 1},
    ],
}


def orders_page(count: int = 2, *, start: int = 0) -> Dict[str, Any]:
    return {
        "orders": [
            {
                "id": f"ORD-{start + n}",
                "amount": 49.99,
                "status": "completed",
                "email": f"buyer{start + n}@example.com",
                "createdAt": "2026-08-01T09:00:00.000Z",
                "updatedAt": "2026-08-02T09:00:00.000Z",
                "billing": {"city": "Bath", "countryCode": "GB"},
                "products": [{"productId": "SKU-1", "price": 49.99, "quantity": 1}],
            }
            for n in range(count)
        ],
        "count": 4,
    }


class TestTheEcommerceSpec:
    def test_every_operation_declares_its_own_allowance(self) -> None:
        """The whole reason the field exists. An eCommerce operation left on the
        connector's figure would send at the wrong rate for its endpoint, and Brevo's
        differ by 180×."""
        for operation in ecommerce.OPERATIONS:
            assert operation.rate_limits is not None, operation.operation_id
            assert operation.rate_limit_group, operation.operation_id

    def test_the_reads_and_the_category_write_share_one_allowance(self) -> None:
        """Brevo's hundred-an-hour is one pool, not one per endpoint. Four buckets would
        send four hundred an hour against it and read as correct until the 429s."""
        sharing = ("orders", "products", "categories", "upsert_category")

        groups = {brevo.SPEC.operation(name).rate_limit_group for name in sharing}

        assert groups == {ecommerce.OTHER_GROUP}

    def test_the_two_metered_writes_are_metered_separately(self) -> None:
        assert brevo.SPEC.operation("upsert_order").rate_limits.requests_per_second == 5.0
        assert brevo.SPEC.operation("upsert_product").rate_limits.requests_per_second == 2.0
        assert brevo.SPEC.operation("upsert_order").rate_limit_group == "orders"
        assert brevo.SPEC.operation("upsert_product").rate_limit_group == "products"

    def test_the_hourly_allowance_holds_an_hour_and_drips(self) -> None:
        """100/hour as a leaky bucket: a hundred whenever you like, then one per 36s. A
        burst of 1 would make a three-page read take two minutes for no reason."""
        limits = ecommerce.OTHER_LIMITS

        assert limits.burst == 100
        assert round(1 / limits.requests_per_second) == 36

    def test_an_allowance_is_not_in_the_fingerprint(self) -> None:
        """Retuning a limit when Brevo publishes new figures must not move every hash at
        once and make every prior run look like it ran something else."""
        canonical = brevo.SPEC.operation("upsert_order").canonical()

        assert "rate_limits" not in canonical
        assert "rate_limit_group" not in canonical

    def test_the_writes_declare_what_brevo_requires(self) -> None:
        assert set(brevo.SPEC.operation("upsert_order").required_inputs) == {
            "id",
            "created_at",
            "updated_at",
            "status",
            "amount",
            "products",
        }
        assert set(brevo.SPEC.operation("upsert_product").required_inputs) == {"id", "name"}
        assert brevo.SPEC.operation("upsert_category").required_inputs == ("id",)

    def test_no_read_requires_anything(self) -> None:
        """``test_connection`` builds its one real call with no arguments for an offset
        read. A required input would make every Test fail with a sentence about a missing
        value rather than about the key."""
        for operation in ecommerce.OPERATIONS:
            if operation.kind == OPERATION_READ:
                assert operation.required_inputs == (), operation.operation_id

    def test_money_is_never_a_float(self) -> None:
        """A merchant's revenue must not round-trip through binary floating point between
        two systems that both had it right."""
        order_amount = brevo.SPEC.operation("upsert_order").input_named("amount")
        product_price = brevo.SPEC.operation("upsert_product").input_named("price")

        assert order_amount.type == "string"
        assert product_price.type == "string"

    def test_the_records_are_read_from_the_right_key(self) -> None:
        assert brevo.SPEC.operation("orders").records_path == "orders"
        assert brevo.SPEC.operation("products").records_path == "products"
        assert brevo.SPEC.operation("categories").records_path == "categories"


class TestTheUpsertFlag:
    """Which writes carry ``updateEnabled``, and why one deliberately does not."""

    @pytest.mark.parametrize("operation_id", ["upsert_product", "upsert_category"])
    def test_the_flag_is_sent_as_a_real_boolean(self, operation_id: str) -> None:
        """Not the string "True". Brevo reads a string as truthy today, and the day it
        stops, a duplicate id becomes a 400 on every record of a re-run."""
        operation = brevo.SPEC.operation(operation_id)

        built = request_builder.build_request(operation, {"id": "X", "name": "N"}, base_url=BASE)

        assert built.json_body["updateEnabled"] is True

    @pytest.mark.parametrize("operation_id", ["upsert_product", "upsert_category"])
    def test_the_flag_is_in_the_fingerprint(self, operation_id: str) -> None:
        """It decides whether a second send updates or fails, so a replay that dropped it
        must not claim to be the same operation."""
        assert brevo.SPEC.operation(operation_id).canonical()["body"]["updateEnabled"] is True

    def test_the_order_write_does_not_invent_a_flag_brevo_has_no_use_for(self) -> None:
        """``POST /orders/status`` upserts on ``id`` by itself. The inconsistency with the
        other two is Brevo's, and matching it is the point — a field the vendor does not
        document is a field whose meaning can change under us."""
        built = request_builder.build_request(
            brevo.SPEC.operation("upsert_order"), ORDER_RECORD, base_url=BASE
        )

        assert "updateEnabled" not in built.json_body

    def test_every_write_that_claims_idempotence_can_support_it(self) -> None:
        """``idempotent=True`` permits a retry after a timeout that may already have
        arrived. It is earned by the upsert, so the two must never disagree."""
        for operation in ecommerce.OPERATIONS:
            if operation.kind != OPERATION_WRITE:
                continue

            assert operation.idempotent is True, operation.operation_id

            upserts_natively = operation.path == "/orders/status"
            flagged = (operation.body_template or {}).get("updateEnabled") is True

            assert upserts_natively or flagged, operation.operation_id


class TestEcommerceRequests:
    def test_a_line_item_array_goes_out_as_an_array(self) -> None:
        """The only nested structure this connector sends. As the string "[{...}]" Brevo
        rejects the order, and nothing in the message would say why."""
        built = request_builder.build_request(
            brevo.SPEC.operation("upsert_order"), ORDER_RECORD, base_url=BASE
        )

        assert built.json_body["products"] == ORDER_RECORD["products"]
        assert isinstance(built.json_body["products"][0]["price"], float)

    def test_the_body_survives_serialisation_as_json(self) -> None:
        """`serialise_body` is what actually reaches the socket, so the structure being
        right in Python is only half of it."""
        built = request_builder.build_request(
            brevo.SPEC.operation("upsert_order"), ORDER_RECORD, base_url=BASE
        )

        assert json.loads(request_builder.serialise_body(built.json_body)) == built.json_body

    def test_timestamps_go_out_as_iso_8601_utc(self) -> None:
        """**The one inferred format in this connector.** Brevo documents
        ``YYYY-MM-DDTHH:mm:ssZ``; declaring these ``datetime`` buys a local refusal for a
        mapping that points at the wrong field, and renders ``+00:00`` — the same instant
        in RFC 3339. Pinned here so that if Brevo ever refuses it, the one-line change to
        ``string`` is visible rather than archaeological."""
        built = request_builder.build_request(
            brevo.SPEC.operation("upsert_order"), ORDER_RECORD, base_url=BASE
        )

        assert built.json_body["createdAt"] == "2026-08-20T09:30:00+00:00"
        assert built.json_body["updatedAt"] == "2026-08-20T11:00:00+00:00"

    def test_an_unmapped_optional_field_is_omitted_not_nulled(self) -> None:
        """Brevo reads an explicit null as "clear this", which is a destructive reading of
        a field somebody simply did not map."""
        built = request_builder.build_request(
            brevo.SPEC.operation("upsert_order"), ORDER_RECORD, base_url=BASE
        )

        for absent in ("email", "billing", "coupons", "metaInfo", "storeId", "historical"):
            assert absent not in built.json_body

    def test_an_order_missing_its_total_is_refused_before_it_is_sent(self) -> None:
        """A 400 from Brevo names its own field; this names the one on screen."""
        incomplete = {k: v for k, v in ORDER_RECORD.items() if k != "amount"}

        with pytest.raises(ValueError, match="Total"):
            request_builder.build_request(
                brevo.SPEC.operation("upsert_order"), incomplete, base_url=BASE
            )

    def test_a_read_sends_no_filters_by_default(self) -> None:
        """``?modifiedSince=`` and no ``modifiedSince`` mean different things to Brevo, and
        the one we can be sure of is the one we did not send."""
        built = request_builder.build_request(
            brevo.SPEC.operation("orders"), {}, base_url=BASE
        )

        assert built.url == ORDERS_URL
        assert built.params == {}

    def test_an_incremental_read_filters_by_timestamp(self) -> None:
        built = request_builder.build_request(
            brevo.SPEC.operation("products"),
            {"modified_since": "2026-08-01T00:00:00Z"},
            base_url=BASE,
        )

        assert built.params == {"modifiedSince": "2026-08-01T00:00:00+00:00"}


class TestEcommerceOverTheWire:
    @respx.mock
    async def test_the_records_come_out_of_the_orders_key(self) -> None:
        respx.get(ORDERS_URL).mock(return_value=httpx.Response(200, json=orders_page()))

        operation = brevo.SPEC.operation("orders")
        built = request_builder.build_request(
            operation, pagination.first_page_arguments(operation.page_rule), base_url=BASE
        )
        read = await sender.send(built, operation, context())

        from app.services.integrations.mapping import paths

        assert len(paths.read_records(read.payload, operation.records_path)) == 2

    @respx.mock
    async def test_the_first_page_asks_from_zero(self) -> None:
        """Brevo counts from zero. Starting at one skips the first order of every sync."""
        route = respx.get(ORDERS_URL).mock(
            return_value=httpx.Response(200, json=orders_page())
        )

        operation = brevo.SPEC.operation("orders")
        built = request_builder.build_request(
            operation,
            {},
            base_url=BASE,
            extra_query=pagination.first_page_params(operation.page_rule),
        )
        await sender.send(built, operation, context())

        sent = route.calls.last.request.url

        assert sent.params["offset"] == "0"
        assert sent.params["limit"] == str(ecommerce.MAX_ORDER_PAGE)

    @respx.mock
    async def test_a_204_with_no_body_is_a_successful_write(self) -> None:
        """Brevo answers 204 and nothing at all when an upsert *updated* rather than
        created. Read as a failure it would fail every record of every re-run — the exact
        case the upsert exists to make safe."""
        respx.post(PRODUCTS_URL).mock(return_value=httpx.Response(204))

        operation = brevo.SPEC.operation("upsert_product")
        built = request_builder.build_request(
            operation, {"id": "SKU-1", "name": "Boots"}, base_url=BASE
        )
        read = await sender.send(built, operation, context())

        assert read.ok is True

    @respx.mock
    async def test_a_created_product_reports_the_id_brevo_gave_it(self) -> None:
        respx.post(PRODUCTS_URL).mock(return_value=httpx.Response(201, json={"id": "SKU-1"}))

        operation = brevo.SPEC.operation("upsert_product")
        built = request_builder.build_request(
            operation, {"id": "SKU-1", "name": "Boots"}, base_url=BASE
        )
        read = await sender.send(built, operation, context())

        assert read.payload["id"] == "SKU-1"

    @respx.mock
    async def test_the_key_goes_out_on_an_ecommerce_call_too(self) -> None:
        route = respx.post(CATEGORIES_URL).mock(return_value=httpx.Response(204))

        operation = brevo.SPEC.operation("upsert_category")
        built = request_builder.build_request(operation, {"id": "shoes"}, base_url=BASE)
        await sender.send(built, operation, context())

        assert route.calls.last.request.headers["api-key"] == "xkeysib-0123456789abcdef"

    @respx.mock
    async def test_a_rejected_order_does_not_echo_the_key_back(self) -> None:
        """Brevo repeats request details in some error bodies. Searched by value rather
        than by shape, because a leak is a leak whatever wrapped it."""
        from app.services.integrations.errors import NodeFailure

        respx.post(f"{BASE}/orders/status").mock(
            return_value=httpx.Response(
                400,
                json={
                    "code": "invalid_parameter",
                    "message": "Bad request",
                    "sent": {"api-key": "xkeysib-0123456789abcdef"},
                },
            )
        )

        operation = brevo.SPEC.operation("upsert_order")
        built = request_builder.build_request(operation, ORDER_RECORD, base_url=BASE)

        with pytest.raises(NodeFailure) as caught:
            await sender.send(built, operation, context())

        assert "xkeysib-0123456789abcdef" not in str(caught.value)


class TestWhichBucketAnEcommerceCallSpends:
    """``sender._bucket_key`` — the half of the per-operation allowance that decides
    whether two operations queue behind each other or run in parallel."""

    def test_the_shared_family_lands_on_one_bucket(self) -> None:
        ctx = context()
        keys = {
            sender._bucket_key(brevo.SPEC.operation(name), ctx)
            for name in ("orders", "products", "categories", "upsert_category")
        }

        assert keys == {"brevo-1#other"}

    def test_a_metered_write_gets_its_own(self) -> None:
        ctx = context()

        assert sender._bucket_key(brevo.SPEC.operation("upsert_order"), ctx) == "brevo-1#orders"
        assert (
            sender._bucket_key(brevo.SPEC.operation("upsert_product"), ctx)
            == "brevo-1#products"
        )

    def test_two_connections_never_share_a_bucket(self) -> None:
        """The group narrows a connection's allowance; it must never widen it to somebody
        else's account."""
        operation = brevo.SPEC.operation("orders")

        mine = sender._bucket_key(operation, context(connection_key="brevo-1"))
        theirs = sender._bucket_key(operation, context(connection_key="brevo-2"))

        assert mine != theirs

    def test_an_operation_with_no_allowance_still_spends_the_connections(self) -> None:
        """The regression guard for every connector that predates this field — Shopify's
        limit is per shop and shared across its operations, so splitting it would send
        three times what the store permits."""
        shopify = registry.require("shopify")
        ctx = context(connector=shopify, connection_key="shop-1")

        for operation in shopify.operations:
            assert sender._bucket_key(operation, ctx) == "shop-1"
            assert sender._limits_for(operation, ctx) is shopify.rate_limits
