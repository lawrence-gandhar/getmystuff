"""
Tests for what happens on the visitor's *next* message once a session has already
completed — specifically, the one case that no longer means "hand off to generic AI
answering": a session that completed because it dead-ended on an AI Fallback node (one
with no outgoing edge, drawn that way on purpose).

``engine_service._continue_dead_end_ai_fallback`` is where this is decided, and the
property this file is built around is that it says **no** for every other terminal
point — an explicit End node, a dead end on some other block type, a node deleted since
the visitor was parked on it, or a "completed" state left behind by the
``_MAX_INTERNAL_HOPS`` bailout mid-call — so ``AI_HANDOFF`` is untouched for anything
that isn't exactly this one case.

What happens *inside* the dead-end node itself (the previous-answer memory) is
``test_engine_ai_fallback.py``'s subject, not this file's — this file is only about
whether the right node gets asked again at all.

DB-free, like the neighbouring engine tests.
"""

from __future__ import annotations

import uuid

import pytest

from app.models.chatbot import ChatbotApiKey
from app.models.flow_builder import ChatbotFlow, ChatbotFlowSession
from app.services.ai_analytics.ai_analytics_service import AnalyticsResult
from app.services.flow_builder import engine_service

AI_NODE_ID = "ai_1"
OTHER_NODE_ID = "msg_1"
FLOW_ID = 5
SUMMARY = "Here is what I found."


def _dead_end_graph() -> dict:
    return {
        "nodes": [
            {"id": "start", "type": "start", "data": {}},
            {"id": AI_NODE_ID, "type": "ai_fallback",
             "data": {"context_source": "datasource", "llm_mode": "in_built"}},
        ],
        "edges": [
            {"source": "start", "target": AI_NODE_ID, "source_port": "default"},
        ],
    }


def _graph_ending_on_send_message() -> dict:
    """A flow whose true, ordinary end is a Send Message with nothing wired after it —
    the shape a linear flow's last block usually has."""
    return {
        "nodes": [
            {"id": "start", "type": "start", "data": {}},
            {"id": OTHER_NODE_ID, "type": "send_message", "data": {"message_text": "Bye."}},
        ],
        "edges": [
            {"source": "start", "target": OTHER_NODE_ID, "source_port": "default"},
        ],
    }


def _flow(graph: dict, *, flow_id: int = FLOW_ID) -> ChatbotFlow:
    flow = ChatbotFlow()
    flow.id = flow_id
    flow.uuid = uuid.uuid4()
    flow.graph_data = graph
    return flow


def _session(node_id: str, *, flow_id: int = FLOW_ID, **kwargs) -> ChatbotFlowSession:  # noqa: ANN003
    session = ChatbotFlowSession()
    session.id = 21
    session.flow_id = flow_id
    session.current_node_id = node_id
    session.variables = {}
    session.call_stack = kwargs.pop("call_stack", [])
    session.status = "completed"
    return session


def _key(user_id: int = 7) -> ChatbotApiKey:
    key = ChatbotApiKey()
    key.user_id = user_id
    return key


@pytest.fixture
def stub_run_ai_fallback(monkeypatch: pytest.MonkeyPatch) -> dict:
    calls: dict = {"result": AnalyticsResult(summary=SUMMARY), "count": 0}

    async def run_ai_fallback(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        calls["count"] += 1
        return calls["result"]

    monkeypatch.setattr(
        engine_service.ai_fallback_service, "run_ai_fallback", run_ai_fallback,
    )
    return calls


class TestADeadEndAiFallbackKeepsAnswering:
    async def test_a_real_dead_end_node_is_asked_again(self, stub_run_ai_fallback) -> None:  # noqa: ANN001
        flow = _flow(_dead_end_graph())
        session = _session(AI_NODE_ID)

        result = await engine_service._continue_dead_end_ai_fallback(
            None, _key(), flow, session, "another question",
        )

        assert result is not None
        assert result.type == "text"
        assert result.text == SUMMARY
        assert stub_run_ai_fallback["count"] == 1

    async def test_the_session_stays_completed(self, stub_run_ai_fallback) -> None:  # noqa: ANN001
        flow = _flow(_dead_end_graph())
        session = _session(AI_NODE_ID)

        await engine_service._continue_dead_end_ai_fallback(
            None, _key(), flow, session, "another question",
        )

        assert session.status == "completed"
        assert session.current_node_id == AI_NODE_ID


class TestEveryOtherTerminalPointIsUntouched:
    async def test_a_different_node_type_falls_through_to_handoff(
        self, stub_run_ai_fallback,
    ) -> None:  # noqa: ANN001
        """The flow's true, ordinary end — a Send Message nothing is wired after —
        must not be mistaken for a dead-end AI Fallback."""
        flow = _flow(_graph_ending_on_send_message())
        session = _session(OTHER_NODE_ID)

        result = await engine_service._continue_dead_end_ai_fallback(
            None, _key(), flow, session, "hello?",
        )

        assert result is None
        assert stub_run_ai_fallback["count"] == 0

    async def test_a_node_deleted_since_falls_through_to_handoff(
        self, stub_run_ai_fallback,
    ) -> None:  # noqa: ANN001
        flow = _flow(_dead_end_graph())
        session = _session("a_node_that_no_longer_exists")

        result = await engine_service._continue_dead_end_ai_fallback(
            None, _key(), flow, session, "hello?",
        )

        assert result is None
        assert stub_run_ai_fallback["count"] == 0

    async def test_a_stale_non_empty_call_stack_falls_through_to_handoff(
        self, stub_run_ai_fallback,
    ) -> None:  # noqa: ANN001
        """
        Defensive: a genuine root-level "completed" should never carry a call stack —
        `_hop_until_the_turn_ends` converts an in-call dead end back into a normal call
        return before it can bubble up this far. The one gap is the
        `_MAX_INTERNAL_HOPS` bailout, which sets `status = "completed"` directly
        without going through that unwind. Whatever the cause, a non-empty call stack
        here must not be treated as a root-level dead end.
        """
        flow = _flow(_dead_end_graph())
        session = _session(AI_NODE_ID, call_stack=[{
            "flow_id": 99, "return_node_id": "somewhere", "caller_variables": {},
        }])

        result = await engine_service._continue_dead_end_ai_fallback(
            None, _key(), flow, session, "hello?",
        )

        assert result is None
        assert stub_run_ai_fallback["count"] == 0


class TestAdvanceFlowSessionWiring:
    """One end-to-end check that `advance_flow_session` actually reaches
    `_continue_dead_end_ai_fallback` on a completed session, rather than testing the
    routing logic itself twice — that's `TestADeadEndAiFallbackKeepsAnswering` and
    `TestEveryOtherTerminalPointIsUntouched`'s job."""

    async def test_a_completed_session_on_a_dead_end_node_is_answered_not_handed_off(
        self, monkeypatch: pytest.MonkeyPatch, stub_run_ai_fallback,  # noqa: ANN001
    ) -> None:
        flow = _flow(_dead_end_graph())
        session = _session(AI_NODE_ID)

        async def get_one(db, filters):  # noqa: ANN001
            return session

        async def update(db, record_id, data):  # noqa: ANN001
            for key, value in data.items():
                setattr(session, key, value)
            return session

        monkeypatch.setattr(engine_service.flow_session_crud, "get_one", get_one)
        monkeypatch.setattr(engine_service.flow_session_crud, "update", update)

        result = await engine_service.advance_flow_session(
            None, _key(), flow, "sess-tok", "another question", None,
        )

        assert result.type != engine_service.AI_HANDOFF
        assert result.text == SUMMARY
        assert stub_run_ai_fallback["count"] == 1

    async def test_a_completed_session_on_an_ordinary_end_is_still_handed_off(
        self, monkeypatch: pytest.MonkeyPatch, stub_run_ai_fallback,  # noqa: ANN001
    ) -> None:
        flow = _flow(_graph_ending_on_send_message())
        session = _session(OTHER_NODE_ID)

        async def get_one(db, filters):  # noqa: ANN001
            return session

        monkeypatch.setattr(engine_service.flow_session_crud, "get_one", get_one)

        result = await engine_service.advance_flow_session(
            None, _key(), flow, "sess-tok", "hello?", None,
        )

        assert result.type == engine_service.AI_HANDOFF
        assert stub_run_ai_fallback["count"] == 0


class TestARestartClearsTheRememberedAnswer:
    """
    A restart (`_session_needs_restart`) throws the session's whole position away and
    re-runs from Start — the same reason it already clears `node_results`: a dead-end
    AI Fallback's last answer belongs to the conversation being started over, not the
    new one starting in its place.
    """

    async def test_a_restarted_session_has_no_remembered_answer(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        flow = _flow(_dead_end_graph(), flow_id=FLOW_ID)
        stale_session = _session(AI_NODE_ID, flow_id=FLOW_ID + 1)  # a different flow_id forces a restart
        stale_session.dead_end_ai_context = {AI_NODE_ID: "an old, stale answer"}
        stale_session.node_results = {AI_NODE_ID: {"kind": "table", "columns": [], "rows": []}}

        async def get_one(db, filters):  # noqa: ANN001
            return stale_session

        monkeypatch.setattr(engine_service.flow_session_crud, "get_one", get_one)

        session = await engine_service._load_or_create_session(None, _key(), flow, "sess-tok")

        assert session.dead_end_ai_context == {}
        assert session.node_results == {}
