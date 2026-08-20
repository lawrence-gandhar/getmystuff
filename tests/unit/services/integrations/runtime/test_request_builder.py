"""
Tests for ``runtime/request_builder.py``.

The function is pure, so this file is a table: given an operation and some values, what
goes on the wire. That is the payoff the "operations are data" decision was for — every
URL-escaping and injection question in the module lives in one place and can be
exhausted here rather than reasoned about.

Three assertions carry the weight:

**A path parameter cannot escape its segment.** ``quote(safe="")`` escapes ``/`` too, so
an order id of ``../../admin/users`` becomes one segment rather than a request for
somewhere nobody drew.

**The body is assembled, never concatenated.** A quantity of 3 arrives as ``3`` and not
``"3"``, and there is no template a user or a model can write that produces invalid JSON.

**A header carrying CR or LF is refused.** Response splitting, checked here because the
value can come from a record and the record comes from somebody else's system.
"""

from __future__ import annotations

import json

import pytest

from app.services.integrations.connectors.spec import FieldSpec, OperationSpec
from app.services.integrations.runtime.request_builder import build_request, serialise_body

BASE = "https://api.example.com"


def operation(**overrides) -> OperationSpec:
    defaults = dict(
        operation_id="list_orders",
        label="List orders",
        kind="read",
        method="GET",
        path="/orders",
    )
    defaults.update(overrides)
    return OperationSpec(**defaults).validated()


class TestTheUrl:
    def test_a_plain_path(self) -> None:
        request = build_request(operation(), base_url=BASE)

        assert request.url == "https://api.example.com/orders"
        assert request.method == "GET"
        assert request.host == "api.example.com"

    def test_the_bases_own_path_is_kept(self) -> None:
        """
        ``https://x.example.com/api/v2`` plus ``/orders`` is ``/api/v2/orders``. A vendor
        whose API lives under a prefix is the ordinary case, and ``urljoin`` would
        discard it.
        """
        request = build_request(operation(), base_url="https://api.example.com/api/v2")

        assert request.url == "https://api.example.com/api/v2/orders"

    def test_a_trailing_slash_on_the_base_does_not_double(self) -> None:
        request = build_request(operation(), base_url="https://api.example.com/")

        assert request.url == "https://api.example.com/orders"

    def test_a_path_parameter_is_filled(self) -> None:
        spec = operation(
            path="/orders/{order_id}",
            inputs=(FieldSpec(name="order_id", type="string", required=True),),
        )

        request = build_request(spec, {"order_id": "1234"}, base_url=BASE)

        assert request.url == "https://api.example.com/orders/1234"


class TestPathEscaping:
    """The assertion in the module docstring. Each of these is one request for
    somewhere the author did not write."""

    @pytest.mark.parametrize(
        ("value", "must_not_contain"),
        [
            ("../../admin/users", "/"),
            ("a/b", "/"),
            ("a?b=c", "?"),
            ("a#b", "#"),
            ("a&b", "&"),
        ],
    )
    def test_a_value_cannot_escape_its_segment(
        self, value: str, must_not_contain: str
    ) -> None:
        spec = operation(
            path="/orders/{order_id}",
            inputs=(FieldSpec(name="order_id", type="string", required=True),),
        )

        request = build_request(spec, {"order_id": value}, base_url=BASE)

        after_prefix = request.url[len("https://api.example.com/orders/") :]
        assert must_not_contain not in after_prefix

    def test_a_space_is_percent_encoded(self) -> None:
        spec = operation(
            path="/customers/{name}",
            inputs=(FieldSpec(name="name", type="string", required=True),),
        )

        request = build_request(spec, {"name": "Jane Doe"}, base_url=BASE)

        assert request.url.endswith("/customers/Jane%20Doe")


class TestQuery:
    def test_a_literal_stays_a_literal(self) -> None:
        """
        A template of ``{"limit": 250}`` is a literal, and stringifying it would send
        text where the API documented a number.
        """
        request = build_request(operation(query_template={"limit": 250}), base_url=BASE)

        assert request.params == {"limit": 250}

    def test_a_placeholder_is_filled_with_the_typed_value(self) -> None:
        spec = operation(
            query_template={"since": "{since}"},
            inputs=(FieldSpec(name="since", type="datetime"),),
        )

        request = build_request(spec, {"since": "2026-08-14T09:00:00Z"}, base_url=BASE)

        assert request.params["since"].startswith("2026-08-14T09:00:00")

    def test_values_are_not_pre_escaped(self) -> None:
        """
        They are handed to the HTTP library as a mapping, which encodes them. Escaping
        first double-encodes — ``a b`` becomes ``a%2520b`` — and produces a filter that
        matches nothing, which reads as "there were no results".
        """
        spec = operation(
            query_template={"q": "{term}"},
            inputs=(FieldSpec(name="term", type="string"),),
        )

        request = build_request(spec, {"term": "a b&c"}, base_url=BASE)

        assert request.params["q"] == "a b&c"

    def test_a_parameter_with_no_value_is_dropped(self) -> None:
        """
        ``?since=`` and no ``since`` at all mean different things to several APIs in
        scope, and the one we can be sure of is the one we did not send.
        """
        spec = operation(
            query_template={"since": "{since}", "limit": 50},
            inputs=(FieldSpec(name="since", type="datetime"),),
        )

        request = build_request(spec, {}, base_url=BASE)

        assert request.params == {"limit": 50}

    def test_extra_query_is_applied_after_the_template(self) -> None:
        """
        Pagination adds its cursor without the operation knowing it is being paged, and
        it has to win — a stale page parameter in the operation's own query would
        otherwise pin every request to page one.
        """
        spec = operation(query_template={"page": 1})

        request = build_request(spec, base_url=BASE, extra_query={"page": 7})

        assert request.params["page"] == 7


class TestHeaders:
    def test_a_template_header(self) -> None:
        request = build_request(
            operation(header_template={"Accept": "application/json"}), base_url=BASE
        )

        assert request.headers["Accept"] == "application/json"

    @pytest.mark.parametrize("evil", ["a\r\nX-Injected: 1", "a\nX-Injected: 1"])
    def test_a_line_break_in_a_value_is_refused(self, evil: str) -> None:
        spec = operation(
            header_template={"X-Reference": "{reference}"},
            inputs=(FieldSpec(name="reference", type="string"),),
        )

        with pytest.raises(ValueError, match="line break"):
            build_request(spec, {"reference": evil}, base_url=BASE)

    def test_a_line_break_in_an_extra_header_is_refused_too(self) -> None:
        with pytest.raises(ValueError, match="line break"):
            build_request(
                operation(), base_url=BASE, extra_headers={"X-Key": "a\r\nEvil: 1"}
            )

    def test_extra_headers_are_merged(self) -> None:
        request = build_request(
            operation(header_template={"Accept": "application/json"}),
            base_url=BASE,
            extra_headers={"Idempotency-Key": "abc"},
        )

        assert request.headers == {
            "Accept": "application/json",
            "Idempotency-Key": "abc",
        }


class TestBody:
    def _write(self, **overrides) -> OperationSpec:
        defaults = dict(
            operation_id="create_order",
            kind="write",
            method="POST",
            path="/orders",
        )
        defaults.update(overrides)
        return operation(**defaults)

    def test_a_number_stays_a_number(self) -> None:
        """
        The headline. There is no template a user or a model can write that produces
        invalid JSON, because nothing here concatenates JSON.
        """
        spec = self._write(
            body_template={"qty": "{quantity}"},
            inputs=(FieldSpec(name="quantity", type="integer"),),
        )

        request = build_request(spec, {"quantity": "3"}, base_url=BASE)

        assert request.json_body == {"qty": 3}

    def test_a_boolean_stays_a_boolean(self) -> None:
        spec = self._write(
            body_template={"paid": "{paid}"},
            inputs=(FieldSpec(name="paid", type="boolean"),),
        )

        assert build_request(spec, {"paid": "yes"}, base_url=BASE).json_body == {
            "paid": True
        }

    def test_a_quote_in_a_value_cannot_break_the_document(self) -> None:
        spec = self._write(
            body_template={"note": "{note}"},
            inputs=(FieldSpec(name="note", type="string"),),
        )

        request = build_request(
            spec, {"note": '", "admin": true, "x": "'}, base_url=BASE
        )

        assert request.json_body == {"note": '", "admin": true, "x": "'}
        assert json.loads(serialise_body(request.json_body)) == request.json_body

    def test_a_placeholder_among_text_becomes_text(self) -> None:
        """The only reading that makes sense for ``"Order {id} from the shop"``."""
        spec = self._write(
            body_template={"note": "Order {order_id} from the shop"},
            inputs=(FieldSpec(name="order_id", type="integer"),),
        )

        request = build_request(spec, {"order_id": 42}, base_url=BASE)

        assert request.json_body == {"note": "Order 42 from the shop"}

    def test_nesting_and_lists_are_filled(self) -> None:
        spec = self._write(
            body_template={
                "customer": {"email": "{email}"},
                "items": [{"sku": "{sku}", "qty": 1}],
            },
            inputs=(
                FieldSpec(name="email", type="string"),
                FieldSpec(name="sku", type="string"),
            ),
        )

        request = build_request(spec, {"email": "a@b.com", "sku": "X1"}, base_url=BASE)

        assert request.json_body == {
            "customer": {"email": "a@b.com"},
            "items": [{"sku": "X1", "qty": 1}],
        }

    def test_an_unmapped_field_is_omitted_rather_than_sent_as_null(self) -> None:
        """
        Several APIs in scope treat an explicit null as "clear this field", which is a
        destructive reading of a field that was simply not mapped.
        """
        spec = self._write(
            body_template={"email": "{email}", "phone": "{phone}"},
            inputs=(
                FieldSpec(name="email", type="string"),
                FieldSpec(name="phone", type="string"),
            ),
        )

        request = build_request(spec, {"email": "a@b.com"}, base_url=BASE)

        assert request.json_body == {"email": "a@b.com"}

    def test_no_body_template_means_no_body(self) -> None:
        assert build_request(operation(), base_url=BASE).json_body is None
        assert serialise_body(None) is None


class TestArguments:
    def test_a_missing_required_input_is_refused_before_the_call(self) -> None:
        """"400 Bad Request" from a vendor is a worse sentence than the one this can
        compose."""
        spec = operation(
            inputs=(FieldSpec(name="since", label="Changed after", required=True),)
        )

        with pytest.raises(ValueError, match="Changed after"):
            build_request(spec, {}, base_url=BASE)

    def test_an_undeclared_argument_is_dropped(self) -> None:
        """
        Either a mapping mistake or a stale node. Forwarding it would send a field the
        author did not write to a system they do not control.
        """
        spec = operation(query_template={"limit": 50})

        request = build_request(spec, {"secret_flag": "true"}, base_url=BASE)

        assert request.params == {"limit": 50}

    def test_a_value_that_does_not_fit_its_type_names_the_field(self) -> None:
        spec = operation(
            query_template={"limit": "{limit}"},
            inputs=(FieldSpec(name="limit", label="How many", type="integer"),),
        )

        with pytest.raises(ValueError, match="How many"):
            build_request(spec, {"limit": "lots"}, base_url=BASE)


class TestTemplateSafety:
    def test_a_format_style_index_is_not_an_instruction(self) -> None:
        """
        Hand-written rather than ``str.format``. ``{0}``, ``{a.b}`` and ``{a!r}`` are all
        ways to read something the author did not intend from an object we do not
        control, and ``format`` would treat every one of them as an instruction.
        """
        spec = operation(query_template={"q": "{0}"})

        with pytest.raises(ValueError, match="'0'"):
            build_request(spec, {}, base_url=BASE)

    def test_an_unclosed_brace_is_refused(self) -> None:
        with pytest.raises(ValueError, match="never closed"):
            build_request(operation(query_template={"q": "{term"}), base_url=BASE)

    def test_a_placeholder_naming_nothing_is_refused(self) -> None:
        with pytest.raises(ValueError, match="not one of its inputs"):
            build_request(operation(query_template={"q": "{nope}"}), base_url=BASE)

    def test_an_attribute_style_placeholder_is_just_a_missing_name(self) -> None:
        """It is not evaluated — it is a name, and there is no input called that."""
        with pytest.raises(ValueError, match="not one of its inputs"):
            build_request(operation(query_template={"q": "{user.password}"}), base_url=BASE)


class TestSerialiseBody:
    def test_it_is_compact_and_valid(self) -> None:
        encoded = serialise_body({"a": 1, "b": [1, 2]})

        assert encoded == b'{"a":1,"b":[1,2]}'

    def test_a_stray_datetime_does_not_fail_the_send(self) -> None:
        from datetime import datetime

        encoded = serialise_body({"at": datetime(2026, 8, 14)})

        assert b"2026-08-14" in encoded


class TestBodyLiterals:
    """
    A declared body key that is copied across untouched.

    This exists for GraphQL. A query document is nothing but braces, and the substituter
    reads the first ``{…}`` span in any string as an input name — so without an exemption
    the operation is refused before a request is ever built. The first test below is that
    refusal, kept as the reason the rest of the class exists.
    """

    DOCUMENT = (
        "query GetOrders($first: Int!, $after: String) { "
        "orders(first: $first, after: $after) { "
        "edges { node { id name } } pageInfo { hasNextPage endCursor } } }"
    )

    def graphql(self, **overrides) -> OperationSpec:
        settings = {
            "method": "POST",
            "body_template": {
                "query": self.DOCUMENT,
                "variables": {"first": "{page_size}", "after": "{cursor}"},
            },
            "body_literals": ("query",),
            "inputs": (
                FieldSpec(name="page_size", type="integer"),
                FieldSpec(name="cursor", type="string"),
            ),
        }
        settings.update(overrides)
        return operation(**settings)

    def test_without_the_exemption_a_document_is_refused(self) -> None:
        """The bug the feature fixes, asserted so nobody removes the feature."""
        with pytest.raises(ValueError, match="not one of its inputs"):
            build_request(
                self.graphql(body_literals=()), {"page_size": 250}, base_url=BASE
            )

    def test_the_literal_goes_out_byte_for_byte(self) -> None:
        request = build_request(self.graphql(), {"page_size": 250}, base_url=BASE)

        assert request.json_body["query"] == self.DOCUMENT

    def test_a_sibling_key_still_substitutes(self) -> None:
        """
        The exemption is per key, not per operation. If it were per operation the cursor
        and the page size would never be filled in, and every page would be page one.
        """
        request = build_request(
            self.graphql(), {"page_size": 250, "cursor": "abc"}, base_url=BASE
        )

        assert request.json_body["variables"] == {"first": 250, "after": "abc"}

    def test_an_absent_input_is_still_dropped_from_a_sibling(self) -> None:
        """Page one omits ``after`` rather than sending null — legal GraphQL, and the
        difference between an unqualified first request and an invalid one."""
        request = build_request(self.graphql(), {"page_size": 250}, base_url=BASE)

        assert request.json_body["variables"] == {"first": 250}

    def test_the_size_stays_a_number(self) -> None:
        """``first: "250"`` is a type error in GraphQL, not a coercion."""
        request = build_request(self.graphql(), {"page_size": 250}, base_url=BASE)

        assert request.json_body["variables"]["first"] == 250
        assert not isinstance(request.json_body["variables"]["first"], str)

    def test_the_whole_body_serialises(self) -> None:
        request = build_request(self.graphql(), {"page_size": 250}, base_url=BASE)

        decoded = json.loads(serialise_body(request.json_body))

        assert decoded["query"] == self.DOCUMENT
        assert decoded["variables"]["first"] == 250
