"""
Tests for the ``agent`` target type — a chatbot widget with no datasource of its
own, whose attached data agent's tool configs are the whole of its scope.

Three things have to hold, and each has cost the other two if it slips:

* **Creation is exclusive.** An agent target stores a NULL datasource, empty
  targets and a real agent. A datasource target keeps requiring a datasource.
  Getting this wrong produces a widget with two answers to "what can it read?".
* **The agent cannot be detached afterwards.** The datasource target is immutable
  after creation, so clearing the agent of an agent-backed widget would leave a
  published key that can answer nothing, with no route back through the form.
* **The runtime degrades honestly.** When the agent cannot run there is no data
  profile to fall back on, so the visitor is told the data is unreachable rather
  than being handed an error — or, worse, an answer from somewhere else.

The reply-path tests stub ``deep_agent_service.answer_for_chatbot`` at the seam
``chatbot_reply_service`` imports: whether deepagents runs is not what is being
tested here, only what happens when it fails.
"""

from __future__ import annotations

import uuid as uuid_pkg

import pytest
from litestar.exceptions import HTTPException

from app.models.chatbot import TARGET_TYPE_AGENT, ChatbotApiKey
from app.models.data_agents import DataAgent
from app.models.datasource import DataSource
from app.models.workspaces import Workspace
from app.services.chatbot import chatbot_reply_service, chatbot_service


@pytest.fixture
async def agent(db, user) -> DataAgent:  # noqa: ANN001
    row = DataAgent(user_id=user.id, name="inventory")
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


@pytest.fixture
async def other_agent(db, user) -> DataAgent:  # noqa: ANN001
    row = DataAgent(user_id=user.id, name="sales")
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


ORIGINS = "https://example.com"


async def create_agent_backed(db, user, agent, **overrides):  # noqa: ANN001
    kwargs = {
        "name": "Support bot",
        "datasource_id": None,
        "target_type": TARGET_TYPE_AGENT,
        "target_names": [],
        "file_ids": [],
        "allowed_origins_raw": ORIGINS,
        "data_agent_id": agent.uuid,
    }
    kwargs.update(overrides)
    return await chatbot_service.create_chatbot_key(db, user.id, **kwargs)


class TestCreatingAnAgentBackedWidget:
    async def test_it_stores_no_datasource(self, db, user, agent) -> None:  # noqa: ANN001
        key = await create_agent_backed(db, user, agent)

        assert key.datasource_id is None
        assert key.target_type == TARGET_TYPE_AGENT
        assert key.target_names == []
        assert key.file_ids == []
        assert key.data_agent_id == agent.id

    async def test_the_workspace_is_remembered_for_the_picker(
        self, db, user, agent, workspace  # noqa: ANN001
    ) -> None:
        key = await create_agent_backed(db, user, agent, workspace_id=workspace.uuid)

        assert key.workspace_id == workspace.id

    async def test_it_still_gets_a_persona(self, db, user, agent) -> None:  # noqa: ANN001
        """Both creation paths seed AI settings — the first visitor message must be
        answered with a real prompt, not a placeholder made later."""
        from app.services.chatbot.chatbot_ai_settings_service import (
            get_ai_settings_by_key_id,
        )

        key = await create_agent_backed(db, user, agent)

        assert await get_ai_settings_by_key_id(db, key.id) is not None

    async def test_an_agent_target_without_an_agent_is_refused(
        self, db, user  # noqa: ANN001
    ) -> None:
        """A workspace alone lands here: it groups agents and points at no data."""
        with pytest.raises(HTTPException, match="Pick a data agent"):
            await chatbot_service.create_chatbot_key(
                db,
                user.id,
                name="Support bot",
                datasource_id=None,
                target_type=TARGET_TYPE_AGENT,
                target_names=[],
                file_ids=[],
                allowed_origins_raw=ORIGINS,
            )

    async def test_another_users_agent_cannot_be_attached(
        self, db, user, make_user  # noqa: ANN001
    ) -> None:
        """The ownership check is what stops a pasted uuid attaching someone else's
        agent — and with it their datasource credentials."""
        intruder = await make_user("intruder@example.com")
        theirs = DataAgent(user_id=intruder.id, name="theirs")
        db.add(theirs)
        await db.commit()
        await db.refresh(theirs)

        with pytest.raises(HTTPException):
            await create_agent_backed(db, user, theirs)


class TestDatasourceTargetsAreUnchanged:
    async def test_a_datasource_target_still_requires_a_datasource(
        self, db, user  # noqa: ANN001
    ) -> None:
        with pytest.raises(HTTPException, match="select a data source"):
            await chatbot_service.create_chatbot_key(
                db,
                user.id,
                name="Support bot",
                datasource_id=None,
                target_type="datasource",
                target_names=[],
                file_ids=[],
                allowed_origins_raw=ORIGINS,
            )

    async def test_a_datasource_widget_is_created_as_before(
        self, db, user, datasource  # noqa: ANN001
    ) -> None:
        key = await chatbot_service.create_chatbot_key(
            db,
            user.id,
            name="Support bot",
            datasource_id=datasource.uuid,
            target_type="table",
            target_names=["inventory_items"],
            file_ids=[],
            allowed_origins_raw=ORIGINS,
        )

        assert key.datasource_id == datasource.id
        assert key.target_names == ["inventory_items"]
        assert key.data_agent_id is None


class TestDetachingTheAgent:
    async def test_it_is_refused_for_an_agent_backed_widget(
        self, db, user, agent  # noqa: ANN001
    ) -> None:
        """There is nothing to fall back to, and the datasource target is immutable
        after creation — so this would strand a published key."""
        key = await create_agent_backed(db, user, agent)

        with pytest.raises(HTTPException, match="no data source of its own"):
            await chatbot_service.set_chatbot_data_agent(db, user.id, key.uuid)

        await db.refresh(key)
        assert key.data_agent_id == agent.id

    async def test_swapping_to_another_agent_is_allowed(
        self, db, user, agent, other_agent  # noqa: ANN001
    ) -> None:
        """The operation that case actually needs: a replacement, not a removal."""
        key = await create_agent_backed(db, user, agent)

        await chatbot_service.set_chatbot_data_agent(
            db, user.id, key.uuid, data_agent_id=other_agent.uuid,
        )
        await db.refresh(key)

        assert key.data_agent_id == other_agent.id

    async def test_a_datasource_backed_widget_can_still_detach(
        self, db, user, agent, datasource  # noqa: ANN001
    ) -> None:
        """It has a data profile to go back to, which is the whole difference."""
        key = await chatbot_service.create_chatbot_key(
            db,
            user.id,
            name="Support bot",
            datasource_id=datasource.uuid,
            target_type="datasource",
            target_names=[],
            file_ids=[],
            allowed_origins_raw=ORIGINS,
            data_agent_id=agent.uuid,
        )

        await chatbot_service.set_chatbot_data_agent(db, user.id, key.uuid)
        await db.refresh(key)

        assert key.data_agent_id is None


class TestAnsweringWithNoDatasource:
    async def test_the_profile_path_refuses_rather_than_looking_one_up(
        self, db, user, agent  # noqa: ANN001
    ) -> None:
        """Guarded before the lookup: filtering on id=None would otherwise produce
        "no longer available", which is wrong — it never had one."""
        key = await create_agent_backed(db, user, agent)

        with pytest.raises(HTTPException) as excinfo:
            await chatbot_service.answer_message(db, key, "how many items?")

        assert "no data source of its own" in str(excinfo.value.detail)

    async def test_a_failed_agent_is_answered_honestly(
        self, db, user, agent, monkeypatch  # noqa: ANN001
    ) -> None:
        """The whole point of the fallback branch: a misconfigured agent must not
        become an error bubble in a published widget, and with no datasource there
        is no profile answer to give instead."""
        key = await create_agent_backed(db, user, agent)

        async def failing_agent(*args, **kwargs):  # noqa: ANN002, ANN003
            raise HTTPException(status_code=400, detail="agent has no enabled tools")

        monkeypatch.setattr(
            chatbot_reply_service.deep_agent_service,
            "answer_for_chatbot",
            failing_agent,
        )

        result = await chatbot_reply_service.generate_reply(db, key, "how many items?")

        assert "can't reach that data" in result.summary
        # Nothing is invented in its place.
        assert result.insights == []
        assert result.table is None

    async def test_a_working_agent_answers_normally(
        self, db, user, agent, monkeypatch  # noqa: ANN001
    ) -> None:
        key = await create_agent_backed(db, user, agent)

        async def working_agent(*args, **kwargs):  # noqa: ANN002, ANN003
            return {"answer": "We stock 42 items."}

        monkeypatch.setattr(
            chatbot_reply_service.deep_agent_service,
            "answer_for_chatbot",
            working_agent,
        )

        result = await chatbot_reply_service.generate_reply(db, key, "how many items?")

        assert result.summary == "We stock 42 items."
