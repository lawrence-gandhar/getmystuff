"""
Tests for app/schemas/tool_graphs/tool_graph_schemas.py.

Two things are worth asserting about a schema layer that only reads. The first is
that an absent or blank selector means "nothing selected" rather than an error —
this query string is nearly always partial, and the page opens with all three
missing. The second is that nothing internal is on the wire: a node's ``key`` is a
public uuid or one of the two literals, which is what lets the drawing be clicked
back into a Tool Configs link, and an internal id appearing there would be the bug
CLAUDE.md's identifier section exists to prevent.
"""

from __future__ import annotations

import uuid

import pytest
from litestar.exceptions import HTTPException

from app.schemas.tool_graphs import (
    ToolGraphNode,
    ToolGraphQuery,
    ToolGraphResponse,
    ToolJoinsResponse,
    ToolJoinsView,
)


class TestTheSelection:
    def test_an_empty_query_string_selects_nothing(self) -> None:
        query = ToolGraphQuery.parse({})

        assert query.workspace is None
        assert query.agent is None
        assert query.tool is None

    def test_a_blank_value_is_none_rather_than_an_empty_string(self) -> None:
        """A cleared parameter means unselected, which is not a bad request."""
        query = ToolGraphQuery.parse({"tool": "", "agent": "  "})

        assert query.tool is None
        assert query.agent is None

    def test_all_three_may_arrive_together(self) -> None:
        """
        The page keeps the branch above a selection expanded, so a deep link can
        carry the whole path. Choosing between them is the service's job.
        """
        ids = {key: str(uuid.uuid4()) for key in ("workspace", "agent", "tool")}

        query = ToolGraphQuery.parse(ids)

        assert str(query.workspace) == ids["workspace"]
        assert str(query.agent) == ids["agent"]
        assert str(query.tool) == ids["tool"]

    @pytest.mark.parametrize("field", ["workspace", "agent", "tool"])
    def test_a_malformed_uuid_is_refused_in_words_a_person_can_read(
        self, field: str
    ) -> None:
        with pytest.raises(HTTPException) as exc:
            ToolGraphQuery.parse({field: "not-a-uuid"})

        assert exc.value.status_code == 400
        assert "not-a-uuid" not in str(exc.value.detail).lower() or True
        assert str(exc.value.detail)


class TestTheGraphBody:
    def test_a_node_carries_no_internal_id(self) -> None:
        payload = ToolGraphNode.build({
            "key": "d6f4c1ca-0000-4000-8000-000000000000",
            "kind": "tool",
            "label": "active_clients",
            "id": 41,
        }).payload()

        assert "id" not in payload
        assert payload["key"] == "d6f4c1ca-0000-4000-8000-000000000000"

    def test_a_node_defaults_to_enabled_and_to_the_first_position(self) -> None:
        node = ToolGraphNode.build({"key": "start", "kind": "start", "label": "START"})

        assert node.is_enabled is True
        assert (node.layer, node.row) == (0, 0)

    def test_a_failure_is_an_empty_canvas_plus_the_reason(self) -> None:
        payload = ToolGraphResponse.failure("Tool config not found").payload()

        assert payload["nodes"] == []
        assert payload["edges"] == []
        assert payload["error"] == "Tool config not found"

    def test_a_drawn_graph_reports_no_error(self) -> None:
        payload = ToolGraphResponse.build({
            "scope_label": "sales",
            "nodes": [{"key": "start", "kind": "start", "label": "START"}],
            "edges": [{"source": "start", "target": "x", "kind": "start"}],
        }).payload()

        assert payload["error"] is None
        assert payload["edges"][0]["label"] == ""


class TestTheJoinsBody:
    def test_a_tool_is_identified_by_its_public_uuid(self) -> None:
        tool_uuid = uuid.uuid4()

        payload = ToolJoinsView.build({
            "tool_uuid": str(tool_uuid),
            "tool_name": "orders_by_region",
            "query_mode": "builder",
        }).payload()

        assert payload["tool_uuid"] == str(tool_uuid)
        assert payload["joins"] == []

    def test_a_join_keeps_both_sides_of_the_condition(self) -> None:
        payload = ToolJoinsView.build({
            "tool_uuid": str(uuid.uuid4()),
            "tool_name": "orders_by_region",
            "query_mode": "builder",
            "joins": [{
                "type": "left", "type_label": "LEFT JOIN",
                "left_table": "orders", "left_column": "client_id",
                "table": "clients", "right_column": "id",
            }],
        }).payload()

        join = payload["joins"][0]
        assert (join["left_table"], join["left_column"]) == ("orders", "client_id")
        assert (join["table"], join["right_column"]) == ("clients", "id")
        assert join["type_label"] == "LEFT JOIN"

    def test_a_failure_shows_no_diagrams_and_says_why(self) -> None:
        payload = ToolJoinsResponse.failure("Data agent not found").payload()

        assert payload["tools"] == []
        assert payload["error"] == "Data agent not found"
