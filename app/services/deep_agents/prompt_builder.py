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

import hashlib
from typing import List, Optional

from app.services.deep_agents.query_executor import (
    DISPLAY_ROW_LIMIT,
    PROMPT_ROW_LIMIT,
)
from app.services.tool_configs.tool_config_service import build_query_preview
from app.utils.query_joins import query_tables

# The two download tools rules 8 and 9 name. Defined here rather than imported from
# app/services/downloader_agents/base/download_tools.py, which imports this module —
# and a rule that names a tool the agent does not have is worse than no rule, so the
# download tools are built from *these* constants and a test asserts the pair match.
CONFIRM_DOWNLOAD_TOOL = "confirm_download"
DOWNLOAD_STATUS_TOOL = "download_status"

# The whole-result grouping tool, named here for the same reason as the two above:
# app/services/agent_recursive_dataframes/aggregate_tools.py imports this module,
# so the constant has to live on this side of that edge. The paragraph naming it is
# emitted only when a tool has actually been opted in, so an agent with none has a
# prompt byte-identical to the one it had before the capability existed.
AGGREGATE_TOOL = "aggregate_records"

# The tag put on a model call that is **not** part of the conversation.
#
# `astream_events` reports `on_chat_model_stream` for *every* chat-model call inside a
# turn, and until now there was only ever one: the agent answering. `aggregate_records`
# makes a second — the planner turning an instruction into a plan — and it runs inside a
# tool, so its tokens arrived on the answer stream and the visitor saw the plan's raw JSON
# printed above the answer:
#
#     {"group_by":["crm_id","department"],"aggregations":[...]}**Total revenue in August:**
#
# So the planner tags its calls and the streamer drops tagged tokens. A tag rather than a
# separate model instance, because the call is the same model with the same key and the
# same rate limit — what differs is only whether a human is meant to read it.
#
# Here for the reason AGGREGATE_TOOL is: both sides of the import edge need it, and
# aggregate_planner imports this module's package rather than the other way round.
INTERNAL_CALL_TAG = "gms-internal-call"

# The standing rules, appended to every generated prompt. These are the feature's
# actual guarantee expressed to the model: it has no way to read the database
# except by calling a tool, so inventing a figure is the only failure mode left,
# and this is what forbids it.
_GROUNDING_RULES_TEMPLATE = """
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
   capped row count as a complete count. When a result states a total, that total is
   the figure to report — not the number of rows you were shown.
7. Report figures as the tool returned them. You may add them up or compare them,
   but say when you have done so.
8. Never print more than {display_limit} rows of data in one answer. If there are
   more, show the first {display_limit}, say how many there are in total, and stop.
   Do not continue the list, do not offer to paste the rest into the chat, and do
   not describe the remaining rows one by one.
9. When a tool result gives you a sentence to end your answer with, repeat it
   exactly as written. It offers the user a downloadable file of the full set, and
   neither its wording nor its figures are yours to change. If the user then says
   yes, call `{confirm_tool}`. If they ask whether the file is ready, call
   `{status_tool}`.
10. Never write a download link, a URL or a file path in your answer. The interface
   shows the user a real download button and a live progress indicator of its own.
   Describe the file in words — "it is being prepared", "it is ready below" — and
   leave the link to the interface, which has one and you do not.
11. Most tools take NO arguments, and a tool's own schema is the only thing that
   says otherwise. A tool with no parameters runs a fixed query configured in
   advance: calling it again after the user rephrases runs exactly the same query
   and returns exactly the same result. So unless a tool declares a parameter that
   covers what the user wants changed, never ask them to narrow their question, give
   a date range, pick a smaller period, or filter the data, and never say you could
   answer "if" they did. If the tools you have cannot answer what was asked, say so
   once, say what you *can* report, and stop.
12. Where a tool DOES declare parameters, they are listed with it above. Pass only
   values the user actually gave you or that follow directly from what they asked.
   Never invent one, never guess a date, and never pass a value to widen a result
   you were refused. If you need a required parameter and the user has not given you
   enough to fill it, ask them for that one specific thing.
13. **Describe the rows you actually got, never the rows that were asked for.** A
   tool applies exactly the filters listed with it above and the parameters you
   passed — nothing else. If the user asked to narrow by something the tool does not
   filter on, you have the UNNARROWED result, and calling it "in that department",
   "for that customer", "for last month" or anything else the query never applied is
   a false answer even though every row in it is real. Say what the result actually
   is ("all projects, which I cannot break down by department"), then show it. The
   heading and the sentence above a table are claims about the data and are held to
   rules 1 and 2 exactly as a figure is.
14. A tool that answers "TOOL FAILED" has failed — the data itself may still be
   reachable. Before you give up, check whether another tool listed above covers the
   question, and if one does, call it. Two tools often read the same records in
   different shapes, and one being misconfigured says nothing about the other. Try
   ONE alternative, not every tool in turn. If it also fails, or nothing else fits,
   then say plainly that you cannot answer this at the moment and stop.
15. Format your answer in Markdown. **Put rows in a Markdown table** — a leading
   sentence, then the table — because several records as prose is unreadable and a
   table is what the interface renders. Use `|` columns with a `|---|---|` divider
   row, name every column in the header, and keep to the columns the question was
   actually about rather than every field the tool returned. Bullet lists, **bold**
   and `code` are available for everything that is not rows. Do NOT write links or
   images: they are not rendered, and rule 10 already forbids URLs. One row per
   record, and rule 8's limit still applies — {display_limit} rows, then the total.
"""

# Filled once, at import, so every prompt built afterwards carries the real numbers
# and the real tool names rather than a template nobody noticed was unformatted.
_GROUNDING_RULES = _GROUNDING_RULES_TEMPLATE.format(
    display_limit=DISPLAY_ROW_LIMIT,
    confirm_tool=CONFIRM_DOWNLOAD_TOOL,
    status_tool=DOWNLOAD_STATUS_TOOL,
)

# The whole-result section, emitted only for an agent with an opted-in source. A module
# constant rather than an f-string inside `_aggregate_note`, for one reason: it has to be
# part of what `_STATIC_PROMPT_TEXT` hashes, or editing it would not invalidate a single
# stored prompt. See _PROMPT_FINGERPRINT.
_AGGREGATE_NOTE_TEMPLATE = """\
## Reading every record: filtering and totals
These sources can have their WHOLE result read, narrowed and totalled: {names}.

Calling one of those directly gives you at most the first {row_limit} rows, with the
true total stated beside them — so a condition or a total worked out from the rows you
can see answers only for those rows.

Call `{aggregate_tool}` instead whenever the question needs either of these:
- A CONDITION the source itself does not take — one month, one department, one status,
  a range, a value in a list. The source does NOT need a parameter for it and you do
  NOT need one: say the condition in plain words in `instruction` and the records are
  narrowed after they are read. Months and years work: "in August", "in 2026".
- Every record counted, totalled or averaged rather than the rows you were shown.

**This overrides rules 11 and 13 for these sources, and only for these.** Those rules
say a tool applies only its own fixed filters, so never offer to narrow and never
describe a result as narrower than it is — both true of a direct call and both wrong
here. For the sources named above: never say you cannot filter, break down, total or
group the result, and never answer that the tool takes no parameter for it. You can,
through this tool. For every other tool, rules 11 and 13 stand exactly as written.

Say which source in `tool_name` and what you want in `instruction`, in plain words —
for example "revenue in August", "total revenue per department", "projects in the
Python department". Ask for a total, count, average, smallest or largest to get
figures; leave those out and the matching records themselves come back.

**A source's own description may tell you how it should be read** — which columns to
group by, which column a month is filtered on, what to sum. Those instructions are
carried out through this tool: include them in `instruction` alongside what the user
asked. A description saying "always group by department and sum the revenue", asked for
August, becomes an instruction like "total revenue per department in August".

Report its figures as the complete answer, and report the rows you were shown by a
direct call as a sample whenever the stated total is larger. It cannot do medians,
percentiles, counts of distinct values, rankings, "the top N", percentages of a total,
or comparisons against an average — ask for one of those and it will refuse, so choose
differently rather than asking.\
"""

# Every piece of prompt text this build owns, in one place, so one hash covers all of it.
#
# **This started as a hash of `_GROUNDING_RULES` alone, and that was one scope too
# narrow.** The bug it was introduced to prevent is "a fix to generated prompt text stays
# silently un-deployed", and it prevented that for the rules and for nothing else — so
# rewriting `_AGGREGATE_NOTE_TEMPLATE` would have changed what *new* prompts said and left
# every stored prompt describing the old capability, with no staleness anywhere to notice.
# Which is exactly the failure the marker exists for, arriving through a different door.
#
# So the fingerprint covers every static block: the rules, the no-tools prompt, the
# scratch-tool note and the whole-result note. Anything added later that is generated text
# rather than per-tool data belongs in this tuple, and the test below asserts as much as a
# test can about that.
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

_STATIC_PROMPT_TEXT = (
    _GROUNDING_RULES,
    _NO_TOOLS_PROMPT,
    _SCRATCH_TOOL_NOTE,
    _AGGREGATE_NOTE_TEMPLATE,
)

# A fingerprint of that text, written into every generated prompt so a stored one can be
# recognised as carrying an older copy.
#
# The routing prompt is generated once and *stored* on the agent
# (prompt_sync_service.store_tool_routing_prompt), and staleness was decided purely
# by comparing the sync time against the newest tool change. That is right for the
# per-tool half of the prompt and wrong for this half: editing a rule here changed
# nothing for any agent that already existed, because no tool had been touched, and
# the fix stayed silently un-deployed until somebody happened to re-save a tool.
#
# A hash rather than a hand-maintained version number: the thing that must not drift
# is the text itself, and a number is exactly the sort of thing an edit forgets
# to bump. Short because it is only ever compared for equality, and it is sitting in
# the prompt paying for its own characters.
_RULES_FINGERPRINT = hashlib.sha256(
    "\n".join(_STATIC_PROMPT_TEXT).encode("utf-8"),
).hexdigest()[:12]

# How that fingerprint appears in the prompt. An HTML comment so a model reading the
# prompt has nothing to act on, and one fixed prefix so `is_prompt_stale` can find it
# without knowing the current value.
_RULES_MARKER_PREFIX = "<!-- grounding-rules:"


def rules_marker() -> str:
    """The line stamping a generated prompt with the rules revision it was built from."""
    return f"{_RULES_MARKER_PREFIX}{_RULES_FINGERPRINT} -->"


def has_current_rules(prompt: Optional[str]) -> bool:
    """
    Whether ``prompt`` was generated from the grounding rules in this build.

    ``False`` for a prompt written before the marker existed, which is correct: it
    predates the rule that added the marker, so it is out of date by definition.
    """
    return bool(prompt) and rules_marker() in (prompt or "")



def build_tool_routing_prompt(agent_name: str, tools: List[dict]) -> str:
    """
    The generated routing prompt for one agent.

    ``tools`` is a list of dicts as assembled by
    :func:`app.services.deep_agents.prompt_sync_service.collect_agent_tools` —
    one per *enabled* tool config, each with ``tool_name``, ``description``,
    ``table_name``, ``config``, ``datasource_name`` and ``db_type``.

    One entry in that list may instead be an attached Graph Designer graph, marked
    ``kind: "graph"``. It is described by :func:`_describe_graph` rather than
    :func:`_describe_tool`, because almost nothing that function says about a tool is true
    of a graph: there is no single table, no one query, and it may come back with a
    question instead of an answer.
    """
    heading = (
        f"# Data tools available to {agent_name}\n"
        if agent_name
        else "# Data tools available to you\n"
    )

    if not tools:
        # Stamped too, even though this branch carries no grounding rules: staleness is
        # "generated by this build", and an agent that regains a tool has to come back
        # through the sync either way.
        return f"{heading}{_NO_TOOLS_PROMPT.strip()}\n\n{rules_marker()}"

    blocks = [
        _describe_graph(tool) if tool.get("kind") == "graph" else _describe_tool(tool)
        for tool in tools
    ]

    sections = [
        heading.strip(),
        (
            f"You have {len(tools)} data "
            f"{'tool' if len(tools) == 1 else 'tools'}. Each one runs a fixed, "
            "pre-approved query — you choose which to call, not what it queries. "
            "A tool takes no arguments unless its entry below lists parameters, and "
            "those only fill in values the operator chose to leave open."
        ),
        "\n\n".join(blocks),
    ]

    aggregate_note = _aggregate_note(tools)

    if aggregate_note:
        sections.append(aggregate_note)

    sections.append(_GROUNDING_RULES.strip())
    sections.append(_SCRATCH_TOOL_NOTE.strip())
    sections.append(rules_marker())

    return "\n\n".join(sections)


def _describe_graph(entry: dict) -> str:
    """
    One attached graph's entry in the routing prompt.

    Says the three things a model cannot work out from the name, and would otherwise get
    wrong:

    * **it runs several steps in order**, so a result is the end of a sequence rather than
      one query's rows. Without this a model treats an empty result as a broken tool.
    * **it may ask a question.** A returned question is not a failure, and the question is
      to be relayed *word for word* — the ``offer_sentence`` rule, for the same reason: a
      model rewording a question asks the user the wrong thing, and a paraphrased question
      makes the next turn's answer unmatchable.
    * **how to send the answer back**, because a question the model cannot resume is a
      conversation that cannot continue. This is the failure mode
      ``documentations/DOWNLOADER_AGENTS.md`` describes for the export offer.

    No table names and no statement, deliberately. A graph reads whatever its nodes read,
    which can be several datasources; listing them would imply the model can choose
    between them, and it cannot.
    """
    tool_name = entry.get("tool_name")
    lines = [f"## {tool_name}"]

    description = (entry.get("description") or "").strip()
    lines.append(
        f"Purpose: {description}"
        if description
        else "Purpose: not described by the operator."
    )

    node_count = int(entry.get("node_count") or 0)
    lines.append(
        f"This is a saved graph of {node_count} step(s), not a single query. Calling it "
        "runs the whole graph in the order it was drawn and reports the result of its "
        "last step."
    )

    if entry.get("asks_questions"):
        lines.append(
            "It may come back with a question instead of a result. That is not a "
            "failure. Put the question to the user **exactly as written, word for "
            "word**, add nothing to it, and do not answer it yourself. When they reply, "
            f"call `answer_{tool_name}` with the run id the question came with and what "
            "they said."
        )

    parameters = [
        str((param or {}).get("param") or "").strip()
        for param in entry.get("sql_params") or []
    ]
    named = [name for name in parameters if name]

    if named:
        lines.append(
            "Parameters: " + ", ".join(f"`{name}`" for name in named) +
            ". These are the only values you may supply."
        )
    else:
        lines.append("Takes no arguments.")

    return "\n".join(lines)


def _aggregate_note(tools: List[dict]) -> str:
    """
    The paragraph describing whole-result reading, or nothing at all.

    Emitted only when at least one source has been opted in — which is what keeps
    this feature additive: an agent with none produces the prompt it produced
    before, byte for byte, and there is nothing for a test to notice.

    **Filtering is stated before totalling, and that ordering fixed a real failure.**
    The note used to describe only totals, so an agent whose source returns every
    month's revenue, asked about August, answered:

        I'm unable to filter the data by month, so I can't tell you the revenue
        that was generated specifically in August.

    Which was a fair reading of what it had been told. The operator's own tool
    description said "if the user asks for a specific month, filter the data on
    created_at", and nothing in the prompt named a capability that could do it — so the
    instruction was unfulfillable and the model apologised instead of using the tool
    that was sitting there. A capability the model does not know about is one it says no
    for.

    It still does not repeat *what* can be calculated — the tool's own description
    carries that, and a rule naming a calculation the tool would then refuse is worse
    than no rule. What it now does say is that **the source's own instructions are
    carried out through this tool**, because that is the part neither description can
    state on its own: the operator writes "always group by department" on the graph, and
    only the prompt can join that to the tool which does the grouping.
    """
    permitted = [
        str(tool.get("tool_name"))
        for tool in tools
        if tool.get("allow_recursive_aggregate")
    ]

    if not permitted:
        return ""

    return _AGGREGATE_NOTE_TEMPLATE.format(
        names=", ".join(f"`{name}`" for name in permitted),
        aggregate_tool=AGGREGATE_TOOL,
        row_limit=PROMPT_ROW_LIMIT,
    )


def _describe_tool(tool: dict) -> str:
    """
    One tool's entry: what it is for, where it reads from, and what it returns.

    For a builder-mode tool, "what it returns" is spelled out per field rather than
    left to the SQL preview, because the field names in the result are what the
    model has to quote back — and an aliased aggregation's name is not guessable
    from the config.

    A SQL-mode tool has no config to read that from, so the statement itself is
    what the model is shown (see :func:`_sql_tool_lines`). Claiming a field list
    for it would mean parsing the SELECT list, and a wrong guess would have the
    model quoting a column name that is not in the result.
    """
    config = tool.get("config") or {}
    table_name = tool.get("table_name") or ""
    table_names = [str(name) for name in tool.get("table_names") or [] if name]
    sql_query = (tool.get("sql_query") or "").strip()

    lines = [f"## {tool.get('tool_name')}"]

    description = (tool.get("description") or "").strip()
    lines.append(
        f"Purpose: {description}"
        if description
        else "Purpose: not described by the operator — rely on the fields below."
    )

    source = tool.get("datasource_name") or "the configured datasource"
    db_type = (tool.get("db_type") or "").strip()
    where = f"{source}{f' ({db_type})' if db_type else ''}"

    nesting = _nesting_description(tool.get("chain"))

    if sql_query:
        lines.extend(_sql_tool_lines(sql_query, table_names or [table_name], where))
        lines.extend(
            _parameter_description({}, tool.get("sql_params")),
        )
        lines.extend(nesting)
        return "\n".join(lines)

    lines.append(f"Reads: {_source_description(config, table_name)} in {where}.")

    returned = _returned_fields(config)
    lines.append(
        f"Returns fields: {', '.join(returned)}."
        if returned
        else _expanded_selection_description(config, table_name)
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

    lines.extend(_parameter_description(config))

    lines.append(f"Query it runs: {build_query_preview(config, table_name)}")
    lines.extend(nesting)

    return "\n".join(lines)


def _nesting_description(chain) -> List[str]:
    """
    What this tool embeds, if anything — the same fact the operator sees on the
    list page, said to the model.

    Two things the model cannot work out on its own and will otherwise get wrong.
    That the tool is **already** restricted to another tool's result, so it should
    not call that tool separately and try to combine the two itself. And that no
    rows can mean *the inner tool matched nothing*, which is an answer worth giving
    — without this line a model reads an empty result as a failure and apologises
    for the data instead of reporting it.

    Nothing about *how* the values travel: the columns and the binding are the
    operator's business, and naming them would invite the model to reason about a
    query it cannot change.
    """
    children = list(getattr(chain, "children", None) or [])

    if not children:
        return []

    names = ", ".join(child.tool.tool_name for child in children)

    lines = [
        f"Runs {names} first and reports only the rows matching what they return. "
        "That restriction is fixed. If any of them finds nothing this tool returns "
        "no rows, which means nothing matched — say so plainly rather than treating "
        "it as an error, and do not call those tools separately to work around it.",
    ]

    iterating = next(
        (child for child in children if getattr(child, "iterates", False)), None,
    )

    if iterating is not None:
        # Said because it changes what the *result* is, not because the model can do
        # anything about it: the rows are every value's rows together, so "how many
        # did each one have" is answerable from one call rather than from several.
        # Without this a model sees a wide result and calls the tool again per value,
        # which it cannot do — the tool takes no such argument.
        lines.append(
            f"It runs once for every value {iterating.tool.tool_name} returns and "
            "reports all of those runs together, so one call already covers every "
            "one of them."
        )

    return lines


def _sql_tool_lines(sql_query: str, table_names: List[str], where: str) -> List[str]:
    """
    The entry for a tool whose query is a stored SQL statement.

    The statement is quoted in full and the model is told to read the returned
    field names off the result rather than off this description — which is the
    truthful instruction, because nothing here has parsed the SELECT list.

    ``table_names`` is every table the operator said the statement reads, which is
    why the form asks for them: nothing here parses a FROM clause, so before they
    were recorded this line named the primary table and waved at "any tables its
    query joins" — telling the model a two-table tool was a one-table tool.

    It is put on its own lines rather than inline: these statements are the ones
    the builder could not express, so they are the long ones — window functions,
    CTEs, unions — and a wrapped single line is where a model starts misreading
    which clause belongs to which query.
    """
    named = ", ".join(name for name in table_names if name) or "its configured table"

    return [
        f"Reads: {named} in {where}.",
        "Returns: whatever columns the query below selects. Use the field names "
        "exactly as they come back in the result — they are not listed here.",
        "Query it runs, exactly as written:",
        sql_query,
    ]


def _source_description(config: dict, table_name: str) -> str:
    """Which table(s) the tool reads, naming joined ones explicitly."""
    tables = query_tables(config.get("joins"), table_name)

    if not tables:
        return f"table {table_name}"

    joined = ", ".join(tables[1:])
    return f"table {tables[0]} joined to {joined}"


def _expanded_selection_description(config: dict, table_name: str) -> str:
    """
    What a config that names no columns returns, in words.

    Kept as prose rather than a field list because this prompt is generated from the
    stored config alone — nothing here reflects the database — and a list built from
    what was recorded when the datasource was configured would go stale the first
    time a column is added. What the model actually needs is the naming convention,
    since that is the part it cannot guess:
    :func:`app.services.deep_agents.query_executor._selected_columns` prefixes every
    column with its table once a query joins, so ``id`` arrives as ``orders_id``.

    "Active" is stated because the set really is narrower than the table: a column
    the user has switched off in Data Sources is not in the result, and a model told
    it would receive "every column" would go looking for it.
    """
    tables = query_tables(config.get("joins"), table_name)

    if not tables:
        return f"Returns: every active column of {table_name}."

    return (
        f"Returns: every active column of {', '.join(tables)}. Each field is named "
        f"table_column — a column 'id' of {tables[0]} arrives as '{tables[0]}_id'."
    )


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
    """
    The fixed WHERE clause in words, or None when nothing about it is fixed.

    Agent-supplied filters are excluded, and that exclusion is the point: the
    sentence this feeds says the restriction "is fixed — you cannot widen or change
    it", which is a lie about a filter the operator deliberately opened. Those are
    described separately by :func:`_parameter_description`.
    """
    clauses = [
        _fixed_clause(entry)
        for entry in config.get("filters") or []
        if entry.get("column")
        and entry.get("operator")
        and not entry.get("agent_supplied")
    ]

    return " and ".join(clauses) if clauses else None


def _fixed_clause(entry: dict) -> str:
    """
    One fixed filter in words.

    A value-less operator is said in English rather than as SQL — this sentence is
    read by a model deciding whether a tool answers the question, and "rows where
    technology has a value" is a fact it can act on where ``TRIM(technology) <> ''``
    is a detail it does not need.
    """
    column = entry.get("column")
    operator = entry.get("operator")

    phrases = {
        "IS NULL": f"{column} is empty",
        "IS NOT NULL": f"{column} is set",
        "IS BLANK": f"{column} is empty or blank",
        "IS NOT BLANK": f"{column} has a real value (not empty or blank)",
    }

    if operator in phrases:
        return phrases[operator]

    return f"{column} {operator} '{entry.get('value')}'"


def _parameter_description(
    config: dict,
    sql_params: Optional[List[dict]] = None,
) -> List[str]:
    """
    The values this tool asks the model for, one line each.

    The model already gets these as a JSON schema on the tool itself, so this is
    duplication — deliberately. The schema says a field exists and what type it is;
    these lines say which *column* it narrows and with which comparison, which is
    what decides whether the tool answers the question at all. A model choosing
    between two tools reads the prompt, not the schema.

    Two sources, and exactly one is ever populated: a builder tool opens filters, a
    SQL tool declares parameters (``sql_params``). What can be said about each
    differs and the lines say so rather than pretending otherwise — a builder filter
    names its column and operator because the config holds both; a SQL parameter has
    only what the operator wrote about it, because nothing here parses the statement
    to find out what it compares against.
    """
    parameters = [
        entry for entry in config.get("filters") or []
        if entry.get("agent_supplied") and entry.get("param")
    ]
    declared = [
        entry for entry in sql_params or []
        if (entry or {}).get("param")
    ]

    if not parameters and not declared:
        return []

    lines = ["Parameters you supply when calling this tool:"]

    for entry in parameters:
        requirement = "required" if entry.get("required", True) else "optional"
        described = str(entry.get("description") or "").strip()

        lines.append(
            f"- {entry.get('param')} ({requirement}): narrows "
            f"{entry.get('column')} {entry.get('operator')} <your value>."
            + (f" {described}" if described else "")
        )

    for entry in declared:
        requirement = "required" if entry.get("required", True) else "optional"
        described = str(entry.get("description") or "").strip()

        lines.append(
            f"- {entry.get('param')} ({requirement}, {entry.get('type') or 'text'})."
            + (f" {described}" if described else "")
        )

    lines.append(
        "Pass a value only when the user gave you one. Do not invent or guess one, "
        "and do not use these to widen a result you were refused."
    )

    return lines


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
