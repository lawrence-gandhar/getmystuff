"""
Tests for ``engine/record_log.py``.

The headline property is the one the module exists for: **the cap is on the rows, never
on the counts.** A run that failed forty thousand records logs a thousand of them, sets
``records_log_truncated``, and leaves ``records_failed`` reading forty thousand. Capping
the count instead would have the page say a thousand with nothing to suggest otherwise,
which is the same silent-partial-result failure the ``partial`` run status exists to
prevent, one level down.

The rest:

**A clean run writes nothing here.** The asymmetry is the design — the counters carry the
volume so this table stays small enough to read and cheap enough to keep.

**A failed record carries its whole redacted payload**, because replaying the failures is
what the row is for, and because these records came out of somebody else's API and can
contain a bearer token.

**One insert per batch**, asserted by counting statements — a per-record implementation
passes every behavioural test and turns a chunk of five hundred into two hundred and
fifty round trips.

**Writing never fails the node**, and the truncation flag is written once rather than
once per dropped record.
"""

from __future__ import annotations

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.integrations import (
    MAX_LOGGED_FAILURES,
    MAX_LOGGED_SAMPLES,
    RECORD_FAILED,
    RECORD_INVALID,
    RECORD_SAMPLE,
    RECORD_SKIPPED,
    IntegrationFlow,
    IntegrationRun,
    IntegrationRunRecord,
)
from app.models.user.user import User
from app.services.integrations.engine import record_log, run_store


@pytest.fixture
async def flow(db: AsyncSession, user: User) -> IntegrationFlow:
    row = IntegrationFlow(user_id=user.id, name="Contact sync")
    db.add(row)
    await db.commit()
    return row


@pytest.fixture
async def run(db: AsyncSession, flow: IntegrationFlow) -> IntegrationRun:
    created = await run_store.create_run(
        db, flow_id=flow.id, flow_version_id=None, thread_id="thread-1"
    )
    await db.commit()
    yield created
    record_log.release_run(created.id)


@pytest.fixture
def count_inserts(db_engine):  # noqa: ANN001, ANN201
    """Counts INSERTs into ``integration_run_records``. A behavioural assertion cannot
    tell one statement from five hundred."""
    seen: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001, ANN202
        if "insert into integration_run_records" in statement.lower():
            seen.append(statement)

    event.listen(db_engine.sync_engine, "before_cursor_execute", record)
    yield seen
    event.remove(db_engine.sync_engine, "before_cursor_execute", record)


async def row_count(db: AsyncSession, run_id: int, outcome: str = "") -> int:
    statement = (
        select(func.count())
        .select_from(IntegrationRunRecord)
        .where(IntegrationRunRecord.run_id == run_id)
    )
    if outcome:
        statement = statement.where(IntegrationRunRecord.outcome == outcome)
    result = await db.execute(statement)
    return int(result.scalar_one())


def failure(index: int, **overrides) -> dict:
    defaults = dict(
        node_id="write",
        outcome=RECORD_FAILED,
        message=f"the destination refused record {index}",
        source_key=f"src-{index}",
        payload={"email": f"{index}@example.com"},
        retryable=False,
    )
    defaults.update(overrides)
    return record_log.entry(**defaults)


class TestWritingNothing:
    async def test_a_clean_run_writes_no_rows(
        self, db: AsyncSession, run: IntegrationRun
    ) -> None:
        """The asymmetry is the design: fifty thousand records moved cleanly puts fifty
        thousand in the counters and nothing here."""
        assert await record_log.write(run.id, []) == 0
        assert await row_count(db, run.id) == 0

    async def test_an_unknown_outcome_is_refused_where_the_caller_is(self) -> None:
        """Raised at ``entry`` rather than inside the swallowed write: the page filters
        on this column, and a typo'd value is a row nobody will ever see again."""
        with pytest.raises(ValueError, match="not a record outcome"):
            record_log.entry(node_id="write", outcome="borked")


class TestTheCapIsOnRowsNotCounts:
    async def test_the_log_stops_and_the_run_says_so(
        self, db: AsyncSession, run: IntegrationRun
    ) -> None:
        """
        The headline. The rows stop at the cap, the flag goes up, and — asserted
        separately below — the counters are untouched by any of it.
        """
        written = await record_log.write(
            run.id, [failure(index) for index in range(MAX_LOGGED_FAILURES + 500)]
        )

        assert written == MAX_LOGGED_FAILURES
        assert await row_count(db, run.id) == MAX_LOGGED_FAILURES

        await db.refresh(run)
        assert run.records_log_truncated is True

    async def test_the_counters_stay_exact_when_the_log_truncates(
        self, db: AsyncSession, run: IntegrationRun
    ) -> None:
        """
        A page that says "40,000 failed, 1,000 of them listed" is honest about both
        numbers. One that capped the count would say 1,000, with nothing on it to suggest
        otherwise.
        """
        await record_log.write(
            run.id, [failure(index) for index in range(MAX_LOGGED_FAILURES + 39_000)]
        )
        await run_store.bump_counts(run.id, records_failed=MAX_LOGGED_FAILURES + 39_000)

        await db.refresh(run)
        assert run.records_failed == 40_000
        assert await row_count(db, run.id) == MAX_LOGGED_FAILURES

    async def test_samples_have_their_own_much_smaller_allowance(
        self, db: AsyncSession, run: IntegrationRun
    ) -> None:
        """A sample is a demonstration, not an audit. Sharing the failure budget would
        mean a dry run over fifty thousand records filled the whole thing."""
        entries = [
            record_log.entry(node_id="write", outcome=RECORD_SAMPLE, payload={"n": index})
            for index in range(MAX_LOGGED_SAMPLES + 30)
        ]
        assert await record_log.write(run.id, entries) == MAX_LOGGED_SAMPLES
        assert await row_count(db, run.id, RECORD_SAMPLE) == MAX_LOGGED_SAMPLES

    async def test_spending_the_sample_budget_leaves_the_failure_budget_alone(
        self, db: AsyncSession, run: IntegrationRun
    ) -> None:
        await record_log.write(
            run.id,
            [
                record_log.entry(node_id="w", outcome=RECORD_SAMPLE)
                for _ in range(MAX_LOGGED_SAMPLES + 5)
            ],
        )
        assert await record_log.write(run.id, [failure(1)]) == 1

    async def test_failed_invalid_and_skipped_share_one_budget(
        self, db: AsyncSession, run: IntegrationRun
    ) -> None:
        """
        A run that skipped forty thousand duplicates has made its point in the first
        thousand rows. Giving each outcome its own allowance would let a category that is
        merely numerous crowd out the interesting failures.
        """
        entries = (
            [failure(index) for index in range(400)]
            + [
                record_log.entry(node_id="v", outcome=RECORD_INVALID, message="bad email")
                for _ in range(400)
            ]
            + [
                record_log.entry(node_id="w", outcome=RECORD_SKIPPED, message="duplicate")
                for _ in range(400)
            ]
        )

        assert await record_log.write(run.id, entries) == MAX_LOGGED_FAILURES

    async def test_the_truncation_flag_is_written_once(
        self, db: AsyncSession, run: IntegrationRun, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A run that dropped forty thousand rows would otherwise issue forty thousand
        identical updates to set one boolean that is already true."""
        updates: list[int] = []
        original = record_log.mark_log_truncated

        async def counted(db_session, run_id):  # noqa: ANN001, ANN202
            updates.append(run_id)
            return await original(db_session, run_id)

        monkeypatch.setattr(record_log, "mark_log_truncated", counted)

        for _ in range(3):
            await record_log.write(
                run.id, [failure(index) for index in range(MAX_LOGGED_FAILURES)]
            )

        assert len(updates) == 1


class TestWhatIsStored:
    async def test_a_failed_record_keeps_its_whole_payload(
        self, db: AsyncSession, run: IntegrationRun
    ) -> None:
        """Replaying the failures is what the row is for."""
        await record_log.write(
            run.id,
            [failure(1, payload={"email": "a@b.com", "name": "Ada", "qty": 3})],
        )

        rows = await record_log.failures(db, run.id)
        assert rows[0].payload == {"email": "a@b.com", "name": "Ada", "qty": 3}

    async def test_the_payload_is_redacted_on_the_way_in(
        self, db: AsyncSession, run: IntegrationRun
    ) -> None:
        """
        These records came out of somebody else's API and can carry a bearer token.
        Redacting at write time makes it a property of the table rather than of whoever
        renders it later.
        """
        await record_log.write(
            run.id,
            [failure(1, payload={"email": "a@b.com", "authorization": "Bearer sk-live-1"})],
        )

        rows = await record_log.failures(db, run.id)
        assert rows[0].payload["email"] == "a@b.com"
        assert "sk-live-1" not in str(rows[0].payload)

    async def test_a_flows_own_sensitive_fields_are_redacted_too(
        self, db: AsyncSession, run: IntegrationRun
    ) -> None:
        """No general pattern can guess that ``national_id`` is sensitive in this
        customer's data. The flow's author can say so."""
        await record_log.write(
            run.id,
            [failure(1, payload={"national_id": "QQ123456C"})],
            redacted_fields=["national_id"],
        )

        rows = await record_log.failures(db, run.id)
        assert "QQ123456C" not in str(rows[0].payload)

    async def test_a_payload_that_is_not_an_object_is_wrapped(
        self, db: AsyncSession, run: IntegrationRun
    ) -> None:
        """A JSONB column will not take a bare string, and dropping it loses the ability
        to replay the row — which is the only reason it exists."""
        await record_log.write(run.id, [failure(1, payload=["a", "b"])])

        rows = await record_log.failures(db, run.id)
        assert rows[0].payload == {"value": ["a", "b"]}

    async def test_a_two_megabyte_vendor_error_is_trimmed(
        self, db: AsyncSession, run: IntegrationRun
    ) -> None:
        """A WAF that returns an HTML page per rejected record would otherwise put a
        gigabyte of it in this table."""
        await record_log.write(run.id, [failure(1, message="x" * 50_000)])

        rows = await record_log.failures(db, run.id)
        assert len(rows[0].message) <= record_log.MAX_MESSAGE_CHARS + 20
        assert rows[0].message.endswith("(truncated)")

    async def test_an_overlong_key_is_cut_rather_than_raising(
        self, db: AsyncSession, run: IntegrationRun
    ) -> None:
        """The column is ``String(255)``. Raising here would take the other four hundred
        rows of the batch down inside a swallowed write."""
        await record_log.write(run.id, [failure(1, source_key="k" * 400)])

        rows = await record_log.failures(db, run.id)
        assert len(rows[0].source_key) == 255

    async def test_retryable_is_recorded_as_the_caller_decided_it(
        self, db: AsyncSession, run: IntegrationRun
    ) -> None:
        """
        Decided by the code that made the call, never re-derived later from a stored
        message. A read timeout on a non-idempotent write may well have reached the
        server, and only the caller knows whether the operation said it was safe.
        """
        await record_log.write(
            run.id,
            [failure(1, retryable=False), failure(2, retryable=True)],
        )

        assert len(await record_log.failures(db, run.id, retryable_only=True)) == 1
        assert len(await record_log.failures(db, run.id)) == 2

    async def test_no_bigint_id_reaches_the_browser(
        self, db: AsyncSession, run: IntegrationRun
    ) -> None:
        await record_log.write(run.id, [failure(1)])
        view = record_log.record_view((await record_log.failures(db, run.id))[0])

        assert "id" not in view
        assert view["uuid"]


class TestOneInsertPerBatch:
    async def test_five_hundred_records_are_one_statement(
        self, run: IntegrationRun, count_inserts: list
    ) -> None:
        """
        A per-record implementation passes every behavioural test in this file and turns
        a chunk of five hundred into five hundred round trips inside a node that has just
        finished waiting on somebody else's API.
        """
        await record_log.write(run.id, [failure(index) for index in range(500)])

        assert len(count_inserts) == 1, (
            f"expected one insert for the batch, saw {len(count_inserts)}"
        )


class TestWritingNeverFailsTheNode:
    async def test_a_broken_session_returns_zero_rather_than_raising(
        self, monkeypatch: pytest.MonkeyPatch, run: IntegrationRun
    ) -> None:
        def exploding():  # noqa: ANN202
            raise RuntimeError("the database is not there")

        monkeypatch.setattr(run_store, "open_session", exploding)
        assert await record_log.write(run.id, [failure(1)]) == 0


class TestBudgets:
    async def test_a_budget_is_created_and_released(self, run: IntegrationRun) -> None:
        """The autouse fixture asserts this globally; here it is asserted directly, so a
        failure names the mechanism rather than the next test to run."""
        await record_log.write(run.id, [failure(1)])
        assert record_log.open_budgets() == 1

        record_log.release_run(run.id)
        assert record_log.open_budgets() == 0

    async def test_two_runs_do_not_share_an_allowance(
        self, db: AsyncSession, flow: IntegrationFlow, run: IntegrationRun
    ) -> None:
        other = await run_store.create_run(
            db, flow_id=flow.id, flow_version_id=None, thread_id="thread-2"
        )
        await db.commit()

        try:
            await record_log.write(
                run.id, [failure(index) for index in range(MAX_LOGGED_FAILURES)]
            )
            assert await record_log.write(other.id, [failure(1)]) == 1
        finally:
            record_log.release_run(other.id)


class TestReading:
    async def test_the_logged_count_is_not_the_run_counter(
        self, db: AsyncSession, run: IntegrationRun
    ) -> None:
        """
        Deliberately two numbers. Where they differ is exactly where the log truncated,
        and saying so is more useful than either alone.
        """
        await record_log.write(run.id, [failure(index) for index in range(3)])
        await run_store.bump_counts(run.id, records_failed=900)
        await db.refresh(run)

        assert await record_log.logged_count(db, run.id, RECORD_FAILED) == 3
        assert run.records_failed == 900

    async def test_the_dead_letter_list_paginates(
        self, db: AsyncSession, run: IntegrationRun
    ) -> None:
        await record_log.write(run.id, [failure(index) for index in range(25)])

        first = await record_log.failures(db, run.id, limit=10)
        second = await record_log.failures(db, run.id, limit=10, offset=10)

        assert [row.source_key for row in first][0] == "src-0"
        assert [row.source_key for row in second][0] == "src-10"

    async def test_outcomes_are_counted_in_one_grouped_statement(
        self, db: AsyncSession, run: IntegrationRun
    ) -> None:
        await record_log.write(
            run.id,
            [failure(1), failure(2)]
            + [record_log.entry(node_id="v", outcome=RECORD_INVALID)],
        )

        assert await run_store.record_outcome_counts(db, run.id) == {
            RECORD_FAILED: 2,
            RECORD_INVALID: 1,
        }
