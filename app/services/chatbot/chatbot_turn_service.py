"""
One visitor turn, start to finish: decide who answers it, measure what it
cost, and write the single log row the owner's history and the Chatbot
Analytics dashboard both read.

This is the top of the chatbot answer stack — it is the only module allowed to
import the Flow Builder engine alongside the chatbot reply path, which is why
the "flow first, AI second" decision lives here rather than in a route (routes
hold no business logic) or in chatbot_reply_service (the flow engine imports
*that*, via its AI Fallback node, so the dependency would be circular).

Measurement is deliberately anchored at this level too. A turn can make
several language-model calls in layers far below — an action router picking a
webhook, the grounded answer itself, an AI Fallback node — so timing and token
totals are accumulated in a context-local record (see utils.turn_recorder) and
read back here once, giving exactly one honest row per visitor turn.
"""

import logging
from dataclasses import dataclass, field
from typing import List, Optional

from litestar.exceptions import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.db_utils import CRUDQueryBuilder
from app.models.chatbot import TURN_TYPE_AI, TURN_TYPE_FLOW, ChatbotApiKey, ChatbotMessage
from app.services.chatbot.chatbot_reply_service import generate_reply
from app.services.flow_builder import engine_service, flow_service
from app.utils.turn_recorder import TurnRecord, record_turn

logger = logging.getLogger(__name__)

chatbot_message_crud = CRUDQueryBuilder(ChatbotMessage)

_GENERIC_FAILURE_MESSAGE = (
    "Sorry, something went wrong while answering that. Please try again."
)


@dataclass
class TurnResult:
    """
    One answered turn, in the shape the widget needs.

    ``status`` mirrors what is stored on the log row: "success" means `summary`
    /`options` carry the answer, "error" means `message` carries a
    visitor-safe explanation and nothing else should be rendered.
    """

    status: str = "success"
    # "text" | "buttons" | "dropdown" | "text_prompt"
    type: str = "text"
    summary: str = ""
    insights: List[str] = field(default_factory=list)
    table: Optional[dict] = None
    options: List[dict] = field(default_factory=list)
    message: str = ""
    response_time_ms: int = 0


async def answer_turn(
    db: AsyncSession,
    chatbot_key: ChatbotApiKey,
    message: str,
    session_token: str = "",
    selected_value: Optional[str] = None,
) -> TurnResult:
    """
    Answer one visitor message and log the turn.

    An active flow gets first refusal: it keeps answering until this visitor
    reaches a terminal point, after which the engine reports a handoff and
    plain AI answering takes over for the rest of the conversation (see
    engine_service.AI_HANDOFF).

    Never raises for an answering failure — a visitor-safe message is returned
    (and stored) instead, because the widget shows whatever comes back and must
    not be handed an internal error.
    """
    with record_turn() as record:
        result, turn_type = await _run_turn(db, chatbot_key, message, session_token, selected_value)
        result.response_time_ms = record.elapsed_ms()
        await _log_turn(db, chatbot_key, message, selected_value, result, turn_type, record)

    return result


async def _run_turn(
    db: AsyncSession,
    chatbot_key: ChatbotApiKey,
    message: str,
    session_token: str,
    selected_value: Optional[str],
) -> tuple[TurnResult, str]:
    """Route the turn to the flow engine or to plain AI answering."""
    try:
        if session_token:
            active_flow = await flow_service.get_active_flow(db, chatbot_key.id)
            if active_flow:
                engine_result = await engine_service.advance_flow_session(
                    db, chatbot_key, active_flow, session_token, message, selected_value
                )
                if engine_result.type != engine_service.AI_HANDOFF:
                    return _from_flow(engine_result), TURN_TYPE_FLOW

        reply = await generate_reply(db, chatbot_key, message)
        return (
            TurnResult(
                type="text",
                summary=reply.summary,
                insights=reply.insights or [],
                table=reply.table.model_dump() if reply.table else None,
            ),
            TURN_TYPE_AI,
        )

    except HTTPException as exc:
        # Validation and provider errors already carry visitor-safe text.
        return TurnResult(status="error", message=str(exc.detail)), TURN_TYPE_AI

    except Exception:
        # Anything unplanned is logged in full internally and reduced to a
        # generic apology outside — a stack trace must never reach a visitor.
        logger.exception("Unhandled failure answering a turn for chatbot %s", chatbot_key.uuid)
        return TurnResult(status="error", message=_GENERIC_FAILURE_MESSAGE), TURN_TYPE_AI


def _from_flow(engine_result: engine_service.FlowEngineResult) -> TurnResult:
    return TurnResult(
        type=engine_result.type,
        summary=engine_result.text or "",
        insights=engine_result.insights,
        table=engine_result.table,
        options=engine_result.options,
    )


async def _log_turn(
    db: AsyncSession,
    chatbot_key: ChatbotApiKey,
    message: str,
    selected_value: Optional[str],
    result: TurnResult,
    turn_type: str,
    record: TurnRecord,
) -> None:
    """
    Persist the conversation + performance log for this turn.

    Best effort on purpose: a visitor who has already been answered must not be
    shown a failure because the log write went wrong, so a storage error is
    logged for the operator and swallowed here.
    """
    # A button/dropdown reply carries no typed text — record what the visitor
    # actually chose, so the history reads as a conversation either way.
    visitor_message = (message or "").strip() or (selected_value or "")

    data = {
        "chatbot_key_id": chatbot_key.id,
        "visitor_message": visitor_message,
        "status": result.status,
        "turn_type": turn_type,
        "response_time_ms": result.response_time_ms,
        "request_tokens": record.request_tokens,
        "response_tokens": record.response_tokens,
        "total_tokens": record.total_tokens,
        "llm_call_count": len(record.llm_calls),
        "tokens_estimated": record.tokens_estimated,
        "llm_provider": record.provider,
        "llm_model": record.model,
    }

    if result.status == "success":
        data["ai_response"] = {
            "summary": result.summary,
            "insights": result.insights,
            "table": result.table,
            "action": record.action,
        }
    else:
        data["error_message"] = result.message
        data["ai_response"] = {"action": record.action} if record.action else None

    try:
        await chatbot_message_crud.create(db, data)
    except Exception:
        logger.exception("Could not store the conversation log for chatbot %s", chatbot_key.uuid)
