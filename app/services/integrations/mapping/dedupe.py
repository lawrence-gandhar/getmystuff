"""
Deciding whether a record is a create or an update, before anything is sent.

This is the **second** layer of write safety. The first is the rule in
``engine/idempotency.py``: a write is retried only when the failure provably never
reached the server. That rule stops one request becoming two. It cannot help with the
case it exists because of — Shopify's ``POST /orders.json`` has no idempotency header, so
a create whose response never arrived may or may not have created an order, and the
honest answer is to fail it permanently and tell the operator to look.

This layer is what stops the *next* run repeating it. Before creating, look the record's
natural key up in ``integration_sync_keys``; if it is there, this record has been written
before and this is an update.

**The natural key is over the mapped record, not the source record.** By the time a write
node runs, the fields have been read, transformed and coerced into the destination's
vocabulary, and that is the form both runs of a flow agree on. Keying off the source
would mean a mapping change silently re-creating everything, because the hash of a record
that gained a field is a different hash.

**Three decisions, and the third is the one people do not expect.**

``create``
    Nothing has been written for this key.

``update``
    A previous run wrote it, and the sync key remembers what the destination called it.

``duplicate``
    Two records **in the same batch** carry the same natural key. The first is written;
    the second cannot be an update because the first has not been written yet and there
    is no id to update, and it cannot be a create because that is the duplicate the whole
    module exists to prevent. So it is skipped, and it is *recorded* as skipped — which
    matters, because a run with any skipped record ends ``partial`` rather than
    ``success``. Two customers with one email address is either a real fact about the
    source data or a broken key, and both are things the operator has to see.

**What this deliberately does not do.** It never infers a deletion. A 404 from the
destination for a remembered id leaves the sync key alone, because a 404 during a sync is
far more often a permissions change or a rate-limited gateway than a deleted record, and
guessing wrong re-creates everything. Clearing keys is an explicit operator action —
``queries.forget_sync_keys``.
"""

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.integrations import queries
from app.services.integrations.engine.idempotency import natural_key_hash

#: The three decisions. Strings rather than an enum because they are written straight
#: onto ``integration_run_records.outcome`` and read back by a template.
CREATE = "create"
UPDATE = "update"
DUPLICATE = "duplicate"


@dataclass(frozen=True)
class NaturalKey:
    """
    The fields that say two records are the same record.

    ``("email",)`` for a contact, ``("order_number",)`` for an order, ``("sku",
    "warehouse")`` for a stock level. Field *names* in the destination's vocabulary —
    see the module docstring.

    Empty means no dedupe, which is legitimate: an operation that already takes an
    idempotency key, or one that appends to a log, has nothing to match against. It is
    the caller's decision and not a default this module supplies, because a natural key
    guessed wrong is worse than none at all.
    """

    fields: Tuple[str, ...] = ()

    @property
    def enabled(self) -> bool:
        return bool(self.fields)

    def validated(self) -> "NaturalKey":
        cleaned = tuple(
            str(name).strip() for name in self.fields if str(name).strip()
        )
        if len(set(cleaned)) != len(cleaned):
            raise ValueError(
                "A natural key names the same field twice. Each field counts once "
                "towards a record's identity."
            )
        return NaturalKey(fields=cleaned)

    def hash_for(self, record: Mapping[str, Any]) -> str:
        """
        This record's identity, or ``""`` when there is no key.

        Delegates to ``idempotency.natural_key_hash``, which orders by the *declared*
        field list rather than by the record, so two records with their keys in different
        orders hash the same — and which includes a missing field as ``None`` rather than
        skipping it, so "no email" and "no email field" do not collapse into the identity
        of a record that has one.
        """
        if not self.enabled:
            return ""
        return natural_key_hash(record, self.fields)


def load_natural_key(raw: Any) -> NaturalKey:
    """
    A natural key out of a node's JSON. A bare string is one field.

    ``{"natural_key": "email"}`` is what a single-field key looks like in a form, and
    insisting on ``["email"]`` would be pedantry enforced with a refusal.
    """
    if raw is None or raw == "":
        return NaturalKey()
    if isinstance(raw, str):
        return NaturalKey(fields=(raw,)).validated()
    if isinstance(raw, (list, tuple)):
        return NaturalKey(fields=tuple(str(name) for name in raw)).validated()
    raise ValueError(
        "A natural key has to be a field name or a list of field names — it says which "
        "fields make two records the same record."
    )


@dataclass(frozen=True)
class WriteDecision:
    """
    What to do with one record, and enough context to log it.

    ``position`` is the record's index within its batch. Kept because
    ``integration_run_records`` stores a batch index and this is the other half of the
    coordinate — without it, "record 3 failed" in a run of a hundred batches names a
    hundred records.
    """

    position: int
    record: Mapping[str, Any]
    action: str
    natural_key: str = ""
    target_record_id: str = ""

    @property
    def writes(self) -> bool:
        return self.action in (CREATE, UPDATE)


async def plan_writes(
    db: AsyncSession,
    *,
    connection_id: int,
    operation_id: str,
    records: Sequence[Mapping[str, Any]],
    key: NaturalKey,
) -> List[WriteDecision]:
    """
    One decision per record, in order, using **one** database query for the batch.

    Order is preserved and is load-bearing twice over: the record log's positions have to
    match, and the in-batch duplicate rule needs a first record to point at. A set-based
    implementation would be shorter and would make "which one won" depend on iteration
    order.

    With no natural key every record is a ``create``, and that is not a silent fallback —
    the caller chose it by not configuring one, and the sentence in :class:`NaturalKey`
    explains when that is the right choice.

    Nothing here writes. The plan is computed, the caller executes it and then calls
    :func:`remember` with what actually succeeded, so a create that failed does not leave
    a sync key claiming it exists.
    """
    if not key.enabled:
        return [
            WriteDecision(position=position, record=record, action=CREATE)
            for position, record in enumerate(records)
        ]

    hashes = [key.hash_for(record) for record in records]
    known = await queries.find_sync_keys(db, connection_id, operation_id, hashes)

    decisions: List[WriteDecision] = []
    seen_in_batch: Dict[str, int] = {}

    for position, (record, natural_key) in enumerate(zip(records, hashes)):
        if natural_key in seen_in_batch:
            decisions.append(
                WriteDecision(
                    position=position,
                    record=record,
                    action=DUPLICATE,
                    natural_key=natural_key,
                )
            )
            continue

        seen_in_batch[natural_key] = position
        target_record_id = known.get(natural_key, "")
        decisions.append(
            WriteDecision(
                position=position,
                record=record,
                action=UPDATE if target_record_id else CREATE,
                natural_key=natural_key,
                target_record_id=target_record_id,
            )
        )

    return decisions


async def remember(
    db: AsyncSession,
    *,
    connection_id: int,
    operation_id: str,
    written: Iterable[Tuple[str, str]],
) -> int:
    """
    Record what each natural key became, after the writes succeeded.

    Called with ``(natural_key, target_record_id)`` pairs for the records the destination
    actually accepted — never for the ones that failed. A key remembered for a write that
    did not happen turns the next run's create into an update against an id that does not
    exist, which fails every time and looks like a permissions problem.

    Does not commit. The sync keys and the counters for a chunk belong in one
    transaction; the caller owns the boundary.
    """
    return await queries.remember_sync_keys(db, connection_id, operation_id, written)


def duplicate_message(decision: WriteDecision, key: NaturalKey) -> str:
    """The sentence a ``duplicate`` writes into its record row, naming the key fields so
    the operator can tell a broken key from genuinely duplicated source data."""
    fields = ", ".join(key.fields) or "the natural key"
    return (
        f"Another record earlier in this batch has the same {fields}, so this one was "
        "skipped rather than written twice."
    )


def counts_of(decisions: Iterable[WriteDecision]) -> Dict[str, int]:
    """
    The tally, as the delta ``run_node`` merges into ``state["counts"]``.

    Deltas, never totals — ``flow_state._accumulate`` sums these across every batch pass,
    and a runner returning a running total would make a fifty-thousand record run report
    the size of its last batch.
    """
    tally = {CREATE: 0, UPDATE: 0, DUPLICATE: 0}
    for decision in decisions:
        tally[decision.action] = tally.get(decision.action, 0) + 1
    return tally
