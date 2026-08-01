"""
Tests for the validation, rendering and egress-safety layer of
app/services/chatbot/chatbot_action_service.py.

Chatbot actions let an operator point a public chatbot at an arbitrary outbound
HTTPS endpoint, with parts of the URL and body filled in by the language model at
conversation time. That makes this module the sharpest security boundary in the
codebase, and the tests are weighted accordingly:

* **SSRF** — ``_validate_outbound_url_shape`` and ``_assert_public_host`` are the
  only things stopping an action from being pointed at ``169.254.169.254`` or a
  private-range host. Both are tested against the full set of non-public ranges.
* **Header injection** — AI-supplied values must never reach a header, and no
  rendered header may contain a line break, or a value could split the request.
* **Template integrity** — a placeholder that names an undeclared or optional
  parameter is rejected at save time, because at render time it would produce a
  half-built URL or an invalid JSON body.

Everything here is pure, so it runs without a database or a network. DNS is
faked where ``_assert_public_host`` needs it — the conftest network guard would
otherwise block the lookup, and a test that depended on real DNS would be flaky.
"""

from __future__ import annotations

import json
import socket

import pytest
from litestar.exceptions import HTTPException

from app.models.chatbot import ACTION_HTTP_METHODS, ACTION_PARAMETER_TYPES
from app.services.chatbot import chatbot_action_service as svc
from app.services.chatbot.chatbot_action_service import (
    _coerce_param,
    _parse_json_list,
    _render,
    _validate_body_template,
    _validate_description,
    _validate_headers,
    _validate_method,
    _validate_name,
    _validate_outbound_url_shape,
    _validate_parameters,
    _validate_placeholders,
    _validate_timeout,
    _validate_url_template,
)


def param(name: str, *, type: str = "string", required: bool = True) -> dict:  # noqa: A002
    return {"name": name, "type": type, "description": f"the {name}", "required": required}


# ---------------------------------------------------------------------------
# Scalar field validation
# ---------------------------------------------------------------------------
class TestValidateName:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("lookup_order", "lookup_order"),
            ("  Lookup_Order  ", "lookup_order"),
            ("LOOKUP_ORDER_2", "lookup_order_2"),
            ("abc", "abc"),
            ("a" + "b" * 63, "a" + "b" * 63),
        ],
    )
    def test_accepts_and_normalises(self, raw: str, expected: str) -> None:
        assert _validate_name(raw) == expected

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "   ",
            "ab",                 # too short
            "a" + "b" * 64,       # too long
            "1lookup",            # must start with a letter
            "_lookup",
            "lookup-order",       # hyphen
            "lookup order",       # space
            "lookup.order",
            "lookup!",
            "looküp",
        ],
    )
    def test_rejects(self, bad: str) -> None:
        with pytest.raises(HTTPException) as excinfo:
            _validate_name(bad)

        assert excinfo.value.status_code == 400
        assert "Action name must be" in excinfo.value.detail

    def test_none_is_rejected_rather_than_crashing(self) -> None:
        with pytest.raises(HTTPException):
            _validate_name(None)


class TestValidateDescription:
    def test_strips_and_returns(self) -> None:
        assert _validate_description("  Look up an order  ") == "Look up an order"

    @pytest.mark.parametrize("blank", ["", "   ", None])
    def test_rejects_blank(self, blank) -> None:  # noqa: ANN001
        with pytest.raises(HTTPException, match="Describe when this action"):
            _validate_description(blank)

    def test_accepts_exactly_the_limit(self) -> None:
        assert len(_validate_description("x" * 500)) == 500

    def test_rejects_one_over_the_limit(self) -> None:
        with pytest.raises(HTTPException, match="must not exceed 500 characters"):
            _validate_description("x" * 501)


class TestValidateMethod:
    @pytest.mark.parametrize("method", ACTION_HTTP_METHODS)
    def test_accepts_every_supported_method(self, method: str) -> None:
        assert _validate_method(method) == method

    @pytest.mark.parametrize("raw", ["get", "  post  ", "PaTcH"])
    def test_normalises_case_and_whitespace(self, raw: str) -> None:
        assert _validate_method(raw) == raw.strip().upper()

    @pytest.mark.parametrize("bad", ["", "TRACE", "CONNECT", "OPTIONS", "HEAD", "FETCH"])
    def test_rejects_unsupported_methods(self, bad: str) -> None:
        with pytest.raises(HTTPException, match="HTTP method must be one of"):
            _validate_method(bad)


class TestValidateTimeout:
    @pytest.mark.parametrize(("raw", "expected"), [("1", 1), ("30", 30), ("  15 ", 15)])
    def test_accepts_the_range(self, raw: str, expected: int) -> None:
        assert _validate_timeout(raw) == expected

    @pytest.mark.parametrize("bad", ["0", "31", "-5", "1000"])
    def test_rejects_out_of_range(self, bad: str) -> None:
        with pytest.raises(HTTPException, match="between 1 and 30 seconds"):
            _validate_timeout(bad)

    @pytest.mark.parametrize("bad", ["", "abc", "1.5", None, "1e3"])
    def test_rejects_non_integers(self, bad) -> None:  # noqa: ANN001
        with pytest.raises(HTTPException, match="whole number of seconds"):
            _validate_timeout(bad)


# ---------------------------------------------------------------------------
# JSON list parsing
# ---------------------------------------------------------------------------
class TestParseJsonList:
    @pytest.mark.parametrize("empty", ["", "   ", None])
    def test_blank_is_an_empty_list(self, empty) -> None:  # noqa: ANN001
        assert _parse_json_list(empty, "Things") == []

    def test_parses_a_list(self) -> None:
        assert _parse_json_list('[{"a": 1}]', "Things") == [{"a": 1}]

    def test_rejects_malformed_json_with_a_readable_message(self) -> None:
        with pytest.raises(HTTPException) as excinfo:
            _parse_json_list("{not json", "Things")

        assert excinfo.value.detail == "Things could not be read. Please re-enter them and save again."

    @pytest.mark.parametrize("not_a_list", ['{"a": 1}', '"text"', "42", "null"])
    def test_rejects_json_that_is_not_a_list(self, not_a_list: str) -> None:
        with pytest.raises(HTTPException, match="must be a list"):
            _parse_json_list(not_a_list, "Things")


# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------
class TestValidateParameters:
    def test_normalises_and_defaults(self) -> None:
        result = _validate_parameters(
            json.dumps([{"name": "  Order_ID ", "description": "the id"}])
        )

        assert result == [
            {"name": "order_id", "type": "string", "description": "the id", "required": False}
        ]

    @pytest.mark.parametrize("param_type", ACTION_PARAMETER_TYPES)
    def test_accepts_every_supported_type(self, param_type: str) -> None:
        result = _validate_parameters(
            json.dumps([{"name": "x", "type": param_type, "description": "d"}])
        )
        assert result[0]["type"] == param_type

    def test_required_is_coerced_to_bool(self) -> None:
        result = _validate_parameters(
            json.dumps([{"name": "x", "description": "d", "required": "yes"}])
        )
        assert result[0]["required"] is True

    def test_accepts_the_maximum_count(self) -> None:
        items = [{"name": f"p{i}", "description": "d"} for i in range(10)]
        assert len(_validate_parameters(json.dumps(items))) == 10

    def test_rejects_one_over_the_maximum(self) -> None:
        items = [{"name": f"p{i}", "description": "d"} for i in range(11)]
        with pytest.raises(HTTPException, match="at most 10 parameters"):
            _validate_parameters(json.dumps(items))

    def test_rejects_a_non_object_entry(self) -> None:
        with pytest.raises(HTTPException, match="must have a name and a type"):
            _validate_parameters(json.dumps(["just a string"]))

    @pytest.mark.parametrize("bad", ["", "1abc", "_abc", "a-b", "a b", "A" * 51])
    def test_rejects_an_invalid_name(self, bad: str) -> None:
        with pytest.raises(HTTPException, match="is invalid"):
            _validate_parameters(json.dumps([{"name": bad, "description": "d"}]))

    def test_rejects_an_unsupported_type(self) -> None:
        with pytest.raises(HTTPException, match="type must be one of"):
            _validate_parameters(
                json.dumps([{"name": "x", "type": "object", "description": "d"}])
            )

    def test_rejects_a_missing_description(self) -> None:
        """The description is what the model reads to decide what to put in the
        parameter, so an empty one makes the action unusable rather than merely
        undocumented."""
        with pytest.raises(HTTPException, match="needs a description"):
            _validate_parameters(json.dumps([{"name": "x"}]))

    def test_rejects_a_duplicate_name(self) -> None:
        items = [{"name": "x", "description": "d"}, {"name": "X", "description": "d"}]
        with pytest.raises(HTTPException, match="defined more than once"):
            _validate_parameters(json.dumps(items))


# ---------------------------------------------------------------------------
# Headers
# ---------------------------------------------------------------------------
class TestValidateHeaders:
    def test_strips_and_returns(self) -> None:
        result = _validate_headers(json.dumps([{"key": " X-Api-Key ", "value": " abc "}]))
        assert result == [{"key": "X-Api-Key", "value": "abc"}]

    def test_accepts_the_maximum_count(self) -> None:
        items = [{"key": f"X-H{i}", "value": "v"} for i in range(10)]
        assert len(_validate_headers(json.dumps(items))) == 10

    def test_rejects_one_over_the_maximum(self) -> None:
        items = [{"key": f"X-H{i}", "value": "v"} for i in range(11)]
        with pytest.raises(HTTPException, match="at most 10 headers"):
            _validate_headers(json.dumps(items))

    @pytest.mark.parametrize("bad", ["", "X Api Key", "X:Api", "X_Api", "héader", "a" * 101])
    def test_rejects_an_invalid_header_name(self, bad: str) -> None:
        with pytest.raises(HTTPException, match="is invalid"):
            _validate_headers(json.dumps([{"key": bad, "value": "v"}]))

    def test_rejects_a_blank_value(self) -> None:
        with pytest.raises(HTTPException, match="needs a value"):
            _validate_headers(json.dumps([{"key": "X-Api-Key", "value": "  "}]))

    def test_rejects_a_duplicate_name_case_insensitively(self) -> None:
        items = [{"key": "X-Api-Key", "value": "a"}, {"key": "x-api-key", "value": "b"}]
        with pytest.raises(HTTPException, match="defined more than once"):
            _validate_headers(json.dumps(items))

    def test_a_prompt_variable_placeholder_is_allowed(self) -> None:
        result = _validate_headers(
            json.dumps([{"key": "Authorization", "value": "Bearer {{API_TOKEN}}"}])
        )
        assert result[0]["value"] == "Bearer {{API_TOKEN}}"

    @pytest.mark.parametrize(
        "value",
        ["{{param.token}}", "Bearer {{param.token}}", "a {{ param.token }} b"],
    )
    def test_rejects_an_ai_supplied_parameter_in_a_header(self, value: str) -> None:
        """
        The core rule of this module: a value the language model produced must
        never reach a header. A model persuaded by a hostile visitor could
        otherwise rewrite an Authorization header.
        """
        with pytest.raises(HTTPException) as excinfo:
            _validate_headers(json.dumps([{"key": "Authorization", "value": value}]))

        assert "cannot use {{param.*}} placeholders" in excinfo.value.detail


# ---------------------------------------------------------------------------
# Placeholders
# ---------------------------------------------------------------------------
class TestValidatePlaceholders:
    def test_accepts_a_declared_required_parameter(self) -> None:
        _validate_placeholders("/orders/{{param.order_id}}", "The URL", [param("order_id")])

    def test_accepts_text_with_no_placeholders(self) -> None:
        _validate_placeholders("/orders", "The URL", [])

    def test_prompt_variables_are_not_checked_against_parameters(self) -> None:
        """Only ``{{param.*}}`` is validated here; a bare ``{{VAR}}`` is a prompt
        variable and is resolved at render time."""
        _validate_placeholders("/x/{{TENANT}}", "The URL", [])

    def test_rejects_an_undeclared_parameter(self) -> None:
        with pytest.raises(HTTPException) as excinfo:
            _validate_placeholders("/orders/{{param.missing}}", "The URL", [param("order_id")])

        assert "has no parameter named missing" in excinfo.value.detail

    def test_rejects_an_optional_parameter(self) -> None:
        """An optional parameter the model omits would render into a broken URL,
        so the mismatch is caught at save time instead of mid-conversation."""
        with pytest.raises(HTTPException) as excinfo:
            _validate_placeholders(
                "/orders/{{param.order_id}}", "The URL", [param("order_id", required=False)]
            )

        assert "must be marked Required" in excinfo.value.detail

    def test_matching_is_case_insensitive(self) -> None:
        _validate_placeholders("/x/{{param.ORDER_ID}}", "The URL", [param("order_id")])

    def test_tolerates_whitespace_inside_the_braces(self) -> None:
        _validate_placeholders("/x/{{  param.order_id  }}", "The URL", [param("order_id")])


# ---------------------------------------------------------------------------
# URL template
# ---------------------------------------------------------------------------
class TestValidateUrlTemplate:
    def test_accepts_a_plain_https_url(self) -> None:
        assert _validate_url_template("https://api.example.com/orders", []) == (
            "https://api.example.com/orders"
        )

    def test_strips_surrounding_whitespace(self) -> None:
        assert _validate_url_template("  https://api.example.com  ", []) == (
            "https://api.example.com"
        )

    def test_accepts_a_url_with_placeholders(self) -> None:
        url = "https://api.example.com/orders/{{param.order_id}}?t={{TENANT}}"
        assert _validate_url_template(url, [param("order_id")]) == url

    @pytest.mark.parametrize("blank", ["", "   ", None])
    def test_rejects_a_blank_url(self, blank) -> None:  # noqa: ANN001
        with pytest.raises(HTTPException, match="Action URL is required"):
            _validate_url_template(blank, [])

    def test_rejects_an_over_long_url(self) -> None:
        url = "https://api.example.com/" + "x" * 1000
        with pytest.raises(HTTPException, match="must not exceed 1000 characters"):
            _validate_url_template(url, [])

    def test_placeholders_are_neutralised_before_the_shape_check(self) -> None:
        """An unsubstituted ``{{...}}`` is not a legal URL character sequence, so
        the shape check runs against a placeholder-free copy — otherwise every
        templated URL would be rejected as malformed."""
        url = "https://{{TENANT}}.example.com/orders/{{param.order_id}}"
        assert _validate_url_template(url, [param("order_id")]) == url

    def test_rejects_an_undeclared_placeholder(self) -> None:
        with pytest.raises(HTTPException, match="has no parameter named"):
            _validate_url_template("https://x.example.com/{{param.nope}}", [])


# ---------------------------------------------------------------------------
# Body template
# ---------------------------------------------------------------------------
class TestValidateBodyTemplate:
    @pytest.mark.parametrize("blank", ["", "   ", None])
    def test_a_blank_body_becomes_none(self, blank) -> None:  # noqa: ANN001
        assert _validate_body_template(blank, []) is None

    def test_accepts_valid_json(self) -> None:
        body = '{"status": "open"}'
        assert _validate_body_template(body, []) == body

    def test_accepts_a_template_that_is_valid_json_once_filled(self) -> None:
        body = '{"order_id": "{{param.order_id}}"}'
        assert _validate_body_template(body, [param("order_id")]) == body

    def test_rejects_a_template_that_is_invalid_json_once_filled(self) -> None:
        """Catching a broken template at save time beats failing mid-conversation
        with a request the endpoint rejects."""
        with pytest.raises(HTTPException, match="must be valid JSON"):
            _validate_body_template('{"order_id": {{param.order_id}},}', [param("order_id")])

    def test_rejects_plain_text(self) -> None:
        with pytest.raises(HTTPException, match="must be valid JSON"):
            _validate_body_template("just some text", [])

    def test_rejects_an_over_long_body(self) -> None:
        with pytest.raises(HTTPException, match="must not exceed 4000 characters"):
            _validate_body_template("x" * 4001, [])

    def test_rejects_an_undeclared_placeholder(self) -> None:
        with pytest.raises(HTTPException, match="has no parameter named"):
            _validate_body_template('{"a": "{{param.nope}}"}', [])


# ---------------------------------------------------------------------------
# Egress: URL shape
# ---------------------------------------------------------------------------
class TestValidateOutboundUrlShape:
    def test_returns_host_and_default_port(self) -> None:
        assert _validate_outbound_url_shape("https://api.example.com/x") == (
            "api.example.com",
            443,
        )

    def test_returns_an_explicit_port(self) -> None:
        assert _validate_outbound_url_shape("https://api.example.com:8443/x") == (
            "api.example.com",
            8443,
        )

    @pytest.mark.parametrize(
        "url",
        [
            "http://api.example.com",
            "ftp://api.example.com",
            "file:///etc/passwd",
            "gopher://api.example.com",
            "//api.example.com",
            "api.example.com",
        ],
    )
    def test_only_https_is_allowed(self, url: str) -> None:
        """Plain HTTP would send the action's headers — which carry API keys — in
        cleartext, and the other schemes are classic SSRF pivots."""
        with pytest.raises(HTTPException, match="must start with https://"):
            _validate_outbound_url_shape(url)

    def test_rejects_a_missing_hostname(self) -> None:
        with pytest.raises(HTTPException, match="must include a hostname"):
            _validate_outbound_url_shape("https:///path")

    @pytest.mark.parametrize(
        "url",
        [
            "https://user@api.example.com",
            "https://user:pw@api.example.com",
            "https://:pw@api.example.com",
        ],
    )
    def test_rejects_credentials_in_the_url(self, url: str) -> None:
        """Userinfo in a URL is both a credential-leak risk in logs and a common
        way to disguise the real host from a human reviewer."""
        with pytest.raises(HTTPException, match="must not contain a username or password"):
            _validate_outbound_url_shape(url)


# ---------------------------------------------------------------------------
# Egress: DNS resolution guard
# ---------------------------------------------------------------------------
def fake_addrinfo(*addresses: str):  # noqa: ANN201
    """Build a getaddrinfo stub returning the given addresses."""

    async def _getaddrinfo(host, port, **kwargs):  # noqa: ANN001, ANN003
        return [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, port))
            for address in addresses
        ]

    return _getaddrinfo


class TestAssertPublicHost:
    """
    DNS is stubbed on the running loop rather than hit for real: the conftest
    network guard blocks outbound connections, and a test depending on live DNS
    would be flaky and would assert someone else's zone file rather than this
    code.
    """

    async def test_allows_a_public_address(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import asyncio

        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "getaddrinfo", fake_addrinfo("93.184.216.34"))

        await svc._assert_public_host("api.example.com", 443)

    @pytest.mark.parametrize(
        ("address", "label"),
        [
            ("127.0.0.1", "loopback"),
            ("127.5.5.5", "loopback range"),
            ("10.0.0.5", "private class A"),
            ("172.16.4.4", "private class B"),
            ("192.168.1.10", "private class C"),
            ("169.254.169.254", "cloud instance metadata"),
            ("169.254.1.1", "link-local"),
            ("224.0.0.1", "multicast"),
            ("0.0.0.0", "unspecified"),
            ("240.0.0.1", "reserved"),
            ("::1", "IPv6 loopback"),
            ("fd00::1", "IPv6 unique-local"),
            ("fe80::1", "IPv6 link-local"),
        ],
    )
    async def test_rejects_every_non_public_range(
        self, monkeypatch: pytest.MonkeyPatch, address: str, label: str
    ) -> None:
        """169.254.169.254 is the one that matters most — it is the cloud
        instance-metadata endpoint, and reaching it usually means handing over
        the instance's credentials."""
        import asyncio

        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "getaddrinfo", fake_addrinfo(address))

        with pytest.raises(HTTPException) as excinfo:
            await svc._assert_public_host("evil.example.com", 443)

        assert excinfo.value.status_code == 400
        assert "private or internal address" in excinfo.value.detail
        assert address in excinfo.value.detail

    async def test_rejects_when_any_resolved_address_is_private(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A host answering with both a public and a private address must be
        refused — otherwise the connection could pick the private one."""
        import asyncio

        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "getaddrinfo", fake_addrinfo("93.184.216.34", "10.0.0.1"))

        with pytest.raises(HTTPException, match="private or internal address"):
            await svc._assert_public_host("mixed.example.com", 443)

    async def test_a_name_that_does_not_resolve_is_a_readable_400(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import asyncio

        async def _boom(host, port, **kwargs):  # noqa: ANN001, ANN003
            raise socket.gaierror("no such host")

        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "getaddrinfo", _boom)

        with pytest.raises(HTTPException) as excinfo:
            await svc._assert_public_host("nope.invalid", 443)

        assert excinfo.value.status_code == 400
        assert "could not be resolved" in excinfo.value.detail

    async def test_an_unparseable_address_is_skipped_not_fatal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """getaddrinfo can return a non-IP form for some families; the loop
        `continue`s past it rather than crashing the save."""
        import asyncio

        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "getaddrinfo", fake_addrinfo("not-an-ip"))

        await svc._assert_public_host("odd.example.com", 443)


# ---------------------------------------------------------------------------
# Parameter coercion
# ---------------------------------------------------------------------------
class TestCoerceParam:
    @pytest.mark.parametrize("value", ["42", "-1", "3.14", "0", "  7  ", "1e3"])
    def test_a_valid_number_passes_through_unquoted(self, value: str) -> None:
        url_text, body_literal = _coerce_param(value, "number")
        assert url_text == value.strip()
        assert body_literal == value.strip()

    @pytest.mark.parametrize("value", ["abc", "", "12abc", "one"])
    def test_an_invalid_number_is_rejected(self, value: str) -> None:
        with pytest.raises(ValueError, match="expected a number"):
            _coerce_param(value, "number")

    @pytest.mark.parametrize(("value", "expected"), [("true", "true"), ("TRUE", "true"), ("False", "false")])
    def test_a_boolean_is_lowercased(self, value: str, expected: str) -> None:
        assert _coerce_param(value, "boolean") == (expected, expected)

    @pytest.mark.parametrize("value", ["yes", "1", "", "maybe"])
    def test_an_invalid_boolean_is_rejected(self, value: str) -> None:
        with pytest.raises(ValueError, match="expected true or false"):
            _coerce_param(value, "boolean")

    def test_a_string_is_json_escaped_without_surrounding_quotes(self) -> None:
        """The template supplies the quotes, so the fragment must not — but the
        escaping still has to happen, or a quote in the value would break the
        JSON body."""
        url_text, body_literal = _coerce_param('say "hi"', "string")

        assert url_text == 'say "hi"'
        assert body_literal == 'say \\"hi\\"'

    def test_a_string_containing_a_newline_is_escaped(self) -> None:
        _, body_literal = _coerce_param("line1\nline2", "string")
        assert body_literal == "line1\\nline2"

    def test_a_string_containing_a_backslash_is_escaped(self) -> None:
        _, body_literal = _coerce_param("a\\b", "string")
        assert json.loads(f'"{body_literal}"') == "a\\b"

    def test_none_becomes_an_empty_string(self) -> None:
        assert _coerce_param(None, "string") == ("", "")


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
class TestRenderUrlMode:
    def test_substitutes_a_parameter_percent_encoded(self) -> None:
        rendered = _render(
            "https://api.example.com/orders/{{param.id}}",
            {},
            {"id": ("a b/c", "a b/c")},
            "url",
        )
        assert rendered == "https://api.example.com/orders/a%20b%2Fc"

    def test_substitutes_a_prompt_variable_percent_encoded(self) -> None:
        rendered = _render("https://x/{{TENANT}}", {"TENANT": "acme corp"}, {}, "url")
        assert rendered == "https://x/acme%20corp"

    def test_variable_names_resolve_case_insensitively(self) -> None:
        assert _render("{{tenant}}", {"TENANT": "acme"}, {}, "url") == "acme"

    def test_parameter_names_resolve_case_insensitively(self) -> None:
        assert _render("{{param.ID}}", {}, {"id": ("7", "7")}, "url") == "7"

    def test_encoding_prevents_path_traversal_via_a_parameter(self) -> None:
        """quote(safe="") escapes the slashes, so a model-supplied value cannot
        redirect the request to a different path on the host."""
        rendered = _render("https://x/orders/{{param.id}}", {}, {"id": ("../../admin", "")}, "url")
        assert "/admin" not in rendered
        assert rendered == "https://x/orders/..%2F..%2Fadmin"

    def test_an_unsupplied_parameter_raises(self) -> None:
        """Rendering must fail rather than leave the placeholder in place — a
        half-built URL must never be sent."""
        with pytest.raises(ValueError, match="no value was supplied for parameter id"):
            _render("https://x/{{param.id}}", {}, {}, "url")

    def test_an_undefined_variable_raises(self) -> None:
        with pytest.raises(ValueError, match="is not a defined prompt variable"):
            _render("https://x/{{TENANT}}", {}, {}, "url")

    def test_text_without_placeholders_is_unchanged(self) -> None:
        assert _render("https://x/orders", {}, {}, "url") == "https://x/orders"

    def test_none_renders_as_empty(self) -> None:
        assert _render(None, {}, {}, "url") == ""


class TestRenderBodyMode:
    def test_inserts_the_json_fragment(self) -> None:
        rendered = _render(
            '{"id": "{{param.id}}"}', {}, {"id": ("a", 'say \\"hi\\"')}, "body"
        )
        assert rendered == '{"id": "say \\"hi\\""}'
        assert json.loads(rendered) == {"id": 'say "hi"'}

    def test_a_variable_is_json_escaped(self) -> None:
        rendered = _render('{"t": "{{TENANT}}"}', {"TENANT": 'a"b'}, {}, "body")
        assert json.loads(rendered) == {"t": 'a"b'}

    def test_the_result_is_parseable_json(self) -> None:
        rendered = _render(
            '{"id": "{{param.id}}", "t": "{{TENANT}}"}',
            {"TENANT": "acme"},
            {"id": ("7", "7")},
            "body",
        )
        assert json.loads(rendered) == {"id": "7", "t": "acme"}


class TestRenderHeaderMode:
    def test_substitutes_a_variable_verbatim(self) -> None:
        rendered = _render("Bearer {{TOKEN}}", {"TOKEN": "abc123"}, {}, "header")
        assert rendered == "Bearer abc123"

    def test_a_parameter_is_refused_at_render_time_too(self) -> None:
        """Belt and braces: save-time validation already blocks this, so reaching
        here means the row was edited outside the app."""
        with pytest.raises(ValueError, match="cannot reference AI-supplied parameters"):
            _render("{{param.token}}", {}, {"token": ("x", "x")}, "header")

    @pytest.mark.parametrize("injected", ["a\r\nX-Evil: 1", "a\nX-Evil: 1", "a\rb"])
    def test_a_line_break_in_a_variable_value_is_refused(self, injected: str) -> None:
        """Header injection: a value carrying CR or LF would split the request
        and let an attacker append headers of their own."""
        with pytest.raises(ValueError, match="cannot contain line breaks"):
            _render("X-Trace: {{TRACE}}", {"TRACE": injected}, {}, "header")

    def test_a_clean_variable_value_passes(self) -> None:
        assert _render("X-Trace: {{TRACE}}", {"TRACE": "abc"}, {}, "header") == "X-Trace: abc"
