"""
Tests for app/db/migrations.py — the startup migration runner.

This module decides, from nothing but the shape of the database, whether to build a
schema, top one up, or refuse to touch it. Getting that decision wrong is expensive in
a way a per-query failure is not: replaying the chain over populated tables fails
mid-way, and stamping a revision that does not describe the schema hides drift until
something reads the wrong column. So the three states are tested by their observable
consequence — whether ``alembic upgrade`` was invoked at all.

``command.upgrade`` itself is never run here. It is Alembic's, it needs a real
PostgreSQL server, and what matters at this layer is *whether* it is called and *with
what config* — the config being where the app and its migrations could silently end up
on different databases.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.db import migrations


@pytest.fixture
def engine_with_state():
    """
    Patch the module's engine so ``upgrade_to_head`` sees a chosen schema state.

    Returns a factory taking the ``(has_tables, is_tracked, revision)`` tuple
    ``_inspect_schema_state`` would have produced, plus the revision to report
    afterwards, and yields the mock connection so the advisory lock can be asserted on.
    """
    def _make(before, after_revision):
        connection = AsyncMock()
        connection.run_sync = AsyncMock(
            side_effect=[before, (True, True, after_revision)]
        )

        engine = MagicMock()
        engine.begin.return_value.__aenter__ = AsyncMock(return_value=connection)
        engine.begin.return_value.__aexit__ = AsyncMock(return_value=False)
        return engine, connection

    return _make


class TestUpgradeToHead:
    async def test_an_empty_database_gets_the_whole_chain(self, engine_with_state) -> None:
        engine, _ = engine_with_state((False, False, None), "2abb54ec1a3b")

        with (
            patch.object(migrations, "engine", engine),
            patch.object(migrations, "_upgrade_to_head") as upgrade,
        ):
            await migrations.upgrade_to_head()

        upgrade.assert_called_once_with()

    async def test_a_tracked_database_is_upgraded_from_its_revision(
        self, engine_with_state
    ) -> None:
        engine, _ = engine_with_state((True, True, "e7b3f5a91c26"), "2abb54ec1a3b")

        with (
            patch.object(migrations, "engine", engine),
            patch.object(migrations, "_upgrade_to_head") as upgrade,
        ):
            await migrations.upgrade_to_head()

        upgrade.assert_called_once_with()

    async def test_a_tracked_database_already_at_head_is_still_a_no_op_call(
        self, engine_with_state
    ) -> None:
        """
        Booting under `uvicorn --reload` runs this on every code change. Alembic itself
        is what decides there is nothing to do, so the call still happens — the point is
        that it must not raise.
        """
        engine, _ = engine_with_state((True, True, "2abb54ec1a3b"), "2abb54ec1a3b")

        with (
            patch.object(migrations, "engine", engine),
            patch.object(migrations, "_upgrade_to_head") as upgrade,
        ):
            await migrations.upgrade_to_head()

        upgrade.assert_called_once_with()

    async def test_an_untracked_database_is_refused_and_never_migrated(
        self, engine_with_state
    ) -> None:
        """
        Tables but no alembic_version — a database built by the old create_all path.
        Its revision cannot be inferred, so the only safe move is to stop before
        touching it.
        """
        engine, _ = engine_with_state((True, False, None), None)

        with (
            patch.object(migrations, "engine", engine),
            patch.object(migrations, "_upgrade_to_head") as upgrade,
        ):
            with pytest.raises(RuntimeError) as excinfo:
                await migrations.upgrade_to_head()

        upgrade.assert_not_called()

        message = str(excinfo.value)
        # The operator has to know what to run; a bare "cannot migrate" would leave
        # them guessing at the one command that resolves it.
        assert "alembic stamp" in message
        assert "create_all" in message

    async def test_the_advisory_lock_is_taken_before_anything_is_inspected(
        self, engine_with_state
    ) -> None:
        """
        Two workers booting at once must not both apply the chain. The lock has to come
        first — taking it after the inspection would let both read "pending" and race.
        """
        engine, connection = engine_with_state((True, True, "e7b3f5a91c26"), "2abb54ec1a3b")

        with (
            patch.object(migrations, "engine", engine),
            patch.object(migrations, "_upgrade_to_head"),
        ):
            await migrations.upgrade_to_head()

        statement = str(connection.execute.await_args_list[0].args[0])
        assert "pg_advisory_xact_lock" in statement
        assert connection.execute.await_args_list[0].args[1] == {
            "key": migrations.MIGRATION_LOCK_KEY
        }

    async def test_a_failed_migration_propagates(self, engine_with_state) -> None:
        """
        Startup must fail, not continue. The transaction unwinding is what releases the
        advisory lock, so a swallowed error would also strand it.
        """
        engine, _ = engine_with_state((True, True, "e7b3f5a91c26"), "2abb54ec1a3b")

        with (
            patch.object(migrations, "engine", engine),
            patch.object(
                migrations,
                "_upgrade_to_head",
                side_effect=RuntimeError("relation already exists"),
            ),
        ):
            with pytest.raises(RuntimeError, match="relation already exists"):
                await migrations.upgrade_to_head()


class TestAlembicConfig:
    def test_it_targets_the_database_the_app_itself_uses(self) -> None:
        """
        alembic.ini still carries a hardcoded localhost URL, which is wrong inside a
        container. If this ever stopped overriding it, migrations would run against a
        different database than the app — or none at all.
        """
        with patch.object(migrations, "DATABASE_URL", "postgresql+asyncpg://u:p@db:5432/x"):
            config = migrations._build_alembic_config()

        assert config.get_main_option("sqlalchemy.url") == "postgresql+asyncpg://u:p@db:5432/x"

    def test_paths_are_absolute_so_the_working_directory_cannot_matter(self) -> None:
        config = migrations._build_alembic_config()
        script_location = Path(config.get_main_option("script_location"))

        assert script_location.is_absolute()
        assert script_location.is_dir()
        assert (script_location / "versions").is_dir()

    def test_it_does_not_let_alembic_reconfigure_process_logging(self) -> None:
        """
        env.py calls fileConfig() unless told otherwise, and alembic.ini pins the root
        logger to WARNING — applying that in-process would silence the app's own INFO
        logging for the rest of its life.
        """
        config = migrations._build_alembic_config()

        assert config.attributes["configure_logger"] is False

    def test_a_missing_alembic_ini_is_a_named_failure(self) -> None:
        with patch.object(migrations, "ALEMBIC_INI_PATH", Path("/nonexistent/alembic.ini")):
            with pytest.raises(RuntimeError, match="alembic.ini was not found"):
                migrations._build_alembic_config()

    def test_the_thread_target_upgrades_to_head_with_that_config(self) -> None:
        """
        'head' rather than a pinned revision: the app applies everything the code it
        shipped with expects, which is the property that stops the schema lagging behind.
        """
        with patch.object(migrations.command, "upgrade") as upgrade:
            migrations._upgrade_to_head()

        config, revision = upgrade.call_args.args
        assert revision == "head"
        assert config.attributes["configure_logger"] is False


class TestInspectSchemaState:
    """
    The state read is what every decision above rests on, so it is tested directly
    rather than only through its callers.
    """

    def _connection(self, table_names, revision=None):
        inspector = MagicMock()
        inspector.get_table_names.return_value = table_names
        context = MagicMock()
        context.get_current_revision.return_value = revision
        return inspector, context

    def test_no_tables_is_empty_and_untracked(self) -> None:
        inspector, context = self._connection([])
        with (
            patch.object(migrations, "inspect", return_value=inspector),
            patch.object(migrations.MigrationContext, "configure", return_value=context),
        ):
            assert migrations._inspect_schema_state(MagicMock()) == (False, False, None)

    def test_a_version_table_alone_still_counts_as_empty(self) -> None:
        """
        A stamped-but-unbuilt database has to take the build path, not be mistaken for
        one holding data.
        """
        inspector, context = self._connection(["alembic_version"], revision="6180392f0092")
        with (
            patch.object(migrations, "inspect", return_value=inspector),
            patch.object(migrations.MigrationContext, "configure", return_value=context),
        ):
            has_tables, is_tracked, revision = migrations._inspect_schema_state(MagicMock())

        assert (has_tables, is_tracked, revision) == (False, True, "6180392f0092")

    def test_tables_without_a_version_table_is_the_untracked_case(self) -> None:
        inspector, context = self._connection(["users", "datasources"])
        with (
            patch.object(migrations, "inspect", return_value=inspector),
            patch.object(migrations.MigrationContext, "configure", return_value=context),
        ):
            assert migrations._inspect_schema_state(MagicMock()) == (True, False, None)

    def test_tables_with_a_version_table_report_the_revision(self) -> None:
        inspector, context = self._connection(
            ["users", "alembic_version"], revision="2abb54ec1a3b"
        )
        with (
            patch.object(migrations, "inspect", return_value=inspector),
            patch.object(migrations.MigrationContext, "configure", return_value=context),
        ):
            assert migrations._inspect_schema_state(MagicMock()) == (
                True,
                True,
                "2abb54ec1a3b",
            )
