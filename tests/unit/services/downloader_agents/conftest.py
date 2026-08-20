"""
Fixtures shared by the Downloader Agents tests.

The whole feature turns rows in somebody else's database into a file on disk, so both
ends of it are real here: a genuine SQLite database in ``tmp_path`` standing in for the
user's datasource (the same approach
``tests/unit/services/deep_agents/test_query_executor.py`` takes), and the in-memory
application database from ``tests/conftest.py`` for the export rows.

Nothing is mocked that produces or consumes data. Mocking the writers would test that the
graph calls them; running them proves an export of 125 records contains 125 records, which
is the only claim anybody cares about.
"""

from __future__ import annotations

import sqlite3
import uuid as uuid_pkg
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

import pytest

from app.db.db_utils import CRUDQueryBuilder
from app.models.data_agents import DataAgent
from app.models.datasource import DataSource
from app.models.tool_configs import ToolConfig

datasource_crud = CRUDQueryBuilder(DataSource)
agent_crud = CRUDQueryBuilder(DataAgent)
tool_crud = CRUDQueryBuilder(ToolConfig)


@pytest.fixture
def graph_sessions(background_sessions):  # noqa: ANN001, ANN201
    """
    The shared ``background_sessions`` fixture, under the name these tests read by.

    Defined in tests/conftest.py because the progress SSE route needs it too, and a second
    copy is a second thing to keep correct.
    """
    return background_sessions


@pytest.fixture
def graph_checkpointer(monkeypatch: pytest.MonkeyPatch):  # noqa: ANN001, ANN201
    """
    A fresh in-memory checkpointer per test, never the PostgreSQL one.

    Two separate problems this solves, both discovered the hard way.

    ``checkpointer.get_checkpointer`` picks its store from ``DATABASE_URL``, and in the
    container that variable is the *development* PostgreSQL database — so without this a
    test would write real checkpoint rows into it.

    And the saver is cached in a module global. ``AsyncPostgresSaver`` holds an
    ``asyncio.Lock``, which binds to the event loop that created it; pytest-asyncio gives
    each test its own loop, so the second test to use a cached saver fails inside
    ``asyncio.locks`` on a loop that no longer exists. Clearing the global per test is what
    makes each one get a saver belonging to its own loop.
    """
    from app.services.downloader_agents.base import checkpointer

    monkeypatch.setattr(checkpointer, "_saver", None)
    monkeypatch.setattr(checkpointer, "_pool", None)
    # Returning None is what selects InMemorySaver — see get_checkpointer.
    monkeypatch.setattr(checkpointer, "postgres_dsn", lambda *args, **kwargs: None)

    yield

    monkeypatch.setattr(checkpointer, "_saver", None)


@pytest.fixture
def make_source_db(tmp_path: Path) -> Callable:  # noqa: ANN001
    """
    A real SQLite database holding ``rows`` numbered records.

    A factory rather than a fixed fixture because the interesting cases are the batch
    boundaries — 49, 50, 51, 100 — and each needs its own row count.
    """

    def _make(rows: int, name: str = "source") -> Path:
        # Unique per call, not per row count: a test that queues two exports asks for two
        # databases of the same size, and reusing the path would try to create `items`
        # twice in the same file.
        path = tmp_path / f"{name}_{rows}_{uuid_pkg.uuid4().hex[:8]}.db"

        connection = sqlite3.connect(path)
        connection.executescript(
            """
            CREATE TABLE items (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                qty INTEGER
            );
            """
        )
        connection.executemany(
            "INSERT INTO items (id, name, qty) VALUES (?, ?, ?)",
            [(index, f"n{index}", index % 7) for index in range(1, rows + 1)],
        )
        connection.commit()
        connection.close()

        return path

    return _make


@pytest.fixture
def datasource_row(make_source_db: Callable) -> Callable:  # noqa: ANN001
    """
    A ``DataSource``-shaped object the reader can connect through.

    A ``SimpleNamespace`` where only the reader is under test — it reads five attributes
    off whatever it is handed and never queries the application database for them.
    """

    def _make(rows: int = 125) -> SimpleNamespace:
        return SimpleNamespace(
            db_type="sqlite",
            database_name=str(make_source_db(rows)),
            datasource_name="warehouse",
            host=None,
            port=None,
            username=None,
            password_encrypted=None,
            configuration_data={},
        )

    return _make


@pytest.fixture
def make_export_fixtures(db, user, make_source_db: Callable) -> Callable:  # noqa: ANN001
    """
    A persisted agent + datasource + tool config, ready for an export.

    Returns ``(agent, tool)``. Needed whenever the code under test loads its context back
    out of the database — which every graph node does, because the worker that runs them
    is not the request that created the export.
    """

    async def _make(rows: int = 125, sql_query: str | None = None):  # noqa: ANN202
        source = make_source_db(rows)

        datasource = await datasource_crud.create(
            db,
            {
                "user_id": user.id,
                "datasource_name": f"src-{uuid_pkg.uuid4().hex[:8]}",
                "db_type": "sqlite",
                "database_name": str(source),
                "password_encrypted": "",
                "configuration_data": {},
            },
        )

        agent = await agent_crud.create(
            db, {"user_id": user.id, "name": f"agent-{uuid_pkg.uuid4().hex[:8]}"},
        )

        tool = await tool_crud.create(
            db,
            {
                "data_agent_id": agent.id,
                "datasource_id": datasource.id,
                "tool_name": "all_items",
                "table_name": "items",
                "query_mode": "sql" if sql_query else "builder",
                "config": {},
                "sql_query": sql_query,
            },
        )

        return agent, tool

    return _make
