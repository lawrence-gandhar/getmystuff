"""
Tests for base/retry.py and base/part_store.py.

The retry rule is the user's, stated exactly: three attempts per batch, the part file
deleted before each retry, and a hard stop after the third. So the tests count attempts
and assert what happened to the file, because "it retried" without "and it cleaned up
first" would leave a fragment that the merge would happily fold into the artifact.

``ToolQueryError`` not being retried is the other half and gets its own test. A stored
query that no longer validates fails identically three times; retrying it only makes the
user wait three times as long to hear the same thing.

For part_store the interesting cases are the path rules — the ones that stop a table name
from the user's own database becoming a path — and the two cleanup functions, which are
the difference between a failed export leaving nothing behind and leaving fragments.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.deep_agents.query_executor import ToolQueryError
from app.services.downloader_agents.base import part_store
from app.services.downloader_agents.base.retry import (
    MAX_BATCH_ATTEMPTS,
    BatchRetriesExhausted,
    run_batch_with_retries,
)


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Drop the wait between attempts.

    The backoff is real and correct in production; a test that slept 1.5s per retry case
    would make the suite worse without asserting anything the counters do not.
    """
    from app.services.downloader_agents.base import retry as retry_module

    monkeypatch.setattr(retry_module, "_BACKOFF_BASE_SECONDS", 0.0)


# ---- Retry ----

class TestRunBatchWithRetries:
    async def test_a_clean_batch_runs_once(self) -> None:
        attempts = []

        async def operation(attempt: int) -> str:
            attempts.append(attempt)
            return "written"

        assert await run_batch_with_retries(operation, batch_number=1) == "written"
        assert attempts == [1]

    async def test_it_recovers_on_the_third_attempt(self) -> None:
        """Two transient failures then success is a completed export, not a failed one."""
        attempts = []

        async def operation(attempt: int) -> str:
            attempts.append(attempt)
            if attempt < 3:
                raise RuntimeError("connection reset")
            return "written"

        assert await run_batch_with_retries(operation, batch_number=4) == "written"
        assert attempts == [1, 2, 3]

    async def test_it_gives_up_after_three_attempts(self) -> None:
        attempts = []

        async def operation(attempt: int) -> None:
            attempts.append(attempt)
            raise RuntimeError("connection reset")

        with pytest.raises(BatchRetriesExhausted) as excinfo:
            await run_batch_with_retries(operation, batch_number=7)

        assert attempts == [1, 2, 3]
        assert MAX_BATCH_ATTEMPTS == 3
        assert excinfo.value.batch_number == 7
        assert excinfo.value.attempts == 3
        # The underlying cause is carried for the log, not for the visitor.
        assert isinstance(excinfo.value.last_error, RuntimeError)

    async def test_the_part_is_discarded_after_every_failed_attempt(self) -> None:
        """
        Including the last one.

        A fragment from attempt three is exactly as much of a lie as one from attempt
        one, and the cleanup node should not be the first thing to notice it.
        """
        discards = []

        async def operation(attempt: int) -> None:
            raise RuntimeError("boom")

        async def on_discard(batch: int, attempt: int, exc: BaseException) -> None:
            discards.append((batch, attempt, str(exc)))

        with pytest.raises(BatchRetriesExhausted):
            await run_batch_with_retries(
                operation, batch_number=2, on_discard=on_discard,
            )

        assert [(batch, attempt) for batch, attempt, _ in discards] == [
            (2, 1), (2, 2), (2, 3),
        ]

    async def test_a_tool_query_error_is_not_retried(self) -> None:
        """
        A permanent failure fails once.

        A switched-off table or a stored query that no longer validates will fail
        identically every time; three attempts buys nothing but three times the wait.
        """
        attempts = []

        async def operation(attempt: int) -> None:
            attempts.append(attempt)
            raise ToolQueryError("This tool's saved query is no longer valid.")

        with pytest.raises(ToolQueryError):
            await run_batch_with_retries(operation, batch_number=1)

        assert attempts == [1]

    async def test_a_failing_discard_does_not_replace_the_real_failure(self) -> None:
        """
        The cleanup runs because something already went wrong.

        If it raised on the way, the diagnosis would be replaced by a tidying-up error and
        the actual cause would be lost.
        """

        async def operation(attempt: int) -> None:
            raise RuntimeError("the real problem")

        async def on_discard(batch: int, attempt: int, exc: BaseException) -> None:
            raise OSError("could not delete the part file")

        with pytest.raises(BatchRetriesExhausted) as excinfo:
            await run_batch_with_retries(
                operation, batch_number=1, on_discard=on_discard,
            )

        assert "the real problem" in str(excinfo.value.last_error)


# ---- Paths ----

class TestPaths:
    def test_part_files_are_zero_padded_and_sort_in_order(self) -> None:
        """
        The merge relies on filename order being part order.

        Unpadded names sort 1, 10, 100, 2 — which produces a valid file containing the
        right records in the wrong sequence, the sort of wrong nothing downstream notices.
        """
        names = [
            part_store.part_path("e", number, ".csv").name
            for number in (1, 2, 10, 100, 1000)
        ]

        assert names == sorted(names)
        assert names[0] == "part-000001.csv"

    def test_the_artifact_name_cannot_become_a_path(self) -> None:
        """
        The table name comes from the user's own database, so it can be anything — and it
        ends up in a Content-Disposition header.
        """
        name = part_store.artifact_name("../../etc/passwd", ".csv")

        assert "/" not in name
        assert ".." not in name
        assert name.endswith(".csv")

    def test_the_artifact_name_carries_the_date(self) -> None:
        """
        Two exports of the same tool a week apart should not collide in a downloads
        folder.
        """
        from datetime import date

        assert part_store.artifact_name(
            "inventory_items", ".xlsx", on=date(2026, 8, 6),
        ) == "inventory_items_2026-08-06.xlsx"

    def test_a_path_outside_the_export_is_refused(self, upload_root: Path) -> None:
        """
        The download route opens whatever ``file_path`` says.

        The row was written by this application, so this is not defence against an
        attacker — it is defence against a path built wrongly, which would serve one user
        another user's file.
        """
        with pytest.raises(ValueError):
            part_store.resolve_within_export("export-a", "/etc/passwd")

        with pytest.raises(ValueError):
            part_store.resolve_within_export(
                "export-a", str(part_store.export_dir("export-b") / "theirs.csv"),
            )

    def test_a_path_inside_the_export_resolves(self, upload_root: Path) -> None:
        inside = part_store.part_path("export-a", 1, ".csv")

        assert part_store.resolve_within_export("export-a", str(inside)).name == (
            "part-000001.csv"
        )

    def test_the_artifact_lives_under_the_session_not_the_export(
        self, upload_root: Path,
    ) -> None:
        """
        The path and the download URL are the same two facts.

        ``/file_downloaders/<session>/<file>`` is served by reading
        ``uploads/file_downloaders/<session>/<file>``, so a change to one of these that
        is not a change to the other breaks every link.
        """
        path = part_store.artifact_path("sess-a", "mine.csv")

        assert path.parent.name == "sess-a"
        assert path.parent.parent.name == "file_downloaders"
        assert path.name == "mine.csv"

    def test_a_session_token_cannot_climb_out_of_the_download_root(
        self, upload_root: Path,
    ) -> None:
        """
        The token is minted by the browser and arrives in a query string.

        Joined onto a path as it stands, ``../../etc`` would write outside the download
        root — so the folder name is normalised before it is ever a path segment.
        """
        path = part_store.artifact_path("../../etc", "passwd.csv")

        assert part_store.download_dir("../../etc").parent.name == "file_downloaders"
        assert ".." not in str(path)

    def test_a_path_outside_the_session_is_refused(self, upload_root: Path) -> None:
        """The check that keeps one session's URL from resolving into another's folder."""
        with pytest.raises(ValueError):
            part_store.resolve_within_downloads("sess-a", "/etc/passwd")

        with pytest.raises(ValueError):
            part_store.resolve_within_downloads(
                "sess-a", str(part_store.artifact_path("sess-b", "theirs.csv")),
            )

        inside = part_store.artifact_path("sess-a", "mine.csv")
        assert part_store.resolve_within_downloads("sess-a", str(inside)).name == (
            "mine.csv"
        )

    async def test_a_second_export_of_one_table_does_not_overwrite_the_first(
        self, upload_root: Path,
    ) -> None:
        """
        The cost of grouping by session rather than by export uuid.

        ``artifact_name`` is the table plus the date, so asking for the same tool twice
        in one afternoon wants the same name twice. Writing both to one path would leave
        the first download serving the second export's bytes — the same number of
        records, from a different query, with nothing to show anything was wrong.
        """
        await part_store.ensure_download_dir("sess-a")
        part_store.artifact_path("sess-a", "orders_2026-08-07.csv").write_text("id\n1\n")

        assert await part_store.available_artifact_name(
            "sess-a", "orders_2026-08-07.csv",
        ) == "orders_2026-08-07-1.csv"


# ---- Cleanup ----

class TestCleanup:
    async def test_deleting_a_missing_part_is_not_an_error(
        self, upload_root: Path,
    ) -> None:
        """A batch that failed before it created anything is the normal case."""
        await part_store.delete_part(part_store.part_path("e", 1, ".csv"))

    async def test_the_parts_directory_goes_and_the_artifact_stays(
        self, upload_root: Path,
    ) -> None:
        """The success path: the parts have been folded into the artifact."""
        await part_store.ensure_parts_dir("e1")
        for number in (1, 2, 3):
            part_store.part_path("e1", number, ".csv").write_text("id\n1\n")

        await part_store.ensure_download_dir("sess-e1")
        artifact = part_store.artifact_path("sess-e1", "out.csv")
        artifact.write_text("id\n1\n")

        assert await part_store.delete_parts_dir("e1") == 3
        assert not part_store.parts_dir("e1").exists()
        assert artifact.is_file()

    async def test_the_whole_directory_goes_on_failure(
        self, upload_root: Path,
    ) -> None:
        """
        The failure path takes the partial artifact too.

        An export that half exists is worse than none, because nothing about the file says
        which half is missing.
        """
        await part_store.ensure_parts_dir("e2")
        part_store.part_path("e2", 1, ".csv").write_text("id\n1\n")

        await part_store.delete_export_dir("e2")

        assert not part_store.export_dir("e2").exists()

    async def test_expiry_takes_the_artifact_and_prunes_the_session_folder(
        self, upload_root: Path,
    ) -> None:
        """
        What the reaper does, and the folder it must not leave behind.

        A session that asked for fifty exports would otherwise leave fifty empty
        directories per visitor, forever — nothing else ever comes back for them.
        """
        await part_store.ensure_download_dir("sess-gone")
        part_store.artifact_path("sess-gone", "out.csv").write_text("id\n1\n")

        assert await part_store.delete_artifact("sess-gone", "out.csv") is True
        assert not part_store.download_dir("sess-gone").exists()

    async def test_deleting_an_artifact_that_is_already_gone_is_not_an_error(
        self, upload_root: Path,
    ) -> None:
        """The reaper and the download route can both reach a file the other removed."""
        assert await part_store.delete_artifact("sess-none", "out.csv") is False
        assert await part_store.delete_artifact("sess-none", None) is False

    async def test_a_whole_session_can_be_cleared_in_one_call(
        self, upload_root: Path,
    ) -> None:
        """A session ending takes its files with it, whatever their own TTL says."""
        await part_store.ensure_download_dir("sess-many")
        for name in ("a.csv", "b.csv", "c.csv"):
            part_store.artifact_path("sess-many", name).write_text("id\n1\n")

        assert await part_store.delete_session_downloads("sess-many") == 3
        assert not part_store.download_dir("sess-many").exists()

    async def test_cleaning_an_export_that_wrote_nothing_reports_zero(
        self, upload_root: Path,
    ) -> None:
        """
        Zero is a different story from three after a failure: it means the export failed
        before any batch was written.
        """
        assert await part_store.delete_parts_dir("never-started") == 0

    async def test_parts_are_listed_in_order(self, upload_root: Path) -> None:
        await part_store.ensure_parts_dir("e3")
        for number in (3, 1, 10, 2):
            part_store.part_path("e3", number, ".csv").write_text("id\n")

        listed = await part_store.list_part_paths("e3", ".csv")

        assert [path.name for path in listed] == [
            "part-000001.csv",
            "part-000002.csv",
            "part-000003.csv",
            "part-000010.csv",
        ]

    async def test_only_this_format_is_listed(self, upload_root: Path) -> None:
        """A leftover from a different format is not this merge's business."""
        await part_store.ensure_parts_dir("e4")
        part_store.part_path("e4", 1, ".csv").write_text("id\n")
        part_store.part_path("e4", 2, ".parquet").write_bytes(b"x")

        assert len(await part_store.list_part_paths("e4", ".csv")) == 1

    async def test_the_size_of_a_missing_file_is_zero(self, upload_root: Path) -> None:
        assert await part_store.file_size(part_store.part_path("e5", 1, ".csv")) == 0
