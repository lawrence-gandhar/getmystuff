"""
Tests for app/routes/file_delivery/routes.py.

Two controllers with two different authentication models, so the tests are organised around
what each must refuse. The refusals are the point: this is the second route in the
application that serves a file off disk by reading a path out of a database row, and the
public one is reachable by anybody who has been in a conversation with the widget.

What is pinned hardest:

* **a graph's file is unreachable on the public route**, whatever key is presented. A
  pipeline has no visitor, so any success there would put every pipeline file one guessed
  uuid away from anonymous.
* **the session token is part of the authorisation**, not decoration — another
  conversation's file is a 404.
* **a lapsed file is a 410 with its own sentence**, and it is refused while its bytes are
  still on disk: the sweep deletes bytes, the route enforces the window.

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
from app.models.chatbot import TARGET_TYPE_DATASOURCE, ChatbotApiKey
from app.models.file_delivery import ORIGIN_FLOW, ORIGIN_GRAPH
from app.routes.file_delivery import (
    GeneratedFileController,
    PublicGeneratedFileController,
)
from app.services.file_delivery import file_service as svc
from app.services.file_delivery.row_source import Payload

key_crud = CRUDQueryBuilder(ChatbotApiKey)

_NOT_FOUND = "That file could not be found."
_SESSION = "visitor-1"


@pytest.fixture
async def widget(db, user) -> ChatbotApiKey:
    return await key_crud.create(
        db,
        {
            "user_id": user.id,
            "api_key": "test-widget-key",
            "name": "Support bot",
            "target_type": TARGET_TYPE_DATASOURCE,
            "is_active": True,
        },
    )


@pytest.fixture
def make_file(db, upload_root: Path) -> Callable:  # noqa: ANN001
    """
    A ready file with real bytes behind it.

    The bytes are real because the route streams them: a fixture that only wrote the row
    would exercise the authorisation and none of the delivery.
    """

    async def _make(owner, *, origin: str = ORIGIN_GRAPH, widget=None, session_token="", **overrides):  # noqa: ANN001, ANN202
        record = await svc.create_file(
            db,
            user_id=owner.id,
            payload=Payload(rows=[{"id": 1, "name": "one"}, {"id": 2, "name": "two"}]),
            file_format=overrides.pop("file_format", "csv"),
            name_stem=overrides.pop("name_stem", "orders"),
            origin=origin,
            chatbot_key_id=widget.id if widget is not None else None,
            session_token=session_token or None,
        )

        for key, value in overrides.items():
            setattr(record, key, value)

        if overrides:
            await db.commit()
            await db.refresh(record)

        return record

    return _make


# ---- The owner's route ----

class TestGeneratedFileController:
    async def test_a_file_streams_as_an_attachment(
        self, db, user, make_file: Callable, auth_client_factory: Callable,
    ) -> None:
        record = await make_file(user)

        with auth_client_factory(GeneratedFileController) as client:
            response = client.get(f"/generated_files/{record.uuid}")

        assert response.status_code == 200
        assert response.headers["content-disposition"] == (
            'attachment; filename="orders.csv"'
        )
        assert response.headers["content-type"].startswith("text/csv")
        # The bytes, not just the status.
        assert response.text.splitlines() == ["id,name", "1,one", "2,two"]

    async def test_a_flow_file_is_the_owners_too(
        self, db, user, widget, make_file: Callable, auth_client_factory: Callable,
    ) -> None:
        """
        They own the widget, the flow and the conversation log. What the origin gates is the
        *public* route, where the audience is a visitor rather than an owner.
        """
        record = await make_file(
            user, origin=ORIGIN_FLOW, widget=widget, session_token=_SESSION,
        )

        with auth_client_factory(GeneratedFileController) as client:
            response = client.get(f"/generated_files/{record.uuid}")

        assert response.status_code == 200

    async def test_another_users_file_is_404_not_403(
        self, db, user, make_file: Callable, make_user: Callable,
        auth_client_factory: Callable,
    ) -> None:
        """A 403 would confirm the uuid names a real file."""
        stranger = await make_user("stranger@test.com")
        theirs = await make_file(stranger)

        with auth_client_factory(GeneratedFileController) as client:
            response = client.get(f"/generated_files/{theirs.uuid}")

        assert response.status_code == 404
        assert response.json()["detail"] == _NOT_FOUND

    async def test_an_unknown_uuid_is_404(
        self, db, user, auth_client_factory: Callable,
    ) -> None:
        with auth_client_factory(GeneratedFileController) as client:
            response = client.get(f"/generated_files/{uuid_pkg.uuid4()}")

        assert response.status_code == 404

    async def test_a_lapsed_file_is_410_while_its_bytes_are_still_there(
        self, db, user, make_file: Callable, auth_client_factory: Callable,
    ) -> None:
        record = await make_file(
            user, expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        )
        assert Path(record.file_path).is_file(), (
            "the point of this test is that the reaper has not been round yet"
        )

        with auth_client_factory(GeneratedFileController) as client:
            response = client.get(f"/generated_files/{record.uuid}")

        assert response.status_code == 410
        assert response.json()["detail"] == svc.EXPIRED_MESSAGE

    async def test_a_row_whose_file_is_gone_is_404(
        self, db, user, make_file: Callable, auth_client_factory: Callable,
    ) -> None:
        record = await make_file(user)
        Path(record.file_path).unlink()

        with auth_client_factory(GeneratedFileController) as client:
            response = client.get(f"/generated_files/{record.uuid}")

        assert response.status_code == 404

    async def test_the_status_endpoint_reports_the_window(
        self, db, user, make_file: Callable, auth_client_factory: Callable,
    ) -> None:
        record = await make_file(user)

        with auth_client_factory(GeneratedFileController) as client:
            response = client.get(f"/generated_files/{record.uuid}/status")

        payload = response.json()
        assert response.status_code == 200
        assert payload["file_name"] == "orders.csv"
        assert payload["row_count"] == 2
        assert payload["download_url"] == f"/generated_files/{record.uuid}"
        assert payload["expires_at"]
        assert "id" not in payload, "the bigint id never leaves the server"

    async def test_it_needs_a_session(
        self, db, user, make_file: Callable, client_factory: Callable,
    ) -> None:
        record = await make_file(user)

        with client_factory(GeneratedFileController) as client:
            response = client.get(
                f"/generated_files/{record.uuid}", follow_redirects=False,
            )

        assert response.status_code in (302, 401)


# ---- The visitor's route ----

class TestPublicGeneratedFileController:
    def _url(self, record, widget, token: str = _SESSION) -> str:
        return (
            f"/public/generated_files/{record.uuid}"
            f"?key={widget.uuid}&session_token={token}"
        )

    async def test_the_visitor_who_was_given_it_gets_it(
        self, db, user, widget, make_file: Callable, client_factory: Callable,
    ) -> None:
        record = await make_file(
            user, origin=ORIGIN_FLOW, widget=widget, session_token=_SESSION,
        )

        with client_factory(PublicGeneratedFileController) as client:
            response = client.get(self._url(record, widget))

        assert response.status_code == 200
        assert response.text.splitlines()[0] == "id,name"
        assert response.headers["content-disposition"] == (
            'attachment; filename="orders.csv"'
        )

    async def test_another_conversations_file_is_404(
        self, db, user, widget, make_file: Callable, client_factory: Callable,
    ) -> None:
        """The token is the authorisation, not decoration."""
        record = await make_file(
            user, origin=ORIGIN_FLOW, widget=widget, session_token=_SESSION,
        )

        with client_factory(PublicGeneratedFileController) as client:
            response = client.get(self._url(record, widget, token="somebody-else"))

        assert response.status_code == 404
        assert response.json()["detail"] == _NOT_FOUND

    async def test_a_pipelines_file_is_unreachable_here(
        self, db, user, widget, make_file: Callable, client_factory: Callable,
    ) -> None:
        """
        A graph has no visitor. Any success here would put every pipeline file one guessed
        uuid away from anonymous, with a valid widget key being the only thing needed.
        """
        record = await make_file(user, origin=ORIGIN_GRAPH)

        with client_factory(PublicGeneratedFileController) as client:
            response = client.get(self._url(record, widget))

        assert response.status_code == 404

    @pytest.mark.parametrize("query", ["", "?key=", "?session_token=t", "?key=not-a-uuid"])
    async def test_a_missing_or_malformed_scope_is_404(
        self, db, user, widget, make_file: Callable, client_factory: Callable, query: str,
    ) -> None:
        record = await make_file(
            user, origin=ORIGIN_FLOW, widget=widget, session_token=_SESSION,
        )

        with client_factory(PublicGeneratedFileController) as client:
            response = client.get(f"/public/generated_files/{record.uuid}{query}")

        assert response.status_code == 404

    async def test_switching_the_widget_off_stops_its_links_working(
        self, db, user, widget, make_file: Callable, client_factory: Callable,
    ) -> None:
        """Not just its chat. The key is resolved active-only for exactly this reason."""
        record = await make_file(
            user, origin=ORIGIN_FLOW, widget=widget, session_token=_SESSION,
        )
        await key_crud.update(db, widget.id, {"is_active": False})

        with client_factory(PublicGeneratedFileController) as client:
            response = client.get(self._url(record, widget))

        assert response.status_code == 404

    async def test_another_widgets_key_does_not_open_it(
        self, db, user, widget, make_file: Callable, client_factory: Callable,
    ) -> None:
        record = await make_file(
            user, origin=ORIGIN_FLOW, widget=widget, session_token=_SESSION,
        )
        other = await key_crud.create(
            db,
            {
                "user_id": user.id,
                "api_key": "another-widget-key",
                "name": "Other bot",
                "target_type": TARGET_TYPE_DATASOURCE,
                "is_active": True,
            },
        )

        with client_factory(PublicGeneratedFileController) as client:
            response = client.get(self._url(record, other))

        assert response.status_code == 404

    async def test_a_lapsed_link_is_410(
        self, db, user, widget, make_file: Callable, client_factory: Callable,
    ) -> None:
        """
        Its own sentence: "could not be found" reads as though the application lost the
        file and sends somebody looking for a link that worked yesterday.
        """
        record = await make_file(
            user,
            origin=ORIGIN_FLOW,
            widget=widget,
            session_token=_SESSION,
            expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        )

        with client_factory(PublicGeneratedFileController) as client:
            response = client.get(self._url(record, widget))

        assert response.status_code == 410
        assert response.json()["detail"] == svc.EXPIRED_MESSAGE

    async def test_it_is_reachable_cross_origin(
        self, db, user, widget, make_file: Callable, client_factory: Callable,
    ) -> None:
        """A widget runs on somebody else's site, so the browser has to be allowed to fetch."""
        record = await make_file(
            user, origin=ORIGIN_FLOW, widget=widget, session_token=_SESSION,
        )

        with client_factory(PublicGeneratedFileController) as client:
            response = client.get(
                self._url(record, widget), headers={"Origin": "https://example.com"},
            )

        assert response.status_code == 200
        assert response.headers.get("access-control-allow-origin") == "*"

    @pytest.mark.parametrize(
        ("file_format", "media_type"),
        [
            ("csv", "text/csv"),
            ("txt", "text/plain"),
            ("parquet", "application/vnd.apache.parquet"),
            (
                "xlsx",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ),
        ],
    )
    async def test_each_format_is_served_as_itself(
        self, db, user, widget, make_file: Callable, client_factory: Callable,
        file_format: str, media_type: str,
    ) -> None:
        record = await make_file(
            user,
            origin=ORIGIN_FLOW,
            widget=widget,
            session_token=_SESSION,
            file_format=file_format,
        )

        with client_factory(PublicGeneratedFileController) as client:
            response = client.get(self._url(record, widget))

        assert response.status_code == 200
        assert response.headers["content-type"].startswith(media_type)
        assert response.headers["content-length"] == str(record.byte_size)
