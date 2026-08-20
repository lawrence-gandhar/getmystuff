"""
Tests for ``runtime/rate_limiter.py``.

Two limits with different characters, and the tests are shaped by which failure each one
prevents.

**Per second** is a leaky bucket, and the assertion that matters is that the vendor's own
view can only *lower* it. Shopify's bucket is per shop and shared with every other app
the merchant has installed, so a locally-computed bucket is sending into one somebody
else has already drained — and being right about our own rate is no defence.

**Per day** is persisted, and it refuses at the *soft* limit. A marketplace application
that blows its daily cap gets suspended, and a concurrent worker's in-flight requests
have not reached the counter yet — so stopping exactly at the cap stops after exceeding
it.

Nothing here sleeps for real. ``sleep`` is injected and records what it was asked for; a
rate-limit test that actually waited would be the slowest file in the suite and would
still only prove that ``asyncio.sleep`` works.
"""

from __future__ import annotations

from typing import List

import pytest

from app.services.integrations.connectors.spec import RateLimitSpec
from app.services.integrations.errors import NodeFailure
from app.services.integrations.runtime.rate_limiter import (
    Bucket,
    RateLimiter,
    check_daily_cap,
    read_vendor_view,
)


class Recorder:
    def __init__(self) -> None:
        self.slept: List[float] = []

    async def __call__(self, seconds: float) -> None:
        self.slept.append(seconds)


FAST = RateLimitSpec(requests_per_second=10.0, burst=2)


class TestTheBucket:
    def test_it_starts_full(self) -> None:
        """
        A connection that has not been used has not spent anything. Starting empty would
        make the first request of every run wait for no reason.
        """
        bucket = Bucket(rate=10.0, capacity=2.0, tokens=2.0)

        assert bucket.delay_for_one(now=0.0) == 0.0

    def test_it_refills_over_time(self) -> None:
        bucket = Bucket(rate=10.0, capacity=2.0, tokens=0.0, updated_at=0.0)

        assert bucket.delay_for_one(now=0.0) == pytest.approx(0.1)
        assert bucket.delay_for_one(now=0.1) == 0.0

    def test_taking_a_token_costs_one(self) -> None:
        bucket = Bucket(rate=1.0, capacity=2.0, tokens=2.0, updated_at=0.0)

        bucket.take(now=0.0)
        bucket.take(now=0.0)

        assert bucket.delay_for_one(now=0.0) == pytest.approx(1.0)

    def test_it_never_holds_more_than_its_capacity(self) -> None:
        """An idle connection does not accrue a burst it can spend all at once — which
        is precisely what a vendor's own limiter is watching for."""
        bucket = Bucket(rate=10.0, capacity=2.0, tokens=0.0, updated_at=0.0)

        bucket.delay_for_one(now=1000.0)

        assert bucket.tokens == 2.0


class TestTheVendorViewOnlyLowers:
    """The assertion in the module docstring."""

    def test_a_vendor_reporting_almost_none_left_drains_the_bucket(self) -> None:
        bucket = Bucket(rate=10.0, capacity=40.0, tokens=40.0)

        bucket.apply_vendor_view(remaining=2, limit=40)

        assert bucket.tokens == pytest.approx(2.0)

    def test_a_vendor_reporting_plenty_does_not_raise_it(self) -> None:
        """
        Our configured rate is also protecting us from a limit nobody documented, so a
        cheerful vendor does not license exceeding it.
        """
        bucket = Bucket(rate=10.0, capacity=40.0, tokens=1.0)

        bucket.apply_vendor_view(remaining=40, limit=40)

        assert bucket.tokens == pytest.approx(1.0)

    def test_a_nonsense_limit_is_ignored(self) -> None:
        bucket = Bucket(rate=10.0, capacity=40.0, tokens=40.0)

        bucket.apply_vendor_view(remaining=0, limit=0)

        assert bucket.tokens == 40.0


class TestReadingTheVendorView:
    def test_shopifys_used_of_limit_form(self) -> None:
        """``X-Shopify-Shop-Api-Call-Limit: 32/40`` — used, not remaining."""
        assert read_vendor_view({"X-Shopify-Shop-Api-Call-Limit": "32/40"}) == (8.0, 40.0)

    def test_the_remaining_and_limit_pair(self) -> None:
        assert read_vendor_view(
            {"X-RateLimit-Remaining": "17", "X-RateLimit-Limit": "100"}
        ) == (17.0, 100.0)

    def test_the_header_names_are_case_insensitive(self) -> None:
        assert read_vendor_view({"ratelimit-remaining": "5", "ratelimit-limit": "20"}) == (
            5.0,
            20.0,
        )

    def test_a_response_saying_nothing_is_none(self) -> None:
        """Which is most of them, and must be silent rather than a fault."""
        assert read_vendor_view({"content-type": "application/json"}) is None

    def test_a_malformed_value_is_ignored_rather_than_raising(self) -> None:
        assert read_vendor_view({"x-ratelimit-remaining": "lots", "x-ratelimit-limit": "100"}) is None


class TestAcquire:
    async def test_a_request_within_the_burst_does_not_wait(self) -> None:
        limiter = RateLimiter()
        sleep = Recorder()

        waited = await limiter.acquire("conn-1", FAST, sleep=sleep)

        assert waited == 0.0
        assert sleep.slept == []

    async def test_a_request_past_the_burst_waits(self) -> None:
        limiter = RateLimiter()
        sleep = Recorder()

        for _ in range(3):
            await limiter.acquire("conn-1", FAST, sleep=sleep)

        assert sleep.slept, "the third request should have waited"

    async def test_two_connections_do_not_queue_behind_each_other(self) -> None:
        """
        The buckets are per key. A slow destination must not hold up a fast one, and a
        single global bucket would make every connection as slow as the most limited.
        """
        limiter = RateLimiter()
        sleep = Recorder()

        for _ in range(2):
            await limiter.acquire("conn-1", FAST, sleep=sleep)

        waited = await limiter.acquire("conn-2", FAST, sleep=sleep)

        assert waited == 0.0

    async def test_the_vendor_view_is_applied_to_the_right_key(self) -> None:
        limiter = RateLimiter()

        limiter.observe("conn-1", {"x-ratelimit-remaining": "0", "x-ratelimit-limit": "40"}, FAST)

        assert limiter.bucket("conn-1", FAST).tokens == pytest.approx(0.0)
        assert limiter.bucket("conn-2", FAST).tokens > 0

    async def test_a_response_with_no_rate_headers_changes_nothing(self) -> None:
        limiter = RateLimiter()
        before = limiter.bucket("conn-1", FAST).tokens

        limiter.observe("conn-1", {"content-type": "application/json"}, FAST)

        assert limiter.bucket("conn-1", FAST).tokens == before


class TestTheDailyCap:
    async def test_no_daily_limit_means_no_counter_at_all(self) -> None:
        """The bump is skipped, not merely ignored — a database round trip per request
        for a connector with no daily cap would be pure cost."""
        bumped = []

        async def bump(cost: int) -> int:
            bumped.append(cost)
            return 1

        await check_daily_cap(
            RateLimitSpec(daily_limit=None), bump=bump, connection_label="X"
        )

        assert bumped == []

    async def test_it_spends_against_the_counter(self) -> None:
        spent = []

        async def bump(cost: int) -> int:
            spent.append(cost)
            return len(spent)

        spec = RateLimitSpec(daily_limit=200_000)
        await check_daily_cap(spec, bump=bump, connection_label="Location 12")

        assert spent == [1]

    async def test_it_refuses_at_the_soft_limit_not_the_cap(self) -> None:
        """
        A concurrent worker's in-flight requests have not reached the counter yet, so
        stopping exactly at the cap stops after exceeding it.
        """
        spec = RateLimitSpec(daily_limit=100, daily_soft_limit=0.95)

        async def bump(cost: int) -> int:
            return 96

        with pytest.raises(NodeFailure) as caught:
            await check_daily_cap(spec, bump=bump, connection_label="Location 12")

        assert "96" in str(caught.value)
        assert "100" in str(caught.value)

    async def test_just_under_the_soft_limit_is_allowed(self) -> None:
        spec = RateLimitSpec(daily_limit=100, daily_soft_limit=0.95)

        async def bump(cost: int) -> int:
            return 95

        await check_daily_cap(spec, bump=bump, connection_label="Location 12")

    async def test_the_refusal_is_permanent(self) -> None:
        """
        Waiting will not help until tomorrow, and a retry loop against a
        suspended-account risk is the wrong instinct.
        """
        spec = RateLimitSpec(daily_limit=10)

        async def bump(cost: int) -> int:
            return 100

        with pytest.raises(NodeFailure) as caught:
            await check_daily_cap(spec, bump=bump, connection_label="Location 12")

        assert caught.value.permanent is True

    async def test_the_refusal_names_the_connection_and_says_when_it_will_work(self) -> None:
        spec = RateLimitSpec(daily_limit=10)

        async def bump(cost: int) -> int:
            return 100

        with pytest.raises(NodeFailure) as caught:
            await check_daily_cap(spec, bump=bump, connection_label="GHL Location 12")

        assert "GHL Location 12" in str(caught.value)
        assert "daily allowance resets" in str(caught.value)
