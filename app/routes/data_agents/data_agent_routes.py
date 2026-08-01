"""
HTTP layer for Data Agents. Accepts the request, hands the raw form to
data_agent_service, and renders a template — no business rules live here.

Every mutation answers with the same fragment (see :meth:`_rows`): a success/error
marker for the page-level response div plus an out-of-band refresh of the agents
table.

The list can be narrowed to one workspace with ``?workspace=<uuid>`` — that is what
the agent count on the Workspaces page links to. The filter is carried on every
mutation as a hidden field, so a rebuilt table keeps showing the same subset the
user was looking at.
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
from app.schemas.data_agents import (
    DataAgentCreateRequest,
    DataAgentDeleteRequest,
    DataAgentListQuery,
    DataAgentSetActiveRequest,
    DataAgentUpdateRequest,
)
from app.services.data_agents import data_agent_service
from app.services.workspaces import workspace_service

_ROWS_TEMPLATE = "data_agents/partials/agent_rows_response.htm"
_FORM_TEMPLATE = "data_agents/partials/agent_form.htm"
_MODAL_ERROR_TEMPLATE = "data_agents/partials/modal_error.htm"


class DataAgentController(Controller):
    """The Data Agents library — agents are owned by the user, not by a workspace."""

    path = "/data-agents"
    dependencies = {"user": require_auth}

    # --------------------------
    # LIST
    # --------------------------
    @get("/")
    async def index(
        self,
        request: Request,
        db: AsyncSession,
        user: User,
    ) -> Template:
        workspace_id = DataAgentListQuery.from_query(request).workspace
        agents = await data_agent_service.get_agent_views(db, user.id, workspace_id)

        # Named so the page can say *which* workspace it is filtered to.
        workspace_row = (
            await workspace_service.get_workspace(db, user.id, workspace_id)
            if workspace_id
            else None
        )

        return Template(
            template_name="data_agents/index.htm",
            context={
                "user": user,
                "agents": agents,
                "workspace_filter": str(workspace_id) if workspace_id else "",
                "workspace_filter_name": workspace_row.name if workspace_row else None,
                "active": "data_agents",
            },
        )

    # --------------------------
    # FORMS (modal bodies)
    # --------------------------
    @get("/new-form")
    async def new_form(
        self,
        request: Request,
        db: AsyncSession,
        user: User,
    ) -> Template:
        """
        Blank create form — the same partial the edit form uses. When the list is
        filtered to a workspace, that workspace is preselected.
        """
        try:
            workspace_id = DataAgentListQuery.from_query(request).workspace
            choices = await self._form_choices(db, user)
        except HTTPException as exc:
            return Template(
                template_name=_MODAL_ERROR_TEMPLATE,
                context={"error": str(exc.detail)},
            )

        return Template(
            template_name=_FORM_TEMPLATE,
            context={
                "agent": None,
                "preselected_workspace_id": str(workspace_id) if workspace_id else "",
                "form_action": "/data-agents/create",
                "submit_label": "Create Data Agent",
                **choices,
            },
        )

    @get("/{agent_id:uuid}/edit-form")
    async def edit_form(
        self,
        agent_id: uuid.UUID,
        db: AsyncSession,
        user: User,
    ) -> Template:
        try:
            agent = await data_agent_service.get_data_agent_view(db, user.id, agent_id)
            choices = await self._form_choices(db, user)
        except HTTPException as exc:
            return Template(
                template_name=_MODAL_ERROR_TEMPLATE,
                context={"error": str(exc.detail)},
            )

        return Template(
            template_name=_FORM_TEMPLATE,
            context={
                "agent": agent,
                "preselected_workspace_id": agent["workspace_id"],
                "form_action": f"/data-agents/{agent_id}/update",
                "submit_label": "Save Changes",
                **choices,
            },
        )

    # --------------------------
    # CREATE
    # --------------------------
    @post("/create")
    async def create(self, request: Request, db: AsyncSession, user: User) -> Template:
        payload = await DataAgentCreateRequest.from_form(request)
        error = None
        try:
            await data_agent_service.create_data_agent(
                db,
                user.id,
                name=payload.name,
                description=payload.description,
                system_prompt=payload.system_prompt,
                workspace_id=payload.workspace_id,
                llm_api_key_id=payload.llm_api_key_id,
            )
        except HTTPException as exc:
            error = str(exc.detail)

        return await self._rows(db, user, payload.workspace_filter, error)

    # --------------------------
    # UPDATE
    # --------------------------
    @post("/{agent_id:uuid}/update")
    async def update(
        self,
        agent_id: uuid.UUID,
        request: Request,
        db: AsyncSession,
        user: User,
    ) -> Template:
        payload = await DataAgentUpdateRequest.from_form(request)
        error = None
        try:
            await data_agent_service.update_data_agent(
                db,
                user.id,
                agent_id,
                name=payload.name,
                description=payload.description,
                system_prompt=payload.system_prompt,
                workspace_id=payload.workspace_id,
                llm_api_key_id=payload.llm_api_key_id,
            )
        except HTTPException as exc:
            error = str(exc.detail)

        return await self._rows(db, user, payload.workspace_filter, error)

    # --------------------------
    # ENABLE / DISABLE
    # --------------------------
    @post("/{agent_id:uuid}/set-active")
    async def set_active(
        self,
        agent_id: uuid.UUID,
        request: Request,
        db: AsyncSession,
        user: User,
    ) -> Template:
        payload = await DataAgentSetActiveRequest.from_form(request)
        error = None
        try:
            await data_agent_service.set_data_agent_active(
                db, user.id, agent_id, is_active=payload.is_active,
            )
        except HTTPException as exc:
            error = str(exc.detail)

        return await self._rows(db, user, payload.workspace_filter, error)

    # --------------------------
    # DELETE
    # --------------------------
    @post("/{agent_id:uuid}/delete")
    async def delete(
        self,
        agent_id: uuid.UUID,
        request: Request,
        db: AsyncSession,
        user: User,
    ) -> Template:
        payload = await DataAgentDeleteRequest.from_form(request)
        error = None
        try:
            await data_agent_service.delete_data_agent(db, user.id, agent_id)
        except HTTPException as exc:
            error = str(exc.detail)

        return await self._rows(db, user, payload.workspace_filter, error)

    # --------------------------
    # Helpers
    # --------------------------
    @staticmethod
    async def _form_choices(db: AsyncSession, user: User) -> dict:
        """Dropdown data the create and edit forms both need."""
        return {
            "workspaces": await workspace_service.get_workspace_choices(db, user.id),
            "llm_keys": await data_agent_service.get_llm_key_choices(db, user.id),
        }

    @staticmethod
    async def _rows(
        db: AsyncSession,
        user: User,
        workspace_id: Optional[uuid.UUID],
        error: str | None,
    ) -> Template:
        """
        The HTMX response every mutation returns: marker + rebuilt table, still
        narrowed to whichever workspace the user was filtered to.

        Takes the filter already parsed — the mutation schemas validate it on the
        way in, so there is nothing left to interpret here.
        """
        agents = await data_agent_service.get_agent_views(db, user.id, workspace_id)

        return Template(
            template_name=_ROWS_TEMPLATE,
            context={
                "agents": agents,
                "workspace_filter": str(workspace_id) if workspace_id else "",
                "error": error,
            },
        )
