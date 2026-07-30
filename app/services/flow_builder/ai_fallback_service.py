"""
Runtime orchestration for one AI Fallback node's answer — reads the node's
configured guardrails/prompt, context source (the chatbot key's attached
datasource, this node's knowledge base, or prompt-only), and LLM choice
(the user's own attached AI Settings key, or the in-built default), then
asks the right AI provider for an answer.

Kept separate from engine_service.py (the graph-interpretation loop): this
module's concern is "how does one AI Fallback node answer," not "which node
runs next" — the same relationship knowledge_base_service.py has to
flow_service.py.
"""

import uuid
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chatbot import ChatbotApiKey
from app.services.chatbot import chatbot_service
from app.services.ai_analytics.ai_analytics_service import AnalyticsResult, answer_freeform
from app.services.flow_builder import knowledge_base_service

_VALID_CONTEXT_SOURCES = {"datasource", "knowledge_base", "prompt"}
_VALID_LLM_MODES = {"in_built", "attached"}

_BASE_SYSTEM_PROMPT = (
    "You are an AI assistant answering on behalf of a chatbot widget embedded "
    "on the business's website. Be concise, helpful, and professional."
)


def _parse_uuid(value) -> Optional[uuid.UUID]:
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


def _combine_instructions(guardrails: str, custom_prompt: str) -> str:
    parts = []
    if guardrails:
        parts.append(f"Guardrails (must always be followed): {guardrails}")
    if custom_prompt:
        parts.append(f"Additional instructions: {custom_prompt}")
    return "\n\n".join(parts)


def _build_system_prompt(guardrails: str, custom_prompt: str, has_context: bool) -> str:
    system_prompt = _BASE_SYSTEM_PROMPT
    if has_context:
        system_prompt += (
            " You are given reference material retrieved from the business's "
            "knowledge base below — ground your answer in it, and say so "
            "explicitly if it doesn't contain enough information to answer."
        )
    else:
        system_prompt += " Answer helpfully from your own general knowledge."
    if guardrails:
        system_prompt += f"\n\nGuardrails you must always follow: {guardrails}"
    if custom_prompt:
        system_prompt += f"\n\nAdditional instructions: {custom_prompt}"
    return system_prompt


def _build_user_content(context_text: Optional[str], visitor_message: str) -> str:
    if context_text:
        return f"Knowledge base context:\n{context_text}\n\nVisitor question: {visitor_message}"
    return f"Visitor question: {visitor_message}"


async def run_ai_fallback(
    db: AsyncSession,
    chatbot_key: ChatbotApiKey,
    flow_id: int,
    node_id: str,
    node_data: dict,
    visitor_message: str,
) -> AnalyticsResult:
    context_source = node_data.get("context_source") or "datasource"
    if context_source not in _VALID_CONTEXT_SOURCES:
        context_source = "datasource"

    llm_mode = node_data.get("llm_mode") or "in_built"
    if llm_mode not in _VALID_LLM_MODES:
        llm_mode = "in_built"
    # Mutually exclusive by construction: "attached" forces one saved AI
    # Settings key by uuid; "in_built" calls the local Ollama model instead
    # of any saved credential.
    forced_key_uuid = _parse_uuid(node_data.get("llm_api_key_id")) if llm_mode == "attached" else None
    use_inbuilt_llm = llm_mode == "in_built"

    guardrails = (node_data.get("guardrails") or "").strip()
    custom_prompt = (node_data.get("prompt") or "").strip()

    if context_source == "datasource":
        # Reuses the chatbot key's own attached datasource, exactly like AI
        # Fallback's original behavior — guardrails/prompt layer on top via
        # extra_instructions, LLM choice via forced_key_uuid.
        return await chatbot_service.answer_message(
            db,
            chatbot_key,
            visitor_message,
            extra_instructions=_combine_instructions(guardrails, custom_prompt),
            forced_key_uuid=forced_key_uuid,
            use_inbuilt_llm=use_inbuilt_llm,
        )

    context_text = None
    if context_source == "knowledge_base":
        context_text = await knowledge_base_service.retrieve_context(db, flow_id, node_id, visitor_message)

    return await answer_freeform(
        db,
        chatbot_key.user_id,
        system_prompt=_build_system_prompt(guardrails, custom_prompt, has_context=bool(context_text)),
        user_content=_build_user_content(context_text, visitor_message),
        forced_key_uuid=forced_key_uuid,
        use_inbuilt_llm=use_inbuilt_llm,
    )
