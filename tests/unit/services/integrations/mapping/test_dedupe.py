"""
Tests for ``mapping/dedupe.py`` and the sync-key queries under it.

This is the second layer of write safety, and the thing it protects against is a real
order duplicated in a real business. Four properties matter.

**A remembered key turns a create into an update.** That is the whole feature.

**Two records with the same key inside one batch.** The second cannot be an update —
the first has not been written yet and there is no id — and it must not be a create. It
is skipped and *recorded* as skipped, so the run ends ``partial`` and somebody sees it.

**One query per batch, not one per record.** Asserted by counting statements, because a
per-record implementation passes every behavioural test and then makes a five-hundred
record batch five hundred round trips.

**A key is remembered only for a write that succeeded.** A key stored for a create that
failed turns the next run's create into an update against an id that does not exist,
which fails every time and looks like a permissions problem.

The upsert runs against SQLite here and Postgres in production, which is why
``remember_sync_keys`` picks its dialect rather than assuming one — a dedupe path
exercised only in production is not one anybody should trust.
"""

from __future__ import annotations

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.integrations import queries
from app.models.integrations import AUTH_API_KEY, IntegrationConnection, IntegrationSyncKey
from app.models.user.user import User
from app.services.integrations.mapping import dedupe
from app.services.integrations.mapping.dedupe import CREATE, DUPLICATE, UPDATE, NaturalKey

OPERATION = "create_contact"
EMAIL_KEY = NaturalKey(fields=("email",)).validated()


@pytest.fixture
async def connection(db: AsyncSession, user: User) -> IntegrationConnection:
    row = IntegrationConnection(
        user_id=user.id,
        connector_id="rest_generic",
        label="CRM",
        auth_kind=AUTH_API_KEY,
        base_url="https://api.example.com",
    )
    db.add(row)
    await db.commit()
    return row


@pytest.fixture
def count_statements(db_engine):  # noqa: ANN001, ANN201
    """
    Counts SELECTs against ``integration_sync_keys``.

    A behavioural assertion cannot tell one query from five hundred, and the difference
    between them is the entire reason the unit of a loop pass is a batch.
    """
    seen: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001, ANN202
        if "integration_sync_keys" in statement.lower():
            seen.append(statement)

    event.listen(db_engine.sync_engine, "before_cursor_execute", record)
    yield seen
    event.remove(db_engine.sync_engine, "before_cursor_execute", record)


async def sync_key_count(db: AsyncSession) -> int:
    result = await db.execute(select(func.count()).select_from(IntegrationSyncKey))
    return int(result.scalar_one())


class TestTheNaturalKey:
    def test_a_single_field_may_be_written_as_a_string(self) -> None:
        assert dedupe.load_natural_key("email").fields == ("email",)

    def test_a_list_becomes_a_tuple(self) -> None:
        assert dedupe.load_natural_key(["sku", "warehouse"]).fields == ("sku", "warehouse")

    def test_nothing_means_no_dedupe(self) -> None:
        """Legitimate: an operation that already takes an idempotency key, or one that
        appends to a log, has nothing to match against."""
        assert dedupe.load_natural_key(None).enabled is False
        assert dedupe.load_natural_key("").enabled is False

    def test_a_repeated_field_is_refused(self) -> None:
        with pytest.raises(ValueError, match="same field twice"):
            NaturalKey(fields=("email", "email")).validated()

    def test_something_that_is_not_a_field_list_is_refused(self) -> None:
        with pytest.raises(ValueError, match="field name"):
            dedupe.load_natural_key({"field": "email"})

    def test_field_order_comes_from_the_key_not_the_record(self) -> None:
        """Two records with their keys in a different order are the same record, and a
        hash that disagreed would create a duplicate on every run."""
        key = NaturalKey(fields=("sku", "warehouse")).validated()
        assert key.hash_for({"sku": "A", "warehouse": "W"}) == key.hash_for(
            {"warehouse": "W", "sku": "A"}
        )

    def test_a_missing_field_is_not_the_same_as_a_present_one(self) -> None:
        assert EMAIL_KEY.hash_for({}) != EMAIL_KEY.hash_for({"email": "a@b.com"})


class TestPlanning:
    async def test_an_unseen_record_is_a_create(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        decisions = await dedupe.plan_writes(
            db,
            connection_id=connection.id,
            operation_id=OPERATION,
            records=[{"email": "a@b.com"}],
            key=EMAIL_KEY,
        )

        assert [decision.action for decision in decisions] == [CREATE]
        assert decisions[0].target_record_id == ""

    async def test_a_remembered_record_becomes_an_update(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        """The feature. Shopify's ``POST /orders.json`` has no idempotency header, so
        this is what stops the *next* run repeating a create."""
        record = {"email": "a@b.com"}
        await dedupe.remember(
            db,
            connection_id=connection.id,
            operation_id=OPERATION,
            written=[(EMAIL_KEY.hash_for(record), "cust_991")],
        )
        await db.commit()

        decisions = await dedupe.plan_writes(
            db,
            connection_id=connection.id,
            operation_id=OPERATION,
            records=[record],
            key=EMAIL_KEY,
        )

        assert decisions[0].action == UPDATE
        assert decisions[0].target_record_id == "cust_991"

    async def test_a_key_remembered_for_another_operation_does_not_apply(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        """A contact created by one operation is not an order created by another, even
        with the same email on it."""
        record = {"email": "a@b.com"}
        await dedupe.remember(
            db,
            connection_id=connection.id,
            operation_id="create_order",
            written=[(EMAIL_KEY.hash_for(record), "ord_1")],
        )
        await db.commit()

        decisions = await dedupe.plan_writes(
            db,
            connection_id=connection.id,
            operation_id=OPERATION,
            records=[record],
            key=EMAIL_KEY,
        )
        assert decisions[0].action == CREATE

    async def test_a_second_copy_in_the_same_batch_is_skipped(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        """
        Not an update — the first has not been written and there is no id to update. Not
        a create — that is the duplicate this module exists to prevent.
        """
        decisions = await dedupe.plan_writes(
            db,
            connection_id=connection.id,
            operation_id=OPERATION,
            records=[
                {"email": "a@b.com", "name": "Ada"},
                {"email": "c@d.com"},
                {"email": "a@b.com", "name": "Ada Lovelace"},
            ],
            key=EMAIL_KEY,
        )

        assert [decision.action for decision in decisions] == [CREATE, CREATE, DUPLICATE]
        assert decisions[2].writes is False

    async def test_the_duplicate_message_names_the_key_fields(self) -> None:
        """Two customers with one email address is either a fact about the source data or
        a broken key, and the operator cannot tell which without knowing what the key
        was."""
        decision = dedupe.WriteDecision(position=2, record={}, action=DUPLICATE)
        assert "email" in dedupe.duplicate_message(decision, EMAIL_KEY)

    async def test_no_key_means_every_record_is_a_create(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        decisions = await dedupe.plan_writes(
            db,
            connection_id=connection.id,
            operation_id=OPERATION,
            records=[{"email": "a@b.com"}, {"email": "a@b.com"}],
            key=NaturalKey(),
        )
        assert [decision.action for decision in decisions] == [CREATE, CREATE]

    async def test_order_and_position_survive(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        """The record log stores a batch index; the position is the other half of the
        coordinate. Without it, "record 3 failed" in a run of a hundred batches names a
        hundred records."""
        records = [{"email": f"{index}@x.com"} for index in range(5)]
        decisions = await dedupe.plan_writes(
            db,
            connection_id=connection.id,
            operation_id=OPERATION,
            records=records,
            key=EMAIL_KEY,
        )
        assert [decision.position for decision in decisions] == [0, 1, 2, 3, 4]

    async def test_one_query_for_the_whole_batch(
        self,
        db: AsyncSession,
        connection: IntegrationConnection,
        count_statements: list,
    ) -> None:
        """
        The assertion a behavioural test cannot make. A per-record lookup would satisfy
        every other test in this file and turn a five-hundred record batch into five
        hundred round trips inside a node that is already waiting on somebody else's API.
        """
        records = [{"email": f"{index}@x.com"} for index in range(120)]

        await dedupe.plan_writes(
            db,
            connection_id=connection.id,
            operation_id=OPERATION,
            records=records,
            key=EMAIL_KEY,
        )

        assert len(count_statements) == 1, (
            f"expected one lookup for the batch, saw {len(count_statements)}"
        )

    async def test_nothing_is_written_by_planning(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        """The plan is computed and the caller executes it. A key stored before the write
        would claim a record exists that a failure is about to prevent."""
        await dedupe.plan_writes(
            db,
            connection_id=connection.id,
            operation_id=OPERATION,
            records=[{"email": "a@b.com"}],
            key=EMAIL_KEY,
        )
        assert await sync_key_count(db) == 0


class TestRemembering:
    async def test_a_second_write_of_the_same_key_updates_rather_than_duplicating(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        """
        The upsert. Select-then-insert has a window exactly where it matters — two chunks
        of the same write node run concurrently by design — and the unique constraint is
        the arbiter.
        """
        digest = EMAIL_KEY.hash_for({"email": "a@b.com"})

        await dedupe.remember(
            db, connection_id=connection.id, operation_id=OPERATION,
            written=[(digest, "cust_1")],
        )
        await db.commit()
        await dedupe.remember(
            db, connection_id=connection.id, operation_id=OPERATION,
            written=[(digest, "cust_2")],
        )
        await db.commit()

        assert await sync_key_count(db) == 1
        found = await queries.find_sync_keys(db, connection.id, OPERATION, [digest])
        assert found[digest] == "cust_2"

    async def test_a_duplicated_key_within_one_call_does_not_take_the_batch_down(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        """Postgres refuses the whole statement when ``ON CONFLICT DO UPDATE`` would hit
        one row twice. Losing five hundred sync keys to one repeated pair is not a
        trade worth making."""
        digest = EMAIL_KEY.hash_for({"email": "a@b.com"})

        written = await dedupe.remember(
            db, connection_id=connection.id, operation_id=OPERATION,
            written=[(digest, "cust_1"), (digest, "cust_2")],
        )
        await db.commit()

        assert written == 1
        assert await sync_key_count(db) == 1

    async def test_nothing_to_remember_is_not_a_statement(
        self, db: AsyncSession, connection: IntegrationConnection, count_statements: list
    ) -> None:
        assert await dedupe.remember(
            db, connection_id=connection.id, operation_id=OPERATION, written=[]
        ) == 0
        assert count_statements == []

    async def test_a_pair_with_no_target_id_is_dropped(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        """A destination that returned nothing to identify the record by has not given us
        anything to update later, and a sync key pointing at ``None`` is worse than
        none."""
        digest = EMAIL_KEY.hash_for({"email": "a@b.com"})
        assert await dedupe.remember(
            db, connection_id=connection.id, operation_id=OPERATION,
            written=[(digest, None)],
        ) == 0

    async def test_remembering_does_not_commit(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        """The sync keys and the counters for a chunk belong in one transaction. A key
        remembered for a write that was rolled back would suppress a create that never
        happened."""
        digest = EMAIL_KEY.hash_for({"email": "a@b.com"})
        await dedupe.remember(
            db, connection_id=connection.id, operation_id=OPERATION,
            written=[(digest, "cust_1")],
        )
        await db.rollback()

        assert await sync_key_count(db) == 0


class TestForgetting:
    async def test_the_operator_can_clear_a_connection(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        """
        The escape hatch for the limitation this module states plainly: it never infers a
        deletion from a 404, because a 404 during a sync is far more often a permissions
        change than a deleted record, and guessing wrong re-creates everything.
        """
        await dedupe.remember(
            db, connection_id=connection.id, operation_id=OPERATION,
            written=[(EMAIL_KEY.hash_for({"email": "a@b.com"}), "cust_1")],
        )
        await queries.remember_sync_keys(
            db, connection.id, "create_order",
            [(EMAIL_KEY.hash_for({"email": "c@d.com"}), "ord_1")],
        )
        await db.commit()

        await queries.forget_sync_keys(db, connection.id, operation_id=OPERATION)
        await db.commit()

        assert await sync_key_count(db) == 1, "only the named operation is cleared"

    async def test_the_whole_connection_can_be_cleared(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> None:
        await dedupe.remember(
            db, connection_id=connection.id, operation_id=OPERATION,
            written=[(EMAIL_KEY.hash_for({"email": "a@b.com"}), "cust_1")],
        )
        await db.commit()

        await queries.forget_sync_keys(db, connection.id)
        await db.commit()

        assert await sync_key_count(db) == 0


class TestCounts:
    def test_the_tally_is_a_delta_not_a_total(self) -> None:
        """``flow_state._accumulate`` sums these across every batch pass. A runner
        returning a running total would make a fifty-thousand record run report the size
        of its last batch."""
        decisions = [
            dedupe.WriteDecision(position=0, record={}, action=CREATE),
            dedupe.WriteDecision(position=1, record={}, action=UPDATE),
            dedupe.WriteDecision(position=2, record={}, action=DUPLICATE),
            dedupe.WriteDecision(position=3, record={}, action=CREATE),
        ]
        assert dedupe.counts_of(decisions) == {CREATE: 2, UPDATE: 1, DUPLICATE: 1}

    def test_an_empty_batch_reports_zeroes_rather_than_nothing(self) -> None:
        assert dedupe.counts_of([]) == {CREATE: 0, UPDATE: 0, DUPLICATE: 0}
