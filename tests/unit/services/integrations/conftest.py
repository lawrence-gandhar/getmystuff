"""
Fixtures shared by the integrations tests.

All three are autouse, and all three exist because forgetting them produces a failure
that looks like something else entirely.

``integration_sessions`` is the load-bearing one. Every node, the worker that drives a
run and the progress poll loop open their **own** session — a LangGraph node has no
injected session and the poll loop outlives the handler that returned it — and they all
go through ``run_store.open_session``, which wraps the engine built at import from
``DATABASE_URL``. In the container that variable points at the *development* PostgreSQL
database, so without this the nodes would read and write there while the assertions
looked at the in-memory one. It does not fail cleanly: the test either passes against the
wrong database or trips ``block_network`` with an error about sockets.

The other two assert that process-local state empties. A leak in ``record_buffer`` or in
``record_log``'s budget table is invisible in any single test and shows up in production
as a memory profile nobody can attribute, so it is caught here — at the test that leaked
it, rather than a week later.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker


@pytest.fixture(autouse=True)
def integration_sessions(db_engine, monkeypatch: pytest.MonkeyPatch):  # noqa: ANN001, ANN201
    """Point ``run_store.open_session`` at the per-test database. See the module
    docstring — this is the fixture whose absence does not announce itself."""
    from app.services.integrations.engine import run_store

    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    monkeypatch.setattr(run_store, "open_session", factory)

    return factory


@pytest.fixture(autouse=True)
async def no_workers_left_behind():  # noqa: ANN201
    """
    Stop the queue workers, the scheduler and any live run at the end of a test.

    A worker left running past its test keeps claiming through a session bound to an
    engine the fixture teardown has disposed of — which surfaces as an *unrelated later
    test* failing on a closed connection. Cancelling here keeps each test's failures its
    own.
    """
    yield

    from app.services.integrations.engine import queue, run_service, scheduler

    await scheduler.stop_scheduler()
    await queue.stop_workers()
    await run_service.stop_all_runs()


@pytest.fixture(autouse=True)
def integration_checkpointer(monkeypatch: pytest.MonkeyPatch):  # noqa: ANN001, ANN201
    """
    A fresh in-memory checkpointer per test, never the PostgreSQL one.

    Two problems, both of which the downloader and graph-designer conftests already
    document, reached here from the third feature that compiles a graph.

    ``get_checkpointer`` picks its store from ``DATABASE_URL``, which in the container is
    the *development* database — so without this a test would write real checkpoint rows
    into it, and an integration run checkpoints on every super-step.

    And the saver is cached in a module global. ``AsyncPostgresSaver`` holds an
    ``asyncio.Lock`` bound to the loop that created it, and pytest-asyncio gives each test
    its own loop; the second test to use a cached saver fails inside ``asyncio.locks`` on
    a loop that no longer exists.
    """
    from app.services.downloader_agents.base import checkpointer

    monkeypatch.setattr(checkpointer, "_saver", None)
    monkeypatch.setattr(checkpointer, "_pool", None)
    # Returning None is what selects InMemorySaver — see get_checkpointer.
    monkeypatch.setattr(checkpointer, "postgres_dsn", lambda *args, **kwargs: None)

    yield

    monkeypatch.setattr(checkpointer, "_saver", None)


@pytest.fixture(autouse=True)
def buffers_released():  # noqa: ANN201
    """
    Assert the record buffer is empty when a test ends.

    ``record_buffer`` holds real records in process memory — that is the whole reason
    handles travel in the LangGraph state instead of rows. A run that fails to release
    its buffers keeps fifty thousand records alive for the lifetime of the worker, and
    nothing about that is visible until the process runs out of memory. Catching it here
    names the test that leaked it.
    """
    from app.services.integrations.engine import record_buffer

    record_buffer.clear_all()
    yield
    leaked = record_buffer.open_keys()
    record_buffer.clear_all()

    assert leaked == [], f"record buffer(s) left open by this test: {', '.join(leaked)}"


@pytest.fixture(autouse=True)
def budgets_released():  # noqa: ANN201
    """
    Assert ``record_log``'s per-run budgets and ``run_store``'s cancel cache are released.

    Same argument as above, two tables smaller: a worker that has executed ten thousand
    runs must not be holding ten thousand dictionaries describing them. Asserted rather
    than merely cleared, so the pressure lands on the orchestrator to call
    ``release_run`` on every terminal path — including the failing ones, which are the
    paths where cleanup gets forgotten.
    """
    from app.services.integrations.engine import record_log, run_store

    record_log.clear_all()
    run_store.clear_cancel_cache()
    yield
    budgets, cached = record_log.open_budgets(), run_store.cached_runs()
    record_log.clear_all()
    run_store.clear_cancel_cache()

    assert budgets == 0, f"{budgets} record-log budget(s) were left behind by this test"
    assert cached == 0, f"{cached} cancel-cache entr(ies) were left behind by this test"
