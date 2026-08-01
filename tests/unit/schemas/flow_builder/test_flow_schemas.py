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
