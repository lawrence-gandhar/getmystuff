"""
The one definition of "may this application connect to that?".

User-authored outbound HTTP is textbook SSRF surface: an operator types a URL, the
server fetches it, and without a check that URL can be ``http://169.254.169.254/``
(cloud instance metadata), ``http://127.0.0.1:8003/`` (this application), or the
address of the Postgres this application runs on.

These checks began life as two private functions inside
``app.services.chatbot.chatbot_action_service``, where they guarded webhook actions
and nothing else. They live here now for the reason SERVICE_PATTERNS.md gives for
``utils/query_joins.py``: a rule two features need belongs in ``utils/``, never
inside one of them. The chatbot imports them from here and keeps its own wording.

**Two things this deliberately does not do.**

It does not close DNS rebinding. This is check-then-connect — a hostile DNS server
can answer differently for the actual connection — and closing it means pinning the
resolved IP at the transport layer, which ``httpx`` does not expose directly. It
narrows the window; it does not remove it. That matters more for an allow-listed
private target than a public one, because internal reachability has already been
conceded there.

It does not decide *what* the caller may reach — that is the policy's job, and a
policy is something a caller passes in rather than something this module infers.
:data:`DEFAULT_POLICY` refuses every private address, which is the correct answer
for every caller except an explicitly configured on-premise integration.
"""

import asyncio
import ipaddress
import socket
from dataclasses import dataclass, field
from typing import FrozenSet, List, Tuple
from urllib.parse import urlparse


class EgressError(Exception):
    """
    A destination this application will not connect to.

    Deliberately not an ``HTTPException``. The two callers want different status
    codes and different wording for the same refusal — a chatbot action save is a
    400 on a form, an integration run step is a failed node — so the rule states
    the reason and the caller chooses how to raise it. That is
    ``utils/datasource_status.py``'s doctrine: the message lives next to the rule,
    the exception type belongs to whoever is asking.
    """


@dataclass(frozen=True)
class EgressPolicy:
    """
    What one caller is permitted to reach.

    ``allowed_hosts`` entries are exact ``"host:port"`` strings and
    ``allowed_cidrs`` are networks. A private address is permitted only when it
    satisfies **both** — the hostname is one somebody wrote down, *and* the address
    it resolved to is inside a range somebody wrote down. Either alone is too
    loose: a hostname check alone falls to a DNS answer the operator does not
    control, and a CIDR check alone permits any hostname anywhere that happens to
    resolve into the range.

    ``require_https=False`` is permitted only together with ``allow_private``,
    enforced in :meth:`validated`. Cleartext credentials to a public host is not a
    trade-off anyone should be able to configure by accident; inside a customer's
    own network it is their decision to make explicitly.
    """

    require_https: bool = True
    allow_private: bool = False
    allowed_hosts: FrozenSet[str] = frozenset()
    allowed_cidrs: Tuple[str, ...] = ()

    def validated(self) -> "EgressPolicy":
        """Refuse a policy that is internally incoherent, at the point it is built."""
        if self.allow_private and not (self.allowed_hosts and self.allowed_cidrs):
            raise EgressError(
                "A policy that allows private hosts must list both the exact "
                "host:port entries and the network ranges they may resolve into."
            )
        if not self.require_https and not self.allow_private:
            raise EgressError(
                "Plain HTTP is only permitted for an explicitly allow-listed private "
                "host. A public endpoint must use https://."
            )
        return self

    def permits_private(self, host: str, port: int, ip: ipaddress._BaseAddress) -> bool:
        if not self.allow_private:
            return False
        if f"{host}:{port}".lower() not in {entry.lower() for entry in self.allowed_hosts}:
            return False
        return any(ip in ipaddress.ip_network(cidr, strict=False) for cidr in self.allowed_cidrs)


DEFAULT_POLICY = EgressPolicy()


# Addresses no policy may ever permit, checked *after* the allow-list so that
# ordering cannot be used to slip past them.
#
#   loopback      this application's own process, and anything else on the host
#   link-local    cloud instance metadata. 169.254.169.254 hands out IAM
#                 credentials to anyone who asks, which is the single most
#                 valuable thing an SSRF can reach on a cloud host
#   unspecified   0.0.0.0/8, which several stacks route to localhost
#   multicast     not a thing an integration talks to, and a way to reach many
#                 hosts with one request
_NEVER_ALLOWED = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("ff00::/8"),
)


@dataclass(frozen=True)
class ResolvedTarget:
    """A destination that passed every check, plus what it resolved to."""

    host: str
    port: int
    scheme: str
    addresses: List[str] = field(default_factory=list)


def validate_outbound_url_shape(
    url: str,
    *,
    policy: EgressPolicy = DEFAULT_POLICY,
    label: str = "URL",
) -> Tuple[str, str, int]:
    """
    Scheme and host checks that need no DNS. Returns ``(scheme, host, port)``.

    ``label`` names the thing in the message — "Action URL", "Connection base URL"
    — so one function can produce wording that fits whichever form is open.
    """
    parsed = urlparse(url or "")

    allowed_schemes = ("https",) if policy.require_https else ("https", "http")
    if parsed.scheme not in allowed_schemes:
        expected = "https://" if policy.require_https else "https:// or http://"
        raise EgressError(f"{label} must start with {expected}")
    if not parsed.hostname:
        raise EgressError(f"{label} must include a hostname")
    if parsed.username or parsed.password:
        raise EgressError(
            f"{label} must not contain a username or password — use a header instead"
        )

    default_port = 443 if parsed.scheme == "https" else 80
    return parsed.scheme, parsed.hostname, parsed.port or default_port


async def assert_public_host(
    host: str,
    port: int,
    *,
    policy: EgressPolicy = DEFAULT_POLICY,
) -> List[str]:
    """
    Resolve ``host`` and refuse if any answer is an address the policy does not
    permit. Returns every address it resolved to, so a caller can record what it
    actually checked.

    **Every** answer is checked, not just the first. A hostname with an A record
    for a public address and a second for ``127.0.0.1`` would otherwise pass on
    whichever the resolver happened to return first.
    """
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise EgressError(f"The host {host} could not be resolved.")

    addresses: List[str] = []

    for info in infos:
        address = info[4][0]
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue

        addresses.append(address)

        # An IPv4-mapped IPv6 address (::ffff:127.0.0.1) is the same destination
        # wearing a different notation, and ip_address() reports is_loopback False
        # for it. Unwrap before deciding anything.
        checked = ip.ipv4_mapped if getattr(ip, "ipv4_mapped", None) else ip

        if any(checked in network for network in _NEVER_ALLOWED if checked.version == network.version):
            raise EgressError(
                f"The host {host} resolves to a private or internal address ({address}) "
                "that is never reachable from this application — it is loopback, cloud "
                "metadata or multicast space. No allow-list permits it."
            )

        if not _is_non_public(checked):
            continue

        if policy.permits_private(host, port, checked):
            continue

        raise EgressError(
            f"The host {host} resolves to a private or internal address ({address}). "
            "This connection may only reach public endpoints."
        )

    # An empty list here means getaddrinfo answered with something no IP parser
    # recognised. Deliberately allowed through rather than refused: there is no
    # address to judge, and there is equally no address to connect to, so the
    # request fails at the socket a moment later with the truthful reason. Turning
    # it into an egress refusal would report a resolver quirk as an attack.
    return addresses


def _is_non_public(ip: ipaddress._BaseAddress) -> bool:
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


async def resolve_and_check(
    url: str,
    *,
    policy: EgressPolicy = DEFAULT_POLICY,
    label: str = "URL",
) -> ResolvedTarget:
    """
    Both halves in one call — the shape, then the addresses.

    This is what a request path should use. Checking the shape at save time and
    the addresses at request time is the correct split for a *stored* template
    (``chatbot_action_service`` does exactly that), but anything sending a request
    right now wants both, against the URL it is actually about to fetch.
    """
    scheme, host, port = validate_outbound_url_shape(url, policy=policy, label=label)
    addresses = await assert_public_host(host, port, policy=policy)
    return ResolvedTarget(host=host, port=port, scheme=scheme, addresses=addresses)


def same_origin(first: str, second: str) -> bool:
    """
    Whether two URLs share scheme, host and port.

    For paginated reads that follow a URL out of a response body — SAP OData's
    ``@odata.nextLink``, an RFC 5988 ``Link`` header. That URL is chosen by the
    server being read, so following it unchecked hands the choice of destination to
    whoever controls that response. Asserting it matches page 1's origin keeps
    pagination pagination.
    """
    left, right = urlparse(first), urlparse(second)
    left_port = left.port or (443 if left.scheme == "https" else 80)
    right_port = right.port or (443 if right.scheme == "https" else 80)
    return (
        left.scheme == right.scheme
        and (left.hostname or "").lower() == (right.hostname or "").lower()
        and left_port == right_port
    )
