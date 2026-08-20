"""
Tests for ``deep_agent_service.get_agent_runtime_view`` — the test console's payload.

This file exists because of a crash, and the crash is worth stating plainly: as soon as an
agent had a **graph** among its tools, opening its console raised ``KeyError: 'uuid'``.

The cause is structural, not a typo. ``collect_agent_tools`` deliberately returns *one* list
holding two kinds of entry — tool configs and published graphs — because two lists could
describe different sets, and then an agent would be told about something it cannot call.
Every consumer of that list therefore has to branch on ``kind``, and three of them do
(``prompt_builder._describe_graph``, ``tool_factory.build_tools``,
``tool_factory.find_unsupported_tools``). This one did not: it read ``tool["uuid"]``,
``tool["table_name"]``, ``tool["datasource_name"]`` and ``tool["db_type"]`` off every entry,
and a graph entry has none of them — its public id is ``graph_uuid``, and its nodes each hold
their own datasource.

So what these tests pin is the *invariant*, not the four keys: **a graph among an agent's
tools must not be read as a tool config.** The two assertions that would have caught it are
that the console row for a graph carries the graph's own uuid, and that it does not claim a
table or a datasource — because defaulting those to ``""`` would have avoided the crash and
produced something worse, a row reading "in ()" that sends the operator to check a datasource
that was never involved.

Against the real database and the real services: what is being tested is a shape agreement
between two modules, and a stub of either would only assert that this file calls what it
calls.
"""

from __future__ import annotations

import uuid as uuid_pkg

import pytest

pytest.importorskip("langgraph", reason="LangGraph is installed in the container only")

from app.models.data_agents import DataAgent  # noqa: E402
from app.models.datasource import DataSource  # noqa: E402
from app.models.tool_configs import ToolConfig  # noqa: E402
from app.services.deep_agents import deep_agent_service  # noqa: E402
from app.services.graph_designer import graph_service  # noqa: E402


@pytest.fixture
async def datasource(db, user, tmp_path):  # noqa: ANN001, ANN201
    import sqlite3

    path = tmp_path / "console.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        "CREATE TABLE departments (id INTEGER PRIMARY KEY, name TEXT);"
        "INSERT INTO departments VALUES (1, 'Eng'), (2, 'Sales');"
    )
    connection.commit()
    connection.close()

    row = DataSource(
        user_id=user.id,
        datasource_name=f"console-{uuid_pkg.uuid4().hex[:6]}",
        db_type="sqlite",
        database_name=str(path),
        is_active=True,
        password_encrypted="",
        configuration_data={},
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


@pytest.fixture
async def agent(db, user):  # noqa: ANN001, ANN201
    row = DataAgent(
        user_id=user.id, name=f"agent-{uuid_pkg.uuid4().hex[:6]}", is_active=True,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


@pytest.fixture
async def tool_config(db, agent, datasource):  # noqa: ANN001, ANN201
    """An ordinary SQL-mode tool. No ``user_id``: ownership runs tool → agent → user."""
    row = ToolConfig(
        data_agent_id=agent.id,
        datasource_id=datasource.id,
        tool_name="departments_list",
        table_name="departments",
        extra_tables=[],
        description="Every department.",
        query_mode="sql",
        sql_query="SELECT id, name FROM departments",
        config={},
        sql_params=[],
        is_enabled=True,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


def graph_data(datasource, *, asks: bool = False) -> dict:  # noqa: ANN001
    nodes = [
        {"id": "s", "type": "start", "position": {}, "data": {"label": "Start"}},
        {
            "id": "q", "type": "sql", "position": {},
            "data": {
                "label": "departments",
                "datasource_id": str(datasource.uuid),
                "table_names": ["departments"],
                "sql_query": "SELECT id, name FROM departments",
            },
        },
        {"id": "ok", "type": "success", "position": {}, "data": {}},
    ]
    edges = [
        {"id": "e1", "source": "s", "source_port": "default", "target": "q"},
        {"id": "e2", "source": "q", "source_port": "default", "target": "ok"},
    ]

    if asks:
        nodes.insert(2, {
            "id": "ask", "type": "human", "position": {},
            "data": {
                "label": "Confirm",
                "prompt": "Include archived departments?",
                "expects": "confirm",
            },
        })
        edges[1] = {"id": "e2", "source": "q", "source_port": "default", "target": "ask"}
        edges.append({
            "id": "e3", "source": "ask", "source_port": "default", "target": "ok",
        })

    return {"nodes": nodes, "edges": edges}


@pytest.fixture
def attached_graph(db, user, agent, datasource):  # noqa: ANN001, ANN201
    """A published graph attached to the agent — the state that broke this page."""
    async def _attach(name: str = "Monthly revenue", *, asks: bool = False):
        graph = await graph_service.create_graph(db, user.id, name, "Lists departments.")
        await graph_service.save_graph(
            db, user.id, graph.uuid, graph_data(datasource, asks=asks),
        )
        await graph_service.set_graph_active(db, user.id, graph.uuid, True)
        await graph_service.attach_graph(db, user.id, graph.uuid, agent.uuid)
        return graph

    return _attach


class TestAnAgentHoldingAGraph:
    async def test_the_console_view_can_be_built_at_all(
        self, db, user, agent, attached_graph,
    ) -> None:  # noqa: ANN001
        """
        The regression. This raised ``KeyError: 'uuid'`` — the whole page, 500, for the
        only reason that the agent could call a graph.
        """
        await attached_graph()

        view = await deep_agent_service.get_agent_runtime_view(db, user.id, agent.uuid)

        assert len(view["tools"]) == 1

    async def test_the_graph_row_carries_the_graphs_own_uuid(
        self, db, user, agent, attached_graph,
    ) -> None:  # noqa: ANN001
        """
        ``graph_uuid``, not ``uuid``. The entry has both an internal ``id`` and a
        ``graph_uuid``, and the console must publish the second — the same rule every
        other page follows.
        """
        graph = await attached_graph()

        view = await deep_agent_service.get_agent_runtime_view(db, user.id, agent.uuid)
        row = view["tools"][0]

        assert row["uuid"] == str(graph.uuid)
        assert row["uuid"] != str(graph.id)
        uuid_pkg.UUID(row["uuid"])  # raises if an internal id leaked

    async def test_the_graph_row_does_not_claim_a_table_or_a_datasource(
        self, db, user, agent, attached_graph,
    ) -> None:  # noqa: ANN001
        """
        The assertion that makes the fix a fix rather than a patch. Defaulting the missing
        keys to ``""`` would also have stopped the crash, and the console would then read
        "in ()" — a broken *tool config*, pointing the operator at a datasource that was
        never part of this.
        """
        await attached_graph()

        view = await deep_agent_service.get_agent_runtime_view(db, user.id, agent.uuid)
        row = view["tools"][0]

        assert row["kind"] == "graph"
        assert "table_name" not in row
        assert "datasource_name" not in row
        assert "db_type" not in row

    async def test_the_graph_row_describes_the_drawing_instead(
        self, db, user, agent, attached_graph,
    ) -> None:  # noqa: ANN001
        await attached_graph()

        view = await deep_agent_service.get_agent_runtime_view(db, user.id, agent.uuid)
        row = view["tools"][0]

        assert row["node_count"] == 3
        assert row["asks_questions"] is False

    async def test_a_graph_that_can_pause_says_so(
        self, db, user, agent, attached_graph,
    ) -> None:  # noqa: ANN001
        """
        The one fact about a graph that has no analogue in a tool config: it can stop
        mid-answer and ask the person a question. Worth surfacing on the console, since
        an operator testing the agent is the person who will be asked.
        """
        await attached_graph(asks=True)

        view = await deep_agent_service.get_agent_runtime_view(db, user.id, agent.uuid)

        assert view["tools"][0]["asks_questions"] is True

    async def test_a_graph_is_not_reported_as_an_unsupported_tool(
        self, db, user, agent, attached_graph,
    ) -> None:  # noqa: ANN001
        """
        ``find_unsupported_tools`` skips graphs, and this asserts it through the console
        rather than directly: a graph has no ``db_type``, so the relational-datasource
        check would flag every graph on every agent's console.
        """
        await attached_graph()

        view = await deep_agent_service.get_agent_runtime_view(db, user.id, agent.uuid)

        assert view["unsupported_tools"] == []


class TestWhetherASourceCanBeFiltered:
    """
    The console has to say which sources may have their whole result read, because its
    absence is invisible and its consequence is not.

    A real agent, with one graph whose description said "if the user asks for a specific
    month, filter the data on created_at" and the switch left **off**, answered "I'm unable
    to filter the data by month". The switch was the reason and nothing on the page said
    so. Both halves of that are fixed here: a per-source badge, and a paragraph that stops
    claiming a fixed query is the whole story once one is opted in.
    """

    async def test_a_source_that_is_not_opted_in_is_not_marked(
        self, db, user, agent, tool_config,
    ) -> None:  # noqa: ANN001
        view = await deep_agent_service.get_agent_runtime_view(db, user.id, agent.uuid)

        assert view["tools"][0]["whole_result_readable"] is False
        assert view["has_readable_tools"] is False

    async def test_an_opted_in_tool_config_is_marked(
        self, db, user, agent, tool_config,
    ) -> None:  # noqa: ANN001
        tool_config.allow_recursive_aggregate = True
        await db.commit()

        view = await deep_agent_service.get_agent_runtime_view(db, user.id, agent.uuid)

        assert view["tools"][0]["whole_result_readable"] is True
        assert view["has_readable_tools"] is True

    async def test_an_opted_in_graph_is_marked(
        self, db, user, agent, attached_graph,
    ) -> None:  # noqa: ANN001
        """
        The graph half, which is the one the operator actually had. One key for both kinds,
        so this checks the shared key really is shared through to the page.
        """
        graph = await attached_graph()
        graph.allow_recursive_aggregate = True
        await db.commit()

        view = await deep_agent_service.get_agent_runtime_view(db, user.id, agent.uuid)

        assert view["tools"][0]["kind"] == "graph"
        assert view["tools"][0]["whole_result_readable"] is True
        assert view["has_readable_tools"] is True

    async def test_one_opted_in_source_among_several_flags_the_agent(
        self, db, user, agent, tool_config, attached_graph,
    ) -> None:  # noqa: ANN001
        graph = await attached_graph()
        graph.allow_recursive_aggregate = True
        await db.commit()

        view = await deep_agent_service.get_agent_runtime_view(db, user.id, agent.uuid)
        by_kind = {row["kind"]: row for row in view["tools"]}

        assert view["has_readable_tools"] is True
        assert by_kind["graph"]["whole_result_readable"] is True
        assert by_kind["tool_config"]["whole_result_readable"] is False


class TestTheTwoKindsSideBySide:
    """
    An agent holding both, which is the ordinary case once somebody publishes a graph.

    The order is ``collect_agent_tools``'s: the agent's own tool configs, then its graphs.
    """

    async def test_both_kinds_are_listed_and_each_keeps_its_own_shape(
        self, db, user, agent, tool_config, attached_graph,
    ) -> None:  # noqa: ANN001
        await attached_graph()

        view = await deep_agent_service.get_agent_runtime_view(db, user.id, agent.uuid)
        by_kind = {row["kind"]: row for row in view["tools"]}

        assert set(by_kind) == {"tool_config", "graph"}
        assert by_kind["tool_config"]["table_name"] == "departments"
        assert by_kind["tool_config"]["db_type"] == "sqlite"
        assert by_kind["graph"]["node_count"] == 3

    async def test_a_tool_config_row_is_unchanged_by_the_branch(
        self, db, user, agent, tool_config,
    ) -> None:  # noqa: ANN001
        """
        The keys the console template has always read, on an agent with no graph at all —
        so the fix cannot have been a change to what a tool config reports.
        """
        view = await deep_agent_service.get_agent_runtime_view(db, user.id, agent.uuid)
        row = view["tools"][0]

        assert row["uuid"] == str(tool_config.uuid)
        assert row["tool_name"] == "departments_list"
        assert row["description"] == "Every department."
        assert row["table_name"] == "departments"
        assert row["datasource_name"].startswith("console-")
        assert row["db_type"] == "sqlite"

    async def test_a_shared_graph_arrives_the_same_way_as_an_attached_one(
        self, db, user, agent, datasource,
    ) -> None:  # noqa: ANN001
        """
        The second route to a graph. Both reach the console through the same list, so a
        workspace-shared graph must render identically — otherwise the crash would simply
        have moved to the other route.
        """
        from app.models.workspaces import Workspace

        workspace = Workspace(
            user_id=user.id, name=f"team-{uuid_pkg.uuid4().hex[:6]}", is_active=True,
        )
        db.add(workspace)
        await db.commit()
        await db.refresh(workspace)

        agent.workspace_id = workspace.id
        await db.commit()

        graph = await graph_service.create_graph(db, user.id, "Shared graph", None)
        await graph_service.save_graph(db, user.id, graph.uuid, graph_data(datasource))
        await graph_service.set_graph_active(db, user.id, graph.uuid, True)
        await graph_service.share_graph(db, user.id, graph.uuid, workspace.uuid)

        view = await deep_agent_service.get_agent_runtime_view(db, user.id, agent.uuid)

        assert view["tools"][0]["kind"] == "graph"
        assert view["tools"][0]["uuid"] == str(graph.uuid)
