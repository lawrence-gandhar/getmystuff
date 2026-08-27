"""
The Create File and Download File nodes as the Graph Designer sees them.

Three things are asserted, and each is a refusal an author gets while looking at the box
rather than a failure ten minutes into a run:

* a node must say **which node's rows** to write and **in what format**;
* a Download File node must name a **Create File node on this graph** — a node id rather
  than a typed-in name, for the reason ``_validate_timer_node`` gives: nothing offline can
  prove two boxes spell a name the same way;
* the **chat button settings are refused here**, not ignored. A pipeline has no chat, and a
  field that is accepted and silently dropped is worse than one that is not offered.

Plus the two registry invariants: both types have a runner and both have an entry in the
variable-field table, which their own assertions already check at import — asserted again
here so the failure names this feature rather than arriving as an ImportError somewhere
else.
"""

from __future__ import annotations


import pytest
from litestar.exceptions import HTTPException

from app.models.graph_designer import (
    NODE_CREATE_FILE,
    NODE_DOWNLOAD_FILE,
    NODE_TYPE_VALUES,
)
from app.services.graph_designer import graph_service as svc
from app.services.graph_designer.node_runners import _RUNNERS, referenced_nodes


def node(node_id: str, node_type: str, **data) -> dict:
    return {"id": node_id, "type": node_type, "data": data}


def create_node(node_id: str = "make", **overrides) -> dict:
    data = {
        "label": "Write the CSV",
        "file_format": "csv",
        "file_name": "orders",
        "data": {"source": "node", "source_node": "rows", "path": ""},
    }
    data.update(overrides)
    return node(node_id, NODE_CREATE_FILE, **data)


def download_node(node_id: str = "offer", **overrides) -> dict:
    data = {"label": "Hand it over", "create_file_node": "make"}
    data.update(overrides)
    return node(node_id, NODE_DOWNLOAD_FILE, **data)


def graph(*nodes: dict) -> dict:
    """A runnable drawing: Start, a value node holding the rows, then whatever is given."""
    rows = node("rows", "value", value_kind="array", value_json='[{"a": 1}]')
    ordered = [node("start", "start"), rows, *nodes]

    edges = []
    for before, after in zip(ordered, ordered[1:]):
        edges.append(
            {"source": before["id"], "target": after["id"], "source_port": "default"},
        )

    return {"nodes": ordered, "edges": edges}


class TestAValidPair:
    def test_the_two_blocks_together_save(self) -> None:
        svc.validate_graph(graph(create_node(), download_node()))

    @pytest.mark.parametrize("file_format", ["csv", "xlsx", "txt", "parquet"])
    def test_every_format_is_accepted(self, file_format: str) -> None:
        svc.validate_graph(graph(create_node(file_format=file_format)))

    def test_a_path_into_the_output_is_accepted(self) -> None:
        svc.validate_graph(
            graph(create_node(data={"source": "node", "source_node": "rows", "path": "rows"})),
        )


class TestCreateFileRefusals:
    def test_no_format_is_refused(self) -> None:
        with pytest.raises(HTTPException) as raised:
            svc.validate_graph(graph(create_node(file_format="")))

        assert "Write the CSV" in raised.value.detail

    def test_an_unknown_format_is_refused(self) -> None:
        with pytest.raises(HTTPException):
            svc.validate_graph(graph(create_node(file_format="doc")))

    def test_no_source_node_is_refused(self) -> None:
        with pytest.raises(HTTPException) as raised:
            svc.validate_graph(
                graph(create_node(data={"source": "node", "source_node": ""})),
            )

        assert "which node's rows" in raised.value.detail

    def test_a_source_node_that_is_not_on_the_graph_is_refused(self) -> None:
        with pytest.raises(HTTPException) as raised:
            svc.validate_graph(
                graph(create_node(data={"source": "node", "source_node": "nope"})),
            )

        assert "not on this graph" in raised.value.detail

    def test_a_flow_source_is_refused_on_this_canvas(self) -> None:
        """A graph has no conversation and no agent, so those sources do not exist here."""
        with pytest.raises(HTTPException) as raised:
            svc.validate_graph(
                graph(create_node(data={"source": "variable", "name": "ROWS"})),
            )

        assert "not available in a graph" in raised.value.detail

    def test_a_malformed_path_is_refused_at_the_keyboard(self) -> None:
        with pytest.raises(HTTPException) as raised:
            svc.validate_graph(
                graph(
                    create_node(
                        data={"source": "node", "source_node": "rows", "path": "rows..a"},
                    ),
                ),
            )

        assert "could not be read" in raised.value.detail


class TestDownloadFileRefusals:
    def test_naming_no_node_is_refused(self) -> None:
        with pytest.raises(HTTPException) as raised:
            svc.validate_graph(graph(create_node(), download_node(create_file_node="")))

        assert "which file it hands over" in raised.value.detail

    def test_naming_something_that_is_not_a_create_file_node_is_refused(self) -> None:
        with pytest.raises(HTTPException) as raised:
            svc.validate_graph(
                graph(create_node(), download_node(create_file_node="rows")),
            )

        assert "must point at a Create File node" in raised.value.detail

    def test_naming_a_node_that_is_not_there_is_refused(self) -> None:
        with pytest.raises(HTTPException):
            svc.validate_graph(
                graph(create_node(), download_node(create_file_node="ghost")),
            )

    @pytest.mark.parametrize(
        "button", [{"show_button": True}, {"button_text": "Download"}],
    )
    def test_the_chat_button_settings_are_refused_rather_than_ignored(
        self, button: dict,
    ) -> None:
        """
        A field accepted and silently dropped is worse than one that is not offered: the
        author chose it, and nothing would tell them it did nothing.
        """
        with pytest.raises(HTTPException) as raised:
            svc.validate_graph(graph(create_node(), download_node(**button)))

        assert "no chat to show one in" in raised.value.detail


class TestTheRegistries:
    def test_both_types_are_in_the_vocabulary(self) -> None:
        assert {NODE_CREATE_FILE, NODE_DOWNLOAD_FILE} <= NODE_TYPE_VALUES

    def test_both_types_have_a_runner(self) -> None:
        """
        A type in the vocabulary with no runner would save, validate, compile and then fail
        at the one moment it matters.
        """
        assert NODE_CREATE_FILE in _RUNNERS
        assert NODE_DOWNLOAD_FILE in _RUNNERS

    def test_a_file_name_takes_variables_and_nothing_else_does(self) -> None:
        """
        ``orders-{{RUN_DATE}}`` is why the type has an entry at all — a nightly pipeline
        wants a file per run. The format and the node id do not: one is a picker's value,
        the other a reference the validator resolves before any state exists.
        """
        from app.services.graph_designer.node_variables import fields_for

        assert [spec.key for spec in fields_for(NODE_CREATE_FILE)] == ["file_name"]
        assert fields_for(NODE_DOWNLOAD_FILE) == ()


class TestSelectionHonesty:
    def test_a_create_file_node_declares_the_node_it_reads(self) -> None:
        """
        Without this, testing a selection of just this node would read nothing and fail
        inside the runner claiming the upstream produced no rows — rather than saying the
        upstream was not ticked.
        """
        assert referenced_nodes(create_node()) == {"rows"}

    def test_a_download_file_node_declares_the_node_that_writes_the_file(self) -> None:
        assert referenced_nodes(download_node()) == {"make"}
