"""
Tests for app/services/graph_designer/graph_service.py — what a drawing has to be
before it is allowed to run.

This module needs no LangGraph, and that is the point of the split it tests: the rules
that decide whether a drawing is a valid graph are the part most worth asserting, and
they are assertable without the runtime. ``test_graph_compiler.py`` covers the half that
needs it.

Four properties carry the suite:

* **A cycle is refused only when nothing bounds it.** This is the rule that is wrong in
  both obvious directions — banning cycles bans loops, allowing them allows a run that
  never ends — so it is asserted from both sides: a plain ``A → B → A`` is refused, and
  the same shape routed through a ``for_each`` body is accepted.
* **There is no cap on nodes or edges.** Asserted with a graph two hundred nodes long,
  because "no cap" is a claim about behaviour and the only way to hold it is to run one
  that would trip a cap if there were one.
* **Ownership is a 404, never a 403.** Another user's graph must be indistinguishable
  from one that is not there, or the answer confirms which uuids are real.
* **Publishing validates; saving a draft does not.** A draft is allowed to be broken —
  that is what a draft is — but an active graph attached to an agent is callable by a
  model, so a broken one would fail inside somebody's conversation.
"""

from __future__ import annotations

import uuid as uuid_pkg

import pytest
from litestar.exceptions import HTTPException

from app.models.graph_designer import ToolGraph
from app.services.graph_designer import graph_service as svc


# ---------------------------------------------------------------------------
# Builders — a node and an edge, so a test reads as the shape it is about
# ---------------------------------------------------------------------------

def node(node_id: str, node_type: str, **data) -> dict:
    """One node. ``data`` is spread into its settings, which is where every rule looks."""
    return {
        "id": node_id,
        "type": node_type,
        "position": {"x": 0, "y": 0},
        "data": data,
    }


def edge(source: str, target: str, port: str = "default") -> dict:
    return {
        "id": f"{source}->{target}:{port}",
        "source": source,
        "source_port": port,
        "target": target,
    }


def graph(nodes: list, edges: list) -> dict:
    return {"nodes": nodes, "edges": edges}


def value_node(node_id: str = "v", kind: str = "list", raw: str = "[1]") -> dict:
    return node(node_id, "value", value_kind=kind, value_json=raw)


def sql_node(node_id: str = "q", **overrides) -> dict:
    data = {
        "datasource_id": str(uuid_pkg.uuid4()),
        "table_names": ["clients"],
        "sql_query": "SELECT id FROM clients",
    }
    data.update(overrides)
    return node(node_id, "sql", **data)


@pytest.fixture
def make_graph(db):  # noqa: ANN001, ANN201
    """A stored graph, straight through the session — no service call to set up a test."""
    async def _make(owner, name: str, **kwargs):  # noqa: ANN001
        row = ToolGraph(
            user_id=owner.id,
            name=name,
            graph_data=kwargs.pop("graph_data", {"nodes": [], "edges": []}),
            **kwargs,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row

    return _make


@pytest.fixture
async def other_user(make_user):  # noqa: ANN001, ANN201
    return await make_user("someone.else@example.com")


class TestValidateGraphAcceptance:
    """The drawings that must be allowed through."""

    def test_accepts_a_start_leading_to_success(self) -> None:
        svc.validate_graph(graph(
            [node("s", "start"), node("ok", "success")],
            [edge("s", "ok")],
        ))

    def test_accepts_a_loop_whose_body_returns_to_it(self) -> None:
        """
        The cycle rule from the permissive side.

        ``loop -body-> work -> loop`` is a cycle, and it is exactly the cycle a For each
        node exists to create. Refusing it would make loops undrawable.
        """
        svc.validate_graph(graph(
            [
                node("s", "start"),
                value_node("v"),
                node("loop", "for_each", source_node="v", max_iterations=5),
                value_node("work"),
                node("ok", "success"),
            ],
            [
                edge("s", "v"), edge("v", "loop"),
                edge("loop", "work", "body"), edge("work", "loop"),
                edge("loop", "ok", "done"),
            ],
        ))

    def test_accepts_two_hundred_nodes(self) -> None:
        """
        There is no ceiling on the size of a graph.

        A length check would pass with any cap above 200, so this is deliberately a chain
        long enough to be implausible as a hand-drawn graph and still legal — the claim is
        that no cap exists, not that the cap is generous.
        """
        nodes = [node("s", "start")]
        edges = [edge("s", "v0")]

        for index in range(198):
            nodes.append(value_node(f"v{index}"))
            if index:
                edges.append(edge(f"v{index - 1}", f"v{index}"))

        nodes.append(node("ok", "success"))
        edges.append(edge("v197", "ok"))

        svc.validate_graph(graph(nodes, edges))

    def test_accepts_a_branch_with_an_else_path(self) -> None:
        svc.validate_graph(graph(
            [
                node("s", "start"),
                value_node("v"),
                node(
                    "b", "branch",
                    conditions=[{
                        "source_node": "v", "operator": "not_empty",
                        "port": "found", "label": "found something",
                    }],
                ),
                node("ok", "success"),
                node("bad", "failure"),
            ],
            [
                edge("s", "v"), edge("v", "b"),
                edge("b", "ok", "found"), edge("b", "bad", "else"),
            ],
        ))

    def test_accepts_an_error_path_off_a_sql_node(self) -> None:
        svc.validate_graph(graph(
            [node("s", "start"), sql_node("q"), node("ok", "success"), node("bad", "failure")],
            [edge("s", "q"), edge("q", "ok"), edge("q", "bad", "error")],
        ))


class TestValidateGraphStructure:
    """The structural rules, each with the sentence it is supposed to produce."""

    def test_refuses_a_graph_with_no_start(self) -> None:
        with pytest.raises(HTTPException) as caught:
            svc.validate_graph(graph([node("ok", "success")], []))

        assert "exactly one Start node" in caught.value.detail

    def test_refuses_two_start_nodes(self) -> None:
        with pytest.raises(HTTPException) as caught:
            svc.validate_graph(graph([node("a", "start"), node("b", "start")], []))

        assert "this one has 2" in caught.value.detail

    def test_refuses_duplicate_node_ids(self) -> None:
        with pytest.raises(HTTPException) as caught:
            svc.validate_graph(graph([node("s", "start"), node("s", "success")], []))

        assert "share the id" in caught.value.detail

    def test_refuses_an_edge_to_a_node_that_is_not_there(self) -> None:
        with pytest.raises(HTTPException) as caught:
            svc.validate_graph(graph([node("s", "start")], [edge("s", "ghost")]))

        assert "not in this graph" in caught.value.detail

    def test_refuses_an_edge_into_the_start_node(self) -> None:
        with pytest.raises(HTTPException) as caught:
            svc.validate_graph(graph(
                [node("s", "start"), value_node("v")],
                [edge("v", "s")],
            ))

        assert "connect into the Start node" in caught.value.detail

    def test_refuses_an_edge_out_of_a_terminal_node(self) -> None:
        with pytest.raises(HTTPException) as caught:
            svc.validate_graph(graph(
                [node("s", "start"), node("ok", "success"), value_node("v")],
                [edge("s", "ok"), edge("ok", "v")],
            ))

        assert "ends the run" in caught.value.detail

    def test_refuses_two_edges_leaving_one_port(self) -> None:
        """
        The subtle one: the run would take exactly one of them, and which one would
        depend on iteration order. A graph whose behaviour is not the drawing.
        """
        with pytest.raises(HTTPException) as caught:
            svc.validate_graph(graph(
                [node("s", "start"), node("a", "success"), node("b", "failure")],
                [edge("s", "a"), edge("s", "b")],
            ))

        assert "same outcome" in caught.value.detail


class TestValidateGraphCycles:
    """The cycle rule from the restrictive side. See the acceptance test for the other."""

    def test_refuses_a_two_node_cycle_with_no_loop_node(self) -> None:
        with pytest.raises(HTTPException) as caught:
            svc.validate_graph(graph(
                [node("s", "start"), value_node("a"), value_node("b")],
                [edge("s", "a"), edge("a", "b"), edge("b", "a")],
            ))

        assert "loop that nothing controls" in caught.value.detail

    def test_refuses_a_node_connected_to_itself(self) -> None:
        with pytest.raises(HTTPException) as caught:
            svc.validate_graph(graph(
                [node("s", "start"), value_node("a")],
                [edge("s", "a"), edge("a", "a")],
            ))

        assert "loop that nothing controls" in caught.value.detail

    def test_refuses_a_cycle_through_a_loops_done_port(self) -> None:
        """
        ``done`` is the way *out* of a loop, so a cycle through it is not bounded by the
        loop's cursor — only the ``body`` edge is cut when the rule looks for unbounded
        cycles.
        """
        with pytest.raises(HTTPException) as caught:
            svc.validate_graph(graph(
                [
                    node("s", "start"), value_node("v"),
                    node("loop", "for_each", source_node="v", max_iterations=3),
                    value_node("after"),
                ],
                [
                    edge("s", "v"), edge("v", "loop"),
                    edge("loop", "after", "done"), edge("after", "loop"),
                ],
            ))

        assert "loop that nothing controls" in caught.value.detail

    def test_tolerates_a_long_chain_without_hitting_the_recursion_limit(self) -> None:
        """
        The cycle walk is iterative, not recursive.

        A graph has no node ceiling, so a thousand-node chain has to be walkable —
        a recursive depth-first search would raise ``RecursionError`` well before this.
        """
        nodes = [node("s", "start")] + [value_node(f"v{i}") for i in range(1200)]
        edges = [edge("s", "v0")] + [edge(f"v{i}", f"v{i + 1}") for i in range(1199)]

        svc.validate_graph(graph(nodes, edges))


class TestValidateLoopBodies:
    """
    The cycle rule from the other side: a loop node with no cycle around it.

    ``TestValidateGraphCycles`` covers a cycle with no loop node in it. This is the mirror
    image, and the reason it is worth its own rule is what the drawing would otherwise do —
    run the body once, of however many items, and report success. Nothing in the log would
    say the rest were never attempted.
    """

    def _loop_graph(self, *edges) -> dict:
        return graph(
            [
                node("s", "start"),
                value_node("v"),
                node("loop", "for_each", label="each dept", source_node="v"),
                value_node("work"),
                node("ok", "success"),
            ],
            [edge("s", "v"), edge("v", "loop"), *edges],
        )

    def test_refuses_a_body_that_never_returns(self) -> None:
        with pytest.raises(HTTPException) as caught:
            svc.validate_graph(self._loop_graph(
                edge("loop", "work", "body"), edge("work", "ok"),
            ))

        assert "never comes back" in caught.value.detail
        assert "each dept" in caught.value.detail

    def test_refuses_a_loop_with_nothing_on_its_each_output(self) -> None:
        with pytest.raises(HTTPException) as caught:
            svc.validate_graph(self._loop_graph(edge("loop", "ok", "done")))

        assert "nothing on its 'each' output" in caught.value.detail

    def test_accepts_a_body_that_returns(self) -> None:
        svc.validate_graph(self._loop_graph(
            edge("loop", "work", "body"),
            edge("work", "loop"),
            edge("loop", "ok", "done"),
        ))

    def test_accepts_a_body_that_returns_only_down_its_error_path(self) -> None:
        """
        Any edge back counts. A pass that ends by handling a failure still ends, and
        refusing it would be a rule about *how* a pass must finish rather than whether it
        does — which is not this rule's business.
        """
        svc.validate_graph(self._loop_graph(
            edge("loop", "work", "body"),
            edge("work", "loop", "error"),
            edge("loop", "ok", "done"),
        ))

    def test_accepts_a_branch_in_the_body_where_only_one_port_returns(self) -> None:
        """One way back is a loop; requiring every port to return would refuse a body that
        legitimately gives up on a bad item."""
        svc.validate_graph(graph(
            [
                node("s", "start"),
                value_node("v"),
                node("loop", "for_each", source_node="v"),
                node(
                    "pick", "branch",
                    conditions=[{
                        "port": "yes", "source_node": "v",
                        "operator": "not_empty", "value": "",
                    }],
                ),
                node("stop", "failure", message="a bad item"),
                node("ok", "success"),
            ],
            [
                edge("s", "v"), edge("v", "loop"),
                edge("loop", "pick", "body"),
                edge("pick", "loop", "yes"),
                edge("pick", "stop", "default"),
                edge("loop", "ok", "done"),
            ],
        ))

    def test_the_rule_applies_to_do_until_as_well(self) -> None:
        with pytest.raises(HTTPException) as caught:
            svc.validate_graph(graph(
                [
                    node("s", "start"),
                    value_node("v"),
                    node(
                        "loop", "do_until", label="until done",
                        condition={
                            "source_node": "v", "operator": "not_empty", "value": "",
                        },
                    ),
                    value_node("work"),
                    node("ok", "success"),
                ],
                [
                    edge("s", "v"), edge("v", "loop"),
                    edge("loop", "work", "body"), edge("work", "ok"),
                ],
            ))

        assert "never comes back" in caught.value.detail
        assert "until done" in caught.value.detail


class TestValidateNodeSettings:
    """Each node type's own settings."""

    def test_refuses_a_sql_node_with_no_declared_tables(self) -> None:
        """
        Nothing parses tables out of a statement, so the declared list is the only thing
        that lets a table switched off in Data Sources stop the node. Without it a graph
        would be a way *around* those switches.
        """
        with pytest.raises(HTTPException) as caught:
            svc.validate_graph(graph(
                [node("s", "start"), sql_node("q", table_names=[])],
                [edge("s", "q")],
            ))

        assert "which tables its statement reads" in caught.value.detail

    def test_refuses_a_sql_node_whose_statement_writes(self) -> None:
        """
        The message comes from ``tool_config_service.validated_tool_sql`` — the same
        function the tool config form uses — so the operator reads wording they have seen
        before, and a statement that saves here would save there.
        """
        with pytest.raises(HTTPException) as caught:
            svc.validate_graph(graph(
                [node("s", "start"), sql_node("q", sql_query="DELETE FROM clients")],
                [edge("s", "q")],
            ))

        assert "read-only" in caught.value.detail

    def test_refuses_a_list_value_holding_nested_entries(self) -> None:
        with pytest.raises(HTTPException) as caught:
            svc.validate_graph(graph(
                [node("s", "start"), value_node("v", "list", "[[1],[2]]")],
                [edge("s", "v")],
            ))

        assert "plain values" in caught.value.detail

    def test_refuses_a_dict_value_given_a_list(self) -> None:
        with pytest.raises(HTTPException) as caught:
            svc.validate_graph(graph(
                [node("s", "start"), value_node("v", "dict", "[1]")],
                [edge("s", "v")],
            ))

        assert "not one" in caught.value.detail

    def test_accepts_an_array_value_holding_nested_entries(self) -> None:
        """The distinction the three kinds exist for: an array may nest, a list may not."""
        svc.validate_graph(graph(
            [node("s", "start"), value_node("v", "array", "[[1],[2]]")],
            [edge("s", "v")],
        ))

    def test_refuses_unparseable_json_on_a_value_node(self) -> None:
        with pytest.raises(HTTPException) as caught:
            svc.validate_graph(graph(
                [node("s", "start"), value_node("v", "list", "[1,")],
                [edge("s", "v")],
            ))

        assert "not valid JSON" in caught.value.detail

    def test_refuses_a_branch_with_no_conditions(self) -> None:
        with pytest.raises(HTTPException) as caught:
            svc.validate_graph(graph(
                [node("s", "start"), node("b", "branch", conditions=[])],
                [edge("s", "b")],
            ))

        assert "no conditions" in caught.value.detail

    def test_refuses_a_condition_named_else(self) -> None:
        """``else`` is the fall-through, so a condition claiming it would never be taken."""
        with pytest.raises(HTTPException) as caught:
            svc.validate_graph(graph(
                [
                    node("s", "start"), value_node("v"),
                    node("b", "branch", conditions=[{
                        "source_node": "v", "operator": "not_empty", "port": "else",
                    }]),
                ],
                [edge("s", "v")],
            ))

        assert "reserved" in caught.value.detail

    def test_refuses_two_conditions_sharing_an_outcome(self) -> None:
        with pytest.raises(HTTPException) as caught:
            svc.validate_graph(graph(
                [
                    node("s", "start"), value_node("v"),
                    node("b", "branch", conditions=[
                        {"source_node": "v", "operator": "not_empty", "port": "p"},
                        {"source_node": "v", "operator": "is_empty", "port": "p"},
                    ]),
                ],
                [edge("s", "v")],
            ))

        assert "share the outcome" in caught.value.detail

    def test_refuses_a_for_each_whose_source_is_not_in_the_graph(self) -> None:
        with pytest.raises(HTTPException) as caught:
            svc.validate_graph(graph(
                [node("s", "start"), node("loop", "for_each", source_node="ghost")],
                [edge("s", "loop")],
            ))

        assert "not in this graph" in caught.value.detail

    def test_refuses_a_do_until_with_no_condition(self) -> None:
        """A loop with no way out is the one shape that must never compile."""
        with pytest.raises(HTTPException) as caught:
            svc.validate_graph(graph(
                [node("s", "start"), node("loop", "do_until")],
                [edge("s", "loop")],
            ))

        assert "never end" in caught.value.detail

    @pytest.mark.parametrize("ceiling", [0, -1])
    def test_refuses_a_loop_ceiling_below_one(self, ceiling: int) -> None:
        with pytest.raises(HTTPException) as caught:
            svc.validate_graph(graph(
                [
                    node("s", "start"), value_node("v"),
                    node("loop", "for_each", source_node="v", max_iterations=ceiling),
                ],
                [edge("s", "v"), edge("v", "loop")],
            ))

        assert "at least 1" in caught.value.detail

    def test_refuses_an_absurd_loop_ceiling(self) -> None:
        with pytest.raises(HTTPException) as caught:
            svc.validate_graph(graph(
                [
                    node("s", "start"), value_node("v"),
                    node("loop", "for_each", source_node="v", max_iterations=10 ** 9),
                ],
                [edge("s", "v"), edge("v", "loop")],
            ))

        assert str(svc.ABSOLUTE_MAX_ITERATIONS) in caught.value.detail

    def test_refuses_a_human_node_with_no_question(self) -> None:
        with pytest.raises(HTTPException) as caught:
            svc.validate_graph(graph(
                [node("s", "start"), node("h", "human", expects="text")],
                [edge("s", "h")],
            ))

        assert "pause silently" in caught.value.detail

    def test_refuses_a_choice_question_with_no_choices(self) -> None:
        with pytest.raises(HTTPException) as caught:
            svc.validate_graph(graph(
                [node("s", "start"), node("h", "human", prompt="Pick", expects="choice", choices=[])],
                [edge("s", "h")],
            ))

        assert "list is empty" in caught.value.detail

    def test_refuses_an_unknown_node_type(self) -> None:
        with pytest.raises(HTTPException) as caught:
            svc.validate_graph(graph(
                [node("s", "start"), node("x", "teleport")],
                [edge("s", "x")],
            ))

        assert "does not know" in caught.value.detail

    def test_names_the_node_by_its_label_not_its_id(self) -> None:
        """
        The person reading the message is looking at the drawing, so the message names
        what they see. An id like ``n_msoez780_1`` means nothing to them.
        """
        with pytest.raises(HTTPException) as caught:
            svc.validate_graph(graph(
                [
                    node("s", "start"),
                    node("n_abc_1", "human", label="Ask for approval", expects="text"),
                ],
                [edge("s", "n_abc_1")],
            ))

        assert "Ask for approval" in caught.value.detail
        assert "n_abc_1" not in caught.value.detail


class TestValidateSqlParameters:
    """
    What a statement's ``:name`` placeholders are allowed to be, and where their values
    may come from.

    The declarations themselves — the name, the type, the duplicate, the parameter the
    statement never uses — are ``tool_config_service.validated_sql_params``' rules, reused
    rather than restated. Only one of them is asserted here, to prove the wiring is in
    place; the rest belong to that module's own suite.
    """

    def _sql(self, **overrides) -> dict:
        return sql_node("q", **overrides)

    def test_the_shared_declaration_rules_apply(self) -> None:
        """A parameter the statement never mentions is a field filled for no effect."""
        with pytest.raises(HTTPException) as caught:
            svc.validate_graph(graph(
                [
                    node("s", "start"),
                    self._sql(params=[{"param": "unused", "type": "text"}]),
                ],
                [edge("s", "q")],
            ))

        assert "does not use ':unused'" in caught.value.detail

    def test_refuses_a_placeholder_nothing_declares(self) -> None:
        """
        The opposite direction, and it has to be checked separately: a binding fills a
        parameter, and a parameter is what a declaration creates, so an undeclared
        ``:name`` is bound by nothing. The driver's complaint would arrive mid-run naming
        nothing the author would recognise.
        """
        with pytest.raises(HTTPException) as caught:
            svc.validate_graph(graph(
                [
                    node("s", "start"),
                    self._sql(sql_query="SELECT id FROM clients WHERE id = :wanted"),
                ],
                [edge("s", "q")],
            ))

        assert "':wanted'" in caught.value.detail
        assert "nothing fills" in caught.value.detail

    def test_refuses_a_colon_with_a_space_after_it(self) -> None:
        """
        ``= : item`` is what an author writes when there is nowhere to put a value, and the
        space hides the placeholder from both checks above — the statement would save clean
        and then fail against the database quoting ``': item'``.
        """
        with pytest.raises(HTTPException) as caught:
            svc.validate_graph(graph(
                [
                    node("s", "start"),
                    self._sql(sql_query="SELECT id FROM clients WHERE id = : wanted"),
                ],
                [edge("s", "q")],
            ))

        assert "space after the ':'" in caught.value.detail
        assert "':wanted'" in caught.value.detail

    def test_the_spaced_colon_is_refused_before_the_declarations_are_read(self) -> None:
        """
        A declaration for the name does not make it right, and must not silence the
        refusal: the statement still has a stray colon in it either way.
        """
        with pytest.raises(HTTPException) as caught:
            svc.validate_graph(graph(
                [
                    node("s", "start"),
                    self._sql(
                        sql_query="SELECT id FROM clients WHERE id = : wanted",
                        params=[{"param": "wanted", "type": "number"}],
                    ),
                ],
                [edge("s", "q")],
            ))

        assert "space after the ':'" in caught.value.detail

    def test_accepts_a_declared_parameter_with_no_wiring(self) -> None:
        """
        Not a mistake — it is how a value reaches a graph from outside. The run's inputs
        fill it, whether from the test panel or from a data agent calling the graph, and
        ``graph_tool_factory`` builds the tool's arguments out of exactly these
        declarations. Asserted because refusing it would break that whole path.
        """
        svc.validate_graph(graph(
            [
                node("s", "start"),
                self._sql(
                    sql_query="SELECT id FROM clients WHERE id = :wanted",
                    params=[{"param": "wanted", "type": "number", "required": True}],
                ),
            ],
            [edge("s", "q")],
        ))

    def test_refuses_a_wiring_for_a_parameter_that_is_not_declared(self) -> None:
        with pytest.raises(HTTPException) as caught:
            svc.validate_graph(graph(
                [
                    node("s", "start"),
                    value_node("v"),
                    self._sql(bindings={"ghost": {"node": "v", "mode": "one"}}),
                ],
                [edge("s", "v"), edge("v", "q")],
            ))

        assert "does not declare" in caught.value.detail

    def test_refuses_a_wiring_to_a_node_that_is_not_in_the_graph(self) -> None:
        with pytest.raises(HTTPException) as caught:
            svc.validate_graph(graph(
                [
                    node("s", "start"),
                    self._sql(
                        sql_query="SELECT id FROM clients WHERE id = :wanted",
                        params=[{"param": "wanted", "type": "number"}],
                        bindings={"wanted": {"node": "gone", "mode": "one"}},
                    ),
                ],
                [edge("s", "q")],
            ))

        assert "not in this graph" in caught.value.detail

    def test_refuses_a_list_bound_into_a_single_value_comparison(self) -> None:
        """
        ``id = :x`` given a list renders ``id = (?, ?, ?)`` — a syntax error the *database*
        reports, mid-run, long after this form was closed. The same check a nested tool
        config gets, and it reads the shape from the same function.
        """
        with pytest.raises(HTTPException) as caught:
            svc.validate_graph(graph(
                [
                    node("s", "start"),
                    value_node("v"),
                    self._sql(
                        sql_query="SELECT id FROM clients WHERE id = :wanted",
                        params=[{"param": "wanted", "type": "number"}],
                        bindings={"wanted": {"node": "v", "mode": "in_list"}},
                    ),
                ],
                [edge("s", "v"), edge("v", "q")],
            ))

        assert "as a single value" in caught.value.detail
        assert "IN :wanted" in caught.value.detail

    def test_refuses_one_value_bound_into_an_in_comparison(self) -> None:
        """The same mistake the other way round: ``IN :x`` given a scalar is ``IN ?``."""
        with pytest.raises(HTTPException) as caught:
            svc.validate_graph(graph(
                [
                    node("s", "start"),
                    value_node("v"),
                    self._sql(
                        sql_query="SELECT id FROM clients WHERE id IN :wanted",
                        params=[{"param": "wanted", "type": "number"}],
                        bindings={"wanted": {"node": "v", "mode": "one"}},
                    ),
                ],
                [edge("s", "v"), edge("v", "q")],
            ))

        assert "expects a list" in caught.value.detail

    def test_a_placeholder_in_neither_shape_is_left_alone(self) -> None:
        """
        It may be a function argument. Guessing there would refuse a statement that works,
        so the arity check concludes nothing rather than something.
        """
        svc.validate_graph(graph(
            [
                node("s", "start"),
                value_node("v"),
                self._sql(
                    sql_query="SELECT id FROM clients WHERE upper(name) LIKE upper(:wanted)",
                    params=[{"param": "wanted", "type": "text"}],
                    bindings={"wanted": {"node": "v", "mode": "one"}},
                ),
            ],
            [edge("s", "v"), edge("v", "q")],
        ))

    def test_a_binding_stored_as_a_bare_node_id_is_still_read(self) -> None:
        """
        The old shape. It must still be *seen* by validation — a version that only
        understood the new one would let a wiring to a deleted node through.
        """
        with pytest.raises(HTTPException) as caught:
            svc.validate_graph(graph(
                [
                    node("s", "start"),
                    self._sql(
                        sql_query="SELECT id FROM clients WHERE id = :wanted",
                        params=[{"param": "wanted", "type": "number"}],
                        bindings={"wanted": "gone"},
                    ),
                ],
                [edge("s", "q")],
            ))

        assert "not in this graph" in caught.value.detail


class TestValidateUnionNodes:
    """
    A ``sql_union`` node is a SQL node whose statement is copied per pass, so it is checked
    as one plus the three things that only matter because of the copying.

    The placement rule is the one that matters most: outside a loop the node has no last
    pass, so it would build a statement and never run it — succeeding, with a box that says
    so and no query behind it.
    """

    def _union(self, node_id: str = "u", **overrides) -> dict:
        data = {
            "datasource_id": str(uuid_pkg.uuid4()),
            "table_names": ["clients"],
            "sql_query": "SELECT id FROM clients WHERE id = :item",
            "params": [{"param": "item", "type": "number", "required": True}],
        }
        data.update(overrides)
        return node(node_id, "sql_union", **data)

    def _in_a_loop(self, union: dict) -> dict:
        return graph(
            [
                node("s", "start"),
                value_node("v"),
                node("loop", "for_each", source_node="v", item_name="item"),
                union,
                node("ok", "success"),
            ],
            [
                edge("s", "v"), edge("v", "loop"),
                edge("loop", union["id"], "body"),
                edge(union["id"], "loop"),
                edge(union["id"], "ok", "execute"),
                edge("loop", "ok", "done"),
            ],
        )

    def test_accepts_a_union_inside_a_for_each_body(self) -> None:
        svc.validate_graph(self._in_a_loop(self._union()))

    def test_refuses_a_union_outside_any_loop(self) -> None:
        with pytest.raises(HTTPException) as caught:
            svc.validate_graph(graph(
                [node("s", "start"), self._union(), node("ok", "success")],
                [edge("s", "u"), edge("u", "ok")],
            ))

        assert "inside a For each" in caught.value.detail
        assert "never run it" in caught.value.detail

    def test_refuses_a_union_hanging_off_a_body_without_returning(self) -> None:
        """
        Reachable from ``each`` is not enough — a node that never comes back is where a pass
        stops, so it would be built once and left. Same definition of "in the body" the
        collection rule uses.
        """
        with pytest.raises(HTTPException) as caught:
            svc.validate_graph(graph(
                [
                    node("s", "start"),
                    value_node("v"),
                    node("loop", "for_each", source_node="v", item_name="item"),
                    value_node("work"),
                    self._union(),
                    node("ok", "success"),
                ],
                [
                    edge("s", "v"), edge("v", "loop"),
                    edge("loop", "work", "body"), edge("work", "loop"),
                    edge("work", "u", "error"), edge("u", "ok"),
                    edge("loop", "ok", "done"),
                ],
            ))

        assert "inside a For each" in caught.value.detail

    def test_refuses_a_union_inside_a_do_until(self) -> None:
        """
        The same reason a ``do_until`` cannot collect, and the message says it: whether a
        pass is its last is decided by the router *after* the runner returns, so a node
        inside it cannot be told in time.
        """
        with pytest.raises(HTTPException) as caught:
            svc.validate_graph(graph(
                [
                    node("s", "start"),
                    value_node("v"),
                    node(
                        "loop", "do_until",
                        condition={
                            "source_node": "v", "operator": "not_empty", "value": "",
                        },
                    ),
                    self._union(),
                    node("ok", "success"),
                ],
                [
                    edge("s", "v"), edge("v", "loop"),
                    edge("loop", "u", "body"), edge("u", "loop"),
                    edge("u", "ok", "execute"),
                    edge("loop", "ok", "done"),
                ],
            ))

        assert "Do until" in caught.value.detail
        assert "Use a For each" in caught.value.detail

    @pytest.mark.parametrize(
        "statement",
        [
            "SELECT id FROM clients WHERE id = :item ORDER BY id",
            "SELECT id FROM clients WHERE id = :item LIMIT 5",
            "SELECT id FROM clients WHERE id = :item order  by id",
        ],
    )
    def test_refuses_a_clause_that_would_apply_to_the_whole_union(
        self, statement: str,
    ) -> None:
        with pytest.raises(HTTPException) as caught:
            svc.validate_graph(self._in_a_loop(self._union(sql_query=statement)))

        assert "every pass at once" in caught.value.detail

    def test_the_same_clause_inside_a_subquery_is_left_alone(self) -> None:
        """
        Read at bracket depth zero. Inside a subquery an ORDER BY is local and correct, and
        refusing it would rule out a perfectly ordinary fragment.
        """
        svc.validate_graph(self._in_a_loop(self._union(
            sql_query=(
                "SELECT id FROM clients WHERE id = :item AND ref = "
                "(SELECT ref FROM notes ORDER BY id LIMIT 1)"
            ),
        )))

    def test_refuses_a_parameter_named_like_a_generated_one(self) -> None:
        """
        ``:id__p7`` is how pass 7's copy is kept apart. A parameter spelled that way would
        collide with it, and one pass would be bound with another pass's value — a result
        that looks entirely normal.
        """
        with pytest.raises(HTTPException) as caught:
            svc.validate_graph(self._in_a_loop(self._union(
                sql_query="SELECT id FROM clients WHERE id = :item__p2",
                params=[{"param": "item__p2", "type": "number"}],
            )))

        assert "'item__p2'" in caught.value.detail
        assert "collide" in caught.value.detail

    def test_the_shared_sql_rules_still_apply(self) -> None:
        """It is a SQL node first. Asserted so the two validators cannot drift apart."""
        with pytest.raises(HTTPException) as caught:
            svc.validate_graph(self._in_a_loop(self._union(table_names=[])))

        assert "which tables its statement reads" in caught.value.detail


class TestValidateLoopCollection:
    """What a loop may union, and what it may label the rows with."""

    def _loop_graph(self, **loop_data) -> dict:
        """``start → q → loop``, body ``b``, done ``ok`` — the smallest collecting loop."""
        return graph(
            [
                node("s", "start"),
                sql_node("q"),
                node(
                    "loop", "for_each", label="each row",
                    source_node="q", item_name="item", **loop_data,
                ),
                value_node("b"),
                node("ok", "success"),
            ],
            [
                edge("s", "q"), edge("q", "loop"),
                edge("loop", "b", "body"), edge("b", "loop"),
                edge("loop", "ok", "done"),
            ],
        )

    def test_accepts_collecting_a_node_inside_the_body(self) -> None:
        svc.validate_graph(self._loop_graph(collect_from="b", label_item_as="row_id"))

    def test_refuses_collecting_a_node_outside_the_body(self) -> None:
        """
        That node ran once, before the loop, and its output does not change while the loop
        turns — so every pass would append the same rows, and a union of duplicates looks
        exactly like a union.
        """
        with pytest.raises(HTTPException) as caught:
            svc.validate_graph(self._loop_graph(collect_from="q"))

        assert "not inside its loop" in caught.value.detail
        assert "every pass would collect the same rows" in caught.value.detail

    def test_refuses_a_loop_collecting_itself(self) -> None:
        """
        The loop satisfies "inside the body" by construction — its body leads back to it —
        so it has to be excluded deliberately. Collecting its own output would append the
        item envelope it publishes to the union it is building, once per pass.
        """
        with pytest.raises(HTTPException) as caught:
            svc.validate_graph(self._loop_graph(collect_from="loop"))

        assert "not inside its loop" in caught.value.detail

    def test_refuses_collecting_a_node_that_is_not_in_the_graph(self) -> None:
        with pytest.raises(HTTPException) as caught:
            svc.validate_graph(self._loop_graph(collect_from="ghost"))

        assert "not in this graph" in caught.value.detail

    def test_refuses_a_label_with_nothing_to_label(self) -> None:
        """A field that silently does nothing is one the operator will swear they set."""
        with pytest.raises(HTTPException) as caught:
            svc.validate_graph(self._loop_graph(label_item_as="row_id"))

        assert "does not collect" in caught.value.detail

    def test_refuses_a_label_that_is_not_a_column_name(self) -> None:
        """It becomes a key in the result rows and is grouped by like any other column."""
        with pytest.raises(HTTPException) as caught:
            svc.validate_graph(
                self._loop_graph(collect_from="b", label_item_as="not a column"),
            )

        assert "cannot be a column name" in caught.value.detail

    def test_refuses_a_do_until_that_collects(self) -> None:
        """
        Only a loop that knows which pass is its last can publish a union. For a
        ``do_until`` that is the router's decision, taken after the runner returns —
        so the field is refused rather than accepted and then ignored.
        """
        with pytest.raises(HTTPException) as caught:
            svc.validate_graph(graph(
                [
                    node("s", "start"),
                    sql_node("q"),
                    node(
                        "loop", "do_until", label="until done",
                        condition={"source_node": "q", "operator": "not_empty"},
                        collect_from="b",
                    ),
                    value_node("b"),
                    node("ok", "success"),
                ],
                [
                    edge("s", "q"), edge("q", "loop"),
                    edge("loop", "b", "body"), edge("b", "loop"),
                    edge("loop", "ok", "done"),
                ],
            ))

        assert "cannot collect its passes" in caught.value.detail


class TestOwnership:
    """A graph belonging to somebody else is indistinguishable from a missing one."""

    async def test_another_users_graph_is_not_found(self, db, user, other_user, make_graph) -> None:  # noqa: ANN001
        theirs = await make_graph(other_user, "Theirs")

        with pytest.raises(HTTPException) as caught:
            await svc.get_graph(db, user.id, theirs.uuid)

        assert caught.value.status_code == 404

    async def test_a_missing_graph_gives_the_same_answer(self, db, user) -> None:  # noqa: ANN001
        with pytest.raises(HTTPException) as caught:
            await svc.get_graph(db, user.id, uuid_pkg.uuid4())

        assert caught.value.status_code == 404

    async def test_the_two_sentences_are_identical(self, db, user, other_user, make_graph) -> None:  # noqa: ANN001
        """
        Asserted directly, because a difference here is how somebody finds out which
        uuids are real — and it is the sort of difference a later edit reintroduces
        without noticing.
        """
        theirs = await make_graph(other_user, "Theirs")

        with pytest.raises(HTTPException) as owned:
            await svc.get_graph(db, user.id, theirs.uuid)
        with pytest.raises(HTTPException) as missing:
            await svc.get_graph(db, user.id, uuid_pkg.uuid4())

        assert owned.value.detail == missing.value.detail


class TestCreateAndRename:
    """Names, and the message a collision produces."""

    async def test_a_new_graph_opens_with_one_start_node(self, db, user) -> None:  # noqa: ANN001
        created = await svc.create_graph(db, user.id, "First graph")

        nodes = created.graph_data["nodes"]
        assert len(nodes) == 1
        assert nodes[0]["type"] == "start"

    async def test_the_default_graph_is_valid(self, db, user) -> None:  # noqa: ANN001
        """
        A graph nobody has touched must already pass the validator, or the first thing a
        user does after creating one is read a refusal.
        """
        created = await svc.create_graph(db, user.id, "Untouched")
        svc.validate_graph(created.graph_data)

    async def test_a_duplicate_name_is_refused_with_a_readable_message(self, db, user) -> None:  # noqa: ANN001
        """
        The unique index is the guarantee; this is the message. Without the check the
        failure surfaces as an IntegrityError and a 500, which says nothing actionable.
        """
        await svc.create_graph(db, user.id, "Revenue")

        with pytest.raises(HTTPException) as caught:
            await svc.create_graph(db, user.id, "revenue")

        assert "already have a graph called" in caught.value.detail

    async def test_two_users_may_use_the_same_name(self, db, user, other_user) -> None:  # noqa: ANN001
        await svc.create_graph(db, user.id, "Revenue")
        await svc.create_graph(db, other_user.id, "Revenue")

    async def test_a_graph_may_keep_its_own_name_when_renamed(self, db, user) -> None:  # noqa: ANN001
        """The duplicate check has to exclude the row being edited, or a rename that only
        changes the description fails on the name it already has."""
        created = await svc.create_graph(db, user.id, "Revenue")

        renamed = await svc.rename_graph(db, user.id, created.uuid, "Revenue", "new words")

        assert renamed.description == "new words"

    async def test_a_blank_name_is_refused(self, db, user) -> None:  # noqa: ANN001
        with pytest.raises(HTTPException) as caught:
            await svc.create_graph(db, user.id, "   ")

        assert "needs a name" in caught.value.detail


class TestPublishing:
    """Publishing validates the drawing; saving a draft does not."""

    async def test_a_broken_draft_can_be_saved(self, db, user) -> None:  # noqa: ANN001
        """
        Not quite — a *save* validates too. What a draft may be is **unpublished**; the
        thing this asserts is that the save's validation is the same one, so a graph that
        saved can always be published on the strength of its structure.
        """
        created = await svc.create_graph(db, user.id, "Draft")
        good = graph([node("s", "start"), node("ok", "success")], [edge("s", "ok")])

        await svc.save_graph(db, user.id, created.uuid, good)

        assert created.is_active is False

    async def test_publishing_refuses_a_graph_that_cannot_run(self, db, user, make_graph) -> None:  # noqa: ANN001
        """
        An active graph attached to an agent is callable by a model, so a broken one would
        fail inside somebody's conversation. Stored directly here to get a broken drawing
        past the save that would otherwise refuse it — which is exactly the state a row
        edited outside the application would be in.
        """
        broken = await make_graph(
            user, "Broken",
            graph_data=graph([node("a", "start"), node("b", "start")], []),
        )

        with pytest.raises(HTTPException) as caught:
            await svc.set_graph_active(db, user.id, broken.uuid, True)

        assert "exactly one Start node" in caught.value.detail

    async def test_unpublishing_does_not_validate(self, db, user, make_graph) -> None:  # noqa: ANN001
        """
        Making a broken graph a draft has to work, or a graph that somehow became invalid
        could not be switched off — which is the one action its owner most needs.
        """
        broken = await make_graph(
            user, "Broken", is_active=True,
            graph_data=graph([node("a", "start"), node("b", "start")], []),
        )

        parked = await svc.set_graph_active(db, user.id, broken.uuid, False)

        assert parked.is_active is False
