"""
Turning tool configs into the tools a Deep Agent can call.

One enabled ``ToolConfig`` becomes one LangChain tool, named exactly as the
operator named it. The tool's whole behaviour is fixed by the stored config:
calling it runs that query and nothing else.

**Why the tools take no arguments.** A tool config already declares its filters,
its columns and its grouping. Exposing any of that as a tool argument would put
model-generated text into the query, which is the one thing this feature is built
to avoid — and it would also let the model widen a filter the operator wrote
deliberately. So the model's only decision is *which* tool to call, which is what
the generated routing prompt is for. Parameterised filters (an operator marks a
filter value as "supplied by the agent", validated against the reflected column
type) are a real future feature, not an oversight; see DEEP_AGENTS.md.

The rows a tool returns are the only data the model ever sees.
"""

import logging
from typing import Any, Dict, List, Optional

from langchain_core.tools import StructuredTool
from pydantic import BaseModel

from app.services.deep_agents.query_executor import (
    ToolQueryError,
    describe_result,
    execute_tool_query,
)
from app.utils.query_joins import RDBMS_DB_TYPES

logger = logging.getLogger(__name__)


class _NoArguments(BaseModel):
    """
    The argument schema for every data tool: empty.

    Declared explicitly rather than left for LangChain to infer, so the schema
    advertised to the model is unambiguously "no parameters" across all three
    providers' tool-calling formats.
    """


def build_agent_tools(tools: List[dict]) -> List[StructuredTool]:
    """
    Build a callable tool per entry in ``tools``.

    ``tools`` is what
    :func:`app.services.deep_agents.prompt_sync_service.collect_agent_tools`
    returns — the same list the routing prompt is built from, so the prompt can
    never describe a tool that was not created.
    """
    return [_build_tool(entry) for entry in tools]


def _build_tool(entry: dict) -> StructuredTool:
    """
    One tool.

    The datasource row is captured in the closure rather than re-fetched per call:
    a single agent run may call several tools, and re-reading (and re-decrypting)
    the same datasource each time buys nothing. Connection pooling still happens
    in ``db_utils.get_engine``, keyed by URL.
    """
    datasource = entry["datasource"]
    config: Dict[str, Any] = entry.get("config") or {}
    table_name: str = entry["table_name"]
    tool_name: str = entry["tool_name"]
    # Non-empty for a SQL-mode tool, which the executor runs as written instead of
    # rebuilding from `config`. Passed as the stored value rather than as a mode
    # flag so the two can never disagree.
    sql_query: Optional[str] = entry.get("sql_query")

    async def run_tool() -> str:
        """Execute this tool's stored query and describe the rows."""
        try:
            rows = await execute_tool_query(
                datasource, config, table_name, sql_query=sql_query,
            )
        except ToolQueryError as exc:
            # Returned as tool output, not raised: the agent has to be told the
            # tool failed so it can say so. Raising would abort the whole turn and
            # give the visitor a 500 for what is a recoverable, explainable state.
            logger.warning("Tool %s could not run: %s", tool_name, exc)
            return f"TOOL FAILED: {exc}"

        return describe_result(rows)

    return StructuredTool.from_function(
        coroutine=run_tool,
        name=tool_name,
        description=_tool_description(entry),
        args_schema=_NoArguments,
    )


def _tool_description(entry: dict) -> str:
    """
    What the model is shown for this tool in the tool-calling schema.

    Kept short and behavioural. The full account of what the tool returns lives in
    the routing prompt (prompt_builder), which the model reads once, rather than
    being repeated in every tool schema on every request — the same information in
    both places would just cost tokens.
    """
    description = (entry.get("description") or "").strip()
    table_name = entry.get("table_name") or ""

    if description:
        return f"{description} Runs a fixed pre-approved query over {table_name}."

    return (
        f"Runs a fixed pre-approved query over {table_name}. See the tool list in "
        "your instructions for the fields it returns."
    )


def tool_names(tools: List[dict]) -> List[str]:
    """The tool names for one agent — used by the console and for logging."""
    return [str(entry.get("tool_name")) for entry in tools if entry.get("tool_name")]


def find_unsupported_tools(tools: List[dict]) -> List[str]:
    """
    Names of tools that cannot run, with the reason.

    Two causes, both permanent until the operator changes the config: a non-relational
    datasource, and a RIGHT JOIN (which ``query_executor._apply_joins`` refuses rather
    than approximating). Surfaced on the agent's console up front, so neither is
    discovered only when a visitor happens to ask a question that routes to one.

    The RIGHT JOIN check is a *builder-mode* limitation — it comes from assembling
    the query out of SQLAlchemy join operands — so it is not applied to a SQL-mode
    tool, whose statement is run exactly as written and may right-join freely.
    """
    unsupported = []

    for entry in tools:
        name = str(entry.get("tool_name"))

        if (entry.get("db_type") or "").strip().lower() not in RDBMS_DB_TYPES:
            unsupported.append(f"{name} (not a relational datasource)")
            continue

        if (entry.get("sql_query") or "").strip():
            continue

        joins = (entry.get("config") or {}).get("joins") or []
        if any((join.get("type") or "").lower() == "right" for join in joins):
            unsupported.append(f"{name} (uses a RIGHT JOIN)")

    return unsupported
