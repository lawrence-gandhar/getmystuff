"""
Tests for ``engine/run_service.py``.

The second of the two files that need ``langgraph``, and the one that runs a workflow the
way the worker will.

**The fifty-thousand record test is the headline.** It is the assertion that the whole
state design exists for, and it checks four things at once that only appear together at
scale: the counters are exact, the step log is bounded, the record buffer is empty
afterwards, and — the one that stops anyone reintroducing rows into ``outputs`` — the
serialised final state is small.

The rest:

**A run with any failed or skipped record ends ``partial``.** Never ``succeeded``. A green
tick over "49,997 of 50,000" is a lie the operator has no way to catch.

**Stopping marks the row before cancelling the task.** The other order races the write and
leaves a run that stopped with nothing on it saying why.

**Cleanup happens on every path.** Succeeded, failed, cancelled — the buffer and the log
budget are released in a ``finally``, because a cancelled task never routes to a cleanup
node.

**A pinned version runs however the flow is edited afterwards.** That is what makes a
replay a repeat rather than a different workflow that happens to share a name.
"""

from __future__ import annotations

import asyncio
import json

import pytest

pytest.importorskip("langgraph", reason="run_service compiles a graph")

from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app.models.integrations import (  # noqa: E402
    NODE_BATCH,
    NODE_TRANSFORM,
    PORT_BODY,
    PORT_DEFAULT,
    PORT_DONE,
    RUN_CANCELLED,
    RUN_FAILED,
    RUN_MODE_DRY_RUN,
    RUN_PARTIAL,
    RUN_RUNNING,
    RUN_SUCCEEDED,
    STEP_COLLAPSE_AFTER,
    STEP_SKIPPED,
    IntegrationFlow,
    IntegrationFlowVersion,
    IntegrationRun,
    IntegrationRunStep,
)
from app.models.user.user import User  # noqa: E402
from app.services.integrations.engine import (  # noqa: E402
    record_buffer,
    record_log,
    run_service,
    run_store,
)


def node(node_type: str, node_id: str, **data) -> dict:
    return {"id": node_id, "type": node_type, "data": {"label": node_id, **data}}


def edge(source: str, target: str, port: str = PORT_DEFAULT) -> dict:
    return {"id": f"{source}->{target}:{port}", "source": source, "target": target,
            "source_port": port}


def looping_graph(batch_size: int = 500) -> dict:
    """trigger → batch → transform → back, with ``done`` to success."""
    return {
        "nodes": [
            node("trigger", "start"),
            node(NODE_BATCH, "loop", source_node="start", batch_size=batch_size,
                 max_batches=100_000),
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


@pytest.fixture
async def flow(db: AsyncSession, user: User) -> IntegrationFlow:
    row = IntegrationFlow(user_id=user.id, name="Contact sync", graph_data=looping_graph())
    db.add(row)
    await db.commit()
    return row


async def make_run(db: AsyncSession, flow: IntegrationFlow, **kwargs) -> IntegrationRun:
    run = await run_service.begin_run(db, flow, **kwargs)
    await db.commit()
    return run


async def seed(db: AsyncSession, run: IntegrationRun, records: list) -> None:
    """Put the run's records where the trigger will find them, as a webhook body would."""
    run.interrupt_payload = {"records": records}
    await db.commit()


async def step_count(db: AsyncSession, run_id: int) -> int:
    result = await db.execute(
        select(func.count())
        .select_from(IntegrationRunStep)
        .where(IntegrationRunStep.run_id == run_id)
    )
    return int(result.scalar_one())


# ---------------------------------------------------------------------------
# The headline
# ---------------------------------------------------------------------------


class TestFiftyThousandRecords:
    async def test_the_whole_thing(self, db: AsyncSession, flow: IntegrationFlow) -> None:
        """
        Fifty thousand records at five hundred a pass. Four assertions that only appear
        together at scale, each guarding a different piece of the design.
        """
        run = await make_run(db, flow)
        await seed(db, run, [{"n": index} for index in range(50_000)])

        status = await run_service.execute_run(run.id)
        await db.refresh(run)

        assert status == RUN_SUCCEEDED

        # 1. The counters are exact. `_accumulate` sums across a hundred passes; a
        #    last-wins merge would report five hundred.
        preview = run.result_preview or {}
        assert preview["totals"]["mapped"] == 50_000
        assert preview["totals"]["passes"] == 100

        # 2. The step log is bounded. A hundred passes of three nodes would otherwise be
        #    three hundred rows; the collapse keeps it far under what an unbounded log
        #    would write for a ten-thousand-pass backfill.
        rows = await step_count(db, run.id)
        assert rows <= STEP_COLLAPSE_AFTER * 2, f"the log grew to {rows} rows"

        # 3. Nothing is left in process memory. A leak here is an out-of-memory kill in
        #    production with no explanation attached.
        assert record_buffer.open_keys() == []
        assert record_log.open_budgets() == 0

    async def test_the_final_state_stays_small(
        self, db: AsyncSession, flow: IntegrationFlow
    ) -> None:
        """
        The assertion that stops anyone reintroducing rows into ``outputs``. LangGraph
        serialises the whole state to the checkpointer on **every** super-step, so a batch
        of five hundred records in there is written a hundred times to move it three
        nodes. That is not a tuning problem; it is the difference between a sync that
        finishes and one that does not.
        """
        run = await make_run(db, flow)
        await seed(db, run, [{"n": index, "note": "x" * 100} for index in range(5_000)])

        captured: dict = {}
        original = run_service._settle

        async def capture(run_id, final, compiled):  # noqa: ANN001, ANN202
            captured["final"] = dict(final)
            return await original(run_id, final, compiled)

        run_service._settle = capture
        try:
            await run_service.execute_run(run.id)
        finally:
            run_service._settle = original

        serialised = json.dumps(captured["final"], default=str)
        assert len(serialised) < 32_768, (
            f"the state grew to {len(serialised)} bytes — records are travelling in it"
        )


# ---------------------------------------------------------------------------
# How a run ends
# ---------------------------------------------------------------------------


class TestFinalStatus:
    async def test_a_clean_run_succeeds(
        self, db: AsyncSession, flow: IntegrationFlow
    ) -> None:
        run = await make_run(db, flow)
        await seed(db, run, [{"n": 1}, {"n": 2}])

        assert await run_service.execute_run(run.id) == RUN_SUCCEEDED

    async def test_a_run_with_a_bad_record_ends_partial_not_succeeded(
        self, db: AsyncSession, flow: IntegrationFlow
    ) -> None:
        """
        The rule the three-level failure model exists for. A green tick over "2 of 3" is
        a lie the operator has no way to catch — it is the same argument
        ``downloader_agents`` makes about part files.
        """
        run = await make_run(db, flow)
        await seed(db, run, [{"n": 1}, {"n": "abc"}, {"n": 3}])

        status = await run_service.execute_run(run.id)
        await db.refresh(run)

        assert status == RUN_PARTIAL
        assert (run.result_preview or {})["totals"]["failed"] == 1
        assert (run.result_preview or {})["totals"]["mapped"] == 2

    async def test_an_unhandled_node_failure_fails_the_run_with_its_sentence(
        self, db: AsyncSession, user: User
    ) -> None:
        broken = IntegrationFlow(
            user_id=user.id,
            name="Broken",
            graph_data={
                "nodes": [
                    node("trigger", "start"),
                    node("failure", "stop", message="There is nothing to sync."),
                ],
                "edges": [edge("start", "stop")],
            },
        )
        db.add(broken)
        await db.commit()

        run = await make_run(db, broken)
        status = await run_service.execute_run(run.id)
        await db.refresh(run)

        assert status == RUN_FAILED
        assert run.error_message == "There is nothing to sync."

    async def test_a_workflow_that_no_longer_validates_is_refused_before_anything_is_sent(
        self, db: AsyncSession, user: User
    ) -> None:
        """
        Validated a third time, here. The run executes a *pinned version*, and one
        published by an older build may not satisfy a rule added since — a readable
        refusal beats a half-completed sync.
        """
        invalid = IntegrationFlow(
            user_id=user.id,
            name="No trigger",
            graph_data={"nodes": [node("success", "done")], "edges": []},
        )
        db.add(invalid)
        await db.commit()

        run = await make_run(db, invalid)
        status = await run_service.execute_run(run.id)
        await db.refresh(run)

        assert status == RUN_FAILED
        assert "trigger" in run.error_message.lower()

    async def test_a_deleted_flow_ends_as_a_run_with_a_reason(
        self, db: AsyncSession, flow: IntegrationFlow
    ) -> None:
        """Somebody removing a workflow while its 3am job waits is a real sequence, and it
        has to end as a run with a reason rather than an exception in a worker log."""
        run = await make_run(db, flow)
        await db.delete(flow)
        await db.commit()

        status = await run_service.execute_run(run.id)

        assert status == RUN_FAILED

    async def test_a_run_that_no_longer_exists_does_not_raise(self) -> None:
        assert await run_service.execute_run(999_999) == RUN_FAILED


class TestTheLog:
    async def test_a_node_the_run_never_reached_gets_a_skipped_row(
        self, db: AsyncSession, user: User
    ) -> None:
        """
        A node missing from the log is indistinguishable from a node the run never got to,
        and telling those two apart is most of what somebody reading a failed run wants.
        """
        graph = {
            "nodes": [
                node("trigger", "start"),
                node("failure", "stop", message="Stopped."),
                node("success", "never"),
            ],
            "edges": [edge("start", "stop")],
        }
        row = IntegrationFlow(user_id=user.id, name="Partial", graph_data=graph)
        db.add(row)
        await db.commit()

        run = await make_run(db, row)
        await run_service.execute_run(run.id)

        steps = await db.execute(
            select(IntegrationRunStep).where(
                IntegrationRunStep.run_id == run.id,
                IntegrationRunStep.status == STEP_SKIPPED,
            )
        )
        assert [step.node_id for step in steps.scalars().all()] == ["never"]

    async def test_the_run_is_marked_running_before_it_starts(
        self, db: AsyncSession, flow: IntegrationFlow
    ) -> None:
        """A compilation that fails is then a run somebody can open, rather than a button
        that appeared to do nothing."""
        run = await make_run(db, flow)
        seen: list = []

        original = run_service._drive

        async def watch(plan):  # noqa: ANN001, ANN202
            async with run_store.open_session() as session:
                fresh = await run_store.reload_run(session, plan.run_id)
                seen.append(fresh.status)
            return await original(plan)

        run_service._drive = watch
        try:
            await run_service.execute_run(run.id)
        finally:
            run_service._drive = original

        assert seen == [RUN_RUNNING]


# ---------------------------------------------------------------------------
# Stopping
# ---------------------------------------------------------------------------


class TestStopping:
    async def test_the_row_is_marked_before_the_task_is_cancelled(
        self, db: AsyncSession, flow: IntegrationFlow
    ) -> None:
        """
        The other order races the write, and the page then shows a run that stopped with
        nothing on it saying why.
        """
        run = await make_run(db, flow)
        order: list = []

        class Watching:
            def done(self) -> bool:
                return False

            def cancel(self) -> None:
                order.append("task")

        original = run_store.request_cancel

        async def watched(session, run_id):  # noqa: ANN001, ANN202
            order.append("row")
            return await original(session, run_id)

        run_store.request_cancel = watched
        run_service._RUNNING[run.id] = Watching()
        try:
            await run_service.request_stop(db, run)
        finally:
            run_store.request_cancel = original
            run_service._RUNNING.pop(run.id, None)
            run_store.forget_run(run.id)

        assert order == ["row", "task"]

    async def test_a_durably_cancelled_run_stops_and_says_it_was_cancelled(
        self, db: AsyncSession, flow: IntegrationFlow
    ) -> None:
        """The durable half on its own — no local task involved, which is how a run in
        another worker or after a restart is stopped."""
        run = await make_run(db, flow)
        await seed(db, run, [{"n": index} for index in range(100)])
        await run_store.request_cancel(db, run.id)

        status = await run_service.execute_run(run.id)
        await db.refresh(run)

        assert status == RUN_CANCELLED
        assert not run.error_message or "stopped" in run.error_message.lower()

    async def test_a_cancelled_task_settles_the_run_on_its_way_out(
        self, db: AsyncSession, flow: IntegrationFlow
    ) -> None:
        """
        The wiring, asserted by observing that ``execute_run`` settles before it re-raises.

        Split from the write itself deliberately. Cancelling a task mid-query kills the
        one connection the in-memory SQLite test database shares through ``StaticPool``,
        so the *write* cannot be observed here — that is a fact about the harness, not
        about cancellation. What can be observed is that the cancellation path runs at all,
        and the test below covers what it writes.
        """
        run = await make_run(db, flow)
        await seed(db, run, [{"n": index} for index in range(2_000)])

        settled: list = []
        original = run_service._settle_cancelled

        async def watch(run_id):  # noqa: ANN001, ANN202
            settled.append(run_id)

        run_service._settle_cancelled = watch
        try:
            task = asyncio.create_task(run_service.execute_run(run.id))
            await asyncio.sleep(0)
            task.cancel()

            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            run_service._settle_cancelled = original

        assert settled == [run.id]
        assert run_service.live_run_count() == 0

    async def test_settling_a_cancelled_run_marks_the_row(
        self, db: AsyncSession, flow: IntegrationFlow
    ) -> None:
        """
        A run that stopped and never said so is one the queue eventually reaps as a dead
        worker — a different and misleading story, and one that sends somebody looking for
        a crash that did not happen.
        """
        run = await make_run(db, flow)

        await run_service._settle_cancelled(run.id)
        await db.refresh(run)

        assert run.status == RUN_CANCELLED
        assert run.finished_at is not None
        assert "stopped" in run.error_message.lower()

    async def test_stop_all_runs_leaves_nothing_in_flight(
        self, db: AsyncSession, flow: IntegrationFlow
    ) -> None:
        """``on_shutdown``. Without it the tasks are torn down mid-request with the event
        loop and the run rows stay at ``running`` until the queue reaps them."""
        run = await make_run(db, flow)
        await seed(db, run, [{"n": index} for index in range(2_000)])

        asyncio.create_task(run_service.execute_run(run.id))
        await asyncio.sleep(0)

        await run_service.stop_all_runs()

        assert run_service.live_run_count() == 0


class TestCleanupOnEveryPath:
    @pytest.mark.parametrize("records", [[{"n": 1}], [{"n": "abc"}]])
    async def test_the_buffer_is_released_whatever_happened(
        self, db: AsyncSession, flow: IntegrationFlow, records
    ) -> None:  # noqa: ANN001
        run = await make_run(db, flow)
        await seed(db, run, records)

        await run_service.execute_run(run.id)

        assert record_buffer.open_keys() == []
        assert record_log.open_budgets() == 0
        assert run_store.cached_runs() == 0

    async def test_it_is_released_after_a_cancellation_too(
        self, db: AsyncSession, flow: IntegrationFlow
    ) -> None:
        """The path that matters most: a cancelled task never routes to a cleanup node,
        so the ``finally`` is the only thing that runs."""
        run = await make_run(db, flow)
        await seed(db, run, [{"n": index} for index in range(2_000)])
        await run_store.request_cancel(db, run.id)

        await run_service.execute_run(run.id)

        assert record_buffer.open_keys() == []


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------


class TestPinnedVersions:
    async def test_a_pinned_run_ignores_later_edits_to_the_flow(
        self, db: AsyncSession, flow: IntegrationFlow
    ) -> None:
        """
        What the versions table is for. A replay pinned to a version is a *repeat*; one
        that recompiled from the live drawing would be a different workflow that happens
        to share a name — survivable for a query tool, not for something that writes to a
        CRM on a schedule.
        """
        version = IntegrationFlowVersion(
            flow_id=flow.id,
            version_number=1,
            graph_data={
                "nodes": [node("trigger", "start"), node("success", "done")],
                "edges": [edge("start", "done")],
            },
            graph_hash="h1",
            status="published",
        )
        db.add(version)
        await db.commit()

        run = await make_run(db, flow, version=version)

        # The live flow is edited into something that would fail.
        flow.graph_data = {"nodes": [node("success", "orphan")], "edges": []}
        await db.commit()

        assert await run_service.execute_run(run.id) == RUN_SUCCEEDED

    async def test_a_pinned_version_that_was_deleted_says_so(
        self, db: AsyncSession, flow: IntegrationFlow
    ) -> None:
        version = IntegrationFlowVersion(
            flow_id=flow.id, version_number=1,
            graph_data=looping_graph(), graph_hash="h1", status="published",
        )
        db.add(version)
        await db.commit()

        run = await make_run(db, flow, version=version)
        await db.delete(version)
        await db.commit()

        status = await run_service.execute_run(run.id)
        await db.refresh(run)

        assert status == RUN_FAILED
        assert "publish" in run.error_message.lower()


class TestBeginRun:
    async def test_a_run_is_recorded_but_not_committed(
        self, db: AsyncSession, flow: IntegrationFlow
    ) -> None:
        """The run row and its queue job belong in one transaction; a crash between two
        commits either loses a run or leaves a job pointing at nothing."""
        run = await run_service.begin_run(db, flow)
        assert run.id is not None

        await db.rollback()
        remaining = await db.execute(select(func.count()).select_from(IntegrationRun))
        assert int(remaining.scalar_one()) == 0

    async def test_an_unknown_mode_is_refused(
        self, db: AsyncSession, flow: IntegrationFlow
    ) -> None:
        """A dry run that quietly became a live one would write to somebody's production
        system on the strength of a typo."""
        from app.services.integrations.errors import IntegrationFailure

        with pytest.raises(IntegrationFailure, match="not a way to run"):
            await run_service.begin_run(db, flow, mode="sort-of")

    async def test_a_dry_run_reaches_the_runners_as_one(
        self, db: AsyncSession, flow: IntegrationFlow
    ) -> None:
        run = await make_run(db, flow, mode=RUN_MODE_DRY_RUN)
        await seed(db, run, [{"n": 1}])

        seen: list = []
        original = run_service._drive

        async def watch(plan):  # noqa: ANN001, ANN202
            seen.append(plan.dry_run)
            return await original(plan)

        run_service._drive = watch
        try:
            await run_service.execute_run(run.id)
        finally:
            run_service._drive = original

        assert seen == [True]
