"""
The remaining branches of app/db/db_utils.py: reader-dispatch fallbacks, the
fastavro schema variants, and the ``except`` arms that only become reachable
once the circuit-breaker constants are repaired.

Three groups, each needing a deliberate setup:

* **Unsupported-type fallbacks.** ``_normalise_file_type`` runs first and rejects
  anything unknown, so the ``raise ValueError(f"Unsupported file type")`` at the
  bottom of each reader is unreachable through the public entry points. They are
  reached here by seeding ``_file_cache`` directly — which is exactly the state a
  future format would produce if it were added to ``_SUPPORTED_FILE_TYPES``
  without a matching reader branch, so the guard is worth having and worth
  testing.

* **The Excel branches.** They cannot be executed for real (openpyxl is missing —
  finding 2), so pandas is stubbed to prove the dispatch *reaches* the Excel
  branch and that its failure is handled. These tests deliberately do **not**
  claim Excel reading works; the genuine round-trip tests in
  ``test_db_utils_file_datasources.py`` stay skipped until the dependency is
  declared.

* **The RDBMS/Mongo ``except`` arms.** ``_register_failure`` raises before the
  ``raise`` beneath it can run, so those lines need the constants patched to the
  ints they were always meant to be.
"""

from __future__ import annotations

import time
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine

from app.db import db_utils
from app.db.db_utils import (
    FileDataSourceWrapper,
    _parse_avro_schema,
    fetch_file_listing,
    fetch_file_preview,
    fetch_file_schema,
    fetch_rdbms_metadata,
    fetch_rdbms_rows,
    fetch_rdbms_schema,
    fetch_rdbms_table_names,
    fetch_rdbms_tables,
)

check_file_connection = db_utils.test_file_connection


@pytest.fixture(autouse=True)
async def clean_caches():
    db_utils._file_cache.clear()
    db_utils._engine_cache.clear()
    db_utils._mongo_cache.clear()
    yield
    for wrapper in list(db_utils._engine_cache.values()):
        await wrapper.engine.dispose()
    db_utils._file_cache.clear()
    db_utils._engine_cache.clear()
    db_utils._mongo_cache.clear()


@pytest.fixture
def integer_constants(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db_utils, "CIRCUIT_FAILURE_LIMIT", 3)
    monkeypatch.setattr(db_utils, "CIRCUIT_RESET_SECONDS", 60)


# ---------------------------------------------------------------------------
# _parse_avro_schema — the variants a real .avro file does not produce
# ---------------------------------------------------------------------------
class ObjectSchema:
    """A fastavro parsed-schema object rather than a plain dict."""

    def __init__(self, fields) -> None:  # noqa: ANN001
        self.fields = fields


class ObjectField:
    def __init__(self, name, type) -> None:  # noqa: ANN001, A002
        self.name = name
        self.type = type


class TestParseAvroSchemaVariants:
    def test_reads_an_object_style_schema(self) -> None:
        """fastavro may hand back a parsed-schema object instead of a dict; the
        ``hasattr(avro_schema, "fields")`` branch is what covers that."""
        schema = ObjectSchema([ObjectField("id", "int"), ObjectField("name", "string")])

        assert _parse_avro_schema(schema) == [
            {"column": "id", "type": "int"},
            {"column": "name", "type": "string"},
        ]

    def test_an_object_schema_with_no_fields_yields_nothing(self) -> None:
        assert _parse_avro_schema(ObjectSchema([])) == []

    def test_object_style_fields_fall_back_to_unknown(self) -> None:
        assert _parse_avro_schema(ObjectSchema([object()])) == [
            {"column": "unknown", "type": "unknown"}
        ]

    def test_a_nested_dict_type_reports_its_type_key(self) -> None:
        """A logical or complex type arrives as ``{"type": "array", ...}``; the
        readable name is what belongs in the schema, not the whole dict."""
        schema = {"fields": [{"name": "tags", "type": {"type": "array", "items": "string"}}]}

        assert _parse_avro_schema(schema) == [{"column": "tags", "type": "array"}]

    def test_a_nested_dict_type_with_no_type_key_stringifies(self) -> None:
        schema = {"fields": [{"name": "odd", "type": {"logicalType": "date"}}]}

        (entry,) = _parse_avro_schema(schema)
        assert entry["column"] == "odd"
        assert "logicalType" in entry["type"]

    def test_an_all_null_union_reports_null(self) -> None:
        schema = {"fields": [{"name": "nothing", "type": ["null"]}]}

        assert _parse_avro_schema(schema) == [{"column": "nothing", "type": "null"}]

    def test_a_dict_field_missing_name_and_type_falls_back(self) -> None:
        assert _parse_avro_schema({"fields": [{}]}) == [
            {"column": "unknown", "type": "unknown"}
        ]

    def test_an_object_without_fields_yields_nothing(self) -> None:
        assert _parse_avro_schema(object()) == []


# ---------------------------------------------------------------------------
# Reader dispatch: the unsupported-type fallback
# ---------------------------------------------------------------------------
def seed_wrapper(path: Path, file_type: str) -> FileDataSourceWrapper:
    """
    Put a wrapper straight into the cache, bypassing ``_normalise_file_type``.

    This is the only way to reach each reader's trailing "Unsupported file type"
    guard — and it mirrors the real bug that guard exists for: a type added to
    ``_SUPPORTED_FILE_TYPES`` without a matching branch in every reader.
    """
    resolved = str(path.resolve())
    wrapper = FileDataSourceWrapper(
        path=resolved, file_type=file_type, last_used=time.time()
    )
    db_utils._file_cache[resolved] = wrapper
    return wrapper


@pytest.fixture
def a_file(tmp_path: Path) -> Path:
    path = tmp_path / "data.csv"
    path.write_text("id\n1\n")
    return path


class TestUnsupportedTypeFallback:
    async def test_connection_test_returns_false(self, a_file: Path) -> None:
        seed_wrapper(a_file, "xml")

        assert await check_file_connection(str(a_file), "csv") is False

    async def test_schema_read_raises(self, a_file: Path) -> None:
        seed_wrapper(a_file, "xml")

        with pytest.raises(ValueError, match="Unsupported file type"):
            await fetch_file_schema(str(a_file), "csv")

    async def test_preview_raises(self, a_file: Path) -> None:
        seed_wrapper(a_file, "xml")

        with pytest.raises(ValueError, match="Unsupported file type"):
            await fetch_file_preview(str(a_file), "csv")

    async def test_the_failure_is_recorded_on_the_wrapper(self, a_file: Path) -> None:
        wrapper = seed_wrapper(a_file, "xml")

        with pytest.raises(ValueError):
            await fetch_file_schema(str(a_file), "csv")

        assert wrapper.failures == 1

    async def test_listing_treats_an_unknown_type_as_a_single_table(
        self, a_file: Path
    ) -> None:
        """``fetch_file_listing`` has no such guard — everything that is not
        Excel is "the file is the table", so an unknown type is listed rather
        than rejected. Recorded because it differs from the three readers above."""
        seed_wrapper(a_file, "xml")

        assert await fetch_file_listing(str(a_file), "csv") == ["data.csv"]


# ---------------------------------------------------------------------------
# The Excel branches, with pandas stubbed
# ---------------------------------------------------------------------------
class FakeExcelFile:
    def __init__(self, path) -> None:  # noqa: ANN001
        self.path = path
        self.sheet_names = ["Sheet1", "Summary"]


@pytest.fixture
def stub_excel(monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    """
    Replace pandas' Excel readers.

    This proves the dispatch reaches the Excel branch and that its result is
    shaped like the other formats'. It does **not** prove the application can
    read a real .xlsx — it cannot, because openpyxl is missing (finding 2). The
    genuine round-trip tests stay skipped in
    tests/unit/db/test_db_utils_file_datasources.py.
    """
    frame = pd.DataFrame(
        [{"id": 1, "name": "Widget"}, {"id": 2, "name": "Gadget"}]
    )

    calls: dict = {"read_excel": [], "excel_file": []}

    def fake_read_excel(path, nrows=None, **kwargs):  # noqa: ANN001, ANN003
        calls["read_excel"].append({"path": path, "nrows": nrows})
        return frame.head(nrows) if nrows is not None else frame

    def fake_excel_file(path):  # noqa: ANN001
        calls["excel_file"].append(path)
        return FakeExcelFile(path)

    monkeypatch.setattr(pd, "read_excel", fake_read_excel)
    monkeypatch.setattr(pd, "ExcelFile", fake_excel_file)
    return calls


@pytest.fixture
def xlsx_path(tmp_path: Path) -> Path:
    path = tmp_path / "book.xlsx"
    path.write_bytes(b"stub workbook")
    return path


class TestExcelDispatch:
    async def test_connection_test_reads_one_row(
        self, xlsx_path: Path, stub_excel: dict
    ) -> None:
        assert await check_file_connection(str(xlsx_path), "excel") is True
        assert stub_excel["read_excel"][0]["nrows"] == 1

    async def test_schema_samples_500_rows(
        self, xlsx_path: Path, stub_excel: dict
    ) -> None:
        schema = await fetch_file_schema(str(xlsx_path), "excel")

        assert [entry["column"] for entry in schema] == ["id", "name"]
        assert stub_excel["read_excel"][0]["nrows"] == 500

    async def test_preview_passes_the_limit_through(
        self, xlsx_path: Path, stub_excel: dict
    ) -> None:
        rows = await fetch_file_preview(str(xlsx_path), "excel", limit=1)

        assert rows == [{"id": 1, "name": "Widget"}]
        assert stub_excel["read_excel"][0]["nrows"] == 1

    async def test_listing_returns_the_sheet_names(
        self, xlsx_path: Path, stub_excel: dict
    ) -> None:
        assert await fetch_file_listing(str(xlsx_path), "excel") == ["Sheet1", "Summary"]

    async def test_a_reader_failure_is_recorded_and_re_raised(
        self, xlsx_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ``except`` arm of ``fetch_file_listing`` — reachable without
        openpyxl by making the stubbed reader raise."""

        def boom(path):  # noqa: ANN001
            raise RuntimeError("workbook is corrupt")

        monkeypatch.setattr(pd, "ExcelFile", boom)

        with pytest.raises(RuntimeError, match="workbook is corrupt"):
            await fetch_file_listing(str(xlsx_path), "excel")

        assert db_utils._file_cache[str(xlsx_path.resolve())].failures == 1


# ---------------------------------------------------------------------------
# RDBMS except arms, reachable once the constants are ints
# ---------------------------------------------------------------------------
@pytest.fixture
async def seeded_url(tmp_path: Path) -> str:
    path = tmp_path / "shop.db"
    url = f"sqlite+aiosqlite:///{path}"
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE orders (id INTEGER PRIMARY KEY)"))
    await engine.dispose()
    return url


class TestInformationSchemaResultShaping:
    """
    The Postgres/MySQL arm of ``fetch_rdbms_schema`` — the ``else`` branch that
    binds ``:table_name`` and reads ``row[0] / row[1]``.

    Neither server is available here, but the branch is dialect-agnostic once the
    query runs: it only needs a result set whose first two columns are the column
    name and the data type. SQLite can provide exactly that, because ``ATTACH
    DATABASE ... AS information_schema`` makes ``information_schema.columns`` a
    real, queryable table — so the *actual* Postgres query string in the module
    executes unmodified and the row-shaping code is genuinely exercised rather
    than mocked.

    The attachment is installed on a ``connect`` event so it survives the pool
    handing out a new connection, and the engine is seeded straight into
    ``_engine_cache`` so ``get_engine`` returns it instead of building a plain one.
    """

    @pytest.fixture
    async def information_schema_url(self, tmp_path: Path) -> str:
        from sqlalchemy import event

        catalog = tmp_path / "catalog.db"
        main = tmp_path / "main.db"
        url = f"sqlite+aiosqlite:///{main}"

        # Populate the stand-in catalog once.
        setup = create_async_engine(url)
        async with setup.begin() as conn:
            await conn.execute(text(f"ATTACH DATABASE '{catalog}' AS information_schema"))
            await conn.execute(
                text(
                    "CREATE TABLE information_schema.columns ("
                    "table_name TEXT, column_name TEXT, data_type TEXT, "
                    "ordinal_position INTEGER)"
                )
            )
            for position, (column, data_type) in enumerate(
                [("id", "integer"), ("name", "character varying"), ("total", "numeric")],
                start=1,
            ):
                await conn.execute(
                    text(
                        "INSERT INTO information_schema.columns VALUES "
                        "('orders', :c, :t, :p)"
                    ),
                    {"c": column, "t": data_type, "p": position},
                )
        await setup.dispose()

        engine = create_async_engine(url)

        @event.listens_for(engine.sync_engine, "connect")
        def _attach(dbapi_connection, _record) -> None:  # noqa: ANN001
            dbapi_connection.execute(
                f"ATTACH DATABASE '{catalog}' AS information_schema"
            )

        db_utils._engine_cache[url] = db_utils.EngineWrapper(
            engine=engine, last_used=time.time()
        )
        return url

    async def test_returns_column_and_type_from_the_catalog_query(
        self, information_schema_url: str
    ) -> None:
        schema = await fetch_rdbms_schema(information_schema_url, "postgres", "orders")

        assert schema == [
            {"column": "id", "type": "integer"},
            {"column": "name", "type": "character varying"},
            {"column": "total", "type": "numeric"},
        ]

    async def test_the_table_name_is_bound_as_a_parameter(
        self, information_schema_url: str
    ) -> None:
        """Unlike the SQLite branch, this one parameterises the table name — so a
        name that matches nothing simply returns no rows, with no chance of the
        value being interpreted as SQL."""
        assert await fetch_rdbms_schema(
            information_schema_url, "postgres", "orders'; DROP TABLE x --"
        ) == []

    async def test_the_mysql_branch_shapes_rows_the_same_way(
        self, information_schema_url: str
    ) -> None:
        """The MySQL query aliases its columns to ``column_name`` / ``data_type``
        so both dialects reach identical row-shaping code."""
        schema = await fetch_rdbms_schema(information_schema_url, "mysql", "orders")

        assert [entry["column"] for entry in schema] == ["id", "name", "total"]

    async def test_registers_success(self, information_schema_url: str) -> None:
        db_utils._engine_cache[information_schema_url].failures = 2

        await fetch_rdbms_schema(information_schema_url, "postgres", "orders")

        assert db_utils._engine_cache[information_schema_url].failures == 0


class TestRdbmsErrorPaths:
    """
    Each of these re-raises the original ``SQLAlchemyError`` after recording a
    failure — the behaviour the code intends, and which only happens with the
    circuit-breaker constants repaired.
    """

    async def test_fetch_rdbms_tables_re_raises(
        self, seeded_url: str, integer_constants
    ) -> None:  # noqa: ANN001
        with pytest.raises(SQLAlchemyError):
            await fetch_rdbms_tables(seeded_url, "postgres")

        assert db_utils._engine_cache[seeded_url].failures == 1

    async def test_fetch_rdbms_schema_re_raises(
        self, seeded_url: str, integer_constants
    ) -> None:  # noqa: ANN001
        with pytest.raises(SQLAlchemyError):
            await fetch_rdbms_schema(seeded_url, "postgres", "orders")

        assert db_utils._engine_cache[seeded_url].failures == 1

    async def test_fetch_rdbms_rows_re_raises_for_a_missing_table(
        self, seeded_url: str, integer_constants
    ) -> None:  # noqa: ANN001
        with pytest.raises(SQLAlchemyError):
            await fetch_rdbms_rows(seeded_url, "sqlite", "no_such_table")

        assert db_utils._engine_cache[seeded_url].failures == 1

    async def test_fetch_rdbms_table_names_re_raises(
        self, seeded_url: str, integer_constants, monkeypatch: pytest.MonkeyPatch
    ) -> None:  # noqa: ANN001
        """Reflection has no bad-input path of its own, so the connection itself
        is made to fail."""

        async def boom(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
            raise SQLAlchemyError("catalog unavailable")

        engine = await db_utils.get_engine(seeded_url)
        monkeypatch.setattr(type(engine), "connect", lambda self: _raising_ctx())

        with pytest.raises(SQLAlchemyError):
            await fetch_rdbms_table_names(seeded_url)

        assert db_utils._engine_cache[seeded_url].failures == 1

    async def test_fetch_rdbms_metadata_re_raises(
        self, seeded_url: str, integer_constants, monkeypatch: pytest.MonkeyPatch
    ) -> None:  # noqa: ANN001
        engine = await db_utils.get_engine(seeded_url)
        monkeypatch.setattr(type(engine), "connect", lambda self: _raising_ctx())

        with pytest.raises(SQLAlchemyError):
            await fetch_rdbms_metadata(seeded_url, ["orders"])

        assert db_utils._engine_cache[seeded_url].failures == 1


class _raising_ctx:
    """An async context manager whose __aenter__ fails, standing in for a
    connection that cannot be established."""

    async def __aenter__(self):  # noqa: ANN204
        raise SQLAlchemyError("catalog unavailable")

    async def __aexit__(self, *exc) -> None:  # noqa: ANN002
        return None


# ---------------------------------------------------------------------------
# CRUDQueryBuilder aggregation with no matching rows
# ---------------------------------------------------------------------------
class TestAggregate:
    """
    ``aggregate`` takes ``{result_name: sqlalchemy_expression}`` — the caller
    supplies the function, so the builder stays model-agnostic.
    """

    @pytest.fixture
    def crud(self):  # noqa: ANN201
        from app.models.workspaces import Workspace

        return db_utils.CRUDQueryBuilder(Workspace)

    @pytest.fixture
    def make_workspace(self, db):  # noqa: ANN001, ANN201
        from app.models.workspaces import Workspace

        async def _make(owner, name: str):  # noqa: ANN001
            row = Workspace(user_id=owner.id, name=name)
            db.add(row)
            await db.commit()
            return row

        return _make

    async def test_counts_matching_rows(self, db, user, crud, make_workspace) -> None:  # noqa: ANN001
        from sqlalchemy import func

        from app.models.workspaces import Workspace

        for name in ["a", "b", "c"]:
            await make_workspace(user, name)

        result = await crud.aggregate(
            db,
            aggregations={"total": func.count(Workspace.id)},
            filters={"user_id": user.id},
        )

        assert result == {"total": 3}

    async def test_a_filter_that_matches_nothing_counts_zero(
        self, db, user, crud, make_workspace  # noqa: ANN001
    ) -> None:
        """
        A bare aggregate has no GROUP BY, so the database always returns exactly
        one row — ``count`` of nothing is ``0``, not an absent row.

        That makes the ``if not row: return {name: None ...}`` guard in
        ``aggregate`` unreachable for any aggregation-only query. It is harmless
        defensive code; noted here so a future reader does not spend time
        hunting for the input that triggers it.
        """
        from sqlalchemy import func

        from app.models.workspaces import Workspace

        await make_workspace(user, "a")

        result = await crud.aggregate(
            db,
            aggregations={"total": func.count(Workspace.id)},
            filters={"user_id": 999999},
        )

        assert result == {"total": 0}

    async def test_runs_without_filters(self, db, user, crud, make_workspace) -> None:  # noqa: ANN001
        from sqlalchemy import func

        from app.models.workspaces import Workspace

        await make_workspace(user, "a")
        await make_workspace(user, "b")

        result = await crud.aggregate(db, aggregations={"n": func.count(Workspace.id)})

        assert result == {"n": 2}

    async def test_several_aggregations_map_to_their_names_in_order(
        self, db, user, crud, make_workspace  # noqa: ANN001
    ) -> None:
        from sqlalchemy import func

        from app.models.workspaces import Workspace

        await make_workspace(user, "a")
        await make_workspace(user, "b")

        result = await crud.aggregate(
            db,
            aggregations={
                "n": func.count(Workspace.id),
                "smallest": func.min(Workspace.id),
                "largest": func.max(Workspace.id),
            },
            filters={"user_id": user.id},
        )

        assert result["n"] == 2
        assert result["smallest"] < result["largest"]
