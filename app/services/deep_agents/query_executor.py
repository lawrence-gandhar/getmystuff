"""
Executing a tool config's stored query against the user's own database.

This module is the only thing in the Deep Agents feature that touches a user's
data, and it is the reason the language model never does. The model calls a tool
by name; the tool arrives here with the *stored* query definition; the rows that
go back to the model are the result of that definition and nothing else. There is
no path from model output into a query.

A tool config is written one of two ways, and this module runs both:

**Builder mode — the query is rebuilt through reflection rather than rendered as
text.** ``tool_config_service.build_query_preview`` already turns a config into
SQL, but it is a string builder for display: it inlines filter values with
f-strings, and its own docstring says it is never executed. Executing it would
make every stored filter value a SQL injection vector. Instead each table is
reflected (``Table(autoload_with=...)``, the same approach as
``db_utils._reflect_one``) and the query is assembled from real ``Column``
objects. Three properties follow:

* identifiers are quoted by the dialect, so a table or column name can never be
  read as syntax;
* filter values become **bound parameters**, so a value like ``x' OR 1=1 --``
  reaches the database as a literal string that matches nothing;
* a column that does not exist fails here, as a readable message, instead of
  reaching the driver.

**SQL mode — the stored statement is run as written.** There is no config to
rebuild: the operator approved a specific query, and running an approximation of
it would defeat the point of the mode existing. The guarantees are different, and
worth stating plainly:

* nothing the model produces is in the statement. The SQL was written and saved by
  the operator, in advance, and the tool takes no arguments — the same property
  that makes builder mode safe, arrived at differently;
* it is re-checked by :func:`tool_config_service.validated_tool_sql` on every run,
  not just when it was saved, so a row edited straight in psql is held to the same
  "one read-only statement" rule as one saved through the form;
* the row cap is applied by **streaming** the result and stopping (see
  :func:`_execute_sql_query`) rather than by wrapping the statement in a subquery
  with a ``LIMIT``. Rewriting the operator's SQL would change what runs — and a
  derived table breaks on duplicate output column names in MySQL, which is exactly
  the sort of query (``SELECT a.id, b.id …``) this mode exists to allow.

Only relational datasources are supported (see :data:`query_joins.RDBMS_DB_TYPES`).
A tool config pointed at Mongo or a file is refused with a message the agent can
relay, rather than being silently skipped.
"""

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from litestar.exceptions import HTTPException
from sqlalchemy import MetaData, Table, func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.sql import Select
from sqlalchemy.sql.elements import ColumnElement

from app.db.db_utils import get_engine
from app.services.datasource.metadata_service import rdbms_url
from app.services.tool_configs.tool_config_service import (
    validated_query_config,
    validated_tool_sql,
)
from app.utils.query_joins import RDBMS_DB_TYPES, query_tables

logger = logging.getLogger(__name__)


class ToolQueryError(Exception):
    """
    A tool's query could not be run, with a message meant for the model.

    Not an HTTPException: this is raised inside a tool call, and the agent needs
    to be *told* the tool failed so it can say so, rather than having the whole
    chatbot turn collapse into a 500. The tool wrapper in
    app.services.deep_agents.tool_factory converts it to tool output.
    """


# A tool exists to answer a question, not to export a table. The cap applies to
# every tool query without exception, so no config — however it was authored —
# can pull an unbounded result set into a prompt.
MAX_TOOL_ROWS = 200

# SQL aggregate for each stored aggregation type. The keys are exactly
# tool_configs.AGGREGATION_FUNCTION_VALUES, which is what validated_query_config
# has already checked the config against; anything else never reaches here.
_AGGREGATE_FUNCTIONS = {
    "count": func.count,
    "sum": func.sum,
    "avg": func.avg,
    "min": func.min,
    "max": func.max,
}

# How each stored filter operator becomes a SQLAlchemy expression. Every one of
# these produces a bound parameter for the value — that is the whole reason the
# mapping is written out rather than assembled from the operator string.
_FILTER_BUILDERS = {
    "=": lambda column, value: column == value,
    "!=": lambda column, value: column != value,
    ">": lambda column, value: column > value,
    "<": lambda column, value: column < value,
    "LIKE": lambda column, value: column.like(value),
}


async def execute_tool_query(
    datasource,
    config: dict,
    table_name: str,
    row_limit: int = MAX_TOOL_ROWS,
    sql_query: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Run one tool config's query and return its rows as dictionaries.

    ``sql_query`` picks the mode: present means run that statement, absent means
    build the query from ``config``. The caller passes the row as it was stored
    rather than a mode flag, so a tool cannot end up in a mode whose query is
    missing.

    Whichever mode it is, the stored query is **re-validated here** even though it
    was validated when it was saved. That is deliberate: this function's guarantees
    have to hold for a row however it got into the database, including one edited
    directly in psql, so it re-derives what it will execute instead of trusting the
    row.
    """
    db_type = (datasource.db_type or "").strip().lower()

    if db_type not in RDBMS_DB_TYPES:
        raise ToolQueryError(
            f"This tool reads a {datasource.db_type or 'unknown'} datasource, and "
            "only relational databases (PostgreSQL, MySQL, SQLite) can be queried "
            "this way. Tell the user this tool is not available."
        )

    url = rdbms_url(datasource)
    limit = max(1, min(int(row_limit or MAX_TOOL_ROWS), MAX_TOOL_ROWS))

    try:
        engine = await get_engine(url)

        async with engine.connect() as connection:
            if (sql_query or "").strip():
                rows = await _execute_sql_query(connection, sql_query, limit)
            else:
                rows = await _execute_built_query(
                    connection, config, table_name, db_type, limit,
                )

    except ToolQueryError:
        raise
    except HTTPException as exc:
        # A stored query that no longer passes its own validator — a config hand-
        # edited into an invalid shape, or a sql_query that is no longer a single
        # read. The validators speak to an operator filling in a form, so the
        # detail is logged and the model is told something it can act on instead.
        logger.warning(
            "Tool query for table %s failed validation: %s", table_name, exc.detail,
        )
        raise ToolQueryError(
            "This tool's saved query is no longer valid, so it cannot run. Tell the "
            "user the tool needs reconfiguring."
        ) from exc
    except SQLAlchemyError as exc:
        # The driver message can name schema objects and even echo values, so it is
        # logged rather than handed to a model that is talking to a visitor.
        #
        # Deliberately does not claim the problem is temporary. A driver error here is
        # as likely to be permanent (a config with plain columns and aggregations but
        # no GROUP BY, which the operator has to fix) as transient, and telling a
        # visitor to retry something that will never work is worse than saying the
        # figure is unavailable.
        logger.exception("Tool query failed for table %s", table_name)
        raise ToolQueryError(
            "The query could not be run against the database. Tell the user you "
            "cannot retrieve that figure right now, and do not attempt to answer it "
            "from anything else."
        ) from exc

    return rows


async def _execute_built_query(
    connection,
    config: dict,
    table_name: str,
    db_type: str,
    limit: int,
) -> List[Dict[str, Any]]:
    """
    Builder mode: reflect the tables, assemble a ``Select``, run it.

    ``validated_query_config`` takes the JSON text the form submits, so the stored
    dict is re-serialised rather than a second entry point being added to it — one
    validator, one set of rules, no drift.
    """
    validated = validated_query_config(json.dumps(config or {}), table_name, db_type)

    tables = await _reflect_tables(
        connection, table_name, validated.get("joins") or [],
    )
    statement = _build_select(validated, table_name, tables, limit)

    result = await connection.execute(statement)

    return [dict(row) for row in result.mappings().all()]


async def _execute_sql_query(
    connection,
    sql_query: str,
    limit: int,
) -> List[Dict[str, Any]]:
    """
    SQL mode: run the operator's statement, as written, and stop at ``limit`` rows.

    Two decisions worth spelling out.

    **The statement is re-validated first.** ``validated_tool_sql`` is the same
    check the form applied, so a row that has since been edited outside the
    application — or written by an older version of it — cannot smuggle a second
    statement or a write past this point.

    **The cap is applied by streaming, not by rewriting the SQL.** ``stream`` opens
    a server-side cursor where the driver supports one, and ``fetchmany`` stops
    after the cap, so a query matching a million rows never materialises more than
    ``limit`` of them here. Wrapping the statement as
    ``SELECT * FROM (…) LIMIT n`` would have been simpler and is wrong in two ways:
    it changes the SQL the operator approved, and MySQL rejects a derived table
    with duplicate output column names — so ``SELECT a.id, b.id FROM a JOIN b``,
    a query this mode exists to make possible, would fail for a reason having
    nothing to do with the query.

    The statement still *runs* in full on the database — an unfiltered aggregate
    scans what it scans. The cap bounds what crosses the wire and what reaches the
    prompt, which is what it is there for.
    """
    statement = text(validated_tool_sql(sql_query))

    result = await connection.stream(statement)

    try:
        rows = await result.mappings().fetchmany(limit)
    finally:
        # Releases the server-side cursor without waiting for the connection to be
        # returned to the pool. The whole point of streaming is that the rest of
        # the result set is never read.
        await result.close()

    return [dict(row) for row in rows]


# --------------------------------------------------------------------------
# Reflection
# --------------------------------------------------------------------------

async def _reflect_tables(
    connection,
    base_table: str,
    joins: List[dict],
) -> Dict[str, Table]:
    """
    Reflect the base table and every joined table, keyed by name.

    Reflection is what converts a validated *name* into a real ``Column``, and so
    is what makes the rest of this module unable to emit SQL text. It runs inside
    ``run_sync`` because the Inspector API is synchronous — the same pattern as
    ``db_utils._reflect_one``.
    """
    names = [base_table] + [str(entry.get("table") or "") for entry in joins]

    metadata = MetaData()
    tables: Dict[str, Table] = {}

    for name in names:
        if not name or name in tables:
            continue

        try:
            table = await connection.run_sync(
                lambda sync_connection, table_name=name: Table(
                    table_name, metadata, autoload_with=sync_connection,
                )
            )
        except SQLAlchemyError as exc:
            raise ToolQueryError(
                f"Table '{name}' no longer exists in the datasource, so this tool "
                "cannot run. Tell the user the tool needs reconfiguring."
            ) from exc

        tables[name] = table

    return tables


# --------------------------------------------------------------------------
# Query assembly
# --------------------------------------------------------------------------

def _build_select(
    config: dict,
    base_table: str,
    tables: Dict[str, Table],
    limit: int,
) -> Select:
    """
    Assemble the validated config into a ``Select``.

    Mirrors ``tool_config_service.build_query_preview`` clause for clause, so what
    the operator was shown in the Tool Configs list is what runs here.
    """
    known_tables = query_tables(config.get("joins"), base_table)

    selected = _selected_columns(config, base_table, tables, known_tables)
    statement = select(*selected).select_from(tables[base_table])

    statement = _apply_joins(statement, config.get("joins") or [], tables)

    for condition in _filter_conditions(config, base_table, tables, known_tables):
        statement = statement.where(condition)

    for reference in config.get("group_by") or []:
        statement = statement.group_by(
            _resolve_column(reference, base_table, tables, known_tables),
        )

    return statement.limit(limit)


def _selected_columns(
    config: dict,
    base_table: str,
    tables: Dict[str, Table],
    known_tables: List[str],
) -> List[ColumnElement]:
    """
    The SELECT list: plain columns first, then aggregations — the same order
    ``_preview_selection`` renders.

    An empty list means the config selects nothing specific, which the preview
    shows as ``*``. Here that becomes the base table's columns explicitly rather
    than a literal ``*``: the tool's output is going into a prompt, so the column
    set should be the one that was reflected, not whatever the table happens to
    have grown since.
    """
    selected: List[ColumnElement] = []

    for entry in config.get("columns") or []:
        reference = entry.get("column")
        if not reference:
            continue
        column = _resolve_column(reference, base_table, tables, known_tables)
        alias = entry.get("alias")
        selected.append(column.label(alias) if alias else column)

    for entry in config.get("aggregations") or []:
        reference = entry.get("column")
        aggregate = _AGGREGATE_FUNCTIONS.get((entry.get("type") or "").lower())
        if not reference or aggregate is None:
            continue
        column = _resolve_column(reference, base_table, tables, known_tables)
        expression = aggregate(column)
        alias = entry.get("alias")
        selected.append(
            expression.label(alias) if alias else expression.label(
                f"{(entry.get('type') or '').lower()}_{column.name}",
            )
        )

    if selected:
        return selected

    return list(tables[base_table].columns)


def _apply_joins(
    statement: Select,
    joins: List[dict],
    tables: Dict[str, Table],
) -> Select:
    """
    Apply the joins in stored order.

    ``validated_joins`` guarantees the list is a connected chain — each entry's
    ``left_table`` is the base table or a table joined before it — so applying
    them in order always produces a valid FROM clause.

    **RIGHT JOIN is refused, not approximated.** SQLAlchemy expresses joins as
    ``isouter``/``full`` flags and has no right variant; a right join is only
    expressible by swapping the operands, which this accumulating builder cannot do
    once the base table is fixed. Substituting a left or full outer join would change
    which rows come back — quietly, and in the direction of a plausible wrong figure.
    Given that this feature exists so a model never reports a number it did not read,
    an explicit failure the operator can fix is the only honest option.
    ``tool_factory.find_unsupported_tools`` flags these on the console up front so it
    is not discovered by a visitor.
    """
    for entry in joins:
        join_type = (entry.get("type") or "").lower()

        if join_type == "right":
            raise ToolQueryError(
                "This tool uses a RIGHT JOIN, which cannot be run. Tell the user the "
                "tool needs reconfiguring with an inner or left join."
            )

        right_table = tables[str(entry.get("table"))]
        left_table = tables[str(entry.get("left_table"))]

        condition = (
            _table_column(left_table, str(entry.get("left_column")))
            == _table_column(right_table, str(entry.get("right_column")))
        )

        statement = statement.join(
            right_table,
            condition,
            isouter=join_type in ("left", "full"),
            full=join_type == "full",
        )

    return statement


def _filter_conditions(
    config: dict,
    base_table: str,
    tables: Dict[str, Table],
    known_tables: List[str],
) -> List[ColumnElement]:
    """
    The WHERE conditions, AND-ed — the stored config has no OR.

    Every value goes in as a bound parameter. This is the single most important
    line in the module: it is what makes a stored filter value data rather than
    SQL, no matter who wrote it or how.
    """
    conditions: List[ColumnElement] = []

    for entry in config.get("filters") or []:
        reference = entry.get("column")
        builder = _FILTER_BUILDERS.get(entry.get("operator"))
        if not reference or builder is None:
            continue

        column = _resolve_column(reference, base_table, tables, known_tables)
        conditions.append(builder(column, _coerced_value(column, entry.get("value"))))

    return conditions


def _coerced_value(column: ColumnElement, value: Any) -> Any:
    """
    Coerce a stored filter value to the column's Python type.

    Filter values are stored as strings (the form is a text input), so comparing
    a numeric column against ``"0"`` would fail on a strict driver like asyncpg
    even though the comparison is perfectly valid. Coercion is attempted from the
    *reflected* type and falls back to the string on failure — an unconvertible
    value is a value that matches nothing, which is the right answer, not an error.
    """
    if value is None:
        return None

    try:
        python_type = column.type.python_type
    except (NotImplementedError, AttributeError):
        return value

    if python_type is bool:
        return str(value).strip().lower() in ("1", "true", "t", "yes", "y")

    if python_type in (int, float):
        try:
            return python_type(str(value).strip())
        except (TypeError, ValueError):
            return value

    return value


# --------------------------------------------------------------------------
# Column resolution
# --------------------------------------------------------------------------

def _resolve_column(
    reference: Any,
    base_table: str,
    tables: Dict[str, Table],
    known_tables: List[str],
) -> ColumnElement:
    """
    Turn a stored reference — ``"column"`` or ``"table.column"`` — into a real
    reflected ``Column``.

    An unqualified reference means the base table, matching
    ``query_joins.validated_column_reference``: configs saved before a join was
    added keep their bare names, and they still mean what they always did.
    """
    table_name, column_name = _split_reference(reference, base_table, known_tables)

    table = tables.get(table_name)
    if table is None:
        raise ToolQueryError(
            f"This tool refers to table '{table_name}', which is not part of its "
            "query. Tell the user the tool needs reconfiguring."
        )

    return _table_column(table, column_name)


def _split_reference(
    reference: Any,
    base_table: str,
    known_tables: List[str],
) -> Tuple[str, str]:
    """
    Split a reference into (table, column).

    ``known_tables`` is empty for an unjoined query, which is the signal
    ``query_tables`` exists to give: with one table in play a dot can only be part
    of the column's own name, so the reference is not split at all.
    """
    name = str(reference or "").strip()

    if not known_tables:
        return base_table, name

    table_name, separator, column_name = name.partition(".")
    if not separator:
        return base_table, name

    return table_name, column_name


def _table_column(table: Table, column_name: str) -> ColumnElement:
    """
    One reflected column by name, or a readable failure.

    Reaching this with a missing column means the datasource changed under a
    saved config — a dropped or renamed column. Saying which column is missing is
    what turns that into something the operator can fix.
    """
    column = table.columns.get(column_name)

    if column is None:
        raise ToolQueryError(
            f"Column '{column_name}' no longer exists on table '{table.name}', so "
            "this tool cannot run. Tell the user the tool needs reconfiguring."
        )

    return column


def describe_result(rows: List[Dict[str, Any]]) -> str:
    """
    Render tool output for the model as JSON, with an explicit row count.

    The count is stated separately because a truncated result and a complete one
    look identical to a model otherwise, and "200 rows" invites it to report a
    total that is really a limit. When the cap is hit, the text says so.
    """
    if not rows:
        return "0 rows. The query returned no data."

    truncated = (
        f" (capped at {MAX_TOOL_ROWS} — this is not the total number of matching "
        "rows, so do not report it as one)"
        if len(rows) >= MAX_TOOL_ROWS
        else ""
    )

    return (
        f"{len(rows)} row(s){truncated}:\n"
        f"{json.dumps(rows, default=str, ensure_ascii=False)}"
    )
