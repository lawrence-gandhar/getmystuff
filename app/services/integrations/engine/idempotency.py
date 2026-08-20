"""
Doing a thing once — at the level of a run, and at the level of a single write.

Two separate problems that share a word.

**Run level.** A schedule that fires the 09:00 slot twice, or a vendor that redelivers
the same webhook, must produce one run and not two. The key is deterministic —
``{trigger_uuid}:{scheduled_for}`` for a schedule, the vendor's own event id for a
webhook — and the unique index on ``(flow_id, idempotency_key)`` is what enforces it.
**The insert is the check.** Selecting first and inserting after is racy at exactly the
moment it matters, which is two workers claiming the same tick within a millisecond of
each other. A manual run has no key at all: somebody pressing the button twice means
they want it twice.

**Write level, and this is the rule that protects a merchant's store.**

    A write is retried **only** on a failure that provably never reached the server —
    a connection refused, a connect timeout — unless the operation declares
    ``idempotent`` or supplies an idempotency header. **A read timeout on a
    non-idempotent write is a permanent failure**, and its message tells the operator
    to check the destination before running again.

The uncomfortable case is Shopify's ``POST /orders.json``, which has no idempotency
header. The request goes out, the order is created, and the response never arrives.
Every instinct says retry; retrying duplicates a real order in somebody's real business,
and no amount of backoff makes that less true. So the default is not to retry, and an
operation earns the right to be retried by saying so.

:func:`natural_key_hash` is the second layer. Before creating, look the record's natural
key up in ``integration_sync_keys``; if it is there, this is an update rather than a
create. That does not fix the duplicate the timeout above would have caused — nothing
can, in the same run — but it stops the *next* run repeating it.

Everything here is a pure function. No database, no clock, no randomness: the same
inputs give the same key, which is what makes a replay comparable to the run it repeats.
"""

import hashlib
import json
from datetime import datetime
from typing import Any, Iterable, Mapping, Optional


# ---------------------------------------------------------------------------
# Canonical form
# ---------------------------------------------------------------------------


def canonical_json(value: Any) -> str:
    """
    One JSON string per logical value, whatever order the keys arrived in.

    ``sort_keys`` because a dict built by two different code paths is the same
    operation, and a hash that disagreed about that would report a replay as a
    different run every time. Separators without spaces because whitespace is not
    meaning. ``default=str`` so a stray ``datetime`` or ``Decimal`` from a driver is
    hashed as its text rather than raising — losing a little precision in a *hash*
    beats failing the run that produced it.
    """
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )


def sha256_of(value: Any) -> str:
    """The sha256 hex of a value's canonical JSON."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def graph_hash(graph_data: Any) -> str:
    """
    The identity of a published drawing.

    Stored on ``integration_flow_versions.graph_hash``. It makes "is this the same
    workflow" answerable without comparing two documents, and it is half of the
    determinism claim — the other half is ``operation_hash`` on each step row, which is
    only possible because operations are data rather than Python.
    """
    return sha256_of(graph_data)


def operation_hash(operation: Any) -> str:
    """
    The identity of one operation as it was actually executed.

    Recorded on every step row that made a request. A replay producing a different hash
    is detectably not the same run — which is the whole reason a REST operation is a
    row with columns rather than a function in a module. A function can only record a
    module path and a commit, and a hotfix silently changes what "replay" means.
    """
    return sha256_of(operation)


# ---------------------------------------------------------------------------
# Run-level keys
# ---------------------------------------------------------------------------


def schedule_run_key(trigger_uuid: Any, scheduled_for: Optional[datetime]) -> str:
    """
    The key for one slot of one schedule.

    The slot, not the moment of firing. A run that started eleven minutes late because
    it waited in the queue is still the 09:00 run, and giving it a key derived from
    ``now()`` would make every late fire look like a new one — which defeats the whole
    mechanism precisely when the system is under load and needs it most.
    """
    slot = scheduled_for.isoformat() if scheduled_for is not None else "now"
    return f"{trigger_uuid}:{slot}"


def webhook_run_key(endpoint_uuid: Any, vendor_event_id: Optional[str]) -> Optional[str]:
    """
    The key for one delivered webhook, scoped to the endpoint that received it.

    Scoped because vendors mint event ids in their own namespaces and two of them
    colliding is not something to leave to luck.

    Returns ``None`` when the vendor sent nothing to identify the event by. That is
    deliberate: a fabricated key would deduplicate two genuinely different events, which
    is worse than processing one twice. The body-hash fallback belongs at the delivery
    table, where a duplicate is refused by a unique constraint and answered with a 200 —
    a vendor that gets a 4xx for a redelivery retries harder, and Shopify removes a
    subscription after nineteen consecutive failures.
    """
    if not vendor_event_id or not str(vendor_event_id).strip():
        return None
    return f"{endpoint_uuid}:{str(vendor_event_id).strip()}"


def manual_run_key() -> None:
    """
    Nothing.

    Named as a function rather than left as a bare ``None`` at the call site so the
    decision is visible: somebody pressing Run twice means they want it twice, and that
    is a choice rather than an omission.
    """
    return None


# ---------------------------------------------------------------------------
# Write-level safety
# ---------------------------------------------------------------------------


def write_may_be_retried(
    *,
    reached_server: bool,
    operation_is_idempotent: bool,
    has_idempotency_header: bool,
) -> bool:
    """
    Whether a failed write may be sent again. See the module docstring.

    ``reached_server`` is the one the caller has to get right, and it is the *narrow*
    reading: false only when the failure provably happened before any byte of the
    request could have been processed — connection refused, DNS failure, connect
    timeout. A read timeout means the request went out and the answer did not come
    back, so ``reached_server`` is **true** and this returns false unless the operation
    said it was safe.

    Written as three booleans rather than taking an exception, so the rule can be
    tested as a table and read without knowing which HTTP library is underneath.
    """
    if not reached_server:
        return True
    return bool(operation_is_idempotent or has_idempotency_header)


def natural_key_hash(record: Mapping[str, Any], fields: Iterable[str]) -> str:
    """
    The stable identity of one record, for ``integration_sync_keys``.

    Hashed rather than stored: a natural key is usually an email address or a customer
    name, which is somebody's personal data and does not need to be kept in order to be
    matched.

    Field order comes from ``fields`` — the mapping's declared key — not from the
    record, so two records that happen to have their keys in a different order hash the
    same. A missing field is included as ``None`` rather than skipped, because "no email
    address" and "the email field was absent" must not collapse into the same identity
    as a record that has one.
    """
    ordered = [(str(field), record.get(field)) for field in fields]
    if not ordered:
        raise ValueError(
            "A natural key needs at least one field. Without one there is nothing to "
            "match an existing record against, and every record would be created again "
            "on the next run."
        )
    return sha256_of(ordered)
