"""
Route-level tests for creating an agent-backed chatbot widget.

Through a real HTTP round trip, because the rule is carried by the form as much as
by the service: the mode is decided by a <select> in a partial that htmx replaces,
the datasource block is hidden by a sibling script, and the schema rejects a
submission carrying both answers. A service test cannot see a form that posts the
wrong pair of fields.
"""

from __future__ import annotations

import pytest

from app.models.chatbot import ChatbotApiKey
from app.models.data_agents import DataAgent
from app.models.datasource import DataSource
from app.models.workspaces import Workspace
from app.routes.chatbot import ChatbotSettingsController
from app.routes.deep_agents import DeepAgentController


@pytest.fixture
async def agent(db, user) -> DataAgent:  # noqa: ANN001
    row = DataAgent(user_id=user.id, name="inventory")
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


@pytest.fixture
async def workspace(db, user) -> Workspace:  # noqa: ANN001
    row = Workspace(user_id=user.id, name="retail")
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


@pytest.fixture
async def datasource(db, user) -> DataSource:  # noqa: ANN001
    row = DataSource(
        user_id=user.id,
        datasource_name="pantry_mate",
        db_type="postgres",
        password_encrypted="enc",
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


@pytest.fixture
def client(auth_client_factory):  # noqa: ANN001, ANN201
    return auth_client_factory(ChatbotSettingsController, DeepAgentController)


class TestTheCreateForm:
    def test_it_offers_the_agent_picker_before_the_datasource_block(
        self, client  # noqa: ANN001
    ) -> None:
        """Order matters: the agent decides whether the datasource is asked at all,
        so it cannot come after the fields it removes."""
        body = client.get("/chatbot-settings").text

        assert body.index("cbDataAgentField") < body.index("cbDatasourceBlock")

    def test_the_no_agent_option_is_still_offered_on_create(
        self, client  # noqa: ANN001
    ) -> None:
        """Creating a datasource-scoped widget is unchanged and still the default."""
        body = client.get("/chatbot-settings").text

        assert "answer from a data profile" in body
        assert 'id="cbDatasourceBlock"' in body
        assert 'id="cbAgentScopeNote"' in body


class TestCreatingThroughTheForm:
    async def test_an_agent_target_needs_no_datasource(
        self, client, db, user, agent, workspace  # noqa: ANN001
    ) -> None:
        response = client.post("/chatbot-settings/create", data={
            "name": "Support bot",
            "target_type": "agent",
            "workspace_id": str(workspace.uuid),
            "data_agent_id": str(agent.uuid),
            "allowed_origins": "https://example.com",
        })

        assert response.status_code == 200

        keys = await chatbot_keys(db, user)
        assert len(keys) == 1
        assert keys[0].datasource_id is None
        assert keys[0].data_agent_id == agent.id
        assert keys[0].workspace_id == workspace.id

    def test_a_workspace_alone_is_refused(
        self, client, workspace  # noqa: ANN001
    ) -> None:
        """A workspace groups agents and points at no data, so this would be a
        widget that can answer nothing."""
        response = client.post("/chatbot-settings/create", data={
            "name": "Support bot",
            "target_type": "agent",
            "workspace_id": str(workspace.uuid),
            "allowed_origins": "https://example.com",
        })

        assert "choose a data agent" in response.text

    def test_an_agent_target_carrying_a_datasource_is_refused(
        self, client, agent, datasource  # noqa: ANN001
    ) -> None:
        """A form that got out of step with itself. Dropping one of the two answers
        silently is how a widget ends up scoped to something nobody chose."""
        response = client.post("/chatbot-settings/create", data={
            "name": "Support bot",
            "target_type": "agent",
            "data_agent_id": str(agent.uuid),
            "datasource_id": str(datasource.uuid),
            "allowed_origins": "https://example.com",
        })

        assert "no data source of its own" in response.text

    def test_a_datasource_target_still_requires_one(self, client) -> None:  # noqa: ANN001
        response = client.post("/chatbot-settings/create", data={
            "name": "Support bot",
            "target_type": "datasource",
            "allowed_origins": "https://example.com",
        })

        assert "select a data source" in response.text


class TestTheAgentPickerAfterCreation:
    async def test_an_agent_backed_widget_cannot_offer_no_agent(
        self, client, db, user, agent  # noqa: ANN001
    ) -> None:
        """Detaching would strand the widget, so the option is not rendered — the
        service refuses it either way."""
        key = await make_agent_backed_key(db, user, agent)

        body = client.get(f"/chatbot-settings/{key.uuid}/widget-settings?tab=ai").text

        assert "answer from a data profile" not in body
        assert "Select a data agent" in body
        assert "can't be removed" in body

    async def test_the_cascade_keeps_the_option_hidden(
        self, client, db, user, agent, workspace  # noqa: ANN001
    ) -> None:
        """Without `required` surviving the cascade, the option would come back the
        moment a workspace was chosen."""
        body = client.get(
            "/deep-agents/agent-options",
            params={"workspace_id": str(workspace.uuid), "required": "true"},
        ).text

        assert "answer from a data profile" not in body
        assert "Select a data agent" in body

    def test_the_cascade_offers_it_by_default(self, client, workspace) -> None:  # noqa: ANN001
        body = client.get(
            "/deep-agents/agent-options",
            params={"workspace_id": str(workspace.uuid)},
        ).text

        assert "answer from a data profile" in body


async def chatbot_keys(db, user):  # noqa: ANN001, ANN201
    from sqlalchemy import select

    result = await db.execute(
        select(ChatbotApiKey).where(ChatbotApiKey.user_id == user.id)
    )
    return list(result.scalars().all())


async def make_agent_backed_key(db, user, agent) -> ChatbotApiKey:  # noqa: ANN001
    from app.services.chatbot.chatbot_service import create_chatbot_key

    return await create_chatbot_key(
        db,
        user.id,
        name="Support bot",
        datasource_id=None,
        target_type="agent",
        target_names=[],
        file_ids=[],
        allowed_origins_raw="https://example.com",
        data_agent_id=agent.uuid,
    )
