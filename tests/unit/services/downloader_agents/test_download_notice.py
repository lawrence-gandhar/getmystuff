"""
Tests for base/download_notice.py — the export one turn is about.

This is what turns a sentence into a button. The download tools tell the model in words
that a file exists; the notice is how the *interface* finds out, so it can draw a real
download control and a live progress bar instead of leaving the model to paste a URL into
prose that renders as plain text.

Two properties carry it and both are asserted by trying to break them:

* **A notice never outlives its turn.** It rides a ContextVar, and the same worker task
  answers many visitors. A leak here shows a visitor a download somebody else asked for,
  which is worse than showing none.
* **The URLs are scoped to whoever asked.** An operator fetches an export from one prefix
  and a widget visitor from another, carrying the key and session token that authorise
  it. The notice is now the only place those links are built for a reply, so the scoping
  guarantee lives or dies here.
"""

from __future__ import annotations

import asyncio
from typing import Callable

import pytest

pytest.importorskip(
    "langgraph", reason="langgraph is installed in the container only (see Dockerfile)",
)

from app.models.downloader_agents import EXPORT_READY  # noqa: E402
from app.services.downloader_agents.base import download_service as svc  # noqa: E402
from app.services.downloader_agents.base.download_notice import (  # noqa: E402
    current_download,
    download_scope,
    note_export,
)
from app.services.downloader_agents.base.download_tools import (  # noqa: E402
    DownloadContext,
)

VISITOR_KEY = "11111111-1111-1111-1111-111111111111"


def _visitor_context(agent_id: int) -> DownloadContext:
    return DownloadContext(
        data_agent_id=agent_id,
        session_token="visitor-a",
        chatbot_key_id=7,
        chatbot_key_uuid=VISITOR_KEY,
    )


class TestTheScope:
    def test_a_turn_that_touched_no_download_reports_none(self) -> None:
        with download_scope():
            assert current_download() is None

    async def test_a_notice_does_not_survive_its_scope(
        self, db, make_export_fixtures: Callable,
    ) -> None:
        """
        The property that stops one visitor being shown another's file.

        The same task answers turn after turn, so a notice left set is a download offered
        to whoever asks next.
        """
        agent, tool = await make_export_fixtures(rows=125)
        export = await svc.create_offer(db, agent.id, tool.id, total_rows=125)

        with download_scope():
            note_export(DownloadContext(data_agent_id=agent.id), export)
            assert current_download() is not None

        with download_scope():
            assert current_download() is None

    async def test_the_last_export_of_a_turn_wins(
        self, db, make_export_fixtures: Callable,
    ) -> None:
        """A model that checks a status and then confirms a build has said two things;
        the second is the current one."""
        agent, tool = await make_export_fixtures(rows=125)
        first = await svc.create_offer(db, agent.id, tool.id, total_rows=125)
        second = await svc.create_offer(db, agent.id, tool.id, total_rows=125)

        context = DownloadContext(data_agent_id=agent.id)

        with download_scope():
            note_export(context, first)
            note_export(context, second)

            assert current_download()["uuid"] == str(second.uuid)

    async def test_an_export_noted_inside_a_child_task_reaches_the_turn(
        self, db, make_export_fixtures: Callable,
    ) -> None:
        """
        The bug this module was rewritten for, and the reason it holds a mutable box.

        A tool does not run in the task that started the turn — LangGraph runs its nodes
        as their own tasks, and a new task gets a *copy* of the context. Rebinding a
        ContextVar in that copy is invisible to the parent, so the first version of this
        recorded every export into a context nobody read: the file was built, and the
        widget was never told, so no card ever appeared.

        Asserted with a real ``create_task`` because that is precisely the boundary that
        broke, and nothing weaker crosses it.
        """
        agent, tool = await make_export_fixtures(rows=125)
        export = await svc.create_offer(db, agent.id, tool.id, total_rows=125)
        context = DownloadContext(data_agent_id=agent.id)

        async def in_a_child_task() -> None:
            note_export(context, export)

        with download_scope():
            await asyncio.create_task(in_a_child_task())

            assert current_download() is not None
            assert current_download()["uuid"] == str(export.uuid)

    async def test_noting_outside_a_scope_is_harmless(
        self, db, make_export_fixtures: Callable,
    ) -> None:
        """The tools are reachable from paths that render no reply. A download nobody is
        going to draw is not worth failing a turn over."""
        agent, tool = await make_export_fixtures(rows=125)
        export = await svc.create_offer(db, agent.id, tool.id, total_rows=125)

        note_export(DownloadContext(data_agent_id=agent.id), export)

        assert current_download() is None

    async def test_nothing_is_recorded_for_a_missing_export(self) -> None:
        """``note_export(None)`` is a no-op rather than a crash: a caller that resolved
        nothing has nothing to say, and must not take the turn down for it."""
        with download_scope():
            note_export(DownloadContext(data_agent_id=1), None)

            assert current_download() is None


class TestTheLinks:
    async def test_an_operator_gets_the_console_urls(
        self, db, make_export_fixtures: Callable,
    ) -> None:
        agent, tool = await make_export_fixtures(rows=125)
        export = await svc.create_offer(db, agent.id, tool.id, total_rows=125)
        await svc.mark_ready(
            db, export, file_path="uploads/exports/x/items.csv",
            file_name="items.csv", byte_size=10, checksum="abc", part_count=3,
            rows_written=125,
        )

        with download_scope():
            note_export(DownloadContext(data_agent_id=agent.id), export)
            notice = current_download()

        assert notice["download_url"] == f"/downloads/{export.uuid}"
        assert notice["progress_url"] == f"/downloads/{export.uuid}/events"
        assert notice["status_url"] == f"/downloads/{export.uuid}/status"

    async def test_a_visitor_gets_public_urls_carrying_their_own_scope(
        self, db, make_export_fixtures: Callable,
    ) -> None:
        """
        All three URLs, not just the download. A widget has no session, so every one of
        them has to carry the scope that authorises it — a progress stream that dropped
        the token would be one visitor watching another's file being built.

        The download link names the session and the file, because that is how the
        artifact is stored; the other two still name the export uuid, because they are
        asked while it is being built and there is no file name yet.
        """
        agent, tool = await make_export_fixtures(rows=125)
        export = await svc.create_offer(
            db, agent.id, tool.id, total_rows=125, session_token="visitor-a",
        )
        await svc.mark_ready(
            db, export, file_path="uploads/exports/x/items.csv",
            file_name="items.csv", byte_size=10, checksum="abc", part_count=3,
            rows_written=125,
        )

        with download_scope():
            note_export(_visitor_context(agent.id), export)
            notice = current_download()

        assert notice["download_url"] == (
            f"/file_downloaders/visitor-a/items.csv?key={VISITOR_KEY}"
        )

        for key in ("progress_url", "status_url"):
            assert notice[key].startswith(f"/public/downloads/{export.uuid}")
            assert f"key={VISITOR_KEY}" in notice[key]
            assert "session_token=visitor-a" in notice[key]

        # All three are paths. The widget script lives on the operator's server and may
        # be far older than this one; every version of it prefixes API_BASE, so an
        # absolute URL here is a string the browser never sends — silently.
        for key in ("download_url", "progress_url", "status_url"):
            assert notice[key].startswith("/"), notice[key]

    async def test_a_build_in_progress_has_no_download_url_but_can_be_watched(
        self, db, make_export_fixtures: Callable,
    ) -> None:
        """
        The state the card spends most of its life in.

        No ``download_url``, because the route would 404 — a button offered before the
        artifact exists is a broken button. ``progress_url`` is present precisely
        *because* it is not ready: that is when watching it is worth doing.
        """
        agent, tool = await make_export_fixtures(rows=125)
        export = await svc.create_offer(db, agent.id, tool.id, total_rows=125)
        await svc.mark_queued(db, export, "csv")
        await svc.mark_building(db, export)

        with download_scope():
            note_export(DownloadContext(data_agent_id=agent.id), export)
            notice = current_download()

        assert notice["download_url"] is None
        assert notice["progress_url"]
        assert notice["status_url"]
        assert notice["status"] != EXPORT_READY

    async def test_the_notice_carries_what_the_card_draws(
        self, db, make_export_fixtures: Callable,
    ) -> None:
        """The card shows a file name, a total and a size; none of them may be missing
        from the payload it is built from."""
        agent, tool = await make_export_fixtures(rows=125)
        export = await svc.create_offer(db, agent.id, tool.id, total_rows=125)
        await svc.mark_ready(
            db, export, file_path="uploads/exports/x/items.csv",
            file_name="items.csv", byte_size=4096, checksum="abc", part_count=3,
            rows_written=125,
        )

        with download_scope():
            note_export(DownloadContext(data_agent_id=agent.id), export)
            notice = current_download()

        assert notice["file_name"] == "items.csv"
        assert notice["file_format"] == "csv"
        assert notice["total_rows"] == 125
        assert notice["rows_written"] == 125
        assert notice["byte_size"] == 4096
        assert notice["status"] == EXPORT_READY
