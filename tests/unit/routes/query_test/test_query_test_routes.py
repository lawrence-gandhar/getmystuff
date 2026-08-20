"""
Route-level tests for POST /query-test.

A real HTTP round trip, because the endpoint is only as good as the fragment it
returns: both panels swap this partial into a target of their own, and a verdict
that renders as the wrong alert — or renders a row of the user's data — is a
template problem no service test can see.

The datasource is a real SQLite file, so a passing test here means the whole path
ran: form → schema → validators → executor → database → partial.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.models.datasource import DataSource
from app.routes.query_test import QueryTestController


@pytest.fixture
def database(tmp_path: Path) -> Path:
    path = tmp_path / "warehouse.db"

    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE inventory_items (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            qty INTEGER NOT NULL
        );
        INSERT INTO inventory_items (id, name, qty) VALUES (1, 'bolt', 10);
        """
    )
    connection.commit()
    connection.close()

    return path


@pytest.fixture
async def datasource(db, user, database: Path) -> DataSource:  # noqa: ANN001
    row = DataSource(
        user_id=user.id,
        datasource_name="warehouse",
        db_type="sqlite",
        database_name=str(database),
        password_encrypted="",
        configuration_data={},
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


@pytest.fixture
def client(auth_client_factory):  # noqa: ANN001, ANN201
    return auth_client_factory(QueryTestController)


def form_data(datasource, **overrides) -> dict:  # noqa: ANN001
    """The Tool Configs form as `hx-include="closest form"` posts it."""
    data = {
        "datasource_id": str(datasource.uuid),
        "table_names": ["inventory_items"],
        "query_mode": "sql",
        "config_json": "",
        "sql_query": "SELECT name, qty FROM inventory_items",
        # Fields the form carries that this endpoint has no use for.
        "tool_name": "items",
        "description": "Every item.",
    }
    data.update(overrides)
    return data


class TestTheVerdict:
    def test_a_working_query_renders_the_pass(self, client, datasource) -> None:  # noqa: ANN001
        response = client.post("/query-test/", data=form_data(datasource))

        assert response.status_code == 200
        assert "Query test passed" in response.text
        assert "name, qty" in response.text

    def test_a_broken_query_renders_the_database_s_own_words(
        self, client, datasource  # noqa: ANN001
    ) -> None:
        response = client.post(
            "/query-test/",
            data=form_data(datasource, sql_query="SELECT nope FROM inventory_items"),
        )

        assert "Query test failed" in response.text
        assert "nope" in response.text

    def test_a_failure_is_not_an_http_error(self, client, datasource) -> None:  # noqa: ANN001
        """The panel renders one alert either way, so the route has no error branch
        and a refused query is still a 2xx with a fragment in it."""
        response = client.post(
            "/query-test/", data=form_data(datasource, sql_query="DELETE FROM x")
        )

        assert response.status_code == 200
        assert "read-only" in response.text

    def test_no_row_values_reach_the_page(self, client, datasource) -> None:  # noqa: ANN001
        """One row is read to prove the query runs; the panel reports the shape."""
        response = client.post("/query-test/", data=form_data(datasource))

        assert "bolt" not in response.text

    def test_a_builder_config_is_tested_from_the_json_field(
        self, client, datasource  # noqa: ANN001
    ) -> None:
        response = client.post(
            "/query-test/",
            data=form_data(
                datasource,
                query_mode="builder",
                config_json='{"columns": [{"column": "name", "alias": ""}]}',
                sql_query="",
            ),
        )

        assert "Query test passed" in response.text

    def test_the_builder_is_tested_even_when_the_sql_panel_holds_something(
        self, client, datasource  # noqa: ANN001
    ) -> None:
        """Both queries are always posted — `query_mode` says which one is meant,
        exactly as it does on save."""
        response = client.post(
            "/query-test/",
            data=form_data(
                datasource,
                query_mode="builder",
                config_json='{"columns": [{"column": "name", "alias": ""}]}',
                sql_query="SELECT nope FROM inventory_items",
            ),
        )

        assert "Query test passed" in response.text

    async def test_a_datasource_that_is_not_the_user_s_is_not_tested(
        self, client, db, make_user, database: Path  # noqa: ANN001
    ) -> None:
        stranger = await make_user("stranger@example.com")
        theirs = DataSource(
            user_id=stranger.id,
            datasource_name="theirs",
            db_type="sqlite",
            database_name=str(database),
            password_encrypted="",
            configuration_data={},
        )
        db.add(theirs)
        await db.commit()
        await db.refresh(theirs)

        response = client.post("/query-test/", data=form_data(theirs))

        assert "Query test failed" in response.text
        assert "not found" in response.text
