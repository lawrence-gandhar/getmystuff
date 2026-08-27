"""
Tests for the **Create File** and **Download File** blocks inside the flow engine.

What is asserted here is the engine's half — the parts the runners cannot see:

* neither block **ends the turn**. A Create File block that stopped the conversation would
  make a Send Message after it fire on the visitor's *next* message, which is the exact
  complaint the AI Fallback block already carries a docstring about.
* the button is attached to **whatever ends the turn**, not returned by the block, so a
  Send Message after it still speaks and its words still appear above the button.
* the ``{{VARIABLE}}`` interpolation happens on this side, for the file name and the button
  label, using this module's own "leave an unknown placeholder standing" semantics.
* a failure takes ``error`` if drawn, and otherwise signs off — never a silent hop to
  ``default``, which would offer a download of nothing.
* what each block records: the *path* under Create File's variable, the *link* under
  Download File's, and the file itself in ``node_results`` keyed by node id.

DB-free like the neighbouring engine tests: the runner is stubbed, because what is under
test is how the engine drives the block rather than what the block does with a database.
"""

from __future__ import annotations

import pytest

from app.models.chatbot import ChatbotApiKey
from app.models.flow_builder import ChatbotFlow, ChatbotFlowSession
from app.services.file_delivery.errors import SourceError
from app.services.flow_builder import engine_service

MAKE_ID = "make_1"
OFFER_ID = "offer_1"
AFTER_ID = "msg_after"
ERROR_ID = "msg_err"
USER_ID = 7

WRITTEN = {
    "file_uuid": "8f2c0a1e-0000-4000-8000-000000000001",
    "file_name": "orders-a-1001.csv",
    "file_path": "uploads/generated_files/8f2c/orders-a-1001.csv",
    "file_format": "csv",
    "row_count": 12,
    "byte_size": 340,
}


def _graph(
    *,
    error_edge: bool = True,
    after: bool = True,
    make_data: dict | None = None,
    offer_data: dict | None = None,
    offer: bool = True,
) -> dict:
    make = {
        "file_format": "csv",
        "file_name": "orders-{{ORDER_REF}}",
        "data": {"source": "variable", "name": "ROWS"},
        "variable_name": "FILE_PATH",
    }
    make.update(make_data or {})

    offer_node = {"create_file_node_id": MAKE_ID, "variable_name": "FILE_URL"}
    offer_node.update(offer_data or {})

    nodes = [
        {"id": "start", "type": "start", "data": {}},
        {"id": MAKE_ID, "type": "create_file", "data": make},
        {"id": AFTER_ID, "type": "send_message",
         "data": {"message_text": "Your file is ready."}},
        {"id": ERROR_ID, "type": "send_message",
         "data": {"message_text": "That did not work."}},
    ]
    edges = [{"source": "start", "target": MAKE_ID, "source_port": "default"}]

    if offer:
        nodes.insert(2, {"id": OFFER_ID, "type": "download_file", "data": offer_node})
        edges.append({"source": MAKE_ID, "target": OFFER_ID, "source_port": "default"})
        if after:
            edges.append({"source": OFFER_ID, "target": AFTER_ID, "source_port": "default"})
        if error_edge:
            edges.append({"source": OFFER_ID, "target": ERROR_ID, "source_port": "error"})
    elif after:
        edges.append({"source": MAKE_ID, "target": AFTER_ID, "source_port": "default"})

    if error_edge:
        edges.append({"source": MAKE_ID, "target": ERROR_ID, "source_port": "error"})

    return {"nodes": nodes, "edges": edges}


def _flow(graph: dict) -> ChatbotFlow:
    flow = ChatbotFlow()
    flow.id = 1
    flow.user_id = USER_ID
    flow.name = "Files"
    flow.graph_data = graph
    flow.is_active = True
    flow.kind = "agent"
    return flow


def _session(node_id: str = MAKE_ID, **kwargs) -> ChatbotFlowSession:  # noqa: ANN003
    session = ChatbotFlowSession()
    session.id = 11
    session.current_node_id = node_id
    session.variables = kwargs.pop("variables", {"ROWS": "[]", "ORDER_REF": "A-1001"})
    session.node_results = kwargs.pop("node_results", {})
    session.call_stack = []
    session.status = "active"
    session.awaiting_graph_run = None
    return session


def _key() -> ChatbotApiKey:
    key = ChatbotApiKey()
    key.id = 3
    key.user_id = USER_ID
    return key


@pytest.fixture
def runner(monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    """
    The file runner, recorded rather than run.

    ``calls`` is what each block was handed — which is how the interpolation assertions see
    the *rendered* file name and button text. ``fail`` makes either block raise.
    """
    from app.services.file_delivery.nodes import flow_builder_runner

    state: dict = {"calls": [], "fail": None, "button": None}

    async def create(db, node, *, chatbot_key, session):  # noqa: ANN001, ANN202
        state["calls"].append(("create", node))
        if state["fail"] == "create":
            raise SourceError("That block produced no rows.")
        return dict(WRITTEN)

    async def download(db, node, *, chatbot_key, session, file_uuid):  # noqa: ANN001, ANN202
        state["calls"].append(("download", node, file_uuid))
        if state["fail"] == "download":
            raise SourceError("That file has gone.")
        return {
            "url": "/public/generated_files/8f2c?key=k&session_token=t",
            "file_uuid": WRITTEN["file_uuid"],
            "file_name": WRITTEN["file_name"],
            "file_format": "csv",
            "byte_size": 340,
            "row_count": 12,
            "button": state["button"],
        }

    monkeypatch.setattr(flow_builder_runner, "run_create_file_node", create)
    monkeypatch.setattr(flow_builder_runner, "run_download_file_node", download)
    return state


async def _turn(flow: ChatbotFlow, session: ChatbotFlowSession):  # noqa: ANN202
    return await engine_service._run_internal_hops(None, _key(), flow, session, "")


def _node_of(runner_state: dict, kind: str) -> dict:
    for call in runner_state["calls"]:
        if call[0] == kind:
            return call[1]
    raise AssertionError(f"the {kind} block was never run")


class TestNeitherBlockEndsTheTurn:
    async def test_the_message_after_them_speaks_in_the_same_turn(self, runner) -> None:  # noqa: ANN001
        """
        The point of the pair. If either block ended the turn, the operator's sentence
        would arrive on the visitor's *next* message — with the file already made.
        """
        result = await _turn(_flow(_graph()), _session())

        assert result.type == "text"
        assert result.text == "Your file is ready."
        assert [call[0] for call in runner["calls"]] == ["create", "download"]

    async def test_a_create_file_block_with_nothing_after_it_signs_off(
        self, runner,  # noqa: ANN001
    ) -> None:
        result = await _turn(
            _flow(_graph(offer=False, after=False, error_edge=False)), _session(),
        )

        assert result.text == engine_service._DEFAULT_END_MESSAGE
        assert result.file_download is None


class TestWhatTheBlocksRecord:
    async def test_create_file_stores_the_path_and_the_file(self, runner) -> None:  # noqa: ANN001
        """
        The *path* in the variable, and the file itself in ``node_results`` keyed by node
        id. The link is the other block's business: a path is a fact about this server and
        is no use to a visitor.
        """
        session = _session()

        await _turn(_flow(_graph()), session)

        assert session.variables["FILE_PATH"] == WRITTEN["file_path"]
        assert session.node_results[MAKE_ID] == {
            "kind": "file", "file_uuid": WRITTEN["file_uuid"],
        }

    async def test_download_file_stores_the_link(self, runner) -> None:  # noqa: ANN001
        session = _session()

        await _turn(_flow(_graph()), session)

        assert session.variables["FILE_URL"].startswith("/public/generated_files/")

    async def test_the_download_block_reads_the_uuid_off_the_named_block(
        self, runner,  # noqa: ANN001
    ) -> None:
        """Not off the wire: an operator may put other blocks between the two."""
        session = _session()

        await _turn(_flow(_graph()), session)

        download_call = [c for c in runner["calls"] if c[0] == "download"][0]
        assert download_call[2] == WRITTEN["file_uuid"]


class TestInterpolation:
    async def test_the_file_name_is_rendered_from_the_conversation(self, runner) -> None:  # noqa: ANN001
        await _turn(_flow(_graph()), _session(variables={"ROWS": "[]", "ORDER_REF": "A-9"}))

        assert _node_of(runner, "create")["data"]["file_name"] == "orders-A-9"

    async def test_the_button_label_is_rendered_too(self, runner) -> None:  # noqa: ANN001
        graph = _graph(offer_data={
            "show_button": True, "button_text": "Download {{ORDER_REF}}",
        })

        await _turn(_flow(graph), _session())

        assert _node_of(runner, "download")["data"]["button_text"] == "Download A-1001"

    async def test_an_unknown_placeholder_is_left_standing(self, runner) -> None:  # noqa: ANN001
        """
        This module's semantics, deliberately different from the email renderer's: a file
        called ``orders-{{NOPE}}.csv`` is a visible mistake, where ``orders-.csv`` is a
        silent one. ``_render_text`` says why.
        """
        graph = _graph(make_data={"file_name": "orders-{{NOPE}}"})

        await _turn(_flow(graph), _session())

        assert _node_of(runner, "create")["data"]["file_name"] == "orders-{{NOPE}}"

    async def test_the_nodes_own_data_is_not_mutated(self, runner) -> None:  # noqa: ANN001
        """
        The rendered copy must not be written back onto the graph: the graph is the saved
        drawing, and the next visitor's ORDER_REF is not this one's.
        """
        flow = _flow(_graph())

        await _turn(flow, _session())

        saved = [n for n in flow.graph_data["nodes"] if n["id"] == MAKE_ID][0]
        assert saved["data"]["file_name"] == "orders-{{ORDER_REF}}"


class TestTheButton:
    async def test_no_button_means_no_payload(self, runner) -> None:  # noqa: ANN001
        result = await _turn(_flow(_graph()), _session())

        assert result.file_download is None

    async def test_the_button_rides_along_with_whatever_ends_the_turn(
        self, runner,  # noqa: ANN001
    ) -> None:
        """
        Attached to the *later* block's result, which is what puts the button under the
        operator's own sentence rather than instead of it.
        """
        runner["button"] = {"label": "Get it", "colour": "#198754", "url": "/x"}

        result = await _turn(_flow(_graph()), _session())

        assert result.text == "Your file is ready."
        assert result.file_download == {
            "label": "Get it", "colour": "#198754", "url": "/x",
        }

    async def test_it_survives_to_a_prompt_that_ends_the_turn(self, runner) -> None:  # noqa: ANN001
        """A Menu after the block still offers its options, with the button underneath."""
        runner["button"] = {"label": "Get it", "colour": "#198754", "url": "/x"}
        graph = _graph()
        graph["nodes"].append({
            "id": "menu_1", "type": "menu",
            "data": {"prompt_text": "Anything else?",
                     "options": [{"id": "o1", "label": "No thanks"}]},
        })
        graph["edges"] = [
            edge for edge in graph["edges"]
            if not (edge["source"] == OFFER_ID and edge["source_port"] == "default")
        ] + [{"source": OFFER_ID, "target": "menu_1", "source_port": "default"}]

        result = await _turn(_flow(graph), _session())

        assert result.type == "buttons"
        assert result.file_download["label"] == "Get it"

    async def test_it_is_consumed_so_a_later_turn_does_not_redraw_it(
        self, runner,  # noqa: ANN001
    ) -> None:
        runner["button"] = {"label": "Get it", "colour": "#198754", "url": "/x"}
        session = _session()

        await _turn(_flow(_graph()), session)

        assert getattr(session, "_file_download", None) is None


class TestFailures:
    async def test_a_create_file_failure_takes_the_error_port(self, runner) -> None:  # noqa: ANN001
        runner["fail"] = "create"

        result = await _turn(_flow(_graph()), _session())

        assert result.text == "That did not work.", (
            "a failed file block must not leave by the same edge as a written one"
        )

    async def test_a_create_file_failure_with_no_error_port_signs_off(
        self, runner,  # noqa: ANN001
    ) -> None:
        runner["fail"] = "create"

        result = await _turn(_flow(_graph(error_edge=False)), _session())

        assert result.text == engine_service._DEFAULT_END_MESSAGE
        assert result.type == "text"

    async def test_a_download_failure_takes_the_error_port(self, runner) -> None:  # noqa: ANN001
        runner["fail"] = "download"

        result = await _turn(_flow(_graph()), _session())

        assert result.text == "That did not work."

    async def test_nothing_is_stored_when_the_block_failed(self, runner) -> None:  # noqa: ANN001
        runner["fail"] = "create"
        session = _session()

        await _turn(_flow(_graph()), session)

        assert "FILE_PATH" not in session.variables
        assert MAKE_ID not in session.node_results

    async def test_a_download_block_whose_maker_never_ran_fails_rather_than_skipping(
        self, runner,  # noqa: ANN001
    ) -> None:
        """
        A branch went the other way, or the blocks are wired backwards. A block that
        quietly did nothing would be a button the operator drew and never sees, with
        nothing to explain why.
        """
        session = _session(node_id=OFFER_ID, node_results={})

        result = await _turn(_flow(_graph()), session)

        assert result.text == "That did not work."
        assert [call[0] for call in runner["calls"]] == []
