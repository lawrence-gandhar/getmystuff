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
from typing import Any, AsyncIterator, Dict, List, Optional

import anthropic
import openai
from deepagents import create_deep_agent
from litestar.exceptions import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.data_agents import DataAgent
from app.services.agent_recursive_dataframes.aggregate_tools import aggregate_context
from app.services.data_agents import data_agent_service
from app.services.deep_agents import model_factory
from app.services.deep_agents.prompt_builder import (
    INTERNAL_CALL_TAG,
    compose_runtime_prompt,
)
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
from app.services.downloader_agents.base.download_tools import DownloadContext
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

# A provider refusing because it is busy, from either SDK. Caught separately from
# everything else below, and that separation is the whole point of naming it: a 429
# is transient and nothing about the configuration is wrong, so it must not be
# reported with advice to go and check an API key. The model client has already
# retried it (model_factory.MAX_RETRIES) by the time one of these reaches us — so
# this is not "the provider was busy", it is "the provider was still busy after
# several attempts spaced out over seconds", which is worth telling someone about.
_RATE_LIMIT_ERRORS = (anthropic.RateLimitError, openai.RateLimitError)

# What the operator and the console are told when that happens. It names the cause,
# because "try again" without one is indistinguishable from a broken agent, and
# promises nothing about when — the queue is not ours to predict.
#
# The *visitor* never sees this. chatbot_reply_service degrades to _NO_FALLBACK_REPLY,
# which says the same thing without naming a system they cannot see.
_BUSY_MESSAGE = (
    "The AI provider is busy and could not answer in time. This is temporary — "
    "please try again in a moment. Nothing needs changing in AI Settings."
)


async def agent_has_enabled_tools(db: AsyncSession, data_agent_id: int) -> bool:
    """
    Whether this agent has anything to answer with — the cheap question a caller can
    ask *before* handing it a turn.

    Deliberately built on :func:`collect_agent_tools`, the same call
    :func:`_prepared_turn` refuses on, rather than a count of its own. A second query
    with its own idea of what counts as a tool is a second thing to get wrong: nested
    children and published graphs are tools here, so a plain ``tool_configs`` count
    would report "no tools" for an agent that runs perfectly well.
    """
    return bool(await collect_agent_tools(db, data_agent_id))


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
        # No session token on the console: there is one operator, and the agent is the
        # whole scope an export needs to be confined to.
        download_context=DownloadContext(data_agent_id=agent.id),
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
    session_token: Optional[str] = None,
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
        # The key and the token together scope an export to this one conversation, so a
        # visitor can neither confirm nor download another visitor's file.
        download_context=DownloadContext(
            data_agent_id=agent.id,
            session_token=session_token,
            chatbot_key_id=chatbot_key.id,
            chatbot_key_uuid=str(chatbot_key.uuid),
        ),
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
    download_context: Optional[DownloadContext] = None,
) -> Dict[str, Any]:
    """
    Run one turn for an already-resolved agent.

    Both public entry points funnel here so the console and a live chatbot exercise the
    same code — a console test that took a different path would not be worth much as a
    test. ``timeout`` is the one thing they legitimately differ on, and it is the
    caller's to set because only the caller knows who is waiting.
    """
    deep_agent, tools, model = await _prepared_turn(
        db,
        user_id,
        agent,
        forced_key_uuid=forced_key_uuid,
        use_inbuilt_llm=use_inbuilt_llm,
        download_context=download_context,
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
    except _RATE_LIMIT_ERRORS as exc:
        # Told apart from the catch-all below on purpose. This is the provider saying
        # "busy, come back" — the key is fine, the prompt is fine, the agent is fine —
        # and lumping it in with the message underneath sent whoever read the log off
        # to check an API key that was never the problem.
        logger.warning(
            "Deep agent %s was rate-limited by its provider after %s retries: %s",
            agent.uuid, model_factory.MAX_RETRIES, exc,
        )
        raise HTTPException(status_code=503, detail=_BUSY_MESSAGE) from exc
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
# Streaming
# --------------------------------------------------------------------------

async def stream_answer_with_deep_agent(
    db: AsyncSession,
    user_id: int,
    agent_id: uuid.UUID,
    message: str,
    history: Optional[List[dict]] = None,
    forced_key_uuid: Optional[uuid.UUID] = None,
    use_inbuilt_llm: bool = False,
) -> AsyncIterator[Dict[str, Any]]:
    """
    Answer as this agent, yielding the answer as it is written.

    Same turn as :func:`answer_with_deep_agent` — same prompt, same tools, same
    validation — reported as it happens rather than at the end. The console's blocking
    endpoint is unchanged and still there; this is an addition, so nothing that worked
    before depends on a browser being able to hold an event stream open.
    """
    agent = await data_agent_service.get_data_agent(db, user_id, agent_id)

    async for event in _stream_as_agent(
        db,
        user_id,
        agent,
        message,
        history=history,
        forced_key_uuid=forced_key_uuid,
        use_inbuilt_llm=use_inbuilt_llm,
        timeout=_CONSOLE_TURN_TIMEOUT_SECONDS,
        download_context=DownloadContext(data_agent_id=agent.id),
    ):
        yield event


async def stream_answer_for_chatbot(
    db: AsyncSession,
    chatbot_key,
    message: str,
    history: Optional[List[dict]] = None,
    forced_key_uuid: Optional[uuid.UUID] = None,
    use_inbuilt_llm: bool = False,
    session_token: Optional[str] = None,
) -> AsyncIterator[Dict[str, Any]]:
    """Answer a visitor's message as the attached data agent, streaming it."""
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

    async for event in _stream_as_agent(
        db,
        chatbot_key.user_id,
        agent,
        message,
        history=history,
        forced_key_uuid=forced_key_uuid,
        use_inbuilt_llm=use_inbuilt_llm,
        timeout=_VISITOR_TURN_TIMEOUT_SECONDS,
        download_context=DownloadContext(
            data_agent_id=agent.id,
            session_token=session_token,
            chatbot_key_id=chatbot_key.id,
            chatbot_key_uuid=str(chatbot_key.uuid),
        ),
    ):
        yield event


async def _stream_as_agent(
    db: AsyncSession,
    user_id: int,
    agent: DataAgent,
    message: str,
    history: Optional[List[dict]] = None,
    forced_key_uuid: Optional[uuid.UUID] = None,
    use_inbuilt_llm: bool = False,
    timeout: int = _VISITOR_TURN_TIMEOUT_SECONDS,
    download_context: Optional[DownloadContext] = None,
) -> AsyncIterator[Dict[str, Any]]:
    """
    Run one turn, yielding events as the agent works.

    Four event shapes, and each exists because something is otherwise invisible:

    ``{"event": "tool", "name": ...}``
        A tool has started. This is the long part of a turn — it runs a real query
        against a real database — and without it the interface is silent for exactly as
        long as the work takes.
    ``{"event": "token", "text": ...}``
        A fragment of the answer. Appending these is the answer.
    ``{"event": "done", ...}``
        The finished answer plus the same payload
        :func:`_answer_as_agent` returns, so a consumer can render the final state from
        one event rather than from an accumulator it has been keeping.
    ``{"event": "error", "message": ...}``
        A visitor-safe sentence. Yielded rather than raised: once a stream has begun the
        status code is already sent, so an exception here would truncate the response
        with no explanation in it. Carries ``"stage": "setup"`` when the turn never
        started — see below.

    ``astream_events`` rather than ``astream``: the token stream lives on
    ``on_chat_model_stream``, and a graph-level stream would only surface whole messages
    — which is the blocking behaviour with extra steps.
    """
    try:
        deep_agent, tools, model = await _prepared_turn(
            db,
            user_id,
            agent,
            forced_key_uuid=forced_key_uuid,
            use_inbuilt_llm=use_inbuilt_llm,
            download_context=download_context,
        )
    except HTTPException as exc:
        # Setup failures are still ordinary refusals — a switched-off agent, no tools, a
        # key with no model name — and the operator needs the sentence, not a dead
        # stream.
        #
        # ``stage: "setup"`` marks the one class of failure where *nothing ran*: no model
        # was built, no tool was called, no token was streamed. That distinction is what
        # lets a caller retry the turn somewhere else — see chatbot_turn_service.
        # stream_turn, which turns this into a `fallback` so a published widget degrades
        # instead of showing a visitor the operator's configuration to-do list. Every
        # other error below is raised *after* work was done and must never be retried.
        yield {"event": "error", "message": str(exc.detail), "stage": "setup"}
        return

    messages = _conversation(history, message)
    collected: List[str] = []
    ai_messages: List[Any] = []
    called: List[str] = []

    try:
        async for event in _agent_events(deep_agent, messages, timeout):
            kind = event.get("event")

            if kind == "on_tool_start":
                name = str(event.get("name") or "")
                if name:
                    called.append(name)
                    yield {"event": "tool", "name": name}
                continue

            if kind == "on_chat_model_stream":
                # Not every model call in a turn is the agent talking. `aggregate_records`
                # makes one of its own to plan the aggregation, and it runs inside a tool —
                # so without this the plan's raw JSON was streamed as answer text, printed
                # above the answer it produced. See prompt_builder.INTERNAL_CALL_TAG.
                if INTERNAL_CALL_TAG in (event.get("tags") or ()):
                    continue

                text = _chunk_text(event)
                if text:
                    collected.append(text)
                    yield {"event": "token", "text": text}
                continue

            if kind == "on_chat_model_end":
                # Kept for the token accounting only. Each model call reports its own
                # usage, and a streamed turn is several calls.
                output = (event.get("data") or {}).get("output")
                if output is not None:
                    ai_messages.append(output)
    except asyncio.TimeoutError:
        logger.warning(
            "Deep agent %s timed out after %ss while streaming", agent.uuid, timeout,
        )
        yield {
            "event": "error",
            "message": (
                "The agent took too long to answer. Try a more specific question, or "
                "check that the datasource is responding."
            ),
        }
        return
    except _RATE_LIMIT_ERRORS as exc:
        logger.warning(
            "Deep agent %s was rate-limited by its provider while streaming after %s "
            "retries: %s",
            agent.uuid, model_factory.MAX_RETRIES, exc,
        )
        yield {"event": "error", "message": _BUSY_MESSAGE}
        return
    except Exception:  # noqa: BLE001 — one turn's failure, phrased for whoever asked
        logger.exception("Deep agent %s failed while streaming", agent.uuid)
        yield {
            "event": "error",
            "message": (
                "The agent could not complete an answer. Please try again, or check "
                "the agent's AI key in AI Settings."
            ),
        }
        return

    _record_usage(ai_messages, model)

    answer = "".join(collected).strip() or _final_text([])

    yield {
        "event": "done",
        "answer": answer,
        "tools_called": called,
        "tool_count": len(tools),
        "available_tools": tool_names(tools),
        "model": model_factory.describe_model(model),
    }


async def _agent_events(
    deep_agent: Any,
    messages: List[dict],
    timeout: int,
) -> AsyncIterator[Dict[str, Any]]:
    """
    ``astream_events`` with a wall-clock bound on the whole turn.

    ``asyncio.wait_for`` cannot wrap an async generator, so the budget is applied to each
    ``__anext__`` — with the remaining budget, not the full one, so a model producing a
    token every second forever still stops at the limit rather than never.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout

    stream = deep_agent.astream_events(
        {"messages": messages},
        version="v2",
        config={"recursion_limit": _RECURSION_LIMIT},
    )

    iterator = stream.__aiter__()

    while True:
        remaining = deadline - loop.time()

        if remaining <= 0:
            raise asyncio.TimeoutError

        try:
            event = await asyncio.wait_for(iterator.__anext__(), timeout=remaining)
        except StopAsyncIteration:
            return

        yield event


def _chunk_text(event: Dict[str, Any]) -> str:
    """
    The text in one ``on_chat_model_stream`` event, if it carries any.

    A chunk's content is a string for most providers and a list of typed blocks for
    Anthropic; both shapes are read here. Tool-call and thinking blocks yield nothing,
    which is correct — they are not part of the answer.

    **Nothing is stripped.** This is the one difference from :func:`_message_text`, and
    it is the whole reason this function is not that one. A chunk boundary falls wherever
    the provider's tokeniser put it, very often on a space, so trimming each chunk
    concatenates "Here" and "are" into "Hereare" and "There are " and "125" into
    "There are125". Whitespace inside a stream is content.
    """
    chunk = (event.get("data") or {}).get("chunk")

    if chunk is None:
        return ""

    content = getattr(chunk, "content", None)

    if isinstance(content, str):
        return content

    if isinstance(content, list):
        return "".join(
            str(block.get("text") or "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )

    return ""


# --------------------------------------------------------------------------
# Prompt resolution
# --------------------------------------------------------------------------

async def _prepared_turn(
    db: AsyncSession,
    user_id: int,
    agent: DataAgent,
    forced_key_uuid: Optional[uuid.UUID] = None,
    use_inbuilt_llm: bool = False,
    download_context: Optional[DownloadContext] = None,
) -> tuple:
    """
    Everything a turn needs before it runs: ``(deep_agent, tools, model)``.

    Shared by the blocking and streaming paths so they cannot diverge. The two refusals
    here — a switched-off agent, an agent with no tools — are the ones that must happen
    before any model is built, because building one costs a database read and a
    decryption for a turn that is not going to run.
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
        tools=build_agent_tools(
            tools,
            download_context=download_context,
            # None unless one of this agent's tools was opted in, in which case the
            # tool list and the routing prompt are both exactly what they were
            # before the capability existed. The same model plans the grouping as
            # answers the turn — a second provider decision here would be a second
            # thing to configure and a second thing to get wrong.
            aggregate_context=aggregate_context(tools, model),
        ),
        system_prompt=prompt,
    )

    return deep_agent, tools, model


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
        "tools": [_console_tool(tool) for tool in tools],
        # Whether *any* source may have its whole result read. The console says one thing
        # or the other about filtering depending on this, because "each tool runs a fixed
        # query and takes no arguments" is only the whole truth when nothing is opted in —
        # and half a truth here is what leaves an operator unable to explain why their
        # agent refused to filter by month.
        "has_readable_tools": any(
            tool.get("allow_recursive_aggregate") for tool in tools
        ),
        "unsupported_tools": find_unsupported_tools(tools),
    }


def _console_tool(tool: Dict[str, Any]) -> Dict[str, Any]:
    """
    One row of the console's tool list.

    **Two kinds arrive in ``collect_agent_tools``'s list and they do not share a source.**
    A tool config reads one table of one datasource; a graph holds nodes that each read
    their own, so it has no ``table_name``, no ``datasource_name`` and no ``db_type`` —
    and its public identifier is ``graph_uuid``, because ``uuid`` in that entry would be
    ambiguous about which of the two things it names.

    Branched here rather than defaulted, for two reasons. Reading the tool-config keys off
    a graph entry is what raised ``KeyError: 'uuid'`` on this page as soon as an agent had
    a graph — a crash, but at least an obvious one. Defaulting them to ``""`` would have
    been worse: the console would render "in ()" and read as a *broken tool config*,
    sending the operator to check a datasource that was never involved.

    ``find_unsupported_tools`` already skips graph entries for the same reason, and
    ``prompt_builder`` already branches on ``kind`` — this was the one consumer of the
    shared list that did not.
    """
    # Whether the agent may read this source's whole result and filter or total it. On the
    # console because its absence is invisible and its consequence is not: an operator who
    # has written "filter on created_at" into a description, and left the switch off, gets
    # an agent that says it cannot filter by month — with nothing on the page connecting
    # the two. See documentations/AGENT_RECURSIVE_DATAFRAMES.md.
    readable = bool(tool.get("allow_recursive_aggregate"))

    if tool.get("kind") == "graph":
        return {
            "kind": "graph",
            "uuid": tool["graph_uuid"],
            "tool_name": tool["tool_name"],
            "description": tool.get("description"),
            "node_count": tool.get("node_count") or 0,
            "asks_questions": bool(tool.get("asks_questions")),
            "whole_result_readable": readable,
        }

    return {
        "kind": "tool_config",
        "uuid": tool["uuid"],
        "tool_name": tool["tool_name"],
        "description": tool["description"],
        "table_name": tool["table_name"],
        "datasource_name": tool["datasource_name"],
        "db_type": tool["db_type"],
        "whole_result_readable": readable,
    }
