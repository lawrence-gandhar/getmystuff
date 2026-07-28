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

import json
import os
import uuid
from typing import Any, List, Optional, Tuple

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
    fetch_file_preview,
    fetch_mongo_rows,
    fetch_rdbms_rows,
)
from app.models.datasource import DataSource, DatasourceFile
from app.models.prompt_history import PromptHistory
from app.services.ai_settings_service import get_active_key_details
from app.utils.crypto import decrypt_password

datasource_crud = CRUDQueryBuilder(DataSource)
prompt_history_crud = CRUDQueryBuilder(PromptHistory)

_ANTHROPIC_MODEL = "claude-opus-5"
_SAMPLE_ROW_LIMIT = 500
_MAX_PROMPT_LEN = 2000
_VALID_TARGET_TYPES = {"file", "table", "collection"}
_HISTORY_PAGE_SIZE = 10

# Providers that actually power "Ask AI" today, checked in this order when a
# user has more than one active key across different providers. Any other
# provider (google_gemini, azure_openai) is stored but not called yet.
_PROVIDER_PRIORITY = ("anthropic", "openai", "other")


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


async def _load_dataframe(
    db: AsyncSession,
    datasource: DataSource,
    target_type: str,
    target_name: str,
    file_id: Optional[uuid.UUID],
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

async def _resolve_active_provider(
    db: AsyncSession,
    user_id: uuid.UUID,
) -> Tuple[str, str, Optional[str], Optional[str]]:
    """
    Pick which configured AI provider should handle this prompt.

    Checks the user's active AI Settings keys in _PROVIDER_PRIORITY order,
    then falls back to the server-wide .env ANTHROPIC_API_KEY.

    Returns (provider, api_key, base_url, model_name).
    """
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


def _build_prompts(target_name: str, profile: dict, prompt: str) -> Tuple[str, str]:
    system_prompt = (
        "You are a data analyst embedded in the GetMyStuff analytics platform. "
        "You are given a statistical profile computed directly from the user's "
        "real dataset: exact sampled row count, computed aggregate statistics "
        "per column, top categorical values, and a small row sample. Never "
        "guess or fabricate a number — every figure in your answer must come "
        "from the supplied profile. If the profile does not contain enough "
        "information to answer precisely, say so explicitly instead of "
        "estimating. When a small table would directly answer the question, "
        "include one; otherwise omit it."
    )
    user_content = (
        f"Dataset: {target_name}\n\n"
        f"Data profile (JSON):\n{json.dumps(profile, default=str)}\n\n"
        f"Question: {prompt}"
    )
    return system_prompt, user_content


# --------------------------------------------------------------------------
# Anthropic (Claude) call
# --------------------------------------------------------------------------

async def _call_claude(api_key: str, prompt: str, target_name: str, profile: dict) -> AnalyticsResult:
    client = anthropic.AsyncAnthropic(api_key=api_key)
    system_prompt, user_content = _build_prompts(target_name, profile, prompt)

    try:
        response = await client.messages.parse(
            model=_ANTHROPIC_MODEL,
            max_tokens=4096,
            system=system_prompt,
            messages=[{"role": "user", "content": user_content}],
            output_format=AnalyticsResult,
        )
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


# --------------------------------------------------------------------------
# OpenAI-compatible call — covers the "openai" provider and any custom
# OpenAI-compatible endpoint saved under "other" (Cerebras, Groq, Together,
# a self-hosted server, ...). These providers aren't guaranteed to support
# Anthropic-style strict structured outputs, so this asks for JSON mode and
# validates the result against the same AnalyticsResult schema by hand.
# --------------------------------------------------------------------------

async def _call_openai_compatible(
    api_key: str,
    base_url: Optional[str],
    model_name: Optional[str],
    prompt: str,
    target_name: str,
    profile: dict,
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
    system_prompt, user_content = _build_prompts(target_name, profile, prompt)
    system_prompt += (
        "\n\nRespond with ONLY a single valid JSON object — no markdown code "
        "fences, no commentary before or after — matching exactly this shape: "
        '{"summary": "<string>", "insights": ["<string>", ...] (up to 5), '
        '"table": {"columns": ["<string>", ...], "rows": [["<string>", ...], ...]} or null}.'
    )

    try:
        response = await client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            response_format={"type": "json_object"},
        )
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


async def _call_ai(
    db: AsyncSession,
    user_id: uuid.UUID,
    prompt: str,
    target_name: str,
    profile: dict,
) -> AnalyticsResult:
    provider, api_key, base_url, model_name = await _resolve_active_provider(db, user_id)

    if provider == "anthropic":
        return await _call_claude(api_key, prompt, target_name, profile)

    return await _call_openai_compatible(api_key, base_url, model_name, prompt, target_name, profile)


# --------------------------------------------------------------------------
# Public entry points
# --------------------------------------------------------------------------

async def generate_analytics(
    db: AsyncSession,
    user_id: uuid.UUID,
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

    datasource = await datasource_crud.get_one(
        db, filters={"id": datasource_id, "user_id": user_id}
    )
    if not datasource:
        raise HTTPException(status_code=404, detail="Datasource not found")

    try:
        df = await _load_dataframe(db, datasource, target_type, target_name, file_id)
        if df.empty:
            raise HTTPException(
                status_code=400,
                detail="No data is available for this dataset yet — nothing to analyze.",
            )

        profile = _build_data_profile(df)
        result = await _call_ai(db, user_id, prompt, target_name, profile)

        history = await prompt_history_crud.create(db, {
            "user_id": user_id,
            "datasource_id": datasource_id,
            "target_type": target_type,
            "target_name": target_name,
            "file_id": file_id,
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
            "datasource_id": datasource_id,
            "target_type": target_type,
            "target_name": target_name,
            "file_id": file_id,
            "prompt": prompt,
            "status": "error",
            "error_message": str(exc.detail),
        })
        raise


async def get_prompt_history(
    db: AsyncSession,
    user_id: uuid.UUID,
    datasource_id: uuid.UUID,
    target_type: str,
    target_name: str,
) -> List[PromptHistory]:
    """Return the most recent AI analytics runs for a given datasource target."""

    result = await db.execute(
        select(PromptHistory)
        .where(
            PromptHistory.user_id == user_id,
            PromptHistory.datasource_id == datasource_id,
            PromptHistory.target_type == target_type,
            PromptHistory.target_name == target_name,
        )
        .order_by(PromptHistory.created_at.desc())
        .limit(_HISTORY_PAGE_SIZE)
    )
    return list(result.scalars().all())
