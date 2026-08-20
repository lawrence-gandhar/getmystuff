import asyncio
import os
from logging.config import fileConfig
from dotenv import load_dotenv
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config
from alembic import context

from app.db.base import Base
from app.db import models  # noqa: F401  imported for its side effect: registers every model

config = context.config

# Skipped when the app runs migrations itself at startup — app/db/migrations.py sets
# configure_logger=False. fileConfig() reconfigures logging for the whole process, and
# alembic.ini pins the root logger to WARNING, so applying it in-process would silence
# the app's own INFO logging for the rest of its life. On the command line there is no
# app to affect and the ini's config is the one that should win.
if config.config_file_name and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)

# DATABASE_URL wins over alembic.ini's sqlalchemy.url, so migrations run against
# whichever database the *app* is configured to use. Without this, alembic.ini's
# hardcoded localhost URL is the only target, and `alembic upgrade head` inside the
# Docker container cannot reach the db service at all. Same precedence as
# app/db/db_sessions.py, so the two can never point at different databases.
load_dotenv()

if os.getenv("DATABASE_URL"):
    config.set_main_option("sqlalchemy.url", os.environ["DATABASE_URL"])

target_metadata = Base.metadata

# Tables in our database that are not ours to migrate.
#
# LangGraph's checkpoint store lives in this database (see
# app/services/downloader_agents/base/checkpointer.py) and creates its own tables through
# its own `setup()`. They are not in Base.metadata, so without this every
# `--autogenerate` run proposes dropping their indexes — and a revision that carried
# those drops would break the export confirmation the first time it was applied.
#
# Owned by langgraph, versioned by langgraph, upgraded by langgraph. Matched by prefix
# because the set grows with its releases (`checkpoints`, `checkpoint_blobs`,
# `checkpoint_writes`, `checkpoint_migrations` today).
_FOREIGN_TABLE_PREFIXES = ("checkpoint",)


def include_name(name, type_, parent_names):  # noqa: ANN001, ANN201
    """
    Whether autogenerate should consider a reflected object at all.

    Filtering by *name* rather than by object is what excludes a table we have never
    declared — an `include_object` hook only ever sees objects alembic already decided to
    compare, and a foreign table has no object on our side to match against.
    """
    if type_ == "table" and name:
        return not name.startswith(_FOREIGN_TABLE_PREFIXES)

    return True


def run_migrations_offline():
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_name=include_name,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection):
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_name=include_name,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online():
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())