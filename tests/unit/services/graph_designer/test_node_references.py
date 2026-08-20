"""
Tests for ``node_runners.referenced_nodes`` and the binding normaliser it reads through.

Small, and load-bearing out of proportion to its size. ``referenced_nodes`` is what makes
a **tested selection** honest: run a middle node on its own and the run refuses, naming the
upstream that is not in the selection, rather than reading ``None`` and reporting a green
tick over nothing. Every reference a node's settings can hold has to be collected here, and
its own docstring says so — a reference kind that skipped it would be invisible.

No LangGraph, and none needed: ``node_runners`` imports the state helpers and the rules,
not the runtime, so these are plain function calls.
"""

from __future__ import annotations

from app.services.graph_designer.graph_service import binding_of, bindings_of
from app.services.graph_designer.node_runners import referenced_nodes


def node(node_id: str, node_type: str, **data) -> dict:
    return {"id": node_id, "type": node_type, "data": data}


class TestBindingOf:
    """One binding, read out of either shape it may have been stored in."""

    def test_a_bare_node_id_means_one_value_and_no_field(self) -> None:
        """
        The shape saved before a binding could say anything else. Its meaning must not
        drift: there are stored graphs using it.
        """
        assert binding_of("n_4") == {"node": "n_4", "field": "", "mode": "one"}

    def test_an_object_carries_its_field_and_mode(self) -> None:
        assert binding_of({"node": "n_4", "field": "id", "mode": "in_list"}) == {
            "node": "n_4", "field": "id", "mode": "in_list",
        }

    def test_a_mode_defaults_to_one_value(self) -> None:
        assert binding_of({"node": "n_4"})["mode"] == "one"

    def test_an_unknown_mode_reads_as_one_value(self) -> None:
        """
        The save refuses it; a runner is not the place to fail a graph over a value that
        could only have got there by hand, and "one value" is the conservative reading.
        """
        assert binding_of({"node": "n_4", "mode": "sideways"})["mode"] == "one"

    def test_anything_naming_no_node_is_nothing(self) -> None:
        """
        "Not wired" and "wired to nothing" are the same thing to every caller, so they get
        the same answer rather than two shapes to handle.
        """
        assert binding_of("") is None
        assert binding_of("   ") is None
        assert binding_of({"node": ""}) is None
        assert binding_of({}) is None
        assert binding_of(None) is None
        assert binding_of(7) is None

    def test_bindings_of_skips_the_empty_ones(self) -> None:
        resolved = bindings_of({"bindings": {
            "a": "n_1",
            "b": {"node": "n_2", "mode": "in_list"},
            "c": "",
        }})

        assert set(resolved) == {"a", "b"}
        assert resolved["b"]["mode"] == "in_list"

    def test_bindings_of_tolerates_a_node_with_none(self) -> None:
        assert bindings_of({}) == {}
        assert bindings_of({"bindings": None}) == {}
        assert bindings_of({"bindings": []}) == {}


class TestReferencedNodes:
    def test_a_wired_parameter_is_a_dependency(self) -> None:
        assert referenced_nodes(node(
            "q", "sql", bindings={"wanted": {"node": "v", "mode": "one"}},
        )) == {"v"}

    def test_a_wired_parameter_in_the_old_shape_is_too(self) -> None:
        """
        The reason ``referenced_nodes`` reads through the normaliser rather than off the
        raw dict: a reader that knew only the new shape would report *no* dependency for a
        stored graph, and a selection test over it would quietly read ``None``.
        """
        assert referenced_nodes(node("q", "sql", bindings={"wanted": "v"})) == {"v"}

    def test_a_loop_depends_on_what_it_walks_and_what_it_collects(self) -> None:
        assert referenced_nodes(node(
            "loop", "for_each", source_node="q", collect_from="body",
        )) == {"q", "body"}

    def test_a_branch_depends_on_every_condition_source(self) -> None:
        assert referenced_nodes(node("b", "branch", conditions=[
            {"source_node": "a"}, {"source_node": "c"}, {"source_node": ""},
        ])) == {"a", "c"}

    def test_a_do_until_depends_on_its_conditions_source(self) -> None:
        assert referenced_nodes(node(
            "loop", "do_until", condition={"source_node": "q"},
        )) == {"q"}

    def test_a_node_never_counts_as_reading_itself(self) -> None:
        """
        A ``do_until`` routinely tests its own cursor, and flagging that as a missing
        dependency would make every such loop untestable in isolation.
        """
        assert referenced_nodes(node(
            "loop", "do_until", condition={"source_node": "loop"},
        )) == set()

    def test_a_node_with_no_references_has_none(self) -> None:
        assert referenced_nodes(node("q", "sql", sql_query="SELECT 1")) == set()
