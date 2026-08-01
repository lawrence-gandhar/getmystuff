"""
Composing the tool-routing prompt an agent is given.

This is what tells the Deep Agent which of its tools answers which question. It is
built in Python from the agent's own tool configs — no LLM call — for four
reasons that all matter more than prose quality:

* it cannot describe a tool the agent does not have, because the list *is* the
  tool list;
* it is reproducible, so an agent's behaviour does not drift between two saves of
  the same configuration;
* it costs nothing, so it can be regenerated on every tool change;
* no schema or data is sent anywhere to produce it.

The result is stored on ``data_agents.tool_routing_prompt`` and appended to the
operator's own ``system_prompt`` at answer time. The two are never merged in
storage — see the column comments in app.models.data_agents.

Pure functions only: no database, no I/O. prompt_sync_service supplies the rows.
"""

from typing import List, Optional

from app.services.tool_configs.tool_config_service import build_query_preview
from app.utils.query_joins import query_tables

# The standing rules, appended to every generated prompt. These are the feature's
# actual guarantee expressed to the model: it has no way to read the database
# except by calling a tool, so inventing a figure is the only failure mode left,
# and this is what forbids it.
_GROUNDING_RULES = """
How to answer questions about data:

1. Every figure, count, total, name and date you give MUST come from the output of
   one of the tools listed above. You have no other access to the data.
2. Never estimate, guess, extrapolate or reuse a number from earlier in the
   conversation as if it were fresh. If you did not just read it from a tool
   result, you do not know it.
3. Pick the single tool whose description matches the question and call it. If two
   tools could both apply, prefer the one whose returned fields the question
   actually asks about.
4. If no tool covers what was asked, say plainly that you do not have access to
   that information and name what you *can* report. Do not apologise repeatedly
   and do not speculate about what the answer might be.
5. A tool that returns no rows means there is no matching data. Say that. It does
   not mean the tool failed.
6. A tool result that says it was capped is a sample, not a total. Never present a
   capped row count as a complete count.
7. Report figures as the tool returned them. You may add them up or compare them,
   but say when you have done so.
"""

# Shown when an agent has no usable tools. An agent in this state must refuse
# rather than fall back on the model's own knowledge, which would look like a
# working answer and be entirely invented.
_NO_TOOLS_PROMPT = """
You have NO data tools configured, so you cannot look anything up.

If you are asked a question about the user's data, say that this assistant has no
data sources configured yet and that the account owner needs to add a tool in the
Tool Configs section. Do not attempt to answer from general knowledge, and do not
invent figures, names or dates.
"""

# The built-in tools deepagents always provides alongside ours. Verified against
# deepagents 0.7.1's actual bound tool set, not its documentation: ls, read_file,
# write_file, edit_file, delete, glob, grep and task. They are harmless — the
# default StateBackend keeps that filesystem in conversation state, never on disk,
# and it starts every turn empty — but a model that is not told so will treat
# read_file as somewhere the user's data might already be sitting, and answer from
# an empty file instead of calling a tool. See documentations/DEEP_AGENTS.md.
_SCRATCH_TOOL_NOTE = """
You also have generic file tools (ls, read_file, write_file, edit_file, delete,
glob, grep) and a task tool. None of them can reach the user's data: that
filesystem is your own private scratch space, it starts empty in every
conversation, and nothing ever writes the user's data into it. Never treat
anything found there as a source for an answer, never report an empty file as
meaning there is no data, and never use them in place of calling a data tool.
"""


def build_tool_routing_prompt(agent_name: str, tools: List[dict]) -> str:
    """
    The generated routing prompt for one agent.

    ``tools`` is a list of dicts as assembled by
    :func:`app.services.deep_agents.prompt_sync_service.collect_agent_tools` —
    one per *enabled* tool config, each with ``tool_name``, ``description``,
    ``table_name``, ``config``, ``datasource_name`` and ``db_type``.
    """
    heading = (
        f"# Data tools available to {agent_name}\n"
        if agent_name
        else "# Data tools available to you\n"
    )

    if not tools:
        return f"{heading}{_NO_TOOLS_PROMPT.strip()}"

    blocks = [_describe_tool(tool) for tool in tools]

    return "\n\n".join([
        heading.strip(),
        (
            f"You have {len(tools)} data "
            f"{'tool' if len(tools) == 1 else 'tools'}. Each one runs a fixed, "
            "pre-approved query and takes no arguments — you choose which to call, "
            "not what it queries."
        ),
        "\n\n".join(blocks),
        _GROUNDING_RULES.strip(),
        _SCRATCH_TOOL_NOTE.strip(),
    ])


def _describe_tool(tool: dict) -> str:
    """
    One tool's entry: what it is for, where it reads from, and what it returns.

    "What it returns" is spelled out per field rather than left to the SQL preview
    because the field names in the result are what the model has to quote back —
    and an aliased aggregation's name is not guessable from the config.
    """
    config = tool.get("config") or {}
    table_name = tool.get("table_name") or ""

    lines = [f"## {tool.get('tool_name')}"]

    description = (tool.get("description") or "").strip()
    lines.append(
        f"Purpose: {description}"
        if description
        else "Purpose: not described by the operator — rely on the fields below."
    )

    source = tool.get("datasource_name") or "the configured datasource"
    db_type = (tool.get("db_type") or "").strip()
    lines.append(
        f"Reads: {_source_description(config, table_name)} "
        f"in {source}{f' ({db_type})' if db_type else ''}."
    )

    returned = _returned_fields(config)
    lines.append(
        f"Returns fields: {', '.join(returned)}."
        if returned
        else f"Returns: every column of {table_name}."
    )

    grouping = [str(entry) for entry in config.get("group_by") or [] if entry]
    if grouping:
        lines.append(
            f"One row per distinct {', '.join(grouping)} — the aggregates above are "
            "totals within each of those groups, not overall totals."
        )

    scope = _filter_description(config)
    if scope:
        lines.append(
            f"Always restricted to: {scope}. This restriction is fixed — you cannot "
            "widen or change it, so say so if the question needs data outside it."
        )

    lines.append(f"Query it runs: {build_query_preview(config, table_name)}")

    return "\n".join(lines)


def _source_description(config: dict, table_name: str) -> str:
    """Which table(s) the tool reads, naming joined ones explicitly."""
    tables = query_tables(config.get("joins"), table_name)

    if not tables:
        return f"table {table_name}"

    joined = ", ".join(tables[1:])
    return f"table {tables[0]} joined to {joined}"


def _returned_fields(config: dict) -> List[str]:
    """
    The field names in the tool's result, in the order they are selected.

    An aggregation without an alias is named the way
    :func:`app.services.deep_agents.query_executor._selected_columns` labels it, so
    what the prompt promises matches what the model actually receives.
    """
    fields: List[str] = []

    for entry in config.get("columns") or []:
        column = entry.get("column")
        if not column:
            continue
        fields.append(str(entry.get("alias") or column))

    for entry in config.get("aggregations") or []:
        column = entry.get("column")
        function = (entry.get("type") or "").lower()
        if not column or not function:
            continue

        alias = entry.get("alias")
        if alias:
            fields.append(f"{alias} ({function.upper()} of {column})")
        else:
            bare_column = str(column).rpartition(".")[2]
            fields.append(f"{function}_{bare_column} ({function.upper()} of {column})")

    return fields


def _filter_description(config: dict) -> Optional[str]:
    """The fixed WHERE clause in words, or None when the tool is unfiltered."""
    clauses = [
        f"{entry.get('column')} {entry.get('operator')} '{entry.get('value')}'"
        for entry in config.get("filters") or []
        if entry.get("column") and entry.get("operator")
    ]

    return " and ".join(clauses) if clauses else None


def compose_runtime_prompt(
    system_prompt: Optional[str],
    tool_routing_prompt: Optional[str],
) -> str:
    """
    The prompt actually sent to the model: the operator's instructions, then the
    generated tool guidance.

    Order matters. The operator's text comes first so it reads as the agent's
    persona, and the grounding rules inside the generated block come last so they
    are the most recent instruction — the same reasoning as
    ``ai_analytics_service._GROUNDING_ADDENDUM``, which is always appended after a
    chatbot owner's prompt so no owner prompt can license invented figures.
    """
    sections = [
        section.strip()
        for section in (system_prompt, tool_routing_prompt)
        if section and section.strip()
    ]

    return "\n\n".join(sections)
