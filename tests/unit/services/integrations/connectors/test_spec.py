"""
Tests for ``connectors/spec.py``.

Three properties carry the weight, and each is the foundation of a claim made elsewhere.

**One code path from a row or a declaration.** ``load_operation`` is asserted to produce
an identical spec from an ORM-shaped object and from a dictionary. If the two ever
diverge, the generic-REST path and the vendor path become two request builders, and it
is always the user-facing one that rots.

**The fingerprint is stable for cosmetics and unstable for substance.** Renaming an
operation must not make every replay look like a different run; changing where it posts
must. That is half of the determinism claim on ``integration_run_steps``.

**A hook may not move the target.** ``assert_hook_kept_the_target`` is the fence that
makes "operations are data" true of what actually goes out rather than only of the
declaration. A hook that changed the host would leave the recorded ``operation_hash``
describing a request that never happened — an audit trail quietly claiming the wrong
thing, which is worse than a run that failed.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.services.integrations.connectors.spec import (
    PAGE_CURSOR,
    PAGE_INPUT_CURSOR,
    PAGE_LINK_HEADER,
    PAGE_NEXT_URL,
    PAGE_NONE,
    PAGE_NUMBER,
    PAGE_OFFSET,
    AuthSpec,
    ConnectorSpec,
    FieldSpec,
    HookViolation,
    OperationSpec,
    PageRule,
    PreparedRequest,
    RateLimitSpec,
    assert_hook_kept_the_target,
    describe_operation,
    load_fields,
    load_operation,
    load_page_rule,
)


class Row:
    """An ORM row's duck type — attributes, not keys. See ``spec._as_mapping``."""

    def __init__(self, **columns: object) -> None:
        for name, value in columns.items():
            setattr(self, name, value)


OPERATION_COLUMNS = {
    "operation_id": "list_orders",
    "label": "List orders",
    "description": "Every order, newest first",
    "kind": "read",
    "method": "GET",
    "path": "/orders",
    "query_template": {"status": "any"},
    "header_template": {"Accept": "application/json"},
    "body_template": None,
    "inputs": [{"name": "since", "type": "datetime", "required": False}],
    "outputs": [{"name": "email", "type": "string", "path": "customer.email"}],
    "records_path": "orders",
    "page_rule": {"kind": "cursor", "param": "page_info", "cursor_path": "meta.next"},
    "idempotent": False,
    "idempotency_header": "",
    "ordered": False,
    "timeout_seconds": None,
}


def an_operation(**overrides: object) -> OperationSpec:
    return load_operation({**OPERATION_COLUMNS, **overrides})


# ---------------------------------------------------------------------------
# One code path
# ---------------------------------------------------------------------------


class TestLoadOperation:
    def test_a_row_and_a_dictionary_produce_the_same_spec(self) -> None:
        """
        The whole reason the function exists. ``connector_api`` calls it once and never
        learns whether this operation came from a vendor module or from a form somebody
        filled in — a branch there would be the seam through which the two drift.
        """
        from_dict = load_operation(OPERATION_COLUMNS)
        from_row = load_operation(Row(**OPERATION_COLUMNS))

        assert from_dict == from_row

    def test_a_spec_passes_through_unchanged(self) -> None:
        original = an_operation()

        assert load_operation(original) is original

    def test_it_validates_rather_than_trusting(self) -> None:
        """
        A row is user input — it was typed into a form — and a spec that was never
        checked is a request nobody predicted.
        """
        with pytest.raises(ValueError):
            load_operation({**OPERATION_COLUMNS, "method": "TRACE"})

    def test_the_fields_become_specs(self) -> None:
        operation = an_operation()

        assert operation.inputs[0].name == "since"
        assert operation.inputs[0].type == "datetime"
        assert operation.outputs[0].path == "customer.email"

    def test_the_page_rule_becomes_a_rule(self) -> None:
        assert an_operation().page_rule.kind == PAGE_CURSOR

    def test_an_absent_page_rule_means_one_page(self) -> None:
        assert an_operation(page_rule=None).page_rule.kind == PAGE_NONE

    def test_missing_columns_do_not_crash(self) -> None:
        """A row from an older revision has whatever columns it has."""
        operation = load_operation({"operation_id": "ping", "path": "/ping"})

        assert operation.method == "GET"
        assert operation.kind == "read"


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


class TestOperationValidation:
    def test_an_operation_needs_an_id(self) -> None:
        with pytest.raises(ValueError, match="needs an id"):
            an_operation(operation_id="")

    def test_a_path_must_start_with_a_slash(self) -> None:
        """
        It is joined onto the connection's base URL. A relative path would either be
        appended to whatever came before it or be treated as absolute — and which of
        those happens is a property of the URL library, not of anything the author
        wrote.
        """
        with pytest.raises(ValueError, match="must start with '/'"):
            an_operation(path="orders")

    def test_an_unknown_method_names_the_alternatives(self) -> None:
        with pytest.raises(ValueError, match="TRACE"):
            an_operation(method="TRACE")

    def test_a_write_that_uses_get_is_refused(self) -> None:
        """
        The rule keeps three other rules honest. A write declared as a GET would slip
        past the dry-run suppression, the retry restriction and the node-type check —
        while still not writing anything.
        """
        with pytest.raises(ValueError, match="does not change anything"):
            an_operation(kind="write", method="GET")

    def test_a_write_cannot_be_paged(self) -> None:
        with pytest.raises(ValueError, match="no pages to read"):
            an_operation(kind="write", method="POST", path="/orders")

    def test_a_field_with_an_unknown_type(self) -> None:
        with pytest.raises(ValueError, match="currency"):
            an_operation(inputs=[{"name": "total", "type": "currency"}])

    def test_a_field_needs_a_name(self) -> None:
        with pytest.raises(ValueError, match="needs a name"):
            load_fields([{"type": "string"}])


class TestPageRuleValidation:
    @pytest.mark.parametrize("kind", [PAGE_NUMBER, PAGE_OFFSET, PAGE_CURSOR])
    def test_a_parameterised_kind_needs_its_parameter(self, kind: str) -> None:
        with pytest.raises(ValueError, match="request parameter"):
            PageRule(kind=kind, cursor_path="meta.next").validated()

    @pytest.mark.parametrize("kind", [PAGE_CURSOR, PAGE_NEXT_URL])
    def test_a_cursor_kind_needs_to_know_where_to_look(self, kind: str) -> None:
        with pytest.raises(ValueError, match="where in the response"):
            PageRule(kind=kind, param="cursor").validated()

    def test_an_unknown_kind_lists_the_six(self) -> None:
        with pytest.raises(ValueError) as caught:
            PageRule(kind="scroll").validated()

        for kind in (PAGE_LINK_HEADER, PAGE_NEXT_URL, PAGE_CURSOR):
            assert kind in str(caught.value)

    def test_a_link_header_rule_needs_nothing_else(self) -> None:
        """Shopify's ``Link: <…>; rel="next"`` is self-describing — the URL is complete
        and is used verbatim, so there is nothing for the author to configure."""
        PageRule(kind=PAGE_LINK_HEADER).validated()

    @pytest.mark.parametrize("field", ["max_pages", "max_records"])
    def test_a_limit_of_zero_would_read_nothing(self, field: str) -> None:
        with pytest.raises(ValueError, match="read nothing"):
            replace(PageRule(), **{field: 0}).validated()

    def test_the_default_limits_are_real_ceilings(self) -> None:
        """
        Not decoration. A malformed vendor response that always returns the same page
        would otherwise loop forever, unattended, at whatever the rate limit allows.
        """
        rule = PageRule()

        assert rule.max_pages > 0
        assert rule.max_records > 0

    def test_no_paging_is_not_paged(self) -> None:
        assert PageRule().is_paged is False
        assert PageRule(kind=PAGE_LINK_HEADER).is_paged is True


class TestAuthValidation:
    def test_an_unknown_kind(self) -> None:
        with pytest.raises(ValueError, match="magic_word"):
            AuthSpec(kind="magic_word").validated()

    def test_an_unknown_placement(self) -> None:
        with pytest.raises(ValueError, match="body"):
            AuthSpec(placement="body").validated()

    def test_oauth_without_a_token_url(self) -> None:
        with pytest.raises(ValueError, match="token URL"):
            AuthSpec(kind="oauth2").validated()

    def test_the_rotating_refresh_flag_is_off_by_default(self) -> None:
        """
        The safe default. A connector that rotates and does not say so writes the old
        token back and locks itself out permanently; one that says so needlessly just
        writes a token that was already correct.
        """
        assert AuthSpec().rotates_refresh_token is False


class TestRateLimitValidation:
    def test_zero_requests_per_second_sends_nothing(self) -> None:
        with pytest.raises(ValueError, match="sends nothing"):
            RateLimitSpec(requests_per_second=0).validated()

    def test_a_daily_limit_of_zero_sends_nothing(self) -> None:
        with pytest.raises(ValueError, match="sends nothing"):
            RateLimitSpec(daily_limit=0).validated()

    def test_the_soft_limit_stops_short_of_the_cap(self) -> None:
        """
        Under rather than at, so a concurrent worker's in-flight requests cannot carry
        it over. A marketplace application that blows its daily cap gets suspended.
        """
        assert RateLimitSpec().daily_soft_limit < 1.0


class TestPerOperationRateLimits:
    """
    An operation may declare its own allowance when the connector's single figure would
    be wrong for it. Brevo is why: 10/second for contacts, 5 for order writes, 2 for
    product writes and 100 **per hour** for the rest, all on one connection.
    """

    def test_an_operation_has_no_allowance_by_default(self) -> None:
        """The overwhelmingly common case, and the one every connector written before
        this field relies on."""
        operation = OperationSpec(operation_id="read", path="/x")

        assert operation.rate_limits is None
        assert operation.rate_limit_group == ""

    def test_a_declared_allowance_is_validated_like_any_other(self) -> None:
        with pytest.raises(ValueError, match="sends nothing"):
            OperationSpec(
                operation_id="read",
                path="/x",
                rate_limits=RateLimitSpec(requests_per_second=0),
            ).validated()

    def test_a_group_naming_no_allowance_is_refused(self) -> None:
        """
        The failure worth catching. It reads exactly like an operation with its own
        budget and silently has not got one — the call falls back to the connector's
        figure, which is the number the field exists because it was wrong.
        """
        with pytest.raises(ValueError, match="declares no limits of its own"):
            OperationSpec(
                operation_id="read", path="/x", rate_limit_group="orders"
            ).validated()

    def test_an_allowance_without_a_group_is_fine(self) -> None:
        """The group is an optional narrowing — ``sender._bucket_key`` falls back to the
        operation id, which is the right bucket for a quota granted per endpoint."""
        operation = OperationSpec(
            operation_id="read", path="/x", rate_limits=RateLimitSpec()
        ).validated()

        assert operation.rate_limit_group == ""

    def test_it_stays_out_of_the_fingerprint(self) -> None:
        """
        Same rule as ``has_more_path``: it decides *when* a request leaves, never what it
        says. Retuning a limit after a vendor publishes new figures would otherwise move
        every fingerprint at once and make every prior run look like it ran something
        else.
        """
        plain = OperationSpec(operation_id="read", path="/x")
        metered = OperationSpec(
            operation_id="read",
            path="/x",
            rate_limits=RateLimitSpec(requests_per_second=1.0),
            rate_limit_group="slow",
        )

        assert plain.fingerprint() == metered.fingerprint()

    def test_a_database_row_cannot_grant_itself_an_allowance(self) -> None:
        """
        Vendor-declared only, and deliberately so rather than unfinished. A row is a form
        somebody filled in; an operation that could set its own send rate would let that
        form decide how fast we hammer a third party from our egress address.
        """
        operation = an_operation(
            rate_limits={"requests_per_second": 1000.0}, rate_limit_group="fast"
        )

        assert operation.rate_limits is None
        assert operation.rate_limit_group == ""


# ---------------------------------------------------------------------------
# The fingerprint
# ---------------------------------------------------------------------------


class TestFingerprint:
    def test_the_same_operation_fingerprints_the_same(self) -> None:
        """Two specs built independently from the same columns, so this is determinism
        rather than an identity comparison."""
        first = an_operation()
        second = an_operation()

        assert first is not second
        assert first.fingerprint() == second.fingerprint()

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("label", "Orders, listed"),
            ("description", "A better sentence"),
        ],
    )
    def test_cosmetics_do_not_move_it(self, field: str, value: str) -> None:
        """
        Renaming an operation in the UI does not change what it sends. A hash that moved
        for a typo fix would make every replay look like a different run, and the
        determinism claim would mean nothing.
        """
        assert an_operation(**{field: value}).fingerprint() == an_operation().fingerprint()

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("path", "/orders/v2"),
            ("method", "POST"),
            ("query_template", {"status": "open"}),
            ("records_path", "data.orders"),
            ("idempotent", True),
        ],
    )
    def test_anything_that_reaches_the_wire_does(self, field: str, value: object) -> None:
        overrides = {field: value}
        if field == "method":
            # A POST is a write, and a write cannot be paged — so this case has to give
            # up the cursor rule as well. Both refusals are the operation's own and are
            # asserted above; here they are just the cost of changing the method.
            overrides.update(kind="write", page_rule=None)

        assert an_operation(**overrides).fingerprint() != an_operation().fingerprint()

    def test_it_is_hex_sha256(self) -> None:
        digest = an_operation().fingerprint()

        assert len(digest) == 64
        assert set(digest) <= set("0123456789abcdef")


# ---------------------------------------------------------------------------
# The hook fence
# ---------------------------------------------------------------------------


class TestAssertHookKeptTheTarget:
    request = PreparedRequest(
        method="POST",
        url="https://api.example.com/orders",
        headers={"Accept": "application/json"},
        host="api.example.com",
        path="/orders",
        json_body={"email": "a@b.com"},
    )

    def test_adding_a_header_is_allowed(self) -> None:
        """The whole legitimate purpose: SAP's CSRF token, Shopify's cost header."""
        after = self.request.with_headers({"X-CSRF-Token": "abc"})

        assert_hook_kept_the_target(self.request, after, connector_id="sap_odata")

    def test_replacing_a_header_is_allowed(self) -> None:
        after = self.request.with_headers({"Accept": "application/xml"})

        assert_hook_kept_the_target(self.request, after, connector_id="sap_odata")

    def test_changing_the_host_is_refused(self) -> None:
        """
        The one that matters. The host was approved by the egress guard *before* the
        hook ran, so a hook that changes it sends to somewhere nothing checked.
        """
        after = replace(self.request, host="evil.example.com")

        with pytest.raises(HookViolation, match="changed where the request goes"):
            assert_hook_kept_the_target(self.request, after, connector_id="shopify")

    def test_changing_the_path_is_refused(self) -> None:
        after = replace(self.request, path="/customers")

        with pytest.raises(HookViolation):
            assert_hook_kept_the_target(self.request, after, connector_id="shopify")

    def test_changing_the_method_is_refused(self) -> None:
        after = replace(self.request, method="DELETE")

        with pytest.raises(HookViolation):
            assert_hook_kept_the_target(self.request, after, connector_id="shopify")

    def test_changing_the_body_is_refused(self) -> None:
        """
        The body comes from the operation and its mapped values. A hook that rewrites it
        makes the recorded operation a description of something else.
        """
        after = replace(self.request, json_body={"email": "someone-else@b.com"})

        with pytest.raises(HookViolation, match="changed the request body"):
            assert_hook_kept_the_target(self.request, after, connector_id="shopify")

    def test_the_message_names_the_connector(self) -> None:
        """A programming error in one connector, so whoever is editing it needs to know
        which one."""
        after = replace(self.request, host="elsewhere.example.com")

        with pytest.raises(HookViolation, match="gohighlevel"):
            assert_hook_kept_the_target(self.request, after, connector_id="gohighlevel")

    def test_the_host_comparison_is_case_insensitive(self) -> None:
        after = replace(self.request, host="API.Example.com")

        assert_hook_kept_the_target(self.request, after, connector_id="shopify")


class TestPreparedRequestHoldsNoCredential:
    def test_the_dataclass_has_no_field_for_one(self) -> None:
        """
        The credential is applied at the socket, from the ``AuthSpec``. Nothing that
        gets logged, previewed or hashed has ever held it — which is a property of the
        type rather than of whoever remembers to strip it.
        """
        fields = set(PreparedRequest.__dataclass_fields__)

        assert not (fields & {"api_key", "token", "credential", "auth", "secret"})


# ---------------------------------------------------------------------------
# The connector as a whole
# ---------------------------------------------------------------------------


class TestConnectorSpec:
    def test_a_connector_with_nowhere_to_send(self) -> None:
        with pytest.raises(ValueError, match="nowhere to send"):
            ConnectorSpec(connector_id="x", label="X").validated()

    def test_two_operations_with_one_id(self) -> None:
        with pytest.raises(ValueError, match="two operations"):
            ConnectorSpec(
                connector_id="x",
                label="X",
                base_url_template="https://x.example.com",
                operations=(an_operation(), an_operation()),
            ).validated()

    def test_operations_are_split_by_kind(self) -> None:
        """
        A ``connector_read`` node may only pick a read and a ``connector_write`` only a
        write. That is what stops a workflow drawn as a read from posting to somebody's
        store.
        """
        spec = ConnectorSpec(
            connector_id="x",
            label="X",
            base_url_template="https://x.example.com",
            operations=(
                an_operation(),
                an_operation(
                    operation_id="create_order", kind="write", method="POST",
                    page_rule=None,
                ),
            ),
        ).validated()

        assert [op.operation_id for op in spec.readable_operations()] == ["list_orders"]
        assert [op.operation_id for op in spec.writable_operations()] == ["create_order"]

    def test_an_unknown_operation_is_none_rather_than_an_error(self) -> None:
        """The registry composes the sentence, because it knows the label and the list."""
        spec = ConnectorSpec(
            connector_id="x", label="X", base_url_template="https://x.example.com"
        ).validated()

        assert spec.operation("nope") is None

    def test_private_hosts_are_off_by_default(self) -> None:
        assert ConnectorSpec(connector_id="x", label="X").allows_private_hosts is False


class TestDescribeOperation:
    def test_it_does_not_leak_the_request_shape(self) -> None:
        """
        This payload reaches a browser. A field picker needs to know what a destination
        accepts; it does not need the URL, and putting one in is how an internal
        endpoint ends up in somebody's devtools.
        """
        described = describe_operation(an_operation())

        assert not ({"path", "method", "query_template", "header_template"} & set(described))

    def test_it_carries_what_a_field_picker_needs(self) -> None:
        described = describe_operation(an_operation())

        assert described["inputs"][0]["name"] == "since"
        assert described["outputs"][0]["path"] == "customer.email"
        assert described["paged"] is True

    def test_a_field_falls_back_to_its_name_for_a_label(self) -> None:
        assert FieldSpec(name="since").display_label() == "since"
        assert FieldSpec(name="since", label="Changed after").display_label() == "Changed after"

    def test_it_is_json_serialisable(self) -> None:
        import json

        json.dumps(describe_operation(an_operation()))


class TestRequiredInputs:
    def test_it_lists_only_the_required_ones(self) -> None:
        operation = an_operation(
            inputs=[
                {"name": "email", "type": "string", "required": True},
                {"name": "name", "type": "string", "required": False},
            ]
        )

        assert operation.required_inputs == ("email",)

    def test_it_is_what_publish_checks_against(self) -> None:
        """
        ``flow_service`` stamps this onto the write node so ``validate_for_publish`` can
        refuse an unmapped required field without needing a database.
        """
        operation = an_operation(
            inputs=[{"name": "email", "type": "string", "required": True}]
        )

        assert list(operation.required_inputs) == ["email"]
        assert operation.input_named("email").required is True
        assert operation.input_named("missing") is None


class TestBodyLiteralValidation:
    def test_a_literal_naming_no_key_is_refused(self) -> None:
        """
        A name matching nothing is not harmless. The key it was meant to protect goes
        through substitution as usual and raises somewhere far away — or, if its value
        happens to parse as a placeholder, quietly sends something else.
        """
        with pytest.raises(ValueError, match="no such key"):
            an_operation(
                body_template={"query": "{ x }"}, body_literals=("document",)
            )

    def test_the_error_lists_the_keys_that_do_exist(self) -> None:
        with pytest.raises(ValueError, match="query, variables"):
            an_operation(
                body_template={"query": "{ x }", "variables": {}},
                body_literals=("nope",),
            )

    def test_a_declared_literal_is_accepted(self) -> None:
        operation = an_operation(
            body_template={"query": "{ x }"}, body_literals=("query",)
        )

        assert operation.body_literals == ("query",)

    def test_the_literal_is_inside_the_fingerprint(self) -> None:
        """
        The document *is* the operation. A hash that did not cover it would let a replay
        run different GraphQL against the same recorded step and still claim to match.
        """
        one = an_operation(
            body_template={"query": "{ orders { id } }"}, body_literals=("query",)
        )
        two = an_operation(
            body_template={"query": "{ products { id } }"}, body_literals=("query",)
        )

        assert one.fingerprint() != two.fingerprint()

    def test_declaring_a_key_literal_changes_the_fingerprint(self) -> None:
        """Same body, different meaning: one substitutes and one does not."""
        substituted = an_operation(body_template={"query": "static"})
        literal = an_operation(
            body_template={"query": "static"}, body_literals=("query",)
        )

        assert substituted.fingerprint() != literal.fingerprint()


class TestInputCursorValidation:
    """
    ``param`` and ``size_param`` name declared inputs for an input-shaped page kind.

    Without the check a typo is not an error: the cursor is passed as an argument the
    operation never declared, ``build_request`` drops undeclared arguments silently, and
    every page is a fresh request for page one — read as new records each time, until the
    repeat guard trips thousands of duplicated records later.
    """

    RULE = dict(
        kind=PAGE_INPUT_CURSOR,
        size_param="page_size",
        size=250,
        cursor_path="data.orders.pageInfo.endCursor",
    )

    # `an_operation` goes through `load_operation`, so inputs are column data — a list of
    # dictionaries, as they arrive from JSONB — not `FieldSpec` objects.
    CURSOR_INPUT = {"name": "cursor", "type": "string"}
    SIZE_INPUT = {"name": "page_size", "type": "integer"}

    def test_a_cursor_naming_no_input_is_refused(self) -> None:
        with pytest.raises(ValueError, match="not one of its inputs"):
            an_operation(
                inputs=[self.SIZE_INPUT],
                page_rule=PageRule(param="afterr", **self.RULE),
            )

    def test_a_size_naming_no_input_is_refused(self) -> None:
        with pytest.raises(ValueError, match="not one of its inputs"):
            an_operation(
                inputs=[self.CURSOR_INPUT],
                page_rule=PageRule(param="cursor", **self.RULE),
            )

    def test_declared_names_are_accepted(self) -> None:
        operation = an_operation(
            inputs=[self.CURSOR_INPUT, self.SIZE_INPUT],
            page_rule=PageRule(param="cursor", **self.RULE),
        )

        assert operation.page_rule.kind == PAGE_INPUT_CURSOR

    def test_a_query_string_cursor_is_not_checked_against_inputs(self) -> None:
        """``cursor`` names a request parameter, which an operation never declares."""
        operation = an_operation(
            page_rule=PageRule(
                kind=PAGE_CURSOR, param="after", cursor_path="meta.next"
            )
        )

        assert operation.page_rule.param == "after"


class TestAccountId:
    """
    The account id is user-supplied text that becomes the **host of a request carrying a
    credential**. Everything in this class is about that one sentence.
    """

    SPEC = dict(
        connector_id="vendor",
        label="Vendor",
        base_url_template="https://{account}/admin",
        account_id_pattern=r"[a-z0-9-]+\.vendor\.com",
        account_id_label="Shop domain",
        account_id_help="your-store.vendor.com",
        account_id_required=True,
    )

    @staticmethod
    def connection(account: str):
        return type(
            "Connection",
            (),
            {"external_account_id": account, "base_url": None, "label": "Store"},
        )()

    def test_a_template_without_a_required_account_is_refused(self) -> None:
        """An empty account renders ``https:///admin`` — a request nobody chose."""
        spec = ConnectorSpec(**{**self.SPEC, "account_id_required": False})

        with pytest.raises(ValueError, match="has to be required"):
            spec.validated()

    def test_a_template_without_a_pattern_is_refused(self) -> None:
        spec = ConnectorSpec(**{**self.SPEC, "account_id_pattern": ""})

        with pytest.raises(ValueError, match="needs a pattern"):
            spec.validated()

    def test_an_uncompilable_pattern_is_refused_at_import(self) -> None:
        spec = ConnectorSpec(**{**self.SPEC, "account_id_pattern": "[unclosed"})

        with pytest.raises(ValueError, match="not a valid expression"):
            spec.validated()

    def test_a_good_account_renders_the_address(self) -> None:
        spec = ConnectorSpec(**self.SPEC).validated()

        assert spec.render_base_url(self.connection("shop.vendor.com")) == (
            "https://shop.vendor.com/admin"
        )

    @pytest.mark.parametrize(
        "account",
        [
            "evil.com",
            "shop.vendor.com.evil.com",
            "shop.vendor.com/../x",
            "shop.vendor.com:8080",
            "SHOP.vendor.com",
            "shop.vendor.com evil.com",
            "",
        ],
    )
    def test_a_bad_account_never_becomes_a_host(self, account: str) -> None:
        """
        ``fullmatch``, so a pattern with no anchors still cannot be prefixed or suffixed
        past. ``shop.vendor.com.evil.com`` is the one that a "contains" reading lets
        through, and it is the one that matters.
        """
        spec = ConnectorSpec(**self.SPEC).validated()

        with pytest.raises(ValueError):
            spec.render_base_url(self.connection(account))

    def test_a_stored_url_cannot_override_a_computed_one(self) -> None:
        """
        The ordering that stops a typed address winning. If it could, the whole reason a
        vendor connector refuses a user-supplied base URL would be undone by writing one
        into the column some other way.
        """
        spec = ConnectorSpec(**self.SPEC).validated()
        connection = self.connection("shop.vendor.com")
        connection.base_url = "https://attacker.example.com"

        assert spec.render_base_url(connection) == "https://shop.vendor.com/admin"

    def test_a_hook_wins_over_the_template(self) -> None:
        class Hooks:
            def resolve_base_url(self, connection) -> str:  # noqa: ANN001
                return "https://computed.example.com/v1/"

        spec = ConnectorSpec(**{**self.SPEC, "hooks": Hooks()}).validated()

        assert spec.render_base_url(self.connection("shop.vendor.com")) == (
            "https://computed.example.com/v1"
        )

    def test_a_connector_with_nothing_to_go_on_says_so(self) -> None:
        """A readable sentence, not an ``AttributeError`` and not an empty string that
        the egress guard later rejects for the wrong reason."""
        spec = ConnectorSpec(
            connector_id="generic", label="REST", base_url_is_user_supplied=True
        ).validated()

        with pytest.raises(ValueError, match="no address to send requests to"):
            spec.render_base_url(self.connection(""))

    def test_a_user_supplied_url_is_used_when_there_is_no_template(self) -> None:
        spec = ConnectorSpec(
            connector_id="generic", label="REST", base_url_is_user_supplied=True
        ).validated()
        connection = self.connection("")
        connection.base_url = "https://api.example.com/v2/"

        assert spec.render_base_url(connection) == "https://api.example.com/v2"
