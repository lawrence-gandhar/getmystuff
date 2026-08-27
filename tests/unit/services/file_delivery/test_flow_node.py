"""
The Create File and Download File blocks inside a chatbot conversation.

What is asserted hardest is the scoping, because it is what stands between one visitor and
another's data: the row carries the widget key *and* the session token, ``visitor_file``
requires both plus a flow origin, and a file from another conversation is a 404 rather than
a 403.

Second: the button. It is drawn only when the operator asked for one, its colour is
``#rrggbb`` or the default whatever the node says, and its text falls back rather than
arriving blank.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from litestar.exceptions import HTTPException

from app.models.chatbot import TARGET_TYPE_DATASOURCE, ChatbotApiKey
from app.models.file_delivery import ORIGIN_FLOW, ORIGIN_GRAPH
from app.services.file_delivery import file_service
from app.services.file_delivery.errors import FileFailure, SourceError
from app.services.file_delivery.nodes import flow_builder_runner


@pytest.fixture
async def chatbot_key(db, user) -> ChatbotApiKey:
    key = ChatbotApiKey(
        user_id=user.id,
        api_key="test-widget-key",
        name="Support bot",
        # NOT NULL, and this path never reads it: a chatbot answering from a flow needs
        # neither a datasource nor an agent, which is the configuration under test.
        target_type=TARGET_TYPE_DATASOURCE,
    )
    db.add(key)
    await db.commit()
    await db.refresh(key)
    return key


class FakeSession:
    """
    The two things these runners read off a session, and nothing else.

    A real ``ChatbotFlowSession`` would need a flow row and a chatbot row behind it to
    persist, and neither runner touches anything but these three attributes — the engine
    tests are where the real row is exercised.
    """

    def __init__(self, **kwargs) -> None:
        self.session_token = kwargs.get("session_token", "visitor-1")
        self.variables = kwargs.get("variables", {})
        self.node_results = kwargs.get("node_results", {})


def create_node(**data) -> dict:
    base = {
        "file_format": "csv",
        "file_name": "orders",
        "data": {"source": "variable", "name": "ROWS"},
        "variable_name": "FILE_PATH",
    }
    base.update(data)
    return {"id": "make", "type": "create_file", "data": base}


def download_node(**data) -> dict:
    base = {"create_file_node_id": "make", "variable_name": "FILE_URL"}
    base.update(data)
    return {"id": "offer", "type": "download_file", "data": base}


ROWS_JSON = '[{"order": "A-1", "qty": 3}]'


class TestCreateFile:
    async def test_it_writes_the_file_and_records_it(
        self, db, chatbot_key, upload_root: Path,
    ) -> None:
        session = FakeSession(variables={"ROWS": ROWS_JSON})

        written = await flow_builder_runner.run_create_file_node(
            db, create_node(), chatbot_key=chatbot_key, session=session,
        )

        assert written["file_name"] == "orders.csv"
        assert written["row_count"] == 1
        assert Path(written["file_path"]).read_text().splitlines()[0] == "order,qty"

    async def test_the_row_is_scoped_to_this_conversation(
        self, db, chatbot_key, upload_root: Path,
    ) -> None:
        """Both facts, because the public route needs both to authorise."""
        session = FakeSession(session_token="visitor-7", variables={"ROWS": ROWS_JSON})

        written = await flow_builder_runner.run_create_file_node(
            db, create_node(), chatbot_key=chatbot_key, session=session,
        )

        record = await file_service.visitor_file(
            db, chatbot_key.id, "visitor-7", written["file_uuid"],
        )

        assert record.origin == ORIGIN_FLOW
        assert record.chatbot_key_id == chatbot_key.id
        assert record.session_token == "visitor-7"
        assert record.expires_at is not None, "a file with no window never expires"

    async def test_another_conversations_file_is_not_reachable(
        self, db, chatbot_key, upload_root: Path,
    ) -> None:
        session = FakeSession(session_token="visitor-7", variables={"ROWS": ROWS_JSON})

        written = await flow_builder_runner.run_create_file_node(
            db, create_node(), chatbot_key=chatbot_key, session=session,
        )

        with pytest.raises(HTTPException) as raised:
            await file_service.visitor_file(
                db, chatbot_key.id, "somebody-else", written["file_uuid"],
            )

        assert raised.value.status_code == 404, (
            "a 403 would confirm the uuid names a real file"
        )

    async def test_a_missing_format_is_refused(self, db, chatbot_key) -> None:
        with pytest.raises(SourceError):
            await flow_builder_runner.run_create_file_node(
                db,
                create_node(file_format=""),
                chatbot_key=chatbot_key,
                session=FakeSession(variables={"ROWS": ROWS_JSON}),
            )

    async def test_the_extension_comes_from_the_format_not_the_name(
        self, db, chatbot_key, upload_root: Path,
    ) -> None:
        """A block writing Parquet must not produce ``orders.csv`` and mislead the reader."""
        written = await flow_builder_runner.run_create_file_node(
            db,
            create_node(file_format="parquet", file_name="orders.csv"),
            chatbot_key=chatbot_key,
            session=FakeSession(variables={"ROWS": ROWS_JSON}),
        )

        assert written["file_name"].endswith(".parquet")

    async def test_a_dangerous_name_is_normalised(
        self, db, chatbot_key, upload_root: Path,
    ) -> None:
        written = await flow_builder_runner.run_create_file_node(
            db,
            create_node(file_name="../../etc/passwd"),
            chatbot_key=chatbot_key,
            session=FakeSession(variables={"ROWS": ROWS_JSON}),
        )

        assert "/" not in written["file_name"]
        assert Path(written["file_path"]).is_file()


class TestDownloadFile:
    async def _written(self, db, chatbot_key, session):  # noqa: ANN001, ANN202
        written = await flow_builder_runner.run_create_file_node(
            db, create_node(), chatbot_key=chatbot_key, session=session,
        )
        session.node_results = {
            **session.node_results,
            "make": {"kind": "file", "file_uuid": written["file_uuid"]},
        }
        return written

    async def test_it_produces_a_relative_url_carrying_both_facts(
        self, db, chatbot_key, upload_root: Path,
    ) -> None:
        """
        Relative, because the widget runs on somebody else's site and prefixes its own
        API_BASE — an absolute URL built here would point at whatever this process believes
        its hostname to be.
        """
        session = FakeSession(variables={"ROWS": ROWS_JSON})
        written = await self._written(db, chatbot_key, session)

        offered = await flow_builder_runner.run_download_file_node(
            db,
            download_node(),
            chatbot_key=chatbot_key,
            session=session,
            file_uuid=written["file_uuid"],
        )

        assert offered["url"].startswith("/public/generated_files/")
        assert f"key={chatbot_key.uuid}" in offered["url"]
        assert "session_token=visitor-1" in offered["url"]

    async def test_no_button_unless_the_operator_asked_for_one(
        self, db, chatbot_key, upload_root: Path,
    ) -> None:
        session = FakeSession(variables={"ROWS": ROWS_JSON})
        written = await self._written(db, chatbot_key, session)

        offered = await flow_builder_runner.run_download_file_node(
            db, download_node(), chatbot_key=chatbot_key, session=session,
            file_uuid=written["file_uuid"],
        )

        assert offered["button"] is None

    async def test_the_button_carries_the_operators_words_and_colour(
        self, db, chatbot_key, upload_root: Path,
    ) -> None:
        session = FakeSession(variables={"ROWS": ROWS_JSON})
        written = await self._written(db, chatbot_key, session)

        offered = await flow_builder_runner.run_download_file_node(
            db,
            download_node(
                show_button=True, button_text="Get my orders", button_colour="#198754",
            ),
            chatbot_key=chatbot_key,
            session=session,
            file_uuid=written["file_uuid"],
        )

        assert offered["button"]["label"] == "Get my orders"
        assert offered["button"]["colour"] == "#198754"
        assert offered["button"]["url"] == offered["url"]

    @pytest.mark.parametrize(
        "colour",
        [
            "red",
            "#fff",
            "javascript:alert(1)",
            "#0d6efd; background-image:url(x)",
            "",
        ],
    )
    async def test_a_colour_that_is_not_a_hex_colour_never_reaches_the_payload(
        self, db, chatbot_key, upload_root: Path, colour: str,
    ) -> None:
        """
        This value lands in an inline ``style`` on a page this application does not own, so
        the runner is the second of three gates — the canvas validator refuses it at save
        and ``FileButtonView`` refuses it on the way out.
        """
        session = FakeSession(variables={"ROWS": ROWS_JSON})
        written = await self._written(db, chatbot_key, session)

        offered = await flow_builder_runner.run_download_file_node(
            db,
            download_node(show_button=True, button_colour=colour),
            chatbot_key=chatbot_key,
            session=session,
            file_uuid=written["file_uuid"],
        )

        assert offered["button"]["colour"] == flow_builder_runner.DEFAULT_BUTTON_COLOUR

    async def test_an_empty_label_falls_back_rather_than_drawing_a_blank_button(
        self, db, chatbot_key, upload_root: Path,
    ) -> None:
        session = FakeSession(variables={"ROWS": ROWS_JSON})
        written = await self._written(db, chatbot_key, session)

        offered = await flow_builder_runner.run_download_file_node(
            db,
            download_node(show_button=True, button_text="   "),
            chatbot_key=chatbot_key,
            session=session,
            file_uuid=written["file_uuid"],
        )

        assert offered["button"]["label"] == flow_builder_runner.DEFAULT_BUTTON_LABEL

    async def test_a_graphs_file_is_not_reachable_from_a_conversation(
        self, db, user, chatbot_key, upload_root: Path,
    ) -> None:
        """
        A pipeline's file has no visitor. Reachable on a public route with a valid widget
        key would make every graph file one guessed uuid away from anonymous.
        """
        from app.services.file_delivery.row_source import Payload

        record = await file_service.create_file(
            db,
            user_id=user.id,
            payload=Payload(rows=[{"a": 1}]),
            file_format="csv",
            name_stem="pipeline",
            origin=ORIGIN_GRAPH,
        )

        with pytest.raises(HTTPException) as raised:
            await file_service.visitor_file(
                db, chatbot_key.id, "visitor-1", str(record.uuid),
            )

        assert raised.value.status_code == 404


class TestWrapFailure:
    def test_a_file_failure_keeps_its_own_sentence(self) -> None:
        assert flow_builder_runner.wrap_failure(
            SourceError("That block produced no rows."),
        ) == "That block produced no rows."

    def test_anything_else_is_reduced(self) -> None:
        """A developer's exception text must never reach a visitor."""
        message = flow_builder_runner.wrap_failure(KeyError("rows"))

        assert "rows" not in message
        assert message.endswith("Please try again.")

    def test_the_base_class_is_enough_to_catch_both(self) -> None:
        assert isinstance(SourceError("x"), FileFailure)
