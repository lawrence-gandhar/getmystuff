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

from typing import List, Optional

from litestar import Controller, get, post
from litestar.connection import Request
from litestar.exceptions import HTTPException
from litestar.response import Template
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.auth import require_auth
from app.models.user import User
from app.services.sql_assist import sql_assist_service
from app.services.tool_configs import tool_config_service
from app.utils.validators import parse_optional_uuid

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
        db: AsyncSession,
        user: User,
        agent: Optional[str] = None,
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
                "agent_filter": (agent or "").strip(),
            },
        )

    # --------------------------
    # CASCADE — datasource → tables
    # --------------------------
    @get("/tables")
    async def tables(
        self,
        db: AsyncSession,
        user: User,
        datasource_id: Optional[str] = None,
    ) -> Template:
        """Re-render the table picker for the datasource just chosen."""
        selection = parse_optional_uuid(datasource_id, "Datasource")

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

        The table list arrives as repeated form fields from a multi-select, so it is
        read with ``getall`` — a single-value ``get`` would silently use one table
        out of several.
        """
        form = await request.form()

        try:
            result = await sql_assist_service.generate_sql(
                db,
                user.id,
                datasource_id=parse_optional_uuid(
                    form.get("datasource_id"), "Datasource",
                ),
                table_names=self._table_names(form),
                prompt=form.get("prompt", ""),
                llm_mode=form.get("llm_mode", ""),
                llm_api_key_id=parse_optional_uuid(
                    form.get("llm_api_key_id"), "AI API key",
                ),
                history_json=form.get("history_json", ""),
            )
        except HTTPException as exc:
            return Template(
                template_name=_ERROR_TEMPLATE,
                context={
                    "error": str(exc.detail),
                    # Keeps the conversation alive through a failed turn, so a
                    # refinement that times out doesn't reset the whole session.
                    "history_json": form.get("history_json", "") or "[]",
                },
            )

        return Template(
            template_name=_RESULT_TEMPLATE,
            context={
                **result,
                # Echoed so "Auto Create Tool" can ask for the same query to be
                # converted without the user re-picking any of it.
                **self._echo(form),
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

        A query that cannot be represented is answered with the reason, not a form:
        the tool builder holds columns, aggregations, grouping, filters and joins, and
        plenty of valid SQL needs more than that.
        """
        form = await request.form()
        echo = self._echo(form)

        try:
            draft = await sql_assist_service.draft_tool_config(
                db,
                user.id,
                datasource_id=parse_optional_uuid(
                    form.get("datasource_id"), "Datasource",
                ),
                table_names=self._table_names(form),
                sql=form.get("sql", ""),
                llm_mode=form.get("llm_mode", ""),
                llm_api_key_id=parse_optional_uuid(
                    form.get("llm_api_key_id"), "AI API key",
                ),
            )
            agents = await sql_assist_service.get_agent_choices(db, user.id)
        except HTTPException as exc:
            return Template(
                template_name=_ERROR_TEMPLATE,
                context={
                    "error": str(exc.detail),
                    "history_json": form.get("history_json", "") or "[]",
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

        The config travels in a hidden field and is re-validated on the way in by
        ``tool_config_service`` — the same gate the query builder's own output goes
        through — so nothing here trusts what the browser posted back.
        """
        form = await request.form()
        agent_filter = form.get("agent_filter", "")

        try:
            tool_config = await sql_assist_service.create_tool_from_draft(
                db,
                user.id,
                datasource_id=parse_optional_uuid(
                    form.get("datasource_id"), "Datasource",
                ),
                agent_id=parse_optional_uuid(form.get("data_agent_id"), "Data agent"),
                tool_name=form.get("tool_name", ""),
                table_name=form.get("table_name", ""),
                description=form.get("description", ""),
                config_json=form.get("config_json", ""),
            )
        except HTTPException as exc:
            # Back to the same form, so a name that is already taken can be fixed
            # without converting the query again.
            return Template(
                template_name=_TOOL_FORM_TEMPLATE,
                context={
                    "fits": True,
                    "reason": "",
                    "error": str(exc.detail),
                    "tool_name": form.get("tool_name", ""),
                    "description": form.get("description", ""),
                    "table": form.get("table_name", ""),
                    "config_json": form.get("config_json", ""),
                    "preview": form.get("preview", ""),
                    "selected_agent_id": form.get("data_agent_id", ""),
                    "agents": await sql_assist_service.get_agent_choices(db, user.id),
                    **self._echo(form),
                },
            )

        agent_id = parse_optional_uuid(agent_filter, "Data agent")

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

    # --------------------------
    # Helpers
    # --------------------------
    @staticmethod
    def _echo(form) -> dict:
        """
        The fields every step of the panel has to hand on: which datasource and tables
        the query was written against, which model wrote it, and the host page's agent
        filter. Re-read from the form each time rather than held anywhere, so a
        tampered value is just another value the services validate.
        """
        return {
            "datasource_id": form.get("datasource_id", ""),
            "llm_mode": form.get("llm_mode", ""),
            "llm_api_key_id": form.get("llm_api_key_id", ""),
            "agent_filter": form.get("agent_filter", ""),
        }
    @staticmethod
    def _table_names(form) -> List[str]:
        """
        Every selected table.

        Litestar's FormMultiDict exposes repeated keys through ``getall``, which
        raises on a key that isn't there — hence the explicit default. ``get`` would
        return only the first of several, silently generating a query against one
        table when the user picked four.
        """
        if hasattr(form, "getall"):
            return [str(name) for name in form.getall("table_names", [])]

        value = form.get("table_names")
        if value is None:
            return []
        return [str(name) for name in value] if isinstance(value, list) else [str(value)]
