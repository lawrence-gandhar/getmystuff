"""
Business logic for Tool Graphs — the two shapes a tool config already has, drawn.

Nothing here is authored. Every value this module returns is derived from rows that
already exist, so the module owns no model, no table and no write path:

* **The chain graph.** Nested tool configs compile to a LangGraph
  (``app.services.tool_configs.tool_chain_graph``) whose nodes run deepest-first and
  whose conditional edges stop the run the moment a level matches nothing. That
  graph is real and it decides what an agent gets back, but until now the only way
  to see one was the indented text lines in a tool's list row — which cannot show
  that two parents share a child, and cannot show where a disabled tool breaks the
  chain.

* **The join sets.** A builder query joins tables with ``inner``/``left``/``right``/
  ``full`` (app.utils.query_joins), which is set arithmetic written as dropdown
  rows. Each one is a two-circle diagram.

Scope comes from the tree: a tool, an agent, or a workspace. The **descendants of
the scoped tools are always included**, because that is what actually runs — an
agent given one nested tool is given every tool below it too
(``prompt_sync_service.collect_agent_tools``), and a graph that stopped at the
agent's own rows would draw a chain with its lower half missing.

Ownership is settled by the queries this module composes, all of which scope on
``DataAgent.user_id``, and by the three existing resolvers used to turn a public
uuid into a row — each of which raises 404 rather than 403 for a row that belongs to
someone else.

Layout — which layer and which row each node sits on — is computed **here** rather
than in the browser. It is the one part of a drawing that can be wrong in a way a
person would not notice, this repository has no JavaScript test harness, and the
coverage ratchet only measures ``app/``; computing it in Python is what makes it
testable. The JavaScript turns ``(layer, row)`` into pixels and nothing more.
"""

import uuid
from typing import Dict, List, Optional, Sequence, Set, Tuple

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.data_agents.queries import fetch_agents_with_details
from app.db.tool_configs.chain_queries import fetch_links_for_tools
from app.db.tool_configs.queries import fetch_tool_configs_with_details
from app.db.workspaces.queries import fetch_workspaces_with_agent_counts
from app.models.data_agents import DataAgent
from app.models.datasource import DataSource
from app.models.tool_configs import QUERY_MODE_BUILDER, QUERY_MODE_SQL, ToolConfig
from app.models.tool_configs.tool_chain import ToolConfigLink
from app.services.data_agents import data_agent_service
from app.services.tool_configs import tool_config_service
from app.services.workspaces import workspace_service
from app.utils.query_joins import JOIN_TYPE_SQL

# The two ends of every drawn chain. They are nodes in the picture and keys in the
# edge list, but they are not tools, so they cannot collide with a tool's uuid.
START_KEY = "start"
END_KEY = "end"

# Agents that sit in no workspace are real and common — ``workspace_id`` is nullable
# by design (see app.models.data_agents). They get their own group rather than being
# dropped, which is what a tree built only from workspaces would do.
UNASSIGNED_LABEL = "Unassigned"

# Why a SQL-mode tool shows no diagram. Nothing in this application parses joins out
# of a raw statement — app.utils.sql_guard is explicit that its checks are text
# heuristics rather than a parse — so a Venn drawn for one would be a confident
# picture of something nobody verified. Saying so is the honest answer, and it is the
# same one ``tool_chain_service.child_output_columns`` gives about a SQL tool's
# columns.
SQL_MODE_NOTE = (
    "This tool's query is a SQL statement. Its joins are not read from the "
    "statement — only the tables it declares are known here."
)

NO_JOINS_NOTE = "This query reads one table, so there is nothing to intersect."


# --------------------------------------------------------------------------
# The tree
# --------------------------------------------------------------------------

async def get_graph_tree(db: AsyncSession, user_id: int) -> List[dict]:
    """
    Workspaces, the agents inside them, and the tools inside those — the side menu.

    Four queries for the whole tree, none of them per row: the workspaces, the
    agents (which already carry their workspace's uuid), the tools (which already
    carry their agent and datasource), and the whole edge set among those tools.

    A workspace with no agents and an agent with no tools both still appear. An
    empty branch is information — it is how someone notices the agent they just
    created has nothing in it — and hiding it would make the tree disagree with the
    Data Agents page about what exists.
    """
    workspace_rows = await fetch_workspaces_with_agent_counts(db, user_id)
    agent_rows = await fetch_agents_with_details(db, user_id)
    tool_rows = await fetch_tool_configs_with_details(db, user_id)
    links = await fetch_links_for_tools(
        db, [tool.id for tool, _agent, _datasource in tool_rows],
    )

    parents = {link.parent_id for link in links}
    children = {link.child_id for link in links}

    tools_by_agent: Dict[int, List[dict]] = {}
    for tool, agent, datasource in tool_rows:
        tools_by_agent.setdefault(agent.id, []).append({
            "uuid": str(tool.uuid),
            "tool_name": tool.tool_name,
            "query_mode": tool.query_mode or QUERY_MODE_BUILDER,
            "is_enabled": tool.is_enabled,
            "datasource_name": datasource.datasource_name,
            "has_children": tool.id in parents,
            "is_embedded": tool.id in children,
        })

    # Keyed by the workspace's public uuid, with "" standing for no workspace — the
    # same empty-string convention get_agent_views uses for an unassigned agent.
    agents_by_workspace: Dict[str, List[dict]] = {}
    for agent, tool_count, _name, workspace_uuid, _key_label in agent_rows:
        agents_by_workspace.setdefault(
            str(workspace_uuid) if workspace_uuid else "", [],
        ).append({
            "uuid": str(agent.uuid),
            "name": agent.name,
            "is_active": agent.is_active,
            "tool_count": tool_count,
            "tools": tools_by_agent.get(agent.id, []),
        })

    tree = [
        {
            "uuid": str(workspace.uuid),
            "name": workspace.name,
            "is_active": workspace.is_active,
            "agents": agents_by_workspace.get(str(workspace.uuid), []),
        }
        for workspace, _agent_count in workspace_rows
    ]

    unassigned = agents_by_workspace.get("", [])
    if unassigned:
        tree.append({
            "uuid": "",
            "name": UNASSIGNED_LABEL,
            "is_active": True,
            "agents": unassigned,
        })

    return tree


# --------------------------------------------------------------------------
# The chain graph
# --------------------------------------------------------------------------

async def get_chain_graph(
    db: AsyncSession,
    user_id: int,
    *,
    workspace_id: Optional[uuid.UUID] = None,
    agent_id: Optional[uuid.UUID] = None,
    tool_id: Optional[uuid.UUID] = None,
) -> dict:
    """
    The nodes and edges for one selection, laid out.

    Edges run **child → parent**, which is the direction values actually travel and
    the direction ``tool_chain_graph`` compiles: the child runs first and its column
    restricts the parent. ``START`` feeds every tool that embeds nothing, and every
    tool nothing embeds feeds ``END``, so a tool that stands alone draws as
    ``START → tool → END`` rather than as a lone box.

    A tool embedded by two parents is **one** node with two outgoing edges. That is
    the fact this view exists to make visible: the list page necessarily repeats a
    shared child under each parent, so nothing there can show that editing it
    changes two tools.
    """
    scope, rows, links = await _scoped_graph(
        db, user_id,
        workspace_id=workspace_id, agent_id=agent_id, tool_id=tool_id,
    )

    if not rows:
        return {"scope_label": scope, "nodes": [], "edges": []}

    children_of, edges = _edge_index(rows, links)
    layers = _layers(rows, children_of)
    rows_by_key = _rows_of(rows, children_of, layers)
    last_layer = max(layers.values()) + 1

    nodes = [
        {
            "key": START_KEY,
            "kind": "start",
            "label": "START",
            "datasource": "",
            "query_mode": "",
            "is_enabled": True,
            "agent_name": "",
            "layer": 0,
            "row": 0,
        },
    ]

    for tool, agent, datasource in _ordered(rows):
        key = str(tool.uuid)
        nodes.append({
            "key": key,
            "kind": "tool",
            "label": tool.tool_name,
            "datasource": datasource.datasource_name,
            "query_mode": tool.query_mode or QUERY_MODE_BUILDER,
            "is_enabled": tool.is_enabled,
            "agent_name": agent.name,
            "layer": layers[tool.id],
            "row": rows_by_key[tool.id],
        })

    nodes.append({
        "key": END_KEY,
        "kind": "end",
        "label": "END",
        "datasource": "",
        "query_mode": "",
        "is_enabled": True,
        "agent_name": "",
        "layer": last_layer,
        "row": 0,
    })

    return {"scope_label": scope, "nodes": nodes, "edges": edges}


# --------------------------------------------------------------------------
# The join sets
# --------------------------------------------------------------------------

async def get_join_views(
    db: AsyncSession,
    user_id: int,
    *,
    workspace_id: Optional[uuid.UUID] = None,
    agent_id: Optional[uuid.UUID] = None,
    tool_id: Optional[uuid.UUID] = None,
) -> dict:
    """
    Each scoped tool's joins, in the order the query applies them.

    The same tools the chain graph draws, resolved the same way, so flipping the
    view does not silently change which tools are being looked at.

    A join is only reported for a builder query, where ``config["joins"]`` was
    validated field by field on save and is exactly what the executor will run. A
    SQL-mode tool reports its declared tables and :data:`SQL_MODE_NOTE`.
    """
    scope, rows, _links = await _scoped_graph(
        db, user_id,
        workspace_id=workspace_id, agent_id=agent_id, tool_id=tool_id,
    )

    return {
        "scope_label": scope,
        "tools": [_join_view(tool) for tool, _agent, _datasource in _ordered(rows)],
    }


def _join_view(tool: ToolConfig) -> dict:
    """One tool's joins, or the reason it has none to show."""
    tables = tool_config_service.tables_read(tool.table_name, tool.extra_tables)

    if (tool.query_mode or QUERY_MODE_BUILDER) == QUERY_MODE_SQL:
        return {
            "tool_uuid": str(tool.uuid),
            "tool_name": tool.tool_name,
            "query_mode": QUERY_MODE_SQL,
            "base_table": tool.table_name or "",
            "tables": tables,
            "joins": [],
            "note": SQL_MODE_NOTE,
        }

    joins = [
        {
            "type": str(join.get("type") or ""),
            # The SQL keyword, from the one place the join types are defined, so the
            # caption under a diagram reads as the clause it stands for.
            "type_label": JOIN_TYPE_SQL.get(str(join.get("type") or ""), "JOIN"),
            "left_table": str(join.get("left_table") or ""),
            "left_column": str(join.get("left_column") or ""),
            "table": str(join.get("table") or ""),
            "right_column": str(join.get("right_column") or ""),
        }
        for join in (tool.config or {}).get("joins") or []
        if isinstance(join, dict)
    ]

    return {
        "tool_uuid": str(tool.uuid),
        "tool_name": tool.tool_name,
        "query_mode": QUERY_MODE_BUILDER,
        "base_table": tool.table_name or "",
        "tables": tables,
        "joins": joins,
        "note": "" if joins else NO_JOINS_NOTE,
    }


# --------------------------------------------------------------------------
# Scope
# --------------------------------------------------------------------------

async def _scoped_graph(
    db: AsyncSession,
    user_id: int,
    *,
    workspace_id: Optional[uuid.UUID],
    agent_id: Optional[uuid.UUID],
    tool_id: Optional[uuid.UUID],
) -> Tuple[str, List[Tuple[ToolConfig, DataAgent, DataSource]], List[ToolConfigLink]]:
    """
    Resolve a selection to its label, its tools and the edges among them.

    The most specific selector wins, so a page holding all three from a deep link
    behaves the way the tree does — clicking a tool means that tool, whatever is
    expanded above it.

    Every candidate row is read once and the selection is applied in memory. The
    alternative — a query per level — would be several round trips to answer a
    question the four rows already contain, and the whole tree is already loaded to
    render the side menu beside this graph.
    """
    label, seed_ids = await _selection(
        db, user_id,
        workspace_id=workspace_id, agent_id=agent_id, tool_id=tool_id,
    )

    if seed_ids is None:
        return "", [], []

    all_rows = await fetch_tool_configs_with_details(db, user_id)
    all_links = await fetch_links_for_tools(
        db, [tool.id for tool, _agent, _datasource in all_rows],
    )

    wanted = _with_descendants(seed_ids, all_links)
    rows = [row for row in all_rows if row[0].id in wanted]
    links = [
        link for link in all_links
        if link.parent_id in wanted and link.child_id in wanted
    ]

    # A single tool is named with its agent, which is the one thing its own name
    # does not say. Taken from the row already fetched rather than by resolving the
    # agent again.
    if tool_id is not None:
        for tool, agent, _datasource in rows:
            if str(tool.uuid) == str(tool_id):
                label = f"{agent.name} · {tool.tool_name}"
                break

    return label, rows, links


async def _selection(
    db: AsyncSession,
    user_id: int,
    *,
    workspace_id: Optional[uuid.UUID],
    agent_id: Optional[uuid.UUID],
    tool_id: Optional[uuid.UUID],
) -> Tuple[str, Optional[Set[int]]]:
    """
    The selected label and the internal ids it starts from, or ``(…, None)`` for no
    selection at all.

    Each uuid goes through the module that owns it, so a row belonging to someone
    else raises the same 404 it would anywhere else in the application rather than
    quietly drawing an empty canvas — which would read as "this tool has no chain".
    """
    if tool_id is not None:
        tool = await tool_config_service.get_tool_config(db, user_id, tool_id)
        return tool.tool_name, {tool.id}

    if agent_id is not None:
        agent = await data_agent_service.get_data_agent(db, user_id, agent_id)
        rows = await fetch_tool_configs_with_details(db, user_id, agent.id)
        return agent.name, {tool.id for tool, _agent, _datasource in rows}

    if workspace_id is not None:
        workspace = await workspace_service.get_workspace(db, user_id, workspace_id)
        agent_rows = await fetch_agents_with_details(db, user_id, workspace.id)
        agent_ids = {agent.id for agent, *_rest in agent_rows}
        rows = await fetch_tool_configs_with_details(db, user_id)
        return workspace.name, {
            tool.id for tool, agent, _datasource in rows if agent.id in agent_ids
        }

    return "", None


def _with_descendants(seed_ids: Set[int], links: Sequence[ToolConfigLink]) -> Set[int]:
    """
    The seed plus every tool reachable below it.

    A ``visited`` set rather than pure recursion, and a bounded number of passes:
    ``replace_child_links`` refuses a cycle on save, but a page that only *displays*
    must not be the thing that hangs if a row ever reached the table another way.
    """
    children_of: Dict[int, List[int]] = {}
    for link in links:
        children_of.setdefault(link.parent_id, []).append(link.child_id)

    found = set(seed_ids)
    pending = list(seed_ids)

    while pending:
        current = pending.pop()
        for child_id in children_of.get(current, []):
            if child_id not in found:
                found.add(child_id)
                pending.append(child_id)

    return found


# --------------------------------------------------------------------------
# Layout
# --------------------------------------------------------------------------

def _ordered(
    rows: Sequence[Tuple[ToolConfig, DataAgent, DataSource]],
) -> List[Tuple[ToolConfig, DataAgent, DataSource]]:
    """Tools by name, so the same selection always draws the same picture."""
    return sorted(rows, key=lambda row: (row[0].tool_name or "", row[0].id))


def _edge_index(
    rows: Sequence[Tuple[ToolConfig, DataAgent, DataSource]],
    links: Sequence[ToolConfigLink],
) -> Tuple[Dict[int, List[int]], List[dict]]:
    """
    Build the child lists and the drawable edge list in one pass.

    ``START`` attaches to a tool that embeds nothing and ``END`` to a tool nothing
    embeds, both decided from the edges rather than from the links table, so a
    child pulled in from another agent counts as embedded here exactly as it is.
    """
    keys = {tool.id: str(tool.uuid) for tool, _agent, _datasource in rows}

    children_of: Dict[int, List[int]] = {tool_id: [] for tool_id in keys}
    has_parent: Set[int] = set()
    value_edges: List[dict] = []

    for link in links:
        if link.parent_id not in keys or link.child_id not in keys:
            continue

        children_of[link.parent_id].append(link.child_id)
        has_parent.add(link.child_id)
        value_edges.append({
            "source": keys[link.child_id],
            "target": keys[link.parent_id],
            "kind": "value",
            # What crosses this edge: the child column collected, and where in the
            # parent it lands. The whole contract of a nested tool, in one label.
            "label": f"{link.child_column} → {link.parent_reference}",
        })

    start_edges = [
        {"source": START_KEY, "target": key, "kind": "start", "label": ""}
        for tool_id, key in keys.items() if not children_of[tool_id]
    ]
    end_edges = [
        {"source": key, "target": END_KEY, "kind": "end", "label": ""}
        for tool_id, key in keys.items() if tool_id not in has_parent
    ]

    return children_of, start_edges + value_edges + end_edges


def _layers(
    rows: Sequence[Tuple[ToolConfig, DataAgent, DataSource]],
    children_of: Dict[int, List[int]],
) -> Dict[int, int]:
    """
    Each tool's column: one past its deepest child, so every edge points forward.

    Relaxed to a fixed point in at most one pass per node instead of recursing. That
    is the cycle-safe form — a cycle stops the loop at the bound with a usable
    number rather than exhausting the stack — and the node counts here are small
    enough (``MAX_CHAIN_NODES`` is 20) that the difference is not measurable.
    """
    tool_ids = [tool.id for tool, _agent, _datasource in rows]
    layers = dict.fromkeys(tool_ids, 1)

    for _pass in range(len(tool_ids)):
        settled = True

        for tool_id in tool_ids:
            deepest = max(
                (layers[child_id] for child_id in children_of.get(tool_id, [])),
                default=0,
            )
            if deepest + 1 != layers[tool_id]:
                layers[tool_id] = deepest + 1
                settled = False

        if settled:
            break

    return layers


def _rows_of(
    rows: Sequence[Tuple[ToolConfig, DataAgent, DataSource]],
    children_of: Dict[int, List[int]],
    layers: Dict[int, int],
) -> Dict[int, int]:
    """
    Each tool's row: a chain runs along one line, a second branch drops below it.

    A node keeps the row it was first given. That is what makes a shared child draw
    once, on the line of whichever parent reached it first, with the other parent's
    edge angling into it — the alternative, moving it each time, would make the row
    depend on iteration order and the picture jump between reloads.
    """
    ordered = _ordered(rows)
    placed: Dict[int, int] = {}
    next_row = 0

    for root_id in _roots(ordered, children_of):
        if root_id in placed:
            continue

        next_row = _place_subtree(root_id, next_row, placed, children_of, layers) + 1

    # Anything a cycle left unreachable still needs a row of its own.
    for tool, _agent, _datasource in ordered:
        if tool.id not in placed:
            placed[tool.id] = next_row
            next_row += 1

    return placed


def _roots(
    ordered: Sequence[Tuple[ToolConfig, DataAgent, DataSource]],
    children_of: Dict[int, List[int]],
) -> List[int]:
    """
    The tools nothing embeds — where each drawn chain begins on the right.

    A cycle leaves nothing without a parent. Every node becomes a starting point in
    that case, so the page shows the tangle rather than an empty canvas.
    """
    embedded = {child_id for kids in children_of.values() for child_id in kids}
    roots = [tool.id for tool, _agent, _datasource in ordered if tool.id not in embedded]

    return roots or [tool.id for tool, _agent, _datasource in ordered]


def _place_subtree(
    root_id: int,
    first_row: int,
    placed: Dict[int, int],
    children_of: Dict[int, List[int]],
    layers: Dict[int, int],
) -> int:
    """
    Give every unplaced tool below ``root_id`` a row, and report the last one used.

    The first child inherits its parent's row and each further one starts a new
    line, which is what draws a straight chain as a straight line and a fan-out as a
    fan. Deepest child first, so the longest branch is the one that stays on the
    parent's line.
    """
    stack = [(root_id, first_row)]
    used = first_row

    while stack:
        tool_id, row = stack.pop()
        if tool_id in placed:
            continue

        placed[tool_id] = row
        used = max(used, row)

        kids = [
            child_id for child_id in sorted(
                children_of.get(tool_id, []),
                key=lambda child_id: -layers.get(child_id, 0),
            )
            if child_id not in placed
        ]

        for index, child_id in enumerate(kids):
            if index:
                used += 1
            stack.append((child_id, row if not index else used))

    return used
