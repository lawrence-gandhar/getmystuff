import json
import uuid

from litestar import Controller, get, post
from litestar.connection import Request
from litestar.exceptions import HTTPException
from litestar.response import Response, Template
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.auth import require_auth
from app.models.user import User
from app.services.ai_settings import ai_settings_service
from app.services.flow_builder import flow_service

_JSON = "application/json"
_ROWS_TEMPLATE = "flow_builder/flow_rows.htm"


class FlowBuilderController(Controller):
    """
    The Flow Builder library and canvas. Flows are user-owned and standalone —
    no chatbot appears in these URLs; attaching a flow to a chatbot happens on
    that chatbot's settings page (see ChatbotSettingsController.save_flow).
    """
    path = "/flow-builder"
    dependencies = {"user": require_auth}

    # --------------------------
    # LIST
    # --------------------------
    @get("/")
    async def index(self, db: AsyncSession, user: User) -> Template:
        flows = await flow_service.get_user_flow_views(db, user.id)
        return Template(
            template_name="flow_builder/list.htm",
            context={"user": user, "flows": flows, "active": "flow_builder"},
        )

    # --------------------------
    # CREATE
    # --------------------------
    @post("/create")
    async def create(self, request: Request, db: AsyncSession, user: User) -> Template:
        form = await request.form()
        error = None
        try:
            await flow_service.create_flow(db, user.id, form.get("name", ""))
        except HTTPException as e:
            error = str(e.detail)

        return await self._rows(db, user, error)

    # --------------------------
    # CANVAS PAGE
    # --------------------------
    @get("/{flow_id:uuid}/edit")
    async def edit(self, flow_id: uuid.UUID, db: AsyncSession, user: User) -> Template:
        flow = await flow_service.get_flow(db, user.id, flow_id)
        ai_api_keys = await ai_settings_service.get_user_api_keys(db, user.id)
        ai_api_keys_json = [
            {"id": str(key.uuid), "label": key.label, "provider": key.provider_display}
            for key in ai_api_keys
        ]
        return Template(
            template_name="flow_builder/canvas.htm",
            context={
                "user": user,
                "flow": flow,
                "graph_data_json": json.dumps(flow.graph_data),
                "ai_api_keys_json": json.dumps(ai_api_keys_json),
                "active": "flow_builder",
            },
        )

    # --------------------------
    # GRAPH — JSON GET (reload/discard)
    # --------------------------
    @get("/{flow_id:uuid}/graph")
    async def graph(self, flow_id: uuid.UUID, db: AsyncSession, user: User) -> Response:
        flow = await flow_service.get_flow(db, user.id, flow_id)
        return Response(flow.graph_data, media_type=_JSON, status_code=200)

    # --------------------------
    # SAVE
    # --------------------------
    @post("/{flow_id:uuid}/save")
    async def save(
        self,
        flow_id: uuid.UUID,
        request: Request,
        db: AsyncSession,
        user: User,
    ) -> Response:
        try:
            graph_data = await request.json()
        except Exception:
            return Response(
                "<div class='alert alert-danger' data-success='false'>Invalid graph data.</div>",
                media_type="text/html",
                status_code=200,
            )

        try:
            await flow_service.update_flow_graph(db, user.id, flow_id, graph_data)
        except HTTPException as e:
            return Response(
                f"<div class='alert alert-danger' data-success='false'>{e.detail}</div>",
                media_type="text/html",
                status_code=200,
            )

        return Response(
            "<div class='alert alert-success' data-success='true'>Flow saved.</div>",
            media_type="text/html",
            status_code=200,
        )

    # --------------------------
    # RENAME
    # --------------------------
    @post("/{flow_id:uuid}/rename")
    async def rename(
        self,
        flow_id: uuid.UUID,
        request: Request,
        db: AsyncSession,
        user: User,
    ) -> Template:
        form = await request.form()
        error = None
        try:
            await flow_service.rename_flow(db, user.id, flow_id, form.get("name", ""))
        except HTTPException as e:
            error = str(e.detail)

        return await self._rows(db, user, error)

    # --------------------------
    # PUBLISH / UNPUBLISH
    # --------------------------
    @post("/{flow_id:uuid}/set-active")
    async def set_active(
        self,
        flow_id: uuid.UUID,
        request: Request,
        db: AsyncSession,
        user: User,
    ) -> Template:
        """Toggle the published/draft flag. Attachment is untouched."""
        form = await request.form()
        error = None
        try:
            await flow_service.set_flow_active(
                db, user.id, flow_id, is_active=form.get("is_active") == "true",
            )
        except HTTPException as e:
            error = str(e.detail)

        return await self._rows(db, user, error)

    # --------------------------
    # DELETE
    # --------------------------
    @post("/{flow_id:uuid}/delete")
    async def delete(self, flow_id: uuid.UUID, db: AsyncSession, user: User) -> Template:
        error = None
        try:
            await flow_service.delete_flow(db, user.id, flow_id)
        except HTTPException as e:
            error = str(e.detail)

        return await self._rows(db, user, error)

    @staticmethod
    async def _rows(db: AsyncSession, user: User, error: str | None) -> Template:
        """The HTMX response every mutation returns: error banner + rebuilt table body."""
        flows = await flow_service.get_user_flow_views(db, user.id)
        return Template(
            template_name=_ROWS_TEMPLATE,
            context={"flows": flows, "error": error},
        )
