"""
Tests for the per-feature ``app/db/<feature>/queries.py`` modules.

These are the deliberate raw-SQL exceptions to the CRUDQueryBuilder rule — joins,
aggregate counts and case-insensitive lookups the generic builder cannot express.
Two properties recur across all of them and are what these tests are really for:

* **the aggregate must not fan out** — a workspace with three agents and an agent
  with two tool configs must still report the right counts once several joins are
  stacked on the same statement;
* **ownership scoping** — every list is filtered to one user, and a fixture below
  always creates a second user's rows so a missing ``where user_id = ...`` shows
  up as a failure rather than passing silently.

Run against the real SQLite test database, so the SQL is genuinely executed.
"""

from __future__ import annotations

import pytest

from app.db.chatbot import queries as chatbot_queries
from app.db.data_agents.queries import (
    data_agent_name_exists,
    fetch_agents_with_details,
)
from app.db.flow_builder.queries import fetch_flows_with_chatbot_names
from app.db.tool_configs.queries import (
    fetch_enabled_tools_for_agent,
    fetch_tool_configs_with_details,
    tool_name_exists,
)
from app.db.workspaces.queries import (
    fetch_workspaces_with_agent_counts,
    workspace_name_exists,
)
from app.models.ai_settings import AIApiKey
from app.models.chatbot import ChatbotApiKey
from app.models.data_agents import DataAgent
from app.models.datasource import DataSource
from app.models.flow_builder import ChatbotFlow
from app.models.tool_configs import ToolConfig
from app.models.workspaces import Workspace


# ---------------------------------------------------------------------------
# Row factories
# ---------------------------------------------------------------------------
@pytest.fixture
def make_workspace(db):  # noqa: ANN001, ANN201
    async def _make(user, name: str, **kwargs):  # noqa: ANN001
        row = Workspace(user_id=user.id, name=name, **kwargs)
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row

    return _make


@pytest.fixture
def make_agent(db):  # noqa: ANN001, ANN201
    async def _make(user, name: str, **kwargs):  # noqa: ANN001
        row = DataAgent(user_id=user.id, name=name, **kwargs)
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row

    return _make


@pytest.fixture
def make_datasource(db):  # noqa: ANN001, ANN201
    async def _make(user, name: str, **kwargs):  # noqa: ANN001
        row = DataSource(
            user_id=user.id,
            datasource_name=name,
            db_type=kwargs.pop("db_type", "postgres"),
            password_encrypted=kwargs.pop("password_encrypted", "enc"),
            **kwargs,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row

    return _make


@pytest.fixture
def make_tool_config(db):  # noqa: ANN001, ANN201
    async def _make(agent, datasource, tool_name: str, **kwargs):  # noqa: ANN001
        row = ToolConfig(
            data_agent_id=agent.id,
            datasource_id=datasource.id,
            tool_name=tool_name,
            table_name=kwargs.pop("table_name", "orders"),
            **kwargs,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row

    return _make


@pytest.fixture
def make_ai_key(db):  # noqa: ANN001, ANN201
    async def _make(user, label: str, **kwargs):  # noqa: ANN001
        row = AIApiKey(
            user_id=user.id,
            provider=kwargs.pop("provider", "anthropic"),
            label=label,
            api_key_encrypted=kwargs.pop("api_key_encrypted", "enc"),
            **kwargs,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row

    return _make


@pytest.fixture
def make_chatbot(db):  # noqa: ANN001, ANN201
    async def _make(user, name: str, datasource, **kwargs):  # noqa: ANN001
        row = ChatbotApiKey(
            user_id=user.id,
            name=name,
            datasource_id=datasource.id,
            target_type=kwargs.pop("target_type", "datasource"),
            **kwargs,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row

    return _make


@pytest.fixture
def make_flow(db):  # noqa: ANN001, ANN201
    async def _make(user, name: str, **kwargs):  # noqa: ANN001
        row = ChatbotFlow(user_id=user.id, name=name, **kwargs)
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row

    return _make


@pytest.fixture
async def other_user(make_user):  # noqa: ANN001, ANN201
    """A second account. Every list test seeds rows here so a missing ownership
    filter fails rather than passing by accident."""
    return await make_user("intruder@example.com")


# ---------------------------------------------------------------------------
# workspaces
# ---------------------------------------------------------------------------
class TestFetchWorkspacesWithAgentCounts:
    async def test_returns_zero_for_a_workspace_with_no_agents(
        self, db, user, make_workspace  # noqa: ANN001
    ) -> None:
        await make_workspace(user, "empty")

        rows = await fetch_workspaces_with_agent_counts(db, user.id)

        assert [(w.name, count) for w, count in rows] == [("empty", 0)]

    async def test_counts_the_agents_in_each_workspace(
        self, db, user, make_workspace, make_agent  # noqa: ANN001
    ) -> None:
        busy = await make_workspace(user, "busy")
        quiet = await make_workspace(user, "quiet")
        for i in range(3):
            await make_agent(user, f"agent{i}", workspace_id=busy.id)
        await make_agent(user, "lonely", workspace_id=quiet.id)

        rows = await fetch_workspaces_with_agent_counts(db, user.id)

        assert dict((w.name, count) for w, count in rows) == {"busy": 3, "quiet": 1}

    async def test_unassigned_agents_are_not_counted(
        self, db, user, make_workspace, make_agent  # noqa: ANN001
    ) -> None:
        """``workspace_id`` is nullable, so a free-floating agent must not be
        attributed to any workspace."""
        await make_workspace(user, "ws")
        await make_agent(user, "floating")

        rows = await fetch_workspaces_with_agent_counts(db, user.id)

        assert [count for _, count in rows] == [0]

    async def test_excludes_other_users_workspaces(
        self, db, user, other_user, make_workspace  # noqa: ANN001
    ) -> None:
        await make_workspace(user, "mine")
        await make_workspace(other_user, "theirs")

        rows = await fetch_workspaces_with_agent_counts(db, user.id)

        assert [w.name for w, _ in rows] == ["mine"]

    async def test_returns_nothing_for_a_user_with_no_workspaces(
        self, db, user  # noqa: ANN001
    ) -> None:
        assert await fetch_workspaces_with_agent_counts(db, user.id) == []


class TestWorkspaceNameExists:
    async def test_finds_an_exact_match(self, db, user, make_workspace) -> None:  # noqa: ANN001
        await make_workspace(user, "Analytics")
        assert await workspace_name_exists(db, user.id, "Analytics") is True

    @pytest.mark.parametrize("probe", ["analytics", "ANALYTICS", "  AnAlYtIcS  "])
    async def test_matching_ignores_case_and_surrounding_space(
        self, db, user, make_workspace, probe: str  # noqa: ANN001
    ) -> None:
        await make_workspace(user, "Analytics")
        assert await workspace_name_exists(db, user.id, probe) is True

    async def test_returns_false_for_an_unused_name(
        self, db, user, make_workspace  # noqa: ANN001
    ) -> None:
        await make_workspace(user, "Analytics")
        assert await workspace_name_exists(db, user.id, "Reporting") is False

    async def test_is_scoped_to_the_user(
        self, db, user, other_user, make_workspace  # noqa: ANN001
    ) -> None:
        """Two users may each have an "Analytics" workspace — the unique index is
        per user, so the pre-check has to be too."""
        await make_workspace(other_user, "Analytics")
        assert await workspace_name_exists(db, user.id, "Analytics") is False

    async def test_exclude_id_ignores_the_row_being_renamed(
        self, db, user, make_workspace  # noqa: ANN001
    ) -> None:
        """Without this, saving a workspace without changing its name would
        report its own name as taken."""
        workspace = await make_workspace(user, "Analytics")

        assert await workspace_name_exists(db, user.id, "Analytics", workspace.id) is False

    async def test_exclude_id_still_catches_a_different_row(
        self, db, user, make_workspace  # noqa: ANN001
    ) -> None:
        first = await make_workspace(user, "Analytics")
        second = await make_workspace(user, "Reporting")

        assert await workspace_name_exists(db, user.id, "Analytics", second.id) is True
        assert first.id != second.id


# ---------------------------------------------------------------------------
# data agents
# ---------------------------------------------------------------------------
class TestFetchAgentsWithDetails:
    async def test_an_unassigned_agent_reports_nulls_and_a_zero_count(
        self, db, user, make_agent  # noqa: ANN001
    ) -> None:
        await make_agent(user, "solo")

        (agent, tools, ws_name, ws_uuid, key_label), = await fetch_agents_with_details(
            db, user.id
        )

        assert agent.name == "solo"
        assert (tools, ws_name, ws_uuid, key_label) == (0, None, None, None)

    async def test_joins_workspace_and_key_and_counts_tools(
        self,
        db,
        user,
        make_workspace,
        make_agent,
        make_ai_key,
        make_datasource,
        make_tool_config,  # noqa: ANN001
    ) -> None:
        """The count must survive two extra many-to-one joins on the same
        statement — the reason the query groups by the joined columns."""
        workspace = await make_workspace(user, "Analytics")
        key = await make_ai_key(user, "prod-key")
        agent = await make_agent(
            user, "reporter", workspace_id=workspace.id, llm_api_key_id=key.id
        )
        datasource = await make_datasource(user, "warehouse")
        for i in range(2):
            await make_tool_config(agent, datasource, f"tool{i}")

        (row,) = await fetch_agents_with_details(db, user.id)
        _, tool_count, ws_name, ws_uuid, key_label = row

        assert tool_count == 2
        assert ws_name == "Analytics"
        assert ws_uuid == workspace.uuid
        assert key_label == "prod-key"

    async def test_the_workspace_filter_narrows_the_list(
        self, db, user, make_workspace, make_agent  # noqa: ANN001
    ) -> None:
        first = await make_workspace(user, "one")
        second = await make_workspace(user, "two")
        await make_agent(user, "in-one", workspace_id=first.id)
        await make_agent(user, "in-two", workspace_id=second.id)
        await make_agent(user, "unassigned")

        rows = await fetch_agents_with_details(db, user.id, workspace_id=first.id)

        assert [agent.name for agent, *_ in rows] == ["in-one"]

    async def test_excludes_other_users_agents(
        self, db, user, other_user, make_agent  # noqa: ANN001
    ) -> None:
        await make_agent(user, "mine")
        await make_agent(other_user, "theirs")

        rows = await fetch_agents_with_details(db, user.id)

        assert [agent.name for agent, *_ in rows] == ["mine"]

    async def test_returns_nothing_for_a_user_with_no_agents(self, db, user) -> None:  # noqa: ANN001
        assert await fetch_agents_with_details(db, user.id) == []


class TestDataAgentNameExists:
    async def test_matching_ignores_case(self, db, user, make_agent) -> None:  # noqa: ANN001
        await make_agent(user, "Reporter")
        assert await data_agent_name_exists(db, user.id, "reporter") is True

    async def test_returns_false_for_an_unused_name(
        self, db, user, make_agent  # noqa: ANN001
    ) -> None:
        await make_agent(user, "Reporter")
        assert await data_agent_name_exists(db, user.id, "Analyst") is False

    async def test_is_scoped_to_the_user(
        self, db, user, other_user, make_agent  # noqa: ANN001
    ) -> None:
        await make_agent(other_user, "Reporter")
        assert await data_agent_name_exists(db, user.id, "Reporter") is False

    async def test_exclude_id_ignores_the_row_being_renamed(
        self, db, user, make_agent  # noqa: ANN001
    ) -> None:
        agent = await make_agent(user, "Reporter")
        assert await data_agent_name_exists(db, user.id, "Reporter", agent.id) is False


# ---------------------------------------------------------------------------
# tool configs
# ---------------------------------------------------------------------------
class TestFetchToolConfigsWithDetails:
    async def test_returns_the_config_with_its_agent_and_datasource(
        self, db, user, make_agent, make_datasource, make_tool_config  # noqa: ANN001
    ) -> None:
        agent = await make_agent(user, "reporter")
        datasource = await make_datasource(user, "warehouse")
        await make_tool_config(agent, datasource, "query_orders")

        (config, joined_agent, joined_datasource), = await fetch_tool_configs_with_details(
            db, user.id
        )

        assert config.tool_name == "query_orders"
        assert joined_agent.id == agent.id
        assert joined_datasource.datasource_name == "warehouse"

    async def test_ownership_comes_from_the_agent_not_the_config(
        self, db, user, other_user, make_agent, make_datasource, make_tool_config  # noqa: ANN001
    ) -> None:
        """``tool_configs`` has no ``user_id`` column — the join to
        ``data_agents`` is the only thing scoping this list, so a second user's
        config must not appear."""
        mine = await make_agent(user, "mine")
        theirs = await make_agent(other_user, "theirs")
        my_ds = await make_datasource(user, "mine_ds")
        their_ds = await make_datasource(other_user, "their_ds")
        await make_tool_config(mine, my_ds, "my_tool")
        await make_tool_config(theirs, their_ds, "their_tool")

        rows = await fetch_tool_configs_with_details(db, user.id)

        assert [c.tool_name for c, _, _ in rows] == ["my_tool"]

    async def test_the_agent_filter_narrows_the_list(
        self, db, user, make_agent, make_datasource, make_tool_config  # noqa: ANN001
    ) -> None:
        first = await make_agent(user, "first")
        second = await make_agent(user, "second")
        datasource = await make_datasource(user, "warehouse")
        await make_tool_config(first, datasource, "tool_a")
        await make_tool_config(second, datasource, "tool_b")

        rows = await fetch_tool_configs_with_details(db, user.id, data_agent_id=first.id)

        assert [c.tool_name for c, _, _ in rows] == ["tool_a"]

    async def test_returns_nothing_when_there_are_no_configs(self, db, user) -> None:  # noqa: ANN001
        assert await fetch_tool_configs_with_details(db, user.id) == []


class TestFetchEnabledToolsForAgent:
    async def test_returns_only_enabled_configs(
        self, db, user, make_agent, make_datasource, make_tool_config  # noqa: ANN001
    ) -> None:
        """The runtime builds both the prompt and the callable tools from this
        list, so a disabled tool leaking through would be advertised to the model
        and then fail when called."""
        agent = await make_agent(user, "reporter")
        datasource = await make_datasource(user, "warehouse")
        await make_tool_config(agent, datasource, "on_tool", is_enabled=True)
        await make_tool_config(agent, datasource, "off_tool", is_enabled=False)

        rows = await fetch_enabled_tools_for_agent(db, agent.id)

        assert [c.tool_name for c, _ in rows] == ["on_tool"]

    async def test_is_scoped_to_the_named_agent(
        self, db, user, make_agent, make_datasource, make_tool_config  # noqa: ANN001
    ) -> None:
        first = await make_agent(user, "first")
        second = await make_agent(user, "second")
        datasource = await make_datasource(user, "warehouse")
        await make_tool_config(first, datasource, "tool_a")
        await make_tool_config(second, datasource, "tool_b")

        rows = await fetch_enabled_tools_for_agent(db, first.id)

        assert [c.tool_name for c, _ in rows] == ["tool_a"]

    async def test_pairs_each_config_with_its_datasource(
        self, db, user, make_agent, make_datasource, make_tool_config  # noqa: ANN001
    ) -> None:
        agent = await make_agent(user, "reporter")
        warehouse = await make_datasource(user, "warehouse")
        crm = await make_datasource(user, "crm")
        await make_tool_config(agent, warehouse, "wh_tool")
        await make_tool_config(agent, crm, "crm_tool")

        rows = await fetch_enabled_tools_for_agent(db, agent.id)

        assert {c.tool_name: d.datasource_name for c, d in rows} == {
            "wh_tool": "warehouse",
            "crm_tool": "crm",
        }

    async def test_an_agent_with_no_tools_returns_nothing(
        self, db, user, make_agent  # noqa: ANN001
    ) -> None:
        agent = await make_agent(user, "inert")
        assert await fetch_enabled_tools_for_agent(db, agent.id) == []


class TestToolNameExists:
    async def test_matching_ignores_case(
        self, db, user, make_agent, make_datasource, make_tool_config  # noqa: ANN001
    ) -> None:
        agent = await make_agent(user, "reporter")
        datasource = await make_datasource(user, "warehouse")
        await make_tool_config(agent, datasource, "Query_Orders")

        assert await tool_name_exists(db, agent.id, "query_orders") is True

    async def test_is_scoped_to_one_agent(
        self, db, user, make_agent, make_datasource, make_tool_config  # noqa: ANN001
    ) -> None:
        """Two agents may each have a ``query_orders`` tool; uniqueness is per
        agent, not global."""
        first = await make_agent(user, "first")
        second = await make_agent(user, "second")
        datasource = await make_datasource(user, "warehouse")
        await make_tool_config(first, datasource, "query_orders")

        assert await tool_name_exists(db, second.id, "query_orders") is False

    async def test_exclude_id_ignores_the_row_being_renamed(
        self, db, user, make_agent, make_datasource, make_tool_config  # noqa: ANN001
    ) -> None:
        agent = await make_agent(user, "reporter")
        datasource = await make_datasource(user, "warehouse")
        config = await make_tool_config(agent, datasource, "query_orders")

        assert await tool_name_exists(db, agent.id, "query_orders", config.id) is False


# ---------------------------------------------------------------------------
# flow builder
# ---------------------------------------------------------------------------
class TestFetchFlowsWithChatbotNames:
    async def test_an_unattached_flow_reports_a_null_chatbot_name(
        self, db, user, make_flow  # noqa: ANN001
    ) -> None:
        await make_flow(user, "onboarding")

        rows = await fetch_flows_with_chatbot_names(db, user.id)

        assert rows[0][0].name == "onboarding"
        assert rows[0][1] is None

    async def test_an_attached_flow_reports_the_chatbot_name(
        self, db, user, make_flow, make_datasource, make_chatbot  # noqa: ANN001
    ) -> None:
        datasource = await make_datasource(user, "warehouse")
        chatbot = await make_chatbot(user, "Support Bot", datasource)
        await make_flow(user, "onboarding", chatbot_key_id=chatbot.id)

        rows = await fetch_flows_with_chatbot_names(db, user.id)

        assert rows[0][1] == "Support Bot"

    async def test_excludes_other_users_flows(
        self, db, user, other_user, make_flow  # noqa: ANN001
    ) -> None:
        await make_flow(user, "mine")
        await make_flow(other_user, "theirs")

        rows = await fetch_flows_with_chatbot_names(db, user.id)

        assert [flow.name for flow, _ in rows] == ["mine"]

    async def test_the_outer_join_does_not_drop_unattached_flows(
        self, db, user, make_flow, make_datasource, make_chatbot  # noqa: ANN001
    ) -> None:
        """An inner join here would silently hide every flow that has not been
        wired to a chatbot yet — which is every flow, right after creation."""
        datasource = await make_datasource(user, "warehouse")
        chatbot = await make_chatbot(user, "Support Bot", datasource)
        await make_flow(user, "attached", chatbot_key_id=chatbot.id)
        await make_flow(user, "detached")

        rows = await fetch_flows_with_chatbot_names(db, user.id)

        assert len(rows) == 2

    async def test_returns_nothing_for_a_user_with_no_flows(self, db, user) -> None:  # noqa: ANN001
        assert await fetch_flows_with_chatbot_names(db, user.id) == []


# ---------------------------------------------------------------------------
# chatbot — get_or_create_ai_settings
# ---------------------------------------------------------------------------
class TestGetOrCreateAiSettings:
    async def test_creates_the_row_when_absent(
        self, db, user, make_datasource, make_chatbot  # noqa: ANN001
    ) -> None:
        datasource = await make_datasource(user, "warehouse")
        chatbot = await make_chatbot(user, "Support Bot", datasource)

        settings = await chatbot_queries.get_or_create_ai_settings(db, chatbot.id)

        assert settings.id is not None
        assert settings.chatbot_key_id == chatbot.id

    async def test_returns_the_existing_row_on_a_second_call(
        self, db, user, make_datasource, make_chatbot  # noqa: ANN001
    ) -> None:
        """Get-or-create, not create-or-duplicate: a second call must not insert
        a competing settings row for the same chatbot."""
        datasource = await make_datasource(user, "warehouse")
        chatbot = await make_chatbot(user, "Support Bot", datasource)

        first = await chatbot_queries.get_or_create_ai_settings(db, chatbot.id)
        second = await chatbot_queries.get_or_create_ai_settings(db, chatbot.id)

        assert first.id == second.id

    async def test_each_chatbot_gets_its_own_row(
        self, db, user, make_datasource, make_chatbot  # noqa: ANN001
    ) -> None:
        datasource = await make_datasource(user, "warehouse")
        first = await make_chatbot(user, "First Bot", datasource)
        second = await make_chatbot(user, "Second Bot", datasource)

        a = await chatbot_queries.get_or_create_ai_settings(db, first.id)
        b = await chatbot_queries.get_or_create_ai_settings(db, second.id)

        assert a.id != b.id
