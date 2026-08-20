"""
Sending one prepared request, with every guard in the right order.

This is where the pieces meet, and the ordering is the content of the module:

1. **The shape and the destination are checked** — ``outbound_http``, with the
   connection's own policy. Before anything is opened.
2. **The rate limit is waited on** — after the check, so a refused request does not
   spend an allowance.
3. **The credential is applied** — last, and never before the destination is known good.
   A token attached to a request that then turns out to point somewhere private would be
   a token sent somewhere private.
4. **The hook runs and is fenced** — ``assert_hook_kept_the_target``, so a hook cannot
   move the request away from what step 1 approved.
5. **The response is read under a byte cap**, the rate view is taken from it, and a
   non-2xx becomes a ``NodeFailure`` whose ``retryable`` was decided *here*.

**``classify`` is the point of the module.** Whether a failure may be retried is decided
by the code that made the call, from the operation's own declaration, and never
re-derived later from a stored message:

    A write is retried only on a failure that provably never reached the server, unless
    the operation declares itself idempotent or supplies an idempotency header. **A read
    timeout on a non-idempotent write is permanent.**

Shopify's ``POST /orders.json`` has no idempotency header. Retrying a create that timed
out mid-flight duplicates the merchant's order, and no amount of backoff prevents it.

**A redirect is refused rather than followed.** The client has ``follow_redirects=False``
and this does not re-issue: a 3xx from an API endpoint is almost always a sign-in page,
and the one case where it is genuine — a moved resource — is better fixed in the
operation than papered over on every call.
"""

import logging
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

import httpx

from app.services.integrations.connectors.spec import (
    ConnectorSpec,
    OperationSpec,
    PreparedRequest,
    RateLimitSpec,
    assert_hook_kept_the_target,
)
from app.services.integrations.engine import retry as retry_engine
from app.services.integrations.engine.idempotency import write_may_be_retried
from app.services.integrations.errors import NodeFailure
from app.services.integrations.runtime import http_client, rate_limiter, response_reader
from app.services.integrations.runtime.request_builder import serialise_body
from app.utils import outbound_http

logger = logging.getLogger(__name__)


#: Statuses worth trying again. 408 and 425 are the far end saying "not now"; 5xx is the
#: far end being broken, which is usually briefly. 500 is included and 501 is not — one
#: is a crash, the other is "this endpoint does not do that", which will be equally true
#: in half a second.
_RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})

#: Statuses that mean the credential is the problem. They set the connection to
#: ``needs_reauth`` rather than being retried — retrying a 401 is how a working
#: connection gets locked out by a provider that counts failures.
AUTH_STATUSES = frozenset({401, 403})

#: Failures that provably never reached the server. Everything else is assumed to have,
#: which is the safe direction: assuming a request arrived means not repeating it.
_NEVER_ARRIVED = (httpx.ConnectError, httpx.ConnectTimeout, httpx.UnsupportedProtocol)

#: What replaces a credential found in a vendor's own words. See :func:`scrubbed`.
REDACTED = "[redacted]"

#: Below this, a credential is not scrubbed out of an error message. A short value would
#: match ordinary words and turn a useful explanation into a row of asterisks — and
#: anything that short is not a secret worth the message.
MIN_SCRUBBED_LENGTH = 8


def is_retryable_status(status_code: int) -> bool:
    """
    Whether a status is worth sending again.

    Public because the node runners need the same answer when they turn a non-2xx into a
    record row, and two lists of statuses would eventually disagree about 429 — which is
    the one that matters, because getting it wrong means either hammering a rate limit or
    giving up on a wait the vendor asked for.
    """
    return int(status_code) in _RETRYABLE_STATUSES


def is_auth_status(status_code: int) -> bool:
    """Whether a status means the credential is the problem rather than the request."""
    return int(status_code) in AUTH_STATUSES


@dataclass
class SendContext:
    """
    Everything about *this* call that is not in the operation.

    ``auth_header`` arrives already built, from ``credentials/credential_service``. The
    sender never sees a token in any other form and never stores one — which is why
    nothing it logs, previews or hashes can contain one.
    """

    connection_key: str
    connection_label: str
    egress_policy: outbound_http.EgressPolicy = outbound_http.DEFAULT_POLICY
    auth_header: Optional[tuple] = None
    auth_query: Optional[tuple] = None
    timeout: float = http_client.DEFAULT_TIMEOUT_SECONDS
    connector: Optional[ConnectorSpec] = None

    #: Bumps the persisted daily counter. Injected so this layer does no database work;
    #: ``None`` when the connector has no daily cap.
    bump_daily: Optional[Callable[[int], Any]] = None


async def send(
    request: PreparedRequest,
    operation: OperationSpec,
    context: SendContext,
) -> response_reader.ReadResponse:
    """
    One call, guarded. See the module docstring for the ordering.

    Retries are handled by ``engine/retry`` with :func:`classifier_for` deciding what
    may be repeated, so the backoff, the jitter and the ``Retry-After`` handling are the
    ones every other part of the module uses.
    """
    classify = classifier_for(operation)

    async def _attempt(attempt: int) -> response_reader.ReadResponse:
        # The attempt number is the retry loop's bookkeeping, not the request's: the
        # same request is sent each time, or it would not be a retry.
        return await _send_once(request, operation, context)

    return await retry_engine.run_with_retries(
        _attempt,
        classify=classify,
        label=f"{operation.method} {operation.operation_id}",
    )


async def _send_once(
    request: PreparedRequest,
    operation: OperationSpec,
    context: SendContext,
) -> response_reader.ReadResponse:
    # 1. Where it goes, before anything is opened.
    target = await _checked_target(request, context)

    # 2. The allowance, after the check so a refused request does not spend one.
    await rate_limiter.limiter.acquire(
        context.connection_key,
        (context.connector.rate_limits if context.connector else _DEFAULT_LIMITS),
    )

    if context.bump_daily is not None and context.connector is not None:
        await rate_limiter.check_daily_cap(
            context.connector.rate_limits,
            bump=context.bump_daily,
            connection_label=context.connection_label,
        )

    # 3. The credential, last and only now.
    outgoing = _with_credential(request, context)

    # 4. The connector's own hook, fenced.
    outgoing = _through_hook(outgoing, context)

    client = http_client.get_client(f"{target.scheme}://{target.host}")

    # Streamed rather than requested whole, and that is what makes the byte cap real:
    # `client.request()` has already loaded the body by the time it returns, so a cap
    # applied afterwards is a report of how much was allocated rather than a limit.
    try:
        async with client.stream(
            outgoing.method,
            outgoing.url,
            headers=dict(outgoing.headers),
            params=dict(outgoing.params),
            content=_body_bytes(outgoing),
            timeout=context.timeout,
        ) as response:
            if 300 <= response.status_code < 400:
                raise NodeFailure(
                    f"'{context.connection_label}' answered with a redirect, which is "
                    "usually a sign-in page rather than the data. Check the address and "
                    "the credentials on this connection.",
                    permanent=True,
                    status_code=response.status_code,
                )

            # 5. Read under the cap, then take the vendor's view of the bucket.
            read = await response_reader.read_json(response)
    except httpx.HTTPError as exc:
        raise _transport_failure(exc, operation, context) from exc

    if context.connector is not None:
        rate_limiter.limiter.observe(
            context.connection_key, read.headers, context.connector.rate_limits
        )

    # 6. The connector's look at the parsed body, before the status is judged.
    _through_response_hook(read, operation, context)

    if not read.ok:
        raise _status_failure(read, operation, context)

    return read


# ---------------------------------------------------------------------------
# The guards
# ---------------------------------------------------------------------------


async def _checked_target(
    request: PreparedRequest, context: SendContext
) -> outbound_http.ResolvedTarget:
    """
    Shape, then DNS, then IP class — the whole egress guard, before a socket exists.

    ``EgressError`` becomes a ``NodeFailure`` here so the workflow's error path can catch
    it like any other node failure, and permanently: a URL that points somewhere private
    will still point somewhere private in half a second.
    """
    try:
        return await outbound_http.resolve_and_check(
            request.url, policy=context.egress_policy, label=context.connection_label
        )
    except outbound_http.EgressError as exc:
        raise NodeFailure(str(exc), permanent=True) from exc


def _with_credential(request: PreparedRequest, context: SendContext) -> PreparedRequest:
    outgoing = request

    if context.auth_header:
        name, value = context.auth_header
        outgoing = outgoing.with_headers({name: value})

    if context.auth_query:
        name, value = context.auth_query
        params = dict(outgoing.params)
        params[name] = value
        outgoing = type(outgoing)(
            method=outgoing.method,
            url=outgoing.url,
            headers=outgoing.headers,
            params=params,
            json_body=outgoing.json_body,
            host=outgoing.host,
            path=outgoing.path,
        )

    return outgoing


def _through_hook(request: PreparedRequest, context: SendContext) -> PreparedRequest:
    """
    The connector's ``before_request``, if it has one, and the fence around it.

    See ``connectors/spec.assert_hook_kept_the_target``. A hook that moved the request
    would leave the recorded ``operation_hash`` describing something that never happened,
    which is an audit trail quietly claiming the wrong thing.
    """
    connector = context.connector
    hooks = getattr(connector, "hooks", None)
    before_request = getattr(hooks, "before_request", None)

    if before_request is None:
        return request

    after = before_request(request, context)
    assert_hook_kept_the_target(
        request, after, connector_id=connector.connector_id
    )
    return after


def _through_response_hook(
    read: response_reader.ReadResponse, operation: OperationSpec, context: SendContext
) -> None:
    """
    The connector's ``after_response``, if it has one.

    **The hook may only raise; anything it returns is discarded.** That is the fence on
    this side, and it is the mirror of ``assert_hook_kept_the_target``: a hook able to
    rewrite a response could make the recorded step disagree with what the vendor
    actually sent, and an audit trail that can be edited by the thing it audits is not
    one.

    Why this exists at all: an API is free to report failure inside a success. Shopify's
    Admin GraphQL answers a missing scope with HTTP 200 and an ``errors`` array, and with
    nowhere to look at that, the read finds no records, paging stops because "the last
    page was empty", and the run ends **green**. A refused sync and a sync of an empty
    store then look identical, which is the one failure shape that never gets
    investigated.
    """
    hooks = getattr(context.connector, "hooks", None)
    after_response = getattr(hooks, "after_response", None)

    if after_response is None:
        return

    after_response(read, operation, context)


def _body_bytes(request: PreparedRequest) -> Optional[bytes]:
    return serialise_body(request.json_body)


# ---------------------------------------------------------------------------
# Failures, and what may be retried
# ---------------------------------------------------------------------------


def classifier_for(operation: OperationSpec) -> Callable[[BaseException], retry_engine.RetryVerdict]:
    """
    The retry rule for one operation. See the module docstring.

    Returned as a closure over the operation rather than reading it from the exception,
    because the exception knows what happened and only the operation knows whether
    repeating it is safe.
    """

    def classify(exc: BaseException) -> retry_engine.RetryVerdict:
        if isinstance(exc, NodeFailure):
            if exc.permanent:
                return retry_engine.RetryVerdict(retry=False, reason="permanent")
            return retry_engine.RetryVerdict(
                retry=exc.retryable,
                retry_after=getattr(exc, "retry_after", None),
                reason=f"status {exc.status_code}" if exc.status_code else "",
            )

        if isinstance(exc, httpx.HTTPError):
            reached = not isinstance(exc, _NEVER_ARRIVED)

            if operation.is_read:
                # A read that failed can always be repeated: reading twice costs an
                # allowance and changes nothing at the far end.
                return retry_engine.RetryVerdict(retry=True, reason=type(exc).__name__)

            allowed = write_may_be_retried(
                reached_server=reached,
                operation_is_idempotent=operation.idempotent,
                has_idempotency_header=bool(operation.idempotency_header),
            )
            return retry_engine.RetryVerdict(
                retry=allowed,
                reason=type(exc).__name__ if allowed else "may already have happened",
            )

        return retry_engine.RetryVerdict(retry=False, reason=type(exc).__name__)

    return classify


def _transport_failure(
    exc: httpx.HTTPError, operation: OperationSpec, context: SendContext
) -> NodeFailure:
    """
    A connection-level failure, as a sentence and a retry decision.

    The read-timeout-on-a-write case gets its own wording, because "it failed" is the
    wrong thing to tell somebody whose order may or may not have been created. They need
    to check before running it again, and the message says so.
    """
    reached = not isinstance(exc, _NEVER_ARRIVED)

    if operation.is_write and reached:
        retryable = write_may_be_retried(
            reached_server=True,
            operation_is_idempotent=operation.idempotent,
            has_idempotency_header=bool(operation.idempotency_header),
        )
        if not retryable:
            return NodeFailure(
                f"'{context.connection_label}' did not answer in time, and this step "
                "creates or changes records — so it may or may not have gone through. "
                "It was not sent again, because sending it twice could duplicate the "
                "record. Check the destination before running this again.",
                permanent=True,
                retryable=False,
            )

    if isinstance(exc, _NEVER_ARRIVED):
        return NodeFailure(
            f"'{context.connection_label}' could not be reached ({exc}).",
            retryable=True,
        )

    return NodeFailure(
        f"'{context.connection_label}' did not answer in time ({type(exc).__name__}).",
        retryable=True,
    )


def _status_failure(
    read: response_reader.ReadResponse,
    operation: OperationSpec,
    context: SendContext,
) -> NodeFailure:
    status = read.status_code

    if status in AUTH_STATUSES:
        # Not retried. A provider that counts consecutive auth failures is one that will
        # lock the connection out, and the answer is a person reconnecting rather than
        # another attempt.
        return NodeFailure(
            f"'{context.connection_label}' rejected the credentials "
            f"({status}). Reconnect it and run this again."
            + _detail(read, context),
            permanent=True,
            status_code=status,
        )

    retryable = status in _RETRYABLE_STATUSES

    if operation.is_write and retryable and status != 429:
        # A 5xx on a write may have been applied before the error was generated. 429 is
        # the exception: it means the request was rejected before any work was done.
        retryable = write_may_be_retried(
            reached_server=True,
            operation_is_idempotent=operation.idempotent,
            has_idempotency_header=bool(operation.idempotency_header),
        )

    failure = NodeFailure(
        scrubbed(
            response_reader.failure_message(read, label=f"'{context.connection_label}'"),
            context,
        ),
        retryable=retryable,
        permanent=not retryable,
        status_code=status,
    )
    failure.retry_after = response_reader.retry_after_seconds(read.headers)  # type: ignore[attr-defined]
    return failure


def _detail(read: response_reader.ReadResponse, context: SendContext) -> str:
    message = scrubbed(response_reader.vendor_message(read), context)
    return f" It said: {message}" if message else ""


def scrubbed(text: str, context: SendContext) -> str:
    """
    A vendor's own words with this connection's credential taken back out of them.

    ``response_reader.redact`` is a deny-list over **key names** — ``authorization``,
    ``token``, ``api_key`` — and it is the right tool for a body that carries a secret in
    a field. It cannot see one embedded in free text, and that is exactly what a
    surprising number of APIs return: ``{"error": "invalid key sk-live-…"}``, or a 400
    quoting the query string it was given. The key name is ``error``; the secret is in the
    value.

    So the value is scrubbed by **value**, here, because this is the only layer that knows
    what the credential is. The message this produces reaches a browser, a run record and
    a log line, and any one of those is somewhere a token must not be.

    Short values are left alone. A two-character credential would match half the words in
    an English sentence and turn a useful error into a row of asterisks — and anything
    that short is not a secret worth protecting at the cost of the message.
    """
    if not text:
        return text

    for pair in (context.auth_header, context.auth_query):
        if not pair:
            continue
        value = str(pair[1] or "")
        if len(value) < MIN_SCRUBBED_LENGTH:
            continue
        text = text.replace(value, REDACTED)
        # The header value is usually "Bearer sk-1", so the bare credential — which is
        # what a vendor echoes — has to be replaced as well as the whole rendered header.
        for part in value.split():
            if len(part) >= MIN_SCRUBBED_LENGTH:
                text = text.replace(part, REDACTED)

    return text


#: Used when a call somehow arrives without a connector — a defensive default rather
#: than an expected path, deliberately conservative so the fallback is slower than any
#: real connector's configured rate rather than faster.
_DEFAULT_LIMITS = RateLimitSpec()
