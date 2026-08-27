"""
Tests for app/schemas/flow_builder/flow_schemas.py.

Flow Builder's canvas is entirely client-rendered, so this is the one module whose
request schemas are JSON-bodied and whose graph payload deliberately keeps fields
it does not declare. Two properties are worth pinning:

* ``extra="allow"`` on the graph — the node and edge vocabulary belongs to
  ``flow_builder.js`` and ``flow_service.update_flow_graph``. Narrowing it here
  would silently drop a key the canvas needs, so there is a test that a
  ``viewport`` the schema knows nothing about survives a round trip.
* the graph is still *bounded*. A runaway client must not be able to put an
  unbounded document into the ``graph_data`` column.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from litestar.exceptions import HTTPException

from app.schemas.base import MAX_CANVAS_COORD, MAX_EDGE_WAYPOINTS
from app.schemas.flow_builder import (
    MAX_GRAPH_EDGES,
    MAX_GRAPH_NODES,
    MAX_MANUAL_TEXT_LENGTH,
    FlowCreateRequest,
    FlowGraphSaveRequest,
    FlowRenameRequest,
    FlowSetActiveRequest,
    FlowView,
    KnowledgeBaseManualTextRequest,
    KnowledgeBaseStateResponse,
)

NAME_FORMS = [FlowCreateRequest, FlowRenameRequest]


def _detail(schema, data: dict) -> str:
    with pytest.raises(HTTPException) as exc_info:
        schema.parse(data)
    return str(exc_info.value.detail)


@pytest.mark.parametrize("schema", NAME_FORMS, ids=["create", "rename"])
class TestNameForms:
    def test_a_valid_name(self, schema) -> None:
        assert schema.parse({"name": "  Onboarding  "}).name == "Onboarding"

    def test_the_name_is_required(self, schema) -> None:
        assert _detail(schema, {}) == "Flow name is required"

    def test_a_whitespace_only_name_is_empty(self, schema) -> None:
        assert _detail(schema, {"name": "   "}) == "Flow name is required"


class TestSetActive:
    def test_reads_the_publish_toggle(self) -> None:
        assert FlowSetActiveRequest.parse({"is_active": "true"}).is_active is True
        assert FlowSetActiveRequest.parse({"is_active": "false"}).is_active is False

    def test_an_absent_flag_is_draft(self) -> None:
        assert FlowSetActiveRequest.parse({}).is_active is False


class TestGraphSave:
    def test_an_empty_canvas_is_valid(self) -> None:
        payload = FlowGraphSaveRequest.parse({})
        assert payload.graph_data() == {"nodes": [], "edges": []}

    def test_nodes_and_edges_survive(self) -> None:
        graph = {"nodes": [{"id": "n1", "type": "menu"}], "edges": [{"from": "n1"}]}
        assert FlowGraphSaveRequest.parse(graph).graph_data() == graph

    def test_keys_the_schema_does_not_declare_are_kept(self) -> None:
        """
        The canvas owns this document's shape. Dropping a key it added — a
        viewport, a zoom level — would lose the user's canvas position on save.
        """
        graph = {"nodes": [], "edges": [], "viewport": {"x": 12, "y": -4}, "zoom": 1.5}
        assert FlowGraphSaveRequest.parse(graph).graph_data() == graph

    def test_a_runaway_node_count_is_refused(self) -> None:
        assert "cannot have more than 500" in _detail(
            FlowGraphSaveRequest, {"nodes": [{}] * (MAX_GRAPH_NODES + 1)}
        )

    def test_a_runaway_edge_count_is_refused(self) -> None:
        assert "cannot have more than 2000" in _detail(
            FlowGraphSaveRequest, {"edges": [{}] * (MAX_GRAPH_EDGES + 1)}
        )

    def test_the_node_count_boundary_is_inclusive(self) -> None:
        payload = FlowGraphSaveRequest.parse({"nodes": [{}] * MAX_GRAPH_NODES})
        assert len(payload.nodes) == MAX_GRAPH_NODES

    def test_hand_placed_bends_survive(self) -> None:
        """
        A connector routed by hand stores where it was dragged to. Nothing
        server-side needed to change for that — it rides on ``extra="allow"`` the
        same way ``layout`` does — so this is the test that keeps it true.
        """
        graph = {
            "nodes": [],
            "edges": [{"id": "e1", "waypoints": [{"x": 40, "y": 120}]}],
        }
        assert FlowGraphSaveRequest.parse(graph).graph_data() == graph

    def test_a_connector_with_no_bends_is_untouched(self) -> None:
        graph = {"nodes": [], "edges": [{"id": "e1", "source": "a", "target": "b"}]}
        assert FlowGraphSaveRequest.parse(graph).graph_data() == graph

    def test_the_bend_count_boundary_is_inclusive(self) -> None:
        bends = [{"x": 10, "y": 10}] * MAX_EDGE_WAYPOINTS
        graph = {"nodes": [], "edges": [{"id": "e1", "waypoints": bends}]}
        assert FlowGraphSaveRequest.parse(graph).graph_data() == graph

    def test_too_many_bends_is_refused(self) -> None:
        bends = [{"x": 10, "y": 10}] * (MAX_EDGE_WAYPOINTS + 1)
        assert "more than 4 bend points" in _detail(
            FlowGraphSaveRequest, {"edges": [{"id": "e1", "waypoints": bends}]}
        )

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
    def test_a_non_finite_bend_is_refused(self, value: float) -> None:
        """
        The reason this validator exists at all.

        ``NaN`` passes every other rule in this layer, ``json.dumps`` then writes a
        bare ``NaN``, and PostgreSQL refuses it as ``jsonb`` — so without this the
        request is a 500 with a stack trace instead of a sentence.
        """
        assert "not a valid position" in _detail(
            FlowGraphSaveRequest,
            {"edges": [{"id": "e1", "waypoints": [{"x": value, "y": 0}]}]},
        )

    @pytest.mark.parametrize(
        "bends",
        ["not-a-list", {"x": 1, "y": 2}, 7],
        ids=["string", "object", "number"],
    )
    def test_bends_that_are_not_a_list_are_refused(self, bends: object) -> None:
        assert "could not be read" in _detail(
            FlowGraphSaveRequest, {"edges": [{"id": "e1", "waypoints": bends}]}
        )

    @pytest.mark.parametrize(
        "bend",
        [{"x": 1}, {"y": 1}, {}, "nope", None, {"x": "10", "y": "20"}, {"x": True, "y": 1}],
        ids=["no-y", "no-x", "empty", "string", "none", "strings", "bool"],
    )
    def test_a_malformed_bend_is_refused(self, bend: object) -> None:
        assert "missing its position" in _detail(
            FlowGraphSaveRequest, {"edges": [{"id": "e1", "waypoints": [bend]}]}
        )

    @pytest.mark.parametrize("value", [-1, MAX_CANVAS_COORD + 1])
    def test_a_bend_off_the_canvas_is_refused(self, value: int) -> None:
        assert "outside the canvas" in _detail(
            FlowGraphSaveRequest,
            {"edges": [{"id": "e1", "waypoints": [{"x": value, "y": 0}]}]},
        )

    def test_a_null_bend_list_is_left_alone(self) -> None:
        """
        Not the canvas's doing, but a document that has been through a tool which
        writes ``null`` for an empty list should still load rather than refuse.
        """
        graph = {"nodes": [], "edges": [{"id": "e1", "waypoints": None}]}
        assert FlowGraphSaveRequest.parse(graph).graph_data() == graph

    def test_node_types_are_not_pinned_here(self) -> None:
        """
        Deliberate. ``flow_service.update_flow_graph`` validates the structure it
        understands; a second copy of the vocabulary would mean two places to
        change every time a node type is added, and this one would be guessing.
        """
        payload = FlowGraphSaveRequest.parse({"nodes": [{"type": "a_brand_new_node"}]})
        assert payload.nodes[0]["type"] == "a_brand_new_node"

    def test_it_carries_the_modules_own_message_for_a_bad_body(self) -> None:
        assert FlowGraphSaveRequest.invalid_body_message == "Invalid graph data."


class TestKnowledgeBaseManualText:
    def _valid(self, **extra) -> dict:
        return {"label": "Refund policy", "text": "Refunds within 30 days.", **extra}

    def test_a_valid_entry(self) -> None:
        payload = KnowledgeBaseManualTextRequest.parse(self._valid())
        assert payload.label == "Refund policy"

    def test_the_label_is_required(self) -> None:
        assert _detail(KnowledgeBaseManualTextRequest, {"text": "x"}) == (
            "Label is required"
        )

    def test_the_text_is_required(self) -> None:
        assert _detail(KnowledgeBaseManualTextRequest, self._valid(text="   ")) == (
            "Text is required"
        )

    def test_the_text_is_bounded(self) -> None:
        at_cap = "t" * MAX_MANUAL_TEXT_LENGTH
        assert KnowledgeBaseManualTextRequest.parse(
            self._valid(text=at_cap)
        ).text == at_cap
        with pytest.raises(HTTPException):
            KnowledgeBaseManualTextRequest.parse(
                self._valid(text="t" * (MAX_MANUAL_TEXT_LENGTH + 1))
            )


class TestKnowledgeBaseState:
    def test_the_shape_the_service_actually_returns(self) -> None:
        state = {
            "status": "trained",
            "trained_at": "2026-08-01T10:00:00",
            "error_message": None,
            "documents": [
                {
                    "id": "doc-uuid",
                    "source_type": "file",
                    "label": "policy.pdf",
                    "size_bytes": 2048,
                    "extraction_status": "done",
                    "error_message": None,
                    "created_at": "2026-08-01T09:00:00",
                }
            ],
        }
        payload = KnowledgeBaseStateResponse.payload_for(state)
        assert payload["status"] == "trained"
        assert payload["documents"][0]["id"] == "doc-uuid"

    def test_a_datetime_is_serialized_rather_than_left_to_an_encoder(self) -> None:
        """
        This is a JSON body read by ``flow_builder.js``, so the timestamp has to
        be a string regardless of what the service handed over.
        """
        moment = datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc)
        payload = KnowledgeBaseStateResponse.payload_for({"trained_at": moment})
        assert isinstance(payload["trained_at"], str)
        assert payload["trained_at"].startswith("2026-08-01T10:00:00")

    def test_an_untrained_base_has_no_timestamp(self) -> None:
        assert KnowledgeBaseStateResponse.build({"status": "empty"}).trained_at is None

    def test_a_document_never_carries_an_internal_id(self) -> None:
        """
        ``id`` here holds the document's public uuid — the key is named ``id``
        because the canvas script reads it under that name.
        """
        payload = KnowledgeBaseStateResponse.payload_for(
            {"documents": [{"id": "doc-uuid", "label": "x", "knowledge_base_id": 7}]}
        )
        assert payload["documents"][0] == {
            "id": "doc-uuid",
            "label": "x",
            "source_type": "",
            "size_bytes": None,
            "extraction_status": "",
            "error_message": None,
            "created_at": None,
        }


class TestFlowView:
    def test_the_shape_the_service_actually_returns(self) -> None:
        view = FlowView.build(
            {
                "uuid": "f-1",
                "name": "Onboarding",
                "is_active": True,
                "updated_at": datetime(2026, 8, 1, tzinfo=timezone.utc),
                "chatbot_name": "Support bot",
            }
        )
        assert view.chatbot_name == "Support bot"

    def test_an_unattached_flow_has_no_chatbot_name(self) -> None:
        """A flow runs on at most one agent, so the list shows a name or nothing."""
        assert FlowView.build({"uuid": "f", "name": "n"}).chatbot_name is None

    def test_no_internal_id_is_exposed(self) -> None:
        payload = FlowView.payload_for({"id": 2, "uuid": "f", "name": "n"})
        assert "id" not in payload
