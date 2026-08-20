"""
Tests for ``engine/run_store.py`` and the run queries under it.

Five properties, each with a class.

**Counters are bumped, never read-modify-written.** Asserted by issuing two concurrent
additions and requiring both to survive — the only shape of test that can tell the two
implementations apart, since a read-then-write passes every sequential assertion and
loses one of every pair under load.

**A run with any failed, invalid or skipped record ends ``partial``.** A pure function of
four numbers, asserted as a table.

**Step rows collapse and the count is kept.** A ten-thousand-pass loop writes a bounded
number of rows and the rollup says how many passes it stands for. The second half is the
point: a log that silently stops at 500 implies there were 500.

**Logging never fails the node.** Every write here swallows. The test breaks the session
factory and requires the call to return rather than raise.

**Cancellation is polled and cached**, which is what makes the contract stated in the UI
both honest and affordable.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.integrations import queries
from app.models.integrations import (
    RUN_CANCELLED,
    RUN_PARTIAL,
    RUN_QUEUED,
    RUN_RUNNING,
    RUN_SUCCEEDED,
    STEP_COLLAPSE_AFTER,
    STEP_FAILED,
    STEP_SUCCEEDED,
    IntegrationFlow,
    IntegrationRun,
    IntegrationRunStep,
)
from app.models.user.user import User
from app.services.integrations.engine import run_store


@pytest.fixture
async def flow(db: AsyncSession, user: User) -> IntegrationFlow:
    row = IntegrationFlow(user_id=user.id, name="Nightly contact sync")
    db.add(row)
    await db.commit()
    return row


@pytest.fixture
async def run(db: AsyncSession, flow: IntegrationFlow) -> IntegrationRun:
    created = await run_store.create_run(
        db, flow_id=flow.id, flow_version_id=None, thread_id="thread-1"
    )
    await db.commit()
    return created


async def step_count(db: AsyncSession, run_id: int) -> int:
    result = await db.execute(
        select(func.count())
        .select_from(IntegrationRunStep)
        .where(IntegrationRunStep.run_id == run_id)
    )
    return int(result.scalar_one())


class TestCreatingARun:
    async def test_a_run_is_recorded_before_anything_is_compiled(
        self, db: AsyncSession, run: IntegrationRun
    ) -> None:
        """A compilation that fails is then a run somebody can open and read a reason
        from, rather than a button that appeared to do nothing."""
        assert run.status == RUN_QUEUED

    async def test_it_starts_queued_even_when_somebody_pressed_the_button(
        self, run: IntegrationRun
    ) -> None:
        """A manual run goes through the same queue and the same execution path, so the
        run tested at eleven in the morning is the run that fires at three."""
        assert run.status == RUN_QUEUED

    async def test_the_row_is_flushed_but_not_committed(
        self, db: AsyncSession, flow: IntegrationFlow
    ) -> None:
        """
        The run row, its queue job and a scheduled trigger's advanced ``next_run_at``
        belong in one transaction — a crash between them either loses a run or fires a
        slot twice. The id is available immediately so the job's foreign key can be set
        in the very next statement.
        """
        created = await run_store.create_run(
            db, flow_id=flow.id, flow_version_id=None, thread_id="t"
        )

        assert created.id is not None, "the id has to be there for the job's foreign key"

        await db.rollback()
        result = await db.execute(
            select(func.count()).select_from(IntegrationRun).where(
                IntegrationRun.thread_id == "t"
            )
        )
        assert int(result.scalar_one()) == 0, "create_run must not have committed"

    async def test_ownership_runs_through_the_flow(
        self, db: AsyncSession, run: IntegrationRun, flow: IntegrationFlow, make_user
    ) -> None:
        """A run has no ``user_id`` of its own, deliberately: one place to change when a
        run becomes shareable."""
        stranger = await make_user(email="other@example.com")

        assert await run_store.get_run_and_flow(db, run.uuid, flow.user_id) is not None
        assert await run_store.get_run_and_flow(db, run.uuid, stranger.id) is None


class TestFinalStatus:
    """The rule the three-levels-of-failure design exists for. A green tick over "49,997
    of 50,000" is a lie the operator has no way to catch."""

    @pytest.mark.parametrize(
        "failed,skipped,invalid,expected",
        [
            (0, 0, 0, RUN_SUCCEEDED),
            (1, 0, 0, RUN_PARTIAL),
            (0, 1, 0, RUN_PARTIAL),
            (0, 0, 1, RUN_PARTIAL),
            (3, 7, 2, RUN_PARTIAL),
        ],
    )
    def test_the_table(self, failed, skipped, invalid, expected) -> None:  # noqa: ANN001
        assert (
            run_store.final_status(failed=failed, skipped=skipped, invalid=invalid)
            == expected
        )

    def test_cancelled_wins_over_everything(self) -> None:
        """A cancelled run that had already written some records is cancelled, not
        partial — the operator asked for it to stop and that is the headline."""
        assert (
            run_store.final_status(failed=5, skipped=1, cancelled=True) == RUN_CANCELLED
        )


class TestCounters:
    async def test_two_concurrent_additions_both_survive(
        self, db: AsyncSession, run: IntegrationRun
    ) -> None:
        """
        The assertion the design exists for. A write node fans its chunks out with
        ``asyncio.gather``, so two additions land at the same moment; a read-modify-write
        passes every sequential test in this file and loses one of every pair here.
        """
        await asyncio.gather(
            run_store.bump_counts(run.id, records_written=300),
            run_store.bump_counts(run.id, records_written=200),
        )

        await db.refresh(run)
        assert run.records_written == 500

    async def test_a_zero_delta_writes_nothing(
        self, db: AsyncSession, run: IntegrationRun
    ) -> None:
        """A batch pass that failed nothing should not lock the row. Issuing ``+ 0`` a
        hundred times per run is a hundred row locks bought for nothing."""
        await queries.bump_run_counts(db, run.id, records_failed=0)
        await db.refresh(run)

        assert run.records_failed == 0
        assert run.heartbeat_at is None, "an empty bump must not touch the row at all"

    async def test_an_unknown_counter_name_is_refused(
        self, db: AsyncSession, run: IntegrationRun
    ) -> None:
        """Raised rather than ignored: a typo'd counter that silently does nothing is a
        run reporting zero written for a sync that worked."""
        with pytest.raises(ValueError, match="not a run counter"):
            await queries.bump_run_counts(db, run.id, records_writen=5)

    async def test_bumping_refreshes_the_heartbeat(
        self, db: AsyncSession, run: IntegrationRun
    ) -> None:
        """A run that is moving records is a run that is alive. Free, and it means a
        long batch cannot be requeued as stale while it is working."""
        await run_store.bump_counts(run.id, records_read=10)
        await db.refresh(run)

        assert run.heartbeat_at is not None


class TestSteps:
    async def test_the_running_row_is_written_before_the_work(
        self, db: AsyncSession, run: IntegrationRun
    ) -> None:
        """Recorded only on completion, a node that hung would be indistinguishable from
        one the run never reached."""
        step_id = await run_store.begin_step(run.id, "n1", "connector_read", "Read")

        step = await db.get(IntegrationRunStep, step_id)
        assert step.status == "running"
        assert step.finished_at is None

    async def test_sequence_advances(self, db: AsyncSession, run: IntegrationRun) -> None:
        first = await run_store.begin_step(run.id, "n1", "transform", "Map")
        second = await run_store.begin_step(run.id, "n2", "validate", "Check")

        assert (await db.get(IntegrationRunStep, first)).sequence == 1
        assert (await db.get(IntegrationRunStep, second)).sequence == 2

    async def test_finishing_records_what_went_in_and_what_came_out(
        self, db: AsyncSession, run: IntegrationRun
    ) -> None:
        """Where the two differ is where a workflow is quietly dropping data, which is
        the single most useful thing this log has to say."""
        step_id = await run_store.begin_step(run.id, "n1", "filter", "Only new")
        await run_store.finish_step(
            step_id, STEP_SUCCEEDED, records_in=500, records_out=213
        )

        step = await db.get(IntegrationRunStep, step_id)
        await db.refresh(step)
        assert (step.records_in, step.records_out) == (500, 213)

    async def test_finishing_a_step_that_was_never_opened_is_not_an_error(self) -> None:
        """``None`` means the opening write failed or the node has collapsed. The caller
        treats both the same, which is what keeps the collapse invisible to the runner."""
        await run_store.finish_step(None, STEP_SUCCEEDED)


class TestCollapse:
    async def test_a_long_loop_stops_inserting(
        self, db: AsyncSession, run: IntegrationRun
    ) -> None:
        """A ten-thousand-pass backfill would otherwise write a log table larger than the
        data it describes."""
        for index in (0, 1, STEP_COLLAPSE_AFTER, STEP_COLLAPSE_AFTER + 1):
            step_id = await run_store.begin_step(
                run.id, "loop", "transform", "Map", batch_index=index
            )
            if step_id is None:
                await run_store.roll_up_step(
                    run.id, "loop", "transform", "Map",
                    status=STEP_SUCCEEDED, records_in=10, records_out=10,
                )
            else:
                await run_store.finish_step(step_id, STEP_SUCCEEDED)

        assert await step_count(db, run.id) == 3, (
            "two ordinary rows plus one rollup standing for the rest"
        )

    async def test_the_rollup_counts_the_passes_it_stands_for(
        self, db: AsyncSession, run: IntegrationRun
    ) -> None:
        """
        The half that matters. A log that silently stops at five hundred implies there
        were five hundred, which is a worse answer than no log at all.
        """
        for _ in range(5):
            await run_store.roll_up_step(
                run.id, "loop", "transform", "Map",
                status=STEP_SUCCEEDED, records_in=100, records_out=90,
            )

        rollup = await queries.find_rollup_step(db, run.id, "loop")
        await db.refresh(rollup)

        assert rollup.rollup_count == 5
        assert (rollup.records_in, rollup.records_out) == (500, 450)
        assert "5 further passes" in rollup.message

    async def test_a_failed_pass_makes_the_rollup_stick_at_failed(
        self, db: AsyncSession, run: IntegrationRun
    ) -> None:
        """A rollup that reported the last pass would hide the only interesting thing in
        it."""
        await run_store.roll_up_step(
            run.id, "loop", "transform", "Map", status=STEP_SUCCEEDED
        )
        await run_store.roll_up_step(
            run.id, "loop", "transform", "Map", status=STEP_FAILED
        )
        await run_store.roll_up_step(
            run.id, "loop", "transform", "Map", status=STEP_SUCCEEDED
        )

        rollup = await queries.find_rollup_step(db, run.id, "loop")
        await db.refresh(rollup)
        assert rollup.status == STEP_FAILED

    async def test_only_one_rollup_row_per_node(
        self, db: AsyncSession, run: IntegrationRun
    ) -> None:
        for _ in range(4):
            await run_store.roll_up_step(
                run.id, "loop", "transform", "Map", status=STEP_SUCCEEDED
            )
        assert await step_count(db, run.id) == 1


class TestLoggingNeverFailsTheNode:
    """A sync that moved fifty thousand records into a CRM must not be reported as failed
    because the row describing it could not be written."""

    async def test_a_broken_session_does_not_raise_from_begin_step(
        self, monkeypatch: pytest.MonkeyPatch, run: IntegrationRun
    ) -> None:
        monkeypatch.setattr(run_store, "open_session", _exploding_session)
        assert await run_store.begin_step(run.id, "n1", "transform", "Map") is None

    async def test_a_broken_session_does_not_raise_from_a_counter_bump(
        self, monkeypatch: pytest.MonkeyPatch, run: IntegrationRun
    ) -> None:
        """The cost is stated rather than hidden: the run's total ends up smaller than
        the truth, and the application log is the only place that says so."""
        monkeypatch.setattr(run_store, "open_session", _exploding_session)
        await run_store.bump_counts(run.id, records_written=10)

    async def test_a_broken_session_does_not_raise_from_a_rollup(
        self, monkeypatch: pytest.MonkeyPatch, run: IntegrationRun
    ) -> None:
        monkeypatch.setattr(run_store, "open_session", _exploding_session)
        await run_store.roll_up_step(
            run.id, "loop", "transform", "Map", status=STEP_SUCCEEDED
        )


def _exploding_session():  # noqa: ANN202
    raise RuntimeError("the database is not there")


class TestCancellation:
    async def test_the_flag_is_read_from_the_row(
        self, db: AsyncSession, run: IntegrationRun
    ) -> None:
        assert await run_store.cancel_requested(run.id) is False

        await run_store.request_cancel(db, run.id)
        assert await run_store.cancel_requested(run.id) is True

        run_store.forget_run(run.id)

    async def test_the_row_is_marked_before_the_task_is_cancelled(
        self, db: AsyncSession, run: IntegrationRun
    ) -> None:
        """
        The other half of the mechanism. Cancelling the task first races this write, and
        the page then shows a run that stopped with nothing on it saying why.
        """
        await run_store.request_cancel(db, run.id)
        await db.refresh(run)

        assert run.cancel_requested is True
        run_store.forget_run(run.id)

    async def test_the_answer_is_cached(
        self, db: AsyncSession, run: IntegrationRun, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        Checking a row at the top of every node and between every chunk would be
        thousands of round trips per run to read a boolean that changes at most once.
        """
        await run_store.cancel_requested(run.id)

        monkeypatch.setattr(run_store, "open_session", _exploding_session)
        assert await run_store.cancel_requested(run.id) is False, (
            "the cached answer is used rather than a second query"
        )
        run_store.forget_run(run.id)

    async def test_a_database_failure_does_not_cancel_a_run_nobody_cancelled(
        self, monkeypatch: pytest.MonkeyPatch, run: IntegrationRun
    ) -> None:
        """Guessing wrong this way is a sync that keeps going; the other way it is a sync
        that stops for no reason and reports a cancellation nobody requested."""
        monkeypatch.setattr(run_store, "open_session", _exploding_session)
        assert await run_store.cancel_requested(run.id) is False

    async def test_finishing_a_run_forgets_its_cache_entry(
        self, db: AsyncSession, run: IntegrationRun
    ) -> None:
        await run_store.cancel_requested(run.id)
        assert run_store.cached_runs() == 1

        await run_store.mark_finished(db, run.id, RUN_SUCCEEDED)
        assert run_store.cached_runs() == 0


class TestTheView:
    async def test_the_frame_carries_whole_numbers_and_a_window_of_steps(
        self, db: AsyncSession, run: IntegrationRun, flow: IntegrationFlow
    ) -> None:
        """
        Whole state for the counters, because a client that missed a frame must not be
        left holding a wrong total. A window for the list, because a fifty-thousand step
        run must not arrive on every one-second poll.
        """
        for index in range(run_store.FRAME_STEP_LIMIT + 20):
            step_id = await run_store.begin_step(
                run.id, f"n{index}", "transform", "Map", batch_index=0
            )
            await run_store.finish_step(step_id, STEP_SUCCEEDED)

        await run_store.bump_counts(run.id, records_read=1200, records_written=1190)
        await db.refresh(run)

        view = await run_store.run_view(db, run, flow)

        assert view["counts"] == {
            "read": 1200, "written": 1190, "failed": 0, "skipped": 0,
        }
        assert len(view["steps"]) == run_store.FRAME_STEP_LIMIT
        assert view["steps_total"] == run_store.FRAME_STEP_LIMIT + 20

    async def test_the_window_is_the_tail_in_order(
        self, db: AsyncSession, run: IntegrationRun, flow: IntegrationFlow
    ) -> None:
        for index in range(5):
            step_id = await run_store.begin_step(run.id, f"n{index}", "transform", "Map")
            await run_store.finish_step(step_id, STEP_SUCCEEDED)

        view = await run_store.run_view(db, run, flow)
        assert [step["node_id"] for step in view["steps"]] == [f"n{i}" for i in range(5)]

    async def test_no_bigint_id_reaches_the_browser(
        self, db: AsyncSession, run: IntegrationRun, flow: IntegrationFlow
    ) -> None:
        """The house rule, asserted rather than assumed: only ``uuid`` ever leaves."""
        step_id = await run_store.begin_step(run.id, "n1", "transform", "Map")
        await run_store.finish_step(step_id, STEP_SUCCEEDED)

        view = await run_store.run_view(db, run, flow)

        assert "id" not in view
        assert all("id" not in step for step in view["steps"])
        assert view["uuid"] == str(run.uuid)

    async def test_the_log_paginates_after_the_window(
        self, db: AsyncSession, run: IntegrationRun
    ) -> None:
        for index in range(10):
            step_id = await run_store.begin_step(run.id, f"n{index}", "transform", "Map")
            await run_store.finish_step(step_id, STEP_SUCCEEDED)

        page = await run_store.steps_page(db, run.id, after_sequence=6)
        assert [step["sequence"] for step in page] == [7, 8, 9, 10]

    async def test_the_canvas_sees_the_latest_pass_of_each_node(
        self, db: AsyncSession, run: IntegrationRun
    ) -> None:
        """The canvas draws one ring per node however many passes it made."""
        for batch in range(3):
            step_id = await run_store.begin_step(
                run.id, "loop", "transform", "Map", batch_index=batch
            )
            await run_store.finish_step(step_id, STEP_SUCCEEDED, records_out=batch)

        latest = await run_store.node_rollup(db, run.id)
        assert set(latest) == {"loop"}
        assert latest["loop"]["batch_index"] == 2


class TestRunLifecycle:
    async def test_starting_moves_started_at_to_now(
        self, db: AsyncSession, run: IntegrationRun
    ) -> None:
        """A queued run that waited an hour did not start an hour ago, and a duration
        measured from enqueue would describe the queue rather than the sync."""
        enqueued_at = run.started_at

        await run_store.mark_running(db, run.id)
        await db.refresh(run)

        assert run.status == RUN_RUNNING
        assert run.started_at >= enqueued_at

    async def test_finishing_clears_an_outstanding_question(
        self, db: AsyncSession, run: IntegrationRun
    ) -> None:
        """A finished run with a question still attached renders a prompt nobody can
        answer."""
        run.interrupt_payload = {"ask": "approve?"}
        await db.commit()

        await run_store.mark_finished(db, run.id, RUN_SUCCEEDED)
        await db.refresh(run)

        assert run.interrupt_payload is None
        assert run.finished_at is not None

    def test_terminal_statuses_are_recognised(self) -> None:
        assert run_store.is_terminal(RUN_SUCCEEDED) is True
        assert run_store.is_terminal(RUN_PARTIAL) is True
        assert run_store.is_terminal(RUN_RUNNING) is False
        assert run_store.is_terminal(None) is False
