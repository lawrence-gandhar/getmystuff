"""
Tests for app/services/query_test/query_test_service.py.

The service exists to answer one question honestly — *will the database run this
query* — so it is tested against a **real SQLite database** in ``tmp_path``, the same
way the executor it calls is. Stubbing the run would leave exactly the failures this
feature was added to catch untested: the ones no validator can predict and only a
database can report.

Two properties are asserted throughout:

* **every outcome is a payload, never an exception.** The panel renders one alert
  either way, and the route has no error branch — a failed test is a result.
* **a failure says what to fix.** The driver's own words when the database refused
  it, the validator's sentence when the config is wrong, and never the
  "tell the user…" advice that belongs to an agent mid-conversation.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.models.datasource import DataSource
from app.services.query_test import query_test_service as svc


@pytest.fixture
def database(tmp_path: Path) -> Path:
    """A small SQLite database: two related tables, one with a duplicate name."""
    path = tmp_path / "warehouse.db"

    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE inventory_items (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            qty INTEGER NOT NULL
        );
        CREATE TABLE suppliers (
            id INTEGER PRIMARY KEY,
            item_id INTEGER NOT NULL,
            name TEXT NOT NULL
        );
        INSERT INTO inventory_items (id, name, qty) VALUES
            (1, 'bolt', 10), (2, 'nut', 0), (3, 'bolt', 5);
        INSERT INTO suppliers (id, item_id, name) VALUES (1, 1, 'Acme');
        """
    )
    connection.commit()
    connection.close()

    return path


@pytest.fixture
def make_datasource(db):  # noqa: ANN001, ANN201
    async def _make(owner, **kwargs):  # noqa: ANN001
        row = DataSource(
            user_id=owner.id,
            datasource_name=kwargs.pop("datasource_name", "warehouse"),
            db_type=kwargs.pop("db_type", "sqlite"),
            # NOT NULL on the column; SQLite needs no password and `rdbms_url`
            # decrypts only a non-empty one.
            password_encrypted="",
            **kwargs,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row

    return _make


@pytest.fixture
async def datasource(make_datasource, user, database: Path):  # noqa: ANN001, ANN201
    """A SQLite datasource the user owns, with nothing switched off."""
    return await make_datasource(
        user, database_name=str(database), configuration_data={},
    )


def _switched_off(**tables) -> dict:  # noqa: ANN003
    """``configuration_data`` with the named tables or columns switched off."""
    configuration = {}

    for table_name, columns in tables.items():
        if columns is None:
            configuration[table_name] = {"status": "inactive"}
            continue

        configuration[table_name] = {
            "status": "active",
            "column_data": {
                name: {"column_name": name, "status": "inactive"} for name in columns
            },
        }

    return configuration


class TestAQueryThatRuns:
    async def test_a_sql_statement_passes_and_names_its_columns(
        self, db, user, datasource  # noqa: ANN001
    ) -> None:
        result = await svc.test_query(
            db,
            user.id,
            datasource.uuid,
            ["inventory_items"],
            "sql",
            {},
            "SELECT name, qty FROM inventory_items",
        )

        assert result["passed"] is True
        assert result["columns"] == ["name", "qty"]
        assert "ran successfully" in result["message"]

    async def test_a_builder_config_passes(
        self, db, user, datasource  # noqa: ANN001
    ) -> None:
        result = await svc.test_query(
            db,
            user.id,
            datasource.uuid,
            ["inventory_items"],
            "builder",
            {
                "columns": [{"column": "name", "alias": ""}],
                "aggregations": [{"type": "count", "column": "id", "alias": "n"}],
                "group_by": ["name"],
            },
            "",
        )

        assert result["passed"] is True
        assert result["columns"] == ["name", "n"]

    async def test_a_query_matching_nothing_passes_and_says_so(
        self, db, user, datasource  # noqa: ANN001
    ) -> None:
        """Valid SQL over data that does not match. Not a failure — but worth
        saying, because a tool that always returns nothing is rarely intended."""
        result = await svc.test_query(
            db,
            user.id,
            datasource.uuid,
            ["inventory_items"],
            "sql",
            {},
            "SELECT name FROM inventory_items WHERE qty > 9999",
        )

        assert result["passed"] is True
        assert result["row_count"] == 0
        assert "matched no rows" in result["message"]

    async def test_no_values_are_reported(
        self, db, user, datasource  # noqa: ANN001
    ) -> None:
        """A row is read to prove the query runs; none of it is handed back. In the
        Ask AI panel that is the difference between a test and a data leak."""
        result = await svc.test_query(
            db,
            user.id,
            datasource.uuid,
            ["inventory_items"],
            "sql",
            {},
            "SELECT name FROM inventory_items",
        )

        assert "bolt" not in result["message"]
        assert set(result) == {"passed", "message", "columns", "row_count"}


class TestAQueryTheDatabaseRefuses:
    async def test_the_driver_s_own_message_is_shown(
        self, db, user, datasource  # noqa: ANN001
    ) -> None:
        """The reason the button exists: "no such column: nope" names the thing to
        fix, where "the query could not be run" names nothing."""
        result = await svc.test_query(
            db,
            user.id,
            datasource.uuid,
            ["inventory_items"],
            "sql",
            {},
            "SELECT nope FROM inventory_items",
        )

        assert result["passed"] is False
        assert "nope" in result["message"]

    async def test_the_message_does_not_carry_the_bound_parameters(
        self, db, user, datasource  # noqa: ANN001
    ) -> None:
        """SQLAlchemy's own str() appends the statement and its parameters. The
        statement is already on screen above the alert."""
        result = await svc.test_query(
            db,
            user.id,
            datasource.uuid,
            ["inventory_items"],
            "sql",
            {},
            "SELECT nope FROM inventory_items",
        )

        assert "[SQL:" not in result["message"]

    async def test_an_over_long_driver_message_is_cut(
        self, db, user, datasource, monkeypatch: pytest.MonkeyPatch  # noqa: ANN001
    ) -> None:
        from sqlalchemy.exc import SQLAlchemyError

        async def boom(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            raise SQLAlchemyError("x" * (svc.MAX_DATABASE_MESSAGE + 500))

        monkeypatch.setattr(svc, "probe_tool_query", boom)

        result = await svc.test_query(
            db, user.id, datasource.uuid, ["inventory_items"], "sql", {}, "SELECT 1",
        )

        assert result["passed"] is False
        assert len(result["message"]) < svc.MAX_DATABASE_MESSAGE + 100


class TestAQueryTheApplicationRefuses:
    async def test_a_write_is_refused_without_reaching_the_database(
        self, db, user, datasource  # noqa: ANN001
    ) -> None:
        result = await svc.test_query(
            db,
            user.id,
            datasource.uuid,
            ["inventory_items"],
            "sql",
            {},
            "DELETE FROM inventory_items",
        )

        assert result["passed"] is False
        assert "read-only" in result["message"]

    async def test_a_grouped_query_selecting_an_ungrouped_column_is_named(
        self, db, user, datasource  # noqa: ANN001
    ) -> None:
        """Refused by the same validator the save uses, so the test and the save
        agree — and the message is the one the form would have given."""
        result = await svc.test_query(
            db,
            user.id,
            datasource.uuid,
            ["inventory_items"],
            "builder",
            {
                "columns": [{"column": "name", "alias": ""}],
                "aggregations": [{"type": "count", "column": "id", "alias": "n"}],
                "group_by": ["qty"],
            },
            "",
        )

        assert result["passed"] is False
        assert "not grouped" in result["message"]

    async def test_an_invented_column_is_named(
        self, db, user, datasource  # noqa: ANN001
    ) -> None:
        result = await svc.test_query(
            db,
            user.id,
            datasource.uuid,
            ["inventory_items"],
            "builder",
            {"columns": [{"column": "nope", "alias": ""}]},
            "",
        )

        assert result["passed"] is False
        assert "nope" in result["message"]

    async def test_an_inactive_table_is_reported_in_the_form_s_words(
        self, db, user, make_datasource, database: Path  # noqa: ANN001
    ) -> None:
        datasource = await make_datasource(
            user,
            database_name=str(database),
            configuration_data=_switched_off(inventory_items=None),
        )

        result = await svc.test_query(
            db,
            user.id,
            datasource.uuid,
            ["inventory_items"],
            "sql",
            {},
            "SELECT name FROM inventory_items",
        )

        assert result["passed"] is False
        assert "inactive" in result["message"]
        assert "Tell the user" not in result["message"]

    async def test_an_inactive_column_is_reported_without_agent_advice(
        self, db, user, make_datasource, database: Path  # noqa: ANN001
    ) -> None:
        datasource = await make_datasource(
            user,
            database_name=str(database),
            configuration_data=_switched_off(inventory_items=["qty"]),
        )

        result = await svc.test_query(
            db,
            user.id,
            datasource.uuid,
            ["inventory_items"],
            "builder",
            {"columns": [{"column": "qty", "alias": ""}]},
            "",
        )

        assert result["passed"] is False
        assert "'inventory_items.qty' is inactive" in result["message"]
        assert "Tell the user" not in result["message"]

    async def test_no_tables_is_a_readable_refusal(
        self, db, user, datasource  # noqa: ANN001
    ) -> None:
        result = await svc.test_query(
            db, user.id, datasource.uuid, [], "sql", {}, "SELECT 1",
        )

        assert result["passed"] is False
        assert "Table is required" in result["message"]


class TestWhatCannotBeTested:
    async def test_no_datasource_asks_for_one(self, db, user) -> None:  # noqa: ANN001
        result = await svc.test_query(db, user.id, None, ["orders"], "sql", {}, "SELECT 1")

        assert result["passed"] is False
        assert "Pick a datasource" in result["message"]

    async def test_someone_else_s_datasource_is_not_found(
        self, db, user, make_datasource, make_user, database: Path  # noqa: ANN001
    ) -> None:
        """Ownership is checked here and not left to the executor, which is handed a
        row and asked no questions about who it belongs to."""
        theirs = await make_datasource(
            await make_user("other@example.com"),
            database_name=str(database),
            configuration_data={},
        )

        result = await svc.test_query(
            db, user.id, theirs.uuid, ["inventory_items"], "sql", {}, "SELECT 1",
        )

        assert result["passed"] is False
        assert "not found" in result["message"]

    async def test_a_non_relational_datasource_says_why_not(
        self, db, user, make_datasource  # noqa: ANN001
    ) -> None:
        mongo = await make_datasource(
            user,
            datasource_name="events",
            db_type="mongodb",
            database_name="events",
            configuration_data={},
        )

        result = await svc.test_query(
            db, user.id, mongo.uuid, ["events"], "sql", {}, "SELECT 1",
        )

        assert result["passed"] is False
        assert "not a relational datasource" in result["message"]

    async def test_an_unreachable_datasource_is_a_connection_message(
        self, db, user, make_datasource  # noqa: ANN001
    ) -> None:
        """Not a query problem, and saying "the database refused this query" would
        send the user off editing perfectly good SQL."""
        unreachable = await make_datasource(
            user,
            db_type="postgres",
            host="127.0.0.1",
            port="1",
            username="nobody",
            database_name="nothing",
            configuration_data={},
        )

        result = await svc.test_query(
            db, user.id, unreachable.uuid, ["orders"], "sql", {}, "SELECT 1",
        )

        assert result["passed"] is False
        assert "Could not connect" in result["message"]
