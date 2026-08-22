"""
When to try a failed send again, and when to stop.

**The decision is made from the SMTP reply code, once, at the moment of failure**, and
recorded on the attempt row. Never re-derived later by reading ``error_message`` — the rule
``NodeFailure`` states, and the reason is the same: the string is for a person, and a
person's sentence is not a specification. ``sender`` translates whatever the driver raised
into a :class:`~app.services.email_dispatch.errors.SendError` carrying the flags this
module computed, and the worker acts on the flags alone.

**The code, not the exception class.** aiosmtplib raises a dozen types and the mapping from
type to "worth retrying" is not one-to-one — ``SMTPResponseException`` covers both a 421
that will clear in a minute and a 550 that never will. The reply code is the thing the
protocol actually defines, so that is what is classified here, and ``sender`` is the only
module that imports aiosmtplib at all.

**No jitter on the backoff, deliberately.** The usual reason for jitter is a thundering
herd: a provider comes back and two hundred messages retry in the same instant. That cannot
happen here, because ``claim_next_email`` refuses a second message for an SMTP config that
already has one in flight — the queue is serialised per server, so retries against one
provider are single-file whatever their timings say. Leaving jitter out buys a backoff that
is exactly reproducible in a test, which is worth more than randomness that would do
nothing.

**Anything not recognised is retried, not failed.** A code this module has never seen is
more likely a provider being unusual than a message being undeliverable, and the cost of
the two mistakes is not symmetric: retrying a doomed message wastes five attempts and
leaves a clear log, while failing a deliverable one loses an email somebody was waiting
for. The ceiling on being wrong is ``max_attempts``.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

#: Seconds before the first retry. Short enough that a blip is invisible to whoever is
#: waiting, long enough that a server rebooting has moved on.
BASE_DELAY_SECONDS = 30.0

#: Each attempt waits this many times longer than the last: 30s, 2m, 8m, 32m.
BACKOFF_FACTOR = 4.0

#: Longest a retry ever waits. Past this the message is stale enough that an operator
#: should be looking at it rather than the queue quietly trying again.
MAX_DELAY_SECONDS = 3600.0


# ---------------------------------------------------------------------------
# Reply-code classification
# ---------------------------------------------------------------------------

#: 5xx codes that mean *this message to this address* will never be accepted, so there is
#: no point spending the remaining attempts. Failing fast here is what stops one bad
#: address in a bound variable holding a queue slot for twenty minutes.
#:
#: 550 mailbox unavailable / rejected      554 transaction failed
#: 551 user not local                      556 domain does not accept mail
#: 553 mailbox name not allowed
PERMANENT_RECIPIENT_CODES = frozenset({550, 551, 553, 554, 556})

#: Codes that mean the *configuration* is wrong rather than the message. Permanent for the
#: same reason, but worth their own set because the operator's next action is different —
#: these are fixed on the SMTP config page, not by editing a recipient.
#:
#: 530 authentication required             535 authentication failed
#: 534 auth mechanism too weak             538 encryption required for this mechanism
PERMANENT_AUTH_CODES = frozenset({530, 534, 535, 538})

#: 4xx codes that mean "not now". Every one of these clears on its own.
#:
#: 421 service not available, closing      450 mailbox temporarily unavailable
#: 451 local error in processing           452 insufficient system storage
#: 454 temporary authentication failure    455 server unable to accommodate parameters
TRANSIENT_CODES = frozenset({421, 450, 451, 452, 454, 455})


def classify_code(code: Optional[int]) -> Tuple[bool, bool]:
    """
    ``(retryable, permanent)`` for one SMTP reply code.

    The two flags are not opposites. ``(False, False)`` is the third state and it means
    "do not retry automatically, but do not claim a later attempt is hopeless either" —
    which is where an unrecognised 5xx lands. The worker leaves those failed and an
    operator can press Retry, which is the honest answer when the protocol has not told us
    which it is.

    ``None`` — no code at all, because the connection died before the server said anything
    — is transient. A socket that dropped mid-handshake says nothing about the message.
    """
    if code is None:
        return True, False

    if code in TRANSIENT_CODES:
        return True, False
    if code in PERMANENT_RECIPIENT_CODES or code in PERMANENT_AUTH_CODES:
        return False, True

    # Whole-class fallbacks, for a provider using a code not listed above.
    if 400 <= code < 500:
        return True, False
    if 500 <= code < 600:
        # Not marked permanent: see the module docstring on asymmetric cost. It will not be
        # retried automatically, but nothing here claims a later attempt is pointless.
        return False, False

    # A 2xx or 3xx reached the failure path, which means the driver raised on something
    # other than the reply. Treat it as transient — the message itself was not refused.
    return True, False


def delay_seconds(attempt: int) -> float:
    """
    How long to wait before attempt ``attempt + 1``.

    ``attempt`` is the number that has just been made, 1-based, so the first failure waits
    ``BASE_DELAY_SECONDS``. Capped at ``MAX_DELAY_SECONDS``, and the cap is applied before
    the multiplication overflows into a number nobody meant.
    """
    if attempt < 1:
        attempt = 1
    delay = BASE_DELAY_SECONDS * (BACKOFF_FACTOR ** (attempt - 1))
    return min(delay, MAX_DELAY_SECONDS)


def next_attempt_at(attempt: int, *, moment: Optional[datetime] = None) -> datetime:
    """
    When the message becomes claimable again.

    ``moment`` is injectable so a test can assert the backoff without sleeping — the same
    clock seam ``scheduler.now()`` exposes, and for the same reason.
    """
    base = moment or datetime.now(timezone.utc)
    return base + timedelta(seconds=delay_seconds(attempt))


def should_retry(
    *,
    attempt: int,
    max_attempts: int,
    retryable: bool,
    permanent: bool,
) -> bool:
    """
    Whether the worker puts this message back on the queue.

    ``permanent`` wins over ``retryable`` if a caller ever sets both: a contradiction
    should resolve to the safer answer, and sending nothing is safer than sending a
    duplicate to an address the server has already refused.
    """
    if permanent:
        return False
    if not retryable:
        return False
    return attempt < max_attempts
