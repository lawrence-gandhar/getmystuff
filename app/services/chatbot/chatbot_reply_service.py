"""
Composes one visitor reply from a chatbot's own AI configuration: its rendered
system prompt, its language-model choice, and any webhook action that should run
first.

Sits above chatbot_service rather than inside it — chatbot_service is the layer
the AI settings and action services already build on, so putting the composition
step here keeps the dependency direction one-way (routes -> turn -> reply ->
settings / actions -> chatbot -> ai_analytics).

Composing a reply is all this module does. Deciding whether a flow answers
instead, timing the turn and logging it belong to chatbot_turn_service, one
level up — the Flow Builder engine imports *this* module (via its AI Fallback
node), so the flow decision cannot live here without a circular import.
"""

import logging
from dataclasses import dataclass
from typing import List, Optional

from litestar.exceptions import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chatbot import ChatbotAiSettings, ChatbotApiKey
from app.services.ai_analytics.ai_analytics_service import AnalyticsResult
from app.services.chatbot import chatbot_service
from app.services.chatbot.chatbot_action_service import maybe_run_action
from app.services.chatbot.chatbot_ai_settings_service import (
    LlmChoice,
    get_ai_settings_by_key_id,
    render_system_prompt,
    resolve_llm_choice,
    variables_map,
)
from app.services.deep_agents import deep_agent_service

logger = logging.getLogger(__name__)

# What a visitor is told when the attached agent could not run and the chatbot has
# no datasource target to profile instead (see _generate_deep_agent_reply).
#
# Written to be true regardless of the cause, and to close nothing off: it does not
# blame the visitor, does not name the agent or any part of the configuration, and
# does not promise a specific recovery time. The operator gets the actual reason
# from the log — the visitor gets no detail about a system they cannot see.
_NO_FALLBACK_REPLY = (
    "I can't reach that data at the moment, so I'd rather not guess. Please try "
    "again shortly, or ask me something else in the meantime."
)


@dataclass
class ChatbotAiContext:
    """A chatbot's AI configuration, resolved and ready to answer with."""
    settings: ChatbotAiSettings
    system_prompt: str
    variables: dict
    llm_choice: LlmChoice


async def load_ai_context(db: AsyncSession, chatbot_key: ChatbotApiKey) -> ChatbotAiContext:
    """
    Load and render everything a turn needs from the chatbot's AI settings.

    Also used by the Flow Builder AI Fallback node, which keeps this prompt as
    its base persona but substitutes its own LLM choice.
    """
    settings = await get_ai_settings_by_key_id(db, chatbot_key.id)
    return ChatbotAiContext(
        settings=settings,
        system_prompt=render_system_prompt(settings),
        variables=variables_map(settings),
        llm_choice=await resolve_llm_choice(db, settings),
    )


async def generate_reply(
    db: AsyncSession,
    chatbot_key: ChatbotApiKey,
    message: str,
    history: Optional[List[dict]] = None,
    session_token: str = "",
) -> AnalyticsResult:
    """
    Answer a visitor message with the chatbot's configured prompt, model and
    actions. Used for every turn not handled by a Flow Builder node.

    Two ways this can be answered, decided by whether a data agent is attached:

    * **Attached** — the agent's Deep Agent answers. The model chooses among that
      agent's tools and sees only what they return; no sample of the data reaches
      the prompt at all.
    * **Not attached** — the original path: a statistical profile of the target data
      is computed and put in the prompt.

    Unattached is the default for every existing chatbot, so this branch changes
    nothing until an operator opts one in.
    """
    context = await load_ai_context(db, chatbot_key)

    if getattr(chatbot_key, "data_agent_id", None):
        return await _generate_deep_agent_reply(
            db, chatbot_key, message, context,
            history=history, session_token=session_token,
        )

    outcome = await maybe_run_action(
        db, chatbot_key, message, context.llm_choice, context.variables
    )

    return await chatbot_service.answer_message(
        db,
        chatbot_key,
        message,
        forced_key_uuid=context.llm_choice.forced_key_uuid,
        use_inbuilt_llm=context.llm_choice.use_inbuilt_llm,
        system_prompt_override=context.system_prompt,
        action_context=outcome.context_text if outcome else "",
    )


async def _generate_deep_agent_reply(
    db: AsyncSession,
    chatbot_key: ChatbotApiKey,
    message: str,
    context: ChatbotAiContext,
    history: Optional[List[dict]] = None,
    session_token: str = "",
) -> AnalyticsResult:
    """
    Answer through the attached data agent, degrading to the profile path if it
    cannot run.

    The fallback is the important decision here. A visitor is mid-conversation, and a
    misconfigured agent — no enabled tools, an AI key with no model name, a disabled
    agent — must not turn into an error bubble in a published widget. So the failure
    is logged for the operator and the chatbot answers the way it did before the
    agent was attached.

    That fallback cannot leak data the agent was meant to gate: the profile path is
    scoped to the chatbot's *own* datasource target, which the operator chose when
    they created the widget and which is unchanged by attaching an agent.

    **An agent-backed widget has no such target** (``target_type == "agent"``), and
    so nothing to degrade to. It says it cannot answer instead — see
    :data:`_NO_FALLBACK_REPLY`. That is a worse visitor experience than a profile
    answer and a better one than a wrong answer or an error bubble, and it is the
    trade the operator accepted by not nominating a datasource.

    Webhook actions are deliberately not run on this path. The action router is a
    second model call that picks a webhook, and a Deep Agent already decides for
    itself which tool to call — running both would mean two independent routers
    disagreeing about one turn. Actions on an agent-backed chatbot are a follow-up;
    see documentations/DEEP_AGENTS.md.
    """
    try:
        result = await deep_agent_service.answer_for_chatbot(
            db,
            chatbot_key,
            message,
            # The previous turns, so a follow-up ("and for last month?") and a bare
            # confirmation ("yes") resolve against what was actually said. Read back from
            # the turn log by chatbot_turn_service.recent_history.
            history=history,
            # Scopes any download this turn offers to this one visitor's conversation.
            session_token=session_token,
            forced_key_uuid=context.llm_choice.forced_key_uuid,
            use_inbuilt_llm=context.llm_choice.use_inbuilt_llm,
        )
    except HTTPException as exc:
        if chatbot_key.datasource_id is None:
            # Nothing to fall back to. Logged at warning because it is the operator's
            # to fix — a visitor just failed to get an answer — and the agent's own
            # reason is included, since that is the actionable part.
            logger.warning(
                "Data agent reply failed for chatbot %s (%s) and it has no datasource "
                "target to fall back to. The visitor was told it cannot answer.",
                chatbot_key.uuid,
                exc.detail,
            )
            return AnalyticsResult(summary=_NO_FALLBACK_REPLY)

        logger.warning(
            "Data agent reply failed for chatbot %s (%s) — falling back to the data "
            "profile answer.",
            chatbot_key.uuid,
            exc.detail,
        )
        return await chatbot_service.answer_message(
            db,
            chatbot_key,
            message,
            forced_key_uuid=context.llm_choice.forced_key_uuid,
            use_inbuilt_llm=context.llm_choice.use_inbuilt_llm,
            system_prompt_override=context.system_prompt,
        )

    # The Deep Agent writes prose, not the structured summary/insights/table shape
    # the grounded path returns. Mapping it to `summary` alone is deliberate: an
    # empty `insights` and no `table` is honest about what was produced, and
    # inventing bullet points by splitting the text would be putting words in the
    # model's mouth.
    return AnalyticsResult(summary=result["answer"])
