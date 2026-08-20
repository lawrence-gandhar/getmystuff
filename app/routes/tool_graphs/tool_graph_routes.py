"""
HTTP layer for Tool Graphs. Accepts the selection, hands it to tool_graph_service,
and returns either the page shell or one of the two drawings as JSON — no business
rules live here.

The page is served once and the canvas is drawn client-side, so the two view
endpoints answer JSON rather than partials. That is the same call
``FlowBuilderController`` and ``KnowledgeBaseController`` made, for the same reason:
a renderer that positions nodes and shades diagrams needs the data, not markup, and
a server-templated SVG could not be zoomed or re-laid-out without a round trip.

Neither view endpoint raises. A failure is returned in the body's ``error`` field
with a 200, because the canvas sits beside a tree the user is clicking through: a
tool deleted in another tab should put one sentence next to the canvas, not replace
the page they are working in. ``GET /tool-configs/child-options`` answers the same
way for the same reason.
"""

from litestar import Controller, get
from litestar.connection import Request
from litestar.exceptions import HTTPException
from litestar.response import Template
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.auth import require_auth
from app.models.user import User
from app.schemas.tool_graphs import (
    ToolGraphQuery,
    ToolGraphResponse,
    ToolJoinsResponse,
)
from app.services.tool_graphs import tool_graph_service

_INDEX_TEMPLATE = "tool_graphs/index.htm"


class ToolGraphController(Controller):
    """Tool Graphs — a tool chain as the graph it compiles to, and its joins as sets."""

    path = "/tool-graphs"
    dependencies = {"user": require_auth}

    # --------------------------
    # PAGE
    # --------------------------
    @get("/")
    async def index(
        self,
        request: Request,
        db: AsyncSession,
        user: User,
    ) -> Template:
        """
        The tree, the toolbar and an empty canvas.

        The tree is rendered server-side because it is a list of links to things the
        user owns — the same kind of page the rest of the application serves — while
        only the canvas needs a renderer. A selection in the query string is passed
        through untouched so ``/tool-graphs?tool=<uuid>`` opens on that tool: the
        graph of a chain is the sort of thing someone pastes into a ticket.
        """
        selection = ToolGraphQuery.from_query(request)
        tree = await tool_graph_service.get_graph_tree(db, user.id)

        return Template(
            template_name=_INDEX_TEMPLATE,
            context={
                "user": user,
                "tree": tree,
                "selected_workspace": str(selection.workspace or ""),
                "selected_agent": str(selection.agent or ""),
                "selected_tool": str(selection.tool or ""),
                "active": "tool_graphs",
            },
        )

    # --------------------------
    # TOOL GRAPH — JSON
    # --------------------------
    @get("/graph")
    async def graph(
        self,
        request: Request,
        db: AsyncSession,
        user: User,
    ) -> dict:
        """The selected tools as nodes and edges, already laid out."""
        try:
            selection = ToolGraphQuery.from_query(request)
            payload = await tool_graph_service.get_chain_graph(
                db, user.id,
                workspace_id=selection.workspace,
                agent_id=selection.agent,
                tool_id=selection.tool,
            )
        except HTTPException as exc:
            return ToolGraphResponse.failure(str(exc.detail)).payload()

        return ToolGraphResponse.build(payload).payload()

    # --------------------------
    # SQL GRAPH — JSON
    # --------------------------
    @get("/joins")
    async def joins(
        self,
        request: Request,
        db: AsyncSession,
        user: User,
    ) -> dict:
        """The same selection's joins, one entry per tool, in query order."""
        try:
            selection = ToolGraphQuery.from_query(request)
            payload = await tool_graph_service.get_join_views(
                db, user.id,
                workspace_id=selection.workspace,
                agent_id=selection.agent,
                tool_id=selection.tool,
            )
        except HTTPException as exc:
            return ToolJoinsResponse.failure(str(exc.detail)).payload()

        return ToolJoinsResponse.build(payload).payload()
