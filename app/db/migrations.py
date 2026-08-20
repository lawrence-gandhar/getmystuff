"""
Schema migration on startup — Alembic, run in-process.

Startup used to call ``Base.metadata.create_all``, which creates tables that do not
exist yet and nothing else. It never alters a table it already found, so a column added
to a model simply never reached the database: the app booted clean, then every query
naming that column failed with ``UndefinedColumnError``. Nothing reported the schema was
stale, because from ``create_all``'s point of view there was nothing left to do.

``alembic upgrade head`` is the opposite: the migration chain is the schema's definition,
so a column arrives the moment its revision does, and a database that is behind is a
state Alembic can name rather than one that has to be inferred from a failing query.

Three database states, and what each one means here:

*empty*
    No tables at all — a new ``pgdata`` volume. The whole chain runs, from the first
    revision. It builds the same schema ``create_all`` used to, including the ``vector``
    extension (revision ``a3f5c9d21b47`` issues ``CREATE EXTENSION IF NOT EXISTS``), so
    this path needs no help from ``docker/postgres-init.sql``.

*tracked*
    ``alembic_version`` is present. Whatever revisions are pending get applied; if it is
    already at head this costs one query, which is what makes it safe to do on every
    boot under ``uvicorn --reload``.

*untracked*
    Tables exist but ``alembic_version`` does not — a database built entirely by the old
    ``create_all`` path. Its schema cannot be matched to a revision by inspection, so
    guessing one would either replay the whole chain over existing tables (immediate
    failure) or stamp a revision that may not describe what is actually there (silent
    drift). Both are worse than stopping, so :func:`upgrade_to_head` refuses and says
    which command to run. See ``documentations/DOCKER_AND_LOCAL_LLM.md``.

The upgrade runs in a worker thread. ``alembic/env.py`` drives an async engine through
``asyncio.run()``, which cannot be called from a thread that already has a running event
loop — and startup does. The thread has no loop of its own, so ``asyncio.run()`` there
behaves exactly as it does on the command line.

A Postgres advisory lock serialises the whole thing, so booting several workers at once
(``uvicorn --workers N``) applies the chain once instead of racing on
``alembic_version``. The lock is transaction-scoped: it is released when the surrounding
transaction ends, including when it ends because the migration raised.
"""

import asyncio
import logging
from pathlib import Path
from typing import Optional

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect, text

from app.db.db_sessions import DATABASE_URL, engine


logger = logging.getLogger(__name__)


# app/db/migrations.py -> app/db -> app -> project root, where alembic.ini lives.
# Derived from this file rather than the working directory so migrations run the same
# way whether uvicorn was started from the project root or anywhere else.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI_PATH = PROJECT_ROOT / "alembic.ini"
ALEMBIC_SCRIPT_PATH = PROJECT_ROOT / "alembic"

# Any fixed 64-bit integer works — an advisory lock key means nothing to Postgres beyond
# "the same number is the same lock". Hardcoded so every worker of every process asks
# for the same one.
MIGRATION_LOCK_KEY = 4812003991


def _build_alembic_config() -> Config:
    """
    Load ``alembic.ini`` and point it at the database the *app* is using.

    Both paths are absolute and ``sqlalchemy.url`` is set from the app's own
    ``DATABASE_URL``, so the app and its migrations cannot end up on different
    databases — ``alembic.ini`` still carries a hardcoded localhost URL that is wrong
    everywhere except a bare local run.

    ``configure_logger`` is what stops ``env.py`` calling ``fileConfig()``, which would
    reconfigure logging process-wide and pin the root logger to ``alembic.ini``'s
    WARNING.
    """
    if not ALEMBIC_INI_PATH.is_file():
        raise RuntimeError(
            f"Cannot migrate the database: alembic.ini was not found at "
            f"{ALEMBIC_INI_PATH}. The application expects it in the project root."
        )

    config = Config(str(ALEMBIC_INI_PATH))
    config.set_main_option("script_location", str(ALEMBIC_SCRIPT_PATH))
    config.set_main_option("sqlalchemy.url", DATABASE_URL)
    config.attributes["configure_logger"] = False
    return config


def _inspect_schema_state(sync_connection) -> tuple[bool, bool, Optional[str]]:
    """
    Report what state the database is in, as ``(has_tables, is_tracked, revision)``.

    Runs on a sync connection because SQLAlchemy's inspector and Alembic's
    ``MigrationContext`` are both sync APIs; the caller supplies one via ``run_sync``.
    """
    table_names = set(inspect(sync_connection).get_table_names())
    is_tracked = "alembic_version" in table_names
    has_tables = bool(table_names - {"alembic_version"})

    revision = None
    if is_tracked:
        revision = MigrationContext.configure(sync_connection).get_current_revision()

    return has_tables, is_tracked, revision


def _upgrade_to_head() -> None:
    """Run ``alembic upgrade head``. Called in a worker thread — see module docstring."""
    command.upgrade(_build_alembic_config(), "head")


async def upgrade_to_head() -> None:
    """
    Bring the database up to the latest revision before the app serves anything.

    Raises ``RuntimeError`` with the command to run if the database has tables but no
    ``alembic_version``; startup fails loudly rather than serving requests against a
    schema nobody can account for.
    """
    async with engine.begin() as connection:
        # Held until this transaction ends, so concurrent workers queue here instead of
        # applying the same revision twice.
        await connection.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": MIGRATION_LOCK_KEY},
        )

        has_tables, is_tracked, revision = await connection.run_sync(
            _inspect_schema_state
        )

        if has_tables and not is_tracked:
            raise RuntimeError(
                "Cannot migrate the database: it already has tables but no Alembic "
                "version table, so there is no way to tell which revision its schema "
                "matches. This database was built by the old create_all startup path. "
                "Check it against the models, then record the revision it matches "
                "before starting the app again:\n"
                "    docker compose run --rm app alembic stamp head\n"
                "Use the revision that actually describes the schema — 'head' is only "
                "right if the schema is already up to date with every migration."
            )

        if is_tracked:
            logger.info("Database at revision %s; applying any pending migrations.", revision)
        else:
            logger.info("Empty database; building the schema from the migration chain.")

        await asyncio.to_thread(_upgrade_to_head)

        _, _, new_revision = await connection.run_sync(_inspect_schema_state)

    if new_revision == revision:
        logger.info("Database schema already up to date at %s.", new_revision)
    else:
        logger.info("Database schema migrated to %s.", new_revision)
