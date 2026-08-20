"""
Fixtures for the Recursive DataFrame Agents tests.

The datasource is a real SQLite file and the aggregate is compared against what
SQLite itself says, because that is the actual promise: the same numbers the
database would have given, arrived at by reading the records in batches. Mocking
the reader would only prove the graph calls it.

Deliberately no checkpointer fixture, unlike ``tests/unit/services/downloader_agents``:
this graph compiles without one — it never interrupts and never resumes across a
request — so a copied checkpointer fixture would be a store nothing writes to.
"""

from __future__ import annotations

import sqlite3
import uuid as uuid_pkg
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List

import pytest


@pytest.fixture
def make_sales_db(tmp_path: Path) -> Callable:
    """
    A real SQLite database of sales, with both null traps in it.

    The region cycles on 3 and the missing amounts on 4, so no group is
    accidentally all-null; ``west`` is added separately and is, because "SUM over
    a group with no values" has to be exercised deliberately rather than hoped for.
    """

    def _make(rows: int, regions: int = 3) -> Path:
        path = tmp_path / f"sales_{rows}_{uuid_pkg.uuid4().hex[:8]}.db"
        names = ["north", "south", "east", "central", "coastal"][:max(1, regions)]

        records = [
            (
                index,
                names[index % len(names)],
                None if index % 4 == 0 else float(index),
                f"2026-01-{(index % 28) + 1:02d}",
            )
            for index in range(1, rows + 1)
        ]
        # A group whose amounts are all missing: SUM must come back NULL, not 0.
        records.extend(
            (rows + offset, "west", None, "2026-02-01") for offset in range(1, 5)
        )

        connection = sqlite3.connect(path)
        connection.executescript(
            """
            CREATE TABLE sales (
                id       INTEGER PRIMARY KEY,
                region   TEXT NOT NULL,
                amount   REAL,
                sold_on  TEXT NOT NULL
            );
            """
        )
        connection.executemany(
            "INSERT INTO sales (id, region, amount, sold_on) VALUES (?, ?, ?, ?)",
            records,
        )
        connection.commit()
        connection.close()

        return path

    return _make


@pytest.fixture
def sales_datasource(make_sales_db: Callable) -> Callable:
    """
    A ``DataSource``-shaped object the reader can connect through.

    A ``SimpleNamespace`` because the reader and the executor read attributes off
    whatever they are handed and never query the application database for them.
    """

    def _make(rows: int = 500, regions: int = 3) -> SimpleNamespace:
        return SimpleNamespace(
            db_type="sqlite",
            database_name=str(make_sales_db(rows, regions)),
            datasource_name="warehouse",
            host=None,
            port=None,
            username=None,
            password_encrypted=None,
            configuration_data={},
        )

    return _make


@pytest.fixture
def tool_entry(sales_datasource: Callable) -> Callable:
    """
    One tool as ``collect_agent_tools`` shapes it — the only shape this feature reads.

    ``config`` empty means "every active column", which is what makes the SQL-mode
    and builder-mode paths return the same three columns to group over.
    """

    def _make(
        rows: int = 500,
        regions: int = 3,
        sql_query: str | None = None,
        name: str = "sales_records",
    ) -> Dict[str, Any]:
        return {
            "uuid": str(uuid_pkg.uuid4()),
            "id": 1,
            "data_agent_id": 1,
            "tool_name": name,
            "description": "Every sale, one row each",
            "table_name": "sales",
            "table_names": ["sales"],
            "query_mode": "sql" if sql_query else "builder",
            "config": {},
            "sql_query": sql_query,
            "datasource": sales_datasource(rows, regions),
            "datasource_name": "warehouse",
            "db_type": "sqlite",
            "chain": None,
            "allow_recursive_aggregate": True,
        }

    return _make


@pytest.fixture
def sales_rows() -> Callable:
    """The records a sales database holds, for comparing against SQLite directly."""

    def _read(datasource: SimpleNamespace) -> List[dict]:
        connection = sqlite3.connect(datasource.database_name)
        connection.row_factory = sqlite3.Row
        rows = [dict(row) for row in connection.execute("SELECT * FROM sales")]
        connection.close()

        return rows

    return _read


@pytest.fixture
def sqlite_answer() -> Callable:
    """The same question put to SQLite, which is the standard being met."""

    def _ask(datasource: SimpleNamespace, sql: str) -> List[dict]:
        connection = sqlite3.connect(datasource.database_name)
        connection.row_factory = sqlite3.Row
        rows = [dict(row) for row in connection.execute(sql)]
        connection.close()

        return rows

    return _ask


@pytest.fixture(autouse=True)
def _registries_start_and_end_empty():
    """
    Both module registries are empty before and after every test.

    Autouse and asserting on the way out as well as in, because the failure this
    catches — a run that left a cursor checked out of the pool — does not fail the
    test that caused it, it fails whichever test runs next.
    """
    from app.services.agent_recursive_dataframes import frame_buffer
    from app.services.downloader_agents.base import record_reader

    frame_buffer.release_all()
    record_reader._readers.clear()

    yield

    assert frame_buffer.open_keys() == 0, "an aggregation left records behind"
    assert not record_reader._readers, "an aggregation left a cursor open"
