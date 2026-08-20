"""
Tests for ``engine/flow_rules.py`` — one per refusal.

Two habits run through this file.

**Every refusal is asserted by its sentence, not just by its type.** The reader of a
``FlowValidationError`` is somebody looking at a canvas, and the difference between
"invalid graph" and "the body of 'For each order' never comes back to it" is the
difference between a message they can act on and one they cannot. Asserting only
``pytest.raises(FlowValidationError)`` would let the wording rot silently.

**The valid case is asserted beside the invalid one.** A validator that refuses
everything passes every negative test in this file. Each class that refuses something
also builds the nearest thing that is fine, so a rule cannot be over-tightened without
a failure.

The two rules worth reading first are ``TestBatchBodiesMustReturn`` and
``TestAWriteInABodyMustReadFromInsideIt``. Both prevent the same class of disaster: a
run that reports success having moved a fraction of the data, or having moved the same
fraction repeatedly. Nothing about either outcome looks wrong in the dock.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from app.models.integrations import MAX_BATCH_SIZE, MIN_INTERVAL_SECONDS
from app.services.integrations.engine import flow_rules
from app.services.integrations.engine.flow_rules import (
    node_specs,
    validate_flow,
    validate_for_publish,
    vocabulary,
)
from app.services.integrations.errors import FlowValidationError


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def node(node_id: str, node_type: str, **data: Any) -> dict:
    return {"id": node_id, "type": node_type, "position": {"x": 0, "y": 0}, "data": data}


def edge(source: str, target: str, port: str = "default") -> dict:
    return {
        "id": f"{source}-{port}-{target}",
        "source": source,
        "source_port": port,
        "target": target,
    }


def graph(nodes: List[dict], edges: List[dict]) -> dict:
    return {"nodes": nodes, "edges": edges}


#: Real uuids, because ``connection_uuid`` is parsed rather than merely required — a
#: placeholder like "conn-1" is the shape a hallucinated identifier takes and this module
#: refuses it on purpose. Fixed rather than generated so a failure is reproducible.
CONNECTION_A = "0b6f5b1c-3d5a-4a6e-9f2b-1c7d8e9a0b11"
CONNECTION_B = "0b6f5b1c-3d5a-4a6e-9f2b-1c7d8e9a0b22"


def trigger(node_id: str = "t", **data: Any) -> dict:
    return node(node_id, "trigger", label="When it runs", **data)


def read(node_id: str = "r", **data: Any) -> dict:
    return node(
        node_id,
        "connector_read",
        label="Read orders",
        connection_uuid=CONNECTION_A,
        operation_id="list_orders",
        **data,
    )


def write(node_id: str = "w", source: Optional[str] = "r", **data: Any) -> dict:
    payload: Dict[str, Any] = {
        "label": "Write customers",
        "connection_uuid": CONNECTION_B,
        "operation_id": "create_customer",
        "mappings": [{"source": "email", "target": "email"}],
    }
    if source is not None:
        payload["source_node"] = source
    payload.update(data)
    return node(node_id, "connector_write", **payload)


def batch(node_id: str = "b", source: str = "r", **data: Any) -> dict:
    return node(node_id, "batch", label="For each order", source_node=source, **data)


def success(node_id: str = "s") -> dict:
    return node(node_id, "success", label="Done")


def simple_flow() -> dict:
    """trigger → read → write → success. The smallest thing that is fine."""
    return graph(
        [trigger(), read(), write(), success()],
        [edge("t", "r"), edge("r", "w"), edge("w", "s")],
    )


def looping_flow() -> dict:
    """trigger → read → batch ⇄ write, batch → success. The ordinary shape."""
    return graph(
        [trigger(), read(), batch(), write(source="b"), success()],
        [
            edge("t", "r"),
            edge("r", "b"),
            edge("b", "w", "body"),
            edge("w", "b"),
            edge("b", "s", "done"),
        ],
    )


def refusal(graph_data: dict) -> FlowValidationError:
    with pytest.raises(FlowValidationError) as caught:
        validate_flow(graph_data)
    return caught.value


# ---------------------------------------------------------------------------
# The happy cases, first — see the module docstring
# ---------------------------------------------------------------------------


class TestWhatIsAccepted:
    def test_a_straight_line_flow(self) -> None:
        validate_flow(simple_flow())

    def test_a_flow_with_a_batch_loop(self) -> None:
        validate_flow(looping_flow())

    def test_a_flow_with_error_paths_drawn(self) -> None:
        data = simple_flow()
        data["nodes"].append(node("f", "failure", label="Give up"))
        data["edges"].append(edge("r", "f", "error"))
        data["edges"].append(edge("w", "f", "error"))

        validate_flow(data)

    def test_an_orphaned_read_is_allowed(self) -> None:
        """
        A half-finished canvas left overnight is a normal state. Only an unreachable
        *write* is refused, because that one is a workflow whose author believes it
        writes somewhere it never reaches.
        """
        data = simple_flow()
        data["nodes"].append(read("r2"))

        validate_flow(data)


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------


class TestTheDrawingItself:
    @pytest.mark.parametrize("junk", [None, "graph", 5, []])
    def test_something_that_is_not_a_drawing(self, junk: object) -> None:
        with pytest.raises(FlowValidationError):
            validate_flow(junk)

    def test_an_empty_workflow_says_where_to_start(self) -> None:
        assert "trigger" in str(refusal(graph([], []))).lower()

    def test_nodes_must_be_a_list(self) -> None:
        with pytest.raises(FlowValidationError, match="list of steps"):
            validate_flow({"nodes": {}, "edges": []})

    def test_a_step_with_no_id(self) -> None:
        data = graph([{"type": "trigger", "data": {}}], [])

        assert "needs an id" in str(refusal(data))

    def test_two_steps_sharing_an_id(self) -> None:
        data = graph([trigger("t"), read("t")], [])

        assert "share the id 't'" in str(refusal(data))


class TestExactlyOneTrigger:
    def test_none_at_all(self) -> None:
        data = graph([read(), success()], [edge("r", "s")])

        assert "no trigger" in str(refusal(data))

    def test_two_of_them(self) -> None:
        data = simple_flow()
        data["nodes"].append(trigger("t2"))

        error = refusal(data)
        assert "exactly one trigger" in str(error)
        assert error.node_id == "t2"

    def test_nothing_may_lead_back_into_it(self) -> None:
        """
        A trigger with an inbound edge is somebody trying to draw a loop the wrong way.
        The message says which step to use instead.
        """
        data = simple_flow()
        data["edges"].append(edge("w", "t", "error"))

        error = refusal(data)
        assert "back into the trigger" in str(error)
        assert "Batch" in str(error)


class TestEdges:
    def test_an_edge_to_a_step_that_is_not_there(self) -> None:
        data = simple_flow()
        data["edges"].append(edge("r", "ghost", "error"))

        assert "not in this workflow" in str(refusal(data))

    def test_an_edge_from_nowhere(self) -> None:
        data = simple_flow()
        data["edges"].append({"id": "e", "source": "", "target": "s"})

        assert "does not say which step" in str(refusal(data))

    def test_two_edges_on_one_exit(self) -> None:
        """A run follows one path, so it could not say which."""
        data = simple_flow()
        data["nodes"].append(read("r2"))
        data["edges"].append(edge("r", "r2"))

        error = refusal(data)
        assert "two connections leaving" in str(error)
        assert error.node_id == "r"

    def test_two_edges_on_different_exits_are_fine(self) -> None:
        data = simple_flow()
        data["nodes"].append(node("f", "failure", label="Give up"))
        data["edges"].append(edge("r", "f", "error"))

        validate_flow(data)

    def test_an_edge_out_of_a_terminal(self) -> None:
        data = simple_flow()
        data["nodes"].append(read("r2"))
        data["edges"].append(edge("s", "r2"))

        assert "where the workflow ends" in str(refusal(data))

    def test_an_exit_the_step_does_not_have(self) -> None:
        """The message lists what it does offer, because "no 'body' exit" alone leaves
        the author guessing."""
        data = simple_flow()
        data["edges"] = [edge("t", "r"), edge("r", "w", "body"), edge("w", "s")]

        error = refusal(data)
        assert "no 'body' exit" in str(error)
        assert "default" in str(error)


# ---------------------------------------------------------------------------
# Loops
# ---------------------------------------------------------------------------


class TestCyclesMustPassThroughABatch:
    def test_a_loop_with_no_batch_in_it(self) -> None:
        data = graph(
            [trigger(), read(), node("x", "transform", label="Tidy",
                                     source_node="r",
                                     mappings=[{"source": "a", "target": "b"}]), success()],
            [edge("t", "r"), edge("r", "x"), edge("x", "r", "error"), edge("r", "s", "error")],
        )

        # The `r → s` edge on `error` collides with `x → r` making a cycle r → x → r.
        error = refusal(data)
        assert "nothing to stop it" in str(error)

    def test_a_loop_through_a_batch_is_fine(self) -> None:
        validate_flow(looping_flow())

    def test_a_wide_graph_does_not_exhaust_the_recursion_limit(self) -> None:
        """
        The cycle search is iterative on purpose. A thousand-node chain is not a
        realistic workflow, but a validator that raised ``RecursionError`` would report
        it as an unreadable graph rather than as anything the author could fix.
        """
        nodes = [trigger()]
        edges = [edge("t", "n0")]
        for index in range(1000):
            nodes.append(
                node(f"n{index}", "transform", label=f"Step {index}",
                     mappings=[{"source": "a", "target": "b"}])
            )
            edges.append(edge(f"n{index}", f"n{index + 1}"))
        nodes.append(success("n1000"))

        validate_flow(graph(nodes, edges))


class TestBatchBodiesMustReturn:
    """
    Both halves of "one batch of a hundred, reported as success". See the module
    docstring.
    """

    def test_a_body_wired_to_nothing(self) -> None:
        data = graph(
            [trigger(), read(), batch(), success()],
            [edge("t", "r"), edge("r", "b"), edge("b", "s", "done")],
        )

        error = refusal(data)
        assert "nothing wired to its body" in str(error)
        assert error.node_id == "b"

    def test_a_body_that_never_comes_back(self) -> None:
        data = graph(
            [trigger(), read(), batch(), write(source="b"), success()],
            [
                edge("t", "r"),
                edge("r", "b"),
                edge("b", "w", "body"),
                edge("w", "s"),
                edge("b", "s", "done"),
            ],
        )

        error = refusal(data)
        assert "never comes back" in str(error)
        assert "only the first batch" in str(error)

    def test_a_body_that_returns_through_several_steps_is_fine(self) -> None:
        data = graph(
            [
                trigger(),
                read(),
                batch(),
                node("x", "transform", label="Tidy", source_node="b",
                     mappings=[{"source": "a", "target": "b"}]),
                write(source="x"),
                success(),
            ],
            [
                edge("t", "r"),
                edge("r", "b"),
                edge("b", "x", "body"),
                edge("x", "w"),
                edge("w", "b"),
                edge("b", "s", "done"),
            ],
        )

        validate_flow(data)


class TestAWriteInABodyMustReadFromInsideIt:
    """
    A write inside a loop reading from outside it writes the same batch on every pass.
    The counters would say 50,000 written and the destination would hold 500 records
    repeated a hundred times — a discrepancy nobody finds until a customer asks why
    they got the same confirmation a hundred times.
    """

    def test_reading_from_outside_the_body(self) -> None:
        data = graph(
            [trigger(), read("r"), read("outside"), batch(), write(source="outside"), success()],
            [
                edge("t", "r"),
                edge("r", "outside"),
                edge("outside", "b"),
                edge("b", "w", "body"),
                edge("w", "b"),
                edge("b", "s", "done"),
            ],
        )

        error = refusal(data)
        assert "would write the same records on every pass" in str(error)
        assert error.node_id == "w"

    def test_reading_from_the_batch_itself_is_fine(self) -> None:
        """The batch node is what hands out the current page, so it is the ordinary
        source for anything in the body."""
        validate_flow(looping_flow())


# ---------------------------------------------------------------------------
# Data references
# ---------------------------------------------------------------------------


class TestSourcesMustBeUpstream:
    def test_reading_from_a_step_that_is_not_there(self) -> None:
        data = simple_flow()
        data["nodes"][2]["data"]["source_node"] = "ghost"

        assert "not in this workflow" in str(refusal(data))

    def test_reading_from_itself(self) -> None:
        data = simple_flow()
        data["nodes"][2]["data"]["source_node"] = "w"

        assert "read from itself" in str(refusal(data))

    def test_reading_from_a_step_that_cannot_reach_it(self) -> None:
        """
        Not merely "has not run": the worse case is that it holds the *previous* pass's
        records, which produces a plausible result that is one batch stale.
        """
        data = graph(
            [trigger(), read("r"), read("later"), write(source="later"), success()],
            [edge("t", "r"), edge("r", "w"), edge("w", "later"), edge("later", "s")],
        )

        error = refusal(data)
        assert "no path from that step to this one" in str(error)
        assert error.node_id == "w"

    def test_reading_from_several_steps_back_is_fine(self) -> None:
        data = graph(
            [
                trigger(),
                read(),
                node("x", "transform", label="Tidy", source_node="r",
                     mappings=[{"source": "a", "target": "b"}]),
                write(source="r"),
                success(),
            ],
            [edge("t", "r"), edge("r", "x"), edge("x", "w"), edge("w", "s")],
        )

        validate_flow(data)


class TestWritesMustBeReachable:
    def test_a_stranded_write(self) -> None:
        data = simple_flow()
        data["nodes"].append(write("w2", source=None))

        error = refusal(data)
        assert "Nothing leads to" in str(error)
        assert error.node_id == "w2"


# ---------------------------------------------------------------------------
# One node at a time
# ---------------------------------------------------------------------------


class TestNodeTypes:
    def test_a_type_that_does_not_exist(self) -> None:
        data = simple_flow()
        data["nodes"].append(node("x", "webhook_send", label="Ping"))

        assert "not a kind of step" in str(refusal(data))

    def test_a_type_that_exists_but_has_no_runner(self) -> None:
        """
        The Phase 2 and Phase 3 entries are in the vocabulary so there is one list, and
        refused here so the palette cannot offer them. ``agent`` is the one the design
        argues about at length: the only non-deterministic node, and not available yet.
        """
        data = simple_flow()
        data["nodes"].append(node("x", "agent", label="Think"))

        error = refusal(data)
        assert "not available yet" in str(error)
        assert error.node_id == "x"


class TestTriggerConfiguration:
    def test_a_schedule_with_no_interval(self) -> None:
        data = simple_flow()
        data["nodes"][0]["data"]["kind"] = "schedule"

        assert "does not say how often" in str(refusal(data))

    def test_a_schedule_faster_than_a_minute(self) -> None:
        data = simple_flow()
        data["nodes"][0]["data"].update(kind="schedule", interval_seconds=30)

        error = refusal(data)
        assert f"every {MIN_INTERVAL_SECONDS} seconds" in str(error)
        assert "spends that allowance" in str(error)

    def test_a_schedule_at_the_floor_is_fine(self) -> None:
        data = simple_flow()
        data["nodes"][0]["data"].update(
            kind="schedule", interval_seconds=MIN_INTERVAL_SECONDS
        )

        validate_flow(data)

    def test_catch_up_is_refused(self) -> None:
        """
        Twelve missed hourly slots cost twelve times the API quota for zero extra data.
        Refused rather than defaulted, so nobody switches it on and finds out at the end
        of the month.
        """
        data = simple_flow()
        data["nodes"][0]["data"].update(
            kind="schedule", interval_seconds=3600, catch_up=True
        )

        assert "Catching up on missed runs is not available" in str(refusal(data))

    def test_cron_is_refused_with_the_alternative(self) -> None:
        data = simple_flow()
        data["nodes"][0]["data"].update(kind="schedule", cron_expression="0 * * * *")

        assert "interval in seconds" in str(refusal(data))

    def test_an_unknown_overlap_policy(self) -> None:
        data = simple_flow()
        data["nodes"][0]["data"].update(kind="schedule", interval_seconds=60,
                                        overlap_policy="pile_up")

        assert "pile_up" in str(refusal(data))


class TestConnectorNodes:
    def test_a_read_with_no_connection(self) -> None:
        data = simple_flow()
        data["nodes"][1]["data"].pop("connection_uuid")

        assert "which connection to use" in str(refusal(data))

    def test_a_connection_that_is_not_a_uuid_is_refused_by_name(self) -> None:
        """
        The shape a hallucination takes is a plausible word, not a blank. A model writing
        ``"shopify-prod"`` where an identifier belongs must be caught while the canvas is
        still open — at run time it is a line in a log at 3am.
        """
        data = simple_flow()
        data["nodes"][1]["data"]["connection_uuid"] = "shopify-prod"

        assert "shopify-prod" in str(refusal(data))

    def test_the_field_is_spelled_the_way_the_runner_reads_it(self) -> None:
        """
        These two rules used to disagree — the validator asked for ``connection_id`` while
        ``connector_nodes.resolve_target`` read ``connection_uuid`` — so a workflow could
        save green and fail on its first record. Pinned against the runner's own reader
        rather than against a string, so renaming one without the other fails here.
        """
        from app.services.integrations.nodes import connector_nodes

        data = simple_flow()["nodes"][1]["data"]

        assert str(connector_nodes._connection_uuid(data)) == CONNECTION_A

    def test_a_read_with_no_operation(self) -> None:
        data = simple_flow()
        data["nodes"][1]["data"].pop("operation_id")

        assert "what to do with that connection" in str(refusal(data))

    @pytest.mark.parametrize("size", [0, -1, MAX_BATCH_SIZE + 1])
    def test_a_batch_size_out_of_range(self, size: int) -> None:
        """
        Refused rather than clamped: this is a bound on how much of somebody's data one
        worker holds in memory at once, and silently reducing it would make the workflow
        behave differently from what its author wrote.
        """
        data = simple_flow()
        data["nodes"][1]["data"]["batch_size"] = size

        error = refusal(data)
        assert "held in memory" in str(error)

    def test_a_batch_size_that_is_not_a_number(self) -> None:
        data = simple_flow()
        data["nodes"][1]["data"]["batch_size"] = "500"

        assert "whole number" in str(refusal(data))


class TestMappings:
    def test_a_transform_step_with_no_mappings(self) -> None:
        data = simple_flow()
        data["nodes"].insert(2, node("x", "transform", label="Tidy", source_node="r"))
        data["edges"] = [edge("t", "r"), edge("r", "x"), edge("x", "w"), edge("w", "s")]

        assert "does not map any fields" in str(refusal(data))

    def test_a_mapping_with_no_destination(self) -> None:
        data = simple_flow()
        data["nodes"][2]["data"]["mappings"] = [{"source": "email"}]

        assert "does not say which field it fills in" in str(refusal(data))

    def test_two_mappings_into_one_field(self) -> None:
        """One of them would silently win, so say which."""
        data = simple_flow()
        data["nodes"][2]["data"]["mappings"] = [
            {"source": "email", "target": "email"},
            {"source": "contact_email", "target": "email"},
        ]

        assert "maps two things into 'email'" in str(refusal(data))

    def test_an_unknown_transform_names_the_alternatives(self) -> None:
        data = simple_flow()
        data["nodes"][2]["data"]["mappings"] = [
            {"source": "email", "target": "email", "transform": ["uppercase"]}
        ]

        error = refusal(data)
        assert "uppercase" in str(error)
        assert "upper" in str(error)

    def test_a_known_transform_chain_is_fine(self) -> None:
        data = simple_flow()
        data["nodes"][2]["data"]["mappings"] = [
            {"source": "email", "target": "email", "transform": ["trim", "lower"]}
        ]

        validate_flow(data)


class TestFilters:
    def _with_filter(self, **spec: Any) -> dict:
        return graph(
            [
                trigger(),
                read(),
                node("f", "filter", label="Only paid", source_node="r", specs=[spec]),
                write(source="f"),
                success(),
            ],
            [edge("t", "r"), edge("r", "f"), edge("f", "w", "kept"), edge("w", "s")],
        )

    def test_a_valid_filter(self) -> None:
        validate_flow(self._with_filter(column="status", operator="==", values=["paid"]))

    def test_a_filter_with_no_conditions(self) -> None:
        data = self._with_filter(column="status", operator="==", values=["paid"])
        data["nodes"][2]["data"]["specs"] = []

        assert "let every record through" in str(refusal(data))

    def test_an_operator_the_runner_does_not_have(self) -> None:
        """
        Checked with ``filter_algebra``'s own function, so a condition the validator
        accepted and the runner refused cannot exist.
        """
        data = self._with_filter(column="status", operator="~=", values=["paid"])

        assert "not a way of comparing values" in str(refusal(data))

    def test_the_wrong_number_of_values(self) -> None:
        data = self._with_filter(column="total", operator="between", values=[100])

        assert "between" in str(refusal(data))

    def test_a_condition_with_no_field(self) -> None:
        data = self._with_filter(operator="==", values=["paid"])

        assert "which field to look at" in str(refusal(data))


class TestBranches:
    def _with_branch(self, conditions: List[dict]) -> dict:
        return graph(
            [
                trigger(),
                read(),
                node("br", "branch", label="By country", source_node="r",
                     conditions=conditions),
                write(source="r"),
                success(),
            ],
            [edge("t", "r"), edge("r", "br"), edge("br", "w", "uk"), edge("w", "s")],
        )

    def test_a_valid_branch(self) -> None:
        validate_flow(
            self._with_branch([{"port": "uk", "operator": "==", "values": ["GB"]}])
        )

    def test_a_branch_with_no_conditions(self) -> None:
        data = self._with_branch([])
        data["edges"] = [edge("t", "r"), edge("r", "br"), edge("w", "s")]

        assert "every record would take the same path" in str(refusal(data))

    def test_a_condition_with_no_name(self) -> None:
        data = self._with_branch([{"operator": "==", "values": ["GB"]}])
        data["edges"] = [edge("t", "r"), edge("r", "br"), edge("w", "s")]

        assert "cannot be drawn" in str(refusal(data))

    @pytest.mark.parametrize("reserved", ["else", "error"])
    def test_a_condition_cannot_take_a_reserved_exit_name(self, reserved: str) -> None:
        data = self._with_branch([{"port": reserved, "operator": "==", "values": ["GB"]}])
        data["edges"] = [edge("t", "r"), edge("r", "br"), edge("w", "s")]

        assert "already means something here" in str(refusal(data))

    def test_two_conditions_with_one_name(self) -> None:
        data = self._with_branch(
            [
                {"port": "uk", "operator": "==", "values": ["GB"]},
                {"port": "uk", "operator": "==", "values": ["IE"]},
            ]
        )

        assert "two conditions called 'uk'" in str(refusal(data))

    def test_a_branchs_authored_exits_can_be_drawn_from(self) -> None:
        """
        The reason ``branch`` is absent from the static port table: its exits come from
        its own data, so the validator has to derive them rather than look them up.
        """
        data = self._with_branch(
            [
                {"port": "uk", "operator": "==", "values": ["GB"]},
                {"port": "rest", "operator": "!=", "values": ["GB"]},
            ]
        )
        data["nodes"].append(node("f", "failure", label="Give up"))
        data["edges"].append(edge("br", "f", "rest"))

        validate_flow(data)


class TestValidateNodes:
    def _with_rules(self, rules: List[dict]) -> dict:
        return graph(
            [
                trigger(),
                read(),
                node("v", "validate", label="Check", source_node="r", rules=rules),
                write(source="v"),
                success(),
            ],
            [edge("t", "r"), edge("r", "v"), edge("v", "w", "valid"), edge("w", "s")],
        )

    def test_a_valid_rule_set(self) -> None:
        validate_flow(self._with_rules([{"field": "email", "required": True}]))

    def test_no_rules_at_all(self) -> None:
        data = self._with_rules([])

        assert "every record would count as valid" in str(refusal(data))

    def test_a_rule_with_no_field(self) -> None:
        data = self._with_rules([{"required": True}])

        assert "which field it checks" in str(refusal(data))

    def test_a_type_that_is_not_a_type(self) -> None:
        """
        The list comes from ``app/utils/type_coercion``, so the rule and the coercion
        cannot disagree about what a type is.
        """
        data = self._with_rules([{"field": "total", "type": "currency"}])

        error = refusal(data)
        assert "currency" in str(error)
        assert "number" in str(error)


# ---------------------------------------------------------------------------
# Publish
# ---------------------------------------------------------------------------


class TestPublishIsStricterThanSave:
    def test_publish_accepts_what_save_accepts(self) -> None:
        validate_for_publish(simple_flow())

    def test_a_draft_may_have_an_unmapped_required_field(self) -> None:
        """
        Refusing this on save would make the canvas unusable — it is what a workflow
        looks like halfway through being built.
        """
        data = simple_flow()
        data["nodes"][2]["data"]["required_inputs"] = ["email", "name"]

        validate_flow(data)

    def test_publishing_it_does_not(self) -> None:
        """A scheduled run has nobody to ask, so the mapping panel's warning has to
        mean something at exactly this moment."""
        data = simple_flow()
        data["nodes"][2]["data"]["required_inputs"] = ["email", "name"]

        with pytest.raises(FlowValidationError) as caught:
            validate_for_publish(data)

        assert "name" in str(caught.value)
        assert "nobody to ask" in str(caught.value)

    def test_a_constant_satisfies_a_required_field(self) -> None:
        data = simple_flow()
        data["nodes"][2]["data"]["required_inputs"] = ["email", "source"]
        data["nodes"][2]["data"]["mappings"].append(
            {"target": "source", "const": "shopify"}
        )

        validate_for_publish(data)

    def test_a_default_with_nothing_to_fall_back_from_is_refused_as_a_constant(
        self,
    ) -> None:
        """
        Changed when ``field_map`` landed and the validator started loading mappings the
        way the runner does. A default only applies when the value it would replace is
        absent; with no source to read, that is *always*, which makes it a fixed value
        entered in the wrong column.

        Allowing both spellings would mean two columns of the mapping grid doing the
        same job depending on whether a third is blank. The refusal names the fix, and
        ``const`` — which the test above covers — is the one way to say it.
        """
        data = simple_flow()
        data["nodes"][2]["data"]["required_inputs"] = ["email", "tier"]
        data["nodes"][2]["data"]["mappings"].append(
            {"target": "tier", "default": "standard"}
        )

        with pytest.raises(FlowValidationError) as caught:
            validate_for_publish(data)

        assert "fixed value column" in str(caught.value)


# ---------------------------------------------------------------------------
# The vocabulary, as served
# ---------------------------------------------------------------------------


class TestTheVocabularyMatchesTheValidator:
    """
    The promise the whole module rests on: **the palette can never offer what the
    validator refuses.** Graph Designer keeps its port table in JavaScript as well as in
    Python, and these are the assertions that stop that happening again here.
    """

    def test_only_implemented_types_are_offered(self) -> None:
        offered = {spec["type"] for spec in node_specs()}

        assert offered == set(flow_rules.IMPLEMENTED_NODE_TYPES)

    def test_the_agent_node_is_in_the_vocabulary_but_not_the_palette(self) -> None:
        from app.models.integrations import NODE_TYPE_VALUES

        assert "agent" in NODE_TYPE_VALUES
        assert "agent" not in {spec["type"] for spec in node_specs()}

    def test_every_offered_port_is_one_the_validator_accepts(self) -> None:
        """
        Asserted by drawing from each port rather than by comparing two lists — a
        comparison would pass if both were wrong in the same way.
        """
        for spec in node_specs():
            if spec["terminal"] or spec["dynamic_ports"]:
                continue
            for port in spec["ports"]:
                data = graph(
                    [trigger(), node("x", spec["type"]), success()],
                    [edge("t", "x"), edge("x", "s", port)],
                )
                # The node's own configuration is refused, but never the port.
                try:
                    validate_flow(data)
                except FlowValidationError as error:
                    assert f"no '{port}' exit" not in str(error), (spec["type"], port)

    def test_a_terminal_offers_no_exits(self) -> None:
        for spec in node_specs():
            if spec["terminal"]:
                assert spec["ports"] == []

    def test_the_batch_node_is_the_only_loop(self) -> None:
        loops = {spec["type"] for spec in node_specs() if spec["loop"]}

        assert loops == {"batch"}

    def test_the_connector_nodes_are_the_ones_needing_a_connection(self) -> None:
        needs = {spec["type"] for spec in node_specs() if spec["needs_connection"]}

        assert needs == {"connector_read", "connector_write"}


class TestVocabularyPayload:
    def test_the_operators_are_filter_algebras_own(self) -> None:
        from app.services.agent_recursive_dataframes import filter_algebra

        assert set(vocabulary()["operators"]) == set(filter_algebra.OPERATORS)

    def test_the_transforms_are_the_transform_tables_own(self) -> None:
        from app.services.integrations.engine import transform

        assert {t["name"] for t in vocabulary()["transforms"]} == set(transform.TRANSFORMS)

    def test_the_defaults_come_from_the_model_constants(self) -> None:
        from app.models.integrations import DEFAULT_BATCH_SIZE

        defaults = vocabulary()["defaults"]

        assert defaults["batch_size"] == DEFAULT_BATCH_SIZE
        assert defaults["max_batch_size"] == MAX_BATCH_SIZE
        assert defaults["min_interval_seconds"] == MIN_INTERVAL_SECONDS

    def test_it_is_json_serialisable(self) -> None:
        """It is served to a browser, so a stray frozenset would be a 500 at runtime."""
        import json

        json.dumps(vocabulary())


class TestRefusalsPointAtANode:
    """
    ``node_id`` is what lets the canvas highlight the step instead of showing a banner
    about a workflow the author has to search by hand. Pinned for the refusals where the
    node is knowable.
    """

    def test_a_node_level_refusal_carries_its_id(self) -> None:
        data = simple_flow()
        data["nodes"][1]["data"].pop("connection_uuid")

        assert refusal(data).node_id == "r"

    def test_an_edge_level_refusal_carries_the_edge_id(self) -> None:
        data = simple_flow()
        data["edges"].append(edge("r", "ghost", "error"))

        assert refusal(data).edge_id == "r-error-ghost"
