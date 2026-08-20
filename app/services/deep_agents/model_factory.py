"""
Building the LangChain chat model a Deep Agent runs on.

The rest of the app talks to providers through the ``anthropic`` and ``openai``
SDKs directly, and forces JSON output because that works identically across all
three provider paths. A Deep Agent cannot work that way: it needs *native
tool-calling*, which is what LangChain's chat models expose uniformly. So this
module is the one place a LangChain model object is constructed, and it does it
from exactly the same provider decision every other AI feature uses —
:func:`app.services.ai_analytics.ai_analytics_service.resolve_provider` — rather
than reading the key table again.

Which key is used therefore follows the app's existing precedence: a pinned key
first, then the user's active keys in provider priority order, then the
server-wide ``ANTHROPIC_API_KEY``.
"""

import logging
import os
import uuid
from typing import Optional

from langchain_anthropic import ChatAnthropic
from langchain_core.language_models import BaseChatModel
from langchain_ollama import ChatOllama
from langchain_openai import ChatOpenAI
from litestar.exceptions import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.ai_analytics.ai_analytics_service import (
    ANTHROPIC_MODEL,
    resolve_provider,
)

logger = logging.getLogger(__name__)

# Deep Agent turns are multi-step (call a tool, read the rows, answer), so the
# model needs room for a tool result plus a written answer.
_MAX_TOKENS = 4096

# A routing decision should be near-deterministic: the same question must not pick
# a different tool on a retry.
_TEMPERATURE = 0.0

# How many times the provider SDK retries a 429 before giving up, with its own
# exponential backoff between attempts.
#
# Both SDKs default to 2, which is sized for a provider that rate-limits *per key*:
# two quick retries and you are past your own burst. It is not enough for a gateway
# that queues under load and answers `queue_exceeded` — Cerebras and the other
# OpenAI-compatible hosts do this, and the queue takes seconds to drain, not
# milliseconds. `ai_analytics_service._with_rate_limit_retry` was added for exactly
# that; this is the same decision at the layer a Deep Agent needs it.
#
# **The retry has to live here, on the client, and not around the graph.** A Deep
# Agent turn is a loop — call a tool, read the rows, answer — so re-running
# `deep_agent.ainvoke` on a 429 would re-execute every tool call that had already
# succeeded, which means running the user's SQL again for a failure that happened
# after it. Retrying one HTTP call retries one HTTP call.
#
# The ceiling on all of this is the turn timeout, which is unchanged: a turn that
# spends its whole budget waiting on a queue still ends at _VISITOR_TURN_TIMEOUT_SECONDS
# with the "took too long" message rather than hanging.
MAX_RETRIES = 4


async def build_chat_model(
    db: AsyncSession,
    user_id: int,
    forced_key_uuid: Optional[uuid.UUID] = None,
    use_inbuilt_llm: bool = False,
) -> BaseChatModel:
    """
    The chat model for one Deep Agent run.

    ``use_inbuilt_llm`` mirrors the flag the chatbot and Flow Builder already pass
    around (see ``chatbot_ai_settings_service.LlmChoice``), so a caller does not
    have to learn a second way to express "use the local model".
    """
    if use_inbuilt_llm:
        return _build_ollama_model()

    provider, api_key, base_url, model_name = await resolve_provider(
        db, user_id, forced_key_uuid,
    )

    if provider == "anthropic":
        return ChatAnthropic(
            model=ANTHROPIC_MODEL,
            api_key=api_key,
            max_tokens=_MAX_TOKENS,
            temperature=_TEMPERATURE,
            max_retries=MAX_RETRIES,
        )

    # "openai" and "other" (Cerebras, Groq, Together, self-hosted, ...) both speak
    # the OpenAI chat API. model_name is required for these — unlike the Anthropic
    # path there is no sensible default to fall back on.
    if not model_name:
        raise HTTPException(
            status_code=503,
            detail=(
                "The AI key this agent uses has no model name set. Open AI Settings "
                "and add the model name for that key (for example gpt-4o)."
            ),
        )

    return ChatOpenAI(
        model=model_name,
        api_key=api_key,
        base_url=base_url or None,
        max_tokens=_MAX_TOKENS,
        temperature=_TEMPERATURE,
        max_retries=MAX_RETRIES,
    )


def _build_ollama_model() -> BaseChatModel:
    """
    The in-built local model.

    **OLLAMA_DEEP_AGENT_MODEL overrides OLLAMA_CHAT_MODEL here, and only here.**
    Tool calling and single-shot answering want different models: a data agent needs
    one big enough to hold a tool-calling loop, while everything else in-built (a
    chatbot reply, an AI Fallback node, knowledge-base extraction) is a single
    structured-output call that a small fast model handles well. On a CPU-only host
    that difference is roughly 3x in latency, so promoting the whole app to an 8B
    model to enable one feature would be a poor trade. Two variables, each doing one
    job — and with the override unset this falls back to OLLAMA_CHAT_MODEL, so nothing
    changes for an existing deployment.

    Refused rather than attempted for the small default. A Deep Agent's entire
    behaviour depends on the model deciding to emit a tool call; qwen3:1.7b does
    that unreliably, and the failure mode is not an error but a confident answer
    with no tool call behind it — which is precisely the thing this feature exists
    to prevent. Failing loudly here is the only honest option.
    """
    model = (
        os.getenv("OLLAMA_DEEP_AGENT_MODEL")
        or os.getenv("OLLAMA_CHAT_MODEL")
        or "qwen3:1.7b"
    ).strip()

    if model in _MODELS_WITHOUT_RELIABLE_TOOL_CALLING:
        raise HTTPException(
            status_code=503,
            detail=(
                f"The in-built model ({model}) is too small to use tools reliably, "
                "so a data agent cannot run on it. Pick a saved API key for this "
                "agent, or set OLLAMA_DEEP_AGENT_MODEL to a tool-calling model such "
                "as qwen3:8b or llama3.1:8b."
            ),
        )

    return ChatOllama(
        base_url=(os.getenv("OLLAMA_BASE_URL") or "http://localhost:11434").strip(),
        model=model,
        temperature=_TEMPERATURE,
        num_ctx=_ollama_num_ctx(),
        # A tool call is only useful if it arrives complete. The default
        # OLLAMA_NUM_PREDICT is tuned for short single-shot replies and would
        # truncate a tool call mid-JSON, which reads as a malformed call rather than
        # as "the answer was cut off".
        num_predict=_ollama_num_predict(),
    )


# Models measured on this project's host as unable to hold a tool-calling loop.
# Deliberately a denylist and not an allowlist: an operator who has pulled a model
# we have never heard of should be able to try it.
_MODELS_WITHOUT_RELIABLE_TOOL_CALLING = frozenset({
    "qwen3:0.6b",
    "qwen3:1.7b",
    "llama3.2:1b",
    "tinyllama",
    "gemma3:1b",
})


def _ollama_num_ctx() -> int:
    """
    The context window for the local model.

    The .env OLLAMA_NUM_CTX is tuned for the existing single-shot prompts. A Deep
    Agent conversation is longer — a system prompt describing every tool, plus tool
    results — so this floors it at 8192. Ollama silently *truncates* an
    over-long prompt (see the note in .env), and a truncated tool result is a
    wrong answer rather than an error, so the floor matters.
    """
    try:
        configured = int(os.getenv("OLLAMA_NUM_CTX") or 0)
    except ValueError:
        configured = 0

    return max(configured, 8192)


def _ollama_num_predict() -> int:
    """
    The output token budget for the local model, floored at 1024.

    The .env OLLAMA_NUM_PREDICT is sized for a short single-shot reply. A Deep Agent
    turn needs room for a tool call *and* a written answer, and a truncated tool call
    is worse than a truncated sentence: it arrives as malformed JSON and the graph
    sees a broken call rather than a cut-off answer.
    """
    try:
        configured = int(os.getenv("OLLAMA_NUM_PREDICT") or 0)
    except ValueError:
        configured = 0

    return max(configured, 1024)


def describe_model(model: BaseChatModel) -> str:
    """A short label for logs and the test console — never the API key."""
    for attribute in ("model", "model_name"):
        name = getattr(model, attribute, None)
        if name:
            return f"{type(model).__name__} ({name})"

    return type(model).__name__
