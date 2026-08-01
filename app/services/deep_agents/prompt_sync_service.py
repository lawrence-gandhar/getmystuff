"""
Keeping an agent's generated tool-routing prompt in step with its tools.

Attaching, editing, enabling, disabling or deleting a tool config changes what the
agent can do, so the prompt describing its tools has to change too. That work does
not belong in the request: the operator has just saved a form and should get their
table back, not wait on a prompt rebuild.

:func:`sync_tool_routing_prompt` is therefore run as a Litestar ``BackgroundTask``
from the Tool Configs routes — after the response has been sent. It opens its own
session, because the request's session is closed by then.

It is deliberately *not* the only thing that keeps the prompt correct.
``deep_agent_service`` compares ``tool_prompt_synced_at`` against the newest tool
config and regenerates inline if it is behind. That makes this job an optimisation:
if it fails, is interrupted by a restart, or never runs at all, the next answer is
still built from the current tools. Which is why no queue table, scheduler or
retry logic is needed for it.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.db_sessions import AsyncSessionLocal
from app.db.db_utils import CRUDQueryBuilder
from app.db.tool_configs.queries import fetch_enabled_tools_for_agent
from app.models.data_agents import DataAgent
from app.services.deep_agents.prompt_builder import build_tool_routing_prompt

logger = logging.getLogger(__name__)

agent_crud = CRUDQueryBuilder(DataAgent)


async def collect_agent_tools(db: AsyncSession, data_agent_id: int) -> List[dict]:
    """
    One agent's enabled tools, shaped for both the prompt builder and the tool
    factory.

    A single shape for both consumers is what guarantees the prompt and the
    callable tools describe the same set — the failure this avoids is an agent
    being told about a tool it cannot call, or handed one the prompt never
    mentioned.

    ``datasource`` is the ORM row itself, not a view: the tool factory needs its
    encrypted password to build a connection. Nothing in this list is ever sent to
    a model except the fields prompt_builder explicitly formats.
    """
    rows = await fetch_enabled_tools_for_agent(db, data_agent_id)

    return [
        {
            "uuid": str(tool_config.uuid),
            "tool_name": tool_config.tool_name,
            "description": tool_config.description,
            "table_name": tool_config.table_name,
            "config": dict(tool_config.config or {}),
            "updated_at": tool_config.updated_at,
            "datasource": datasource,
            "datasource_name": datasource.datasource_name,
            "db_type": datasource.db_type,
        }
        for tool_config, datasource in rows
    ]


def newest_tool_change(tools: List[Dict[str, Any]]) -> Optional[datetime]:
    """The most recent ``updated_at`` across the agent's tools, or None."""
    timestamps = [tool.get("updated_at") for tool in tools if tool.get("updated_at")]
    return max(timestamps) if timestamps else None


def is_prompt_stale(agent: DataAgent, tools: List[Dict[str, Any]]) -> bool:
    """
    Whether the stored routing prompt is behind the agent's tools.

    Never synced is stale. Otherwise the check is against the newest tool change,
    with a tools-but-no-prompt case caught explicitly: an agent whose tools were
    all deleted has no newest change, and its prompt (which still lists them) is
    stale precisely because there is nothing left to compare against.
    """
    synced_at = getattr(agent, "tool_prompt_synced_at", None)

    if synced_at is None:
        return True

    latest = newest_tool_change(tools)

    if latest is None:
        # No tools. The prompt is correct only if it is already the no-tools one.
        return bool(agent.tool_routing_prompt) and "NO data tools" not in (
            agent.tool_routing_prompt or ""
        )

    return _as_aware(synced_at) < _as_aware(latest)


def _as_aware(value: datetime) -> datetime:
    """
    Compare timestamps safely.

    Both columns are ``timezone=True``, but a value that has been round-tripped
    through SQLite (used by the datasource layer, and possible in tests) comes back
    naive, and comparing naive to aware raises. Assuming UTC for a naive value is
    right here: everything is written by ``func.now()`` on the same database.
    """
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def build_prompt_for_agent(
    db: AsyncSession,
    agent: DataAgent,
) -> tuple[str, List[dict]]:
    """
    Generate the routing prompt for an already-resolved agent, and return it with
    the tools it was built from.

    Returning both is what lets the caller build the callable tools from the exact
    same list, without a second query.
    """
    tools = await collect_agent_tools(db, agent.id)
    return build_tool_routing_prompt(agent.name, tools), tools


async def store_tool_routing_prompt(
    db: AsyncSession,
    agent: DataAgent,
    prompt: str,
) -> None:
    """
    Persist a generated prompt and stamp the sync time.

    Writes only the two generated columns. ``system_prompt`` is not in the dict and
    must never be: it belongs to the operator.
    """
    await agent_crud.update(db, agent.id, {
        "tool_routing_prompt": prompt,
        "tool_prompt_synced_at": datetime.now(timezone.utc),
    })


async def sync_tool_routing_prompt(data_agent_id: Optional[int]) -> None:
    """
    Regenerate and store one agent's routing prompt. The background entry point.

    Takes the internal bigint id rather than a uuid because the caller is a route
    that has already resolved the agent — and because this runs detached from the
    request, with no user to scope an ownership check against.

    Swallows every exception by design. This runs after the response has been sent,
    so there is nothing left to report an error to, and an unhandled failure in a
    background task would otherwise surface as a bare traceback in the server log
    with no indication of which agent it concerned. The staleness fallback in
    deep_agent_service is what makes that safe.
    """
    if not data_agent_id:
        return

    try:
        async with AsyncSessionLocal() as db:
            agent = await agent_crud.get_one(db, filters={"id": data_agent_id})

            if not agent:
                # Deleted between the response and this task — nothing to sync.
                return

            prompt, tools = await build_prompt_for_agent(db, agent)
            await store_tool_routing_prompt(db, agent, prompt)

            logger.info(
                "Synced tool routing prompt for data agent %s (%d tool(s))",
                agent.uuid,
                len(tools),
            )
    except Exception:
        logger.exception(
            "Failed to sync tool routing prompt for data agent id=%s. The prompt "
            "will be rebuilt on the agent's next answer.",
            data_agent_id,
        )
