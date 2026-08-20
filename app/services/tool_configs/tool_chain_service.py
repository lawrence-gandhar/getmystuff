"""
Nested tool configs — the structure half: what may be embedded in what, and what
the resulting tree looks like.

A tool config answers one question with one query. Nesting lets a tool say *"and
restrict that to whatever this other tool returns"*: the child runs first, one
named column of its result becomes a list of values, and the parent's query is
filtered to them. It is a sub-query written as two tools rather than one large
statement, which is the point — the child keeps its own name, its own description
and its own callability, and more than one parent can embed it.

This module owns everything about that relationship **except running it**. It
resolves a tool into a :class:`ChainNode` tree, decides whether a proposed set of
children is allowed, and renders a tree for the list page.
:mod:`app.services.tool_configs.tool_chain_graph` takes the tree from here and
executes it as a LangGraph. The split is deliberate: LangGraph is installed in the
container only, and none of the rules below need it — so every rule in this file
is testable anywhere, and a mistake in a rule fails in a unit test rather than in
an agent's conversation.

**What is refused, and why each refusal exists**

* A cycle, direct or transitive. A chain is evaluated depth-first with no visited
  set at run time, so a cycle would not be a wrong answer, it would be a hang.
* A child on another datasource. Only values cross the boundary, so it *could*
  work across databases — it is refused because the parent's filter is compared
  against a reflected column of its own datasource, and matching an id from one
  system against an id in another is a coincidence, not a join.
* A child owned by someone else. Ownership on this feature runs tool → agent →
  user, exactly as :func:`tool_config_service.get_tool_config` does it.
* A disabled child. ``is_enabled`` is the operator's "stop using this"; a parent
  quietly running it anyway would make the switch a lie.
* Deleting or disabling a tool that something embeds. This is the important one: a
  link that vanished under a live parent would drop that parent's filter and
  **widen its results**, and it would do so silently. It is the same reason
  ``query_executor`` fails a tool loudly rather than dropping a switched-off
  column.

Depth, fan-out and total size are capped because a chain is executed inside an
agent's turn, one database round trip per node, with a visitor waiting.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from litestar.exceptions import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.db_utils import CRUDQueryBuilder
from app.db.tool_configs.chain_queries import (
    delete_links_for_parent,
    fetch_child_graph_links,
    fetch_child_links,
    fetch_graph_parent_links,
    fetch_parent_links,
    fetch_tools_by_ids,
)
from app.db.tool_configs.queries import fetch_tool_configs_with_details
from app.models.data_agents import DataAgent
from app.models.datasource import DataSource
from app.models.tool_configs import (
    BINDING_MODE_EACH,
    BINDING_MODE_IN_LIST,
    BINDING_MODE_VALUES,
    QUERY_MODE_SQL,
    ToolConfig,
    ToolConfigLink,
)
from app.utils.query_joins import query_tables, validated_column_reference
from app.utils.sql_guard import (
    PLACEHOLDER_LIST,
    PLACEHOLDER_SINGLE,
    bind_placeholders,
    placeholder_shape,
)
from app.utils.validators import require_object_name, require_uuid

logger = logging.getLogger(__name__)

tool_config_crud = CRUDQueryBuilder(ToolConfig)
agent_crud = CRUDQueryBuilder(DataAgent)
datasource_crud = CRUDQueryBuilder(DataSource)

#: How many levels a chain may have, the root counted as one. Every level is a
#: round trip to the user's database inside a turn a visitor is waiting on.
MAX_CHAIN_DEPTH = 5

#: How many children one tool may embed. They run in order and the first empty one
#: stops the chain, so more than a handful is a query that wants writing as SQL.
MAX_CHILDREN_PER_TOOL = 5

#: How many tools one chain may involve in total, the root included.
MAX_CHAIN_NODES = 20

#: A bind parameter name in a SQL-mode parent: `:active_clients`.
_BIND_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,49}$")

#: The name a fan-out value is recorded under. The same shape as a query-builder
#: alias (``tool_config_service._ALIAS_PATTERN``), because it becomes a key in the
#: result rows and is grouped by exactly like any other output column.
_VALUE_ALIAS_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


@dataclass
class ChainNode:
    """
    One thing in a resolved chain, with the things it embeds.

    ``child_column``, ``parent_reference``, ``binding_mode`` and ``value_alias``
    describe how this node feeds **its parent**, so all four are empty or default on
    the root — the root feeds nobody, it is what the agent called.

    A node holds **either** a tool config (with its datasource) **or** a graph, matching
    the two shapes of link. ``tool`` and ``graph`` are both optional in the type only:
    exactly one is set on any node that exists, which the CHECK constraint on the row
    guarantees and :attr:`is_graph` is how everything downstream asks which.

    A graph node is always a **leaf and never the root**. Never the root because the
    root is whatever the model called, and what a model calls here is a tool config — a
    graph reached as an agent's own tool goes through ``graph_tool_factory`` instead. A
    leaf because a graph's own composition is drawn inside it: its nodes already read
    tool configs and other queries, so a graph with children in *this* tree would be a
    second, weaker way to express what the canvas expresses properly.
    """

    tool: Optional[ToolConfig] = None
    datasource: Optional[DataSource] = None
    graph: Optional[Any] = None
    child_column: str = ""
    parent_reference: str = ""
    binding_mode: str = BINDING_MODE_IN_LIST
    value_alias: str = ""
    children: List["ChainNode"] = field(default_factory=list)

    @property
    def is_graph(self) -> bool:
        """Whether this node runs a drawn graph rather than one query."""
        return self.graph is not None

    @property
    def key(self) -> str:
        """The node's identity inside a graph run — its own public uuid."""
        return str(self.graph.uuid if self.is_graph else self.tool.uuid)

    @property
    def label(self) -> str:
        """
        What this node is called when something has to be said about it.

        A tool's ``tool_name`` is an identifier a model calls; a graph's ``name`` is a
        sentence a person wrote. Both are what the *operator* would recognise in a
        message, which is what this property is for — it is never used to address
        anything.
        """
        return str(self.graph.name if self.is_graph else self.tool.tool_name)

    @property
    def iterates(self) -> bool:
        """Whether this node's values make its parent run once each."""
        return self.binding_mode == BINDING_MODE_EACH

    @property
    def iterating_child(self) -> Optional["ChainNode"]:
        """
        The one child, if any, whose values this node is run once per.

        ``None`` is the ordinary case. At most one is possible — refused when the
        links are saved (:func:`_require_single_iteration_child`) — so this returns a
        node rather than a list, and a caller never has to decide what two would
        mean.
        """
        return next((child for child in self.children if child.iterates), None)

    def walk(self) -> List["ChainNode"]:
        """Every node in this subtree, deepest first — the order they must run."""
        ordered: List[ChainNode] = []

        for child in self.children:
            ordered.extend(child.walk())

        ordered.append(self)

        return ordered


# --------------------------------------------------------------------------
# Reading the tree
# --------------------------------------------------------------------------

async def build_chains(
    db: AsyncSession,
    roots: Sequence[Tuple[ToolConfig, DataSource]],
) -> Dict[int, ChainNode]:
    """
    Resolve each root into a :class:`ChainNode` tree, keyed by the root's id.

    Loads **one level at a time for every root at once** rather than recursing per
    node: a page listing forty tools resolves every chain in as many queries as the
    deepest chain has levels, not as many as there are nodes.

    A node already on the path from the root is not expanded again. Cycles are
    refused when a link is saved, so this is a backstop and not a feature — but it
    is the difference between a bad row causing a wrong list page and causing a
    hang, and the guard costs one set membership.

    **Graph children are attached at every level but never expanded.** A graph is a leaf
    here (see :class:`ChainNode`) so it contributes no next level and cannot take part in
    a cycle — its own composition is drawn on its canvas, not in this tree.
    """
    trees = {
        tool.id: ChainNode(tool=tool, datasource=datasource)
        for tool, datasource in roots
    }

    level = [(node, {node.tool.id}) for node in trees.values()]
    depth = 1

    while level and depth < MAX_CHAIN_DEPTH:
        parent_ids = [node.tool.id for node, _ in level]
        links = await fetch_child_links(db, parent_ids)
        graph_links = await fetch_child_graph_links(db, parent_ids)

        by_parent: Dict[int, List[Tuple[ToolConfigLink, ToolConfig, DataSource]]] = {}
        for link, tool, datasource in links:
            by_parent.setdefault(link.parent_id, []).append((link, tool, datasource))

        graphs_by_parent: Dict[int, List[Tuple[ToolConfigLink, Any]]] = {}
        for link, graph in graph_links:
            graphs_by_parent.setdefault(link.parent_id, []).append((link, graph))

        next_level: List[Tuple[ChainNode, set]] = []

        for node, seen in level:
            for link, tool, datasource in by_parent.get(node.tool.id, []):
                if tool.id in seen:
                    logger.warning(
                        "Tool chain for %s revisits %s; not expanding further",
                        node.tool.tool_name, tool.tool_name,
                    )
                    continue

                child = ChainNode(
                    tool=tool,
                    datasource=datasource,
                    child_column=link.child_column,
                    parent_reference=link.parent_reference,
                    binding_mode=link.binding_mode or BINDING_MODE_IN_LIST,
                    value_alias=link.value_alias or "",
                )
                node.children.append(child)
                next_level.append((child, seen | {tool.id}))

            for link, graph in graphs_by_parent.get(node.tool.id, []):
                node.children.append(
                    ChainNode(
                        graph=graph,
                        child_column=link.child_column,
                        parent_reference=link.parent_reference,
                        binding_mode=link.binding_mode or BINDING_MODE_IN_LIST,
                        value_alias=link.value_alias or "",
                    ),
                )

        level = next_level
        depth += 1

    return trees


async def chain_for_tool(
    db: AsyncSession,
    tool: ToolConfig,
    datasource: DataSource,
) -> ChainNode:
    """One tool's chain. A tool that embeds nothing is a root with no children."""
    chains = await build_chains(db, [(tool, datasource)])

    return chains[tool.id]


async def chain_from_links(
    db: AsyncSession,
    parent: ToolConfig,
    datasource: DataSource,
    links: Sequence[dict],
) -> ChainNode:
    """
    A chain rooted at a tool that is **not saved yet**, from validated link
    payloads.

    The Test Query path: the children are real, stored tools with chains of their
    own, but the parent is a form. Building the tree from the payloads rather than
    from the database is what lets the button test the nesting the operator is
    looking at instead of the nesting that was last saved.
    """
    root = ChainNode(tool=parent, datasource=datasource)
    rows = await fetch_tools_by_ids(db, [link["child_id"] for link in links])
    subtrees = await build_chains(db, rows)

    for link in links:
        node = subtrees.get(link["child_id"])

        if node is None:
            continue

        node.child_column = link["child_column"]
        node.parent_reference = link["parent_reference"]
        node.binding_mode = link.get("binding_mode") or BINDING_MODE_IN_LIST
        node.value_alias = link.get("value_alias") or ""
        root.children.append(node)

    return root


async def descendant_rows(
    db: AsyncSession,
    tool_ids: Sequence[int],
) -> List[Tuple[ToolConfig, DataSource]]:
    """
    Every tool reachable *below* these tools, with its datasource, de-duplicated
    and excluding the starting set.

    This is what makes "assign the parent to an agent and its children come along"
    true: the agent's runtime tool list is its own tools plus this. The child rows
    are not modified — a child keeps the agent it belongs to, so embedding a shared
    tool never takes it away from another agent.
    """
    seen = {int(tool_id) for tool_id in tool_ids or [] if tool_id}
    frontier = list(seen)
    found: Dict[int, Tuple[ToolConfig, DataSource]] = {}
    depth = 1

    while frontier and depth < MAX_CHAIN_DEPTH:
        links = await fetch_child_links(db, frontier)
        frontier = []

        for _link, tool, datasource in links:
            if tool.id in seen:
                continue
            seen.add(tool.id)
            found[tool.id] = (tool, datasource)
            frontier.append(tool.id)

        depth += 1

    return list(found.values())


async def parents_of(db: AsyncSession, child_id: int) -> List[ToolConfig]:
    """The tools that embed this one, by name."""
    return [parent for _link, parent in await fetch_parent_links(db, [child_id])]


async def parent_names(
    db: AsyncSession,
    child_ids: Sequence[int],
) -> Dict[int, List[str]]:
    """
    ``child id → the names of the tools embedding it``, for a whole page at once.

    The list page's "embedded in" badge. One query for every row on the page rather
    than one per row, which is the same reason ``build_chains`` works in levels.
    """
    names: Dict[int, List[str]] = {}

    for link, parent in await fetch_parent_links(db, list(child_ids)):
        entry = names.setdefault(link.child_id, [])
        if parent.tool_name not in entry:
            entry.append(parent.tool_name)

    return names


async def children_view(
    db: AsyncSession,
    tool: ToolConfig,
) -> List[dict]:
    """
    A tool's direct children as the edit form posts them back:
    ``{child_id | child_graph_id (uuid), child_name, child_kind, child_column,
    parent_reference, binding_mode, value_alias}``.

    Both kinds of child, in run order, because the form edits them as one list — the
    choice an operator is making is the same choice either way. ``child_kind`` is what
    decides which key a row posts back under; ``child_name`` is only ever displayed.

    Only one level. The form edits what *this* tool embeds; what those tools embed
    in turn is edited on their own forms, which is what keeps a chain something you
    build one honest step at a time rather than a tree editor.
    """
    links = await fetch_child_links(db, [tool.id])
    graph_links = await fetch_child_graph_links(db, [tool.id])

    rows = [
        (
            link,
            {
                "child_id": str(child.uuid),
                "child_name": child.tool_name,
                "child_kind": "tool",
            },
        )
        for link, child, _datasource in links
    ] + [
        (
            link,
            {
                "child_graph_id": str(child.uuid),
                "child_name": child.name,
                "child_kind": "graph",
            },
        )
        for link, child in graph_links
    ]

    # Re-sorted across both queries. Each returns its own rows in position order, so
    # concatenating them would show a graph at position 0 after a tool at position 2.
    rows.sort(key=lambda pair: (int(pair[0].position or 0), int(pair[0].id or 0)))

    return [
        {
            **identity,
            "child_column": link.child_column,
            "parent_reference": link.parent_reference,
            "binding_mode": link.binding_mode or BINDING_MODE_IN_LIST,
            "value_alias": link.value_alias or "",
        }
        for link, identity in rows
    ]


async def require_not_embedded(db: AsyncSession, tool: ToolConfig, action: str) -> None:
    """
    Refuse an action that would break a parent, naming the parents.

    ``action`` completes the sentence — "cannot be deleted", "cannot be disabled" —
    so the message says what was attempted rather than describing the relationship
    and leaving the user to work out why it matters.
    """
    parents = await parents_of(db, tool.id)

    if not parents:
        return

    names = ", ".join(sorted(parent.tool_name for parent in parents))

    raise HTTPException(
        status_code=400,
        detail=(
            f"'{tool.tool_name}' {action} because it is embedded in {names}. "
            "Remove it from there first — otherwise those tools would quietly "
            "stop filtering on it and start returning more rows than they should."
        ),
    )


async def require_graph_not_embedded(db: AsyncSession, graph, action: str) -> None:  # noqa: ANN001
    """
    Refuse an action on a **graph** that would break a tool config embedding it.

    The graph counterpart of :func:`require_not_embedded`, and it exists for exactly the
    reason that one does: a filter that quietly disappears widens its parent's results
    and says nothing about having done so. Deleting the graph, or making it a draft again,
    would do that.

    ``action`` completes the sentence — "cannot be deleted", "cannot be made a draft" — so
    the message names what was attempted rather than describing the relationship and
    leaving the operator to work out why it matters.
    """
    links = await fetch_graph_parent_links(db, [int(graph.id)])

    if not links:
        return

    names = ", ".join(sorted({parent.tool_name for _link, parent in links}))

    raise HTTPException(
        status_code=400,
        detail=(
            f"The graph '{graph.name}' {action} because it is embedded in {names}. "
            "Remove it from there first — otherwise those tools would quietly stop "
            "filtering on it and start returning more rows than they should."
        ),
    )


async def embeddable_tools(
    db: AsyncSession,
    user_id: int,
    datasource_id: Optional[Any],
    exclude_uuid: Optional[Any] = None,
) -> List[dict]:
    """
    The tools this datasource can offer as children, with the columns each returns.

    What the form's picker is filled from, so the same rules that would refuse a
    link on save decide what is offered in the first place: the user's own tools,
    enabled, on this datasource, never the tool being edited, and never one that
    already embeds it — that last is the cycle rule, applied before the operator can
    build one rather than after.

    ``columns`` is empty when the tool's output cannot be known without running it
    (a SQL-mode tool, or a builder tool that selects everything). The form then
    takes a typed column name and the chain checks it against the real result.
    """
    if datasource_id is None:
        return []

    datasource = await datasource_crud.get_by_uuid(
        db, datasource_id, extra_filters={"user_id": user_id},
    )
    if not datasource:
        return []

    rows = await fetch_tool_configs_with_details(db, user_id)
    excluded = set()
    editing = None

    for tool, _agent, _ds in rows:
        if exclude_uuid and str(tool.uuid) == str(exclude_uuid):
            editing = tool

    if editing is not None:
        excluded.add(editing.id)
        # Anything that already embeds this tool would close a loop.
        for _link, parent in await fetch_parent_links(db, [editing.id]):
            excluded.add(parent.id)

    return [
        {
            "uuid": str(tool.uuid),
            "tool_name": tool.tool_name,
            "query_mode": tool.query_mode,
            "columns": child_output_columns(tool),
            "kind": "tool",
        }
        for tool, _agent, _ds in rows
        if tool.datasource_id == datasource.id
        and tool.is_enabled
        and tool.id not in excluded
    ]


async def embeddable_graphs(
    db: AsyncSession,
    user_id: int,
    exclude_uuid: Optional[Any] = None,
) -> List[dict]:
    """
    The published graphs this tool can offer as children.

    The graph half of the picker, in the same shape :func:`embeddable_tools` returns so
    the form draws one list from two sources. ``kind`` is what tells them apart, and
    ``columns`` is always empty: nothing knows what a graph's last node returns until it
    runs, exactly as for a SQL-mode tool config, so the form takes a typed name.

    **Not filtered by datasource**, unlike the tool list. A graph's nodes each name their
    own datasource, so there is no single one to compare — see ``_validated_graph_link``,
    which documents the rule this omission follows rather than skips.

    Published only. An unpublished graph is refused on save, so offering it would be
    offering a choice that cannot be taken.

    A graph that reads the tool being edited is dropped, which is the cycle rule applied
    before the operator can build one rather than after — the same courtesy the tool list
    extends. That check costs a walk per candidate, which is affordable here for the
    reason it is not affordable at run time: a form is opened by a person, once.
    """
    from app.models.graph_designer import ToolGraph

    graphs = await CRUDQueryBuilder(ToolGraph).get_many(
        db, filters={"user_id": user_id, "is_active": True}, order_by="name",
    )

    editing = None

    if exclude_uuid:
        # Parsed rather than passed through: the caller is a query string, and the UUID
        # column's bind processor wants a `UUID` and fails on a `str` with an
        # AttributeError that says nothing about what happened.
        import uuid as uuid_pkg

        try:
            editing = await tool_config_crud.get_by_uuid(
                db, uuid_pkg.UUID(str(exclude_uuid)),
            )
        except (TypeError, ValueError):
            editing = None

    offered = []

    for graph in graphs:
        if editing is not None and await _graph_reaches_tool(db, graph, editing.id):
            continue

        offered.append({
            "uuid": str(graph.uuid),
            "tool_name": graph.name,
            "query_mode": "graph",
            "columns": [],
            "kind": "graph",
        })

    return offered


# --------------------------------------------------------------------------
# Writing the tree
# --------------------------------------------------------------------------

async def validated_children(
    db: AsyncSession,
    user_id: int,
    parent: ToolConfig,
    children: Optional[Iterable[dict]],
) -> List[dict]:
    """
    Check a proposed set of children and return them as link payloads.

    Separate from writing them, and called **before** the parent row is saved,
    because ``CRUDQueryBuilder.create`` commits: validating afterwards would leave a
    tool created and its nesting refused, which is a half-saved form the operator
    then has to notice.

    ``parent`` is the tool as it will be *after* this save — a transient
    :class:`ToolConfig` carrying the fields being written is exactly right, and its
    ``id`` may be ``None`` for a tool that does not exist yet. The checks that need
    an id (a cycle, room in a chain that already exists above this tool) are the
    ones a new tool cannot fail, and they are skipped in that case rather than
    guessed at.

    Each entry is ``{"child_id": uuid, "child_column": str, "parent_reference":
    str}``.
    """
    entries = list(children or [])

    if not entries:
        # Still checked with nothing to check against: a statement holding
        # `:active_clients` and embedding no tools is the same unrunnable query as
        # one embedding the wrong tools, and returning early here let it be saved.
        _require_every_placeholder_bound(parent, [])
        return []

    if len(entries) > MAX_CHILDREN_PER_TOOL:
        raise HTTPException(
            status_code=400,
            detail=(
                f"A tool can embed at most {MAX_CHILDREN_PER_TOOL} other tools. "
                "Combine the inner queries, or nest one inside another instead of "
                "hanging them all off the same tool."
            ),
        )

    resolved = [
        await _validated_link(db, user_id, parent, entry, position)
        for position, entry in enumerate(entries)
    ]

    _require_distinct_targets(resolved)
    _require_single_iteration_child(resolved)
    _require_every_placeholder_bound(parent, resolved)
    _require_placeholder_arity(parent, resolved)
    await _require_room_in_the_chain(db, parent, resolved)

    return resolved


async def replace_child_links(
    db: AsyncSession,
    parent_id: int,
    links: Sequence[dict],
) -> None:
    """
    Make this tool's children exactly ``links`` — already validated by
    :func:`validated_children`.

    Replaced wholesale rather than diffed, matching how the form posts the query
    config: the browser sends the complete list, so a diff would only add a way for
    the stored set and the posted set to disagree.
    """
    await delete_links_for_parent(db, parent_id)

    for link in links:
        db.add(ToolConfigLink(parent_id=parent_id, **link))

    await db.flush()


async def _validated_link(
    db: AsyncSession,
    user_id: int,
    parent: ToolConfig,
    entry: Any,
    position: int,
) -> dict:
    """One proposed child, checked against everything that does not need the tree."""
    if not isinstance(entry, dict):
        raise HTTPException(
            status_code=400, detail="Nested tools are not in the expected format",
        )

    if str((entry or {}).get("child_graph_id") or "").strip():
        return await _validated_graph_link(db, user_id, parent, entry, position)

    child = await _resolve_child(db, user_id, entry.get("child_id"))

    if parent.id is not None and child.id == parent.id:
        raise HTTPException(
            status_code=400,
            detail=f"'{parent.tool_name}' cannot embed itself.",
        )

    if child.datasource_id != parent.datasource_id:
        raise HTTPException(
            status_code=400,
            detail=(
                f"'{child.tool_name}' reads a different datasource, so its values "
                "cannot restrict this query. Pick a tool on the same datasource."
            ),
        )

    if not child.is_enabled:
        raise HTTPException(
            status_code=400,
            detail=(
                f"'{child.tool_name}' is disabled, so it cannot be embedded. Enable "
                "it first, or pick another tool."
            ),
        )

    needed = [
        str((entry or {}).get("param") or "")
        for entry in (child.sql_params or [])
        if (entry or {}).get("required", True)
    ]

    if needed:
        # An inner tool is never called by the model — the model calls the parent —
        # so nothing would ever fill these, and the chain would fail on its first
        # run with a message about a parameter the operator did not know was
        # involved. Refused here instead, where the sentence can name it.
        raise HTTPException(
            status_code=400,
            detail=(
                f"'{child.tool_name}' needs "
                + ", ".join(f"'{name}'" for name in needed if name)
                + " to be supplied by the assistant, and an embedded tool is never "
                "called by the assistant — the tool that embeds it is. Make those "
                "values optional, or give the inner query fixed ones."
            ),
        )

    # A tool that does not exist yet cannot be part of a cycle: nothing can embed it.
    if parent.id is not None and await _reaches(db, child.id, parent.id):
        raise HTTPException(
            status_code=400,
            detail=(
                f"'{child.tool_name}' already embeds '{parent.tool_name}', directly "
                "or through another tool. A chain cannot loop back on itself."
            ),
        )

    binding_mode = _validated_binding_mode(entry.get("binding_mode"))

    return {
        "child_id": child.id,
        "child_column": _validated_child_column(child, entry.get("child_column")),
        "parent_reference": _validated_parent_reference(
            parent, child, entry.get("parent_reference"),
        ),
        "binding_mode": binding_mode,
        "value_alias": _validated_value_alias(
            entry.get("value_alias"), binding_mode, child,
        ),
        "position": position,
    }


async def _validated_graph_link(
    db: AsyncSession,
    user_id: int,
    parent: ToolConfig,
    entry: dict,
    position: int,
) -> dict:
    """
    One proposed **graph** child, checked.

    Most of the tool-config rules apply unchanged and are reused. Three differ, and each
    difference is a fact about graphs rather than a relaxation:

    * **No same-datasource rule.** A tool config reads one datasource; a graph's nodes
      each name their own, so there is no single datasource to compare against the
      parent's. The rule it replaces is not dropped so much as unanswerable — and the
      thing that rule protected against, matching an id from one system against an id in
      another, is now the graph author's judgement, exercised node by node.
    * **The child column is not validated against a column list.** Nothing knows what a
      graph's last node returns until it runs — the same position a SQL-mode tool config
      is in, which ``child_output_columns`` already returns ``[]`` for. So the name is
      checked for shape and taken at its word.
    * **A cycle check that follows both kinds of edge**, because a graph's
      ``tool_config`` node runs that tool *including its chain*. See
      :func:`_graph_reaches_tool`; that one prevents a hang rather than a wrong answer.

    A **draft** is refused. Unlike the attachment controls, where a draft would silently
    do nothing, here it would fail the parent loudly on every call — which is better but
    still worse than saying so now, while the operator is looking at the form.
    """
    graph = await _resolve_child_graph(db, user_id, entry.get("child_graph_id"))

    if not graph.is_active:
        raise HTTPException(
            status_code=400,
            detail=(
                f"The graph '{graph.name}' is still a draft, so it cannot be embedded. "
                "Publish it in the Graph Designer first."
            ),
        )

    if parent.id is not None and await _graph_reaches_tool(db, graph, parent.id):
        raise HTTPException(
            status_code=400,
            detail=(
                f"The graph '{graph.name}' reads '{parent.tool_name}', directly or "
                "through another tool, so embedding it here would make the two run each "
                "other without end. Point that node at a different tool config."
            ),
        )

    binding_mode = _validated_binding_mode(entry.get("binding_mode"))

    return {
        "child_graph_id": graph.id,
        "child_column": _validated_graph_column(graph, entry.get("child_column")),
        "parent_reference": _validated_parent_reference(
            parent, graph, entry.get("parent_reference"),
        ),
        "binding_mode": binding_mode,
        "value_alias": _validated_value_alias(
            entry.get("value_alias"), binding_mode, graph,
        ),
        "position": position,
    }


async def _resolve_child_graph(db: AsyncSession, user_id: int, value: Any):  # noqa: ANN201
    """
    The graph behind a submitted uuid, scoped to its owner.

    Ownership on a graph is direct — ``tool_graphs.user_id`` — rather than the
    tool → agent → user hop a tool config takes, so this is one lookup. A graph belonging
    to somebody else is refused with the sentence a missing one gets, the rule every read
    on this feature follows.
    """
    from app.models.graph_designer import ToolGraph

    wanted = require_uuid(value, "graph")
    graph = await CRUDQueryBuilder(ToolGraph).get_by_uuid(
        db, wanted, extra_filters={"user_id": user_id},
    )

    if graph is None:
        raise HTTPException(
            status_code=400,
            detail="That graph could not be found, so it cannot be embedded.",
        )

    return graph


def _validated_graph_column(graph, value: Any) -> str:  # noqa: ANN001
    """
    The name of the value a graph hands upward.

    Checked for shape only. A graph's last node may return rows, a bare list or a single
    value, and nothing here knows which — so a name that turns out to match no key is
    reported at run time as "no values", the same way a SQL-mode tool config's column is.

    It is still **required**, even though a bare list ignores it: leaving it blank would
    make a link that reads whichever shape it happens to get, and an operator changing the
    graph's last node from a list to rows would find their filter had quietly become
    empty. Naming it is how the intent survives an edit to the graph.
    """
    name = require_object_name(value, "the value this graph provides")

    if not _VALUE_ALIAS_PATTERN.match(name):
        raise HTTPException(
            status_code=400,
            detail=(
                f"'{name}' is not a valid name for the value '{graph.name}' provides. "
                "Use letters, numbers and underscores, starting with a letter."
            ),
        )

    return name


def _child_label(child) -> str:  # noqa: ANN001
    """
    What to call a child in a message, whichever kind it is.

    A tool config has a ``tool_name`` — an identifier a model calls; a graph has a
    ``name`` — a sentence a person wrote. Both validators below quote the child at the
    operator, so both need whichever the child has, and neither cares which it was.
    """
    return str(getattr(child, "tool_name", None) or getattr(child, "name", "") or "")


def _validated_binding_mode(value: Any) -> str:
    """
    How this child's values reach its parent. Blank means the historical default.

    Blank is treated as ``in_list`` rather than refused because that is what every
    link meant before the column existed, and a form posted by an older cached page
    should save the tool it describes rather than an error.
    """
    mode = str(value or "").strip().lower() or BINDING_MODE_IN_LIST

    if mode not in BINDING_MODE_VALUES:
        raise HTTPException(
            status_code=400,
            detail=(
                "Choose how a nested tool's values are used: match any of them, or "
                "run this query once per value."
            ),
        )

    return mode


def _validated_value_alias(
    value: Any,
    binding_mode: str,
    child: Any,
) -> Optional[str]:
    """
    The name each row records the value it was produced for under, or ``None``.

    Only meaningful for an iterating link — a list binding produces one result set,
    and every row of it already matched *some* value in the list, so there is no
    single value to label it with. Setting one on a list binding is refused rather
    than ignored, because a name in a form that does nothing is a name the operator
    will later swear they set.

    Whether the name **collides** with a column the query returns is decided at run
    time, not here: for a SQL-mode parent nothing knows the output columns until the
    statement runs, so checking half the cases here and half there would only make
    the rule harder to state.
    """
    alias = str(value or "").strip()

    if not alias:
        return None

    if binding_mode != BINDING_MODE_EACH:
        raise HTTPException(
            status_code=400,
            detail=(
                f"'{_child_label(child)}' matches a list of values, so there is no "
                "single "
                "value to record on each row. Either switch it to 'run once per "
                "value', or leave the name blank."
            ),
        )

    if not _VALUE_ALIAS_PATTERN.match(alias):
        raise HTTPException(
            status_code=400,
            detail=(
                "The name a nested tool's value is recorded under must start with a "
                "letter or underscore and contain only letters, numbers and "
                "underscores — for example 'department_id'."
            ),
        )

    return alias


async def _resolve_child(
    db: AsyncSession,
    user_id: int,
    child_id: Any,
) -> ToolConfig:
    """
    The child tool, scoped to its owner.

    Two hops, the same as ``tool_config_service.get_tool_config``: the tool row
    carries no ``user_id``, so ownership comes from its agent. A tool belonging to
    someone else is **not found** rather than forbidden — the uuid of a row you do
    not own should not be confirmable.
    """
    if not child_id:
        raise HTTPException(status_code=400, detail="Pick the tool to embed")

    # The children arrive as a raw JSON array — the schema guarantees its shape, not
    # the type of what is inside it — so the uuid is text until it is parsed here.
    child = await tool_config_crud.get_by_uuid(
        db, require_uuid(str(child_id), "Nested tool"),
    )

    if child:
        owner = await agent_crud.get_one(
            db, filters={"id": child.data_agent_id, "user_id": user_id},
        )
        if owner:
            return child

    raise HTTPException(status_code=404, detail="Nested tool not found")


def _validated_child_column(child: ToolConfig, value: Any) -> str:
    """
    The column of the child's result whose values are collected.

    Checked against what the child's query actually returns **when that is
    knowable** — a builder query naming its columns. It is not knowable for a
    SQL-mode child, and not for a builder query that selects everything (which
    expands to every *active* column at run time, a set that changes with Data
    Sources). In those cases the name is only checked as a name here and verified
    against the real result when the chain runs, which is the same bargain
    ``prompt_builder`` makes when it refuses to list a SQL tool's fields.
    """
    column = require_object_name(value, "Nested tool column")
    available = child_output_columns(child)

    if available and column not in available:
        raise HTTPException(
            status_code=400,
            detail=(
                f"'{child.tool_name}' does not return a column called '{column}'. "
                f"It returns: {', '.join(available)}."
            ),
        )

    return column


def _validated_parent_reference(
    parent: ToolConfig,
    child: Any,
    value: Any,
) -> str:
    """
    Where the child's values land in the parent — a column in builder mode, a bind
    parameter name in SQL mode.

    ``child`` is quoted in the messages and is otherwise unread, so it may be a tool
    config or a graph — see :func:`_child_label`.

    The two modes are checked by their own rules because they mean different
    things: a builder reference has to be a column of a table this query reads,
    while a SQL reference is a name the operator wrote into their statement and
    which must actually be in it. An unbound placeholder would be a crash the
    moment the tool ran.
    """
    if (parent.query_mode or "") == QUERY_MODE_SQL:
        name = str(value or "").strip().lower()

        if not _BIND_NAME_PATTERN.match(name):
            raise HTTPException(
                status_code=400,
                detail=(
                    "A nested tool's placeholder name must start with a letter and "
                    "contain only lowercase letters, numbers and underscores — for "
                    "example 'active_clients'."
                ),
            )

        if name not in _placeholders_in(parent.sql_query):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"The SQL query does not use ':{name}' anywhere, so "
                    f"'{_child_label(child)}' would have nowhere to put its values. "
                    f"Add it to the statement, for example: WHERE id IN :{name}"
                ),
            )

        return name

    tables = query_tables(parent.config.get("joins"), parent.table_name)

    return validated_column_reference(value, "Nested tool target column", tables)


def _require_distinct_targets(links: Sequence[dict]) -> None:
    """
    Refuse the same child bound to the same target twice.

    The database refuses it too (``uq_tool_config_links_parent_child_target`` and its
    ``_graph_`` counterpart), but an IntegrityError arrives after the transaction is dirty
    and reads as a bug. The same child may still feed two *different* targets — one tool
    returning client ids can restrict both ``owner_id`` and ``billed_to_id``.

    The identity is *which kind of child and which one*, keyed off whichever of the two
    columns the row carries. A row is one or the other by construction, so a tool and a
    graph can share a target — they are different children — while the same one twice
    cannot.
    """
    seen = set()

    for link in links:
        reference = str(link["parent_reference"])
        target = (
            link.get("child_id"),
            link.get("child_graph_id"),
            reference.lower(),
        )

        if target in seen:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"The same tool is embedded twice against '{reference}'. One of "
                    "the two has nothing to add."
                ),
            )

        seen.add(target)


def _require_single_iteration_child(links: Sequence[dict]) -> None:
    """
    Refuse more than one child that makes the parent run once per value.

    Two would be a cartesian product: ten departments and eight regions is eighty
    runs of the parent, past
    :data:`~app.services.deep_agents.query_executor.MAX_CHAIN_ITERATIONS` and so
    refused outright, and eighty round trips inside one chat turn even where it is
    not. The query that actually wants writing is one statement joining both.
    """
    iterating = [
        link for link in links
        if link.get("binding_mode") == BINDING_MODE_EACH
    ]

    if len(iterating) > 1:
        raise HTTPException(
            status_code=400,
            detail=(
                "Only one nested tool can make this query run once per value. Two "
                "would run it once per combination of the two, which is more runs "
                "than a single answer can hold. Set the others to match a list of "
                "values instead."
            ),
        )


def _require_placeholder_arity(
    parent: ToolConfig,
    links: Sequence[dict],
) -> None:
    """
    Refuse a placeholder used in a shape its binding cannot take, in SQL mode.

    A list binding renders parenthesised — ``IN (?, ?, ?)`` — and a scalar one does
    not, so the two are not interchangeable in a statement:

    * ``WHERE id = :x`` with a list binding becomes ``id = (?, ?, ?)``;
    * ``WHERE id IN :x`` with a scalar binding becomes ``id IN ?``.

    Both are syntax errors, and both are errors the *database* reports, in the middle
    of a conversation, months after the tool was saved. This is a text check over the
    statement with literals blanked, so it catches the shape immediately next to the
    placeholder and nothing cleverer — which is the common mistake and worth the
    twenty lines.
    """
    if (parent.query_mode or "") != QUERY_MODE_SQL:
        return

    for link in links:
        name = str(link.get("parent_reference") or "")

        if not name:
            continue

        iterating = link.get("binding_mode") == BINDING_MODE_EACH
        shape = placeholder_shape(parent.sql_query, name)
        in_shape = shape == PLACEHOLDER_LIST
        comparison_shape = shape == PLACEHOLDER_SINGLE

        if iterating and in_shape:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"The SQL query uses 'IN :{name}', which expects a list, but this "
                    "nested tool is set to run the query once per value. Either "
                    f"compare it directly — 'id = :{name}' — or switch the tool back "
                    "to matching a list of values."
                ),
            )

        if not iterating and comparison_shape:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"The SQL query compares against ':{name}' as a single value, but "
                    "this nested tool supplies a list. Either write it as "
                    f"'IN :{name}', or set the tool to run the query once per value."
                ),
            )


def _require_every_placeholder_bound(
    parent: ToolConfig,
    links: Sequence[dict],
) -> None:
    """
    In SQL mode, every ``:name`` in the statement must be filled by something.

    The opposite check to :func:`_validated_parent_reference`, and it has to exist
    separately: that one refuses a child pointing at a placeholder that is not
    there, this one refuses a placeholder with nothing to fill it. Either way the
    statement cannot run, and the difference between finding out now and finding
    out mid-conversation is the whole reason both checks are here.

    A placeholder is filled either by a nested tool or by a parameter the operator
    declared for the agent to supply. Both are checked here rather than in two
    places, because "is this name accounted for" is one question however it is
    answered — and splitting it would let a tool be saved with a name that each
    check thought the other one covered.
    """
    if (parent.query_mode or "") != QUERY_MODE_SQL:
        return

    bound = {link["parent_reference"] for link in links}
    bound |= {
        str((entry or {}).get("param") or "").strip().lower()
        for entry in (parent.sql_params or [])
    }
    unbound = sorted(_placeholders_in(parent.sql_query) - bound)

    if unbound:
        raise HTTPException(
            status_code=400,
            detail=(
                "The SQL query uses "
                + ", ".join(f"':{name}'" for name in unbound)
                + ", which nothing fills. Embed a nested tool for each placeholder, "
                "declare it as a value the assistant supplies, or take it out of the "
                "statement."
            ),
        )


def _placeholders_in(sql_query: Optional[str]) -> set:
    """
    The ``:name`` placeholders a statement uses.

    Now one line over ``sql_guard.bind_placeholders``, which is where the rule lives
    for every caller — see that function on why it used to be copied into this module
    and ``tool_config_service`` instead.
    """
    return bind_placeholders(sql_query)


async def _require_room_in_the_chain(
    db: AsyncSession,
    parent: ToolConfig,
    links: Sequence[dict],
) -> None:
    """
    Refuse a set of children that would make the whole chain too deep or too big.

    Measured over the **whole** chain and not just downwards, because this tool may
    itself be embedded: hanging a three-level subtree under a tool that is already
    two levels down produces a five-level chain nobody looked at. Every level is a
    round trip inside a turn a visitor is waiting on.
    """
    # `above` counts this tool and everything over it; `below` is the tallest child
    # subtree on its own. Added rather than nested — this tool is in `above`, and
    # counting it in both would refuse a chain one level shorter than the limit.
    above = await _levels_above(db, parent.id) if parent.id is not None else 1
    below = 0
    nodes = {parent.id}

    for link in links:
        child_id = link.get("child_id")

        if child_id is None:
            # A graph child. One level and one node, always: a graph is a leaf in this
            # tree — its own composition is drawn on its canvas, where the Graph
            # Designer's own ceilings apply — so there is no subtree to measure. It is
            # still counted, because running it is a round trip like any other.
            below = max(below, 1)
            nodes.add(f"graph:{link['child_graph_id']}")
            continue

        depth, subtree = await _subtree_size(db, child_id)
        below = max(below, depth)
        nodes |= subtree

    if above + below > MAX_CHAIN_DEPTH:
        raise HTTPException(
            status_code=400,
            detail=(
                f"That would make a chain {above + below} tools deep, and the limit "
                f"is {MAX_CHAIN_DEPTH}. Flatten one of the inner tools into the "
                "query that embeds it."
            ),
        )

    if len(nodes) > MAX_CHAIN_NODES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"That chain would involve {len(nodes)} tools, and the limit is "
                f"{MAX_CHAIN_NODES}. Each one is a separate query run before the "
                "answer comes back."
            ),
        )


async def _levels_above(db: AsyncSession, tool_id: int) -> int:
    """How many levels of parents sit above this tool, itself counted as one."""
    levels = 1
    frontier = [tool_id]
    seen = {tool_id}

    while frontier and levels < MAX_CHAIN_DEPTH:
        links = await fetch_parent_links(db, frontier)
        frontier = [
            parent.id for _link, parent in links if parent.id not in seen
        ]
        seen.update(frontier)

        if not frontier:
            break

        levels += 1

    return levels


async def _subtree_size(db: AsyncSession, tool_id: int) -> Tuple[int, set]:
    """``(levels below and including this tool, every tool id in it)``."""
    levels = 1
    frontier = [tool_id]
    seen = {tool_id}

    while frontier and levels < MAX_CHAIN_DEPTH:
        links = await fetch_child_links(db, frontier)
        frontier = [tool.id for _link, tool, _ds in links if tool.id not in seen]
        seen.update(frontier)

        if not frontier:
            break

        levels += 1

    return levels, seen


async def _reaches(db: AsyncSession, start_id: int, target_id: int) -> bool:
    """Whether ``target_id`` is anywhere below ``start_id``."""
    _levels, seen = await _subtree_size(db, start_id)

    return target_id in seen


async def _graph_reaches_tool(db: AsyncSession, graph, target_id: int) -> bool:  # noqa: ANN001
    """
    Whether running ``graph`` would end up running the tool ``target_id``.

    **This one prevents a hang, not a wrong answer**, which is why it walks further than
    it looks like it needs to. A graph's ``tool_config`` node runs that tool *including
    its chain* (``node_runners._run_tool_config`` says so), so:

        tool P  ──embeds──▶  graph G  ──tool_config node──▶  tool P

    is unbounded recursion across separate LangGraph runs, where neither run's recursion
    limit nor any loop ceiling applies to the other. Nothing would report it; the turn
    would simply never end.

    And it can be longer than that. A graph's node may name a tool that embeds *another*
    graph that reads the first tool, so the cycle alternates between the two kinds of
    edge and following only one of them would miss it. So this walks both: every tool a
    graph reads, everything below those tools, and every graph *they* embed, until nothing
    new turns up.

    Bounded by ``MAX_CHAIN_DEPTH`` rounds and a seen set, so a bad row already in the
    database cannot make the guard itself the hang.
    """
    graph_ids = {int(graph.id)}
    tool_ids: set = set()
    frontier_graphs = [graph]
    rounds = 0

    while frontier_graphs and rounds < MAX_CHAIN_DEPTH:
        rounds += 1

        # Every tool config named by a node of every graph on this frontier.
        wanted = {
            uuid
            for item in frontier_graphs
            for uuid in _tool_uuids_in(item)
        }
        reached = await _tools_by_uuid(db, wanted) if wanted else []

        fresh_tools = [tool.id for tool in reached if tool.id not in tool_ids]
        tool_ids.update(fresh_tools)

        if target_id in tool_ids:
            return True

        # Everything below those tools, by the ordinary tool-to-tool edge.
        for tool_id in fresh_tools:
            _levels, below = await _subtree_size(db, tool_id)

            if target_id in below:
                return True

            tool_ids.update(below)

        # And every graph those tools embed, which is the next frontier.
        links = await fetch_child_graph_links(db, sorted(tool_ids))
        frontier_graphs = [
            found for _link, found in links if int(found.id) not in graph_ids
        ]
        graph_ids.update(int(found.id) for found in frontier_graphs)

    return False


def _tool_uuids_in(graph) -> List[str]:  # noqa: ANN001
    """The tool configs a graph's ``tool_config`` nodes name, by public uuid."""
    from app.models.graph_designer import NODE_TOOL_CONFIG

    found = []

    for node in (getattr(graph, "graph_data", None) or {}).get("nodes") or []:
        if not isinstance(node, dict) or str(node.get("type")) != NODE_TOOL_CONFIG:
            continue

        wanted = str((node.get("data") or {}).get("tool_config_id") or "").strip()

        if wanted:
            found.append(wanted)

    return found


async def _tools_by_uuid(db: AsyncSession, uuids: Iterable[str]) -> List[ToolConfig]:
    """The tool configs behind a set of public uuids, skipping any that do not parse."""
    import uuid as uuid_pkg

    parsed = []

    for value in uuids:
        try:
            parsed.append(uuid_pkg.UUID(str(value)))
        except (TypeError, ValueError):
            # A node naming something that is not a uuid cannot reach any tool, so it
            # cannot close a cycle. It is refused when the graph is saved, not here.
            continue

    if not parsed:
        return []

    found = []

    for value in parsed:
        row = await tool_config_crud.get_by_uuid(db, value)

        if row is not None:
            found.append(row)

    return found


# --------------------------------------------------------------------------
# Describing the tree
# --------------------------------------------------------------------------

def child_output_columns(tool: ToolConfig) -> List[str]:
    """
    The names a builder tool's rows come back under, or ``[]`` when they cannot be
    known without running it.

    Mirrors ``query_executor._selected_columns`` / ``_aggregated_columns``: an
    explicit column arrives under its alias or its bare name, an aggregation under
    its alias or ``function_column``. Two cases return nothing rather than a guess —
    a SQL-mode tool (its columns are whatever the statement selects, which nothing
    here parses) and a builder tool that selects everything (which expands to every
    *active* column at run time, a set Data Sources can change).
    """
    if (tool.query_mode or "") == QUERY_MODE_SQL:
        return []

    config = tool.config or {}
    columns = config.get("columns") or []
    aggregations = config.get("aggregations") or []

    if not columns and not aggregations:
        return []

    names = []

    for entry in columns:
        reference = str((entry or {}).get("column") or "")
        alias = str((entry or {}).get("alias") or "")
        name = alias or reference.rpartition(".")[2]
        if name:
            names.append(name)

    for entry in aggregations:
        reference = str((entry or {}).get("column") or "")
        function = str((entry or {}).get("type") or "").lower()
        alias = str((entry or {}).get("alias") or "")
        bare = reference.rpartition(".")[2]
        name = alias or (f"{function}_{bare}" if function and bare else "")
        if name:
            names.append(name)

    return names


def chain_view(node: ChainNode) -> List[dict]:
    """
    A chain flattened for display: one entry per embedded tool, deepest last,
    carrying the indent level and the binding it applies.

    Excludes the root — the row it is rendered in *is* the root — so an empty list
    means "this tool embeds nothing", which is what the list page checks.
    """
    entries: List[dict] = []

    def visit(current: ChainNode, depth: int) -> None:
        for child in current.children:
            entries.append({
                "depth": depth,
                "tool_uuid": str(child.tool.uuid),
                "tool_name": child.tool.tool_name,
                "child_column": child.child_column,
                "parent_reference": child.parent_reference,
                "parent_name": current.tool.tool_name,
                "binding_mode": child.binding_mode,
                "iterates": child.iterates,
                "value_alias": child.value_alias,
                "is_enabled": child.tool.is_enabled,
            })
            visit(child, depth + 1)

    visit(node, 1)

    return entries
