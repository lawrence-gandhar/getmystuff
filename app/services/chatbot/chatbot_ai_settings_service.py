"""
Business logic for one chatbot's AI configuration — the agent name, the system
prompt, the owner-defined prompt variables substituted into it, and which
language model answers with it.

Prompt variables are the {{NAME}} placeholders inside the prompt. {{AGENT_NAME}}
is built in (bound to the agent_name field); everything else must be declared as
a variable with a value. That is enforced when the prompt is saved rather than
patched over at answer time: a chatbot whose prompt still says
"an assistant for {{COMPANY}}" is a misconfiguration the owner needs to see, not
something to silently blank out mid-conversation.

The LLM choice made here applies to the whole chatbot. A Flow Builder AI
Fallback node keeps its own choice and wins for the turns it handles (see
app.services.flow_builder.ai_fallback_service) — the prompt below is still that
node's base persona.
"""

import json
import re
import uuid
from dataclasses import dataclass
from typing import List, Optional, Tuple

from litestar.exceptions import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.chatbot.queries import ai_settings_crud, get_or_create_ai_settings
from app.db.db_utils import CRUDQueryBuilder
from app.models.ai_settings import AIApiKey
from app.models.chatbot import DEFAULT_SYSTEM_PROMPT, ChatbotAiSettings
from app.services.chatbot.chatbot_service import get_chatbot_key

ai_api_key_crud = CRUDQueryBuilder(AIApiKey)

VALID_LLM_MODES = ("api_key", "in_built")

# Substituted into the prompt from the agent_name field rather than declared as
# a variable, so renaming the agent doesn't mean editing two places.
BUILT_IN_VARIABLES = ("AGENT_NAME",)

_PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_VARIABLE_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,49}$")

_MAX_AGENT_NAME_LEN = 100
_MAX_PROMPT_LEN = 20_000
_MAX_VARIABLES = 30
_MAX_VARIABLE_VALUE_LEN = 500


@dataclass
class AiSettingsInput:
    """Raw form values for the AI & Prompt tab, before validation."""
    agent_name: str
    system_prompt: str
    variables_json: str
    llm_mode: str
    llm_api_key_id: str  # the AI Settings key's public uuid, or "" for "any active key"


@dataclass
class LlmChoice:
    """
    Which model should answer, in the form ai_analytics_service expects.
    ``forced_key_uuid=None`` + ``use_inbuilt_llm=False`` means "resolve the
    user's active AI Settings keys as usual".
    """
    forced_key_uuid: Optional[uuid.UUID] = None
    use_inbuilt_llm: bool = False


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

def _validate_agent_name(value: str) -> str:
    value = (value or "").strip()
    if not value:
        raise HTTPException(status_code=400, detail="Agent name is required")
    if len(value) > _MAX_AGENT_NAME_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"Agent name must not exceed {_MAX_AGENT_NAME_LEN} characters",
        )
    return value


def _parse_variables(raw: str) -> List[dict]:
    """
    Parse the variables editor's hidden JSON field into a clean
    [{"name","value"}] list. Submitted as one JSON field rather than repeated
    form inputs so there is exactly one place to validate its shape.
    """
    raw = (raw or "").strip()
    if not raw:
        return []

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=400,
            detail="Prompt variables could not be read. Please re-enter them and save again.",
        )

    if not isinstance(parsed, list):
        raise HTTPException(status_code=400, detail="Prompt variables must be a list")
    if len(parsed) > _MAX_VARIABLES:
        raise HTTPException(
            status_code=400,
            detail=f"A chatbot can have at most {_MAX_VARIABLES} prompt variables",
        )

    variables: List[dict] = []
    seen: set = set()

    for item in parsed:
        if not isinstance(item, dict):
            raise HTTPException(status_code=400, detail="Each prompt variable must have a name and a value")

        name = str(item.get("name") or "").strip().upper()
        value = str(item.get("value") or "").strip()

        if not name:
            raise HTTPException(status_code=400, detail="Every prompt variable needs a name")
        if not _VARIABLE_NAME_RE.match(name):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Variable name {name!r} is invalid — use uppercase letters, numbers "
                    "and underscores only, starting with a letter (e.g. COMPANY_NAME)"
                ),
            )
        if name in BUILT_IN_VARIABLES:
            raise HTTPException(
                status_code=400,
                detail=f"{name} is set automatically from the agent name — remove it from the variables list",
            )
        if name in seen:
            raise HTTPException(status_code=400, detail=f"Variable {name} is defined more than once")
        if not value:
            raise HTTPException(status_code=400, detail=f"Variable {name} needs a value")
        if len(value) > _MAX_VARIABLE_VALUE_LEN:
            raise HTTPException(
                status_code=400,
                detail=f"Variable {name}'s value must not exceed {_MAX_VARIABLE_VALUE_LEN} characters",
            )

        seen.add(name)
        variables.append({"name": name, "value": value})

    return variables


def _validate_prompt(prompt: str, variables: List[dict]) -> str:
    prompt = (prompt or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="System prompt is required")
    if len(prompt) > _MAX_PROMPT_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"System prompt must not exceed {_MAX_PROMPT_LEN} characters",
        )

    # Variable names are stored upper-cased, so placeholders are matched
    # case-insensitively — {{company}} and {{COMPANY}} are the same variable.
    known = {v["name"] for v in variables} | set(BUILT_IN_VARIABLES)
    missing = sorted({name.upper() for name in _PLACEHOLDER_RE.findall(prompt)} - known)
    if missing:
        listed = ", ".join(f"{{{{{name}}}}}" for name in missing)
        raise HTTPException(
            status_code=400,
            detail=(
                f"Your prompt uses {listed} but no matching variable is set. "
                "Add each one under Prompt Variables, or remove it from the prompt."
            ),
        )

    return prompt


async def _resolve_llm_fields(
    db: AsyncSession,
    user_id: int,
    llm_mode: str,
    llm_api_key_uuid: str,
) -> Tuple[str, Optional[int]]:
    """Validate the LLM choice and resolve the public key uuid to its bigint id."""
    llm_mode = (llm_mode or "api_key").strip()
    if llm_mode not in VALID_LLM_MODES:
        raise HTTPException(status_code=400, detail=f"Invalid language model option: {llm_mode!r}")

    if llm_mode == "in_built":
        # The in-built model is app-wide (see AI_INBUILT.md) — a saved key is
        # meaningless here, so clear any previous pin rather than keeping a
        # stale one that would silently reappear on switching back.
        return llm_mode, None

    raw_uuid = (llm_api_key_uuid or "").strip()
    if not raw_uuid:
        return llm_mode, None

    try:
        key_uuid = uuid.UUID(raw_uuid)
    except ValueError:
        raise HTTPException(status_code=400, detail="The selected AI API key is not valid")

    key = await ai_api_key_crud.get_by_uuid(db, key_uuid, extra_filters={"user_id": user_id})
    if not key:
        raise HTTPException(
            status_code=404,
            detail="The selected AI API key was not found. Pick another one, or add it in AI Settings.",
        )

    return llm_mode, key.id


# --------------------------------------------------------------------------
# Read / write
# --------------------------------------------------------------------------

async def get_ai_settings(db: AsyncSession, user_id: int, key_id: uuid.UUID) -> ChatbotAiSettings:
    """Ownership-checked fetch; creates the row with defaults on first access."""
    key = await get_chatbot_key(db, user_id, key_id)  # 404s if not owned
    return await get_or_create_ai_settings(db, key.id)


async def get_ai_settings_by_key_id(db: AsyncSession, chatbot_key_id: int) -> ChatbotAiSettings:
    """
    Runtime lookup — no ownership check, because the caller already resolved the
    chatbot key from a publishable widget key (see chatbot_reply_service).
    """
    return await get_or_create_ai_settings(db, chatbot_key_id)


async def update_ai_settings(
    db: AsyncSession,
    user_id: int,
    key_id: uuid.UUID,
    fields: AiSettingsInput,
) -> ChatbotAiSettings:
    """Validate and persist the AI & Prompt tab. Nothing is written unless every field passes."""
    key = await get_chatbot_key(db, user_id, key_id)
    settings = await get_or_create_ai_settings(db, key.id)

    agent_name = _validate_agent_name(fields.agent_name)
    variables = _parse_variables(fields.variables_json)
    system_prompt = _validate_prompt(fields.system_prompt, variables)
    llm_mode, llm_api_key_id = await _resolve_llm_fields(
        db, user_id, fields.llm_mode, fields.llm_api_key_id
    )

    return await ai_settings_crud.update(db, settings.id, {
        "agent_name": agent_name,
        "system_prompt": system_prompt,
        "variables": variables,
        "llm_mode": llm_mode,
        "llm_api_key_id": llm_api_key_id,
    })


async def reset_system_prompt(db: AsyncSession, user_id: int, key_id: uuid.UUID) -> ChatbotAiSettings:
    """Restore the built-in default prompt, leaving variables and LLM choice alone."""
    key = await get_chatbot_key(db, user_id, key_id)
    settings = await get_or_create_ai_settings(db, key.id)
    return await ai_settings_crud.update(db, settings.id, {"system_prompt": DEFAULT_SYSTEM_PROMPT})


# --------------------------------------------------------------------------
# Runtime
# --------------------------------------------------------------------------

def variables_map(settings: ChatbotAiSettings) -> dict:
    """
    The full substitution map: the agent name plus every declared variable.
    Also used by chatbot_action_service to fill {{VAR}} placeholders in an
    action's URL, headers and body.
    """
    resolved = {"AGENT_NAME": settings.agent_name}
    for item in settings.variables or []:
        name = str(item.get("name") or "").strip()
        if name:
            resolved[name] = str(item.get("value") or "")
    return resolved


def render_system_prompt(settings: ChatbotAiSettings) -> str:
    """
    The chatbot's prompt with every {{PLACEHOLDER}} replaced.

    Unknown placeholders are left exactly as written: save-time validation
    already rules them out, and leaving a stray one visible is safer than
    silently changing the prompt's meaning if one ever slips through (e.g. a
    row edited directly in the database).
    """
    resolved = variables_map(settings)

    def _replace(match: re.Match) -> str:
        name = match.group(1).upper()
        return resolved.get(name, match.group(0))

    return _PLACEHOLDER_RE.sub(_replace, settings.system_prompt or "")


async def resolve_llm_choice(db: AsyncSession, settings: ChatbotAiSettings) -> LlmChoice:
    """
    Turn the stored LLM mode into the (forced_key_uuid, use_inbuilt_llm) pair
    ai_analytics_service resolves providers with.

    A pinned key that was deleted leaves llm_api_key_id NULL (FK ON DELETE SET
    NULL), which degrades to "any active key" rather than erroring mid-chat.
    """
    if settings.llm_mode == "in_built":
        return LlmChoice(use_inbuilt_llm=True)

    if settings.llm_api_key_id:
        key = await ai_api_key_crud.get_one(db, filters={"id": settings.llm_api_key_id})
        if key:
            return LlmChoice(forced_key_uuid=key.uuid)

    return LlmChoice()
