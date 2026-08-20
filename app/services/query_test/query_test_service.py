"""
Test Query — run the query a form is holding, once, and say what the database did
with it.

A tool config is written in a browser and run months later inside a conversation
nobody is watching. Everything between those two moments is checked: the shape of
the config, every identifier in it, that the statement is a single read, that each
table is still switched on. None of it can answer the one question that actually
decides whether the tool works — **will this database run this query** — because
that is the database's answer to give, not ours. A grouping MySQL refuses under
ONLY_FULL_GROUP_BY, a column that exists but is spelled differently in this
environment, a join that is valid SQL and finds nothing to join on: all of it passes
every check the application can honestly make, and all of it fails at run time, in
front of a visitor.

So the *Test Query* button runs it. Not an approximation of it, not a parse of it:
the query itself, through
:func:`app.services.deep_agents.query_executor.probe_tool_query`, which is the same
code path the agent's tool call takes — same validators, same reflection, same
active-table and active-column rules. A test that ran anything else would be a
reassurance rather than a test.

Two things are deliberately not done here:

* **Nothing is saved and nothing is changed.** The query is read-only by
  construction (the executor refuses anything else) and the row cap is
  :data:`~app.services.deep_agents.query_executor.PROBE_ROWS`. Pressing Test on a
  half-finished form leaves no trace.
* **No rows are shown.** The result is the column names and how many rows came back.
  What a passing test needs to prove is that the query runs and returns a shape; the
  values are the agent's business at run time, and in the Ask AI panel showing them
  would break the one promise that feature makes about never displaying data.

It is its own module rather than a function on Tool Configs because both callers are
peers of each other: the Tool Configs form and the Ask AI panel test the same query
the same way, and neither owns the other.
"""

import logging
import uuid
from typing import Any, List, Optional, Tuple

from litestar.exceptions import HTTPException
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.db_utils import CRUDQueryBuilder
from app.models.datasource import DataSource
from app.models.tool_configs import QUERY_MODE_SQL, ToolConfig
from app.services.deep_agents.query_executor import (
    PROBE_ROWS,
    ToolQueryError,
    probe_tool_query,
)
from app.services.tool_configs import tool_chain_service, tool_config_service
from app.utils.datasource_status import inactive_table_names
from app.utils.query_joins import supports_joins

logger = logging.getLogger(__name__)

datasource_crud = CRUDQueryBuilder(DataSource)

#: How much of a driver's complaint is shown. Long enough for the paragraph MySQL
#: writes about ONLY_FULL_GROUP_BY, short enough that a driver echoing the whole
#: statement back does not fill the panel.
MAX_DATABASE_MESSAGE = 600


async def test_query(
    db: AsyncSession,
    user_id: int,
    datasource_id: Optional[uuid.UUID],
    table_names: List[str],
    query_mode: str,
    config: Any,
    sql_query: str,
    children: Optional[Any] = None,
    sql_params: Optional[Any] = None,
    test_values: Optional[dict] = None,
) -> dict:
    """
    Run the query once and return
    ``{"passed", "message", "columns", "row_count"}``.

    ``children`` is the nested tools the form is holding. They are run first, as
    the chain, so what is probed is the query as it will actually run — a nested
    tool tested without its children would be a different, unrestricted query, and
    a pass on it would mean nothing.

    ``sql_params`` and ``test_values`` are the same bargain for a statement that
    declares values the assistant supplies. The operator provides one value per
    parameter in the panel, because a statement holding ``:department_id`` cannot be
    run without one — and a test that filled it with something invented would prove
    the statement runs for a value nobody chose.

    **A failed test is a result, not an error.** Every outcome comes back as a
    payload with ``passed`` false rather than as an exception, because the panel
    renders one alert either way and the question "did it run" has two normal
    answers. The route stays a route: no try/except of its own, no branching on
    what went wrong.

    The three ways it can fail are worth telling apart, because they are fixed in
    different places:

    * the payload or the query config is invalid (``HTTPException`` from the same
      validators the save uses) — the message names the field;
    * the tool could not be assembled (``ToolQueryError``: an inactive column, a
      table that no longer exists, a RIGHT JOIN) — the message names the thing that
      is in the way, and deliberately not the agent-facing "tell the user…" advice
      the same failure carries in a conversation;
    * the database refused it (``SQLAlchemyError``) — the driver's own words, which
      is the whole reason the button exists.
    """
    try:
        datasource = await _resolve_datasource(db, user_id, datasource_id)
        statement, config_payload, base_table, tables = _validated_query(
            datasource, table_names, query_mode, config, sql_query,
        )
        _require_active_tables(datasource, tables)

        declared = tool_config_service.validated_sql_params(
            sql_params, statement or "",
        ) if statement else None
        values = dict(test_values or {})

        if children:
            return await _test_chain(
                db, user_id, datasource, base_table, statement,
                config_payload, query_mode, children, declared, values,
            )

        result = await probe_tool_query(
            datasource,
            config_payload,
            base_table,
            sql_query=statement,
            table_names=tables,
            agent_values=values,
            sql_params=declared,
        )

    except HTTPException as exc:
        return _failed(str(exc.detail))
    except ToolQueryError as exc:
        # `str(exc)`, never `exc.for_agent`: the advice on it is addressed to a model
        # mid-conversation, and the person reading this panel is the one holding the
        # form that would do the reconfiguring.
        return _failed(str(exc))
    except SQLAlchemyError as exc:
        logger.info("Test query failed against datasource %s", datasource_id)
        return _failed(_database_message(exc))
    except Exception as exc:  # noqa: BLE001 — connection-level failures
        # Not a query problem: the datasource could not be reached at all. Logged in
        # full, reported as the one thing the user can act on.
        logger.exception("Test query could not reach datasource %s", datasource_id)
        return _failed(
            "Could not connect to the datasource, so the query was not run. Check "
            "the connection details in Data Sources and try again.",
        )

    return _passed(result)


def _passed(result: dict) -> dict:
    """A run that the database accepted, described in terms of what came back."""
    columns = list(result.get("columns") or [])
    rows = int(result.get("row_count") or 0)

    if not rows:
        # Not a failure. An empty result is a perfectly good query over data that
        # does not match — worth saying plainly, because a tool that always returns
        # nothing is usually not what was intended either.
        message = (
            "The query ran successfully but matched no rows. It is valid — check the "
            "filters if you expected data."
        )
    else:
        named = ", ".join(columns) if columns else "no named columns"
        message = f"The query ran successfully and returned {named}."

    return {
        "passed": True,
        "message": message,
        "columns": columns,
        "row_count": rows,
    }


def _failed(message: str) -> dict:
    return {"passed": False, "message": message, "columns": [], "row_count": 0}


def _stopped(stopped_by: str) -> dict:
    """
    A chain that stopped before this query ran, because an inner tool matched
    nothing.

    Reported as a **pass**, and the wording carries the weight: every query in the
    chain ran and the database accepted all of them, which is exactly what the
    button was asked. The tool would return no rows right now — worth knowing, and
    not the same thing as being broken.
    """
    return {
        "passed": True,
        "message": (
            f"The chain ran, but '{stopped_by}' matched nothing, so this query was "
            "not reached. Every query is valid — the tool would return no rows "
            "until that inner tool matches something."
        ),
        "columns": [],
        "row_count": 0,
    }


async def _test_chain(
    db: AsyncSession,
    user_id: int,
    datasource: DataSource,
    base_table: str,
    statement: Optional[str],
    config: dict,
    query_mode: str,
    children: Any,
    sql_params: Optional[list] = None,
    test_values: Optional[dict] = None,
) -> dict:
    """
    Run a nested tool's **whole chain** and report on it.

    Not the outer query with the children skipped: that would be a different,
    unrestricted query, and a pass on it would say nothing about the tool anyone is
    about to save. The graph that runs here is the graph an agent's call would run,
    compiled with a one-row limit on the root.

    The children are validated by the same function the save uses, against the query
    *as the form currently has it* — the mode, the statement and the tables may all
    be changing in this same edit — so a nesting the save would refuse is refused
    here, in the same words, before anything runs.
    """
    # Imported here rather than at module scope: this is the only path that needs
    # LangGraph, and a form with no nested tools — which is most of them — should
    # not depend on it to be tested.
    from app.services.tool_configs.tool_chain_graph import build_chain_graph, run_chain

    prospective = ToolConfig(
        id=None,
        tool_name="this tool",
        datasource_id=datasource.id,
        table_name=base_table,
        extra_tables=[],
        query_mode=query_mode,
        config=config,
        sql_query=statement,
        sql_params=sql_params,
    )

    links = await tool_chain_service.validated_children(
        db, user_id, prospective, children,
    )
    chain = await tool_chain_service.chain_from_links(
        db, prospective, datasource, links,
    )

    result = await run_chain(
        chain,
        build_chain_graph(chain, row_limit=PROBE_ROWS),
        dict(test_values or {}),
    )

    if result.short_circuited:
        return _stopped(result.stopped_by)

    rows = result.rows

    return _passed({
        "columns": list(rows[0].keys()) if rows else [],
        "row_count": len(rows),
    })


def _validated_query(
    datasource: DataSource,
    table_names: List[str],
    query_mode: str,
    config: Any,
    sql_query: str,
) -> Tuple[Optional[str], dict, str, List[str]]:
    """
    Put the posted form through the validators the save would use, and return
    ``(statement, config, base_table, tables)`` ready for the executor.

    ``statement`` is ``None`` in builder mode — the same way a tool config records
    its mode, by which query it holds rather than by a flag, so a test cannot end up
    running a different mode than the save will store.

    Deliberately the *save's* validators and not a second set: a query that fails
    here would fail on save with this exact message, and one that passes here is one
    the form will accept. A test with looser rules than the save would pass queries
    that then cannot be created.
    """
    base_table, extra_tables = tool_config_service.validated_tables(table_names)
    tables = [base_table, *extra_tables]

    if (query_mode or "").strip().lower() == QUERY_MODE_SQL:
        return tool_config_service.validated_tool_sql(sql_query), {}, base_table, tables

    validated = tool_config_service.validated_query_config(
        config, base_table, datasource.db_type,
    )

    return None, validated, base_table, tables


def _require_active_tables(datasource: DataSource, tables: List[str]) -> None:
    """
    Refuse a test over a table switched off in Data Sources, in the words of the form
    the user is standing in.

    The executor checks this too and would refuse a moment later. It is done here
    first so the message is the one an operator needs — "activate them, or deselect
    them" — rather than the one written for an agent to relay.
    """
    inactive = inactive_table_names(datasource.configuration_data, tables)

    if inactive:
        raise HTTPException(
            status_code=400,
            detail=(
                f"These tables are inactive in this datasource: {', '.join(inactive)}. "
                "Activate them in Data Sources or remove them from the query."
            ),
        )


async def _resolve_datasource(
    db: AsyncSession,
    user_id: int,
    datasource_id: Optional[uuid.UUID],
) -> DataSource:
    """
    The datasource, scoped to its owner, and only if a query can be run against it
    at all.

    A file or Mongo datasource is refused here with a sentence about *this form*.
    The executor refuses it too, but in words addressed to an agent about a saved
    tool — which is not what has happened yet.
    """
    if datasource_id is None:
        raise HTTPException(
            status_code=400, detail="Pick a datasource before testing the query",
        )

    datasource = await datasource_crud.get_by_uuid(
        db, datasource_id, extra_filters={"user_id": user_id},
    )
    if not datasource:
        raise HTTPException(status_code=404, detail="Datasource not found")

    if not supports_joins(datasource.db_type):
        raise HTTPException(
            status_code=400,
            detail=(
                f"'{datasource.datasource_name}' is not a relational datasource, so "
                "there is no SQL query to run against it."
            ),
        )

    return datasource


def _database_message(exc: SQLAlchemyError) -> str:
    """
    What the database said, as the user should read it.

    The driver's own message is the point of this feature — "Unknown column 'x' in
    'field list'", or the paragraph MySQL writes when a grouped query selects a
    column it does not group. Paraphrasing it would throw away the only text that
    says which column, and a generic "the query failed" is exactly the state this
    button exists to get people out of.

    ``exc.orig`` is the driver's exception; ``str(exc)`` on the SQLAlchemy wrapper
    would append the whole statement and its bound parameters, which is noise here —
    the statement is already on the screen above the message.
    """
    original = getattr(exc, "orig", None) or exc
    message = " ".join(str(original).split()) or "The database refused the query."

    if len(message) > MAX_DATABASE_MESSAGE:
        message = f"{message[:MAX_DATABASE_MESSAGE].rstrip()}…"

    return f"The database refused this query: {message}"
