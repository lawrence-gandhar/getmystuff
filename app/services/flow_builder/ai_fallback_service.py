"""
Runtime orchestration for one AI Fallback node's answer — reads the node's
configured guardrails/prompt, context source (the chatbot key's attached
datasource, this node's knowledge base, or prompt-only), and LLM choice
(the user's own attached AI Settings key, or the in-built default), then
asks the right AI provider for an answer.

The chatbot's own configured system prompt (Chatbot Settings -> AI & Prompt) is
the agent's base persona here too; this node's guardrails/prompt layer on top of
it. The node's LLM choice, by contrast, wins outright over the chatbot-level one
for the turns this node handles — it is a deliberate per-node override.

Kept separate from engine_service.py (the graph-interpretation loop): this
module's concern is "how does one AI Fallback node answer," not "which node
runs next" — the same relationship knowledge_base_service.py has to
flow_service.py.
"""

import uuid
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chatbot import ChatbotApiKey
from app.services.chatbot import chatbot_reply_service, chatbot_service
from app.services.chatbot.chatbot_action_service import maybe_run_action
from app.services.chatbot.chatbot_ai_settings_service import LlmChoice
from app.services.ai_analytics.ai_analytics_service import AnalyticsResult, answer_freeform
from app.services.flow_builder import knowledge_base_service

_VALID_CONTEXT_SOURCES = {"datasource", "knowledge_base", "prompt"}
_VALID_LLM_MODES = {"in_built", "attached"}

_FALLBACK_BASE_SYSTEM_PROMPT = (
    "You answer for a chatbot widget on the business's website. Be concise, "
    "helpful, and professional."
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
        parts.append(f"Guardrails: {guardrails}")
    if custom_prompt:
        parts.append(f"Also: {custom_prompt}")
    return "\n\n".join(parts)


def _build_system_prompt(
    base_prompt: str,
    guardrails: str,
    custom_prompt: str,
    has_context: bool,
) -> str:
    """
    Layer this node's context/guardrails/prompt on top of the chatbot's own
    persona (`base_prompt`), falling back to a generic assistant preamble only
    if the chatbot has no prompt at all.
    """
    system_prompt = base_prompt.strip() or _FALLBACK_BASE_SYSTEM_PROMPT
    if has_context:
        system_prompt += (
            " Ground your answer in the knowledge base content below, and say so "
            "if it doesn't cover the question."
        )
    else:
        system_prompt += " Answer helpfully from your own general knowledge."
    if guardrails:
        system_prompt += f"\n\nAlways follow these guardrails: {guardrails}"
    if custom_prompt:
        system_prompt += f"\n\nAlso: {custom_prompt}"
    return system_prompt


def _retrieval_query(custom_prompt: str, visitor_message: str, from_selection: bool) -> str:
    """
    What to search the knowledge base for.

    The visitor's own words — except on a turn where they did not type any. A button
    reply hands on the option's **label** (see ``engine_service._effective_message``),
    and a label is written to be a good thing to *click*, not a good thing to
    *search*. Measured against a real proposal document: "Email me the data" retrieves
    that document's security and authentication sections, so a model told to answer
    strictly from what was retrieved explains that it cannot share user data — an
    answer that is grounded, faithful, and about the wrong subject entirely. The same
    knowledge base searched with the node's own instructions returns the scope,
    deliverables and estimates, which is what the operator was asking for.

    So on a selection turn the node's instructions join the query. They are the
    operator's statement of what this block is *for*, and on a turn where nobody typed
    a question that is the closest thing to one that exists. The label stays in as
    well, so two options wired to two blocks still retrieve differently.

    On a typed turn the instructions are left out: the visitor's question is a better
    query than any standing instruction, and folding in "answer in a friendly tone"
    would make every retrieval slightly worse for no gain.
    """
    message = (visitor_message or "").strip()
    if not from_selection or not custom_prompt:
        return message
    return f"{custom_prompt}\n{message}".strip()


def _build_user_content(
    context_text: Optional[str],
    visitor_message: str,
    action_context: str = "",
) -> str:
    parts = []
    if context_text:
        parts.append(f"Knowledge base context:\n{context_text}")
    if action_context:
        parts.append(action_context)
    parts.append(f"Visitor question: {visitor_message}")
    return "\n\n".join(parts)


async def run_ai_fallback(
    db: AsyncSession,
    chatbot_key: ChatbotApiKey,
    flow_id: int,
    node_id: str,
    node_data: dict,
    visitor_message: str,
    from_selection: bool = False,
) -> AnalyticsResult:
    """
    Answer one turn for this AI Fallback node.

    ``from_selection`` says the visitor clicked a button rather than typing: their
    "question" is an option's label, which changes what this searches a knowledge base
    for. See :func:`_retrieval_query` — it is the difference between answering the
    question the operator wired the block for and answering the words on the button.
    """
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

    # The chatbot's persona/variables come from its AI settings; the node's LLM
    # choice replaces the chatbot-level one for this turn.
    ai_context = await chatbot_reply_service.load_ai_context(db, chatbot_key)
    llm_choice = LlmChoice(forced_key_uuid=forced_key_uuid, use_inbuilt_llm=use_inbuilt_llm)

    outcome = await maybe_run_action(
        db, chatbot_key, visitor_message, llm_choice, ai_context.variables
    )
    action_context = outcome.context_text if outcome else ""

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
            system_prompt_override=ai_context.system_prompt,
            action_context=action_context,
        )

    context_text = None
    if context_source == "knowledge_base":
        context_text = await knowledge_base_service.retrieve_context(
            db, flow_id, node_id,
            _retrieval_query(custom_prompt, visitor_message, from_selection),
        )

    return await answer_freeform(
        db,
        chatbot_key.user_id,
        system_prompt=_build_system_prompt(
            ai_context.system_prompt, guardrails, custom_prompt, has_context=bool(context_text)
        ),
        user_content=_build_user_content(context_text, visitor_message, action_context),
        forced_key_uuid=forced_key_uuid,
        use_inbuilt_llm=use_inbuilt_llm,
    )
