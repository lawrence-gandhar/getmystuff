"""
Tests for base/download_graph.py — the export pipeline, end to end.

``pytest.importorskip("langgraph")`` **before** importing the graph module, following
``tests/unit/services/tool_configs/test_tool_chain_graph.py``: langgraph is only installed
in the container, and an import at module scope would collect-error the whole file in a
local run rather than skipping it.

The graph is exercised as a graph — real checkpointer, real interrupt, real resume — and
the datasource under it is a real SQLite file. What is asserted is what a user would
notice:

* the offer states the exact count, in the exact words;
* saying no builds nothing and cannot be re-confirmed later;
* saying yes produces a file containing every record, once;
* a batch that fails twice recovers, and the export completes;
* a batch that fails three times stops the whole export, tells the agent one fixed
  sentence, and leaves nothing on disk.

The last one is the point of the whole retry design, so it asserts all three of those
things rather than just the status.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Callable

import pytest

pytest.importorskip(
    "langgraph", reason="langgraph is installed in the container only (see Dockerfile)",
)

from app.models.downloader_agents import (  # noqa: E402
    EXPORT_DECLINED,
    EXPORT_FAILED,
    EXPORT_READY,
    PART_DISCARDED,
    PART_MERGED,
)
from app.services.downloader_agents.base import download_graph as graph  # noqa: E402
from app.services.downloader_agents.base import download_service as svc  # noqa: E402
from app.services.downloader_agents.base import record_reader, retry  # noqa: E402
from app.services.downloader_agents.csv import csv_writer  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_graph(  # noqa: ANN201
    monkeypatch: pytest.MonkeyPatch,
    upload_root: Path,
    graph_sessions,  # noqa: ANN001
    graph_checkpointer,  # noqa: ANN001
):
    """
    A graph compiled per test, against the in-memory checkpointer.

    The compiled graph is a module global in production — one per process, which is right
    there and wrong here, because a graph compiled in a previous test holds a saver from a
    closed event loop.

    The three fixtures above are each load-bearing and each has a docstring saying why:
    ``upload_root`` puts the export files in ``tmp_path``, ``graph_sessions`` points the
    nodes' own sessions at the test database, and ``graph_checkpointer`` parks the
    interrupt in memory. Without any one of them these tests read or write the
    development database.
    """
    monkeypatch.setattr(graph, "_graph", None)
    monkeypatch.setattr(retry, "_BACKOFF_BASE_SECONDS", 0.0)
    yield
    monkeypatch.setattr(graph, "_graph", None)


async def _offer(db, agent, tool, file_format: str = "csv"):  # noqa: ANN001, ANN202
    """Create the export row and run the graph to its confirmation interrupt."""
    export = await svc.create_offer(
        db, agent.id, tool.id, total_rows=0, file_format=file_format,
    )
    payload = await graph.start_export_offer(str(export.uuid), file_format)

    return export, payload


def _records(path: str | Path) -> list:
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


# ---- The offer ----

class TestTheOffer:
    async def test_the_offer_states_the_exact_count_in_the_agreed_words(
        self, db, make_export_fixtures: Callable,
    ) -> None:
        """
        The sentence is the contract with the user and is produced by the graph, not by
        the model — its wording and its number are not the model's to change.
        """
        agent, tool = await make_export_fixtures(rows=125)

        export, payload = await _offer(db, agent, tool)

        assert payload["total_rows"] == 125
        assert payload["question"] == (
            "There are 125 records. Do you want me to create a downloadable CSV file "
            "containing the list of all the records."
        )

        await db.refresh(export)
        assert export.total_rows == 125
        assert export.count_is_lower_bound is False

    async def test_a_result_past_the_ceiling_is_refused_before_the_offer(
        self, db, make_export_fixtures: Callable, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        Asking someone whether they would like a file and then saying it was never
        possible is worse than saying so straight away — so there is no interrupt at all.
        """
        monkeypatch.setattr(record_reader, "MAX_EXPORT_ROWS", 10)
        agent, tool = await make_export_fixtures(rows=125)

        export, payload = await _offer(db, agent, tool)

        assert payload is None

        await db.refresh(export)
        assert export.status == EXPORT_FAILED
        assert "more than the 10" in export.error_message


# ---- Declining ----

class TestDeclining:
    async def test_saying_no_builds_nothing(
        self, db, make_export_fixtures: Callable, upload_root: Path,
    ) -> None:
        agent, tool = await make_export_fixtures(rows=125)
        export, _payload = await _offer(db, agent, tool)

        state = await graph.resume_export(
            str(export.uuid), confirmed=False, file_format="csv",
        )

        assert state.get("confirmed") is False

        await db.refresh(export)
        assert export.status == EXPORT_DECLINED
        assert export.file_path is None
        assert not (upload_root / "exports" / str(export.uuid) / "parts").exists()

    async def test_a_declined_offer_is_not_found_by_a_later_yes(
        self, db, make_export_fixtures: Callable,
    ) -> None:
        """
        Otherwise a "yes" ten minutes later would resurrect an offer the user turned
        down.
        """
        agent, tool = await make_export_fixtures(rows=125)
        export, _payload = await _offer(db, agent, tool)
        await graph.resume_export(str(export.uuid), confirmed=False, file_format="csv")

        assert await svc.latest_open_offer(db, agent.id) is None


# ---- Building ----

class TestBuilding:
    async def test_a_confirmed_export_contains_every_record_once(
        self, db, make_export_fixtures: Callable,
    ) -> None:
        agent, tool = await make_export_fixtures(rows=125)
        export, _payload = await _offer(db, agent, tool)

        state = await graph.resume_export(
            str(export.uuid), confirmed=True, file_format="csv",
        )

        assert state.get("failure") is None

        await db.refresh(export)
        assert export.status == EXPORT_READY
        assert export.rows_written == 125
        assert export.part_count == 3          # 50 + 50 + 25
        assert export.byte_size > 0
        assert export.checksum
        assert export.expires_at is not None

        records = _records(export.file_path)
        assert [int(row["id"]) for row in records] == list(range(1, 126))

    async def test_the_parts_are_gone_and_recorded_as_merged(
        self, db, make_export_fixtures: Callable, upload_root: Path,
    ) -> None:
        """
        Both halves: the files are deleted, and the rows say so. A part still marked
        `written` with no file is a cleanup that did not run.
        """
        agent, tool = await make_export_fixtures(rows=100)
        export, _payload = await _offer(db, agent, tool)

        await graph.resume_export(str(export.uuid), confirmed=True, file_format="csv")

        assert not (upload_root / "exports" / str(export.uuid) / "parts").exists()

        parts = await svc.part_progress(db, export.id)
        assert [part.status for part in parts] == [PART_MERGED, PART_MERGED]
        assert [part.row_count for part in parts] == [50, 50]

    async def test_the_format_asked_for_at_confirmation_wins(
        self, db, make_export_fixtures: Callable,
    ) -> None:
        """
        The offer says CSV; the user may answer "yes, as a spreadsheet". Building a CSV
        for them would be ignoring the only instruction they gave.
        """
        agent, tool = await make_export_fixtures(rows=60)
        export, _payload = await _offer(db, agent, tool)

        await graph.resume_export(str(export.uuid), confirmed=True, file_format="xls")

        await db.refresh(export)
        assert export.status == EXPORT_READY
        assert export.file_name.endswith(".xlsx")
        assert Path(export.file_path).is_file()

    async def test_a_sql_mode_tool_exports_the_same_way(
        self, db, make_export_fixtures: Callable,
    ) -> None:
        """
        The mode that cannot be paginated with LIMIT/OFFSET at all — worth an end-to-end
        case of its own, not just a reader test.
        """
        agent, tool = await make_export_fixtures(
            rows=125, sql_query="SELECT id, name FROM items WHERE qty > 0",
        )
        export, payload = await _offer(db, agent, tool)

        assert payload["total_rows"] == 108

        await graph.resume_export(str(export.uuid), confirmed=True, file_format="csv")

        await db.refresh(export)
        assert export.rows_written == 108
        assert len(_records(export.file_path)) == 108


# ---- Failing ----

class TestFailing:
    async def test_a_batch_that_fails_twice_recovers(
        self, db, make_export_fixtures: Callable, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        Two transient failures must not cost the user the export — and the recovered
        export must still contain every record exactly once, which is the part a retry
        could plausibly get wrong.
        """
        agent, tool = await make_export_fixtures(rows=125)
        export, _payload = await _offer(db, agent, tool)

        real_write = csv_writer.write_part
        attempts = {"count": 0}

        async def flaky(rows, path):  # noqa: ANN001, ANN202
            if "000002" in str(path):
                attempts["count"] += 1
                if attempts["count"] <= 2:
                    # Leave a fragment, so the discard has something to delete.
                    Path(path).write_text("id,name,qty\n1,partial")
                    raise RuntimeError("connection reset")
            return await real_write(rows, path)

        monkeypatch.setattr(csv_writer, "write_part", flaky)

        state = await graph.resume_export(
            str(export.uuid), confirmed=True, file_format="csv",
        )

        assert state.get("failure") is None

        await db.refresh(export)
        assert export.status == EXPORT_READY
        assert export.rows_written == 125
        assert [int(row["id"]) for row in _records(export.file_path)] == list(
            range(1, 126)
        )

        # The retry history survives, so a recovered export is distinguishable from a
        # clean one afterwards.
        parts = await svc.part_progress(db, export.id)
        discarded = [part for part in parts if part.status == PART_DISCARDED]
        assert [part.attempts for part in discarded] == [1, 2]
        assert all(part.part_number == 2 for part in discarded)

    async def test_three_failures_stop_the_export_and_leave_nothing_behind(
        self, db, make_export_fixtures: Callable, monkeypatch: pytest.MonkeyPatch,
        upload_root: Path,
    ) -> None:
        """
        The whole point of the retry design, asserted in one place.

        Three things have to be true together: the export stops rather than producing a
        partial file, the agent is given one fixed sentence to relay, and every part file
        is gone. A partial export is the one outcome worse than none, because nothing
        about the file says which half is missing.
        """
        agent, tool = await make_export_fixtures(rows=125)
        export, _payload = await _offer(db, agent, tool)

        real_write = csv_writer.write_part
        attempts = {"count": 0}

        async def always_fails(rows, path):  # noqa: ANN001, ANN202
            if "000002" in str(path):
                attempts["count"] += 1
                Path(path).write_text("id,name,qty\n1,partial")
                raise RuntimeError("connection reset")
            return await real_write(rows, path)

        monkeypatch.setattr(csv_writer, "write_part", always_fails)

        state = await graph.resume_export(
            str(export.uuid), confirmed=True, file_format="csv",
        )

        assert attempts["count"] == 3
        assert "Batch 2 failed after 3 attempt(s)" in state["failure"]

        await db.refresh(export)
        assert export.status == EXPORT_FAILED
        assert export.error_message == (
            "The file cannot be created at the moment. Please try again."
        )
        assert export.file_path is None
        # Not "the parts directory is empty" — the whole export directory is gone,
        # including the batch that did succeed.
        assert not (upload_root / "exports" / str(export.uuid)).exists()

    async def test_a_permanent_query_failure_is_not_retried_three_times(
        self, db, make_export_fixtures: Callable, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        A stored query that no longer validates fails once, not three times.

        The user waits a third as long to hear the same thing.
        """
        from app.services.deep_agents.query_executor import ToolQueryError

        agent, tool = await make_export_fixtures(rows=125)
        export, _payload = await _offer(db, agent, tool)

        attempts = {"count": 0}

        async def refuses(rows, path):  # noqa: ANN001, ANN202
            attempts["count"] += 1
            raise ToolQueryError("This tool's saved query is no longer valid.")

        monkeypatch.setattr(csv_writer, "write_part", refuses)

        state = await graph.resume_export(
            str(export.uuid), confirmed=True, file_format="csv",
        )

        assert attempts["count"] == 1
        assert state.get("failure")

        await db.refresh(export)
        assert export.status == EXPORT_FAILED

    async def test_a_merge_failure_fails_the_export_rather_than_publishing(
        self, db, make_export_fixtures: Callable, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        Every batch succeeded and the merge did not. Publishing anyway would mark an
        export ready with no artifact behind its download link.
        """
        agent, tool = await make_export_fixtures(rows=60)
        export, _payload = await _offer(db, agent, tool)

        async def broken_merge(paths, destination):  # noqa: ANN001, ANN202
            raise OSError("no space left on device")

        monkeypatch.setattr(csv_writer, "merge_parts", broken_merge)

        state = await graph.resume_export(
            str(export.uuid), confirmed=True, file_format="csv",
        )

        assert "could not be combined" in state["failure"]

        await db.refresh(export)
        assert export.status == EXPORT_FAILED
        assert export.file_path is None
