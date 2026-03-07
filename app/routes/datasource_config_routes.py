import uuid
import json

from litestar import Controller, get, post
from litestar.response import Response, Template
from litestar.connection import Request
from litestar.exceptions import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.auth import require_auth
from app.models.user import User
from app.services.datasource_config_service import create_config

from app.services.datasource_service import (
    get_datasource_table_schema,
    get_user_datasources,
    get_datasource_objects
)


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
        base_config_raw = form.get("base_config", "{}")

        try:
            base_config = json.loads(base_config_raw)
        except (json.JSONDecodeError, TypeError):
            base_config = {}

        try:
            await create_config(
                db=db,
                user_id=user.id,
                datasource_id=datasource_id,
                tool_name=tool_name,
                base_config=base_config,
            )
            return Response(
                "<div class='alert alert-success'>Configuration Created Successfully</div>",
                media_type="text/html",
            )
        except HTTPException as e:
            return Response(
                f"<div class='alert alert-danger'>{e.detail}</div>",
                media_type="text/html",
            )
