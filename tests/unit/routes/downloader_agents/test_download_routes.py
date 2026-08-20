"""
Tests for app/routes/downloader_agents/download_routes.py.

Two controllers with two different authentication models, so the tests are organised
around what each one must refuse. The refusals are the point: an export is one user's data,
and this is the only route in the application that serves a file off disk by reading a path
out of a database row.

Every refusal is the same 404 with the same sentence — an export that never existed, one
belonging to somebody else, one still building, one whose expiry has passed, and one whose
file is missing. That is asserted rather than assumed, because distinguishing them would
tell an anonymous caller which uuids are real.

The download itself is asserted on the bytes and the headers, not just the status: a 200
carrying an empty body with no ``Content-Disposition`` is a broken download that passes a
status check.
"""

from __future__ import annotations

import uuid as uuid_pkg
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import pytest

from app.db.db_utils import CRUDQueryBuilder
from app.models.chatbot import ChatbotApiKey
from app.models.data_agents import DataAgent
from app.models.datasource import DataSource
from app.models.downloader_agents import DownloadExport
from app.models.tool_configs import ToolConfig
from app.routes.downloader_agents import (
    DownloadController,
    FileDownloadController,
    PublicDownloadController,
)
from app.services.downloader_agents.base import download_service as svc

datasource_crud = CRUDQueryBuilder(DataSource)
agent_crud = CRUDQueryBuilder(DataAgent)
tool_crud = CRUDQueryBuilder(ToolConfig)
key_crud = CRUDQueryBuilder(ChatbotApiKey)

_NOT_FOUND = "That download could not be found."


@pytest.fixture
def make_export(db, upload_root: Path) -> Callable:  # noqa: ANN001
    """
    A ready export owned by ``owner``, with a real file behind it.

    The file is real because the route streams it: a fixture that only wrote the row would
    exercise the authorisation and none of the delivery.
    """

    async def _make(owner, *, ready: bool = True, rows: int = 3, **overrides):  # noqa: ANN001, ANN202
        datasource = await datasource_crud.create(
            db,
            {
                "user_id": owner.id,
                "datasource_name": f"src-{uuid_pkg.uuid4().hex[:8]}",
                "db_type": "sqlite",
                "database_name": ":memory:",
                "password_encrypted": "",
                "configuration_data": {},
            },
        )
        agent = await agent_crud.create(
            db, {"user_id": owner.id, "name": f"agent-{uuid_pkg.uuid4().hex[:8]}"},
        )
        tool = await tool_crud.create(
            db,
            {
                "data_agent_id": agent.id,
                "datasource_id": datasource.id,
                "tool_name": "all_items",
                "table_name": "items",
                "query_mode": "builder",
                "config": {},
            },
        )

        export = await svc.create_offer(
            db, agent.id, tool.id, total_rows=rows, **overrides,
        )

        if not ready:
            return export

        from app.services.downloader_agents.base import part_store

        # Under the *session's* folder, which is where the merge writes it and where
        # the download route reads it from — see part_store's module docstring.
        await part_store.ensure_download_dir(export.session_token)
        artifact = part_store.artifact_path(export.session_token, "items.csv")
        body = "id,name\n" + "".join(f"{n},n{n}\n" for n in range(1, rows + 1))
        artifact.write_text(body)

        await svc.mark_ready(
            db,
            export,
            file_path=str(artifact),
            file_name="items.csv",
            byte_size=len(body.encode()),
            checksum="deadbeef",
            part_count=1,
            rows_written=rows,
        )

        return export

    return _make


# ---- The operator's route ----

class TestDownloadController:
    async def test_a_ready_export_streams_as_an_attachment(
        self, db, user, make_export: Callable, auth_client_factory: Callable,
    ) -> None:
        export = await make_export(user, rows=3)

        with auth_client_factory(DownloadController) as client:
            response = client.get(f"/downloads/{export.uuid}")

        assert response.status_code == 200
        assert response.headers["content-disposition"] == (
            'attachment; filename="items.csv"'
        )
        assert response.headers["content-type"].startswith("text/csv")
        # The bytes, not just the status: a 200 with an empty body passes a status check
        # and is a broken download.
        assert response.text.splitlines() == ["id,name", "1,n1", "2,n2", "3,n3"]

    async def test_another_users_export_is_404_not_403(
        self, db, user, make_export: Callable, make_user: Callable,
        auth_client_factory: Callable,
    ) -> None:
        """
        A 403 would confirm that the uuid names a real file. Same rule as the rest of the
        application.
        """
        stranger = await make_user("stranger@test.com")
        theirs = await make_export(stranger)

        with auth_client_factory(DownloadController) as client:
            response = client.get(f"/downloads/{theirs.uuid}")

        assert response.status_code == 404
        assert response.json()["detail"] == _NOT_FOUND

    async def test_an_unknown_uuid_is_404(
        self, db, user, auth_client_factory: Callable,
    ) -> None:
        with auth_client_factory(DownloadController) as client:
            response = client.get(f"/downloads/{uuid_pkg.uuid4()}")

        assert response.status_code == 404

    async def test_an_export_that_is_not_ready_is_404(
        self, db, user, make_export: Callable, auth_client_factory: Callable,
    ) -> None:
        """That file is half written. Serving it would hand over a truncated export."""
        pending = await make_export(user, ready=False)

        with auth_client_factory(DownloadController) as client:
            response = client.get(f"/downloads/{pending.uuid}")

        assert response.status_code == 404

    async def test_a_lapsed_export_is_refused_even_while_the_file_is_there(
        self, db, user, make_export: Callable, auth_client_factory: Callable,
    ) -> None:
        """
        Refused by the route as well as by the reaper, so a dead link cannot be served in
        the window before the reaper next runs.
        """
        export = await make_export(user)
        export.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        await db.commit()

        with auth_client_factory(DownloadController) as client:
            response = client.get(f"/downloads/{export.uuid}")

        assert response.status_code == 404
        assert "expired" in response.json()["detail"]

    async def test_an_export_the_reaper_has_already_swept_still_says_expired(
        self, db, user, make_export: Callable, auth_client_factory: Callable,
    ) -> None:
        """
        The other side of the sweep, and it used to give a different answer.

        The status check ran before the clock check, so once the reaper marked the row
        ``expired`` the request fell into the not-found branch and the visitor was told
        the download "could not be found" — which reads like the application lost their
        file, and is exactly what keeping the row was supposed to avoid. Nearly invisible
        at a 24-hour TTL; at thirty minutes with a three-minute sweep, the normal case.
        """
        export = await make_export(user)
        export.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        await db.commit()
        await svc.expire_lapsed_exports(db)

        with auth_client_factory(DownloadController) as client:
            response = client.get(f"/downloads/{export.uuid}")

        assert response.status_code == 404
        assert "expired" in response.json()["detail"]
        assert "could not be found" not in response.json()["detail"]

    async def test_a_ready_export_whose_file_vanished_is_404(
        self, db, user, make_export: Callable, auth_client_factory: Callable,
    ) -> None:
        """Someone cleared the volume. A 500 would blame the caller for it."""
        export = await make_export(user)
        Path(export.file_path).unlink()

        with auth_client_factory(DownloadController) as client:
            response = client.get(f"/downloads/{export.uuid}")

        assert response.status_code == 404

    async def test_a_stored_path_outside_the_export_is_refused(
        self, db, user, make_export: Callable, auth_client_factory: Callable,
    ) -> None:
        """
        Not defence against an attacker — the row is ours — but against a path built
        wrongly, which would serve one user another user's file.
        """
        export = await make_export(user)
        export.file_path = "/etc/passwd"
        await db.commit()

        with auth_client_factory(DownloadController) as client:
            response = client.get(f"/downloads/{export.uuid}")

        assert response.status_code == 404

    async def test_the_status_endpoint_reports_the_link_and_no_bigint_id(
        self, db, user, make_export: Callable, auth_client_factory: Callable,
    ) -> None:
        export = await make_export(user, rows=3)

        with auth_client_factory(DownloadController) as client:
            response = client.get(f"/downloads/{export.uuid}/status")

        assert response.status_code == 200
        payload = response.json()
        assert payload["uuid"] == str(export.uuid)
        assert payload["download_url"] == f"/downloads/{export.uuid}"
        assert payload["rows_written"] == 3
        assert "id" not in payload

    async def test_the_progress_stream_ends_with_a_ready_frame(
        self, db, user, make_export: Callable, auth_client_factory: Callable,
        background_sessions,  # noqa: ANN001
    ) -> None:
        """
        A finished export replays its history and closes, rather than holding the
        connection open waiting for something that already happened.

        ``background_sessions`` because the stream opens its own session — it outlives the
        handler that returned it, so it cannot use the request's.
        """
        export = await make_export(user)

        with auth_client_factory(DownloadController) as client:
            response = client.get(f"/downloads/{export.uuid}/events")

        assert response.status_code == 200
        assert "event: ready" in response.text
        assert f"/downloads/{export.uuid}" in response.text

    async def test_the_routes_require_authentication(
        self, db, user, make_export: Callable, client_factory: Callable,
    ) -> None:
        """
        Without a token the app redirects to the login page — the application-wide 401
        behaviour, not a download-specific one.
        """
        export = await make_export(user)

        with client_factory(DownloadController) as client:
            response = client.get(f"/downloads/{export.uuid}", follow_redirects=False)

        assert response.status_code in (301, 302, 307)


# ---- The visitor's route ----

class TestPublicDownloadController:
    @pytest.fixture
    def make_widget_export(  # noqa: ANN201
        self, db, user, make_export: Callable,
    ) -> Callable:
        """An export produced for one widget visitor's conversation."""

        async def _make(session_token: str = "visitor-a"):  # noqa: ANN202
            key = await key_crud.create(
                db,
                {
                    "user_id": user.id,
                    "name": f"widget-{uuid_pkg.uuid4().hex[:6]}",
                    "api_key": uuid_pkg.uuid4().hex,
                    # An agent-backed widget: the shape that produces exports at all.
                    "target_type": "agent",
                    "is_active": True,
                },
            )
            export = await make_export(
                user, chatbot_key_id=key.id, session_token=session_token,
            )
            return key, export

        return _make

    async def test_a_visitor_with_their_key_and_token_gets_the_file(
        self, db, make_widget_export: Callable, client_factory: Callable,
    ) -> None:
        key, export = await make_widget_export()

        with client_factory(PublicDownloadController) as client:
            response = client.get(
                f"/public/downloads/{export.uuid}",
                params={"key": str(key.uuid), "session_token": "visitor-a"},
            )

        assert response.status_code == 200
        assert response.text.startswith("id,name")

    async def test_the_token_alone_is_not_enough(
        self, db, make_widget_export: Callable, client_factory: Callable,
    ) -> None:
        _key, export = await make_widget_export()

        with client_factory(PublicDownloadController) as client:
            response = client.get(
                f"/public/downloads/{export.uuid}",
                params={"session_token": "visitor-a"},
            )

        assert response.status_code == 404

    async def test_the_key_alone_is_not_enough(
        self, db, make_widget_export: Callable, client_factory: Callable,
    ) -> None:
        """
        The decisive one. A widget key identifies a public website, not a person, so a key
        without a token would let any visitor read every export ever produced for it.
        """
        key, export = await make_widget_export()

        with client_factory(PublicDownloadController) as client:
            response = client.get(
                f"/public/downloads/{export.uuid}", params={"key": str(key.uuid)},
            )

        assert response.status_code == 404

    async def test_another_visitors_token_is_refused(
        self, db, make_widget_export: Callable, client_factory: Callable,
    ) -> None:
        key, export = await make_widget_export(session_token="visitor-a")

        with client_factory(PublicDownloadController) as client:
            response = client.get(
                f"/public/downloads/{export.uuid}",
                params={"key": str(key.uuid), "session_token": "visitor-b"},
            )

        assert response.status_code == 404

    async def test_a_key_that_is_not_a_uuid_is_refused(
        self, db, make_widget_export: Callable, client_factory: Callable,
    ) -> None:
        _key, export = await make_widget_export()

        with client_factory(PublicDownloadController) as client:
            response = client.get(
                f"/public/downloads/{export.uuid}",
                params={"key": "not-a-uuid", "session_token": "visitor-a"},
            )

        assert response.status_code == 404

    async def test_an_inactive_key_is_refused(
        self, db, make_widget_export: Callable, client_factory: Callable,
    ) -> None:
        """Switching a widget off has to stop its links working, not just its chat."""
        key, export = await make_widget_export()
        key.is_active = False
        await db.commit()

        with client_factory(PublicDownloadController) as client:
            response = client.get(
                f"/public/downloads/{export.uuid}",
                params={"key": str(key.uuid), "session_token": "visitor-a"},
            )

        assert response.status_code == 404

    async def test_the_status_link_carries_the_visitors_own_scope(
        self, db, make_widget_export: Callable, client_factory: Callable,
    ) -> None:
        key, export = await make_widget_export()

        with client_factory(PublicDownloadController) as client:
            response = client.get(
                f"/public/downloads/{export.uuid}/status",
                params={"key": str(key.uuid), "session_token": "visitor-a"},
            )

        assert response.status_code == 200
        url = response.json()["download_url"]
        # A path, never an absolute URL. The widget script is hosted on the operator's
        # own site and prefixes API_BASE onto whatever it is given, so an absolute URL
        # here becomes "https://host/https://host/..." in a browser the server has no
        # way to update. See download_service._visitor_url.
        assert url == f"/file_downloaders/visitor-a/items.csv?key={key.uuid}"


# ---- The file's own route ----

class TestFileDownloadController:
    """
    ``/file_downloaders/<session>/<file>`` — the URL a visitor's browser actually goes
    to, and the reason this feature was rebuilt around the session rather than the
    export uuid.

    It looks like a static path and is nothing of the kind, so what is asserted here is
    the refusals: an inactive widget, another session's file, an export whose window has
    closed, and a link with no key at all. Serving this directory statically would hand
    every visitor every other visitor's data.
    """

    @pytest.fixture
    def make_widget_export(  # noqa: ANN201
        self, db, user, make_export: Callable,
    ) -> Callable:
        async def _make(session_token: str = "visitor-a"):  # noqa: ANN202
            key = await key_crud.create(
                db,
                {
                    "user_id": user.id,
                    "name": f"widget-{uuid_pkg.uuid4().hex[:6]}",
                    "api_key": uuid_pkg.uuid4().hex,
                    "target_type": "agent",
                    "is_active": True,
                },
            )
            export = await make_export(
                user, chatbot_key_id=key.id, session_token=session_token,
            )
            return key, export

        return _make

    async def test_the_visitor_who_asked_for_it_gets_the_file(
        self, db, make_widget_export: Callable, client_factory: Callable,
    ) -> None:
        key, _export = await make_widget_export()

        with client_factory(FileDownloadController) as client:
            response = client.get(
                "/file_downloaders/visitor-a/items.csv",
                params={"key": str(key.uuid)},
            )

        assert response.status_code == 200
        assert response.text.startswith("id,name")
        assert 'filename="items.csv"' in response.headers["content-disposition"]

    async def test_a_link_with_no_key_is_refused(
        self, db, make_widget_export: Callable, client_factory: Callable,
    ) -> None:
        await make_widget_export()

        with client_factory(FileDownloadController) as client:
            response = client.get("/file_downloaders/visitor-a/items.csv")

        assert response.status_code == 404
        assert _NOT_FOUND in response.text

    async def test_another_sessions_file_is_refused(
        self, db, make_widget_export: Callable, client_factory: Callable,
    ) -> None:
        """
        The decisive one. The session is in the path, so it is the thing an attacker
        would edit — and the file it names must belong to the session that names it.
        """
        key, _export = await make_widget_export(session_token="visitor-a")

        with client_factory(FileDownloadController) as client:
            response = client.get(
                "/file_downloaders/visitor-b/items.csv",
                params={"key": str(key.uuid)},
            )

        assert response.status_code == 404

    async def test_an_inactive_widget_stops_serving_its_files(
        self, db, make_widget_export: Callable, client_factory: Callable,
    ) -> None:
        """Switching a widget off has to stop its links working, not just its chat."""
        key, _export = await make_widget_export()
        key.is_active = False
        await db.commit()

        with client_factory(FileDownloadController) as client:
            response = client.get(
                "/file_downloaders/visitor-a/items.csv",
                params={"key": str(key.uuid)},
            )

        assert response.status_code == 404

    async def test_a_lapsed_export_says_so_rather_than_not_found(
        self, db, make_widget_export: Callable, client_factory: Callable,
    ) -> None:
        """
        "Could not be found" reads like the application lost the file and sends the
        visitor looking for a link that worked an hour ago. This one says what happened
        and what to do about it.
        """
        key, export = await make_widget_export()
        export.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        await db.commit()

        with client_factory(FileDownloadController) as client:
            response = client.get(
                "/file_downloaders/visitor-a/items.csv",
                params={"key": str(key.uuid)},
            )

        assert response.status_code == 404
        assert "expired" in response.text

    async def test_a_file_the_reaper_has_taken_is_refused(
        self, db, make_widget_export: Callable, client_factory: Callable,
    ) -> None:
        key, export = await make_widget_export()

        from app.services.downloader_agents.base import part_store

        await part_store.delete_artifact(export.session_token, export.file_name)

        with client_factory(FileDownloadController) as client:
            response = client.get(
                "/file_downloaders/visitor-a/items.csv",
                params={"key": str(key.uuid)},
            )

        assert response.status_code == 404

    async def test_a_file_name_that_no_export_claims_is_refused(
        self, db, make_widget_export: Callable, client_factory: Callable,
    ) -> None:
        """The row lookup is the authorisation; a name off the disk is not enough."""
        key, _export = await make_widget_export()

        with client_factory(FileDownloadController) as client:
            response = client.get(
                "/file_downloaders/visitor-a/something_else.csv",
                params={"key": str(key.uuid)},
            )

        assert response.status_code == 404
