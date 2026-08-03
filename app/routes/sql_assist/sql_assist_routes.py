"""
HTTP layer for Ask AI — the SQL assistant. Accepts the form, hands it to
sql_assist_service, and renders a partial; no business rules here.

Its own module rather than part of Tool Configs: the panel is opened from the Tool
Configs page, but generating SQL from a schema is a capability of its own, usable
from anywhere a datasource is in view. Tool Configs calls it; it does not belong to
it.

Five endpoints, all rendering into the panel:
  :meth:`form`        the panel body — datasource, tables, model, prompt
  :meth:`tables`      the table picker for the datasource just chosen (HTMX cascade)
  :meth:`generate`    one attempt, or a refinement of the last one
  :meth:`tool_form`   "Auto Create Tool" — the generated query as a Tool Config, plus
                      the name to give it
  :meth:`create_tool` creates it

Errors are rendered as inline alerts rather than raised, so a rejected prompt, an
unreachable datasource or a name already taken leaves the panel — and everything typed
into it — exactly where it was.
"""

import json

from litestar import Controller, get, post
from litestar.connection import Request
from litestar.exceptions import HTTPException
from litestar.response import Template
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.auth import require_auth
from app.models.user import User
from app.schemas.sql_assist import (
    SqlAssistCreateToolRequest,
    SqlAssistFormQuery,
    SqlAssistGenerateRequest,
    SqlAssistTablesQuery,
    SqlAssistToolFormRequest,
)
from app.services.sql_assist import sql_assist_service
from app.services.tool_configs import tool_config_service

_FORM_TEMPLATE = "sql_assist/partials/form.htm"
_TABLES_TEMPLATE = "sql_assist/partials/tables_field.htm"
_RESULT_TEMPLATE = "sql_assist/partials/result.htm"
_ERROR_TEMPLATE = "sql_assist/partials/error.htm"
_PANEL_ERROR_TEMPLATE = "sql_assist/partials/panel_error.htm"
_TOOL_FORM_TEMPLATE = "sql_assist/partials/tool_form.htm"
_TOOL_CREATED_TEMPLATE = "sql_assist/partials/tool_created.htm"


class SqlAssistController(Controller):
    """Ask AI — a plain-English request in, a SQL query out."""

    path = "/sql-assist"
    dependencies = {"user": require_auth}

    # --------------------------
    # PANEL BODY
    # --------------------------
    @get("/form")
    async def form(
        self,
        request: Request,
        db: AsyncSession,
        user: User,
    ) -> Template:
        """
        The blank panel. Only relational datasources are listed — there is no SQL to
        write for a file or a collection — so an empty list is a state worth
        explaining rather than an empty dropdown.

        ``agent`` is the host page's current filter. It travels through every
        subsequent request so a tool created here lands on the agent the user was
        looking at, and the rebuilt table keeps showing the same subset.
        """
        try:
            agent_id = SqlAssistFormQuery.from_query(request).agent
            datasources = await sql_assist_service.get_datasource_choices(db, user.id)
            llm_keys = await sql_assist_service.get_llm_key_choices(db, user.id)
        except HTTPException as exc:
            return Template(
                template_name=_PANEL_ERROR_TEMPLATE,
                context={"error": str(exc.detail)},
            )

        return Template(
            template_name=_FORM_TEMPLATE,
            context={
                "datasources": datasources,
                "llm_keys": llm_keys,
                "llm_modes": sql_assist_service.LLM_MODES,
                "tables": [],
                "selected_datasource_id": "",
                "schema_error": None,
                "agent_filter": str(agent_id) if agent_id else "",
            },
        )

    # --------------------------
    # CASCADE — datasource → tables
    # --------------------------
    @get("/tables")
    async def tables(
        self,
        request: Request,
        db: AsyncSession,
        user: User,
    ) -> Template:
        """Re-render the table picker for the datasource just chosen."""
        selection = SqlAssistTablesQuery.from_query(request).datasource_id

        table_names: list = []
        schema_error = None
        if selection is not None:
            try:
                table_names = await sql_assist_service.get_table_choices(
                    db, user.id, selection,
                )
            except HTTPException as exc:
                schema_error = str(exc.detail)

        return Template(
            template_name=_TABLES_TEMPLATE,
            context={
                "tables": table_names,
                "selected_datasource_id": str(selection) if selection else "",
                "schema_error": schema_error,
            },
        )

    # --------------------------
    # GENERATE / REFINE
    # --------------------------
    @post("/generate")
    async def generate(
        self,
        request: Request,
        db: AsyncSession,
        user: User,
    ) -> Template:
        """
        Ask the model for a query, or for a better version of the last one.

        The table list arrives as repeated form fields from a multi-select, which is
        why the schema declares it in ``multi_fields`` — read as a single value it
        would silently use one table out of several.
        """
        payload = await SqlAssistGenerateRequest.from_form(request)

        try:
            result = await sql_assist_service.generate_sql(
                db,
                user.id,
                datasource_id=payload.datasource_id,
                table_names=payload.table_names,
                prompt=payload.prompt,
                llm_mode=payload.llm_mode,
                llm_api_key_id=payload.llm_api_key_id,
                history_json=payload.history_json,
            )
        except HTTPException as exc:
            return Template(
                template_name=_ERROR_TEMPLATE,
                context={
                    "error": str(exc.detail),
                    # Keeps the conversation alive through a failed turn, so a
                    # refinement that times out doesn't reset the whole session.
                    "history_json": payload.history_json or "[]",
                },
            )

        return Template(
            template_name=_RESULT_TEMPLATE,
            context={
                **result,
                # Echoed so "Auto Create Tool" can ask for the same query to be
                # converted without the user re-picking any of it.
                **payload.echo(),
            },
        )

    # --------------------------
    # AUTO CREATE TOOL — draft
    # --------------------------
    @post("/tool-form")
    async def tool_form(
        self,
        request: Request,
        db: AsyncSession,
        user: User,
    ) -> Template:
        """
        Express the generated query as a Tool Config and ask for the name to save it
        under.

        A POST rather than a GET because it carries the query and re-reads the schema
        to convert it — and because the SQL is too long to put in a query string.

        Every valid read-only query gets a form. The draft's ``mode`` says whether
        the tool will hold the builder's shape or the statement as written; the
        partial explains which, and why, rather than the two being different
        outcomes here.
        """
        payload = await SqlAssistToolFormRequest.from_form(request)
        echo = payload.echo()

        try:
            draft = await sql_assist_service.draft_tool_config(
                db,
                user.id,
                datasource_id=payload.datasource_id,
                table_names=payload.table_names,
                sql=payload.sql,
                llm_mode=payload.llm_mode,
                llm_api_key_id=payload.llm_api_key_id,
            )
            agents = await sql_assist_service.get_agent_choices(db, user.id)
        except HTTPException as exc:
            return Template(
                template_name=_ERROR_TEMPLATE,
                context={
                    "error": str(exc.detail),
                    "history_json": payload.history_json or "[]",
                },
            )

        return Template(
            template_name=_TOOL_FORM_TEMPLATE,
            context={
                **draft,
                **echo,
                "agents": agents,
                # A page filtered to one agent is a clear statement of which agent
                # this tool is for.
                "selected_agent_id": echo["agent_filter"],
                "error": None,
            },
        )

    # --------------------------
    # AUTO CREATE TOOL — create
    # --------------------------
    @post("/create-tool")
    async def create_tool(
        self,
        request: Request,
        db: AsyncSession,
        user: User,
    ) -> Template:
        """
        Create the drafted Tool Config, then rebuild the host page's table.

        Both the drafted config and the drafted SQL travel in hidden fields and are
        re-validated on the way in by ``tool_config_service`` — the same gates the
        Tool Configs form goes through — so nothing here trusts what the browser
        posted back, ``query_mode`` included.
        """
        payload = await SqlAssistCreateToolRequest.from_form(request)

        try:
            tool_config = await sql_assist_service.create_tool_from_draft(
                db,
                user.id,
                datasource_id=payload.datasource_id,
                agent_id=payload.data_agent_id,
                tool_name=payload.tool_name,
                table_name=payload.table_name,
                description=payload.description,
                config_json=payload.config_json,
                query_mode=payload.query_mode,
                sql_query=payload.sql_query,
            )
        except HTTPException as exc:
            # Back to the same form, so a name that is already taken can be fixed
            # without converting the query again. Every value the user had is
            # re-rendered from the validated payload rather than re-read from the
            # form, so what comes back is what the server accepted — the mode
            # included, or a SQL tool would come back as an empty builder one.
            return Template(
                template_name=_TOOL_FORM_TEMPLATE,
                context={
                    "mode": payload.query_mode,
                    "reason": "",
                    "error": str(exc.detail),
                    "tool_name": payload.tool_name,
                    "description": payload.description or "",
                    "table": payload.table_name,
                    "config_json": json.dumps(payload.config_json),
                    "sql_query": payload.sql_query,
                    "preview": payload.preview or "",
                    "selected_agent_id": (
                        str(payload.data_agent_id) if payload.data_agent_id else ""
                    ),
                    "agents": await sql_assist_service.get_agent_choices(db, user.id),
                    **payload.echo(),
                },
            )

        agent_id = payload.agent_filter

        return Template(
            template_name=_TOOL_CREATED_TEMPLATE,
            context={
                "tool_config": tool_config,
                "agent_filter": str(agent_id) if agent_id else "",
                # Rebuilds the host page's table out of band, the same way every
                # Tool Configs mutation does.
                "tool_configs": await tool_config_service.get_tool_config_views(
                    db, user.id, agent_id,
                ),
            },
        )
