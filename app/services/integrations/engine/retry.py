"""
Retrying one call to somebody else's server, and giving up when trying again cannot help.

Mirrors the shape of ``app/services/downloader_agents/base/retry.py`` — same doubling
from half a second, same immediate re-raise of ``CancelledError`` — but deliberately
does **not** import it. That module's permanent-failure escape is hard-wired to
``ToolQueryError`` and its ``on_discard`` hook exists to delete a half-written part
file; neither means anything here, and bending it to fit would make one function serve
two features that are about to diverge.

Two things are new, and both come from talking to HTTP rather than to a database.

**Jitter.** A write node sends four chunks concurrently. All four hit the same rate
limit at the same moment, all four get a 429, and without jitter all four sleep for
exactly the same interval and retry in lockstep — which is the same burst again, one
second later. The spread is proportional rather than fixed, because the point is to
decorrelate the retries, not to add a constant.

**``Retry-After`` is honoured.** When the server has said how long to wait, waiting a
different amount is either rude or useless. It is clamped at :data:`MAX_DELAY_SECONDS`
because a misconfigured gateway sending ``Retry-After: 86400`` would otherwise park a
worker for a day, and a run that never finishes is indistinguishable from one that hung.

**What is retryable is not decided here.** The caller passes a ``classify`` function, so
this module imports no HTTP library and stays a pure, table-testable loop. That is not
tidiness: the rule it would otherwise have to encode is the most dangerous one in the
module —

    A write is retried only on a failure that provably never reached the server,
    unless the operation declares itself idempotent or supplies an idempotency
    header. **A read timeout on a non-idempotent write is a permanent failure.**

Shopify's ``POST /orders.json`` has no idempotency header. Retrying a create that timed
out mid-flight silently duplicates the merchant's order, and no amount of backoff
prevents it. Only the code holding the operation spec knows whether that applies, so
that code decides — see ``runtime/sender.py`` and ``engine/idempotency.py``.
"""

import asyncio
import logging
import random
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)


# Total attempts, not retries in addition to the first. Three is the same number the
# downloader settled on, and for the same reason: the failures worth retrying are
# transient enough to clear inside a couple of seconds or not at all.
MAX_ATTEMPTS = 3

# Wait before attempt 2, doubled before attempt 3.
BASE_DELAY_SECONDS = 0.5

# The ceiling on any single wait, including one the server asked for. See the module
# docstring.
MAX_DELAY_SECONDS = 120.0

# How much of the computed delay is randomised. 0.25 means the actual wait is uniform
# across ±25% of it, which is enough to break lockstep between a handful of concurrent
# chunks without materially changing how long anything takes.
JITTER_RATIO = 0.25


@dataclass(frozen=True)
class RetryVerdict:
    """
    What ``classify`` says about one failure.

    ``retry_after`` is what the *server* asked for, in seconds, or ``None``. It
    overrides the computed backoff rather than adding to it — a server that has said
    "come back in thirty seconds" has more information than our doubling does.

    ``reason`` is a short phrase for the log and the step row. It is not shown to the
    user directly; the user gets the ``NodeFailure`` message, which is composed by
    whoever gave up.
    """

    retry: bool
    retry_after: Optional[float] = None
    reason: str = ""


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = MAX_ATTEMPTS
    base_delay: float = BASE_DELAY_SECONDS
    max_delay: float = MAX_DELAY_SECONDS
    jitter: float = JITTER_RATIO


DEFAULT_POLICY = RetryPolicy()


def backoff_delay(
    attempt: int,
    *,
    policy: RetryPolicy = DEFAULT_POLICY,
    retry_after: Optional[float] = None,
    random_fn: Callable[[], float] = random.random,
) -> float:
    """
    How long to wait before ``attempt`` (1-based; the delay before attempt 2 is the
    first one that exists).

    ``random_fn`` is injected so a test can pin the jitter rather than assert a range —
    a test that accepts anything in a window is a test that would also accept the jitter
    being removed.
    """
    if retry_after is not None and retry_after >= 0:
        base = min(float(retry_after), policy.max_delay)
    else:
        base = min(policy.base_delay * (2 ** max(0, attempt - 2)), policy.max_delay)

    if policy.jitter <= 0:
        return base

    # Uniform across ±jitter of the base, never negative.
    spread = base * policy.jitter
    return max(0.0, base - spread + (2 * spread * random_fn()))


async def run_with_retries(
    operation: Callable[[int], Awaitable[Any]],
    *,
    classify: Callable[[BaseException], RetryVerdict],
    policy: RetryPolicy = DEFAULT_POLICY,
    on_retry: Optional[Callable[[int, float, BaseException], Awaitable[None]]] = None,
    label: str = "request",
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> Any:
    """
    Call ``operation(attempt)`` until it succeeds, is classified as not worth retrying,
    or runs out of attempts.

    The last failure is re-raised **as itself**, not wrapped. The caller is a node
    runner that already knows how to turn it into a ``NodeFailure`` with the right
    sentence and the right ``retryable`` flag, and wrapping it here would put a layer of
    this module's vocabulary between the user and what actually happened.

    ``CancelledError`` is re-raised immediately and never retried or slept through.
    Retrying a cancelled operation is doing more work on the way out, which is precisely
    what cancellation is asking not to happen.
    """
    attempts = max(1, int(policy.max_attempts))
    last_error: Optional[BaseException] = None

    for attempt in range(1, attempts + 1):
        try:
            return await operation(attempt)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 — classify decides, not the type
            last_error = exc
            verdict = classify(exc)

            if not verdict.retry:
                logger.info(
                    "%s failed permanently on attempt %d/%d: %s",
                    label, attempt, attempts, verdict.reason or exc,
                )
                raise

            if attempt >= attempts:
                logger.warning(
                    "%s failed after %d attempt(s): %s", label, attempts, exc
                )
                raise

            delay = backoff_delay(
                attempt + 1, policy=policy, retry_after=verdict.retry_after
            )
            logger.info(
                "%s failed on attempt %d/%d (%s) — retrying in %.2fs",
                label, attempt, attempts, verdict.reason or exc, delay,
            )
            if on_retry is not None:
                await on_retry(attempt, delay, exc)
            await sleep(delay)

    # Unreachable: the loop either returns, raises, or exhausts and raises above. Kept
    # so the function has no path that returns None by falling off the end.
    raise last_error if last_error else RuntimeError(f"{label} did not run")
