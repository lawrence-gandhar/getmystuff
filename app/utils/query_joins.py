"""
Joins across multiple tables — the rules shared by the two places a query is
authored (the Tool Configs library and the Configurations page's Tool Base Config
panel).

A join is only ever offered where the datasource can actually execute one, which
in practice means a relational database: a CSV file or a Mongo collection has no
second table to join to. :data:`RDBMS_DB_TYPES` is what that decision is made
against, and :func:`validated_joins` refuses a payload carrying joins for anything
else rather than storing something that could never run.

The stored shape, appended to the query config both forms produce::

    "joins": [
      {
        "type":         "inner",      # see JOIN_TYPES
        "table":        "orders",     # the table being joined in
        "left_table":   "customers",  # base table, or a table joined before this one
        "left_column":  "id",
        "right_column": "customer_id"
      }
    ]

Order matters and is preserved: each entry may only join onto a table already in
the query, so the list always reads as a connected chain that can be turned into
SQL in exactly the order it is stored.

Once a query has at least one join, both forms qualify every column reference as
``table.column`` — with two tables in play, a bare ``id`` is ambiguous. That is why
:func:`validated_column_reference` takes the tables the query knows about: a
reference qualified with anything else is a reference to a table the user never
joined.
"""

from typing import Any, Iterable, List, Optional

from litestar.exceptions import HTTPException

from app.utils.validators import require_object_name

# Datasource types that can execute a join. Everything else (the file-based types
# in app.utils.file_utils and "mongodb") is single-object by nature.
RDBMS_DB_TYPES = frozenset({"postgres", "mysql", "sqlite"})

# (value, SQL keyword) — stored lowercase, rendered as the keyword. The keyword is
# also the dropdown label, so the form reads as the SQL it produces.
JOIN_TYPES = (
    ("inner", "INNER JOIN"),
    ("left", "LEFT JOIN"),
    ("right", "RIGHT JOIN"),
    ("full", "FULL OUTER JOIN"),
)
JOIN_TYPE_SQL = dict(JOIN_TYPES)
JOIN_TYPE_VALUES = frozenset(JOIN_TYPE_SQL)

# Not every dialect has every join. MySQL has no FULL OUTER JOIN at all, so
# offering one would only produce a query that fails when it is finally run.
# Anything not listed here gets the full set.
_JOIN_TYPES_BY_DB_TYPE = {
    "mysql": ("inner", "left", "right"),
}

# Bounded like the rest of the query payload: the JSON arrives from a form field,
# and "however many the client sent" is not an acceptable number of tables to join.
MAX_JOINS = 10


def supports_joins(db_type: Optional[str]) -> bool:
    """Whether a datasource of this type can join to a second table."""
    return (db_type or "").strip().lower() in RDBMS_DB_TYPES


def join_types_for(db_type: Optional[str]) -> tuple:
    """
    The (value, label) join types this datasource can run — what the form's
    dropdown offers, and the same list :func:`validated_joins` enforces.

    Empty for a datasource that cannot join at all, so a template can use it
    directly to decide whether to render the Joins section.
    """
    if not supports_joins(db_type):
        return ()

    allowed = _JOIN_TYPES_BY_DB_TYPE.get((db_type or "").strip().lower())
    if allowed is None:
        return JOIN_TYPES

    return tuple(pair for pair in JOIN_TYPES if pair[0] in allowed)


def validated_joins(
    raw: Any,
    base_table: str,
    db_type: Optional[str],
) -> List[dict]:
    """
    Validate the join list and return it normalised, or ``[]`` when there is none.

    Everything is rebuilt field by field rather than stored as received, so only
    known keys are persisted and every table and column name in the result has been
    checked (see :func:`app.utils.validators.require_object_name` — these names end
    up as identifiers in a generated query, not as bound parameters).
    """
    entries = _as_list(raw)

    if not entries:
        return []

    allowed_types = join_types_for(db_type)
    if not allowed_types:
        raise HTTPException(
            status_code=400,
            detail=(
                "Joins are only available for relational datasources "
                "(PostgreSQL, MySQL or SQLite). Remove the joins to save this tool."
            ),
        )

    if len(entries) > MAX_JOINS:
        raise HTTPException(
            status_code=400,
            detail=f"A query cannot join more than {MAX_JOINS} tables",
        )

    allowed_type_values = {value for value, _ in allowed_types}
    readable_types = ", ".join(label for _, label in allowed_types)

    # The tables an ON condition may refer to: the base table, plus each table as
    # it is joined in. Grown inside the loop, which is what makes the chain
    # connected — join three refers to tables one and two, never the other way.
    known_tables = [base_table]
    joins: List[dict] = []

    for entry in entries:
        if not isinstance(entry, dict):
            raise HTTPException(
                status_code=400, detail="Joins are not in the expected format",
            )

        join_type = str(entry.get("type") or "").strip().lower()
        if join_type not in allowed_type_values:
            raise HTTPException(
                status_code=400,
                detail=f"Every join needs a valid type ({readable_types})",
            )

        table = require_object_name(entry.get("table"), "Join table")
        if table in known_tables:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Table '{table}' is already part of this query — "
                    "join each table only once"
                ),
            )

        left_table = require_object_name(entry.get("left_table"), "Join left table")
        if left_table not in known_tables:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"The join on '{table}' matches against table '{left_table}', "
                    "which is not part of this query. Pick a table that is already "
                    "joined, and keep the joins in order."
                ),
            )

        joins.append({
            "type": join_type,
            "table": table,
            "left_table": left_table,
            "left_column": require_object_name(
                entry.get("left_column"), "Join left column",
            ),
            "right_column": require_object_name(
                entry.get("right_column"), "Join right column",
            ),
        })
        known_tables.append(table)

    return joins


def query_tables(joins: Optional[Iterable[dict]], base_table: str) -> List[str]:
    """
    Every table the query reads — the base table plus each joined one.

    Returns ``[]`` when there are no joins, which both callers read as "this query
    has one table, so column references are bare". Keeping that signal in one place
    stops the form and the validator disagreeing about when a reference needs
    qualifying.
    """
    entries = list(joins or [])
    if not entries:
        return []

    return [base_table] + [str(entry.get("table") or "") for entry in entries]


def validated_column_reference(
    value: Any,
    field_label: str,
    allowed_tables: Optional[Iterable[str]] = None,
) -> str:
    """
    Validate one column reference, which may be qualified as ``table.column``.

    With no joins ``allowed_tables`` is empty and this is exactly the plain
    column-name check it has always been — an unjoined query's columns all belong
    to its one table.

    With joins in play, a qualified reference has to name a table that is actually
    in the query. An unqualified one is still accepted and means the base table:
    the form qualifies everything the moment a join is added, so a bare name only
    reaches here from a config saved before the join was, and rejecting it would
    make that config uneditable.
    """
    tables = list(allowed_tables or [])
    name = require_object_name(value, field_label)

    if not tables:
        return name

    table, separator, column = name.partition(".")
    if not separator:
        return name

    if table not in tables:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{field_label} '{name}' refers to table '{table}', which is not "
                "part of this query"
            ),
        )

    if not column or "." in column:
        raise HTTPException(
            status_code=400, detail=f"{field_label} '{name}' is not a valid name",
        )

    return name


def build_join_sql(joins: Optional[Iterable[dict]]) -> List[str]:
    """
    Render the joins as SQL clauses for the query preview.

    Display only — the preview is never executed. Building it here rather than in a
    template or a template's JavaScript keeps the two forms, the Tool Configs list
    and any future runtime from describing the same config differently.
    """
    clauses = []

    for entry in joins or []:
        keyword = JOIN_TYPE_SQL.get(str(entry.get("type") or "").lower())
        table = entry.get("table")
        left_table = entry.get("left_table")
        left_column = entry.get("left_column")
        right_column = entry.get("right_column")

        if not (keyword and table and left_table and left_column and right_column):
            continue

        clauses.append(
            f"{keyword} {table} "
            f"ON {left_table}.{left_column} = {table}.{right_column}"
        )

    return clauses


def _as_list(raw: Any) -> list:
    """A missing or null section means "no joins", not an error."""
    if raw is None:
        return []

    if not isinstance(raw, list):
        raise HTTPException(
            status_code=400, detail="Joins are not in the expected format",
        )

    return raw
