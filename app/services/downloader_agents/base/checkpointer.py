"""
Where an interrupted export graph is parked between two chat turns.

The export graph stops on a LangGraph ``interrupt()`` to ask the user whether they want
the file. Answering happens in a **different HTTP request**, and building it happens in
a **different task again** — the queue worker. So the graph's state cannot live in the
process that created it, and this module is what makes "resume where we left off"
possible across all three.

**Postgres, via psycopg 3, deliberately as a second driver.** ``AsyncPostgresSaver`` is
langgraph's own store and it is built on psycopg 3; it cannot use the asyncpg engine the
rest of the application runs on. Writing a checkpointer over the existing SQLAlchemy
session was the alternative — around two hundred lines of ``aput`` / ``aget_tuple`` /
``alist`` / ``aput_writes`` that we would own and have to keep correct against
langgraph's protocol — and a second driver on the same database is the cheaper honesty.
The pool is small (see :data:`_POOL_MAX_SIZE`) because it serves checkpoint writes, not
traffic.

**In-memory when there is no Postgres.** The test suite runs on
``sqlite+aiosqlite://``, and langgraph has no SQLite saver installed here. Rather than
skip every graph test, :func:`get_checkpointer` returns ``InMemorySaver`` for any DSN
that is not Postgres. That is correct for a test — one process, one event loop — and
would be wrong in production, which is why it is chosen from the DSN rather than from a
setting somebody could get wrong: you cannot end up on the in-memory saver while
pointed at a real database.

**Why the DSN is rewritten rather than configured separately.** ``DATABASE_URL`` is
``postgresql+asyncpg://…``; psycopg needs ``postgresql://…``. Deriving one from the
other means there is one place to change the database, not two that can disagree —
and a deployment that moves the database cannot accidentally leave the checkpoints
behind on the old one.
"""

import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)


# Checkpoint traffic is a handful of small writes per export, and an export is a
# background job that nobody is waiting on. Four connections would be three more than
# this needs; two leaves room for the reaper to write while a request reads.
_POOL_MIN_SIZE = 1
_POOL_MAX_SIZE = 2

# The SQLAlchemy dialect suffixes to strip. `postgresql+asyncpg` and plain `postgres`
# both appear in the wild for the same database.
_POSTGRES_SCHEMES = ("postgresql", "postgres")

# Set once, on first use, and reused for the life of the process. Two globals rather
# than one because the pool has to be closed on shutdown and the saver does not own it.
_saver: Optional[Any] = None
_pool: Optional[Any] = None


def postgres_dsn(database_url: Optional[str] = None) -> Optional[str]:
    """
    ``DATABASE_URL`` as psycopg 3 wants it, or None if it is not Postgres.

    Returning None rather than raising is what drives the in-memory fallback: "this is
    not a Postgres database" is a fact about the environment, not an error, and the
    caller decides what to do about it.
    """
    url = (database_url or os.getenv("DATABASE_URL") or "").strip()

    if not url:
        return None

    scheme, separator, rest = url.partition("://")

    if not separator:
        return None

    # `postgresql+asyncpg` -> `postgresql`. The driver half is SQLAlchemy's business
    # and psycopg rejects it.
    base = scheme.split("+", 1)[0].lower()

    if base not in _POSTGRES_SCHEMES:
        return None

    return f"postgresql://{rest}"


async def get_checkpointer() -> Any:
    """
    The process's checkpoint saver, created on first use.

    Created lazily rather than at import so that importing this module — which
    ``download_graph`` does, which the agent tools do — never opens a connection. An
    application that has no exports pending should not hold a pool for them.

    ``setup()`` is run once, here, on the Postgres saver. It creates langgraph's own
    checkpoint tables if they are not there. Deliberately **not** an Alembic migration:
    the schema belongs to langgraph and changes when langgraph changes, so a revision
    of ours claiming to own it would be a revision that goes stale on an upgrade.
    """
    global _saver, _pool

    if _saver is not None:
        return _saver

    dsn = postgres_dsn()

    if dsn is None:
        from langgraph.checkpoint.memory import InMemorySaver

        logger.info(
            "DATABASE_URL is not PostgreSQL, so export confirmations are checkpointed "
            "in memory. They will not survive a restart.",
        )
        _saver = InMemorySaver()
        return _saver

    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from psycopg_pool import AsyncConnectionPool

    # autocommit is required by AsyncPostgresSaver.setup(), which issues DDL, and
    # row_factory=dict_row by the saver's own queries. Both are langgraph's
    # documented requirements rather than choices made here.
    from psycopg.rows import dict_row

    _pool = AsyncConnectionPool(
        conninfo=dsn,
        min_size=_POOL_MIN_SIZE,
        max_size=_POOL_MAX_SIZE,
        # open=False then an explicit open(): constructing an open pool inside a
        # coroutine emits a deprecation warning in psycopg_pool and starts background
        # work before anyone has awaited anything.
        open=False,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
    )
    await _pool.open()

    saver = AsyncPostgresSaver(_pool)
    await saver.setup()

    _saver = saver
    logger.info("Export checkpoints are stored in PostgreSQL")

    return _saver


async def close_checkpointer() -> None:
    """
    Close the pool, for application shutdown and for tests.

    The saver is dropped along with the pool it was built on: keeping it would leave a
    saver holding a closed pool, which fails on next use in a way that looks like a
    database problem rather than a lifecycle one.
    """
    global _saver, _pool

    pool, _saver, _pool = _pool, None, None

    if pool is not None:
        await pool.close()
