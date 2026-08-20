"""
Not sending faster than the far end will tolerate.

Two limits with genuinely different characters, and conflating them would get one of
them wrong.

**Per second: an in-memory leaky bucket, corrected from the response.** The correction
is not a refinement. Shopify's bucket is per shop and shared with **every other app the
merchant has installed** — a locally-computed bucket is sending into one somebody else
has already drained, and being right about our own rate is no defence. So where the
vendor reports its own view (``X-Shopify-Shop-Api-Call-Limit`` and its equivalents),
that view wins.

**Per day: a row in ``integration_rate_counters``.** An in-process counter resets on
every deploy, so a day with four deploys can spend four times the cap while believing it
spent one. A marketplace application that blows its daily cap gets suspended, which
makes this the most account-endangering number in the module — and the only one that has
to survive a restart. The counter is bumped through a callback the caller supplies, so
this module still does no I/O and can be tested without a database.

**Known limitation, and it is real.** An in-process bucket is per worker. Under
``uvicorn --workers N`` the effective send rate is N times what is configured. The
mitigation is structural rather than clever: the sync worker runs as a single in-process
loop, the same call ``job_queue`` makes for a related reason, and the daily counter is in
Postgres regardless. Stated here because a reader of this file would otherwise reasonably
assume the bucket is global.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Dict, Mapping, Optional

from app.services.integrations.connectors.spec import RateLimitSpec
from app.services.integrations.errors import NodeFailure

logger = logging.getLogger(__name__)


#: Headers that carry a vendor's own view of the bucket, and how to read them. Kept as a
#: table rather than a hook because all three in scope are "used/limit" or a pair of
#: remaining/limit numbers, and a hook per connector for that would be four copies of
#: one parser.
_USED_OF_LIMIT = ("x-shopify-shop-api-call-limit",)
_REMAINING = ("x-ratelimit-remaining", "ratelimit-remaining", "x-rate-limit-remaining")
_LIMIT = ("x-ratelimit-limit", "ratelimit-limit", "x-rate-limit-limit")


@dataclass
class Bucket:
    """
    One origin's allowance, as tokens that refill over time.

    A leaky bucket rather than a fixed window, because a fixed window lets a whole
    second's worth of requests leave in the same millisecond — which is precisely the
    burst a vendor's own limiter is watching for.
    """

    rate: float
    capacity: float
    tokens: float = 0.0
    updated_at: float = field(default_factory=time.monotonic)

    def _refill(self, now: float) -> None:
        elapsed = max(0.0, now - self.updated_at)
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        self.updated_at = now

    def delay_for_one(self, now: float) -> float:
        """How long until a token is available. Zero when one already is."""
        self._refill(now)
        if self.tokens >= 1.0:
            return 0.0
        return (1.0 - self.tokens) / self.rate

    def take(self, now: float) -> None:
        self._refill(now)
        self.tokens = max(0.0, self.tokens - 1.0)

    def apply_vendor_view(self, remaining: float, limit: float) -> None:
        """
        Believe the vendor about how much of the bucket is left.

        Only ever *lowers* the local view. A vendor reporting plenty of room does not
        mean our own configured rate should be exceeded — the configured rate is also
        protecting us from a limit nobody documented — but a vendor reporting almost none
        is information we do not otherwise have.
        """
        if limit <= 0:
            return
        share = max(0.0, min(1.0, remaining / limit))
        self.tokens = min(self.tokens, self.capacity * share)


class RateLimiter:
    """
    Buckets by key, plus the daily counter's plumbing.

    An instance rather than module state so a test gets a fresh one and a second worker
    process is honestly a second limiter. ``sender`` holds the process-wide one.
    """

    def __init__(self) -> None:
        self._buckets: Dict[str, Bucket] = {}
        self._locks: Dict[str, asyncio.Lock] = {}

    def bucket(self, key: str, spec: RateLimitSpec) -> Bucket:
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = Bucket(
                rate=spec.requests_per_second,
                capacity=float(max(1, spec.burst)),
                # Starts full: a connection that has not been used has not spent
                # anything, and starting empty would make the first request of every run
                # wait for no reason.
                tokens=float(max(1, spec.burst)),
            )
            self._buckets[key] = bucket
        return bucket

    async def acquire(
        self,
        key: str,
        spec: RateLimitSpec,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> float:
        """
        Wait until this key may send. Returns how long it waited.

        The lock is per key, so two chunks of the same write node queue behind each
        other while a different connection's call goes straight through. Without it both
        would read the same token count and both would take it.
        """
        lock = self._locks.setdefault(key, asyncio.Lock())
        waited = 0.0

        async with lock:
            bucket = self.bucket(key, spec)

            delay = bucket.delay_for_one(time.monotonic())
            if delay > 0:
                waited = delay
                await sleep(delay)

            bucket.take(time.monotonic())

        return waited

    def observe(self, key: str, headers: Mapping[str, str], spec: RateLimitSpec) -> None:
        """
        Correct the bucket from what the response said.

        Silent when the response says nothing, which is most of them. See the module
        docstring on why this matters more than it looks.
        """
        view = read_vendor_view(headers)
        if view is None:
            return

        remaining, limit = view
        self.bucket(key, spec).apply_vendor_view(remaining, limit)
        logger.debug("Rate view for %s: %s of %s remaining", key, remaining, limit)

    def reset(self) -> None:
        """Test support. A leaked bucket would make one test's waiting another's."""
        self._buckets.clear()
        self._locks.clear()


def read_vendor_view(headers: Mapping[str, str]) -> Optional[tuple]:
    """
    ``(remaining, limit)`` from a response's headers, or ``None``.

    Two shapes, because vendors disagree: Shopify sends ``used/limit`` in one header,
    and everybody else sends remaining and limit in two.
    """
    lowered = {str(k).lower(): str(v) for k, v in (headers or {}).items()}

    for name in _USED_OF_LIMIT:
        raw = lowered.get(name)
        if raw and "/" in raw:
            used, _, limit = raw.partition("/")
            numbers = _numbers(used, limit)
            if numbers:
                spent, total = numbers
                return (max(0.0, total - spent), total)

    remaining = _first(lowered, _REMAINING)
    limit = _first(lowered, _LIMIT)
    numbers = _numbers(remaining, limit)
    return numbers if numbers else None


def _first(headers: Mapping[str, str], names: tuple) -> Optional[str]:
    for name in names:
        if name in headers:
            return headers[name]
    return None


def _numbers(*values) -> Optional[tuple]:
    try:
        parsed = tuple(float(str(value).strip()) for value in values if value is not None)
    except (TypeError, ValueError):
        return None
    return parsed if len(parsed) == len(values) else None


# ---------------------------------------------------------------------------
# The daily cap
# ---------------------------------------------------------------------------


async def check_daily_cap(
    spec: RateLimitSpec,
    *,
    bump: Callable[[int], Awaitable[int]],
    connection_label: str,
    cost: int = 1,
) -> None:
    """
    Spend ``cost`` against the persisted daily counter, refusing at the soft limit.

    ``bump`` is supplied by the caller and does the single
    ``INSERT … ON CONFLICT DO UPDATE … RETURNING`` — one statement, so the read and the
    write cannot be interleaved by another worker. It is injected rather than imported so
    this module still does no I/O.

    **Refused at the soft limit, not the hard one.** A concurrent worker's in-flight
    requests have not reached the counter yet, so stopping exactly at the cap stops after
    exceeding it. The failure is permanent — waiting will not help until tomorrow, and a
    retry loop against a suspended-account risk is the wrong instinct.
    """
    if not spec.daily_limit:
        return

    total = await bump(cost)
    ceiling = spec.daily_limit * spec.daily_soft_limit

    if total > ceiling:
        raise NodeFailure(
            f"'{connection_label}' has used {int(total):,} of its {spec.daily_limit:,} "
            "requests for today, so this run stopped rather than risk the account being "
            "suspended. It will be able to run again after the daily allowance resets.",
            permanent=True,
        )


#: The process-wide limiter. One per worker — see the module docstring's note about
#: ``--workers N``.
limiter = RateLimiter()
