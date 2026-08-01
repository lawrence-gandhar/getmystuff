"""
HTTP layer for Workspaces. Accepts the request, hands the raw form to
workspace_service, and renders a template — no business rules live here.

Every mutation answers with the same fragment (see :meth:`_rows`): a success/error
marker for the page-level response div plus an out-of-band refresh of the
workspaces table. The modal's after-request hook closes itself on the success
marker, so one response drives both.

The agents assigned to a workspace are not listed here — Data Agents is its own
module, so the agent count links across to it filtered by this workspace.
"""

import uuid

from litestar import Controller, get, post
from litestar.connection import Request
from litestar.exceptions import HTTPException
from litestar.response import Template
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.auth import require_auth
from app.models.user import User
from app.schemas.workspaces import (
    WorkspaceCreateRequest,
    WorkspaceSetActiveRequest,
    WorkspaceUpdateRequest,
)
from app.services.workspaces import workspace_service

_ROWS_TEMPLATE = "workspaces/partials/workspace_rows_response.htm"
_FORM_TEMPLATE = "workspaces/partials/workspace_form.htm"
_MODAL_ERROR_TEMPLATE = "workspaces/partials/modal_error.htm"


class WorkspaceController(Controller):
    """The Workspaces library — a grouping for the data agents built elsewhere."""

    path = "/workspaces"
    dependencies = {"user": require_auth}

    # --------------------------
    # LIST
    # --------------------------
    @get("/")
    async def index(self, db: AsyncSession, user: User) -> Template:
        workspaces = await workspace_service.get_user_workspace_views(db, user.id)
        return Template(
            template_name="workspaces/index.htm",
            context={"user": user, "workspaces": workspaces, "active": "workspaces"},
        )

    # --------------------------
    # FORMS (modal bodies)
    # --------------------------
    @get("/new-form")
    async def new_form(self) -> Template:
        """Blank create form — the same partial the edit form uses."""
        return Template(
            template_name=_FORM_TEMPLATE,
            context={
                "workspace": None,
                "form_action": "/workspaces/create",
                "submit_label": "Create Workspace",
            },
        )

    @get("/{workspace_id:uuid}/edit-form")
    async def edit_form(
        self,
        workspace_id: uuid.UUID,
        db: AsyncSession,
        user: User,
    ) -> Template:
        try:
            workspace = await workspace_service.get_workspace(db, user.id, workspace_id)
        except HTTPException as exc:
            return Template(
                template_name=_MODAL_ERROR_TEMPLATE,
                context={"error": str(exc.detail)},
            )

        return Template(
            template_name=_FORM_TEMPLATE,
            context={
                "workspace": workspace,
                "form_action": f"/workspaces/{workspace.uuid}/update",
                "submit_label": "Save Changes",
            },
        )

    # --------------------------
    # CREATE
    # --------------------------
    @post("/create")
    async def create(self, request: Request, db: AsyncSession, user: User) -> Template:
        error = None
        try:
            payload = await WorkspaceCreateRequest.from_form(request)
            await workspace_service.create_workspace(
                db,
                user.id,
                name=payload.name,
                description=payload.description,
            )
        except HTTPException as exc:
            error = str(exc.detail)

        return await self._rows(db, user, error)

    # --------------------------
    # UPDATE
    # --------------------------
    @post("/{workspace_id:uuid}/update")
    async def update(
        self,
        workspace_id: uuid.UUID,
        request: Request,
        db: AsyncSession,
        user: User,
    ) -> Template:
        error = None
        try:
            payload = await WorkspaceUpdateRequest.from_form(request)
            await workspace_service.update_workspace(
                db,
                user.id,
                workspace_id,
                name=payload.name,
                description=payload.description,
            )
        except HTTPException as exc:
            error = str(exc.detail)

        return await self._rows(db, user, error)

    # --------------------------
    # ARCHIVE / RESTORE
    # --------------------------
    @post("/{workspace_id:uuid}/set-active")
    async def set_active(
        self,
        workspace_id: uuid.UUID,
        request: Request,
        db: AsyncSession,
        user: User,
    ) -> Template:
        error = None
        try:
            payload = await WorkspaceSetActiveRequest.from_form(request)
            await workspace_service.set_workspace_active(
                db, user.id, workspace_id, is_active=payload.is_active,
            )
        except HTTPException as exc:
            error = str(exc.detail)

        return await self._rows(db, user, error)

    # --------------------------
    # DELETE
    # --------------------------
    @post("/{workspace_id:uuid}/delete")
    async def delete(
        self,
        workspace_id: uuid.UUID,
        db: AsyncSession,
        user: User,
    ) -> Template:
        error = None
        try:
            await workspace_service.delete_workspace(db, user.id, workspace_id)
        except HTTPException as exc:
            error = str(exc.detail)

        return await self._rows(db, user, error)

    @staticmethod
    async def _rows(db: AsyncSession, user: User, error: str | None) -> Template:
        """The HTMX response every mutation returns: marker + rebuilt table."""
        workspaces = await workspace_service.get_user_workspace_views(db, user.id)
        return Template(
            template_name=_ROWS_TEMPLATE,
            context={"workspaces": workspaces, "error": error},
        )
