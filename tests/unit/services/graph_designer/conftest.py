"""
Fixtures shared by the Graph Designer tests.

Both ends of a run are real here: a genuine SQLite database in ``tmp_path`` standing in
for the user's datasource, and the in-memory application database for the graph, run and
step rows. Nothing that produces or consumes data is mocked — mocking a node would prove
the compiler calls it, whereas running it proves a loop over three departments runs three
times.

Two fixtures are load-bearing and both are autouse, because forgetting either produces a
failure that looks like something else entirely.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker


@pytest.fixture(autouse=True)
def graph_sessions(db_engine, monkeypatch: pytest.MonkeyPatch):  # noqa: ANN001, ANN201
    """
    Point ``run_store.open_session`` at the per-test database.

    Every graph node, the run's background task and the progress poll loop open their
    **own** session rather than taking an injected one — a LangGraph node has no injected
    session, and the poll loop outlives the handler that returned it. They all go through
    ``run_store.open_session``, which wraps ``db_sessions.AsyncSessionLocal``: the engine
    built at import from ``DATABASE_URL``.

    In the container that variable is the *development* PostgreSQL database. So without
    this fixture a test's nodes would read and write the development database while the
    assertions looked at the in-memory one — the exact trap
    ``tests/conftest.background_sessions`` documents for the export graph. Autouse, because
    a test that forgets it does not fail cleanly: it either passes against the wrong
    database or trips the ``block_network`` guard with an error about sockets.
    """
    from app.services.graph_designer import run_store

    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    monkeypatch.setattr(run_store, "open_session", factory)

    return factory


@pytest.fixture(autouse=True)
def graph_checkpointer(monkeypatch: pytest.MonkeyPatch):  # noqa: ANN001, ANN201
    """
    A fresh in-memory checkpointer per test, never the PostgreSQL one.

    The same two problems ``tests/unit/services/downloader_agents/conftest.py`` documents,
    reached from the other feature that compiles a graph with a checkpointer:

    ``get_checkpointer`` picks its store from ``DATABASE_URL``, which in the container is
    the development database — so without this a test would write real checkpoint rows into
    it.

    And the saver is cached in a module global. ``AsyncPostgresSaver`` holds an
    ``asyncio.Lock`` bound to the loop that created it, and pytest-asyncio gives each test
    its own loop; the second test to use a cached saver fails inside ``asyncio.locks`` on a
    loop that no longer exists.

    Autouse for the same reason as above: every run in this package compiles a graph, so
    every test needs it, and the failure without it is confusing rather than obvious.
    """
    from app.services.downloader_agents.base import checkpointer

    monkeypatch.setattr(checkpointer, "_saver", None)
    monkeypatch.setattr(checkpointer, "_pool", None)
    # Returning None is what selects InMemorySaver — see get_checkpointer.
    monkeypatch.setattr(checkpointer, "postgres_dsn", lambda *args, **kwargs: None)

    yield

    monkeypatch.setattr(checkpointer, "_saver", None)


@pytest.fixture(autouse=True)
async def no_runs_left_behind():  # noqa: ANN201
    """
    Cancel anything still in flight at the end of a test.

    A run is a background task, and one left running past its test would keep writing
    through a session bound to an engine the fixture teardown has disposed of — which
    surfaces as an unrelated later test failing on a closed connection. Cancelling here
    keeps each test's failures its own.
    """
    yield

    from app.services.graph_designer import graph_run_service

    await graph_run_service.stop_all_runs()
