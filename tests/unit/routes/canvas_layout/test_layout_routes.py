"""
Route tests for the two "arrange this canvas" endpoints.

One module for both, because it is one endpoint contract mounted on two controllers — the
Flow Builder's and the Graph Designer's — and asserting the same six things twice in two
files is how the two drift apart.

What is worth testing here is not the arithmetic (that is
``tests/unit/services/canvas_layout/``) but the four things a route decides:

* the drawing in the **body** is what gets arranged, not the one in the row — an operator
  arranges a canvas that has unsaved changes, and answering for the stored graph would give
  them positions for a picture one edit behind;
* nothing is written, so an arrange followed by a reload still shows the stored drawing;
* somebody else's canvas is a 404, exactly as every sibling endpoint makes it;
* a body that cannot be read, or one over the cap, is a refusal with a sentence rather than
  a 500 — the canvas is holding unsaved work and must not be handed an error page.
"""

from __future__ import annotations

import pytest

from app.models.flow_builder import ChatbotFlow
from app.models.graph_designer import ToolGraph
from app.routes.flow_builder import FlowBuilderController
from app.routes.graph_designer import GraphDesignerController
from app.schemas.canvas_layout import MAX_LAYOUT_NODES

#: A four-block drawing that fans out and comes back together — the shape both canvases
#: actually have, and the one a naive layout gets wrong.
DRAWING = {
    "nodes": [
        {"id": "start", "type": "start", "position": {"x": 999, "y": 999}, "data": {}},
        {"id": "left", "type": "send_message", "position": {"x": 0, "y": 0}, "data": {}},
        {"id": "right", "type": "send_message", "position": {"x": 0, "y": 0}, "data": {}},
        {"id": "end", "type": "end", "position": {"x": 0, "y": 0}, "data": {}},
    ],
    "edges": [
        {"id": "e1", "source": "start", "target": "left"},
        {"id": "e2", "source": "start", "target": "right"},
        {"id": "e3", "source": "left", "target": "end"},
        {"id": "e4", "source": "right", "target": "end"},
    ],
}

STORED = {
    "nodes": [{"id": "start", "type": "start", "position": {"x": 60, "y": 60}, "data": {}}],
    "edges": [],
}


@pytest.fixture
def flow_client(auth_client_factory):  # noqa: ANN001, ANN201
    return auth_client_factory(FlowBuilderController)


@pytest.fixture
def graph_client(auth_client_factory):  # noqa: ANN001, ANN201
    return auth_client_factory(GraphDesignerController)


@pytest.fixture
def make_flow(db):  # noqa: ANN001, ANN201
    async def _make(owner, **kwargs):  # noqa: ANN001
        row = ChatbotFlow(
            user_id=owner.id,
            name=kwargs.pop("name", "Flow"),
            graph_data=kwargs.pop("graph_data", STORED),
            **kwargs,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row

    return _make


@pytest.fixture
def make_graph(db):  # noqa: ANN001, ANN201
    async def _make(owner, **kwargs):  # noqa: ANN001
        row = ToolGraph(
            user_id=owner.id,
            name=kwargs.pop("name", "Graph"),
            graph_data=kwargs.pop("graph_data", STORED),
            **kwargs,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row

    return _make


class TestTheFlowBuilderEndpoint:
    async def test_it_arranges_the_drawing_in_the_body(self, flow_client, user, make_flow) -> None:  # noqa: ANN001
        flow = await make_flow(user)

        response = flow_client.post(f"/flow-builder/{flow.uuid}/layout", json=DRAWING)

        assert response.status_code == 200
        positions = response.json()["positions"]
        assert set(positions) == {"start", "left", "right", "end"}
        assert positions["start"]["layer"] == 0
        assert positions["end"]["layer"] == 2

    async def test_it_ignores_the_stored_drawing(self, flow_client, user, make_flow) -> None:  # noqa: ANN001
        """The stored graph has one block. Answering from it would return one position for
        a canvas holding four."""
        flow = await make_flow(user, graph_data=STORED)

        positions = flow_client.post(
            f"/flow-builder/{flow.uuid}/layout", json=DRAWING,
        ).json()["positions"]

        assert len(positions) == 4

    async def test_it_ignores_the_positions_it_was_sent(self, flow_client, user, make_flow) -> None:  # noqa: ANN001
        """Start is posted at (999, 999) and still comes back on the top layer: the
        answer is derived from the wiring, not from where the blocks currently are."""
        flow = await make_flow(user)

        positions = flow_client.post(
            f"/flow-builder/{flow.uuid}/layout", json=DRAWING,
        ).json()["positions"]

        assert positions["start"]["layer"] == 0

    async def test_it_reports_a_return_jump_by_its_index(self, flow_client, user, make_flow) -> None:  # noqa: ANN001
        flow = await make_flow(user)
        looping = {
            "nodes": [
                {"id": "start", "type": "start", "data": {}},
                {"id": "menu", "type": "menu", "data": {}},
            ],
            "edges": [
                {"source": "start", "target": "menu"},
                {"source": "menu", "target": "menu"},
            ],
        }

        assert flow_client.post(
            f"/flow-builder/{flow.uuid}/layout", json=looping,
        ).json()["back_edges"] == [1]

    async def test_it_writes_nothing(self, flow_client, user, make_flow) -> None:  # noqa: ANN001
        flow = await make_flow(user, graph_data=STORED)

        flow_client.post(f"/flow-builder/{flow.uuid}/layout", json=DRAWING)
        reloaded = flow_client.get(f"/flow-builder/{flow.uuid}/graph").json()

        assert reloaded == STORED

    async def test_somebody_elses_flow_is_a_404(self, flow_client, make_user, make_flow) -> None:  # noqa: ANN001
        flow = await make_flow(await make_user("other@example.com"))

        response = flow_client.post(f"/flow-builder/{flow.uuid}/layout", json=DRAWING)

        assert response.status_code == 404
        assert "error" in response.json()

    def test_an_unauthenticated_caller_is_refused(self, client_factory, user) -> None:  # noqa: ANN001
        """``follow_redirects=False`` because `require_auth` bounces to the login page,
        which is not mounted in a single-controller test app — following the redirect
        would turn the refusal into a 404 and pass for the wrong reason."""
        anonymous = client_factory(FlowBuilderController)

        response = anonymous.post(
            f"/flow-builder/{user.uuid}/layout", json=DRAWING, follow_redirects=False,
        )

        assert response.status_code in (302, 401)

    async def test_a_body_that_is_not_an_object_is_a_sentence(self, flow_client, user, make_flow) -> None:  # noqa: ANN001
        flow = await make_flow(user)

        response = flow_client.post(f"/flow-builder/{flow.uuid}/layout", json=[1, 2, 3])

        assert response.status_code == 400
        assert "left as it is" in response.json()["error"]

    async def test_a_drawing_over_the_cap_is_refused(self, flow_client, user, make_flow) -> None:  # noqa: ANN001
        """The bound exists because this endpoint is the one that does real work per
        block. Refused with a sentence, not a 500."""
        flow = await make_flow(user)
        oversized = {
            "nodes": [{"id": f"n{index}"} for index in range(MAX_LAYOUT_NODES + 1)],
            "edges": [],
        }

        response = flow_client.post(f"/flow-builder/{flow.uuid}/layout", json=oversized)

        assert response.status_code == 400

    async def test_an_empty_canvas_is_an_empty_answer(self, flow_client, user, make_flow) -> None:  # noqa: ANN001
        flow = await make_flow(user)

        response = flow_client.post(
            f"/flow-builder/{flow.uuid}/layout", json={"nodes": [], "edges": []},
        )

        assert response.status_code == 200
        assert response.json() == {"positions": {}, "back_edges": []}


class TestTheGraphDesignerEndpoint:
    """The same contract on the other canvas. Both are asserted rather than one, because
    the two controllers each mount their own handler and only a test notices when one of
    them is changed and the other is not."""

    async def test_it_arranges_the_drawing_in_the_body(self, graph_client, user, make_graph) -> None:  # noqa: ANN001
        graph = await make_graph(user)

        response = graph_client.post(f"/graph-designer/{graph.uuid}/layout", json=DRAWING)

        assert response.status_code == 200
        positions = response.json()["positions"]
        assert positions["start"]["layer"] == 0
        assert positions["end"]["layer"] == 2

    async def test_it_writes_nothing(self, graph_client, user, make_graph) -> None:  # noqa: ANN001
        graph = await make_graph(user, graph_data=STORED)

        graph_client.post(f"/graph-designer/{graph.uuid}/layout", json=DRAWING)

        assert graph_client.get(f"/graph-designer/{graph.uuid}/graph").json() == STORED

    async def test_somebody_elses_graph_is_a_404(self, graph_client, make_user, make_graph) -> None:  # noqa: ANN001
        graph = await make_graph(await make_user("other@example.com"))

        response = graph_client.post(
            f"/graph-designer/{graph.uuid}/layout", json=DRAWING,
        )

        assert response.status_code == 404

    async def test_a_body_that_is_not_an_object_is_a_sentence(self, graph_client, user, make_graph) -> None:  # noqa: ANN001
        graph = await make_graph(user)

        response = graph_client.post(
            f"/graph-designer/{graph.uuid}/layout", json="not a drawing",
        )

        assert response.status_code == 400
        assert "left as it is" in response.json()["error"]


class TestBothCanvasesAgree:
    async def test_the_same_drawing_arranges_the_same_way(  # noqa: ANN001
        self, flow_client, graph_client, user, make_flow, make_graph,
    ) -> None:
        """One algorithm, so a block in the same place on both canvases. If these ever
        differ, one of the two routes has grown a special case."""
        flow = await make_flow(user)
        graph = await make_graph(user)

        from_flow = flow_client.post(
            f"/flow-builder/{flow.uuid}/layout", json=DRAWING,
        ).json()
        from_graph = graph_client.post(
            f"/graph-designer/{graph.uuid}/layout", json=DRAWING,
        ).json()

        assert from_flow == from_graph
