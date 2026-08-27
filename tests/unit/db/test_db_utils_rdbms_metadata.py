"""
Tests for the RDBMS metadata and row-sampling functions in app/db/db_utils.py.

These run against a **real** file-backed SQLite database created per test, so
the SQL and the SQLAlchemy Inspector calls genuinely execute rather than being
mocked. That matters here more than usual: the whole point of
``fetch_rdbms_metadata`` is that it emits dialect-correct catalog queries, and a
mocked Inspector would prove nothing about that.

File-backed rather than in-memory, because ``get_engine`` always passes
``pool_size`` / ``max_overflow`` / ``pool_timeout`` and an in-memory aiosqlite
URL resolves to StaticPool, which rejects them.

The Postgres and MySQL branches cannot be executed for real without those
servers. What *is* testable — and is tested — is that each branch selects its own
query, which shows up as the query failing against SQLite rather than being
silently skipped.

Two module-global caches (``_engine_cache``, ``_mongo_cache``) are cleared and
their engines disposed around every test.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine

from app.db import db_utils
from app.db.db_utils import (
    MAX_REFLECTED_TABLES,
    _quote_identifier,
    _reflect_one,
    fetch_rdbms_metadata,
    fetch_rdbms_rows,
    fetch_rdbms_schema,
    fetch_rdbms_table_names,
    fetch_rdbms_tables,
)


@pytest.fixture(autouse=True)
async def clean_caches():
    db_utils._engine_cache.clear()
    db_utils._mongo_cache.clear()
    yield
    for wrapper in list(db_utils._engine_cache.values()):
        await wrapper.engine.dispose()
    db_utils._engine_cache.clear()
    db_utils._mongo_cache.clear()


SCHEMA_SQL = [
    """
    CREATE TABLE customers (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        email TEXT
    )
    """,
    """
    CREATE TABLE orders (
        id INTEGER PRIMARY KEY,
        customer_id INTEGER NOT NULL REFERENCES customers(id),
        total REAL,
        note TEXT
    )
    """,
    "CREATE VIEW recent_orders AS SELECT id, total FROM orders",
    "INSERT INTO customers (id, name, email) VALUES (1, 'Ada', 'ada@example.com')",
    "INSERT INTO customers (id, name, email) VALUES (2, 'Grace', NULL)",
    "INSERT INTO orders (id, customer_id, total, note) VALUES (1, 1, 9.99, 'first')",
    "INSERT INTO orders (id, customer_id, total, note) VALUES (2, 1, 19.50, NULL)",
    "INSERT INTO orders (id, customer_id, total, note) VALUES (3, 2, 4.25, 'third')",
]


@pytest.fixture
async def seeded_url(tmp_path: Path) -> str:
    """A real SQLite database with two tables, a view, a foreign key and rows."""
    path = tmp_path / "shop.db"
    url = f"sqlite+aiosqlite:///{path}"

    engine = create_async_engine(url)
    async with engine.begin() as conn:
        for statement in SCHEMA_SQL:
            await conn.execute(text(statement))
    await engine.dispose()

    return url


@pytest.fixture
async def empty_url(tmp_path: Path) -> str:
    path = tmp_path / "empty.db"
    url = f"sqlite+aiosqlite:///{path}"
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE placeholder (id INTEGER PRIMARY KEY)"))
        await conn.execute(text("DROP TABLE placeholder"))
    await engine.dispose()
    return url


# ---------------------------------------------------------------------------
# fetch_rdbms_tables
# ---------------------------------------------------------------------------
class TestFetchRdbmsTables:
    async def test_lists_sqlite_tables(self, seeded_url: str) -> None:
        names = await fetch_rdbms_tables(seeded_url, "sqlite")

        assert set(names) >= {"customers", "orders"}

    async def test_registers_success_and_caches_the_engine(self, seeded_url: str) -> None:
        await fetch_rdbms_tables(seeded_url, "sqlite")

        wrapper = db_utils._engine_cache[seeded_url]
        assert wrapper.failures == 0
        assert wrapper.circuit_open_until is None

    async def test_an_empty_database_lists_nothing(self, empty_url: str) -> None:
        assert await fetch_rdbms_tables(empty_url, "sqlite") == []

    @pytest.mark.parametrize("db_type", ["oracle", "mssql", "", "SQLITE", "mongo"])
    async def test_an_unsupported_dialect_is_rejected_before_connecting(
        self, seeded_url: str, db_type: str
    ) -> None:
        """The ValueError is raised before ``get_engine``, so an unsupported
        dialect never opens a pool."""
        with pytest.raises(ValueError, match="Unsupported RDBMS"):
            await fetch_rdbms_tables(seeded_url, db_type)

        assert db_utils._engine_cache == {}

    @pytest.mark.parametrize("db_type", ["postgres", "mysql"])
    async def test_each_dialect_selects_its_own_query(
        self, seeded_url: str, db_type: str
    ) -> None:
        """
        Proves the Postgres and MySQL branches are taken rather than skipped:
        their catalog queries (``information_schema.tables`` / ``SHOW TABLES``)
        are meaningless to SQLite, so running them against it fails.

        The original ``SQLAlchemyError`` reaches the caller intact: the
        ``except`` arm records a failure and re-raises. (It used to surface as
        ``TypeError`` from the broken circuit breaker, which masked the real
        cause — see the regression tests in test_db_utils_pool.py.)
        """
        with pytest.raises(SQLAlchemyError):
            await fetch_rdbms_tables(seeded_url, db_type)

        assert db_utils._engine_cache[seeded_url].failures == 1


# ---------------------------------------------------------------------------
# fetch_rdbms_schema
# ---------------------------------------------------------------------------
class TestFetchRdbmsSchema:
    async def test_returns_column_names_and_types(self, seeded_url: str) -> None:
        schema = await fetch_rdbms_schema(seeded_url, "sqlite", "customers")

        assert [entry["column"] for entry in schema] == ["id", "name", "email"]
        assert {entry["type"] for entry in schema} == {"INTEGER", "TEXT"}

    async def test_columns_are_in_declaration_order(self, seeded_url: str) -> None:
        schema = await fetch_rdbms_schema(seeded_url, "sqlite", "orders")

        assert [entry["column"] for entry in schema] == [
            "id",
            "customer_id",
            "total",
            "note",
        ]

    async def test_an_unknown_table_returns_an_empty_schema(self, seeded_url: str) -> None:
        """PRAGMA on a missing table returns no rows rather than erroring, so the
        caller gets [] — worth pinning, since it is not an exception."""
        assert await fetch_rdbms_schema(seeded_url, "sqlite", "no_such_table") == []

    async def test_works_for_a_view(self, seeded_url: str) -> None:
        schema = await fetch_rdbms_schema(seeded_url, "sqlite", "recent_orders")

        assert [entry["column"] for entry in schema] == ["id", "total"]

    @pytest.mark.parametrize("db_type", ["oracle", "mssql", ""])
    async def test_an_unsupported_dialect_is_rejected(
        self, seeded_url: str, db_type: str
    ) -> None:
        with pytest.raises(ValueError, match="Unsupported RDBMS"):
            await fetch_rdbms_schema(seeded_url, db_type, "customers")

    @pytest.mark.parametrize("db_type", ["postgres", "mysql"])
    async def test_each_dialect_selects_its_own_parameterized_query(
        self, seeded_url: str, db_type: str
    ) -> None:
        """Same reasoning as the tables test above — and note these two branches
        bind ``:table_name`` as a parameter, while the SQLite branch cannot
        (PRAGMA takes no parameters) and validates then interpolates it."""
        with pytest.raises(SQLAlchemyError):
            await fetch_rdbms_schema(seeded_url, db_type, "customers")

    @pytest.mark.parametrize(
        "hostile",
        [
            "customers]; DROP TABLE orders; --",
            "customers'",
            'customers"',
            "customers`",
            "customers)",
            "customers[x]",
            "",
            "   ",
            "x" * 256,
        ],
    )
    async def test_a_table_name_that_could_escape_the_pragma_is_refused(
        self, seeded_url: str, hostile: str
    ) -> None:
        """
        Regression test for a fixed defect.

        PRAGMA accepts no bound parameters, so the name is interpolated into
        ``PRAGMA table_info([...])``. The code's own comment said "in production,
        validate table_name to prevent injection" — and nothing did. It now runs
        through ``_validated_identifier`` first, which refuses anything that
        could close the bracket quoting or start a new statement.
        """
        with pytest.raises(ValueError, match="Invalid table identifier"):
            await fetch_rdbms_schema(seeded_url, "sqlite", hostile)

    @pytest.mark.parametrize("name", ["Order Details", "sales.2024", "a-b", "_x"])
    async def test_ordinary_table_names_with_spaces_and_dots_are_accepted(
        self, seeded_url: str, name: str
    ) -> None:
        """
        The guard is deliberately looser than ``_IDENTIFIER_RE``: real tables are
        called things like "Order Details", and those names come back from the
        catalog, so rejecting them would make such tables unreadable.

        These do not exist in the fixture database, so the assertion is that the
        name is *accepted* — PRAGMA returns no rows for an unknown table rather
        than erroring.
        """
        assert await fetch_rdbms_schema(seeded_url, "sqlite", name) == []

    async def test_a_real_table_still_reads_after_validation(
        self, seeded_url: str
    ) -> None:
        schema = await fetch_rdbms_schema(seeded_url, "sqlite", "orders")
        assert [entry["column"] for entry in schema] == [
            "id",
            "customer_id",
            "total",
            "note",
        ]


# ---------------------------------------------------------------------------
# fetch_rdbms_table_names (reflection)
# ---------------------------------------------------------------------------
class TestFetchRdbmsTableNames:
    async def test_includes_tables_and_views_sorted(self, seeded_url: str) -> None:
        """Views are as queryable as tables for generating a SELECT, so they
        belong in the list the SQL assistant is shown."""
        names = await fetch_rdbms_table_names(seeded_url)

        assert names == ["customers", "orders", "recent_orders"]

    async def test_result_is_deduplicated(self, seeded_url: str) -> None:
        names = await fetch_rdbms_table_names(seeded_url)
        assert len(names) == len(set(names))

    async def test_an_empty_database_returns_nothing(self, empty_url: str) -> None:
        assert await fetch_rdbms_table_names(empty_url) == []

    async def test_registers_success(self, seeded_url: str) -> None:
        await fetch_rdbms_table_names(seeded_url)
        assert db_utils._engine_cache[seeded_url].failures == 0

    async def test_needs_no_dialect_argument(self, seeded_url: str) -> None:
        """Reflection asks the Inspector, which emits its own dialect-correct
        catalog queries — so unlike ``fetch_rdbms_tables`` there is no per-dialect
        branch to get wrong."""
        assert await fetch_rdbms_table_names(seeded_url)


# ---------------------------------------------------------------------------
# fetch_rdbms_metadata (reflection)
# ---------------------------------------------------------------------------
class TestFetchRdbmsMetadata:
    async def test_reflects_columns_with_types_and_nullability(
        self, seeded_url: str
    ) -> None:
        (entry,) = await fetch_rdbms_metadata(seeded_url, ["customers"])

        assert entry["table"] == "customers"
        assert entry["kind"] == "table"
        by_name = {c["name"]: c for c in entry["columns"]}
        assert by_name["name"]["nullable"] is False
        assert by_name["email"]["nullable"] is True
        assert "INTEGER" in by_name["id"]["type"]

    async def test_reports_the_primary_key(self, seeded_url: str) -> None:
        (entry,) = await fetch_rdbms_metadata(seeded_url, ["customers"])
        assert entry["primary_key"] == ["id"]

    async def test_reports_foreign_keys(self, seeded_url: str) -> None:
        """Foreign keys are what make a generated join correct rather than
        guessed, which is why they are included when defaults and comments are
        deliberately not."""
        (entry,) = await fetch_rdbms_metadata(seeded_url, ["orders"])

        assert entry["foreign_keys"] == [
            {
                "columns": ["customer_id"],
                "references_table": "customers",
                "references_columns": ["id"],
            }
        ]

    async def test_never_returns_row_data(self, seeded_url: str) -> None:
        """This path exists to send structure and nothing else to an AI prompt —
        no default values, no comments, no rows."""
        (entry,) = await fetch_rdbms_metadata(seeded_url, ["customers"])

        assert set(entry) == {"table", "kind", "columns", "primary_key", "foreign_keys"}
        assert all(set(c) == {"name", "type", "nullable"} for c in entry["columns"])
        assert "Ada" not in str(entry)

    async def test_a_view_reports_columns_but_no_keys(self, seeded_url: str) -> None:
        """Keys are only asked for on real tables — a view has none, and some
        dialects raise rather than return empty when asked."""
        (entry,) = await fetch_rdbms_metadata(seeded_url, ["recent_orders"])

        assert entry["kind"] == "view"
        assert [c["name"] for c in entry["columns"]] == ["id", "total"]
        assert "primary_key" not in entry
        assert "foreign_keys" not in entry

    async def test_unknown_names_are_dropped_rather_than_erroring(
        self, seeded_url: str
    ) -> None:
        """The caller diffs what it asked for against what came back and reports
        the difference itself (see sql_assist_service.generate_sql)."""
        entries = await fetch_rdbms_metadata(seeded_url, ["customers", "ghost_table"])

        assert [e["table"] for e in entries] == ["customers"]

    async def test_requested_order_is_preserved(self, seeded_url: str) -> None:
        entries = await fetch_rdbms_metadata(seeded_url, ["orders", "customers"])
        assert [e["table"] for e in entries] == ["orders", "customers"]

    async def test_an_empty_request_returns_nothing(self, seeded_url: str) -> None:
        assert await fetch_rdbms_metadata(seeded_url, []) == []

    async def test_the_reflected_table_count_is_capped(
        self, tmp_path: Path
    ) -> None:
        """Every reflected table costs a catalog round-trip and lands in an AI
        prompt, so the count is bounded rather than however many were asked for."""
        path = tmp_path / "many.db"
        url = f"sqlite+aiosqlite:///{path}"
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            for i in range(MAX_REFLECTED_TABLES + 5):
                await conn.execute(text(f"CREATE TABLE t{i:03d} (id INTEGER PRIMARY KEY)"))
        await engine.dispose()

        names = [f"t{i:03d}" for i in range(MAX_REFLECTED_TABLES + 5)]
        entries = await fetch_rdbms_metadata(url, names)

        assert len(entries) == MAX_REFLECTED_TABLES

    async def test_registers_success(self, seeded_url: str) -> None:
        await fetch_rdbms_metadata(seeded_url, ["customers"])
        assert db_utils._engine_cache[seeded_url].failures == 0


class TestReflectOne:
    """``_reflect_one`` is sync and takes an Inspector, so it is driven directly
    through ``run_sync`` rather than through the async wrapper above."""

    async def test_skips_a_foreign_key_with_no_referred_table(
        self, seeded_url: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Some dialects can report an incomplete FK; the comprehension filters
        it out rather than raising a KeyError on ``referred_table``."""
        from sqlalchemy import inspect as sync_inspect

        engine = create_async_engine(seeded_url)
        try:
            async with engine.connect() as conn:

                def _run(sync_conn):  # noqa: ANN001, ANN202
                    inspector = sync_inspect(sync_conn)
                    monkeypatch.setattr(
                        type(inspector),
                        "get_foreign_keys",
                        lambda self, name, **kw: [
                            {"constrained_columns": ["x"], "referred_table": None},
                            {"constrained_columns": ["y"], "referred_table": ""},
                        ],
                    )
                    return _reflect_one(inspector, "orders", is_view=False)

                entry = await conn.run_sync(_run)
        finally:
            await engine.dispose()

        assert entry["foreign_keys"] == []

    async def test_a_missing_pk_constraint_yields_an_empty_list(
        self, seeded_url: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from sqlalchemy import inspect as sync_inspect

        engine = create_async_engine(seeded_url)
        try:
            async with engine.connect() as conn:

                def _run(sync_conn):  # noqa: ANN001, ANN202
                    inspector = sync_inspect(sync_conn)
                    monkeypatch.setattr(
                        type(inspector), "get_pk_constraint", lambda self, name, **kw: None
                    )
                    return _reflect_one(inspector, "customers", is_view=False)

                entry = await conn.run_sync(_run)
        finally:
            await engine.dispose()

        assert entry["primary_key"] == []


# ---------------------------------------------------------------------------
# fetch_rdbms_rows
# ---------------------------------------------------------------------------
class TestFetchRdbmsRows:
    async def test_returns_rows_as_plain_dicts(self, seeded_url: str) -> None:
        rows = await fetch_rdbms_rows(seeded_url, "sqlite", "customers")

        assert rows == [
            {"id": 1, "name": "Ada", "email": "ada@example.com"},
            {"id": 2, "name": "Grace", "email": None},
        ]
        assert all(type(row) is dict for row in rows)

    async def test_the_limit_is_applied(self, seeded_url: str) -> None:
        rows = await fetch_rdbms_rows(seeded_url, "sqlite", "orders", limit=2)
        assert len(rows) == 2

    async def test_the_default_limit_is_500(self, seeded_url: str) -> None:
        rows = await fetch_rdbms_rows(seeded_url, "sqlite", "orders")
        assert len(rows) == 3

    async def test_a_limit_of_zero_returns_nothing(self, seeded_url: str) -> None:
        assert await fetch_rdbms_rows(seeded_url, "sqlite", "orders", limit=0) == []

    async def test_reads_from_a_view(self, seeded_url: str) -> None:
        rows = await fetch_rdbms_rows(seeded_url, "sqlite", "recent_orders", limit=1)
        assert set(rows[0]) == {"id", "total"}

    @pytest.mark.parametrize(
        "hostile",
        [
            "orders; DROP TABLE customers",
            "orders WHERE 1=1",
            'orders" --',
            "orders`",
            "ord ers",
            "",
            "orders(1)",
        ],
    )
    async def test_a_hostile_table_name_is_refused_before_any_sql_runs(
        self, seeded_url: str, hostile: str
    ) -> None:
        """
        The identifier charset check is the injection guard: the name cannot be
        bound as a parameter, so it is validated and quoted instead. Rejection
        happens before ``get_engine``, so nothing is executed and no pool opens.
        """
        with pytest.raises(ValueError, match="Invalid table identifier"):
            await fetch_rdbms_rows(seeded_url, "sqlite", hostile)

        assert db_utils._engine_cache == {}

    async def test_an_unknown_table_raises(self, seeded_url: str) -> None:
        """A name that passes the charset check but does not exist fails in the
        database, and the original SQLAlchemyError reaches the caller."""
        with pytest.raises(SQLAlchemyError):
            await fetch_rdbms_rows(seeded_url, "sqlite", "no_such_table")

        assert db_utils._engine_cache[seeded_url].failures == 1

    async def test_registers_success(self, seeded_url: str) -> None:
        await fetch_rdbms_rows(seeded_url, "sqlite", "customers")
        assert db_utils._engine_cache[seeded_url].failures == 0


class TestQuoteIdentifier:
    @pytest.mark.parametrize("db_type", ["postgres", "sqlite"])
    def test_double_quotes_for_postgres_and_sqlite(self, db_type: str) -> None:
        assert _quote_identifier(db_type, "orders") == '"orders"'

    def test_backticks_for_mysql(self) -> None:
        assert _quote_identifier("mysql", "orders") == "`orders`"

    @pytest.mark.parametrize("name", ["orders", "Orders_2", "_x", "A1_b2"])
    def test_accepts_plain_identifiers(self, name: str) -> None:
        assert name in _quote_identifier("sqlite", name)
