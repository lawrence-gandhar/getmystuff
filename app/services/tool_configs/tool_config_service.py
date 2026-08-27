"""
Business logic for Tool Configs — one query a data agent is allowed to run.

Tool configs are authored here, in their own module, and are independent of the
Configurations section: nothing is shared or referenced between the two (see
app.models.tool_configs). The query is described in the same shape the
Configurations builder produces, so the two mean the same thing without being
coupled.

A tool config is written one of two ways, and ``query_mode`` says which (see the
model docstring for what each stores):

* ``builder`` — the structured query. :func:`validated_query_config` checks every
  identifier in it against the tables the query actually reads.
* ``sql`` — one read-only statement the operator wrote or approved.
  :func:`validated_tool_sql` holds it to
  :func:`app.utils.sql_guard.read_only_violation`: a single statement, a read, of
  bounded length. It does not, and cannot honestly, promise the SQL is *correct* —
  the database decides that when it runs. What it promises is that nothing but a
  read will be attempted.

Both modes go through :func:`_validated_fields`, which returns all three columns
together, so a mode switch can never leave the previous mode's query behind for
the executor to pick up.

Ownership: ``tool_configs`` has no ``user_id`` of its own — it comes from the
agent, so every function resolves the agent first (via data_agent_service, scoped
to the logged-in user) and only then touches the tool config. The datasource being
read is separately ownership-checked the same way.

The user's own tables and columns are read live for the form's dropdowns
(:func:`get_table_choices`, :func:`get_column_choices`, :func:`get_column_map`), and
filtered to the ones left switched on in Data Sources (app.utils.datasource_status) —
an inactive table or column is not offered at all. They are deliberately *not*
re-fetched when saving: a datasource that is momentarily unreachable must not stop a
tool config being edited. What is enforced on save is the shape of the query and the
safety of every name in it (see :func:`_validated_config`); whether each name is
still switched on is enforced where the query is actually run
(app.services.deep_agents.query_executor), so switching a column off never makes an
existing tool config uneditable — only unrunnable until it is fixed.

A query over a relational datasource may join further tables in; the rules for that
live in app.utils.query_joins, shared with the Configurations page so a join means
the same thing wherever it was authored.
"""

import re
import uuid
from typing import Any, Dict, List, NoReturn, Optional, Set, Tuple

from litestar.exceptions import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.db_utils import MAX_REFLECTED_TABLES, CRUDQueryBuilder
from app.db.tool_configs.queries import (
    fetch_tool_configs_with_details,
    tool_name_exists,
)
from app.models.data_agents import DataAgent
from app.models.datasource import DataSource
from app.models.tool_configs import (
    AGGREGATION_FUNCTION_VALUES,
    FILTER_OPERATOR_VALUES,
    FILTER_OPERATORS,
    QUERY_MODE_BUILDER,
    QUERY_MODE_SQL,
    QUERY_MODE_VALUES,
    SQL_PARAM_TYPE_VALUES,
    VALUELESS_FILTER_OPERATORS,
    ToolConfig,
)
from app.services.data_agents import data_agent_service
from app.services.datasource import datasource_service
from app.services.tool_configs import tool_chain_service
from app.utils.datasource_status import (
    NO_ACTIVE_TABLES_MESSAGE,
    active_column_names,
    active_table_names,
    inactive_table_message,
    is_table_active,
    no_active_columns_message,
)
from app.utils.query_joins import (
    RDBMS_DB_TYPES,
    build_join_sql,
    join_types_for,
    query_tables,
    supports_joins,
    validated_column_reference,
    validated_joins,
)
from app.utils.sql_guard import (
    MAX_SQL_LENGTH,
    bind_placeholders,
    normalised_sql,
    read_only_violation,
)
from app.utils.validators import (
    optional_text,
    parse_json_object,
    require_identifier,
    require_object_name,
)

tool_config_crud = CRUDQueryBuilder(ToolConfig)
datasource_crud = CRUDQueryBuilder(DataSource)
agent_crud = CRUDQueryBuilder(DataAgent)

_DESCRIPTION_MAX = 2000

# Caps on the query payload. Generous for real use, but bounded — the JSON arrives
# from a hidden form field, so "however many the client sent" is not an acceptable
# size for something that will be turned into a query.
_MAX_COLUMNS = 200
_MAX_AGGREGATIONS = 50
_MAX_GROUP_BY = 50
_MAX_FILTERS = 50
_MAX_FILTER_VALUE_LEN = 500

# An agent-supplied filter's parameter name and description — the two things a model
# is shown for it in the tool's argument schema. The name is short because it is an
# identifier the model has to reproduce exactly; the description has room for a
# sentence saying what the value means and what format it takes.
_MAX_PARAM_NAME_LEN = 64
_MAX_PARAM_DESCRIPTION_LEN = 300

# How many values a SQL-mode tool may ask the assistant for. Small, and about the
# tool call rather than about storage: every declared parameter is one more field the
# model has to fill correctly on every call, and a tool that needs five things named
# before it can run is a tool that is usually called wrong.
_MAX_SQL_PARAMS = 5

# How many tables one tool may read. The same cap the schema applies, restated here
# because the service is also reached by Ask AI's Auto Create Tool, which does not
# come through that form.
_MAX_TABLES = MAX_REFLECTED_TABLES

# An alias is emitted as `… AS alias`, so it has to be a plain identifier. Table and
# column names have their own rule, shared with the Configurations page — see
# app.utils.validators.require_object_name.
_ALIAS_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def tables_read(table_name: Any, extra_tables: Any) -> List[str]:
    """
    Every table a tool config reads, primary first and de-duplicated.

    The one place the two columns are put back together, because four consumers need
    the same answer — the list page, the edit form's multi-select, the routing prompt
    and the executor's active-table check — and a tool that reports its tables
    differently in any of them is a tool nobody can reason about.

    ``extra_tables`` is ``NULL`` for every row written before the column existed, and
    that means "one table" rather than being an error.
    """
    tables = [str(table_name)] if table_name else []

    for name in extra_tables or []:
        name = str(name or "").strip()
        if name and name not in tables:
            tables.append(name)

    return tables


# --------------------------------------------------------------------------
# Read
# --------------------------------------------------------------------------

async def get_tool_config_views(
    db: AsyncSession,
    user_id: int,
    agent_id: Optional[uuid.UUID] = None,
) -> List[dict]:
    """
    Every tool config this user owns, shaped for the list page: public uuids only,
    the agent and datasource it belongs to, and a readable preview of the query.

    ``agent_id`` filters to one agent — that is what the tool count on the Data
    Agents page links to. It is ownership-checked, so filtering by someone else's
    agent 404s rather than quietly returning nothing.

    Each row also carries its nesting: ``chain`` is the tools it embeds, deepest
    last, and ``embedded_in`` names the tools that embed it. Both are resolved in
    bulk from the rows already fetched rather than per row — a page of forty tools
    costs as many queries as the deepest chain has levels, not forty.
    """
    internal_agent_id = None
    if agent_id is not None:
        agent = await data_agent_service.get_data_agent(db, user_id, agent_id)
        internal_agent_id = agent.id

    rows = await fetch_tool_configs_with_details(db, user_id, internal_agent_id)
    chains = await tool_chain_service.build_chains(
        db, [(tool_config, datasource) for tool_config, _agent, datasource in rows],
    )
    embedded_in = await tool_chain_service.parent_names(
        db, [tool_config.id for tool_config, _agent, _datasource in rows],
    )

    return [
        {
            "uuid": str(tool_config.uuid),
            "tool_name": tool_config.tool_name,
            "description": tool_config.description,
            "table_name": tool_config.table_name,
            # Every table it reads, primary first — what the list page shows as the
            # tool's source. Built here so the row and the edit form's multi-select
            # cannot disagree about the order.
            "table_names": tables_read(
                tool_config.table_name, tool_config.extra_tables,
            ),
            "query_mode": tool_config.query_mode or QUERY_MODE_BUILDER,
            "is_enabled": tool_config.is_enabled,
            "agent_uuid": str(agent.uuid),
            "agent_name": agent.name,
            "agent_is_active": agent.is_active,
            "datasource_name": datasource.datasource_name,
            "datasource_is_active": datasource.is_active,
            "query_preview": build_query_preview(
                tool_config.config or {},
                tool_config.table_name,
                tool_config.sql_query,
            ),
            # The tools this one embeds, flattened with their indent level, and the
            # tools that embed it. Empty lists for a tool that stands alone, which
            # is what the row template checks.
            "chain": tool_chain_service.chain_view(chains[tool_config.id]),
            "embedded_in": embedded_in.get(tool_config.id, []),
            "created_at": tool_config.created_at,
            "updated_at": tool_config.updated_at,
        }
        for tool_config, agent, datasource in rows
    ]


async def get_tool_config(
    db: AsyncSession,
    user_id: int,
    tool_config_id: uuid.UUID,
) -> ToolConfig:
    """
    Resolve a tool config by its public uuid, scoped to the user who owns its agent.

    Two steps, because ``tool_configs`` carries no ``user_id``: fetch by uuid, then
    confirm the agent belongs to this user. A tool config owned by someone else
    raises 404 rather than 403 — a 403 would confirm the uuid is real.
    """
    tool_config = await tool_config_crud.get_by_uuid(db, tool_config_id)
    if not tool_config:
        raise HTTPException(status_code=404, detail="Tool config not found")

    agent = await agent_crud.get_one(
        db, filters={"id": tool_config.data_agent_id, "user_id": user_id},
    )
    if not agent:
        raise HTTPException(status_code=404, detail="Tool config not found")

    return tool_config


async def get_tool_config_view(
    db: AsyncSession,
    user_id: int,
    tool_config_id: uuid.UUID,
) -> dict:
    """
    One tool config shaped for its edit form — the agent and datasource are exposed
    as *their* public uuids so the dropdowns can preselect them, and ``config`` is
    handed over as-is for the query builder to reload.

    Both queries travel, whichever mode the tool is in: the form renders the
    builder and the SQL editor together and shows one of them, so switching mode
    mid-edit does not need a round trip — and a switch made by mistake can be
    switched back without having lost anything.
    """
    tool_config = await get_tool_config(db, user_id, tool_config_id)

    agent = await agent_crud.get_one(db, filters={"id": tool_config.data_agent_id})
    datasource = await datasource_crud.get_one(
        db, filters={"id": tool_config.datasource_id},
    )

    return {
        "uuid": str(tool_config.uuid),
        "tool_name": tool_config.tool_name,
        "description": tool_config.description,
        "table_name": tool_config.table_name,
        # The multi-select needs the whole list to mark its selected options; the
        # builder still needs to know which one is the base table, so both travel.
        "table_names": tables_read(tool_config.table_name, tool_config.extra_tables),
        "query_mode": tool_config.query_mode or QUERY_MODE_BUILDER,
        "sql_query": tool_config.sql_query or "",
        # The values this statement asks the assistant for, in the shape the form
        # posts back. Empty in builder mode, whose equivalent is inside `config`.
        "sql_params": list(tool_config.sql_params or []),
        "is_enabled": tool_config.is_enabled,
        "allow_recursive_aggregate": bool(tool_config.allow_recursive_aggregate),
        "agent_id": str(agent.uuid) if agent else "",
        "datasource_id": str(datasource.uuid) if datasource else "",
        "config": tool_config.config or {},
        # The tools this one embeds, in the shape the form posts back. One level
        # only — what a child embeds is edited on the child's own form.
        "children": await tool_chain_service.children_view(db, tool_config),
    }


async def get_datasource_choices(db: AsyncSession, user_id: int) -> List[dict]:
    """
    The user's datasources for the form's dropdown.

    Inactive ones are still offered — a tool is often defined before its datasource
    is switched on — but flagged, so the choice is informed. ``supports_joins`` rides
    along because the Joins section only makes sense for a relational datasource, and
    the form needs to know that before anything is fetched from it.

    ``supports_sql`` is the same idea for the SQL-query mode: a CSV file or a Mongo
    collection has no SQL to run, so the form offers the mode only where it means
    something rather than letting it be chosen and then refused on save.
    """
    datasources = await datasource_crud.get_many(
        db, filters={"user_id": user_id}, order_by="datasource_name",
    )
    return [
        {
            "uuid": str(datasource.uuid),
            "name": datasource.datasource_name,
            "is_active": datasource.is_active,
            "db_type": datasource.db_type,
            "supports_joins": supports_joins(datasource.db_type),
            "supports_sql": supports_sql(datasource.db_type),
        }
        for datasource in datasources
    ]


def supports_sql(db_type: Optional[str]) -> bool:
    """
    Whether a raw SQL tool config can be written against this datasource type.

    Relational only, and for the same reason ``query_executor`` accepts only those:
    a file datasource is read through pandas and a Mongo collection through an
    aggregation pipeline, so there is no statement to run against either.
    """
    return (db_type or "").strip().lower() in RDBMS_DB_TYPES


async def get_join_options(
    db: AsyncSession,
    user_id: int,
    datasource_id: Optional[uuid.UUID],
) -> dict:
    """
    What the Joins section of the form needs to know about one datasource: whether
    it can join at all, and which join types its dialect actually has.

    Returned as a dict rather than a bare bool so the template has one thing to
    check — an empty ``join_types`` is exactly "don't render the Joins section",
    which is also true when nothing is selected yet.
    """
    if datasource_id is None:
        return {"supports_joins": False, "join_types": (), "supports_sql": False}

    datasource = await _resolve_datasource(db, user_id, datasource_id)

    return {
        "supports_joins": supports_joins(datasource.db_type),
        "join_types": join_types_for(datasource.db_type),
        "supports_sql": supports_sql(datasource.db_type),
    }


async def get_table_choices(
    db: AsyncSession,
    user_id: int,
    datasource_id: uuid.UUID,
) -> List[str]:
    """
    The **active** tables, collections or files inside one datasource, read live for
    the form's second dropdown.

    Inactive ones are not offered at all — not greyed out, not flagged. A tool config
    is a standing permission for an agent to read something, and a table the user has
    switched off in Data Sources is exactly the thing they have said an agent may not
    read; letting one be picked here would make the switch advisory.

    ``get_datasource_objects`` already ownership-checks the datasource and raises a
    readable 400 when the connection fails, so a broken datasource shows a message
    in the form instead of an empty dropdown with no explanation. It also returns the
    saved ``configuration_data``, so the status is read without a second query.
    """
    details = await datasource_service.get_datasource_objects(
        db=db, datasource_id=datasource_id, user_id=user_id,
    )

    names = []
    for entry in details.get("objects") or []:
        # RDBMS/Mongo return plain names; file datasources return {"name", "file_id"}.
        name = entry["name"] if isinstance(entry, dict) else entry
        if name:
            names.append(name)

    active = active_table_names(details.get("configuration_data"), names)

    # A datasource with nothing in it and a datasource whose every table is switched
    # off are different problems with different fixes, so they are not both an empty
    # dropdown. The first is the form's existing "nothing to pick" state; the second
    # is named, because the user can act on it.
    if names and not active:
        raise HTTPException(status_code=400, detail=NO_ACTIVE_TABLES_MESSAGE)

    return sorted(active)


async def get_column_choices(
    db: AsyncSession,
    user_id: int,
    datasource_id: uuid.UUID,
    table_name: str,
) -> List[str]:
    """
    The **active** columns of one table, read live so the query builder offers real
    fields rather than free text, and only the ones the user has left switched on.

    Ownership and connection errors are handled by ``get_datasource_table_schema``.
    The status lives on the datasource row rather than in that response, so the row
    is resolved here — a second uuid lookup, against a query per column.
    """
    datasource = await _resolve_datasource(db, user_id, datasource_id)

    return await _active_column_choices(db, user_id, datasource, table_name)


async def _active_column_choices(
    db: AsyncSession,
    user_id: int,
    datasource: DataSource,
    table_name: str,
) -> List[str]:
    """
    One table's active column names, for a datasource already resolved.

    Split out so :func:`get_column_map` can resolve the datasource once for a whole
    joined query instead of once per table.
    """
    table_name = require_object_name(table_name, "Table")

    if not is_table_active(datasource.configuration_data, table_name):
        raise HTTPException(status_code=400, detail=inactive_table_message(table_name))

    details = await datasource_service.get_datasource_table_schema(
        db=db, datasource_id=datasource.uuid, user_id=user_id, table_name=table_name,
    )

    names = []
    for column in details.get("schema") or []:
        if isinstance(column, dict):
            name = column.get("name") or column.get("column") or column.get("column_name")
        else:
            name = column
        if name:
            names.append(str(name))

    active = active_column_names(datasource.configuration_data, table_name, names)

    if names and not active:
        raise HTTPException(
            status_code=400, detail=no_active_columns_message(table_name),
        )

    return active


async def get_column_map(
    db: AsyncSession,
    user_id: int,
    datasource_id: uuid.UUID,
    table_names: List[str],
) -> Dict[str, List[str]]:
    """
    The active columns of several tables at once, keyed by table name.

    A joined query needs every one of its tables' columns in the builder's
    dropdowns, so the edit form loads the base table plus each joined one in a
    single call. A table that cannot be read raises rather than being dropped from
    the map: the caller turns that into the same visible "schema could not be read"
    warning a single unreadable table already produces, instead of leaving the user
    with a dropdown that is quietly missing half its options. A table that has since
    been switched off raises for the same reason — the form has to say so, not just
    show fewer options than the saved query uses.
    """
    datasource = await _resolve_datasource(db, user_id, datasource_id)

    column_map: Dict[str, List[str]] = {}

    for table_name in table_names:
        if not table_name or table_name in column_map:
            continue
        column_map[table_name] = await _active_column_choices(
            db, user_id, datasource, table_name,
        )

    return column_map


def build_query_preview(
    config: dict,
    table_name: str,
    sql_query: Optional[str] = None,
) -> str:
    """
    Render a tool config's query as readable SQL for the list page.

    Display only — it is never executed. Building it here rather than in the
    template keeps the list, the form and any future runtime from disagreeing about
    what a config means.

    A SQL-mode tool has nothing to render: its stored statement *is* the query, so
    it is returned as-is. That is what makes the list page's Query column honest
    across both modes without the caller having to know which mode it is looking
    at — passing ``sql_query`` is enough.
    """
    if (sql_query or "").strip():
        return sql_query.strip()

    sql = f"SELECT {_preview_selection(config)} FROM {table_name}"

    for clause in build_join_sql(config.get("joins")):
        sql += f" {clause}"

    conditions = _preview_conditions(config)
    if conditions:
        sql += f" WHERE {conditions}"

    group_by = [column for column in config.get("group_by") or [] if column]
    if group_by:
        sql += " GROUP BY " + ", ".join(group_by)

    return sql


def _preview_selection(config: dict) -> str:
    """
    The SELECT list: plain columns first, then aggregations.

    ``*`` when the config names nothing, which is shorthand for what the executor
    actually runs: every **active** column of every table the query reads, the joined
    ones included (see
    :func:`app.services.deep_agents.query_executor._selected_columns`).
    """
    selected = [
        _with_alias(column.get("column"), column.get("alias"))
        for column in config.get("columns") or []
        if column.get("column")
    ]
    selected += [
        _with_alias(
            f"{(aggregation.get('type') or '').upper()}({aggregation.get('column')})",
            aggregation.get("alias"),
        )
        for aggregation in config.get("aggregations") or []
        if aggregation.get("column") and aggregation.get("type")
    ]

    return ", ".join(selected) if selected else "*"


def _preview_conditions(config: dict) -> str:
    """
    The WHERE clause, or "" when the config has no filters.

    An agent-supplied filter has no stored value, so it renders as its parameter
    name in placeholder form — ``created_at > :created_after``. Rendering it as
    ``'None'`` would show the operator, and the model that reads this preview in its
    routing prompt, a filter comparing against the literal string "None".
    """
    return " AND ".join(
        _preview_condition(entry)
        for entry in config.get("filters") or []
        if entry.get("column")
    )


def _preview_condition(entry: dict) -> str:
    """One filter as readable SQL, in whichever of its three forms it takes."""
    column = entry.get("column")
    operator = entry.get("operator")

    if operator in VALUELESS_FILTER_OPERATORS:
        # Rendered as the SQL it actually becomes, not as the label on the dropdown:
        # "IS BLANK" is not a thing a database understands, and this preview is read
        # by an operator checking the query and by the model in its routing prompt.
        return _valueless_preview(column, operator)

    if entry.get("agent_supplied"):
        return f"{column} {operator} :{entry.get('param')}"

    return f"{column} {operator} '{entry.get('value')}'"


def _valueless_preview(column: Any, operator: str) -> str:
    """
    The SQL a value-less operator stands for.

    ``TRIM`` is shown unconditionally here even though the executor only applies it
    to a text column — a preview cannot reflect the table, and showing the fuller
    form is the honest way round: it describes what the operator means rather than
    understating it.
    """
    if operator == "IS BLANK":
        return f"({column} IS NULL OR TRIM({column}) = '')"

    if operator == "IS NOT BLANK":
        return f"({column} IS NOT NULL AND TRIM({column}) <> '')"

    return f"{column} {operator}"


def _with_alias(expression: str, alias: Optional[str]) -> str:
    return f"{expression} AS {alias}" if alias else expression


# --------------------------------------------------------------------------
# Write
# --------------------------------------------------------------------------

async def create_tool_config(
    db: AsyncSession,
    user_id: int,
    agent_id: Optional[uuid.UUID],
    datasource_id: Optional[uuid.UUID],
    tool_name: str,
    table_names: Any,
    description: Optional[str] = None,
    config_json: Optional[str] = None,
    query_mode: Optional[str] = None,
    sql_query: Optional[str] = None,
    sql_params: Optional[Any] = None,
    children: Optional[Any] = None,
    allow_recursive_aggregate: bool = False,
) -> ToolConfig:
    """
    Create a tool config and give it to one agent. Enabled on creation.

    ``table_names`` is the tables the tool reads, primary first. A bare string is
    accepted as a one-table list, so a caller that only ever had one table — Ask AI's
    Auto Create Tool, a test — needs no change.

    ``children`` is the tools this one embeds (see
    :mod:`app.services.tool_configs.tool_chain_service`). They are linked after the
    row exists, because a link needs the parent's id, and inside the same
    transaction, so a tool and its nesting are saved together or not at all.
    """
    agent = await _resolve_agent(db, user_id, agent_id)
    datasource = await _resolve_datasource(db, user_id, datasource_id)

    fields = await _validated_fields(
        db,
        agent,
        datasource,
        tool_name,
        table_names,
        description,
        config_json,
        query_mode,
        sql_query,
        sql_params,
        allow_recursive_aggregate=allow_recursive_aggregate,
    )

    # Checked before the row is written, because `create` commits: a refusal after
    # it would leave the tool created and its nesting rejected, which is a
    # half-saved form.
    links = await tool_chain_service.validated_children(
        db,
        user_id,
        ToolConfig(datasource_id=datasource.id, **fields),
        children,
    )

    try:
        tool_config = await tool_config_crud.create(db, {
            "data_agent_id": agent.id,
            "datasource_id": datasource.id,
            "is_enabled": True,
            **fields,
        })
    except IntegrityError as exc:
        await _fail_on_duplicate_name(db, fields["tool_name"], agent, exc)

    if links:
        await tool_chain_service.replace_child_links(db, tool_config.id, links)
        await db.commit()

    return tool_config


async def update_tool_config(
    db: AsyncSession,
    user_id: int,
    tool_config_id: uuid.UUID,
    agent_id: Optional[uuid.UUID],
    datasource_id: Optional[uuid.UUID],
    tool_name: str,
    table_names: Any,
    description: Optional[str] = None,
    config_json: Optional[str] = None,
    query_mode: Optional[str] = None,
    sql_query: Optional[str] = None,
    sql_params: Optional[Any] = None,
    children: Optional[Any] = None,
    allow_recursive_aggregate: bool = False,
) -> Set[int]:
    """
    Update a tool config, including moving it to a different agent or datasource —
    both are ownership-checked afresh rather than trusted from the form.

    Returns the ids of every agent whose tool set changed, for the caller to hand to
    the Deep Agent prompt sync. Moving a tool between agents changes *two* agents:
    the tool leaves one and joins the other, and the one it left is still describing
    it in its routing prompt. Returning both is why that case cannot be forgotten at
    the call site.
    """
    tool_config = await get_tool_config(db, user_id, tool_config_id)
    previous_agent_id = tool_config.data_agent_id
    agent = await _resolve_agent(db, user_id, agent_id)
    datasource = await _resolve_datasource(db, user_id, datasource_id)

    fields = await _validated_fields(
        db,
        agent,
        datasource,
        tool_name,
        table_names,
        description,
        config_json,
        query_mode,
        sql_query,
        sql_params,
        allow_recursive_aggregate=allow_recursive_aggregate,
        # Exclude itself from the duplicate check only while it stays on the same
        # agent; moved to another agent it has to clear that agent's names.
        exclude_id=tool_config.id if agent.id == tool_config.data_agent_id else None,
    )

    # Validated against the tool as it will be *after* this save, not as it is
    # stored: the mode, the statement and the tables may all be changing in this
    # same request, and a child has to fit the query that is about to exist.
    links = await tool_chain_service.validated_children(
        db,
        user_id,
        ToolConfig(id=tool_config.id, datasource_id=datasource.id, **fields),
        children,
    )

    try:
        await tool_config_crud.update(db, tool_config.id, {
            "data_agent_id": agent.id,
            "datasource_id": datasource.id,
            **fields,
        })
    except IntegrityError as exc:
        await _fail_on_duplicate_name(db, fields["tool_name"], agent, exc)

    await tool_chain_service.replace_child_links(db, tool_config.id, links)
    await db.commit()

    return {agent.id, previous_agent_id}


async def set_tool_config_enabled(
    db: AsyncSession,
    user_id: int,
    tool_config_id: uuid.UUID,
    is_enabled: bool,
) -> Set[int]:
    """
    Switch one tool off (or back on) without losing its definition — the quick way
    to revoke a capability while a datasource is investigated.

    Returns the owning agent's id: enabling or disabling a tool changes which tools
    the agent's routing prompt should describe, so it needs regenerating either way.

    Switching *off* a tool that another tool embeds is refused. The parent would
    keep running with its filter gone — a query that still returns rows, just more
    of them than it should — so the switch has to be a decision about the parents
    too, made by someone who can see them named.
    """
    tool_config = await get_tool_config(db, user_id, tool_config_id)

    if not is_enabled:
        await tool_chain_service.require_not_embedded(
            db, tool_config, "cannot be disabled",
        )

    await tool_config_crud.update(db, tool_config.id, {"is_enabled": is_enabled})

    return {tool_config.data_agent_id}


async def delete_tool_config(
    db: AsyncSession,
    user_id: int,
    tool_config_id: uuid.UUID,
) -> Set[int]:
    """
    Delete a tool config. The agent and the datasource are untouched.

    Returns the owning agent's id, read *before* the delete — afterwards there is no
    row to read it from.

    A tool another tool embeds cannot be deleted. The foreign key would cascade the
    link away happily, and that is exactly the problem: the parent would go on
    running with one fewer restriction and nothing would say so.
    """
    tool_config = await get_tool_config(db, user_id, tool_config_id)  # ownership check
    await tool_chain_service.require_not_embedded(db, tool_config, "cannot be deleted")

    agent_id = tool_config.data_agent_id

    await tool_config_crud.delete(db, tool_config.id)

    return {agent_id}


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------

async def _resolve_agent(
    db: AsyncSession,
    user_id: int,
    agent_id: Optional[uuid.UUID],
) -> DataAgent:
    """The owning agent is required — a tool config with no agent has no meaning."""
    if agent_id is None:
        raise HTTPException(status_code=400, detail="Data agent is required")

    return await data_agent_service.get_data_agent(db, user_id, agent_id)


async def _resolve_datasource(
    db: AsyncSession,
    user_id: int,
    datasource_id: Optional[uuid.UUID],
) -> DataSource:
    """
    The datasource is required too: the query has to read from something. Scoping
    the lookup to ``user_id`` is what stops one user pointing a tool at another
    user's datasource by pasting its uuid.
    """
    if datasource_id is None:
        raise HTTPException(status_code=400, detail="Datasource is required")

    datasource = await datasource_crud.get_by_uuid(
        db, datasource_id, extra_filters={"user_id": user_id},
    )
    if not datasource:
        raise HTTPException(status_code=404, detail="Datasource not found")

    return datasource


async def _validated_fields(
    db: AsyncSession,
    agent: DataAgent,
    datasource: DataSource,
    tool_name: str,
    table_names: Any,
    description: Optional[str],
    config_json: Optional[str],
    query_mode: Optional[str] = None,
    sql_query: Optional[str] = None,
    sql_params: Optional[Any] = None,
    allow_recursive_aggregate: bool = False,
    exclude_id: Optional[int] = None,
) -> dict:
    """
    Validate the writable fields once for both create and update, returning them as
    the column dict to persist.

    The datasource is needed for more than ownership: its type decides whether the
    query may join at all, whether raw SQL can be run against it, and its first table
    is the one every unqualified column reference belongs to.

    All four query columns are always in the returned dict, so whichever mode is
    saved clears the other mode's leftovers. Without that, switching a tool to SQL
    and back would leave a stale statement in ``sql_query`` for the executor to
    prefer over the config the operator is now looking at — or, worse, leave
    ``sql_params`` declaring arguments a builder-mode tool has no use for, so the
    model is asked for a value that goes nowhere. ``extra_tables`` is in it for the
    same reason: a tool edited down from three tables to one must not keep reporting
    three.
    """
    tool_name = require_identifier(tool_name, "Tool name")

    if await tool_name_exists(db, agent.id, tool_name, exclude_id=exclude_id):
        raise HTTPException(
            status_code=400,
            detail=f"Agent '{agent.name}' already has a tool named '{tool_name}'",
        )

    base_table, extra_tables = validated_tables(table_names)
    mode = _validated_query_mode(query_mode, datasource)

    fields = {
        "tool_name": tool_name,
        "table_name": base_table,
        "extra_tables": extra_tables,
        "description": optional_text(description, "Description", _DESCRIPTION_MAX),
        "query_mode": mode,
        # Always written, like the query columns above and for the same reason: a
        # capability left at its stored value by an edit that did not mention it is
        # a permission nobody remembers granting.
        "allow_recursive_aggregate": bool(allow_recursive_aggregate),
    }

    if mode == QUERY_MODE_SQL:
        statement = validated_tool_sql(sql_query)
        return {
            **fields,
            "config": {},
            "sql_query": statement,
            "sql_params": validated_sql_params(sql_params, statement),
        }

    config = validated_query_config(config_json, base_table, datasource.db_type)
    _require_joins_within_selection(config, base_table, extra_tables)

    return {**fields, "config": config, "sql_query": None, "sql_params": None}


def validated_tables(table_names: Any) -> Tuple[str, List[str]]:
    """
    Split the submitted table list into the primary table and the rest.

    Public because a second caller now needs the same split from the same rules:
    app.services.query_test.query_test_service, testing the query a form is holding
    before it is saved. The primary table decides what a builder query is built
    against, so the test has to pick it exactly as the save will.

    The **first** selection is the primary one, and that is a choice with
    consequences, so it is fixed rather than sorted: in builder mode it is the base
    table every join hangs off and every bare column reference means. Re-ordering the
    list would silently re-point a saved query at a different table.

    Duplicates are dropped rather than refused — a browser can post the same option
    twice, and "you selected orders twice" is not a problem the user caused. An empty
    list is refused, with the wording of a required field, because a tool with nothing
    to read is not a tool.
    """
    if isinstance(table_names, str):
        table_names = [table_names]

    names: List[str] = []
    for raw in table_names or []:
        if not str(raw or "").strip():
            continue
        name = require_object_name(raw, "Table")
        if name not in names:
            names.append(name)

    if not names:
        raise HTTPException(status_code=400, detail="Table is required")

    if len(names) > _MAX_TABLES:
        raise HTTPException(
            status_code=400,
            detail=f"A tool can read at most {_MAX_TABLES} tables",
        )

    return names[0], names[1:]


def _require_joins_within_selection(
    config: dict,
    base_table: str,
    extra_tables: List[str],
) -> None:
    """
    Every table a builder join brings in has to be one the operator selected.

    Two fields describe which tables a built query reads — the Tables multi-select
    and the Joins card — and this is what stops them disagreeing. A join onto a table
    that is not in the list would produce a tool whose recorded scope is narrower than
    what it reads: the routing prompt would understate it, and the run-time active
    check would never look at it.

    Only checked in builder mode. A SQL statement is not parsed here, so what it reads
    is what the operator says it reads.
    """
    selected = {base_table, *extra_tables}

    for entry in config.get("joins") or []:
        for key in ("table", "left_table"):
            name = str(entry.get(key) or "")
            if name and name not in selected:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"The query joins '{name}', which is not one of the tables "
                        "selected for this tool. Add it to the Tables field or remove "
                        "the join."
                    ),
                )


def _validated_query_mode(query_mode: Optional[str], datasource: DataSource) -> str:
    """
    Which of the two ways this query is written, defaulting to the builder.

    Blank means builder rather than being an error: every tool config written
    before SQL mode existed posts no mode at all, and so does any caller that only
    knows about the builder.

    SQL mode is refused for a non-relational datasource here, at save time, rather
    than being stored and failing on the agent's first call — a tool that can never
    run is a configuration mistake, and the operator is standing right in front of
    the form.
    """
    mode = (query_mode or QUERY_MODE_BUILDER).strip().lower()

    if mode not in QUERY_MODE_VALUES:
        raise HTTPException(
            status_code=400,
            detail="Choose whether the query is built or written as SQL",
        )

    if mode == QUERY_MODE_SQL and not supports_sql(datasource.db_type):
        raise HTTPException(
            status_code=400,
            detail=(
                f"'{datasource.datasource_name}' is not a relational datasource, so "
                "it cannot run a SQL query. Use the query builder instead, or pick a "
                "PostgreSQL, MySQL or SQLite datasource."
            ),
        )

    return mode


def validated_tool_sql(
    sql_query: Optional[str],
    max_length: int = MAX_SQL_LENGTH,
) -> str:
    """
    Validate a raw SQL tool query and return it normalised, ready to store.

    Public for the same reason :func:`validated_query_config` is: Ask AI saves
    tools through this module, and the Deep Agents executor re-checks a stored
    statement before running it. One definition of an acceptable query, checked on
    the way in and again on the way out.

    **Any read-only statement is accepted.** ``DISTINCT``, ``ORDER BY``, ``LIMIT``,
    ``HAVING``, subqueries, CTEs, window functions, ``UNION``, ``CASE`` — all of it,
    because the point of this mode is that the query builder's subset is not the
    limit of what a tool may run. What is refused is anything that is not a single
    read: see :func:`app.utils.sql_guard.read_only_violation`.

    Syntax is not checked, and cannot honestly be: dialects differ, and a parser
    strict enough to be worth trusting would reject valid queries. A syntax error
    surfaces when the tool is run, named by the database.

    ``max_length`` defaults to the length rule every hand-written statement has always
    had, and is raised by exactly one caller: the graph designer's union node, whose
    statement this application composed out of fragments that each passed this check
    already. See :data:`app.utils.sql_guard.MAX_BUILT_SQL_LENGTH` for why that is a
    different question from a pasted dump.
    """
    statement = normalised_sql(sql_query)

    if not statement:
        raise HTTPException(
            status_code=400,
            detail="Write the SQL query this tool should run",
        )

    violation = read_only_violation(statement, max_length=max_length)
    if violation:
        raise HTTPException(
            status_code=400,
            detail=(
                f"The SQL query {violation}. A tool config runs one read-only "
                "statement — the agent can read data, never change it."
            ),
        )

    return statement


def validated_query_config(
    config_json: Optional[str],
    base_table: str,
    db_type: Optional[str],
) -> dict:
    """
    Validate the query payload built by the form and return it normalised.

    Public because it is the definition of a valid query config, and a second module
    now needs to be held to it: the AI SQL assistant converts a generated query into
    this shape (app.services.sql_assist.sql_assist_service), and a config it produced
    has to be exactly as trustworthy as one the builder produced. One validator, so
    the two cannot diverge.

    Everything is rebuilt field by field rather than stored as received, so only
    known keys are ever persisted and every name in the result has been checked.
    An empty selection is allowed and means "all columns" — the same default the
    Configurations builder uses.

    The joins are validated first because they decide what the rest may refer to:
    with a join in the query, ``orders.total`` is only a legal column reference if
    ``orders`` is one of the tables actually joined (see
    :func:`app.utils.query_joins.validated_column_reference`).

    The grouping is checked last, once every reference is known to be a real one —
    see :func:`_require_grouped_selection`.
    """
    raw = parse_json_object(config_json, "Query configuration")

    joins = validated_joins(raw.get("joins"), base_table, db_type)
    tables = query_tables(joins, base_table)

    config = {
        "columns": _validated_columns(raw.get("columns"), tables),
        "aggregations": _validated_aggregations(raw.get("aggregations"), tables),
        "group_by": _validated_group_by(raw.get("group_by"), tables),
        "filters": _validated_filters(raw.get("filters"), tables),
        "joins": joins,
    }

    _require_grouped_selection(config, base_table)

    return config


def _require_grouped_selection(config: dict, base_table: str) -> None:
    """
    Refuse a grouped query that selects a column it does not group.

    Once a query aggregates or groups, MySQL (ONLY_FULL_GROUP_BY, on by default) and
    PostgreSQL both accept only columns that are aggregated or grouped:

        SELECT list is not in GROUP BY clause and contains nonaggregated column
        'x.y' which is not functionally dependent on columns in GROUP BY clause

    That is a query that can never run, so it is refused when the tool is saved
    rather than when an agent calls it mid-conversation — which is where it used to
    surface, as a tool that fails in front of a visitor for a reason the operator
    could not see from the form.

    **Refused, not corrected.** Adding the column to the grouping would be a
    different query — one row per pair instead of one per group — and the operator
    is the only person who knows which they meant. The message names both ways out.

    A column that is functionally dependent on a grouped primary key is legal SQL and
    is refused here anyway: nothing at this layer knows which columns are keys (the
    config is validated without touching the datasource), and the fix — grouping by
    that column too — returns exactly the same rows when the dependency is real.

    An empty selection is not the exception it looks like. It means "every column",
    which :func:`app.services.deep_agents.query_executor._selected_columns` expands
    to every active column of every table the query reads — so a grouped query that
    selects nothing specific is the same violation written shorter.
    """
    aggregations = config.get("aggregations") or []
    group_by = config.get("group_by") or []
    columns = config.get("columns") or []

    if not aggregations and not group_by:
        return

    if group_by and not columns and not aggregations:
        raise HTTPException(
            status_code=400,
            detail=(
                "This query groups rows but selects every column, which the database "
                "will refuse. Add the grouped columns and the aggregations you want "
                "to Columns and Aggregations, or remove the grouping."
            ),
        )

    grouped = {_grouping_key(entry, base_table) for entry in group_by}

    for entry in columns:
        reference = entry.get("column")

        if _grouping_key(reference, base_table) not in grouped:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Column '{reference}' is selected but not grouped. A query that "
                    "aggregates can only select columns that are also in Group By — "
                    f"add '{reference}' to Group By, aggregate it instead (COUNT, "
                    "SUM, AVG, MIN, MAX), or remove it from Columns."
                ),
            )


def _grouping_key(reference: Any, base_table: str) -> str:
    """
    One column reference in the single form the grouping check compares.

    An unqualified name means the base table (see
    :func:`app.utils.query_joins.validated_column_reference`), so ``total`` and
    ``orders.total`` in a query based on ``orders`` are one column and not two —
    which they have to be, because the builder writes the bare form until a join is
    added and the qualified form afterwards.
    """
    name = str(reference or "").strip().lower()

    return name if "." in name else f"{base_table.strip().lower()}.{name}"


def _validated_columns(raw: Any, tables: List[str]) -> List[dict]:
    entries = _as_list(raw, "Columns", _MAX_COLUMNS)

    columns = []
    for entry in entries:
        entry = _as_dict(entry, "Column")
        columns.append({
            "column": validated_column_reference(
                entry.get("column"), "Column", tables,
            ),
            "alias": _optional_alias(entry.get("alias")),
        })

    return columns


def _validated_aggregations(raw: Any, tables: List[str]) -> List[dict]:
    entries = _as_list(raw, "Aggregations", _MAX_AGGREGATIONS)

    aggregations = []
    for entry in entries:
        entry = _as_dict(entry, "Aggregation")
        function = (entry.get("type") or "").strip().lower()

        if function not in AGGREGATION_FUNCTION_VALUES:
            raise HTTPException(
                status_code=400,
                detail="Every aggregation needs a valid function (COUNT, SUM, AVG, MIN or MAX)",
            )

        aggregations.append({
            "type": function,
            "column": validated_column_reference(
                entry.get("column"), "Aggregation column", tables,
            ),
            "alias": _optional_alias(entry.get("alias")),
        })

    return aggregations


def _validated_group_by(raw: Any, tables: List[str]) -> List[str]:
    entries = _as_list(raw, "Group by", _MAX_GROUP_BY)
    return [
        validated_column_reference(entry, "Group by column", tables)
        for entry in entries
    ]


def _validated_filters(raw: Any, tables: List[str]) -> List[dict]:
    entries = _as_list(raw, "Filters", _MAX_FILTERS)

    filters = []
    for entry in entries:
        entry = _as_dict(entry, "Filter")
        operator = (entry.get("operator") or "").strip()

        if operator not in FILTER_OPERATOR_VALUES:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Every filter needs a valid operator ("
                    + ", ".join(FILTER_OPERATORS) + ")"
                ),
            )

        column = validated_column_reference(
            entry.get("column"), "Filter column", tables,
        )

        if operator in VALUELESS_FILTER_OPERATORS:
            # Stored with no value key at all, rather than with an empty one. The
            # executor's builders for these ignore whatever they are handed, so an
            # empty string would be inert either way — but a filter row carrying a
            # value that provably cannot affect the query is the kind of thing that
            # gets read as meaningful later.
            filters.append({"column": column, "operator": operator})
            continue

        if _is_truthy(entry.get("agent_supplied")):
            filters.append({
                "column": column,
                "operator": operator,
                # No stored value: the agent supplies it per call. Absent rather
                # than empty-string so nothing downstream can mistake a parameter
                # for a filter that happens to compare against "".
                "agent_supplied": True,
                "required": _is_truthy(entry.get("required", True)),
                "param": _validated_param_name(entry.get("param"), column, filters),
                "description": _validated_param_description(entry.get("description")),
            })
            continue

        value = str(entry.get("value") or "").strip()
        if not value:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Every filter needs a value to compare against, or must be "
                    "marked as supplied by the agent"
                ),
            )
        if len(value) > _MAX_FILTER_VALUE_LEN:
            raise HTTPException(
                status_code=400,
                detail=(
                    "A filter value cannot be longer than "
                    f"{_MAX_FILTER_VALUE_LEN} characters"
                ),
            )

        filters.append({
            "column": column,
            "operator": operator,
            "value": value,
        })

    return filters


def _is_truthy(raw: Any) -> bool:
    """
    A checkbox, however the form or a JSON caller expressed it.

    The builder posts its config as JSON so a real ``bool`` is the common case, but
    ``"on"`` (an HTML checkbox), ``"true"`` and ``1`` all reach here from one caller
    or another — and a filter silently staying fixed because ``"true"`` is a
    non-empty string that nobody compared properly is not a failure anyone would see
    until an agent could not narrow a query.
    """
    if isinstance(raw, bool):
        return raw

    if isinstance(raw, (int, float)):
        return bool(raw)

    return str(raw or "").strip().lower() in ("1", "true", "t", "yes", "y", "on")


def _validated_param_name(
    raw: Any,
    column: str,
    existing: List[dict],
    label: str = "An agent-supplied filter",
) -> str:
    """
    The argument name the agent passes this value under.

    Derived from the column when the operator did not name it, because a name is
    required — it is what the model sees in the tool's schema — and asking for one
    per filter is friction for the common case where the column name is the obvious
    answer. ``projects.created_at`` becomes ``created_at``. A SQL-mode parameter has
    no column to fall back to and passes ``""``, so the operator's name is the only
    one there is.

    Restricted to an identifier because it becomes a Pydantic field name in
    :mod:`app.services.deep_agents.tool_factory`. It never reaches SQL — the value
    it carries is bound, and what it is compared against comes from the stored
    reference or from the operator's own statement — so this is about the schema
    being well-formed, not about injection.

    ``label`` names the thing in the message, because the same rule is now reached
    from two places and "an agent-supplied filter" is the wrong noun for a parameter
    on a statement that has no filters.
    """
    name = str(raw or "").strip() or str(column).rsplit(".", 1)[-1]
    name = re.sub(r"[^0-9a-zA-Z_]", "_", name).strip("_").lower()

    if not name or name[0].isdigit():
        raise HTTPException(
            status_code=400,
            detail=(
                f"{label} needs a parameter name starting with a letter or underscore"
            ),
        )

    if len(name) > _MAX_PARAM_NAME_LEN:
        raise HTTPException(
            status_code=400,
            detail=(
                "A parameter name cannot be longer than "
                f"{_MAX_PARAM_NAME_LEN} characters"
            ),
        )

    if any(other.get("param") == name for other in existing):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Two parameters are both named '{name}'. Give one of them a "
                "different name."
            ),
        )

    return name


def validated_sql_params(sql_params: Any, sql_query: str) -> Optional[List[dict]]:
    """
    The values a SQL-mode tool asks the assistant for, checked against its statement.

    Builder mode's equivalent is an ``agent_supplied`` filter, and it can be checked
    against the config it lives in. A statement has no config, so the check that
    matters here is the other direction: **every declared name must actually appear
    as a placeholder in the SQL**. A parameter the statement never uses is a field the
    model is asked to fill on every call for no effect, and the operator who declared
    it believes it is filtering something.

    The opposite direction — a placeholder nothing fills — is
    ``tool_chain_service._require_every_placeholder_bound``'s, because a placeholder
    may equally be filled by a nested tool and that check needs the links.

    Returns ``None`` rather than ``[]`` for a tool that declares nothing, matching
    the column: NULL is what every row written before this existed says, and it says
    "no arguments" rather than "an empty list of them".
    """
    entries = _as_list(sql_params, "Assistant-supplied values", _MAX_SQL_PARAMS)

    if not entries:
        return None

    placeholders = _placeholders_in_sql(sql_query)
    validated: List[dict] = []

    for raw in entries:
        entry = _as_dict(raw, "Assistant-supplied value")
        name = _validated_param_name(
            entry.get("param"), "", validated, "An assistant-supplied value",
        )

        if name not in placeholders:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"The SQL query does not use ':{name}' anywhere, so a value for "
                    f"'{name}' would go nowhere. Add it to the statement, for "
                    f"example: WHERE department_id = :{name}"
                ),
            )

        validated.append({
            "param": name,
            "type": _validated_param_type(entry.get("type")),
            "required": _is_truthy(entry.get("required", True)),
            "description": _validated_param_description(entry.get("description")),
        })

    return validated


def _validated_param_type(raw: Any) -> str:
    """
    What the parameter holds, so its value can be typed before it is bound.

    Defaults to text, which binds the string as it arrived — correct for a text
    column and correct for any comparison the database will coerce itself. The other
    two exist for the drivers that will not: asyncpg refuses ``id = $1`` with a string
    against an integer column, and that is a failure mid-conversation rather than at
    save time.
    """
    kind = str(raw or "").strip().lower() or "text"

    if kind not in SQL_PARAM_TYPE_VALUES:
        raise HTTPException(
            status_code=400,
            detail=(
                "Choose what an assistant-supplied value holds: text, a number, or "
                "true/false."
            ),
        )

    return kind


def _placeholders_in_sql(sql_query: str) -> set:
    """
    The ``:name`` placeholders a statement uses.

    Now one line over ``sql_guard.bind_placeholders``. It used to carry its own copy of
    the pattern, because the rule is also needed in ``tool_chain_service`` and that
    module imports this one — so neither could import the other. Moving it to
    ``sql_guard``, which both already import, removed that constraint; a placeholder is
    a property of a statement's text, which is what that module is about.
    """
    return bind_placeholders(sql_query)


def _validated_param_description(raw: Any) -> str:
    """
    What the model is told this parameter means, trimmed to a sane length.

    Optional: an empty one produces a schema field with no description, which is
    worse than a good sentence and better than a wrong one.
    """
    text = " ".join(str(raw or "").split())

    if len(text) > _MAX_PARAM_DESCRIPTION_LEN:
        raise HTTPException(
            status_code=400,
            detail=(
                "A filter parameter description cannot be longer than "
                f"{_MAX_PARAM_DESCRIPTION_LEN} characters"
            ),
        )

    return text


def _as_list(raw: Any, field_label: str, max_items: int) -> list:
    """A missing or null section means "none of these", not an error."""
    if raw is None:
        return []

    if not isinstance(raw, list):
        raise HTTPException(
            status_code=400,
            detail=f"{field_label} is not in the expected format",
        )

    if len(raw) > max_items:
        raise HTTPException(
            status_code=400,
            detail=f"{field_label} cannot have more than {max_items} entries",
        )

    return raw


def _as_dict(entry: Any, field_label: str) -> dict:
    if not isinstance(entry, dict):
        raise HTTPException(
            status_code=400,
            detail=f"{field_label} is not in the expected format",
        )
    return entry


def _optional_alias(value: Any) -> str:
    """Aliases are optional; an empty one is stored as "" so the shape is uniform."""
    alias = str(value or "").strip()

    if not alias:
        return ""

    if len(alias) > 255 or not _ALIAS_PATTERN.match(alias):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Alias '{alias}' must start with a letter and contain only "
                "letters, numbers and underscores"
            ),
        )

    return alias


async def _fail_on_duplicate_name(
    db: AsyncSession,
    tool_name: str,
    agent: DataAgent,
    exc: IntegrityError,
) -> NoReturn:
    """
    Backstop for the race between the name check above and the write, where
    uq_tool_config_agent_name_lower is what catches it.

    The rollback matters: the failed flush leaves the session unusable, and the
    HTMX route goes on to re-render the tool table in that same session, so without
    it the user would get a 500 instead of the message below.
    """
    await db.rollback()
    raise HTTPException(
        status_code=400,
        detail=f"Agent '{agent.name}' already has a tool named '{tool_name}'",
    ) from exc
