"""
Business logic for Data Agents.

An agent is owned directly by a user, not by a workspace — the workspace is an
optional grouping it can be moved into, out of, or left without (see
app.models.data_agents). :func:`get_data_agent` is the single place a public agent
uuid plus the logged-in user id becomes a row; the Tool Configs module calls it too
when a tool config is assigned to an agent.
"""

import uuid
from typing import List, NoReturn, Optional

from litestar.exceptions import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.data_agents.queries import (
    data_agent_name_exists,
    fetch_agents_with_details,
)
from app.db.db_utils import CRUDQueryBuilder
from app.models.ai_settings import AIApiKey
from app.models.data_agents import DataAgent
from app.services.ai_settings import ai_settings_service
from app.services.workspaces import workspace_service
from app.utils.validators import optional_text, require_text

agent_crud = CRUDQueryBuilder(DataAgent)

# Resolves an AI key uuid to its internal id for the FK. Deliberately not
# ai_settings_service.get_key_details_by_uuid: that decrypts the secret, which has
# no business happening while wiring up a foreign key.
ai_key_crud = CRUDQueryBuilder(AIApiKey)

_NAME_MAX = 255
_DESCRIPTION_MAX = 2000
_SYSTEM_PROMPT_MAX = 20000


# --------------------------------------------------------------------------
# Read
# --------------------------------------------------------------------------

async def get_agent_views(
    db: AsyncSession,
    user_id: int,
    workspace_id: Optional[uuid.UUID] = None,
) -> List[dict]:
    """
    Every agent this user owns, shaped for the list page: public uuids only, plus
    its tool-config count, its workspace and its AI key.

    ``workspace_id`` filters the list to one workspace — that is what the agent
    count on the Workspaces page links to. It is ownership-checked, so filtering by
    someone else's workspace 404s rather than quietly returning nothing.
    """
    internal_workspace_id = None
    if workspace_id is not None:
        workspace = await workspace_service.get_workspace(db, user_id, workspace_id)
        internal_workspace_id = workspace.id

    rows = await fetch_agents_with_details(db, user_id, internal_workspace_id)

    return [
        {
            "uuid": str(agent.uuid),
            "name": agent.name,
            "description": agent.description,
            "system_prompt": agent.system_prompt,
            "is_active": agent.is_active,
            "tool_count": tool_count,
            "workspace_name": workspace_name,
            "workspace_uuid": str(workspace_uuid) if workspace_uuid else "",
            "llm_key_label": key_label,
            "created_at": agent.created_at,
            "updated_at": agent.updated_at,
        }
        for agent, tool_count, workspace_name, workspace_uuid, key_label in rows
    ]


async def get_data_agent(
    db: AsyncSession,
    user_id: int,
    agent_id: uuid.UUID,
) -> DataAgent:
    """
    Resolve an agent by its public uuid, scoped to its owner.

    The 404 is deliberate for an agent that exists but belongs to someone else — a
    403 there would confirm the uuid is real.
    """
    agent = await agent_crud.get_by_uuid(
        db, agent_id, extra_filters={"user_id": user_id},
    )
    if not agent:
        raise HTTPException(status_code=404, detail="Data agent not found")

    return agent


async def get_data_agent_view(
    db: AsyncSession,
    user_id: int,
    agent_id: uuid.UUID,
) -> dict:
    """
    One agent shaped for its edit form — the attached workspace and AI key are
    exposed as *their* public uuids so the dropdowns can preselect them.
    """
    agent = await get_data_agent(db, user_id, agent_id)

    workspace = (
        await workspace_service.workspace_crud.get_one(
            db, filters={"id": agent.workspace_id},
        )
        if agent.workspace_id
        else None
    )
    key = (
        await ai_key_crud.get_one(db, filters={"id": agent.llm_api_key_id})
        if agent.llm_api_key_id
        else None
    )

    return {
        "uuid": str(agent.uuid),
        "name": agent.name,
        "description": agent.description,
        "system_prompt": agent.system_prompt,
        "is_active": agent.is_active,
        "workspace_id": str(workspace.uuid) if workspace else "",
        "workspace_name": workspace.name if workspace else None,
        "llm_api_key_id": str(key.uuid) if key else "",
        "llm_key_label": key.label if key else None,
    }


async def get_agent_choices(db: AsyncSession, user_id: int) -> List[dict]:
    """
    The user's agents as {uuid, name, is_active} for the Tool Configs form's
    dropdown. Disabled agents are marked but still listed — a tool config can be
    prepared before its agent is switched on.
    """
    agents = await agent_crud.get_many(
        db, filters={"user_id": user_id}, order_by="name",
    )
    return [
        {
            "uuid": str(agent.uuid),
            "name": agent.name,
            "is_active": agent.is_active,
        }
        for agent in agents
    ]


async def get_agent_public_id(
    db: AsyncSession,
    user_id: int,
    internal_id: Optional[int],
) -> str:
    """
    The public uuid for an internal agent id, or ``""`` when there is none.

    Exists so a caller holding a foreign key (a chatbot's ``data_agent_id``) can
    preselect the agent in a form without a bigint id ever reaching the template.
    Scoped to the owner, and returns ``""`` rather than raising for a missing or
    foreign row: the caller is rendering a dropdown, and "nothing selected" is the
    correct outcome there, not a 404.
    """
    if not internal_id:
        return ""

    agent = await agent_crud.get_one(
        db, filters={"id": internal_id, "user_id": user_id},
    )

    return str(agent.uuid) if agent else ""


async def get_llm_key_choices(db: AsyncSession, user_id: int) -> List[dict]:
    """
    The user's AI keys as {uuid, label, provider} for the agent form's dropdown.
    Public uuids only — the secret never reaches the template.
    """
    keys = await ai_settings_service.get_user_api_keys(db, user_id)
    return [
        {
            "uuid": str(key.uuid),
            "label": key.label,
            "provider": key.provider_display,
        }
        for key in keys
    ]


# --------------------------------------------------------------------------
# Write
# --------------------------------------------------------------------------

async def create_data_agent(
    db: AsyncSession,
    user_id: int,
    name: str,
    description: Optional[str] = None,
    system_prompt: Optional[str] = None,
    workspace_id: Optional[uuid.UUID] = None,
    llm_api_key_id: Optional[uuid.UUID] = None,
) -> DataAgent:
    """
    Create an agent. Its tools are configured afterwards, in the Tool Configs
    module — a fresh agent has none and so can reach nothing.
    """
    fields = await _validated_fields(
        db, user_id, name, description, system_prompt, workspace_id, llm_api_key_id,
    )

    try:
        return await agent_crud.create(db, {
            "user_id": user_id,
            "is_active": True,
            **fields,
        })
    except IntegrityError as exc:
        await _fail_on_duplicate_name(db, fields["name"], exc)


async def update_data_agent(
    db: AsyncSession,
    user_id: int,
    agent_id: uuid.UUID,
    name: str,
    description: Optional[str] = None,
    system_prompt: Optional[str] = None,
    workspace_id: Optional[uuid.UUID] = None,
    llm_api_key_id: Optional[uuid.UUID] = None,
) -> DataAgent:
    agent = await get_data_agent(db, user_id, agent_id)

    fields = await _validated_fields(
        db,
        user_id,
        name,
        description,
        system_prompt,
        workspace_id,
        llm_api_key_id,
        exclude_id=agent.id,
        current_workspace_id=agent.workspace_id,
    )

    try:
        return await agent_crud.update(db, agent.id, fields)
    except IntegrityError as exc:
        await _fail_on_duplicate_name(db, fields["name"], exc)


async def set_data_agent_active(
    db: AsyncSession,
    user_id: int,
    agent_id: uuid.UUID,
    is_active: bool,
) -> DataAgent:
    """
    Enable or disable an agent. Its tool configs are left untouched, so re-enabling
    brings back exactly the setup it had.
    """
    agent = await get_data_agent(db, user_id, agent_id)
    return await agent_crud.update(db, agent.id, {"is_active": is_active})


async def delete_data_agent(
    db: AsyncSession,
    user_id: int,
    agent_id: uuid.UUID,
) -> None:
    """Delete an agent and every tool config on it (the child FK cascades)."""
    agent = await get_data_agent(db, user_id, agent_id)  # ownership check
    await agent_crud.delete(db, agent.id)


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------

async def _validated_fields(
    db: AsyncSession,
    user_id: int,
    name: str,
    description: Optional[str],
    system_prompt: Optional[str],
    workspace_id: Optional[uuid.UUID],
    llm_api_key_id: Optional[uuid.UUID],
    exclude_id: Optional[int] = None,
    current_workspace_id: Optional[int] = None,
) -> dict:
    """
    Validate the writable fields once for both create and update, returning them as
    the column dict to persist.
    """
    name = require_text(name, "Agent name", _NAME_MAX)

    if await data_agent_name_exists(db, user_id, name, exclude_id=exclude_id):
        raise HTTPException(
            status_code=400,
            detail=f"You already have a data agent named '{name}'",
        )

    return {
        "name": name,
        "description": optional_text(description, "Description", _DESCRIPTION_MAX),
        "system_prompt": optional_text(
            system_prompt, "System prompt", _SYSTEM_PROMPT_MAX,
        ),
        "workspace_id": await _resolve_workspace_id(
            db, user_id, workspace_id, current_workspace_id,
        ),
        "llm_api_key_id": await _resolve_llm_key_id(db, user_id, llm_api_key_id),
    }


async def _resolve_workspace_id(
    db: AsyncSession,
    user_id: int,
    workspace_id: Optional[uuid.UUID],
    current_workspace_id: Optional[int],
) -> Optional[int]:
    """
    Turn the submitted workspace uuid into the internal FK value, or ``None`` when
    the agent is left unassigned.

    An archived workspace is refused only when this is a *move into* it; an agent
    already sitting in one can still be edited, otherwise archiving a workspace
    would make every agent in it unsavable.

    Ownership is resolved through workspace_service, so one user cannot file an
    agent under another user's workspace by pasting its uuid.
    """
    if workspace_id is None:
        return None

    workspace = await workspace_service.get_workspace(db, user_id, workspace_id)

    if not workspace.is_active and workspace.id != current_workspace_id:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Workspace '{workspace.name}' is archived. Restore it before "
                "assigning agents to it."
            ),
        )

    return workspace.id


async def _resolve_llm_key_id(
    db: AsyncSession,
    user_id: int,
    llm_api_key_id: Optional[uuid.UUID],
) -> Optional[int]:
    """
    Turn the submitted AI-key uuid into the internal FK value, or ``None`` when no
    key was picked (an agent may be drafted before a key exists).

    Scoping the lookup to ``user_id`` is what stops one user attaching another
    user's key by pasting its uuid.
    """
    if llm_api_key_id is None:
        return None

    key = await ai_key_crud.get_by_uuid(
        db, llm_api_key_id, extra_filters={"user_id": user_id},
    )
    if not key:
        raise HTTPException(
            status_code=404,
            detail="The selected AI API key was not found. Pick one from AI Settings.",
        )

    return key.id


async def _fail_on_duplicate_name(
    db: AsyncSession,
    name: str,
    exc: IntegrityError,
) -> NoReturn:
    """
    Backstop for the race between the name check above and the write, where
    uq_data_agent_user_name_lower is what catches it.

    The rollback matters: the failed flush leaves the session unusable, and the
    HTMX route goes on to re-render the agents table in that same session, so
    without it the user would get a 500 instead of the message below.
    """
    await db.rollback()
    raise HTTPException(
        status_code=400,
        detail=f"You already have a data agent named '{name}'",
    ) from exc
