"""
Tests for the **Run graph** node: a flow step whose work is a whole Graph Designer graph,
and the only non-prompt node that can end a turn waiting for a reply.

The property the file is built around: **a graph that asks a question suspends the
conversation, and the visitor's next message answers it.** Everything else here is a
consequence of getting that one right.

* The question reaches the visitor **word for word**. An operator wrote it into the graph;
  a paraphrase asks a different question and makes the answer unmatchable.
* The run id is parked on the **session**, not held in memory, because the question goes
  out in one request and the answer arrives in another.
* An answer that does not fit — "maybe" to a yes/no — asks **again** with the reason. It is
  ordinary input, not a fault, and reporting it as a failure would tell a visitor the
  conversation is broken when they need only answer differently.
* A finished graph says **nothing** to the visitor and the flow hops on. A graph that read
  some rows is a step in a conversation, not a message in it.
* A failed graph takes the ``error`` port, or signs off. **Never a silent hop onward** — a
  flow carrying on as though a step succeeded is how a visitor gets told something untrue.

``graph_runner`` is stubbed, which is the one place a stub is right rather than convenient:
what these tests are about is how the *engine* reads the three outcomes, and the outcomes
themselves are asserted against a real graph in
``tests/unit/services/graph_designer/test_graph_runner.py``. Running a real graph here
would test that file's subject twice and this one's once.
"""

from __future__ import annotations

import pytest

from app.models.chatbot import ChatbotApiKey
from app.models.flow_builder import ChatbotFlowSession
from app.services.flow_builder import engine_service

GRAPH_NODE_ID = "graph_1"
NEXT_ID = "msg_1"
ERROR_ID = "msg_err"
GRAPH_UUID = "3f4a6b2c-1d5e-4a7b-8c9d-0e1f2a3b4c5d"
RUN_UUID = "aa11bb22-cc33-dd44-ee55-ff6677889900"
QUESTION = "Shall I include archived departments?"


class _Outcome:
    """A stand-in for ``graph_runner.GraphOutcome`` with only what the engine reads."""

    def __init__(self, kind, run_id="", question=None, reason="", total_rows=0):  # noqa: ANN001
        self.kind = kind
        self.run_id = run_id
        self.question = question
        self.reason = reason
        self.total_rows = total_rows

    @property
    def finished(self) -> bool:
        return self.kind == "finished"

    @property
    def asks(self) -> bool:
        return self.kind == "question"


def _graph(*, error_edge: bool = False, variable_name: str = "") -> dict:
    data = {"graph_id": GRAPH_UUID}

    if variable_name:
        data["variable_name"] = variable_name

    edges = [
        {"source": "start", "target": GRAPH_NODE_ID, "source_port": "default"},
        {"source": GRAPH_NODE_ID, "target": NEXT_ID, "source_port": "default"},
    ]

    if error_edge:
        edges.append(
            {"source": GRAPH_NODE_ID, "target": ERROR_ID, "source_port": "error"},
        )

    return {
        "nodes": [
            {"id": "start", "type": "start", "data": {}},
            {"id": GRAPH_NODE_ID, "type": "run_graph", "data": data},
            {"id": NEXT_ID, "type": "send_message",
             "data": {"message_text": "All done."}},
            {"id": ERROR_ID, "type": "send_message",
             "data": {"message_text": "That did not work."}},
        ],
        "edges": edges,
    }


def _session(node_id: str = GRAPH_NODE_ID, **kwargs) -> ChatbotFlowSession:  # noqa: ANN003
    session = ChatbotFlowSession()
    session.current_node_id = node_id
    session.variables = kwargs.pop("variables", {})
    session.status = "active"
    session.awaiting_graph_run = kwargs.pop("awaiting_graph_run", None)
    return session


def _key(user_id: int = 7) -> ChatbotApiKey:
    key = ChatbotApiKey()
    key.user_id = user_id
    return key


@pytest.fixture
def runner(monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    """
    A stub ``graph_runner``, recording what it was asked and returning what it is told to.

    Installed into ``sys.modules`` because the engine imports the module inside the
    function — a lazy import that exists to avoid a cycle, and which a plain
    ``monkeypatch.setattr`` on the engine therefore could not reach.
    """
    import sys
    import types

    calls: dict = {"run": [], "answer": []}
    module = types.ModuleType("app.services.graph_designer.graph_runner")

    async def run_graph(user_id, graph_uuid, inputs=None):  # noqa: ANN001, ANN202
        calls["run"].append((user_id, graph_uuid, dict(inputs or {})))
        return calls["next_run"]

    async def answer_graph_run(user_id, run_uuid, answer):  # noqa: ANN001, ANN202
        calls["answer"].append((user_id, run_uuid, answer))
        return calls["next_answer"]

    module.run_graph = run_graph
    module.answer_graph_run = answer_graph_run

    import app.services.graph_designer as package

    monkeypatch.setattr(package, "graph_runner", module, raising=False)
    monkeypatch.setitem(
        sys.modules, "app.services.graph_designer.graph_runner", module,
    )

    return calls


class TestAFinishedGraphIsAStepNotAMessage:
    async def test_it_says_nothing_and_the_flow_hops_on(self, runner) -> None:  # noqa: ANN001
        runner["next_run"] = _Outcome("finished", run_id=RUN_UUID, total_rows=12)
        session = _session()

        result = await engine_service._step_run_graph(
            None,
            _key(), _graph(), session, _graph()["nodes"][1],
        )

        assert result is None, "None keeps the hop loop going"
        assert session.current_node_id == NEXT_ID

    async def test_the_row_count_is_stored_when_a_variable_is_named(
        self, runner,
    ) -> None:  # noqa: ANN001
        """
        A count, not the rows: a flow variable is interpolated into message text and
        compared by If/Else, so a result set in one would produce a chat bubble of JSON.
        """
        runner["next_run"] = _Outcome("finished", run_id=RUN_UUID, total_rows=12)
        graph = _graph(variable_name="found")
        session = _session()

        await engine_service._step_run_graph(
            None,
            _key(), graph, session, graph["nodes"][1],
        )

        assert session.variables["found"] == "12"

    async def test_the_count_is_the_real_total_not_the_preview(
        self, runner,
    ) -> None:  # noqa: ANN001
        """
        ``total_rows`` and ``len(rows)`` differ whenever a result was larger than the
        preview. Telling a visitor "20" when there were 5,275 is the failure this
        application keeps writing tests against.
        """
        runner["next_run"] = _Outcome("finished", run_id=RUN_UUID, total_rows=5275)
        graph = _graph(variable_name="found")
        session = _session()

        await engine_service._step_run_graph(
            None,
            _key(), graph, session, graph["nodes"][1],
        )

        assert session.variables["found"] == "5275"

    async def test_nothing_is_stored_without_a_variable_name(self, runner) -> None:  # noqa: ANN001
        runner["next_run"] = _Outcome("finished", run_id=RUN_UUID, total_rows=3)
        session = _session()
        graph = _graph()

        await engine_service._step_run_graph(None, _key(), graph, session, graph["nodes"][1])

        assert session.variables == {}

    async def test_the_graph_runs_as_the_chatbots_owner_with_the_visitors_variables(
        self, runner,
    ) -> None:  # noqa: ANN001
        """
        The owner comes from the chatbot key, not from the graph row: a flow may only run
        a graph its own owner has, and the datasources its nodes read are that person's.
        The visitor's captured variables go in as the run's inputs, which is what lets a
        graph filter on something an earlier Ask-for-Input node collected.
        """
        runner["next_run"] = _Outcome("finished", run_id=RUN_UUID)
        graph = _graph()
        session = _session(variables={"department": "Sales"})

        await engine_service._step_run_graph(
            None,
            _key(user_id=42), graph, session, graph["nodes"][1],
        )

        assert runner["run"] == [(42, GRAPH_UUID, {"department": "Sales"})]


class TestAQuestionSuspendsTheConversation:
    async def test_the_question_reaches_the_visitor_word_for_word(
        self, runner,
    ) -> None:  # noqa: ANN001
        runner["next_run"] = _Outcome(
            "question", run_id=RUN_UUID, question={"prompt": QUESTION},
        )
        graph = _graph()
        session = _session()

        result = await engine_service._step_run_graph(
            None,
            _key(), graph, session, graph["nodes"][1],
        )

        assert result.type == "text_prompt"
        assert result.text == QUESTION, "not paraphrased, not decorated"

    async def test_the_session_stays_on_the_node_and_parks_the_run(
        self, runner,
    ) -> None:  # noqa: ANN001
        """
        Both halves matter. Staying on the node is what makes the answer belong to this
        step; parking the run id is what lets a later request find the paused run, since
        nothing about the pause can live in memory.
        """
        runner["next_run"] = _Outcome(
            "question", run_id=RUN_UUID, question={"prompt": QUESTION},
        )
        graph = _graph()
        session = _session()

        await engine_service._step_run_graph(None, _key(), graph, session, graph["nodes"][1])

        assert session.current_node_id == GRAPH_NODE_ID
        assert session.awaiting_graph_run == RUN_UUID
        assert session.status == "active", "waiting is not finished"

    async def test_a_question_with_no_text_still_asks_something(
        self, runner,
    ) -> None:  # noqa: ANN001
        """A blank chat bubble is not a question, and the widget would draw one."""
        runner["next_run"] = _Outcome("question", run_id=RUN_UUID, question={})
        graph = _graph()

        result = await engine_service._step_run_graph(
            None,
            _key(), graph, _session(), graph["nodes"][1],
        )

        assert result.text


class TestAnsweringTheQuestion:
    async def test_a_good_answer_finishes_the_graph_and_lets_the_flow_continue(
        self, runner,
    ) -> None:  # noqa: ANN001
        runner["next_answer"] = _Outcome("finished", run_id=RUN_UUID, total_rows=4)
        session = _session(awaiting_graph_run=RUN_UUID)

        result = await engine_service._answer_waiting_graph(
            None,
            _key(user_id=9), session, _graph(), "yes",
        )

        assert result is None, "None hands the turn to the ordinary hop loop"
        assert session.awaiting_graph_run is None, "no longer waiting"
        assert session.current_node_id == NEXT_ID
        assert runner["answer"] == [(9, RUN_UUID, "yes")]

    async def test_an_answer_that_does_not_fit_asks_again_with_the_reason(
        self, runner,
    ) -> None:  # noqa: ANN001
        """
        "maybe" to a yes/no. Ordinary input, so the question comes back with the
        validator's sentence in front of it — and the run stays parked, because the
        visitor can still answer it.
        """
        runner["next_answer"] = _Outcome(
            "question",
            run_id=RUN_UUID,
            question={"prompt": QUESTION},
            reason="Please answer yes or no.",
        )
        session = _session(awaiting_graph_run=RUN_UUID)

        result = await engine_service._answer_waiting_graph(
            None,
            _key(), session, _graph(), "maybe",
        )

        assert result.type == "text_prompt"
        assert "Please answer yes or no." in result.text
        assert QUESTION in result.text
        assert session.awaiting_graph_run == RUN_UUID, "still answerable"
        assert session.status == "active"

    async def test_a_second_question_is_asked_and_reparked(self, runner) -> None:  # noqa: ANN001
        """A graph may interrupt twice. Nothing special-cases it."""
        second_run = "bb22cc33-dd44-ee55-ff66-778899001122"
        runner["next_answer"] = _Outcome(
            "question", run_id=second_run, question={"prompt": "And which year?"},
        )
        session = _session(awaiting_graph_run=RUN_UUID)

        result = await engine_service._answer_waiting_graph(
            None,
            _key(), session, _graph(), "yes",
        )

        assert result.text == "And which year?"
        assert session.awaiting_graph_run == second_run

    async def test_a_failure_stops_waiting_rather_than_waiting_forever(
        self, runner,
    ) -> None:  # noqa: ANN001
        runner["next_answer"] = _Outcome("failed", reason="The run was lost.")
        session = _session(awaiting_graph_run=RUN_UUID)

        result = await engine_service._answer_waiting_graph(
            None,
            _key(), session, _graph(), "yes",
        )

        assert session.awaiting_graph_run is None
        assert result.type == "text"
        assert session.status == "completed"

    async def test_a_failure_takes_the_error_port_when_one_is_drawn(
        self, runner,
    ) -> None:  # noqa: ANN001
        runner["next_answer"] = _Outcome("failed", reason="The run was lost.")
        session = _session(awaiting_graph_run=RUN_UUID)

        result = await engine_service._answer_waiting_graph(
            None,
            _key(), session, _graph(error_edge=True), "yes",
        )

        assert result is None
        assert session.current_node_id == ERROR_ID
        assert session.awaiting_graph_run is None


class TestAFailedGraphIsNeverASilentHop:
    async def test_it_takes_the_error_port_when_one_is_drawn(self, runner) -> None:  # noqa: ANN001
        runner["next_run"] = _Outcome("failed", reason="A node could not run.")
        graph = _graph(error_edge=True)
        session = _session()

        result = await engine_service._step_run_graph(
            None,
            _key(), graph, session, graph["nodes"][1],
        )

        assert result is None
        assert session.current_node_id == ERROR_ID

    async def test_without_one_it_signs_off_rather_than_carrying_on(
        self, runner,
    ) -> None:  # noqa: ANN001
        """
        The important half. Hopping to ``default`` on a failure would let the flow say
        "All done." about work that did not happen.
        """
        runner["next_run"] = _Outcome("failed", reason="A node could not run.")
        graph = _graph()
        session = _session()

        result = await engine_service._step_run_graph(
            None,
            _key(), graph, session, graph["nodes"][1],
        )

        assert result.type == "text"
        assert session.current_node_id != NEXT_ID
        assert session.status == "completed"

    async def test_a_still_running_graph_is_not_treated_as_finished(
        self, runner,
    ) -> None:  # noqa: ANN001
        """
        ``running`` is neither finished nor asking. Reading it as finished would store a
        count of zero and tell the visitor there was nothing to report.
        """
        runner["next_run"] = _Outcome("running", run_id=RUN_UUID)
        graph = _graph(variable_name="found")
        session = _session()

        result = await engine_service._step_run_graph(
            None,
            _key(), graph, session, graph["nodes"][1],
        )

        assert result is not None, "the turn ends rather than continuing"
        assert "found" not in session.variables


class TestANodeNobodyFinishedConfiguring:
    async def test_a_run_graph_node_with_no_graph_says_so(self, runner) -> None:  # noqa: ANN001
        """
        Said out loud rather than skipped: a flow that quietly steps over a step is a flow
        whose author cannot tell it is broken.
        """
        graph = _graph()
        graph["nodes"][1]["data"] = {}
        session = _session()

        result = await engine_service._step_run_graph(
            None,
            _key(), graph, session, graph["nodes"][1],
        )

        assert result.type == "text"
        assert session.status == "completed"
        assert runner["run"] == [], "nothing was run"


class TestTheNodeTypeIsPartOfTheVocabulary:
    def test_the_flow_validator_accepts_it(self) -> None:
        """
        Otherwise the canvas could draw it and the save would refuse — which is how a node
        type ends up half-added.
        """
        from app.services.flow_builder import flow_service

        assert "run_graph" in flow_service._VALID_NODE_TYPES

    def test_it_is_not_one_of_the_input_waiting_types(self) -> None:
        """
        It ends a turn waiting, but not through ``_AWAITING_NODE_TYPES``: that set is
        consumed by ``_deliver_reply_to_waiting_node``, which would try to store the answer
        as a flow variable and hop on, never handing it to the paused graph.
        """
        assert "run_graph" not in engine_service._AWAITING_NODE_TYPES
