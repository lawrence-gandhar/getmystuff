"""
Workspaces data access that the generic CRUDQueryBuilder cannot express: an
aggregate count joined onto the parent row, and a case-insensitive name lookup
(``lower(name) = …``, where CRUDQueryBuilder only does plain column equality).

This mirrors app/db/flow_builder/queries.py — a module's raw-SQL exceptions live
in its own ``db`` subpackage instead of leaking into the service layer or
polluting the shared, model-agnostic app/db/db_utils.py.
"""

from typing import List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.data_agents import DataAgent
from app.models.workspaces import Workspace


async def fetch_workspaces_with_agent_counts(
    db: AsyncSession, user_id: int
) -> List[Tuple[Workspace, int]]:
    """
    Every workspace this user owns, paired with how many data agents are assigned
    to it — the list page shows that column, and an outer join beats one extra
    query per row.
    """
    result = await db.execute(
        select(Workspace, func.count(DataAgent.id))
        .outerjoin(DataAgent, DataAgent.workspace_id == Workspace.id)
        .where(Workspace.user_id == user_id)
        .group_by(Workspace.id)
        .order_by(Workspace.created_at.desc())
    )
    return [(workspace, agent_count) for workspace, agent_count in result.all()]


async def workspace_name_exists(
    db: AsyncSession,
    user_id: int,
    name: str,
    exclude_id: Optional[int] = None,
) -> bool:
    """
    True when this user already has a workspace with that name (any casing).

    Backs a friendly "that name is taken" message *before* the insert;
    uq_workspace_user_name_lower is still the real guarantee (see the
    IntegrityError handler in workspace_service, which covers the race between
    this check and the write).
    """
    query = select(Workspace.id).where(
        Workspace.user_id == user_id,
        func.lower(Workspace.name) == name.strip().lower(),
    )
    if exclude_id is not None:
        query = query.where(Workspace.id != exclude_id)

    result = await db.execute(query.limit(1))
    return result.scalar_one_or_none() is not None
