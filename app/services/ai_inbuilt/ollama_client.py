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
from typing import List, Optional

import httpx
from litestar.exceptions import HTTPException

logger = logging.getLogger(__name__)

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_CHAT_MODEL = os.getenv("OLLAMA_CHAT_MODEL", "qwen3:8b")
OLLAMA_EMBED_MODEL = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")
OLLAMA_CHAT_TIMEOUT_SECONDS = float(os.getenv("OLLAMA_CHAT_TIMEOUT_SECONDS", "120"))
OLLAMA_EMBED_TIMEOUT_SECONDS = float(os.getenv("OLLAMA_EMBED_TIMEOUT_SECONDS", "60"))
OLLAMA_KEEP_ALIVE = os.getenv("OLLAMA_KEEP_ALIVE", "30m")

_MAX_OUTPUT_TOKENS = 800
_MAX_EMBED_BATCH = 32

_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


async def _post(path: str, payload: dict, timeout: float) -> dict:
    url = f"{OLLAMA_BASE_URL.rstrip('/')}{path}"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, json=payload)
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


async def chat(system_prompt: str, user_content: str, *, json_mode: bool = True) -> str:
    """Send one chat turn to the local Ollama chat model, return the reply text."""
    payload = {
        "model": OLLAMA_CHAT_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "stream": False,
        "think": False,
        "keep_alive": OLLAMA_KEEP_ALIVE,
        "options": {"temperature": 0.2, "num_predict": _MAX_OUTPUT_TOKENS},
    }
    if json_mode:
        payload["format"] = "json"

    data = await _post("/api/chat", payload, OLLAMA_CHAT_TIMEOUT_SECONDS)
    message = (data.get("message") or {}).get("content")
    if not message:
        raise HTTPException(status_code=502, detail="The local AI model returned an empty response.")
    return _strip_thinking(message)


async def embed_texts(texts: List[str], expected_dimensions: Optional[int] = None) -> List[List[float]]:
    """Embed a batch of texts with the local embedding model, preserving order."""
    if not texts:
        return []

    vectors: List[List[float]] = []
    for start in range(0, len(texts), _MAX_EMBED_BATCH):
        batch = texts[start:start + _MAX_EMBED_BATCH]
        data = await _post(
            "/api/embed",
            {"model": OLLAMA_EMBED_MODEL, "input": batch},
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
