"""
Route-level tests for the Tool Graphs controller.

A real HTTP round trip, because two of the three things this module has to get
right only exist at that level: the page has to render its tree as markup a person
can click, and the two view endpoints have to answer JSON in the shape
``tool_graphs.js`` reads — a renderer given a field it did not expect draws nothing
and says nothing.

The third is the error contract. Neither view endpoint raises: a selection that
cannot be resolved comes back as a 200 with ``error`` set, because the canvas sits
beside a tree the user is clicking through and replacing the page would throw away
what they were doing.
"""

from __future__ import annotations

import uuid

import pytest

from app.models.data_agents import DataAgent
from app.models.datasource import DataSource
from app.models.tool_configs import ToolConfig, ToolConfigLink
from app.models.workspaces import Workspace
from app.routes.tool_graphs import ToolGraphController


@pytest.fixture
async def scene(db, user):  # noqa: ANN001, ANN201
    """A workspace, one agent in it, and a two-level chain on one datasource."""
    workspace = Workspace(user_id=user.id, name="Revenue")
    datasource = DataSource(
        user_id=user.id,
        datasource_name="warehouse",
        db_type="postgres",
        password_encrypted="enc",
    )
    db.add_all([workspace, datasource])
    await db.commit()
    await db.refresh(workspace)
    await db.refresh(datasource)

    agent = DataAgent(user_id=user.id, name="sales", workspace_id=workspace.id)
    db.add(agent)
    await db.commit()
    await db.refresh(agent)

    root = ToolConfig(
        data_agent_id=agent.id,
        datasource_id=datasource.id,
        tool_name="projects_by_client",
        table_name="projects",
        extra_tables=["clients"],
        config={
            "columns": [{"column": "projects.id", "alias": ""}],
            "joins": [{
                "type": "inner", "table": "clients", "left_table": "projects",
                "left_column": "client_id", "right_column": "id",
            }],
        },
    )
    child = ToolConfig(
        data_agent_id=agent.id,
        datasource_id=datasource.id,
        tool_name="active_clients",
        table_name="clients",
        config={"columns": [{"column": "id", "alias": ""}]},
    )
    db.add_all([root, child])
    await db.commit()
    await db.refresh(root)
    await db.refresh(child)

    db.add(ToolConfigLink(
        parent_id=root.id,
        child_id=child.id,
        child_column="id",
        parent_reference="projects.client_id",
    ))
    await db.commit()

    return {
        "workspace": workspace,
        "agent": agent,
        "datasource": datasource,
        "root": root,
        "child": child,
    }


@pytest.fixture
async def foreign_tool(db, make_user):  # noqa: ANN001, ANN201
    """A tool belonging to someone else, as its public uuid."""
    other = await make_user("someone@else.test")

    agent = DataAgent(user_id=other.id, name="theirs")
    datasource = DataSource(
        user_id=other.id, datasource_name="theirs",
        db_type="postgres", password_encrypted="enc",
    )
    db.add_all([agent, datasource])
    await db.commit()
    await db.refresh(agent)
    await db.refresh(datasource)

    tool = ToolConfig(
        data_agent_id=agent.id, datasource_id=datasource.id,
        tool_name="secret_report", table_name="secrets", config={},
    )
    db.add(tool)
    await db.commit()
    await db.refresh(tool)

    return tool.uuid


@pytest.fixture
def client(auth_client_factory):  # noqa: ANN001, ANN201
    return auth_client_factory(ToolGraphController)


class TestThePage:
    def test_it_renders_with_the_sidebar_entry_highlighted(self, client, scene) -> None:  # noqa: ANN001
        response = client.get("/tool-graphs/")

        assert response.status_code == 200
        assert "Tool Graphs" in response.text
        # `active` drives the sidebar highlight; the entry itself is in the layout.
        assert 'href="/tool-graphs" class="active"' in response.text

    def test_the_tree_lists_the_workspace_its_agent_and_its_tools(
        self, client, scene  # noqa: ANN001
    ) -> None:
        response = client.get("/tool-graphs/")

        assert "Revenue" in response.text
        assert "sales" in response.text
        assert "projects_by_client" in response.text
        assert str(scene["root"].uuid) in response.text

    def test_no_internal_id_reaches_the_page(self, client, scene) -> None:  # noqa: ANN001
        response = client.get("/tool-graphs/")

        assert 'data-id="{}"'.format(scene["root"].id) not in response.text

    def test_a_selection_in_the_query_string_is_handed_to_the_page(
        self, client, scene  # noqa: ANN001
    ) -> None:
        """`/tool-graphs?tool=<uuid>` is the link someone pastes into a ticket."""
        response = client.get(f"/tool-graphs/?tool={scene['root'].uuid}")

        assert response.status_code == 200
        assert str(scene["root"].uuid) in response.text

    def test_an_empty_account_gets_an_explanation_not_a_blank_panel(
        self, client, user  # noqa: ANN001
    ) -> None:
        response = client.get("/tool-graphs/")

        assert response.status_code == 200
        assert "No workspaces yet" in response.text


class TestTheToolGraphEndpoint:
    def test_a_chain_comes_back_as_nodes_and_edges(self, client, scene) -> None:  # noqa: ANN001
        response = client.get(f"/tool-graphs/graph?tool={scene['root'].uuid}")
        body = response.json()

        assert response.status_code == 200
        assert body["error"] is None
        assert [node["label"] for node in body["nodes"]] == [
            "START", "active_clients", "projects_by_client", "END",
        ]
        assert body["scope_label"] == "sales · projects_by_client"

    def test_an_edge_says_what_crosses_it(self, client, scene) -> None:  # noqa: ANN001
        body = client.get(f"/tool-graphs/graph?tool={scene['root'].uuid}").json()

        labels = [edge["label"] for edge in body["edges"] if edge["kind"] == "value"]
        assert labels == ["id → projects.client_id"]

    def test_an_agent_draws_every_tool_it_owns(self, client, scene) -> None:  # noqa: ANN001
        body = client.get(f"/tool-graphs/graph?agent={scene['agent'].uuid}").json()

        drawn = {node["label"] for node in body["nodes"] if node["kind"] == "tool"}
        assert drawn == {"projects_by_client", "active_clients"}

    def test_no_selection_is_an_empty_canvas_not_an_error(self, client, scene) -> None:  # noqa: ANN001
        response = client.get("/tool-graphs/graph")
        body = response.json()

        assert response.status_code == 200
        assert body == {"scope_label": "", "nodes": [], "edges": [], "error": None}

    def test_an_unknown_tool_answers_with_a_sentence_not_a_broken_page(
        self, client, scene  # noqa: ANN001
    ) -> None:
        response = client.get(f"/tool-graphs/graph?tool={uuid.uuid4()}")
        body = response.json()

        assert response.status_code == 200
        assert body["nodes"] == []
        assert "not found" in body["error"].lower()

    def test_another_users_tool_is_refused_the_same_way_a_missing_one_is(
        self, client, foreign_tool  # noqa: ANN001
    ) -> None:
        """Answering differently would confirm the uuid is real."""
        body = client.get(f"/tool-graphs/graph?tool={foreign_tool}").json()

        assert body["nodes"] == []
        assert "not found" in body["error"].lower()

    def test_a_malformed_uuid_is_a_sentence_in_the_body_too(self, client, scene) -> None:  # noqa: ANN001
        """
        Still a 200 with an error, not a 400 page. The canvas is a panel inside a
        page the user is working in, and every other way this endpoint can fail
        already answers that way — one path that swapped in an error page instead
        would be the surprising one.
        """
        response = client.get("/tool-graphs/graph?tool=not-a-uuid")
        body = response.json()

        assert response.status_code == 200
        assert body["nodes"] == []
        assert body["error"]


class TestTheSqlGraphEndpoint:
    def test_a_builder_tools_joins_come_back_in_query_order(self, client, scene) -> None:  # noqa: ANN001
        response = client.get(f"/tool-graphs/joins?tool={scene['root'].uuid}")
        body = response.json()

        assert response.status_code == 200
        # The embedded tool is here too: selecting a nested tool selects the chain,
        # and the two views have to agree about which tools that is.
        views = {tool["tool_name"]: tool for tool in body["tools"]}
        assert set(views) == {"projects_by_client", "active_clients"}

        join = views["projects_by_client"]["joins"][0]
        assert join["type"] == "inner"
        assert join["type_label"] == "INNER JOIN"
        assert join["left_table"] == "projects"
        assert join["table"] == "clients"

    def test_a_tool_with_no_joins_says_why_rather_than_showing_nothing(
        self, client, scene  # noqa: ANN001
    ) -> None:
        body = client.get(f"/tool-graphs/joins?tool={scene['child'].uuid}").json()

        assert body["tools"][0]["joins"] == []
        assert "nothing to intersect" in body["tools"][0]["note"]

    def test_both_views_describe_the_same_tools(self, client, scene) -> None:  # noqa: ANN001
        """The toggle changes how a selection is drawn, never what is drawn."""
        graph = client.get(f"/tool-graphs/graph?agent={scene['agent'].uuid}").json()
        joins = client.get(f"/tool-graphs/joins?agent={scene['agent'].uuid}").json()

        assert {n["label"] for n in graph["nodes"] if n["kind"] == "tool"} == (
            {tool["tool_name"] for tool in joins["tools"]}
        )

    def test_an_unknown_agent_answers_with_a_sentence(self, client, scene) -> None:  # noqa: ANN001
        body = client.get(f"/tool-graphs/joins?agent={uuid.uuid4()}").json()

        assert body["tools"] == []
        assert "not found" in body["error"].lower()
