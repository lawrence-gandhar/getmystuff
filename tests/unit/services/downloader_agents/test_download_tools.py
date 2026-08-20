"""
Tests for base/download_tools.py — the two tools and the offer they come from.

These tools are the one place in the application where a language model supplies a request
payload, so the tests are written from that premise: a model will pass a uuid it invented,
a format that does not exist, another conversation's export id, and the word "latest". None
of those may reach anything but a validated lookup, and none of them may raise — a tool
that raises aborts the whole chat turn and gives the visitor a 500 for something
recoverable.

The scoping tests are the important ones. ``session_token`` is what stops one widget
visitor confirming or downloading another's export, and it is asserted by trying exactly
that.
"""

from __future__ import annotations

from typing import Callable

import pytest

pytest.importorskip(
    "langgraph", reason="langgraph is installed in the container only (see Dockerfile)",
)

from app.models.downloader_agents import (  # noqa: E402
    EXPORT_BUILDING,
    EXPORT_FAILED,
    EXPORT_QUEUED,
    EXPORT_READY,
)
from app.services.deep_agents.prompt_builder import (  # noqa: E402
    CONFIRM_DOWNLOAD_TOOL,
    DOWNLOAD_STATUS_TOOL,
)
from app.services.deep_agents.query_executor import DISPLAY_ROW_LIMIT  # noqa: E402
from app.services.downloader_agents.base import download_service as svc  # noqa: E402
from app.services.downloader_agents.base import download_tools  # noqa: E402
from app.services.downloader_agents.base.download_notice import (  # noqa: E402
    current_download,
    download_scope,
)
from app.services.downloader_agents.base.download_tools import (  # noqa: E402
    DownloadContext,
    build_download_tools,
    describe_tool_result,
)


@pytest.fixture(autouse=True)
def _tool_environment(  # noqa: ANN201
    monkeypatch: pytest.MonkeyPatch,
    upload_root,  # noqa: ANN001
    graph_sessions,  # noqa: ANN001
    graph_checkpointer,  # noqa: ANN001
):
    """The same isolation the graph tests need — see those fixtures' docstrings."""
    from app.services.downloader_agents.base import download_graph

    monkeypatch.setattr(download_graph, "_graph", None)
    yield
    monkeypatch.setattr(download_graph, "_graph", None)


def _sample_past_the_budget() -> list:
    """
    A tool result bigger than the display budget, which is what makes the offer path run.

    Written against the constant rather than as a literal count. These tests were once
    written with 29 rows, and raising DISPLAY_ROW_LIMIT to 100 turned them into
    small-result tests that asserted nothing they claimed to — the offer path they were
    named for never ran.
    """
    return [{"id": index} for index in range(1, DISPLAY_ROW_LIMIT + 6)]


def _download_tool(context: DownloadContext, name: str):  # noqa: ANN202
    """One of the two tools by name, so a rename fails loudly rather than silently."""
    for tool in build_download_tools(context):
        if tool.name == name:
            return tool

    raise AssertionError(f"{name} is not among the download tools")


async def _entry(db, agent, tool) -> dict:  # noqa: ANN001
    """The tool-factory entry shape ``describe_tool_result`` is called with."""
    from app.models.datasource import DataSource

    datasource = await db.get(DataSource, tool.datasource_id)

    return {
        "id": tool.id,
        "data_agent_id": agent.id,
        "tool_name": tool.tool_name,
        "table_name": tool.table_name,
        "table_names": [tool.table_name],
        "config": dict(tool.config or {}),
        "sql_query": tool.sql_query,
        "datasource": datasource,
    }


# ---- The offer, from a tool result ----

class TestDescribeToolResult:
    async def test_a_small_result_gets_no_offer_and_no_export_row(
        self, db, make_export_fixtures: Callable,
    ) -> None:
        """
        The cheap path is the common one. A result inside the display budget must not pay
        for a COUNT(*), and must not leave an export row nobody asked for.
        """
        agent, tool = await make_export_fixtures(rows=5)
        rows = [{"id": index} for index in range(1, 6)]

        described = await describe_tool_result(
            await _entry(db, agent, tool), rows, DownloadContext(data_agent_id=agent.id),
        )

        assert "Do you want me to create" not in described
        assert await svc.latest_export(db, agent.id) is None

    async def test_the_offer_boundary_is_the_display_budget(
        self, db, make_export_fixtures: Callable,
    ) -> None:
        """
        The display cap and the download offer are one decision, not two.

        A result the answer can print in full needs no file, and a result it cannot must
        come with one — so exactly DISPLAY_ROW_LIMIT rows is the last size that arrives
        whole. Asserted at the boundary because that is where the two could drift apart:
        raise the cap and forget this, and the visitor is offered a download of records
        they were about to be shown anyway.
        """
        agent, tool = await make_export_fixtures(rows=DISPLAY_ROW_LIMIT)
        entry = await _entry(db, agent, tool)
        context = DownloadContext(data_agent_id=agent.id)

        at_the_budget = [{"id": index} for index in range(1, DISPLAY_ROW_LIMIT + 1)]
        described = await describe_tool_result(entry, at_the_budget, context)

        assert "Do you want me to create" not in described
        assert await svc.latest_export(db, agent.id) is None

        agent, tool = await make_export_fixtures(rows=DISPLAY_ROW_LIMIT + 1)
        described = await describe_tool_result(
            await _entry(db, agent, tool),
            at_the_budget + [{"id": DISPLAY_ROW_LIMIT + 1}],
            DownloadContext(data_agent_id=agent.id),
        )

        assert "Do you want me to create" in described

    async def test_a_large_result_states_the_total_and_offers_the_file(
        self, db, make_export_fixtures: Callable,
    ) -> None:
        agent, tool = await make_export_fixtures(rows=125)
        # What the tool actually returned: a sample, capped well below the real total.
        rows = _sample_past_the_budget()

        described = await describe_tool_result(
            await _entry(db, agent, tool), rows, DownloadContext(data_agent_id=agent.id),
        )

        assert "out of 125 matching record(s)" in described
        assert f"Print at most {DISPLAY_ROW_LIMIT} of these rows" in described
        assert (
            "There are 125 records. Do you want me to create a downloadable CSV file "
            "containing the list of all the records."
        ) in described

        offered = await svc.latest_open_offer(db, agent.id)
        assert offered is not None
        assert offered.total_rows == 125

    async def test_no_context_means_no_offer(
        self, db, make_export_fixtures: Callable,
    ) -> None:
        """
        A caller with no conversation to scope an export to gets the old behaviour, not an
        offer nobody could act on.
        """
        agent, tool = await make_export_fixtures(rows=125)
        rows = _sample_past_the_budget()

        described = await describe_tool_result(
            await _entry(db, agent, tool), rows, None,
        )

        assert "Do you want me to create" not in described

    async def test_a_result_past_the_ceiling_is_refused_instead_of_offered(
        self, db, make_export_fixtures: Callable, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No offer and no export row: there would be nothing to confirm."""
        from app.services.downloader_agents.base import record_reader

        monkeypatch.setattr(record_reader, "MAX_EXPORT_ROWS", 10)
        agent, tool = await make_export_fixtures(rows=125)
        rows = _sample_past_the_budget()

        described = await describe_tool_result(
            await _entry(db, agent, tool), rows, DownloadContext(data_agent_id=agent.id),
        )

        assert "more than the 10" in described
        assert "Do you want me to create" not in described
        assert await svc.latest_export(db, agent.id) is None

    async def test_a_failure_preparing_the_offer_still_returns_the_answer(
        self, db, make_export_fixtures: Callable, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        An offer is an extra; the answer is not.

        A user who asked a question must get their answer even if the export machinery is
        broken — the alternative is losing the reply to a feature they did not ask for.
        """
        agent, tool = await make_export_fixtures(rows=125)
        rows = _sample_past_the_budget()

        async def explodes(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            raise RuntimeError("the counter fell over")

        monkeypatch.setattr(download_tools, "count_records", explodes)

        described = await describe_tool_result(
            await _entry(db, agent, tool), rows, DownloadContext(data_agent_id=agent.id),
        )

        assert f"{len(rows)} row(s)" in described
        assert "Do you want me to create" not in described


# ---- confirm_download ----

class TestConfirmDownload:
    async def test_the_tool_names_match_the_prompt_rule(self) -> None:
        """
        The grounding rule tells the model to call these by name. A rule naming a tool the
        agent does not have is worse than no rule.
        """
        names = {tool.name for tool in build_download_tools(DownloadContext(1))}

        assert names == {CONFIRM_DOWNLOAD_TOOL, DOWNLOAD_STATUS_TOOL}

    async def test_a_bare_yes_confirms_the_latest_offer(
        self, db, make_export_fixtures: Callable,
    ) -> None:
        """
        The case that matters most: the user typed "yes" and the model passed nothing.
        """
        agent, tool = await make_export_fixtures(rows=125)
        export = await svc.create_offer(db, agent.id, tool.id, total_rows=125)

        context = DownloadContext(data_agent_id=agent.id)
        reply = await _download_tool(context, CONFIRM_DOWNLOAD_TOOL).coroutine()

        assert "being created" in reply

        await db.refresh(export)
        assert export.status == EXPORT_QUEUED
        assert await queued(db) == 1

    async def test_a_requested_format_is_recorded(
        self, db, make_export_fixtures: Callable,
    ) -> None:
        agent, tool = await make_export_fixtures(rows=125)
        export = await svc.create_offer(db, agent.id, tool.id, total_rows=125)

        context = DownloadContext(data_agent_id=agent.id)
        await _download_tool(context, CONFIRM_DOWNLOAD_TOOL).coroutine(file_format="xlsx")

        await db.refresh(export)
        # "xlsx" is an alias the schema maps; the stored value is the canonical one.
        assert export.file_format == "xls"

    async def test_an_unknown_format_is_refused_without_queueing(
        self, db, make_export_fixtures: Callable,
    ) -> None:
        agent, tool = await make_export_fixtures(rows=125)
        export = await svc.create_offer(db, agent.id, tool.id, total_rows=125)

        from litestar.exceptions import HTTPException

        context = DownloadContext(data_agent_id=agent.id)

        with pytest.raises(HTTPException):
            await _download_tool(context, CONFIRM_DOWNLOAD_TOOL).coroutine(file_format="pdf")

        await db.refresh(export)
        assert export.status != EXPORT_QUEUED

    async def test_confirming_with_nothing_on_offer_says_so(
        self, db, make_export_fixtures: Callable,
    ) -> None:
        """A model that calls this out of the blue is told what to do instead."""
        agent, _tool = await make_export_fixtures(rows=10)

        context = DownloadContext(data_agent_id=agent.id)
        reply = await _download_tool(context, CONFIRM_DOWNLOAD_TOOL).coroutine()

        assert "no download waiting" in reply

    async def test_confirming_twice_does_not_queue_twice(
        self, db, make_export_fixtures: Callable,
    ) -> None:
        """
        A user repeats themselves, or a model calls the tool twice in one turn. Either way
        one confirmation must not produce two identical files.
        """
        agent, tool = await make_export_fixtures(rows=125)
        await svc.create_offer(db, agent.id, tool.id, total_rows=125)

        context = DownloadContext(data_agent_id=agent.id)
        await _download_tool(context, CONFIRM_DOWNLOAD_TOOL).coroutine()
        second = await _download_tool(context, CONFIRM_DOWNLOAD_TOOL).coroutine()

        assert await queued(db) == 1
        assert "still being prepared" in second

    async def test_an_invented_export_id_falls_back_to_the_latest_offer(
        self, db, make_export_fixtures: Callable,
    ) -> None:
        """
        A model that lost the id has still been told yes. Treating a bad id as a bare
        confirmation is what makes a mistyped uuid behave like "yes".
        """
        agent, tool = await make_export_fixtures(rows=125)
        export = await svc.create_offer(db, agent.id, tool.id, total_rows=125)

        context = DownloadContext(data_agent_id=agent.id)
        await _download_tool(context, CONFIRM_DOWNLOAD_TOOL).coroutine(
            export_id="the-export-id-you-gave-me",
        )

        await db.refresh(export)
        assert export.status == EXPORT_QUEUED

    async def test_another_conversations_export_cannot_be_confirmed(
        self, db, make_export_fixtures: Callable,
    ) -> None:
        """
        The scoping rule, tried directly. A widget key identifies a public website, so
        without the token any visitor could act on any other's offer.
        """
        agent, tool = await make_export_fixtures(rows=125)
        theirs = await svc.create_offer(
            db, agent.id, tool.id, total_rows=125,
            chatbot_key_id=None, session_token="visitor-a",
        )

        mine = DownloadContext(data_agent_id=agent.id, session_token="visitor-b")
        reply = await _download_tool(mine, CONFIRM_DOWNLOAD_TOOL).coroutine(
            export_id=str(theirs.uuid),
        )

        assert "no download waiting" in reply

        await db.refresh(theirs)
        assert theirs.status != EXPORT_QUEUED


# ---- download_status ----

class TestDownloadStatus:
    async def test_a_ready_export_hands_over_the_link(
        self, db, make_export_fixtures: Callable,
    ) -> None:
        agent, tool = await make_export_fixtures(rows=125)
        export = await svc.create_offer(db, agent.id, tool.id, total_rows=125)
        await svc.mark_ready(
            db, export, file_path="uploads/exports/x/items.csv",
            file_name="items.csv", byte_size=10, checksum="abc", part_count=3,
            rows_written=125,
        )

        context = DownloadContext(data_agent_id=agent.id)

        with download_scope():
            reply = await _download_tool(context, DOWNLOAD_STATUS_TOOL).coroutine()
            notice = current_download()

        # The model is told the file exists and nothing about where. It renders as plain
        # text, so a URL in here reaches the user as a URL in here.
        assert "125 record(s)" in reply
        assert "/downloads/" not in reply
        assert "http" not in reply

        # The link the interface draws its button from, scoped to an operator.
        assert notice["download_url"] == f"/downloads/{export.uuid}"
        assert notice["status"] == EXPORT_READY

    async def test_a_visitor_gets_the_public_link_with_their_own_scope(
        self, db, make_export_fixtures: Callable,
    ) -> None:
        """
        The same file, a different route, because a widget visitor has no session — and
        the link has to carry what authorises it.
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

        context = DownloadContext(
            data_agent_id=agent.id,
            session_token="visitor-a",
            chatbot_key_id=7,
            chatbot_key_uuid="11111111-1111-1111-1111-111111111111",
        )
        with download_scope():
            await _download_tool(context, DOWNLOAD_STATUS_TOOL).coroutine()
            notice = current_download()

        # Still the scoping guarantee this test has always made — the session and the
        # key are what authorise the fetch, and a widget visitor has no cookie to carry
        # them. Asserted on the notice now rather than on the model's sentence, because
        # that is where the link lives. The session is in the path rather than the query
        # string because that is the folder the artifact is stored in.
        assert notice["download_url"] == (
            "/file_downloaders/visitor-a/items.csv"
            "?key=11111111-1111-1111-1111-111111111111"
        )

        # And the two the card needs while it is still being made, on the same scope.
        assert "session_token=visitor-a" in notice["progress_url"]
        assert "session_token=visitor-a" in notice["status_url"]

    async def test_a_building_export_reports_progress_and_no_link(
        self, db, make_export_fixtures: Callable,
    ) -> None:
        """A link for an export that is not ready would be a link that 404s."""
        agent, tool = await make_export_fixtures(rows=125)
        export = await svc.create_offer(db, agent.id, tool.id, total_rows=125)
        await svc.mark_queued(db, export, "csv")
        await svc.mark_building(db, export)
        export.rows_written = 50
        await db.commit()

        context = DownloadContext(data_agent_id=agent.id)
        reply = await _download_tool(context, DOWNLOAD_STATUS_TOOL).coroutine()

        assert "still being prepared" in reply
        assert "50 of 125" in reply
        assert "/downloads/" not in reply

    async def test_a_failed_export_relays_the_stored_sentence_verbatim(
        self, db, make_export_fixtures: Callable,
    ) -> None:
        """
        One sentence, stored once, said the same way to everyone — the driver's own words
        are not a visitor's business.
        """
        agent, tool = await make_export_fixtures(rows=125)
        export = await svc.create_offer(db, agent.id, tool.id, total_rows=125)
        await svc.mark_failed(db, export, reason="asyncpg said something alarming")

        context = DownloadContext(data_agent_id=agent.id)
        reply = await _download_tool(context, DOWNLOAD_STATUS_TOOL).coroutine()

        assert svc.FAILURE_MESSAGE in reply
        assert "asyncpg" not in reply

    async def test_asking_with_nothing_in_flight_says_so(
        self, db, make_export_fixtures: Callable,
    ) -> None:
        agent, _tool = await make_export_fixtures(rows=10)

        context = DownloadContext(data_agent_id=agent.id)
        reply = await _download_tool(context, DOWNLOAD_STATUS_TOOL).coroutine()

        assert "no file being prepared" in reply


async def queued(db) -> int:  # noqa: ANN001
    """How many jobs are waiting, for the confirmation tests."""
    from app.db.downloader_agents.queries import queued_job_count

    return await queued_job_count(db)
