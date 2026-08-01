"""
Tests for the file-datasource half of app/db/db_utils.py.

This is the layer that turns an uploaded CSV / Excel / Parquet / JSON / Avro
file into the same "list tables, read schema, preview rows" interface the RDBMS
and Mongo paths expose. It needs no external service — only real files — so it
is exercised against genuine files written to tmp_path rather than mocked, which
means the pandas / pyarrow / fastavro reader selection is actually proven.

``_file_cache`` is module-global state keyed by resolved absolute path. The
autouse fixture below clears it around every test; without that, a wrapper left
behind with an open circuit would fail an unrelated test later in the session.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import time
from pathlib import Path

import pandas as pd
import pytest

from app.db import db_utils
from app.db.db_utils import (
    FileDataSourceWrapper,
    _file_register_failure,
    _file_register_success,
    close_file_datasource,
    fetch_file_listing,
    fetch_file_preview,
    fetch_file_schema,
    fetch_tables_or_collections_or_files,
    get_file_datasource,
)

# Aliased on import. The application function is named ``test_file_connection``,
# and importing that name into a test module makes pytest collect it as a test
# case — it then "fails" at collection for wanting a ``path`` fixture.
check_file_connection = db_utils.test_file_connection


# ---------------------------------------------------------------------------
# Excel support
#
# pandas needs ``openpyxl`` to read .xlsx. It used to be installed nowhere and
# declared in no requirements file, even though the application calls
# pd.read_excel / pd.ExcelFile in six places and advertises XLSX as a supported
# upload type (CLAUDE.md; the "xls" entry in
# app/utils/file_utils.ALLOWED_EXTENSIONS) — so every Excel upload failed at
# runtime with ModuleNotFoundError. It is now declared in requirements.txt.
#
# The guard stays because the dependency is easy to drop again: if openpyxl ever
# goes missing these skip loudly instead of silently ceasing to test Excel, and
# ``test_the_excel_engine_is_installed`` fails outright.
# ---------------------------------------------------------------------------
HAS_OPENPYXL = importlib.util.find_spec("openpyxl") is not None

requires_openpyxl = pytest.mark.skipif(
    not HAS_OPENPYXL,
    reason="openpyxl is not installed — Excel datasources cannot be read",
)


@pytest.fixture(autouse=True)
def clean_file_cache():
    """Isolate the module-global wrapper cache per test."""
    db_utils._file_cache.clear()
    yield
    db_utils._file_cache.clear()


# ---------------------------------------------------------------------------
# Sample files, one per supported format
# ---------------------------------------------------------------------------
ROWS = [
    {"id": 1, "name": "Widget", "price": 9.99},
    {"id": 2, "name": "Gadget", "price": 19.50},
    {"id": 3, "name": "Doohickey", "price": 4.25},
]


@pytest.fixture
def csv_file(tmp_path: Path) -> Path:
    path = tmp_path / "products.csv"
    pd.DataFrame(ROWS).to_csv(path, index=False)
    return path


@pytest.fixture
def excel_file(tmp_path: Path) -> Path:
    if not HAS_OPENPYXL:
        pytest.skip("openpyxl is not installed — Excel datasources cannot be read")
    path = tmp_path / "products.xlsx"
    with pd.ExcelWriter(path) as writer:
        pd.DataFrame(ROWS).to_excel(writer, sheet_name="products", index=False)
        pd.DataFrame(ROWS[:1]).to_excel(writer, sheet_name="summary", index=False)
    return path


@pytest.fixture
def parquet_file(tmp_path: Path) -> Path:
    path = tmp_path / "products.parquet"
    pd.DataFrame(ROWS).to_parquet(path, index=False)
    return path


@pytest.fixture
def json_array_file(tmp_path: Path) -> Path:
    path = tmp_path / "products.json"
    path.write_text(json.dumps(ROWS))
    return path


@pytest.fixture
def jsonl_file(tmp_path: Path) -> Path:
    path = tmp_path / "products_lines.json"
    path.write_text("\n".join(json.dumps(row) for row in ROWS) + "\n")
    return path


@pytest.fixture
def avro_file(tmp_path: Path) -> Path:
    import fastavro

    schema = {
        "type": "record",
        "name": "Product",
        "fields": [
            {"name": "id", "type": "int"},
            {"name": "name", "type": ["null", "string"]},
            {"name": "price", "type": "double"},
        ],
    }
    path = tmp_path / "products.avro"
    with path.open("wb") as fh:
        fastavro.writer(fh, fastavro.parse_schema(schema), ROWS)
    return path


@pytest.fixture
def every_format(
    csv_file, parquet_file, json_array_file, avro_file  # noqa: ANN001
) -> dict:
    """
    Every format the reader dispatch supports except Excel, which is covered by
    dedicated ``@requires_openpyxl`` tests — that way a missing openpyxl shows
    up as an explicit skip rather than quietly removing Excel from this matrix.
    """
    return {
        "csv": csv_file,
        "parquet": parquet_file,
        "json": json_array_file,
        "avro": avro_file,
    }


ALL_TYPES = ["csv", "parquet", "json", "avro"]


# ---------------------------------------------------------------------------
# get_file_datasource / close_file_datasource
# ---------------------------------------------------------------------------
class TestGetFileDatasource:
    async def test_creates_and_caches_a_wrapper(self, csv_file: Path) -> None:
        wrapper = await get_file_datasource(str(csv_file), "csv")

        assert isinstance(wrapper, FileDataSourceWrapper)
        assert wrapper.path == str(csv_file.resolve())
        assert wrapper.file_type == "csv"
        assert wrapper.failures == 0
        assert wrapper.circuit_open_until is None
        assert db_utils._file_cache[str(csv_file.resolve())] is wrapper

    async def test_second_call_returns_the_same_wrapper(self, csv_file: Path) -> None:
        first = await get_file_datasource(str(csv_file), "csv")
        second = await get_file_datasource(str(csv_file), "csv")

        assert first is second
        assert len(db_utils._file_cache) == 1

    async def test_cache_is_keyed_by_resolved_path(
        self, csv_file: Path, tmp_path: Path
    ) -> None:
        """A relative path and an absolute path to the same file must share one
        cache entry, or the circuit breaker would track them separately."""
        direct = await get_file_datasource(str(csv_file), "csv")
        indirect = await get_file_datasource(
            str(tmp_path / "." / csv_file.name), "csv"
        )

        assert direct is indirect

    async def test_refreshes_last_used_on_a_cache_hit(self, csv_file: Path) -> None:
        wrapper = await get_file_datasource(str(csv_file), "csv")
        wrapper.last_used = 0.0

        await get_file_datasource(str(csv_file), "csv")

        assert wrapper.last_used > 0.0

    async def test_auto_infers_the_type_from_the_extension(
        self, parquet_file: Path
    ) -> None:
        wrapper = await get_file_datasource(str(parquet_file), "auto")
        assert wrapper.file_type == "parquet"

    @pytest.mark.parametrize("alias", ["xls", "xlsx", "XLSX", " Excel "])
    async def test_excel_aliases_normalise_to_excel(
        self, tmp_path: Path, alias: str
    ) -> None:
        """Type normalisation never opens the file, so this needs no openpyxl —
        only a real path for ``_resolve_safe_path`` to resolve."""
        path = tmp_path / "book.xlsx"
        path.write_bytes(b"placeholder")

        wrapper = await get_file_datasource(str(path), alias)
        assert wrapper.file_type == "excel"

    async def test_rejects_path_traversal(self, csv_file: Path) -> None:
        with pytest.raises(ValueError, match="Path traversal detected"):
            await get_file_datasource(f"{csv_file.parent}/../etc/passwd", "csv")

    async def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            await get_file_datasource(str(tmp_path / "absent.csv"), "csv")

    async def test_unsupported_type_raises(self, csv_file: Path) -> None:
        with pytest.raises(ValueError, match="Unsupported file type"):
            await get_file_datasource(str(csv_file), "xml")

    async def test_an_open_circuit_blocks_access(self, csv_file: Path) -> None:
        wrapper = await get_file_datasource(str(csv_file), "csv")
        wrapper.circuit_open_until = time.time() + 60

        with pytest.raises(Exception, match="circuit open"):
            await get_file_datasource(str(csv_file), "csv")

    async def test_an_expired_circuit_allows_access_again(self, csv_file: Path) -> None:
        wrapper = await get_file_datasource(str(csv_file), "csv")
        wrapper.circuit_open_until = time.time() - 1

        assert await get_file_datasource(str(csv_file), "csv") is wrapper


class TestCloseFileDatasource:
    async def test_evicts_the_wrapper(self, csv_file: Path) -> None:
        await get_file_datasource(str(csv_file), "csv")
        await close_file_datasource(str(csv_file))

        assert db_utils._file_cache == {}

    async def test_is_safe_when_the_path_was_never_cached(self, tmp_path: Path) -> None:
        """Resolves without touching the filesystem, so it works even for a
        path that no longer exists — a cleanup path must not raise."""
        await close_file_datasource(str(tmp_path / "never_seen.csv"))

    async def test_only_evicts_the_named_path(
        self, csv_file: Path, parquet_file: Path
    ) -> None:
        await get_file_datasource(str(csv_file), "csv")
        await get_file_datasource(str(parquet_file), "parquet")

        await close_file_datasource(str(csv_file))

        assert list(db_utils._file_cache) == [str(parquet_file.resolve())]


# ---------------------------------------------------------------------------
# Circuit-breaker helpers (the typed, working ones)
# ---------------------------------------------------------------------------
class TestFileCircuitBreaker:
    @pytest.fixture
    def wrapper(self) -> FileDataSourceWrapper:
        return FileDataSourceWrapper(path="/tmp/x.csv", file_type="csv", last_used=0.0)

    async def test_failures_accumulate_below_the_limit(self, wrapper) -> None:  # noqa: ANN001
        for _ in range(db_utils._FILE_CIRCUIT_FAILURE_LIMIT - 1):
            await _file_register_failure(wrapper)

        assert wrapper.failures == db_utils._FILE_CIRCUIT_FAILURE_LIMIT - 1
        assert wrapper.circuit_open_until is None

    async def test_reaching_the_limit_opens_the_circuit_and_resets_the_count(
        self, wrapper  # noqa: ANN001
    ) -> None:
        before = time.time()
        for _ in range(db_utils._FILE_CIRCUIT_FAILURE_LIMIT):
            await _file_register_failure(wrapper)

        assert wrapper.failures == 0
        assert wrapper.circuit_open_until is not None
        assert wrapper.circuit_open_until >= before + db_utils._FILE_CIRCUIT_RESET_SECONDS

    async def test_success_clears_failure_state(self, wrapper) -> None:  # noqa: ANN001
        wrapper.failures = 3
        wrapper.circuit_open_until = time.time() + 60

        await _file_register_success(wrapper)

        assert wrapper.failures == 0
        assert wrapper.circuit_open_until is None

    def test_the_file_limits_are_ints_unlike_the_rdbms_ones(self) -> None:
        """
        The file section casts its config to int at import time. The original
        RDBMS constants do not, which is the bug recorded in
        tests/unit/db/test_db_utils_pool.py — this asserts the file half is
        genuinely type-safe rather than accidentally working.
        """
        assert isinstance(db_utils._FILE_CIRCUIT_FAILURE_LIMIT, int)
        assert isinstance(db_utils._FILE_CIRCUIT_RESET_SECONDS, int)
        assert isinstance(db_utils._FILE_TTL_SECONDS, int)


# ---------------------------------------------------------------------------
# test_file_connection
# ---------------------------------------------------------------------------
class TestTestFileConnection:
    @pytest.mark.parametrize("file_type", ALL_TYPES)
    async def test_succeeds_for_every_supported_format(
        self, every_format: dict, file_type: str
    ) -> None:
        assert await check_file_connection(str(every_format[file_type]), file_type) is True

    async def test_jsonl_is_detected_and_read(self, jsonl_file: Path) -> None:
        assert await check_file_connection(str(jsonl_file), "json") is True

    async def test_returns_false_rather_than_raising_for_a_missing_file(
        self, tmp_path: Path
    ) -> None:
        """Contrast with the RDBMS path, which raises TypeError here — this one
        genuinely honours its documented bool contract."""
        assert await check_file_connection(str(tmp_path / "absent.csv"), "csv") is False

    async def test_returns_false_for_an_unsupported_type(self, csv_file: Path) -> None:
        assert await check_file_connection(str(csv_file), "xml") is False

    async def test_returns_false_for_traversal(self, csv_file: Path) -> None:
        assert await check_file_connection(f"{csv_file.parent}/../x.csv", "csv") is False

    async def test_a_corrupt_file_returns_false_and_records_a_failure(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "broken.parquet"
        path.write_bytes(b"this is not parquet")

        assert await check_file_connection(str(path), "parquet") is False
        assert db_utils._file_cache[str(path.resolve())].failures == 1

    async def test_a_success_after_failures_clears_them(self, csv_file: Path) -> None:
        wrapper = await get_file_datasource(str(csv_file), "csv")
        wrapper.failures = 2

        assert await check_file_connection(str(csv_file), "csv") is True
        assert wrapper.failures == 0

    async def test_repeated_failures_eventually_open_the_circuit(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "broken.parquet"
        path.write_bytes(b"nope")

        for _ in range(db_utils._FILE_CIRCUIT_FAILURE_LIMIT):
            assert await check_file_connection(str(path), "parquet") is False

        wrapper = db_utils._file_cache[str(path.resolve())]
        assert wrapper.circuit_open_until is not None
        # With the circuit open, get_file_datasource raises and the caller is
        # handed False rather than an exception.
        assert await check_file_connection(str(path), "parquet") is False


# ---------------------------------------------------------------------------
# fetch_file_schema
# ---------------------------------------------------------------------------
class TestFetchFileSchema:
    @pytest.mark.parametrize("file_type", ALL_TYPES)
    async def test_returns_a_column_and_type_for_each_field(
        self, every_format: dict, file_type: str
    ) -> None:
        schema = await fetch_file_schema(str(every_format[file_type]), file_type)

        assert [entry["column"] for entry in schema] == ["id", "name", "price"]
        assert all(isinstance(entry["type"], str) and entry["type"] for entry in schema)

    async def test_csv_types_are_inferred_not_all_strings(self, csv_file: Path) -> None:
        schema = await fetch_file_schema(str(csv_file), "csv")
        by_column = {entry["column"]: entry["type"] for entry in schema}

        assert by_column["id"] == "int64"
        assert by_column["price"] == "float64"

    async def test_parquet_schema_comes_from_metadata(self, parquet_file: Path) -> None:
        schema = await fetch_file_schema(str(parquet_file), "parquet")
        by_column = {entry["column"]: entry["type"] for entry in schema}

        assert by_column["id"] == "int64"
        # pyarrow reports either "string" or "large_string" depending on the
        # version's default string type; both are the same logical column type.
        assert by_column["name"] in ("string", "large_string")

    async def test_avro_unions_collapse_to_the_non_null_member(
        self, avro_file: Path
    ) -> None:
        """``["null", "string"]`` must report as ``string`` — reporting the
        union verbatim would break the prompt that describes the schema to the
        model."""
        schema = await fetch_file_schema(str(avro_file), "avro")
        by_column = {entry["column"]: entry["type"] for entry in schema}

        assert by_column["name"] == "string"
        assert by_column["id"] == "int"

    async def test_jsonl_schema(self, jsonl_file: Path) -> None:
        schema = await fetch_file_schema(str(jsonl_file), "json")
        assert [entry["column"] for entry in schema] == ["id", "name", "price"]

    async def test_a_corrupt_file_raises_and_records_a_failure(
        self, tmp_path: Path
    ) -> None:
        """Unlike test_file_connection, schema reads propagate the error — the
        caller needs to know why, not just that it failed."""
        path = tmp_path / "broken.parquet"
        path.write_bytes(b"nope")

        with pytest.raises(Exception):
            await fetch_file_schema(str(path), "parquet")

        assert db_utils._file_cache[str(path.resolve())].failures == 1

    async def test_a_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            await fetch_file_schema(str(tmp_path / "absent.csv"), "csv")


# ---------------------------------------------------------------------------
# fetch_file_preview
# ---------------------------------------------------------------------------
class TestFetchFilePreview:
    @pytest.mark.parametrize("file_type", ALL_TYPES)
    async def test_returns_row_dicts_for_every_format(
        self, every_format: dict, file_type: str
    ) -> None:
        rows = await fetch_file_preview(str(every_format[file_type]), file_type)

        assert len(rows) == 3
        assert [row["name"] for row in rows] == ["Widget", "Gadget", "Doohickey"]

    @pytest.mark.parametrize("file_type", ALL_TYPES)
    async def test_limit_caps_the_row_count(
        self, every_format: dict, file_type: str
    ) -> None:
        rows = await fetch_file_preview(str(every_format[file_type]), file_type, limit=2)
        assert len(rows) == 2

    async def test_default_limit_is_fifty(self, tmp_path: Path) -> None:
        path = tmp_path / "many.csv"
        pd.DataFrame({"n": range(120)}).to_csv(path, index=False)

        assert len(await fetch_file_preview(str(path), "csv")) == 50

    async def test_limit_larger_than_the_file_returns_everything(
        self, csv_file: Path
    ) -> None:
        assert len(await fetch_file_preview(str(csv_file), "csv", limit=999)) == 3

    async def test_an_empty_parquet_file_previews_as_no_rows(
        self, tmp_path: Path
    ) -> None:
        """iter_batches raises StopIteration on an empty file; the handler turns
        that into an empty list rather than letting it escape."""
        path = tmp_path / "empty.parquet"
        pd.DataFrame({"id": pd.Series([], dtype="int64")}).to_parquet(path, index=False)

        assert await fetch_file_preview(str(path), "parquet") == []

    async def test_jsonl_preview(self, jsonl_file: Path) -> None:
        rows = await fetch_file_preview(str(jsonl_file), "json", limit=2)
        assert [row["id"] for row in rows] == [1, 2]

    async def test_values_round_trip(self, csv_file: Path) -> None:
        rows = await fetch_file_preview(str(csv_file), "csv")
        assert rows[0] == {"id": 1, "name": "Widget", "price": 9.99}

    async def test_a_corrupt_file_raises_and_records_a_failure(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "broken.parquet"
        path.write_bytes(b"nope")

        with pytest.raises(Exception):
            await fetch_file_preview(str(path), "parquet")

        assert db_utils._file_cache[str(path.resolve())].failures == 1


# ---------------------------------------------------------------------------
# fetch_file_listing
# ---------------------------------------------------------------------------
class TestFetchFileListing:
    @pytest.mark.parametrize("file_type", ALL_TYPES)
    async def test_non_excel_formats_list_the_file_itself(
        self, every_format: dict, file_type: str
    ) -> None:
        listing = await fetch_file_listing(str(every_format[file_type]), file_type)
        assert listing == [every_format[file_type].name]

    @requires_openpyxl
    async def test_excel_lists_every_sheet(self, excel_file: Path) -> None:
        """This is the one format where a single file exposes several logical
        tables, which is why the abstraction exists at all."""
        listing = await fetch_file_listing(str(excel_file), "excel")
        assert listing == ["products", "summary"]

    @requires_openpyxl
    async def test_a_corrupt_workbook_raises_and_records_a_failure(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "broken.xlsx"
        path.write_bytes(b"not a workbook")

        with pytest.raises(Exception):
            await fetch_file_listing(str(path), "excel")

        assert db_utils._file_cache[str(path.resolve())].failures == 1

    def test_the_excel_engine_is_installed(self) -> None:
        """
        Regression test for a fixed defect.

        pandas needs ``openpyxl`` to read .xlsx, and it was declared in no
        requirements file — so every Excel upload failed at runtime with
        ModuleNotFoundError despite XLSX being an advertised datasource type.

        This asserts the dependency is present rather than asserting on
        behaviour, because the failure mode was an import error at the point of
        use: nothing in the application's own code changed, only what is
        installed alongside it. If this fails, the Excel tests above are silently
        skipping and Excel uploads are broken again.
        """
        assert importlib.util.find_spec("openpyxl") is not None, (
            "openpyxl is missing — Excel uploads will fail at runtime. "
            "It is declared in requirements.txt; the environment is out of date."
        )

    async def test_success_clears_prior_failures(self, csv_file: Path) -> None:
        wrapper = await get_file_datasource(str(csv_file), "csv")
        wrapper.failures = 4

        await fetch_file_listing(str(csv_file), "csv")

        assert wrapper.failures == 0


# ---------------------------------------------------------------------------
# cleanup_idle_file_datasources
# ---------------------------------------------------------------------------
class TestCleanupIdleFileDatasources:
    async def test_evicts_only_entries_idle_past_the_ttl(
        self, csv_file: Path, parquet_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        The real loop sleeps 300s before its first pass, so it is driven here by
        stubbing asyncio.sleep to run exactly one iteration and then break out.
        """
        fresh = await get_file_datasource(str(csv_file), "csv")
        stale = await get_file_datasource(str(parquet_file), "parquet")
        stale.last_used = time.time() - (db_utils._FILE_TTL_SECONDS + 60)

        calls = {"n": 0}

        async def one_pass_then_stop(seconds: float) -> None:
            calls["n"] += 1
            if calls["n"] > 1:
                raise asyncio.CancelledError
            assert seconds == 300

        monkeypatch.setattr(db_utils.asyncio, "sleep", one_pass_then_stop)

        with pytest.raises(asyncio.CancelledError):
            await db_utils.cleanup_idle_file_datasources()

        assert str(parquet_file.resolve()) not in db_utils._file_cache
        assert db_utils._file_cache[str(csv_file.resolve())] is fresh


# ---------------------------------------------------------------------------
# fetch_tables_or_collections_or_files
# ---------------------------------------------------------------------------
class TestUnifiedListing:
    async def test_file_source_delegates_to_fetch_file_listing(
        self, csv_file: Path
    ) -> None:
        listing = await fetch_tables_or_collections_or_files(
            "file", path=str(csv_file), file_type="csv"
        )
        assert listing == ["products.csv"]

    @requires_openpyxl
    async def test_file_source_returns_excel_sheet_names(self, excel_file: Path) -> None:
        listing = await fetch_tables_or_collections_or_files(
            "file", path=str(excel_file), file_type="excel"
        )
        assert listing == ["products", "summary"]

    async def test_rdbms_source_delegates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict = {}

        async def fake(url: str, db_type: str):  # noqa: ANN202
            captured.update({"url": url, "db_type": db_type})
            return ["orders"]

        monkeypatch.setattr(db_utils, "fetch_rdbms_tables", fake)

        result = await fetch_tables_or_collections_or_files(
            "rdbms", url="postgresql+asyncpg://h/d", db_type="postgres"
        )

        assert result == ["orders"]
        assert captured == {"url": "postgresql+asyncpg://h/d", "db_type": "postgres"}

    async def test_mongo_source_delegates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake(uri: str, database: str):  # noqa: ANN202
            return ["events"]

        monkeypatch.setattr(db_utils, "fetch_mongo_collections", fake)

        result = await fetch_tables_or_collections_or_files(
            "mongo", uri="mongodb://h:1", database="app"
        )
        assert result == ["events"]

    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"source_type": "rdbms"}, "'url' and 'db_type' are required"),
            ({"source_type": "rdbms", "url": "x"}, "'url' and 'db_type' are required"),
            ({"source_type": "mongo"}, "'uri' and 'database' are required"),
            ({"source_type": "mongo", "uri": "x"}, "'uri' and 'database' are required"),
            ({"source_type": "file"}, "'path' and 'file_type' are required"),
            ({"source_type": "file", "path": "x"}, "'path' and 'file_type' are required"),
        ],
    )
    async def test_missing_required_kwargs_raise_a_named_error(
        self, kwargs: dict, message: str
    ) -> None:
        with pytest.raises(ValueError, match=message):
            await fetch_tables_or_collections_or_files(**kwargs)

    @pytest.mark.parametrize("source_type", ["", "sql", "RDBMS", "s3", "elasticsearch"])
    async def test_an_unknown_source_type_is_rejected(self, source_type: str) -> None:
        with pytest.raises(ValueError, match="Unknown source_type"):
            await fetch_tables_or_collections_or_files(source_type)
