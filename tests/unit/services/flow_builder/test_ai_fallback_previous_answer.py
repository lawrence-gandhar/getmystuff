"""
Tests for ``run_ai_fallback``'s ``previous_answer`` parameter — the prior answer a
dead-end AI Fallback node (see ``engine_service._continue_dead_end_ai_fallback``)
passes back in when a visitor's follow-up message re-asks the same node.

The property this file exists to pin down: **it has to land as conversational
context, not as a guardrail.** ``extra_instructions`` is rendered to the model as
"Always follow the owner's guardrails: ..." (``ai_analytics_service._build_prompts``),
so a prior answer routed through there would tell the model to treat its own last
answer as a rule to obey rather than something it already said. ``action_context`` is
the channel already used for this shape of thing (a webhook action's result), and it
reaches the model as ordinary user content on both branches — so that is where
``previous_answer`` has to go instead.

Every other seam (``chatbot_service.answer_message``, ``answer_freeform``,
``knowledge_base_service.retrieve_context``, ``maybe_run_action``,
``chatbot_reply_service.load_ai_context``) is stubbed: what is under test here is only
where ``previous_answer`` ends up, not how an answer is produced.
"""

from __future__ import annotations

import pytest

from app.models.chatbot import ChatbotApiKey
from app.services.ai_analytics.ai_analytics_service import AnalyticsResult
from app.services.flow_builder import ai_fallback_service as svc

FLOW_ID = 4
NODE_ID = "ai_1"
PREVIOUS = "Returns are accepted within 30 days of purchase."


def _key(user_id: int = 7) -> ChatbotApiKey:
    key = ChatbotApiKey()
    key.user_id = user_id
    return key


class _AiContext:
    def __init__(self) -> None:
        self.system_prompt = "You are a helpful assistant."
        self.variables = {}


@pytest.fixture(autouse=True)
def stub_shared_seams(monkeypatch: pytest.MonkeyPatch) -> None:
    """The seams every branch of run_ai_fallback touches regardless of context_source."""

    async def load_ai_context(db, chatbot_key):  # noqa: ANN001
        return _AiContext()

    async def no_action(db, chatbot_key, message, llm_choice, variables):  # noqa: ANN001
        return None

    monkeypatch.setattr(svc.chatbot_reply_service, "load_ai_context", load_ai_context)
    monkeypatch.setattr(svc, "maybe_run_action", no_action)


class TestTheDatasourceBranch:
    async def test_previous_answer_reaches_action_context_not_extra_instructions(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: dict = {}

        async def answer_message(db, chatbot_key, message, **kwargs):  # noqa: ANN001
            captured.update(kwargs)
            return AnalyticsResult(summary="ok")

        monkeypatch.setattr(svc.chatbot_service, "answer_message", answer_message)

        await svc.run_ai_fallback(
            None, _key(), FLOW_ID, NODE_ID,
            {"context_source": "datasource", "llm_mode": "in_built"},
            "what about opened items?",
            previous_answer=PREVIOUS,
        )

        assert PREVIOUS in captured["action_context"]
        assert PREVIOUS not in captured["extra_instructions"]

    async def test_no_previous_answer_leaves_action_context_as_before(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: dict = {}

        async def answer_message(db, chatbot_key, message, **kwargs):  # noqa: ANN001
            captured.update(kwargs)
            return AnalyticsResult(summary="ok")

        monkeypatch.setattr(svc.chatbot_service, "answer_message", answer_message)

        await svc.run_ai_fallback(
            None, _key(), FLOW_ID, NODE_ID,
            {"context_source": "datasource", "llm_mode": "in_built"},
            "how many orders?",
        )

        assert captured["action_context"] == ""

    async def test_it_is_prepended_ahead_of_a_webhook_actions_own_context(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def with_action(db, chatbot_key, message, llm_choice, variables):  # noqa: ANN001
            class _Outcome:
                context_text = "Action ran: ticket #42 created."
            return _Outcome()

        monkeypatch.setattr(svc, "maybe_run_action", with_action)

        captured: dict = {}

        async def answer_message(db, chatbot_key, message, **kwargs):  # noqa: ANN001
            captured.update(kwargs)
            return AnalyticsResult(summary="ok")

        monkeypatch.setattr(svc.chatbot_service, "answer_message", answer_message)

        await svc.run_ai_fallback(
            None, _key(), FLOW_ID, NODE_ID,
            {"context_source": "datasource", "llm_mode": "in_built"},
            "and now?",
            previous_answer=PREVIOUS,
        )

        assert captured["action_context"].index(PREVIOUS) < captured["action_context"].index(
            "ticket #42",
        )


class TestTheKnowledgeBaseBranch:
    async def test_previous_answer_reaches_the_composed_user_content(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def no_context(db, flow_id, node_id, query):  # noqa: ANN001
            return None

        captured: dict = {}

        async def answer_freeform(db, user_id, **kwargs):  # noqa: ANN001
            captured.update(kwargs)
            return AnalyticsResult(summary="ok")

        monkeypatch.setattr(svc.knowledge_base_service, "retrieve_context", no_context)
        monkeypatch.setattr(svc, "answer_freeform", answer_freeform)

        await svc.run_ai_fallback(
            None, _key(), FLOW_ID, NODE_ID,
            {"context_source": "knowledge_base", "llm_mode": "in_built"},
            "what about opened items?",
            previous_answer=PREVIOUS,
        )

        assert PREVIOUS in captured["user_content"]


class TestThePromptOnlyBranch:
    async def test_previous_answer_reaches_the_composed_user_content(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: dict = {}

        async def answer_freeform(db, user_id, **kwargs):  # noqa: ANN001
            captured.update(kwargs)
            return AnalyticsResult(summary="ok")

        monkeypatch.setattr(svc, "answer_freeform", answer_freeform)

        await svc.run_ai_fallback(
            None, _key(), FLOW_ID, NODE_ID,
            {"context_source": "prompt", "llm_mode": "in_built"},
            "what about opened items?",
            previous_answer=PREVIOUS,
        )

        assert PREVIOUS in captured["user_content"]
