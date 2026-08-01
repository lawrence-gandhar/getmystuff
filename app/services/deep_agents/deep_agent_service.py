"""
Answering a question with a data agent's Deep Agent.

The public entry point for the whole feature. Given an agent and a question it
builds a Deep Agent whose tools are that agent's tool configs, runs one turn, and
returns the answer with an account of which tools were actually called.

**The guarantee.** The model receives the operator's system prompt, the generated
tool descriptions, the question, and whatever rows its tool calls return. It is
never given a table sample, a schema dump or a connection. Compare the path this
replaces — ``ai_analytics_service.run_grounded_prompt`` reads 500 rows and puts a
profile of them in the prompt. Here, no tool call means no data.

**On deepagents' built-in tools.** ``create_deep_agent`` binds eight tools of its
own alongside ours — ``ls``, ``read_file``, ``write_file``, ``edit_file``,
``delete``, ``glob``, ``grep`` and ``task`` (verified against the tool set
deepagents 0.7.1 actually binds, not its documentation). None of them is a data
path: the default ``StateBackend`` keeps that filesystem in the conversation's own
state — in memory, empty at the start of every turn, never the host's disk — and
the ``execute`` shell tool is not bound at all without a sandbox backend, which
this module does not supply. They cannot be removed either
(``FilesystemMiddleware`` is required scaffolding in deepagents 0.7.x, and
``excluded_middleware`` raises rather than dropping it), so the routing prompt
tells the model explicitly that they are private scratch space and not a source
for answers.
"""

import asyncio
import logging
import os
import uuid
from typing import Any, Dict, List, Optional

from deepagents import create_deep_agent
from litestar.exceptions import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.data_agents import DataAgent
from app.services.data_agents import data_agent_service
from app.services.deep_agents import model_factory
from app.services.deep_agents.prompt_builder import compose_runtime_prompt
from app.services.deep_agents.prompt_sync_service import (
    build_prompt_for_agent,
    collect_agent_tools,
    is_prompt_stale,
    store_tool_routing_prompt,
)
from app.services.deep_agents.tool_factory import (
    build_agent_tools,
    find_unsupported_tools,
    tool_names,
)
from app.utils.turn_recorder import estimate_tokens, record_llm_call

logger = logging.getLogger(__name__)


def _int_env(name: str, default: int) -> int:
    """
    An int from the environment, falling back on anything unparseable.

    Deliberately forgiving: a typo in a timeout variable should not stop the app
    booting, and the default is a working value rather than a placeholder.
    """
    try:
        value = int(os.getenv(name) or 0)
    except ValueError:
        return default

    return value if value > 0 else default

# How long one turn may take. Bounded so a model that keeps calling tools cannot hold a
# request open indefinitely — but the right bound depends on *who is waiting*, not on
# which provider is answering.
#
# A chatbot visitor is waiting on a web request, so 120s is already at the limit of
# reasonable; past that, falling back to the data-profile answer serves them better
# than a spinner. The test console is an operator deliberately running a diagnostic and
# willing to wait.
#
# That distinction matters because the two provider paths differ by an order of
# magnitude. Measured on a 6-core CPU-only host: qwen3:8b runs at ~2.5 tok/s, a single
# tool-calling round trip on a 133-token prompt took 67-81s, and a full two-call turn
# over the real routing prompt took 417s. A hosted provider does the same turn in
# seconds. So in-built mode is usable from the console and not usable for a live widget
# on this class of hardware — which the console timeout permits and the visitor
# timeout, correctly, does not.
_VISITOR_TURN_TIMEOUT_SECONDS = _int_env("DEEP_AGENT_TIMEOUT_SECONDS", 120)
_CONSOLE_TURN_TIMEOUT_SECONDS = _int_env("DEEP_AGENT_CONSOLE_TIMEOUT_SECONDS", 900)

# Each tool call plus its follow-up costs two steps, so this allows roughly a
# dozen tool calls before the graph stops. Reached in practice only when the model
# is looping, which the timeout would otherwise catch far more expensively.
_RECURSION_LIMIT = 25

# How much prior conversation to carry in. Long enough for follow-ups ("and for
# last month?") to resolve, short enough to keep the tool descriptions dominant in
# the context. Matches the history window sql_assist uses.
_MAX_HISTORY_TURNS = 6


async def answer_with_deep_agent(
    db: AsyncSession,
    user_id: int,
    agent_id: uuid.UUID,
    message: str,
    history: Optional[List[dict]] = None,
    forced_key_uuid: Optional[uuid.UUID] = None,
    use_inbuilt_llm: bool = False,
) -> Dict[str, Any]:
    """
    Answer one question as this data agent.

    Returns ``{"answer", "tools_called", "tool_count", "model"}``. ``tools_called``
    is what the operator sees on the console and what makes the guarantee auditable:
    an answer containing figures with an empty ``tools_called`` is a bug, and it is
    visible rather than inferred.
    """
    agent = await data_agent_service.get_data_agent(db, user_id, agent_id)

    return await _answer_as_agent(
        db,
        user_id,
        agent,
        message,
        history=history,
        forced_key_uuid=forced_key_uuid,
        use_inbuilt_llm=use_inbuilt_llm,
        # An operator ran this deliberately and can wait; a slow local model is the
        # thing they are most likely to be testing.
        timeout=_CONSOLE_TURN_TIMEOUT_SECONDS,
    )


async def answer_for_chatbot(
    db: AsyncSession,
    chatbot_key,
    message: str,
    history: Optional[List[dict]] = None,
    forced_key_uuid: Optional[uuid.UUID] = None,
    use_inbuilt_llm: bool = False,
) -> Dict[str, Any]:
    """
    Answer a visitor's message with the data agent attached to this chatbot.

    Resolves the agent from ``chatbot_key.data_agent_id`` (the internal FK) rather
    than a public uuid, and scopes it to ``chatbot_key.user_id``. The scope matters
    even though the FK was ownership-checked when it was attached: it is what stops
    a re-pointed row from ever reaching another user's agent, and with it their
    datasource credentials.

    The caller decides whether an agent is attached at all — see
    ``chatbot_reply_service.generate_reply``.
    """
    agent = await data_agent_service.agent_crud.get_one(db, filters={
        "id": chatbot_key.data_agent_id,
        "user_id": chatbot_key.user_id,
    })

    if not agent:
        raise HTTPException(
            status_code=404,
            detail=(
                "The data agent this chatbot uses was not found. Re-attach one in "
                "the chatbot's AI & Prompt settings."
            ),
        )

    return await _answer_as_agent(
        db,
        chatbot_key.user_id,
        agent,
        message,
        history=history,
        forced_key_uuid=forced_key_uuid,
        use_inbuilt_llm=use_inbuilt_llm,
        # A visitor is waiting on this request. Deliberately NOT widened for the
        # in-built model: an agent too slow to answer inside this budget should degrade
        # to the data-profile reply, not hold a widget open for several minutes.
        timeout=_VISITOR_TURN_TIMEOUT_SECONDS,
    )


async def _answer_as_agent(
    db: AsyncSession,
    user_id: int,
    agent: DataAgent,
    message: str,
    history: Optional[List[dict]] = None,
    forced_key_uuid: Optional[uuid.UUID] = None,
    use_inbuilt_llm: bool = False,
    timeout: int = _VISITOR_TURN_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    """
    Run one turn for an already-resolved agent.

    Both public entry points funnel here so the console and a live chatbot exercise the
    same code — a console test that took a different path would not be worth much as a
    test. ``timeout`` is the one thing they legitimately differ on, and it is the
    caller's to set because only the caller knows who is waiting.
    """
    if not agent.is_active:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Data agent '{agent.name}' is switched off. Enable it on the Data "
                "Agents page before using it."
            ),
        )

    prompt, tools = await _resolved_prompt_and_tools(db, agent)

    if not tools:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Data agent '{agent.name}' has no enabled tools, so it cannot look "
                "anything up. Add a tool for it in the Tool Configs section."
            ),
        )

    model = await model_factory.build_chat_model(
        db, user_id, forced_key_uuid=forced_key_uuid, use_inbuilt_llm=use_inbuilt_llm,
    )

    deep_agent = create_deep_agent(
        model=model,
        tools=build_agent_tools(tools),
        system_prompt=prompt,
    )

    messages = _conversation(history, message)

    try:
        state = await asyncio.wait_for(
            deep_agent.ainvoke(
                {"messages": messages},
                config={"recursion_limit": _RECURSION_LIMIT},
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError as exc:
        logger.warning(
            "Deep agent %s timed out after %ss (in-built model: %s)",
            agent.uuid, timeout, use_inbuilt_llm,
        )
        raise HTTPException(
            status_code=504,
            detail=(
                "The agent took too long to answer. Try a more specific question, or "
                "switch this agent to a saved API key — the in-built local model can "
                "take several minutes per answer on a CPU-only machine."
                if use_inbuilt_llm else
                "The agent took too long to answer. Try a more specific question, "
                "or check that the datasource is responding."
            ),
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:
        # Provider and graph errors both land here. The detail is logged rather
        # than returned: it can carry prompt fragments and endpoint URLs.
        logger.exception("Deep agent %s failed", agent.uuid)
        raise HTTPException(
            status_code=502,
            detail=(
                "The agent could not complete an answer. Please try again, or check "
                "the agent's AI key in AI Settings."
            ),
        ) from exc

    produced = _state_messages(state)
    called = _tools_called(produced)

    _record_usage(produced, model)

    return {
        "answer": _final_text(produced),
        "tools_called": called,
        "tool_count": len(tools),
        "available_tools": tool_names(tools),
        "model": model_factory.describe_model(model),
    }


# --------------------------------------------------------------------------
# Prompt resolution
# --------------------------------------------------------------------------

async def _resolved_prompt_and_tools(
    db: AsyncSession,
    agent: DataAgent,
) -> tuple[str, List[dict]]:
    """
    The runtime prompt and the tools behind it.

    The staleness check is what makes the background sync job optional. If the
    stored prompt is behind the agent's tools — because the job failed, the process
    restarted before it ran, or the row was seeded by a migration — it is rebuilt
    and stored here, in the request. That costs one extra write on the first answer
    after a change and is never wrong; trusting a stale prompt would describe tools
    that no longer exist.
    """
    tools = await collect_agent_tools(db, agent.id)

    if is_prompt_stale(agent, tools):
        logger.info("Rebuilding stale tool routing prompt for data agent %s", agent.uuid)
        prompt, tools = await build_prompt_for_agent(db, agent)
        await store_tool_routing_prompt(db, agent, prompt)
    else:
        prompt = agent.tool_routing_prompt or ""

    return compose_runtime_prompt(agent.system_prompt, prompt), tools


def _conversation(history: Optional[List[dict]], message: str) -> List[dict]:
    """
    The message list for this turn: recent history, then the new question.

    Only ``user`` and ``assistant`` roles are carried over. A stored system message
    is dropped deliberately — the system prompt is rebuilt from the agent every
    turn, so replaying an old one would reintroduce tool descriptions that may no
    longer be true.
    """
    messages: List[dict] = []

    for entry in (history or [])[-_MAX_HISTORY_TURNS:]:
        role = (entry.get("role") or "").strip().lower()
        content = (entry.get("content") or "").strip()

        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})

    messages.append({"role": "user", "content": message})

    return messages


# --------------------------------------------------------------------------
# Reading the result
# --------------------------------------------------------------------------

def _state_messages(state: Any) -> List[Any]:
    """The messages the graph produced, however the state is shaped."""
    if isinstance(state, dict):
        return list(state.get("messages") or [])

    return list(getattr(state, "messages", None) or [])


def _final_text(messages: List[Any]) -> str:
    """
    The agent's answer: the last AI message with actual text.

    Searched backwards past tool messages and past the tool-call-only AI messages
    that carry no prose. An empty result is reported rather than returned as a
    blank reply — a visitor seeing nothing cannot tell it apart from a hang.
    """
    for message in reversed(messages):
        if _message_type(message) != "ai":
            continue

        text = _message_text(message)
        if text:
            return text

    return (
        "I could not produce an answer for that. Please try rephrasing the question."
    )


def _message_text(message: Any) -> str:
    """
    A message's text, flattening the block form.

    Anthropic returns content as a list of typed blocks (thinking, tool_use, text);
    only the text blocks are the answer.
    """
    content = getattr(message, "content", None)

    if isinstance(content, str):
        return content.strip()

    if isinstance(content, list):
        parts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return "\n".join(part for part in parts if part).strip()

    return ""


def _message_type(message: Any) -> str:
    """The message's role, normalised across LangChain's classes and dicts."""
    kind = getattr(message, "type", None)
    if kind:
        return str(kind)

    if isinstance(message, dict):
        return str(message.get("role") or message.get("type") or "")

    return ""


def _tools_called(messages: List[Any]) -> List[str]:
    """
    Which tools the model actually invoked, in order, with repeats kept.

    Repeats are kept on purpose: the same tool called three times is a signal
    (usually a model that did not believe the first result), and collapsing it would
    hide that from the console.
    """
    called: List[str] = []

    for message in messages:
        for call in getattr(message, "tool_calls", None) or []:
            name = call.get("name") if isinstance(call, dict) else getattr(call, "name", None)
            if name:
                called.append(str(name))

    return called


def _record_usage(messages: List[Any], model: Any) -> None:
    """
    Report this run's token cost into the open turn record, if any.

    A Deep Agent turn is several model calls, and each AI message carries its own
    ``usage_metadata``, so they are summed. Falls back to estimating from text
    length — flagged as an estimate — when a provider reports nothing, matching what
    ai_analytics_service does for OpenAI-compatible endpoints that omit usage.
    """
    request_tokens = 0
    response_tokens = 0
    reported = False

    for message in messages:
        if _message_type(message) != "ai":
            continue

        usage = getattr(message, "usage_metadata", None) or {}

        if usage:
            reported = True
            request_tokens += int(usage.get("input_tokens") or 0)
            response_tokens += int(usage.get("output_tokens") or 0)
        else:
            response_tokens += estimate_tokens(_message_text(message))

    record_llm_call(
        provider=type(model).__name__,
        model=model_factory.describe_model(model),
        request_tokens=request_tokens,
        response_tokens=response_tokens,
        estimated=not reported,
    )


async def get_agent_runtime_view(
    db: AsyncSession,
    user_id: int,
    agent_id: uuid.UUID,
) -> Dict[str, Any]:
    """
    What the test console shows before a question is asked: the agent, its tools,
    and any reason it would not run.

    Read-only — no model is built and nothing is queried against a datasource.
    """
    agent = await data_agent_service.get_data_agent(db, user_id, agent_id)
    tools = await collect_agent_tools(db, agent.id)

    return {
        "uuid": str(agent.uuid),
        "name": agent.name,
        "description": agent.description,
        "is_active": agent.is_active,
        "has_system_prompt": bool(agent.system_prompt),
        "tool_routing_prompt": agent.tool_routing_prompt,
        "tool_prompt_synced_at": agent.tool_prompt_synced_at,
        "prompt_is_stale": is_prompt_stale(agent, tools),
        "tools": [
            {
                "uuid": tool["uuid"],
                "tool_name": tool["tool_name"],
                "description": tool["description"],
                "table_name": tool["table_name"],
                "datasource_name": tool["datasource_name"],
                "db_type": tool["db_type"],
            }
            for tool in tools
        ],
        "unsupported_tools": find_unsupported_tools(tools),
    }
