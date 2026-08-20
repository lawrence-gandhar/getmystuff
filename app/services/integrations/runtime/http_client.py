"""
The outbound HTTP clients, pooled per origin.

``chatbot_action_service`` builds a client per call, which is right for one action
firing once in a conversation. A forty-page sync must not pay forty TLS handshakes, so
these are kept and reused — the same shape ``ollama_client._get_client()`` uses, keyed
rather than singular because there is more than one destination.

Three settings are load-bearing rather than tuning:

``follow_redirects=False``
    The classic way past an SSRF check: the URL that was validated returns a 302 to one
    that was not. ``execute_action`` already sets this, and the reasoning is the same
    here — a redirect is answered by re-validating and re-issuing, never by the client
    quietly going somewhere else.

``trust_env=False``
    Ignores ``HTTP_PROXY``, ``HTTPS_PROXY``, ``NO_PROXY`` and ``SSL_CERT_FILE`` from the
    environment. A proxy variable set for some unrelated reason would route a merchant's
    OAuth token through it, and nothing about the run would say so.

``verify=True``
    Certificate verification, never configurable. An "allow self-signed" switch is the
    one setting that turns every other guard in this module into decoration, and the
    on-premise case that would ask for it is answered by trusting a CA rather than by
    trusting nothing.

The pool is closed in ``on_shutdown``. Not doing so leaks sockets across
``uvicorn --reload`` cycles until the file-descriptor limit ends the process, which
presents as an unrelated failure hours later.
"""

import logging
from typing import Dict, Optional

import httpx

logger = logging.getLogger(__name__)


#: Per origin. Modest, because a worker holds several of these at once and the
#: bottleneck is the destination's rate limit rather than our socket count.
MAX_CONNECTIONS = 10
MAX_KEEPALIVE = 5

#: The default per-request ceiling. An operation may lower it; ``engine/node_runners``
#: has a separate, longer ceiling for the node as a whole, and the two are different
#: questions — one call hanging is not the same as a node taking a while.
DEFAULT_TIMEOUT_SECONDS = 30.0

#: Long enough for a slow gateway to finish a large page and short enough that a hung
#: connection does not hold a worker for the length of a lease.
MAX_TIMEOUT_SECONDS = 300.0


_clients: Dict[str, httpx.AsyncClient] = {}


def get_client(origin: str, *, timeout: Optional[float] = None) -> httpx.AsyncClient:
    """
    The pooled client for one origin, created on first use.

    Keyed by origin rather than by connection: two connections to the same host share a
    connection pool, which is what a merchant with three Shopify stores on one domain
    actually wants. The credential is applied per request, never on the client, so
    sharing one cannot leak one connection's token onto another's call.

    ``timeout`` is a default for the client; a per-request timeout still overrides it,
    which is how an operation's own ``timeout_seconds`` reaches the wire.
    """
    key = str(origin or "").rstrip("/")

    client = _clients.get(key)
    if client is not None and not client.is_closed:
        return client

    client = httpx.AsyncClient(
        timeout=httpx.Timeout(timeout or DEFAULT_TIMEOUT_SECONDS),
        limits=httpx.Limits(
            max_connections=MAX_CONNECTIONS, max_keepalive_connections=MAX_KEEPALIVE
        ),
        follow_redirects=False,
        trust_env=False,
        verify=True,
    )
    _clients[key] = client
    return client


def clamp_timeout(seconds: Optional[float]) -> float:
    """
    An operation's declared timeout, bounded.

    A ceiling rather than a suggestion: an operation declaring 3600 would hold a worker
    for an hour on one call, and a run that never finishes is indistinguishable from one
    that hung.
    """
    if not seconds or seconds <= 0:
        return DEFAULT_TIMEOUT_SECONDS
    return min(float(seconds), MAX_TIMEOUT_SECONDS)


async def close_all_clients() -> None:
    """
    Release every pooled connection. Registered on ``on_shutdown``.

    Failures are logged rather than raised: this runs during teardown, and an exception
    here would replace whatever the process was actually shutting down for.
    """
    for origin, client in list(_clients.items()):
        try:
            if not client.is_closed:
                await client.aclose()
        except Exception:  # noqa: BLE001 — teardown must not raise
            logger.warning("Could not close the HTTP client for %s", origin, exc_info=True)

    _clients.clear()


def open_origins() -> list:
    """Which origins have a live client. For the test fixture that asserts a run left
    nothing behind, and for a diagnostic."""
    return sorted(origin for origin, client in _clients.items() if not client.is_closed)
