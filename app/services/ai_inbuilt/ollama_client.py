"""
Generic async client for a locally-running Ollama server — chat completions
and text embeddings. Deliberately has no knowledge of this app's data shapes
(AnalyticsResult, Flow Builder models, etc.) so it stays reusable; callers own
interpreting the raw text/vectors it returns.

Configuration is hardcoded app config (env vars with defaults) rather than a
per-user settings UI, matching this module's scope: a single local Ollama
install serving the whole app, not a per-user credential like the AI Settings
provider keys.
"""

import logging
import os
import re
from dataclasses import dataclass
from typing import List, Optional

import httpx
from litestar.exceptions import HTTPException

logger = logging.getLogger(__name__)

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_CHAT_MODEL = os.getenv("OLLAMA_CHAT_MODEL", "qwen3:1.7b")
OLLAMA_EMBED_MODEL = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")
OLLAMA_CHAT_TIMEOUT_SECONDS = float(os.getenv("OLLAMA_CHAT_TIMEOUT_SECONDS", "120"))
OLLAMA_EMBED_TIMEOUT_SECONDS = float(os.getenv("OLLAMA_EMBED_TIMEOUT_SECONDS", "60"))

def _parse_keep_alive(raw: str):
    """
    Ollama's JSON API accepts keep_alive either as a duration string ("30m") or
    as a NUMBER of seconds, where -1 means "never unload". It rejects the string
    "-1" outright ({"error": "time: missing unit in duration \\"-1\\""}), so a
    bare number from the environment has to be sent as a JSON number.
    """
    try:
        return int(raw)
    except ValueError:
        return raw


# -1 keeps a model resident in RAM indefinitely once loaded, so no request ever
# pays the model reload cost (tens of seconds on a CPU-only host). Both the chat
# and the embed model are pinned — the embed model is hit on every
# knowledge-base lookup and its 274 MB footprint is cheap to keep around.
OLLAMA_KEEP_ALIVE = _parse_keep_alive(os.getenv("OLLAMA_KEEP_ALIVE", "-1"))

# Context window per request. NOT a speed knob — measured on this host, 2048 vs
# 4096 on the same prompt was 4.6 vs 4.5 tok/s, because Ollama sizes the KV cache
# from this but only evaluates the tokens actually sent. What it does control is
# truncation: Ollama silently clips anything longer, and at 1024 an 8-chunk
# knowledge-base prompt lost 743 tokens and returned invalid JSON on every trial.
# 2048 fits the measured ~1270-token worst case with headroom.
OLLAMA_NUM_CTX = int(os.getenv("OLLAMA_NUM_CTX", "2048"))

# Generation threads. Ollama has no OLLAMA_NUM_THREADS env var — thread count
# is a per-request option, which is why it is sent in `options.num_thread`
# below. Default is the physical core count: llama.cpp scales with real cores,
# and oversubscribing with hyperthread siblings (12 on a 6-core i5-10400F)
# makes the threads contend and typically runs slower, not faster.
OLLAMA_NUM_THREAD = int(os.getenv("OLLAMA_NUM_THREAD", "6"))

OLLAMA_NUM_PREDICT = int(os.getenv("OLLAMA_NUM_PREDICT", "512"))

_MAX_EMBED_BATCH = 32

_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)

# One pooled client for the whole process. Re-created per call, the connection
# (and its TCP + HTTP handshake) was thrown away after every single request.
_client: Optional[httpx.AsyncClient] = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            base_url=OLLAMA_BASE_URL.rstrip("/"),
            timeout=OLLAMA_CHAT_TIMEOUT_SECONDS,
            limits=httpx.Limits(max_keepalive_connections=4, max_connections=8),
        )
    return _client


async def close_client() -> None:
    """Release the pooled connection on app shutdown."""
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


async def _post(path: str, payload: dict, timeout: float) -> dict:
    url = f"{OLLAMA_BASE_URL.rstrip('/')}{path}"
    try:
        response = await _get_client().post(path, json=payload, timeout=timeout)
        response.raise_for_status()
        return response.json()
    except httpx.ConnectError:
        logger.exception("Could not connect to Ollama at %s", OLLAMA_BASE_URL)
        raise HTTPException(
            status_code=503,
            detail=(
                f"Could not reach the local Ollama server at {OLLAMA_BASE_URL} — "
                "make sure it is running (`ollama serve`)."
            ),
        )
    except httpx.TimeoutException:
        logger.exception("Ollama request to %s timed out after %ss", url, timeout)
        raise HTTPException(
            status_code=504,
            detail=(
                "The local AI model did not respond in time. It may still be "
                "loading — please try again in a moment."
            ),
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            model = payload.get("model", "the requested model")
            logger.exception("Ollama model not available: %s", model)
            raise HTTPException(
                status_code=502,
                detail=(
                    f"The local model '{model}' is not available in Ollama. "
                    f"Pull it with `ollama pull {model}`."
                ),
            )
        logger.exception("Ollama request to %s failed: %s", url, exc.response.text)
        raise HTTPException(status_code=502, detail=f"The local AI model request failed: {exc.response.text}")
    except ValueError:
        logger.exception("Ollama response from %s was not valid JSON", url)
        raise HTTPException(status_code=502, detail="The local AI model returned an unreadable response.")


def _strip_thinking(content: str) -> str:
    content = _THINK_BLOCK_RE.sub("", content).strip()
    content = _JSON_FENCE_RE.sub("", content).strip()
    return content


def _estimate_tokens(text: str) -> int:
    """
    Rough token count for the pre-flight context check — ~4 characters per token
    for English prose. Only ever used to decide whether to log a warning, never
    to alter a request, so the approximation is good enough.
    """
    return len(text) // 4


def _warn_if_context_too_small(system_prompt: str, user_content: str) -> None:
    """
    Warn *before* sending when the prompt cannot fit the configured window.

    This has to be a pre-flight check on the outgoing text: Ollama truncates an
    over-long prompt and then reports `prompt_eval_count` for what survived, so
    the response count sits *below* num_ctx and cannot reveal the loss.
    Measured on this host — an 8-chunk knowledge-base prompt reported 1257
    tokens at num_ctx=2048 but only 514 tokens at num_ctx=1024, silently
    dropping 743 tokens, with no post-hoc signal that it had happened.
    """
    estimated = _estimate_tokens(system_prompt) + _estimate_tokens(user_content)
    budget = OLLAMA_NUM_CTX - OLLAMA_NUM_PREDICT
    if estimated > budget:
        logger.warning(
            "Prompt is ~%s tokens but only %s are available (OLLAMA_NUM_CTX=%s minus "
            "OLLAMA_NUM_PREDICT=%s). Ollama will truncate it and answer from partial "
            "context — retrieved knowledge-base chunks are what gets dropped. Raise "
            "OLLAMA_NUM_CTX, or lower knowledge_base_service._MAX_CONTEXT_CHUNKS/"
            "_MAX_CONTEXT_CHARS to bound how much context is retrieved.",
            estimated, budget, OLLAMA_NUM_CTX, OLLAMA_NUM_PREDICT,
        )


@dataclass
class ChatCompletion:
    """
    One chat reply plus the token counts Ollama reported for it.

    The counts travel with the text rather than being logged and discarded:
    callers that bill or profile a request (see utils.turn_recorder) need the
    real prompt/output sizes, and Ollama only reports them on the response.
    """

    text: str
    prompt_tokens: int = 0
    output_tokens: int = 0


def _log_timings(prompt_tokens: int, output_tokens: int, eval_nanoseconds: int) -> None:
    """Log throughput so slow answers can be attributed (model vs prompt size)."""
    tokens_per_second = (output_tokens / (eval_nanoseconds / 1e9)) if eval_nanoseconds else 0.0

    logger.info(
        "Ollama chat: model=%s prompt=%s tokens, output=%s tokens, %.1f tok/s, num_ctx=%s",
        OLLAMA_CHAT_MODEL, prompt_tokens, output_tokens, tokens_per_second, OLLAMA_NUM_CTX,
    )


async def chat(system_prompt: str, user_content: str, *, json_mode: bool = True) -> ChatCompletion:
    """Send one chat turn to the local Ollama chat model, return its reply and token usage."""
    _warn_if_context_too_small(system_prompt, user_content)

    payload = {
        "model": OLLAMA_CHAT_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "stream": False,
        "think": False,
        "keep_alive": OLLAMA_KEEP_ALIVE,
        "options": {
            "temperature": 0.2,
            "num_predict": OLLAMA_NUM_PREDICT,
            "num_ctx": OLLAMA_NUM_CTX,
            "num_thread": OLLAMA_NUM_THREAD,
        },
    }
    if json_mode:
        payload["format"] = "json"

    data = await _post("/api/chat", payload, OLLAMA_CHAT_TIMEOUT_SECONDS)

    prompt_tokens = int(data.get("prompt_eval_count") or 0)
    output_tokens = int(data.get("eval_count") or 0)
    _log_timings(prompt_tokens, output_tokens, int(data.get("eval_duration") or 0))

    message = (data.get("message") or {}).get("content")
    if not message:
        raise HTTPException(status_code=502, detail="The local AI model returned an empty response.")

    return ChatCompletion(
        text=_strip_thinking(message),
        prompt_tokens=prompt_tokens,
        output_tokens=output_tokens,
    )


async def embed_texts(texts: List[str], expected_dimensions: Optional[int] = None) -> List[List[float]]:
    """Embed a batch of texts with the local embedding model, preserving order."""
    if not texts:
        return []

    vectors: List[List[float]] = []
    for start in range(0, len(texts), _MAX_EMBED_BATCH):
        batch = texts[start:start + _MAX_EMBED_BATCH]
        data = await _post(
            "/api/embed",
            {
                "model": OLLAMA_EMBED_MODEL,
                "input": batch,
                # Without this the embed model falls back to Ollama's 5-minute
                # default and unloads between knowledge-base lookups, so the
                # next lookup pays a cold load before it can embed anything.
                "keep_alive": OLLAMA_KEEP_ALIVE,
                "options": {"num_thread": OLLAMA_NUM_THREAD},
            },
            OLLAMA_EMBED_TIMEOUT_SECONDS,
        )
        batch_vectors = data.get("embeddings")
        if not batch_vectors or len(batch_vectors) != len(batch):
            raise HTTPException(
                status_code=502,
                detail="The local embedding model returned an unexpected number of vectors.",
            )
        vectors.extend(batch_vectors)

    if expected_dimensions is not None:
        for vector in vectors:
            if len(vector) != expected_dimensions:
                raise HTTPException(
                    status_code=502,
                    detail=(
                        f"The local embedding model '{OLLAMA_EMBED_MODEL}' returned "
                        f"{len(vector)}-dimensional vectors, expected {expected_dimensions}. "
                        "If you've changed OLLAMA_EMBED_MODEL, a new database migration "
                        "is required to match its output size."
                    ),
                )

    return vectors


async def embed_text(text: str, expected_dimensions: Optional[int] = None) -> List[float]:
    vectors = await embed_texts([text], expected_dimensions=expected_dimensions)
    return vectors[0]


async def preload_models() -> None:
    """
    Load both local models into RAM at app startup so the first real user
    request doesn't pay the cold-load cost. An empty `messages`/`input` makes
    Ollama load the model and return immediately without generating.

    `options` must match what chat()/embed_texts() send — Ollama reloads a model
    when a request asks for a different num_ctx, which would defeat the preload.

    Best effort by design: a local Ollama that is down or still pulling a model
    must not stop the whole app from booting, so failures are logged loudly and
    left for the first request to report to the caller.
    """
    for path, payload in (
        (
            "/api/chat",
            {
                "model": OLLAMA_CHAT_MODEL,
                "messages": [],
                "keep_alive": OLLAMA_KEEP_ALIVE,
                "options": {"num_ctx": OLLAMA_NUM_CTX, "num_thread": OLLAMA_NUM_THREAD},
            },
        ),
        (
            "/api/embed",
            {"model": OLLAMA_EMBED_MODEL, "input": "", "keep_alive": OLLAMA_KEEP_ALIVE},
        ),
    ):
        try:
            await _post(path, payload, OLLAMA_CHAT_TIMEOUT_SECONDS)
            logger.info("Preloaded Ollama model '%s' (keep_alive=%s)", payload["model"], OLLAMA_KEEP_ALIVE)
        except HTTPException as exc:
            logger.warning(
                "Could not preload Ollama model '%s': %s The first AI request will be slow.",
                payload["model"], exc.detail,
            )
