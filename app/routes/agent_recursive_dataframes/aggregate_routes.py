"""
HTTP layer for whole-result grouping. Takes the form, calls the service, renders a
partial; no business rules here.

Three endpoints, all rendering into the page:

  :meth:`index`  the console — pick an agent, pick a tool, say what to group
  :meth:`tools`  the tool picker for the agent just chosen (HTMX cascade)
  :meth:`run`    plan it and run it, and show the result

Its own page rather than a panel on the Deep Agents console, because the two answer
different questions. That console asks "what does this agent say"; this one asks
"what is the actual total", and the second is a thing an operator wants to check
against their own database without spending a chat turn on it.

Errors render as inline alerts rather than raising, so a refused instruction leaves
the page — and everything typed into it — where it was. A run that fails is an
ordinary outcome here: the instruction may be unanswerable, the result set may be
too large, the column may not exist, and every one of those is a sentence rather
than a stack trace.
"""

import uuid

from litestar import Controller, get, post
from litestar.connection import Request
from litestar.exceptions import HTTPException
from litestar.response import Template
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.auth import require_auth
from app.models.user import User
from app.schemas.agent_recursive_dataframes import (
    AggregateRunRequest,
    AggregationResultView,
)
from app.services.agent_recursive_dataframes import aggregate_service

_INDEX_TEMPLATE = "agent_recursive_dataframes/index.htm"
_TOOLS_TEMPLATE = "agent_recursive_dataframes/partials/tool_field.htm"
_RESULT_TEMPLATE = "agent_recursive_dataframes/partials/result.htm"
_ERROR_TEMPLATE = "agent_recursive_dataframes/partials/error.htm"


class AggregationController(Controller):
    """Grouping and totalling a tool's whole result set."""

    path = "/aggregations"
    dependencies = {"user": require_auth}

    # --------------------------
    # PAGE
    # --------------------------
    @get("/")
    async def index(self, db: AsyncSession, user: User) -> Template:
        """
        The console. Only active agents are listed, and the tool picker starts empty
        because which tools are aggregatable depends on which agent is chosen.
        """
        try:
            view = await aggregate_service.get_console_view(db, user.id)
        except HTTPException as exc:
            return Template(
                template_name=_INDEX_TEMPLATE,
                context={"agents": [], "tools": [], "error": str(exc.detail)},
            )

        return Template(
            template_name=_INDEX_TEMPLATE,
            context={**view, "error": None, "limits": _limits()},
        )

    # --------------------------
    # CASCADE — agent → tools
    # --------------------------
    @get("/tools")
    async def tools(self, request: Request, db: AsyncSession, user: User) -> Template:
        """
        Re-render the tool picker for the agent just chosen.

        An agent with no opted-in tool renders an explanation rather than an empty
        dropdown — "there are none and here is how to add one" is the useful state,
        and an empty select looks like a page that failed to load.
        """
        raw = request.query_params.get("agent_id") or ""
        agent_id = None
        error = None
        tools: list = []

        if raw.strip():
            try:
                agent_id = uuid.UUID(raw.strip())
                tools = (await aggregate_service.get_console_view(
                    db, user.id, agent_id,
                ))["tools"]
            except ValueError:
                error = "That data agent could not be read."
            except HTTPException as exc:
                error = str(exc.detail)

        return Template(
            template_name=_TOOLS_TEMPLATE,
            context={"tools": tools, "error": error},
        )

    # --------------------------
    # RUN
    # --------------------------
    @post("/run")
    async def run(self, request: Request, db: AsyncSession, user: User) -> Template:
        """
        Plan the instruction and run it, rendering the totals or the refusal.

        The result goes through ``AggregationResultView`` rather than straight from
        the service dict, so the template is rendering a declared contract — and so
        ``is_capped`` is decided in one place rather than by a comparison written
        into the markup.
        """
        payload = await AggregateRunRequest.from_form(request)

        try:
            outcome = await aggregate_service.run_for_agent(
                db,
                user.id,
                payload.agent_id,
                payload.instruction,
                tool_id=payload.tool_id,
            )
        except HTTPException as exc:
            return Template(
                template_name=_ERROR_TEMPLATE,
                context={"error": str(exc.detail)},
            )

        return Template(
            template_name=_RESULT_TEMPLATE,
            context={"result": AggregationResultView.build(outcome)},
        )


def _limits() -> dict:
    """
    The ceilings, for the page to state up front.

    Shown rather than left to be discovered: "at most 200,000 records" is something
    an operator should read before typing an instruction, not after waiting for one
    to be refused.
    """
    # No row ceiling to state: every group the fold produced is reported. The two
    # that remain are about what can be *computed* — records read, groups held in
    # memory — and both refuse loudly rather than trimming an answer.
    return {
        "max_records": aggregate_service.AGGREGATE_MAX_SOURCE_ROWS,
        "max_groups": aggregate_service.MAX_GROUPS,
    }
