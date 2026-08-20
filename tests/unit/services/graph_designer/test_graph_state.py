"""
Tests for app/services/graph_designer/graph_state.py — the reducers and the caps.

No LangGraph needed, which is why this file exists separately from the compiler's: the
reducers are the part that decides whether a node with two upstreams sees both of them,
and the caps are what stop a log row carrying a result set. Both are pure.

Three properties carry the suite:

* **A reducer merges rather than replaces.** Without it a node with two upstreams sees
  only the second, and the graph is quietly missing half its inputs — the same bug
  ``tool_chain_graph._merge_values`` exists to prevent.
* **A preview states the real count, not the sample size.** A dock showing twenty rows
  and saying "20" when there were two thousand is the class of quietly-wrong number this
  application is careful about.
* **A preview is always JSON-serialisable.** It goes into a JSONB column, and a value the
  encoder refuses would turn a successful node into a failed step — the log breaking the
  run it is only supposed to be observing.
"""

from __future__ import annotations

import datetime
import decimal
import json

import pytest

from app.services.graph_designer import graph_state as gs


class TestMergeReducer:
    def test_merges_two_contributions(self) -> None:
        assert gs._merge({"a": 1}, {"b": 2}) == {"a": 1, "b": 2}

    def test_the_incoming_value_wins_on_a_collision(self) -> None:
        """A loop's second pass must overwrite its first — the newest answer is current."""
        assert gs._merge({"a": 1}, {"a": 9}) == {"a": 9}

    def test_treats_none_as_empty_on_either_side(self) -> None:
        assert gs._merge(None, {"a": 1}) == {"a": 1}
        assert gs._merge({"a": 1}, None) == {"a": 1}

    def test_does_not_mutate_the_current_mapping(self) -> None:
        """
        LangGraph may hold the previous state; mutating it in place would edit history.
        """
        current = {"a": 1}
        gs._merge(current, {"b": 2})
        assert current == {"a": 1}


class TestInitialState:
    def test_every_channel_is_present_and_empty(self) -> None:
        """
        Present rather than absent, so a runner reading ``state["outputs"]`` never has to
        guard for the first node's case.
        """
        state = gs.initial_state("run-1", {"x": 1})

        assert state["outputs"] == {}
        assert state["loops"] == {}
        assert state["answers"] == {}
        assert state["errors"] == {}
        assert state["failed_at"] == ""
        assert state["inputs"] == {"x": 1}

    def test_the_run_id_is_a_string(self) -> None:
        """State has to be JSON-serialisable for the checkpointer, and a UUID is not."""
        import uuid as uuid_pkg

        state = gs.initial_state(uuid_pkg.uuid4())
        assert isinstance(state["run_id"], str)
        json.dumps(state)


class TestPreviewOf:
    def test_rows_report_the_real_count_not_the_sample(self) -> None:
        rows = [{"id": index} for index in range(500)]

        preview = gs.preview_of(rows)

        assert preview["kind"] == "rows"
        assert preview["count"] == 500
        assert preview["truncated"] is True
        assert len(preview["rows"]) == gs.PREVIEW_ROWS

    def test_columns_are_collected_across_the_sample(self) -> None:
        """
        A union or a document store can return rows with different keys; reading only the
        first row's columns would silently drop a field.
        """
        preview = gs.preview_of([{"a": 1}, {"b": 2}])

        assert preview["columns"] == ["a", "b"]

    def test_a_long_value_is_trimmed_and_says_so(self) -> None:
        preview = gs.preview_of([{"note": "x" * 5000}])

        rendered = preview["rows"][0]["note"]
        assert len(rendered) < 5000
        assert "5000 characters" in rendered

    def test_a_flat_list_is_a_list_not_rows(self) -> None:
        preview = gs.preview_of([1, 2, 3])

        assert preview["kind"] == "list"
        assert preview["items"] == [1, 2, 3]

    def test_a_dict_is_capped_by_key_count(self) -> None:
        preview = gs.preview_of({str(index): index for index in range(200)})

        assert preview["kind"] == "dict"
        assert preview["count"] == 200
        assert preview["truncated"] is True
        assert len(preview["entries"]) == gs.PREVIEW_KEYS

    def test_none_is_reported_as_empty_rather_than_omitted(self) -> None:
        assert gs.preview_of(None) == {"kind": "empty", "count": 0, "truncated": False}

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_floats_become_text(self, value: float) -> None:
        """
        Valid Python floats and invalid JSON. Postgres refuses them, so leaving one in a
        preview would fail the insert — a successful node recorded as a failed step.
        """
        preview = gs.preview_of([{"v": value}])

        assert isinstance(preview["rows"][0]["v"], str)
        json.dumps(preview)

    def test_dates_and_decimals_survive_as_text(self) -> None:
        preview = gs.preview_of([
            {"when": datetime.date(2026, 1, 2), "amount": decimal.Decimal("1.50")},
        ])

        assert preview["rows"][0]["when"] == "2026-01-02"
        assert preview["rows"][0]["amount"] == "1.50"
        json.dumps(preview)

    def test_a_preview_is_always_a_dict(self) -> None:
        """
        Never a bare list. The column is JSONB and a consumer that has to handle both
        shapes is one that will get a shape wrong.
        """
        for value in ([1, 2], {"a": 1}, "text", 7, None, [{"a": 1}]):
            assert isinstance(gs.preview_of(value), dict)


class TestStatePreview:
    def test_reduces_each_output_to_its_shape(self) -> None:
        """
        Shape rather than content, because a snapshot per node would otherwise repeat every
        earlier node's rows — a ten-node run storing the first node's result ten times.
        """
        rows = [{"id": index} for index in range(40)]

        preview = gs.state_preview({"outputs": {"q": rows}})

        assert preview["outputs"]["q"] == {"kind": "rows", "count": 40}

    def test_caps_how_many_outputs_travel(self) -> None:
        outputs = {f"n{index}": [1] for index in range(80)}

        preview = gs.state_preview({"outputs": outputs})

        assert len(preview["outputs"]) == gs.PREVIEW_STATE_ENTRIES
        assert preview["outputs_truncated"] is True

    def test_reports_a_loop_as_a_position(self) -> None:
        preview = gs.state_preview({"loops": {"l": {"items": [1, 2, 3], "index": 1}}})

        assert preview["loops"]["l"] == {"index": 1, "total": 3}

    def test_is_json_serialisable(self) -> None:
        json.dumps(gs.state_preview({
            "outputs": {"q": [{"when": datetime.date(2026, 1, 1)}]},
            "answers": {"h": True},
            "inputs": {"p": decimal.Decimal("2.5")},
        }))


class TestRowsAndValues:
    def test_rows_of_wraps_plain_values(self) -> None:
        """So a value node holding a list can feed something expecting rows."""
        assert gs.rows_of([1, 2]) == [{"value": 1}, {"value": 2}]

    def test_rows_of_passes_dicts_through(self) -> None:
        assert gs.rows_of([{"a": 1}]) == [{"a": 1}]

    def test_values_of_unwraps_a_single_column(self) -> None:
        """``SELECT id FROM …`` is usable as a filter without an intermediate node."""
        assert gs.values_of([{"id": 1}, {"id": 2}]) == [1, 2]

    def test_values_of_refuses_to_guess_between_columns(self) -> None:
        """
        Returning the first column would build a filter on an arbitrary one of them. The
        caller reports the emptiness rather than acting on a guess.
        """
        assert gs.values_of([{"id": 1, "name": "a"}]) == []

    def test_values_of_leaves_a_flat_list_alone(self) -> None:
        assert gs.values_of([1, 2]) == [1, 2]

    def test_output_of_returns_none_for_a_node_that_has_not_run(self) -> None:
        """
        On a branch the run did not take, a node genuinely has no output — and asking
        about it is an ordinary thing for a condition to do, not an error.
        """
        assert gs.output_of({"outputs": {}}, "ghost") is None
