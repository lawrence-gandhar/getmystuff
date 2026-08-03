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
(:func:`get_table_choices`, :func:`get_column_choices`, :func:`get_column_map`), but
deliberately *not* re-fetched when saving: a datasource that is momentarily
unreachable must not stop a tool config being edited. What is enforced on save is
the shape of the query and the safety of every name in it (see
:func:`_validated_config`).

A query over a relational datasource may join further tables in; the rules for that
live in app.utils.query_joins, shared with the Configurations page so a join means
the same thing wherever it was authored.
"""

import re
import uuid
from typing import Any, Dict, List, NoReturn, Optional, Set

from litestar.exceptions import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.db_utils import CRUDQueryBuilder
from app.db.tool_configs.queries import (
    fetch_tool_configs_with_details,
    tool_name_exists,
)
from app.models.data_agents import DataAgent
from app.models.datasource import DataSource
from app.models.tool_configs import (
    AGGREGATION_FUNCTION_VALUES,
    FILTER_OPERATOR_VALUES,
    QUERY_MODE_BUILDER,
    QUERY_MODE_SQL,
    QUERY_MODE_VALUES,
    ToolConfig,
)
from app.services.data_agents import data_agent_service
from app.services.datasource import datasource_service
from app.utils.query_joins import (
    RDBMS_DB_TYPES,
    build_join_sql,
    join_types_for,
    query_tables,
    supports_joins,
    validated_column_reference,
    validated_joins,
)
from app.utils.sql_guard import normalised_sql, read_only_violation
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

# An alias is emitted as `… AS alias`, so it has to be a plain identifier. Table and
# column names have their own rule, shared with the Configurations page — see
# app.utils.validators.require_object_name.
_ALIAS_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


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
    """
    internal_agent_id = None
    if agent_id is not None:
        agent = await data_agent_service.get_data_agent(db, user_id, agent_id)
        internal_agent_id = agent.id

    rows = await fetch_tool_configs_with_details(db, user_id, internal_agent_id)

    return [
        {
            "uuid": str(tool_config.uuid),
            "tool_name": tool_config.tool_name,
            "description": tool_config.description,
            "table_name": tool_config.table_name,
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
        "query_mode": tool_config.query_mode or QUERY_MODE_BUILDER,
        "sql_query": tool_config.sql_query or "",
        "is_enabled": tool_config.is_enabled,
        "agent_id": str(agent.uuid) if agent else "",
        "datasource_id": str(datasource.uuid) if datasource else "",
        "config": tool_config.config or {},
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
    The tables, collections or files inside one datasource, read live for the form's
    second dropdown.

    ``get_datasource_objects`` already ownership-checks the datasource and raises a
    readable 400 when the connection fails, so a broken datasource shows a message
    in the form instead of an empty dropdown with no explanation.
    """
    details = await datasource_service.get_datasource_objects(
        db=db, datasource_id=datasource_id, user_id=user_id,
    )

    names = []
    for entry in details.get("objects") or []:
        # RDBMS/Mongo return plain names; file datasources return {"name", "file_id"}.
        names.append(entry["name"] if isinstance(entry, dict) else entry)

    return sorted(name for name in names if name)


async def get_column_choices(
    db: AsyncSession,
    user_id: int,
    datasource_id: uuid.UUID,
    table_name: str,
) -> List[str]:
    """
    The columns of one table, read live so the query builder offers real fields
    rather than free text. Ownership and connection errors are handled by
    ``get_datasource_table_schema``.
    """
    table_name = require_object_name(table_name, "Table")

    details = await datasource_service.get_datasource_table_schema(
        db=db, datasource_id=datasource_id, user_id=user_id, table_name=table_name,
    )

    names = []
    for column in details.get("schema") or []:
        if isinstance(column, dict):
            name = column.get("name") or column.get("column") or column.get("column_name")
        else:
            name = column
        if name:
            names.append(str(name))

    return names


async def get_column_map(
    db: AsyncSession,
    user_id: int,
    datasource_id: uuid.UUID,
    table_names: List[str],
) -> Dict[str, List[str]]:
    """
    The columns of several tables at once, keyed by table name.

    A joined query needs every one of its tables' columns in the builder's
    dropdowns, so the edit form loads the base table plus each joined one in a
    single call. A table that cannot be read raises rather than being dropped from
    the map: the caller turns that into the same visible "schema could not be read"
    warning a single unreadable table already produces, instead of leaving the user
    with a dropdown that is quietly missing half its options.
    """
    column_map: Dict[str, List[str]] = {}

    for table_name in table_names:
        if not table_name or table_name in column_map:
            continue
        column_map[table_name] = await get_column_choices(
            db, user_id, datasource_id, table_name,
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
    """The SELECT list: plain columns first, then aggregations. ``*`` when empty."""
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
    """The WHERE clause, or "" when the config has no filters."""
    return " AND ".join(
        f"{entry.get('column')} {entry.get('operator')} '{entry.get('value')}'"
        for entry in config.get("filters") or []
        if entry.get("column")
    )


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
    table_name: str,
    description: Optional[str] = None,
    config_json: Optional[str] = None,
    query_mode: Optional[str] = None,
    sql_query: Optional[str] = None,
) -> ToolConfig:
    """Create a tool config and give it to one agent. Enabled on creation."""
    agent = await _resolve_agent(db, user_id, agent_id)
    datasource = await _resolve_datasource(db, user_id, datasource_id)

    fields = await _validated_fields(
        db,
        agent,
        datasource,
        tool_name,
        table_name,
        description,
        config_json,
        query_mode,
        sql_query,
    )

    try:
        return await tool_config_crud.create(db, {
            "data_agent_id": agent.id,
            "datasource_id": datasource.id,
            "is_enabled": True,
            **fields,
        })
    except IntegrityError as exc:
        await _fail_on_duplicate_name(db, fields["tool_name"], agent, exc)


async def update_tool_config(
    db: AsyncSession,
    user_id: int,
    tool_config_id: uuid.UUID,
    agent_id: Optional[uuid.UUID],
    datasource_id: Optional[uuid.UUID],
    tool_name: str,
    table_name: str,
    description: Optional[str] = None,
    config_json: Optional[str] = None,
    query_mode: Optional[str] = None,
    sql_query: Optional[str] = None,
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
        table_name,
        description,
        config_json,
        query_mode,
        sql_query,
        # Exclude itself from the duplicate check only while it stays on the same
        # agent; moved to another agent it has to clear that agent's names.
        exclude_id=tool_config.id if agent.id == tool_config.data_agent_id else None,
    )

    try:
        await tool_config_crud.update(db, tool_config.id, {
            "data_agent_id": agent.id,
            "datasource_id": datasource.id,
            **fields,
        })
    except IntegrityError as exc:
        await _fail_on_duplicate_name(db, fields["tool_name"], agent, exc)

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
    """
    tool_config = await get_tool_config(db, user_id, tool_config_id)
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
    """
    tool_config = await get_tool_config(db, user_id, tool_config_id)  # ownership check
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
    table_name: str,
    description: Optional[str],
    config_json: Optional[str],
    query_mode: Optional[str] = None,
    sql_query: Optional[str] = None,
    exclude_id: Optional[int] = None,
) -> dict:
    """
    Validate the writable fields once for both create and update, returning them as
    the column dict to persist.

    The datasource is needed for more than ownership: its type decides whether the
    query may join at all, whether raw SQL can be run against it, and its table is
    the one every unqualified column reference belongs to.

    All three query columns are always in the returned dict, so whichever mode is
    saved clears the other mode's leftovers. Without that, switching a tool to SQL
    and back would leave a stale statement in ``sql_query`` for the executor to
    prefer over the config the operator is now looking at.
    """
    tool_name = require_identifier(tool_name, "Tool name")

    if await tool_name_exists(db, agent.id, tool_name, exclude_id=exclude_id):
        raise HTTPException(
            status_code=400,
            detail=f"Agent '{agent.name}' already has a tool named '{tool_name}'",
        )

    base_table = require_object_name(table_name, "Table")
    mode = _validated_query_mode(query_mode, datasource)

    fields = {
        "tool_name": tool_name,
        "table_name": base_table,
        "description": optional_text(description, "Description", _DESCRIPTION_MAX),
        "query_mode": mode,
    }

    if mode == QUERY_MODE_SQL:
        return {**fields, "config": {}, "sql_query": validated_tool_sql(sql_query)}

    return {
        **fields,
        "config": validated_query_config(config_json, base_table, datasource.db_type),
        "sql_query": None,
    }


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


def validated_tool_sql(sql_query: Optional[str]) -> str:
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
    """
    statement = normalised_sql(sql_query)

    if not statement:
        raise HTTPException(
            status_code=400,
            detail="Write the SQL query this tool should run",
        )

    violation = read_only_violation(statement)
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
    """
    raw = parse_json_object(config_json, "Query configuration")

    joins = validated_joins(raw.get("joins"), base_table, db_type)
    tables = query_tables(joins, base_table)

    return {
        "columns": _validated_columns(raw.get("columns"), tables),
        "aggregations": _validated_aggregations(raw.get("aggregations"), tables),
        "group_by": _validated_group_by(raw.get("group_by"), tables),
        "filters": _validated_filters(raw.get("filters"), tables),
        "joins": joins,
    }


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
                detail="Every filter needs a valid operator (=, !=, >, < or LIKE)",
            )

        value = str(entry.get("value") or "").strip()
        if not value:
            raise HTTPException(
                status_code=400,
                detail="Every filter needs a value to compare against",
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
            "column": validated_column_reference(
                entry.get("column"), "Filter column", tables,
            ),
            "operator": operator,
            "value": value,
        })

    return filters


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
