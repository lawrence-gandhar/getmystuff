"""
The Create File and Download File nodes inside a Graph Designer run.

Two things are asserted that the flow side cannot be: the rows in a graph are already
whole — a SQL node's output is every matching row — and a graph's file has **no visitor**,
so it is owner-only in the database as well as on the route.

The runners open their own session, which is why ``generated_file_sessions`` is autouse
here: they run on the graph's background task, which has no request session, and without
the patch they would read and write the development database while the assertions looked at
the in-memory one. It does not fail cleanly — the same trap
``tests/unit/services/email_dispatch/conftest.py`` documents for the email worker.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from litestar.exceptions import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.file_delivery import ORIGIN_GRAPH
from app.services.file_delivery import file_service
from app.services.file_delivery.errors import SourceError
from app.services.file_delivery.nodes import graph_designer_runner

ROWS = [{"sku": "W-1", "qty": 4}, {"sku": "W-2", "qty": None}]


@pytest.fixture(autouse=True)
def generated_file_sessions(db_engine, monkeypatch: pytest.MonkeyPatch):  # noqa: ANN001, ANN201
    """Point ``file_service.open_session`` at the per-test database. See the module
    docstring — this is the fixture whose absence does not announce itself."""
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    monkeypatch.setattr(file_service, "open_session", factory)

    return factory


def create_node(**data) -> dict:
    base = {
        "label": "Write the CSV",
        "file_format": "csv",
        "file_name": "pipeline-orders",
        "data": {"source": "node", "source_node": "rows", "path": ""},
    }
    base.update(data)
    return {"id": "make", "type": "create_file", "data": base}


class TestCreateFile:
    async def test_it_writes_an_earlier_nodes_rows(
        self, db, user, upload_root: Path,
    ) -> None:
        written = await graph_designer_runner.run_create_file_node(
            create_node(), {"outputs": {"rows": ROWS}}, user_id=user.id, run_ref="42",
        )

        assert written["file_name"] == "pipeline-orders.csv"
        assert written["row_count"] == 2
        assert Path(written["file_path"]).read_text().splitlines()[0] == "sku,qty"

    async def test_the_file_has_no_visitor_scope(
        self, db, user, upload_root: Path,
    ) -> None:
        """
        No key and no token, so nothing about it can be authorised by presenting a widget
        key — which is what makes ``visitor_file``'s origin filter meaningful.
        """
        written = await graph_designer_runner.run_create_file_node(
            create_node(), {"outputs": {"rows": ROWS}}, user_id=user.id, run_ref="42",
        )

        record = await file_service.owner_file(db, user.id, written["file_uuid"])

        assert record.origin == ORIGIN_GRAPH
        assert record.chatbot_key_id is None
        assert record.session_token is None
        assert record.source_ref == "graph run 42"

    async def test_another_users_file_is_a_404(
        self, db, user, make_user, upload_root: Path,
    ) -> None:
        written = await graph_designer_runner.run_create_file_node(
            create_node(), {"outputs": {"rows": ROWS}}, user_id=user.id,
        )
        stranger = await make_user("stranger@test.com")

        with pytest.raises(HTTPException) as raised:
            await file_service.owner_file(db, stranger.id, written["file_uuid"])

        assert raised.value.status_code == 404

    async def test_a_node_that_produced_nothing_is_refused_by_label(
        self, db, user, upload_root: Path,
    ) -> None:
        with pytest.raises(SourceError) as raised:
            await graph_designer_runner.run_create_file_node(
                create_node(),
                {"outputs": {}},
                user_id=user.id,
                node_label_of=lambda node_id: "Read orders",
            )

        assert "Read orders" in raised.value.message

    async def test_a_missing_format_is_refused(self, db, user) -> None:
        with pytest.raises(SourceError) as raised:
            await graph_designer_runner.run_create_file_node(
                create_node(file_format=""), {"outputs": {"rows": ROWS}}, user_id=user.id,
            )

        assert "Write the CSV" in raised.value.message


class TestDownloadFile:
    async def test_it_produces_the_owner_url(self, db, user, upload_root: Path) -> None:
        written = await graph_designer_runner.run_create_file_node(
            create_node(), {"outputs": {"rows": ROWS}}, user_id=user.id,
        )

        offered = await graph_designer_runner.run_download_file_node(
            {"id": "offer", "type": "download_file", "data": {"label": "Hand it over"}},
            {"outputs": {"make": written}},
            user_id=user.id,
            file_uuid=written["file_uuid"],
        )

        assert offered["url"] == f"/generated_files/{written['file_uuid']}"
        assert offered["file_name"] == "pipeline-orders.csv"
        assert offered["expires_at"], "a link with no window is a link that never lapses"
        assert "button" not in offered, (
            "a graph has no chat, so a button payload here would be a field nothing draws"
        )

    async def test_no_file_yet_is_refused(self, db, user) -> None:
        """The named node has not run — a branch went another way, or the wiring is wrong."""
        with pytest.raises(SourceError) as raised:
            await graph_designer_runner.run_download_file_node(
                {"id": "offer", "type": "download_file", "data": {"label": "Hand it over"}},
                {"outputs": {}},
                user_id=user.id,
                file_uuid="",
            )

        assert "Hand it over" in raised.value.message

    async def test_somebody_elses_file_uuid_is_refused(
        self, db, user, make_user, upload_root: Path,
    ) -> None:
        """
        A node edited to name another person's file fails here rather than exposing it —
        ``owner_file`` re-checks ownership even though the node is on this person's graph.
        """
        written = await graph_designer_runner.run_create_file_node(
            create_node(), {"outputs": {"rows": ROWS}}, user_id=user.id,
        )
        stranger = await make_user("stranger@test.com")

        with pytest.raises(HTTPException):
            await graph_designer_runner.run_download_file_node(
                {"id": "offer", "type": "download_file", "data": {}},
                {"outputs": {}},
                user_id=stranger.id,
                file_uuid=written["file_uuid"],
            )


class TestWrapFailure:
    def test_a_file_failure_keeps_its_own_sentence(self) -> None:
        assert graph_designer_runner.wrap_failure(
            SourceError("No rows there."),
        ) == "No rows there."

    def test_anything_else_says_nothing_was_produced(self) -> None:
        message = graph_designer_runner.wrap_failure(RuntimeError("disk on fire"))

        assert "disk on fire" not in message
        assert "Nothing was produced" in message
