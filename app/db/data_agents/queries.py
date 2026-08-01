"""
Data Agents data access that the generic CRUDQueryBuilder cannot express: the list
row joined to its workspace and AI key plus a tool-config count, and a
case-insensitive name lookup.

Mirrors app/db/flow_builder/queries.py — a module's raw-SQL exceptions live in its
own ``db`` subpackage rather than leaking into the service layer or polluting the
shared, model-agnostic app/db/db_utils.py.
"""

import uuid
from typing import List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai_settings import AIApiKey
from app.models.data_agents import DataAgent
from app.models.tool_configs import ToolConfig
from app.models.workspaces import Workspace


async def fetch_agents_with_details(
    db: AsyncSession,
    user_id: int,
    workspace_id: Optional[int] = None,
) -> List[Tuple[DataAgent, int, Optional[str], Optional[uuid.UUID], Optional[str]]]:
    """
    Every data agent this user owns — optionally only those in one workspace —
    paired with its tool-config count, its workspace (name and public uuid, so the
    list can link to it) and its AI key label. The workspace and key parts are
    ``None`` when unassigned.

    Both name joins are many-to-one so they cannot fan the rows out; the tool count
    stays accurate alongside them.
    """
    query = (
        select(
            DataAgent,
            func.count(ToolConfig.id),
            Workspace.name,
            Workspace.uuid,
            AIApiKey.label,
        )
        .outerjoin(ToolConfig, ToolConfig.data_agent_id == DataAgent.id)
        .outerjoin(Workspace, Workspace.id == DataAgent.workspace_id)
        .outerjoin(AIApiKey, AIApiKey.id == DataAgent.llm_api_key_id)
        .where(DataAgent.user_id == user_id)
        .group_by(DataAgent.id, Workspace.name, Workspace.uuid, AIApiKey.label)
        .order_by(DataAgent.created_at.desc())
    )

    if workspace_id is not None:
        query = query.where(DataAgent.workspace_id == workspace_id)

    result = await db.execute(query)
    return [
        (agent, tool_count, workspace_name, workspace_uuid, key_label)
        for agent, tool_count, workspace_name, workspace_uuid, key_label in result.all()
    ]


async def data_agent_name_exists(
    db: AsyncSession,
    user_id: int,
    name: str,
    exclude_id: Optional[int] = None,
) -> bool:
    """
    True when this user already has an agent with that name (any casing).

    Backs a friendly message before the insert; uq_data_agent_user_name_lower is
    still the real guarantee (see the IntegrityError handler in
    data_agent_service, which covers the race between this check and the write).
    """
    query = select(DataAgent.id).where(
        DataAgent.user_id == user_id,
        func.lower(DataAgent.name) == name.strip().lower(),
    )
    if exclude_id is not None:
        query = query.where(DataAgent.id != exclude_id)

    result = await db.execute(query.limit(1))
    return result.scalar_one_or_none() is not None
