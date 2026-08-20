"""
Tests for ``app/models/integrations/models.py``.

Two kinds of assertion, and both exist because of a specific way this could go wrong
quietly.

**The constraints.** Every uniqueness rule here is tested *against the database*, not
read off the model. Three of them are partial or functional indexes, and a partial index
silently does not exist on SQLite unless ``sqlite_where`` is set — so a constraint could
be perfectly declared, enforced in production, and enforce nothing at all under this
suite. A test that asserted the ``Index`` object's attributes would pass in exactly that
situation. These insert two rows and require the second to fail.

**The vocabulary.** ``NODE_TYPES`` and ``NODE_PORTS`` are read by the validator, by the
palette endpoint and by the AI prompt renderer. Nothing else makes them agree; if a node
type gains an entry in one and not the other, the palette offers a node with no exits or
the validator refuses an edge the canvas drew. The coherence checks below are cheap and
catch that at the point the constant is edited.
"""

from __future__ import annotations

import uuid as uuid_pkg
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.integrations import (
    CONNECTOR_NODE_TYPES,
    DEFAULT_BATCH_SIZE,
    LOOP_NODE_TYPES,
    MAX_BATCH_SIZE,
    MIN_BATCH_SIZE,
    MIN_INTERVAL_SECONDS,
    NODE_BATCH,
    NODE_BRANCH,
    NODE_PORTS,
    NODE_TYPE_LABELS,
    NODE_TYPE_VALUES,
    NODE_TYPES,
    PORT_VALUES,
    RUN_MODE_LIVE,
    RUN_PARTIAL,
    RUN_QUEUED,
    RUN_STATUSES,
    RUN_SUCCEEDED,
    TERMINAL_NODE_TYPES,
    TERMINAL_RUN_STATUSES,
    VERSION_ARCHIVED,
    VERSION_PUBLISHED,
    IntegrationConnection,
    IntegrationCredential,
    IntegrationCursor,
    IntegrationFlow,
    IntegrationFlowVersion,
    IntegrationRateCounter,
    IntegrationRun,
    IntegrationRunJob,
    IntegrationSyncKey,
)
from app.models.user.user import User


async def make_flow(db: AsyncSession, user: User, name: str = "Nightly sync") -> IntegrationFlow:
    flow = IntegrationFlow(user_id=user.id, name=name, graph_data={})
    db.add(flow)
    await db.commit()
    return flow


async def make_version(
    db: AsyncSession,
    flow: IntegrationFlow,
    number: int,
    status: str = VERSION_PUBLISHED,
) -> IntegrationFlowVersion:
    version = IntegrationFlowVersion(
        flow_id=flow.id,
        version_number=number,
        graph_data={},
        graph_hash=f"{number:064d}",
        status=status,
    )
    db.add(version)
    await db.commit()
    return version


async def make_run(
    db: AsyncSession,
    flow: IntegrationFlow,
    idempotency_key: str | None = None,
) -> IntegrationRun:
    run = IntegrationRun(
        flow_id=flow.id,
        thread_id=str(uuid_pkg.uuid4()),
        idempotency_key=idempotency_key,
    )
    db.add(run)
    await db.commit()
    return run


async def make_connection(
    db: AsyncSession,
    user: User,
    connector_id: str = "rest_generic",
    external_account_id: str | None = None,
    label: str = "Test connection",
) -> IntegrationConnection:
    connection = IntegrationConnection(
        user_id=user.id,
        connector_id=connector_id,
        label=label,
        external_account_id=external_account_id,
    )
    db.add(connection)
    await db.commit()
    return connection


# ---------------------------------------------------------------------------
# The constraints, tested against the database
# ---------------------------------------------------------------------------


class TestOnePublishedVersionPerFlow:
    """
    A partial unique index over ``flow_id WHERE status = 'published'``.

    This is the one the design calls out by name as a hazard: without
    ``sqlite_where`` beside ``postgresql_where``, SQLAlchemy drops the predicate on
    SQLite and the index becomes an unconditional unique on ``flow_id`` — which would
    make the *second* test below fail here and pass in production, an inversion that is
    considerably worse than no test.
    """

    async def test_two_published_versions_are_refused(
        self, db: AsyncSession, user: User
    ) -> None:
        flow = await make_flow(db, user)
        await make_version(db, flow, 1, VERSION_PUBLISHED)

        with pytest.raises(IntegrityError):
            await make_version(db, flow, 2, VERSION_PUBLISHED)

    async def test_many_archived_versions_are_fine(
        self, db: AsyncSession, user: User
    ) -> None:
        """
        The half that proves the predicate survived to the database. An
        unconditional unique index would fail this.
        """
        flow = await make_flow(db, user)
        await make_version(db, flow, 1, VERSION_ARCHIVED)
        await make_version(db, flow, 2, VERSION_ARCHIVED)
        await make_version(db, flow, 3, VERSION_PUBLISHED)

    async def test_two_flows_may_each_have_a_published_version(
        self, db: AsyncSession, user: User
    ) -> None:
        first = await make_flow(db, user, "First")
        second = await make_flow(db, user, "Second")

        await make_version(db, first, 1, VERSION_PUBLISHED)
        await make_version(db, second, 1, VERSION_PUBLISHED)

    async def test_a_version_number_is_used_once_per_flow(
        self, db: AsyncSession, user: User
    ) -> None:
        flow = await make_flow(db, user)
        await make_version(db, flow, 1, VERSION_ARCHIVED)

        with pytest.raises(IntegrityError):
            await make_version(db, flow, 1, VERSION_PUBLISHED)


class TestRunIdempotency:
    """
    The dedupe that stops a schedule firing one slot twice and a vendor redelivering
    one webhook twice. **The insert is the check** — a select-then-insert is racy at
    exactly the moment it matters, which is two workers claiming the same tick.
    """

    async def test_the_same_key_twice_on_one_flow_is_refused(
        self, db: AsyncSession, user: User
    ) -> None:
        flow = await make_flow(db, user)
        await make_run(db, flow, "trigger-abc:2026-08-14T09:00:00Z")

        with pytest.raises(IntegrityError):
            await make_run(db, flow, "trigger-abc:2026-08-14T09:00:00Z")

    async def test_many_runs_with_no_key_coexist(
        self, db: AsyncSession, user: User
    ) -> None:
        """
        Manual runs carry no key, and there are a lot of them. Losing the partial
        predicate would make a second manual run of any flow impossible — the failure
        this index's `sqlite_where` prevents from being invisible until production.
        """
        flow = await make_flow(db, user)
        for _ in range(3):
            await make_run(db, flow, None)

    async def test_the_same_key_on_a_different_flow_is_fine(
        self, db: AsyncSession, user: User
    ) -> None:
        first = await make_flow(db, user, "First")
        second = await make_flow(db, user, "Second")

        await make_run(db, first, "same-key")
        await make_run(db, second, "same-key")


class TestFlowNamesAreUniquePerUserCaseInsensitively:
    async def test_the_same_name_in_a_different_case_is_refused(
        self, db: AsyncSession, user: User
    ) -> None:
        await make_flow(db, user, "Nightly Sync")

        with pytest.raises(IntegrityError):
            await make_flow(db, user, "nightly sync")

    async def test_another_user_may_use_the_name(
        self, db: AsyncSession, user: User, make_user
    ) -> None:
        other = await make_user(email="other@example.com")
        await make_flow(db, user, "Nightly Sync")
        await make_flow(db, other, "Nightly Sync")


class TestOneJobPerRun:
    """
    Two jobs for one run is two workers executing it, which is the failure the queue
    table exists to prevent.
    """

    async def test_a_second_job_is_refused(self, db: AsyncSession, user: User) -> None:
        flow = await make_flow(db, user)
        run = await make_run(db, flow)

        db.add(IntegrationRunJob(run_id=run.id))
        await db.commit()

        db.add(IntegrationRunJob(run_id=run.id))
        with pytest.raises(IntegrityError):
            await db.commit()


class TestOneCredentialRowPerConnection:
    async def test_a_second_credential_row_is_refused(
        self, db: AsyncSession, user: User
    ) -> None:
        connection = await make_connection(db, user)

        db.add(IntegrationCredential(connection_id=connection.id))
        await db.commit()

        db.add(IntegrationCredential(connection_id=connection.id))
        with pytest.raises(IntegrityError):
            await db.commit()


class TestConnectionAccountUniqueness:
    """
    Many connections per connector is the point here — three Shopify stores, forty
    GoHighLevel locations — so the uniqueness is on the *account*, not on the
    connector. That is a deliberate departure from ``ai_api_keys``' one-per-provider
    rule and is worth pinning, because tightening it back would break the product
    rather than a test.
    """

    async def test_two_connections_to_the_same_account_are_refused(
        self, db: AsyncSession, user: User
    ) -> None:
        await make_connection(db, user, "shopify", "acme.myshopify.com")

        with pytest.raises(IntegrityError):
            await make_connection(db, user, "shopify", "acme.myshopify.com", "Duplicate")

    async def test_two_stores_on_one_connector_are_fine(
        self, db: AsyncSession, user: User
    ) -> None:
        await make_connection(db, user, "shopify", "acme.myshopify.com", "Acme")
        await make_connection(db, user, "shopify", "acme-eu.myshopify.com", "Acme EU")

    async def test_several_accountless_connections_coexist(
        self, db: AsyncSession, user: User
    ) -> None:
        """
        A generic REST connection has no account identity, so ``external_account_id``
        is NULL. NULLs are distinct in a unique constraint on both engines, which is
        what makes "three different APIs, one connector" expressible at all.
        """
        await make_connection(db, user, "rest_generic", None, "Billing API")
        await make_connection(db, user, "rest_generic", None, "Warehouse API")
        await make_connection(db, user, "rest_generic", None, "Support API")


class TestTheRemainingUniqueConstraints:
    async def test_one_cursor_per_flow_node(
        self, db: AsyncSession, user: User
    ) -> None:
        flow = await make_flow(db, user)
        db.add(IntegrationCursor(flow_id=flow.id, node_id="read-1", cursor_value="10"))
        await db.commit()

        db.add(IntegrationCursor(flow_id=flow.id, node_id="read-1", cursor_value="20"))
        with pytest.raises(IntegrityError):
            await db.commit()

    async def test_one_sync_key_row_per_natural_key(
        self, db: AsyncSession, user: User
    ) -> None:
        connection = await make_connection(db, user)
        for _ in range(2):
            db.add(
                IntegrationSyncKey(
                    connection_id=connection.id,
                    operation_id="create_customer",
                    natural_key_sha256="a" * 64,
                    target_record_id="cust_1",
                )
            )
        with pytest.raises(IntegrityError):
            await db.commit()

    async def test_one_rate_counter_per_connection_per_day(
        self, db: AsyncSession, user: User
    ) -> None:
        """
        The daily cap is the most account-endangering number in the module, so two
        rows for one day — each counting half the requests — is the shape of bug that
        gets a marketplace application suspended.
        """
        connection = await make_connection(db, user)
        today = date(2026, 8, 14)
        for _ in range(2):
            db.add(
                IntegrationRateCounter(
                    connection_id=connection.id, window_start_date=today, count=1
                )
            )
        with pytest.raises(IntegrityError):
            await db.commit()


# ---------------------------------------------------------------------------
# Defaults and cascades
# ---------------------------------------------------------------------------


class TestDefaults:
    async def test_a_new_flow_is_inactive_and_not_ai_authored(
        self, db: AsyncSession, user: User
    ) -> None:
        """
        Both default to false, and both matter. A flow that were active on creation
        could be fired by a trigger before anybody had read it; ``created_by_ai`` is a
        record of provenance and defaulting it true would make every hand-drawn
        workflow claim otherwise.
        """
        flow = await make_flow(db, user)
        await db.refresh(flow)

        assert flow.is_active is False
        assert flow.created_by_ai is False
        assert flow.default_batch_size == DEFAULT_BATCH_SIZE

    async def test_a_new_run_starts_queued_at_zero(
        self, db: AsyncSession, user: User
    ) -> None:
        flow = await make_flow(db, user)
        run = await make_run(db, flow)
        await db.refresh(run)

        assert run.status == RUN_QUEUED
        assert run.mode == RUN_MODE_LIVE
        assert run.cancel_requested is False
        assert run.records_log_truncated is False
        assert (
            run.records_read,
            run.records_written,
            run.records_failed,
            run.records_skipped,
        ) == (0, 0, 0, 0)

    async def test_a_new_connection_is_active_without_the_private_hatch(
        self, db: AsyncSession, user: User
    ) -> None:
        """
        ``allow_private_hosts`` defaulting false is a security default, not a
        convenience one: it is the difference between a connector that can reach the
        public internet and one that can be pointed inside the network.
        """
        connection = await make_connection(db, user)
        await db.refresh(connection)

        assert connection.is_active is True
        assert connection.allow_private_hosts is False
        assert connection.private_host_allowlist is None

    async def test_an_operation_is_not_idempotent_until_it_says_so(
        self, db: AsyncSession, user: User
    ) -> None:
        """
        The safe default, and the reason it is per-operation rather than per-vendor:
        a write retried after a read timeout may already have happened, and creating
        a second order is not something a backoff can undo.
        """
        from app.models.integrations import IntegrationRestOperation

        connection = await make_connection(db, user)
        operation = IntegrationRestOperation(
            connection_id=connection.id,
            operation_id="create_order",
            label="Create an order",
            kind="write",
            method="POST",
            path="/orders",
        )
        db.add(operation)
        await db.commit()
        await db.refresh(operation)

        assert operation.idempotent is False
        assert operation.ordered is False


class TestPublicIdentifiers:
    async def test_every_row_gets_a_uuid_without_being_asked(
        self, db: AsyncSession, user: User
    ) -> None:
        """
        The uuid is the only identifier that ever leaves the module, so a row that
        somehow got none would be a row no route could name.
        """
        flow = await make_flow(db, user)
        run = await make_run(db, flow)
        connection = await make_connection(db, user)

        for row in (flow, run, connection):
            assert isinstance(row.uuid, uuid_pkg.UUID)


@pytest.fixture
async def fk_db():
    """
    A session on an engine with ``PRAGMA foreign_keys = ON``.

    SQLite ignores ``ON DELETE`` clauses unless foreign keys are switched on for the
    connection, and the suite's shared ``db`` fixture leaves them off — so a cascade
    test written against it would pass whatever the migration declared, which is the
    least useful kind of passing. The pragma is set here rather than in
    ``tests/conftest.py`` because turning it on globally changes the behaviour of every
    existing test that inserts a row referring to one it did not create, and that is a
    separate decision from this one.

    Local to this module for the same reason: these two tests are the only ones that
    need it.
    """
    from sqlalchemy import event
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import StaticPool

    from app.db.base import Base

    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
        future=True,
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_foreign_keys(connection, record):  # noqa: ANN001, ANN202
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session

    await engine.dispose()


@pytest.fixture
async def fk_user(fk_db: AsyncSession) -> User:
    """A user on the foreign-key-enforcing engine, since ``user`` is bound to the other."""
    from app.models.user.user import User as UserModel

    row = UserModel(
        uuid=uuid_pkg.uuid4(),
        email="cascade@example.com",
        password="not-a-real-hash",
        role="admin",
        is_active=True,
    )
    fk_db.add(row)
    await fk_db.commit()
    return row


class TestCascades:
    """
    Deleting a parent must take its children with it, at the database rather than in
    the ORM: the rows are also written by a background worker that has no identity map,
    so a cascade that only exists in ``relationship(cascade=...)`` is a cascade that
    does not happen where it matters.
    """

    async def test_deleting_a_flow_takes_its_runs_and_versions(
        self, fk_db: AsyncSession, fk_user: User
    ) -> None:
        from sqlalchemy import func, select

        flow = await make_flow(fk_db, fk_user)
        await make_version(fk_db, flow, 1)
        await make_run(fk_db, flow)

        await fk_db.execute(
            IntegrationFlow.__table__.delete().where(IntegrationFlow.id == flow.id)
        )
        await fk_db.commit()

        for model in (IntegrationRun, IntegrationFlowVersion):
            remaining = await fk_db.scalar(select(func.count()).select_from(model))
            assert remaining == 0, model.__name__

    async def test_deleting_a_connection_takes_its_credential(
        self, fk_db: AsyncSession, fk_user: User
    ) -> None:
        """
        The reason the secrets are a separate table rather than six more columns:
        revoking is one DELETE that provably leaves nothing behind.
        """
        from sqlalchemy import func, select

        connection = await make_connection(fk_db, fk_user)
        fk_db.add(
            IntegrationCredential(
                connection_id=connection.id, api_key_encrypted="ciphertext"
            )
        )
        await fk_db.commit()

        await fk_db.execute(
            IntegrationConnection.__table__.delete().where(
                IntegrationConnection.id == connection.id
            )
        )
        await fk_db.commit()

        remaining = await fk_db.scalar(
            select(func.count()).select_from(IntegrationCredential)
        )
        assert remaining == 0


# ---------------------------------------------------------------------------
# The vocabulary
# ---------------------------------------------------------------------------


class TestNodeVocabularyIsCoherent:
    """
    One list feeds the validator, the palette and the AI prompt. These assertions are
    what stop it becoming three.
    """

    def test_every_node_type_declares_its_ports(self) -> None:
        # `branch` is the one exception, and deliberately so: its ports are authored,
        # one per condition the user wrote plus `else`, so a static list would be a
        # lie. `flow_rules` derives them from the node's own data.
        expected = NODE_TYPE_VALUES - {NODE_BRANCH}
        assert set(NODE_PORTS) == expected

    def test_no_port_is_invented(self) -> None:
        for node_type, ports in NODE_PORTS.items():
            assert set(ports) <= PORT_VALUES, node_type

    def test_a_terminal_node_has_no_exits(self) -> None:
        """An edge out of a terminal is a refusal the validator can state, which is
        only true if the terminal genuinely offers nowhere to go."""
        for node_type in TERMINAL_NODE_TYPES:
            assert NODE_PORTS[node_type] == ()

    def test_the_batch_node_is_the_only_loop(self) -> None:
        """A cycle is legal exactly when it passes through one of these. If a second
        node type became a loop, `validate_flow`'s cycle check would have to cut two
        kinds of back edge — so the set is asserted rather than assumed."""
        assert LOOP_NODE_TYPES == {NODE_BATCH}

    def test_a_loop_offers_both_a_body_and_a_way_out(self) -> None:
        for node_type in LOOP_NODE_TYPES:
            assert set(NODE_PORTS[node_type]) == {"body", "done"}

    def test_every_connector_node_can_fail(self) -> None:
        """It calls somebody else's server, so an error port is not optional — a
        connector node with no error exit would make "what do I do when the API is
        down" undrawable."""
        for node_type in CONNECTOR_NODE_TYPES:
            assert "error" in NODE_PORTS[node_type]

    def test_every_node_type_has_a_label(self) -> None:
        assert set(NODE_TYPE_LABELS) == NODE_TYPE_VALUES
        assert all(label.strip() for label in NODE_TYPE_LABELS.values())

    def test_the_vocabulary_has_no_duplicates(self) -> None:
        values = [value for value, _ in NODE_TYPES]
        assert len(values) == len(set(values))


class TestRunStatusesAreCoherent:
    def test_every_terminal_status_is_a_status(self) -> None:
        assert TERMINAL_RUN_STATUSES <= RUN_STATUSES

    def test_partial_is_terminal_and_is_not_success(self) -> None:
        """
        The distinction the whole three-level failure model rests on. A run that moved
        49,997 of 50,000 records did not succeed, and reporting it as success with a
        counter nobody reads is how a silent data-loss bug survives for months.
        """
        assert RUN_PARTIAL in TERMINAL_RUN_STATUSES
        assert RUN_PARTIAL != RUN_SUCCEEDED

    def test_queued_and_running_are_not_terminal(self) -> None:
        assert not ({RUN_QUEUED, "running"} & TERMINAL_RUN_STATUSES)


class TestBounds:
    def test_the_batch_size_range_is_sane(self) -> None:
        assert MIN_BATCH_SIZE <= DEFAULT_BATCH_SIZE <= MAX_BATCH_SIZE

    def test_the_batch_ceiling_is_a_memory_bound_not_a_round_number(self) -> None:
        """
        Pinned because it is enforced in *validation* rather than defaulted: a batch is
        held in process memory, so raising this is a decision about how much of
        somebody's data one worker holds at once, not a tuning knob.
        """
        assert MAX_BATCH_SIZE == 5000

    def test_a_schedule_cannot_be_faster_than_a_minute(self) -> None:
        assert MIN_INTERVAL_SECONDS == 60


class TestTriggerSchedulingColumns:
    async def test_next_run_at_is_stored_rather_than_computed(
        self, db: AsyncSession, user: User
    ) -> None:
        """
        The scheduler holds nothing in memory. A trigger written by one process must be
        due for a *different* process that has just started — which is only true
        because the due time is a column.
        """
        from app.models.integrations import IntegrationTrigger, TRIGGER_SCHEDULE

        flow = await make_flow(db, user)
        due = datetime.now(timezone.utc) + timedelta(seconds=MIN_INTERVAL_SECONDS)
        trigger = IntegrationTrigger(
            flow_id=flow.id,
            node_id="trigger-1",
            kind=TRIGGER_SCHEDULE,
            is_enabled=True,
            interval_seconds=MIN_INTERVAL_SECONDS,
            next_run_at=due,
        )
        db.add(trigger)
        await db.commit()

        db.expunge_all()
        reloaded = await db.get(IntegrationTrigger, trigger.id)
        assert reloaded.next_run_at is not None
        assert reloaded.catch_up is False
        assert reloaded.overlap_policy == "skip"
