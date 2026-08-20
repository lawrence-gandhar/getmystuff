"""
Tests for the export TTL and the reaper that enforces it.

An export is a snapshot of somebody's data sitting on a disk. The honest default is that
it does not sit there forever, and :data:`EXPORT_TTL_SECONDS` — half an hour — is how long
it does. Two separate mechanisms honour it and both are asserted here, because either one
alone is a hole:

* **The route refuses a lapsed export**, whether or not the reaper has run. This is the
  rule. It is what makes the window exact rather than "half an hour, give or take however
  long since the last sweep".
* **The reaper deletes the bytes.** This is the housekeeping. Without it the rule holds
  and the disk still fills.

The reaper's sweep had no test before this file, which mattered: it is the half nobody
notices failing. A route that wrongly serves an expired file is a bug somebody reports;
a reaper that quietly stopped deleting is a disk that fills up months later.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import pytest

pytest.importorskip(
    "langgraph", reason="langgraph is installed in the container only (see Dockerfile)",
)

from app.models.downloader_agents import (  # noqa: E402
    EXPORT_EXPIRED,
    EXPORT_READY,
)
from app.services.downloader_agents.base import download_service as svc  # noqa: E402
from app.services.downloader_agents.base import part_store  # noqa: E402


async def _ready_export(db, agent, tool, *, session_token: str | None = None):  # noqa: ANN001, ANN202
    """A finished export with a real artifact on disk, as the graph leaves one."""
    export = await svc.create_offer(
        db, agent.id, tool.id, total_rows=3, session_token=session_token,
    )

    directory = part_store.export_dir(export.uuid)
    directory.mkdir(parents=True, exist_ok=True)
    artifact = directory / "items.csv"
    artifact.write_text("id\n1\n2\n3\n", encoding="utf-8")

    await svc.mark_ready(
        db, export,
        file_path=str(artifact), file_name="items.csv",
        byte_size=artifact.stat().st_size, checksum="abc",
        part_count=1, rows_written=3,
    )

    return export, artifact


class TestTheTtl:
    def test_it_is_thirty_minutes(self) -> None:
        assert svc.EXPORT_TTL_SECONDS == 30 * 60

    @pytest.mark.parametrize(
        ("seconds", "phrase"),
        [
            (60, "1 minute"),
            (120, "2 minutes"),
            (30 * 60, "30 minutes"),
            (3600, "1 hour"),
            (2 * 3600, "2 hours"),
            (24 * 3600, "1 day"),
            (48 * 3600, "2 days"),
        ],
    )
    def test_the_phrase_is_derived_from_the_setting(
        self, monkeypatch: pytest.MonkeyPatch, seconds: int, phrase: str,
    ) -> None:
        """
        What the agent reads out has to follow the setting.

        This was hard-coded as "available for the next 24 hours" while the TTL was a day,
        which was true and then silently was not. A user believing a file lasts a day when
        it lasts thirty minutes is worse served than one told nothing.
        """
        monkeypatch.setattr(svc, "EXPORT_TTL_SECONDS", seconds)

        assert svc.ttl_phrase() == phrase

    def test_the_reaper_sweeps_more_often_than_the_ttl(self) -> None:
        """A fixed interval that outlived a shortened TTL would leave files well past
        their expiry — an hour's sweep against a half-hour TTL is a 90-minute file."""
        assert svc.REAPER_INTERVAL_SECONDS <= svc.EXPORT_TTL_SECONDS / 2
        assert svc.REAPER_INTERVAL_SECONDS >= 60

    async def test_a_finished_export_expires_one_ttl_from_now(
        self, db, make_export_fixtures: Callable, upload_root,  # noqa: ANN001
    ) -> None:
        agent, tool = await make_export_fixtures(rows=3)
        before = datetime.now(timezone.utc)

        export, _ = await _ready_export(db, agent, tool)

        expected = before + timedelta(seconds=svc.EXPORT_TTL_SECONDS)
        actual = export.expires_at

        if actual.tzinfo is None:            # SQLite stores naive datetimes
            actual = actual.replace(tzinfo=timezone.utc)

        assert abs((actual - expected).total_seconds()) < 60


class TestTheReaper:
    async def test_it_deletes_the_artifact_and_marks_the_row(
        self, db, make_export_fixtures: Callable, upload_root,  # noqa: ANN001
    ) -> None:
        agent, tool = await make_export_fixtures(rows=3)
        export, artifact = await _ready_export(db, agent, tool)

        assert artifact.exists()

        export.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        await db.commit()

        assert await svc.expire_lapsed_exports(db) == 1

        await db.refresh(export)
        assert export.status == EXPORT_EXPIRED
        assert not artifact.exists()
        assert not Path(part_store.export_dir(export.uuid)).exists()

    async def test_it_leaves_a_live_export_alone(
        self, db, make_export_fixtures: Callable, upload_root,  # noqa: ANN001
    ) -> None:
        """The sweep runs every few minutes against every ready export. One that ate a
        file still inside its window would be worse than no sweep at all."""
        agent, tool = await make_export_fixtures(rows=3)
        export, artifact = await _ready_export(db, agent, tool)

        assert await svc.expire_lapsed_exports(db) == 0

        await db.refresh(export)
        assert export.status == EXPORT_READY
        assert artifact.exists()

    async def test_the_row_survives_so_a_dead_link_can_explain_itself(
        self, db, make_export_fixtures: Callable, upload_root,  # noqa: ANN001
    ) -> None:
        """
        Deleting the row would turn "that file has expired, ask me again" into "that
        download could not be found", which reads like the application lost it.
        """
        agent, tool = await make_export_fixtures(rows=3)
        export, _ = await _ready_export(db, agent, tool, session_token="visitor-a")

        export.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        await db.commit()
        await svc.expire_lapsed_exports(db)

        still_there = await svc.get_export(db, str(export.uuid))

        assert still_there is not None
        assert still_there.status == EXPORT_EXPIRED

    async def test_a_sweep_with_nothing_to_do_is_not_an_error(
        self, db, upload_root,  # noqa: ANN001
    ) -> None:
        assert await svc.expire_lapsed_exports(db) == 0


class TestWhatALapsedLinkSays:
    """
    An export goes out of date in two steps minutes apart: its ``expires_at`` passes, and
    then the reaper's next sweep marks the row and deletes the bytes. A visitor must get
    the same answer either side of that sweep.

    They did not. The route tested the status before the clock, so an export the reaper
    had already swept fell into the *not found* branch — "that download could not be
    found", which reads like the application lost the file, and is exactly the reading
    that keeping the row was supposed to prevent. Barely visible at a 24-hour TTL; at
    thirty minutes with a three-minute sweep it is what almost everybody would have seen.
    """

    async def test_a_lapsed_clock_counts_before_the_reaper_has_run(
        self, db, make_export_fixtures: Callable, upload_root,  # noqa: ANN001
    ) -> None:
        agent, tool = await make_export_fixtures(rows=3)
        export, _ = await _ready_export(db, agent, tool)
        export.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)

        assert export.status == EXPORT_READY
        assert svc.has_lapsed(export)

    async def test_an_expired_row_counts_after_the_reaper_has_run(
        self, db, make_export_fixtures: Callable, upload_root,  # noqa: ANN001
    ) -> None:
        agent, tool = await make_export_fixtures(rows=3)
        export, _ = await _ready_export(db, agent, tool)

        export.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        await db.commit()
        await svc.expire_lapsed_exports(db)
        await db.refresh(export)

        assert export.status == EXPORT_EXPIRED
        assert svc.has_lapsed(export)

    async def test_a_live_export_has_not_lapsed(
        self, db, make_export_fixtures: Callable, upload_root,  # noqa: ANN001
    ) -> None:
        agent, tool = await make_export_fixtures(rows=3)
        export, _ = await _ready_export(db, agent, tool)

        assert not svc.has_lapsed(export)

    def test_the_two_refusals_do_not_say_the_same_thing(self) -> None:
        """One says the file is gone because time passed and they may ask again; the
        other says nothing was found. Collapsing them loses the only actionable half."""
        assert svc.EXPIRED_MESSAGE != svc.NOT_FOUND_MESSAGE
        assert "expired" in svc.EXPIRED_MESSAGE.lower()
        assert "again" in svc.EXPIRED_MESSAGE.lower()
