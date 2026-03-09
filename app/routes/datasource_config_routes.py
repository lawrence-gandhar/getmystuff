import re
import uuid
import json

from litestar import Controller, get, post, delete
from litestar.response import Response, Template
from litestar.connection import Request
from litestar.exceptions import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.auth import require_auth
from app.models.user import User
from app.services.datasource_config_service import (
    create_config_with_subqueries,
    check_tool_name_exists,
)

from app.services.datasource_service import (
    get_datasource_table_schema,
    get_user_datasources,
    get_datasource_objects,
    delete_datasource_file,
)

# Compiled once at import time — reused on every validation request.
_TOOL_NAME_RE = re.compile(r'^[a-z0-9_]+$')


class DataSourceConfigurations(Controller):
    path = "/datasource"
    dependencies = {"user": require_auth}

    @get("/configurations")
    async def get_all_configuration(
        self,
        db: AsyncSession,
        request: Request,
        user: User,
    ) -> Template:
        
        datasources = await get_user_datasources(
            db=db,
            user_id=user.id,
        )
        
        return Template(
            template_name="datasources/configuration.htm",
            context={
                "request": request,
                "datasources": datasources,
                "table_name": "",
                "config":{},
                "user": user,
                "active": "datasource_configuration"
            },
        )
    
    @get("/{datasource_id:str}/details")
    async def get_datasource_details(
        self,
        db: AsyncSession,
        request: Request,
        user: User,
        datasource_id: str
    ) -> dict:
        
        data = await get_datasource_objects(
            db=db,
            datasource_id=datasource_id,
            user_id=user.id,
        )
        
        return {
            "datasource_details": data
        }
        

    @get("/{datasource_id:str}/{table_name:str}/configuration")
    async def get_table_configuration(
        self,
        request: Request,
        datasource_id: str,
        table_name: str,
        db: AsyncSession,
        user: User,
    ) -> Template:
        """
        Render configuration page for a specific table/collection.
        """

        config = await get_datasource_table_schema(
            db,
            datasource_id = datasource_id,
            user_id = user.id,
            table_name = table_name,
        )

        return Template(
            template_name="datasources/configuration.htm",
            context={
                "request": request,
                "datasource_id": datasource_id,
                "table_name": table_name,
                "config":config,
                "user": user,
            },
        )

    # --------------------------
    # VALIDATE TOOL NAME
    # (called on blur via HTMX — returns an HTML fragment, never JSON)
    # --------------------------
    @post("/{datasource_id:uuid}/config/validate-tool-name")
    async def validate_tool_name(
        self,
        datasource_id: uuid.UUID,
        request: Request,
        db: AsyncSession,
        user: User,
    ) -> Response:
        form = await request.form()
        raw = form.get("tool_name", "")
        name = raw.strip().lower()

        # ── 1. Empty ────────────────────────────────────────────────────────
        if not name:
            return Response(
                "<div class='text-danger small mt-1'>"
                "<i class='las la-times-circle'></i> Tool name is required."
                "</div>",
                media_type="text/html",
            )

        # ── 2. Length ────────────────────────────────────────────────────────
        if len(name) > 255:
            return Response(
                "<div class='text-danger small mt-1'>"
                "<i class='las la-times-circle'></i> "
                "Tool name must not exceed 255 characters."
                "</div>",
                media_type="text/html",
            )

        # ── 3. Character set ─────────────────────────────────────────────────
        if not _TOOL_NAME_RE.match(name):
            return Response(
                "<div class='text-danger small mt-1'>"
                "<i class='las la-times-circle'></i> "
                "Only lowercase letters (a–z), digits (0–9), "
                "and underscores (_) are allowed."
                "</div>",
                media_type="text/html",
            )

        # ── 4. Uniqueness check ───────────────────────────────────────────────
        exists = await check_tool_name_exists(
            db=db,
            user_id=user.id,
            datasource_id=datasource_id,
            tool_name=name,
        )
        if exists:
            return Response(
                "<div class='text-danger small mt-1'>"
                "<i class='las la-times-circle'></i> "
                "This tool name is already taken. Please choose a different one."
                "</div>",
                media_type="text/html",
            )

        # ── All checks passed ────────────────────────────────────────────────
        return Response(
            "<div class='text-success small mt-1'>"
            "<i class='las la-check-circle'></i> Tool name is available."
            "</div>",
            media_type="text/html",
        )

    # --------------------------
    # CREATE CONFIG
    # --------------------------
    @post("/{datasource_id:uuid}/config/create")
    async def create_configuration(
        self,
        datasource_id: uuid.UUID,
        request: Request,
        db: AsyncSession,
        user: User,
    ) -> Response:

        form = await request.form()

        tool_name = form.get("tool_name")
        table_name = form.get("table_name")
        base_config_raw = form.get("base_config", "{}")
        subquery_configs_raw = form.get("subquery_configs", "[]")

        try:
            base_config = json.loads(base_config_raw)
        except (json.JSONDecodeError, TypeError):
            base_config = {}

        try:
            subquery_configs = json.loads(subquery_configs_raw)
            if not isinstance(subquery_configs, list):
                subquery_configs = []
        except (json.JSONDecodeError, TypeError):
            subquery_configs = []

        try:
            await create_config_with_subqueries(
                db=db,
                user_id=user.id,
                datasource_id=datasource_id,
                tool_name=tool_name,
                table_name=table_name,
                base_config=base_config,
                subquery_configs=subquery_configs,
            )
            return Response(
                "<div class='alert alert-success' data-success='true'>Configuration Created Successfully</div>",
                media_type="text/html",
                status_code=200,
            )
        except HTTPException as e:
            return Response(
                f"<div class='alert alert-danger' data-success='false'>{e.detail}</div>",
                media_type="text/html",
                status_code=200,
            )

    # --------------------------
    # DELETE DATASOURCE FILE
    # --------------------------
    @delete("/{datasource_id:uuid}/file/{file_id:uuid}/delete", status_code=204)
    async def delete_file(
        self,
        datasource_id: uuid.UUID,
        file_id: uuid.UUID,
        db: AsyncSession,
        user: User,
    ) -> None:
        await delete_datasource_file(
            db=db,
            datasource_id=datasource_id,
            file_id=file_id,
            user_id=user.id,
        )
