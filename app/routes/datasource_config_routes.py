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


class DataSourceConfigurations(Controller):
    path = "/datasource"
    dependencies = {"user": require_auth}

    @get("/{datasource_id:str}/{table_name:str}/configuration")
    async def get_configuration(
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

        

        return Template(
            template_name="datasources/configuration.htm",
            context={
                "request": request,
                "datasource_id": datasource_id,
                "table_name": table_name,
                "config": {},
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
