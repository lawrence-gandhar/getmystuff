"""
The map-reduce itself: does it read everything exactly once and add up right.

Run with a **hard-coded plan and no language model**. The graph has to be proven
exact before a model is anywhere near it, and a planner failure showing up as an
arithmetic failure would be the worst way to find either.

The load-bearing tests, in the order they matter:

* the aggregate equals what SQLite says for the same question;
* changing the fan-out width does not change a single number;
* every record is read exactly once — not one batch short, not one batch twice;
* every way a run can end releases the cursor and the records.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Dict, List

import pytest

pytest.importorskip(
    "langgraph", reason="langgraph is installed in the container only (see Dockerfile)",
)

from app.services.agent_recursive_dataframes import (  # noqa: E402
    aggregate_graph,
    aggregate_service,
    frame_buffer,
    frame_ops,
    row_supply,
)
from app.services.deep_agents.query_executor import ToolQueryError  # noqa: E402
from app.services.downloader_agents.base import record_reader  # noqa: E402

FULL_PLAN: Dict[str, Any] = {
    "group_by": ["region"],
    "aggregations": [
        {"type": "count", "column": "", "alias": "record_count"},
        {"type": "count", "column": "amount", "alias": "count_amount"},
        {"type": "sum", "column": "amount", "alias": "sum_amount"},
        {"type": "avg", "column": "amount", "alias": "avg_amount"},
        {"type": "min", "column": "amount", "alias": "min_amount"},
        {"type": "max", "column": "amount", "alias": "max_amount"},
    ],
}

GROUP_SQL = """
    SELECT region,
           COUNT(*)      AS record_count,
           COUNT(amount) AS count_amount,
           SUM(amount)   AS sum_amount,
           AVG(amount)   AS avg_amount,
           MIN(amount)   AS min_amount,
           MAX(amount)   AS max_amount
      FROM sales GROUP BY region
"""


async def _sources(entry: Dict[str, Any]) -> list:
    """The tool as the things the reader reads — one source unless it fans out."""
    return (await aggregate_service.record_sources(entry)).sources


async def _supply(entry: Dict[str, Any]):  # noqa: ANN202
    """
    The tool's sources behind the interface the graph reads.

    A ``QuerySupply`` wrapper and not the bare list: the graph asks a supply to count,
    open and release, so that a designed graph's finished result can be read by the same
    nodes. See ``row_supply``.
    """
    return row_supply.for_sources(await _sources(entry))


async def _run(entry: Dict[str, Any], plan: Dict[str, Any] = None) -> Dict[str, Any]:
    return await aggregate_graph.run_aggregation(
        await _supply(entry), plan or FULL_PLAN, "run-under-test",
    )


def _assert_matches(actual: List[dict], expected: List[dict]) -> None:
    by_region = {row["region"]: row for row in expected}
    got = {row["region"]: row for row in actual}

    assert set(got) == set(by_region)

    for region, row in got.items():
        for column, value in by_region[region].items():
            if isinstance(value, float):
                assert row[column] == pytest.approx(value), (region, column)
            else:
                assert row[column] == value, (region, column)


# --- The claim -----------------------------------------------------------


class TestExactness:
    async def test_the_aggregate_equals_what_the_database_says(
        self, tool_entry: Callable, sqlite_answer: Callable,
    ) -> None:
        entry = tool_entry(rows=12_347, regions=5)

        outcome = await _run(entry)

        _assert_matches(
            outcome["rows"], sqlite_answer(entry["datasource"], GROUP_SQL),
        )

    async def test_a_sql_mode_tool_gives_the_same_answer_as_a_builder_one(
        self, tool_entry: Callable, sqlite_answer: Callable,
    ) -> None:
        entry = tool_entry(rows=1_500, sql_query="SELECT * FROM sales")

        outcome = await _run(entry)

        _assert_matches(
            outcome["rows"], sqlite_answer(entry["datasource"], GROUP_SQL),
        )

    @pytest.mark.parametrize("width", [1, 2, 3, 4, 7])
    async def test_the_fan_out_width_does_not_change_a_single_number(
        self, tool_entry: Callable, monkeypatch: pytest.MonkeyPatch, width: int,
    ) -> None:
        """
        The test that proves the parallelism is safe. If widening the wave ever
        changes an answer, the fold is not associative and everything else here is
        decoration.
        """
        entry = tool_entry(rows=2_003)

        monkeypatch.setattr(aggregate_service, "AGGREGATE_WAVE_WIDTH", 1)
        baseline = await _run(entry)

        monkeypatch.setattr(aggregate_service, "AGGREGATE_WAVE_WIDTH", width)

        assert await _run(entry) == baseline

    @pytest.mark.parametrize("chunk", [1, 7, 200, 5_000])
    async def test_the_batch_size_does_not_change_a_single_number(
        self, tool_entry: Callable, monkeypatch: pytest.MonkeyPatch, chunk: int,
    ) -> None:
        entry = tool_entry(rows=613)

        monkeypatch.setattr(aggregate_service, "AGGREGATE_CHUNK_ROWS", 5_000)
        baseline = await _run(entry)

        monkeypatch.setattr(aggregate_service, "AGGREGATE_CHUNK_ROWS", chunk)

        assert await _run(entry) == baseline

    async def test_grouping_by_two_columns_matches_the_database(
        self, tool_entry: Callable, sqlite_answer: Callable,
    ) -> None:
        entry = tool_entry(rows=900)
        plan = {
            "group_by": ["region", "sold_on"],
            "aggregations": [{"type": "count", "column": "", "alias": "n"}],
        }

        outcome = await _run(entry, plan)
        expected = sqlite_answer(
            entry["datasource"],
            "SELECT region, sold_on, COUNT(*) AS n FROM sales "
            "GROUP BY region, sold_on ORDER BY n DESC LIMIT 200",
        )

        assert outcome["group_count"] == len(sqlite_answer(
            entry["datasource"],
            "SELECT 1 FROM sales GROUP BY region, sold_on",
        ))
        assert sorted(r["n"] for r in outcome["rows"]) == sorted(
            r["n"] for r in expected
        )

    async def test_the_all_null_group_reports_nothing_rather_than_zero(
        self, tool_entry: Callable,
    ) -> None:
        entry = tool_entry(rows=400)

        west = {r["region"]: r for r in (await _run(entry))["rows"]}["west"]

        assert west["record_count"] == 4
        assert west["count_amount"] == 0
        assert west["sum_amount"] is None
        assert west["avg_amount"] is None


# --- The loop ------------------------------------------------------------


class TestTheWaveLoop:
    async def test_every_record_is_read_exactly_once(
        self, tool_entry: Callable,
    ) -> None:
        """
        Catches both halves of the classic batch bug at once: a dropped tail batch
        shows up as too few, a re-read one as too many.
        """
        entry = tool_entry(rows=1_777)

        outcome = await _run(entry)

        assert outcome["total_records"] == 1_781      # 1,777 plus the four wests
        assert outcome["records_read"] == outcome["total_records"]
        assert sum(row["record_count"] for row in outcome["rows"]) == 1_781

    @pytest.mark.parametrize("rows", [1, 199, 200, 201, 799, 800, 801])
    async def test_the_batch_and_wave_boundaries_are_all_correct(
        self, tool_entry: Callable, rows: int,
    ) -> None:
        """One record either side of a batch edge and of a wave edge."""
        entry = tool_entry(rows=rows)

        outcome = await _run(entry)

        assert outcome["records_read"] == rows + 4
        assert sum(row["record_count"] for row in outcome["rows"]) == rows + 4

    async def test_the_number_of_waves_is_what_the_wave_size_implies(
        self, tool_entry: Callable,
    ) -> None:
        entry = tool_entry(rows=1_596)          # 1,600 records, exactly two waves
        compiled = aggregate_graph.build_graph(await _supply(entry))

        from app.services.agent_recursive_dataframes.aggregate_state import (
            initial_state,
        )

        try:
            state = await compiled.ainvoke(
                initial_state("wave-count", FULL_PLAN),
                config={"recursion_limit": aggregate_graph._recursion_limit()},
            )
        finally:
            await record_reader.release_reader("agg:wave-count")
            frame_buffer.release_run("wave-count")

        # `wave` counts up once per merged wave and once more for the empty one
        # that ends the loop, so two full waves leave it at 3.
        assert state["wave"] == 3
        assert state["rows_read"] == 1_600
        assert state["finished_reading"] is True

    async def test_the_barrier_holds_and_merge_runs_once_per_wave(
        self, tool_entry: Callable, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        Every slice of a wave must finish before that wave is merged, and the merge
        must happen once — not once per slice. If either were wrong the totals
        would still look plausible, which is why this is asserted rather than
        assumed from the edge list.
        """
        # 1,596 plus the four wests is 1,600 records: eight batches, two whole waves.
        entry = tool_entry(rows=1_596)
        events: List[str] = []
        real_fold, real_merge = frame_ops.partial_aggregate, frame_ops.merge_partials

        def fold(rows, plan):  # noqa: ANN001, ANN202
            events.append("fold-start")
            result = real_fold(rows, plan)
            events.append("fold-end")
            return result

        def merge(running, partials, plan, keep=None):  # noqa: ANN001, ANN202
            events.append(f"merge:{len(partials)}")
            return real_merge(running, partials, plan, keep)

        monkeypatch.setattr(frame_ops, "partial_aggregate", fold)
        monkeypatch.setattr(frame_ops, "merge_partials", merge)

        await _run(entry)

        merges = [event for event in events if event.startswith("merge:")]

        # Two full waves of four slices, each merged in a single call — not one
        # call per slice, which is what a missing barrier would look like.
        assert merges == ["merge:4", "merge:4"]

        # And every slice that was folded was merged exactly once: a wave whose
        # partials outnumbered its folds would be double-counting records.
        folded = events.count("fold-start")
        merged = sum(int(event.split(":")[1]) for event in merges)
        assert merged == folded

        for merge_index in [i for i, e in enumerate(events) if e.startswith("merge:")]:
            before = events[:merge_index]
            assert before.count("fold-start") == before.count("fold-end"), (
                "a wave was merged while one of its slices was still folding"
            )

    async def test_the_recursion_limit_covers_a_full_sized_run(self) -> None:
        per_wave = (
            aggregate_service.AGGREGATE_WAVE_WIDTH
            * aggregate_service.AGGREGATE_CHUNK_ROWS
        )
        waves = aggregate_service.AGGREGATE_MAX_SOURCE_ROWS / per_wave

        # Three super-steps a wave, and headroom for the fixed nodes at both ends.
        assert aggregate_graph._recursion_limit() > 3 * waves


# --- Nothing to do -------------------------------------------------------


class TestEmptyAndTrivial:
    async def test_a_tool_that_returns_nothing_is_an_answer_not_a_failure(
        self, tool_entry: Callable,
    ) -> None:
        entry = tool_entry(rows=0, sql_query="SELECT * FROM sales WHERE 1 = 0")

        outcome = await _run(entry)

        assert outcome["rows"] == []
        assert outcome["group_count"] == 0
        assert outcome["records_read"] == 0

    async def test_a_plan_with_no_grouping_gives_one_total_row(
        self, tool_entry: Callable, sqlite_answer: Callable,
    ) -> None:
        entry = tool_entry(rows=1_000)
        plan = {
            "group_by": [],
            "aggregations": [{"type": "sum", "column": "amount", "alias": "total"}],
        }

        outcome = await _run(entry, plan)
        expected = sqlite_answer(
            entry["datasource"], "SELECT SUM(amount) AS total FROM sales",
        )

        assert len(outcome["rows"]) == 1
        assert outcome["rows"][0]["total"] == pytest.approx(expected[0]["total"])


# --- Refusals ------------------------------------------------------------


class TestCeilings:
    async def test_too_many_records_is_refused_before_any_are_read(
        self, tool_entry: Callable, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        entry = tool_entry(rows=500)
        monkeypatch.setattr(aggregate_service, "AGGREGATE_MAX_SOURCE_ROWS", 100)

        opened: List[str] = []
        monkeypatch.setattr(
            record_reader,
            "get_reader",
            lambda *args, **kwargs: opened.append("opened"),
        )

        with pytest.raises(ToolQueryError) as caught:
            await _run(entry)

        assert "504" in str(caught.value) or "100" in str(caught.value)
        assert not opened, "records were read for a run that was going to be refused"

    async def test_the_refusal_names_the_real_number_and_the_ceiling(
        self, tool_entry: Callable, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        entry = tool_entry(rows=996)
        monkeypatch.setattr(aggregate_service, "AGGREGATE_MAX_SOURCE_ROWS", 500)

        with pytest.raises(ToolQueryError) as caught:
            await _run(entry)

        assert "1,000" in str(caught.value)
        assert "500" in str(caught.value)

    async def test_too_many_groups_discards_rather_than_truncates(
        self, tool_entry: Callable, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        Grouping by the primary key: one group per record, which is what the cap
        exists to catch. Nothing partial may come back.
        """
        entry = tool_entry(rows=1_000)
        monkeypatch.setattr(aggregate_service, "MAX_GROUPS", 300)
        plan = {
            "group_by": ["id"],
            "aggregations": [{"type": "count", "column": "", "alias": "n"}],
        }

        with pytest.raises(ToolQueryError) as caught:
            await _run(entry, plan)

        assert "300" in str(caught.value)
        assert "coarser" in str(caught.value)


class TestFailures:
    async def test_a_failing_slice_aborts_the_run_with_nothing(
        self, tool_entry: Callable, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        The important half is that no partial total comes back. A run that folded
        three of its four batches and reported the answer would be wrong in a way
        nothing about the result would show.
        """
        entry = tool_entry(rows=2_000)
        real = frame_ops.partial_aggregate
        calls = {"n": 0}

        def flaky(rows, plan):  # noqa: ANN001, ANN202
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("the fold broke")
            return real(rows, plan)

        monkeypatch.setattr(frame_ops, "partial_aggregate", flaky)

        with pytest.raises(ToolQueryError, match="could not be grouped"):
            await _run(entry)

    async def test_a_whole_wave_failing_at_once_is_one_failure_not_a_crash(
        self, tool_entry: Callable,
    ) -> None:
        """
        Every slice of a wave fails together whenever the fault is the plan rather
        than the data — a column that is not numeric is not numeric in any of them.
        All four then write the failure in the same super-step, which LangGraph
        refuses on a plain field. It is one fault seen four times, so the state
        keeps the first; without that reducer this raised InvalidUpdateError and
        the run died with no message anyone could act on.
        """
        entry = tool_entry(rows=2_000)
        plan = {
            "group_by": ["region"],
            "aggregations": [{"type": "sum", "column": "sold_on", "alias": "s"}],
        }

        with pytest.raises(ToolQueryError) as caught:
            await _run(entry, plan)

        assert "sold_on" in str(caught.value)
        # One explanation, not four concatenated copies of it.
        assert str(caught.value).count("sold_on") == 1

    async def test_a_plan_naming_an_unknown_column_fails_readably(
        self, tool_entry: Callable,
    ) -> None:
        entry = tool_entry(rows=300)
        plan = {
            "group_by": ["regoin"],
            "aggregations": [{"type": "count", "column": "", "alias": "n"}],
        }

        with pytest.raises(ToolQueryError) as caught:
            await _run(entry, plan)

        assert "regoin" in str(caught.value)
        assert "region" in str(caught.value)

    async def test_a_missing_table_fails_as_a_tool_error_not_a_crash(
        self, tool_entry: Callable,
    ) -> None:
        entry = tool_entry(rows=100)
        entry["table_name"] = "nope"
        entry["table_names"] = ["nope"]

        with pytest.raises(ToolQueryError):
            await _run(entry)


# --- Releasing -----------------------------------------------------------


class TestCleanup:
    """
    The autouse fixture already asserts both registries are empty after every test
    in this module. These name the paths explicitly, because "it happened to be
    clean" and "this path releases" are different claims.
    """

    async def test_a_successful_run_releases_everything(
        self, tool_entry: Callable,
    ) -> None:
        await _run(tool_entry(rows=900))

        assert frame_buffer.open_keys() == 0
        assert not record_reader._readers

    async def test_a_refused_run_releases_everything(
        self, tool_entry: Callable, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(aggregate_service, "AGGREGATE_MAX_SOURCE_ROWS", 10)

        with pytest.raises(ToolQueryError):
            await _run(tool_entry(rows=500))

        assert frame_buffer.open_keys() == 0
        assert not record_reader._readers

    async def test_a_failed_run_releases_everything(
        self, tool_entry: Callable, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            frame_ops,
            "partial_aggregate",
            lambda *args: (_ for _ in ()).throw(RuntimeError("nope")),
        )

        with pytest.raises(ToolQueryError):
            await _run(tool_entry(rows=500))

        assert frame_buffer.open_keys() == 0
        assert not record_reader._readers

    async def test_a_cancelled_run_releases_everything(
        self, tool_entry: Callable, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        The leak the cleanup node cannot cover. A chat turn timing out cancels the
        task mid-node, and a cancelled node routes nowhere — so if the release only
        lived in the graph, this would leave a cursor checked out of the pool for
        the life of the process.
        """
        entry = tool_entry(rows=4_000)
        started = asyncio.Event()
        real = frame_ops.partial_aggregate

        def slow(rows, plan):  # noqa: ANN001, ANN202
            started.set()
            import time
            time.sleep(0.2)
            return real(rows, plan)

        monkeypatch.setattr(frame_ops, "partial_aggregate", slow)

        task = asyncio.create_task(_run(entry))
        await started.wait()
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

        assert frame_buffer.open_keys() == 0
        assert not record_reader._readers

    async def test_the_reader_key_cannot_collide_with_an_export(
        self, tool_entry: Callable,
    ) -> None:
        """
        The registry is shared with the export path. A key that could be mistaken
        for an export's uuid would hand one run the other's cursor position.
        """
        key = aggregate_graph._reader_key("11111111222233334444555566667777")

        assert key.startswith("agg:")

        import uuid as uuid_pkg

        assert key != str(uuid_pkg.UUID("11111111-2222-3333-4444-555566667777"))
