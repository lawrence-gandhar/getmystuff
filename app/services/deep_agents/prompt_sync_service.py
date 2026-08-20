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
from app.services.deep_agents.prompt_builder import (
    build_tool_routing_prompt,
    has_current_rules,
)
from app.services.tool_configs import tool_chain_service
from app.services.tool_configs.tool_config_service import tables_read

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

    **The set is the agent's own tools plus everything they embed.** Giving an
    agent a nested tool gives it the whole chain: the children run as part of the
    parent whether or not they are listed, and listing them means the agent can also
    call one on its own — which is the point of a child being a tool rather than a
    sub-query buried in someone else's SQL. Their rows are not modified, so a shared
    child stays where it belongs and no other agent loses it.

    ``datasource`` is the ORM row itself, not a view: the tool factory needs its
    encrypted password to build a connection. Nothing in this list is ever sent to
    a model except the fields prompt_builder explicitly formats.

    **Published graphs appear in this list too**, as entries marked ``kind: "graph"`` —
    see :func:`_graph_entries`. They are in *this* list rather than a parallel one
    precisely because of the guarantee in the paragraph above: two lists could describe
    different sets, and then the agent would be told about something it cannot call, or
    handed something the prompt never mentioned. Both routes to a graph arrive here the
    same way: one attached to this agent, and any shared with its workspace.
    """
    rows = await fetch_enabled_tools_for_agent(db, data_agent_id)

    # Descendants come after the agent's own tools, so the prompt reads as "what
    # you were given, then what came with it" and an unchanged set still produces a
    # byte-identical prompt.
    inherited = await tool_chain_service.descendant_rows(
        db, [tool_config.id for tool_config, _datasource in rows],
    )
    rows = list(rows) + [
        (tool_config, datasource)
        for tool_config, datasource in inherited
        if tool_config.is_enabled
    ]

    chains = await tool_chain_service.build_chains(db, rows)

    entries = [
        {
            "uuid": str(tool_config.uuid),
            # The internal bigint ids. Present because an export has to record which
            # tool and which agent it came from, and a foreign key takes the id — see
            # app/services/downloader_agents/base/download_service.create_offer. They
            # never reach a model: prompt_builder formats named fields only, and the
            # tool factory passes them to the database rather than into a prompt.
            "id": tool_config.id,
            "data_agent_id": tool_config.data_agent_id,
            "tool_name": tool_config.tool_name,
            "description": tool_config.description,
            "table_name": tool_config.table_name,
            # Every table the tool reads, primary first — the prompt names them all,
            # and the executor checks each one is still switched on in Data Sources.
            "table_names": tables_read(
                tool_config.table_name, tool_config.extra_tables,
            ),
            "query_mode": tool_config.query_mode,
            # Whether this tool's whole result set may be read and grouped in
            # memory. Read by the aggregate tool factory to decide whether to bind
            # anything at all, and by prompt_builder to decide whether to mention
            # it — so with every tool opted out, both are unchanged from before the
            # capability existed.
            "allow_recursive_aggregate": bool(
                getattr(tool_config, "allow_recursive_aggregate", False),
            ),
            "config": dict(tool_config.config or {}),
            # Non-empty only for a SQL-mode tool. Both consumers need it: the
            # factory runs it, and the prompt quotes it as the query the tool runs.
            "sql_query": tool_config.sql_query,
            # The values a SQL-mode statement asks the assistant for. Non-empty only
            # in that mode — builder mode's equivalent lives inside `config` — and
            # needed by both consumers for the same reason as everything else here:
            # the factory turns them into the tool's arguments, and the prompt says
            # what the tool needs to be told.
            "sql_params": list(getattr(tool_config, "sql_params", None) or []),
            "updated_at": tool_config.updated_at,
            "datasource": datasource,
            "datasource_name": datasource.datasource_name,
            "db_type": datasource.db_type,
            # The resolved tree this tool is the root of. Both consumers need it and
            # for the same reason as everything else here: the factory runs the
            # chain, and the prompt says what restricts the tool, so a tool cannot
            # be described as unrestricted and then run restricted.
            "chain": chains.get(tool_config.id),
        }
        for tool_config, datasource in rows
    ]

    # Appended after the tool configs, so an agent with no graphs produces a
    # byte-identical prompt to the one it produced before graphs existed.
    entries.extend(await _graph_entries(db, data_agent_id))

    return entries


async def _graph_entries(db: AsyncSession, data_agent_id: int) -> List[dict]:
    """
    Every graph this agent may call, in the same shape a tool config takes.

    Two ways in, and ``fetch_agent_graphs`` is the one place that knows both: a graph
    **attached** to this agent, and any graph **shared with the workspace** the agent is
    assigned to. Both require ``is_active``, which is what lets a graph be parked
    mid-edit without being detached or un-shared.

    A list rather than the single graph this returned before workspace sharing existed.
    Nothing downstream needed changing for that: ``build_graph_tools`` already takes one
    entry and is called per entry, and each graph's answering tool is named after the
    graph, so an agent holding three has three unambiguous ones. What *is* checked
    elsewhere is that two of them cannot derive the same tool name — see
    ``graph_service._require_unique_graph_tool_name``, because a model offered two tools
    of one name cannot choose between them.

    ``updated_at`` is included for a reason worth stating: ``is_prompt_stale`` compares the
    agent's stored prompt against the newest ``updated_at`` in this list, so putting the
    graph's timestamp here is what makes **editing a graph invalidate the routing prompt**.
    Without it the prompt would keep describing the graph as it was, and no new staleness
    path had to be written for that to work. Sharing a graph with a workspace invalidates
    the prompt of every agent in it through the same route.

    ``asks_questions`` is derived here rather than in the tool factory because it is a fact
    about the drawing, and this is the only layer holding the drawing.

    Imported inside the function: ``graph_designer`` reads ``query_executor`` from this
    package, so a module-scope import would be a cycle. The same lazy-import call
    ``aggregate_service`` and ``query_test_service`` already make in the other direction.
    """
    from app.db.graph_designer.queries import fetch_agent_graphs

    graphs = await fetch_agent_graphs(db, data_agent_id)

    return [_graph_entry(graph, data_agent_id) for graph in graphs]


def _graph_entry(graph, data_agent_id: int) -> dict:  # noqa: ANN001
    """One graph as a tool entry."""
    from app.models.graph_designer import NODE_HUMAN, NODE_SQL, NODE_SQL_UNION

    nodes = [
        node for node in (graph.graph_data or {}).get("nodes") or []
        if isinstance(node, dict)
    ]

    # Every parameter every statement node declares, de-duplicated by name. These become
    # the graph tool's arguments, so a value the operator opened on any node is one the model
    # can fill — and one it did not open has nowhere to land.
    #
    # `sql_union` counts as well as `sql`. Its parameters are usually filled by the loop it
    # sits in, but one it declares and does not wire is filled from the run's inputs exactly
    # as a SQL node's is — so omitting it would make that parameter reachable from the test
    # panel and unreachable from a conversation, which is the sort of difference nobody would
    # think to look for.
    statement_nodes = {NODE_SQL, NODE_SQL_UNION}
    declared: Dict[str, dict] = {}

    for node in nodes:
        if str(node.get("type")) not in statement_nodes:
            continue

        for param in (node.get("data") or {}).get("params") or []:
            name = str((param or {}).get("param") or "").strip()
            if name and name not in declared:
                declared[name] = dict(param)

    return {
        "kind": "graph",
        "graph_uuid": str(graph.uuid),
        # Whose graph it is, which is **not** necessarily whoever owns the agent calling
        # it: a graph shared with a workspace is run as its author, because the
        # datasources its nodes read are scoped to that author. The runner takes this
        # value and no other.
        "user_id": graph.user_id,
        "id": graph.id,
        # The agent this entry was collected for, not an owner. A shared graph has no
        # `data_agent_id` of its own, and the exporter needs to know which agent's turn
        # produced a result.
        "data_agent_id": data_agent_id,
        # Deliberately nothing recording *how* the graph got here. Whether it was
        # attached or inherited from a workspace changes nothing about calling it, and a
        # clause in the prompt saying so would be noise a model might act on.
        "tool_name": _graph_tool_name(graph.name),
        "description": graph.description,
        "node_count": len(nodes),
        "asks_questions": any(
            str(node.get("type")) == NODE_HUMAN for node in nodes
        ),
        "sql_params": list(declared.values()),
        # Whether this graph's whole result may be read and filtered in polars. Under the
        # **same key** a tool config uses, so `aggregate_service.readable_tools` filters
        # both kinds with one expression — a second key would be a second thing to
        # remember, and the one that got forgotten would silently opt nothing in.
        "allow_recursive_aggregate": bool(
            getattr(graph, "allow_recursive_aggregate", False),
        ),
        "updated_at": graph.updated_at,
    }


def _graph_tool_name(name: Optional[str]) -> str:
    """
    A graph's name as an identifier a model can call.

    A graph is named by a person — "Monthly revenue check" — and a tool name has to be a
    single token. Lowercased, non-word characters collapsed to underscores, and prefixed
    when it would otherwise start with a digit, because a name a model cannot address is a
    tool it cannot use.
    """
    cleaned = "".join(
        character if character.isalnum() else "_"
        for character in str(name or "").strip().lower()
    ).strip("_")

    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")

    if not cleaned:
        return "saved_graph"

    return cleaned if cleaned[0].isalpha() else f"graph_{cleaned}"


def newest_tool_change(tools: List[Dict[str, Any]]) -> Optional[datetime]:
    """The most recent ``updated_at`` across the agent's tools, or None."""
    timestamps = [tool.get("updated_at") for tool in tools if tool.get("updated_at")]
    return max(timestamps) if timestamps else None


def is_prompt_stale(agent: DataAgent, tools: List[Dict[str, Any]]) -> bool:
    """
    Whether the stored routing prompt is behind the agent's tools *or* behind the
    grounding rules this build generates.

    Never synced is stale. Otherwise the check is against the newest tool change,
    with a tools-but-no-prompt case caught explicitly: an agent whose tools were
    all deleted has no newest change, and its prompt (which still lists them) is
    stale precisely because there is nothing left to compare against.

    The rules check is the second half, and it exists because a timestamp cannot see
    it. Half of this prompt comes from the agent's tools and half from
    ``prompt_builder``'s standing rules, and only the first half moves when a tool is
    saved — so a rule corrected in code stayed unused by every agent already in the
    database until one of its tools happened to be re-saved. A prompt built before
    the marker existed has no marker and is stale for the same reason.
    """
    synced_at = getattr(agent, "tool_prompt_synced_at", None)

    if synced_at is None:
        return True

    if not has_current_rules(agent.tool_routing_prompt):
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
