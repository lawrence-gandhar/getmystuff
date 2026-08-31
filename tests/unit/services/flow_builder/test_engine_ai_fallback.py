"""
Tests for what an **AI Fallback** node leaves behind: the answer, kept in a variable.

The node has always said its answer to the visitor and then forgotten it, which made one
ordinary flow impossible to draw — *"email me the data"* → AI Fallback → Send Email had
nothing for the email to bind to. Naming a variable on the block now keeps the answer, and
these tests are built around what that answer has to be:

* **The whole answer, not just the narrative.** Somebody who asked to be emailed the data
  meant the figures. A variable holding only the summary mails them a sentence about a
  table they never received.
* **Plain text, in the order the widget draws it.** The email's HTML body escapes every
  value it substitutes (``rendering.py``), so markup smuggled through a variable arrives as
  visible tag soup — and an email that disagreed with the chat bubble about the order of
  the same answer is a support ticket.
* **Nothing stored when the AI could not answer.** The variable stays *absent* rather than
  holding the failure sentence, so the email template's own default fills in or the send is
  refused. Storing it would mail a customer an internal error as though it were the answer.
* **The visitor still sees the answer.** Keeping a copy is not a mode; the chat bubble is
  unchanged whether a variable is named or not.

DB-free, like the neighbouring engine tests: ``ai_fallback_service`` is stubbed because the
subject here is what the *engine* does with an answer, and how the answer itself is
produced is asserted against the real thing in the ai_analytics tests.
"""

from __future__ import annotations

import pytest
from litestar.exceptions import HTTPException

from app.models.chatbot import ChatbotApiKey
from app.models.flow_builder import ChatbotFlowSession
from app.services.ai_analytics.ai_analytics_service import AnalyticsResult, AnalyticsTable
from app.services.flow_builder import engine_service

AI_NODE_ID = "ai_1"
NEXT_ID = "email_1"
FLOW_ID = 3
SUMMARY = "You had 3 orders last week."


def _graph(*, variable_name: str = "") -> dict:
    data: dict = {"context_source": "datasource", "llm_mode": "in_built"}

    if variable_name:
        data["variable_name"] = variable_name

    return {
        "nodes": [
            {"id": "start", "type": "start", "data": {}},
            {"id": AI_NODE_ID, "type": "ai_fallback", "data": data},
            {"id": NEXT_ID, "type": "send_message", "data": {"message_text": "Done."}},
        ],
        "edges": [
            {"source": "start", "target": AI_NODE_ID, "source_port": "default"},
            {"source": AI_NODE_ID, "target": NEXT_ID, "source_port": "default"},
        ],
    }


def _dead_end_graph(*, variable_name: str = "") -> dict:
    """An AI Fallback node with no outgoing edge at all — a deliberate dead end."""
    data: dict = {"context_source": "datasource", "llm_mode": "in_built"}

    if variable_name:
        data["variable_name"] = variable_name

    return {
        "nodes": [
            {"id": "start", "type": "start", "data": {}},
            {"id": AI_NODE_ID, "type": "ai_fallback", "data": data},
        ],
        "edges": [
            {"source": "start", "target": AI_NODE_ID, "source_port": "default"},
        ],
    }


def _node(graph: dict) -> dict:
    return graph["nodes"][1]


def _session() -> ChatbotFlowSession:
    session = ChatbotFlowSession()
    session.id = 11
    session.current_node_id = AI_NODE_ID
    session.variables = {}
    session.status = "active"
    return session


def _key(user_id: int = 7) -> ChatbotApiKey:
    key = ChatbotApiKey()
    key.user_id = user_id
    return key


@pytest.fixture
def fallback(monkeypatch: pytest.MonkeyPatch) -> dict:
    """
    A stub ``run_ai_fallback`` returning whatever ``calls["result"]`` holds, or raising it
    when that is an exception.
    """
    calls: dict = {"result": AnalyticsResult(summary=SUMMARY), "asked": [], "previous_answers": []}

    async def run_ai_fallback(  # noqa: ANN202
        db, chatbot_key, flow_id, node_id, node_data, message, from_selection=False,  # noqa: ANN001
        session_variables=None, previous_answer=None,  # noqa: ANN001
    ):
        calls["asked"].append((node_id, message, from_selection))
        calls["previous_answers"].append(previous_answer)
        result = calls["result"]
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(
        engine_service.ai_fallback_service, "run_ai_fallback", run_ai_fallback,
    )
    return calls


async def _run(  # noqa: ANN202
    graph: dict,
    session: ChatbotFlowSession,
    message: str = "how many orders?",
    from_selection: bool = False,
):
    return await engine_service._step_ai_fallback(
        None, _key(), FLOW_ID, graph, session, _node(graph), message,
        from_selection=from_selection,
    )


class TestTheAnswerIsKept:
    async def test_it_is_stored_under_the_nodes_variable_name(self, fallback) -> None:  # noqa: ANN001
        graph = _graph(variable_name="ai_answer")
        session = _session()

        await _run(graph, session)

        assert session.variables["ai_answer"] == SUMMARY

    async def test_a_node_with_no_variable_name_stores_nothing(self, fallback) -> None:  # noqa: ANN001
        session = _session()

        await _run(_graph(), session)

        assert session.variables == {}

    async def test_the_visitor_still_gets_the_answer(self, fallback) -> None:  # noqa: ANN001
        """Keeping a copy is not a mode — the chat bubble is the same either way."""
        fallback["result"] = AnalyticsResult(summary=SUMMARY, insights=["Up 20%"])
        graph = _graph(variable_name="ai_answer")

        result = await _run(graph, _session())

        assert result.type == "text"
        assert result.text == SUMMARY
        assert result.insights == ["Up 20%"]

    async def test_the_flow_still_hops_on(self, fallback) -> None:  # noqa: ANN001
        session = _session()

        await _run(_graph(variable_name="ai_answer"), session)

        assert session.current_node_id == NEXT_ID, (
            "the turn ends here, but the next node is where the next message resumes — "
            "which is what lets an Email node a step later read the variable"
        )


class TestTheNodeIsToldHowTheQuestionArrived:
    """
    A button click and a typed sentence reach the node as the same string, so the flag is
    the only thing that can tell them apart — and it changes what a knowledge base is
    searched for. See ``ai_fallback_service._retrieval_query``.
    """

    async def test_a_selection_turn_is_passed_through(self, fallback) -> None:  # noqa: ANN001
        await _run(_graph(), _session(), "Email me the data", from_selection=True)

        assert fallback["asked"] == [(AI_NODE_ID, "Email me the data", True)]

    async def test_a_typed_turn_is_passed_through(self, fallback) -> None:  # noqa: ANN001
        await _run(_graph(), _session(), "what is the warranty?")

        assert fallback["asked"] == [(AI_NODE_ID, "what is the warranty?", False)]


class TestWhatTheStoredAnswerContains:
    async def test_insights_and_table_are_kept_not_just_the_summary(
        self, fallback,
    ) -> None:  # noqa: ANN001
        """
        The whole answer. A visitor who asked to be emailed the data meant the figures, and
        a variable holding only the narrative mails them a sentence about a table they
        never received.
        """
        fallback["result"] = AnalyticsResult(
            summary=SUMMARY,
            insights=["Up 20% on the week", "Tuesday was the peak"],
            table=AnalyticsTable(columns=["day", "orders"], rows=[["Mon", "1"], ["Tue", "2"]]),
        )
        session = _session()

        await _run(_graph(variable_name="ai_answer"), session)

        assert session.variables["ai_answer"] == (
            "You had 3 orders last week.\n"
            "- Up 20% on the week\n"
            "- Tuesday was the peak\n"
            "day | orders\n"
            "Mon | 1\n"
            "Tue | 2"
        )

    async def test_a_long_table_is_capped_and_says_so(self, fallback) -> None:  # noqa: ANN001
        """
        Truncated, never silently: a stored answer that dropped rows without saying so is
        the same failure as telling somebody "20" when there were 5,275.
        """
        rows = [[str(n), "x"] for n in range(engine_service._MAX_STORED_TABLE_ROWS + 5)]
        fallback["result"] = AnalyticsResult(
            summary="", table=AnalyticsTable(columns=["n", "v"], rows=rows),
        )
        session = _session()

        await _run(_graph(variable_name="ai_answer"), session)

        stored = session.variables["ai_answer"].splitlines()
        assert len(stored) == engine_service._MAX_STORED_TABLE_ROWS + 2, "header + rows + note"
        assert stored[-1] == "(+5 more rows)"

    async def test_an_empty_answer_is_stored_as_empty_text(self, fallback) -> None:  # noqa: ANN001
        """So an If/Else `not_empty` on the variable is false, which is the truth."""
        fallback["result"] = AnalyticsResult(summary="   ")
        session = _session()

        await _run(_graph(variable_name="ai_answer"), session)

        assert session.variables["ai_answer"] == ""


class TestAFailedAnswerIsNotStored:
    async def test_the_variable_stays_absent(self, fallback) -> None:  # noqa: ANN001
        """
        Absent, not set to the error sentence: a later Email node then falls back to the
        template's declared default (or refuses a required variable) rather than mailing a
        customer an internal failure as though it were the answer.
        """
        fallback["result"] = HTTPException(
            status_code=400, detail="No datasource is attached to this chatbot.",
        )
        session = _session()

        await _run(_graph(variable_name="ai_answer"), session)

        assert "ai_answer" not in (session.variables or {})

    async def test_the_visitor_is_told_what_went_wrong(self, fallback) -> None:  # noqa: ANN001
        fallback["result"] = HTTPException(
            status_code=400, detail="No datasource is attached to this chatbot.",
        )

        result = await _run(_graph(variable_name="ai_answer"), _session())

        assert result.type == "text"
        assert result.text == "No datasource is attached to this chatbot."


class TestDeadEndContinuation:
    """
    An AI Fallback node with no outgoing edge is a deliberate dead end — the operator
    drew it that way on purpose. What it leaves behind for the *next* time this same
    node runs (a visitor's follow-up message; see ``engine_service
    ._continue_dead_end_ai_fallback``) is a rolling one-answer memory in
    ``session.dead_end_ai_context``, gated strictly on there being no edge so a
    normally-connected node's behavior never changes.
    """

    async def test_the_first_dead_end_turn_has_no_previous_answer(self, fallback) -> None:  # noqa: ANN001
        session = _session()

        await _run(_dead_end_graph(), session)

        assert fallback["previous_answers"] == [None]

    async def test_the_first_dead_end_turn_records_its_answer(self, fallback) -> None:  # noqa: ANN001
        session = _session()

        await _run(_dead_end_graph(), session)

        assert session.dead_end_ai_context[AI_NODE_ID] == engine_service._ai_answer_text(
            fallback["result"],
        )

    async def test_a_second_dead_end_turn_is_given_the_first_answer(self, fallback) -> None:  # noqa: ANN001
        session = _session()
        graph = _dead_end_graph()

        await _run(graph, session, "how many orders?")
        first_answer = session.dead_end_ai_context[AI_NODE_ID]

        fallback["result"] = AnalyticsResult(summary="A different answer.")
        await _run(graph, session, "and last month?")

        assert fallback["previous_answers"] == [None, first_answer]

    async def test_the_remembered_answer_is_replaced_not_accumulated(self, fallback) -> None:  # noqa: ANN001
        session = _session()
        graph = _dead_end_graph()

        await _run(graph, session, "how many orders?")
        fallback["result"] = AnalyticsResult(summary="A different answer.")
        await _run(graph, session, "and last month?")

        assert session.dead_end_ai_context[AI_NODE_ID] == "A different answer."

    async def test_a_connected_node_never_reads_or_writes_dead_end_context(
        self, fallback,
    ) -> None:  # noqa: ANN001
        """Proves the ``edge is None`` gate is airtight: even with stale context
        already sitting under this node's id, a connected node ignores it."""
        session = _session()
        session.dead_end_ai_context = {AI_NODE_ID: "stale answer from a different node"}

        await _run(_graph(), session)

        assert fallback["previous_answers"] == [None]
        assert session.dead_end_ai_context == {AI_NODE_ID: "stale answer from a different node"}

    async def test_a_failed_dead_end_turn_does_not_record_anything(self, fallback) -> None:  # noqa: ANN001
        fallback["result"] = HTTPException(status_code=400, detail="No datasource attached.")
        session = _session()

        await _run(_dead_end_graph(), session)

        assert session.dead_end_ai_context in ({}, None)
