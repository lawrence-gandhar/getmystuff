"""
Tests for ``engine/node_runners.py``.

The wrapper first, because everything else depends on it holding: one step row per node,
the timeout, the cancel check, and the broad-except that turns an unexpected fault into a
readable failed step rather than a stack trace on somebody's screen.

Then the runners, and the properties that are easy to get wrong:

**Deltas, never totals.** Asserted by running a node's update through
``flow_state._accumulate`` a hundred times and requiring the sum. A runner returning a
running total passes every single-pass test and makes a fifty-thousand record run report
the size of its last batch.

**Records never travel in the state.** Asserted by measuring the serialised state — the
assertion that stops anyone reintroducing rows into ``outputs``.

**A bad record fails the record, not the node.** Three bad email addresses in fifty
thousand is a fact about the data; failing the node turns it into a failed sync.

**The router and the runner agree.** Both go through the same function, so the port the
log records and the edge the run follows cannot differ.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.integrations import (
    NODE_BATCH,
    NODE_TRANSFORM,
    PORT_BODY,
    PORT_DONE,
    PORT_DROPPED,
    PORT_ELSE,
    PORT_INVALID,
    PORT_KEPT,
    PORT_VALID,
    RECORD_INVALID,
    STEP_FAILED,
    STEP_SUCCEEDED,
    IntegrationFlow,
    IntegrationRun,
    IntegrationRunRecord,
    IntegrationRunStep,
)
from app.models.user.user import User
from app.services.integrations.engine import (
    batching,
    flow_state,
    node_runners,
    record_buffer,
    record_log,
    run_store,
)
from app.services.integrations.errors import NodeFailure, RunCancelled


@pytest.fixture
async def flow(db: AsyncSession, user: User) -> IntegrationFlow:
    row = IntegrationFlow(user_id=user.id, name="Contact sync")
    db.add(row)
    await db.commit()
    return row


@pytest.fixture
async def run(db: AsyncSession, flow: IntegrationFlow) -> IntegrationRun:
    created = await run_store.create_run(
        db, flow_id=flow.id, flow_version_id=None, thread_id="t1"
    )
    await db.commit()
    yield created
    record_log.release_run(created.id)
    run_store.forget_run(created.id)


@pytest.fixture
def context(run: IntegrationRun, flow: IntegrationFlow, integration_sessions):  # noqa: ANN001, ANN201
    """
    A run context, with the buffers released afterwards.

    The release is what ``run_service`` does in a ``finally``, and doing it here rather
    than letting the autouse leak check clean up is deliberate: these tests are the ones
    that put records in the buffer, so they are the ones that have to demonstrate the
    contract.
    """
    built = node_runners.RunContext(
        run_id=run.id,
        run_uuid=str(run.uuid),
        user_id=flow.user_id,
        open_session=integration_sessions,
    )
    yield built
    record_buffer.release_run(built.run_uuid)


def node(node_type: str, node_id: str = "n1", **data) -> dict:
    return {"id": node_id, "type": node_type, "data": {"label": node_id, **data}}


def state(**overrides) -> dict:
    base = flow_state.initial_state(run_id="r", version_hash="v")
    base.update(overrides)
    return base


def stash(context, node_id: str, records) -> dict:  # noqa: ANN001
    """Put a batch where a node reading from ``node_id`` will find it."""
    return {node_id: node_runners._emit(context, node_id, records)}


async def step_rows(db: AsyncSession, run_id: int) -> list:
    result = await db.execute(
        select(IntegrationRunStep)
        .where(IntegrationRunStep.run_id == run_id)
        .order_by(IntegrationRunStep.sequence)
    )
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# The wrapper
# ---------------------------------------------------------------------------


class TestRunNode:
    async def test_one_step_row_per_node_opened_before_the_work(
        self, db: AsyncSession, run: IntegrationRun, context
    ) -> None:  # noqa: ANN001
        await node_runners.run_node(node("success"), state(), context)

        rows = await step_rows(db, run.id)
        assert len(rows) == 1
        assert rows[0].status == STEP_SUCCEEDED
        assert rows[0].node_type == "success"

    async def test_a_failure_is_logged_before_it_is_re_raised(
        self, db: AsyncSession, run: IntegrationRun, context
    ) -> None:  # noqa: ANN001
        """A run that ends on a failure must not be missing the row explaining why."""
        with pytest.raises(NodeFailure):
            await node_runners.run_node(
                node("failure", message="Nothing to sync."), state(), context
            )

        rows = await step_rows(db, run.id)
        assert rows[0].status == STEP_FAILED
        assert rows[0].message == "Nothing to sync."

    async def test_an_unexpected_fault_becomes_a_readable_failed_step(
        self, db: AsyncSession, run: IntegrationRun, context, monkeypatch
    ) -> None:  # noqa: ANN001
        """
        Deliberately broad and deliberately not shown. A client error can name internal
        hosts and echo values; the operator gets a fixed sentence and the real reason goes
        to the application log.
        """
        async def explode(*_args, **_kwargs):  # noqa: ANN202
            raise KeyError("internal-host-3.corp")

        monkeypatch.setitem(node_runners._RUNNERS, "success", explode)

        with pytest.raises(NodeFailure) as caught:
            await node_runners.run_node(node("success"), state(), context)

        assert "internal-host-3" not in str(caught.value)
        assert "has been logged" in str(caught.value)
        assert (await step_rows(db, run.id))[0].status == STEP_FAILED

    async def test_a_hung_node_is_stopped_and_fails(
        self, db: AsyncSession, run: IntegrationRun, context, monkeypatch
    ) -> None:  # noqa: ANN001
        """
        Graph Designer has no per-node timeout because somebody is always watching. Here
        nobody is, and without one the run sits at ``running`` with a step row that never
        closes.
        """
        async def hang(*_args, **_kwargs):  # noqa: ANN202
            await asyncio.sleep(30)

        monkeypatch.setitem(node_runners._RUNNERS, "success", hang)

        with pytest.raises(NodeFailure) as caught:
            await node_runners.run_node(
                node("success", timeout_seconds=1), state(), context
            )

        assert "did not finish within 1 seconds" in str(caught.value)
        assert caught.value.retryable is True
        assert (await step_rows(db, run.id))[0].status == STEP_FAILED

    async def test_the_timeout_is_clamped(self) -> None:
        assert node_runners._timeout_for(node("success")) == (
            node_runners.DEFAULT_NODE_TIMEOUT_SECONDS
        )
        assert node_runners._timeout_for(node("success", timeout_seconds=99_999)) == (
            node_runners.MAX_NODE_TIMEOUT_SECONDS
        )

    async def test_a_cancelled_run_does_not_open_a_step_row(
        self, db: AsyncSession, run: IntegrationRun, context
    ) -> None:  # noqa: ANN001
        """A cancelled run must not accumulate a ``running`` row per node it declined to
        execute — and cancellation is not a failure, so it gets no failed row either."""
        await run_store.request_cancel(db, run.id)

        with pytest.raises(RunCancelled):
            await node_runners.run_node(node("success"), state(), context)

        assert await step_rows(db, run.id) == []

    async def test_an_unregistered_type_says_so_rather_than_raising_key_error(
        self, context
    ) -> None:  # noqa: ANN001
        """Unreachable through the canvas. Handled anyway, because a row edited by hand in
        psql is somebody's Tuesday."""
        with pytest.raises(NodeFailure, match="cannot run"):
            await node_runners.run_node(node("agent"), state(), context)

    async def test_the_registry_and_the_validator_agree(self) -> None:
        """Asserted at import as well; here so a failure names the mechanism. If the two
        could drift, the canvas would offer a node that fails at run time."""
        from app.services.integrations.engine import flow_rules

        assert node_runners.registered_types() == set(flow_rules.IMPLEMENTED_NODE_TYPES)


# ---------------------------------------------------------------------------
# The runners
# ---------------------------------------------------------------------------


class TestTransform:
    async def test_records_are_mapped_into_the_destination_shape(
        self, context
    ) -> None:  # noqa: ANN001
        source = stash(context, "src", [{"customer": {"email": "ADA@X.COM"}}])
        step = node(
            NODE_TRANSFORM, "map",
            source_node="src",
            mappings=[{"source": "customer.email", "target": "email", "transform": "lower"}],
        )

        update = await node_runners.run_node(step, state(outputs=source), context)

        handle = update["outputs"]["map"]
        assert record_buffer.peek(handle["key"]) == [{"email": "ada@x.com"}]

    async def test_a_bad_record_fails_the_record_not_the_node(
        self, db: AsyncSession, run: IntegrationRun, context
    ) -> None:  # noqa: ANN001
        """
        Three bad email addresses in fifty thousand records is a fact about the data.
        Failing the node turns it into a failed sync, which is the collapse the
        three-level failure model exists to prevent.
        """
        source = stash(context, "src", [{"qty": "5"}, {"qty": "abc"}, {"qty": "7"}])
        step = node(
            NODE_TRANSFORM, "map",
            source_node="src",
            mappings=[{"source": "qty", "target": "qty", "type": "integer"}],
        )

        update = await node_runners.run_node(step, state(outputs=source), context)

        assert update["counts"]["map"] == {"mapped": 2, "failed": 1}

        logged = await db.execute(
            select(func.count()).select_from(IntegrationRunRecord).where(
                IntegrationRunRecord.run_id == run.id,
                IntegrationRunRecord.outcome == RECORD_INVALID,
            )
        )
        assert int(logged.scalar_one()) == 1

    async def test_a_broken_mapping_list_fails_the_node(self, context) -> None:  # noqa: ANN001
        """Every record would fail the same way. That is the author's mistake, not the
        data's, so it stops the step instead of writing fifty thousand identical rows."""
        source = stash(context, "src", [{"a": 1}])
        step = node(
            NODE_TRANSFORM, "map",
            source_node="src",
            mappings=[{"source": "a", "target": "x", "transform": "uppercaseify"}],
        )

        with pytest.raises(NodeFailure, match="uppercaseify"):
            await node_runners.run_node(step, state(outputs=source), context)

    async def test_the_counts_are_deltas(self, context) -> None:  # noqa: ANN001
        """
        The single bug the state design exists to prevent. Accumulated a hundred times, a
        delta sums to a hundred; a total stays at one.
        """
        source = stash(context, "src", [{"a": 1}])
        step = node(
            NODE_TRANSFORM, "map", source_node="src",
            mappings=[{"source": "a", "target": "a"}],
        )

        update = await node_runners.run_node(step, state(outputs=source), context)

        accumulated: dict = {}
        for _ in range(100):
            accumulated = flow_state._accumulate(accumulated, update["counts"])

        assert accumulated["map"]["mapped"] == 100


class TestValidate:
    async def test_a_batch_splits_across_two_ports(self, context) -> None:  # noqa: ANN001
        source = stash(
            context, "src",
            [{"email": "a@b.com"}, {"name": "no email"}, {"email": "c@d.com"}],
        )
        step = node(
            "validate", "check",
            source_node="src",
            rules=[{"field": "email", "type": "string", "required": True}],
        )

        update = await node_runners.run_node(step, state(outputs=source), context)

        ports = update["outputs"]["check"]["ports"]
        assert record_buffer.peek(ports[PORT_VALID]["key"]) == [
            {"email": "a@b.com"}, {"email": "c@d.com"}
        ]
        assert len(record_buffer.peek(ports[PORT_INVALID]["key"])) == 1

    async def test_both_ports_get_a_handle_even_when_one_is_empty(
        self, context
    ) -> None:  # noqa: ANN001
        """A downstream node reading a port with no handle would be an error, and "nothing
        was invalid" is not an error."""
        source = stash(context, "src", [{"email": "a@b.com"}])
        step = node(
            "validate", "check", source_node="src",
            rules=[{"field": "email", "required": True}],
        )

        update = await node_runners.run_node(step, state(outputs=source), context)

        ports = update["outputs"]["check"]["ports"]
        assert record_buffer.peek(ports[PORT_INVALID]["key"]) == []


class TestFilter:
    async def test_matching_records_are_kept(self, context) -> None:  # noqa: ANN001
        source = stash(
            context, "src", [{"status": "paid"}, {"status": "void"}, {"status": "paid"}]
        )
        step = node(
            "filter", "only_paid",
            source_node="src",
            specs=[{"column": "status", "operator": "==", "values": ["paid"]}],
        )

        update = await node_runners.run_node(step, state(outputs=source), context)

        ports = update["outputs"]["only_paid"]["ports"]
        assert len(record_buffer.peek(ports[PORT_KEPT]["key"])) == 2
        assert len(record_buffer.peek(ports[PORT_DROPPED]["key"])) == 1

    async def test_a_dropped_record_is_not_logged(
        self, db: AsyncSession, run: IntegrationRun, context
    ) -> None:  # noqa: ANN001
        """
        Filtering is what the author asked for. A hundred thousand rows saying "this
        record did not match your filter" is a log nobody reads, hiding the rows somebody
        needs.
        """
        source = stash(context, "src", [{"status": "void"}] * 10)
        step = node(
            "filter", "only_paid", source_node="src",
            specs=[{"column": "status", "operator": "==", "values": ["paid"]}],
        )

        await node_runners.run_node(step, state(outputs=source), context)

        logged = await db.execute(
            select(func.count()).select_from(IntegrationRunRecord).where(
                IntegrationRunRecord.run_id == run.id
            )
        )
        assert int(logged.scalar_one()) == 0


class TestBatch:
    async def test_each_pass_hands_out_one_batch(self, context) -> None:  # noqa: ANN001
        supply_key = f"{context.run_uuid}:src:supply"
        record_buffer.stash(supply_key, batching.ListSupply([{"n": i} for i in range(12)]))
        outputs = {"src": {"kind": "supply", "key": supply_key}}

        step = node(NODE_BATCH, "loop", source_node="src", batch_size=5)
        current = state(outputs=outputs)

        first = await node_runners.run_node(step, current, context)
        assert first["counts"]["loop"] == {"passes": 1, "read": 5}

        current["counts"] = flow_state._accumulate(current["counts"], first["counts"])
        current["outputs"].update(first["outputs"])

        second = await node_runners.run_node(step, current, context)
        assert second["counts"]["loop"]["read"] == 5

    async def test_an_exhausted_source_routes_to_done(self, context) -> None:  # noqa: ANN001
        supply_key = f"{context.run_uuid}:src:supply"
        record_buffer.stash(supply_key, batching.ListSupply([]))
        step = node(NODE_BATCH, "loop", source_node="src")

        update = await node_runners.run_node(
            step, state(outputs={"src": {"kind": "supply", "key": supply_key}}), context
        )

        assert update["outputs"]["loop"]["kind"] == "exhausted"
        assert node_runners.batch_port(step, {"outputs": update["outputs"]}) == PORT_DONE

    async def test_a_batch_with_records_routes_to_the_body(self, context) -> None:  # noqa: ANN001
        supply_key = f"{context.run_uuid}:src:supply"
        record_buffer.stash(supply_key, batching.ListSupply([{"n": 1}]))
        step = node(NODE_BATCH, "loop", source_node="src")

        update = await node_runners.run_node(
            step, state(outputs={"src": {"kind": "supply", "key": supply_key}}), context
        )

        assert node_runners.batch_port(step, {"outputs": update["outputs"]}) == PORT_BODY

    async def test_the_pass_limit_stops_the_loop_and_says_so(
        self, db: AsyncSession, run: IntegrationRun, context
    ) -> None:  # noqa: ANN001
        """
        "Read everything" and "stopped after N passes" are different facts, and a run
        reporting the second as the first is a backfill somebody believes finished.

        The sentence is asserted on the **step row** rather than on the returned update,
        because that is where the operator reads it — ``run_node`` moves it there and
        strips it from the state, which is what keeps a hundred-pass loop from carrying a
        hundred messages in ``outputs``.
        """
        supply_key = f"{context.run_uuid}:src:supply"
        record_buffer.stash(supply_key, batching.ListSupply([{"n": i} for i in range(50)]))
        step = node(NODE_BATCH, "loop", source_node="src", batch_size=1, max_batches=2)

        current = state(outputs={"src": {"kind": "supply", "key": supply_key}})
        current["counts"] = {"loop": {"passes": 2}}

        update = await node_runners.run_node(step, current, context)

        assert update["outputs"]["loop"]["reason"] == "limit"
        assert node_runners.batch_port(step, {"outputs": update["outputs"]}) == PORT_DONE

        rows = await step_rows(db, run.id)
        assert "there may be more" in rows[-1].message.lower()

    async def test_a_batch_wired_to_nothing_says_so(self, context) -> None:  # noqa: ANN001
        step = node(NODE_BATCH, "loop", source_node="missing")
        with pytest.raises(NodeFailure, match="not connected"):
            await node_runners.run_node(step, state(), context)


class TestBranch:
    def test_first_match_wins(self) -> None:
        step = node(
            "branch", "size",
            conditions=[
                {"column": "read", "operator": ">", "values": [100], "port": "big"},
                {"column": "read", "operator": ">", "values": [0], "port": "small"},
            ],
        )
        current = {"counts": {"loop": {"read": 500}}, "inputs": {}}

        assert node_runners.branch_port(step, current) == "big"

    def test_else_when_nothing_matches(self) -> None:
        step = node(
            "branch", "size",
            conditions=[{"column": "read", "operator": ">", "values": [100], "port": "big"}],
        )
        assert node_runners.branch_port(step, {"counts": {}, "inputs": {}}) == PORT_ELSE

    async def test_the_runner_and_the_router_agree(self, context) -> None:  # noqa: ANN001
        """Both go through the same function, so the port the log records and the edge the
        run follows cannot differ."""
        step = node(
            "branch", "size",
            conditions=[{"column": "read", "operator": ">", "values": [0], "port": "some"}],
        )
        current = state(counts={"loop": {"read": 5}})

        update = await node_runners.run_node(step, current, context)

        assert update["outputs"]["size"]["port"] == node_runners.branch_port(step, current)


class TestPorts:
    def test_validate_takes_invalid_only_when_nothing_was_valid(self) -> None:
        step = node("validate", "check")
        assert node_runners.validate_port(step, {"counts": {"check": {"valid": 3, "invalid": 1}}}) == PORT_VALID
        assert node_runners.validate_port(step, {"counts": {"check": {"valid": 0, "invalid": 4}}}) == PORT_INVALID

    def test_filter_takes_dropped_only_when_nothing_was_kept(self) -> None:
        step = node("filter", "f")
        assert node_runners.filter_port(step, {"counts": {"f": {"kept": 1, "dropped": 9}}}) == PORT_KEPT
        assert node_runners.filter_port(step, {"counts": {"f": {"kept": 0, "dropped": 9}}}) == PORT_DROPPED


class TestRecordsNeverTravelInTheState:
    async def test_the_state_stays_small_however_many_records_move(
        self, context
    ) -> None:  # noqa: ANN001
        """
        The assertion that stops anyone reintroducing rows into ``outputs``. LangGraph
        serialises the whole state on every super-step, so five hundred records in there
        would be written a hundred times to move them three nodes.
        """
        records = [{"email": f"{index}@example.com", "note": "x" * 200} for index in range(500)]
        source = stash(context, "src", records)
        step = node(
            NODE_TRANSFORM, "map", source_node="src",
            mappings=[{"source": "email", "target": "email"}],
        )

        update = await node_runners.run_node(step, state(outputs=source), context)

        assert len(json.dumps(update["outputs"], default=str)) < 1024

    async def test_a_node_reading_from_nothing_says_so(self, context) -> None:  # noqa: ANN001
        step = node(NODE_TRANSFORM, "map", source_node="ghost",
                    mappings=[{"source": "a", "target": "a"}])
        with pytest.raises(NodeFailure, match="has not produced"):
            await node_runners.run_node(step, state(), context)

    async def test_a_node_with_no_source_and_no_loop_says_so(
        self, context
    ) -> None:  # noqa: ANN001
        step = node(NODE_TRANSFORM, "map", mappings=[{"source": "a", "target": "a"}])
        with pytest.raises(NodeFailure, match="does not say which step"):
            await node_runners.run_node(step, state(), context)

    async def test_a_node_inside_a_loop_needs_no_wiring(self, context) -> None:  # noqa: ANN001
        """The common case — a chain of steps inside a loop body — reads the enclosing
        batch without the author drawing a second connection for it."""
        inner = context.inside("loop")
        source = stash(inner, "loop", [{"a": 1}])
        step = node(NODE_TRANSFORM, "map", mappings=[{"source": "a", "target": "a"}])

        update = await node_runners.run_node(step, state(outputs=source), inner)

        assert update["counts"]["map"]["mapped"] == 1


class TestEnds:
    async def test_success_carries_the_totals(self, context) -> None:  # noqa: ANN001
        current = state(counts={"w": {"written": 40}, "x": {"written": 10}})
        update = await node_runners.run_node(node("success", "end"), current, context)

        assert update["outputs"]["end"]["totals"]["written"] == 50

    async def test_failure_raises_rather_than_returning(self, context) -> None:  # noqa: ANN001
        """Reaching a failure end *is* the run failing. ``run_service`` decides the run's
        status from what came out of the graph, so returning quietly would record it as a
        success."""
        with pytest.raises(NodeFailure, match="No orders"):
            await node_runners.run_node(
                node("failure", "stop", message="No orders to sync."), state(), context
            )


class TestTrigger:
    async def test_a_payload_becomes_a_recordset(self, context) -> None:  # noqa: ANN001
        """A webhook body or a manual run carrying records. Stashed like any other
        recordset so a ``batch`` node can loop over it without a second code path."""
        current = state(inputs={"records": [{"a": 1}, {"a": 2}]})

        update = await node_runners.run_node(node("trigger", "start"), current, context)

        handle = update["outputs"]["start"]
        assert record_buffer.is_handle(handle)
        assert handle["count"] == 2

    async def test_a_trigger_with_no_records_still_gets_a_step_row(
        self, db: AsyncSession, run: IntegrationRun, context
    ) -> None:  # noqa: ANN001
        """"What fired this and with what" is the first question anybody asks of a run
        that did something unexpected."""
        await node_runners.run_node(node("trigger", "start"), state(), context)

        rows = await step_rows(db, run.id)
        assert len(rows) == 1
