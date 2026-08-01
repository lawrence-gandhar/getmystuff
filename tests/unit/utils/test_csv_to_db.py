"""
Tests for app/utils/csv_to_db.py — the bulk CSV → PostgreSQL seeding script.

Like csv_to_parquet, this module was invisible to coverage rather than merely
uncovered: nothing in the application imports it, and coverage's source scan
skips app/utils (no __init__.py), so it never appeared in the report at all.
See documentations/TESTING.md.

The module targets PostgreSQL specifically — ``copy_chunk`` drives
``cursor.copy_expert``, which is a psycopg2 extension with no SQLite
equivalent. The DB-API layer is therefore faked here rather than mocked away
wholesale: the fake records the exact COPY statement and payload it is handed,
so the tests still assert what the module would actually send to Postgres.

``get_engine`` is never allowed to connect. It is only asserted on for the URL
it builds; ``seed_folder`` gets a patched factory.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from app.utils import csv_to_db
from app.utils.csv_to_db import (
    clean_column,
    copy_chunk,
    create_table,
    get_engine,
    process_csv_file,
    seed_folder,
)


# ---------------------------------------------------------------------------
# Fakes for the psycopg2 DB-API surface copy_chunk drives
# ---------------------------------------------------------------------------
class FakeCursor:
    def __init__(self, sink: list, fail: bool = False) -> None:
        self._sink = sink
        self._fail = fail

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *exc) -> None:  # noqa: ANN002
        return None

    def copy_expert(self, sql: str, buffer) -> None:  # noqa: ANN001
        if self._fail:
            raise RuntimeError("COPY exploded")
        self._sink.append({"sql": sql, "payload": buffer.read()})


class FakeRawConnection:
    def __init__(self, sink: list, fail: bool = False) -> None:
        self._sink = sink
        self._fail = fail
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def cursor(self) -> FakeCursor:
        return FakeCursor(self._sink, self._fail)

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        self.closed = True


class FakeEngine:
    """Just enough of an Engine for copy_chunk / process_csv_file."""

    def __init__(self, fail_copy: bool = False) -> None:
        self.copies: list = []
        self.created_tables: list = []
        self.disposed = False
        self._fail_copy = fail_copy
        self.connection = FakeRawConnection(self.copies, fail_copy)

    def raw_connection(self) -> FakeRawConnection:
        # A fresh handle per call, but recorded so assertions can reach it.
        self.connection = FakeRawConnection(self.copies, self._fail_copy)
        return self.connection

    def dispose(self) -> None:
        self.disposed = True


# ---------------------------------------------------------------------------
# clean_column
# ---------------------------------------------------------------------------
class TestCleanColumn:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("  Flight Date  ", "flight_date"),
            ("ARRIVAL-DELAY", "arrival_delay"),
            ("Tail Number", "tail_number"),
            ("already_clean", "already_clean"),
            ("MIXED-Case Name", "mixed_case_name"),
            ("", ""),
        ],
    )
    def test_normalizes(self, raw: str, expected: str) -> None:
        assert clean_column(raw) == expected

    def test_does_not_strip_characters_outside_its_rules(self) -> None:
        """
        Recorded rather than desired: clean_column only handles whitespace,
        case, spaces and dashes. Quotes, slashes and parentheses survive into
        the generated DDL, where they are quoted rather than rejected.
        """
        assert clean_column("Delay (mins)/Total") == "delay_(mins)/total"


# ---------------------------------------------------------------------------
# get_engine
# ---------------------------------------------------------------------------
class TestGetEngine:
    def test_builds_a_psycopg2_url_from_db_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No connection is attempted — create_engine is lazy — but the URL is
        the whole contract of this function, so it is asserted directly."""
        captured: dict = {}

        def fake_create_engine(url: str, **kwargs):  # noqa: ANN003, ANN202
            captured["url"] = url
            captured["kwargs"] = kwargs
            return FakeEngine()

        monkeypatch.setattr(csv_to_db, "create_engine", fake_create_engine)

        engine = get_engine()

        assert isinstance(engine, FakeEngine)
        assert captured["url"] == (
            "postgresql+psycopg2://postgres:123@localhost:5432/flight_delays_cancelation"
        )
        assert captured["kwargs"] == {
            "pool_size": 10,
            "max_overflow": 20,
            "pool_pre_ping": True,
        }


# ---------------------------------------------------------------------------
# create_table
# ---------------------------------------------------------------------------
class TestCreateTable:
    def test_creates_a_real_table_with_every_column_as_text(self, tmp_path: Path) -> None:
        """Run against a real SQLite engine so the generated DDL is proven to
        parse, not just string-matched."""
        from sqlalchemy import create_engine as real_create_engine, inspect

        engine = real_create_engine(f"sqlite:///{tmp_path / 'seed.db'}")
        create_table(engine, "flights", ["year", "month", "tail_number"])

        columns = inspect(engine).get_columns("flights")
        assert [c["name"] for c in columns] == ["year", "month", "tail_number"]
        assert all("TEXT" in str(c["type"]).upper() for c in columns)

    def test_is_idempotent(self, tmp_path: Path) -> None:
        """CREATE TABLE IF NOT EXISTS — a second call on an existing table is a
        no-op, which is what makes re-running the seeder safe."""
        from sqlalchemy import create_engine as real_create_engine

        engine = real_create_engine(f"sqlite:///{tmp_path / 'seed.db'}")
        create_table(engine, "flights", ["year"])
        create_table(engine, "flights", ["year"])

    def test_quotes_identifiers(self, tmp_path: Path) -> None:
        """Column names that are SQL keywords or contain spaces must survive,
        which they only do because the DDL double-quotes every identifier."""
        from sqlalchemy import create_engine as real_create_engine, inspect

        engine = real_create_engine(f"sqlite:///{tmp_path / 'seed.db'}")
        create_table(engine, "odd table", ["select", "group by"])

        columns = [c["name"] for c in inspect(engine).get_columns("odd table")]
        assert columns == ["select", "group by"]


# ---------------------------------------------------------------------------
# copy_chunk
# ---------------------------------------------------------------------------
class TestCopyChunk:
    def test_sends_headerless_csv_via_copy_and_commits(self) -> None:
        engine = FakeEngine()
        frame = pd.DataFrame({"a": [1, 2], "b": ["x", "y"]})

        copy_chunk(engine, "flights", frame)

        assert len(engine.copies) == 1
        assert engine.copies[0]["sql"] == 'COPY "flights" FROM STDIN WITH CSV'
        # Header suppressed and index omitted — both matter, because COPY maps
        # by position and would otherwise insert the header as a data row.
        assert engine.copies[0]["payload"] == "1,x\n2,y\n"
        assert engine.connection.committed is True
        assert engine.connection.closed is True

    def test_rolls_back_and_re_raises_when_copy_fails(self) -> None:
        """No silent failures: the exception propagates to the caller after the
        transaction is rolled back and the connection returned to the pool."""
        engine = FakeEngine(fail_copy=True)
        frame = pd.DataFrame({"a": [1]})

        with pytest.raises(RuntimeError, match="COPY exploded"):
            copy_chunk(engine, "flights", frame)

        assert engine.connection.rolled_back is True
        assert engine.connection.committed is False
        assert engine.connection.closed is True

    def test_an_empty_frame_still_closes_the_connection(self) -> None:
        engine = FakeEngine()
        copy_chunk(engine, "flights", pd.DataFrame({"a": []}))

        assert engine.copies[0]["payload"] == ""
        assert engine.connection.closed is True


# ---------------------------------------------------------------------------
# process_csv_file
# ---------------------------------------------------------------------------
class TestProcessCsvFile:
    def test_derives_the_table_name_from_the_filename(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        created: list = []
        monkeypatch.setattr(
            csv_to_db, "create_table", lambda e, t, c: created.append((t, c))
        )

        source = tmp_path / "Flights_2015.csv"
        source.write_text("Year,Tail Number\n2015,N123\n")

        process_csv_file(FakeEngine(), str(source))

        assert created[0][0] == "flights_2015"

    def test_cleans_column_names_before_creating_the_table(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        created: list = []
        monkeypatch.setattr(
            csv_to_db, "create_table", lambda e, t, c: created.append((t, c))
        )

        source = tmp_path / "flights.csv"
        source.write_text("Flight Date,ARRIVAL-DELAY\n2015-01-01,5\n")

        process_csv_file(FakeEngine(), str(source))

        assert created[0][1] == ["flight_date", "arrival_delay"]

    def test_creates_the_table_once_then_copies_every_chunk(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The first-chunk flag is the only thing stopping a CREATE per chunk;
        with CHUNK_SIZE forced to 2 a 5-row file makes 3 chunks and 1 CREATE."""
        monkeypatch.setattr(csv_to_db, "CHUNK_SIZE", 2)
        created: list = []
        monkeypatch.setattr(
            csv_to_db, "create_table", lambda e, t, c: created.append(t)
        )

        source = tmp_path / "flights.csv"
        source.write_text("a\n" + "".join(f"{i}\n" for i in range(5)))

        engine = FakeEngine()
        process_csv_file(engine, str(source))

        assert len(created) == 1
        assert len(engine.copies) == 3

    def test_re_raises_when_the_file_cannot_be_read(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            process_csv_file(FakeEngine(), str(tmp_path / "missing.csv"))

    def test_re_raises_when_a_chunk_fails_to_load(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(csv_to_db, "create_table", lambda *a: None)
        source = tmp_path / "flights.csv"
        source.write_text("a\n1\n")

        with pytest.raises(RuntimeError, match="COPY exploded"):
            process_csv_file(FakeEngine(fail_copy=True), str(source))


# ---------------------------------------------------------------------------
# seed_folder
# ---------------------------------------------------------------------------
class TestSeedFolder:
    def test_processes_only_csv_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine = FakeEngine()
        monkeypatch.setattr(csv_to_db, "get_engine", lambda: engine)
        processed: list = []
        monkeypatch.setattr(
            csv_to_db, "process_csv_file", lambda e, p: processed.append(Path(p).name)
        )

        (tmp_path / "flights.csv").write_text("a\n1\n")
        (tmp_path / "airports.csv").write_text("a\n1\n")
        (tmp_path / "notes.txt").write_text("ignored")
        (tmp_path / "archive.csv.gz").write_bytes(b"ignored")

        seed_folder(str(tmp_path))

        assert sorted(processed) == ["airports.csv", "flights.csv"]

    def test_disposes_the_engine_on_success(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine = FakeEngine()
        monkeypatch.setattr(csv_to_db, "get_engine", lambda: engine)
        monkeypatch.setattr(csv_to_db, "process_csv_file", lambda e, p: None)

        seed_folder(str(tmp_path))

        assert engine.disposed is True

    def test_disposes_the_engine_even_when_a_file_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The finally block is the only thing returning the pool's connections
        on the error path — without it a failed seed leaks them."""
        engine = FakeEngine()
        monkeypatch.setattr(csv_to_db, "get_engine", lambda: engine)

        def boom(e, p) -> None:  # noqa: ANN001
            raise ValueError("bad file")

        monkeypatch.setattr(csv_to_db, "process_csv_file", boom)
        (tmp_path / "flights.csv").write_text("a\n1\n")

        with pytest.raises(ValueError, match="bad file"):
            seed_folder(str(tmp_path))

        assert engine.disposed is True

    def test_re_raises_when_the_folder_does_not_exist(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine = FakeEngine()
        monkeypatch.setattr(csv_to_db, "get_engine", lambda: engine)

        with pytest.raises(FileNotFoundError):
            seed_folder(str(tmp_path / "nope"))

        assert engine.disposed is True


class TestModuleConfiguration:
    def test_csv_folder_default_is_a_hardcoded_windows_path(self) -> None:
        """
        Pinned as a finding, not an endorsement. CSV_FOLDER points at one
        developer's Downloads directory, so the ``__main__`` entry point cannot
        run anywhere else. Documented in TESTING.md; changing it is an
        application fix, not a test fix.
        """
        assert csv_to_db.CSV_FOLDER.startswith("C:\\Users")

    def test_chunk_size_is_set(self) -> None:
        assert csv_to_db.CHUNK_SIZE == 50000
