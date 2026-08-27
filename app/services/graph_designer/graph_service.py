"""
What a designed graph *is* — the rules, the ownership, and the CRUD.

This module deliberately does **not** import LangGraph. It is the same split
``tool_chain_service`` / ``tool_chain_graph`` makes, for the same reason: the rules that
decide whether a drawing is a valid graph are the part most worth testing, and they are
testable without the runtime. ``graph_compiler`` is the only module here that needs
``langgraph`` installed.

**This module owns the node vocabulary.** The schemas deliberately do not pin it (see
``app/schemas/graph_designer/graph_designer_schemas.py``) because the compiler and the
runners read a graph through the rules below, so this is the version that has to be
right. ``static/js/graph_designer.js`` mirrors these rules in the browser so the user is
stopped before they save; the browser's copy is a courtesy and this one is the authority.

Two independent switches decide whether a data agent may call a graph — ``is_active``
(published vs draft, set here) and the attachment itself. Both are required, and
``queries.fetch_agent_graphs`` is where that is enforced, exactly as
``flow_service.get_active_flow`` does for a conversation flow.

**There are two kinds of attachment, and a graph has at most one of them.**
:func:`attach_graph` points one data agent at the graph; :func:`share_graph` puts it on a
workspace's shelf, where every agent assigned to that workspace picks it up. They are
mutually exclusive because holding both would hand the same graph to one agent twice —
once as its own, once through its workspace — and a model offered two identically named
tools cannot choose between them. Each write path clears the other, rather than refusing,
because "share this instead" is what the operator meant by pressing the second control.

## What is refused, and why

Each of these produces a *plausible wrong run* rather than an obvious error, which is
why none of them is left until execution:

| Refused | Because |
|---|---|
| No start node, or more than one | a drawing has no reading order, so "where does it begin" cannot be inferred — and two starts is two different graphs |
| An edge naming a node that is not there | the compiler would build a graph with a dangling transition and the run would stop somewhere nobody drew |
| Two edges on one output port | the run would take one of them, and which one would depend on dict ordering |
| An edge into ``start``, or out of ``success``/``failure`` | both are lies about the compiled graph: START has no inbound edge and END has no outbound one |
| A cycle that does not pass through a loop node | this is the one that matters most. A cycle is exactly what a loop is, so cycles cannot simply be banned — but a cycle with no loop node has no cursor and no ceiling, so it is an unbounded run, and LangGraph would surface it as ``GraphRecursionError`` a long way from the drawing that caused it |
| A loop whose ``each`` output leads nowhere, or whose body never comes back to it | the mirror of the cycle rule above, and the more expensive half. The router would send the loop into its body, the body would run, and with nothing leading back the run would carry on past it — **one pass of eighty-two, reported as success**, with nothing in the log saying the rest were never attempted |
| A ``value`` node whose JSON does not match its kind | a ``dict`` where a list was promised feeds a downstream ``IN`` nothing it can use |
| A ``sql`` node with no statement, or one that is not a single read | ``validated_tool_sql`` is the same check a tool config's SQL passes, so a statement that saves here is one that would save there |
| A ``branch`` with no conditions | every port would be unreachable and the run would stop at it |
| A ``for_each``/``do_until`` whose source node cannot reach it | it would loop over a value that does not exist yet |
| A ``:name`` in a statement that is not declared as a parameter | nothing can fill it, so the statement cannot run — and the driver's complaint arrives mid-run naming nothing the author would recognise |
| A ``:`` with a space after it — ``where id = : item`` | the space hides the placeholder from every other check here, so the statement saves clean and then fails against the database quoting ``': item'``. It is also the exact mistake an author makes when there is nowhere to put a value, which makes it worth naming rather than diagnosing |
| A parameter written ``= :x`` but wired to a list, or ``IN :x`` but wired to one value | an expanding parameter always renders parenthesised, so one of them is `= (?, ?, ?)` and the other is `IN ?`. Both are syntax errors the *database* reports, long after the form was closed |
| A ``for_each`` collecting a node outside its own body | that node ran once, before the loop, so every pass would collect the same rows again — and a union of duplicates looks exactly like a union |
| A ``do_until`` that collects | only a loop that knows which pass is its last can publish a union, and for a ``do_until`` that is the router's decision, taken after the runner returns |
| A ``sql_union`` outside a ``for_each`` body | it builds one copy of its statement per pass and runs them on the pass it is told is the last, which only a ``for_each`` cursor knows. Outside one it would build a statement and never run it — a silent nothing from a node whose box says it succeeded |
| A ``sql_union`` fragment carrying ``ORDER BY`` or ``LIMIT`` | unparenthesised, either binds to the whole union rather than to the member it was written on, so it would quietly sort or truncate every pass at once. Parentheses would fix it on two of the three supported databases and are invalid around a compound-select operand in SQLite |
| A parameter named like a generated one — ``id__p7`` | that is how each pass's copy of the statement is kept apart, so it would collide and one pass would be filled with another pass's value |

**There is no cap on how many nodes or edges a graph may have.** What bounds a run is
the per-loop iteration ceiling, which is a bound on work rather than on drawing — see
``graph_compiler``.
"""

import json
import re
import uuid as uuid_pkg
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from litestar.exceptions import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.db_utils import CRUDQueryBuilder
from app.services.email_dispatch import smtp_service as email_smtp_service
from app.services.email_dispatch import template_service as email_template_service
from app.db.graph_designer.queries import fetch_graphs_with_owner_names
from app.models.data_agents import DataAgent
from app.models.file_delivery import FILE_FORMAT_VALUES
from app.models.graph_designer import (
    BINDING_MODE_IN_LIST,
    BINDING_MODE_ONE,
    BINDING_MODE_VALUES,
    HUMAN_EXPECTS_VALUES,
    LOOP_NODE_TYPES,
    NODE_BRANCH,
    NODE_CREATE_FILE,
    NODE_DOWNLOAD_FILE,
    NODE_EMAIL,
    NODE_FOR_EACH,
    NODE_HUMAN,
    NODE_SQL,
    NODE_SQL_UNION,
    NODE_START,
    NODE_TIMER,
    NODE_TOOL_CONFIG,
    NODE_TYPE_LABELS,
    NODE_TYPE_VALUES,
    NODE_VALUE,
    NODE_WAIT,
    TERMINAL_NODE_TYPES,
    TIMER_ACTION_VALUES,
    TIMER_START,
    VALUE_KIND_ARRAY,
    VALUE_KIND_DICT,
    VALUE_KIND_LIST,
    VALUE_KIND_VALUES,
    ToolGraph,
)
from app.services.data_agents import data_agent_service
from app.services.tool_configs import tool_chain_service, tool_config_service
from app.services.workspaces import workspace_service
from app.utils.sql_guard import (
    PLACEHOLDER_LIST,
    PLACEHOLDER_SINGLE,
    at_depth_zero,
    bind_placeholders,
    normalised_sql,
    paren_depths,
    placeholder_shape,
    spaced_placeholder,
    stripped_literals,
)

graph_crud = CRUDQueryBuilder(ToolGraph)
agent_crud = CRUDQueryBuilder(DataAgent)

# A column name a collected row is labelled with. It becomes a key in the result rows and
# is grouped by like any other output column, so it has to be a plain identifier — the
# same rule, character for character, as `tool_chain_service._VALUE_ALIAS_PATTERN`, which
# is the same field one layer down. Spelled `[A-Za-z0-9_]` rather than `\w` on purpose:
# `\w` is Unicode-aware in Python and would quietly widen what an alias may contain.
_ALIAS_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")

# How a union node keeps one pass's copy of a statement apart from another's: pass 7's
# `:id` is bound as `:id__p7` (`node_runners._extended_union`). A parameter the author
# happens to name this way is refused, because it would collide with a generated one and a
# pass would be filled from another pass.
_GENERATED_SUFFIX_PATTERN = re.compile(r"__p\d+$")

# A clause that, written unparenthesised on one member of a union, applies to the whole
# union instead. Matched at bracket depth zero, so the same words inside a subquery — where
# they are local and correct — are left alone.
_ORDER_BY_PATTERN = re.compile(r"\border\s+by\b", re.IGNORECASE)
_LIMIT_PATTERN = re.compile(r"\blimit\b", re.IGNORECASE)


# The port every node with a single outcome uses. Named because three places compare
# against it and a bare "default" in each of them is three chances to typo one.
PORT_DEFAULT = "default"

# The port a node takes when its own work failed and the author drew a path for that.
# Optional by design: a node with no `error` edge fails the run, which is the ordinary
# behaviour and what someone who has not thought about failure should get.
PORT_ERROR = "error"

# A branch's fall-through. Reserved, so a condition cannot be given this name and then
# silently never be evaluated.
PORT_ELSE = "else"

# A loop's two outcomes.
PORT_BODY = "body"
PORT_DONE = "done"

# A union node's way out, taken on the one pass it runs its statement. Its ``default`` port
# is the ordinary one and goes back round the loop, so this is what tells the drawing apart
# from a node that simply carries on — and why a union node needs no second box to run what
# it built.
PORT_EXECUTE = "execute"

# How many times a loop may go round when its node does not say. Deliberately generous
# — the point of the ceiling is to make an unbounded loop *stop*, not to make a large
# one impossible — and overridable per node from the properties panel.
DEFAULT_MAX_ITERATIONS = 200

# The hard ceiling on a node's `max_iterations`. A loop asking for more than this is
# almost certainly a typo (a stray zero), and the run would outlive any reason to watch
# it. Refused at save time, which is the only point at which the author is present to
# read the message.
ABSOLUTE_MAX_ITERATIONS = 100_000

# How a branch condition may compare. Compared, never evaluated — there is no `eval`
# anywhere on this path. The same approach `engine_service._evaluate_condition` takes,
# widened only by what a data pipeline needs that a conversation flow did not.
CONDITION_OPERATORS = (
    ("equals", "is equal to"),
    ("not_equals", "is not equal to"),
    ("contains", "contains"),
    ("not_contains", "does not contain"),
    ("is_empty", "is empty"),
    ("not_empty", "is not empty"),
    ("greater_than", "is greater than"),
    ("less_than", "is less than"),
)

CONDITION_OPERATOR_VALUES = frozenset(value for value, _ in CONDITION_OPERATORS)

# The operators that compare against nothing. A filled-in value beside one of these is
# ignored rather than refused — the control is still on screen when the operator is
# switched, and clearing it for the user would lose what they typed.
VALUELESS_OPERATORS = frozenset({"is_empty", "not_empty"})


# A new graph opens with a Start node already on the canvas. An empty canvas is a
# worse first experience than one node: it gives no clue what a node looks like, and
# the first rule below would refuse a save until the user found the palette.
_DEFAULT_GRAPH: Dict[str, Any] = {
    "nodes": [
        {
            "id": "start",
            "type": NODE_START,
            "position": {"x": 80, "y": 80},
            "data": {"label": "Start"},
        },
    ],
    "edges": [],
}


# --------------------------------------------------------------------------
# Read
# --------------------------------------------------------------------------

async def get_graph_views(db: AsyncSession, user_id: int) -> List[dict]:
    """
    Every graph this user owns, shaped for the library page.

    The node and edge counts come from the stored document rather than from a
    subquery, because they are two ``len()`` calls over JSON already in memory and a
    graph is read whole or not at all.

    ``agent_id`` and ``workspace_id`` are the **public uuids** of the current attachment,
    and they are what the edit form preselects. They come from the same joins the names
    do, so the identifier and the label a row shows can never disagree.
    """
    rows = await fetch_graphs_with_owner_names(db, user_id)

    return [
        {
            "uuid": str(graph.uuid),
            "name": graph.name,
            "description": graph.description,
            "is_active": graph.is_active,
            "node_count": len(_nodes_of(graph.graph_data)),
            "edge_count": len(_edges_of(graph.graph_data)),
            "agent_id": str(agent_uuid) if agent_uuid else None,
            "agent_name": agent_name,
            "allow_recursive_aggregate": bool(graph.allow_recursive_aggregate),
            # The workspace it is shared with, if it is. At most one of the two is ever
            # set, so the column reads as one answer to "who can call this" rather than
            # two that have to be reconciled.
            "workspace_id": str(workspace_uuid) if workspace_uuid else None,
            "workspace_name": workspace_name,
            "updated_at": graph.updated_at,
        }
        for graph, agent_uuid, agent_name, workspace_uuid, workspace_name in rows
    ]


async def get_attachment_choices(db: AsyncSession, user_id: int) -> dict:
    """
    The two things a graph can be made callable through, for the library's pickers.

    ``{"data_agents": [{uuid, name, is_active}], "workspaces": [...]}`` — composed from
    the services that own those lists rather than queried here, the same call
    :func:`node_options` makes for the canvas.

    Both lists include switched-off entries. A graph attached to an archived workspace
    has to be visible in that picker to be moved out of it, and hiding it would leave a
    graph attached to something the operator cannot see.
    """
    return {
        "data_agents": await data_agent_service.get_agent_choices(db, user_id),
        "workspaces": await workspace_service.get_workspace_choices(db, user_id),
    }


async def get_graph(
    db: AsyncSession,
    user_id: int,
    graph_id: uuid_pkg.UUID,
) -> ToolGraph:
    """
    One graph, scoped to its owner.

    A graph belonging to someone else is refused with the same sentence a missing one
    gets. Answering differently would confirm that the uuid is real — the rule
    ``tool_graph_service`` and ``tool_config_service`` both follow.
    """
    graph = await graph_crud.get_by_uuid(
        db, graph_id, extra_filters={"user_id": user_id},
    )

    if not graph:
        raise HTTPException(status_code=404, detail="That graph could not be found.")

    return graph


async def get_graph_view(
    db: AsyncSession,
    user_id: int,
    graph_id: uuid_pkg.UUID,
) -> dict:
    """
    One graph shaped for the canvas page, with its owner's public uuid resolved.

    Both attachments are reported even though only one can be set, so the page's two
    controls each read their own field rather than deducing their state from the other's
    being empty.
    """
    graph = await get_graph(db, user_id, graph_id)

    agent: Optional[DataAgent] = None
    if graph.data_agent_id is not None:
        agent = await agent_crud.get_one(db, filters={"id": graph.data_agent_id})

    workspace_uuid = await workspace_service.get_workspace_public_id(
        db, user_id, graph.workspace_id,
    )

    return {
        "uuid": str(graph.uuid),
        "name": graph.name,
        "description": graph.description,
        "is_active": graph.is_active,
        "node_count": len(_nodes_of(graph.graph_data)),
        "edge_count": len(_edges_of(graph.graph_data)),
        "agent_id": str(agent.uuid) if agent else None,
        "agent_name": agent.name if agent else None,
        "workspace_id": workspace_uuid or None,
        "allow_recursive_aggregate": bool(graph.allow_recursive_aggregate),
        "updated_at": graph.updated_at,
    }


async def node_options(
    db: AsyncSession, user_id: int, graph_id: uuid_pkg.UUID,
) -> dict:
    """
    Everything the properties panel's pickers need, in one payload.

    Composed from the services that already own these lists —
    ``tool_config_service.get_datasource_choices`` / ``get_tool_config_views`` and
    ``data_agent_service.get_agent_choices`` — so nothing here queries those tables
    and a datasource that is inactive is flagged the same way it is on every other
    page.

    Unavailable options are **offered and flagged, not hidden**. An operator looking
    for a tool that is switched off needs to see that it is switched off; a list that
    silently omits it sends them to check the wrong thing.

    Scoped to one graph because the email template list is: a graph shared into a
    workspace picks from that workspace's templates. Resolving the graph first is also
    what makes this 404 for somebody else's graph rather than answer it.
    """
    graph = await get_graph(db, user_id, graph_id)

    datasources = await tool_config_service.get_datasource_choices(db, user_id)
    tool_configs = await tool_config_service.get_tool_config_views(db, user_id)
    agents = await data_agent_service.get_agent_choices(db, user_id)

    return {
        "datasources": [
            {
                "uuid": str(choice.get("uuid") or ""),
                "label": str(choice.get("name") or ""),
                "detail": str(choice.get("db_type") or ""),
                "disabled_reason": (
                    "" if choice.get("is_active", True)
                    else "This datasource is switched off in Data Sources."
                ),
            }
            for choice in datasources
        ],
        "tool_configs": [
            {
                "uuid": str(view.get("uuid") or ""),
                "label": str(view.get("tool_name") or ""),
                "detail": str(view.get("datasource_name") or ""),
                "disabled_reason": (
                    "" if view.get("is_enabled", True)
                    else "This tool config is switched off."
                ),
            }
            for view in tool_configs
        ],
        "data_agents": [
            {
                "uuid": str(view.get("uuid") or ""),
                "label": str(view.get("name") or ""),
                "detail": "",
                "disabled_reason": (
                    "" if view.get("is_active", True)
                    else "This data agent is switched off."
                ),
            }
            for view in agents
        ],
        # What an Email node's property panel picks from. Sent with the rest of the
        # options rather than fetched separately, so the panel can draw one binding row per
        # declared variable the instant a template is chosen — a second round trip would let
        # somebody save the node before its bindings had loaded.
        #
        # Scoped to the graph's own workspace: a template shared into a different one is
        # listed with a reason rather than dropped, the same rule the lists above follow.
        "email_templates": await email_template_service.choices_for_workspace(
            db, user_id, graph.workspace_id,
        ),
        "smtp_configs": await email_smtp_service.choices(db, user_id),
        "human_expects": sorted(HUMAN_EXPECTS_VALUES),
        "error": None,
    }


# --------------------------------------------------------------------------
# Write
# --------------------------------------------------------------------------

async def create_graph(
    db: AsyncSession,
    user_id: int,
    name: str,
    description: Optional[str] = None,
) -> ToolGraph:
    """Create a draft graph, opened on a canvas holding one Start node."""
    await _require_unused_name(db, user_id, name)

    return await graph_crud.create(db, {
        "user_id": user_id,
        "name": _validated_name(name),
        "description": (description or "").strip() or None,
        "graph_data": json.loads(json.dumps(_DEFAULT_GRAPH)),
        "is_active": False,
    })


async def rename_graph(
    db: AsyncSession,
    user_id: int,
    graph_id: uuid_pkg.UUID,
    name: str,
    description: Optional[str] = None,
) -> ToolGraph:
    """Rename a graph and edit the description a model reads when it is attached."""
    graph = await get_graph(db, user_id, graph_id)
    await _require_unused_name(db, user_id, name, exclude_id=graph.id)

    return await graph_crud.update(db, graph.id, {
        "name": _validated_name(name),
        "description": (description or "").strip() or None,
    })


async def update_graph(
    db: AsyncSession,
    user_id: int,
    graph_id: uuid_pkg.UUID,
    name: str,
    description: Optional[str] = None,
    agent_id: Optional[uuid_pkg.UUID] = None,
    workspace_id: Optional[uuid_pkg.UUID] = None,
    allow_recursive_aggregate: bool = False,
) -> ToolGraph:
    """
    Everything the library's edit form can change, in one call: the name, the
    description a model reads, who may call the graph, and whether an agent may read
    its whole result and filter it.

    Composed from :func:`rename_graph`, :func:`attach_graph` and :func:`share_graph`
    rather than writing the columns itself, so the rules each of those enforces —
    name uniqueness, the draft refusal, the tool-name collision, the unique agent slot —
    hold on this path too and can only be stated once.

    Three decisions worth knowing about:

    * **The rename happens before the attachment.** ``attach_graph`` checks whether the
      graph's name collides with another tool on the destination, and that check has to
      see the name the operator just typed rather than the one being replaced.
    * **An unchanged attachment is not rewritten.** Unpublishing leaves an attachment in
      place, so a draft can legitimately be attached; re-submitting that state through
      the form must not trip the draft refusal when the operator only edited the name.
    * **A draft is refused before anything is written**, duplicating the sentence
      ``attach_graph`` would produce, because the alternative is committing the rename
      and then refusing — a half-applied form is worse than a rejected one.

    ``allow_recursive_aggregate`` is written with the rename rather than through a write
    path of its own, because unlike the two attachments it has nothing to refuse: it is a
    property of this graph alone, it collides with nothing, and it changes what an agent
    may *ask* of the result rather than who may ask. See
    ``documentations/AGENT_RECURSIVE_DATAFRAMES.md``.
    """
    graph = await get_graph(db, user_id, graph_id)

    if agent_id is not None and workspace_id is not None:
        raise HTTPException(
            status_code=400,
            detail=(
                "A graph is callable through one data agent or one workspace, not "
                "both — an agent cannot be given the same tool twice. Pick whichever "
                "you meant and leave the other blank."
            ),
        )

    wanted = (
        str(agent_id) if agent_id else "",
        str(workspace_id) if workspace_id else "",
    )
    current = (
        await data_agent_service.get_agent_public_id(db, user_id, graph.data_agent_id),
        await workspace_service.get_workspace_public_id(db, user_id, graph.workspace_id),
    )

    if wanted != current and any(wanted) and not graph.is_active:
        raise HTTPException(
            status_code=400,
            detail=(
                f"The graph '{graph.name}' is still a draft. Publish it before making "
                "it callable by a data agent or a workspace."
            ),
        )

    await rename_graph(db, user_id, graph_id, name, description)
    await graph_crud.update(db, graph.id, {
        "allow_recursive_aggregate": bool(allow_recursive_aggregate),
    })

    if wanted == current:
        return await get_graph(db, user_id, graph_id)

    if agent_id is not None:
        return await attach_graph(db, user_id, graph_id, agent_id)

    if workspace_id is not None:
        return await share_graph(db, user_id, graph_id, workspace_id)

    # Neither: clear both, because each write path only clears its own column and
    # "callable by nobody" has to mean both are gone.
    await attach_graph(db, user_id, graph_id, None)
    return await share_graph(db, user_id, graph_id, None)


async def save_graph(
    db: AsyncSession,
    user_id: int,
    graph_id: uuid_pkg.UUID,
    graph_data: dict,
) -> ToolGraph:
    """
    Replace a graph's drawing, whole, after checking it is one.

    Replaced rather than merged for the reason ``ChatbotFlow.graph_data``'s column
    comment gives: the canvas holds the entire document and posts the entire document,
    so a merge would have to guess which of two versions of a node the user meant.
    """
    graph = await get_graph(db, user_id, graph_id)
    validate_graph(graph_data)

    return await graph_crud.update(db, graph.id, {"graph_data": graph_data})


async def set_graph_active(
    db: AsyncSession,
    user_id: int,
    graph_id: uuid_pkg.UUID,
    is_active: bool,
) -> ToolGraph:
    """
    Publish or unpublish.

    Publishing **validates the drawing**, which unpublishing does not: an active graph
    attached to an agent is callable by a model, and a graph that cannot compile would
    fail inside somebody's conversation. A draft is allowed to be broken — that is
    what a draft is for.

    Unpublishing leaves any attachment in place. The agent simply stops calling it,
    because ``fetch_agent_graph`` requires both switches, so a graph can be parked
    mid-edit without being detached.
    """
    graph = await get_graph(db, user_id, graph_id)

    if is_active:
        validate_graph(graph.graph_data)
    else:
        # A tool config embedding this graph uses it as a filter, and an unpublished
        # graph would stop supplying one — so that tool would start returning more rows
        # than it should, silently. Refused here rather than at the next call, where the
        # only audience is a visitor who cannot act on it.
        await tool_chain_service.require_graph_not_embedded(
            db, graph, "cannot be made a draft",
        )

    return await graph_crud.update(db, graph.id, {"is_active": is_active})


async def attach_graph(
    db: AsyncSession,
    user_id: int,
    graph_id: uuid_pkg.UUID,
    agent_id: Optional[uuid_pkg.UUID],
) -> ToolGraph:
    """
    Point one data agent at this graph — the single write path for the attachment.

    ``agent_id=None`` detaches. Whatever the agent currently holds is released first,
    because ``tool_graphs.data_agent_id`` is unique: an agent has at most one graph and
    a graph runs on at most one agent. The released graph stays in the library.

    **Attaching also un-shares.** The two attachments are mutually exclusive — see the
    module docstring — and clearing rather than refusing is what the operator meant by
    pressing this control while the other was set.

    A draft is refused rather than attached-and-ignored. Attaching a draft would
    silently do nothing — ``fetch_agent_graphs`` filters on ``is_active`` — and a
    control that appears to work and does not is worse than one that says no.
    """
    graph = await get_graph(db, user_id, graph_id)

    if agent_id is None:
        graph.data_agent_id = None
        await db.commit()
        await db.refresh(graph)
        return graph

    if not graph.is_active:
        raise HTTPException(
            status_code=400,
            detail=(
                f"The graph '{graph.name}' is still a draft. Publish it before "
                "attaching it to a data agent."
            ),
        )

    # Ownership of the agent is the agent service's to check, not this module's.
    agent = await data_agent_service.get_data_agent(db, user_id, agent_id)

    await _require_unique_graph_tool_name(db, graph, agent_id=agent.id)

    held = await graph_crud.get_one(db, filters={"data_agent_id": agent.id})

    if held is not None and held.id != graph.id:
        # Free the unique slot before the new graph claims it. Without the flush the
        # two writes race inside one transaction and Postgres refuses the second.
        held.data_agent_id = None
        await db.flush()

    graph.data_agent_id = agent.id
    graph.workspace_id = None
    await db.commit()
    await db.refresh(graph)
    return graph


async def share_graph(
    db: AsyncSession,
    user_id: int,
    graph_id: uuid_pkg.UUID,
    workspace_id: Optional[uuid_pkg.UUID],
) -> ToolGraph:
    """
    Put this graph on a workspace's shelf — the single write path for sharing.

    Every data agent assigned to that workspace picks it up as a tool, so an operator
    adding a fourth agent to the team does not have to remember to attach anything: the
    agent inherits the shelf. That is the whole difference from :func:`attach_graph`, and
    it is why the workspace column is not unique — a shelf holds several.

    ``workspace_id=None`` un-shares. **Sharing also detaches**, for the reason in the
    module docstring.

    A draft is refused for the same reason attaching one is: ``fetch_agent_graphs``
    filters on ``is_active``, so it would be a control that appears to work and does not.

    The tool-name check is what makes a shelf usable. Two graphs whose names reduce to
    the same identifier — "Monthly revenue" and "monthly-revenue" both become
    ``monthly_revenue`` — would give every agent in the workspace two tools of one name,
    and a model asked to pick between them cannot. Refused here, where the second graph's
    name can be quoted, rather than discovered as an agent behaving oddly.
    """
    graph = await get_graph(db, user_id, graph_id)

    if workspace_id is None:
        graph.workspace_id = None
        await db.commit()
        await db.refresh(graph)
        return graph

    if not graph.is_active:
        raise HTTPException(
            status_code=400,
            detail=(
                f"The graph '{graph.name}' is still a draft. Publish it before "
                "sharing it with a workspace."
            ),
        )

    # Ownership of the workspace is the workspace service's to check, not this module's.
    workspace = await workspace_service.get_workspace(db, user_id, workspace_id)

    await _require_unique_graph_tool_name(db, graph, workspace_id=workspace.id)

    graph.workspace_id = workspace.id
    graph.data_agent_id = None
    await db.commit()
    await db.refresh(graph)
    return graph


async def _require_unique_graph_tool_name(
    db: AsyncSession,
    graph: ToolGraph,
    agent_id: Optional[int] = None,
    workspace_id: Optional[int] = None,
) -> None:
    """
    Refuse an attachment that would give one agent two tools of the same name.

    A graph's tool name is derived from the name a person wrote, so two graphs can
    reduce to one identifier without looking alike — ``_graph_tool_name`` lowercases and
    collapses every non-alphanumeric character, which is what makes "Monthly revenue"
    and "monthly-revenue" collide. A model handed two tools called ``monthly_revenue``
    has no way to choose, and nothing about the answer it gives would say why.

    Both directions are checked from the *destination*, because that is where the
    collision lives:

    * sharing with a workspace — against every other graph on that shelf, **draft
      included**. A draft that collides becomes a live collision the moment somebody
      presses Publish, and refusing it then, from a different control, is how a refusal
      ends up looking arbitrary.
    * attaching to an agent — against the shelf of the workspace that agent is assigned
      to, because the agent will hold both.

    Imported inside the function for the reason ``_graph_entries`` documents in the other
    direction: this module and ``prompt_sync_service`` reach each other through
    ``query_executor``, so a module-scope import is a cycle.
    """
    from app.db.graph_designer.queries import fetch_workspace_graphs
    from app.services.deep_agents.prompt_sync_service import _graph_tool_name

    shelf_id = workspace_id

    if shelf_id is None and agent_id is not None:
        agent = await agent_crud.get_one(db, filters={"id": agent_id})
        shelf_id = getattr(agent, "workspace_id", None) if agent else None

    if not shelf_id:
        return

    wanted = _graph_tool_name(graph.name)

    for other in await fetch_workspace_graphs(db, shelf_id):
        if other.id == graph.id or _graph_tool_name(other.name) != wanted:
            continue

        raise HTTPException(
            status_code=400,
            detail=(
                f"'{other.name}' is already shared with this workspace and both "
                f"names become the same tool name ('{wanted}'). Rename one of them "
                "first — an agent cannot be given two tools with one name."
            ),
        )


async def delete_graph(
    db: AsyncSession,
    user_id: int,
    graph_id: uuid_pkg.UUID,
) -> None:
    """
    Delete a graph, its runs and their steps.

    The runs go with it by cascade, which is right: a run log describes a drawing, and
    a log of a graph nobody can look at any more is not an audit trail, it is orphaned
    rows. An attached agent simply loses the tool — the FK is on this side.

    A graph **embedded in a tool config** is refused instead. That is the one dependency
    a cascade would resolve wrongly: the link would go, and the tool that had been
    filtering on it would carry on running and quietly return more rows than it should.
    """
    graph = await get_graph(db, user_id, graph_id)

    await tool_chain_service.require_graph_not_embedded(
        db, graph, "cannot be deleted",
    )

    await graph_crud.delete(db, graph.id)


# --------------------------------------------------------------------------
# Validation — the node vocabulary
# --------------------------------------------------------------------------

def validate_graph(graph_data: Any) -> None:
    """
    Refuse a drawing that is not a runnable graph, naming the node at fault.

    Public because three callers need exactly this and must not disagree: the save,
    the publish, and ``graph_run_service`` before it compiles. A run that validated
    more loosely than the save would be a run of a graph the author could not have
    stored.

    Every message names the node the way the canvas labels it, because the person
    reading this is looking at the drawing.
    """
    if not isinstance(graph_data, dict):
        raise HTTPException(
            status_code=400, detail="That graph could not be read.",
        )

    nodes = graph_data.get("nodes")
    edges = graph_data.get("edges")

    if not isinstance(nodes, list) or not isinstance(edges, list):
        raise HTTPException(
            status_code=400,
            detail="A graph must have a list of nodes and a list of connections.",
        )

    if not nodes:
        raise HTTPException(
            status_code=400, detail="A graph needs at least one node.",
        )

    node_by_id = _indexed_nodes(nodes)

    starts = [node for node in nodes if node.get("type") == NODE_START]

    if len(starts) != 1:
        raise HTTPException(
            status_code=400,
            detail=(
                "A graph must have exactly one Start node — it is where the run "
                f"begins, and this one has {len(starts)}."
            ),
        )

    for node in nodes:
        _validate_node(node, node_by_id)

    _validate_edges(edges, node_by_id)
    _require_bounded_cycles(nodes, edges, node_by_id)
    _require_looping_bodies(nodes, edges)
    _require_unions_in_loop_bodies(nodes, edges)
    _require_collected_nodes_in_body(nodes, edges, node_by_id)
    _require_timers_started(nodes, edges, node_by_id)


def _indexed_nodes(nodes: Sequence[Any]) -> Dict[str, dict]:
    """Every node by id, refusing a duplicate or a missing one."""
    indexed: Dict[str, dict] = {}

    for node in nodes:
        if not isinstance(node, dict):
            raise HTTPException(
                status_code=400, detail="One of the nodes could not be read.",
            )

        node_id = str(node.get("id") or "").strip()

        if not node_id:
            raise HTTPException(
                status_code=400,
                detail="Every node needs an id, and one of them has none.",
            )

        if node_id in indexed:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Two nodes share the id '{node_id}'. Every connection names a "
                    "node by its id, so a duplicate makes the run ambiguous."
                ),
            )

        indexed[node_id] = node

    return indexed


def binding_of(raw: Any) -> Optional[dict]:
    """
    One parameter's wiring, in the shape every reader of it expects.

    Two stored shapes, because the first graphs were saved before a binding could say
    anything but which node to read::

        "n_4"                                        -> one value, the whole output
        {"node": "n_4", "field": "id", "mode": "one"}

    Normalised in **one** place so nothing downstream has to know there were ever two. A
    plain string is read as ``mode: "one"`` with no field, which is exactly what it used
    to mean — a graph saved last week keeps binding what it bound.

    Returns ``None`` for a binding that names no node, so a caller can treat "not wired"
    and "wired to nothing" alike; they are the same thing to everything that follows.

    Lives here, with the rest of the node vocabulary, because three callers need the same
    answer: this module's validation, ``node_runners`` when it fills a parameter, and
    ``referenced_nodes`` when it works out what a selection has to cover. A second reader
    is how the two shapes would drift apart.
    """
    if isinstance(raw, str):
        node = raw.strip()
        return {"node": node, "field": "", "mode": BINDING_MODE_ONE} if node else None

    if not isinstance(raw, Mapping):
        return None

    node = str(raw.get("node") or "").strip()
    if not node:
        return None

    mode = str(raw.get("mode") or "").strip() or BINDING_MODE_ONE

    return {
        "node": node,
        "field": str(raw.get("field") or "").strip(),
        # An unrecognised mode is read as `one` rather than refused here: the save
        # refuses it, and a runner is not the place to fail a graph over a value that
        # could only have got there by hand.
        "mode": mode if mode in BINDING_MODE_VALUES else BINDING_MODE_ONE,
    }


def bindings_of(data: Mapping[str, Any]) -> Dict[str, dict]:
    """Every declared wiring on a node, keyed by parameter name."""
    raw = data.get("bindings")
    if not isinstance(raw, Mapping):
        return {}

    resolved: Dict[str, dict] = {}

    for name, entry in raw.items():
        binding = binding_of(entry)
        if binding:
            resolved[str(name)] = binding

    return resolved


def node_label(node: dict) -> str:
    """
    What to call a node in a message or a log row.

    The author's own label if they set one, otherwise the type's name — never the
    generated id, which means nothing to the person reading it.
    """
    data = node.get("data") or {}
    label = str(data.get("label") or "").strip()

    if label:
        return label

    return NODE_TYPE_LABELS.get(str(node.get("type")), str(node.get("type") or "node"))


def _validate_node(node: dict, node_by_id: Dict[str, dict]) -> None:
    """One node's own data, dispatched on its type."""
    node_type = str(node.get("type") or "")
    label = node_label(node)

    if node_type not in NODE_TYPE_VALUES:
        raise HTTPException(
            status_code=400,
            detail=f"'{label}' has a node type this application does not know.",
        )

    data = node.get("data") or {}

    if not isinstance(data, dict):
        raise HTTPException(
            status_code=400, detail=f"The settings on '{label}' could not be read.",
        )

    # Before the type dispatch, deliberately. A `{{TABLE}}` the author forgot to declare
    # should be reported as that, and not as whatever `validated_tool_sql` makes of a
    # statement with braces in it.
    _validate_variables(node, label, node_by_id)

    if node_type == NODE_SQL:
        _validate_sql_node(data, label, node_by_id)
    elif node_type == NODE_SQL_UNION:
        _validate_sql_union_node(data, label, node_by_id)
    elif node_type == NODE_VALUE:
        _validate_value_node(data, label)
    elif node_type == NODE_TOOL_CONFIG:
        _validate_tool_config_node(data, label)
    elif node_type == NODE_HUMAN:
        _validate_human_node(data, label)
    elif node_type == NODE_BRANCH:
        _validate_branch_node(data, label, node_by_id)
    elif node_type in LOOP_NODE_TYPES:
        _validate_loop_node(node_type, data, label, node_by_id)
    elif node_type == NODE_EMAIL:
        _validate_email_node(data, label)
    elif node_type == NODE_CREATE_FILE:
        _validate_create_file_node(data, label, node_by_id)
    elif node_type == NODE_DOWNLOAD_FILE:
        _validate_download_file_node(data, label, node_by_id)
    elif node_type == NODE_TIMER:
        _validate_timer_node(data, label, node_by_id)
    elif node_type == NODE_WAIT:
        _validate_wait_node(data, label)


def _validate_variables(node: dict, label: str, node_by_id: Dict[str, dict]) -> None:
    """
    One node's own ``{{VARIABLE}}`` declarations, for every node type.

    Offline like every other validator here — ``validate_graph`` runs on save, publish
    **and** run, so a database read in it would slow all three and make the rules
    untestable without a session.

    Delegated to ``node_variables`` rather than written out, because the same rules have
    to hold when the node runs and two copies is how a form and a runner drift apart.
    """
    from app.services.graph_designer import node_variables

    node_variables.assert_valid(node, label, node_by_id)


def _validate_create_file_node(
    data: dict, label: str, node_by_id: Dict[str, dict],
) -> None:
    """
    A Create File node: a format, and an earlier node whose output holds the rows.

    The formats and the sources come from the file module rather than being restated here,
    so a node this accepts cannot be one the runner refuses — the arrangement
    :func:`_validate_email_node` states. A graph offers exactly one source: an earlier
    node's output. There is no chat session and no agent here, and a literal is not a
    dataset.

    The **path** is checked for shape with the same restricted reader the email bindings
    use, so ``rows[0]..id`` is refused at the keyboard rather than at run time. What is not
    checked is whether that path will find anything — that is a fact about somebody's data,
    not about the drawing, and it is a run-time refusal down ``error`` naming the node.
    """
    from app.services.file_delivery.nodes.graph_designer_runner import GRAPH_DATA_SOURCES
    from app.services.integrations.mapping import paths
    from app.services.integrations.mapping.paths import PathError

    if str(data.get("file_format") or "").strip() not in FILE_FORMAT_VALUES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"'{label}' has no file format chosen — CSV, Excel, Text or Parquet."
            ),
        )

    source_data = data.get("data")

    if not isinstance(source_data, dict):
        raise HTTPException(
            status_code=400, detail=f"The data source on '{label}' could not be read.",
        )

    source = str(source_data.get("source") or "").strip().lower()

    if source not in GRAPH_DATA_SOURCES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"'{label}' reads its data from '{source}', which is not available in a "
                "graph. Point it at an earlier node's output."
            ),
        )

    target = str(source_data.get("source_node") or "").strip()

    if not target:
        raise HTTPException(
            status_code=400,
            detail=(
                f"'{label}' does not say which node's rows to write. Choose an earlier "
                "node."
            ),
        )

    if target not in node_by_id:
        raise HTTPException(
            status_code=400,
            detail=f"'{label}' points at a node that is not on this graph.",
        )

    path = str(source_data.get("path") or "").strip()

    if path and not paths.is_valid(path):
        try:
            paths.parse(path)
        except PathError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"The path on '{label}' could not be read: {exc}",
            ) from exc


def _validate_download_file_node(
    data: dict, label: str, node_by_id: Dict[str, dict],
) -> None:
    """
    A Download File node: a Create File node on this graph, and no chat-only settings.

    **The button fields are refused rather than ignored.** There is no visitor and no chat
    in a graph run, so a colour and a label here would be settings an author chose and this
    application silently dropped — which is worse than not offering them. The canvas does
    not draw them for this node type; this is what makes that true for a graph saved by
    other means.

    A node id rather than a typed-in name, for the reason ``_validate_timer_node`` gives:
    nothing offline can prove two boxes spell a name the same way, and a typo would surface
    at run time as "no file", which is indistinguishable from a branch not taken.
    """
    target = str(data.get("create_file_node") or "").strip()

    if not target:
        raise HTTPException(
            status_code=400,
            detail=(
                f"'{label}' does not say which file it hands over. Choose the Create "
                "File node that writes it."
            ),
        )

    named = node_by_id.get(target)

    if named is None or named.get("type") != NODE_CREATE_FILE:
        raise HTTPException(
            status_code=400,
            detail=f"'{label}' must point at a Create File node on this graph.",
        )

    if data.get("show_button") or str(data.get("button_text") or "").strip():
        raise HTTPException(
            status_code=400,
            detail=(
                f"'{label}' has chat button settings, and a pipeline has no chat to show "
                "one in. A Download File node here produces a link on its output — bind "
                "an Email node to it to send the file on."
            ),
        )


def _validate_timer_node(data: dict, label: str, node_by_id: Dict[str, dict]) -> None:
    """
    A Timer node: which of the four things it does, and — for three of them — to which timer.

    The instance set to *start* **is** the timer, so it names nothing. The other three
    act on it and must say which, by node id. A node id rather than a typed-in name
    because a name cannot be checked: nothing offline can prove two boxes spell "Job
    timer" the same way, and a typo would surface at run time as "that timer has not
    been started", which is indistinguishable from a branch not taken.
    """
    action = str(data.get("action") or "").strip().lower()

    if action not in TIMER_ACTION_VALUES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"'{label}' has no action chosen. A Timer node must Start, Pause, "
                "Resume or Stop a timer."
            ),
        )

    target = str(data.get("timer_node") or "").strip()

    if action == TIMER_START:
        if target:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"'{label}' starts a timer, so it cannot also point at one — it "
                    "*is* the timer. Clear the Timer box."
                ),
            )
        return

    if not target:
        raise HTTPException(
            status_code=400,
            detail=(
                f"'{label}' does not say which timer it acts on. Choose the Timer node "
                "that starts it."
            ),
        )

    referenced = node_by_id.get(target)

    if referenced is None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"'{label}' acts on a timer that is no longer in this graph. Choose it "
                "again."
            ),
        )

    started_here = (
        str(referenced.get("type") or "") == NODE_TIMER
        and str((referenced.get("data") or {}).get("action") or "").strip().lower()
        == TIMER_START
    )

    if not started_here:
        raise HTTPException(
            status_code=400,
            detail=(
                f"'{label}' points at '{node_label(referenced)}', which is not a Timer "
                "set to Start. Point it at the Timer node that begins the timing."
            ),
        )


def _validate_wait_node(data: dict, label: str) -> None:
    """
    A Wait node: a number of seconds inside the ceiling.

    The ceiling is enforced here **and** in the runner. There is no ``asyncio.wait_for``
    around a runner in this package, so this number is the only thing bounding how long
    a run can be parked, and a ``graph_data`` row can be edited by hand.
    """
    from app.services.graph_designer import timers

    try:
        timers.validated_wait_seconds(data.get("seconds"), label)
    except timers.TimerError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _validate_email_node(data: dict, label: str) -> None:
    """
    An Email node: a template, a server, and a binding for everything the template needs.

    The bindings are checked against ``GRAPH_BINDING_SOURCES`` — upstream node outputs and
    literals only. A graph has no chat session and no record in hand, and it is not attached
    to a chatbot, so there is no agent whose prompt variables it could read; refusing those
    here means the property panel and the runner agree, and a graph can never execute a
    binding looser than one that could be saved.

    What is **not** checked here is whether the named template and server exist. That needs
    the database, and this function is deliberately synchronous and offline like every other
    validator in this module — ``validate_graph`` is called from save, publish and run, and
    making it do IO would make all three slower and harder to test. A deleted template is
    caught at run time with a sentence naming it.
    """
    from app.services.email_dispatch.nodes.graph_designer_runner import (
        GRAPH_BINDING_SOURCES,
    )
    from app.services.email_dispatch.errors import RenderError
    from app.services.email_dispatch import variable_sources

    if not str(data.get("template_id") or "").strip():
        raise HTTPException(
            status_code=400, detail=f"'{label}' has no email template chosen.",
        )

    if not str(data.get("smtp_config_id") or "").strip():
        raise HTTPException(
            status_code=400, detail=f"'{label}' has no SMTP server chosen.",
        )

    recipients = data.get("recipients") or {}
    if not isinstance(recipients, dict) or not (recipients.get("to") or []):
        raise HTTPException(
            status_code=400,
            detail=(
                f"'{label}' has nobody to email. Add at least one TO address — it may be "
                "a {{VARIABLE}}."
            ),
        )

    bindings = data.get("variable_bindings") or {}
    if not isinstance(bindings, dict):
        raise HTTPException(
            status_code=400,
            detail=f"The variable bindings on '{label}' could not be read.",
        )

    # The template's own declaration is not available offline, so only the *shape* and the
    # source availability are checked here. `assert_bindable`'s declaration checks run again
    # at enqueue, where the template has been read.
    for name, binding in bindings.items():
        if not isinstance(binding, dict):
            raise HTTPException(
                status_code=400,
                detail=f"The binding for {{{{{str(name).upper()}}}}} on '{label}' could not be read.",
            )
        source = str(binding.get("source") or "").strip().lower()
        if source not in GRAPH_BINDING_SOURCES:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{{{{{str(name).upper()}}}}} on '{label}' is bound to '{source}', "
                    "which is not available in a graph. Use an earlier node's output or a "
                    "fixed value."
                ),
            )
        if source == "node" and not str(binding.get("node_id") or "").strip():
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{{{{{str(name).upper()}}}}} on '{label}' is bound to an earlier node "
                    "but no node was chosen."
                ),
            )
        path = str(binding.get("path") or "").strip()
        if path:
            try:
                variable_sources.assert_path(path, name=str(name).upper())
            except RenderError as exc:
                raise HTTPException(
                    status_code=400, detail=f"On '{label}': {exc.message}",
                ) from exc


def _validate_sql_node(
    data: dict,
    label: str,
    node_by_id: Dict[str, dict],
) -> None:
    """
    A SQL node holds a statement, the datasource to run it against, the tables it reads,
    and the parameters its statement asks for.

    The statement goes through ``tool_config_service.validated_tool_sql`` — the same
    function a SQL-mode tool config's statement passes, which composes
    ``utils/sql_guard``. Using it rather than a private copy is what makes "a statement
    that saves here would save as a tool" true rather than approximately true, and it
    means a read-only violation is described in words the operator has seen before.

    **The declared tables are required, and that is not bureaucracy.** Nothing in this
    application parses a raw statement, so ``query_executor.require_active_tables`` can
    only honour the list the operator recorded — that is SQL mode's whole bargain, and
    ``documentations/TOOL_QUERY_MODES.md`` states it. A node with no declared tables
    would run a statement with the active-table check silently skipped, which would
    make a graph a way *around* the Data Sources switches rather than a way to use
    them. ``validated_tables`` is the tool config form's own validator, so the same
    statement declares the same tables in both places.
    """
    if not str(data.get("datasource_id") or "").strip():
        raise HTTPException(
            status_code=400,
            detail=f"'{label}' has no datasource selected, so its query cannot run.",
        )

    sql_query = str(data.get("sql_query") or "").strip()

    if not sql_query:
        raise HTTPException(
            status_code=400, detail=f"'{label}' has no SQL statement.",
        )

    table_names = data.get("table_names")

    if not isinstance(table_names, list) or not [
        name for name in table_names if str(name or "").strip()
    ]:
        raise HTTPException(
            status_code=400,
            detail=(
                f"'{label}' must say which tables its statement reads. Nothing here "
                "reads them out of the SQL, so that list is what lets a table "
                "switched off in Data Sources stop this node."
            ),
        )

    # Both raise HTTPException with a readable sentence of their own, and both are
    # allowed to propagate rather than being caught and rephrased: they are already
    # the messages the operator would get from the tool config form for the same
    # statement and the same tables.
    tool_config_service.validated_tables(table_names)
    tool_config_service.validated_tool_sql(sql_query)

    _refuse_detached_colon(sql_query, label)
    _validate_sql_params(data, label, sql_query, node_by_id)


def _validate_sql_union_node(
    data: dict,
    label: str,
    node_by_id: Dict[str, dict],
) -> None:
    """
    A union node is a SQL node that is copied per pass, so it is checked as one, plus two.

    Everything a ``sql`` node must be, this must be — one read-only statement, a datasource,
    a declared table list, parameters that match the placeholders — and it goes through the
    identical function so the two cannot drift into disagreeing about what a statement is.

    Its own two rules exist because its statement is not run as written but *joined to
    copies of itself*:

    **No ``ORDER BY`` or ``LIMIT``.** Unparenthesised, either one binds to the whole union
    rather than to the member it was written on, so a fragment carrying one would silently
    order or truncate everything. Parentheses would fix that on PostgreSQL and MySQL and are
    invalid around a compound-select operand in SQLite, so the honest answer is to refuse it
    here and say where the ordering belongs instead.

    **No declared parameter ending in the generated suffix.** Pass 7's ``:id`` becomes
    ``:id__p7``; a parameter the author called ``id__p7`` would collide with it, and one
    pass would be bound with another pass's value — a result that looks entirely normal.

    Whether the node sits inside a loop at all is :func:`_require_unions_in_loop_bodies`'
    question: it is a fact about the edges, and this function is handed only the nodes.
    """
    _validate_sql_node(data, label, node_by_id)

    sql_query = str(data.get("sql_query") or "").strip()

    _refuse_clause_belonging_to_the_whole_union(sql_query, label)

    for entry in data.get("params") or []:
        name = str((entry or {}).get("param") or "").strip()

        if _GENERATED_SUFFIX_PATTERN.search(name):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"'{label}' declares a parameter called '{name}'. Names ending in "
                    "'__p' and a number are how each pass's copy of this statement is kept "
                    "apart, so this one would collide with a generated name and a pass "
                    "would be filled with another pass's value. Rename it."
                ),
            )


def _refuse_clause_belonging_to_the_whole_union(sql_query: str, label: str) -> None:
    """
    Refuse a fragment whose ``ORDER BY`` or ``LIMIT`` would apply to the entire union.

    Read at bracket depth zero — ``sql_guard.paren_depths`` — so the same clause
    inside a subquery or a window function, where it is local and correct, is left alone.
    """
    bare = stripped_literals(normalised_sql(sql_query))
    depths = paren_depths(bare)

    for pattern, clause in ((_ORDER_BY_PATTERN, "ORDER BY"), (_LIMIT_PATTERN, "LIMIT")):
        if at_depth_zero(pattern, bare, depths):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"The statement on '{label}' ends with {clause}, which in a union "
                    f"applies to every pass at once rather than to this one. Take it out — "
                    "a node after the 'execute' output is where the whole result is sorted "
                    "or cut."
                ),
            )


def _refuse_detached_colon(sql_query: str, label: str) -> None:
    """
    Refuse ``= : item`` — a placeholder whose colon has a space after it.

    Checked before the parameters are, because the space hides the placeholder from the
    two checks that would otherwise catch it: ``:name`` is what they look for, and this is
    not that. So the statement passes both, saves, and fails against the database with the
    dialect's own message quoting the fragment. That is the failure this feature was built
    to remove, and it is the one mistake an author makes when there is nowhere to put a
    value — which is what the parameters editor now is.

    Only here, not in ``tool_config_service.validated_tool_sql``: that function is also
    what re-checks a *stored* statement before a tool runs, so a rule added there could
    stop an existing tool mid-run rather than at a form. Its docstring says syntax is not
    checked and cannot honestly be, and that promise is worth keeping.
    """
    adrift = spaced_placeholder(sql_query)

    if adrift:
        raise HTTPException(
            status_code=400,
            detail=(
                f"'{label}' has a space after the ':' in ': {adrift}', so the database "
                f"reads it as a stray colon rather than a value. Write it as ':{adrift}' "
                "and declare it in the parameter list."
            ),
        )


def _validate_sql_params(
    data: dict,
    label: str,
    sql_query: str,
    node_by_id: Dict[str, dict],
) -> None:
    """
    The parameters a SQL node declares, and where each one's value comes from.

    ``tool_config_service.validated_sql_params`` does the declarations themselves — the
    name, the type, the duplicate check, and the refusal of a parameter the statement
    never mentions. Reused rather than re-implemented for the same reason the statement
    and the table list are: it is the same declaration, in the same shape, checked by the
    same words the tool config form uses.

    What is checked here is the part that is this feature's own: which node fills each
    parameter, and whether the statement is written for the shape that node supplies.
    """
    declared = tool_config_service.validated_sql_params(data.get("params"), sql_query) or []
    names = {str(entry["param"]) for entry in declared}

    bindings = bindings_of(data)

    for name, binding in bindings.items():
        if name not in names:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"'{label}' wires a value into ':{name}', which it does not declare "
                    "as a parameter. Add it to the parameter list, or remove the wiring."
                ),
            )

        if binding["node"] not in node_by_id:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"The value for ':{name}' on '{label}' comes from a node that is "
                    "not in this graph."
                ),
            )

        _require_binding_arity(name, binding, sql_query, label)

    _require_every_placeholder_declared(declared, sql_query, label)


def _require_binding_arity(
    name: str,
    binding: dict,
    sql_query: str,
    label: str,
) -> None:
    """
    Refuse a parameter written in a shape its binding cannot take.

    An expanding bind parameter always renders parenthesised, so ``id = :x`` given a list
    becomes ``id = (?, ?, ?)`` and ``id IN :x`` given one value becomes ``id IN ?``. Both
    are syntax errors, and both are errors the *database* reports — mid-run, in the dock,
    long after the form that caused them was closed.

    The identical check a nested tool config gets
    (``tool_chain_service._require_placeholder_arity``); both now read the shape from
    ``sql_guard.placeholder_shape``. A statement where the placeholder is in neither shape
    is left alone: it may be a function argument, and guessing there would refuse
    something that works.
    """
    shape = placeholder_shape(sql_query, name)
    wants_list = binding["mode"] == BINDING_MODE_IN_LIST

    if wants_list and shape == PLACEHOLDER_SINGLE:
        raise HTTPException(
            status_code=400,
            detail=(
                f"'{label}' compares against ':{name}' as a single value, but the node "
                f"wired to it supplies a list. Either write it as 'IN :{name}', or set "
                "the parameter to take one value."
            ),
        )

    if not wants_list and shape == PLACEHOLDER_LIST:
        raise HTTPException(
            status_code=400,
            detail=(
                f"'{label}' uses 'IN :{name}', which expects a list, but the parameter "
                f"is set to take one value. Either compare it directly — '= :{name}' — "
                "or set the parameter to take all the values as a list."
            ),
        )


def _require_every_placeholder_declared(
    declared: List[dict],
    sql_query: str,
    label: str,
) -> None:
    """
    Every ``:name`` in the statement must be declared as a parameter.

    The opposite direction to ``validated_sql_params``, which refuses a *declaration* the
    statement never uses. This one refuses a *placeholder* with no declaration, and it has
    to exist separately for the reason ``tool_chain_service`` gives for its own pair: one
    is a name that goes nowhere, the other is a statement that cannot run, and a single
    check would let each side assume the other had covered it.

    Unrunnable is the point. A binding fills a parameter, and a parameter is what a
    declaration creates — so an undeclared ``:name`` is bound by nothing, and SQLAlchemy
    raises about a missing parameter mid-run, in the dock, naming nothing the author would
    recognise.

    Note what is deliberately **not** refused: a declared parameter with no wiring. That
    is not a mistake — it is how a value reaches a graph from outside. The run's ``inputs``
    fill it, whether from the test panel or from a data agent calling the graph as a tool,
    and ``graph_tool_factory`` builds the tool's arguments out of exactly these
    declarations. Refusing an unwired parameter would refuse the whole agent-callable path.
    """
    names = {str(entry["param"]) for entry in declared}
    undeclared = sorted(bind_placeholders(sql_query) - names)

    if undeclared:
        raise HTTPException(
            status_code=400,
            detail=(
                f"'{label}' uses "
                + ", ".join(f"':{name}'" for name in undeclared)
                + ", which nothing fills. Add each one to the parameter list, or take it "
                "out of the statement."
            ),
        )


def _validate_value_node(data: dict, label: str) -> None:
    """
    A value node holds JSON, and its kind decides what shape that JSON may be.

    The three kinds validate differently on purpose — see the note on ``VALUE_KINDS``.
    A ``dict`` where a ``list`` was promised is the failure this catches, and it would
    otherwise surface as a downstream ``IN`` comparison built from an object.
    """
    kind = str(data.get("value_kind") or "").strip()

    if kind not in VALUE_KIND_VALUES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"'{label}' must say whether it holds a list, an array or a "
                "dictionary."
            ),
        )

    parsed = _parsed_value(data, label)

    if kind == VALUE_KIND_DICT and not isinstance(parsed, dict):
        raise HTTPException(
            status_code=400,
            detail=f"'{label}' is set to hold a dictionary, but its value is not one.",
        )

    if kind in (VALUE_KIND_LIST, VALUE_KIND_ARRAY) and not isinstance(parsed, list):
        raise HTTPException(
            status_code=400,
            detail=f"'{label}' is set to hold a {kind}, but its value is not a list.",
        )

    if kind == VALUE_KIND_LIST and any(
        isinstance(entry, (list, dict)) for entry in parsed
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                f"'{label}' is a list of values, so its entries must be plain values. "
                "Use the Array kind for nested entries."
            ),
        )


def _parsed_value(data: dict, label: str) -> Any:
    """
    A value node's JSON, parsed.

    Accepts an already-parsed object as well as the string the canvas posts, because
    the canvas keeps the text the user typed and a graph built by a test does not.
    """
    raw = data.get("value_json")

    if isinstance(raw, (list, dict)):
        return raw

    text = str(raw or "").strip()

    if not text:
        raise HTTPException(
            status_code=400, detail=f"'{label}' has no value.",
        )

    try:
        return json.loads(text)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"The value on '{label}' is not valid JSON.",
        ) from exc


def _validate_tool_config_node(data: dict, label: str) -> None:
    """
    A tool-config node names an existing tool by its public uuid.

    That the uuid *resolves*, and that the caller owns it, is checked when the run
    starts rather than here — it needs the database, and a tool deleted after the
    graph was saved has to be reported as a failed step rather than as a graph that
    can no longer be opened.
    """
    if not str(data.get("tool_config_id") or "").strip():
        raise HTTPException(
            status_code=400, detail=f"'{label}' has no tool config selected.",
        )


def _validate_human_node(data: dict, label: str) -> None:
    """A human node needs a question, and a kind of answer the dock can render."""
    if not str(data.get("prompt") or "").strip():
        raise HTTPException(
            status_code=400,
            detail=f"'{label}' has no question to ask, so the run would pause silently.",
        )

    expects = str(data.get("expects") or "").strip()

    if expects not in HUMAN_EXPECTS_VALUES:
        raise HTTPException(
            status_code=400,
            detail=f"'{label}' must say what kind of answer it expects.",
        )

    if expects == "choice" and not _choices_of(data):
        raise HTTPException(
            status_code=400,
            detail=(
                f"'{label}' asks the user to pick one of a list, but the list is "
                "empty."
            ),
        )


def _choices_of(data: dict) -> List[str]:
    """A human node's offered answers, trimmed and de-blanked."""
    raw = data.get("choices")
    entries = raw if isinstance(raw, list) else []
    return [str(entry).strip() for entry in entries if str(entry).strip()]


def _validate_branch_node(
    data: dict,
    label: str,
    node_by_id: Dict[str, dict],
) -> None:
    """
    A branch holds an ordered list of conditions, each owning one output port.

    Conditions are **compared, never evaluated**. Every operator is a name from
    ``CONDITION_OPERATORS`` and the comparison is done in Python by
    ``node_runners``; there is no expression language here and nothing is passed to
    ``eval``. That is the same decision ``engine_service._evaluate_condition`` made.
    """
    conditions = data.get("conditions")

    if not isinstance(conditions, list) or not conditions:
        raise HTTPException(
            status_code=400,
            detail=(
                f"'{label}' has no conditions, so nothing after it could ever run."
            ),
        )

    seen_ports: Set[str] = set()

    for index, condition in enumerate(conditions, start=1):
        if not isinstance(condition, dict):
            raise HTTPException(
                status_code=400,
                detail=f"Condition {index} on '{label}' could not be read.",
            )

        operator = str(condition.get("operator") or "").strip()

        if operator not in CONDITION_OPERATOR_VALUES:
            raise HTTPException(
                status_code=400,
                detail=f"Condition {index} on '{label}' has an operator we don't know.",
            )

        source = str(condition.get("source_node") or "").strip()

        if source not in node_by_id:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Condition {index} on '{label}' reads a node that is not in this "
                    "graph."
                ),
            )

        port = str(condition.get("port") or "").strip()

        if not port:
            raise HTTPException(
                status_code=400,
                detail=f"Condition {index} on '{label}' has no outcome to connect to.",
            )

        if port == PORT_ELSE:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Condition {index} on '{label}' cannot be called '{PORT_ELSE}' — "
                    "that name is reserved for the fall-through, which would then "
                    "never be taken."
                ),
            )

        if port in seen_ports:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Two conditions on '{label}' share the outcome '{port}', so only "
                    "one of them could ever be connected."
                ),
            )

        seen_ports.add(port)


def _validate_loop_node(
    node_type: str,
    data: dict,
    label: str,
    node_by_id: Dict[str, dict],
) -> None:
    """
    A loop needs something to loop over, and a ceiling.

    ``for_each`` reads a list from another node. ``do_until`` needs a condition, or it
    is a loop with no way out — which is precisely the shape the cycle rule below
    exists to keep from compiling.
    """
    max_iterations = data.get("max_iterations", DEFAULT_MAX_ITERATIONS)

    try:
        ceiling = int(max_iterations)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=400,
            detail=f"The iteration limit on '{label}' must be a whole number.",
        ) from exc

    if ceiling < 1:
        raise HTTPException(
            status_code=400,
            detail=f"The iteration limit on '{label}' must be at least 1.",
        )

    if ceiling > ABSOLUTE_MAX_ITERATIONS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"The iteration limit on '{label}' is {ceiling}, which is more than "
                f"the {ABSOLUTE_MAX_ITERATIONS} one run may go round."
            ),
        )

    if node_type == NODE_FOR_EACH:
        source = str(data.get("source_node") or "").strip()

        if source not in node_by_id:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"'{label}' loops over another node's result, and the node it "
                    "names is not in this graph."
                ),
            )

        _validate_collection(data, label, node_by_id)
        return

    _refuse_collection_on(data, label)

    condition = data.get("condition")

    if not isinstance(condition, dict):
        raise HTTPException(
            status_code=400,
            detail=f"'{label}' has no condition, so the loop would never end.",
        )

    if str(condition.get("operator") or "") not in CONDITION_OPERATOR_VALUES:
        raise HTTPException(
            status_code=400,
            detail=f"The condition on '{label}' has an operator we don't know.",
        )

    if str(condition.get("source_node") or "") not in node_by_id:
        raise HTTPException(
            status_code=400,
            detail=(
                f"The condition on '{label}' reads a node that is not in this graph."
            ),
        )


def _validate_collection(
    data: dict,
    label: str,
    node_by_id: Dict[str, dict],
) -> None:
    """
    What a ``for_each`` unions, and what it records each row against.

    Only the node's *existence* is checked here, not that it sits inside this loop's
    body. Body membership is a fact about the edges, and this function is handed the
    nodes — ``_require_collection_reachable`` does it once the whole drawing is in view,
    where the edge walk already lives.

    ``label_item_as`` has to be a plain identifier because it becomes a key in the
    result rows and is grouped by like any other output column — the same rule
    ``tool_chain_service`` applies to a link's ``value_alias``, which is the same field
    one layer down.
    """
    collect_from = str(data.get("collect_from") or "").strip()

    if collect_from and collect_from not in node_by_id:
        raise HTTPException(
            status_code=400,
            detail=(
                f"'{label}' collects the result of a node that is not in this graph."
            ),
        )

    alias = str(data.get("label_item_as") or "").strip()

    if not alias:
        return

    if not collect_from:
        # A field that silently does nothing is one the operator will swear they set.
        raise HTTPException(
            status_code=400,
            detail=(
                f"'{label}' records the item as '{alias}', but it does not collect any "
                "node's result, so there would be no rows to record it against."
            ),
        )

    if not _ALIAS_PATTERN.match(alias):
        raise HTTPException(
            status_code=400,
            detail=(
                f"'{alias}' cannot be a column name. Use letters, numbers and "
                "underscores, starting with a letter."
            ),
        )


def _refuse_collection_on(data: dict, label: str) -> None:
    """
    A ``do_until`` may not collect, and is told so rather than ignoring the field.

    A loop can only publish its union on the visit it knows is its last. For a
    ``for_each`` that is a fact about the cursor its runner holds; for a ``do_until`` it
    is ``loop_continues``' decision, taken by the compiler as a router *after* the runner
    returns. Deciding it in the runner as well would put one decision in two places, and
    the pass where the two disagreed is the pass whose rows go missing.
    """
    if str(data.get("collect_from") or "").strip():
        raise HTTPException(
            status_code=400,
            detail=(
                f"'{label}' is a do-until loop, which cannot collect its passes into "
                "one result — only a for-each can, because only it knows in advance "
                "which pass is the last. Put a for-each here, or collect the rows with "
                "a statement of their own."
            ),
        )


def _validate_edges(edges: Sequence[Any], node_by_id: Dict[str, dict]) -> None:
    """
    Every connection, and the four things one can get wrong.

    A second edge on one output port is the subtle one: the run would take exactly one
    of them and which one would depend on iteration order, so it is a graph whose
    behaviour is not the drawing.

    A terminal node **may** lead on — see ``TERMINAL_NODE_TYPES`` for why, and note that
    this rule used to refuse it outright. What is still refused is one terminal leading to
    another, because the second one cannot undo the first: ``failed_at`` is already set by
    then and nothing clears it, so a ``success`` drawn after a ``failure`` would draw a
    green run that reports as failed. Refused rather than documented, because the drawing
    is the thing people trust.
    """
    start_ids = {
        node_id for node_id, node in node_by_id.items()
        if node.get("type") == NODE_START
    }
    terminal_ids = {
        node_id for node_id, node in node_by_id.items()
        if node.get("type") in TERMINAL_NODE_TYPES
    }

    seen: Set[Tuple[str, str]] = set()

    for edge in edges:
        if not isinstance(edge, dict):
            raise HTTPException(
                status_code=400, detail="One of the connections could not be read.",
            )

        source = str(edge.get("source") or "").strip()
        target = str(edge.get("target") or "").strip()
        port = str(edge.get("source_port") or PORT_DEFAULT).strip() or PORT_DEFAULT

        if source not in node_by_id or target not in node_by_id:
            raise HTTPException(
                status_code=400,
                detail=(
                    "A connection points at a node that is not in this graph. Delete "
                    "it and draw it again."
                ),
            )

        if target in start_ids:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Nothing can connect into the Start node — it is where the run "
                    "begins."
                ),
            )

        if source in terminal_ids and target in terminal_ids:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"'{node_label(node_by_id[source])}' already decides how this run "
                    f"ends, so it cannot lead to '{node_label(node_by_id[target])}', "
                    "which decides it again. The first outcome is the one reported, so "
                    "the drawing would promise something the run does not do."
                ),
            )

        key = (source, port)

        if key in seen:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"'{node_label(node_by_id[source])}' has two connections leaving "
                    f"the same outcome ('{port}'). Only one of them could be followed."
                ),
            )

        seen.add(key)


def _require_bounded_cycles(
    nodes: Sequence[dict],
    edges: Sequence[Any],
    node_by_id: Dict[str, dict],
) -> None:
    """
    A cycle is allowed only if a loop node sits on it.

    This is the rule that earns the most explanation, because the obvious version of
    it is wrong in both directions. Banning cycles outright would ban loops, and a
    loop is a thing the user explicitly asked to be able to draw. Allowing them
    outright would let a plain ``A → B → A`` compile, and that run has no cursor and
    no ceiling — it stops when LangGraph raises ``GraphRecursionError``, which arrives
    as an internal error a long way from the two edges that caused it.

    So: cut every edge *out of* a loop node's ``body`` port, and require what is left
    to be acyclic. A loop's back edge is the edge that closes its cycle, so removing
    the body edges removes exactly the cycles a loop is responsible for bounding, and
    anything still cyclic afterwards is a cycle nobody bounds.

    Implemented as an iterative depth-first search with an explicit stack — a
    recursive walk would hit Python's recursion limit on a long chain, and a graph
    has no node ceiling.
    """
    adjacency: Dict[str, List[str]] = {node_id: [] for node_id in node_by_id}

    for edge in edges:
        source = str(edge.get("source") or "").strip()
        target = str(edge.get("target") or "").strip()
        port = str(edge.get("source_port") or PORT_DEFAULT).strip() or PORT_DEFAULT

        is_loop_body = (
            node_by_id.get(source, {}).get("type") in LOOP_NODE_TYPES
            and port == PORT_BODY
        )

        if is_loop_body:
            continue

        adjacency[source].append(target)

    # 0 = unvisited, 1 = on the current path, 2 = finished.
    state: Dict[str, int] = {node_id: 0 for node_id in node_by_id}

    for root in node_by_id:
        if state[root] != 0:
            continue

        stack: List[Tuple[str, int]] = [(root, 0)]
        state[root] = 1

        while stack:
            node_id, index = stack[-1]
            neighbours = adjacency[node_id]

            if index >= len(neighbours):
                state[node_id] = 2
                stack.pop()
                continue

            stack[-1] = (node_id, index + 1)
            neighbour = neighbours[index]

            if state[neighbour] == 1:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"'{node_label(node_by_id[neighbour])}' is part of a loop that "
                        "nothing controls. Route it through a For each or Do until "
                        "node so the run has a limit, or remove the connection that "
                        "closes the circle."
                    ),
                )

            if state[neighbour] == 0:
                state[neighbour] = 1
                stack.append((neighbour, 0))


def _require_looping_bodies(
    nodes: Sequence[dict],
    edges: Sequence[Any],
) -> None:
    """
    A loop's body must be able to come back to the loop, or it is not a loop.

    The mirror image of ``_require_bounded_cycles``: that one refuses a cycle with no loop
    node in it, and this one refuses a loop node with no cycle around it. Both describe the
    same requirement from opposite sides — a loop is a cycle with a cursor — and neither
    catches the other's case.

    What the drawing would otherwise do is the reason this is refused rather than allowed:
    the router sends the loop to its ``body`` port, the body runs, and if nothing leads back
    the run carries on past it. **One pass, of eighty-two, and the run reports success.**
    Nothing in the log says the other eighty-one were skipped, because as far as the graph
    is concerned nobody asked for them. That is the most expensive kind of wrong answer
    this module can produce, and it is invisible in exactly the place people look.

    Any edge back to the loop counts, from any port — a body that returns only down its
    error path still iterates, and refusing that would be inventing a rule about *how* a
    pass must end. A branch inside the body needs only one of its ports to return.
    """
    forward = _adjacency(edges)
    backward = _adjacency(edges, reverse=True)

    for node in nodes:
        if str(node.get("type") or "") not in LOOP_NODE_TYPES:
            continue

        loop_id = str(node.get("id") or "")
        label = node_label(node)
        started = _body_targets(edges, loop_id)

        if not started:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"'{label}' has nothing on its 'each' output, so there is no body to "
                    "run. Connect that output to the first node of the work you want "
                    "repeated."
                ),
            )

        body = _reachable_from(forward, started)
        returns = _reachable_from(backward, {loop_id})

        if not (body & returns) - {loop_id}:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"The body of '{label}' never comes back to it, so the loop would run "
                    "one pass and then carry on as though it had finished. Connect the "
                    f"last node of the body back to '{label}'."
                ),
            )


def _require_unions_in_loop_bodies(
    nodes: Sequence[dict],
    edges: Sequence[Any],
) -> None:
    """
    A union node must sit inside a ``for_each`` body, and that is not a matter of taste.

    It builds one copy of its statement per pass and runs the lot on the pass it is told is
    the last — and *which pass of how many* is a fact only a loop's cursor has. Outside a
    loop there is no last pass, so the node would append one fragment, take its ``default``
    port, and the statement it built would never run. A silent nothing, from a node whose
    box says it ran.

    ``do_until`` is refused as the host for the reason it cannot collect: whether a pass is
    its last is the router's decision, taken from the condition *after* the runner has
    returned, so a node inside it cannot know in time.

    "Inside the body" is the definition :func:`_require_collected_nodes_in_body` uses —
    reachable from the loop's ``body`` port and able to reach the loop again — and it is
    checked here, with the edges, rather than in the node validator, which is handed only
    the nodes.
    """
    unions = [
        node for node in nodes if str(node.get("type") or "") == NODE_SQL_UNION
    ]

    if not unions:
        return

    forward = _adjacency(edges)
    backward = _adjacency(edges, reverse=True)

    inside: Set[str] = set()
    inside_a_do_until: Set[str] = set()

    for node in nodes:
        node_type = str(node.get("type") or "")

        if node_type not in LOOP_NODE_TYPES:
            continue

        loop_id = str(node.get("id") or "")
        body = _reachable_from(forward, _body_targets(edges, loop_id))
        returns = _reachable_from(backward, {loop_id})
        members = (body & returns) - {loop_id}

        if node_type == NODE_FOR_EACH:
            inside |= members
        else:
            inside_a_do_until |= members

    for node in unions:
        node_id = str(node.get("id") or "")

        if node_id in inside:
            continue

        label = node_label(node)

        if node_id in inside_a_do_until:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"'{label}' sits inside a Do until loop, which cannot host a union: "
                    "only a loop that knows in advance which pass is its last can tell this "
                    "node when to run what it has built, and a Do until decides that after "
                    "the pass. Use a For each."
                ),
            )

        raise HTTPException(
            status_code=400,
            detail=(
                f"'{label}' builds one copy of its statement per pass, so it has to sit "
                "inside a For each loop's body. Outside one there is no last pass, so it "
                "would build a statement and never run it. Connect it between a For each's "
                "'each' output and its way back."
            ),
        )


def _require_collected_nodes_in_body(
    nodes: Sequence[dict],
    edges: Sequence[Any],
    node_by_id: Dict[str, dict],
) -> None:
    """
    A loop may only collect the result of a node inside its own body.

    Collecting something outside the body would append the *same* rows on every pass:
    that node ran once, before the loop, and its output does not change while the loop
    turns. Twenty passes would produce twenty copies, and a union of duplicates looks
    exactly like a union — a total taken over it is a plausible number that is wrong.

    "Inside the body" is defined the only way the drawing supports: reachable from the
    loop's ``body`` port, and able to reach the loop again. The second half matters — a
    node hanging off the body that never comes back is not part of the pass, it is where
    the pass stops, and its output is written once like anything else.

    Checked here rather than in ``_validate_collection`` because it needs the edges, and
    a node validator is handed only the nodes.
    """
    forward = _adjacency(edges)
    backward = _adjacency(edges, reverse=True)

    for node in nodes:
        if str(node.get("type") or "") != NODE_FOR_EACH:
            continue

        data = node.get("data") or {}
        collect_from = str(data.get("collect_from") or "").strip()

        if not collect_from:
            continue

        loop_id = str(node.get("id") or "")
        body = _reachable_from(forward, _body_targets(edges, loop_id))
        returns = _reachable_from(backward, {loop_id})

        # The loop itself is in both sets by construction — its body leads back to it — so
        # it has to be taken out by hand. Collecting its own output would append its item
        # envelope to the union it is building, once per pass.
        allowed = (body & returns) - {loop_id}

        if collect_from not in allowed:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"'{node_label(node)}' collects "
                    f"'{node_label(node_by_id[collect_from])}', which is not inside its "
                    "loop. A node outside the body runs once, so every pass would "
                    "collect the same rows again. Choose a node on the path between this "
                    "loop's 'each' output and its way back."
                ),
            )


def _require_timers_started(
    nodes: Sequence[dict],
    edges: Sequence[Any],
    node_by_id: Dict[str, dict],
) -> None:
    """
    A timer must be able to have been started by the time something pauses or stops it.

    Two rules, and it is worth being precise about what each one proves.

    **Reachability.** If the *start* box cannot reach the *stop* box by any path, then no
    run can ever have started that timer before arriving — the drawing is dead, and
    refusing it is not a guess. What reachability does **not** prove is the converse: a
    branch whose other port also leads to the stop means a run can still arrive without
    having passed the start. That case cannot be settled from the drawing, so it is left
    to run time, where ``timers`` refuses it with a sentence that names the branch as the
    likely reason. Static analysis here proves *impossible*, never *certain*.

    **Loop membership.** A pause, resume or stop inside a loop's body whose start sits
    outside it is refused. Pass one works and pass two finds the timer already stopped,
    which is the worst failure mode available — a graph that goes green and then red on
    the same drawing with the same data. Detected with the same body definition
    :func:`_require_collected_nodes_in_body` uses, and refused for the same reason
    :func:`_require_unions_in_loop_bodies` refuses its case: the picture cannot mean what
    it appears to.

    A *start* with no matching stop is deliberately allowed. A graph that only wants to
    know when something began is a legitimate graph.
    """
    forward = _adjacency(edges)
    backward = _adjacency(edges, reverse=True)

    loops = [
        str(node.get("id") or "")
        for node in nodes
        if str(node.get("type") or "") in LOOP_NODE_TYPES
    ]

    bodies = {
        loop_id: (
            _reachable_from(forward, _body_targets(edges, loop_id))
            & _reachable_from(backward, {loop_id})
        ) - {loop_id}
        for loop_id in loops
        if loop_id
    }

    for node in nodes:
        if str(node.get("type") or "") != NODE_TIMER:
            continue

        data = node.get("data") or {}

        if str(data.get("action") or "").strip().lower() == TIMER_START:
            continue

        node_id = str(node.get("id") or "")
        target = str(data.get("timer_node") or "").strip()

        # `_validate_timer_node` has already refused a missing or wrong target, so
        # anything still here names a real Timer set to Start.
        if not target or target not in node_by_id:
            continue

        started = node_label(node_by_id[target])

        if node_id not in _reachable_from(forward, {target}):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"'{node_label(node)}' acts on '{started}', but there is no path "
                    "from that timer to it. Nothing this run does can have started the "
                    "timer by the time it reaches this node."
                ),
            )

        for loop_id, body in bodies.items():
            if node_id in body and target not in body:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"'{node_label(node)}' is inside the loop "
                        f"'{node_label(node_by_id[loop_id])}', but '{started}' starts "
                        "outside it. The first pass would work and every pass after it "
                        "would find the timer already finished. Move the Start inside "
                        "the loop, or this node outside it."
                    ),
                )


def _adjacency(edges: Sequence[Any], reverse: bool = False) -> Dict[str, Set[str]]:
    """Every edge as a lookup, forwards or backwards."""
    graph: Dict[str, Set[str]] = {}

    for edge in edges:
        source = str(edge.get("source") or "").strip()
        target = str(edge.get("target") or "").strip()

        if not source or not target:
            continue

        if reverse:
            source, target = target, source

        graph.setdefault(source, set()).add(target)

    return graph


def _body_targets(edges: Sequence[Any], loop_id: str) -> Set[str]:
    """What a loop's ``body`` port leads to — where one pass begins."""
    return {
        str(edge.get("target") or "").strip()
        for edge in edges
        if str(edge.get("source") or "").strip() == loop_id
        and (str(edge.get("source_port") or "").strip() or PORT_DEFAULT) == PORT_BODY
    }


def _reachable_from(graph: Dict[str, Set[str]], roots: Set[str]) -> Set[str]:
    """
    Every node reachable from any of ``roots``, the roots included.

    Iterative, like the cycle walk, and for the same reason: a graph has no node
    ceiling, so a recursive version would fail on a long chain rather than on a wrong
    one.
    """
    seen: Set[str] = set()
    stack = [root for root in roots if root]

    while stack:
        node_id = stack.pop()

        if node_id in seen:
            continue

        seen.add(node_id)
        stack.extend(graph.get(node_id, ()))

    return seen


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _nodes_of(graph_data: Any) -> List[dict]:
    """The nodes of a stored document, tolerating one that predates a rule."""
    nodes = (graph_data or {}).get("nodes") if isinstance(graph_data, dict) else None
    return [node for node in nodes if isinstance(node, dict)] if isinstance(nodes, list) else []


def _edges_of(graph_data: Any) -> List[dict]:
    """The edges of a stored document, tolerating one that predates a rule."""
    edges = (graph_data or {}).get("edges") if isinstance(graph_data, dict) else None
    return [edge for edge in edges if isinstance(edge, dict)] if isinstance(edges, list) else []


def _validated_name(name: Optional[str]) -> str:
    """A graph's name: present, and short enough for the column."""
    cleaned = (name or "").strip()

    if not cleaned:
        raise HTTPException(status_code=400, detail="A graph needs a name.")

    if len(cleaned) > 255:
        raise HTTPException(
            status_code=400,
            detail="A graph's name cannot be longer than 255 characters.",
        )

    return cleaned


async def _require_unused_name(
    db: AsyncSession,
    user_id: int,
    name: Optional[str],
    exclude_id: Optional[int] = None,
) -> None:
    """
    Refuse a name this user already used, before the unique index does.

    The index is the guarantee; this is the message. Without it the failure surfaces
    as an ``IntegrityError`` and a 500, which tells the user nothing about what to
    change. Case-insensitive, matching ``uq_tool_graphs_user_name_lower``.
    """
    cleaned = _validated_name(name).lower()

    existing = await graph_crud.get_many(db, filters={"user_id": user_id})

    for graph in existing:
        if graph.id == exclude_id:
            continue

        if (graph.name or "").strip().lower() == cleaned:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"You already have a graph called '{graph.name}'. Pick a "
                    "different name."
                ),
            )
