"""
Tests for app/services/file_delivery/row_source.py.

This module's whole job is that **what reaches the file is everything, or the block fails**,
so most of what is asserted here is a refusal. Each one exists because the alternative is a
file that looks complete and is not:

* a Run Graph block's rows are re-read in full at file time. The stored record holds the
  run's *id* and the real total, never ``GraphOutcome.rows`` — which is a twenty-row
  preview. A file made from the preview would be a twenty-row file with nothing about it
  saying so.
* a truncated AI table is refused rather than written short.
* a list of lists is refused rather than given invented ``column_1..n`` headers, which
  would travel in a file somebody sends on.
* an absent variable is refused rather than written as an empty file: "the flow never set
  it" and "the query matched nothing" are different, and only the first is a wiring
  mistake.
"""

from __future__ import annotations

import json

import pytest

from app.services.file_delivery import file_writer, row_source
from app.services.file_delivery.errors import SourceError

ROWS = [{"id": 1, "name": "one"}, {"id": 2, "name": "two"}]


async def resolve(
    *, data: dict, node_results: dict | None = None, variables: dict | None = None,
) -> row_source.Payload:
    """One flow-side resolution, with the two containers defaulted to empty."""
    return await row_source.resolve_flow_data(
        None,
        user_id=7,
        node_results=node_results or {},
        variables=variables or {},
        data=data,
        block_label="Write the CSV",
    )


class TestAVariableSource:
    async def test_json_rows_become_columns(self) -> None:
        payload = await resolve(
            data={"source": "variable", "name": "ORDER_ROWS"},
            variables={"ORDER_ROWS": json.dumps(ROWS)},
        )

        assert payload.rows == ROWS
        assert payload.row_count == 2

    async def test_a_json_object_is_one_row(self) -> None:
        payload = await resolve(
            data={"source": "variable", "name": "CUSTOMER"},
            variables={"CUSTOMER": json.dumps({"id": 1})},
        )

        assert payload.rows == [{"id": 1}]

    async def test_a_list_of_scalars_gets_one_named_column(self) -> None:
        """A legitimate result — one column of ids — with no column name of its own."""
        payload = await resolve(
            data={"source": "variable", "name": "IDS"},
            variables={"IDS": json.dumps([1, 2, 3])},
        )

        assert payload.rows == [{"value": 1}, {"value": 2}, {"value": 3}]

    async def test_prose_is_text_not_rows(self) -> None:
        """An AI Fallback's answer. TXT can write it; the tabular three refuse it."""
        payload = await resolve(
            data={"source": "variable", "name": "ANSWER"},
            variables={"ANSWER": "We shipped 12 orders."},
        )

        assert payload.rows is None
        assert payload.text == "We shipped 12 orders."

    async def test_a_string_that_only_looks_like_json_stays_text(self) -> None:
        payload = await resolve(
            data={"source": "variable", "name": "ANSWER"},
            variables={"ANSWER": "[not actually json"},
        )

        assert payload.text == "[not actually json"

    async def test_an_absent_variable_is_refused_by_name(self) -> None:
        with pytest.raises(SourceError) as raised:
            await resolve(data={"source": "variable", "name": "ORDER_ROWS"}, variables={})

        assert "{{ORDER_ROWS}}" in raised.value.message
        assert raised.value.block == "Write the CSV"

    async def test_no_variable_chosen_is_refused(self) -> None:
        with pytest.raises(SourceError):
            await resolve(data={"source": "variable", "name": ""})

    async def test_rows_with_no_column_names_are_refused(self) -> None:
        """
        Inventing ``column_1..n`` would put invented headers in a file somebody sends on.
        """
        with pytest.raises(SourceError) as raised:
            await resolve(
                data={"source": "variable", "name": "GRID"},
                variables={"GRID": json.dumps([[1, 2], [3, 4]])},
            )

        assert "column names" in raised.value.message


class TestABlockSource:
    async def test_an_ai_table_becomes_rows(self) -> None:
        payload = await resolve(
            data={"source": "block", "block_id": "ai"},
            node_results={
                "ai": {
                    "kind": "table",
                    "columns": ["order", "qty"],
                    "rows": [["A-1", "3"], ["A-2", "7"]],
                    "truncated": False,
                }
            },
        )

        assert payload.rows == [
            {"order": "A-1", "qty": "3"},
            {"order": "A-2", "qty": "7"},
        ]

    async def test_a_short_row_is_padded_rather_than_dropped(self) -> None:
        """A cell that arrives as None is visible; a row that vanished is not."""
        payload = await resolve(
            data={"source": "block", "block_id": "ai"},
            node_results={
                "ai": {"kind": "table", "columns": ["a", "b"], "rows": [["1"]]},
            },
        )

        assert payload.rows == [{"a": "1", "b": None}]

    async def test_a_truncated_table_is_refused(self) -> None:
        with pytest.raises(SourceError) as raised:
            await resolve(
                data={"source": "block", "block_id": "ai"},
                node_results={
                    "ai": {
                        "kind": "table",
                        "columns": ["a"],
                        "rows": [["1"]],
                        "truncated": True,
                    }
                },
            )

        assert "missing rows" in raised.value.message

    async def test_a_table_with_no_columns_is_refused(self) -> None:
        with pytest.raises(SourceError):
            await resolve(
                data={"source": "block", "block_id": "ai"},
                node_results={"ai": {"kind": "table", "columns": [], "rows": []}},
            )

    async def test_a_block_that_has_not_run_is_refused(self) -> None:
        with pytest.raises(SourceError) as raised:
            await resolve(data={"source": "block", "block_id": "graph"}, node_results={})

        assert "has to run before this block" in raised.value.message

    async def test_no_block_chosen_is_refused(self) -> None:
        with pytest.raises(SourceError):
            await resolve(data={"source": "block", "block_id": ""})

    async def test_an_unknown_record_kind_is_refused_not_guessed_at(self) -> None:
        with pytest.raises(SourceError):
            await resolve(
                data={"source": "block", "block_id": "x"},
                node_results={"x": {"kind": "something-newer"}},
            )


class TestAGraphRunSource:
    async def test_every_row_is_read_back_not_the_preview(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        The distinction this whole module exists for. The stored record says 5,000 rows;
        what is written is what ``full_result`` returns, not what the conversation saw.
        """
        from app.services.graph_designer import graph_runner

        every_row = [{"id": index} for index in range(5000)]

        async def fake_full_result(user_id, run_uuid):  # noqa: ANN001, ANN202
            assert (user_id, run_uuid) == (7, "run-1")
            return every_row

        monkeypatch.setattr(graph_runner, "full_result", fake_full_result)

        payload = await resolve(
            data={"source": "block", "block_id": "graph"},
            node_results={
                "graph": {"kind": "graph_run", "run_id": "run-1", "total_rows": 5000},
            },
        )

        assert payload.row_count == 5000

    async def test_a_result_past_the_ceiling_is_refused_before_it_is_read(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        Refused off the *recorded total*, so an impossible file costs nothing — nothing is
        read back at all, which the exploding stub proves.
        """
        from app.services.graph_designer import graph_runner

        async def explode(user_id, run_uuid):  # noqa: ANN001, ANN202
            raise AssertionError("the rows must not be read when the total is too large")

        monkeypatch.setattr(graph_runner, "full_result", explode)
        monkeypatch.setattr(file_writer, "FILE_MAX_ROWS", 10)

        with pytest.raises(SourceError) as raised:
            await resolve(
                data={"source": "block", "block_id": "graph"},
                node_results={
                    "graph": {"kind": "graph_run", "run_id": "run-1", "total_rows": 99},
                },
            )

        assert "99 rows" in raised.value.message

    async def test_a_run_whose_result_is_gone_is_refused(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from app.services.graph_designer import graph_runner

        async def gone(user_id, run_uuid):  # noqa: ANN001, ANN202
            return None

        monkeypatch.setattr(graph_runner, "full_result", gone)

        with pytest.raises(SourceError) as raised:
            await resolve(
                data={"source": "block", "block_id": "graph"},
                node_results={
                    "graph": {"kind": "graph_run", "run_id": "run-1", "total_rows": 2},
                },
            )

        assert "no longer be read" in raised.value.message


class TestTheGraphCanvas:
    def test_an_earlier_nodes_rows_are_the_payload(self) -> None:
        payload = row_source.resolve_graph_data(
            outputs={"n1": ROWS},
            data={"source": "node", "source_node": "n1", "path": ""},
            node_label_of=lambda node_id: "Read orders",
            block_label="Write the CSV",
        )

        assert payload.rows == ROWS

    def test_a_path_reaches_into_the_output(self) -> None:
        payload = row_source.resolve_graph_data(
            outputs={"n1": {"rows": ROWS, "count": 2}},
            data={"source": "node", "source_node": "n1", "path": "rows"},
            node_label_of=lambda node_id: "Read orders",
        )

        assert payload.rows == ROWS

    def test_a_node_that_has_not_run_is_refused_by_label(self) -> None:
        """A sentence naming "Read orders" is worth more than one naming n4."""
        with pytest.raises(SourceError) as raised:
            row_source.resolve_graph_data(
                outputs={},
                data={"source": "node", "source_node": "n1"},
                node_label_of=lambda node_id: "Read orders",
            )

        assert "Read orders" in raised.value.message

    def test_a_session_source_is_refused_on_this_canvas(self) -> None:
        """A graph has no conversation, so the flow's sources are not available here."""
        with pytest.raises(SourceError) as raised:
            row_source.resolve_graph_data(
                outputs={},
                data={"source": "variable", "name": "X"},
                node_label_of=lambda node_id: node_id,
            )

        assert "a graph cannot provide" in raised.value.message

    def test_a_malformed_path_is_refused(self) -> None:
        with pytest.raises(SourceError):
            row_source.resolve_graph_data(
                outputs={"n1": {"rows": ROWS}},
                data={"source": "node", "source_node": "n1", "path": "rows..id"},
                node_label_of=lambda node_id: node_id,
            )


class TestFlowSourcesAreClosed:
    async def test_a_node_source_is_refused_in_a_conversation(self) -> None:
        """There are no upstream node outputs in a flow — its state is a flat map."""
        with pytest.raises(SourceError) as raised:
            await resolve(data={"source": "node", "source_node": "n1"})

        assert "a conversation cannot provide" in raised.value.message
