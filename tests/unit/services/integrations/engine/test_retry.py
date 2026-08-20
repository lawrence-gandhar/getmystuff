"""
Tests for ``engine/retry.py``.

Three things are worth pinning here, and each corresponds to a way a retry loop hurts
rather than helps.

**Jitter is real.** Four chunks of a write node fail on the same rate limit at the same
moment. Without jitter all four sleep for exactly the same interval and retry in
lockstep, which is the same burst one second later. ``random_fn`` is injected so this
can be asserted exactly rather than as a range — a test that accepts anything in a
window would also accept the jitter being deleted.

**``Retry-After`` wins, and is clamped.** A server that has said how long to wait knows
more than our doubling does. A misconfigured gateway saying ``86400`` would park a
worker for a day, and a run that never finishes is indistinguishable from one that hung.

**Cancellation is never slept through.** Retrying a cancelled operation is doing more
work on the way out, which is exactly what cancellation asks not to happen.

Nothing here sleeps for real: ``sleep`` is injected and records what it was asked for.
A retry test that actually waited would be the slowest file in the suite and would still
only prove that ``asyncio.sleep`` works.
"""

from __future__ import annotations

import asyncio
from typing import List

import pytest

from app.services.integrations.engine.retry import (
    DEFAULT_POLICY,
    MAX_DELAY_SECONDS,
    RetryPolicy,
    RetryVerdict,
    backoff_delay,
    run_with_retries,
)


class Recorder:
    """A ``sleep`` that records rather than waits."""

    def __init__(self) -> None:
        self.slept: List[float] = []

    async def __call__(self, seconds: float) -> None:
        self.slept.append(seconds)


ALWAYS = lambda exc: RetryVerdict(retry=True, reason="transient")  # noqa: E731
NEVER = lambda exc: RetryVerdict(retry=False, reason="permanent")  # noqa: E731
NO_JITTER = RetryPolicy(jitter=0.0)


class TestBackoffDelay:
    def test_it_doubles(self) -> None:
        assert backoff_delay(2, policy=NO_JITTER) == 0.5
        assert backoff_delay(3, policy=NO_JITTER) == 1.0
        assert backoff_delay(4, policy=NO_JITTER) == 2.0

    def test_it_is_capped(self) -> None:
        assert backoff_delay(40, policy=NO_JITTER) == MAX_DELAY_SECONDS

    def test_jitter_spreads_the_wait_both_ways(self) -> None:
        """
        Pinned at both ends of the range rather than sampled, so removing the jitter
        fails this rather than passing it 96% of the time.
        """
        policy = RetryPolicy(jitter=0.25)

        lowest = backoff_delay(3, policy=policy, random_fn=lambda: 0.0)
        highest = backoff_delay(3, policy=policy, random_fn=lambda: 1.0)
        middle = backoff_delay(3, policy=policy, random_fn=lambda: 0.5)

        assert (lowest, middle, highest) == (0.75, 1.0, 1.25)

    def test_retry_after_overrides_the_doubling(self) -> None:
        assert backoff_delay(2, policy=NO_JITTER, retry_after=30) == 30.0

    def test_retry_after_is_clamped_too(self) -> None:
        """A gateway asking for a day would otherwise park a worker for a day."""
        assert backoff_delay(2, policy=NO_JITTER, retry_after=86_400) == MAX_DELAY_SECONDS

    def test_a_delay_is_never_negative(self) -> None:
        policy = RetryPolicy(jitter=2.0)  # absurd, but must not go below zero

        assert backoff_delay(2, policy=policy, random_fn=lambda: 0.0) >= 0.0


class TestRunWithRetries:
    async def test_a_call_that_works_is_made_once(self) -> None:
        calls = []

        async def operation(attempt: int) -> str:
            calls.append(attempt)
            return "ok"

        sleep = Recorder()
        result = await run_with_retries(
            operation, classify=ALWAYS, policy=NO_JITTER, sleep=sleep
        )

        assert result == "ok"
        assert calls == [1]
        assert sleep.slept == []

    async def test_it_retries_up_to_the_limit_then_raises_the_real_error(self) -> None:
        """
        Re-raised as itself, not wrapped: the node runner already knows how to turn it
        into a sentence, and a wrapper would put this module's vocabulary between the
        user and what happened.
        """
        calls = []

        async def operation(attempt: int) -> None:
            calls.append(attempt)
            raise ConnectionError("refused")

        sleep = Recorder()
        with pytest.raises(ConnectionError, match="refused"):
            await run_with_retries(
                operation, classify=ALWAYS, policy=NO_JITTER, sleep=sleep
            )

        assert calls == [1, 2, 3]
        assert sleep.slept == [0.5, 1.0]

    async def test_it_succeeds_on_a_later_attempt(self) -> None:
        async def operation(attempt: int) -> str:
            if attempt < 3:
                raise ConnectionError("refused")
            return "ok"

        assert (
            await run_with_retries(
                operation, classify=ALWAYS, policy=NO_JITTER, sleep=Recorder()
            )
            == "ok"
        )

    async def test_a_permanent_failure_is_not_retried_at_all(self) -> None:
        """
        Zero retries, not fewer retries. This is the shape of the rule that protects a
        merchant's store: a read timeout on a non-idempotent write must be attempted
        exactly once.
        """
        calls = []

        async def operation(attempt: int) -> None:
            calls.append(attempt)
            raise TimeoutError("no response")

        sleep = Recorder()
        with pytest.raises(TimeoutError):
            await run_with_retries(
                operation, classify=NEVER, policy=NO_JITTER, sleep=sleep
            )

        assert calls == [1]
        assert sleep.slept == []

    async def test_the_servers_retry_after_is_used(self) -> None:
        def classify(exc: BaseException) -> RetryVerdict:
            return RetryVerdict(retry=True, retry_after=12, reason="rate limited")

        async def operation(attempt: int) -> None:
            raise RuntimeError("429")

        sleep = Recorder()
        with pytest.raises(RuntimeError):
            await run_with_retries(
                operation, classify=classify, policy=NO_JITTER, sleep=sleep
            )

        assert sleep.slept == [12.0, 12.0]

    async def test_cancellation_is_immediate_and_unslept(self) -> None:
        calls = []

        async def operation(attempt: int) -> None:
            calls.append(attempt)
            raise asyncio.CancelledError()

        sleep = Recorder()
        with pytest.raises(asyncio.CancelledError):
            await run_with_retries(
                operation, classify=ALWAYS, policy=NO_JITTER, sleep=sleep
            )

        assert calls == [1]
        assert sleep.slept == []

    async def test_classify_never_sees_a_cancellation(self) -> None:
        """
        A ``classify`` that reported ``retry=True`` for everything would otherwise turn
        a cancellation into two more attempts.
        """
        seen = []

        def classify(exc: BaseException) -> RetryVerdict:
            seen.append(exc)
            return RetryVerdict(retry=True)

        async def operation(attempt: int) -> None:
            raise asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await run_with_retries(
                operation, classify=classify, policy=NO_JITTER, sleep=Recorder()
            )

        assert seen == []

    async def test_the_on_retry_hook_sees_each_wait(self) -> None:
        """
        What a caller uses to write a heartbeat or a step message while waiting, so a
        two-minute ``Retry-After`` does not look like a hung node.
        """
        notified = []

        async def on_retry(attempt: int, delay: float, exc: BaseException) -> None:
            notified.append((attempt, delay))

        async def operation(attempt: int) -> None:
            raise ConnectionError("refused")

        with pytest.raises(ConnectionError):
            await run_with_retries(
                operation,
                classify=ALWAYS,
                policy=NO_JITTER,
                on_retry=on_retry,
                sleep=Recorder(),
            )

        assert notified == [(1, 0.5), (2, 1.0)]

    async def test_a_single_attempt_policy_never_sleeps(self) -> None:
        async def operation(attempt: int) -> None:
            raise ConnectionError("refused")

        sleep = Recorder()
        with pytest.raises(ConnectionError):
            await run_with_retries(
                operation,
                classify=ALWAYS,
                policy=RetryPolicy(max_attempts=1, jitter=0.0),
                sleep=sleep,
            )

        assert sleep.slept == []


class TestTheDefaults:
    def test_three_attempts_from_half_a_second(self) -> None:
        """
        Same numbers as ``downloader_agents/base/retry.py``, deliberately — a user who
        has read one is not surprised by the other. The shape is shared; the module is
        not, because that one's permanent-failure escape is hard-wired to a different
        exception.
        """
        assert DEFAULT_POLICY.max_attempts == 3
        assert DEFAULT_POLICY.base_delay == 0.5

    def test_jitter_is_on_by_default(self) -> None:
        assert DEFAULT_POLICY.jitter > 0
