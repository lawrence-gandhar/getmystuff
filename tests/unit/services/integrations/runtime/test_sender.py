"""
Tests for ``runtime/sender.py`` — the module where the guards meet.

**Why ``respx`` rather than a stubbed client.** ``respx`` intercepts at the httpx
*transport* layer, so ``block_network``'s socket guard never fires and the real pooling,
streaming, byte-cap and retry code actually runs. ``mock_outbound_http``'s
replace-the-client approach would stub out exactly the code under test. ``respx`` is
already in ``requirements-dev.txt``.

DNS is stubbed on the running loop for the same reason it is in
``tests/unit/utils/test_outbound_http.py``: a test that depended on live DNS would be
flaky and would be asserting somebody else's zone file.

The assertion that matters most is in ``TestWriteSafety``: **a read timeout on a
non-idempotent write is attempted exactly once.** Shopify's ``POST /orders.json`` has no
idempotency header, so retrying a create that timed out mid-flight duplicates a real
order in somebody's real business.
"""

from __future__ import annotations

import socket
from typing import Any, List

import httpx
import pytest
import respx

from app.services.integrations.connectors.spec import (
    ConnectorSpec,
    OperationSpec,
    PreparedRequest,
    RateLimitSpec,
)
from app.services.integrations.errors import NodeFailure
from app.services.integrations.runtime import http_client, rate_limiter, sender

BASE = "https://api.example.com"


@pytest.fixture(autouse=True)
async def clean_runtime(monkeypatch: pytest.MonkeyPatch):
    """
    A fresh client pool and rate limiter per test, and DNS that answers.

    The pool and the limiter are module state; leaking one would make a test's waiting
    another's, and a leaked client would hold a transport ``respx`` has stopped mocking.
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


CONNECTOR = ConnectorSpec(
    connector_id="rest_generic",
    label="REST API",
    base_url_is_user_supplied=True,
    operations_are_user_defined=True,
    rate_limits=RateLimitSpec(requests_per_second=1000.0, burst=1000),
)


def read_operation(**overrides) -> OperationSpec:
    defaults = dict(operation_id="list_orders", kind="read", method="GET", path="/orders")
    defaults.update(overrides)
    return OperationSpec(**defaults).validated()


def write_operation(**overrides) -> OperationSpec:
    defaults = dict(
        operation_id="create_order", kind="write", method="POST", path="/orders"
    )
    defaults.update(overrides)
    return OperationSpec(**defaults).validated()


def request_for(operation: OperationSpec, body: Any = None) -> PreparedRequest:
    return PreparedRequest(
        method=operation.method,
        url=f"{BASE}{operation.path}",
        headers={"Accept": "application/json"},
        params={},
        json_body=body,
        host="api.example.com",
        path=operation.path,
    )


def context(**overrides) -> sender.SendContext:
    defaults = dict(
        connection_key="conn-1",
        connection_label="Billing API",
        connector=CONNECTOR,
    )
    defaults.update(overrides)
    return sender.SendContext(**defaults)


class TestASuccessfulCall:
    @respx.mock
    async def test_it_returns_the_parsed_body(self) -> None:
        respx.get(f"{BASE}/orders").mock(
            return_value=httpx.Response(200, json={"orders": [{"id": 1}]})
        )

        read = await sender.send(request_for(read_operation()), read_operation(), context())

        assert read.ok is True
        assert read.payload == {"orders": [{"id": 1}]}

    @respx.mock
    async def test_the_credential_is_applied_and_nothing_else_holds_it(self) -> None:
        """
        The credential is attached at the socket, after the destination is known good.
        A token on a request that then turned out to point somewhere private would be a
        token sent somewhere private.
        """
        route = respx.get(f"{BASE}/orders").mock(
            return_value=httpx.Response(200, json={})
        )
        prepared = request_for(read_operation())

        await sender.send(
            prepared,
            read_operation(),
            context(auth_header=("Authorization", "Bearer sk-live-1")),
        )

        assert route.calls.last.request.headers["Authorization"] == "Bearer sk-live-1"
        # The object the engine logs and hashes never held it.
        assert "Authorization" not in prepared.headers

    @respx.mock
    async def test_a_body_is_sent_as_compact_json(self) -> None:
        route = respx.post(f"{BASE}/orders").mock(
            return_value=httpx.Response(201, json={"id": 9})
        )

        operation = write_operation()
        await sender.send(
            request_for(operation, {"email": "a@b.com"}), operation, context()
        )

        assert route.calls.last.request.content == b'{"email":"a@b.com"}'

    @respx.mock
    async def test_the_rate_view_is_taken_from_the_response(self) -> None:
        respx.get(f"{BASE}/orders").mock(
            return_value=httpx.Response(
                200, json={}, headers={"X-Shopify-Shop-Api-Call-Limit": "39/40"}
            )
        )

        await sender.send(request_for(read_operation()), read_operation(), context())

        bucket = rate_limiter.limiter.bucket("conn-1", CONNECTOR.rate_limits)
        assert bucket.tokens == pytest.approx(1000.0 * (1 / 40), abs=0.5)


class TestTheDestinationIsCheckedFirst:
    @respx.mock
    async def test_a_private_address_is_refused_before_any_request(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import asyncio

        async def _private(host, port, **kwargs):  # noqa: ANN001, ANN003
            return [
                (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("10.0.0.5", port))
            ]

        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", _private)
        route = respx.get(f"{BASE}/orders").mock(return_value=httpx.Response(200, json={}))

        with pytest.raises(NodeFailure, match="private or internal address"):
            await sender.send(request_for(read_operation()), read_operation(), context())

        assert route.call_count == 0

    @respx.mock
    async def test_the_refusal_is_permanent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A URL pointing somewhere private will still point somewhere private in half a
        second, so retrying is pure cost."""
        import asyncio

        async def _private(host, port, **kwargs):  # noqa: ANN001, ANN003
            return [
                (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", port))
            ]

        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", _private)

        with pytest.raises(NodeFailure) as caught:
            await sender.send(request_for(read_operation()), read_operation(), context())

        assert caught.value.permanent is True


class TestRedirectsAreNotFollowed:
    @respx.mock
    async def test_a_302_is_refused_rather_than_followed(self) -> None:
        """
        The classic way past an SSRF check: the URL that was validated returns a 302 to
        one that was not.
        """
        respx.get(f"{BASE}/orders").mock(
            return_value=httpx.Response(302, headers={"location": "https://evil.example.com/"})
        )

        with pytest.raises(NodeFailure, match="redirect"):
            await sender.send(request_for(read_operation()), read_operation(), context())

    @respx.mock
    async def test_the_message_points_at_the_likely_cause(self) -> None:
        respx.get(f"{BASE}/orders").mock(return_value=httpx.Response(301, headers={"location": "/x"}))

        with pytest.raises(NodeFailure, match="sign-in page"):
            await sender.send(request_for(read_operation()), read_operation(), context())


class TestFailedStatuses:
    @respx.mock
    async def test_a_401_is_not_retried(self) -> None:
        """
        A provider that counts consecutive auth failures is one that will lock the
        connection out. The answer is a person reconnecting, not another attempt.
        """
        route = respx.get(f"{BASE}/orders").mock(
            return_value=httpx.Response(401, json={"message": "Invalid API key"})
        )

        with pytest.raises(NodeFailure) as caught:
            await sender.send(request_for(read_operation()), read_operation(), context())

        assert route.call_count == 1
        assert "Reconnect it" in str(caught.value)
        assert "Invalid API key" in str(caught.value)

    @respx.mock
    async def test_a_503_on_a_read_is_retried(self) -> None:
        route = respx.get(f"{BASE}/orders").mock(
            side_effect=[
                httpx.Response(503, json={"message": "restarting"}),
                httpx.Response(200, json={"orders": []}),
            ]
        )

        read = await sender.send(
            request_for(read_operation()), read_operation(), context()
        )

        assert route.call_count == 2
        assert read.ok is True

    @respx.mock
    async def test_a_404_is_not_retried(self) -> None:
        """It will be equally absent in half a second."""
        route = respx.get(f"{BASE}/orders").mock(return_value=httpx.Response(404, json={}))

        with pytest.raises(NodeFailure):
            await sender.send(request_for(read_operation()), read_operation(), context())

        assert route.call_count == 1

    @respx.mock
    async def test_the_vendors_own_message_reaches_the_operator(self) -> None:
        respx.get(f"{BASE}/orders").mock(
            return_value=httpx.Response(422, json={"message": "since must be a date"})
        )

        with pytest.raises(NodeFailure, match="since must be a date"):
            await sender.send(request_for(read_operation()), read_operation(), context())


class TestWriteSafety:
    """The rule that protects a merchant's store. See the module docstring."""

    @respx.mock
    async def test_a_read_timeout_on_a_plain_write_is_attempted_exactly_once(self) -> None:
        operation = write_operation()
        route = respx.post(f"{BASE}/orders").mock(
            side_effect=httpx.ReadTimeout("no response")
        )

        with pytest.raises(NodeFailure) as caught:
            await sender.send(request_for(operation, {"a": 1}), operation, context())

        assert route.call_count == 1
        assert caught.value.permanent is True

    @respx.mock
    async def test_the_message_tells_them_to_check_before_running_it_again(self) -> None:
        """
        "It failed" is the wrong thing to tell somebody whose order may or may not have
        been created.
        """
        operation = write_operation()
        respx.post(f"{BASE}/orders").mock(side_effect=httpx.ReadTimeout("no response"))

        with pytest.raises(NodeFailure) as caught:
            await sender.send(request_for(operation, {"a": 1}), operation, context())

        assert "may or may not have gone through" in str(caught.value)
        assert "Check the destination" in str(caught.value)

    @respx.mock
    async def test_a_connect_error_on_a_write_is_retried(self) -> None:
        """It provably never reached the server, so nothing can have happened twice."""
        operation = write_operation()
        route = respx.post(f"{BASE}/orders").mock(
            side_effect=[httpx.ConnectError("refused"), httpx.Response(201, json={"id": 1})]
        )

        read = await sender.send(request_for(operation, {"a": 1}), operation, context())

        assert route.call_count == 2
        assert read.ok is True

    @respx.mock
    async def test_an_idempotent_write_may_be_retried_after_a_timeout(self) -> None:
        operation = write_operation(idempotent=True)
        route = respx.post(f"{BASE}/orders").mock(
            side_effect=[httpx.ReadTimeout("no response"), httpx.Response(201, json={})]
        )

        await sender.send(request_for(operation, {"a": 1}), operation, context())

        assert route.call_count == 2

    @respx.mock
    async def test_an_idempotency_header_earns_the_retry_too(self) -> None:
        operation = write_operation(idempotency_header="Idempotency-Key")
        route = respx.post(f"{BASE}/orders").mock(
            side_effect=[httpx.ReadTimeout("no response"), httpx.Response(201, json={})]
        )

        await sender.send(request_for(operation, {"a": 1}), operation, context())

        assert route.call_count == 2

    @respx.mock
    async def test_a_500_on_a_plain_write_is_not_retried(self) -> None:
        """
        A 5xx may have been applied before the error was generated — the record could
        exist. 429 is the exception, because it means the request was rejected before any
        work was done.
        """
        operation = write_operation()
        route = respx.post(f"{BASE}/orders").mock(return_value=httpx.Response(500, json={}))

        with pytest.raises(NodeFailure):
            await sender.send(request_for(operation, {"a": 1}), operation, context())

        assert route.call_count == 1

    @respx.mock
    async def test_a_429_on_a_plain_write_is_retried(self) -> None:
        operation = write_operation()
        route = respx.post(f"{BASE}/orders").mock(
            side_effect=[
                httpx.Response(429, json={}, headers={"retry-after": "0"}),
                httpx.Response(201, json={}),
            ]
        )

        await sender.send(request_for(operation, {"a": 1}), operation, context())

        assert route.call_count == 2


class TestTheByteCapRunsForReal:
    @respx.mock
    async def test_an_oversize_response_is_refused(self) -> None:
        """
        Through the real streaming path rather than a stub, which is the point of using
        ``respx`` at the transport layer.
        """
        from app.services.integrations.runtime import response_reader

        big = b'{"x":"' + b"y" * (response_reader.MAX_RESPONSE_BYTES + 10) + b'"}'
        respx.get(f"{BASE}/orders").mock(
            return_value=httpx.Response(
                200, content=big, headers={"content-type": "application/json"}
            )
        )

        with pytest.raises(NodeFailure, match="Reduce the batch size"):
            await sender.send(request_for(read_operation()), read_operation(), context())


class TestTheClientPool:
    @respx.mock
    async def test_two_calls_to_one_origin_share_a_client(self) -> None:
        """A forty-page sync must not pay forty TLS handshakes."""
        respx.get(f"{BASE}/orders").mock(return_value=httpx.Response(200, json={}))

        await sender.send(request_for(read_operation()), read_operation(), context())
        await sender.send(request_for(read_operation()), read_operation(), context())

        assert http_client.open_origins() == ["https://api.example.com"]

    async def test_closing_releases_everything(self) -> None:
        http_client.get_client("https://a.example.com")
        http_client.get_client("https://b.example.com")

        await http_client.close_all_clients()

        assert http_client.open_origins() == []

    def test_a_declared_timeout_is_clamped(self) -> None:
        """
        An operation declaring an hour would hold a worker for an hour on one call, and
        a run that never finishes is indistinguishable from one that hung.
        """
        assert http_client.clamp_timeout(3600) == http_client.MAX_TIMEOUT_SECONDS
        assert http_client.clamp_timeout(None) == http_client.DEFAULT_TIMEOUT_SECONDS
        assert http_client.clamp_timeout(15) == 15.0


class TestTheHookFence:
    @respx.mock
    async def test_a_hook_may_add_a_header(self) -> None:
        class Hooks:
            def before_request(self, request, ctx):  # noqa: ANN001, ANN201
                return request.with_headers({"X-CSRF-Token": "abc"})

        route = respx.get(f"{BASE}/orders").mock(return_value=httpx.Response(200, json={}))
        connector = ConnectorSpec(
            connector_id="sap_odata",
            label="SAP",
            base_url_is_user_supplied=True,
            hooks=Hooks(),
            rate_limits=RateLimitSpec(requests_per_second=1000.0, burst=1000),
        )

        await sender.send(
            request_for(read_operation()), read_operation(), context(connector=connector)
        )

        assert route.calls.last.request.headers["X-CSRF-Token"] == "abc"

    @respx.mock
    async def test_a_hook_that_moves_the_request_is_refused(self) -> None:
        """
        The host was approved by the egress guard *before* the hook ran, and the
        recorded ``operation_hash`` claims a particular request. A hook that changed
        either would make both wrong.
        """
        from dataclasses import replace

        from app.services.integrations.connectors.spec import HookViolation

        class Hooks:
            def before_request(self, request, ctx):  # noqa: ANN001, ANN201
                return replace(request, host="evil.example.com")

        route = respx.get(f"{BASE}/orders").mock(return_value=httpx.Response(200, json={}))
        connector = ConnectorSpec(
            connector_id="sap_odata",
            label="SAP",
            base_url_is_user_supplied=True,
            hooks=Hooks(),
            rate_limits=RateLimitSpec(requests_per_second=1000.0, burst=1000),
        )

        with pytest.raises(HookViolation):
            await sender.send(
                request_for(read_operation()), read_operation(), context(connector=connector)
            )

        assert route.call_count == 0


class TestTheDailyCap:
    @respx.mock
    async def test_it_is_spent_before_the_request(self) -> None:
        spent: List[int] = []

        async def bump(cost: int) -> int:
            spent.append(cost)
            return len(spent)

        route = respx.get(f"{BASE}/orders").mock(return_value=httpx.Response(200, json={}))
        connector = ConnectorSpec(
            connector_id="ghl",
            label="GoHighLevel",
            base_url_is_user_supplied=True,
            rate_limits=RateLimitSpec(
                requests_per_second=1000.0, burst=1000, daily_limit=200_000
            ),
        )

        await sender.send(
            request_for(read_operation()),
            read_operation(),
            context(connector=connector, bump_daily=bump),
        )

        assert spent == [1]
        assert route.call_count == 1

    @respx.mock
    async def test_exceeding_it_stops_before_the_request(self) -> None:
        async def bump(cost: int) -> int:
            return 200_000

        route = respx.get(f"{BASE}/orders").mock(return_value=httpx.Response(200, json={}))
        connector = ConnectorSpec(
            connector_id="ghl",
            label="GoHighLevel",
            base_url_is_user_supplied=True,
            rate_limits=RateLimitSpec(
                requests_per_second=1000.0, burst=1000, daily_limit=200_000
            ),
        )

        with pytest.raises(NodeFailure, match="suspended"):
            await sender.send(
                request_for(read_operation()),
                read_operation(),
                context(connector=connector, bump_daily=bump),
            )

        assert route.call_count == 0


class TestTheCredentialIsScrubbedFromWhatComesBack:
    """
    A vendor's own error text, with this connection's credential taken back out of it.

    ``response_reader.redact`` is a deny-list over **key names**, which is right for a body
    carrying a secret in a field and useless against one embedded in prose — and prose is
    exactly what a surprising number of APIs return: ``{"error": "invalid key sk-live-…"}``
    or a 400 quoting the query string it was handed. The key is ``error``; the secret is in
    the value.

    These messages reach a browser, a run record and a log line. Any one of those is
    somewhere a token must not be, which is why the scrub happens in the sender — the only
    layer that knows what the credential actually is.
    """

    KEY = "sk-live-0123456789abcdef"

    @respx.mock
    async def test_a_header_credential_echoed_in_an_error_is_removed(self) -> None:
        respx.get(f"{BASE}/orders").mock(
            return_value=httpx.Response(
                400, json={"error": f"the key {self.KEY} is not valid for this shop"}
            )
        )

        with pytest.raises(NodeFailure) as caught:
            await sender.send(
                request_for(read_operation()),
                read_operation(),
                context(auth_header=("Authorization", f"Bearer {self.KEY}")),
            )

        assert self.KEY not in str(caught.value)
        assert sender.REDACTED in str(caught.value)

    @respx.mock
    async def test_a_query_credential_echoed_in_an_error_is_removed(self) -> None:
        """The commoner case, because an API key in a query string ends up in the vendor's
        access log and its error messages without anybody intending it."""
        respx.get(f"{BASE}/orders").mock(
            return_value=httpx.Response(403, json={"message": f"api_key={self.KEY} denied"})
        )

        with pytest.raises(NodeFailure) as caught:
            await sender.send(
                request_for(read_operation()),
                read_operation(),
                context(auth_query=("api_key", self.KEY)),
            )

        assert self.KEY not in str(caught.value)

    @respx.mock
    async def test_the_rest_of_the_vendors_message_survives(self) -> None:
        """
        The scrub is not a reason to throw the explanation away. 'Email has already been
        taken' is worth more than 'the write failed', and it is the sentence the operator
        acts on.
        """
        respx.get(f"{BASE}/orders").mock(
            return_value=httpx.Response(
                422, json={"message": f"email has already been taken (key {self.KEY})"}
            )
        )

        with pytest.raises(NodeFailure) as caught:
            await sender.send(
                request_for(read_operation()),
                read_operation(),
                context(auth_header=("Authorization", f"Bearer {self.KEY}")),
            )

        assert "email has already been taken" in str(caught.value)

    def test_a_very_short_credential_is_left_alone(self) -> None:
        """
        Below ``MIN_SCRUBBED_LENGTH`` the scrub would match ordinary words and replace half
        the sentence — and a credential that short is not a secret worth the message.
        """
        text = "the request was rejected"

        cleaned = sender.scrubbed(text, context(auth_header=("Authorization", "ab")))

        assert cleaned == text


class TestAfterResponseHook:
    """
    The seam for an API that reports failure inside a success.

    Shopify's Admin GraphQL answers a missing scope with HTTP 200 and an ``errors``
    array. Without somewhere to look at that, the read finds no records, paging stops
    because "the last page was empty", and the run ends **green** — a refused sync and a
    sync of an empty store become indistinguishable.

    The fence is the mirror of ``assert_hook_kept_the_target``: the hook may only raise,
    and anything it returns is discarded. A hook able to rewrite a response could make the
    recorded step disagree with what the vendor actually sent.
    """

    @staticmethod
    def connector_with(hooks: Any) -> ConnectorSpec:
        return ConnectorSpec(
            connector_id="vendor",
            label="Vendor",
            base_url_is_user_supplied=True,
            rate_limits=RateLimitSpec(requests_per_second=1000.0, burst=1000),
            hooks=hooks,
        )

    @respx.mock
    async def test_a_hook_turns_a_200_into_a_failure(self) -> None:
        class Hooks:
            def after_response(self, read, operation, context) -> None:  # noqa: ANN001
                if isinstance(read.payload, dict) and read.payload.get("errors"):
                    raise NodeFailure("Access denied for orders field.", permanent=True)

        respx.get(f"{BASE}/orders").mock(
            return_value=httpx.Response(200, json={"errors": [{"message": "nope"}]})
        )

        operation = read_operation()

        with pytest.raises(NodeFailure, match="Access denied"):
            await sender.send(
                request_for(operation),
                operation,
                context(connector=self.connector_with(Hooks())),
            )

    @respx.mock
    async def test_a_permanent_hook_failure_is_not_retried(self) -> None:
        """A missing scope stays missing. Eight backoffs is eight requests spent proving
        what the first one said."""
        route = respx.get(f"{BASE}/orders").mock(
            return_value=httpx.Response(200, json={"errors": [{"message": "nope"}]})
        )

        class Hooks:
            def after_response(self, read, operation, context) -> None:  # noqa: ANN001
                raise NodeFailure("Refused.", permanent=True)

        operation = read_operation()

        with pytest.raises(NodeFailure):
            await sender.send(
                request_for(operation),
                operation,
                context(connector=self.connector_with(Hooks())),
            )

        assert route.call_count == 1

    @respx.mock
    async def test_a_hook_that_returns_a_value_has_it_ignored(self) -> None:
        """The fence on this side. The response the caller gets is the one that arrived."""

        class Hooks:
            def after_response(self, read, operation, context):  # noqa: ANN001, ANN201
                return {"replaced": True}

        respx.get(f"{BASE}/orders").mock(
            return_value=httpx.Response(200, json={"orders": [{"id": 1}]})
        )

        operation = read_operation()
        read = await sender.send(
            request_for(operation),
            operation,
            context(connector=self.connector_with(Hooks())),
        )

        assert read.payload == {"orders": [{"id": 1}]}

    @respx.mock
    async def test_it_sees_the_parsed_body(self) -> None:
        seen: List[Any] = []

        class Hooks:
            def after_response(self, read, operation, context) -> None:  # noqa: ANN001
                seen.append((read.payload, operation.operation_id, read.status_code))

        respx.get(f"{BASE}/orders").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )

        operation = read_operation()
        await sender.send(
            request_for(operation),
            operation,
            context(connector=self.connector_with(Hooks())),
        )

        assert seen == [({"ok": True}, "list_orders", 200)]

    @respx.mock
    async def test_a_connector_with_no_hook_is_unaffected(self) -> None:
        respx.get(f"{BASE}/orders").mock(
            return_value=httpx.Response(200, json={"orders": []})
        )

        operation = read_operation()
        read = await sender.send(request_for(operation), operation, context())

        assert read.ok is True

    @respx.mock
    async def test_the_hook_runs_before_the_status_is_judged(self) -> None:
        """
        Order matters for a vendor that explains a 4xx in its body. The hook's sentence
        names the actual cause; the status failure would only say "400".
        """

        class Hooks:
            def after_response(self, read, operation, context) -> None:  # noqa: ANN001
                raise NodeFailure("The shop said why.", permanent=True)

        respx.get(f"{BASE}/orders").mock(
            return_value=httpx.Response(400, json={"errors": [{"message": "bad"}]})
        )

        operation = read_operation()

        with pytest.raises(NodeFailure, match="The shop said why"):
            await sender.send(
                request_for(operation),
                operation,
                context(connector=self.connector_with(Hooks())),
            )
