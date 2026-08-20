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
from typing import AsyncIterator, List, Optional

from litestar.exceptions import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from sqlalchemy import select

from app.db.db_utils import CRUDQueryBuilder
from app.models.chatbot import TURN_TYPE_AI, TURN_TYPE_FLOW, ChatbotApiKey, ChatbotMessage
from app.services.chatbot.chatbot_reply_service import generate_reply, load_ai_context
from app.services.deep_agents import deep_agent_service
from app.services.downloader_agents.base.download_notice import (
    current_download,
    download_scope,
)
from app.services.flow_builder import engine_service, flow_service
from app.utils.turn_recorder import TurnRecord, record_turn

logger = logging.getLogger(__name__)

chatbot_message_crud = CRUDQueryBuilder(ChatbotMessage)

_GENERIC_FAILURE_MESSAGE = (
    "Sorry, something went wrong while answering that. Please try again."
)

# Said instead of free AI answering when a visitor runs out of flow and there is no
# AI path behind it — a flow-only chatbot, i.e. one with no datasource of its own and
# a data agent that has no enabled tools (see _can_answer_off_flow).
#
# This is the honest answer for that configuration, not a failure: the operator built
# a fixed-steps conversation and never gave the widget anything to answer freely from,
# so the flow's own boundary is the whole scope. Pointing at the restart control is the
# actionable part — it is the only way back into the flow, and the widget always has it.
_FLOW_ONLY_MESSAGE = (
    "That's everything I can help with here. Use the restart button at the top of "
    "this chat to go through the options again."
)

# How many previous turns are read back and handed to the answering model. Six, matching
# deep_agent_service._MAX_HISTORY_TURNS and sql_assist: long enough for a follow-up to
# resolve against what was just said, short enough that the tool descriptions stay
# dominant in the context.
_HISTORY_TURNS = 6


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
    # The export this turn queued or reported on, when it touched one — see
    # downloader_agents.base.download_notice. Set for the turn that says "yes" and for
    # any later turn that asks where the file is; None for every other turn, which is
    # nearly all of them.
    download: Optional[dict] = None
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
    engine_service.AI_HANDOFF) — unless there is no AI path to take over, which
    is the flow-only case in :func:`_can_answer_off_flow`.

    Never raises for an answering failure — a visitor-safe message is returned
    (and stored) instead, because the widget shows whatever comes back and must
    not be handed an internal error.
    """
    with record_turn() as record, download_scope():
        result, turn_type = await _run_turn(db, chatbot_key, message, session_token, selected_value)
        result.response_time_ms = record.elapsed_ms()
        result.download = current_download()
        await _log_turn(
            db, chatbot_key, message, selected_value, result, turn_type, record,
            session_token=session_token,
        )

    return result


async def _run_turn(
    db: AsyncSession,
    chatbot_key: ChatbotApiKey,
    message: str,
    session_token: str,
    selected_value: Optional[str],
) -> tuple[TurnResult, str]:
    """Route the turn to the flow engine, to plain AI answering, or to neither."""
    try:
        if session_token:
            active_flow = await flow_service.get_active_flow(db, chatbot_key.id)
            if active_flow:
                engine_result = await engine_service.advance_flow_session(
                    db, chatbot_key, active_flow, session_token, message, selected_value
                )
                if engine_result.type != engine_service.AI_HANDOFF:
                    return _from_flow(engine_result), TURN_TYPE_FLOW

                if not await _can_answer_off_flow(db, chatbot_key):
                    # A flow-only chatbot. Checked here rather than left to fail in the
                    # AI path because the two produce different sentences for the same
                    # visitor: this one describes the chatbot's real scope, while the AI
                    # path can only report that it cannot reach data the operator never
                    # attached. Logged as a flow turn, which is what it is — no model ran.
                    return (
                        TurnResult(type="text", summary=_FLOW_ONLY_MESSAGE),
                        TURN_TYPE_FLOW,
                    )

        history = await recent_history(db, chatbot_key.id, session_token)
        reply = await generate_reply(
            db, chatbot_key, message, history=history, session_token=session_token,
        )
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


async def _can_answer_off_flow(db: AsyncSession, chatbot_key: ChatbotApiKey) -> bool:
    """
    Whether this chatbot can answer anything once its flow is finished with a visitor.

    Two ways it can, mirroring :func:`chatbot_reply_service.generate_reply`:

    * it has a datasource target of its own, so the data-profile path always answers; or
    * its data agent has at least one enabled tool.

    Neither means the chatbot is *only* its flow. Attaching a data agent is not enough
    on its own — an agent with no enabled tools is refused by
    ``deep_agent_service._prepared_turn`` before a model is built, so a chatbot behind
    one has no AI path at all, only a flow.

    Cheap on purpose: one query, and only on the handoff turn — the turn that would
    otherwise have gone to a model.
    """
    if chatbot_key.datasource_id is not None:
        return True

    if not chatbot_key.data_agent_id:
        return False

    return await deep_agent_service.agent_has_enabled_tools(db, chatbot_key.data_agent_id)


def _from_flow(engine_result: engine_service.FlowEngineResult) -> TurnResult:
    return TurnResult(
        type=engine_result.type,
        summary=engine_result.text or "",
        insights=engine_result.insights,
        table=engine_result.table,
        options=engine_result.options,
    )


async def stream_turn(
    db: AsyncSession,
    chatbot_key: ChatbotApiKey,
    message: str,
    session_token: str = "",
) -> AsyncIterator[dict]:
    """
    Answer one visitor message as a stream of events, and log the turn at the end.

    The streaming twin of :func:`answer_turn`, and it lives here for the same reason that
    one does: this is the module that owns the log row, and a turn that streamed but was
    never logged would be missing from the owner's history and from Chatbot Analytics.

    **Not every turn can stream, and the ones that cannot say so.** A turn handled by an
    active Flow Builder node produces buttons and dropdowns, not prose, and there is
    nothing to stream — likewise a chatbot with no data agent attached, which answers
    from a computed data profile in one step. Both yield a single ``fallback`` event, and
    the widget posts to ``/message`` instead. Pretending to stream a turn that arrives
    whole would be slower than not streaming it, since nothing could render until the end
    anyway.

    A third ``fallback`` is decided mid-stream: an agent that could not start at all
    (``reason: "agent_unavailable"``). That one exists so the blocking path's degradation
    — a data-profile answer, or a flow-only chatbot's own scope sentence — is what the
    visitor sees, rather than a configuration message meant for the owner.

    Event shapes are ``deep_agent_service._stream_as_agent``'s, plus that ``fallback``.
    """
    if not chatbot_key.data_agent_id:
        yield {"event": "fallback", "reason": "no_agent"}
        return

    if session_token and await flow_service.get_active_flow(db, chatbot_key.id):
        # A flow may or may not hand this turn over to the AI — only advancing it can say
        # — and advancing it here would consume the visitor's message twice.
        yield {"event": "fallback", "reason": "flow_active"}
        return

    context = await load_ai_context(db, chatbot_key)
    history = await recent_history(db, chatbot_key.id, session_token)

    with record_turn() as record, download_scope():
        result = TurnResult(type="text")
        events = deep_agent_service.stream_answer_for_chatbot(
            db,
            chatbot_key,
            message,
            history=history,
            session_token=session_token,
            forced_key_uuid=context.llm_choice.forced_key_uuid,
            use_inbuilt_llm=context.llm_choice.use_inbuilt_llm,
        )

        async for event in events:
            kind = event.get("event")

            if kind == "error" and event.get("stage") == "setup":
                # The agent could not even start — no enabled tools, switched off, an AI
                # key with no model name. Nothing ran, nothing was streamed and nothing
                # is logged here, so handing the turn to /message costs one fast retry
                # and gets the visitor chatbot_reply_service's degraded answer (a data
                # profile where there is one, otherwise a sentence that names no system
                # they cannot see) instead of the operator's to-do list in a red bubble.
                #
                # Only `stage: "setup"` may be retried this way. A timeout or a
                # rate-limit arrives after real work and re-running it would bill the
                # owner twice for one question — see the widget's own `error` listener,
                # which refuses to re-POST for exactly that reason.
                logger.warning(
                    "Data agent stream for chatbot %s could not start (%s) — falling "
                    "back to the blocking turn.",
                    chatbot_key.uuid,
                    event.get("message"),
                )
                yield {"event": "fallback", "reason": "agent_unavailable"}
                return

            if kind == "done":
                result.summary = str(event.get("answer") or "")
                # Attached to the last event rather than sent as one of its own. A tool
                # runs before the answer it feeds, so by the time `done` is passing
                # through, a download this turn started is already known — and the widget
                # gets the message and the thing to put under it together, which is one
                # render instead of two.
                result.download = current_download()
                event["download"] = result.download
            elif kind == "error":
                result.status = "error"
                result.message = str(event.get("message") or _GENERIC_FAILURE_MESSAGE)

            yield event

        result.response_time_ms = record.elapsed_ms()
        await _log_turn(
            db, chatbot_key, message, None, result, TURN_TYPE_AI, record,
            session_token=session_token,
        )


async def recent_history(
    db: AsyncSession,
    chatbot_key_id: int,
    session_token: str,
    limit: int = _HISTORY_TURNS,
) -> List[dict]:
    """
    The last few turns of one visitor's conversation, oldest first.

    ``[{"role": "user"|"assistant", "content": str}, ...]`` — the shape
    ``deep_agent_service._conversation`` expects.

    **Why this exists.** A visitor answering "yes" is answering something the assistant
    said on the previous turn, and without that turn the reply is unanswerable. It is
    read back out of the log this module already writes rather than held in a session,
    because the log is the durable record and a second copy of the same conversation is
    a second thing that can be wrong.

    Returns nothing without a ``session_token``. Filtering on ``chatbot_key_id`` alone
    would hand one visitor another visitor's conversation, which is worse than having no
    history at all — so the absence of a token means the absence of a history.

    Only successful turns. An error turn's ``visitor_message`` was never answered, and
    replaying the question without an answer invites the model to answer it a turn late.
    """
    if not session_token:
        return []

    statement = (
        select(ChatbotMessage)
        .where(
            ChatbotMessage.chatbot_key_id == chatbot_key_id,
            ChatbotMessage.session_token == session_token,
            ChatbotMessage.status == "success",
        )
        .order_by(ChatbotMessage.created_at.desc(), ChatbotMessage.id.desc())
        .limit(max(1, int(limit)))
    )

    rows = list((await db.execute(statement)).scalars().all())

    history: List[dict] = []

    # Reversed: the query takes the newest N, the model reads oldest first.
    for row in reversed(rows):
        if row.visitor_message:
            history.append({"role": "user", "content": row.visitor_message})

        summary = (row.ai_response or {}).get("summary") if row.ai_response else None

        if summary:
            history.append({"role": "assistant", "content": str(summary)})

    return history


async def _log_turn(
    db: AsyncSession,
    chatbot_key: ChatbotApiKey,
    message: str,
    selected_value: Optional[str],
    result: TurnResult,
    turn_type: str,
    record: TurnRecord,
    *,
    session_token: str,
) -> None:
    """
    Persist the conversation + performance log for this turn.

    Best effort on purpose: a visitor who has already been answered must not be
    shown a failure because the log write went wrong, so a storage error is
    logged for the operator and swallowed here.

    ``session_token`` is keyword-only and has no default deliberately. It is what makes
    the stored rows readable back as one conversation — :func:`recent_history` filters on
    it, and without it a visitor's "yes" has nothing to refer to. A default would let a
    new call site drop it silently and only show up as a chatbot that has forgotten
    everything it just said.
    """
    # A button/dropdown reply carries no typed text — record what the visitor
    # actually chose, so the history reads as a conversation either way.
    visitor_message = (message or "").strip() or (selected_value or "")

    data = {
        "chatbot_key_id": chatbot_key.id,
        "session_token": session_token or None,
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
