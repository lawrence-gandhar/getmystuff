"""
HTTP layer for Deep Agents. Accepts the request, calls deep_agent_service, renders
a template — no business rules here.

Three endpoints, and each exists for a distinct reason:

* :meth:`agent_options` is the Workspace -> Data Agent cascade. It is shared: the
  Chatbot Settings create form and the AI tab's attachment form both call it, so
  "which agents can I pick" is answered in one place.
* :meth:`console` and :meth:`ask` are the test console — the only way to see what an
  agent actually does before a visitor does. It reports which tools were called on
  every answer, which is what makes the "the model only sees tool output" claim
  something an operator can check rather than take on trust.
"""

import uuid
from typing import Optional

from litestar import Controller, get, post
from litestar.connection import Request
from litestar.exceptions import HTTPException
from litestar.response import Template
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.auth import require_auth
from app.models.user import User
from app.services.data_agents import data_agent_service
from app.services.deep_agents import deep_agent_service
from app.services.workspaces import workspace_service
from app.utils.validators import parse_optional_uuid

_AGENT_OPTIONS_TEMPLATE = "deep_agents/partials/agent_options.htm"
_ANSWER_TEMPLATE = "deep_agents/partials/answer.htm"

# A console question is a person typing, not a form field with a schema. Bounded
# the same way sql_assist bounds its prompt, for the same reason.
_MAX_QUESTION_LEN = 2000


class DeepAgentController(Controller):
    """Running a data agent: the shared agent picker, and the test console."""

    path = "/deep-agents"
    dependencies = {"user": require_auth}

    # --------------------------
    # CASCADE — workspace → data agent
    # --------------------------
    @get("/agent-options")
    async def agent_options(
        self,
        db: AsyncSession,
        user: User,
        workspace_id: Optional[str] = None,
        selected: Optional[str] = None,
        field_name: Optional[str] = None,
    ) -> Template:
        """
        The Data Agent ``<select>`` for one workspace.

        A blank ``workspace_id`` lists every agent the user owns rather than none.
        Deliberate: an agent's workspace is optional (``data_agents.workspace_id`` is
        nullable), so "no workspace chosen" has to be able to reach an unassigned
        agent, otherwise those agents would be unpickable here.

        ``field_name`` lets both host forms reuse this fragment with their own field
        name instead of one form dictating the other's markup.
        """
        selected_id = parse_optional_uuid(selected, "Data agent")

        agents: list = []
        error = None
        try:
            agents = await data_agent_service.get_agent_views(
                db, user.id, parse_optional_uuid(workspace_id, "Workspace"),
            )
        except HTTPException as exc:
            error = str(exc.detail)

        return Template(
            template_name=_AGENT_OPTIONS_TEMPLATE,
            context={
                "agents": agents,
                "selected_agent_id": str(selected_id) if selected_id else "",
                "field_name": (field_name or "data_agent_id").strip(),
                "error": error,
            },
        )

    # --------------------------
    # TEST CONSOLE
    # --------------------------
    @get("/{agent_id:uuid}/console")
    async def console(
        self,
        agent_id: uuid.UUID,
        db: AsyncSession,
        user: User,
    ) -> Template:
        """
        The console page: the agent, its tools, and anything that would stop it
        running. No model is built and no datasource is touched until a question is
        asked.
        """
        runtime = await deep_agent_service.get_agent_runtime_view(db, user.id, agent_id)

        return Template(
            template_name="deep_agents/console.htm",
            context={
                "user": user,
                "agent": runtime,
                # The console runs an agent, so it belongs under the Data Agents
                # section in the sidebar rather than claiming a nav item of its own.
                "active": "data_agents",
            },
        )

    @post("/{agent_id:uuid}/ask")
    async def ask(
        self,
        agent_id: uuid.UUID,
        request: Request,
        db: AsyncSession,
        user: User,
    ) -> Template:
        """
        Run one turn and render the answer with the tools it called.

        Errors render into the same fragment rather than raising. The console is a
        diagnostic surface: "your key has no model name" is the answer the operator
        came for, and losing it to an error page would defeat the point.
        """
        form = await request.form()
        question = (form.get("question") or "").strip()

        if not question:
            return Template(
                template_name=_ANSWER_TEMPLATE,
                context={"error": "Type a question first."},
            )

        if len(question) > _MAX_QUESTION_LEN:
            return Template(
                template_name=_ANSWER_TEMPLATE,
                context={
                    "error": (
                        f"That question is too long — keep it under "
                        f"{_MAX_QUESTION_LEN} characters."
                    ),
                },
            )

        try:
            result = await deep_agent_service.answer_with_deep_agent(
                db, user.id, agent_id, question,
            )
        except HTTPException as exc:
            return Template(
                template_name=_ANSWER_TEMPLATE,
                context={"error": str(exc.detail), "question": question},
            )

        return Template(
            template_name=_ANSWER_TEMPLATE,
            context={"question": question, **result},
        )
