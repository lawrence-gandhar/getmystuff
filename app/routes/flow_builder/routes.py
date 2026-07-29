import json
import uuid

from litestar import Controller, get, post
from litestar.connection import Request
from litestar.exceptions import HTTPException
from litestar.response import Response, Template
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.auth import require_auth
from app.models.user import User
from app.services.flow_builder import flow_service

_JSON = "application/json"


class FlowBuilderController(Controller):
    path = "/flow-builder"
    dependencies = {"user": require_auth}

    # --------------------------
    # LIST
    # --------------------------
    @get("/{key_id:uuid}")
    async def index(self, key_id: uuid.UUID, db: AsyncSession, user: User) -> Template:
        flows = await flow_service.get_flows_for_key(db, user.id, key_id)
        return Template(
            template_name="flow_builder/list.htm",
            context={"user": user, "key_id": key_id, "flows": flows, "active": "chatbot_settings"},
        )

    # --------------------------
    # CREATE
    # --------------------------
    @post("/{key_id:uuid}/create")
    async def create(self, key_id: uuid.UUID, request: Request, db: AsyncSession, user: User) -> Template:
        form = await request.form()
        error = None
        try:
            await flow_service.create_flow(db, user.id, key_id, form.get("name", ""))
        except HTTPException as e:
            error = str(e.detail)

        flows = await flow_service.get_flows_for_key(db, user.id, key_id)
        return Template(
            template_name="flow_builder/flow_rows.htm",
            context={"key_id": key_id, "flows": flows, "error": error},
        )

    # --------------------------
    # CANVAS PAGE
    # --------------------------
    @get("/{key_id:uuid}/{flow_id:uuid}/edit")
    async def edit(
        self, key_id: uuid.UUID, flow_id: uuid.UUID, db: AsyncSession, user: User
    ) -> Template:
        flow = await flow_service.get_flow(db, user.id, key_id, flow_id)
        return Template(
            template_name="flow_builder/canvas.htm",
            context={
                "user": user,
                "key_id": key_id,
                "flow": flow,
                "graph_data_json": json.dumps(flow.graph_data),
                "active": "chatbot_settings",
            },
        )

    # --------------------------
    # GRAPH — JSON GET (reload/discard)
    # --------------------------
    @get("/{key_id:uuid}/{flow_id:uuid}/graph")
    async def graph(
        self, key_id: uuid.UUID, flow_id: uuid.UUID, db: AsyncSession, user: User
    ) -> Response:
        flow = await flow_service.get_flow(db, user.id, key_id, flow_id)
        return Response(flow.graph_data, media_type=_JSON, status_code=200)

    # --------------------------
    # SAVE
    # --------------------------
    @post("/{key_id:uuid}/{flow_id:uuid}/save")
    async def save(
        self,
        key_id: uuid.UUID,
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
            await flow_service.update_flow_graph(db, user.id, key_id, flow_id, graph_data)
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
    @post("/{key_id:uuid}/{flow_id:uuid}/rename")
    async def rename(
        self,
        key_id: uuid.UUID,
        flow_id: uuid.UUID,
        request: Request,
        db: AsyncSession,
        user: User,
    ) -> Response:
        form = await request.form()
        try:
            await flow_service.rename_flow(db, user.id, key_id, flow_id, form.get("name", ""))
        except HTTPException as e:
            return Response(
                f"<div class='alert alert-danger' data-success='false'>{e.detail}</div>",
                media_type="text/html",
                status_code=200,
            )

        return Response(
            "<div class='alert alert-success' data-success='true'>Renamed.</div>",
            media_type="text/html",
            status_code=200,
        )

    # --------------------------
    # ACTIVATE
    # --------------------------
    @post("/{key_id:uuid}/{flow_id:uuid}/activate")
    async def activate(
        self, key_id: uuid.UUID, flow_id: uuid.UUID, db: AsyncSession, user: User
    ) -> Template:
        error = None
        try:
            await flow_service.activate_flow(db, user.id, key_id, flow_id)
        except HTTPException as e:
            error = str(e.detail)

        flows = await flow_service.get_flows_for_key(db, user.id, key_id)
        return Template(
            template_name="flow_builder/flow_rows.htm",
            context={"key_id": key_id, "flows": flows, "error": error},
        )

    # --------------------------
    # DELETE
    # --------------------------
    @post("/{key_id:uuid}/{flow_id:uuid}/delete")
    async def delete(
        self, key_id: uuid.UUID, flow_id: uuid.UUID, db: AsyncSession, user: User
    ) -> Template:
        error = None
        try:
            await flow_service.delete_flow(db, user.id, key_id, flow_id)
        except HTTPException as e:
            error = str(e.detail)

        flows = await flow_service.get_flows_for_key(db, user.id, key_id)
        return Template(
            template_name="flow_builder/flow_rows.htm",
            context={"key_id": key_id, "flows": flows, "error": error},
        )
