"""
Route-level tests for the Tool Configs offcanvas, covering the SQL-query mode.

These go through a real HTTP round trip — form render, then submit — because the
mode is carried by the templates as much as by the service: the mode selector, the
SQL panel and the builder panel are three partials that have to agree, and the
route has to pass `query_mode` through for any of it to matter. A service-level
test cannot see a partial that renders the wrong field name.

The datasource is never actually connected to: `tool_config_service`'s two live
readers are stubbed at the seam, the same way the service tests stub them. What is
being tested here is the form and the wiring, not schema reflection.
"""

from __future__ import annotations

import pytest
from litestar.exceptions import HTTPException

from app.models.data_agents import DataAgent
from app.models.datasource import DataSource
from app.models.tool_configs import ToolConfig
from app.routes.tool_configs import ToolConfigController
from app.services.tool_configs import tool_config_service as svc


@pytest.fixture(autouse=True)
def stub_datasource_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    """The form's two live reads, so no real database is contacted."""

    async def fake_objects(db, datasource_id, user_id):  # noqa: ANN001
        return {"objects": ["inventory_items", "suppliers"]}

    async def fake_schema(db, datasource_id, user_id, table_name):  # noqa: ANN001
        return {"schema": [{"column": "id"}, {"column": "name"}]}

    monkeypatch.setattr(svc.datasource_service, "get_datasource_objects", fake_objects)
    monkeypatch.setattr(
        svc.datasource_service, "get_datasource_table_schema", fake_schema
    )


@pytest.fixture(autouse=True)
def no_prompt_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    The background routing-prompt rebuild opens its own session and is not what
    these tests are about. It is an optimisation by design — deep_agent_service
    regenerates a stale prompt inline — so skipping it changes nothing observable.
    """
    from app.routes.tool_configs import tool_config_routes

    monkeypatch.setattr(tool_config_routes, "_prompt_sync_task", lambda ids: None)


@pytest.fixture
async def agent(db, user) -> DataAgent:  # noqa: ANN001
    row = DataAgent(user_id=user.id, name="reporter")
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


@pytest.fixture
async def datasource(db, user) -> DataSource:  # noqa: ANN001
    row = DataSource(
        user_id=user.id,
        datasource_name="warehouse",
        db_type="postgres",
        password_encrypted="enc",
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


@pytest.fixture
async def mongo_datasource(db, user) -> DataSource:  # noqa: ANN001
    row = DataSource(
        user_id=user.id,
        datasource_name="events",
        db_type="mongodb",
        password_encrypted="enc",
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


@pytest.fixture
def client(auth_client_factory):  # noqa: ANN001, ANN201
    return auth_client_factory(ToolConfigController)


DISTINCT_SQL = "SELECT DISTINCT name FROM inventory_items"


def form_data(agent, datasource, **overrides) -> dict:  # noqa: ANN001
    data = {
        "data_agent_id": str(agent.uuid),
        "datasource_id": str(datasource.uuid),
        "tool_name": "distinct_items",
        "table_name": "inventory_items",
        "description": "Every distinct item name.",
        "query_mode": "sql",
        "config_json": "",
        "sql_query": DISTINCT_SQL,
    }
    data.update(overrides)
    return data


class TestTheFormOffersBothModes:
    def test_the_new_form_renders_the_mode_selector_and_the_sql_panel(
        self, client
    ) -> None:  # noqa: ANN001
        body = client.get("/tool-configs/new-form").text

        assert 'name="query_mode"' in body
        assert 'name="sql_query"' in body
        assert 'data-query-mode-panel="builder"' in body
        assert 'data-query-mode-panel="sql"' in body

    def test_sql_mode_is_disabled_until_a_relational_datasource_is_picked(
        self, client
    ) -> None:  # noqa: ANN001
        """Nothing is selected yet, so nothing is known to support SQL."""
        body = client.get("/tool-configs/new-form").text

        assert "not relational" in body or "disabled" in body

    def test_picking_a_relational_datasource_enables_sql_mode(
        self, client, datasource  # noqa: ANN001
    ) -> None:
        body = client.get(
            "/tool-configs/tables", params={"datasource_id": str(datasource.uuid)}
        ).text

        assert 'id="toolQueryModeField"' in body
        assert "Choose <strong>SQL query</strong>" in body

    def test_picking_a_mongo_datasource_explains_why_sql_is_not_offered(
        self, client, mongo_datasource  # noqa: ANN001
    ) -> None:
        body = client.get(
            "/tool-configs/tables",
            params={"datasource_id": str(mongo_datasource.uuid)},
        ).text

        assert "not relational" in body


class TestCreatingASqlTool:
    async def test_a_distinct_query_is_accepted(
        self, client, db, agent, datasource  # noqa: ANN001
    ) -> None:
        """The query from the bug report: valid SQL, outside the builder's shape."""
        response = client.post(
            "/tool-configs/create", data=form_data(agent, datasource)
        )

        assert response.status_code == 200
        assert 'data-success="true"' in response.text

        tool = await svc.get_tool_config_views(db, agent.user_id)
        assert len(tool) == 1
        assert tool[0]["query_mode"] == "sql"
        assert tool[0]["query_preview"] == DISTINCT_SQL

    def test_a_write_statement_is_refused_with_a_readable_message(
        self, client, agent, datasource  # noqa: ANN001
    ) -> None:
        response = client.post(
            "/tool-configs/create",
            data=form_data(agent, datasource, sql_query="DELETE FROM inventory_items"),
        )

        assert "read-only" in response.text
        assert 'data-success="true"' not in response.text

    def test_an_empty_statement_is_refused(
        self, client, agent, datasource  # noqa: ANN001
    ) -> None:
        response = client.post(
            "/tool-configs/create", data=form_data(agent, datasource, sql_query="   ")
        )

        assert "Write the SQL query" in response.text

    def test_an_unknown_mode_is_refused(
        self, client, agent, datasource  # noqa: ANN001
    ) -> None:
        response = client.post(
            "/tool-configs/create",
            data=form_data(agent, datasource, query_mode="freehand"),
        )

        assert 'data-success="true"' not in response.text

    def test_sql_mode_is_refused_for_mongo(
        self, client, agent, mongo_datasource  # noqa: ANN001
    ) -> None:
        response = client.post(
            "/tool-configs/create",
            data=form_data(agent, mongo_datasource, table_name="events"),
        )

        assert "not a relational datasource" in response.text


class TestEditingASqlTool:
    async def test_the_edit_form_reopens_in_sql_mode_with_the_statement(
        self, client, db, agent, datasource  # noqa: ANN001
    ) -> None:
        tool = ToolConfig(
            data_agent_id=agent.id,
            datasource_id=datasource.id,
            tool_name="distinct_items",
            table_name="inventory_items",
            query_mode="sql",
            sql_query=DISTINCT_SQL,
        )
        db.add(tool)
        await db.commit()
        await db.refresh(tool)

        body = client.get(f"/tool-configs/{tool.uuid}/edit-form").text

        assert DISTINCT_SQL in body
        # The SQL radio is the checked one, so the JS opens on that panel.
        assert 'id="toolQueryModeSql"' in body
        assert body.index('id="toolQueryModeSql"') < body.index("checked")

    async def test_switching_to_the_builder_clears_the_statement(
        self, client, db, agent, datasource  # noqa: ANN001
    ) -> None:
        tool = ToolConfig(
            data_agent_id=agent.id,
            datasource_id=datasource.id,
            tool_name="distinct_items",
            table_name="inventory_items",
            query_mode="sql",
            sql_query=DISTINCT_SQL,
        )
        db.add(tool)
        await db.commit()
        await db.refresh(tool)

        client.post(
            f"/tool-configs/{tool.uuid}/update",
            data=form_data(
                agent,
                datasource,
                query_mode="builder",
                config_json='{"columns": [{"column": "name", "alias": ""}]}',
            ),
        )
        await db.refresh(tool)

        assert tool.query_mode == "builder"
        assert tool.sql_query is None
        assert tool.config["columns"] == [{"column": "name", "alias": ""}]
