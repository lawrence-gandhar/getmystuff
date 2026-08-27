"""
Tests for app/schemas/graph_designer/graph_designer_schemas.py.

The schema layer's own job: is the payload *shaped* correctly, and does a refusal read as
a sentence rather than as Pydantic's internal wording. What a valid graph *is* belongs to
``graph_service`` and is deliberately not restated here — see the module docstring on why
the node vocabulary is pinned in one place only.

Two properties carry the suite:

* **The save payload is bounded but not narrowed.** The canvas owns the document's shape,
  so unknown keys survive; what the schema guarantees is that it is an object with two
  collections.
* **There is no node or edge ceiling.** Asserted, because "no cap" is a claim about
  behaviour and the flow schemas next door do impose one — so the absence here is a
  decision rather than an omission.
"""

from __future__ import annotations

import pytest
from litestar.exceptions import HTTPException

from app.schemas.base import MAX_CANVAS_COORD, MAX_EDGE_WAYPOINTS
from app.schemas.graph_designer import (
    GraphNodeOptionsResponse,
    GraphResumeRequest,
    GraphRunRequest,
    GraphRunStartedResponse,
    GraphRunView,
    GraphSaveRequest,
    GraphSaveResponse,
)


def _detail(schema, data: dict) -> str:
    """The sentence a refusal puts in front of the person who caused it."""
    with pytest.raises(HTTPException) as exc_info:
        schema.parse(data)
    return str(exc_info.value.detail)


class TestGraphSaveRequest:
    def test_keeps_keys_the_client_owns(self) -> None:
        payload = GraphSaveRequest.parse({
            "nodes": [{"id": "a"}], "edges": [], "viewport": {"x": 1, "y": 2},
        })

        assert payload.graph_data()["viewport"] == {"x": 1, "y": 2}

    def test_defaults_both_collections(self) -> None:
        """A canvas that has never been saved posts neither key."""
        payload = GraphSaveRequest.parse({})

        assert payload.graph_data() == {"nodes": [], "edges": []}

    def test_accepts_a_thousand_nodes(self) -> None:
        """
        No ceiling. ``FlowGraphSaveRequest`` caps at 500 because a conversation flow that
        large is a runaway client; a data pipeline is not, and what bounds a *run* is the
        per-loop iteration ceiling — a bound on work rather than on drawing.
        """
        payload = GraphSaveRequest.parse({
            "nodes": [{"id": f"n{index}"} for index in range(1000)],
            "edges": [{"id": f"e{index}"} for index in range(1000)],
        })

        assert len(payload.graph_data()["nodes"]) == 1000
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
        assert GraphSaveRequest.parse(graph).graph_data() == graph

    def test_the_bend_count_boundary_is_inclusive(self) -> None:
        bends = [{"x": 10, "y": 10}] * MAX_EDGE_WAYPOINTS
        graph = {"nodes": [], "edges": [{"id": "e1", "waypoints": bends}]}

        assert GraphSaveRequest.parse(graph).graph_data() == graph

    def test_too_many_bends_is_refused(self) -> None:
        bends = [{"x": 10, "y": 10}] * (MAX_EDGE_WAYPOINTS + 1)

        assert "more than 4 bend points" in _detail(
            GraphSaveRequest, {"edges": [{"id": "e1", "waypoints": bends}]}
        )

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
    def test_a_non_finite_bend_is_refused(self, value: float) -> None:
        """
        The reason this validator exists at all: ``NaN`` passes every other rule
        here, and PostgreSQL then refuses the ``jsonb`` it is written into — so
        without this the request is a 500 with a stack trace instead of a sentence.
        """
        assert "not a valid position" in _detail(
            GraphSaveRequest,
            {"edges": [{"id": "e1", "waypoints": [{"x": value, "y": 0}]}]},
        )

    def test_bends_that_are_not_a_list_are_refused(self) -> None:
        assert "could not be read" in _detail(
            GraphSaveRequest, {"edges": [{"id": "e1", "waypoints": "nope"}]}
        )

    def test_a_malformed_bend_is_refused(self) -> None:
        assert "missing its position" in _detail(
            GraphSaveRequest, {"edges": [{"id": "e1", "waypoints": [{"x": 5}]}]}
        )

    @pytest.mark.parametrize("value", [-1, MAX_CANVAS_COORD + 1])
    def test_a_bend_off_the_canvas_is_refused(self, value: int) -> None:
        assert "outside the canvas" in _detail(
            GraphSaveRequest,
            {"edges": [{"id": "e1", "waypoints": [{"x": value, "y": 0}]}]},
        )

    def test_the_bend_cap_does_not_bound_the_drawing(self) -> None:
        """
        Bends are capped per connector; the drawing itself is deliberately not
        capped here — see ``test_accepts_a_thousand_nodes`` above, which is the
        decision this must not quietly reverse.
        """
        graph = {
            "nodes": [{"id": f"n{index}"} for index in range(1000)],
            "edges": [
                {"id": f"e{index}", "waypoints": [{"x": index, "y": index}]}
                for index in range(1000)
            ],
        }

        assert len(GraphSaveRequest.parse(graph).graph_data()["edges"]) == 1000


class TestGraphRunRequest:
    def test_defaults_to_the_whole_graph(self) -> None:
        assert GraphRunRequest.parse({}).selection() is None

    def test_a_selection_returns_its_ids(self) -> None:
        payload = GraphRunRequest.parse({"scope": "selection", "node_ids": ["a", "b"]})

        assert payload.selection() == ["a", "b"]

    def test_none_and_empty_mean_different_things(self) -> None:
        """
        ``None`` is "everything" and a list is "exactly these". The run row's nullable
        column depends on the distinction, so it is asserted rather than assumed.
        """
        assert GraphRunRequest.parse({"scope": "full", "node_ids": ["a"]}).selection() is None

    def test_trims_and_deduplicates_ids(self) -> None:
        """
        Selecting a node twice is a click, not an instruction to run it twice — and a
        duplicate would produce two step rows for one pass.
        """
        payload = GraphRunRequest.parse({
            "scope": "selection", "node_ids": [" a ", "b", "a"],
        })

        assert payload.node_ids == ["a", "b"]

    def test_refuses_an_unknown_scope(self) -> None:
        with pytest.raises(HTTPException) as caught:
            GraphRunRequest.parse({"scope": "sideways"})

        assert "whole graph or a selection" in caught.value.detail

    def test_refuses_a_blank_node_id(self) -> None:
        with pytest.raises(HTTPException) as caught:
            GraphRunRequest.parse({"scope": "selection", "node_ids": ["  "]})

        assert "has no id" in caught.value.detail

    def test_refuses_an_id_longer_than_the_column(self) -> None:
        """
        The column is 64 characters, so a longer id would be a selection the log could not
        record — and it cannot have come from the canvas.
        """
        with pytest.raises(HTTPException) as caught:
            GraphRunRequest.parse({"scope": "selection", "node_ids": ["x" * 200]})

        assert "not one this canvas produced" in caught.value.detail


class TestGraphResumeRequest:
    def test_accepts_a_plain_answer(self) -> None:
        assert GraphResumeRequest.parse({"answer": "yes"}).answer == "yes"

    def test_refuses_an_answer_longer_than_the_cap(self) -> None:
        with pytest.raises(HTTPException):
            GraphResumeRequest.parse({"answer": "x" * 20_000})


class TestResponses:
    def test_run_urls_are_relative_paths(self) -> None:
        """
        Never absolute. Every URL this application hands a browser is a path — an absolute
        one built server-side is what goes stale when a tunnel rotates or a domain changes.
        """
        payload = GraphRunStartedResponse.for_run("abc").payload()

        assert payload["events_url"] == "/graph-designer/runs/abc/events"
        assert payload["status_url"] == "/graph-designer/runs/abc"

    def test_a_null_selection_reads_as_an_empty_list(self) -> None:
        """
        NULL on the row means "the whole graph"; the dock only needs to know which nodes to
        highlight, and for a full run that is none in particular.
        """
        view = GraphRunView.build({
            "uuid": "u", "graph_uuid": "g", "status": "running",
            "scope": "full", "selected_nodes": None,
        })

        assert view.selected_nodes == []

    def test_node_options_failure_is_empty_pickers_and_a_reason(self) -> None:
        """
        A 200 with ``error`` set, the contract ``ChildToolOptionsResponse`` established: a
        picker that cannot be filled puts a sentence beside itself rather than replacing
        the canvas.
        """
        payload = GraphNodeOptionsResponse.failure("nope").payload()

        assert payload["datasources"] == []
        assert payload["error"] == "nope"

    def test_save_failure_carries_the_reason_and_no_marker(self) -> None:
        result = GraphSaveResponse.failure("bad graph")

        assert result.saved is False
        assert result.message == "bad graph"


class TestEveryPickerSurvivesTheResponse:
    """
    ``ResponseSchema`` is ``extra="ignore"``, so a list the service builds and this schema
    does not declare is dropped from the JSON **without a word**.

    That is not hypothetical. ``email_templates`` and ``smtp_configs`` were built by
    ``node_options`` and undeclared here, so an Email node's Template picker was empty in
    every browser and nothing anywhere said why — which in turn hid a TypeError in the
    binding editor that could not be reached while the picker stayed empty.

    These tests exist so that failure mode is loud next time.
    """

    def _built(self) -> dict:
        return GraphNodeOptionsResponse.build({
            "datasources": [
                {"uuid": "d1", "label": "Warehouse", "detail": "postgres",
                 "disabled_reason": ""},
            ],
            "tool_configs": [],
            "data_agents": [],
            "email_templates": [
                {"uuid": "t1", "label": "Weekly digest", "detail": "Your week",
                 "disabled_reason": "",
                 "variables": [{"name": "CUSTOMER", "required": True}]},
            ],
            "smtp_configs": [
                {"uuid": "s1", "label": "Relay", "detail": "smtp.example.com",
                 "disabled_reason": ""},
            ],
            "human_expects": ["text"],
            "error": None,
        }).payload()

    def test_the_email_templates_reach_the_browser(self) -> None:
        assert [row["label"] for row in self._built()["email_templates"]] == ["Weekly digest"]

    def test_the_smtp_servers_reach_the_browser(self) -> None:
        assert [row["label"] for row in self._built()["smtp_configs"]] == ["Relay"]

    def test_a_template_carries_what_it_declares(self) -> None:
        """
        The panel draws one binding row per declared variable the moment a template is
        chosen. Dropping this would make it draw none, silently.
        """
        variables = self._built()["email_templates"][0]["variables"]

        assert [variable["name"] for variable in variables] == ["CUSTOMER"]

    def test_a_failure_still_answers_with_every_picker_empty(self) -> None:
        """
        The panel reads each list unconditionally, so a missing key is a TypeError in the
        browser rather than an empty dropdown.
        """
        payload = GraphNodeOptionsResponse.failure("nope").payload()

        for picker in ("datasources", "tool_configs", "data_agents",
                       "email_templates", "smtp_configs"):
            assert payload[picker] == []
