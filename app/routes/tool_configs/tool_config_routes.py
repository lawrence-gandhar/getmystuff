"""
HTTP layer for Tool Configs. Accepts the request, hands the raw form to
tool_config_service, and renders a template — no business rules live here.

Beyond the usual CRUD there are three read-only schema endpoints the form uses:
:meth:`tables` (the objects inside the chosen datasource), :meth:`fields` (the
columns of the chosen table, plus the query builder built on them) and
:meth:`columns` (one table's columns as JSON, which the join builder calls as tables
are joined in). They exist so the builder offers the user's real schema instead of
free text.

Every mutation answers with the same fragment (see :meth:`_rows`): a success/error
marker plus an out-of-band refresh of the tool-configs table. The list can be
narrowed to one agent with ``?agent=<uuid>`` — what the tool count on the Data
Agents page links to — and that filter is carried through mutations as a hidden
field so the rebuilt table keeps showing the same subset.
"""

import uuid
from typing import Iterable, Optional, Sequence

from litestar import Controller, get, post
from litestar.background_tasks import BackgroundTask, BackgroundTasks
from litestar.connection import Request
from litestar.exceptions import HTTPException
from litestar.response import Template
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.auth import require_auth
from app.models.tool_configs import (
    AGGREGATION_FUNCTIONS,
    FILTER_OPERATORS,
    QUERY_MODE_BUILDER,
)
from app.models.user import User
from app.schemas.tool_configs import (
    ChildToolOptionsResponse,
    SchemaCascadeQuery,
    TableColumnsResponse,
    ToolConfigCreateRequest,
    ToolConfigDeleteRequest,
    ToolConfigListQuery,
    ToolConfigSetEnabledRequest,
    ToolConfigUpdateRequest,
)
from app.services.data_agents import data_agent_service
from app.services.deep_agents.prompt_sync_service import sync_tool_routing_prompt
from app.services.tool_configs import tool_chain_service, tool_config_service
from app.utils.validators import parse_optional_uuid

_ROWS_TEMPLATE = "tool_configs/partials/tool_config_rows_response.htm"
_FORM_TEMPLATE = "tool_configs/partials/tool_config_form.htm"
_OFFCANVAS_ERROR_TEMPLATE = "tool_configs/partials/offcanvas_error.htm"
_TABLES_TEMPLATE = "tool_configs/partials/table_field_response.htm"
_BUILDER_TEMPLATE = "tool_configs/partials/query_builder.htm"


def _prompt_sync_task(
    agent_ids: Iterable[int] | None,
) -> BackgroundTask | BackgroundTasks | None:
    """
    The background job that regenerates the routing prompts of the agents whose
    tools just changed, or ``None`` when nothing changed (a failed save).

    ``None`` ids are filtered out rather than guarded against at each call site:
    ``update_tool_config`` returns the previous agent id too, and a tool that was
    not moved reports the same id twice — a set with one entry, not two tasks.
    """
    ids = {agent_id for agent_id in (agent_ids or ()) if agent_id}

    if not ids:
        return None

    tasks = [BackgroundTask(sync_tool_routing_prompt, agent_id) for agent_id in ids]

    return tasks[0] if len(tasks) == 1 else BackgroundTasks(tasks)


class ToolConfigController(Controller):
    """The Tool Configs library — one query, owned by one data agent."""

    path = "/tool-configs"
    dependencies = {"user": require_auth}

    # --------------------------
    # LIST
    # --------------------------
    @get("/")
    async def index(
        self,
        request: Request,
        db: AsyncSession,
        user: User,
    ) -> Template:
        agent_id = ToolConfigListQuery.from_query(request).agent
        tool_configs = await tool_config_service.get_tool_config_views(
            db, user.id, agent_id,
        )

        # Named so the page can say *which* agent it is filtered to.
        agent_row = (
            await data_agent_service.get_data_agent(db, user.id, agent_id)
            if agent_id
            else None
        )

        return Template(
            template_name="tool_configs/index.htm",
            context={
                "user": user,
                "tool_configs": tool_configs,
                "agent_filter": str(agent_id) if agent_id else "",
                "agent_filter_name": agent_row.name if agent_row else None,
                "active": "tool_configs",
            },
        )

    # --------------------------
    # HELP
    # --------------------------
    @get("/help")
    async def help_page(self, user: User) -> Template:
        """
        The Tool Configs help page — the browsable form of
        documentations/TOOL_CONFIGS.md, opened in its own tab by the Help button on
        the list page.

        Static: it reads nothing and takes no query parameters, so there is no
        service call and no schema to parse. It is a route rather than a link to the
        markdown file because a help page has to arrive inside the application's own
        layout, behind the same auth as the page it explains.

        A literal path, so it cannot be confused with ``/{tool_config_id:uuid}/…``.
        """
        return Template(
            template_name="tool_configs/help.htm",
            context={"user": user, "active": "tool_configs"},
        )

    # --------------------------
    # FORMS (offcanvas bodies)
    # --------------------------
    @get("/new-form")
    async def new_form(
        self,
        request: Request,
        db: AsyncSession,
        user: User,
    ) -> Template:
        """
        Blank create form — the same partial the edit form uses. When the list is
        filtered to one agent, that agent is preselected.
        """
        try:
            agent_id = ToolConfigListQuery.from_query(request).agent
            choices = await self._form_choices(db, user)
        except HTTPException as exc:
            return Template(
                template_name=_OFFCANVAS_ERROR_TEMPLATE,
                context={"error": str(exc.detail)},
            )

        return Template(
            template_name=_FORM_TEMPLATE,
            context={
                "tool_config": None,
                "preselected_agent_id": str(agent_id) if agent_id else "",
                "form_action": "/tool-configs/create",
                "submit_label": "Create Tool Config",
                **self._builder_defaults(),
                **choices,
            },
        )

    @get("/{tool_config_id:uuid}/edit-form")
    async def edit_form(
        self,
        tool_config_id: uuid.UUID,
        db: AsyncSession,
        user: User,
    ) -> Template:
        """
        Edit form, prefilled. The table and column lists are re-read from the
        datasource so the builder can be reloaded with real options — including the
        columns of every joined table, so a joined query comes back exactly as it was
        saved. If the datasource is unreachable the saved values are still shown and
        editable.
        """
        try:
            tool_config = await tool_config_service.get_tool_config_view(
                db, user.id, tool_config_id,
            )
            choices = await self._form_choices(db, user)
        except HTTPException as exc:
            return Template(
                template_name=_OFFCANVAS_ERROR_TEMPLATE,
                context={"error": str(exc.detail)},
            )

        datasource_id = parse_optional_uuid(tool_config["datasource_id"], "Datasource")
        builder = await self._builder_context(
            db,
            user,
            datasource_id,
            tool_config["table_names"],
            tool_config["config"],
            tool_config["query_mode"],
        )

        return Template(
            template_name=_FORM_TEMPLATE,
            context={
                "tool_config": tool_config,
                "preselected_agent_id": tool_config["agent_id"],
                "form_action": f"/tool-configs/{tool_config_id}/update",
                "submit_label": "Save Changes",
                **builder,
                **choices,
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
        """
        Re-render the Table field for the datasource just picked, and reset the query
        builder and the mode selector along with it — see the partial for why the
        three move together.
        """
        selection = SchemaCascadeQuery.from_query(request).datasource_id

        table_names: list = []
        options: dict = {}
        schema_error = None
        if selection is not None:
            try:
                # Whether this datasource can run SQL at all decides whether the mode
                # selector offers it, and is known without reading any schema.
                options = await tool_config_service.get_join_options(
                    db, user.id, selection,
                )
                table_names = await tool_config_service.get_table_choices(
                    db, user.id, selection,
                )
            except HTTPException as exc:
                schema_error = str(exc.detail)

        return Template(
            template_name=_TABLES_TEMPLATE,
            context={
                **self._builder_defaults(),
                **options,
                "datasource_id": str(selection) if selection else "",
                "tables": table_names,
                "selected_tables": [],
                "schema_error": schema_error,
            },
        )

    # --------------------------
    # CASCADE — table → columns + builder
    # --------------------------
    @get("/fields")
    async def fields(
        self,
        request: Request,
        db: AsyncSession,
        user: User,
    ) -> Template:
        """
        Re-render the query builder against the columns of the tables just picked.

        The saved query is deliberately *not* carried over: a different table has
        different columns, so the previous selections — joins included — no longer
        mean anything.
        """
        query = SchemaCascadeQuery.from_query(request)
        builder = await self._builder_context(
            db, user, query.datasource_id, query.tables, config={},
        )

        return Template(template_name=_BUILDER_TEMPLATE, context=builder)

    # --------------------------
    # SCHEMA — one table's columns (JSON)
    # --------------------------
    @get("/columns")
    async def columns(
        self,
        request: Request,
        db: AsyncSession,
        user: User,
    ) -> dict:
        """
        The columns of one table, for the join builder: joining a table has to add
        that table's fields to every column dropdown, and which table that is only
        becomes known as the user picks it.

        A connection failure is reported in ``error`` rather than raised, so the
        builder can show the reason next to the join row instead of the offcanvas
        being replaced by an error page mid-edit.
        """
        query = SchemaCascadeQuery.from_query(request)
        name = query.table

        if query.datasource_id is None or not name:
            return TableColumnsResponse.failure(name, "Pick a table first").payload()

        try:
            columns = await tool_config_service.get_column_choices(
                db, user.id, query.datasource_id, name,
            )
        except HTTPException as exc:
            return TableColumnsResponse.failure(name, str(exc.detail)).payload()

        return TableColumnsResponse.build(
            {"table_name": name, "columns": columns, "error": None}
        ).payload()

    # --------------------------
    # SCHEMA — tools this one may embed (JSON)
    # --------------------------
    @get("/child-options")
    async def child_options(
        self,
        request: Request,
        db: AsyncSession,
        user: User,
    ) -> dict:
        """
        The tools the Nested Tools picker may offer, with the columns each returns.

        JSON rather than a partial because the picker is drawn client-side: choosing
        a tool has to repopulate the column dropdown beside it without a round trip,
        which needs the columns of *every* candidate up front.

        Filtered by the rules that would refuse the link on save, so the picker can
        never offer something the form then rejects — see
        ``tool_chain_service.embeddable_tools``.
        """
        query = SchemaCascadeQuery.from_query(request)

        if query.datasource_id is None:
            return ChildToolOptionsResponse.failure("Pick a datasource first").payload()

        try:
            tools = await tool_chain_service.embeddable_tools(
                db, user.id, query.datasource_id, query.exclude,
            )
            # Published graphs, offered in the same list. Not filtered by datasource —
            # a graph's nodes each name their own — so they are appended rather than
            # merged in, which keeps a picker whose first entries are the ones the
            # operator was already used to.
            tools += await tool_chain_service.embeddable_graphs(
                db, user.id, query.exclude,
            )
        except HTTPException as exc:
            return ChildToolOptionsResponse.failure(str(exc.detail)).payload()

        return ChildToolOptionsResponse.build({"tools": tools, "error": None}).payload()

    # --------------------------
    # CREATE
    # --------------------------
    @post("/create")
    async def create(self, request: Request, db: AsyncSession, user: User) -> Template:
        payload = await ToolConfigCreateRequest.from_form(request)
        error = None
        resync: set[int] = set()
        try:
            tool_config = await tool_config_service.create_tool_config(
                db,
                user.id,
                agent_id=payload.data_agent_id,
                datasource_id=payload.datasource_id,
                tool_name=payload.tool_name,
                table_names=payload.table_names,
                description=payload.description,
                config_json=payload.config_json,
                query_mode=payload.query_mode,
                sql_query=payload.sql_query,
                children=payload.children_json,
                sql_params=payload.sql_params_json,
                allow_recursive_aggregate=payload.allow_recursive_aggregate,
            )
            resync = {tool_config.data_agent_id}
        except HTTPException as exc:
            error = str(exc.detail)

        return await self._rows(db, user, payload.agent_filter, error, resync)

    # --------------------------
    # UPDATE
    # --------------------------
    @post("/{tool_config_id:uuid}/update")
    async def update(
        self,
        tool_config_id: uuid.UUID,
        request: Request,
        db: AsyncSession,
        user: User,
    ) -> Template:
        payload = await ToolConfigUpdateRequest.from_form(request)
        error = None
        resync: set[int] = set()
        try:
            resync = await tool_config_service.update_tool_config(
                db,
                user.id,
                tool_config_id,
                agent_id=payload.data_agent_id,
                datasource_id=payload.datasource_id,
                tool_name=payload.tool_name,
                table_names=payload.table_names,
                description=payload.description,
                config_json=payload.config_json,
                query_mode=payload.query_mode,
                sql_query=payload.sql_query,
                children=payload.children_json,
                sql_params=payload.sql_params_json,
                allow_recursive_aggregate=payload.allow_recursive_aggregate,
            )
        except HTTPException as exc:
            error = str(exc.detail)

        return await self._rows(db, user, payload.agent_filter, error, resync)

    # --------------------------
    # ENABLE / DISABLE
    # --------------------------
    @post("/{tool_config_id:uuid}/set-enabled")
    async def set_enabled(
        self,
        tool_config_id: uuid.UUID,
        request: Request,
        db: AsyncSession,
        user: User,
    ) -> Template:
        payload = await ToolConfigSetEnabledRequest.from_form(request)
        error = None
        resync: set[int] = set()
        try:
            resync = await tool_config_service.set_tool_config_enabled(
                db, user.id, tool_config_id, is_enabled=payload.is_enabled,
            )
        except HTTPException as exc:
            error = str(exc.detail)

        return await self._rows(db, user, payload.agent_filter, error, resync)

    # --------------------------
    # DELETE
    # --------------------------
    @post("/{tool_config_id:uuid}/delete")
    async def delete(
        self,
        tool_config_id: uuid.UUID,
        request: Request,
        db: AsyncSession,
        user: User,
    ) -> Template:
        payload = await ToolConfigDeleteRequest.from_form(request)
        error = None
        resync: set[int] = set()
        try:
            resync = await tool_config_service.delete_tool_config(
                db, user.id, tool_config_id,
            )
        except HTTPException as exc:
            error = str(exc.detail)

        return await self._rows(db, user, payload.agent_filter, error, resync)

    # --------------------------
    # Helpers
    # --------------------------
    @staticmethod
    async def _form_choices(db: AsyncSession, user: User) -> dict:
        """
        Dropdown data the create and edit forms both need. The query builder's own
        option lists come from :meth:`_builder_defaults`, which the cascade endpoints
        share — one source each, so the form and a mid-edit swap always agree.
        """
        return {
            "agents": await data_agent_service.get_agent_choices(db, user.id),
            "datasources": await tool_config_service.get_datasource_choices(db, user.id),
        }

    @staticmethod
    def _builder_defaults() -> dict:
        """
        The builder context for a form with nothing selected yet. Empty
        ``join_types`` is what keeps the Joins section out of the way until a
        relational datasource is chosen, and ``supports_sql`` false does the same
        for the SQL-query mode.
        """
        return {
            "base_table": "",
            "selected_tables": [],
            "tables": [],
            "columns": [],
            "column_map": {},
            "join_tables": [],
            "join_types": [],
            "supports_joins": False,
            "supports_sql": False,
            "query_mode": QUERY_MODE_BUILDER,
            "datasource_id": "",
            "config": {},
            "schema_error": None,
            "aggregation_functions": AGGREGATION_FUNCTIONS,
            "filter_operators": FILTER_OPERATORS,
        }

    @classmethod
    async def _builder_context(
        cls,
        db: AsyncSession,
        user: User,
        datasource_id: Optional[uuid.UUID],
        table_names: Sequence[str],
        config: dict,
        query_mode: str = QUERY_MODE_BUILDER,
    ) -> dict:
        """
        Everything the query builder renders from: the base table's columns, the
        other tables available to join, the columns of every selected table, and the
        join types this datasource's dialect supports.

        ``table_names`` is the multi-select's value, primary first. The **first** one
        is the base table, and the rest are the join candidates — so the Joins card
        offers exactly what the operator said this tool reads, rather than every
        object in the datasource. That is what keeps the two fields from disagreeing:
        the service refuses a join onto a table that is not in the list, and this
        makes it impossible to build one in the first place.

        ``query_mode`` rides along because the mode selector is rendered from the
        same context — a tool saved as SQL has to reopen showing its statement, not
        an empty builder.

        The connection error is returned rather than raised — a datasource that is
        temporarily down must not make an existing tool config uneditable — so the
        form shows a warning above the saved values instead of an error page.
        """
        selected = [str(name).strip() for name in table_names or [] if str(name).strip()]

        context = cls._builder_defaults()
        context["selected_tables"] = selected
        context["base_table"] = selected[0] if selected else ""
        context["join_tables"] = selected[1:]
        context["config"] = config or {}
        context["query_mode"] = query_mode or QUERY_MODE_BUILDER

        if datasource_id is None:
            return context

        context["datasource_id"] = str(datasource_id)

        try:
            context.update(
                await tool_config_service.get_join_options(db, user.id, datasource_id)
            )

            context["tables"] = await tool_config_service.get_table_choices(
                db, user.id, datasource_id,
            )

            if not selected:
                return context

            # Every selected table's columns: the base table's for the Columns card,
            # and the joined ones' for the join rows and their qualified references.
            column_map = await tool_config_service.get_column_map(
                db, user.id, datasource_id, selected,
            )
            context["column_map"] = column_map
            context["columns"] = column_map.get(context["base_table"], [])
        except HTTPException as exc:
            context["schema_error"] = str(exc.detail)
            context["columns"] = []
            context["column_map"] = {}

        return context

    @staticmethod
    async def _rows(
        db: AsyncSession,
        user: User,
        agent_id: Optional[uuid.UUID],
        error: str | None,
        resync_agent_ids: Iterable[int] | None = None,
    ) -> Template:
        """
        The HTMX response every mutation returns: marker + rebuilt table, still
        narrowed to whichever agent the user was filtered to.

        The filter arrives already parsed — each mutation's request schema validated
        it on the way in, so there is nothing left to interpret here.

        ``resync_agent_ids`` are the agents whose tool set just changed. Their Deep
        Agent routing prompts are regenerated in a background task — after this
        response has been sent — so saving a tool config stays as fast as it was
        before the feature existed. See
        app.services.deep_agents.prompt_sync_service for why losing one of these
        tasks is safe.
        """
        tool_configs = await tool_config_service.get_tool_config_views(
            db, user.id, agent_id,
        )

        return Template(
            template_name=_ROWS_TEMPLATE,
            context={
                "tool_configs": tool_configs,
                "agent_filter": str(agent_id) if agent_id else "",
                "error": error,
            },
            background=_prompt_sync_task(resync_agent_ids),
        )
