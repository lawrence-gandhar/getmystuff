import uuid

from litestar import Controller, get, post, delete
from litestar.response import Response, Template
from litestar.connection import Request
from litestar.exceptions import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.auth import require_auth
from app.models.user import User
from app.schemas.datasource import (
    DatasourceDetailsResponse,
    ToolBaseConfigCreateRequest,
    ToolNameRequest,
)
from app.services.datasource.datasource_config_service import (
    create_config_with_subqueries,
    check_tool_name_exists,
)

from app.services.datasource.datasource_service import (
    get_datasource_table_schema,
    get_user_datasources,
    get_datasource_objects,
    delete_datasource_file,
)
from app.utils.query_joins import join_types_for

_HTML = "text/html"


def _tool_name_feedback(message: str, available: bool) -> Response:
    """
    The inline badge the tool-name blur check swaps in.

    One builder for both outcomes, so the markup and the icon cannot diverge
    between the four places that used to write it out by hand.
    """
    css_class = "text-success" if available else "text-danger"
    icon = "la-check-circle" if available else "la-times-circle"

    return Response(
        f"<div class='{css_class} small mt-1'>"
        f"<i class='las {icon}'></i> {message}"
        f"</div>",
        media_type=_HTML,
    )


# The join types each relational dialect supports, handed to the page so the Tool
# Base Config builder can offer the right ones for whichever datasource the user
# opens — the panel is reused for all of them without a round trip. Built from the
# same source the server validates against (app.utils.query_joins), so the dropdown
# can never offer a join the save would then reject.
_JOIN_TYPES_BY_DB_TYPE = {
    db_type: join_types_for(db_type)
    for db_type in ("postgres", "mysql", "sqlite")
}


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
                "join_types_by_db_type": _JOIN_TYPES_BY_DB_TYPE,
                "user": user,
                "active": "datasource_configuration"
            },
        )
    
    @get("/{datasource_id:uuid}/details")
    async def get_datasource_details(
        self,
        db: AsyncSession,
        request: Request,
        user: User,
        datasource_id: uuid.UUID
    ) -> dict:
        
        data = await get_datasource_objects(
            db=db,
            datasource_id=datasource_id,
            user_id=user.id,
        )

        return {
            "datasource_details": DatasourceDetailsResponse.payload_for(data)
        }
        

    @get("/{datasource_id:uuid}/{table_name:str}/configuration")
    async def get_table_configuration(
        self,
        request: Request,
        datasource_id: uuid.UUID,
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
                "join_types_by_db_type": _JOIN_TYPES_BY_DB_TYPE,
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
        """
        Tell the user in advance whether this tool name will be accepted.

        The name's own rules — required, length, character set — come from
        ``ToolNameRequest``, which is the same schema
        :meth:`create_configuration` validates with. Sharing it is the point: this
        endpoint's whole job is to predict that save, so a second copy of the rules
        here could only ever be wrong.

        Uniqueness is the one check that needs the database, so it stays below.
        """
        try:
            name = (await ToolNameRequest.from_form(request)).tool_name
        except HTTPException as exc:
            return _tool_name_feedback(str(exc.detail), available=False)

        if await check_tool_name_exists(
            db=db,
            user_id=user.id,
            datasource_id=datasource_id,
            tool_name=name,
        ):
            return _tool_name_feedback(
                "This tool name is already taken. Please choose a different one.",
                available=False,
            )

        return _tool_name_feedback("Tool name is available.", available=True)

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
        """
        Save one tool base config plus its subqueries.

        Both JSON fields are hand-editable in the form, so a bad payload is a
        fixable user mistake and gets said so. That was already true of
        ``base_config``; ``subquery_configs`` used to be read with a ``json.loads``
        whose ``except`` fell back to ``[]`` — a malformed payload silently
        discarded every subquery the user had built and then reported success.
        ``JsonArrayField`` refuses it instead.
        """
        try:
            payload = await ToolBaseConfigCreateRequest.from_form(request)

            await create_config_with_subqueries(
                db=db,
                user_id=user.id,
                datasource_id=datasource_id,
                tool_name=payload.tool_name,
                table_name=payload.table_name,
                base_config=payload.base_config,
                subquery_configs=payload.subquery_configs,
            )
            return Response(
                "<div class='alert alert-success' data-success='true'>Configuration Created Successfully</div>",
                media_type=_HTML,
                status_code=200,
            )
        except HTTPException as e:
            return Response(
                f"<div class='alert alert-danger' data-success='false'>{e.detail}</div>",
                media_type=_HTML,
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
