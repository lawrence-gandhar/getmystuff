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
* a caller that asks for a specific number of rows gets it by **streaming** the
  result and stopping (see :func:`_execute_sql_query`) rather than by wrapping the
  statement in a subquery with a ``LIMIT``. Rewriting the operator's SQL would change
  what runs — and a derived table breaks on duplicate output column names in MySQL,
  which is exactly the sort of query (``SELECT a.id, b.id …``) this mode exists to
  allow.

**Nothing caps how many rows a query returns.** Every matching row comes back, in
either mode. There used to be a flat ceiling of 200 on every tool query, and it was
wrong in a way that could not be seen from the answer: the operator's ``LIMIT``, or
its absence, is the statement of how much data the question is about, and a second
number applied underneath it turned every large result into a sample that was then
reported as a total. The only bound that remains is on the *text handed to a language
model* — :data:`PROMPT_ROW_LIMIT`, applied by :func:`describe_result`, where a context
window is a physical size — and because the rows were all read, the exact total is
stated beside them. Everything that moves rows between components rather than into a
prompt (a chain's root, an export, a Graph Designer node, an aggregation) sees them
all.

**Only active tables and columns are read.** Data Sources lets the user switch a
table or a column off, and this is the place that switch has to be real: a tool
config is a standing permission written once and run for months, so the question
"may this column be read" is answered at run time from
``datasource.configuration_data`` (app.utils.datasource_status), not from what was
true when the config was saved. A reference that has since been switched off fails
the tool with a message the agent relays — it is not quietly dropped, because a
dropped filter widens the result set and a dropped group-by changes what the number
means, and either way the model states the wrong figure as fact.

The same rule decides what ``SELECT *`` means here. A config that names no columns
selects **every active column of every table the query reads**, joined tables
included — see :func:`_selected_columns`.

SQL-mode tools get the **table** half of that rule and not the column half. The
tables come from what the operator recorded on the tool config (``table_name`` plus
``extra_tables``), because nothing here parses a FROM clause; the columns cannot be
checked at all without rewriting the statement, which is exactly what this module
refuses to do. Their statement is what the operator wrote and approved.

Only relational datasources are supported (see :data:`query_joins.RDBMS_DB_TYPES`).
A tool config pointed at Mongo or a file is refused with a message the agent can
relay, rather than being silently skipped.

**Two entry points, one execution.** :func:`execute_tool_query` is the agent's:
every failure becomes a sentence a model can say out loud, and the driver's own
words never leave this module. :func:`probe_tool_query` is the *Test* button's, in
the Tool Configs form and the Ask AI panel — the same query, the same validators,
the same active-table and active-column rules, but failures leave intact so the
operator is shown what the database actually said. The point of that button is that
a query which cannot run is found while it is being written, not by a visitor
months later, and a query is only tested honestly by running the thing itself.
"""

import json
import logging
import os
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from litestar.exceptions import HTTPException
from sqlalchemy import (
    MetaData,
    Table,
    and_,
    bindparam,
    func,
    or_,
    select,
    text,
)
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.sql import Select
from sqlalchemy.sql.elements import ColumnElement

from app.db.db_utils import get_engine
from app.services.datasource.metadata_service import rdbms_url
from app.services.tool_configs.tool_config_service import (
    validated_query_config,
    validated_tool_sql,
)
from app.utils.datasource_status import (
    active_column_names,
    inactive_column_message,
    inactive_table_message,
    is_table_active,
)
from app.utils.query_joins import RDBMS_DB_TYPES, query_tables
from app.utils.sql_guard import MAX_SQL_LENGTH

logger = logging.getLogger(__name__)


# What the agent is to do about a tool that cannot run. Kept off the messages
# themselves and carried on the exception instead, because the same failure now has
# two audiences: an agent mid-conversation, which needs telling to stop and say so,
# and an operator who pressed **Test** in the form, for whom "tell the user" is
# addressed to the wrong person. The message states the fault; the advice states who
# does what about it.
NEEDS_RECONFIGURING = "Tell the user the tool needs reconfiguring."
NOT_AVAILABLE = "Tell the user this tool is not available."


class ToolQueryError(Exception):
    """
    A tool's query could not be run.

    Not an HTTPException: this is raised inside a tool call, and the agent needs
    to be *told* the tool failed so it can say so, rather than having the whole
    chatbot turn collapse into a 500. The tool wrapper in
    app.services.deep_agents.tool_factory converts it to tool output.

    ``str(exc)`` is the fault on its own, which is what an operator testing the
    query is shown. :attr:`for_agent` adds ``advice`` — the instruction that only
    makes sense to a model talking to a visitor.
    """

    def __init__(self, message: str, advice: str = NEEDS_RECONFIGURING) -> None:
        super().__init__(message)
        self.advice = advice

    @property
    def for_agent(self) -> str:
        """The message as the model should read it: the fault, then what to do."""
        return f"{self} {self.advice}".strip()


# How many rows are written into a language model's prompt. **This is not a cap on
# what a query returns** — nothing here bounds that any more, and the distinction is
# the whole point of the name.
#
# A tool query reads every row it matches. It used to stop at 200, and that number
# then meant two incompatible things at once: how much a model can be handed, and how
# much of somebody's data exists. Those came apart on any real table. A total taken
# over the first 200 of 5,275 rows is a plausible number that is wrong, and neither
# the tool nor the model had anything to say about the other 5,075 because they were
# never read.
#
# So the fetch is unbounded and this bound moved to the one place that genuinely
# cannot take every row: the prompt string itself. A context window is a physical
# limit, and the alternative to truncating here is not a bigger answer, it is a turn
# that fails outright. What makes that honest — and what the old cap could not do — is
# that the rows *were* all read, so ``describe_result`` states the exact total beside
# the sample instead of warning that a total is unknowable.
#
# Everything that moves rows between components — a chain's root, an export, a Graph
# Designer node, an aggregation — reads past this number and is unaffected by it.
PROMPT_ROW_LIMIT = 200

# How many rows the model may **print**. Below PROMPT_ROW_LIMIT, and for a
# different reason: the 200 is what the model may reason over — count, compare,
# aggregate — and this is what may go into a chat bubble as a table the reader
# actually scrolls. Past it the answer is this many rows plus the real total and an
# offer to send the rest as a file (see app/services/downloader_agents/), because a
# result with no end and no total is not an answer.
#
# Raised from 20 on request: the widget now renders a Markdown table with its own
# horizontal scroll (see documentations/WIDGET_RENDERING.md), so a long result reads
# as a table rather than as a wall of prose. This is the number the offer keys off
# too — describe_tool_result only counts and offers past it — so a set of 100 or
# fewer arrives whole with no download step in the way.
#
# It is enforced by instruction rather than by truncation, because truncating here
# would take the other rows away from the model as well and leave it unable to
# answer the question it was asked. See _GROUNDING_RULES in prompt_builder.
DISPLAY_ROW_LIMIT = 100


def _int_env(name: str, default: int) -> int:
    """An operator-set integer, falling back rather than failing on nonsense."""
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        logger.warning("%s is not a whole number; using %d", name, default)
        return default

    return value if value > 0 else default


# How many times a parent may be re-run when a child link iterates (``binding_mode``
# ``each``): the child returns N values and the parent's query runs once per value.
#
# This is the one row-shaped ceiling that survived the caps being removed, and it
# survived because it is not one. It bounds **round trips**, not rows: an expanding IN
# hands any number of values to the database in one statement, while an iterating link
# is N separate statements, each with its own planning, its own cursor and its own
# share of a chat turn's 120 seconds. Removing it would not return more of somebody's
# data — it would spend a whole turn's budget and time out with none of it.
#
# Past it the run is **refused**, never truncated, and that is the other half of why it
# is allowed to stay: rows from the first fifty departments look exactly like rows from
# every department, and a total taken over them is a plausible number that is wrong. A
# refusal says so; a quiet stop at fifty does not. Env-settable, so an operator whose
# hardware and timeouts justify more can raise it.
MAX_CHAIN_ITERATIONS = _int_env("TOOL_CHAIN_MAX_ITERATIONS", 50)

# What a test run fetches. One row is the whole proof: the database accepted the
# statement, resolved every identifier in it, and produced a result set. Reading
# more would only move data nobody is going to look at — the panel reports the
# column names and the count, never a value.
PROBE_ROWS = 1

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

def _is_text_column(column: ColumnElement) -> bool:
    """
    Whether this column can hold a string, and so whether emptiness means anything.

    Decided from the **reflected** type, which is why it is a function and not a
    config flag: the answer has to be about the column the database has now, not the
    one it had when the tool was saved.
    """
    try:
        return column.type.python_type is str
    except (NotImplementedError, AttributeError):
        # A type SQLAlchemy cannot map to a Python one. Treated as non-text, which
        # degrades IS BLANK to IS NULL rather than emitting a TRIM the database may
        # refuse — the conservative direction.
        return False


def _blank(column: ColumnElement, _value: Any) -> ColumnElement:
    """
    No value at all: NULL, empty, or nothing but whitespace.

    ``TRIM`` is applied only to a text column. Postgres has no ``btrim(integer)``, so
    trimming a numeric column is not a stricter check, it is an error — and for a
    numeric column there is no empty string to catch, so ``IS NULL`` is already the
    whole of what "blank" can mean.
    """
    if _is_text_column(column):
        return or_(column.is_(None), func.trim(column) == "")

    return column.is_(None)


def _not_blank(column: ColumnElement, _value: Any) -> ColumnElement:
    """
    The negation of :func:`_blank`, written out rather than wrapped in ``not_``.

    ``NOT (a OR b)`` over a nullable column is where three-valued logic bites: the
    intent is "has a real value", and spelling out ``IS NOT NULL AND TRIM(…) <> ''``
    says exactly that in a form no reader has to reason about.
    """
    if _is_text_column(column):
        return and_(column.is_not(None), func.trim(column) != "")

    return column.is_not(None)


# How each stored filter operator becomes a SQLAlchemy expression. Every one of
# these produces a bound parameter for the value — that is the whole reason the
# mapping is written out rather than assembled from the operator string.
#
# The last four ignore the value they are handed, because they have none: see
# VALUELESS_FILTER_OPERATORS. They still take the argument so that every builder has
# one signature and the caller does not have to branch.
_FILTER_BUILDERS = {
    "=": lambda column, value: column == value,
    "!=": lambda column, value: column != value,
    ">": lambda column, value: column > value,
    "<": lambda column, value: column < value,
    "LIKE": lambda column, value: column.like(value),
    "IS NULL": lambda column, _value: column.is_(None),
    "IS NOT NULL": lambda column, _value: column.is_not(None),
    "IS BLANK": _blank,
    "IS NOT BLANK": _not_blank,
}


async def execute_tool_query(
    datasource,
    config: dict,
    table_name: str,
    row_limit: Optional[int] = None,
    sql_query: Optional[str] = None,
    table_names: Optional[List[str]] = None,
    value_bindings: Optional[List[dict]] = None,
    agent_values: Optional[Dict[str, Any]] = None,
    sql_params: Optional[List[dict]] = None,
    max_length: int = MAX_SQL_LENGTH,
    max_rows: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    Run one tool config's query and return its rows as dictionaries.

    ``sql_query`` picks the mode: present means run that statement, absent means
    build the query from ``config``. The caller passes the row as it was stored
    rather than a mode flag, so a tool cannot end up in a mode whose query is
    missing.

    ``value_bindings`` narrows the query to values another tool produced — the
    nested-tool feature, see :func:`execute_value_query`. Each entry is
    ``{"reference": "projects.client_id", "values": [7, 9]}`` in builder mode or
    ``{"reference": "active_clients", "values": [...]}`` in SQL mode, where the
    reference is a bind parameter name, plus an optional ``"expanding": False`` for a
    link that iterates rather than matching a list. **Values, never text**: in builder
    mode they become an ``IN`` or an ``=`` over a reflected column, in SQL mode a bind
    parameter, so nothing here is ever concatenated into a statement.

    ``agent_values`` is what the model supplied for the parameters the operator
    opened — builder mode's ``agent_supplied`` filters, and SQL mode's declared
    ``sql_params``. Both are read through the stored declaration, so a name the
    operator did not open has nowhere to land.

    **Every matching row comes back.** ``row_limit`` and ``max_rows`` both default to
    ``None`` — no ceiling — and a caller that wants fewer rows says so: ``PROBE_ROWS``
    for a test run, which is the only caller left that does. There is no longer a cap
    a stored config could be silently held to, because a capped result set is a sample,
    and a sample presented as an answer is wrong in a way nothing downstream can detect:
    a total taken over the first 200 of 5,275 rows is a plausible number.

    What is bounded instead is the *prompt* — :data:`PROMPT_ROW_LIMIT`, applied by
    :func:`describe_result` when the rows are turned into text for a model, with the
    exact total stated beside them. Reading every row is what makes that total exact.
    See :func:`_row_ceiling`.

    Whichever mode it is, the stored query is **re-validated here** even though it
    was validated when it was saved. That is deliberate: this function's guarantees
    have to hold for a row however it got into the database, including one edited
    directly in psql, so it re-derives what it will execute instead of trusting the
    row. The datasource's ``configuration_data`` is read the same way and for the
    same reason: a column switched off after the config was saved has to be refused
    on this run, not on the next edit.

    ``table_names`` is every table the tool records itself as reading, primary first.
    It is what makes the active-table check possible in **SQL mode**, where nothing
    parses the statement: the operator said which tables it reads, so those are the
    ones checked. Builder mode ignores it and derives its tables from the config,
    which cannot be out of step with the query it is about to build.

    Every failure leaves here as a :class:`ToolQueryError` phrased for the model.
    :func:`probe_tool_query` runs the same query for an operator and lets the real
    failure through instead — see there for why the two entry points differ.
    """
    try:
        return await _run_query(
            datasource,
            config,
            table_name,
            row_limit,
            sql_query,
            table_names,
            value_bindings,
            agent_values,
            sql_params=sql_params,
            max_length=max_length,
            max_rows=max_rows,
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
            "This tool's saved query is no longer valid, so it cannot run."
        ) from exc
    except SQLAlchemyError as exc:
        # The driver message can name schema objects and even echo values, so it is
        # logged rather than handed to a model that is talking to a visitor. The
        # operator gets it verbatim through `probe_tool_query`, which is a different
        # audience with a different right to it.
        #
        # Deliberately does not claim the problem is temporary. A driver error here is
        # as likely to be permanent (a config with plain columns and aggregations but
        # no GROUP BY, which the operator has to fix) as transient, and telling a
        # visitor to retry something that will never work is worse than saying the
        # figure is unavailable.
        logger.exception("Tool query failed for table %s", table_name)
        raise ToolQueryError(
            "The query could not be run against the database.",
            advice=(
                "Tell the user you cannot retrieve that figure right now, and do not "
                "attempt to answer it from anything else."
            ),
        ) from exc


async def probe_tool_query(
    datasource,
    config: dict,
    table_name: str,
    sql_query: Optional[str] = None,
    table_names: Optional[List[str]] = None,
    value_bindings: Optional[List[dict]] = None,
    agent_values: Optional[Dict[str, Any]] = None,
    sql_params: Optional[List[dict]] = None,
) -> Dict[str, Any]:
    """
    Run a query once, as a test, and report what came back —
    ``{"columns": [...], "row_count": n}``.

    Same query, same validators, same active-table and active-column rules as
    :func:`execute_tool_query`: a test that ran anything less than what the tool will
    run would be worth nothing. What differs is the **audience**, and so what happens
    to a failure. The agent gets a sentence it can relay and never the driver's words;
    the operator pressing *Test* in the form is the person who has to fix the query,
    and the driver's own complaint — "Unknown column 'x' in field list", the
    ONLY_FULL_GROUP_BY paragraph — is the single most useful thing to show them. So
    `ToolQueryError`, `HTTPException` and `SQLAlchemyError` all leave here unchanged
    and the caller (app.services.query_test.query_test_service) phrases them.

    Only :data:`PROBE_ROWS` rows are fetched. The query still *runs* in full on the
    database, exactly as a real call would — this bounds what crosses the wire, and
    the point is to learn whether it runs at all, not to read data.

    ``value_bindings`` is what a nested tool's children produced. A test of a tool
    that embeds others runs the whole chain first, so what is probed here is the
    query as it will actually run rather than an unrestricted version of it.

    ``agent_values`` stands in for what a model would supply for the tool's declared
    parameters. The test panel fills required ones so the statement can be proved to
    run; the values are the operator's own, never invented here.
    """
    rows = await _run_query(
        datasource,
        config,
        table_name,
        PROBE_ROWS,
        sql_query,
        table_names,
        value_bindings,
        agent_values,
        sql_params=sql_params,
    )

    return {
        "columns": list(rows[0].keys()) if rows else [],
        "row_count": len(rows),
    }


async def execute_value_query(
    datasource,
    config: dict,
    table_name: str,
    column: str,
    sql_query: Optional[str] = None,
    table_names: Optional[List[str]] = None,
    value_bindings: Optional[List[dict]] = None,
    agent_values: Optional[Dict[str, Any]] = None,
    sql_params: Optional[List[dict]] = None,
) -> List[Any]:
    """
    Run a tool as an **inner** query and return one column of its result,
    de-duplicated, ``NULL``s dropped.

    This is what an embedded tool does inside a chain. It stays a separate entry point
    from :func:`execute_tool_query` because it answers a different question — *which
    values*, not *which rows* — and de-duplicates, which a tool result must not.

    **Every value comes back.** These rows go nowhere near a model: one column of them
    becomes a ``WHERE … IN`` on the tool that embeds this one, and is discarded the
    moment the outer query runs. So the question here is only what a database can be
    asked to match, and a database is better placed to answer that than a constant is
    — a driver refusing an oversized statement says so plainly, where a truncated list
    produced a query that ran, returned rows, and was **wrong** with nothing to show
    for it.

    That second half is why nothing is capped rather than capped higher. A partial
    ``IN`` list answers a different question than the one asked and says nothing about
    having done so, so the only two honest options were refusing the chain outright —
    which meant a tool over the limit simply could not be embedded — or reading it all.

    Ordinary tool rules still apply: the statement is re-validated, only active
    tables and columns are read, and the failure phrasing is the caller's to choose
    — nothing is caught here.
    """
    rows = await _run_query(
        datasource,
        config,
        table_name,
        None,
        sql_query,
        table_names,
        value_bindings,
        agent_values,
        sql_params=sql_params,
        max_rows=None,
    )

    values: List[Any] = []
    seen = set()

    for row in rows:
        if column not in row:
            raise ToolQueryError(
                f"The inner tool does not return a column called '{column}'. It "
                f"returns: {', '.join(str(name) for name in row) or 'nothing'}.",
            )

        value = row[column]

        if value is None:
            # A NULL never matches an IN comparison, so carrying it forward would
            # only inflate the list. If every value is NULL the list is empty, and
            # an empty list stops the chain — which is the right answer.
            continue

        marker = (type(value).__name__, value)
        if marker in seen:
            continue

        seen.add(marker)
        values.append(value)

    return values


def _row_ceiling(
    row_limit: Optional[int],
    max_rows: Optional[int],
) -> Optional[int]:
    """
    How many rows this query may return — or ``None`` for every row it matches.

    Two numbers meet here and the rule reads badly inline, which is why it is a function
    with a name: ``row_limit`` is what the caller asked for and ``max_rows`` is the ceiling
    it is held to. A caller asking for nothing in particular gets the ceiling; a caller
    asking for more than the ceiling gets the ceiling.

    **Both are ``None`` by default now, so the ordinary answer is "every row".** The
    clamping survives for the callers that ask for a specific small number — a test probe
    wants one row and must not be handed a table — but no caller imposes a ceiling on a
    query it did not ask for. That used to be 200 everywhere, which meant a stored tool
    config answering a question about 5,275 records returned 200 of them and reported the
    figure as though it were the answer.

    Rows in a *prompt* are a separate matter and still bounded, by
    :data:`PROMPT_ROW_LIMIT` where the text is built. Bounding the text rather than the
    query is what lets the exact total be stated: it was read.
    """
    if max_rows is None:
        return int(row_limit) if row_limit else None

    if not row_limit:
        return max_rows

    return max(1, min(int(row_limit), max_rows))


async def _run_query(
    datasource,
    config: dict,
    table_name: str,
    row_limit: Optional[int],
    sql_query: Optional[str],
    table_names: Optional[List[str]],
    value_bindings: Optional[List[dict]] = None,
    agent_values: Optional[Dict[str, Any]] = None,
    sql_params: Optional[List[dict]] = None,
    max_rows: Optional[int] = None,
    max_length: int = MAX_SQL_LENGTH,
) -> List[Dict[str, Any]]:
    """
    Connect, run whichever mode the tool is in, and return the rows.

    The shared half of :func:`execute_tool_query`, :func:`probe_tool_query` and
    :func:`execute_value_query` — everything up to what a failure should *say*,
    which is the only thing those callers disagree about.

    ``max_rows`` is the ceiling ``row_limit`` is clamped to, and :func:`_row_ceiling`
    is where the two meet. Both default to no ceiling: whatever the caller asked for is
    what it gets, and asking for nothing means every matching row. The only caller that
    still names a number is the test probe, which wants exactly one.
    """
    db_type = (datasource.db_type or "").strip().lower()

    if db_type not in RDBMS_DB_TYPES:
        raise ToolQueryError(
            f"This tool reads a {datasource.db_type or 'unknown'} datasource, and "
            "only relational databases (PostgreSQL, MySQL, SQLite) can be queried "
            "this way.",
            advice=NOT_AVAILABLE,
        )

    url = rdbms_url(datasource)
    limit = _row_ceiling(row_limit, max_rows)

    engine = await get_engine(url)

    async with engine.connect() as connection:
        if (sql_query or "").strip():
            require_active_tables(
                table_names or [table_name], datasource.configuration_data,
            )
            return await _execute_sql_query(
                connection, sql_query, limit, value_bindings, sql_params,
                agent_values, max_length=max_length,
            )

        return await _execute_built_query(
            connection,
            config,
            table_name,
            db_type,
            limit,
            datasource.configuration_data,
            value_bindings,
            agent_values,
        )


def require_active_tables(table_names: List[str], configuration_data: Any) -> None:
    """
    Refuse before running if any table the tool reads has been switched off.

    Public because the exporter
    (app.services.downloader_agents.base.record_reader) has to apply the same check
    before it streams a SQL-mode statement. A second implementation next door would
    be a second answer to "may this table be read", and the whole point of the rule
    is that there is one.

    This is the SQL-mode half of the active rule. The statement cannot be inspected
    or rewritten here — that is the mode's whole bargain — so what *can* be honoured
    is the table list the operator recorded. It is a coarser guarantee than builder
    mode's (a switched-off *column* still gets read by a SQL statement that names it),
    and saying so plainly is better than implying a check that is not happening.
    """
    for name in table_names:
        if name and not is_table_active(configuration_data, name):
            raise ToolQueryError(
                inactive_table_message(name)
            )


async def assemble_built_query(
    connection,
    config: dict,
    table_name: str,
    db_type: str,
    configuration_data: Any = None,
    value_bindings: Optional[List[dict]] = None,
    agent_values: Optional[Dict[str, Any]] = None,
) -> Tuple[Select, Dict[str, Table]]:
    """
    Builder mode, up to but not including the row cap: ``(statement, tables)``.

    Split out of :func:`_execute_built_query` for the exporter
    (app.services.downloader_agents.base.record_reader), which needs the same
    statement this module would run but has to add its own ``ORDER BY``, ``LIMIT``
    and ``OFFSET`` to page through it — and needs to wrap it in a ``COUNT(*)``.

    It is a **borrowed statement, not a copied one**. That is the whole point: an
    export must be able to read exactly what the tool can read and nothing more, and
    re-deriving the clause logic next door is how that stops being true after the
    third edit to one of them. Every guarantee this module makes — re-validation,
    reflection instead of string building, bound filter values, active tables and
    columns — is made here, once, for both callers.

    The reflected tables come back with the statement because the exporter needs
    them: a deterministic ``ORDER BY`` for offset paging has to name real columns,
    and the base table's primary key is only knowable from the reflection.
    """
    validated = validated_query_config(json.dumps(config or {}), table_name, db_type)

    tables = await _reflect_tables(
        connection, table_name, validated.get("joins") or [], configuration_data,
    )
    statement = _build_select_core(
        validated, table_name, tables, configuration_data, value_bindings,
        agent_values,
    )

    return statement, tables


def assemble_sql_statement(
    sql_query: str,
    value_bindings: Optional[List[dict]] = None,
    sql_params: Optional[List[dict]] = None,
    agent_values: Optional[Dict[str, Any]] = None,
    max_length: int = MAX_SQL_LENGTH,
):
    """
    SQL mode, up to but not including execution: the re-validated statement.

    The lines every SQL-mode caller needs before it can run anything —
    ``validated_tool_sql``, the nested tools' bind parameters and the agent-supplied
    ones — in one place, for the same reason as :func:`assemble_built_query`. The
    exporter must not be able to run a statement that skipped any of them.

    ``sql_params`` are the parameters the *operator* declared on the tool and the
    model fills in; ``agent_values`` is what it filled them with. The same division
    builder mode already has for ``agent_supplied`` filters, and the same guarantee:
    a value is bound, never rendered, so the model's whole influence is the
    right-hand side of a comparison the operator chose to open. A name the operator
    did not declare is ignored outright — ``agent_values`` is read *through*
    ``sql_params``, never iterated.

    ``max_length`` is passed on to ``validated_tool_sql`` and defaults to the rule every
    stored statement has always had. The graph designer's union node raises it because the
    statement it hands over is one this application built out of fragments that each
    passed the ordinary check — see :data:`app.utils.sql_guard.MAX_BUILT_SQL_LENGTH`.
    Nothing else about the re-validation is relaxed.
    """
    statement = text(validated_tool_sql(sql_query, max_length=max_length))

    bindings = _bindparams(value_bindings)
    bindings.extend(_declared_bindparams(sql_params, agent_values))

    if bindings:
        statement = statement.bindparams(*bindings)

    return statement


# How a declared SQL parameter's value is typed before it is bound. Builder mode has
# no equivalent because it does not need one: `_coerced_value` reads the *reflected*
# column and coerces against that. A SQL-mode statement has no column to reflect —
# nothing here parses it — so the operator says what the parameter holds, and a
# strict driver like asyncpg gets an int for `dd.id = :x` instead of the string a
# tool argument always arrives as.
_SQL_PARAM_COERCERS: Dict[str, Callable[[str], Any]] = {
    "number": lambda value: float(value) if "." in value else int(value),
    "boolean": lambda value: value.lower() in ("1", "true", "t", "yes", "y"),
    "text": lambda value: value,
}


def _declared_bindparams(
    sql_params: Optional[List[dict]],
    agent_values: Optional[Dict[str, Any]] = None,
) -> List[Any]:
    """
    The operator-declared parameters, bound to what the model supplied.

    Iterates the *declarations*, so a value the model invented for a name nobody
    declared has nowhere to land. A missing optional parameter binds ``None``, which
    is what a statement written with ``(:x IS NULL OR col = :x)`` reads as "no
    filter"; a missing required one refuses the run and tells the model to call again
    with a value rather than to make one up.
    """
    supplied = agent_values or {}
    bindings = []

    for entry in sql_params or []:
        name = str((entry or {}).get("param") or "").strip()

        if not name:
            continue

        raw = supplied.get(name)

        if raw is None or str(raw).strip() == "":
            if bool(entry.get("required", True)):
                raise ToolQueryError(
                    f"This tool needs a value for '{name}' and none was given.",
                    advice=(
                        f"Call the tool again with '{name}' set, using a value the "
                        "user actually gave you. Do not invent one."
                    ),
                )
            bindings.append(bindparam(name, value=None))
            continue

        bindings.append(
            bindparam(name, value=_coerced_declared_value(entry, str(raw).strip())),
        )

    return bindings


def _coerced_declared_value(entry: dict, value: str) -> Any:
    """
    A declared parameter's value as the type the operator said it holds.

    An unconvertible value falls back to the string rather than raising, matching
    :func:`_coerced_value`: "abc" for a number parameter is a value that matches
    nothing, which is the right answer to the question that was asked, and a
    conversion error here would read to the model as the tool being broken.
    """
    coerce = _SQL_PARAM_COERCERS.get(
        str(entry.get("type") or "text").strip().lower(), None,
    )

    if coerce is None:
        return value

    try:
        return coerce(value)
    except (TypeError, ValueError):
        return value


async def _execute_built_query(
    connection,
    config: dict,
    table_name: str,
    db_type: str,
    limit: Optional[int],
    configuration_data: Any = None,
    value_bindings: Optional[List[dict]] = None,
    agent_values: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """
    Builder mode: reflect the tables, assemble a ``Select``, run it.

    ``validated_query_config`` takes the JSON text the form submits, so the stored
    dict is re-serialised rather than a second entry point being added to it — one
    validator, one set of rules, no drift.
    """
    statement, _tables = await assemble_built_query(
        connection, config, table_name, db_type, configuration_data, value_bindings,
        agent_values,
    )

    # `limit is None` means "no ceiling" (see `_row_ceiling`). Unreachable from builder
    # mode today — only a Graph Designer SQL node asks for it, and a SQL node is always
    # SQL mode — but handled rather than left to fail as `LIMIT NULL`, because the shape
    # of that failure would be a driver error naming nothing an operator would recognise.
    result = await connection.execute(
        statement if limit is None else statement.limit(limit),
    )

    return [dict(row) for row in result.mappings().all()]


async def _execute_sql_query(
    connection,
    sql_query: str,
    limit: Optional[int],
    value_bindings: Optional[List[dict]] = None,
    sql_params: Optional[List[dict]] = None,
    agent_values: Optional[Dict[str, Any]] = None,
    max_length: int = MAX_SQL_LENGTH,
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

    **``limit is None`` fetches every matching row** (see :func:`_row_ceiling`), and only a
    Graph Designer SQL node asks for it. Everything above still holds — the statement is
    re-validated, it is not rewritten, and it is still streamed — so a `LIMIT` the operator
    wrote in the statement is the one thing bounding the result, which is the point. What
    changes is that the rows all cross the wire and land in the run's state, so the size of
    the answer is the size of the query: the graph author's decision, made in SQL, rather
    than a number this module chose for a different purpose.

    **Nested values are bound, not written in.** A statement embedding another tool
    names it as ``:active_clients``; the values arrive here as an *expanding* bind
    parameter, which SQLAlchemy renders as ``IN (?, ?, ?)`` with every value a
    parameter — or as a plain scalar when the link iterates instead. The statement
    itself is still the exact text the operator approved and ``validated_tool_sql``
    just re-checked — nothing is substituted into it, in either shape.
    """
    statement = assemble_sql_statement(
        sql_query, value_bindings, sql_params, agent_values, max_length=max_length,
    )

    result = await connection.stream(statement)

    try:
        mapped = result.mappings()
        rows = await (mapped.fetchall() if limit is None else mapped.fetchmany(limit))
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
    configuration_data: Any = None,
) -> Dict[str, Table]:
    """
    Reflect the base table and every joined table, keyed by name.

    Reflection is what converts a validated *name* into a real ``Column``, and so
    is what makes the rest of this module unable to emit SQL text. It runs inside
    ``run_sync`` because the Inspector API is synchronous — the same pattern as
    ``db_utils._reflect_one``.

    A table the user has switched off is refused before it is reflected. It reads
    like the "no longer exists" case below and is fixed the same way, because from
    the tool's point of view it is the same thing: something it was built on is no
    longer available to it.
    """
    names = [base_table] + [str(entry.get("table") or "") for entry in joins]

    metadata = MetaData()
    tables: Dict[str, Table] = {}

    for name in names:
        if not name or name in tables:
            continue

        if not is_table_active(configuration_data, name):
            raise ToolQueryError(
                inactive_table_message(name)
            )

        try:
            table = await connection.run_sync(
                lambda sync_connection, table_name=name: Table(
                    table_name, metadata, autoload_with=sync_connection,
                )
            )
        except SQLAlchemyError as exc:
            raise ToolQueryError(
                f"Table '{name}' no longer exists in the datasource, so this "
                "tool cannot run."
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
    configuration_data: Any = None,
    value_bindings: Optional[List[dict]] = None,
) -> Select:
    """
    Assemble the validated config into a capped ``Select``.

    The cap is the only thing this adds to :func:`_build_select_core`, and it is
    applied last so that nothing below can widen what was already narrowed.
    """
    return _build_select_core(
        config, base_table, tables, configuration_data, value_bindings,
    ).limit(limit)


def _build_select_core(
    config: dict,
    base_table: str,
    tables: Dict[str, Table],
    configuration_data: Any = None,
    value_bindings: Optional[List[dict]] = None,
    agent_values: Optional[Dict[str, Any]] = None,
) -> Select:
    """
    Assemble the validated config into a ``Select``, with no row cap.

    Mirrors ``tool_config_service.build_query_preview`` clause for clause, so what
    the operator was shown in the Tool Configs list is what runs here.

    The active columns are worked out once, here, from the reflected tables — so
    every clause below is checked against the same set, and a column that exists in
    ``configuration_data`` but not in the real table can never enter the query.

    A nested tool's values are ANDed on as further conditions, after the stored
    filters, so they narrow the query and can never widen it.

    Uncapped, because how many rows a caller wants is not a property of the clauses:
    a tool run reads every matching row, an export pages through them fifty at a time,
    and a test probe wants one. Any ``LIMIT`` is applied by whoever runs this.
    """
    known_tables = query_tables(config.get("joins"), base_table)
    active = _active_columns(tables, configuration_data)

    selected = _selected_columns(config, base_table, tables, known_tables, active)
    statement = select(*selected).select_from(tables[base_table])

    statement = _apply_joins(statement, config.get("joins") or [], tables, active)

    for condition in _filter_conditions(
        config, base_table, tables, known_tables, active, agent_values,
    ):
        statement = statement.where(condition)

    for condition in _value_conditions(
        value_bindings, base_table, tables, known_tables, active,
    ):
        statement = statement.where(condition)

    for reference in config.get("group_by") or []:
        statement = statement.group_by(
            _resolve_column(reference, base_table, tables, known_tables, active),
        )

    return statement


def _active_columns(
    tables: Dict[str, Table],
    configuration_data: Any,
) -> Dict[str, List[str]]:
    """
    The reflected column names of each table, filtered to the active ones and kept
    in the table's own column order.

    Built from the reflection rather than from ``configuration_data`` so the result
    is what the database actually has: a column dropped from the table is gone even
    if it is still recorded as active, and a column added since the datasource was
    configured is present because nobody has switched it off.
    """
    return {
        name: active_column_names(
            configuration_data, name, [column.name for column in table.columns],
        )
        for name, table in tables.items()
    }


def _selected_columns(
    config: dict,
    base_table: str,
    tables: Dict[str, Table],
    known_tables: List[str],
    active: Dict[str, List[str]],
) -> List[ColumnElement]:
    """
    The SELECT list: plain columns first, then aggregations — the same order
    ``_preview_selection`` renders.

    An empty list means the config selects nothing specific, which the preview
    shows as ``*``. Here that becomes **every active column of every table the query
    reads**, spelled out — never a literal ``*``. Three things follow from that, all
    of them the point:

    * the columns the user switched off in Data Sources are not in the result, so
      they are not in the prompt, so the agent cannot quote them;
    * a joined query returns its joined tables' columns too. Returning only the base
      table's — which is what this used to do — meant a tool built to join customers
      to orders answered every question about the customer with nothing but order
      rows;
    * the set is the one that was reflected on this run, not whatever the table has
      grown since it was configured.

    With joins in play every column is labelled ``table_column``. Two joined tables
    both having an ``id`` is the ordinary case, and ``_execute_built_query`` returns
    ``dict(row)`` — so without distinct labels one of the two would silently
    overwrite the other and the agent would be handed a row that quietly lost a
    column. Unjoined queries keep bare names, because there is nothing to collide
    with and the field names are what the routing prompt has promised.
    """
    def resolve(reference: Any) -> ColumnElement:
        return _resolve_column(reference, base_table, tables, known_tables, active)

    selected: List[ColumnElement] = []

    for entry in config.get("columns") or []:
        reference = entry.get("column")
        if not reference:
            continue
        column = resolve(reference)
        alias = entry.get("alias")
        selected.append(column.label(alias) if alias else column)

    selected.extend(_aggregated_columns(config, resolve))

    if selected:
        return selected

    return _every_active_column(base_table, tables, known_tables, active)


def _aggregated_columns(
    config: dict,
    resolve: Callable[[Any], ColumnElement],
) -> List[ColumnElement]:
    """
    The aggregation half of the SELECT list.

    An unaliased aggregation is labelled ``function_column`` — the same name
    ``prompt_builder._returned_fields`` promises the model it will find in the
    result, which is why the two are written to be read side by side.
    """
    columns: List[ColumnElement] = []

    for entry in config.get("aggregations") or []:
        reference = entry.get("column")
        function = (entry.get("type") or "").lower()
        aggregate = _AGGREGATE_FUNCTIONS.get(function)
        if not reference or aggregate is None:
            continue

        column = resolve(reference)
        expression = aggregate(column)
        alias = entry.get("alias")
        columns.append(
            expression.label(alias) if alias
            else expression.label(f"{function}_{column.name}")
        )

    return columns


def _every_active_column(
    base_table: str,
    tables: Dict[str, Table],
    known_tables: List[str],
    active: Dict[str, List[str]],
) -> List[ColumnElement]:
    """
    The expansion of an empty selection — see :func:`_selected_columns`.

    ``known_tables`` is ``[]`` for an unjoined query (that is what
    ``query_joins.query_tables`` means by it), so the base table stands in.
    """
    read_tables = known_tables or [base_table]
    qualify = bool(known_tables)

    columns: List[ColumnElement] = []
    for table_name in read_tables:
        table = tables.get(table_name)
        if table is None:
            continue

        for name in active.get(table_name) or []:
            column = table.columns[name]
            columns.append(column.label(f"{table_name}_{name}") if qualify else column)

    if not columns:
        raise ToolQueryError(
            "Every column this tool reads is inactive in the datasource, so it "
            "has nothing to return."
        )

    return columns


def _apply_joins(
    statement: Select,
    joins: List[dict],
    tables: Dict[str, Table],
    active: Dict[str, List[str]],
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

    The join keys are held to the same active check as every other reference. A join
    on a column the user has switched off would read that column to build the result
    — it just would not show it — and the rows that come back would be selected by
    something the user believes is out of use.
    """
    for entry in joins:
        join_type = (entry.get("type") or "").lower()

        if join_type == "right":
            raise ToolQueryError(
                "This tool uses a RIGHT JOIN, which cannot be run.",
                advice=(
                    "Tell the user the tool needs reconfiguring with an inner or "
                    "left join."
                ),
            )

        right_table = tables[str(entry.get("table"))]
        left_table = tables[str(entry.get("left_table"))]

        condition = (
            _table_column(left_table, str(entry.get("left_column")), active)
            == _table_column(right_table, str(entry.get("right_column")), active)
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
    active: Dict[str, List[str]],
    agent_values: Optional[Dict[str, Any]] = None,
) -> List[ColumnElement]:
    """
    The WHERE conditions, AND-ed — the stored config has no OR.

    Every value goes in as a bound parameter. This is the single most important
    line in the module: it is what makes a stored filter value data rather than
    SQL, no matter who wrote it or how — and it is what makes ``agent_values`` safe
    to accept at all.

    **An agent-supplied filter changes where the value comes from and nothing else.**
    The column comes from the stored reference and is resolved against the reflected
    schema exactly as a fixed filter's is; the operator comes from the stored config;
    the value is coerced to the column's own Python type by :func:`_coerced_value`
    and bound. The model chooses a *value*, never a column, an operator, or a table —
    so the guarantee that no model-written text reaches the query is untouched.

    A value that is missing is not the same as a value that is empty. Missing on a
    required parameter refuses the whole query; missing on an optional one drops that
    clause and leaves every other filter standing. Dropping *any* clause the operator
    marked fixed is not a case that exists here — only the filter the operator
    explicitly opened can be absent.
    """
    supplied = agent_values or {}
    conditions: List[ColumnElement] = []

    for entry in config.get("filters") or []:
        reference = entry.get("column")
        builder = _FILTER_BUILDERS.get(entry.get("operator"))
        if not reference or builder is None:
            continue

        if entry.get("agent_supplied"):
            param = str(entry.get("param") or "")
            value = supplied.get(param)

            if value is None or str(value).strip() == "":
                if entry.get("required", True):
                    raise ToolQueryError(
                        f"This tool needs a value for '{param}' and none was given.",
                        advice=(
                            f"Call the tool again with '{param}' set, using a value "
                            "the user actually gave you. Do not invent one."
                        ),
                    )
                continue
        else:
            value = entry.get("value")

        column = _resolve_column(reference, base_table, tables, known_tables, active)
        conditions.append(builder(column, _coerced_value(column, value)))

    return conditions


def _value_conditions(
    value_bindings: Optional[List[dict]],
    base_table: str,
    tables: Dict[str, Table],
    known_tables: List[str],
    active: Dict[str, List[str]],
) -> List[ColumnElement]:
    """
    The conditions a nested tool's children produced, one per binding.

    Two shapes, decided by the link's ``binding_mode`` and carried on the binding as
    ``expanding``:

    * expanding (the default) — ``column IN (…)``, the whole list at once;
    * scalar — ``column = value``, one value, because the parent is being run once
      per value rather than once for all of them.

    Both go through ``_resolve_column`` like every other reference in this module, so
    an embedded tool cannot reach a column that does not exist or one the user has
    switched off in Data Sources — nesting gets no privilege the stored config does
    not have.

    An **empty list of values is refused, not rendered**. SQLAlchemy would happily
    emit ``IN ()``, which some databases reject and others treat as "matches
    nothing" — either way it is a query that should never have been built, because
    the chain runner stops before the parent runs when a child returns nothing.
    Reaching here with none is a bug in that runner, and it should say so.
    """
    conditions: List[ColumnElement] = []

    for binding in value_bindings or []:
        reference = (binding or {}).get("reference")
        values = _binding_values(binding)

        if not reference:
            continue

        column = _resolve_column(reference, base_table, tables, known_tables, active)

        if _is_expanding(binding):
            conditions.append(
                column.in_([_coerced_value(column, value) for value in values])
            )
        else:
            conditions.append(column == _coerced_value(column, values[0]))

    return conditions


def _bindparams(value_bindings: Optional[List[dict]]) -> List[Any]:
    """
    A nested tool's values as bind parameters, for SQL mode.

    ``expanding=True`` is what lets one ``:name`` in the statement stand for a list:
    SQLAlchemy renders it as ``IN (?, ?, ?)`` at execution and binds every value
    separately. The alternative — building the list into the SQL text — would mean
    rewriting a statement the operator approved and turning values into syntax, and
    this module does neither anywhere else.

    A binding marked ``expanding: False`` is bound as a plain scalar instead, and
    that is not a smaller version of the same thing — it is the only shape that works
    anywhere other than the right-hand side of an ``IN``. An expanding parameter
    always renders parenthesised, so ``CONCAT('%a:', :x, ':b%')`` and ``dd.id = :x``
    are syntax errors with one and correct with the other. Which shape a link uses is
    the operator's choice, recorded on the link and validated when it is saved.
    """
    bindings = []

    for binding in value_bindings or []:
        name = str((binding or {}).get("reference") or "").strip()
        values = _binding_values(binding)

        if not name:
            continue

        if _is_expanding(binding):
            bindings.append(bindparam(name, value=values, expanding=True))
        else:
            bindings.append(bindparam(name, value=values[0]))

    return bindings


def _is_expanding(binding: Optional[dict]) -> bool:
    """
    Whether this binding stands for a list or for one value.

    Defaults to ``True`` so every caller written before iterating links existed keeps
    the behaviour it had — an omitted flag means the expanding ``IN`` that was once
    the only option.
    """
    return bool((binding or {}).get("expanding", True))


def _binding_values(binding: Optional[dict]) -> List[Any]:
    """
    A binding's values, refusing an empty list.

    Shared by both modes because the refusal is the same one in both: the chain
    runner stops before the parent runs when a child returns nothing, so a binding
    that arrives empty is a bug in that runner rather than a query to build.
    """
    reference = str((binding or {}).get("reference") or "").strip()
    values = list((binding or {}).get("values") or [])

    if not values:
        raise ToolQueryError(
            f"The tool feeding '{reference}' returned no values, so this query was "
            "not run.",
        )

    return values


def labelled_rows(
    rows: List[Dict[str, Any]],
    label: Optional[Mapping[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """
    Rows with the value that produced them written alongside, for an iterating link.

    When a parent runs once per value, the rows of each run are indistinguishable
    once they are concatenated unless the query itself returns the value — and a
    statement that filters on a department without selecting it is perfectly
    reasonable SQL. The link's ``value_alias`` closes that: every row of iteration
    *i* carries the value of iteration *i* under a name the operator chose.

    Done **here, in Python, over the result** — the statement is not rewritten to add
    a column, for the same reason nothing else in this module rewrites it.

    A collision with a column the query already returns is **refused, not
    overwritten**. Either direction of silent resolution is wrong: overwriting
    replaces a real value from the database with one from the chain, and skipping
    leaves rows whose label says nothing about them. Both produce a result that looks
    right and is not, which is the failure mode this module exists to avoid.
    """
    if not label:
        return rows

    for name in label:
        for row in rows:
            if name in row:
                raise ToolQueryError(
                    f"This tool already returns a column called '{name}', so the "
                    "nested tool's value cannot also be recorded under that name. "
                    "Choose a different name for it, or leave it blank because the "
                    "query already reports the value.",
                )

    return [{**row, **label} for row in rows]


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
    active: Dict[str, List[str]],
) -> ColumnElement:
    """
    Turn a stored reference — ``"column"`` or ``"table.column"`` — into a real
    reflected ``Column``.

    An unqualified reference means the base table, matching
    ``query_joins.validated_column_reference``: configs saved before a join was
    added keep their bare names, and they still mean what they always did.

    This is the one place every explicit reference in a config passes through —
    selected columns, aggregations, filters and group-by alike — which is why the
    active check lives here rather than being repeated in each of the four.
    """
    table_name, column_name = _split_reference(reference, base_table, known_tables)

    table = tables.get(table_name)
    if table is None:
        raise ToolQueryError(
            f"This tool refers to table '{table_name}', which is not part of "
            "its query."
        )

    return _table_column(table, column_name, active)


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


def _table_column(
    table: Table,
    column_name: str,
    active: Dict[str, List[str]],
) -> ColumnElement:
    """
    One reflected, active column by name, or a readable failure.

    Reaching this with a missing column means the datasource changed under a
    saved config — a dropped or renamed column. Saying which column is missing is
    what turns that into something the operator can fix.

    A column that exists but has been switched off fails the same way rather than
    being dropped from the query. Dropping it would leave a query that still runs
    and still returns a number: a filter quietly removed widens the result set, a
    group-by quietly removed changes what each row counts, and the agent reports the
    answer with no idea it is the wrong one. A tool that says it needs reconfiguring
    is recoverable; a plausible wrong figure is not.
    """
    column = table.columns.get(column_name)

    if column is None:
        raise ToolQueryError(
            f"Column '{column_name}' no longer exists on table "
            f"'{table.name}', so this tool cannot run."
        )

    if column_name not in (active.get(table.name) or []):
        raise ToolQueryError(
            inactive_column_message(f"{table.name}.{column_name}")
        )

    return column


def describe_result(
    rows: List[Dict[str, Any]],
    total_rows: Optional[int] = None,
    count_is_lower_bound: bool = False,
    offer: Optional[str] = None,
) -> str:
    """
    Render tool output for the model as JSON, with an explicit row count.

    The count is stated separately because a shortened result and a complete one look
    identical to a model otherwise, and a bare row count invites it to report a figure
    that is really a limit.

    **This is the one place row count is bounded, and it is bounded here because a
    prompt is a fixed size.** Queries return every matching row now; a set of 5,275
    cannot go into a context window, and the failure if it were tried is not a
    shortened answer but no answer at all. So :data:`PROMPT_ROW_LIMIT` rows are
    serialised — and the header says how many there really were, exactly, because they
    were all read to find out. That is the difference the uncapped fetch bought: the
    old text could only warn that a total was unknowable.

    ``total_rows`` is the exact ``COUNT(*)`` of the same query, when the caller ran one
    (app.services.downloader_agents.base.record_reader.count_records). A caller that
    did not is no longer made to invent one: the rows it passed *are* the whole result,
    so their length is the total. It is still accepted because the export path knows the
    count without holding the rows, and because a streamed count can be a lower bound.

    ``offer`` is a finished sentence — the download offer, produced by the export
    graph's confirmation interrupt — and it is passed through **verbatim** with an
    instruction to repeat it exactly. It is not composed here and not paraphrased
    there: the sentence contains the record count and the promise of a file, and a
    model rewording either is how a user ends up told the wrong number or offered
    something that will not arrive.
    """
    if not rows:
        return "0 rows. The query returned no data."

    # Absent an explicit count, the rows are the complete result — nothing shortened
    # them on the way here — so their length is the exact total, and it is worked out
    # before any of them are dropped for the prompt.
    counted = len(rows) if total_rows is None else total_rows
    shown = rows[:PROMPT_ROW_LIMIT]

    parts = [_result_header(shown, counted, count_is_lower_bound)]
    parts.append(json.dumps(shown, default=str, ensure_ascii=False))

    if counted > DISPLAY_ROW_LIMIT:
        parts.append(
            f"Print at most {DISPLAY_ROW_LIMIT} of these rows in your answer, and "
            f"say that there are {counted} records in total."
        )

    if offer:
        parts.append(
            "Then end your answer with this sentence, word for word:\n" f"{offer}"
        )

    return "\n".join(parts)


def _result_header(
    rows: List[Dict[str, Any]],
    total_rows: int,
    count_is_lower_bound: bool,
) -> str:
    """
    The one line that says how many rows these are, and of how many.

    Two cases, because a model needs to tell them apart and cannot from the rows: these
    are the whole result, or they are the first of a larger set whose size is known.

    There used to be a third — no count at all, "200 rows, and this is not the total,
    so do not report it as one" — and removing the fetch cap removed it. Every caller
    now either ran a count or is holding every row it matched, so the number in this
    line is always a real number of records. A model asked to reason about a total it
    was told is unknowable has nothing to reason with.
    """
    if len(rows) >= total_rows and not count_is_lower_bound:
        return f"{total_rows} row(s), which is the complete result:"

    at_least = "at least " if count_is_lower_bound else ""

    return (
        f"{len(rows)} row(s) out of {at_least}{total_rows} matching record(s). "
        "These are a sample; the total is the figure to report:"
    )
