"""
Tests for ``app.utils.outbound_http`` — the one definition of what this
application may connect to.

DNS is stubbed on the running loop rather than resolved for real. The conftest's
network guard blocks outbound connections, and a test that depended on live DNS
would be flaky and would be asserting somebody else's zone file rather than this
code.

The assertions worth naming:

* **169.254.169.254 is refused even when explicitly allow-listed.** It is the
  cloud instance-metadata endpoint, and reaching it usually means handing over the
  host's credentials. A configuration mistake must not be able to open it.
* **The allow-list needs both halves.** A hostname alone falls to a DNS answer the
  operator does not control; a CIDR alone permits any hostname that happens to
  resolve into the range.
* **Every resolved address is checked**, not just the first.
"""

from __future__ import annotations

import socket

import pytest

from app.utils.outbound_http import (
    DEFAULT_POLICY,
    EgressError,
    EgressPolicy,
    assert_public_host,
    resolve_and_check,
    same_origin,
    validate_outbound_url_shape,
)


def fake_addrinfo(*addresses: str):  # noqa: ANN201
    async def _getaddrinfo(host, port, **kwargs):  # noqa: ANN001, ANN003
        return [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, port))
            for address in addresses
        ]

    return _getaddrinfo


def _stub_dns(monkeypatch: pytest.MonkeyPatch, *addresses: str) -> None:
    import asyncio

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "getaddrinfo", fake_addrinfo(*addresses))


# --------------------------------------------------------------------------
# Shape
# --------------------------------------------------------------------------

class TestUrlShape:
    def test_a_plain_https_url_parses(self) -> None:
        assert validate_outbound_url_shape("https://api.example.com/v1/orders") == (
            "https",
            "api.example.com",
            443,
        )

    def test_an_explicit_port_is_kept(self) -> None:
        _, _, port = validate_outbound_url_shape("https://api.example.com:8443/x")
        assert port == 8443

    def test_http_is_refused_by_default(self) -> None:
        with pytest.raises(EgressError, match="must start with https://"):
            validate_outbound_url_shape("http://api.example.com")

    def test_a_missing_hostname_is_refused(self) -> None:
        with pytest.raises(EgressError, match="must include a hostname"):
            validate_outbound_url_shape("https:///orders")

    def test_userinfo_is_refused(self) -> None:
        """
        ``https://user:pass@evil.example.com`` is both a credential in a URL and a
        classic way to make a URL look like it points somewhere it does not.
        """
        with pytest.raises(EgressError, match="username or password"):
            validate_outbound_url_shape("https://user:pass@api.example.com")

    def test_the_label_names_the_field(self) -> None:
        with pytest.raises(EgressError, match="Action URL must start with"):
            validate_outbound_url_shape("ftp://x.example.com", label="Action URL")

    def test_http_is_allowed_only_under_a_private_policy(self) -> None:
        policy = EgressPolicy(
            require_https=False,
            allow_private=True,
            allowed_hosts=frozenset({"sap.internal:8000"}),
            allowed_cidrs=("10.0.0.0/8",),
        ).validated()
        scheme, host, port = validate_outbound_url_shape(
            "http://sap.internal:8000/odata", policy=policy
        )
        assert (scheme, host, port) == ("http", "sap.internal", 8000)


class TestPolicyCoherence:
    """A policy that cannot mean anything sensible is refused where it is built."""

    def test_allowing_private_without_both_lists_is_refused(self) -> None:
        with pytest.raises(EgressError, match="both the exact host:port"):
            EgressPolicy(allow_private=True, allowed_cidrs=("10.0.0.0/8",)).validated()

        with pytest.raises(EgressError, match="both the exact host:port"):
            EgressPolicy(
                allow_private=True, allowed_hosts=frozenset({"sap.internal:443"})
            ).validated()

    def test_plain_http_to_a_public_host_is_refused(self) -> None:
        with pytest.raises(EgressError, match="only permitted for an explicitly allow-listed"):
            EgressPolicy(require_https=False).validated()

    def test_the_default_policy_is_coherent(self) -> None:
        assert DEFAULT_POLICY.validated() is DEFAULT_POLICY


# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------

class TestAssertPublicHost:
    async def test_a_public_address_is_allowed_and_reported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_dns(monkeypatch, "93.184.216.34")
        assert await assert_public_host("api.example.com", 443) == ["93.184.216.34"]

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
    async def test_every_non_public_range_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, address: str, label: str
    ) -> None:
        _stub_dns(monkeypatch, address)
        with pytest.raises(EgressError) as caught:
            await assert_public_host("evil.example.com", 443)
        assert "private or internal address" in str(caught.value)
        assert address in str(caught.value)

    async def test_an_ipv4_mapped_loopback_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        ``::ffff:127.0.0.1`` is loopback wearing IPv6 notation, and
        ``ip_address().is_loopback`` reports False for it. Unwrapping the mapped
        form is what stops it being the one way through.
        """
        _stub_dns(monkeypatch, "::ffff:127.0.0.1")
        with pytest.raises(EgressError, match="private or internal address"):
            await assert_public_host("sneaky.example.com", 443)

    async def test_any_private_answer_refuses_the_whole_host(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_dns(monkeypatch, "93.184.216.34", "10.0.0.1")
        with pytest.raises(EgressError, match="private or internal address"):
            await assert_public_host("mixed.example.com", 443)

    async def test_a_name_that_does_not_resolve_says_so(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import asyncio

        async def _boom(host, port, **kwargs):  # noqa: ANN001, ANN003
            raise socket.gaierror("no such host")

        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", _boom)

        with pytest.raises(EgressError, match="could not be resolved"):
            await assert_public_host("nope.invalid", 443)

    async def test_an_unparseable_address_is_skipped_not_fatal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        There is no address to judge and equally none to connect to, so the
        request fails at the socket with the truthful reason rather than being
        reported as an egress refusal.
        """
        _stub_dns(monkeypatch, "not-an-ip")
        assert await assert_public_host("odd.example.com", 443) == []


class TestPrivateAllowList:
    """The SAP on-premise door, and the things it must not open."""

    policy = EgressPolicy(
        allow_private=True,
        allowed_hosts=frozenset({"sap.internal:443"}),
        allowed_cidrs=("10.42.0.0/16",),
    )

    async def test_an_allow_listed_host_in_an_allow_listed_range_passes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_dns(monkeypatch, "10.42.7.9")
        assert await assert_public_host("sap.internal", 443, policy=self.policy) == ["10.42.7.9"]

    async def test_the_host_must_be_listed_too(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """In-range, but nobody wrote this hostname down."""
        _stub_dns(monkeypatch, "10.42.7.9")
        with pytest.raises(EgressError, match="private or internal address"):
            await assert_public_host("other.internal", 443, policy=self.policy)

    async def test_the_address_must_be_in_range_too(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The right hostname, but DNS answered with something outside the range."""
        _stub_dns(monkeypatch, "192.168.0.5")
        with pytest.raises(EgressError, match="private or internal address"):
            await assert_public_host("sap.internal", 443, policy=self.policy)

    async def test_the_port_is_part_of_the_entry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_dns(monkeypatch, "10.42.7.9")
        with pytest.raises(EgressError, match="private or internal address"):
            await assert_public_host("sap.internal", 8080, policy=self.policy)

    @pytest.mark.parametrize(
        "address", ["169.254.169.254", "127.0.0.1", "0.0.0.0", "224.0.0.1"]
    )
    async def test_the_never_allowed_set_survives_an_explicit_allow_list(
        self, monkeypatch: pytest.MonkeyPatch, address: str
    ) -> None:
        """
        The important one. An operator who allow-lists ``0.0.0.0/0`` — or who is
        tricked into allow-listing the metadata range — must still not be able to
        reach the instance-metadata endpoint. The never-allowed check runs *after*
        the allow-list precisely so ordering cannot be used to slip past it.
        """
        wide_open = EgressPolicy(
            allow_private=True,
            allowed_hosts=frozenset({"metadata.internal:443"}),
            allowed_cidrs=("0.0.0.0/0", "::/0"),
        )
        _stub_dns(monkeypatch, address)
        with pytest.raises(EgressError, match="No allow-list permits it"):
            await assert_public_host("metadata.internal", 443, policy=wide_open)


class TestResolveAndCheck:
    async def test_it_does_both_halves(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_dns(monkeypatch, "93.184.216.34")
        target = await resolve_and_check("https://api.example.com/v1")
        assert (target.scheme, target.host, target.port) == ("https", "api.example.com", 443)
        assert target.addresses == ["93.184.216.34"]

    async def test_a_bad_shape_fails_before_dns(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import asyncio

        async def _never(*args, **kwargs):  # noqa: ANN002, ANN003
            raise AssertionError("DNS must not be consulted for a malformed URL")

        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", _never)

        with pytest.raises(EgressError, match="must start with https://"):
            await resolve_and_check("http://api.example.com")


class TestSameOrigin:
    """
    Guards a paginated read that follows a URL out of a response body. That URL is
    chosen by the server being read, so following it unchecked hands the choice of
    destination to whoever controls the response.
    """

    @pytest.mark.parametrize(
        ("first", "second"),
        [
            ("https://api.example.com/a", "https://api.example.com/b?page=2"),
            ("https://api.example.com:443/a", "https://api.example.com/b"),
            ("https://API.example.com/a", "https://api.example.com/b"),
        ],
    )
    def test_same(self, first: str, second: str) -> None:
        assert same_origin(first, second)

    @pytest.mark.parametrize(
        ("first", "second"),
        [
            ("https://api.example.com/a", "https://evil.example.com/b"),
            ("https://api.example.com/a", "http://api.example.com/b"),
            ("https://api.example.com/a", "https://api.example.com:8443/b"),
            ("https://api.example.com/a", "https://api.example.com.evil.net/b"),
        ],
    )
    def test_different(self, first: str, second: str) -> None:
        assert not same_origin(first, second)
