"""
Tool Configs data access that the generic CRUDQueryBuilder cannot express: the
list row joined to its agent and datasource, ownership scoped through the agent,
and a case-insensitive name lookup.

Mirrors app/db/flow_builder/queries.py — a module's raw-SQL exceptions live in its
own ``db`` subpackage rather than leaking into the service layer or polluting the
shared, model-agnostic app/db/db_utils.py.
"""

from typing import List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.data_agents import DataAgent
from app.models.datasource import DataSource
from app.models.tool_configs import ToolConfig


async def fetch_tool_configs_with_details(
    db: AsyncSession,
    user_id: int,
    data_agent_id: Optional[int] = None,
) -> List[Tuple[ToolConfig, DataAgent, DataSource]]:
    """
    Every tool config the user owns — optionally only one agent's — paired with
    that agent and the datasource it reads.

    ``tool_configs`` carries no ``user_id``: ownership comes from its agent, which
    is why the join to ``data_agents`` is what scopes this list.
    """
    query = (
        select(ToolConfig, DataAgent, DataSource)
        .join(DataAgent, DataAgent.id == ToolConfig.data_agent_id)
        .join(DataSource, DataSource.id == ToolConfig.datasource_id)
        .where(DataAgent.user_id == user_id)
        .order_by(ToolConfig.created_at.desc())
    )

    if data_agent_id is not None:
        query = query.where(ToolConfig.data_agent_id == data_agent_id)

    result = await db.execute(query)
    return [
        (tool_config, agent, datasource)
        for tool_config, agent, datasource in result.all()
    ]


async def fetch_enabled_tools_for_agent(
    db: AsyncSession,
    data_agent_id: int,
) -> List[Tuple[ToolConfig, DataSource]]:
    """
    One agent's *enabled* tool configs with their datasources, oldest first.

    This is what the Deep Agent runtime and the prompt generator both read, which
    is why they cannot disagree about which tools an agent has: the same rows build
    the routing prompt and the callable tools.

    Ordered by ``created_at`` so the prompt lists tools in the order the operator
    created them — a stable order means an unchanged configuration regenerates a
    byte-identical prompt.

    Not scoped to a user: the caller has already resolved the agent through
    ``data_agent_service.get_data_agent``, which is where ownership is enforced.
    Taking ``data_agent_id`` (the internal bigint) rather than a uuid makes that
    ordering explicit — you cannot call this without having resolved the agent first.
    """
    query = (
        select(ToolConfig, DataSource)
        .join(DataSource, DataSource.id == ToolConfig.datasource_id)
        .where(
            ToolConfig.data_agent_id == data_agent_id,
            ToolConfig.is_enabled.is_(True),
        )
        .order_by(ToolConfig.created_at)
    )

    result = await db.execute(query)
    return [(tool_config, datasource) for tool_config, datasource in result.all()]


async def tool_name_exists(
    db: AsyncSession,
    data_agent_id: int,
    tool_name: str,
    exclude_id: Optional[int] = None,
) -> bool:
    """
    True when this agent already has a tool with that name (any casing).

    Backs a friendly message before the insert; uq_tool_config_agent_name_lower is
    still the real guarantee (see the IntegrityError handler in
    tool_config_service, which covers the race between this check and the write).
    """
    query = select(ToolConfig.id).where(
        ToolConfig.data_agent_id == data_agent_id,
        func.lower(ToolConfig.tool_name) == tool_name.strip().lower(),
    )
    if exclude_id is not None:
        query = query.where(ToolConfig.id != exclude_id)

    result = await db.execute(query.limit(1))
    return result.scalar_one_or_none() is not None
