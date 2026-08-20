"""
Tests for base/progress.py — the build-progress frames.

``frames_for`` is pure: a row, its parts, a link, and the same frames come out every time.
So it is tested directly rather than through the stream, and the stream is tested for the
one thing it adds — sending each frame once and stopping when the export finishes.

What the frames are *for* shapes what is asserted. Somebody watching a slow export wants to
know whether it is big or struggling, so a discarded part must surface as a ``retry`` frame
rather than being hidden; and ``rows_written`` must count only what was actually written,
or a retried export would report more records than the file contains.
"""

from __future__ import annotations

import uuid as uuid_pkg
from types import SimpleNamespace
from typing import Callable

import pytest

from app.models.downloader_agents import (
    EXPORT_BUILDING,
    EXPORT_FAILED,
    EXPORT_READY,
    PART_DISCARDED,
    PART_MERGED,
    PART_WRITTEN,
)
from app.services.downloader_agents.base import download_service as svc
from app.schemas.downloader_agents import DownloadProgressEvent
from app.services.downloader_agents.base import progress


def _export(**overrides):  # noqa: ANN202
    base = {
        "uuid": uuid_pkg.uuid4(),
        "status": EXPORT_BUILDING,
        "total_rows": 125,
        "rows_written": 0,
        "error_message": None,
        # Both None until the parts are merged, which is what the ready frame carries
        # them for — a client watching since the build was queued has never seen either.
        "file_name": None,
        "byte_size": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _part(number: int, status: str = PART_WRITTEN, rows: int = 50, attempts: int = 1):  # noqa: ANN202
    return SimpleNamespace(
        part_number=number, status=status, row_count=rows, attempts=attempts,
    )


class TestExpectedParts:
    @pytest.mark.parametrize(
        ("total", "parts"),
        [(None, None), (0, None), (1, 1), (49, 1), (50, 1), (51, 2), (125, 3), (4821, 97)],
    )
    def test_the_part_count_is_derived_from_the_batch_size(
        self, total, parts,  # noqa: ANN001
    ) -> None:
        """
        Derived, not stored: a stored copy is a second number that can disagree with the
        batch size the reader actually used.
        """
        assert progress.expected_parts(total) == parts


class TestFramesFor:
    def test_a_building_export_emits_one_frame_per_written_part(self) -> None:
        export = _export()
        parts = [_part(1), _part(2)]

        frames = progress.frames_for(export, parts, None)

        assert [frame.event for frame in frames] == ["progress", "progress"]
        assert [frame.part for frame in frames] == [1, 2]
        assert [frame.of for frame in frames] == [3, 3]
        # Cumulative, so a client that missed a frame is not left with a wrong total.
        assert [frame.rows_written for frame in frames] == [50, 100]

    def test_a_discarded_part_becomes_a_retry_frame(self) -> None:
        """
        Shown, not hidden. It is the difference between "this export is big" and "this
        export is struggling", which is the only question somebody watching one has.
        """
        export = _export()
        parts = [_part(1), _part(2, status=PART_DISCARDED, rows=0, attempts=1)]

        frames = progress.frames_for(export, parts, None)

        assert [frame.event for frame in frames] == ["progress", "retry"]
        assert frames[1].attempt == 1
        assert "failed on attempt 1" in frames[1].message

    def test_a_retry_does_not_inflate_the_records_written(self) -> None:
        """
        A discarded attempt wrote nothing that survived. Counting it would report more
        records than the file contains.
        """
        export = _export()
        parts = [
            _part(1),
            _part(2, status=PART_DISCARDED, rows=0, attempts=1),
            _part(2, status=PART_DISCARDED, rows=0, attempts=2),
            _part(2, attempts=3),
        ]

        frames = progress.frames_for(export, parts, None)

        assert [frame.rows_written for frame in frames] == [50, 50, 50, 100]

    def test_a_ready_export_closes_with_the_link(self) -> None:
        export = _export(status=EXPORT_READY, rows_written=125)
        url = f"/downloads/{export.uuid}"

        frames = progress.frames_for(export, [_part(1), _part(2, rows=25)], url)

        assert frames[-1].event == "ready"
        assert frames[-1].download_url == url
        assert frames[-1].rows_written == 125

    def test_a_failed_export_closes_with_the_stored_sentence(self) -> None:
        export = _export(status=EXPORT_FAILED, error_message=svc.FAILURE_MESSAGE)

        frames = progress.frames_for(export, [], None)

        assert [frame.event for frame in frames] == ["failed"]
        assert frames[0].message == svc.FAILURE_MESSAGE
        assert frames[0].download_url is None

    def test_a_failed_export_with_no_stored_message_still_says_something(self) -> None:
        """A blank bubble is indistinguishable from a hang."""
        export = _export(status=EXPORT_FAILED, error_message=None)

        assert progress.frames_for(export, [], None)[0].message == svc.FAILURE_MESSAGE

    def test_a_building_export_has_no_closing_frame(self) -> None:
        """
        The stream ends on `ready` or `failed`, so a consumer knows to stop rather than
        inferring it from silence. Emitting one early would end the stream mid-build.
        """
        frames = progress.frames_for(_export(), [_part(1)], None)

        assert frames[-1].event == "progress"

    def test_merged_parts_still_count(self) -> None:
        """
        The cleanup node flips written parts to `merged`, and a browser that connects
        afterwards must still see the whole story rather than an empty one.
        """
        export = _export(status=EXPORT_READY, rows_written=100)
        parts = [_part(1, status=PART_MERGED), _part(2, status=PART_MERGED)]

        frames = progress.frames_for(export, parts, "/downloads/x")

        assert [frame.event for frame in frames] == ["progress", "progress", "ready"]


class TestStreamProgress:
    async def test_it_sends_each_frame_once_and_stops_when_ready(
        self, db, background_sessions, make_export_fixtures: Callable, upload_root,  # noqa: ANN001
    ) -> None:
        """
        A finished export replays its history and closes. Re-sending frames on every poll
        would make a reconnecting browser render the same batch twice.
        """
        agent, tool = await make_export_fixtures(rows=100)
        export = await svc.create_offer(db, agent.id, tool.id, total_rows=100)
        await svc.record_part(db, export.id, 1, 1, 50, "p1", 10)
        await svc.record_part(db, export.id, 2, 1, 50, "p2", 10)
        await svc.mark_ready(
            db, export, file_path="p", file_name="items.csv", byte_size=10,
            checksum="x", part_count=2, rows_written=100,
        )

        frames = [
            frame
            async for frame in progress.stream_progress(
                str(export.uuid), lambda row: "/downloads/x", poll_interval=0.0,
            )
        ]

        assert [frame["event"] for frame in frames] == ["progress", "progress", "ready"]

    async def test_a_deleted_export_ends_the_stream_silently(
        self, db, background_sessions,  # noqa: ANN001
    ) -> None:
        """
        Nothing further can be said about it, and an invented `failed` frame would be a
        claim we cannot support.
        """
        frames = [
            frame
            async for frame in progress.stream_progress(
                str(uuid_pkg.uuid4()), lambda row: None, poll_interval=0.0,
            )
        ]

        assert frames == []

    async def test_it_gives_up_rather_than_streaming_forever(
        self, db, background_sessions, make_export_fixtures: Callable,  # noqa: ANN001
    ) -> None:
        """
        A tab left open on an export that never finishes must not hold a connection for
        good.
        """
        agent, tool = await make_export_fixtures(rows=100)
        export = await svc.create_offer(db, agent.id, tool.id, total_rows=100)
        await svc.mark_queued(db, export, "csv")
        await svc.mark_building(db, export)

        frames = [
            frame
            async for frame in progress.stream_progress(
                str(export.uuid),
                lambda row: None,
                poll_interval=0.0,
                max_seconds=0.0,
            )
        ]

        # One pass happens, then the budget is spent — and crucially it returns rather
        # than looping.
        assert all(frame["event"] == "progress" for frame in frames)


class TestTheReadyFrameCarriesTheArtifact:
    """
    The ``ready`` frame is the only thing a long-running client is told about the
    finished file, and it has to be enough to render it.

    A card that has been watching since the export was queued has never seen a file
    name or a byte size — neither exists until the parts are merged. Without them
    here it draws a finished download as an unnamed file of unknown size, and its
    ``download`` attribute is empty, so the browser saves it under the URL's last
    path segment (a uuid) instead of the name we chose.
    """

    def test_it_carries_the_name_and_the_size(self) -> None:
        export = _export(
            status=EXPORT_READY,
            rows_written=125,
            file_name="project_details_2026-08-07.csv",
            byte_size=45650,
        )

        frames = progress.frames_for(export, [_part(1)], f"/downloads/{export.uuid}")
        ready = frames[-1].payload()

        assert ready["event"] == DownloadProgressEvent.READY
        assert ready["file_name"] == "project_details_2026-08-07.csv"
        assert ready["byte_size"] == 45650
        assert ready["download_url"] == f"/downloads/{export.uuid}"

    def test_a_frame_before_the_end_carries_neither(self) -> None:
        """They do not exist yet, and inventing them would name a file that does not."""
        export = _export(rows_written=50)

        frames = progress.frames_for(export, [_part(1)])

        assert all(f.payload().get("file_name") is None for f in frames)
        assert all(f.payload().get("byte_size") is None for f in frames)
