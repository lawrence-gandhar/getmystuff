"""
The per-record audit: which individual records did something other than move cleanly.

A run that writes fifty thousand records successfully writes **nothing** here. That
asymmetry is the whole design — the four counters on the run row carry the volume, and
this table carries the detail, so it stays small enough to read and cheap enough to keep
forever.

**The cap is on the rows, never on the counts.** After ``MAX_LOGGED_FAILURES`` failed and
invalid records, this module stops inserting and sets ``records_log_truncated`` on the
run; the counters keep counting, exactly. A run page that says *"50,000 records, 3,412
failed, 1,000 of them listed"* is honest about all three numbers. Capping the count
instead would have it say 1,000, with nothing on the page to suggest otherwise — which is
the failure this whole module exists to avoid, one level down.

**A failed record carries its whole payload**, because replaying the failures is what the
row is for. The payload goes through ``flow_state.redact`` on the way in: these records
came out of somebody else's API and can contain a bearer token, a card number or a
customer's address, and redacting at write time is what makes it a property of the table
rather than of whoever renders it later.

**``retryable`` is decided by the code that made the call, not here and not later.** A
``ReadTimeout`` on a non-idempotent write may well have reached the server, so re-sending
could duplicate a real order — and only the caller knows whether the operation declared
itself idempotent. Re-deriving that afterwards from a stored message is how a merchant
ends up with two of everything.

Writes are batched and swallowed for the same reason every write in ``run_store`` is: the
log is an observation of the run, not part of it.
"""

import logging
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from sqlalchemy import insert

from app.db.integrations.queries import (
    count_run_records,
    fetch_run_records,
    mark_log_truncated,
)
from app.models.integrations import (
    MAX_LOGGED_FAILURES,
    MAX_LOGGED_SAMPLES,
    RECORD_FAILED,
    RECORD_OUTCOMES,
    RECORD_SAMPLE,
    IntegrationRunRecord,
)
from app.services.integrations.engine import flow_state, run_store

logger = logging.getLogger(__name__)

#: Longest message stored per record. A vendor that returns a two-megabyte HTML error page
#: per rejected record would otherwise put a gigabyte of it in this table.
MAX_MESSAGE_CHARS = 2000

# run_id -> {"failures": n, "samples": n, "truncated": bool}. Process-local, like the
# record buffer: a run executes inside one worker task, so a counter shared across
# processes would be solving a problem that does not exist. The budget is per run and
# per process, and after a requeue the new worker starts fresh — which slightly
# over-logs a retried run and is the right way round, because the second attempt's
# failures are the interesting ones.
_budgets: Dict[int, Dict[str, Any]] = {}


def budget_for(run_id: int) -> Dict[str, Any]:
    """This run's remaining allowance. Created on first use."""
    return _budgets.setdefault(run_id, {"failures": 0, "samples": 0, "truncated": False})


def release_run(run_id: int) -> None:
    """Forget a finished run's budget. Called on every terminal path, so a worker that
    has executed ten thousand runs is not holding ten thousand dictionaries."""
    _budgets.pop(run_id, None)


def open_budgets() -> int:
    """How many runs the budget table is holding. For the autouse fixture that asserts it
    empties — a leak here is invisible until it is a memory profile."""
    return len(_budgets)


def clear_all() -> None:
    """Drop every budget. For tests, and for a worker shutting down."""
    _budgets.clear()


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def entry(
    *,
    node_id: str,
    outcome: str,
    message: str = "",
    source_key: Optional[str] = None,
    target_key: Optional[str] = None,
    payload: Any = None,
    batch_index: int = 0,
    retryable: bool = False,
    step_id: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Build one record row, refusing an outcome that is not one of the four.

    A dict rather than a model instance so a runner can assemble a batch of these without
    a session in hand, and so the refusal happens where the caller is rather than inside a
    swallowed write. An unknown outcome raises: the run page filters on this column, and a
    typo'd value is a row nobody will ever see again.
    """
    if outcome not in RECORD_OUTCOMES:
        raise ValueError(
            f"'{outcome}' is not a record outcome. The outcomes are: "
            f"{', '.join(sorted(RECORD_OUTCOMES))}."
        )

    return {
        "node_id": node_id,
        "batch_index": batch_index,
        "outcome": outcome,
        "message": _trimmed(message),
        "source_key": _key(source_key),
        "target_key": _key(target_key),
        "payload": payload,
        "retryable": bool(retryable),
        "step_id": step_id,
    }


async def write(
    run_id: int,
    entries: Sequence[Mapping[str, Any]],
    *,
    redacted_fields: Optional[Iterable[str]] = None,
) -> int:
    """
    Write a batch of record rows, up to what the run's budget allows.

    **One insert for the batch**, not one per record. A chunk of five hundred records
    where half failed would otherwise be two hundred and fifty round trips inside a node
    that has just finished waiting on somebody else's API.

    Returns how many rows were actually written, which is less than ``len(entries)`` once
    the budget runs out. The caller does not have to check: the counters are bumped
    separately and stay exact either way, which is the whole point of keeping the two
    apart.
    """
    if not entries:
        return 0

    budget = budget_for(run_id)
    allowed = [row for row in (_allow(budget, row) for row in entries) if row]

    if budget["truncated"]:
        await _mark_truncated(run_id, budget)

    if not allowed:
        return 0

    rows = [
        {
            "run_id": run_id,
            "step_id": row.get("step_id"),
            "node_id": row["node_id"],
            "batch_index": row.get("batch_index", 0),
            "outcome": row["outcome"],
            "source_key": row.get("source_key"),
            "target_key": row.get("target_key"),
            "message": row.get("message") or None,
            "payload": _payload(row.get("payload"), redacted_fields),
            "retryable": bool(row.get("retryable")),
        }
        for row in allowed
    ]

    try:
        async with run_store.open_session() as db:
            # A Core insert over a list of dicts, **not** ``add_all`` over model
            # instances. The ORM path asks for `RETURNING id, created_at` on every row so
            # it can populate the objects it just made, and that turns a five-hundred row
            # write into five hundred statements — for identifiers nothing here wants.
            # Nobody reads these rows back; they are written and forgotten until somebody
            # opens the dead-letter page.
            await db.execute(insert(IntegrationRunRecord), rows)
            await db.commit()
    except Exception:  # noqa: BLE001 — logging must not break the run
        logger.exception("Could not write %s record rows for run %s", len(rows), run_id)
        return 0

    return len(rows)


def _allow(budget: Dict[str, Any], row: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    """
    Whether this row fits in what is left, spending the budget if it does.

    Samples have their own, much smaller allowance. They are a demonstration that a dry
    run would have sent the right thing, not an audit — twenty is enough to check a
    mapping, and letting them share the failure budget would mean a dry run over fifty
    thousand records filled the whole thing with rows nobody asked for.

    Everything else — failed, invalid and skipped alike — shares one budget. A run that
    skipped forty thousand duplicates has made its point in the first thousand rows, and
    giving each outcome its own allowance would mean the interesting failures could be
    crowded out by a category that is merely numerous.
    """
    outcome = row.get("outcome")

    if outcome == RECORD_SAMPLE:
        if budget["samples"] >= MAX_LOGGED_SAMPLES:
            return None
        budget["samples"] += 1
        return row

    if budget["failures"] >= MAX_LOGGED_FAILURES:
        # Not a sample, and the failure budget is spent. The run row gets a flag so the
        # page can say the list is partial; the counters are untouched.
        budget["truncated"] = True
        return None

    budget["failures"] += 1
    return row


async def _mark_truncated(run_id: int, budget: Dict[str, Any]) -> None:
    """
    Set ``records_log_truncated`` once, not once per dropped record.

    A run that dropped forty thousand rows would otherwise issue forty thousand identical
    updates to set one boolean that is already true.
    """
    if budget.get("flag_written"):
        return
    budget["flag_written"] = True

    try:
        async with run_store.open_session() as db:
            await mark_log_truncated(db, run_id)
            await db.commit()
    except Exception:  # noqa: BLE001 — logging must not break the run
        logger.exception("Could not flag the record log as truncated for run %s", run_id)
        budget["flag_written"] = False


def _payload(value: Any, redacted_fields: Optional[Iterable[str]]) -> Optional[dict]:
    """
    The stored payload: redacted, and only ever a dict.

    A record that is not an object is wrapped rather than dropped — a JSONB column will
    not take a bare string, and losing the payload loses the ability to replay the row,
    which is the only reason it exists.
    """
    if value is None:
        return None

    redacted = flow_state.redact(value, redacted_fields)
    return redacted if isinstance(redacted, dict) else {"value": redacted}


def _trimmed(message: Any) -> str:
    text = str(message or "").strip()
    if len(text) <= MAX_MESSAGE_CHARS:
        return text
    return text[:MAX_MESSAGE_CHARS] + "… (truncated)"


def _key(value: Any) -> Optional[str]:
    """A record's identity at one end, as text and bounded.

    ``String(255)`` on the column, so a vendor id that is a whole GraphQL GID or a
    concatenated natural key is cut here rather than raising a database error in the
    middle of a swallowed write — where it would take the other four hundred rows of the
    batch down with it."""
    if value is None:
        return None
    text = str(value)
    return text[:255]


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


async def failures(
    db, run_id: int, *, retryable_only: bool = False, limit: int = 100, offset: int = 0
) -> List[IntegrationRunRecord]:  # noqa: ANN001
    """The dead-letter list. ``retryable_only`` is what the replay button selects on."""
    return await fetch_run_records(
        db,
        run_id,
        outcome=RECORD_FAILED,
        retryable_only=retryable_only,
        limit=limit,
        offset=offset,
    )


async def logged_count(db, run_id: int, outcome: Optional[str] = None) -> int:  # noqa: ANN001
    """
    How many rows the log actually holds.

    Deliberately *not* the same number as the run's counter, and the run page shows both.
    Where they differ is exactly where the log truncated, and saying so is more useful
    than either number alone.
    """
    return await count_run_records(db, run_id, outcome)


def record_view(row: IntegrationRunRecord) -> dict:
    """One record row as the dead-letter page reads it. No bigint id."""
    return {
        "uuid": str(row.uuid),
        "node_id": row.node_id,
        "batch_index": row.batch_index,
        "outcome": row.outcome,
        "source_key": row.source_key,
        "target_key": row.target_key,
        "message": row.message,
        "payload": dict(row.payload) if row.payload else None,
        "retryable": bool(row.retryable),
        "created_at": row.created_at,
    }
