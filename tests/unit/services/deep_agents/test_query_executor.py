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
* **sql** — the stored statement runs verbatim, re-validated first, and read by
  streaming rather than by rewriting the SQL. The tests that matter are the ones
  showing that queries the builder cannot express (``DISTINCT``, ``ORDER BY``,
  duplicate output column names) run correctly, and that a write never does.

Both modes return **every matching row**; see ``TestNothingCapsWhatAQueryReturns`` for
what that replaced and why the only surviving bound is on the prompt.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from litestar.exceptions import HTTPException
from sqlalchemy.exc import SQLAlchemyError

from app.services.deep_agents.query_executor import (
    DISPLAY_ROW_LIMIT,
    NEEDS_RECONFIGURING,
    NOT_AVAILABLE,
    PROBE_ROWS,
    ToolQueryError,
    describe_result,
    execute_tool_query,
    execute_value_query,
    labelled_rows,
    probe_tool_query,
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
        # Nothing switched off. An empty configuration means every table and column
        # is active, which is the state of every datasource created before Data
        # Sources recorded any of this.
        configuration_data={},
    )


#: Rows in the large fixture. Past **both** ceilings that used to exist — 200 on a tool
#: result and 2,000 on an inner tool's values — so one table proves both are gone. A
#: number either cap would have trimmed to something plausible and wrong.
LARGE_ROWS = 2500


@pytest.fixture
def large_datasource(tmp_path: Path) -> SimpleNamespace:
    """
    A datasource with more rows than any of the removed caps allowed.

    Real rows in a real SQLite file rather than a stub: what is being tested is that
    nothing between the statement and the returned list drops any of them, and a stub
    executor would only prove that this test's own loop counts correctly.
    """
    path = tmp_path / "large.db"

    connection = sqlite3.connect(path)
    connection.executescript(
        f"""
        CREATE TABLE readings (id INTEGER PRIMARY KEY, label TEXT NOT NULL);
        WITH RECURSIVE counter(n) AS (
            SELECT 1 UNION ALL SELECT n + 1 FROM counter WHERE n < {LARGE_ROWS}
        )
        INSERT INTO readings (id, label) SELECT n, 'r' || n FROM counter;
        """
    )
    connection.commit()
    connection.close()

    return SimpleNamespace(
        db_type="sqlite",
        database_name=str(path),
        datasource_name="large",
        host=None,
        port=None,
        username=None,
        password_encrypted=None,
        configuration_data={},
    )


def _switched_off(**tables: list) -> dict:
    """
    A ``configuration_data`` with the named columns switched off, e.g.
    ``_switched_off(inventory_items=["qty"])``. A table mapped to ``None`` is
    switched off entirely.
    """
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


JOIN_TO_SUPPLIERS = {
    "type": "inner",
    "table": "suppliers",
    "left_table": "inventory_items",
    "left_column": "id",
    "right_column": "item_id",
}


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
        """Wrapping the statement in ``SELECT * FROM (…) LIMIT n`` to apply a row limit
        would break exactly this query on MySQL — which is why a limit is applied by
        streaming instead."""
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

    async def test_a_caller_that_asks_for_a_number_of_rows_gets_it(
        self, datasource: SimpleNamespace
    ) -> None:
        """``row_limit`` is still honoured — the test probe is what needs it."""
        rows = await execute_tool_query(
            datasource,
            {},
            "inventory_items",
            row_limit=2,
            sql_query="SELECT * FROM inventory_items",
        )

        assert len(rows) == 2

    async def test_nothing_caps_a_caller_that_asks_for_nothing(
        self, datasource: SimpleNamespace
    ) -> None:
        """
        The rule that replaced the flat 200-row ceiling.

        A statement with no ``LIMIT`` of its own returns every matching row, because the
        operator's SQL is the statement of how much data the question is about. A second
        number applied underneath it made every large result a sample, and a total taken
        over a sample is a plausible figure that is wrong.
        """
        rows = await execute_tool_query(
            datasource,
            {},
            "inventory_items",
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


class TestOnlyActiveColumnsAreRead:
    """
    Data Sources lets the user switch a table or column off, and a tool config is a
    standing permission written once and run for months — so the switch is read on
    every run, not remembered from when the config was saved.

    Explicit references fail loudly rather than being dropped. A filter quietly
    removed widens the result set and a group-by quietly removed changes what each row
    counts; either way the agent states the wrong figure as fact, which is the one
    failure this module exists to prevent.
    """

    async def test_an_empty_selection_leaves_out_the_inactive_columns(
        self, datasource: SimpleNamespace
    ) -> None:
        datasource.configuration_data = _switched_off(inventory_items=["qty"])

        rows = await execute_tool_query(datasource, {}, "inventory_items")

        assert set(rows[0]) == {"id", "name"}

    async def test_an_empty_selection_includes_every_joined_tables_columns(
        self, datasource: SimpleNamespace
    ) -> None:
        """A tool built to join suppliers to items used to answer with nothing but
        item columns — the joined table's data never reached the agent at all."""
        rows = await execute_tool_query(
            datasource, {"joins": [JOIN_TO_SUPPLIERS]}, "inventory_items",
        )

        assert set(rows[0]) == {
            "inventory_items_id",
            "inventory_items_name",
            "inventory_items_qty",
            "suppliers_id",
            "suppliers_item_id",
            "suppliers_name",
        }

    async def test_joined_columns_are_labelled_so_a_duplicate_name_survives(
        self, datasource: SimpleNamespace
    ) -> None:
        """Both tables have an ``id`` and a ``name``. The rows come back as a dict, so
        without the table prefix one would overwrite the other and the agent would be
        handed a row that quietly lost two columns."""
        rows = await execute_tool_query(
            datasource, {"joins": [JOIN_TO_SUPPLIERS]}, "inventory_items",
        )

        assert rows[0]["inventory_items_name"] == "bolt"
        assert rows[0]["suppliers_name"] == "Acme"

    async def test_an_inactive_column_of_a_joined_table_is_left_out(
        self, datasource: SimpleNamespace
    ) -> None:
        datasource.configuration_data = _switched_off(suppliers=["name"])

        rows = await execute_tool_query(
            datasource, {"joins": [JOIN_TO_SUPPLIERS]}, "inventory_items",
        )

        assert "suppliers_name" not in rows[0]
        assert "inventory_items_name" in rows[0]

    async def test_a_selected_column_that_is_switched_off_fails_loudly(
        self, datasource: SimpleNamespace
    ) -> None:
        datasource.configuration_data = _switched_off(inventory_items=["qty"])

        with pytest.raises(ToolQueryError, match="is inactive"):
            await execute_tool_query(
                datasource,
                {"columns": [{"column": "qty", "alias": ""}]},
                "inventory_items",
            )

    async def test_a_filter_column_that_is_switched_off_fails_loudly(
        self, datasource: SimpleNamespace
    ) -> None:
        """Dropping the filter would widen the result set — the tool would still
        return a number, and it would be the wrong one."""
        datasource.configuration_data = _switched_off(inventory_items=["qty"])

        with pytest.raises(ToolQueryError, match="is inactive"):
            await execute_tool_query(
                datasource,
                {
                    "columns": [{"column": "name", "alias": ""}],
                    "filters": [{"column": "qty", "operator": ">", "value": "0"}],
                },
                "inventory_items",
            )

    async def test_a_group_by_column_that_is_switched_off_fails_loudly(
        self, datasource: SimpleNamespace
    ) -> None:
        datasource.configuration_data = _switched_off(inventory_items=["name"])

        with pytest.raises(ToolQueryError, match="is inactive"):
            await execute_tool_query(
                datasource,
                {
                    "aggregations": [{"type": "count", "column": "id", "alias": ""}],
                    "group_by": ["name"],
                },
                "inventory_items",
            )

    async def test_a_join_key_that_is_switched_off_fails_loudly(
        self, datasource: SimpleNamespace
    ) -> None:
        """A join on a switched-off column reads it to decide which rows come back —
        it just does not show it."""
        datasource.configuration_data = _switched_off(suppliers=["item_id"])

        with pytest.raises(ToolQueryError, match="is inactive"):
            await execute_tool_query(
                datasource, {"joins": [JOIN_TO_SUPPLIERS]}, "inventory_items",
            )

    async def test_an_inactive_table_fails_before_it_is_read(
        self, datasource: SimpleNamespace
    ) -> None:
        datasource.configuration_data = _switched_off(inventory_items=None)

        with pytest.raises(ToolQueryError, match="'inventory_items' is inactive"):
            await execute_tool_query(datasource, {}, "inventory_items")

    async def test_an_inactive_joined_table_fails_too(
        self, datasource: SimpleNamespace
    ) -> None:
        datasource.configuration_data = _switched_off(suppliers=None)

        with pytest.raises(ToolQueryError, match="'suppliers' is inactive"):
            await execute_tool_query(
                datasource, {"joins": [JOIN_TO_SUPPLIERS]}, "inventory_items",
            )

    async def test_a_table_with_nothing_active_left_says_so(
        self, datasource: SimpleNamespace
    ) -> None:
        """An empty SELECT list would fail in the driver with a message about syntax,
        which tells the operator nothing about what to fix."""
        datasource.configuration_data = _switched_off(
            inventory_items=["id", "name", "qty"],
        )

        with pytest.raises(ToolQueryError, match="nothing to return"):
            await execute_tool_query(datasource, {}, "inventory_items")

    async def test_sql_mode_still_reads_an_inactive_column(
        self, datasource: SimpleNamespace
    ) -> None:
        """The documented exemption, and the reason it is stated rather than fudged:
        there is no config to inspect and rewriting the operator's statement is what
        this module refuses to do, so a switched-off *column* is still readable by a
        statement that names it."""
        datasource.configuration_data = _switched_off(inventory_items=["qty"])

        rows = await execute_tool_query(
            datasource,
            {},
            "inventory_items",
            sql_query="SELECT qty FROM inventory_items ORDER BY qty",
        )

        assert [row["qty"] for row in rows] == [0, 5, 10]


class TestSqlModeChecksTheTablesItRecords:
    """
    SQL mode gets the *table* half of the active rule. Nothing here parses a FROM
    clause, so the check uses the table list the operator recorded on the tool config
    — which is the whole reason the form asks for it.
    """

    async def test_an_inactive_primary_table_stops_the_statement(
        self, datasource: SimpleNamespace
    ) -> None:
        datasource.configuration_data = _switched_off(inventory_items=None)

        with pytest.raises(ToolQueryError, match="'inventory_items' is inactive"):
            await execute_tool_query(
                datasource,
                {},
                "inventory_items",
                sql_query="SELECT name FROM inventory_items",
                table_names=["inventory_items"],
            )

    async def test_an_inactive_joined_table_stops_it_too(
        self, datasource: SimpleNamespace
    ) -> None:
        """The case that could not be caught before the tables were recorded: the
        statement joins suppliers, and only the tool config knows that."""
        datasource.configuration_data = _switched_off(suppliers=None)

        with pytest.raises(ToolQueryError, match="'suppliers' is inactive"):
            await execute_tool_query(
                datasource,
                {},
                "inventory_items",
                sql_query=(
                    "SELECT i.name FROM inventory_items i "
                    "JOIN suppliers s ON s.item_id = i.id"
                ),
                table_names=["inventory_items", "suppliers"],
            )

    async def test_active_tables_run_as_written(
        self, datasource: SimpleNamespace
    ) -> None:
        rows = await execute_tool_query(
            datasource,
            {},
            "inventory_items",
            sql_query=(
                "SELECT i.name AS item, s.name AS supplier FROM inventory_items i "
                "JOIN suppliers s ON s.item_id = i.id ORDER BY s.name"
            ),
            table_names=["inventory_items", "suppliers"],
        )

        assert [row["supplier"] for row in rows] == ["Acme", "Globex"]

    async def test_a_tool_with_no_recorded_list_checks_its_primary_table(
        self, datasource: SimpleNamespace
    ) -> None:
        """Rows written before the list existed have one table, and it is still
        checked."""
        datasource.configuration_data = _switched_off(inventory_items=None)

        with pytest.raises(ToolQueryError, match="'inventory_items' is inactive"):
            await execute_tool_query(
                datasource,
                {},
                "inventory_items",
                sql_query="SELECT name FROM inventory_items",
            )

    async def test_an_unconfigured_datasource_reads_everything(
        self, datasource: SimpleNamespace
    ) -> None:
        """The default that keeps every pre-existing datasource working: nothing
        recorded means nothing switched off."""
        datasource.configuration_data = None

        rows = await execute_tool_query(datasource, {}, "inventory_items")

        assert set(rows[0]) == {"id", "name", "qty"}


class TestNonRelationalDatasources:
    async def test_a_mongo_datasource_is_refused_with_a_relayable_message(
        self,
    ) -> None:
        mongo = SimpleNamespace(db_type="mongodb", datasource_name="events")

        with pytest.raises(ToolQueryError, match="only relational databases"):
            await execute_tool_query(mongo, {}, "events", sql_query="SELECT 1")


class TestProbeToolQuery:
    """
    The *Test Query* button's entry point.

    Two things make it worth its own tests. It has to run **the same query** the
    agent would — a test over an approximation would pass for queries that then
    fail in a conversation — and it has to let the real failure through, because
    the operator reading it is the one who has to fix the query. The agent-facing
    entry point does the opposite with both, deliberately.
    """

    async def test_a_working_query_reports_its_shape(
        self, datasource: SimpleNamespace
    ) -> None:
        result = await probe_tool_query(
            datasource,
            {},
            "inventory_items",
            sql_query="SELECT name, qty FROM inventory_items",
        )

        assert result == {"columns": ["name", "qty"], "row_count": 1}

    async def test_only_one_row_is_read(self, datasource: SimpleNamespace) -> None:
        """Proving the query runs needs a row fetched, not a table transferred."""
        result = await probe_tool_query(
            datasource, {}, "inventory_items", sql_query="SELECT * FROM inventory_items"
        )

        assert result["row_count"] == PROBE_ROWS == 1

    async def test_a_query_matching_nothing_still_passes(
        self, datasource: SimpleNamespace
    ) -> None:
        """An empty result is a valid query over data that does not match — the
        caller words it, but it must not arrive here as a failure."""
        result = await probe_tool_query(
            datasource,
            {},
            "inventory_items",
            sql_query="SELECT name FROM inventory_items WHERE qty > 9999",
        )

        assert result == {"columns": [], "row_count": 0}

    async def test_a_builder_config_is_run_the_way_a_tool_would_run_it(
        self, datasource: SimpleNamespace
    ) -> None:
        result = await probe_tool_query(
            datasource,
            {
                "columns": [{"column": "name", "alias": ""}],
                "aggregations": [{"type": "count", "column": "id", "alias": "n"}],
                "group_by": ["name"],
            },
            "inventory_items",
        )

        assert result["columns"] == ["name", "n"]

    async def test_the_database_s_own_error_is_left_intact(
        self, datasource: SimpleNamespace
    ) -> None:
        """The whole point of the button: `execute_tool_query` would have replaced
        this with "the query could not be run", which names nothing to fix."""
        with pytest.raises(SQLAlchemyError, match="nope"):
            await probe_tool_query(
                datasource,
                {},
                "inventory_items",
                sql_query="SELECT nope FROM inventory_items",
            )

    async def test_a_write_is_refused_before_it_reaches_the_database(
        self, datasource: SimpleNamespace
    ) -> None:
        """Testing is not a way around the read-only rule."""
        with pytest.raises(HTTPException):
            await probe_tool_query(
                datasource,
                {},
                "inventory_items",
                sql_query="DELETE FROM inventory_items",
            )

        rows = await execute_tool_query(
            datasource, {}, "inventory_items", sql_query="SELECT * FROM inventory_items"
        )
        assert len(rows) == 3

    async def test_an_inactive_column_fails_the_test_as_it_fails_a_run(
        self, datasource: SimpleNamespace
    ) -> None:
        datasource.configuration_data = _switched_off(inventory_items=["qty"])

        with pytest.raises(ToolQueryError, match="'inventory_items.qty' is inactive"):
            await probe_tool_query(
                datasource,
                {"columns": [{"column": "qty", "alias": ""}]},
                "inventory_items",
            )

    async def test_the_failure_carries_no_advice_meant_for_an_agent(
        self, datasource: SimpleNamespace
    ) -> None:
        """"Tell the user the tool needs reconfiguring" is addressed to a model in a
        conversation. The operator pressing Test *is* the user."""
        datasource.configuration_data = _switched_off(inventory_items=None)

        with pytest.raises(ToolQueryError) as excinfo:
            await probe_tool_query(
                datasource,
                {},
                "inventory_items",
                sql_query="SELECT name FROM inventory_items",
            )

        assert "Tell the user" not in str(excinfo.value)
        assert "Tell the user" in excinfo.value.for_agent


class TestTheAgentStillGetsItsAdvice:
    """The other half of that split: nothing the model reads may lose the
    instruction about what to do, because a bare fault reads as something it should
    work around."""

    async def test_a_tool_failure_tells_the_model_what_to_do(
        self, datasource: SimpleNamespace
    ) -> None:
        datasource.configuration_data = _switched_off(inventory_items=None)

        with pytest.raises(ToolQueryError) as excinfo:
            await execute_tool_query(datasource, {}, "inventory_items")

        assert excinfo.value.for_agent.endswith(NEEDS_RECONFIGURING)

    async def test_a_non_relational_datasource_keeps_its_own_advice(self) -> None:
        mongo = SimpleNamespace(db_type="mongodb", datasource_name="events")

        with pytest.raises(ToolQueryError) as excinfo:
            await execute_tool_query(mongo, {}, "events", sql_query="SELECT 1")

        assert excinfo.value.for_agent.endswith(NOT_AVAILABLE)


class TestValueBindings:
    """
    Values another tool produced, bound into this query — the nested-tool feature's
    half of the executor.

    The rule that matters is the same one the stored filters have: what arrives is
    **data**, never SQL. A value shaped like an injection has to come back as a
    value that matches nothing, in both modes.
    """

    async def test_a_builder_query_is_narrowed_by_the_values(
        self, datasource: SimpleNamespace
    ) -> None:
        rows = await execute_tool_query(
            datasource,
            {"columns": [{"column": "name", "alias": ""}]},
            "inventory_items",
            value_bindings=[{"reference": "id", "values": [1, 3]}],
        )

        assert sorted(row["name"] for row in rows) == ["bolt", "bolt"]

    async def test_a_sql_query_takes_them_as_an_expanding_bind_parameter(
        self, datasource: SimpleNamespace
    ) -> None:
        """One `:name` in the statement stands for the whole list, and the
        statement itself is never rewritten."""
        rows = await execute_tool_query(
            datasource,
            {},
            "inventory_items",
            sql_query=(
                "SELECT name FROM inventory_items WHERE id IN :wanted ORDER BY id"
            ),
            value_bindings=[{"reference": "wanted", "values": [2, 3]}],
        )

        assert [row["name"] for row in rows] == ["nut", "bolt"]

    async def test_the_values_are_bound_and_not_written_into_the_query(
        self, datasource: SimpleNamespace
    ) -> None:
        rows = await execute_tool_query(
            datasource,
            {"columns": [{"column": "name", "alias": ""}]},
            "inventory_items",
            value_bindings=[{"reference": "id", "values": ["1) OR (1=1"]}],
        )

        assert rows == []

    async def test_they_narrow_rather_than_replace_the_stored_filters(
        self, datasource: SimpleNamespace
    ) -> None:
        rows = await execute_tool_query(
            datasource,
            {
                "columns": [{"column": "name", "alias": ""}],
                "filters": [{"column": "qty", "operator": ">", "value": "6"}],
            },
            "inventory_items",
            value_bindings=[{"reference": "id", "values": [1, 3]}],
        )

        assert [row["name"] for row in rows] == ["bolt"]

    async def test_a_switched_off_column_cannot_be_reached_through_a_binding(
        self, datasource: SimpleNamespace
    ) -> None:
        """Nesting gets no privilege the stored config does not have — the binding
        goes through the same resolver as every other reference."""
        datasource.configuration_data = _switched_off(inventory_items=["qty"])

        with pytest.raises(ToolQueryError, match="'inventory_items.qty' is inactive"):
            await execute_tool_query(
                datasource,
                {"columns": [{"column": "name", "alias": ""}]},
                "inventory_items",
                value_bindings=[{"reference": "qty", "values": [10]}],
            )

    @pytest.mark.parametrize("sql_query", [None, "SELECT name FROM inventory_items"])
    async def test_an_empty_list_of_values_is_refused_in_either_mode(
        self, datasource: SimpleNamespace, sql_query
    ) -> None:
        """`IN ()` is a query that should never have been built: the chain runner
        stops before the parent runs. Reaching here with none is its bug to hear
        about."""
        with pytest.raises(ToolQueryError, match="returned no values"):
            await execute_tool_query(
                datasource,
                {"columns": [{"column": "name", "alias": ""}]},
                "inventory_items",
                sql_query=sql_query,
                value_bindings=[{"reference": "id", "values": []}],
            )


class TestExecuteValueQuery:
    """
    The inner half of a chain: one column of a tool's result, for the tool above it.
    """

    async def test_it_returns_one_column_of_values(
        self, datasource: SimpleNamespace
    ) -> None:
        values = await execute_value_query(
            datasource,
            {"columns": [{"column": "id", "alias": ""}]},
            "inventory_items",
            "id",
        )

        assert sorted(values) == [1, 2, 3]

    async def test_duplicates_are_collapsed(
        self, datasource: SimpleNamespace
    ) -> None:
        """Two rows naming the same client restrict the outer query once."""
        values = await execute_value_query(
            datasource,
            {"columns": [{"column": "name", "alias": ""}]},
            "inventory_items",
            "name",
        )

        assert sorted(values) == ["bolt", "nut"]

    async def test_nulls_are_dropped(self, datasource: SimpleNamespace) -> None:
        """A NULL never matches an IN comparison, so carrying it forward would only
        inflate the list."""
        values = await execute_value_query(
            datasource,
            {
                "columns": [{"column": "name", "alias": ""}],
                "filters": [{"column": "qty", "operator": ">", "value": "9999"}],
            },
            "inventory_items",
            "name",
        )

        assert values == []

    async def test_every_value_comes_back(
        self, datasource: SimpleNamespace
    ) -> None:
        values = await execute_value_query(
            datasource,
            {"columns": [{"column": "id", "alias": ""}]},
            "inventory_items",
            "id",
        )

        assert len(values) == 3

    async def test_a_column_the_query_does_not_return_is_named(
        self, datasource: SimpleNamespace
    ) -> None:
        with pytest.raises(ToolQueryError, match="does not return a column"):
            await execute_value_query(
                datasource,
                {"columns": [{"column": "name", "alias": ""}]},
                "inventory_items",
                "nope",
            )

    async def test_a_sql_tool_can_be_the_inner_one(
        self, datasource: SimpleNamespace
    ) -> None:
        values = await execute_value_query(
            datasource,
            {},
            "inventory_items",
            "name",
            sql_query="SELECT DISTINCT name FROM inventory_items",
        )

        assert sorted(values) == ["bolt", "nut"]


class TestNothingCapsWhatAQueryReturns:
    """
    The rule that replaced the 200-row ceiling, in both query modes and on the value
    path — every matching row, unless the caller asked for a number.

    Worth its own class because the old cap was invisible from the result. 200 rows of a
    2,500-row table is a perfectly ordinary-looking answer, so nothing downstream could
    have noticed it was a sample, and a total taken over it was a plausible wrong figure
    rather than a failure anybody would have reported.
    """

    async def test_a_sql_mode_tool_returns_every_row(
        self, large_datasource: SimpleNamespace
    ) -> None:
        rows = await execute_tool_query(
            large_datasource, {}, "readings", sql_query="SELECT * FROM readings",
        )

        assert len(rows) == LARGE_ROWS

    async def test_a_builder_mode_tool_returns_every_row(
        self, large_datasource: SimpleNamespace
    ) -> None:
        """Both modes, because they take different paths to the rows: one streams the
        operator's statement, the other runs a ``Select`` assembled here."""
        rows = await execute_tool_query(
            large_datasource,
            {"columns": [{"column": "label", "alias": ""}]},
            "readings",
        )

        assert len(rows) == LARGE_ROWS

    async def test_an_inner_tool_hands_up_every_value(
        self, large_datasource: SimpleNamespace
    ) -> None:
        """
        Past the 2,000 an ``IN`` list used to be refused at. A truncated list would have
        built a filter that ran, returned rows, and answered a different question than
        the one asked — with nothing in the result saying so.
        """
        values = await execute_value_query(
            large_datasource,
            {"columns": [{"column": "id", "alias": ""}]},
            "readings",
            "id",
        )

        assert len(values) == LARGE_ROWS

    async def test_the_operators_own_limit_is_still_the_one_that_counts(
        self, large_datasource: SimpleNamespace
    ) -> None:
        """A ``LIMIT`` in the statement is a statement about the question; a cap
        underneath it was a statement about nothing the author could see."""
        rows = await execute_tool_query(
            large_datasource,
            {},
            "readings",
            sql_query="SELECT * FROM readings LIMIT 7",
        )

        assert len(rows) == 7


class TestDescribeResult:
    def test_no_rows_is_stated_as_a_result_not_a_failure(self) -> None:
        assert describe_result([]) == "0 rows. The query returned no data."

    def test_a_large_result_is_sampled_for_the_prompt_with_the_exact_total(
        self,
    ) -> None:
        """
        The one bound left, and the trade that makes it honest.

        A prompt is a fixed size, so only :data:`PROMPT_ROW_LIMIT` rows are serialised.
        What the uncapped fetch bought is the number in the header: every row was read,
        so the total is exact and stated, where the old text could only warn that a
        total was unknowable.
        """
        described = describe_result([{"n": index} for index in range(2500)])

        assert "200 row(s) out of 2500 matching record(s)" in described
        assert "the total is the figure to report" in described
        # The 201st row is not in the prompt; the fact that it exists is.
        assert '"n": 199' in described
        assert '"n": 200' not in described

    def test_a_result_that_fits_is_named_as_complete_without_a_count(self) -> None:
        """
        A caller with no count is no longer made to invent one: it is holding every row
        it matched, so their number *is* the total. Before, 30 rows with no count said
        "30 row(s)" and left a model to guess whether more existed.
        """
        described = describe_result([{"n": 1}] * 30)

        assert "30 row(s), which is the complete result" in described

    def test_a_known_total_replaces_the_warning_with_the_figure(self) -> None:
        """
        The point of the count: "this might not be all of them" becomes "there are 4821".
        """
        described = describe_result([{"n": 1}] * 30, total_rows=4821)

        assert "30 row(s) out of 4821 matching record(s)" in described
        assert "the total is the figure to report" in described
        assert "capped" not in described

    def test_a_complete_result_is_named_as_complete(self) -> None:
        """
        Otherwise a model shown 12 of 12 rows has no way to tell them from a sample, and
        hedges an answer it should state plainly.
        """
        described = describe_result([{"n": 1}] * 12, total_rows=12)

        assert "12 row(s), which is the complete result" in described

    def test_an_inexact_total_is_reported_as_a_lower_bound(self) -> None:
        described = describe_result(
            [{"n": 1}] * 30, total_rows=500_001, count_is_lower_bound=True,
        )

        assert "out of at least 500001" in described

    def test_the_display_budget_is_stated_only_when_it_bites(self) -> None:
        assert (
            f"Print at most {DISPLAY_ROW_LIMIT}"
            in describe_result([{"n": 1}] * 30, total_rows=4821)
        )
        assert "Print at most" not in describe_result(
            [{"n": 1}] * 5, total_rows=DISPLAY_ROW_LIMIT,
        )

    def test_the_offer_sentence_is_passed_through_verbatim(self) -> None:
        """
        It carries the record count and the promise of a file. A model rewording either is
        how a user is told the wrong number or offered something that will not arrive — so
        it is quoted, with an instruction to repeat it exactly.
        """
        offer = (
            "There are 4821 records. Do you want me to create a downloadable CSV file "
            "containing the list of all the records."
        )

        described = describe_result([{"n": 1}] * 30, total_rows=4821, offer=offer)

        assert "word for word" in described
        assert described.endswith(offer)

    def test_nothing_is_truncated(self) -> None:
        """
        The budget is an instruction, not a cut. Truncating here would take the other rows
        away from the *model* too, leaving it unable to answer the question it was asked.
        """
        described = describe_result([{"n": index} for index in range(100)], total_rows=100)

        assert '{"n": 99}' in described


# ---- Agent-supplied filter values ----

class TestAgentSuppliedFilters:
    """
    An operator opens one filter's *value* to the agent; everything else about the
    query stays theirs.

    These tests are written from the two things that must both hold: the agent can
    change the value (or the feature is pointless), and it can change nothing else
    (or the feature is a SQL injection with extra steps).
    """

    def _config(self, **overrides) -> dict:  # noqa: ANN003
        filter_entry = {
            "column": "qty",
            "operator": ">",
            "agent_supplied": True,
            "required": True,
            "param": "min_qty",
        }
        filter_entry.update(overrides)
        return {
            "columns": [{"column": "name", "alias": ""}],
            "aggregations": [],
            "group_by": [],
            "joins": [],
            "filters": [filter_entry],
        }

    async def test_the_agents_value_narrows_the_query(
        self, datasource: SimpleNamespace,
    ) -> None:
        rows = await execute_tool_query(
            datasource, self._config(), "inventory_items",
            agent_values={"min_qty": "6"},
        )

        assert [row["name"] for row in rows] == ["bolt"]

    async def test_a_different_value_gives_a_different_result(
        self, datasource: SimpleNamespace,
    ) -> None:
        """
        The whole point. One tool config, two questions, two answers — which is what
        a tool per date range was being created to achieve.
        """
        rows = await execute_tool_query(
            datasource, self._config(), "inventory_items",
            agent_values={"min_qty": "1"},
        )

        assert sorted(row["name"] for row in rows) == ["bolt", "bolt"]

    async def test_the_value_is_bound_and_not_concatenated(
        self, datasource: SimpleNamespace,
    ) -> None:
        """
        The security property, tested the way it would actually be attacked.

        A model that emitted SQL instead of a value must produce a comparison that
        matches nothing, not a second statement. Coercion against the reflected
        INTEGER column turns this into a literal that no row equals.
        """
        rows = await execute_tool_query(
            datasource, self._config(operator="="), "inventory_items",
            agent_values={"min_qty": "0 OR 1=1 --"},
        )

        assert rows == []

    async def test_the_agent_cannot_reach_a_column_the_operator_did_not_open(
        self, datasource: SimpleNamespace,
    ) -> None:
        """
        A value is a value. Passing a column reference as one compares against the
        string, it does not switch which column is filtered.
        """
        rows = await execute_tool_query(
            datasource, self._config(operator="="), "inventory_items",
            agent_values={"min_qty": "id"},
        )

        assert rows == []

    async def test_a_fixed_filter_beside_it_still_applies(
        self, datasource: SimpleNamespace,
    ) -> None:
        """
        The guarantee that made parameterised filters worth refusing until now: a
        tool scoped to something is a decision, and opening one filter must not
        relax another.
        """
        config = self._config()
        config["filters"].append({
            "column": "name", "operator": "=", "value": "nut",
        })

        rows = await execute_tool_query(
            datasource, config, "inventory_items", agent_values={"min_qty": "0"},
        )

        # qty > 0 AND name = 'nut' — the nut has qty 0, so the fixed filter wins.
        assert rows == []

    async def test_a_missing_required_value_refuses_rather_than_widening(
        self, datasource: SimpleNamespace,
    ) -> None:
        """
        Dropping the clause would return every row and look like a working answer.
        The model is told what to do instead, by name.
        """
        with pytest.raises(ToolQueryError) as caught:
            await execute_tool_query(
                datasource, self._config(), "inventory_items", agent_values={},
            )

        assert "min_qty" in str(caught.value)
        assert "min_qty" in caught.value.for_agent
        assert "Do not invent one" in caught.value.for_agent

    async def test_an_empty_string_counts_as_missing(
        self, datasource: SimpleNamespace,
    ) -> None:
        """A model that fills a required field with "" has not supplied a value."""
        with pytest.raises(ToolQueryError):
            await execute_tool_query(
                datasource, self._config(), "inventory_items",
                agent_values={"min_qty": "   "},
            )

    async def test_an_optional_value_left_out_drops_only_its_own_clause(
        self, datasource: SimpleNamespace,
    ) -> None:
        """
        The operator ticked "not required", so an unfiltered result on that column
        is what they asked for — and every other filter is untouched.
        """
        config = self._config(required=False)
        config["filters"].append({
            "column": "name", "operator": "=", "value": "bolt",
        })

        rows = await execute_tool_query(
            datasource, config, "inventory_items", agent_values={},
        )

        assert sorted(row["name"] for row in rows) == ["bolt", "bolt"]

    async def test_a_tool_with_no_agent_filters_ignores_supplied_values(
        self, datasource: SimpleNamespace,
    ) -> None:
        """
        Nothing reads agent_values but a filter that opted in, so a stray argument
        cannot reach a query that declared no parameters.
        """
        config = {
            "columns": [], "aggregations": [], "group_by": [], "joins": [],
            "filters": [
                {"column": "name", "operator": "=", "value": "nut"},
            ],
        }

        rows = await execute_tool_query(
            datasource, config, "inventory_items",
            agent_values={"name": "bolt", "anything": "at all"},
        )

        assert [row["name"] for row in rows] == ["nut"]


# ---- Value-less filter operators ----

class TestNullAndBlankOperators:
    """
    The operators that compare against nothing.

    They exist because `!= ''` is the trap they replace: the builder ANDs its
    conditions, so a filter excluding empty strings silently keeps every NULL row,
    and the operator who wrote it has no way to see that from the form. These tests
    are written over a table holding all four states — a value, NULL, '' and '   ' —
    because that is the only way to tell the four operators apart.
    """

    @pytest.fixture
    def mixed(self, tmp_path: Path) -> SimpleNamespace:
        """A table whose text column holds every kind of "no value" there is."""
        path = tmp_path / "mixed.db"
        connection = sqlite3.connect(path)
        connection.executescript(
            """
            CREATE TABLE rows_of_all_kinds (
                id INTEGER PRIMARY KEY,
                label TEXT,
                qty INTEGER
            );
            INSERT INTO rows_of_all_kinds (id, label, qty) VALUES
                (1, 'python', 10),
                (2, NULL, NULL),
                (3, '', 0),
                (4, '   ', 5);
            """
        )
        connection.commit()
        connection.close()

        return SimpleNamespace(
            db_type="sqlite", database_name=str(path), datasource_name="mixed",
            host=None, port=None, username=None, password_encrypted=None,
            configuration_data={},
        )

    async def _ids(self, datasource, operator: str, column: str = "label") -> list:
        rows = await execute_tool_query(
            datasource,
            {
                "columns": [{"column": "id", "alias": ""}],
                "aggregations": [], "group_by": [], "joins": [],
                "filters": [{"column": column, "operator": operator}],
            },
            "rows_of_all_kinds",
        )
        return sorted(row["id"] for row in rows)

    async def test_is_null_matches_only_the_null(self, mixed) -> None:  # noqa: ANN001
        assert await self._ids(mixed, "IS NULL") == [2]

    async def test_is_not_null_keeps_the_empty_and_the_whitespace(self, mixed) -> None:  # noqa: ANN001
        """
        The distinction that matters. '' and '   ' are *not* null, so IS NOT NULL
        keeps them — which is why IS NOT BLANK has to exist separately.
        """
        assert await self._ids(mixed, "IS NOT NULL") == [1, 3, 4]

    async def test_is_blank_matches_null_empty_and_whitespace(self, mixed) -> None:  # noqa: ANN001
        assert await self._ids(mixed, "IS BLANK") == [2, 3, 4]

    async def test_is_not_blank_is_the_one_that_means_has_a_value(self, mixed) -> None:  # noqa: ANN001
        """
        What an operator means by "not empty", and what `!= ''` failed to give them:
        that filter would have returned rows 1, 3 and 4 — keeping the whitespace —
        while also dropping the NULL for a reason nobody asked for.
        """
        assert await self._ids(mixed, "IS NOT BLANK") == [1]

    async def test_blank_on_a_numeric_column_is_just_null(self, mixed) -> None:  # noqa: ANN001
        """
        TRIM(integer) is not a stricter check on Postgres, it is an error — and a
        number has no empty string to catch, so IS NULL is the whole of what blank
        can mean for one.
        """
        assert await self._ids(mixed, "IS BLANK", column="qty") == [2]
        assert await self._ids(mixed, "IS NOT BLANK", column="qty") == [1, 3, 4]

    async def test_a_value_less_filter_combines_with_an_ordinary_one(self, mixed) -> None:  # noqa: ANN001
        """They are ANDed like any other pair, which is the point of having both."""
        rows = await execute_tool_query(
            mixed,
            {
                "columns": [{"column": "id", "alias": ""}],
                "aggregations": [], "group_by": [], "joins": [],
                "filters": [
                    {"column": "label", "operator": "IS NOT BLANK"},
                    {"column": "qty", "operator": ">", "value": "5"},
                ],
            },
            "rows_of_all_kinds",
        )

        assert [row["id"] for row in rows] == [1]

    async def test_a_stored_value_is_ignored_rather_than_breaking_the_query(
        self, mixed,  # noqa: ANN001
    ) -> None:
        """
        A row hand-edited to carry both an IS NULL and a leftover value still runs.
        The builders for these take the argument and discard it, so there is no
        shape of stored config that turns into a broken statement here.
        """
        rows = await execute_tool_query(
            mixed,
            {
                "columns": [{"column": "id", "alias": ""}],
                "aggregations": [], "group_by": [], "joins": [],
                "filters": [
                    {"column": "label", "operator": "IS NULL", "value": "ignored"},
                ],
            },
            "rows_of_all_kinds",
        )

        assert [row["id"] for row in rows] == [2]


class TestScalarValueBindings:
    """
    A binding that stands for **one** value rather than a list.

    The whole reason the shape exists: an expanding parameter always renders
    parenthesised, so it is only ever valid on the right of an ``IN``. A scalar one
    goes anywhere a value goes — an equality, a concatenation, a function argument —
    which is what lets a chain drive a query the ``IN`` form cannot express.
    """

    async def test_a_builder_query_compares_rather_than_matching_a_list(
        self, datasource: SimpleNamespace
    ) -> None:
        rows = await execute_tool_query(
            datasource,
            {"columns": [{"column": "name", "alias": ""}]},
            "inventory_items",
            value_bindings=[
                {"reference": "id", "values": [2], "expanding": False},
            ],
        )

        assert [row["name"] for row in rows] == ["nut"]

    async def test_a_sql_query_can_put_it_where_a_list_would_be_a_syntax_error(
        self, datasource: SimpleNamespace
    ) -> None:
        """``'item-' || :x`` is the case the mode exists for: an expanding
        parameter renders as ``(?)`` and the statement will not parse."""
        rows = await execute_tool_query(
            datasource,
            {},
            "inventory_items",
            sql_query=(
                "SELECT name, 'item-' || :wanted AS tag FROM inventory_items "
                "WHERE id = :wanted"
            ),
            value_bindings=[
                {"reference": "wanted", "values": [3], "expanding": False},
            ],
        )

        assert rows == [{"name": "bolt", "tag": "item-3"}]

    async def test_it_is_still_bound_and_never_written_into_the_query(
        self, datasource: SimpleNamespace
    ) -> None:
        """The scalar shape changes where a value may sit, not what it is."""
        rows = await execute_tool_query(
            datasource,
            {"columns": [{"column": "name", "alias": ""}]},
            "inventory_items",
            value_bindings=[
                {"reference": "id", "values": ["1 OR 1=1"], "expanding": False},
            ],
        )

        assert rows == []

    async def test_an_omitted_flag_still_means_the_list(
        self, datasource: SimpleNamespace
    ) -> None:
        """Every caller written before iterating links existed keeps its
        behaviour — which is what makes the column's default safe."""
        rows = await execute_tool_query(
            datasource,
            {"columns": [{"column": "name", "alias": ""}]},
            "inventory_items",
            value_bindings=[{"reference": "id", "values": [1, 2]}],
        )

        assert len(rows) == 2

    async def test_no_values_is_refused_in_either_shape(
        self, datasource: SimpleNamespace
    ) -> None:
        """An empty binding is a bug in the chain runner, which stops before the
        parent runs — so it says so rather than building a query matching nothing."""
        with pytest.raises(ToolQueryError, match="returned no values"):
            await execute_tool_query(
                datasource,
                {"columns": [{"column": "name", "alias": ""}]},
                "inventory_items",
                value_bindings=[
                    {"reference": "id", "values": [], "expanding": False},
                ],
            )


class TestLabelledRows:
    """
    ``labelled_rows`` — writing the value that produced a row alongside it.

    Rows from twenty runs of one statement are indistinguishable once concatenated,
    and a statement that filters on a department without selecting it is ordinary
    SQL. This is the only thing that closes that, and it does it in Python rather
    than by rewriting the statement.
    """

    def test_it_writes_the_value_onto_every_row(self) -> None:
        rows = labelled_rows(
            [{"name": "bolt"}, {"name": "nut"}], {"department_id": 4},
        )

        assert rows == [
            {"name": "bolt", "department_id": 4},
            {"name": "nut", "department_id": 4},
        ]

    def test_no_label_leaves_the_rows_exactly_as_they_were(self) -> None:
        original = [{"name": "bolt"}]

        assert labelled_rows(original, None) is original

    def test_a_collision_is_refused_rather_than_overwritten(self) -> None:
        """
        Overwriting replaces a real value from the database with one from the chain;
        skipping leaves a label that says nothing about the row. Both look right and
        are not, which is the failure this module exists to avoid.
        """
        with pytest.raises(ToolQueryError, match="already returns a column"):
            labelled_rows([{"name": "bolt", "dept": 1}], {"dept": 9})

    def test_the_refusal_says_what_to_do_about_it(self) -> None:
        with pytest.raises(ToolQueryError) as caught:
            labelled_rows([{"dept": 1}], {"dept": 9})

        assert "Choose a different name" in str(caught.value)


class TestDeclaredSqlParameters:
    """
    Values a SQL-mode statement declares and the model fills in.

    Builder mode opens a *filter*, which has a column and an operator the operator
    chose. A statement has neither — nothing here parses it — so the operator
    declares the value and writes the comparison themselves. What the model supplies
    is still only ever the right-hand side of one.
    """

    async def test_the_model_s_value_is_bound_into_the_statement(
        self, datasource: SimpleNamespace
    ) -> None:
        rows = await execute_tool_query(
            datasource,
            {},
            "inventory_items",
            sql_query="SELECT name FROM inventory_items WHERE qty > :floor",
            sql_params=[{"param": "floor", "type": "number", "required": True}],
            agent_values={"floor": "6"},
        )

        assert [row["name"] for row in rows] == ["bolt"]

    async def test_a_number_parameter_is_typed_before_it_is_bound(
        self, datasource: SimpleNamespace
    ) -> None:
        """
        A tool argument always arrives as a string, and a SQL statement has no
        reflected column to coerce against — so the operator's declared type is the
        only thing that can make ``= :x`` work on a strict driver.
        """
        rows = await execute_tool_query(
            datasource,
            {},
            "inventory_items",
            sql_query="SELECT name FROM inventory_items WHERE id = :wanted",
            sql_params=[{"param": "wanted", "type": "number", "required": True}],
            agent_values={"wanted": "2"},
        )

        assert [row["name"] for row in rows] == ["nut"]

    async def test_a_value_shaped_like_sql_matches_nothing(
        self, datasource: SimpleNamespace
    ) -> None:
        rows = await execute_tool_query(
            datasource,
            {},
            "inventory_items",
            sql_query="SELECT name FROM inventory_items WHERE name = :wanted",
            sql_params=[{"param": "wanted", "required": True}],
            agent_values={"wanted": "bolt' OR '1'='1"},
        )

        assert rows == []

    async def test_a_missing_required_value_tells_the_model_to_ask(
        self, datasource: SimpleNamespace
    ) -> None:
        """Never invented. A value nobody gave is a question for the visitor, not a
        gap for the model to fill."""
        with pytest.raises(ToolQueryError) as caught:
            await execute_tool_query(
                datasource,
                {},
                "inventory_items",
                sql_query="SELECT name FROM inventory_items WHERE qty > :floor",
                sql_params=[{"param": "floor", "required": True}],
                agent_values={},
            )

        assert "needs a value for 'floor'" in str(caught.value)
        assert "Do not invent one" in caught.value.advice

    async def test_a_missing_optional_value_binds_null(
        self, datasource: SimpleNamespace
    ) -> None:
        """Which is what a statement written as ``(:x IS NULL OR col = :x)`` reads
        as "no filter" — the idiom the optional flag exists to allow."""
        rows = await execute_tool_query(
            datasource,
            {},
            "inventory_items",
            sql_query=(
                "SELECT name FROM inventory_items "
                "WHERE (:wanted IS NULL OR name = :wanted) ORDER BY id"
            ),
            sql_params=[{"param": "wanted", "required": False}],
            agent_values={},
        )

        assert [row["name"] for row in rows] == ["bolt", "nut", "bolt"]

    async def test_a_name_the_operator_did_not_declare_has_nowhere_to_land(
        self, datasource: SimpleNamespace
    ) -> None:
        """
        The declarations are iterated, not the supplied values — so a model that
        invents an argument cannot reach a placeholder with it. Here the statement
        has none at all, and the extra value simply does not exist.
        """
        rows = await execute_tool_query(
            datasource,
            {},
            "inventory_items",
            sql_query="SELECT name FROM inventory_items WHERE id = 2",
            sql_params=[],
            agent_values={"anything": "1 OR 1=1"},
        )

        assert [row["name"] for row in rows] == ["nut"]

    async def test_an_unconvertible_number_matches_nothing_rather_than_failing(
        self, datasource: SimpleNamespace
    ) -> None:
        """"abc" for a number is a value that matches nothing, which is the right
        answer to what was asked — and a conversion error here would read to the
        model as the tool being broken."""
        rows = await execute_tool_query(
            datasource,
            {},
            "inventory_items",
            sql_query="SELECT name FROM inventory_items WHERE name = :wanted",
            sql_params=[{"param": "wanted", "type": "number"}],
            agent_values={"wanted": "abc"},
        )

        assert rows == []
