"""
AI-powered analytics generation.

Given a natural-language prompt and a target datasource object (a file, an
RDBMS table, or a MongoDB collection), this service:

  1. Loads a bounded sample of the real data (via db_utils / file_service).
  2. Computes a statistical profile from that sample with pandas — exact
     row counts, per-column aggregate stats, top categorical values.
  3. Sends the profile (never the raw prompt alone) to Claude, instructed to
     answer strictly from the provided numbers rather than guessing.
  4. Persists the prompt + result to prompt_history for later review.

Grounding the model in a computed profile — rather than raw code execution
against user data — keeps the feature accurate without giving the LLM the
ability to run arbitrary code against the application's data stores.
"""

import asyncio
import json
import os
import uuid
from typing import Any, Awaitable, Callable, List, Optional, Tuple, TypeVar

import anthropic
import openai
import pandas as pd
from litestar.exceptions import HTTPException
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.db_utils import (
    CRUDQueryBuilder,
    build_mongo_uri,
    build_rdbms_url,
    fetch_file_listing,
    fetch_file_preview,
    fetch_mongo_rows,
    fetch_rdbms_rows,
)
from app.models.datasource import DataSource, DatasourceFile
from app.models.ai_analytics import PromptHistory
from app.services.ai_inbuilt import ollama_client
from app.services.ai_settings.ai_settings_service import get_active_key_details, get_key_details_by_uuid
from app.services.datasource.metadata_service import get_mongo_collections, get_rdbms_tables
from app.utils.crypto import decrypt_password
from app.utils.file_utils import FILE_BASED_TYPES

datasource_crud = CRUDQueryBuilder(DataSource)
prompt_history_crud = CRUDQueryBuilder(PromptHistory)

_ANTHROPIC_MODEL = "claude-opus-5"
_SAMPLE_ROW_LIMIT = 500
_MAX_PROMPT_LEN = 2000
_VALID_TARGET_TYPES = {"file", "table", "collection"}
_HISTORY_PAGE_SIZE = 10

# Safety cap on how many tables/collections/files get profiled in a single
# prompt (relevant to "whole datasource" targets, and to explicit multi-select
# targets) — keeps prompt size/cost bounded. Truncation is always disclosed to
# the AI via a "note" field rather than silently dropped.
_MAX_TARGETS_PER_PROMPT = 8

# Provider "queue exceeded" / 429 responses (e.g. Cerebras under load) are
# transient — worth a couple of short retries before surfacing an error.
_RATE_LIMIT_MAX_RETRIES = 2
_RATE_LIMIT_BASE_DELAY_SECONDS = 1.0

_T = TypeVar("_T")

# Providers that actually power "Ask AI" today, checked in this order when a
# user has more than one active key across different providers. Any other
# provider (google_gemini, azure_openai) is stored but not called yet.
_PROVIDER_PRIORITY = ("anthropic", "openai", "other")

# Shared by every provider that can't guarantee strict structured output
# (OpenAI-compatible endpoints and the local Ollama model) — asks for JSON
# mode and validates the result against AnalyticsResult by hand.
_JSON_ONLY_INSTRUCTION = (
    "\n\nRespond with ONLY a single valid JSON object — no markdown code "
    "fences, no commentary before or after — matching exactly this shape: "
    '{"summary": "<string>", "insights": ["<string>", ...] (up to 5), '
    '"table": {"columns": ["<string>", ...], "rows": [["<string>", ...], ...]} or null}.'
)


# --------------------------------------------------------------------------
# Structured Claude output — guarantees a predictable shape to render.
# --------------------------------------------------------------------------

class AnalyticsTable(BaseModel):
    columns: List[str]
    rows: List[List[str]]


class AnalyticsResult(BaseModel):
    summary: str = Field(
        description="Concise narrative answer to the user's question, "
        "grounded strictly in the supplied data profile."
    )
    insights: List[str] = Field(
        default_factory=list,
        description="Up to 5 short, standalone bullet-point insights.",
    )
    table: Optional[AnalyticsTable] = Field(
        default=None,
        description="A small table of computed results that directly answers "
        "the prompt (e.g. a group-by breakdown). Omit when a table isn't a "
        "natural fit for the answer.",
    )


# --------------------------------------------------------------------------
# Data profiling
# --------------------------------------------------------------------------

def _round(value: Any) -> Optional[float]:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    return round(float(value), 4)


def _build_data_profile(df: pd.DataFrame) -> dict:
    """Compute a grounded, real-numbers profile of a DataFrame sample."""

    columns: List[dict] = []

    for col in df.columns:
        series = df[col]
        col_info: dict = {
            "name": str(col),
            "dtype": str(series.dtype),
            "null_count": int(series.isna().sum()),
        }

        if pd.api.types.is_numeric_dtype(series):
            desc = series.describe()
            col_info["stats"] = {
                "count": int(desc.get("count", 0)),
                "mean": _round(desc.get("mean")),
                "std": _round(desc.get("std")),
                "min": _round(desc.get("min")),
                "25%": _round(desc.get("25%")),
                "50%": _round(desc.get("50%")),
                "75%": _round(desc.get("75%")),
                "max": _round(desc.get("max")),
            }
        else:
            col_info["top_values"] = (
                series.astype(str).value_counts().head(10).to_dict()
            )

        columns.append(col_info)

    sample = df.head(20)
    sample_rows = sample.astype(object).where(pd.notna(sample), None).to_dict(orient="records")

    return {
        "sampled_row_count": len(df),
        "columns": columns,
        "sample_rows": sample_rows,
    }


async def _resolve_full_datasource_targets(
    db: AsyncSession,
    datasource: DataSource,
) -> List[Tuple[str, str, Optional[int]]]:
    """
    Enumerate every object in a datasource for a "whole datasource" target,
    as (item_target_type, name, file_id) tuples ready for `_load_one_target`.

    File-based datasources are enumerated one entry per physical file
    (labeled by original_filename), not per sheet — loading is keyed off
    file_id regardless of sheet name, so sheet-level entries would imply a
    granularity that doesn't actually exist at load time.
    """
    if datasource.db_type == "mongodb":
        names = await get_mongo_collections(datasource)
        return [("collection", name, None) for name in names]

    if datasource.db_type in FILE_BASED_TYPES:
        result = await db.execute(
            select(DatasourceFile).where(
                DatasourceFile.datasource_id == datasource.id,
                DatasourceFile.is_active == True,  # noqa: E712
            )
        )
        files = result.scalars().all()
        return [("file", f.original_filename, f.id) for f in files]

    names = await get_rdbms_tables(datasource)
    return [("table", name, None) for name in names]


async def _load_one_target(
    db: AsyncSession,
    datasource: DataSource,
    target_type: str,
    target_name: str,
    file_id: Optional[int],
) -> pd.DataFrame:
    if target_type == "file":
        if not file_id:
            raise HTTPException(status_code=400, detail="file_id is required for file targets")

        result = await db.execute(
            select(DatasourceFile).where(
                DatasourceFile.id == file_id,
                DatasourceFile.datasource_id == datasource.id,
                DatasourceFile.is_active == True,  # noqa: E712
            )
        )
        file = result.scalar_one_or_none()
        if not file:
            raise HTTPException(status_code=404, detail="File not found")

        rows = await fetch_file_preview(file.file_path, datasource.db_type, limit=_SAMPLE_ROW_LIMIT)
        return pd.DataFrame(rows)

    password = decrypt_password(datasource.password_encrypted) if datasource.password_encrypted else ""

    if target_type == "collection":
        uri = build_mongo_uri(datasource.host, datasource.port, datasource.username, password)
        rows = await fetch_mongo_rows(uri, datasource.database_name, target_name, limit=_SAMPLE_ROW_LIMIT)
        return pd.DataFrame(rows)

    if target_type == "table":
        url = build_rdbms_url(
            db_type=datasource.db_type,
            host=datasource.host,
            port=datasource.port,
            database=datasource.database_name,
            username=datasource.username,
            password=password,
        )
        rows = await fetch_rdbms_rows(url, datasource.db_type, target_name, limit=_SAMPLE_ROW_LIMIT)
        return pd.DataFrame(rows)

    raise HTTPException(status_code=400, detail=f"Unsupported target_type: {target_type!r}")


# --------------------------------------------------------------------------
# Provider resolution — which saved key (if any) should answer this prompt
# --------------------------------------------------------------------------

async def _resolve_provider(
    db: AsyncSession,
    user_id: int,
    forced_key_uuid: Optional[uuid.UUID] = None,
) -> Tuple[str, str, Optional[str], Optional[str]]:
    """
    Pick which configured AI provider should handle this prompt.

    When `forced_key_uuid` is given (a caller explicitly attached one saved
    key by reference, e.g. a Flow Builder AI Fallback node's "attached LLM
    API" setting), that key is used regardless of its is_active flag —
    otherwise falls back to the "in-built LLM" resolution: the user's active
    AI Settings keys in _PROVIDER_PRIORITY order, then the server-wide .env
    ANTHROPIC_API_KEY.

    Returns (provider, api_key, base_url, model_name).
    """
    if forced_key_uuid is not None:
        details = await get_key_details_by_uuid(db, user_id, forced_key_uuid)
        if not details:
            raise HTTPException(
                status_code=404,
                detail="The selected AI API key was not found. It may have been deleted — pick another one in AI Settings.",
            )
        return details["provider"], details["api_key"], details["base_url"], details["model_name"]

    for provider in _PROVIDER_PRIORITY:
        details = await get_active_key_details(db, user_id, provider)
        if details:
            return provider, details["api_key"], details["base_url"], details["model_name"]

    env_key = os.getenv("ANTHROPIC_API_KEY")
    if env_key:
        return "anthropic", env_key, None, None

    raise HTTPException(
        status_code=503,
        detail=(
            "AI analytics is not configured. Add an active API key in AI "
            "Settings (Anthropic, OpenAI, or Other), or set ANTHROPIC_API_KEY."
        ),
    )


def _build_prompts(
    target_label: str,
    profiles: dict,
    prompt: str,
    extra_instructions: str = "",
) -> Tuple[str, str]:
    system_prompt = (
        "You are a data analyst embedded in the GetMyStuff analytics platform. "
        "You are given a statistical profile computed directly from the user's "
        "real dataset(s) — possibly more than one table/collection/file: exact "
        "sampled row count, computed aggregate statistics per column, top "
        "categorical values, and a small row sample, for each one. Never guess "
        "or fabricate a number — every figure in your answer must come from "
        "the supplied profile(s). If the profile does not contain enough "
        "information to answer precisely, say so explicitly instead of "
        "estimating. When a small table would directly answer the question, "
        "include one; otherwise omit it."
    )
    if extra_instructions:
        system_prompt += (
            "\n\nThe chatbot owner has set the following guardrails/instructions — "
            f"always follow them: {extra_instructions}"
        )
    user_content = (
        f"Dataset(s): {target_label}\n\n"
        f"Data profile (JSON):\n{json.dumps(profiles, default=str)}\n\n"
        f"Question: {prompt}"
    )
    return system_prompt, user_content


# --------------------------------------------------------------------------
# Rate-limit retry — shared by both provider calls below
# --------------------------------------------------------------------------

async def _with_rate_limit_retry(call: Callable[[], Awaitable[_T]]) -> _T:
    """Retry `call` on a 429 rate-limit response, with exponential backoff.

    Any other error propagates immediately on the first attempt.
    """
    for attempt in range(_RATE_LIMIT_MAX_RETRIES + 1):
        try:
            return await call()
        except (anthropic.RateLimitError, openai.RateLimitError):
            if attempt == _RATE_LIMIT_MAX_RETRIES:
                raise
            await asyncio.sleep(_RATE_LIMIT_BASE_DELAY_SECONDS * (2 ** attempt))


# --------------------------------------------------------------------------
# Anthropic (Claude) call
# --------------------------------------------------------------------------

async def _call_claude_core(api_key: str, system_prompt: str, user_content: str) -> AnalyticsResult:
    client = anthropic.AsyncAnthropic(api_key=api_key)

    try:
        response = await _with_rate_limit_retry(lambda: client.messages.parse(
            model=_ANTHROPIC_MODEL,
            max_tokens=4096,
            system=system_prompt,
            messages=[{"role": "user", "content": user_content}],
            output_format=AnalyticsResult,
        ))
    except anthropic.APIConnectionError:
        raise HTTPException(
            status_code=502,
            detail="Could not reach the AI analytics service. Please try again.",
        )
    except anthropic.APIStatusError as e:
        raise HTTPException(status_code=502, detail=f"AI analytics request failed: {e.message}")

    if response.stop_reason == "refusal":
        raise HTTPException(
            status_code=422,
            detail="The AI declined to analyze this prompt. Please rephrase your question.",
        )

    if response.parsed_output is None:
        raise HTTPException(status_code=502, detail="AI analytics returned an unreadable response.")

    return response.parsed_output


async def _call_claude(api_key: str, prompt: str, target_label: str, profiles: dict, extra_instructions: str = "") -> AnalyticsResult:
    system_prompt, user_content = _build_prompts(target_label, profiles, prompt, extra_instructions)
    return await _call_claude_core(api_key, system_prompt, user_content)


# --------------------------------------------------------------------------
# OpenAI-compatible call — covers the "openai" provider and any custom
# OpenAI-compatible endpoint saved under "other" (Cerebras, Groq, Together,
# a self-hosted server, ...). These providers aren't guaranteed to support
# Anthropic-style strict structured outputs, so this asks for JSON mode and
# validates the result against the same AnalyticsResult schema by hand.
# --------------------------------------------------------------------------

async def _call_openai_core(
    api_key: str,
    base_url: Optional[str],
    model_name: Optional[str],
    system_prompt: str,
    user_content: str,
) -> AnalyticsResult:
    if not model_name:
        raise HTTPException(
            status_code=503,
            detail=(
                "This provider's saved key is missing a model name. Edit it in "
                "AI Settings and set the Model Name (and Base URL, if it's a "
                "custom endpoint)."
            ),
        )

    client = openai.AsyncOpenAI(api_key=api_key, base_url=base_url or None)
    system_prompt = system_prompt + _JSON_ONLY_INSTRUCTION

    try:
        response = await _with_rate_limit_retry(lambda: client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            response_format={"type": "json_object"},
        ))
    except openai.APIConnectionError:
        raise HTTPException(
            status_code=502,
            detail="Could not reach the AI analytics service. Please try again.",
        )
    except openai.APIStatusError as e:
        raise HTTPException(status_code=502, detail=f"AI analytics request failed: {e.message}")

    raw = response.choices[0].message.content if response.choices else None
    if not raw:
        raise HTTPException(status_code=502, detail="AI analytics returned an empty response.")

    try:
        return AnalyticsResult.model_validate(json.loads(raw))
    except (json.JSONDecodeError, ValidationError):
        raise HTTPException(status_code=502, detail="AI analytics returned an unreadable response.")


async def _call_openai_compatible(
    api_key: str,
    base_url: Optional[str],
    model_name: Optional[str],
    prompt: str,
    target_label: str,
    profiles: dict,
    extra_instructions: str = "",
) -> AnalyticsResult:
    system_prompt, user_content = _build_prompts(target_label, profiles, prompt, extra_instructions)
    return await _call_openai_core(api_key, base_url, model_name, system_prompt, user_content)


# --------------------------------------------------------------------------
# In-built local LLM (Ollama) call — used only when a caller explicitly opts
# in via use_inbuilt_llm (currently just the Flow Builder AI Fallback node's
# "In-built LLM" option). No api_key/base_url/model_name params: those live
# in app.services.ai_inbuilt.ollama_client's own hardcoded config constants,
# not a saved AI Settings credential.
# --------------------------------------------------------------------------

async def _call_ollama_core(system_prompt: str, user_content: str) -> AnalyticsResult:
    system_prompt = system_prompt + _JSON_ONLY_INSTRUCTION

    try:
        raw = await ollama_client.chat(system_prompt, user_content, json_mode=True)
    except HTTPException:
        # use_inbuilt_llm is only ever set by the Flow Builder AI Fallback
        # node today, which answers a live chatbot visitor — ollama_client's
        # detail text ("make sure it is running", etc.) is operator-facing
        # and must not reach them. Logged (with traceback) by ollama_client
        # itself before this is raised.
        raise HTTPException(
            status_code=503,
            detail="The assistant is temporarily unavailable. Please try again in a moment.",
        )

    try:
        return AnalyticsResult.model_validate(json.loads(raw))
    except (json.JSONDecodeError, ValidationError):
        raise HTTPException(status_code=502, detail="The local AI model returned an unreadable response.")


async def _call_ollama(
    prompt: str,
    target_label: str,
    profiles: dict,
    extra_instructions: str = "",
) -> AnalyticsResult:
    system_prompt, user_content = _build_prompts(target_label, profiles, prompt, extra_instructions)
    return await _call_ollama_core(system_prompt, user_content)


async def answer_with_ai(
    db: AsyncSession,
    user_id: int,
    prompt: str,
    target_label: str,
    profiles: dict,
    extra_instructions: str = "",
    forced_key_uuid: Optional[uuid.UUID] = None,
    use_inbuilt_llm: bool = False,
) -> AnalyticsResult:
    """Resolve a provider and answer a data-profile-grounded prompt with it."""
    if use_inbuilt_llm:
        return await _call_ollama(prompt, target_label, profiles, extra_instructions)

    provider, api_key, base_url, model_name = await _resolve_provider(db, user_id, forced_key_uuid)

    if provider == "anthropic":
        return await _call_claude(api_key, prompt, target_label, profiles, extra_instructions)

    return await _call_openai_compatible(api_key, base_url, model_name, prompt, target_label, profiles, extra_instructions)


async def answer_freeform(
    db: AsyncSession,
    user_id: int,
    system_prompt: str,
    user_content: str,
    forced_key_uuid: Optional[uuid.UUID] = None,
    use_inbuilt_llm: bool = False,
) -> AnalyticsResult:
    """
    Resolve a provider and answer a plain system/user prompt pair with it —
    no data profile involved. Used by the Flow Builder AI Fallback node when
    it's grounded in a knowledge base (or nothing at all) rather than a
    datasource, sharing this module's provider resolution + retry/error
    handling instead of duplicating it.
    """
    if use_inbuilt_llm:
        return await _call_ollama_core(system_prompt, user_content)

    provider, api_key, base_url, model_name = await _resolve_provider(db, user_id, forced_key_uuid)

    if provider == "anthropic":
        return await _call_claude_core(api_key, system_prompt, user_content)

    return await _call_openai_core(api_key, base_url, model_name, system_prompt, user_content)


async def run_grounded_prompt(
    db: AsyncSession,
    user_id: int,
    datasource: DataSource,
    target_type: str,
    target_names: List[str],
    file_ids: List[int],
    prompt: str,
    extra_instructions: str = "",
    forced_key_uuid: Optional[uuid.UUID] = None,
    use_inbuilt_llm: bool = False,
) -> AnalyticsResult:
    """
    Load a real data sample for one or more targets, compute each one's
    statistical profile, and ask the resolved AI provider to answer `prompt`
    grounded in those profiles.

    Shared by both the authenticated "Ask AI" flow (generate_analytics, below)
    and the public chatbot widget flow (chatbot_service.answer_message), so
    the actual data-loading / grounding / AI-calling logic lives in one place.
    `target_type == "datasource"` resolves to every object currently in the
    datasource; otherwise `target_names`/`file_ids` are used as given.

    `extra_instructions` (e.g. a Flow Builder AI Fallback node's guardrails/
    custom prompt), `forced_key_uuid` (its "attached LLM API" choice), and
    `use_inbuilt_llm` (its "In-built LLM" choice) are optional passthroughs to
    answer_with_ai — all default to "unset" so existing callers are unaffected.
    """
    if target_type == "datasource":
        targets = await _resolve_full_datasource_targets(db, datasource)
    elif target_type == "file":
        # Pad missing file_ids rather than silently dropping targets, so a
        # caller misuse (name given without a matching file_id) still surfaces
        # _load_one_target's "file_id is required for file targets" error.
        padded_file_ids = list(file_ids) + [None] * (len(target_names) - len(file_ids))
        targets = [("file", name, fid) for name, fid in zip(target_names, padded_file_ids)]
    else:
        targets = [(target_type, name, None) for name in target_names]

    truncated = len(targets) > _MAX_TARGETS_PER_PROMPT
    targets = targets[:_MAX_TARGETS_PER_PROMPT]

    profiles: List[dict] = []
    for item_type, name, file_id in targets:
        df = await _load_one_target(db, datasource, item_type, name, file_id)
        if df.empty:
            continue
        profiles.append({"target": name, **_build_data_profile(df)})

    if not profiles:
        raise HTTPException(
            status_code=400,
            detail="No data is available for this dataset yet — nothing to analyze.",
        )

    combined_profile: dict = {"targets": profiles}
    if truncated:
        combined_profile["note"] = (
            f"This datasource has more than {_MAX_TARGETS_PER_PROMPT} objects; "
            f"only the first {_MAX_TARGETS_PER_PROMPT} are included above. Ask "
            "about a specific table/file by name for full coverage."
        )

    names = [p["target"] for p in profiles]
    target_label = names[0] if len(names) == 1 else f"{len(names)} datasets ({', '.join(names)})"

    return await answer_with_ai(
        db, user_id, prompt, target_label, combined_profile, extra_instructions, forced_key_uuid, use_inbuilt_llm,
    )


# --------------------------------------------------------------------------
# Public entry points
# --------------------------------------------------------------------------

async def generate_analytics(
    db: AsyncSession,
    user_id: int,
    datasource_id: uuid.UUID,
    target_type: str,
    target_name: str,
    prompt: str,
    file_id: Optional[uuid.UUID] = None,
) -> PromptHistory:
    """Run an AI analytics prompt against a datasource target and store the result."""

    prompt = (prompt or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="Prompt is required")
    if len(prompt) > _MAX_PROMPT_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"Prompt must not exceed {_MAX_PROMPT_LEN} characters",
        )

    if target_type not in _VALID_TARGET_TYPES:
        raise HTTPException(status_code=400, detail=f"Invalid target_type: {target_type!r}")
    if not target_name:
        raise HTTPException(status_code=400, detail="target_name is required")

    datasource = await datasource_crud.get_by_uuid(
        db, datasource_id, extra_filters={"user_id": user_id}
    )
    if not datasource:
        raise HTTPException(status_code=404, detail="Datasource not found")

    resolved_file_id: Optional[int] = None
    if file_id:
        file_result = await db.execute(
            select(DatasourceFile).where(
                DatasourceFile.uuid == file_id,
                DatasourceFile.datasource_id == datasource.id,
            )
        )
        file_obj = file_result.scalar_one_or_none()
        if not file_obj:
            raise HTTPException(status_code=404, detail="File not found")
        resolved_file_id = file_obj.id

    try:
        result = await run_grounded_prompt(
            db,
            user_id,
            datasource,
            target_type,
            target_names=[target_name],
            file_ids=[resolved_file_id] if resolved_file_id else [],
            prompt=prompt,
        )

        history = await prompt_history_crud.create(db, {
            "user_id": user_id,
            "datasource_id": datasource.id,
            "target_type": target_type,
            "target_name": target_name,
            "file_id": resolved_file_id,
            "prompt": prompt,
            "status": "success",
            "summary": result.summary,
            "insights": result.insights or None,
            "result_table": result.table.model_dump() if result.table else None,
        })
        return history

    except HTTPException as exc:
        await prompt_history_crud.create(db, {
            "user_id": user_id,
            "datasource_id": datasource.id,
            "target_type": target_type,
            "target_name": target_name,
            "file_id": resolved_file_id,
            "prompt": prompt,
            "status": "error",
            "error_message": str(exc.detail),
        })
        raise


async def get_prompt_history(
    db: AsyncSession,
    user_id: int,
    datasource_id: uuid.UUID,
    target_type: str,
    target_name: str,
) -> List[PromptHistory]:
    """Return the most recent AI analytics runs for a given datasource target."""

    datasource = await datasource_crud.get_by_uuid(
        db, datasource_id, extra_filters={"user_id": user_id}
    )
    if not datasource:
        return []

    result = await db.execute(
        select(PromptHistory)
        .where(
            PromptHistory.user_id == user_id,
            PromptHistory.datasource_id == datasource.id,
            PromptHistory.target_type == target_type,
            PromptHistory.target_name == target_name,
        )
        .order_by(PromptHistory.created_at.desc())
        .limit(_HISTORY_PAGE_SIZE)
    )
    return list(result.scalars().all())
