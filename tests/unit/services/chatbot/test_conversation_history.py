"""
Tests for ``chatbot_turn_service.recent_history``.

This function is why the download confirmation works at all. A visitor replying "yes" is
answering something the assistant said on the previous turn, and without that turn the
reply is unanswerable — so the turn log is read back as a conversation.

Two properties carry it and both are asserted by trying to break them:

* **A conversation is scoped by its session token.** Filtering on ``chatbot_key_id`` alone
  would hand one visitor another visitor's messages, which is worse than having no history
  at all. So no token means no history, and one visitor's token never returns another's
  turns.
* **Oldest first.** The query takes the newest N, the model reads in order. Reversed
  history is a model answering the wrong question with real data.
"""

from __future__ import annotations

import uuid as uuid_pkg
from typing import Callable

import pytest
from sqlalchemy import select

from app.db.db_utils import CRUDQueryBuilder
from app.models.chatbot import TURN_TYPE_AI, ChatbotApiKey, ChatbotMessage
from app.services.chatbot.chatbot_turn_service import (
    _HISTORY_TURNS,
    TurnResult,
    _log_turn,
    recent_history,
)
from app.utils.turn_recorder import record_turn

key_crud = CRUDQueryBuilder(ChatbotApiKey)
message_crud = CRUDQueryBuilder(ChatbotMessage)


@pytest.fixture
def chatbot_key(db, user) -> Callable:  # noqa: ANN001
    """One widget key to log turns against."""

    async def _make():  # noqa: ANN202
        return await key_crud.create(
            db,
            {
                "user_id": user.id,
                "name": f"widget-{uuid_pkg.uuid4().hex[:6]}",
                "api_key": uuid_pkg.uuid4().hex,
                "target_type": "agent",
                "is_active": True,
            },
        )

    return _make


@pytest.fixture
def log_turn(db) -> Callable:  # noqa: ANN001
    """One stored turn, as ``_log_turn`` writes it."""

    async def _make(
        key,  # noqa: ANN001
        visitor_message: str,
        answer: str | None = None,
        session_token: str | None = "visitor-a",
        status: str = "success",
    ):  # noqa: ANN202
        return await message_crud.create(
            db,
            {
                "chatbot_key_id": key.id,
                "session_token": session_token,
                "visitor_message": visitor_message,
                "status": status,
                "turn_type": TURN_TYPE_AI,
                "ai_response": {"summary": answer} if answer else None,
            },
        )

    return _make


class TestRecentHistory:
    async def test_no_token_means_no_history(
        self, db, chatbot_key: Callable, log_turn: Callable,
    ) -> None:
        """
        Deliberately empty rather than "everything for this widget". A key identifies a
        public website, not a person, so an unscoped history would be one visitor reading
        another's conversation.
        """
        key = await chatbot_key()
        await log_turn(key, "how many items?", "There are 125.")

        assert await recent_history(db, key.id, "") == []

    async def test_it_returns_the_conversation_oldest_first(
        self, db, chatbot_key: Callable, log_turn: Callable,
    ) -> None:
        key = await chatbot_key()
        await log_turn(key, "how many items?", "There are 125 records.")
        await log_turn(key, "and last month?", "There were 40.")

        history = await recent_history(db, key.id, "visitor-a")

        assert history == [
            {"role": "user", "content": "how many items?"},
            {"role": "assistant", "content": "There are 125 records."},
            {"role": "user", "content": "and last month?"},
            {"role": "assistant", "content": "There were 40."},
        ]

    async def test_another_visitors_turns_are_not_included(
        self, db, chatbot_key: Callable, log_turn: Callable,
    ) -> None:
        key = await chatbot_key()
        await log_turn(key, "mine", "yours", session_token="visitor-a")
        await log_turn(key, "theirs", "not yours", session_token="visitor-b")

        history = await recent_history(db, key.id, "visitor-a")

        assert [entry["content"] for entry in history] == ["mine", "yours"]

    async def test_turns_from_before_the_column_existed_are_not_attributed(
        self, db, chatbot_key: Callable, log_turn: Callable,
    ) -> None:
        """
        ``session_token`` is NULL for every row logged before it was added. Those turns
        cannot be attributed to a conversation after the fact, and guessing would be worse
        than admitting it.
        """
        key = await chatbot_key()
        await log_turn(key, "old turn", "old answer", session_token=None)

        assert await recent_history(db, key.id, "visitor-a") == []

    async def test_a_failed_turn_is_left_out(
        self, db, chatbot_key: Callable, log_turn: Callable,
    ) -> None:
        """
        Its question was never answered. Replaying it invites the model to answer it a turn
        late, in reply to something else.
        """
        key = await chatbot_key()
        await log_turn(key, "this one broke", None, status="error")
        await log_turn(key, "this one worked", "an answer")

        history = await recent_history(db, key.id, "visitor-a")

        assert [entry["content"] for entry in history] == [
            "this one worked", "an answer",
        ]

    async def test_it_is_bounded_to_the_most_recent_turns(
        self, db, chatbot_key: Callable, log_turn: Callable,
    ) -> None:
        """
        Long enough for a follow-up to resolve, short enough that the tool descriptions
        stay dominant in the context.
        """
        key = await chatbot_key()

        for index in range(_HISTORY_TURNS + 4):
            await log_turn(key, f"question {index}", f"answer {index}")

        history = await recent_history(db, key.id, "visitor-a")

        assert len(history) == _HISTORY_TURNS * 2
        # The newest turns, and the oldest of those first.
        assert history[0]["content"] == "question 4"
        assert history[-1]["content"] == f"answer {_HISTORY_TURNS + 3}"

    async def test_a_turn_with_no_answer_contributes_only_the_question(
        self, db, chatbot_key: Callable, log_turn: Callable,
    ) -> None:
        """A flow turn stores options rather than a summary; the question still happened."""
        key = await chatbot_key()
        await log_turn(key, "just asking", None)

        assert await recent_history(db, key.id, "visitor-a") == [
            {"role": "user", "content": "just asking"},
        ]


class TestLogTurnWritesWhatRecentHistoryReads:
    """
    The writing half of the same contract, exercised through ``_log_turn`` itself.

    Everything above builds its rows by hand, which tests the reader thoroughly and the
    writer not at all. That seam is where a real bug lived: ``_log_turn`` referred to a
    ``session_token`` it was never passed, so every widget turn raised ``NameError`` after
    the visitor had already been answered — a 500 on ``/public/chatbot/message`` — and no
    turn was ever logged. The reader tests all passed throughout, because they never
    called the writer.

    So these go the whole way round: log a turn the way the service does, then read it
    back the way the next turn does.
    """

    async def test_a_logged_turn_comes_back_as_history(
        self, db, chatbot_key: Callable,
    ) -> None:
        key = await chatbot_key()

        with record_turn() as record:
            await _log_turn(
                db,
                key,
                "how many projects are there",
                None,
                TurnResult(status="success", summary="There are 2921 records."),
                TURN_TYPE_AI,
                record,
                session_token="visitor-a",
            )

        assert await recent_history(db, key.id, "visitor-a") == [
            {"role": "user", "content": "how many projects are there"},
            {"role": "assistant", "content": "There are 2921 records."},
        ]

    async def test_the_turn_is_scoped_to_the_visitor_who_asked(
        self, db, chatbot_key: Callable,
    ) -> None:
        """The token must be *stored*, not merely accepted — otherwise every conversation
        is either invisible or shared."""
        key = await chatbot_key()

        with record_turn() as record:
            await _log_turn(
                db, key, "mine", None, TurnResult(summary="yours"),
                TURN_TYPE_AI, record, session_token="visitor-a",
            )

        assert await recent_history(db, key.id, "visitor-a")
        assert await recent_history(db, key.id, "visitor-b") == []

    async def test_a_console_turn_with_no_visitor_stores_no_token(
        self, db, chatbot_key: Callable,
    ) -> None:
        """An empty token is stored as NULL rather than "", so it cannot collide with a
        real visitor whose token failed to reach us."""
        key = await chatbot_key()

        with record_turn() as record:
            await _log_turn(
                db, key, "no session", None, TurnResult(summary="ok"),
                TURN_TYPE_AI, record, session_token="",
            )

        rows = list((await db.execute(
            select(ChatbotMessage).where(ChatbotMessage.chatbot_key_id == key.id)
        )).scalars().all())

        assert len(rows) == 1
        assert rows[0].session_token is None
