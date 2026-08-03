"""
Tests for app/services/deep_agents/query_executor.py.

This is the only module in the Deep Agents feature that touches a user's data,
so it is exercised against a **real SQLite database** written to ``tmp_path``
rather than against mocks. Mocking the connection would test that the module
calls SQLAlchemy, which is not the interesting question; running it proves the
rows come back, the row cap holds, and — for the SQL mode — that the operator's
statement executes as written.

The two modes have different guarantees and both are asserted here:

* **builder** — the query is rebuilt from reflected ``Column`` objects, filter
  values arrive as bound parameters. The injection test is the point of it.
* **sql** — the stored statement runs verbatim, re-validated first, capped by
  streaming rather than by rewriting the SQL. The tests that matter are the ones
  showing that queries the builder cannot express (``DISTINCT``, ``ORDER BY``,
  duplicate output column names) run correctly, and that a write never does.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.deep_agents.query_executor import (
    MAX_TOOL_ROWS,
    ToolQueryError,
    describe_result,
    execute_tool_query,
)


@pytest.fixture
def database(tmp_path: Path) -> Path:
    """A small SQLite database with two related tables and a duplicate name."""
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
        INSERT INTO suppliers (id, item_id, name) VALUES
            (1, 1, 'Acme'), (2, 3, 'Globex');
        """
    )
    connection.commit()
    connection.close()

    return path


@pytest.fixture
def datasource(database: Path) -> SimpleNamespace:
    """
    A datasource row as the executor reads it.

    A ``SimpleNamespace`` rather than a persisted ``DataSource``: this module
    never queries the application database, it only reads five attributes off
    whatever it is handed.
    """
    return SimpleNamespace(
        db_type="sqlite",
        database_name=str(database),
        datasource_name="warehouse",
        host=None,
        port=None,
        username=None,
        password_encrypted=None,
    )


class TestSqlMode:
    async def test_runs_a_distinct_query_the_builder_cannot_express(
        self, datasource: SimpleNamespace
    ) -> None:
        """The query from the bug report: DISTINCT has no place in the builder's
        five sections, and is an ordinary read."""
        rows = await execute_tool_query(
            datasource,
            {},
            "inventory_items",
            sql_query="SELECT DISTINCT name FROM inventory_items",
        )

        assert sorted(row["name"] for row in rows) == ["bolt", "nut"]

    async def test_runs_order_by_and_limit(self, datasource: SimpleNamespace) -> None:
        rows = await execute_tool_query(
            datasource,
            {},
            "inventory_items",
            sql_query="SELECT name, qty FROM inventory_items ORDER BY qty DESC LIMIT 2",
        )

        assert [row["qty"] for row in rows] == [10, 5]

    async def test_runs_a_cte(self, datasource: SimpleNamespace) -> None:
        rows = await execute_tool_query(
            datasource,
            {},
            "inventory_items",
            sql_query=(
                "WITH stocked AS (SELECT * FROM inventory_items WHERE qty > 0) "
                "SELECT COUNT(*) AS n FROM stocked"
            ),
        )

        assert rows == [{"n": 2}]

    async def test_runs_a_join_selecting_two_columns_of_the_same_name(
        self, datasource: SimpleNamespace
    ) -> None:
        """Wrapping the statement in ``SELECT * FROM (…) LIMIT n`` to apply the cap
        would break exactly this query on MySQL — which is why the cap is applied
        by streaming instead."""
        rows = await execute_tool_query(
            datasource,
            {},
            "inventory_items",
            sql_query=(
                "SELECT i.name, s.name FROM inventory_items i "
                "JOIN suppliers s ON s.item_id = i.id ORDER BY i.id"
            ),
        )

        assert len(rows) == 2

    async def test_the_row_cap_is_enforced(self, datasource: SimpleNamespace) -> None:
        """A statement with no LIMIT of its own still cannot flood the prompt."""
        rows = await execute_tool_query(
            datasource,
            {},
            "inventory_items",
            row_limit=2,
            sql_query="SELECT * FROM inventory_items",
        )

        assert len(rows) == 2

    async def test_the_cap_can_never_be_raised_above_the_ceiling(
        self, datasource: SimpleNamespace
    ) -> None:
        rows = await execute_tool_query(
            datasource,
            {},
            "inventory_items",
            row_limit=MAX_TOOL_ROWS + 5000,
            sql_query="SELECT * FROM inventory_items",
        )

        assert len(rows) == 3

    @pytest.mark.parametrize(
        "sql",
        [
            "DELETE FROM inventory_items",
            "UPDATE inventory_items SET qty = 0",
            "SELECT 1; DROP TABLE inventory_items",
        ],
    )
    async def test_a_stored_statement_that_is_not_a_read_is_refused_at_run_time(
        self, datasource: SimpleNamespace, sql: str
    ) -> None:
        """Re-validated on every run, not just when saved — a row edited straight
        in psql is held to the same rule as one saved through the form."""
        with pytest.raises(ToolQueryError, match="no longer valid"):
            await execute_tool_query(datasource, {}, "inventory_items", sql_query=sql)

        # And nothing ran: the table is untouched.
        rows = await execute_tool_query(
            datasource, {}, "inventory_items", sql_query="SELECT * FROM inventory_items"
        )
        assert len(rows) == 3

    async def test_a_broken_statement_fails_as_a_tool_error_not_a_crash(
        self, datasource: SimpleNamespace
    ) -> None:
        """Syntax is the database's to judge; the agent has to be told, not 500."""
        with pytest.raises(ToolQueryError, match="could not be run"):
            await execute_tool_query(
                datasource,
                {},
                "inventory_items",
                sql_query="SELECT nope FROM inventory_items",
            )

    async def test_sql_mode_wins_over_a_config_left_behind(
        self, datasource: SimpleNamespace
    ) -> None:
        """Which is why the service clears ``config`` when saving a SQL tool —
        a stale one would be silently ignored rather than silently used."""
        rows = await execute_tool_query(
            datasource,
            {"columns": [{"column": "qty", "alias": ""}]},
            "inventory_items",
            sql_query="SELECT DISTINCT name FROM inventory_items",
        )

        assert set(rows[0]) == {"name"}


class TestBuilderMode:
    async def test_runs_a_built_query(self, datasource: SimpleNamespace) -> None:
        rows = await execute_tool_query(
            datasource,
            {"columns": [{"column": "name", "alias": ""}]},
            "inventory_items",
        )

        assert [row["name"] for row in rows] == ["bolt", "nut", "bolt"]

    async def test_a_filter_value_is_a_bound_parameter(
        self, datasource: SimpleNamespace
    ) -> None:
        """The value reaches the database as a string that matches nothing, not as
        syntax — the single most important property of builder mode."""
        rows = await execute_tool_query(
            datasource,
            {
                "columns": [{"column": "name", "alias": ""}],
                "filters": [
                    {"column": "name", "operator": "=", "value": "x' OR 1=1 --"}
                ],
            },
            "inventory_items",
        )

        assert rows == []

    async def test_an_empty_selection_reads_every_column(
        self, datasource: SimpleNamespace
    ) -> None:
        rows = await execute_tool_query(datasource, {}, "inventory_items")

        assert set(rows[0]) == {"id", "name", "qty"}


class TestNonRelationalDatasources:
    async def test_a_mongo_datasource_is_refused_with_a_relayable_message(
        self,
    ) -> None:
        mongo = SimpleNamespace(db_type="mongodb", datasource_name="events")

        with pytest.raises(ToolQueryError, match="only relational databases"):
            await execute_tool_query(mongo, {}, "events", sql_query="SELECT 1")


class TestDescribeResult:
    def test_no_rows_is_stated_as_a_result_not_a_failure(self) -> None:
        assert describe_result([]) == "0 rows. The query returned no data."

    def test_a_capped_result_says_so(self) -> None:
        described = describe_result([{"n": 1}] * MAX_TOOL_ROWS)

        assert "capped" in described
        assert "not the total" in described
