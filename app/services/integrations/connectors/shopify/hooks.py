"""
The two things about Shopify's Admin GraphQL API that cannot be said as data.

**1. A failure arrives as a success.** GraphQL answers everything with HTTP 200. A missing
access scope, a query that costs more than the whole bucket, a field removed in this API
version — all of them are ``{"errors": [...], "data": null}`` under a 200, and the
declarative path has no opinion about a 200. Left alone, the read finds no records, paging
stops because "the last page was empty", and the run ends **green**. A store that was
refused and a store with no orders then look identical, and the refused one is the case
nobody investigates. :meth:`ShopifyHooks.after_response` is the whole reason the
``after_response`` seam exists.

**2. The bucket is shared and priced in points, not requests.** Shopify's Admin API limit
is a points bucket per shop — and *per shop*, not per app, so every other app the merchant
installed is drawing on the same allowance. A locally-computed request-per-second rate
cannot see that, and is therefore always optimistic. Every response carries
``extensions.cost.throttleStatus``, which is the vendor's own view of what is left, and
feeding it to :meth:`Bucket.apply_vendor_view` is the correction. That method only ever
*lowers* the local view, which is what makes accepting a number from the far end safe.

**What this is not.** It is not cost accounting. The limiter still spends one token per
request whatever the query cost, so a 900-point products query and a 5-point shop query
are charged alike. The correction above plus retrying on ``THROTTLED`` absorbs the
difference in practice, and a points dimension on the bucket is the honest fix. It is not
done here; see ``documentations/SHOPIFY_CONNECTOR.md``.

Nothing in this module does I/O or touches the database.
"""

import logging
import re
from typing import Any, List, Mapping, Optional, Set, Tuple

from app.services.integrations.errors import NodeFailure
from app.services.integrations.runtime import rate_limiter

logger = logging.getLogger(__name__)


#: A shop domain, and the only thing this connector will build an address from.
#:
#: This is the security control the whole vendor-connector shape rests on. The value is
#: user-supplied text that becomes the **host of a request carrying the merchant's access
#: token**, so an unconstrained one hands both the destination and the credential to
#: whoever typed it. Anchored by ``re.fullmatch`` at the point of use.
#:
#: Deliberately narrow: lowercase only (Shopify's own domains are, and accepting mixed
#: case would mean two connections for one shop), no port, no path, no userinfo, and the
#: literal ``.myshopify.com`` suffix — which is what makes ``shop.myshopify.com.evil.com``
#: fail rather than pass a naive "contains myshopify.com" reading.
SHOP_DOMAIN_PATTERN = r"[a-z0-9]([a-z0-9-]{0,58}[a-z0-9])?\.myshopify\.com"

#: Codes worth trying again. Shopify's throttle is a full bucket, not a broken request:
#: the same query a second later usually succeeds.
_RETRYABLE_CODES = frozenset({"THROTTLED"})

#: Codes where trying again cannot help, so it should not be tried. A missing scope stays
#: missing however many times it is asked for, and a query over the cost ceiling is over
#: it every time — eight backoffs against either is eight requests spent proving what the
#: first one said.
_PERMANENT_CODES = frozenset(
    {
        "ACCESS_DENIED",
        "UNAUTHENTICATED",
        "MAX_COST_EXCEEDED",
        "SHOP_INACTIVE",
        "FORBIDDEN",
    }
)

#: The longest wait a throttle is allowed to ask for. The computed figure is normally a
#: second or two; the clamp is there for a shop whose bucket is being drained by another
#: app, where the honest answer is "a very long time" and the useful one is "back off, then
#: let the retry engine give up and say so".
MAX_THROTTLE_WAIT_SECONDS = 60.0

#: How many vendor messages to put in one sentence. Shopify reports every bad field in one
#: response, and a run whose error message is forty lines of GraphQL is a run whose error
#: message does not get read.
MAX_REPORTED_ERRORS = 3


class ShopifyHooks:
    """The connector's three procedural bits. Stateless; one instance on the spec."""

    # `resolve_base_url` is deliberately absent. `ConnectorSpec.render_base_url` already
    # substitutes the shop domain into `base_url_template` *and re-checks it against the
    # pattern* on the way past; a hook here would be a second place that check could be
    # forgotten, so the one that has it wins. The seam stays available for a connector
    # whose address is genuinely computed rather than templated.

    def after_response(self, read: Any, operation: Any, context: Any) -> None:
        """
        Look inside a 200 for the failure Shopify puts there, and take its word on the
        bucket.

        Order matters: the throttle correction runs **first**, so a throttled response
        still teaches the limiter how little is left before the failure it raises sends
        the caller back to try again. Getting this the other way round would mean the one
        response that knows the bucket is empty is the one whose reading is discarded.
        """
        payload = read.payload if isinstance(read.payload, Mapping) else None
        if payload is None:
            return

        self._correct_the_bucket(payload, operation, context)

        errors = _error_entries(payload)
        if not errors:
            return

        raise self._failure(errors, payload, context)

    # -- the two halves -----------------------------------------------------

    def _correct_the_bucket(
        self, payload: Mapping, operation: Any, context: Any
    ) -> None:
        """Believe ``throttleStatus`` about how much of the shop's allowance is left."""
        status = _throttle_status(payload)
        if status is None:
            return

        available, maximum = status
        connector = getattr(context, "connector", None)
        if connector is None:
            return

        rate_limiter.limiter.bucket(
            context.connection_key, connector.rate_limits
        ).apply_vendor_view(available, maximum)

        logger.debug(
            "Shopify throttle after %s on %s: %s of %s points available",
            getattr(operation, "operation_id", "?"),
            context.connection_key,
            available,
            maximum,
        )

    def _failure(
        self, errors: List[Mapping], payload: Mapping, context: Any
    ) -> NodeFailure:
        """One ``NodeFailure`` carrying Shopify's own words and a retry decision."""
        codes = _error_codes(errors)
        label = getattr(context, "connection_label", "This connection")

        failure = NodeFailure(
            f"{label} refused the request: {_readable(errors)}",
            retryable=bool(codes & _RETRYABLE_CODES),
            permanent=bool(codes & _PERMANENT_CODES),
            # A GraphQL error has no status of its own. Recording the 200 would be a lie
            # in the audit and recording a made-up 4xx a worse one, so it records neither.
            status_code=None,
        )

        if codes & _RETRYABLE_CODES:
            failure.retry_after = _throttle_wait(payload)

        return failure


# ---------------------------------------------------------------------------
# Reading the response
# ---------------------------------------------------------------------------


def _error_entries(payload: Mapping) -> List[Mapping]:
    """
    The ``errors`` array, as mappings.

    A response with errors *and* partial data is still treated as a failure. Shopify does
    return both when a nullable field is refused, and taking the partial rows would mean a
    sync that quietly dropped whichever field the token was not allowed to read — the
    silent-partial-success shape this module exists to prevent.
    """
    raw = payload.get("errors")
    if not isinstance(raw, (list, tuple)):
        return []

    return [entry for entry in raw if isinstance(entry, Mapping)]


def _error_codes(errors: List[Mapping]) -> Set[str]:
    codes = set()
    for entry in errors:
        extensions = entry.get("extensions")
        if isinstance(extensions, Mapping):
            code = str(extensions.get("code") or "").strip().upper()
            if code:
                codes.add(code)
    return codes


def _readable(errors: List[Mapping]) -> str:
    """Shopify's messages as one sentence, capped and de-duplicated."""
    seen: List[str] = []
    for entry in errors:
        message = str(entry.get("message") or "").strip()
        if message and message not in seen:
            seen.append(message)

    if not seen:
        return "it reported an error but did not say what."

    shown = "; ".join(seen[:MAX_REPORTED_ERRORS])
    remaining = len(seen) - MAX_REPORTED_ERRORS

    if remaining > 0:
        return f"{shown} (and {remaining} more)."

    return f"{shown}."


def _throttle_status(payload: Mapping) -> Optional[Tuple[float, float]]:
    """``(currentlyAvailable, maximumAvailable)`` from ``extensions.cost``, or None."""
    extensions = payload.get("extensions")
    if not isinstance(extensions, Mapping):
        return None

    cost = extensions.get("cost")
    if not isinstance(cost, Mapping):
        return None

    status = cost.get("throttleStatus")
    if not isinstance(status, Mapping):
        return None

    available = _number(status.get("currentlyAvailable"))
    maximum = _number(status.get("maximumAvailable"))

    if available is None or maximum is None or maximum <= 0:
        return None

    return available, maximum


def _throttle_wait(payload: Mapping) -> Optional[float]:
    """
    How long until the bucket holds enough points for this query again.

    Computed from the vendor's own numbers rather than left to the retry engine's fixed
    backoff, because the two answers differ by a lot: a 1000-point query against a bucket
    restoring 100 points a second needs several seconds, and 0.5 then 1.0 then 2.0 spends
    three requests discovering that.
    """
    extensions = payload.get("extensions")
    if not isinstance(extensions, Mapping):
        return None

    cost = extensions.get("cost")
    if not isinstance(cost, Mapping):
        return None

    status = cost.get("throttleStatus")
    if not isinstance(status, Mapping):
        return None

    needed = _number(cost.get("requestedQueryCost"))
    available = _number(status.get("currentlyAvailable"))
    restore_rate = _number(status.get("restoreRate"))

    if needed is None or available is None or not restore_rate:
        return None

    shortfall = needed - available
    if shortfall <= 0:
        return None

    return min(shortfall / restore_rate, MAX_THROTTLE_WAIT_SECONDS)


def _number(value: Any) -> Optional[float]:
    """A float, or None. A vendor field that is null or a word is not a number."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def is_shop_domain(value: str) -> bool:
    """Whether a string is a shop domain. The pattern, applied the one right way."""
    return bool(re.fullmatch(SHOP_DOMAIN_PATTERN, str(value or "").strip()))
