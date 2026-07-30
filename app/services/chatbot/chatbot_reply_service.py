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

from dataclasses import dataclass

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
) -> AnalyticsResult:
    """
    Answer a visitor message with the chatbot's configured prompt, model and
    actions. Used for every turn not handled by a Flow Builder node.
    """
    context = await load_ai_context(db, chatbot_key)

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
