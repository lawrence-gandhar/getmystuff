"""
Tests for app/services/tool_graphs/tool_graph_service.py.

This module draws things, and a drawing is believed. That is the whole risk: a
wrong number in a query is argued with, a wrong picture of a tool chain is trusted,
so the properties asserted here are the ones a reader would take on faith —

* **the graph matches what runs.** Edges point the way values travel, ``START``
  hangs off the tools that go first, and the descendants of a scoped tool are in the
  picture even when they belong to another agent, because that is what the agent
  will actually run.
* **a shared child is one node.** The list page necessarily repeats it under each
  parent; drawing it twice here would hide the only thing this view adds.
* **nothing is quietly dropped.** A disabled tool is drawn and flagged, not omitted
  — a chain that stops is exactly what someone opens this page to find.
* **someone else's row is not reachable**, and asks for one 404 rather than
  rendering an empty canvas, which would read as "this tool has no chain".
* **a cycle terminates.** Links cannot be saved in a cycle, but a page that only
  displays must not be the thing that hangs if a row ever arrived another way.
"""

from __future__ import annotations

import uuid

import pytest
from litestar.exceptions import HTTPException

from app.models.data_agents import DataAgent
from app.models.datasource import DataSource
from app.models.tool_configs import ToolConfig, ToolConfigLink
from app.models.workspaces import Workspace
from app.services.tool_graphs import tool_graph_service as svc


# --------------------------------------------------------------------------
# Fixtures — rows built directly, since nothing here goes through a save path
# --------------------------------------------------------------------------

@pytest.fixture
def make_workspace(db):  # noqa: ANN001, ANN201
    async def _make(owner, name: str, **kwargs):  # noqa: ANN001
        row = Workspace(user_id=owner.id, name=name, **kwargs)
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row

    return _make


@pytest.fixture
def make_agent(db):  # noqa: ANN001, ANN201
    async def _make(owner, name: str, **kwargs):  # noqa: ANN001
        row = DataAgent(user_id=owner.id, name=name, **kwargs)
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row

    return _make


@pytest.fixture
def make_datasource(db):  # noqa: ANN001, ANN201
    async def _make(owner, name: str, **kwargs):  # noqa: ANN001
        row = DataSource(
            user_id=owner.id,
            datasource_name=name,
            db_type=kwargs.pop("db_type", "postgres"),
            password_encrypted="enc",
            **kwargs,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row

    return _make


@pytest.fixture
def make_tool(db):  # noqa: ANN001, ANN201
    async def _make(agent, datasource, tool_name: str, **kwargs):  # noqa: ANN001
        row = ToolConfig(
            data_agent_id=agent.id,
            datasource_id=datasource.id,
            tool_name=tool_name,
            table_name=kwargs.pop("table_name", "orders"),
            config=kwargs.pop("config", {"columns": [{"column": "id", "alias": ""}]}),
            **kwargs,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row

    return _make


@pytest.fixture
def make_link(db):  # noqa: ANN001, ANN201
    """
    A raw edge, deliberately not through ``replace_child_links``.

    These tests are about what is drawn, not about what may be saved — and one of
    them needs a cycle, which the save path is there to refuse.
    """
    async def _make(parent, child, column: str = "id", target: str = "client_id"):  # noqa: ANN001
        row = ToolConfigLink(
            parent_id=parent.id,
            child_id=child.id,
            child_column=column,
            parent_reference=target,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row

    return _make


@pytest.fixture
async def chain(db, user, make_agent, make_datasource, make_tool, make_link):  # noqa: ANN001, ANN201
    """
    The three-level chain the feature was built around:

        paid_invoices → active_clients → projects_by_client
    """
    agent = await make_agent(user, "sales")
    datasource = await make_datasource(user, "warehouse")

    root = await make_tool(agent, datasource, "projects_by_client")
    child = await make_tool(agent, datasource, "active_clients")
    grandchild = await make_tool(agent, datasource, "paid_invoices")

    await make_link(root, child, column="id", target="projects.client_id")
    await make_link(child, grandchild, column="client_id", target="clients.id")

    return {
        "agent": agent,
        "datasource": datasource,
        "root": root,
        "child": child,
        "grandchild": grandchild,
    }


def node(graph: dict, label: str) -> dict:
    """The one node with this label, so a test can assert on it by name."""
    return next(item for item in graph["nodes"] if item["label"] == label)


def edges_into(graph: dict, key: str) -> list:
    return [edge for edge in graph["edges"] if edge["target"] == key]


def edges_out_of(graph: dict, key: str) -> list:
    return [edge for edge in graph["edges"] if edge["source"] == key]


# --------------------------------------------------------------------------
# The tree
# --------------------------------------------------------------------------

class TestTheTree:
    async def test_agents_sit_under_their_workspace_with_their_tools(
        self, db, user, make_workspace, make_agent, make_datasource, make_tool  # noqa: ANN001
    ) -> None:
        workspace = await make_workspace(user, "Revenue")
        agent = await make_agent(user, "sales", workspace_id=workspace.id)
        datasource = await make_datasource(user, "warehouse")
        await make_tool(agent, datasource, "projects_by_client")

        tree = await svc.get_graph_tree(db, user.id)

        assert [group["name"] for group in tree] == ["Revenue"]
        assert [entry["name"] for entry in tree[0]["agents"]] == ["sales"]
        tools = tree[0]["agents"][0]["tools"]
        assert [item["tool_name"] for item in tools] == ["projects_by_client"]
        assert tools[0]["datasource_name"] == "warehouse"

    async def test_an_agent_in_no_workspace_gets_its_own_group(
        self, db, user, make_workspace, make_agent  # noqa: ANN001
    ) -> None:
        """``workspace_id`` is nullable by design, so this is not an edge case."""
        await make_workspace(user, "Revenue")
        await make_agent(user, "loose")

        tree = await svc.get_graph_tree(db, user.id)

        assert [group["name"] for group in tree] == ["Revenue", "Unassigned"]
        assert [entry["name"] for entry in tree[-1]["agents"]] == ["loose"]

    async def test_the_unassigned_group_is_absent_when_every_agent_has_a_home(
        self, db, user, make_workspace, make_agent  # noqa: ANN001
    ) -> None:
        workspace = await make_workspace(user, "Revenue")
        await make_agent(user, "sales", workspace_id=workspace.id)

        tree = await svc.get_graph_tree(db, user.id)

        assert [group["name"] for group in tree] == ["Revenue"]

    async def test_an_empty_workspace_and_an_empty_agent_still_appear(
        self, db, user, make_workspace, make_agent  # noqa: ANN001
    ) -> None:
        """
        An empty branch is how someone notices the agent they just made is empty.
        Hiding it would make this tree disagree with the Data Agents page.
        """
        await make_workspace(user, "Empty")
        workspace = await make_workspace(user, "Revenue")
        await make_agent(user, "toolless", workspace_id=workspace.id)

        tree = await svc.get_graph_tree(db, user.id)

        by_name = {group["name"]: group for group in tree}
        assert by_name["Empty"]["agents"] == []
        assert by_name["Revenue"]["agents"][0]["tools"] == []

    async def test_nesting_is_flagged_in_both_directions(
        self, db, user, chain  # noqa: ANN001
    ) -> None:
        tree = await svc.get_graph_tree(db, user.id)
        tools = {item["tool_name"]: item for item in tree[0]["agents"][0]["tools"]}

        assert tools["projects_by_client"]["has_children"] is True
        assert tools["projects_by_client"]["is_embedded"] is False
        assert tools["active_clients"]["has_children"] is True
        assert tools["active_clients"]["is_embedded"] is True
        assert tools["paid_invoices"]["has_children"] is False
        assert tools["paid_invoices"]["is_embedded"] is True

    async def test_another_users_rows_are_not_in_the_tree(
        self, db, user, make_user, make_workspace, make_agent  # noqa: ANN001
    ) -> None:
        other = await make_user("someone@else.test")
        await make_workspace(other, "Theirs")
        await make_agent(other, "their agent")
        await make_workspace(user, "Mine")

        tree = await svc.get_graph_tree(db, user.id)

        assert [group["name"] for group in tree] == ["Mine"]


# --------------------------------------------------------------------------
# The chain graph
# --------------------------------------------------------------------------

class TestTheChainGraph:
    async def test_a_three_level_chain_runs_start_to_end_in_order(
        self, db, user, chain  # noqa: ANN001
    ) -> None:
        graph = await svc.get_chain_graph(db, user.id, tool_id=chain["root"].uuid)

        assert node(graph, "START")["layer"] == 0
        assert node(graph, "paid_invoices")["layer"] == 1
        assert node(graph, "active_clients")["layer"] == 2
        assert node(graph, "projects_by_client")["layer"] == 3
        assert node(graph, "END")["layer"] == 4

    async def test_a_straight_chain_draws_on_one_line(
        self, db, user, chain  # noqa: ANN001
    ) -> None:
        graph = await svc.get_chain_graph(db, user.id, tool_id=chain["root"].uuid)

        assert {node(graph, name)["row"] for name in (
            "paid_invoices", "active_clients", "projects_by_client",
        )} == {0}

    async def test_edges_point_the_way_values_travel(
        self, db, user, chain  # noqa: ANN001
    ) -> None:
        graph = await svc.get_chain_graph(db, user.id, tool_id=chain["root"].uuid)
        root, child = str(chain["root"].uuid), str(chain["child"].uuid)

        value_edges = [edge for edge in graph["edges"] if edge["kind"] == "value"]
        assert {"source": child, "target": root, "kind": "value",
                "label": "id → projects.client_id"} in value_edges

    async def test_start_feeds_only_the_deepest_tool(
        self, db, user, chain  # noqa: ANN001
    ) -> None:
        graph = await svc.get_chain_graph(db, user.id, tool_id=chain["root"].uuid)

        started = [edge["target"] for edge in edges_out_of(graph, "start")]
        assert started == [str(chain["grandchild"].uuid)]

    async def test_only_the_root_feeds_end(
        self, db, user, chain  # noqa: ANN001
    ) -> None:
        graph = await svc.get_chain_graph(db, user.id, tool_id=chain["root"].uuid)

        ended = [edge["source"] for edge in edges_into(graph, "end")]
        assert ended == [str(chain["root"].uuid)]

    async def test_a_standalone_tool_is_drawn_start_to_end(
        self, db, user, make_agent, make_datasource, make_tool  # noqa: ANN001
    ) -> None:
        agent = await make_agent(user, "sales")
        datasource = await make_datasource(user, "warehouse")
        tool = await make_tool(agent, datasource, "open_tickets")

        graph = await svc.get_chain_graph(db, user.id, tool_id=tool.uuid)

        assert [item["kind"] for item in graph["nodes"]] == ["start", "tool", "end"]
        assert [edge["kind"] for edge in graph["edges"]] == ["start", "end"]

    async def test_a_child_of_two_parents_is_one_node_with_two_edges(
        self, db, user, chain, make_tool, make_link  # noqa: ANN001
    ) -> None:
        """
        The fact this view exists for. The list page repeats a shared child under
        each parent, so nothing there can show that editing it changes two tools.
        """
        second = await make_tool(chain["agent"], chain["datasource"], "billing_report")
        await make_link(second, chain["child"], column="id", target="bills.client_id")

        graph = await svc.get_chain_graph(db, user.id, agent_id=chain["agent"].uuid)

        child_key = str(chain["child"].uuid)
        assert [item["key"] for item in graph["nodes"]].count(child_key) == 1
        assert sorted(edge["target"] for edge in edges_out_of(graph, child_key)) == (
            sorted([str(chain["root"].uuid), str(second.uuid)])
        )

    async def test_a_disabled_tool_is_drawn_and_flagged_not_dropped(
        self, db, user, chain  # noqa: ANN001
    ) -> None:
        """A chain that stops is exactly what someone opens this page to find."""
        chain["child"].is_enabled = False
        await db.commit()

        graph = await svc.get_chain_graph(db, user.id, tool_id=chain["root"].uuid)

        assert node(graph, "active_clients")["is_enabled"] is False
        assert len(graph["nodes"]) == 5

    async def test_an_agents_graph_includes_children_from_another_agent(
        self, db, user, chain, make_agent, make_tool, make_link  # noqa: ANN001
    ) -> None:
        """
        An agent given a nested tool is given every tool below it, whoever owns
        them. A graph that stopped at the agent's own rows would draw a chain with
        its lower half missing.
        """
        other_agent = await make_agent(user, "finance")
        borrowed = await make_tool(other_agent, chain["datasource"], "ledger_ids")
        await make_link(chain["grandchild"], borrowed, column="id", target="ledger_id")

        graph = await svc.get_chain_graph(db, user.id, agent_id=chain["agent"].uuid)

        assert node(graph, "ledger_ids")["agent_name"] == "finance"
        assert node(graph, "ledger_ids")["layer"] == 1

    async def test_a_second_branch_drops_to_its_own_row(
        self, db, user, chain, make_tool, make_link  # noqa: ANN001
    ) -> None:
        sibling = await make_tool(chain["agent"], chain["datasource"], "vip_clients")
        await make_link(chain["root"], sibling, column="id", target="projects.owner_id")

        graph = await svc.get_chain_graph(db, user.id, tool_id=chain["root"].uuid)

        assert node(graph, "projects_by_client")["row"] == 0
        assert node(graph, "active_clients")["row"] == 0
        assert node(graph, "vip_clients")["row"] == 1

    async def test_a_workspace_draws_every_agents_tools(
        self, db, user, make_workspace, make_agent, make_datasource, make_tool  # noqa: ANN001
    ) -> None:
        workspace = await make_workspace(user, "Revenue")
        datasource = await make_datasource(user, "warehouse")
        first = await make_agent(user, "sales", workspace_id=workspace.id)
        second = await make_agent(user, "support", workspace_id=workspace.id)
        outside = await make_agent(user, "hr")
        await make_tool(first, datasource, "deals")
        await make_tool(second, datasource, "tickets")
        await make_tool(outside, datasource, "headcount")

        graph = await svc.get_chain_graph(db, user.id, workspace_id=workspace.uuid)

        drawn = {item["label"] for item in graph["nodes"] if item["kind"] == "tool"}
        assert drawn == {"deals", "tickets"}
        assert graph["scope_label"] == "Revenue"

    async def test_the_most_specific_selection_wins(
        self, db, user, chain  # noqa: ANN001
    ) -> None:
        graph = await svc.get_chain_graph(
            db, user.id,
            agent_id=chain["agent"].uuid,
            tool_id=chain["grandchild"].uuid,
        )

        drawn = {item["label"] for item in graph["nodes"] if item["kind"] == "tool"}
        assert drawn == {"paid_invoices"}

    async def test_no_selection_is_an_empty_graph_not_an_error(
        self, db, user, chain  # noqa: ANN001
    ) -> None:
        graph = await svc.get_chain_graph(db, user.id)

        assert graph == {"scope_label": "", "nodes": [], "edges": []}

    async def test_a_tool_is_labelled_with_its_agent(
        self, db, user, chain  # noqa: ANN001
    ) -> None:
        graph = await svc.get_chain_graph(db, user.id, tool_id=chain["root"].uuid)

        assert graph["scope_label"] == "sales · projects_by_client"

    async def test_another_users_tool_is_not_found(
        self, db, user, make_user, make_agent, make_datasource, make_tool  # noqa: ANN001
    ) -> None:
        other = await make_user("someone@else.test")
        agent = await make_agent(other, "theirs")
        datasource = await make_datasource(other, "theirs")
        tool = await make_tool(agent, datasource, "secret_report")

        with pytest.raises(HTTPException) as exc:
            await svc.get_chain_graph(db, user.id, tool_id=tool.uuid)

        assert exc.value.status_code == 404
        assert "not found" in str(exc.value.detail).lower()

    async def test_an_unknown_uuid_is_not_found(self, db, user) -> None:  # noqa: ANN001
        with pytest.raises(HTTPException) as exc:
            await svc.get_chain_graph(db, user.id, agent_id=uuid.uuid4())

        assert exc.value.status_code == 404

    async def test_a_cycle_terminates_instead_of_hanging(
        self, db, user, chain, make_link  # noqa: ANN001
    ) -> None:
        """
        Saving this is refused. Displaying it must still return, because a page that
        hangs on bad data is worse than one that draws it oddly.
        """
        await make_link(chain["grandchild"], chain["root"], column="id", target="x")

        graph = await svc.get_chain_graph(db, user.id, tool_id=chain["root"].uuid)

        drawn = {item["label"] for item in graph["nodes"] if item["kind"] == "tool"}
        assert drawn == {"projects_by_client", "active_clients", "paid_invoices"}
        assert len({item["row"] for item in graph["nodes"]}) >= 1

    async def test_every_identifier_on_the_wire_is_a_public_uuid(
        self, db, user, chain  # noqa: ANN001
    ) -> None:
        graph = await svc.get_chain_graph(db, user.id, tool_id=chain["root"].uuid)

        for item in graph["nodes"]:
            assert "id" not in item
        keys = {item["key"] for item in graph["nodes"]}
        assert str(chain["root"].id) not in keys


# --------------------------------------------------------------------------
# The join sets
# --------------------------------------------------------------------------

JOINED_CONFIG = {
    "columns": [{"column": "orders.id", "alias": ""}],
    "joins": [
        {"type": "inner", "table": "clients",
         "left_table": "orders", "left_column": "client_id", "right_column": "id"},
        {"type": "left", "table": "regions",
         "left_table": "clients", "left_column": "region_id", "right_column": "id"},
    ],
}


class TestTheJoinSets:
    async def test_joins_are_reported_in_the_order_the_query_applies_them(
        self, db, user, make_agent, make_datasource, make_tool  # noqa: ANN001
    ) -> None:
        agent = await make_agent(user, "sales")
        datasource = await make_datasource(user, "warehouse")
        tool = await make_tool(
            agent, datasource, "orders_by_region",
            config=JOINED_CONFIG, extra_tables=["clients", "regions"],
        )

        view = (await svc.get_join_views(db, user.id, tool_id=tool.uuid))["tools"][0]

        assert [join["table"] for join in view["joins"]] == ["clients", "regions"]
        assert [join["type_label"] for join in view["joins"]] == [
            "INNER JOIN", "LEFT JOIN",
        ]
        assert view["joins"][0]["left_table"] == "orders"
        assert view["joins"][0]["left_column"] == "client_id"
        assert view["joins"][0]["right_column"] == "id"
        assert view["tables"] == ["orders", "clients", "regions"]
        assert view["note"] == ""

    async def test_a_single_table_query_says_there_is_nothing_to_intersect(
        self, db, user, make_agent, make_datasource, make_tool  # noqa: ANN001
    ) -> None:
        agent = await make_agent(user, "sales")
        datasource = await make_datasource(user, "warehouse")
        tool = await make_tool(agent, datasource, "all_orders")

        view = (await svc.get_join_views(db, user.id, tool_id=tool.uuid))["tools"][0]

        assert view["joins"] == []
        assert view["note"] == svc.NO_JOINS_NOTE

    async def test_a_sql_tool_reports_its_tables_and_says_it_is_not_parsed(
        self, db, user, make_agent, make_datasource, make_tool  # noqa: ANN001
    ) -> None:
        """
        Nothing in this application parses joins out of a statement. Drawing one
        anyway would be a confident picture of something nobody verified.
        """
        agent = await make_agent(user, "sales")
        datasource = await make_datasource(user, "warehouse")
        tool = await make_tool(
            agent, datasource, "handwritten",
            query_mode="sql",
            sql_query="SELECT o.id FROM orders o JOIN clients c ON c.id = o.client_id",
            config={},
            extra_tables=["clients"],
        )

        view = (await svc.get_join_views(db, user.id, tool_id=tool.uuid))["tools"][0]

        assert view["joins"] == []
        assert view["note"] == svc.SQL_MODE_NOTE
        assert view["tables"] == ["orders", "clients"]

    async def test_an_agents_joins_cover_every_tool_it_draws(
        self, db, user, chain  # noqa: ANN001
    ) -> None:
        """The two views must show the same tools, or the toggle changes the subject."""
        graph = await svc.get_chain_graph(db, user.id, agent_id=chain["agent"].uuid)
        joins = await svc.get_join_views(db, user.id, agent_id=chain["agent"].uuid)

        assert {item["label"] for item in graph["nodes"] if item["kind"] == "tool"} == (
            {view["tool_name"] for view in joins["tools"]}
        )

    async def test_another_users_tool_is_not_found(
        self, db, user, make_user, make_agent, make_datasource, make_tool  # noqa: ANN001
    ) -> None:
        other = await make_user("someone@else.test")
        agent = await make_agent(other, "theirs")
        datasource = await make_datasource(other, "theirs")
        tool = await make_tool(agent, datasource, "secret_report")

        with pytest.raises(HTTPException) as exc:
            await svc.get_join_views(db, user.id, tool_id=tool.uuid)

        assert exc.value.status_code == 404
