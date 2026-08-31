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

import asyncio
import logging
import uuid
from typing import Any, Dict, List, Optional

from litestar.exceptions import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.db_utils import CRUDQueryBuilder
from app.models.chatbot import ChatbotApiKey
from app.models.datasource import DataSource
from app.services.chatbot import chatbot_reply_service, chatbot_service
from app.services.chatbot.chatbot_action_service import maybe_run_action
from app.services.chatbot.chatbot_ai_settings_service import LlmChoice
from app.services.ai_analytics.ai_analytics_service import AnalyticsResult, answer_freeform
from app.services.deep_agents.query_executor import ToolQueryError, describe_result, execute_tool_query
from app.services.flow_builder import knowledge_base_service
from app.services.graph_designer import graph_runner
from app.services.tool_configs import tool_chain_service, tool_config_service
from app.services.tool_configs.tool_chain_graph import describe_stop, run_chain
from app.services.tool_configs.tool_config_service import tables_read

logger = logging.getLogger(__name__)

_VALID_CONTEXT_SOURCES = {"datasource", "knowledge_base", "prompt"}
_VALID_LLM_MODES = {"in_built", "attached"}

# Vector-retrieved document text alone is capped at 4000 (knowledge_base_service's own
# constant). This is the cap on the *composed* context — document text plus every live
# pipeline's and tool config's text joined in — so it needs headroom for more than one
# block without ballooning the prompt unbounded. Kept below OLLAMA_NUM_CTX's prompt
# budget: a 12000-char composed prompt measured ~5500 real Ollama tokens and blew past
# num_ctx=2048, truncating the AI Fallback node's context (2026-08-31 prod log).
_MAX_KB_CONTEXT_CHARS = 7000

_FALLBACK_BASE_SYSTEM_PROMPT = (
    "You answer for a chatbot widget on the business's website. Be concise, "
    "helpful, and professional."
)

datasource_crud = CRUDQueryBuilder(DataSource)


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


def _rows_from_pipeline_result(raw: Any) -> List[Dict[str, Any]]:
    """
    Normalise a graph's last data-producing output into rows for ``describe_result``.

    ``graph_run_service.full_result``'s own contract is "whatever that node returns" —
    a list of row dicts most often, but a bare list, a single dict, or a scalar are all
    legitimate outputs of a graph whose last node is not table-shaped. Each is wrapped
    so ``describe_result`` always has something JSON-row-shaped to describe.
    """
    if isinstance(raw, list):
        return [row if isinstance(row, dict) else {"value": row} for row in raw]
    if isinstance(raw, dict):
        return [raw]
    return [{"value": raw}]


async def _one_pipeline_text(user_id: int, graph_uuid: str, variables: Dict[str, Any]) -> Optional[str]:
    """
    Run one attached pipeline to completion and describe its output, or ``None``.

    ``graph_runner.run_graph``/``full_result`` never raise — every failure of theirs
    already comes back as an outcome or a logged ``None`` — so nothing here needs a
    ``try/except`` of its own. A pipeline that pauses to ask a question, fails, or is
    still running after its own wait budget cannot contribute to an answer that has to
    return this turn, so it is omitted and logged rather than made to block or fail the
    whole AI Fallback answer.
    """
    outcome = await graph_runner.run_graph(user_id, graph_uuid, inputs=variables)
    if not outcome.finished:
        logger.warning(
            "AI Fallback pipeline %s did not finish (kind=%s%s); omitted from context",
            graph_uuid, outcome.kind, f": {outcome.reason}" if outcome.reason else "",
        )
        return None

    raw = await graph_runner.full_result(user_id, outcome.run_id)
    if raw is None:
        logger.warning("AI Fallback pipeline %s produced nothing to read; omitted", graph_uuid)
        return None

    return describe_result(_rows_from_pipeline_result(raw))


async def _pipeline_context_texts(
    user_id: int, graph_ids: List[str], variables: Dict[str, Any],
) -> List[str]:
    """
    Every attached pipeline's text, run **concurrently**.

    Each opens its own database session inside ``graph_runner`` — nothing here is
    shared — and each may wait up to ``graph_runner.WAIT_SECONDS`` (90s) for its own
    run, so running several one after another could turn one chat answer into minutes.
    Tool configs, below, cannot take this path: they share this call's own
    ``AsyncSession``, which one coroutine at a time may use.
    """
    if not graph_ids:
        return []
    texts = await asyncio.gather(*(_one_pipeline_text(user_id, gid, variables) for gid in graph_ids))
    return [text for text in texts if text]


async def _one_tool_config_text(
    db: AsyncSession, user_id: int, tool_config_uuid: str, variables: Dict[str, Any],
) -> Optional[str]:
    """
    Run one attached Tool Config's own stored query and describe the rows, or ``None``.

    ``variables`` — the conversation's own, the same shape ``_step_run_graph`` passes a
    pipeline as ``inputs`` — stands in for ``agent_values``: the values an agent would
    normally have supplied for a declared parameter. A required parameter with no
    matching variable is not caught here; it falls through to ``execute_tool_query``'s
    own validation, which raises ``ToolQueryError`` like any other failed query, and is
    treated the same way — omitted and logged, not a reason to refuse saving the flow,
    because whether the variable exists depends on which path the conversation took.
    """
    try:
        tool_config_id = uuid.UUID(str(tool_config_uuid))
    except ValueError:
        logger.warning("AI Fallback names a malformed tool config id %r; omitted", tool_config_uuid)
        return None

    try:
        tool_config = await tool_config_service.get_tool_config(db, user_id, tool_config_id)
    except HTTPException as exc:
        logger.warning("AI Fallback tool config %s unavailable: %s", tool_config_uuid, exc.detail)
        return None

    if not tool_config.is_enabled:
        logger.warning("AI Fallback tool config %s is disabled; omitted", tool_config_uuid)
        return None

    datasource = await datasource_crud.get_one(db, filters={"id": tool_config.datasource_id})
    if datasource is None:
        logger.warning("AI Fallback tool config %s has no datasource; omitted", tool_config_uuid)
        return None

    chains = await tool_chain_service.build_chains(db, [(tool_config, datasource)])
    chain = chains.get(tool_config.id)

    try:
        if chain is not None and chain.children:
            result = await run_chain(chain, None, variables)
            if result.waiting:
                logger.warning(
                    "AI Fallback tool config %s embeds a chain that stopped to ask a "
                    "question; omitted — cannot pause an answer to wait for one",
                    tool_config_uuid,
                )
                return None
            if result.short_circuited:
                return f"{describe_result([])} {describe_stop(result)}"
            rows = result.rows
        else:
            rows = await execute_tool_query(
                datasource, tool_config.config or {}, tool_config.table_name,
                sql_query=tool_config.sql_query,
                table_names=tables_read(tool_config.table_name, tool_config.extra_tables),
                agent_values=variables,
                sql_params=list(tool_config.sql_params or []),
            )
    except ToolQueryError as exc:
        logger.warning("AI Fallback tool config %s query failed: %s", tool_config_uuid, exc)
        return None

    return describe_result(rows)


async def _tool_config_context_texts(
    db: AsyncSession, user_id: int, tool_config_ids: List[str], variables: Dict[str, Any],
) -> List[str]:
    """Every attached tool config's text, run one at a time — see ``_pipeline_context_texts``
    for why these cannot be gathered concurrently the way pipelines are."""
    texts = []
    for tool_config_id in tool_config_ids:
        text = await _one_tool_config_text(db, user_id, tool_config_id, variables)
        if text:
            texts.append(text)
    return texts


def _compose_kb_context(*text_blocks: Optional[str]) -> Optional[str]:
    """Join whichever knowledge-base source texts are non-empty — the uploaded/typed
    document context plus every live pipeline's and tool config's text — capped so a
    node with several attached sources cannot grow the prompt without bound."""
    blocks = [block for block in text_blocks if block]
    if not blocks:
        return None
    return "\n\n---\n\n".join(blocks)[:_MAX_KB_CONTEXT_CHARS]


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
    session_variables: Optional[dict] = None,
    previous_answer: Optional[str] = None,
) -> AnalyticsResult:
    """
    Answer one turn for this AI Fallback node.

    ``from_selection`` says the visitor clicked a button rather than typing: their
    "question" is an option's label, which changes what this searches a knowledge base
    for. See :func:`_retrieval_query` — it is the difference between answering the
    question the operator wired the block for and answering the words on the button.

    ``session_variables`` is the conversation's own variables — passed to a knowledge
    base's attached pipelines and tool configs as their input, the same way
    ``_step_run_graph`` passes them to a Run Graph block. Absent (``None``) for any
    caller that predates this, including this module's own test stub — treated the
    same as an empty conversation, not an error.

    ``previous_answer`` is this same node's own last answer, set only when
    ``engine_service`` is re-invoking a dead-end node (one with no outgoing edge) for a
    visitor's follow-up message — see ``engine_service._step_ai_fallback``. Folded into
    ``action_context`` below, not ``extra_instructions``: the latter is rendered to the
    model as a guardrail to obey (``_build_prompts``'s "Always follow the owner's
    guardrails: ..."), and a prior answer is conversational continuity, not a rule.

    Explicitly labeled "background only — do not repeat this verbatim": a node's own
    guardrails/prompt often say things like "show the data" or "don't answer outside the
    source", which are reasonable instructions for the first turn but, read literally on
    every turn after, a small model can take as "keep saying the same thing" — so a
    visitor narrowing or changing their question gets a reworded copy of the first
    answer instead of a real answer to what they actually asked this time.
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
    if previous_answer:
        # Background only, not a script to repeat. A node's own guardrails/prompt often
        # say things like "show the data" or "don't answer outside the source" — good
        # advice for the *first* turn, but read literally by a small model on every turn
        # after, "your previous answer was: X" alone reads as "keep saying X" rather than
        # "here is what you already told them; now answer what they're asking *now*." A
        # visitor narrowing or changing their question must get a different answer, not
        # the same one reworded.
        prior_block = (
            "For background only — do not repeat this verbatim, and do not treat it as "
            "the answer to give again — your previous answer in this conversation was:\n"
            f"{previous_answer}\n\n"
            "Now answer the visitor's new message below on its own terms. If it asks for "
            "something narrower, different, or additional compared to your previous "
            "answer, give exactly that — do not just restate what you said before."
        )
        action_context = "\n\n".join(filter(None, [prior_block, action_context]))

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
        doc_context = await knowledge_base_service.retrieve_context(
            db, flow_id, node_id,
            _retrieval_query(custom_prompt, visitor_message, from_selection),
        )

        variables = session_variables or {}
        pipeline_ids = [str(x) for x in (node_data.get("kb_pipeline_ids") or []) if str(x).strip()]
        tool_config_ids = [str(x) for x in (node_data.get("kb_tool_config_ids") or []) if str(x).strip()]

        pipeline_texts = await _pipeline_context_texts(chatbot_key.user_id, pipeline_ids, variables)
        tool_texts = await _tool_config_context_texts(db, chatbot_key.user_id, tool_config_ids, variables)

        context_text = _compose_kb_context(doc_context, *pipeline_texts, *tool_texts)

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
