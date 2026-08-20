"""
Data access for nested tool configs — the ``tool_config_links`` edge, read in the
three directions the feature needs.

A separate module from ``queries.py`` for the reason that file gives for existing
at all: these are the queries the generic ``CRUDQueryBuilder`` cannot express, and
the edge is a different subject from the tool row. Everything here returns rows
already joined to what the caller will need next — the child's ``ToolConfig`` and
its ``DataSource`` — because every caller walks a tree and a per-node round trip
would turn one nested tool into a dozen queries.

None of these functions is user-scoped. Ownership on this feature is settled where
it always is: the caller has already resolved the parent through
``tool_config_service.get_tool_config``, which goes tool → agent → user, and a link
can only be created between two tools of the same owner (see
``tool_chain_service.replace_child_links``).
"""

from typing import TYPE_CHECKING, Iterable, List, Sequence, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.datasource import DataSource
from app.models.tool_configs import ToolConfig, ToolConfigLink

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.models.graph_designer import ToolGraph


async def fetch_child_links(
    db: AsyncSession,
    parent_ids: Sequence[int],
) -> List[Tuple[ToolConfigLink, ToolConfig, DataSource]]:
    """
    Every **tool-config** link hanging off these parents, with the child tool and its
    datasource, in the order the children run.

    Takes a list of parents rather than one so a whole tree can be resolved level
    by level: the chain builder starts with the root, collects that level's
    children, and asks again for all of them at once.

    Graph children are **not** returned here — see :func:`fetch_child_graph_links`. The
    inner join on ``child_id`` excludes them for free, which is why this function needed
    no ``WHERE child_id IS NOT NULL``: a graph link has no tool to join to. That is worth
    knowing rather than relying on, because the two callers that walk levels
    (``build_chains``, ``descendant_rows``) mean different things by "the children" —
    one wants everything that runs, the other only the tool configs an agent inherits.
    """
    ids = [int(parent_id) for parent_id in parent_ids or [] if parent_id]

    if not ids:
        return []

    query = (
        select(ToolConfigLink, ToolConfig, DataSource)
        .join(ToolConfig, ToolConfig.id == ToolConfigLink.child_id)
        .join(DataSource, DataSource.id == ToolConfig.datasource_id)
        .where(ToolConfigLink.parent_id.in_(ids))
        .order_by(ToolConfigLink.parent_id, ToolConfigLink.position, ToolConfigLink.id)
    )

    result = await db.execute(query)

    return [(link, tool, datasource) for link, tool, datasource in result.all()]


async def fetch_child_graph_links(
    db: AsyncSession,
    parent_ids: Sequence[int],
) -> List[Tuple[ToolConfigLink, "ToolGraph"]]:
    """
    Every **graph** link hanging off these parents, with the graph, in run order.

    The sibling of :func:`fetch_child_links` rather than a branch inside it, because the
    two return different shapes — a graph has no datasource of its own; its nodes each
    name theirs — and a function returning one of two tuple shapes would push that
    decision onto every caller.

    Published or not. ``is_active`` is checked when the link is *saved*, where the
    message can name the graph, and re-checking here would make a graph parked mid-edit
    silently drop a parent's filter — the one failure this whole module is designed
    against. A run against an unpublished graph fails loudly instead.
    """
    from app.models.graph_designer import ToolGraph

    ids = [int(parent_id) for parent_id in parent_ids or [] if parent_id]

    if not ids:
        return []

    query = (
        select(ToolConfigLink, ToolGraph)
        .join(ToolGraph, ToolGraph.id == ToolConfigLink.child_graph_id)
        .where(ToolConfigLink.parent_id.in_(ids))
        .order_by(ToolConfigLink.parent_id, ToolConfigLink.position, ToolConfigLink.id)
    )

    result = await db.execute(query)

    return [(link, graph) for link, graph in result.all()]


async def fetch_graph_parent_links(
    db: AsyncSession,
    graph_ids: Sequence[int],
) -> List[Tuple[ToolConfigLink, ToolConfig]]:
    """
    Every link *into* these graphs, with the tool config that embeds each one.

    The graph counterpart of :func:`fetch_parent_links`, and it exists for the same
    caller: the guard that refuses to delete or unpublish something another thing
    depends on. A graph deleted under a live parent would drop that parent's filter and
    widen its results silently.
    """
    ids = [int(graph_id) for graph_id in graph_ids or [] if graph_id]

    if not ids:
        return []

    query = (
        select(ToolConfigLink, ToolConfig)
        .join(ToolConfig, ToolConfig.id == ToolConfigLink.parent_id)
        .where(ToolConfigLink.child_graph_id.in_(ids))
        .order_by(ToolConfig.tool_name)
    )

    result = await db.execute(query)

    return [(link, parent) for link, parent in result.all()]


async def fetch_parent_links(
    db: AsyncSession,
    child_ids: Sequence[int],
) -> List[Tuple[ToolConfigLink, ToolConfig]]:
    """
    Every link *into* these tools, with the parent that embeds each one.

    Two callers, both of which need the parent's name rather than its id: the
    delete/disable guard, which refuses and says which tool is in the way, and the
    list page's "embedded in" badge.
    """
    ids = [int(child_id) for child_id in child_ids or [] if child_id]

    if not ids:
        return []

    query = (
        select(ToolConfigLink, ToolConfig)
        .join(ToolConfig, ToolConfig.id == ToolConfigLink.parent_id)
        .where(ToolConfigLink.child_id.in_(ids))
        .order_by(ToolConfig.tool_name)
    )

    result = await db.execute(query)

    return [(link, parent) for link, parent in result.all()]


async def fetch_links_for_tools(
    db: AsyncSession,
    tool_ids: Iterable[int],
) -> List[ToolConfigLink]:
    """
    Every link among a set of tools, in either direction.

    The list page's one query: it already holds every tool the user owns, so it
    reads the whole edge set once and assembles the trees in memory rather than
    walking the database per row.
    """
    ids = [int(tool_id) for tool_id in tool_ids or [] if tool_id]

    if not ids:
        return []

    query = (
        select(ToolConfigLink)
        .where(ToolConfigLink.parent_id.in_(ids))
        .order_by(ToolConfigLink.parent_id, ToolConfigLink.position, ToolConfigLink.id)
    )

    result = await db.execute(query)

    return list(result.scalars().all())


async def fetch_tools_by_ids(
    db: AsyncSession,
    tool_ids: Sequence[int],
) -> List[Tuple[ToolConfig, DataSource]]:
    """
    Specific tools with their datasources, for a chain rooted at a tool that is not
    saved yet — the **Test Query** path, where the children exist but their parent
    is still a form.
    """
    ids = [int(tool_id) for tool_id in tool_ids or [] if tool_id]

    if not ids:
        return []

    query = (
        select(ToolConfig, DataSource)
        .join(DataSource, DataSource.id == ToolConfig.datasource_id)
        .where(ToolConfig.id.in_(ids))
    )

    result = await db.execute(query)

    return [(tool, datasource) for tool, datasource in result.all()]


async def delete_links_for_parent(db: AsyncSession, parent_id: int) -> None:
    """
    Remove every link a parent owns, without committing.

    Saving a tool replaces its children wholesale rather than diffing them: the
    form posts the complete list, the same way it posts the complete query config,
    and a diff would only add a way for the two to disagree.
    """
    links = await db.execute(
        select(ToolConfigLink).where(ToolConfigLink.parent_id == int(parent_id))
    )

    for link in links.scalars().all():
        await db.delete(link)
