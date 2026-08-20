"""
Tests for ``engine/flow_compiler.py``.

The only module in the feature that imports ``langgraph``, so this is one of exactly two
test files that need it — hence the ``importorskip`` below rather than a dependency the
whole suite carries.

The compiled graph is **run**, not inspected. A test that asserted the edges existed would
pass for a graph whose router returns the wrong one, and the router is where all the
decisions are. So each class builds a small workflow, invokes it, and asserts on where it
went and what the state says.

The four properties, in the order the failures matter:

**Cancellation outranks the error channels.** A cancelled run must not take an error edge
into a notification node and do more work on the way out.

**A failure routes before the port questions are asked.** A failed read routed by "were
there any records" would answer "no", take ``done``, and the run would report that it
finished.

**Two channels, chosen at compile time.** A node with a drawn error path writes
``errors[id]`` and the run carries on; one without writes ``failed_at`` and the run ends.
A single flag cannot hold both, and with one, a workflow that recovered would still report
itself failed.

**The recursion limit is computed.** LangGraph's default of 25 would stop an ordinary
hundred-pass loop with an internal exception raised a long way from the cause.
"""

from __future__ import annotations

import itertools

import pytest

pytest.importorskip("langgraph", reason="the compiler is the only langgraph importer")

from langgraph.graph import END  # noqa: E402

from app.models.integrations import (  # noqa: E402
    NODE_BATCH,
    NODE_TRANSFORM,
    PORT_BODY,
    PORT_DEFAULT,
    PORT_DONE,
    PORT_ERROR,
    PORT_INVALID,
    PORT_VALID,
    IntegrationFlow,
    IntegrationRun,
)
from app.models.user.user import User  # noqa: E402
from app.services.integrations.engine import (  # noqa: E402
    flow_compiler,
    flow_state,
    node_runners,
    record_buffer,
    record_log,
    run_store,
)
from app.services.integrations.errors import FlowValidationError  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402


@pytest.fixture
async def flow(db: AsyncSession, user: User) -> IntegrationFlow:
    row = IntegrationFlow(user_id=user.id, name="Contact sync")
    db.add(row)
    await db.commit()
    return row


@pytest.fixture
async def run(db: AsyncSession, flow: IntegrationFlow) -> IntegrationRun:
    created = await run_store.create_run(
        db, flow_id=flow.id, flow_version_id=None, thread_id="thread-1"
    )
    await db.commit()
    yield created
    record_log.release_run(created.id)
    run_store.forget_run(created.id)


@pytest.fixture
def context(run: IntegrationRun, flow: IntegrationFlow, integration_sessions):  # noqa: ANN001, ANN201
    built = node_runners.RunContext(
        run_id=run.id,
        run_uuid=str(run.uuid),
        user_id=flow.user_id,
        open_session=integration_sessions,
    )
    yield built
    record_buffer.release_run(built.run_uuid)


def node(node_type: str, node_id: str, **data) -> dict:
    return {"id": node_id, "type": node_type, "data": {"label": node_id, **data}}


def edge(source: str, target: str, port: str = PORT_DEFAULT) -> dict:
    return {"id": f"{source}->{target}:{port}", "source": source, "target": target,
            "source_port": port}


_threads = itertools.count()


async def invoke(compiled, records=None, **overrides):  # noqa: ANN001, ANN201
    """
    Run a compiled workflow to completion and hand back the final state.

    A fresh ``thread_id`` per call. Two invocations sharing one would have the second
    resume the first's checkpoint — which is exactly the feature in production and a
    silent cross-contamination in a test, since the earlier run's ``errors`` and counts
    arrive as the later one's starting state.
    """
    state = flow_state.initial_state(run_id="r", version_hash="v")
    state["inputs"] = {"records": list(records or [])}
    state.update(overrides)

    return await compiled.graph.ainvoke(
        state,
        config={
            "configurable": {"thread_id": f"t-{next(_threads)}"},
            "recursion_limit": compiled.recursion_limit,
        },
    )


# ---------------------------------------------------------------------------
# The ordinary path
# ---------------------------------------------------------------------------


class TestALoopThatRuns:
    """A workflow that reads a list, loops over it in batches, maps and ends."""

    def graph(self, batch_size: int = 2) -> dict:
        return {
            "nodes": [
                node("trigger", "start"),
                node(NODE_BATCH, "loop", source_node="start", batch_size=batch_size),
                node(NODE_TRANSFORM, "map",
                     mappings=[{"source": "n", "target": "n", "type": "integer"}]),
                node("success", "done"),
            ],
            "edges": [
                edge("start", "loop"),
                edge("loop", "map", PORT_BODY),
                edge("loop", "done", PORT_DONE),
                edge("map", "loop"),
            ],
        }

    async def test_every_record_goes_round(self, context) -> None:  # noqa: ANN001
        """
        Five records at two per pass is three passes. Asserted on the summed counts,
        because that is the number the run page shows and the one ``_accumulate`` exists
        to keep right.
        """
        compiled = await flow_compiler.compile_flow(self.graph(), context)

        final = await invoke(compiled, [{"n": index} for index in range(5)])

        assert flow_state.total(final, "passes") == 3
        assert flow_state.total(final, "mapped") == 5
        assert not final.get("failed_at")

    async def test_an_empty_source_takes_done_on_the_first_pass(
        self, context
    ) -> None:  # noqa: ANN001
        compiled = await flow_compiler.compile_flow(self.graph(), context)

        final = await invoke(compiled, [])

        assert flow_state.total(final, "passes") == 0
        assert not final.get("failed_at")

    async def test_a_body_node_finds_its_batch_without_being_wired(
        self, context
    ) -> None:  # noqa: ANN001
        """
        The transform names no ``source_node``. Only the compiler knows the nesting, so
        it scopes the context — and this is what makes the common case, a chain of steps
        inside a body, need no wiring at all.
        """
        compiled = await flow_compiler.compile_flow(self.graph(), context)

        assert compiled.enclosing_batch["map"] == "loop"

        final = await invoke(compiled, [{"n": 1}])
        assert flow_state.total(final, "mapped") == 1


# ---------------------------------------------------------------------------
# Failure
# ---------------------------------------------------------------------------


class TestTwoFailureChannels:
    def graph(self, *, with_error_path: bool) -> dict:
        edges = [edge("start", "map"), edge("map", "done")]
        nodes = [
            node("trigger", "start"),
            # A transform naming an unknown transform fails the node — every record
            # would fail the same way, so it is the author's mistake, not the data's.
            node(NODE_TRANSFORM, "map", source_node="start",
                 mappings=[{"source": "n", "target": "n", "transform": "nope"}]),
            node("success", "done"),
        ]
        if with_error_path:
            nodes.append(node("success", "recovered"))
            edges.append(edge("map", "recovered", PORT_ERROR))
        return {"nodes": nodes, "edges": edges}

    async def test_a_drawn_error_path_is_taken_and_the_run_is_not_failed(
        self, context
    ) -> None:  # noqa: ANN001
        """
        With a single flag, a workflow that recovered from a failed step would still
        report the whole run as failed — the opposite of what drawing a recovery path
        means.
        """
        compiled = await flow_compiler.compile_flow(
            self.graph(with_error_path=True), context
        )

        final = await invoke(compiled, [{"n": 1}])

        assert "map" in (final.get("errors") or {})
        assert not final.get("failed_at"), "a handled failure is not a failed run"
        assert "nope" in final["errors"]["map"]

    async def test_no_error_path_ends_the_run(self, context) -> None:  # noqa: ANN001
        compiled = await flow_compiler.compile_flow(
            self.graph(with_error_path=False), context
        )

        final = await invoke(compiled, [{"n": 1}])

        assert final.get("failed_at") == "map"
        assert not (final.get("errors") or {})

    async def test_the_channel_is_chosen_at_compile_time(self, context) -> None:  # noqa: ANN001
        """The same node, the same failure, two outcomes — decided by the drawing rather
        than by anything the runner could see."""
        handled = await invoke(
            await flow_compiler.compile_flow(self.graph(with_error_path=True), context),
            [{"n": 1}],
        )
        unhandled = await invoke(
            await flow_compiler.compile_flow(self.graph(with_error_path=False), context),
            [{"n": 1}],
        )

        assert bool(handled.get("errors")) != bool(unhandled.get("errors"))

    async def test_a_failure_end_fails_the_run(self, context) -> None:  # noqa: ANN001
        """Reaching a ``failure`` node *is* the run failing, and it says the author's own
        sentence rather than a generic one."""
        graph = {
            "nodes": [node("trigger", "start"),
                      node("failure", "stop", message="No orders to sync.")],
            "edges": [edge("start", "stop")],
        }
        compiled = await flow_compiler.compile_flow(graph, context)

        final = await invoke(compiled)

        assert final.get("failed_at") == "stop"
        assert final["failure_message"] == "No orders to sync."


class TestFailureRoutesBeforeThePortQuestions:
    async def test_a_failed_loop_does_not_take_done(self, context) -> None:  # noqa: ANN001
        """
        The ordering that matters most. A failed read routed by "were there any records"
        answers "no", takes ``done``, and the run reports that it finished — a sync that
        moved nothing and showed a green tick.
        """
        graph = {
            "nodes": [
                node("trigger", "start"),
                # Wired to a step that produces nothing: the batch fails.
                node(NODE_BATCH, "loop", source_node="ghost"),
                node("success", "done"),
            ],
            "edges": [edge("start", "loop"), edge("loop", "done", PORT_DONE)],
        }
        compiled = await flow_compiler.compile_flow(graph, context)

        final = await invoke(compiled, [{"n": 1}])

        assert final.get("failed_at") == "loop", "the loop failed rather than finishing"
        assert flow_state.total(final, "passes") == 0


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


class TestCancellationOutranksEverything:
    async def test_a_cancelled_run_ends_rather_than_taking_an_error_edge(
        self, db: AsyncSession, run: IntegrationRun, context
    ) -> None:  # noqa: ANN001
        """
        The reason the cancel check sits above the error channels. An error edge into a
        notification node would mean a cancelled run sending a message on the way out —
        more work, done because somebody asked it to stop.
        """
        graph = {
            "nodes": [
                node("trigger", "start"),
                node(NODE_TRANSFORM, "map", source_node="start",
                     mappings=[{"source": "n", "target": "n"}]),
                node("success", "notified"),
                node("success", "done"),
            ],
            "edges": [
                edge("start", "map"),
                edge("map", "done"),
                edge("map", "notified", PORT_ERROR),
            ],
        }
        compiled = await flow_compiler.compile_flow(graph, context)

        await run_store.request_cancel(db, run.id)
        final = await invoke(compiled, [{"n": 1}])

        assert final.get("cancelled") is True
        assert not (final.get("errors") or {}), "cancelling is not a failure to handle"
        assert flow_state.total(final, "mapped") == 0

    async def test_a_stopped_run_is_not_recorded_as_a_failed_one(
        self, db: AsyncSession, run: IntegrationRun, context
    ) -> None:  # noqa: ANN001
        """A red badge on something somebody asked for sends an operator looking for a
        fault that does not exist."""
        graph = {
            "nodes": [node("trigger", "start"), node("success", "done")],
            "edges": [edge("start", "done")],
        }
        compiled = await flow_compiler.compile_flow(graph, context)

        await run_store.request_cancel(db, run.id)
        final = await invoke(compiled)

        assert final.get("cancelled") is True
        assert not final.get("failed_at")


# ---------------------------------------------------------------------------
# Ports
# ---------------------------------------------------------------------------


class TestPorts:
    async def test_validate_sends_a_wholly_invalid_batch_down_invalid(
        self, context
    ) -> None:  # noqa: ANN001
        graph = {
            "nodes": [
                node("trigger", "start"),
                node("validate", "check", source_node="start",
                     rules=[{"field": "email", "required": True}]),
                node("success", "good"),
                node("failure", "bad", message="Nothing was valid."),
            ],
            "edges": [
                edge("start", "check"),
                edge("check", "good", PORT_VALID),
                edge("check", "bad", PORT_INVALID),
            ],
        }
        compiled = await flow_compiler.compile_flow(graph, context)

        final = await invoke(compiled, [{"name": "no email"}])

        assert final["failure_message"] == "Nothing was valid."

    async def test_a_port_with_nothing_drawn_on_it_ends_the_run(
        self, context
    ) -> None:  # noqa: ANN001
        """
        An author who wired ``valid`` and left ``invalid`` bare has said the invalid
        records go nowhere — and they are already counted and logged. Guessing a successor
        they did not draw would be inventing a step.
        """
        graph = {
            "nodes": [
                node("trigger", "start"),
                node("validate", "check", source_node="start",
                     rules=[{"field": "email", "required": True}]),
                node("success", "good"),
            ],
            "edges": [edge("start", "check"), edge("check", "good", PORT_VALID)],
        }
        compiled = await flow_compiler.compile_flow(graph, context)

        final = await invoke(compiled, [{"name": "no email"}])

        assert not final.get("failed_at"), "ending is not failing"
        assert flow_state.total(final, "invalid") == 1

    async def test_the_router_and_the_runner_use_the_same_function(self) -> None:
        """Asserted structurally rather than behaviourally: the compiler holds no port
        logic of its own, so there is nothing for the log to disagree with."""
        from app.models.integrations import NODE_BRANCH, NODE_FILTER, NODE_VALIDATE

        assert flow_compiler._PORT_CHOOSERS == {
            NODE_VALIDATE: node_runners.validate_port,
            NODE_FILTER: node_runners.filter_port,
            NODE_BRANCH: node_runners.branch_port,
            NODE_BATCH: node_runners.batch_port,
        }


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------


class TestTheRecursionLimit:
    def test_it_scales_with_the_loop_not_the_drawing(self) -> None:
        """
        A six-node body over a thousand passes is six thousand super-steps. A limit
        derived from the node count alone would be off by three orders of magnitude, and
        LangGraph's default of 25 would stop an ordinary hundred-pass loop.
        """
        nodes = [node("trigger", "start"), node(NODE_BATCH, "loop", max_batches=1000)]
        nodes += [node(NODE_TRANSFORM, f"m{index}") for index in range(4)]

        limit = flow_compiler.recursion_limit_for(nodes)

        assert limit == 6 * 1000 + flow_compiler.RECURSION_SLACK
        assert limit > 25

    def test_a_workflow_with_no_loop_still_gets_room(self) -> None:
        nodes = [node("trigger", "start"), node("success", "done")]
        assert flow_compiler.recursion_limit_for(nodes) > len(nodes)

    def test_it_is_capped(self) -> None:
        """A run needing more super-steps than this is one nobody is waiting for the end
        of, and LangGraph's own error is then the right backstop."""
        nodes = [node(NODE_BATCH, f"loop{i}", max_batches=100_000) for i in range(50)]
        assert (
            flow_compiler.recursion_limit_for(nodes)
            == flow_compiler.MAX_RECURSION_LIMIT
        )


class TestCompiling:
    async def test_a_workflow_with_no_trigger_is_refused(self, context) -> None:  # noqa: ANN001
        """A backstop for a version published before the rule existed, raising the
        validator's own type so a caller has one thing to catch."""
        graph = {"nodes": [node("success", "done")], "edges": []}

        with pytest.raises(FlowValidationError, match="no trigger"):
            await flow_compiler.compile_flow(graph, context)

    async def test_every_reachable_node_is_declared_as_a_destination(self) -> None:
        """
        Declared explicitly rather than inferred, so a router returning a name nobody
        wired fails at compile time rather than mid-run at three in the morning.
        """
        targets = {
            ("a", PORT_DEFAULT): "b",
            ("a", PORT_ERROR): "c",
            ("b", PORT_DEFAULT): "d",
        }
        assert flow_compiler._destinations("a", targets) == sorted(["b", "c", END])

    def test_an_inner_loop_wins_over_an_outer_one(self) -> None:
        """A node in a nested body belongs to the loop it is actually in. Phase 1's
        validator does not refuse nesting, so this is not hypothetical."""
        nodes = [
            node("trigger", "start"),
            node(NODE_BATCH, "outer"),
            node(NODE_BATCH, "inner"),
            node(NODE_TRANSFORM, "deep"),
            node(NODE_TRANSFORM, "shallow"),
        ]
        edges = [
            edge("start", "outer"),
            edge("outer", "shallow", PORT_BODY),
            edge("shallow", "inner"),
            edge("inner", "deep", PORT_BODY),
            edge("deep", "inner"),
            edge("inner", "outer", PORT_DONE),
        ]

        enclosing = flow_compiler.enclosing_batches(nodes, edges)

        assert enclosing["deep"] == "inner"
        assert enclosing["shallow"] == "outer"
