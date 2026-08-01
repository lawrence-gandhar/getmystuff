"""
Business logic for Workspaces.

A workspace is a grouping, nothing more: it owns no agents outright, and deleting
one unassigns the agents pointing at it rather than deleting them (see the FK note
in app.models.workspaces). That keeps this module independent of the Data Agents
module — the only thing they share is the nullable ``workspace_id`` column.

:func:`get_workspace` is the single place a public workspace uuid plus the
logged-in user id becomes a row; the Data Agents module calls it too, when an
agent is assigned to a workspace.
"""

import uuid
from typing import List, NoReturn, Optional

from litestar.exceptions import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.db_utils import CRUDQueryBuilder
from app.db.workspaces.queries import (
    fetch_workspaces_with_agent_counts,
    workspace_name_exists,
)
from app.models.workspaces import Workspace
from app.utils.validators import optional_text, require_text

workspace_crud = CRUDQueryBuilder(Workspace)

_NAME_MAX = 255
_DESCRIPTION_MAX = 2000


# --------------------------------------------------------------------------
# Read
# --------------------------------------------------------------------------

async def get_user_workspace_views(db: AsyncSession, user_id: int) -> List[dict]:
    """
    Every workspace this user owns, shaped for the list page: public uuid only,
    plus how many data agents are assigned to it.
    """
    rows = await fetch_workspaces_with_agent_counts(db, user_id)
    return [
        {
            "uuid": str(workspace.uuid),
            "name": workspace.name,
            "description": workspace.description,
            "is_active": workspace.is_active,
            "agent_count": agent_count,
            "created_at": workspace.created_at,
            "updated_at": workspace.updated_at,
        }
        for workspace, agent_count in rows
    ]


async def get_workspace(
    db: AsyncSession,
    user_id: int,
    workspace_id: uuid.UUID,
) -> Workspace:
    """
    Resolve a workspace by its public uuid, scoped to its owner.

    The 404 is deliberate for a workspace that exists but belongs to someone else
    — a 403 there would confirm the uuid is real.
    """
    workspace = await workspace_crud.get_by_uuid(
        db, workspace_id, extra_filters={"user_id": user_id},
    )
    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found")

    return workspace


async def get_workspace_public_id(
    db: AsyncSession,
    user_id: int,
    internal_id: Optional[int],
) -> str:
    """
    The public uuid for an internal workspace id, or ``""`` when there is none.

    The counterpart of :func:`app.services.data_agents.data_agent_service.
    get_agent_public_id`, and for the same reason: a caller holding a foreign key can
    preselect the workspace in a form without a bigint id reaching the template.
    Returns ``""`` rather than raising, because the caller is rendering a dropdown.
    """
    if not internal_id:
        return ""

    workspace = await workspace_crud.get_one(
        db, filters={"id": internal_id, "user_id": user_id},
    )

    return str(workspace.uuid) if workspace else ""


async def get_workspace_choices(db: AsyncSession, user_id: int) -> List[dict]:
    """
    The user's workspaces as {uuid, name, is_active} for the Data Agents form's
    dropdown. Archived ones are marked but still listed, so an agent already in one
    can be edited without being silently moved out of it.
    """
    workspaces = await workspace_crud.get_many(
        db, filters={"user_id": user_id}, order_by="name",
    )
    return [
        {
            "uuid": str(workspace.uuid),
            "name": workspace.name,
            "is_active": workspace.is_active,
        }
        for workspace in workspaces
    ]


# --------------------------------------------------------------------------
# Write
# --------------------------------------------------------------------------

async def create_workspace(
    db: AsyncSession,
    user_id: int,
    name: str,
    description: Optional[str] = None,
) -> Workspace:
    name = require_text(name, "Workspace name", _NAME_MAX)
    description = optional_text(description, "Description", _DESCRIPTION_MAX)

    await _assert_name_available(db, user_id, name)

    try:
        return await workspace_crud.create(db, {
            "user_id": user_id,
            "name": name,
            "description": description,
            "is_active": True,
        })
    except IntegrityError as exc:
        await _fail_on_duplicate_name(db, name, exc)


async def update_workspace(
    db: AsyncSession,
    user_id: int,
    workspace_id: uuid.UUID,
    name: str,
    description: Optional[str] = None,
) -> Workspace:
    workspace = await get_workspace(db, user_id, workspace_id)

    name = require_text(name, "Workspace name", _NAME_MAX)
    description = optional_text(description, "Description", _DESCRIPTION_MAX)

    await _assert_name_available(db, user_id, name, exclude_id=workspace.id)

    try:
        return await workspace_crud.update(db, workspace.id, {
            "name": name,
            "description": description,
        })
    except IntegrityError as exc:
        await _fail_on_duplicate_name(db, name, exc)


async def set_workspace_active(
    db: AsyncSession,
    user_id: int,
    workspace_id: uuid.UUID,
    is_active: bool,
) -> Workspace:
    """
    Archive or restore a workspace.

    The agents assigned to it are untouched and keep working — archiving only stops
    *new* assignments (see data_agent_service), so a workspace can be parked and
    picked back up instead of rebuilt.
    """
    workspace = await get_workspace(db, user_id, workspace_id)
    return await workspace_crud.update(db, workspace.id, {"is_active": is_active})


async def delete_workspace(
    db: AsyncSession,
    user_id: int,
    workspace_id: uuid.UUID,
) -> None:
    """
    Delete a workspace. Any agents assigned to it survive as unassigned (the FK is
    ON DELETE SET NULL) — the list page's confirm says so.
    """
    workspace = await get_workspace(db, user_id, workspace_id)  # ownership check
    await workspace_crud.delete(db, workspace.id)


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------

async def _assert_name_available(
    db: AsyncSession,
    user_id: int,
    name: str,
    exclude_id: Optional[int] = None,
) -> None:
    if await workspace_name_exists(db, user_id, name, exclude_id=exclude_id):
        raise HTTPException(
            status_code=400,
            detail=f"You already have a workspace named '{name}'",
        )


async def _fail_on_duplicate_name(
    db: AsyncSession,
    name: str,
    exc: IntegrityError,
) -> NoReturn:
    """
    Backstop for the race between _assert_name_available and the write, where
    uq_workspace_user_name_lower is what catches it.

    The rollback matters: the failed flush leaves the session unusable, and the
    HTMX route goes on to re-render the workspace table in that same session, so
    without it the user would get a 500 instead of the message below.
    """
    await db.rollback()
    raise HTTPException(
        status_code=400,
        detail=f"You already have a workspace named '{name}'",
    ) from exc
