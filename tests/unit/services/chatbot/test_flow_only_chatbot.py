"""
Tests for a **flow-only chatbot**: one whose whole scope is its Flow Builder flow,
because it has no datasource of its own and its data agent has no enabled tools.

That configuration is legitimate and was reachable long before it was handled. A
flow needs no tool configs at all — a Send Message / Menu / AI Fallback graph reads
a knowledge base, not a database — so an operator can build a working conversation
and never open Tool Configs. What used to happen the moment such a visitor finished
the flow was the failure this file pins shut, twice over:

* **The blocking turn** handed the turn to the AI path, which refused (no tools) and
  produced a sentence about unreachable *data* — describing a data source the widget
  was never given, for a chatbot that was never meant to answer freely.
* **The streamed turn** was worse: ``_stream_as_agent`` yields its setup refusal as
  an ``error`` event, and the widget paints an error bubble verbatim. So a published
  widget showed a member of the public the operator's configuration to-do list
  ("Add a tool for it in the Tool Configs section").

Both are answered here by the same rule — a flow-only chatbot says what its scope
is — and the streaming path reaches it by declining to stream at all, which is what
``reason: "agent_unavailable"`` is for.

The engine is stubbed at the seam ``chatbot_turn_service`` imports it by: whether a
particular graph reaches its End node is ``test_engine_*``'s subject, not this
file's. What is real here is the chatbot key, the agent and its tool rows, because
the decision under test is made from exactly those three.
"""

from __future__ import annotations

from typing import AsyncIterator, Dict, List

import pytest

from app.models.chatbot import TARGET_TYPE_AGENT, ChatbotMessage
from app.models.data_agents import DataAgent
from app.models.datasource import DataSource
from app.models.tool_configs import ToolConfig
from app.services.ai_analytics.ai_analytics_service import AnalyticsResult
from app.services.chatbot import chatbot_service, chatbot_turn_service
from app.services.flow_builder import engine_service

ORIGINS = "https://example.com"
SESSION = "sess-flow-only"


@pytest.fixture
async def agent(db, user) -> DataAgent:  # noqa: ANN001
    row = DataAgent(user_id=user.id, name="hr panel")
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


@pytest.fixture
async def datasource(db, user) -> DataSource:  # noqa: ANN001
    row = DataSource(
        user_id=user.id,
        datasource_name="people_db",
        db_type="postgres",
        password_encrypted="enc",
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def add_tool(db, agent, datasource, *, is_enabled: bool = True) -> ToolConfig:  # noqa: ANN001
    row = ToolConfig(
        data_agent_id=agent.id,
        datasource_id=datasource.id,
        tool_name="leave_balance",
        table_name="leaves",
        config={},
        is_enabled=is_enabled,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def agent_backed_key(db, user, agent):  # noqa: ANN001
    return await chatbot_service.create_chatbot_key(
        db,
        user.id,
        name="HR panel",
        datasource_id=None,
        target_type=TARGET_TYPE_AGENT,
        target_names=[],
        file_ids=[],
        allowed_origins_raw=ORIGINS,
        data_agent_id=agent.uuid,
    )


def hand_off(monkeypatch) -> None:  # noqa: ANN001
    """Make every turn look like "this visitor has finished the flow"."""

    async def active_flow(*args, **kwargs):  # noqa: ANN002, ANN003
        return object()

    async def handoff(*args, **kwargs):  # noqa: ANN002, ANN003
        return engine_service.FlowEngineResult(type=engine_service.AI_HANDOFF)

    monkeypatch.setattr(chatbot_turn_service.flow_service, "get_active_flow", active_flow)
    monkeypatch.setattr(
        chatbot_turn_service.engine_service, "advance_flow_session", handoff,
    )


# --------------------------------------------------------------------------
# Is there an AI path behind the flow at all?
# --------------------------------------------------------------------------

class TestCanAnswerOffFlow:
    async def test_an_agent_with_no_tools_is_not_an_ai_path(
        self, db, user, agent  # noqa: ANN001
    ) -> None:
        """The case the whole file exists for. An attached agent is not enough:
        deep_agent_service refuses a toolless one before a model is built, so there
        is nothing behind the flow."""
        key = await agent_backed_key(db, user, agent)

        assert await chatbot_turn_service._can_answer_off_flow(db, key) is False

    async def test_one_enabled_tool_is_enough(
        self, db, user, agent, datasource  # noqa: ANN001
    ) -> None:
        key = await agent_backed_key(db, user, agent)
        await add_tool(db, agent, datasource)

        assert await chatbot_turn_service._can_answer_off_flow(db, key) is True

    async def test_a_disabled_tool_does_not_count(
        self, db, user, agent, datasource  # noqa: ANN001
    ) -> None:
        """Switching a tool off in Tool Configs is how an operator takes it away —
        the row surviving must not read as an answerable agent."""
        key = await agent_backed_key(db, user, agent)
        await add_tool(db, agent, datasource, is_enabled=False)

        assert await chatbot_turn_service._can_answer_off_flow(db, key) is False

    async def test_a_datasource_target_always_has_one(
        self, db, user, datasource  # noqa: ANN001
    ) -> None:
        """It can compute a data profile with no agent and no tools at all, so its
        flow ending is an ordinary handoff and nothing here should intercept it."""
        key = await chatbot_service.create_chatbot_key(
            db,
            user.id,
            name="Support bot",
            datasource_id=datasource.uuid,
            target_type="datasource",
            target_names=[],
            file_ids=[],
            allowed_origins_raw=ORIGINS,
        )

        assert await chatbot_turn_service._can_answer_off_flow(db, key) is True


# --------------------------------------------------------------------------
# The blocking turn
# --------------------------------------------------------------------------

class TestHandoffOnABlockingTurn:
    async def test_it_states_its_scope_instead_of_reaching_for_a_model(
        self, db, user, agent, monkeypatch  # noqa: ANN001
    ) -> None:
        key = await agent_backed_key(db, user, agent)
        hand_off(monkeypatch)

        called: List[str] = []

        async def reply(*args, **kwargs):  # noqa: ANN002, ANN003
            called.append("ai")
            raise AssertionError("the AI path must not be reached")

        monkeypatch.setattr(chatbot_turn_service, "generate_reply", reply)

        result = await chatbot_turn_service.answer_turn(
            db, key, "what about payroll?", session_token=SESSION,
        )

        assert result.status == "success"
        assert "restart button" in result.summary
        assert called == []

    async def test_the_turn_is_logged_as_a_flow_turn(
        self, db, user, agent, monkeypatch  # noqa: ANN001
    ) -> None:
        """No model ran, so counting it as an AI turn would overstate what the
        Chatbot Analytics dashboard reports about model usage."""
        key = await agent_backed_key(db, user, agent)
        hand_off(monkeypatch)

        await chatbot_turn_service.answer_turn(
            db, key, "what about payroll?", session_token=SESSION,
        )

        rows = (
            await db.execute(
                ChatbotMessage.__table__.select().where(
                    ChatbotMessage.chatbot_key_id == key.id
                )
            )
        ).mappings().all()

        assert len(rows) == 1
        assert rows[0]["turn_type"] == "flow"
        assert rows[0]["status"] == "success"

    async def test_a_tooled_agent_still_takes_the_handoff(
        self, db, user, agent, datasource, monkeypatch  # noqa: ANN001
    ) -> None:
        """The pre-check must not swallow the handoff for a chatbot that can answer
        — a flow ending is where free answering is supposed to begin."""
        key = await agent_backed_key(db, user, agent)
        await add_tool(db, agent, datasource)
        hand_off(monkeypatch)

        async def reply(*args, **kwargs):  # noqa: ANN002, ANN003
            return AnalyticsResult(summary="You have 12 days left.")

        monkeypatch.setattr(chatbot_turn_service, "generate_reply", reply)

        result = await chatbot_turn_service.answer_turn(
            db, key, "how much leave do I have?", session_token=SESSION,
        )

        assert result.summary == "You have 12 days left."


# --------------------------------------------------------------------------
# The streamed turn
# --------------------------------------------------------------------------

class TestStreamedSetupFailure:
    async def test_a_setup_refusal_becomes_a_fallback_not_an_error(
        self, db, user, agent, monkeypatch  # noqa: ANN001
    ) -> None:
        """The bug a visitor could actually see: the agent's own words, written for
        the operator, painted into a public chat window."""
        key = await agent_backed_key(db, user, agent)

        async def events(*args, **kwargs) -> AsyncIterator[Dict]:  # noqa: ANN002, ANN003
            yield {
                "event": "error",
                "message": "Add a tool for it in the Tool Configs section.",
                "stage": "setup",
            }

        monkeypatch.setattr(
            chatbot_turn_service.deep_agent_service, "stream_answer_for_chatbot", events,
        )

        collected = [
            event async for event in chatbot_turn_service.stream_turn(
                db, key, "hi", session_token=SESSION,
            )
        ]

        assert collected == [{"event": "fallback", "reason": "agent_unavailable"}]

    async def test_nothing_is_logged_for_a_turn_that_will_be_retried(
        self, db, user, agent, monkeypatch  # noqa: ANN001
    ) -> None:
        """The widget re-POSTs to /message on `fallback`, and that turn writes its
        own row — logging here too would double every such question in the owner's
        history and in Chatbot Analytics."""
        key = await agent_backed_key(db, user, agent)

        async def events(*args, **kwargs) -> AsyncIterator[Dict]:  # noqa: ANN002, ANN003
            yield {"event": "error", "message": "no tools", "stage": "setup"}

        monkeypatch.setattr(
            chatbot_turn_service.deep_agent_service, "stream_answer_for_chatbot", events,
        )

        async for _ in chatbot_turn_service.stream_turn(db, key, "hi", session_token=SESSION):
            pass

        rows = (
            await db.execute(
                ChatbotMessage.__table__.select().where(
                    ChatbotMessage.chatbot_key_id == key.id
                )
            )
        ).mappings().all()

        assert rows == []

    async def test_a_failure_after_work_is_still_reported_and_logged(
        self, db, user, agent, monkeypatch  # noqa: ANN001
    ) -> None:
        """The other half of the rule. A timeout or a rate limit happens *after* a
        model call, so it must reach the visitor as an error and be recorded —
        retrying it would bill the owner twice for one question."""
        key = await agent_backed_key(db, user, agent)

        async def events(*args, **kwargs) -> AsyncIterator[Dict]:  # noqa: ANN002, ANN003
            yield {"event": "error", "message": "The agent took too long to answer."}

        monkeypatch.setattr(
            chatbot_turn_service.deep_agent_service, "stream_answer_for_chatbot", events,
        )

        collected = [
            event async for event in chatbot_turn_service.stream_turn(
                db, key, "hi", session_token=SESSION,
            )
        ]

        assert collected == [
            {"event": "error", "message": "The agent took too long to answer."}
        ]

        rows = (
            await db.execute(
                ChatbotMessage.__table__.select().where(
                    ChatbotMessage.chatbot_key_id == key.id
                )
            )
        ).mappings().all()

        assert len(rows) == 1
        assert rows[0]["status"] == "error"
