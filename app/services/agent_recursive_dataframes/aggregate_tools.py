"""
The one tool an agent calls to have a whole result set grouped.

Bound only when at least one of the agent's tools has been opted in, and that is
what makes this feature additive rather than a change to every agent: with no tool
opted in, ``build_aggregate_tools`` returns nothing, the routing prompt says nothing
about it, and the agent behaves exactly as it did before this module existed.

**Why one tool rather than one per opted-in tool config.** Every other tool here is
a standing permission with a fixed question — the model chooses *which* tool, never
what it asks. This one takes an instruction, which is the opposite shape, and
minting a variant per tool config would put several free-text tools in front of a
model that ``_GROUNDING_RULES`` has just told to pick the single tool matching the
question. One tool that names which records to group keeps that rule true.

**It still cannot choose its own query.** The instruction decides the grouping, not
the SQL: the tool config's stored query is what runs, re-validated on this run like
any other, and the plan is checked against the columns that query actually returns.
The model widens what can be *asked* of a permitted result set; it does not widen
the permission.
"""

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from langchain_core.tools import StructuredTool

from app.schemas.agent_recursive_dataframes import AggregateRecordsArgs
from app.services.agent_recursive_dataframes import aggregate_service, filter_algebra
from app.services.deep_agents.prompt_builder import AGGREGATE_TOOL
from app.services.deep_agents.query_executor import ToolQueryError, describe_result

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AggregateContext:
    """
    The tools whose records may be grouped, and the model that plans the grouping.

    ``tools`` is already filtered to the opted-in entries — the filtering happens in
    :func:`aggregate_context`, once, so nothing downstream has to remember to check
    a flag. The list is the same ``collect_agent_tools`` shape everything else in
    the Deep Agent runtime reads, which is why no extra query is needed to build it.
    """

    tools: List[Dict[str, Any]]
    model: Any = None


def aggregate_context(
    tools: List[Dict[str, Any]],
    model: Any = None,
) -> Optional[AggregateContext]:
    """
    A context, or ``None`` when no tool has been opted in.

    ``None`` rather than an empty context, because the caller's question is "should
    this agent have the tool at all" and an empty context would answer it with a
    tool that refuses every call.
    """
    permitted = aggregate_service.readable_tools(tools)

    return AggregateContext(tools=permitted, model=model) if permitted else None


def build_aggregate_tools(
    context: Optional[AggregateContext],
) -> List[StructuredTool]:
    """The aggregation tool, or nothing at all when no tool has been opted in."""
    if context is None or not context.tools:
        return []

    return [_build_tool(context)]


def _build_tool(context: AggregateContext) -> StructuredTool:
    async def run_tool(instruction: str, tool_name: str = "") -> str:
        """Group a tool's whole result set and report the result."""
        try:
            outcome = await aggregate_service.aggregate(
                context.tools, instruction, context.model, tool_name=tool_name or None,
            )
        except ToolQueryError as exc:
            # Returned rather than raised, exactly as tool_factory does: a raise
            # ends the whole chat turn with a 500 for something the model can say
            # out loud and move on from.
            return f"TOOL FAILED: {exc.for_agent}"
        except Exception as exc:  # noqa: BLE001
            logger.exception("Aggregating records failed")
            return (
                f"TOOL FAILED: The records could not be grouped: {exc} "
                "Tell the user this could not be calculated."
            )

        return _describe(outcome)

    return StructuredTool.from_function(
        coroutine=run_tool,
        name=AGGREGATE_TOOL,
        description=_description(context),
        args_schema=AggregateRecordsArgs,
    )


def _describe(outcome: Dict[str, Any]) -> str:
    """
    The result as the model should read it: what was asked, then the answer.

    ``describe_result`` is reused rather than reimplemented so a result here is
    rendered and shortened exactly as any other tool result is — including the sentence
    it already writes when there are more rows than are being shown, which is the one
    thing a model must not get wrong about a figure.

    The two modes differ in one sentence and it is the important one. A fold says
    "calculated over all N records", which is the claim that makes the total
    trustworthy. A filtered result says how many **matched** out of how many were read,
    because a model told only "here are 200 records" would report them as the whole
    answer — and 200 of 4,317 is a different fact.
    """
    rows = outcome.get("rows") or []
    counted = int(outcome.get("group_count") or 0)
    read = int(outcome.get("records_read") or 0)
    is_rows = str(outcome.get("mode") or "") == filter_algebra.MODE_ROWS

    if not rows:
        nothing = (
            aggregate_service.nothing_matched_message(read) if is_rows
            else aggregate_service.no_records_message()
        )
        return f"{outcome.get('summary') or 'Nothing was calculated.'} {nothing}"

    body = describe_result(rows, total_rows=counted)

    if is_rows:
        return (
            f"{outcome.get('summary')} "
            f"{counted:,} of the {read:,} record(s) read match.\n{body}"
        )

    return (
        f"{outcome.get('summary')} "
        f"Calculated over all {read:,} record(s).\n"
        f"{body}"
    )


def _description(context: AggregateContext) -> str:
    """
    What the model is told this tool is for, and — as importantly — is not.

    The names are listed because naming one in the call is what makes the run cost
    no extra model call; the refusals are listed because a model that does not know
    a median is unavailable will ask for one and get a failure instead of choosing
    differently.

    **Filtering is stated first, and the reason is a specific failure.** An agent whose
    only tool returns every month's revenue, asked about March, will say "I cannot filter
    by month because the tool takes no date parameter" — which is true of the tool and
    false of what the agent can do, because this tool filters the records after they are
    read. A description that led with grouping left that unsaid, and a capability the
    model does not know about is one it apologises for instead of using.
    """
    names = ", ".join(str(entry.get("tool_name")) for entry in context.tools)

    return (
        "Read ALL the records one of these sources returns, narrow them to what was "
        f"asked, and report either the matching records or totals over them: {names}. "
        "Use this whenever the answer needs a condition the source itself does not "
        "take — a particular month, department, status or range — or needs every "
        "record counted rather than just the first page. The source does NOT need a "
        "parameter for it: the filtering happens after the records are read. "
        "Say which source in `tool_name` and what you want in `instruction`, for "
        "example 'revenue for the Python department in March 2026' or 'total and "
        "average amount by region'. Filtering by part of a date works — say the month "
        "or the year in plain words. Available calculations: count, sum, avg, min, "
        "max; leave them out to get the matching records themselves. It CANNOT do "
        "medians, percentiles, counts of distinct values, rankings, the top N, "
        "percentages of a total, or comparisons against an average."
    )
