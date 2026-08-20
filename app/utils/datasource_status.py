"""
The one definition of "is this table/column switched on for this datasource".

Data Sources lets a user switch individual tables and columns off, and stores that
in ``DataSource.configuration_data``::

    {"orders": {"status": "active" | "inactive",
                "column_data": {"id": {"column_name": "id", "status": "active"}}}}

Four features now have to agree on what that means — the Data Sources listing, the
Tool Configs pickers, the Ask AI schema, and the Deep Agents query executor — and a
switch that only some of them honour is worse than no switch at all: the user turns
a column off, watches it disappear from a dropdown, and it is still in the rows an
agent quotes. So the reading is done here, once.

**Absent means active.** Only the literal ``"inactive"`` switches something off. A
table with no entry, a table whose ``column_data`` is empty, a ``configuration_data``
of ``None`` — all active. This is not leniency for its own sake: every datasource
created before metadata collection worked has an empty ``configuration_data``, and a
rule of "active only if it says active" would empty every dropdown in the
application for those users. It is also the rule the column view already relies on
(``app.routes.datasource.datasource_routes.view_table_schema`` falls back to a live
schema fetch and treats every column it finds as active).

**Nothing here raises.** ``configuration_data`` is a JSON column that a user's own
toggling writes and that can be hand-edited; a malformed blob must not take a page
down, so anything unrecognisable is read as unconfigured.

**The caller owns the list of names.** Every function filters names the caller has
already read from the live database, and never returns one it was not given — so a
column dropped from the real table but still recorded in ``configuration_data`` is
never offered to anyone.
"""

from typing import Any, Dict, Iterable, List, Optional

STATUS_ACTIVE = "active"
STATUS_INACTIVE = "inactive"

# Messages, kept here so the two kinds of caller say the same sentence. The services
# raise these as an HTTPException a person reads in a form; the Deep Agents executor
# raises them as a ToolQueryError a model reads and relays. Neither imports the
# other, and a reword lands in both at once.
NO_ACTIVE_TABLES_MESSAGE = (
    "Every table in this datasource is inactive. Activate at least one in Data "
    "Sources before using it here."
)


def no_active_columns_message(table_name: str) -> str:
    return (
        f"Every column of '{table_name}' is inactive. Activate the columns you need "
        "in Data Sources."
    )


def inactive_table_message(table_name: str) -> str:
    return (
        f"Table '{table_name}' is inactive in this datasource. Activate it in Data "
        "Sources to use it here."
    )


def inactive_column_message(reference: str) -> str:
    return (
        f"Column '{reference}' is inactive in this datasource, so this query cannot "
        "run. Activate the column in Data Sources or change the query."
    )


def _configuration(configuration_data: Any) -> Dict[str, Any]:
    """The configuration as a dict, or an empty one for anything unusable."""
    return configuration_data if isinstance(configuration_data, dict) else {}


def _table_entry(configuration_data: Any, table_name: str) -> Dict[str, Any]:
    entry = _configuration(configuration_data).get(str(table_name or ""))
    return entry if isinstance(entry, dict) else {}


def _is_inactive(status: Any) -> bool:
    """
    Whether a stored status value means "switched off".

    The test is against ``"inactive"`` and not against ``"active"`` — an unknown
    value, an empty string or a missing key all mean nobody has switched this off.
    """
    return str(status or "").strip().lower() == STATUS_INACTIVE


def table_status(configuration_data: Any, table_name: str) -> str:
    """The stored status of one table, defaulting to active."""
    entry = _table_entry(configuration_data, table_name)
    return STATUS_INACTIVE if _is_inactive(entry.get("status")) else STATUS_ACTIVE


def is_table_active(configuration_data: Any, table_name: str) -> bool:
    return table_status(configuration_data, table_name) == STATUS_ACTIVE


def column_status(configuration_data: Any, table_name: str, column_name: str) -> str:
    """
    The stored status of one column, defaulting to active.

    **An inactive table's columns are all inactive**, whatever ``column_data`` says.
    ``toggle_table_status_service`` cascades that on write, but a row edited straight
    in the database can disagree with itself, and the read side is the one that has
    to be safe: a column reported active under a table nobody switched on would put
    data into an agent's answer that the user believed was switched off.
    """
    if not is_table_active(configuration_data, table_name):
        return STATUS_INACTIVE

    column_data = _table_entry(configuration_data, table_name).get("column_data")
    if not isinstance(column_data, dict):
        return STATUS_ACTIVE

    entry = column_data.get(str(column_name or ""))
    if not isinstance(entry, dict):
        return STATUS_ACTIVE

    return STATUS_INACTIVE if _is_inactive(entry.get("status")) else STATUS_ACTIVE


def is_column_active(configuration_data: Any, table_name: str, column_name: str) -> bool:
    return column_status(configuration_data, table_name, column_name) == STATUS_ACTIVE


def active_table_names(
    configuration_data: Any,
    table_names: Iterable[str],
) -> List[str]:
    """The active subset of the live table names given, in the order given."""
    return [
        name for name in table_names or []
        if name and is_table_active(configuration_data, name)
    ]


def inactive_table_names(
    configuration_data: Any,
    table_names: Iterable[str],
) -> List[str]:
    """The inactive subset — for naming them in a refusal, rather than filtering."""
    return [
        name for name in table_names or []
        if name and not is_table_active(configuration_data, name)
    ]


def active_column_names(
    configuration_data: Any,
    table_name: str,
    column_names: Iterable[str],
) -> List[str]:
    """
    The active subset of one table's live column names, in the order given.

    Empty when the table itself is inactive — see :func:`column_status`.
    """
    return [
        name for name in column_names or []
        if name and is_column_active(configuration_data, table_name, name)
    ]


def active_columns_by_table(
    configuration_data: Any,
    columns_by_table: Dict[str, Iterable[str]],
) -> Dict[str, List[str]]:
    """
    :func:`active_column_names` for several tables at once, keyed the same way.

    Used where a query reads more than one table — the joined SELECT list and the
    "columns you must select" block in the Ask AI prompt both need the whole map,
    and building it in each of them would be two chances to forget the cascade.
    """
    return {
        table_name: active_column_names(configuration_data, table_name, columns)
        for table_name, columns in (columns_by_table or {}).items()
    }


def first_inactive_reference(
    configuration_data: Any,
    references: Iterable[str],
    base_table: str,
    known_tables: Optional[Iterable[str]] = None,
) -> Optional[str]:
    """
    The first ``"table.column"`` or ``"column"`` reference that is switched off, or
    ``None`` when they all pass.

    Mirrors ``app.utils.query_joins.validated_column_reference``: a bare name means
    the base table, and a dotted name is only split when the query actually reads
    more than one table (``known_tables`` non-empty), because with a single table a
    dot can only be part of the column's own name.
    """
    tables = list(known_tables or [])

    for reference in references or []:
        name = str(reference or "").strip()
        if not name:
            continue

        if tables and "." in name:
            table_name, _, column_name = name.partition(".")
        else:
            table_name, column_name = base_table, name

        if not is_column_active(configuration_data, table_name, column_name):
            return name

    return None
