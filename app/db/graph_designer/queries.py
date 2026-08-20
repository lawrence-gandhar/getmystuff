"""
Graph-designer data access that the generic ``CRUDQueryBuilder`` cannot express.

A handful of statements, each a join, an aggregate or a two-owner filter. Everything
else in the module goes through ``CRUDQueryBuilder`` like the rest of the application —
this file exists so the
few multi-table statements live in the module's own ``db/`` subpackage rather than
leaking into the service layer or polluting the shared, model-agnostic
``app/db/db_utils.py``. It is the same call ``app/db/flow_builder/queries.py`` makes.
"""

import uuid
from typing import List, Optional, Sequence, Tuple

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.data_agents import DataAgent
from app.models.graph_designer import ToolGraph, ToolGraphRun, ToolGraphRunStep
from app.models.workspaces import Workspace


async def fetch_graphs_with_owner_names(
    db: AsyncSession,
    user_id: int,
) -> List[Tuple[ToolGraph, Optional[uuid.UUID], Optional[str], Optional[uuid.UUID], Optional[str]]]:
    """
    Every graph this user owns, paired with whatever it is callable through:
    ``(graph, agent_uuid, agent_name, workspace_uuid, workspace_name)``, each ``None``
    when absent.

    Two outer joins rather than extra queries per row: the library page shows that
    column for every graph, and both attachments are at most one row by construction —
    ``data_agent_id`` is unique, ``workspace_id`` points at one workspace. At most one
    of the two is ever set, because the attachments are mutually exclusive; the query
    does not assume that, so a row that somehow held both would show as it is rather
    than hiding half of itself.

    **The uuids are selected, not just the names**, because the library's edit form has
    to preselect the current attachment and the only identifier it may put in a form
    field is the public one. Deriving them per row would be two extra queries per graph
    for something these joins already have in hand.

    Ordered newest-first, matching ``fetch_flows_with_chatbot_names``, so a graph
    someone has just created is the one at the top.
    """
    result = await db.execute(
        select(ToolGraph, DataAgent.uuid, DataAgent.name, Workspace.uuid, Workspace.name)
        .outerjoin(DataAgent, DataAgent.id == ToolGraph.data_agent_id)
        .outerjoin(Workspace, Workspace.id == ToolGraph.workspace_id)
        .where(ToolGraph.user_id == user_id)
        .order_by(ToolGraph.created_at.desc())
    )
    return [
        (graph, agent_uuid, agent_name, workspace_uuid, workspace_name)
        for graph, agent_uuid, agent_name, workspace_uuid, workspace_name in result.all()
    ]


async def fetch_run_with_graph(
    db: AsyncSession,
    run_uuid,
) -> Optional[Tuple[ToolGraphRun, ToolGraph]]:
    """
    One run and the graph it belongs to, in a single statement.

    Every run endpoint needs both — the run for its state, the graph for the
    ``user_id`` that authorises reading it — and authorisation must not cost a second
    round trip on a stream that re-reads the run once a second.
    """
    result = await db.execute(
        select(ToolGraphRun, ToolGraph)
        .join(ToolGraph, ToolGraph.id == ToolGraphRun.tool_graph_id)
        .where(ToolGraphRun.uuid == run_uuid)
    )
    row = result.first()
    return (row[0], row[1]) if row else None


async def fetch_run_steps(
    db: AsyncSession,
    run_id: int,
) -> Sequence[ToolGraphRunStep]:
    """
    One run's steps in the order they ran.

    Ordered by ``sequence`` and not by ``id`` or ``started_at``: two steps can be
    written inside the same millisecond, and the runner is the only thing that knows
    what order they actually went in. ``ix_tool_graph_run_steps_run_sequence`` covers
    exactly this.
    """
    result = await db.execute(
        select(ToolGraphRunStep)
        .where(ToolGraphRunStep.run_id == run_id)
        .order_by(ToolGraphRunStep.sequence.asc(), ToolGraphRunStep.id.asc())
    )
    return result.scalars().all()


async def next_step_sequence(db: AsyncSession, run_id: int) -> int:
    """
    The next free position in one run's log.

    Read from the table rather than kept in a counter on the runner, because the
    runner's node functions are separate LangGraph tasks: a task gets a *copy* of the
    context, so an incremented in-memory counter is invisible to its siblings — the
    same constraint ``download_notice`` documents about ContextVars. The read and the
    insert are not atomic against a concurrent writer, which is acceptable here for a
    reason worth stating: a graph's nodes are sequenced, not fanned out, so there is
    only ever one writer per run.
    """
    result = await db.execute(
        select(func.max(ToolGraphRunStep.sequence))
        .where(ToolGraphRunStep.run_id == run_id)
    )
    highest = result.scalar()

    # `highest or -1` would be wrong, and wrongly in the quiet way: the first step's
    # sequence is 0, which is falsy, so every step after it would also be given 0 and
    # the log would have no order at all. NULL — no steps yet — is the only case that
    # means "start at zero".
    return 0 if highest is None else int(highest) + 1


async def fetch_agent_graphs(
    db: AsyncSession,
    data_agent_id: int,
) -> List[ToolGraph]:
    """
    Every published graph one data agent may call as a tool.

    Two ways to be callable, and this is the single place that knows both:

    * **attached** to this agent — ``data_agent_id``, at most one by unique constraint;
    * **shared** with the workspace this agent is assigned to — ``workspace_id``, any
      number of them, because a workspace is a team's shelf.

    The workspace half is a correlated subquery rather than a join, so an agent in no
    workspace reads ``NULL`` and matches nothing: without that, ``workspace_id IS NULL``
    on both sides would hand every unshared graph to every unassigned agent.

    **``is_active`` is checked here for both**, which is the point of the function
    existing at all. It mirrors ``flow_service.get_active_flow``: a graph can be parked
    mid-edit without being detached or un-shared, and a draft can sit attached while it
    is being finished, and in neither case does an agent call it.

    Attached first, then shared, each by ``id``. The order is what makes an unchanged
    set produce a byte-identical routing prompt — ``is_prompt_stale`` compares text, so
    a list whose order drifted would rebuild the prompt on every request.
    """
    agent_workspace = (
        select(DataAgent.workspace_id)
        .where(DataAgent.id == data_agent_id)
        .scalar_subquery()
    )

    result = await db.execute(
        select(ToolGraph)
        .where(
            ToolGraph.is_active.is_(True),
            or_(
                ToolGraph.data_agent_id == data_agent_id,
                and_(
                    ToolGraph.workspace_id.is_not(None),
                    ToolGraph.workspace_id == agent_workspace,
                ),
            ),
        )
        .order_by(
            # `data_agent_id IS NULL` sorts False (attached) before True (shared).
            (ToolGraph.data_agent_id.is_(None)).asc(),
            ToolGraph.id.asc(),
        )
    )
    return list(result.scalars().all())


async def fetch_workspace_graphs(
    db: AsyncSession,
    workspace_id: int,
) -> List[ToolGraph]:
    """
    Every graph shared with one workspace, published or not.

    Unlike :func:`fetch_agent_graphs` this does **not** filter on ``is_active``: its
    caller is the collision check in ``graph_service``, which has to see a draft too. A
    draft sharing a tool name with a published graph becomes a collision the moment
    somebody presses Publish, and finding out then — from the second control — is how a
    refusal ends up looking arbitrary.
    """
    result = await db.execute(
        select(ToolGraph)
        .where(ToolGraph.workspace_id == workspace_id)
        .order_by(ToolGraph.id.asc())
    )
    return list(result.scalars().all())
