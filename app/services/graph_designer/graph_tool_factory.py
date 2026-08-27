"""
A published graph, as a tool a data agent can call.

The seam between the Graph Designer and the agent runtime. It is small on purpose: the
run itself is ``graph_run_service``'s, the *watching* of it is ``graph_runner``'s, the
description is ``prompt_builder``'s, and this module only turns "a run happened" into
"here is what to say about it".

That split is newer than the rest of this module and worth the sentence. A data agent is no
longer the only thing that can own a published graph — a tool config embeds one, a flow node
runs one, a workspace shares one — and all four need the same three questions answered:
finished, asked something, or failed. The answering is ``graph_runner``'s, once; the wording
is each owner's, because a sentence for a model talking to a visitor is not a sentence for an
operator watching a flow. What remains here is the wording, the argument schema, and the
answering tool.

## Why a graph is an entry in the *existing* tool list

``documentations/TOOL_CHAINING.md`` is explicit that the routing prompt and the callable
tool list are built from **one** list, because two lists can describe different sets — an
agent told about a tool it cannot call, or handed one the prompt never mentioned. So
``collect_agent_tools`` appends the agent's graph as an entry in the same shape every
tool config uses, marked ``kind: "graph"``, and both consumers read it from there. This
module is what ``tool_factory.build_agent_tools`` dispatches such an entry to.

## The human node, inside somebody's conversation

A graph may stop to ask a question. In the designer that is a prompt in the dock; in a
conversation there is no dock and no operator — there is a visitor and a model. There is
already a proven pattern for exactly this shape in this application, and this follows it
rather than inventing a second one: ``downloader_agents`` runs a graph to an
``interrupt()``, returns the payload's sentence for the model to relay **verbatim**, and
resumes the parked thread from a later turn when the answer arrives
(``download_graph.start_export_offer`` → ``confirm_download`` → ``resume_export``).

Two rules carry over unchanged, and both are load-bearing:

* **The question is not paraphrased.** It is the text the operator wrote, repeated word
  for word. ``download_service.offer_sentence`` gives the reason: a model rewording a
  question is how a user is asked the wrong thing, and a plain question is what makes the
  next turn's answer actionable.
* **The run is parked on a persisted ``thread_id``.** The interrupt fires in one request
  and the answer arrives in another, so nothing about the pause can live in memory.

``answer_graph_question`` is the companion tool that resumes it — the ``confirm_download``
of this feature.

## What the model is told about a result

Through ``query_executor.describe_result``, the same function every tool's rows go
through. So a graph's result is capped, labelled and counted exactly as a tool's is, and
the row rules in ``prompt_builder._GROUNDING_RULES`` apply to it without amendment. A
graph does not get its own vocabulary for "here are some rows".
"""

import logging
from typing import Any, Dict, List, Optional

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field, create_model

logger = logging.getLogger(__name__)


class _NoArguments(BaseModel):
    """
    The argument schema for a graph that needs nothing told to it.

    A model still has to be given *a* schema, and an empty one is what says "call this
    with no arguments" — the same reason ``tool_factory._NoArguments`` exists.
    """


def build_graph_tools(entry: dict) -> List[StructuredTool]:
    """
    The tools one attached graph contributes.

    Always the graph itself; plus an answering tool when the graph contains a node that
    asks a question, because a graph that can pause is useless to an agent that cannot
    resume it. Offered only when it is needed, so an agent whose graph never asks
    anything gets exactly one new tool.
    """
    tools = [_run_tool(entry)]

    if entry.get("asks_questions"):
        tools.append(_answer_tool(entry))

    return tools


def _run_tool(entry: dict) -> StructuredTool:
    """The graph itself, as one callable tool."""
    graph_uuid = str(entry.get("graph_uuid") or "")
    user_id = int(entry.get("user_id") or 0)
    tool_name = str(entry.get("tool_name") or "run_graph")
    arguments = _arguments_schema(tool_name, entry.get("sql_params") or [])

    async def run_graph(**agent_values: Any) -> str:
        """Run the graph and describe what happened."""
        from app.services.graph_designer import graph_runner

        outcome = await graph_runner.run_graph(
            user_id, graph_uuid, dict(agent_values or {}),
        )

        return _describe_outcome(outcome, tool_name)

    return StructuredTool.from_function(
        coroutine=run_graph,
        name=tool_name,
        description=_tool_description(entry),
        args_schema=arguments,
    )


def _answer_tool(entry: dict) -> StructuredTool:
    """
    The companion that resumes a paused run.

    Named after the graph so an agent holding two graphs has two unambiguous answering
    tools rather than one that has to be told which run it means.
    """
    user_id = int(entry.get("user_id") or 0)
    base_name = str(entry.get("tool_name") or "graph")
    tool_name = f"answer_{base_name}"

    class _Answer(BaseModel):
        run_id: str = Field(
            description="The run id the question came with. Copy it exactly.",
        )
        answer: str = Field(
            description="What the user replied, as they said it.",
        )

    async def answer_graph_question(run_id: str, answer: str) -> str:
        """Hand a paused run its answer and report what happened next."""
        from app.services.graph_designer import graph_runner

        outcome = await graph_runner.answer_graph_run(user_id, run_id, answer)

        # A rejected *answer* is the one failure on this path the user can fix, by
        # answering again — `graph_runner.answer_graph_run` reports it as a question still
        # waiting, with the validator's sentence as the reason. Phrasing it through
        # `_failure_text` would tell the model the opposite: that nothing they say can
        # change it and an operator has to look at it. Observed doing exactly that.
        if outcome.asks and outcome.reason:
            return (
                f"That answer was not accepted: {outcome.reason} Ask the user again for "
                "an answer of that kind, then call this tool with the same run id."
            )

        if outcome.kind == graph_runner.OUTCOME_FAILED and not outcome.view:
            # Nothing ran: a bad run id, or the resume itself was refused.
            return _failure_text(outcome.reason)

        return _describe_outcome(outcome, base_name)

    return StructuredTool.from_function(
        coroutine=answer_graph_question,
        name=tool_name,
        description=(
            f"Give an answer to a question that '{base_name}' asked. Call this only "
            "after the user has answered that question, passing the run id the question "
            "came with and what they said."
        ),
        args_schema=_Answer,
    )


def _tool_description(entry: dict) -> str:
    """
    What the model reads when deciding whether to call this graph.

    The operator's description, plus the one thing they cannot have written: that this is
    a sequence of steps rather than a single query, and that it may come back with a
    question. A model that is not told the second can only treat a returned question as a
    failure.
    """
    description = (entry.get("description") or "").strip()
    lines = [
        description or "Runs a saved sequence of queries and checks.",
        "This runs several steps in order and reports the result of the last one.",
    ]

    if entry.get("asks_questions"):
        lines.append(
            "It may come back with a question that must be put to the user word for "
            "word. When they answer, call the matching answer tool with the run id."
        )

    return " ".join(lines)


def _arguments_schema(tool_name: str, sql_params: List[dict]) -> type:
    """
    The graph's arguments, from the parameters its SQL nodes declare.

    Built the same way ``tool_factory._arguments_schema`` builds a tool's: a model is only
    offered the values an operator opened, so there is nowhere for an invented argument to
    land.
    """
    fields: Dict[str, Any] = {}

    for entry in sql_params:
        name = str((entry or {}).get("param") or "").strip()

        if not name:
            continue

        required = bool((entry or {}).get("required"))
        description = str((entry or {}).get("description") or "").strip() or (
            f"Value for {name}."
        )

        fields[name] = (
            (str, Field(..., description=description)) if required
            else (Optional[str], Field(default=None, description=description))
        )

    if not fields:
        return _NoArguments

    return create_model(f"{tool_name}_arguments", **fields)


def _describe_outcome(outcome, tool_name: str) -> str:
    """
    What to tell the model about a run.

    Four outcomes, and each says something different because a model cannot tell them
    apart otherwise: a question to relay, a result, a failure, and "still going". The
    classifying is ``graph_runner``'s — this is only the wording, which is the half that is
    specific to a model talking to a visitor and is why the two are separate modules.

    :param outcome: a :class:`app.services.graph_designer.graph_runner.GraphOutcome`
    """
    from app.services.graph_designer import graph_runner

    if outcome.asks:
        question = str((outcome.question or {}).get("prompt") or "").strip()

        return (
            f"This needs the user's answer. Ask them exactly this, word for word, and "
            f"nothing else: \"{question}\"\n"
            f"When they reply, call answer_{tool_name} with run_id "
            f"\"{outcome.run_id}\" and what they said."
        )

    if outcome.kind == graph_runner.OUTCOME_FAILED:
        return _failure_text(outcome.reason)

    if outcome.finished:
        return _describe_success(outcome.view or {}, tool_name)

    return (
        f"'{tool_name}' is still running. Tell the user it is being worked out and that "
        "they can ask again in a moment. Do not guess the answer."
    )


def _describe_success(view: dict, tool_name: str) -> str:
    """
    What a finished run produced.

    A graph's last data-producing node is not necessarily a query, so there are four
    shapes to report and they must not all be described as rows. Reporting a dictionary or
    a single value as "no rows" is what makes a model say there was no data when there
    plainly was — the failure this function is split out to avoid.

    Rows go through ``describe_result``, the same function every tool's rows go through, so
    the caps, the labelling and the exact total are the ones the grounding rules were
    written against.
    """
    from app.services.deep_agents.query_executor import describe_result

    preview = (view.get("result_preview") or {}).get("output") or {}
    kind = str(preview.get("kind") or "")
    count = preview.get("count")

    if kind == "rows":
        return describe_result(preview.get("rows") or [], total_rows=count)

    if kind == "list":
        items = preview.get("items") or []
        return (
            f"'{tool_name}' finished and returned {count} value(s): "
            f"{', '.join(str(item) for item in items)}"
            + (" (the first few of them)" if preview.get("truncated") else "")
        )

    if kind == "dict":
        entries = preview.get("entries") or {}
        return (
            f"'{tool_name}' finished and returned: "
            + ", ".join(f"{key} = {value}" for key, value in entries.items())
        )

    if kind == "value":
        return f"'{tool_name}' finished and returned: {preview.get('value')}"

    # Genuinely nothing — a graph of nothing but checks, or one whose query matched
    # nothing. Said as an answer rather than as a failure, because that is what it is:
    # the same distinction `describe_stop` draws for a chain that short-circuits.
    return (
        f"'{tool_name}' finished without returning any data. That is an answer, not a "
        "failure: say plainly that there was nothing to report."
    )


def _failure_text(reason: str) -> str:
    """
    A failed run, phrased for a model talking to a visitor.

    Says what the grounding rules need it to say: this *tool* failed, which is not the
    same as the data being unreachable, and the visitor cannot fix it by rephrasing. Both
    clauses exist because their absence has been observed to produce a loop —
    ``tool_chain_graph._ITERATION_ADVICE`` documents the same failure.
    """
    return (
        f"TOOL FAILED: {reason} Tell the user this cannot be answered at the moment and "
        "that it needs looking at by whoever set it up. Do NOT ask them to narrow, "
        "filter or rephrase the question — no rewording of theirs can change this."
    )

