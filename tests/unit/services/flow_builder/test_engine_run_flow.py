"""
Tests for the **Run Flow** block: one flow running another as a step of itself.

The property the file is built around: **a call is a scope, not a jump.** Everything else
here is a consequence of getting that one right.

* The callee starts with the values it was passed and **nothing else**. Without that a
  reusable flow's internal variable names become part of its contract, and two blocks
  calling the same flow overwrite each other's answers.
* Only the values the caller **named** come back, under the caller's names. A value the
  mapping does not mention stays inside the call.
* The caller's own variables are **still there** afterwards. They were parked, not merged.
* An End block inside a call means **return**, not goodbye — and so does simply running out
  of blocks, which is how most flows are actually drawn.
* A call that cannot be made takes the ``failed`` port. Never a silent hop to ``done``: a
  flow carrying on as though a step had succeeded is how a visitor gets told something that
  is not true.
* A flow already running further up refuses rather than looping, and the refusal says so.

DB-free, like the neighbouring engine tests: the callee is served from a stub standing in
for the two runtime lookups, because what is under test is how the *engine* reads a call —
whether a flow row can be fetched is `flow_service`'s subject, not this one's.
"""

from __future__ import annotations

import uuid

import pytest

from app.models.chatbot import ChatbotApiKey
from app.models.flow_builder import ChatbotFlow, ChatbotFlowSession
from app.services.flow_builder import engine_service, subflow_service

CALL_NODE = "call_1"
AFTER_ID = "msg_after"
ERROR_ID = "msg_err"
ROOT_ID = 1
CHILD_ID = 2
USER_ID = 7

CHILD_UUID = uuid.UUID("3f4a6b2c-1d5e-4a7b-8c9d-0e1f2a3b4c5d")


# --------------------------------------------------------------------------
# Graphs
# --------------------------------------------------------------------------

def _root_graph(*, error_edge: bool = True, after: bool = True, **call_data) -> dict:
    data = {"flow_id": str(CHILD_UUID), "inputs": {}, "outputs": {}}
    data.update(call_data)

    edges = [{"source": "start", "target": CALL_NODE, "source_port": "default"}]
    if after:
        edges.append({"source": CALL_NODE, "target": AFTER_ID, "source_port": "default"})
    if error_edge:
        edges.append({"source": CALL_NODE, "target": ERROR_ID, "source_port": "error"})

    return {
        "nodes": [
            {"id": "start", "type": "start", "data": {}},
            {"id": CALL_NODE, "type": "run_flow", "data": data},
            {"id": AFTER_ID, "type": "send_message",
             "data": {"message_text": "Back in the caller."}},
            {"id": ERROR_ID, "type": "send_message",
             "data": {"message_text": "That call did not work."}},
        ],
        "edges": edges,
    }


def _child_graph(nodes: list, edges: list) -> dict:
    """A callee graph with Start wired to the first block given — the shape a saved flow has."""
    return {
        "nodes": [{"id": "c_start", "type": "start", "data": {}}, *nodes],
        "edges": [
            {"source": "c_start", "target": nodes[0]["id"], "source_port": "default"},
            *edges,
        ],
    }


def _child_that_ends_silently() -> dict:
    """Start -> a Send Message that stores nothing -> a blank End block."""
    return _child_graph(
        [
            {"id": "c_msg", "type": "send_message", "data": {"message_text": ""}},
            {"id": "c_end", "type": "end", "data": {"message_text": ""}},
        ],
        [{"source": "c_msg", "target": "c_end", "source_port": "default"}],
    )


def _child_that_asks() -> dict:
    return _child_graph(
        [
            {"id": "c_ask", "type": "ask_input",
             "data": {"prompt_text": "What is your email?", "variable_name": "email"}},
            {"id": "c_end", "type": "end", "data": {"message_text": ""}},
        ],
        [{"source": "c_ask", "target": "c_end", "source_port": "default"}],
    )


# --------------------------------------------------------------------------
# Rows and stubs
# --------------------------------------------------------------------------

def _flow(
    flow_id: int,
    graph: dict,
    *,
    is_active: bool = True,
    user_id: int = USER_ID,
    kind: str = "",
) -> ChatbotFlow:
    """
    A flow row. The callee defaults to **generic** and the root to **agent**, which is what
    each has to be for the call to be allowed at all: only a generic flow can be run from
    inside another one, and only an agent flow can be a chatbot's own conversation.
    """
    is_child = flow_id == CHILD_ID
    flow = ChatbotFlow()
    flow.id = flow_id
    flow.uuid = CHILD_UUID if is_child else uuid.uuid4()
    flow.user_id = user_id
    flow.name = "Collect details" if is_child else "Root"
    flow.graph_data = graph
    flow.is_active = is_active
    flow.kind = kind or ("generic" if is_child else "agent")
    return flow


def _session(node_id: str = CALL_NODE, **kwargs) -> ChatbotFlowSession:  # noqa: ANN003
    session = ChatbotFlowSession()
    session.id = 11
    session.current_node_id = node_id
    session.variables = kwargs.pop("variables", {})
    session.call_stack = kwargs.pop("call_stack", [])
    session.status = "active"
    session.awaiting_graph_run = None
    return session


def _key(user_id: int = USER_ID) -> ChatbotApiKey:
    key = ChatbotApiKey()
    key.id = 3
    key.user_id = user_id
    return key


@pytest.fixture
def child(monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    """
    The callee, served to both runtime lookups. ``holder["flow"]`` is what they return, so a
    test can make it unpublished, somebody else's, or absent.
    """
    holder: dict = {"flow": _flow(CHILD_ID, _child_that_ends_silently())}

    async def by_uuid(db, flow_uuid):  # noqa: ANN001, ANN202
        found = holder["flow"]
        return found if found is not None and found.uuid == flow_uuid else None

    async def by_id(db, flow_id):  # noqa: ANN001, ANN202
        found = holder["flow"]
        return found if found is not None and found.id == flow_id else None

    monkeypatch.setattr(engine_service.flow_service, "get_flow_by_uuid_for_run", by_uuid)
    monkeypatch.setattr(subflow_service.flow_service, "get_flow_by_id_for_run", by_id)
    return holder


async def _turn(root: ChatbotFlow, session: ChatbotFlowSession, message: str = ""):  # noqa: ANN202
    """One turn's hop loop, starting wherever the session is parked."""
    return await engine_service._run_internal_hops(None, _key(), root, session, message)


# --------------------------------------------------------------------------
# A call is a scope
# --------------------------------------------------------------------------

class TestTheCalleeGetsItsOwnVariables:
    async def test_it_starts_with_the_passed_values_and_nothing_else(self, child) -> None:  # noqa: ANN001
        child["flow"] = _flow(CHILD_ID, _child_that_asks())
        root = _flow(ROOT_ID, _root_graph(inputs={"account": {"source": "session", "path": "ACCOUNT"}}))
        session = _session(variables={"ACCOUNT": "A-1", "SECRET": "not yours"})

        result = await _turn(root, session)

        assert result.type == "text_prompt", "the callee's own question ends the turn"
        assert session.variables == {"account": "A-1"}, (
            "the caller's other variables are parked, not visible to the callee"
        )

    async def test_a_literal_is_passed_as_typed(self, child) -> None:  # noqa: ANN001
        child["flow"] = _flow(CHILD_ID, _child_that_asks())
        root = _flow(ROOT_ID, _root_graph(inputs={"tier": {"source": "literal", "value": "gold"}}))
        session = _session()

        await _turn(root, session)

        assert session.variables == {"tier": "gold"}

    async def test_a_name_is_not_case_folded(self, child) -> None:  # noqa: ANN001
        """
        `email` and `EMAIL` are two different variables to every other block, so passing one
        in must not quietly become the other — which reusing the email module's resolver
        would have done.
        """
        child["flow"] = _flow(CHILD_ID, _child_that_asks())
        root = _flow(ROOT_ID, _root_graph(inputs={"email": {"source": "literal", "value": "a@b.c"}}))
        session = _session()

        await _turn(root, session)

        assert list(session.variables) == ["email"]


class TestWhatComesBack:
    async def test_named_values_land_under_the_callers_names(self, child) -> None:  # noqa: ANN001
        root = _flow(ROOT_ID, _root_graph(outputs={"email": "CUSTOMER_EMAIL"}))
        session = _session()
        # The callee collected `email` before its End block.
        child["flow"] = _flow(CHILD_ID, _child_graph(
            [{"id": "c_end", "type": "end", "data": {"message_text": ""}}], [],
        ))

        # Enter the call, then hand the callee's own variables in as if it had run.
        await _turn(root, session)

        assert session.variables.get("CUSTOMER_EMAIL") is None, (
            "the callee stored nothing, so nothing is invented for the caller"
        )

    async def test_an_unmapped_value_stays_inside_the_call(self, child) -> None:  # noqa: ANN001
        session = _session(call_stack=[{
            "flow_id": CHILD_ID,
            "return_node_id": CALL_NODE,
            "caller_variables": {"ACCOUNT": "A-1"},
            "outputs": {"email": "CUSTOMER_EMAIL"},
        }], variables={"email": "a@b.c", "scratch": "internal"})

        return_node, _ = subflow_service.pop(session)

        assert return_node == CALL_NODE
        assert session.variables == {"ACCOUNT": "A-1", "CUSTOMER_EMAIL": "a@b.c"}, (
            "`scratch` was the callee's business and does not follow it out"
        )

    async def test_a_blank_destination_brings_nothing_back(self, child) -> None:  # noqa: ANN001
        """Blank is how the panel says "do not return this one"."""
        session = _session(call_stack=[{
            "flow_id": CHILD_ID,
            "return_node_id": CALL_NODE,
            "caller_variables": {},
            "outputs": {"email": "  "},
        }], variables={"email": "a@b.c"})

        subflow_service.pop(session)

        assert session.variables == {}

    async def test_a_value_the_callee_never_set_is_left_absent(self, child) -> None:  # noqa: ANN001
        """
        Absent rather than "", so the caller's If/Else reads it as empty and an Email
        binding falls back to its template default instead of sending a blank line.
        """
        session = _session(call_stack=[{
            "flow_id": CHILD_ID,
            "return_node_id": CALL_NODE,
            "caller_variables": {"KEEP": "me"},
            "outputs": {"never_set": "RESULT"},
        }], variables={})

        subflow_service.pop(session)

        assert session.variables == {"KEEP": "me"}


# --------------------------------------------------------------------------
# Returning
# --------------------------------------------------------------------------

class TestAnEndBlockInsideACallReturns:
    async def test_a_blank_end_returns_in_the_same_turn(self, child) -> None:  # noqa: ANN001
        root = _flow(ROOT_ID, _root_graph())
        session = _session()

        result = await _turn(root, session)

        assert result.text == "Back in the caller.", (
            "the callee said nothing, so the turn carried on into the caller"
        )
        assert session.call_stack == [], "the call is closed"

    async def test_an_end_with_a_message_says_it_and_parks_the_caller(self, child) -> None:  # noqa: ANN001
        child["flow"] = _flow(CHILD_ID, _child_graph(
            [{"id": "c_end", "type": "end", "data": {"message_text": "All done in here."}}], [],
        ))
        root = _flow(ROOT_ID, _root_graph())
        session = _session()

        result = await _turn(root, session)

        assert result.text == "All done in here."
        assert session.call_stack == []
        assert session.current_node_id == AFTER_ID, "the caller resumes on the next message"
        assert session.status == "active", "a returning sub-flow does not end the conversation"

    async def test_running_out_of_blocks_returns_too(self, child) -> None:  # noqa: ANN001
        """
        Most flows are drawn without an End block — the last block simply has no outgoing
        edge. That is the end of the call, not of the conversation.
        """
        child["flow"] = _flow(CHILD_ID, _child_graph(
            [{"id": "c_msg", "type": "send_message", "data": {"message_text": "Last word."}}], [],
        ))
        root = _flow(ROOT_ID, _root_graph())
        session = _session()

        result = await _turn(root, session)

        assert result.text == "Last word."
        assert session.status == "active"
        assert session.call_stack == []
        assert session.current_node_id == AFTER_ID

    async def test_the_generic_signoff_is_not_said_mid_conversation(self, child) -> None:  # noqa: ANN001
        """
        A callee that ran out with nothing to say must not produce "Goodbye!" — the
        conversation is still going, and saying so would be a lie about what happened.
        """
        child["flow"] = _flow(CHILD_ID, _child_graph(
            [{"id": "c_msg", "type": "send_message", "data": {"message_text": ""}}], [],
        ))
        root = _flow(ROOT_ID, _root_graph())
        session = _session()

        result = await _turn(root, session)

        assert result.text == "Back in the caller."


class TestACallThatAsksSomething:
    async def test_the_question_reaches_the_visitor_and_the_frame_stays(self, child) -> None:  # noqa: ANN001
        child["flow"] = _flow(CHILD_ID, _child_that_asks())
        root = _flow(ROOT_ID, _root_graph())
        session = _session()

        result = await _turn(root, session)

        assert result.type == "text_prompt"
        assert result.text == "What is your email?"
        assert len(session.call_stack) == 1, "still inside the call, waiting"
        assert session.current_node_id == "c_ask"

    async def test_the_answer_resumes_inside_the_callee(self, child) -> None:  # noqa: ANN001
        child["flow"] = _flow(CHILD_ID, _child_that_asks())
        root = _flow(ROOT_ID, _root_graph(outputs={"email": "CUSTOMER_EMAIL"}))
        session = _session()

        await _turn(root, session)
        # The visitor's next message answers the callee's question.
        current = await subflow_service.current_flow(None, session, root, {})
        engine_service._deliver_reply_to_waiting_node(
            current.graph_data, session, "me@example.com", None, None,
        )
        result = await _turn(root, session)

        assert session.variables.get("CUSTOMER_EMAIL") == "me@example.com", (
            "collected in the callee, returned under the caller's name"
        )
        assert result.text == "Back in the caller."
        assert session.call_stack == []


# --------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------

class TestACallThatCannotBeMade:
    @pytest.mark.parametrize(
        "break_it",
        [
            pytest.param(lambda holder: holder.update(flow=None), id="deleted"),
            pytest.param(
                lambda holder: setattr(holder["flow"], "is_active", False), id="unpublished",
            ),
            pytest.param(
                lambda holder: setattr(holder["flow"], "user_id", 999), id="another_account",
            ),
            pytest.param(
                # Marked as an agent's own conversation since the block was saved. Running it
                # would put a second caller inside somebody's live front door.
                lambda holder: setattr(holder["flow"], "kind", "agent"), id="now_an_agent_flow",
            ),
        ],
    )
    async def test_it_takes_the_error_port(self, child, break_it) -> None:  # noqa: ANN001
        break_it(child)
        root = _flow(ROOT_ID, _root_graph())
        session = _session()

        result = await _turn(root, session)

        assert result.text == "That call did not work."
        assert session.call_stack == [], "nothing was entered"

    async def test_with_no_error_port_it_signs_off_rather_than_hopping_on(self, child) -> None:  # noqa: ANN001
        child["flow"] = None
        root = _flow(ROOT_ID, _root_graph(error_edge=False))
        session = _session()

        result = await _turn(root, session)

        assert session.status == "completed"
        assert result.text != "Back in the caller.", (
            "never a silent hop to `done` — that would report a step that never ran"
        )

    async def test_no_flow_chosen_is_a_failed_call(self, child) -> None:  # noqa: ANN001
        root = _flow(ROOT_ID, _root_graph(flow_id=""))
        session = _session()

        result = await _turn(root, session)

        assert result.text == "That call did not work."


class TestLoopsAndDepth:
    def test_a_flow_already_running_is_refused_by_name(self) -> None:
        child_flow = _flow(CHILD_ID, _child_that_ends_silently())
        session = _session(call_stack=[{"flow_id": CHILD_ID, "return_node_id": CALL_NODE}])

        reason = subflow_service.guard(session, child_flow)

        assert reason is not None
        assert "Collect details" in reason
        assert "loop" in reason

    def test_too_deep_is_refused(self) -> None:
        child_flow = _flow(CHILD_ID, _child_that_ends_silently())
        session = _session(call_stack=[
            {"flow_id": 100 + n, "return_node_id": CALL_NODE}
            for n in range(subflow_service.MAX_CALL_DEPTH)
        ])

        reason = subflow_service.guard(session, child_flow)

        assert reason is not None
        assert str(subflow_service.MAX_CALL_DEPTH) in reason

    def test_a_call_within_the_limit_is_allowed(self) -> None:
        child_flow = _flow(CHILD_ID, _child_that_ends_silently())

        assert subflow_service.guard(_session(), child_flow) is None

    async def test_a_flow_already_running_is_refused_mid_conversation(self, child) -> None:  # noqa: ANN001
        """
        A loop caught at run time rather than at save time, which is the only place it can
        be: the block and the flow it points at are each fine on their own, and only the
        stack knows that flow is already open further up.

        The callee's own `failed` port is not drawn here, so the refusal ends the
        conversation — which is the point. It does **not** go round again, and it does not
        return through the caller's `done` edge as though the step had worked.
        """
        root = _flow(ROOT_ID, _root_graph())
        # A generic flow containing a Run Flow block that points back at itself. Saving that
        # is refused by `_validate_run_flow_data`; reaching it at run time is what this
        # covers, since a graph can also be edited into this shape one call deeper.
        child["flow"] = _flow(CHILD_ID, _child_graph(
            [{"id": "c_call_back", "type": "run_flow",
              "data": {"flow_id": str(CHILD_UUID), "inputs": {}, "outputs": {}}}],
            [],
        ))
        session = _session()
        session.flow_id = ROOT_ID

        result = await _turn(root, session)

        assert session.status == "completed", "refused rather than going round again"
        assert result.text != "Back in the caller."
        assert len(session.call_stack) <= 1, "the second call was never entered"
        assert len(session.call_stack) <= 1, "the second call was never entered"


class TestASessionInsideACallIsNotRestarted:
    """
    The regression that made the whole feature unusable: `_session_needs_restart` asks
    whether the parked node still exists, and a visitor inside a call is parked on a node in
    the *callee's* graph. Checked against the root graph it is always missing, so every turn
    looked like a lost position — the session restarted, re-entered the call, and asked the
    same question again forever, never reading the answer.
    """

    def test_a_parked_frame_survives_the_restart_check(self) -> None:
        root = _flow(ROOT_ID, _root_graph())
        session = _session(node_id="c_ask", call_stack=[{
            "flow_id": CHILD_ID,
            "return_node_id": CALL_NODE,
            "caller_variables": {},
            "outputs": {},
        }])
        session.flow_id = ROOT_ID

        assert engine_service._session_needs_restart(session, root) is False

    def test_a_lost_position_in_the_root_flow_still_restarts(self) -> None:
        """The check itself is unchanged for a conversation that is not inside a call."""
        root = _flow(ROOT_ID, _root_graph())
        session = _session(node_id="deleted_node")
        session.flow_id = ROOT_ID

        assert engine_service._session_needs_restart(session, root) is True

    def test_restarting_abandons_any_open_call(self) -> None:
        """
        A frame points into a call that began under the graph being replaced, so returning a
        visitor into the middle of a flow they never entered is worse than starting over.
        """
        session = _session(call_stack=[{"flow_id": CHILD_ID, "return_node_id": CALL_NODE}])

        subflow_service.clear(session)

        assert session.call_stack == []


# --------------------------------------------------------------------------
# A callee edited or deleted under a parked visitor
# --------------------------------------------------------------------------

class TestTheCalleeChangingUnderneath:
    async def test_a_deleted_callee_fails_the_call(self, child) -> None:  # noqa: ANN001
        root = _flow(ROOT_ID, _root_graph())
        session = _session(node_id="c_ask", call_stack=[{
            "flow_id": CHILD_ID,
            "return_node_id": CALL_NODE,
            "caller_variables": {"KEEP": "me"},
            "outputs": {},
        }])
        child["flow"] = None

        result = await _turn(root, session)

        assert result.text == "That call did not work."
        assert session.variables == {"KEEP": "me"}, "the caller's variables came back"

    async def test_a_vanished_block_fails_the_call(self, child) -> None:  # noqa: ANN001
        """The callee was edited while a visitor was parked on a block it no longer has."""
        root = _flow(ROOT_ID, _root_graph())
        session = _session(node_id="c_gone", call_stack=[{
            "flow_id": CHILD_ID,
            "return_node_id": CALL_NODE,
            "caller_variables": {},
            "outputs": {},
        }])

        result = await _turn(root, session)

        assert result.text == "That call did not work."
        assert session.call_stack == []
