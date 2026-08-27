"""
Tests for app/services/canvas_layout/layout_service.py.

This module exists because layout is the part of a drawing that can be wrong without
looking wrong, and because the two canvases that consume it are JavaScript, which this
repository has no way to test. So the properties a reader of a canvas actually depends on
are asserted here as arithmetic:

* every drawn connector points **down** — an edge going sideways or up is a connector that
  crosses the blocks it passes;
* no two blocks share a place;
* a loop is *reported* rather than layered, because there is no top-down layer that can
  hold a cycle and a silently-included one turns every layer below it into a wrong number;
* the same drawing lays out identically twice — a canvas that shuffled itself on each
  reload would be worse than one that never arranged itself at all;
* a block nobody has wired up yet still appears, and appears visibly apart.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from app.services.canvas_layout import layout_service


def nodes(*ids: str) -> list[dict]:
    return [{"id": node_id} for node_id in ids]


def edges(*pairs: tuple[str, str]) -> list[dict]:
    return [{"source": source, "target": target} for source, target in pairs]


def layout(
    node_list: Sequence[Mapping[str, Any]],
    edge_list: Sequence[Mapping[str, Any]],
    entry_ids: Sequence[str] = (),
) -> dict:
    return layout_service.layered_layout(node_list, edge_list, entry_ids)


def layer_of(result: Mapping[str, Any], node_id: str) -> int:
    return result["positions"][node_id]["layer"]


def column_of(result: Mapping[str, Any], node_id: str) -> float:
    return result["positions"][node_id]["column"]


def assert_every_forward_edge_points_down(
    result: Mapping[str, Any], edge_list: Sequence[Mapping[str, Any]],
) -> None:
    """The one property a top-down canvas is unreadable without."""
    positions = result["positions"]

    for index, edge in enumerate(edge_list):
        if index in result["back_edges"]:
            continue
        source, target = edge["source"], edge["target"]
        if source not in positions or target not in positions:
            continue

        assert positions[target]["layer"] > positions[source]["layer"], (
            f"edge {source} -> {target} does not point down"
        )


def assert_nothing_overlaps(result: Mapping[str, Any]) -> None:
    places = [
        (place["layer"], round(place["column"], 6))
        for place in result["positions"].values()
    ]

    assert len(places) == len(set(places))


class TestAStraightChain:
    def test_each_block_sits_one_layer_below_the_last(self) -> None:
        edge_list = edges(("a", "b"), ("b", "c"), ("c", "d"))
        result = layout(nodes("a", "b", "c", "d"), edge_list, ["a"])

        assert [layer_of(result, n) for n in "abcd"] == [0, 1, 2, 3]
        assert_every_forward_edge_points_down(result, edge_list)

    def test_a_chain_is_drawn_as_one_column(self) -> None:
        result = layout(nodes("a", "b", "c"), edges(("a", "b"), ("b", "c")), ["a"])

        assert len({column_of(result, n) for n in "abc"}) == 1

    def test_nothing_is_reported_as_a_loop(self) -> None:
        result = layout(nodes("a", "b"), edges(("a", "b")), ["a"])

        assert result["back_edges"] == []


class TestABranchThatConverges:
    """The shape both real canvases actually have: one block fans out, and several
    branches come back together on a single End block."""

    def test_the_shared_child_is_centred_under_its_parents(self) -> None:
        edge_list = edges(
            ("start", "left"), ("start", "right"), ("left", "end"), ("right", "end"),
        )
        result = layout(nodes("start", "left", "right", "end"), edge_list, ["start"])

        assert layer_of(result, "end") == 2
        assert column_of(result, "end") == (
            column_of(result, "left") + column_of(result, "right")
        ) / 2
        assert_every_forward_edge_points_down(result, edge_list)
        assert_nothing_overlaps(result)

    def test_the_parent_is_centred_over_its_branches(self) -> None:
        result = layout(
            nodes("start", "left", "right"),
            edges(("start", "left"), ("start", "right")),
            ["start"],
        )

        assert column_of(result, "start") == (
            column_of(result, "left") + column_of(result, "right")
        ) / 2

    def test_branches_read_in_the_order_they_were_drawn(self) -> None:
        result = layout(
            nodes("start", "first", "second", "third"),
            edges(("start", "first"), ("start", "second"), ("start", "third")),
            ["start"],
        )

        assert (
            column_of(result, "first")
            < column_of(result, "second")
            < column_of(result, "third")
        )

    def test_six_branches_onto_one_end_block_all_point_down(self) -> None:
        """`Test Generic`'s real shape: three blocks in a chain, each with an error branch
        of its own, and every branch converging on one End block. The End block has to
        clear the deepest of the six — which is the last error branch, one layer below the
        last block of the chain."""
        edge_list = edges(
            ("start", "a"), ("a", "b"), ("b", "c"), ("c", "end"),
            ("a", "e1"), ("e1", "end"),
            ("b", "e2"), ("e2", "end"),
            ("c", "e3"), ("e3", "end"),
        )
        result = layout(
            nodes("start", "a", "b", "c", "e1", "e2", "e3", "end"), edge_list, ["start"],
        )

        assert layer_of(result, "e3") == layer_of(result, "c") + 1
        assert layer_of(result, "end") == layer_of(result, "e3") + 1
        assert_every_forward_edge_points_down(result, edge_list)
        assert_nothing_overlaps(result)


class TestALongerBranchDecidesTheLayer:
    def test_a_shared_child_clears_the_deepest_parent(self) -> None:
        """Longest path, not shortest. On the shortest, the two-hop branch's edge would
        run past a block sitting on the layer it skips."""
        edge_list = edges(
            ("start", "short"), ("start", "long_a"), ("long_a", "long_b"),
            ("short", "join"), ("long_b", "join"),
        )
        result = layout(
            nodes("start", "short", "long_a", "long_b", "join"), edge_list, ["start"],
        )

        assert layer_of(result, "join") == 3
        assert_every_forward_edge_points_down(result, edge_list)


class TestALoop:
    def test_the_edge_that_closes_it_is_reported_and_not_layered(self) -> None:
        """A Goto's return jump. Layered, it would claim the menu sits below the block
        that jumps back to it, which is the opposite of what the drawing means."""
        edge_list = edges(("start", "menu"), ("menu", "goto"), ("goto", "menu"))
        result = layout(nodes("start", "menu", "goto"), edge_list, ["start"])

        assert result["back_edges"] == [2]
        assert layer_of(result, "menu") == 1
        assert layer_of(result, "goto") == 2
        assert_every_forward_edge_points_down(result, edge_list)

    def test_a_closed_loop_with_no_entry_is_still_drawn(self) -> None:
        edge_list = edges(("a", "b"), ("b", "c"), ("c", "a"))
        result = layout(nodes("a", "b", "c"), edge_list)

        assert len(result["positions"]) == 3
        assert len(result["back_edges"]) == 1
        assert_every_forward_edge_points_down(result, edge_list)
        assert_nothing_overlaps(result)

    def test_a_block_pointing_at_itself_is_a_loop_of_one(self) -> None:
        result = layout(nodes("a"), edges(("a", "a")), ["a"])

        assert result["back_edges"] == [0]
        assert layer_of(result, "a") == 0

    def test_a_loop_off_to_one_side_is_found_too(self) -> None:
        """The walk covers every block, not only the ones the Start reaches — an
        unwalked cycle would send the layering to its pass bound with meaningless
        numbers."""
        edge_list = edges(("start", "a"), ("x", "y"), ("y", "x"))
        result = layout(nodes("start", "a", "x", "y"), edge_list, ["start"])

        assert len(result["back_edges"]) == 1
        assert_every_forward_edge_points_down(result, edge_list)


class TestABlockNobodyHasWiredUp:
    def test_it_is_placed_rather_than_dropped(self) -> None:
        result = layout(nodes("start", "a", "loose"), edges(("start", "a")), ["start"])

        assert "loose" in result["positions"]

    def test_it_is_put_beside_the_chain_and_not_inside_it(self) -> None:
        result = layout(nodes("start", "a", "loose"), edges(("start", "a")), ["start"])
        chain = {column_of(result, "start"), column_of(result, "a")}

        assert column_of(result, "loose") not in chain
        assert column_of(result, "loose") > max(chain)

    def test_the_chain_holding_the_start_comes_first(self) -> None:
        """Even when the loose block was added to the document first."""
        result = layout(nodes("loose", "start", "a"), edges(("start", "a")), ["start"])

        assert column_of(result, "start") < column_of(result, "loose")

    def test_a_detached_pair_keeps_its_own_shape(self) -> None:
        edge_list = edges(("start", "a"), ("x", "y"))
        result = layout(nodes("start", "a", "x", "y"), edge_list, ["start"])

        assert layer_of(result, "x") == 0
        assert layer_of(result, "y") == 1
        assert column_of(result, "x") == column_of(result, "y")
        assert_nothing_overlaps(result)

    def test_a_block_joined_only_by_a_return_jump_is_not_stranded(self) -> None:
        """Connectivity is read undirected and includes the back edge, so a Goto's
        target counts as part of the chain it jumps into."""
        result = layout(
            nodes("start", "a", "jumper"),
            edges(("start", "a"), ("jumper", "a")),
            ["start"],
        )

        assert column_of(result, "jumper") - column_of(result, "a") < (
            1.0 + layout_service.COMPONENT_GAP
        )


class TestTheSameDrawingTwice:
    def test_it_lays_out_identically(self) -> None:
        node_list = nodes("start", "a", "b", "c", "d", "e")
        edge_list = edges(
            ("start", "a"), ("start", "b"), ("a", "c"), ("b", "c"),
            ("c", "d"), ("d", "e"), ("e", "a"),
        )

        first = layout(node_list, edge_list, ["start"])
        second = layout(node_list, edge_list, ["start"])

        assert first == second

    def test_two_blocks_swapped_in_the_document_do_not_reshuffle_the_rest(self) -> None:
        """Only the two that moved may change column, because the walk orders branches
        by the order their connectors were drawn."""
        edge_list = edges(("start", "a"), ("start", "b"))
        forwards = layout(nodes("start", "a", "b"), edge_list, ["start"])
        backwards = layout(nodes("start", "b", "a"), edge_list, ["start"])

        assert column_of(forwards, "start") == column_of(backwards, "start")
        assert column_of(forwards, "a") == column_of(backwards, "a")


class TestAGraphMidEdit:
    """A canvas posts what it is holding, which for a moment can be malformed. Every one
    of these costs one node or one edge its place in the picture, never the picture."""

    def test_no_nodes_is_an_empty_answer_rather_than_an_error(self) -> None:
        assert layout([], edges(("a", "b"))) == {"positions": {}, "back_edges": []}

    def test_an_edge_to_a_deleted_block_is_ignored(self) -> None:
        result = layout(nodes("a", "b"), edges(("a", "b"), ("b", "gone")), ["a"])

        assert set(result["positions"]) == {"a", "b"}
        assert result["back_edges"] == []

    def test_a_node_with_no_id_is_skipped(self) -> None:
        result = layout([{"id": "a"}, {"id": ""}, {}], [], ["a"])

        assert set(result["positions"]) == {"a"}

    def test_the_same_id_twice_is_placed_once(self) -> None:
        result = layout([{"id": "a"}, {"id": "a"}], [], ["a"])

        assert set(result["positions"]) == {"a"}

    def test_two_connectors_between_one_pair_do_not_pull_twice(self) -> None:
        doubled = layout(
            nodes("start", "a", "b"),
            edges(("start", "a"), ("start", "a"), ("start", "b")),
            ["start"],
        )
        single = layout(
            nodes("start", "a", "b"),
            edges(("start", "a"), ("start", "b")),
            ["start"],
        )

        assert doubled["positions"] == single["positions"]

    def test_an_entry_that_is_not_on_the_canvas_falls_back(self) -> None:
        result = layout(nodes("a", "b"), edges(("a", "b")), ["deleted-start"])

        assert layer_of(result, "a") == 0
        assert layer_of(result, "b") == 1

    def test_a_missing_start_falls_back_to_what_nothing_points_at(self) -> None:
        result = layout(nodes("a", "b"), edges(("a", "b")))

        assert layer_of(result, "a") == 0


class TestACanvasAtTheNodeCap:
    def test_five_hundred_blocks_in_a_chain_lay_out(self) -> None:
        """`MAX_GRAPH_NODES` is 500, so this is the largest drawing that can be saved.
        A chain is the worst case for the layering relaxation."""
        ids = [f"n{index}" for index in range(500)]
        edge_list = edges(*zip(ids, ids[1:]))
        result = layout(nodes(*ids), edge_list, [ids[0]])

        assert layer_of(result, ids[-1]) == 499
        assert_every_forward_edge_points_down(result, edge_list)

    def test_a_wide_fan_does_not_overlap(self) -> None:
        ids = [f"n{index}" for index in range(200)]
        result = layout(
            nodes("start", *ids),
            edges(*[("start", node_id) for node_id in ids]),
            ["start"],
        )

        assert_nothing_overlaps(result)
