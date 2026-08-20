"""
Tests for the Shopify Admin GraphQL connector.

**The assertion that matters most is in ``TestGreenEmptySync``.** Shopify answers a
missing access scope with HTTP 200 and an ``errors`` array. Before ``after_response``
existed, that response flowed all the way through: the record path found ``None``, paging
stopped because "the last page was empty", and the run ended **successful with zero
records**. A store that refused us and a store with no orders looked identical, and only
one of them was ever going to be investigated.

That failure shape is why the test asserts two things rather than one. Asserting only the
message would pass against an implementation that logged the error and carried on — which
is precisely the behaviour being prevented.

**The paging tests assert the request body, never the URL.** Every Shopify GraphQL request
goes to the same ``/graphql.json`` with no query string, so page one and page two have
byte-identical URLs. A test that checked the URL would pass whether or not the cursor ever
moved.

``respx`` rather than a stubbed client, for the reason given in ``test_sender.py``: it
intercepts at the transport layer, so the real pooling, streaming, byte-cap, paging and
retry code runs.
"""

from __future__ import annotations

import json
import socket
from typing import Any, Dict, List

import httpx
import pytest
import respx

from app.models.integrations import AUTH_API_KEY
from app.services.integrations.connectors import registry
from app.services.integrations.connectors.shopify import connector as shopify
from app.services.integrations.connectors.shopify.hooks import (
    SHOP_DOMAIN_PATTERN,
    ShopifyHooks,
    is_shop_domain,
)
from app.services.integrations.connectors.spec import (
    PAGE_INPUT_CURSOR,
    describe_operation,
)
from app.services.integrations.errors import NodeFailure
from app.services.integrations.runtime import (
    http_client,
    pagination,
    rate_limiter,
    request_builder,
    sender,
)

SHOP = "demo-store.myshopify.com"
BASE = f"https://{SHOP}/admin/api/{shopify.API_VERSION}"
GRAPHQL = f"{BASE}/graphql.json"


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

    def __init__(self, account: str = SHOP, base_url: Any = None) -> None:
        self.external_account_id = account
        self.base_url = base_url
        self.label = "Demo store"


def context(**overrides) -> sender.SendContext:
    defaults = dict(
        connection_key="shopify-1",
        connection_label="Demo store",
        connector=shopify.SPEC,
        auth_header=("X-Shopify-Access-Token", "shpat_0123456789abcdef"),
    )
    defaults.update(overrides)
    return sender.SendContext(**defaults)


def orders_page(cursor: str, *, more: bool, count: int = 2) -> Dict[str, Any]:
    return {
        "data": {
            "orders": {
                "edges": [
                    {"node": {"id": f"gid://shopify/Order/{n}", "name": f"#{n}"}}
                    for n in range(count)
                ],
                "pageInfo": {"hasNextPage": more, "endCursor": cursor},
            }
        },
        "extensions": {
            "cost": {
                "requestedQueryCost": 12,
                "actualQueryCost": 12,
                "throttleStatus": {
                    "maximumAvailable": 2000.0,
                    "currentlyAvailable": 1988.0,
                    "restoreRate": 100.0,
                },
            }
        },
    }


# ---------------------------------------------------------------------------
# The spec
# ---------------------------------------------------------------------------


class TestTheSpec:
    def test_it_is_registered(self) -> None:
        assert "shopify" in registry.connector_ids()
        assert registry.require("shopify") is shopify.SPEC

    def test_it_declares_no_writes(self) -> None:
        """
        Read-only is a property, not a habit.

        The palette for a ``connector_write`` node is built from ``writable_operations()``,
        so an empty tuple is what makes Shopify unselectable there. The reason is Shopify's
        own: its mutations take no idempotency key, so a create that timed out *after
        reaching the server* has already happened and retrying duplicates a real record.
        """
        assert shopify.SPEC.writable_operations() == ()
        assert len(shopify.SPEC.readable_operations()) == 3

    def test_the_address_is_never_user_supplied(self) -> None:
        """A typed base URL is how something labelled "Shopify", carrying a Shopify token,
        ends up pointing at another host."""
        assert shopify.SPEC.base_url_is_user_supplied is False
        assert shopify.SPEC.requires_https is True
        assert shopify.SPEC.allows_private_hosts is False

    def test_the_token_goes_in_shopifys_header(self) -> None:
        assert shopify.SPEC.auth.kind == AUTH_API_KEY
        assert shopify.SPEC.auth.name == "X-Shopify-Access-Token"

    def test_the_address_is_built_from_the_shop_domain(self) -> None:
        assert shopify.SPEC.render_base_url(Connection()) == BASE

    def test_every_operation_pages_by_input(self) -> None:
        """A query-string cursor cannot reach a POST body's variables."""
        for operation in shopify.SPEC.operations:
            assert operation.page_rule.kind == PAGE_INPUT_CURSOR
            assert operation.page_rule.param == "cursor"
            assert operation.page_rule.size == shopify.MAX_PAGE_SIZE

    def test_every_operation_keeps_its_document_literal(self) -> None:
        for operation in shopify.SPEC.operations:
            assert operation.body_literals == ("query",)
            assert "{" in operation.body_template["query"]

    def test_the_documents_are_inside_the_fingerprints(self) -> None:
        """A replay running different GraphQL must not claim to match."""
        digests = {op.fingerprint() for op in shopify.SPEC.operations}

        assert len(digests) == 3

    def test_no_operation_declares_query_as_an_input(self) -> None:
        """``query`` is the body key holding the document. An input of that name would be
        two different things under one word in one operation."""
        for operation in shopify.SPEC.operations:
            assert operation.input_named("query") is None
            assert operation.input_named("search") is not None

    def test_no_input_is_required(self) -> None:
        """
        ``open_supply`` builds one request before any walk exists, purely to learn the
        origin URL. A required ``page_size`` would make that call raise for a value
        pagination was about to supply.
        """
        for operation in shopify.SPEC.operations:
            assert operation.required_inputs == ()

    def test_each_operation_stays_inside_the_catalogue_ceiling(self) -> None:
        """The AI catalogue caps a description at 25 fields; more are silently dropped."""
        for operation in shopify.SPEC.operations:
            assert len(operation.outputs) <= 25


class TestWhatTheBrowserSees:
    def test_no_operation_leaks_the_address_or_the_document(self) -> None:
        """This payload reaches a browser. See ``describe_operation``."""
        for operation in shopify.SPEC.operations:
            described = json.dumps(describe_operation(operation))

            assert "graphql.json" not in described
            assert "myshopify.com" not in described
            assert "pageInfo" not in described
            assert "X-Shopify-Access-Token" not in described

    def test_the_connector_description_carries_the_account_prompt(self) -> None:
        entry = next(
            c for c in registry.describe_connectors() if c["connector_id"] == "shopify"
        )

        assert entry["asks_for_base_url"] is False
        assert entry["asks_for_account_id"] is True
        assert entry["account_id_label"] == "Shop domain"
        assert entry["account_id_pattern"] == SHOP_DOMAIN_PATTERN


# ---------------------------------------------------------------------------
# The shop domain
# ---------------------------------------------------------------------------


class TestShopDomain:
    """
    The one security control the whole vendor-connector shape rests on: this string
    becomes the host of a request carrying the merchant's access token.
    """

    @pytest.mark.parametrize(
        "domain",
        ["demo-store.myshopify.com", "a.myshopify.com", "shop123.myshopify.com"],
    )
    def test_a_real_shop_domain_is_accepted(self, domain: str) -> None:
        assert is_shop_domain(domain) is True
        assert shopify.SPEC.validated_account_id(domain) == domain

    @pytest.mark.parametrize(
        ("domain", "why"),
        [
            ("evil.com", "not Shopify at all"),
            ("shop.myshopify.com.evil.com", "the suffix trick a 'contains' check misses"),
            ("evil.com/shop.myshopify.com", "a path, not a host"),
            ("shop.myshopify.com/../admin", "path traversal"),
            ("shop.myshopify.com:8080", "a port"),
            ("SHOP.myshopify.com", "uppercase would be a second connection for one shop"),
            ("shop.myshopify.com evil.com", "two hosts"),
            ("user@shop.myshopify.com", "userinfo"),
            ("https://shop.myshopify.com", "a scheme"),
            ("sub.shop.myshopify.com", "shops have no subdomains"),
            ("", "nothing at all"),
        ],
    )
    def test_a_bad_domain_is_refused(self, domain: str, why: str) -> None:
        assert is_shop_domain(domain) is False, why

        with pytest.raises(ValueError):
            shopify.SPEC.validated_account_id(domain)

    @pytest.mark.parametrize(
        "domain",
        ["evil.com", "shop.myshopify.com.evil.com", "SHOP.myshopify.com", ""],
    )
    def test_a_bad_domain_never_becomes_an_address(self, domain: str) -> None:
        """
        The second of the two checks. The first is in ``connection_service``, for the
        person typing; this one protects the request even if some future code path writes
        the column without going through a form.
        """
        with pytest.raises(ValueError):
            shopify.SPEC.render_base_url(Connection(domain))

    def test_a_stored_url_cannot_override_the_computed_one(self) -> None:
        connection = Connection(SHOP, base_url="https://attacker.example.com")

        assert shopify.SPEC.render_base_url(connection) == BASE

    def test_the_refusal_says_what_a_good_one_looks_like(self) -> None:
        with pytest.raises(ValueError, match="your-store.myshopify.com"):
            shopify.SPEC.validated_account_id("evil.com")


# ---------------------------------------------------------------------------
# Building the request
# ---------------------------------------------------------------------------


class TestTheRequest:
    def test_it_is_a_post_to_graphql(self) -> None:
        operation = shopify.SPEC.operation("orders")
        built = request_builder.build_request(operation, {}, base_url=BASE)

        assert built.method == "POST"
        assert built.url == GRAPHQL

    def test_the_document_goes_out_byte_for_byte(self) -> None:
        from app.services.integrations.connectors.shopify import documents

        operation = shopify.SPEC.operation("orders")
        built = request_builder.build_request(operation, {}, base_url=BASE)

        assert built.json_body["query"] == documents.ORDERS

    def test_the_variables_are_typed(self) -> None:
        """``first: "250"`` is a type error in GraphQL, not a coercion."""
        operation = shopify.SPEC.operation("orders")
        built = request_builder.build_request(
            operation, {"page_size": 250, "search": "updated_at:>2026-08-01"},
            base_url=BASE,
        )

        assert built.json_body["variables"]["first"] == 250
        assert built.json_body["variables"]["search"] == "updated_at:>2026-08-01"

    def test_an_unmapped_filter_is_omitted_not_nulled(self) -> None:
        operation = shopify.SPEC.operation("orders")
        built = request_builder.build_request(operation, {"page_size": 250}, base_url=BASE)

        assert built.json_body["variables"] == {"first": 250}


# ---------------------------------------------------------------------------
# The hook — the reason this connector needed runtime work at all
# ---------------------------------------------------------------------------


class TestGreenEmptySync:
    """The failure this connector was built around. See the module docstring."""

    DENIED = {
        "errors": [
            {
                "message": "Access denied for orders field. Required access: `read_orders`.",
                "extensions": {"code": "ACCESS_DENIED"},
            }
        ],
        "data": None,
    }

    @respx.mock
    async def test_a_refusal_inside_a_200_fails_the_node(self) -> None:
        route = respx.post(GRAPHQL).mock(
            return_value=httpx.Response(200, json=self.DENIED)
        )

        operation = shopify.SPEC.operation("orders")
        built = request_builder.build_request(operation, {"page_size": 250}, base_url=BASE)

        with pytest.raises(NodeFailure) as caught:
            await sender.send(built, operation, context())

        # Both halves. The message alone would pass against an implementation that logged
        # the error and carried on returning an empty page — the exact behaviour being
        # prevented — so the single attempt is asserted too.
        assert "read_orders" in str(caught.value)
        assert caught.value.permanent is True
        assert route.call_count == 1

    @respx.mock
    async def test_a_refusal_never_reads_as_an_empty_page(self) -> None:
        """
        The whole-path version: the records are never extracted, because the send raised.
        Without the hook this response yields ``[]`` and the walk stops saying "the last
        page was empty".
        """
        respx.post(GRAPHQL).mock(return_value=httpx.Response(200, json=self.DENIED))

        operation = shopify.SPEC.operation("orders")
        built = request_builder.build_request(operation, {"page_size": 250}, base_url=BASE)

        with pytest.raises(NodeFailure):
            await sender.send(built, operation, context())

    @respx.mock
    async def test_partial_data_beside_an_error_is_still_a_failure(self) -> None:
        """
        Shopify returns both when a nullable field is refused. Taking the rows would be a
        sync that quietly dropped whichever field the token could not read.
        """
        respx.post(GRAPHQL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": {"orders": {"edges": [{"node": {"id": "1"}}]}},
                    "errors": [{"message": "Access denied for email."}],
                },
            )
        )

        operation = shopify.SPEC.operation("orders")
        built = request_builder.build_request(operation, {"page_size": 250}, base_url=BASE)

        with pytest.raises(NodeFailure, match="Access denied for email"):
            await sender.send(built, operation, context())

    @respx.mock
    async def test_a_clean_response_passes_through(self) -> None:
        respx.post(GRAPHQL).mock(
            return_value=httpx.Response(200, json=orders_page("CUR", more=False))
        )

        operation = shopify.SPEC.operation("orders")
        built = request_builder.build_request(operation, {"page_size": 250}, base_url=BASE)

        read = await sender.send(built, operation, context())

        assert read.ok is True
        assert read.payload["data"]["orders"]["edges"]


class TestThrottling:
    @respx.mock
    async def test_throttled_is_retried_and_then_succeeds(self) -> None:
        """A full bucket is not a broken request: the same query a second later works."""
        throttled = httpx.Response(
            200,
            json={
                "errors": [{"message": "Throttled", "extensions": {"code": "THROTTLED"}}],
                "extensions": {
                    "cost": {
                        "requestedQueryCost": 500,
                        "throttleStatus": {
                            "maximumAvailable": 2000.0,
                            "currentlyAvailable": 100.0,
                            "restoreRate": 1000.0,
                        },
                    }
                },
            },
        )
        route = respx.post(GRAPHQL).mock(
            side_effect=[
                throttled,
                httpx.Response(200, json=orders_page("CUR", more=False)),
            ]
        )

        operation = shopify.SPEC.operation("orders")
        built = request_builder.build_request(operation, {"page_size": 250}, base_url=BASE)

        read = await sender.send(built, operation, context())

        assert read.ok is True
        assert route.call_count == 2

    @respx.mock
    async def test_access_denied_is_retried_zero_times(self) -> None:
        """A missing scope stays missing however many times it is asked for."""
        route = respx.post(GRAPHQL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "errors": [
                        {"message": "Denied", "extensions": {"code": "ACCESS_DENIED"}}
                    ]
                },
            )
        )

        operation = shopify.SPEC.operation("orders")
        built = request_builder.build_request(operation, {"page_size": 250}, base_url=BASE)

        with pytest.raises(NodeFailure):
            await sender.send(built, operation, context())

        assert route.call_count == 1

    def test_the_bucket_is_lowered_from_the_response(self) -> None:
        """
        Shopify's bucket is per shop and shared with every other app the merchant
        installed, so a locally-computed rate is always optimistic. This is the only real
        number available.
        """
        ctx = context()
        bucket = rate_limiter.limiter.bucket("shopify-1", shopify.SPEC.rate_limits)
        before = bucket.tokens

        payload = orders_page("CUR", more=False)
        payload["extensions"]["cost"]["throttleStatus"]["currentlyAvailable"] = 20.0

        ShopifyHooks().after_response(_Read(payload), shopify.SPEC.operation("orders"), ctx)

        assert bucket.tokens < before

    def test_a_response_without_a_cost_block_leaves_the_bucket_alone(self) -> None:
        ctx = context()
        bucket = rate_limiter.limiter.bucket("shopify-1", shopify.SPEC.rate_limits)
        before = bucket.tokens

        ShopifyHooks().after_response(
            _Read({"data": {"orders": {"edges": []}}}),
            shopify.SPEC.operation("orders"),
            ctx,
        )

        assert bucket.tokens == before

    def test_a_throttled_response_still_teaches_the_bucket(self) -> None:
        """
        The correction runs before the failure is raised. Getting that the other way round
        would mean the one response that knows the bucket is empty is the one whose
        reading is discarded.
        """
        ctx = context()
        bucket = rate_limiter.limiter.bucket("shopify-1", shopify.SPEC.rate_limits)

        payload = {
            "errors": [{"message": "Throttled", "extensions": {"code": "THROTTLED"}}],
            "extensions": {
                "cost": {
                    "requestedQueryCost": 500,
                    "throttleStatus": {
                        "maximumAvailable": 2000.0,
                        "currentlyAvailable": 0.0,
                        "restoreRate": 100.0,
                    },
                }
            },
        }

        with pytest.raises(NodeFailure):
            ShopifyHooks().after_response(
                _Read(payload), shopify.SPEC.operation("orders"), ctx
            )

        assert bucket.tokens == 0.0

    def test_the_wait_comes_from_shopifys_own_numbers(self) -> None:
        """500 points needed, 100 available, restoring at 100/s — four seconds, not the
        retry engine's 0.5."""
        payload = {
            "errors": [{"message": "Throttled", "extensions": {"code": "THROTTLED"}}],
            "extensions": {
                "cost": {
                    "requestedQueryCost": 500,
                    "throttleStatus": {
                        "maximumAvailable": 2000.0,
                        "currentlyAvailable": 100.0,
                        "restoreRate": 100.0,
                    },
                }
            },
        }

        with pytest.raises(NodeFailure) as caught:
            ShopifyHooks().after_response(
                _Read(payload), shopify.SPEC.operation("orders"), context()
            )

        assert caught.value.retry_after == pytest.approx(4.0)
        assert caught.value.retryable is True

    def test_a_ridiculous_wait_is_clamped(self) -> None:
        payload = {
            "errors": [{"message": "Throttled", "extensions": {"code": "THROTTLED"}}],
            "extensions": {
                "cost": {
                    "requestedQueryCost": 100000,
                    "throttleStatus": {
                        "maximumAvailable": 2000.0,
                        "currentlyAvailable": 0.0,
                        "restoreRate": 1.0,
                    },
                }
            },
        }

        with pytest.raises(NodeFailure) as caught:
            ShopifyHooks().after_response(
                _Read(payload), shopify.SPEC.operation("orders"), context()
            )

        assert caught.value.retry_after == 60.0


class TestErrorReporting:
    def test_several_messages_are_capped(self) -> None:
        payload = {
            "errors": [{"message": f"Problem {n}"} for n in range(6)],
        }

        with pytest.raises(NodeFailure) as caught:
            ShopifyHooks().after_response(
                _Read(payload), shopify.SPEC.operation("orders"), context()
            )

        assert "and 3 more" in str(caught.value)

    def test_an_error_with_no_message_still_says_something(self) -> None:
        with pytest.raises(NodeFailure, match="did not say what"):
            ShopifyHooks().after_response(
                _Read({"errors": [{"extensions": {"code": "WHATEVER"}}]}),
                shopify.SPEC.operation("orders"),
                context(),
            )

    def test_an_empty_errors_array_is_not_a_failure(self) -> None:
        """A vendor that sends ``"errors": []`` on success is saying nothing is wrong."""
        ShopifyHooks().after_response(
            _Read({"errors": [], "data": {"orders": {"edges": []}}}),
            shopify.SPEC.operation("orders"),
            context(),
        )

    def test_a_non_mapping_payload_is_ignored(self) -> None:
        """A list body is not a GraphQL response, and guessing at one would be worse than
        letting the record path report what it found."""
        ShopifyHooks().after_response(
            _Read([1, 2, 3]), shopify.SPEC.operation("orders"), context()
        )

    def test_the_failure_records_no_status(self) -> None:
        """A GraphQL error has no status of its own. Recording the 200 would be a lie in
        the audit and inventing a 4xx a worse one."""
        with pytest.raises(NodeFailure) as caught:
            ShopifyHooks().after_response(
                _Read({"errors": [{"message": "no"}]}),
                shopify.SPEC.operation("orders"),
                context(),
            )

        assert caught.value.status_code is None


# ---------------------------------------------------------------------------
# Paging, end to end
# ---------------------------------------------------------------------------


class TestPagingOverTheWire:
    @respx.mock
    async def test_the_cursor_travels_in_the_body(self) -> None:
        """
        **Asserted against the recorded bodies, not the URLs.** Both requests go to the
        same ``/graphql.json`` with no query string, so a URL assertion would pass whether
        or not the cursor ever moved.
        """
        sent: List[Dict[str, Any]] = []

        def record(request: httpx.Request) -> httpx.Response:
            sent.append(json.loads(request.content))
            page = len(sent)
            return httpx.Response(
                200, json=orders_page(f"CUR{page + 1}", more=page < 2)
            )

        respx.post(GRAPHQL).mock(side_effect=record)

        operation = shopify.SPEC.operation("orders")
        walk = pagination.begin(operation.page_rule, GRAPHQL)

        for _ in range(2):
            built = request_builder.build_request(
                operation, dict(walk.arguments or {}), base_url=BASE
            )
            read = await sender.send(built, operation, context())
            pagination.advance(
                walk, payload=read.payload, headers=read.headers, records_in_page=2
            )

        assert "after" not in sent[0]["variables"]
        assert sent[1]["variables"]["after"] == "CUR2"
        assert sent[0]["variables"]["first"] == 250
        assert walk.finished is True

    @respx.mock
    async def test_both_pages_go_to_the_same_url(self) -> None:
        """The fact that makes the previous test's phrasing necessary."""
        urls: List[str] = []

        def record(request: httpx.Request) -> httpx.Response:
            urls.append(str(request.url))
            return httpx.Response(200, json=orders_page("CUR2", more=len(urls) < 2))

        respx.post(GRAPHQL).mock(side_effect=record)

        operation = shopify.SPEC.operation("orders")
        walk = pagination.begin(operation.page_rule, GRAPHQL)

        for _ in range(2):
            built = request_builder.build_request(
                operation, dict(walk.arguments or {}), base_url=BASE
            )
            read = await sender.send(built, operation, context())
            pagination.advance(
                walk, payload=read.payload, headers=read.headers, records_in_page=2
            )

        assert urls[0] == urls[1] == GRAPHQL

    @respx.mock
    async def test_the_records_come_out_of_the_edges(self) -> None:
        from app.services.integrations.mapping import paths

        respx.post(GRAPHQL).mock(
            return_value=httpx.Response(200, json=orders_page("C", more=False, count=3))
        )

        operation = shopify.SPEC.operation("orders")
        built = request_builder.build_request(operation, {"page_size": 250}, base_url=BASE)
        read = await sender.send(built, operation, context())

        records = paths.read_records(read.payload, operation.records_path)

        assert len(records) == 3
        assert records[0]["id"] == "gid://shopify/Order/0"

    @respx.mock
    async def test_the_token_is_sent_in_shopifys_header(self) -> None:
        seen: List[httpx.Headers] = []

        def record(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers)
            return httpx.Response(200, json=orders_page("C", more=False))

        respx.post(GRAPHQL).mock(side_effect=record)

        operation = shopify.SPEC.operation("orders")
        built = request_builder.build_request(operation, {"page_size": 250}, base_url=BASE)
        await sender.send(built, operation, context())

        assert seen[0]["X-Shopify-Access-Token"] == "shpat_0123456789abcdef"


class TestSecretsStayOut:
    @respx.mock
    async def test_a_vendor_echoing_the_token_does_not_leak_it(self) -> None:
        """
        Searched for by value across the whole message, not checked field by field. The
        key-name deny-list cannot see a secret embedded in a vendor's prose, and this is
        the shape that caught a real leak in Phase 1.
        """
        token = "shpat_0123456789abcdef"
        respx.post(GRAPHQL).mock(
            return_value=httpx.Response(
                401, json={"errors": f"Invalid API key or access token ({token})"}
            )
        )

        operation = shopify.SPEC.operation("orders")
        built = request_builder.build_request(operation, {"page_size": 250}, base_url=BASE)

        with pytest.raises(NodeFailure) as caught:
            await sender.send(built, operation, context())

        assert token not in str(caught.value)


class _Read:
    """A ``ReadResponse``'s duck type, for the hook tests that need no socket."""

    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code
        self.headers: Dict[str, str] = {}
        self.text = ""
        self.ok = 200 <= status_code < 300


class TestTheConnectionTest:
    """
    The Test button, which is the first thing anybody presses after adding a connection.

    It builds a request with page one's arguments rather than none. Shopify's documents
    declare ``$first: Int!``, so a test that sent no page size would fail on *every*
    Shopify connection ever created — and fail with a message about a GraphQL variable,
    which tells the owner nothing about their connection.
    """

    @respx.mock
    async def test_the_test_request_carries_a_page_size(self) -> None:
        sent: List[Dict[str, Any]] = []

        def record(request: httpx.Request) -> httpx.Response:
            sent.append(json.loads(request.content))
            return httpx.Response(200, json=orders_page("C", more=False))

        respx.post(GRAPHQL).mock(side_effect=record)

        operation = shopify.SPEC.operation("orders")
        built = request_builder.build_request(
            operation,
            pagination.first_page_arguments(operation.page_rule),
            base_url=BASE,
        )
        await sender.send(built, operation, context())

        assert sent[0]["variables"]["first"] == shopify.MAX_PAGE_SIZE

    def test_an_unpaged_operation_still_gets_no_arguments(self) -> None:
        """The seeding is the page rule's business, so a connector without one is
        unaffected — this had to not change generic REST."""
        from app.services.integrations.connectors.spec import PageRule

        assert pagination.first_page_arguments(PageRule()) == {}
