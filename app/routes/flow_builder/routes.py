import json
import uuid

from litestar import Controller, get, post
from litestar.connection import Request
from litestar.exceptions import HTTPException
from litestar.response import Response, Template
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.auth import require_auth
from app.models.user import User
from app.schemas.flow_builder import (
    FlowCreateRequest,
    FlowGraphSaveRequest,
    FlowRenameRequest,
    FlowSetActiveRequest,
)
from app.services.ai_settings import ai_settings_service
from app.services.email_dispatch import smtp_service as email_smtp_service
from app.services.email_dispatch import template_service as email_template_service
from app.services.flow_builder import flow_service
from app.services.graph_designer import graph_service

_JSON = "application/json"
_ROWS_TEMPLATE = "flow_builder/flow_rows.htm"
_HELP_TEMPLATE = "flow_builder/help.htm"


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
    # HELP
    # --------------------------
    @get("/help")
    async def help_page(self, user: User) -> Template:
        """
        The Flow Builder help page — the browsable form of
        documentations/FLOW_BUILDER.md, opened in its own tab by the Help button on the
        library page and on the canvas.

        Static: it reads nothing and takes no query parameters, so there is no service
        call and no schema to parse. It is a route rather than a link to the markdown
        file because a help page has to arrive inside the application's own layout,
        behind the same auth as the page it explains — the same call
        ``graph_designer_routes.help_page`` and ``tool_config_routes.help_page`` make.

        A literal path, so it cannot be confused with ``/{flow_id:uuid}/…``.
        """
        return Template(
            template_name=_HELP_TEMPLATE,
            context={"user": user, "active": "flow_builder"},
        )

    # --------------------------
    # CREATE
    # --------------------------
    @post("/create")
    async def create(self, request: Request, db: AsyncSession, user: User) -> Template:
        error = None
        try:
            payload = await FlowCreateRequest.from_form(request)
            await flow_service.create_flow(db, user.id, payload.name)
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
        # The published graphs a Run-Graph node may pick. Only published ones: an
        # unpublished graph fails the run, and offering it would be offering a choice
        # that cannot work. Drafts are not flagged-and-offered here the way an inactive
        # datasource is elsewhere, because the fix is on another page entirely.
        graphs_json = [
            {"id": view["uuid"], "label": view["name"]}
            for view in await graph_service.get_graph_views(db, user.id)
            if view.get("is_active")
        ]
        # What an Email node picks from. Templates carry their declared variables so the
        # property panel can draw one binding row per variable the instant a template is
        # chosen; a second request would let somebody Save the block before its rows loaded.
        # Switched-off entries are offered and flagged rather than hidden, so a node already
        # pointing at one stays editable.
        email_templates_json = await email_template_service.choices(db, user.id)
        smtp_configs_json = await email_smtp_service.choices(db, user.id)

        return Template(
            template_name="flow_builder/canvas.htm",
            context={
                "user": user,
                "flow": flow,
                "graph_data_json": json.dumps(flow.graph_data),
                "ai_api_keys_json": json.dumps(ai_api_keys_json),
                "graphs_json": json.dumps(graphs_json),
                "email_templates_json": json.dumps(email_templates_json),
                "smtp_configs_json": json.dumps(smtp_configs_json),
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
            payload = await FlowGraphSaveRequest.from_json(request)
            await flow_service.update_flow_graph(
                db, user.id, flow_id, payload.graph_data(),
            )
        except HTTPException as e:
            # Covers both a body that is not a valid graph payload (400 from the
            # schema) and a flow the caller does not own (404 from the service).
            # Both render into the canvas's save banner rather than replacing the
            # page, which is what would lose the user's unsaved work.
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
        error = None
        try:
            payload = await FlowRenameRequest.from_form(request)
            await flow_service.rename_flow(db, user.id, flow_id, payload.name)
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
        error = None
        try:
            payload = await FlowSetActiveRequest.from_form(request)
            await flow_service.set_flow_active(
                db, user.id, flow_id, is_active=payload.is_active,
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
