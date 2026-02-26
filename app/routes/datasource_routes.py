import uuid
from datetime import datetime

from litestar import post, get, Controller
from litestar.response import Response, Template
from sqlalchemy.ext.asyncio import AsyncSession
from litestar.connection import Request
from litestar.exceptions import HTTPException

from app.models.user import User
from app.services.datasource_service import (
    test_connection,
    create_datasource,
    get_datasource_objects,
    get_user_datasources,
    toggle_column_status_service
)
from app.db.auth import require_auth


class DataSourceController(Controller):
    path = "/datasource"
    dependencies = {"user": require_auth}

    # --------------------------
    # CREATE DATASOURCE
    # --------------------------
    @post("/create")
    async def create(
        self,
        request: Request,
        db: AsyncSession,
        user: User,
    ) -> Response:

        form = await request.form()

        db_type = form.get("db_type")
        host = form.get("host")
        port = form.get("port")
        database_name = form.get("database_name")
        username = form.get("username")
        password = form.get("password")

        try:
            await create_datasource(
                db=db,
                user_id=user.id,
                db_type=db_type,
                host=host,
                port=port,
                database_name=database_name,
                username=username,
                password=password,
                connection_tester=test_connection,
            )
            return Response(
                "<div class='alert alert-success'>Datasource Added Successfully</div>",
                media_type="text/html",
            )
        except HTTPException as e:
            return Response(
                f"<div class='alert alert-danger'>{e.detail}</div>",
                media_type="text/html",
            )

    # --------------------------
    # GET TABLES / COLLECTIONS
    # --------------------------
    @get("/{datasource_id:uuid}")
    async def get_objects(
        self,
        datasource_id: uuid.UUID,
        db: AsyncSession,
        user: User,
    ) -> Template:

        data = await get_datasource_objects(
            db=db,
            datasource_id=datasource_id,
            user_id=user.id,
        )

        return Template(
            template_name="datasources/schema_preview.htm",
            context={
                "objects": data["objects"],
                "datasource_id":datasource_id
            },
        )

    
    # --------------------------
    # LIST ALL DATASOURCES
    # --------------------------
    @get("/")
    async def list_datasources(
        self,
        db: AsyncSession,
        user: User,
    ) -> Template:

        datasources = await get_user_datasources(
            db=db,
            user_id=user.id,
        )

        return Template(
            template_name="datasources/index.htm",
            context={
                "user": user,
                "datasources": datasources,
                "active": "datasource"
            },
        )
    
    @get("/{datasource_id:uuid}/table/{table_name:str}/view")
    async def view_table_schema(
        self,
        datasource_id: uuid.UUID,
        table_name: str,
        db: AsyncSession,
        user: User,
    ) -> Template:

        datasource = await get_datasource_objects(
            db=db,
            datasource_id=datasource_id,
            user_id=user.id,
        )

        schema = datasource.get("configuration_data", {})

        return Template(
            template_name="datasources/column_view.htm",
            context={
                "table_name": table_name,
                "schema": schema[table_name]["column_data"],
                "datasource_id": str(datasource_id),
            },
        )
    
    @post("/{datasource_id:uuid}/table/{table_name:str}/column/{column_name:str}/toggle")
    async def toggle_column_status(
        self,
        datasource_id: uuid.UUID,
        table_name: str,
        column_name: str,
        request: Request,
        db: AsyncSession,
        user: User,
    ) -> Template:

        form = await request.form()
        new_status = form.get("status")

        if new_status not in {"active", "inactive"}:
            raise HTTPException(status_code=400)

        updated_column = await toggle_column_status_service(
            db=db,
            datasource_id=datasource_id,
            user_id=user.id,
            table_name=table_name,
            column_name=column_name,
            new_status=new_status,
        )

        if not updated_column:
            raise HTTPException(status_code=404)

        return Template(
            template_name="datasources/column_row.htm",
            context={
                "col": updated_column,
                "datasource_id": str(datasource_id),
                "table_name": table_name,
            },
        )