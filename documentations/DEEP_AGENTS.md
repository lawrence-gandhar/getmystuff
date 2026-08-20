# Deep Agents

A data agent's tool configs, made runnable. A chatbot attached to a data agent
answers data questions by calling that agent's tools; the language model sees the
rows a tool returns and nothing else.

Module: `app/services/deep_agents/`, `app/routes/deep_agents/`.
Built on LangChain's [`deepagents`](https://pypi.org/project/deepagents/) (0.7.x) over
LangGraph.

---

## The guarantee

**The model never reads the database.** It has no connection, no schema dump and no
table sample. Its only access to data is calling one of the agent's tools, and each
tool runs a query that was written and saved by the operator.

This is a change of kind, not degree, from the path it replaces. Today a chatbot
answers a data question like this:

```
fetch_rdbms_rows(...)  ->  SELECT * FROM <table> LIMIT 500
                       ->  pandas profile of those 500 rows
                       ->  profile pasted into the prompt
```

The model is handed a sample of real rows and asked to reason over it. With a data
agent attached, that step does not happen at all:

```
question -> Deep Agent (tool descriptions only, no data)
         -> model picks a tool
         -> query_executor runs the saved query
         -> rows go back to the model
         -> answer
```

No tool call means no data. That is visible rather than implied: every answer on the
test console lists the tools that were called, and an answer containing figures with
an empty tool list is a bug you can see.

### Why the query is safe by construction

A tool config holds its query one of two ways — the query builder's structured config,
or one read-only SQL statement the operator wrote or approved. The full account is in
[TOOL_QUERY_MODES.md](TOOL_QUERY_MODES.md); what matters here is that **neither mode
puts model output into a query**, and both are re-validated on every run.

**Builder mode.** `app/services/deep_agents/query_executor.py` never emits SQL text.
It reflects the real tables (`Table(autoload_with=...)`, the pattern already in
`db_utils._reflect_one`) and assembles a SQLAlchemy Core `Select` from actual
`Column` objects. Three properties follow:

| Property | Why it holds |
|---|---|
| Identifiers cannot be read as syntax | They are `Column`/`Table` objects, quoted by the dialect |
| Filter values cannot be SQL | They become **bound parameters** — `col == value`, `col.like(value)` |
| A missing column fails readably | Reflection resolves names before the driver sees anything |
| A switched-off column is never read | The active set is derived from the reflection on **this run**, not from what was true when the config was saved |

Note what is *not* reused: `tool_config_service.build_query_preview()` and
`query_joins.build_join_sql()` render a config as SQL text and inline filter values
with f-strings. They are display-only and always were — executing them would make
every stored filter value an injection vector. The executor mirrors them clause for
clause but shares no code with them, so the preview an operator reads and the query
that runs describe the same thing without the preview becoming a code path.

Verified: a stored filter value of `x' OR 1=1 --` comes back as zero rows, and
`%'; DROP TABLE customers; --` through a `LIKE` filter leaves the table intact.

**What an empty selection means.** A config that names no columns selects **every active
column of every table the query reads, joined tables included** — spelled out, never a
literal `*`. Two consequences worth knowing:

* A joined tool now returns its joined tables' data. Previously it returned only the
  base table's columns, so a tool built to join customers to orders answered every
  question about the customer with nothing but order rows.
* With a join in play every field is named `table_column` (`orders_id`, `customers_id`).
  The rows go back as a dict, so two tables both having an `id` would otherwise collapse
  into one key and the agent would be handed a row that quietly lost a column. Unjoined
  queries keep bare names. `prompt_builder` states the convention in the routing prompt,
  because the field names are what the model has to quote back.

**A reference to a switched-off column fails the tool.** Loudly, with a message the agent
relays — it is not dropped from the query. A dropped filter widens the result set and a
dropped group-by changes what each row counts; either way the query still returns a number
the model states as fact. A tool that says it needs reconfiguring is recoverable; a
plausible wrong figure is not. That covers selected columns, aggregations, filters,
group-by and join keys alike, because they all resolve through one function
(`_resolve_column` → `_table_column`). The status rules themselves are in
[SERVICE_PATTERNS.md](SERVICE_PATTERNS.md#who-reads-the-status--apputilsdatasource_statuspy).

**SQL mode.** The stored statement runs as written — running an approximation of a
query the operator approved would defeat the point of the mode. The safety comes from
elsewhere, and it is the same place: nothing the model produces is in the statement.
It was written and saved in advance, the tool takes no arguments, and it is re-checked
against `app/utils/sql_guard.py` on every run — one statement, a read, bounded length.
The 200-row cap is applied by *streaming* rather than by wrapping the SQL, so the
operator's query is never rewritten. Details, including why the wrap would be wrong,
in [TOOL_QUERY_MODES.md](TOOL_QUERY_MODES.md).

SQL mode gets the **table** half of the active rule and not the column half. Every
table the tool records — `table_name` plus `extra_tables`, the Tables multi-select on
the form — is checked before the statement runs; the columns cannot be, without
rewriting the statement. Choosing SQL mode means the statement is the permission at
column level.

That table list is also what the routing prompt names. Before it was recorded, a
SQL-mode entry read "the primary table, and any tables its query joins" — an agent
told a two-table tool was a one-table tool, because nothing here parses a FROM clause.

### Other bounds

* **No tool query is capped.** Every matching row comes back, in either mode. There was a
  flat 200-row ceiling and it was removed: that number stood for both "what a model can be
  handed" and "how much data exists", and a tool answering about 5,275 records returned 200
  with nothing saying which of the two it was. The operator's own `LIMIT` is the statement
  about how much data a question is about.
* **What is bounded is the prompt, not the query.** `PROMPT_ROW_LIMIT = 200` is how many rows
  `describe_result()` serialises, because a context window is a fixed size and the
  alternative to shortening is a turn that fails outright. Because every row was read, the
  header states the exact total beside them — `200 row(s) out of 5275 matching record(s)` —
  where the old text could only warn that a total was unknowable.
* **A second, lower budget applies to what the model may *print*:**
  `DISPLAY_ROW_LIMIT = 100`. The two are for different things — 200 is what the model may
  reason over, 100 is what may go into a chat bubble — and the display budget is enforced
  by grounding rule 8 rather than by truncation, because cutting at 100 would take the
  other rows away from the model as well. Past 100 the agent offers the whole set as a
  file; see [DOWNLOADER_AGENTS.md](DOWNLOADER_AGENTS.md).
* The stored query is **re-validated at execution time** — through
  `tool_config_service.validated_query_config()` in builder mode and
  `validated_tool_sql()` in SQL mode — not trusted from the row. A row edited directly
  in psql gets the same treatment as one from the form, and a row that no longer
  passes becomes a `ToolQueryError` the agent can relay, never a 500.
* Relational datasources only (`query_joins.RDBMS_DB_TYPES`). A tool config pointed
  at Mongo or a file is refused with a message the agent relays.
* **`RIGHT JOIN` is refused in builder mode, not approximated.** SQLAlchemy has no
  right-join flag, and a right join is only expressible by swapping the operands —
  which this accumulating builder cannot do once the base table is fixed. Substituting
  a left or full outer join would quietly change which rows come back, in the direction
  of a plausible wrong figure. Given the point of the feature, an explicit failure the
  operator can fix is the only honest option. Right joins stay authorable and
  previewable; they are just not runnable *that way* — a SQL-mode tool may right-join
  freely, because its statement is not reassembled.

`tool_factory.find_unsupported_tools()` reports both cases with their reason, and the
console shows them before a visitor can hit one.

### Nested tools are still one tool

A tool config may embed others: the inner tools run first and their values restrict the
outer query, as one call with one name. The model neither supplies nor sees any of that —
the chain is fixed by the operator exactly as a filter is, and what comes back is the
outermost query's rows.

Two things follow that matter here. **An agent given a nested tool is given the whole
chain**: `collect_agent_tools` returns the agent's own tools plus every transitive child, so
the children are described in the routing prompt and callable in their own right, without
their rows being moved to this agent. And **nothing caps a chain either** — every value crosses
every edge and the root returns every row it matched, exactly as an unnested tool does. The one
refusal left is `MAX_CHAIN_ITERATIONS`, which bounds how many times an iterating link may
*re-run* the parent: round trips inside one chat turn, not how much data exists.

See [TOOL_CHAINING.md](TOOL_CHAINING.md) for the graph, the limits and the refusals.

### Two audiences for the same failure

`ToolQueryError` carries the fault as its message and the instruction separately, in
`advice`. `tool_factory` renders `exc.for_agent` — the fault *plus* "tell the user the
tool needs reconfiguring" — because a model handed a bare fault tries to work around
it. The **Test Query** button renders `str(exc)`, without the advice: the operator
reading it is the user someone would be told to tell.

The same split governs driver errors. `execute_tool_query` never lets one reach a
prompt (it can name schema objects and echo values) and substitutes "the query could
not be run against the database". `probe_tool_query`, the entry point the test button
uses, lets it through untouched so the operator sees which column the database could
not find — see [QUERY_TEST.md](QUERY_TEST.md).

### A result is described as what it is, not as what was asked for

Rule 13 forbids the one wrong answer that is built entirely out of true rows.

Asked for *"the list of projects in a department"*, an agent whose projects tool
filters on nothing called it anyway and headed the result **"Projects in the
department"**. Every row was real, the query ran correctly, and the reply was false:
the reader was told they were looking at one department when they were looking at all
of them, and nothing in the answer gave them any way to notice. A wrong number at
least looks like a number somebody could check.

Rules 11 and 12 already governed what the model may *pass* to a tool — it may not
invent a parameter, and it may not ask the visitor to narrow when no tool takes a
narrowing. Neither governed what it may *claim* about the rows that came back, and the
heading above a table is a claim. Rule 13 holds it to rules 1 and 2 exactly as a figure
is held to them: describe the result you have, name the narrowing you could not apply,
then show the rows. In one reply — it does not reopen the door rule 11 closed.

### The answer is Markdown, and the widget renders it

Rule 15 tells the model to put rows in a Markdown table. That is new, and it is new
because for a long time the interface told it the opposite: the widget escaped every
reply, so a table arrived as a wall of `|` characters and prose was the only thing that
read correctly. The widget now renders Markdown — escaping the model's text before
parsing it, so every tag in the output is one the renderer wrote — see
[WIDGET_RENDERING.md](WIDGET_RENDERING.md).

Two older rules constrain it and both still apply. Rule 8's display-row limit is
restated inside rule 15, because a formatting rule that invites a table without
restating the cap is an invitation to paste two hundred rows into a chat bubble. Rule
10's ban on URLs extends to Markdown links, which the renderer deliberately does not
support: `[text](javascript:…)` is how Markdown becomes script execution.

### A busy provider is not a broken agent

A 429 is told apart from every other turn failure, and it earns the separation. The
catch-all says *"please try again, or check the agent's AI key in AI Settings"*, which
is right for a wrong key and a dead endpoint — and wrong for a provider having a busy
minute, where nothing is misconfigured and the advice sends someone hunting a fault
that does not exist. `_RATE_LIMIT_ERRORS` catches `anthropic.RateLimitError` and
`openai.RateLimitError` first and answers **503** with `_BUSY_MESSAGE`, which names the
cause and explicitly says nothing needs changing.

The visitor never sees either sentence. `chatbot_reply_service` degrades to
`_NO_FALLBACK_REPLY` — *"I can't reach that data at the moment, so I'd rather not
guess"* — which is true regardless of the cause and names no system they can see.

**The retry lives on the model client, not around the graph.** `model_factory`
sets `max_retries=MAX_RETRIES` (4) on both chat models, raising it from the SDKs'
default of 2. That default is sized for a provider that rate-limits *per key*: two fast
retries and you are past your own burst. It is not enough for a gateway that queues
under load and answers `queue_exceeded` — Cerebras and the other OpenAI-compatible
hosts do this, and the queue drains in seconds.

The layer matters more than the number. A Deep Agent turn is a loop — call a tool, read
the rows, answer — so retrying `deep_agent.ainvoke` would re-execute every tool call
that had already succeeded, running the user's SQL again for a failure that happened
after it. Retrying one HTTP call retries one HTTP call. `test_rate_limits.py` asserts
this by reading the source, because the tempting wrong version passes every behavioural
test. The turn timeout is unchanged and still the outer bound: a turn that spends its
whole budget queueing ends with "took too long", not by hanging.

This mirrors `ai_analytics_service._with_rate_limit_retry`, added for the same provider
behaviour on the single-shot path.

---

## Tools take no arguments — unless an operator opens one filter's value

A tool config declares its whole query: the columns, aggregations, grouping and
filters in builder mode, or the statement in SQL mode. **By default the tool built
from it exposes an empty argument schema**, so the model's only decision is *which*
tool to call. That default matters and is unchanged.

Two reasons, and the first is the important one:

1. An argument would put model-generated text into the query. That is the single
   thing this feature exists to prevent, and no amount of validation on the argument
   makes it as safe as not having one.
2. It would let the model widen a filter the operator narrowed deliberately — a tool
   scoped to `status = 'paid'` is a decision, not a default.

### Parameterised filters

The cost of that default was real: one tool config per question shape. An agent could
not answer "projects for August" unless a tool already filtered to August, so a
visitor rephrasing the same question got the same answer every time — and a model
handed a tool failure would improvise the remedy it could not have, promising to
filter by a date range nothing could accept.

An operator can now tick **Agent fills in** on a single filter. That filter stores no
value; instead it names a parameter, and the tool grows exactly one string field for
it (`tool_factory._arguments_schema`). One `fetch_projects` then answers August,
September and any other month.

**Both reasons above still hold, and that is the design.** What the model supplies is
the right-hand side of one comparison the operator chose to open — a *value*, and
nothing else:

* the **column** comes from the stored reference and is resolved against the reflected
  schema by `_resolve_column`, exactly as a fixed filter's is;
* the **operator** comes from the stored config;
* the **value** is coerced to the column's own Python type by `_coerced_value` and
  **bound as a parameter** — the same line that has always made a stored filter value
  data rather than SQL. `test_query_executor.py` asserts that `0 OR 1=1 --` passed as
  a value matches nothing, and that a column reference passed as a value compares
  against the string rather than switching which column is filtered;
* every **other filter still applies**. Opening one cannot relax another, which is
  reason 2 kept intact rather than traded away.

So the model's whole influence is one value in one comparison. It cannot choose a
column, change `>` to `<`, reach a table, or widen anything the operator left fixed.

**A missing required value refuses the query.** Dropping the clause would return every
row and look like a working answer, so `_filter_conditions` raises with the parameter
named and tells the model to call again with a real value rather than invent one.
Required is the default; an operator can untick it, and an omitted optional value
drops that one clause and leaves every other filter standing.

**The prompt describes the parameters twice, deliberately.** The JSON schema on the
tool says a field exists and what type it is; `_parameter_description` says which
column it narrows and with which comparison. A model choosing between two tools reads
the prompt, not the schema. Grounding rules 11 and 12 draw the line: a tool with no
parameters cannot be narrowed by rephrasing and the model must not offer to, while a
tool that declares one must be passed only values the user actually gave.

Every field is declared a **string** and typed at the database. A schema typed from
the reflected column would need a reflection at prompt-build time for a tool that may
never be called, and would still have to be re-checked at execution because the column
can change under a saved config. One answer to "what type is this", not two that can
disagree.

### The same thing for a written statement

Builder mode opens a *filter*, which has a column and an operator the operator chose.
A SQL statement has neither, because nothing in this application parses one — so the
values are **declared beside the statement** (`tool_configs.sql_params`) and the
operator writes the `:name` and the comparison themselves.

Everything above still holds, arrived at differently. The model supplies a value; it
is bound as a parameter; the comparison around it is the operator's own text,
re-validated on every run. What it cannot do is choose a name the operator did not
declare — `_declared_bindparams` iterates the *declarations*, so an invented argument
has nowhere to land.

The one difference is typing. There is no reflected column to coerce against, so the
declaration carries a `type` (`text` | `number` | `boolean`) and the operator says what
the value holds. A value that will not convert falls back to the string rather than
raising: `"abc"` for a number is a value that matches nothing, which is the right
answer to what was asked.

A tool that *requires* a value cannot be embedded as a child, because an inner tool is
never called by the model — refused when the link is saved, where the message can name
the parameter. See [TOOL_CHAIN_ITERATION.md](TOOL_CHAIN_ITERATION.md).

The remaining cost is unchanged: a question no tool covers, with no parameter for it,
is still refused. That is what the routing prompt's "say when no tool covers the
question" rule is for.

---

## The generated routing prompt

An agent has **two** prompt columns on `data_agents`, and they never mix in storage:

| Column | Written by | Contents |
|---|---|---|
| `system_prompt` | the operator, via the agent form | Their standing instructions, tone, refusals |
| `tool_routing_prompt` | `prompt_sync_service` | Generated description of the agent's tools |
| `tool_prompt_synced_at` | `prompt_sync_service` | Staleness marker |

Separate columns because a single one would have two writers racing: an operator with
the edit form open would clobber a regenerated block, or the job would overwrite words
the operator wrote. The runtime prompt is composed at answer time by
`prompt_builder.compose_runtime_prompt()` — operator text first, generated block
second, so the grounding rules are the most recent instruction the model reads. Same
reasoning as `ai_analytics_service._GROUNDING_ADDENDUM`.

### Composed in Python, not by an LLM

`prompt_builder.build_tool_routing_prompt()` is a pure function over the agent's
enabled tool configs. No model call. Four reasons:

* **It cannot describe a tool the agent does not have** — the list *is* the tool list.
* **It is reproducible** — an unchanged configuration regenerates a byte-identical
  prompt, so behaviour does not drift between two saves.
* **It is free**, so it can be regenerated on every tool change.
* **Nothing leaves the box** to produce it.

Per tool it states the purpose, the datasource and table(s), the exact field names in
the result (including how an unaliased aggregation is labelled, so what the prompt
promises matches what arrives), the grouping, and any fixed filter with a note that it
cannot be widened. Then the standing rules: answer only from tool output, never
estimate, one tool per question, say so when nothing covers it, no rows means no
matching data, a capped result is not a total, never print more than 100 rows,
repeat a download offer word for word, never write a link or a URL, describe the rows
you actually got rather than the ones that were asked for, and put rows in a Markdown
table.

Rules 8 and 9 are the display budget and the offer; rule 10 is the link ban. The
interface draws a real download button of its own, and the answer renders as plain text —
so a model writing markdown produces a visible `[Download CSV](/public/downloads/…)`
rather than a link, which is exactly what it did before the rule existed. The download
tools stopped handing it a URL at the same time, because a rule the tool output
contradicts is a rule that loses. See
[DOWNLOADER_AGENTS.md](DOWNLOADER_AGENTS.md).

Rules 8 and 9 name two tools by their constants
(`CONFIRM_DOWNLOAD_TOOL`, `DOWNLOAD_STATUS_TOOL`) rather than by literal strings — a rule
naming a tool the agent does not have is worse than no rule, and
`app/services/downloader_agents/base/download_tools.py` builds them from the same
constants so the pair cannot drift.

An agent with **no** enabled tools gets an explicit "you have no data tools" prompt so
it refuses rather than answering from the model's own knowledge — which would look
like a working answer and be entirely invented.

### Sync is an optimisation, not a dependency

`sync_tool_routing_prompt()` runs as a Litestar `BackgroundTask` from every Tool
Configs mutation — create, update, set-enabled, delete — *after* the response is sent,
in its own `AsyncSessionLocal` session (the request's session is closed by then).
It swallows every exception: there is nothing left to report to.

That is safe because `deep_agent_service` calls `is_prompt_stale()` on every answer and
**regenerates inline** if it is behind. So a failed task, a restart mid-flight, or a
task that never ran costs one extra write on the next answer and is never wrong. This
is why the feature needs no queue table, no scheduler and no retry logic — the first
background work in this codebase, and it stays that simple only because correctness
does not rest on it.

Moving a tool between agents syncs **both**: the tool joins one agent and leaves
another, and the one it left is still describing it.
`update_tool_config()` returns both ids for exactly that reason.

#### Staleness has two sources, and a timestamp only sees one

A stored prompt is half the agent's tools and half `prompt_builder`'s standing rules,
and the two go out of date for unrelated reasons.

The tool half is a timestamp comparison: `tool_prompt_synced_at` against the newest
tool config's `updated_at`. That works because saving a tool writes a row.

The rules half writes nothing. Editing a grounding rule in `prompt_builder.py` changes
no database row, so every agent that already existed kept answering from its stored
copy of the *old* rules — indefinitely, and invisibly, until somebody happened to
re-save one of its tools for an unrelated reason. A rule corrected in response to a
real misbehaviour simply did not take effect, and nothing said so. Because
`sync_tool_routing_prompt` is only ever triggered by a Tool Configs mutation, a deploy
was not a trigger either.

So every generated prompt now ends with a marker:

```
<!-- grounding-rules:a1b2c3d4e5f6 -->
```

— a truncated SHA-256 of the filled-in rules text (`prompt_builder.rules_marker()`).
`is_prompt_stale()` returns True when the stored prompt does not carry the current
one, so the next answer rebuilds it and the following ones do not. A prompt written
before the marker existed has no marker and is stale by definition, which is the
correct answer for exactly the prompts this was written to fix.

**It hashed the rules and nothing else, and that was one scope too narrow.** The bug
being prevented is "a fix to generated prompt text stays silently un-deployed", and the
rules are only one of four static blocks the builder owns. Rewriting the whole-result
note — which is what fixed the agent that refused to filter by month — changed what
*new* prompts said and left every stored prompt describing the old capability, with no
staleness anywhere to notice. The same failure, through a different door.

So the fingerprint now covers `_STATIC_PROMPT_TEXT`: the rules, the no-tools prompt, the
scratch-tool note and the whole-result note. Anything added later that is *generated text*
rather than per-tool data belongs in that tuple, and
`test_prompt_aggregate_note.TestTheFingerprintCoversEveryStaticBlock` asserts the property
per block — perturb any one of them and the marker must change — so a block that is listed
but does not affect the hash fails rather than passing a spot check.

A hash rather than a version constant on purpose: the thing that must not drift is the
rules text, and a number somebody has to remember to bump is the thing that gets
forgotten in the same commit that edits a rule. It is an HTML comment so a model
reading its own prompt has nothing to act on, and truncated because it is only ever
compared for equality.

### Telling the model a capability exists

A capability the model does not know about is one it apologises for. Asked "what was the
revenue generated in august", an agent whose one source returned every month's revenue
answered:

> I'm unable to filter the data by month, so I can't tell you the revenue that was
> generated specifically in August.

Nothing was broken, and the operator's own tool description even said *"the user asks for
specific month, then filter the data on the created_at"*. Two things in the prompt made
that instruction unfulfillable:

* the whole-result note described `aggregate_records` as being for **totals** and never
  mentioned narrowing, so nothing named a capability that could filter;
* **rules 11 and 13 say the opposite.** Rule 11: a tool applies a fixed query, so never
  offer to narrow and never say you could answer "if" the user did. Rule 13: describe the
  rows you actually got, never the rows that were asked for. Both are right for a direct
  call — they are what stops a model claiming a result was filtered when it was not, which
  is the worst failure this application has — and both are wrong for a source that *can* be
  narrowed.

So the note now leads with filtering and **states its own precedence**: "This overrides
rules 11 and 13 for these sources, and only for these." Scoped, because unscoped it would
licence exactly the false answer those rules exist to prevent, for every other tool the
agent has. And it stays inside the *conditional* note rather than being folded into the
numbered rules, so an agent with nothing opted in never reads a reference to a tool it does
not have.

One more thing the note now says, because neither description can say it alone: **a
source's own description may prescribe how to read it** — "always group by department", "a
month is filtered on `created_at`" — and those instructions are carried out by passing them
through in `instruction`. The operator writes that on the graph; only the prompt knows which
tool does grouping. With it, the real agent produced:

```json
{"group_by": ["crm_id","department"],
 "aggregations": [{"type":"sum","column":"total_amount"}],
 "filters": [{"column":"created_at","part":"month","operator":"==","value":"8"}]}
```

— every clause of the operator's description, applied.

### Not every model call in a turn is the agent talking

`astream_events` reports `on_chat_model_stream` for **every** chat-model call inside a
turn. There used to be exactly one. `aggregate_records` added a second — the planner
turning an instruction into a plan — and it runs *inside a tool*, so its tokens went onto
the answer stream and the visitor saw the plan's raw JSON printed above the answer:

```
{"group_by":["crm_id","department"],...}**Total revenue in August:** **4,100,165.90**
```

The planner tags its calls with `prompt_builder.INTERNAL_CALL_TAG` and the streamer drops
tagged tokens. A tag rather than a second model instance, because it is the same model with
the same key and the same rate limit — the only thing that differs is whether a human is
meant to read the output. `on_chat_model_end` is deliberately **not** filtered: the call is
real spend and should still be accounted for. Real cost, unreal prose.

Any future nested LLM call inside a tool has the same problem and the same one-line fix.

---

## Attaching an agent to a chatbot

Picked as a **Workspace → Data Agent** cascade, in two places, both served by the same
fragment (`/deep-agents/agent-options` → `templates/deep_agents/partials/agent_options.htm`)
so they cannot offer different sets:

* the create form on Chatbot Settings, **above** the datasource picker — it decides
  whether that picker is asked for at all;
* a form on the chatbot's **AI & Prompt** tab, so it can be changed later.

Editable after creation, unlike the datasource target: swapping which agent answers is
a normal operational change, whereas repointing a published widget at different data
is not.

### Two kinds of chatbot, one column apart

`chatbot_api_keys.target_type` says which:

| `target_type` | `datasource_id` | What the widget may read | If the agent can't run |
|---|---|---|---|
| `datasource` / `table` / `collection` / `file` | set | that datasource target | falls back to a profile answer |
| `agent` | **NULL** | its agent's tool configs | says it can't reach the data |

The `agent` row is the newer one, and it exists because an attached agent already
carries its datasources — one per tool config. Requiring a datasource *as well* asked
the operator the same question twice, and for an agent reading three of them the second
answer could only be arbitrary. Worse, the two answers could disagree: which one applied
would depend on whether the agent happened to run that turn.

So the two are exclusive by construction, enforced in three places that all have to
agree: `ChatbotCreateRequest.check_target` (an `agent` target needs an agent and must
*not* carry a datasource — a submission with both is rejected, not silently trimmed),
`chatbot_service.create_chatbot_key`, and the form, which hides the datasource block
and de-requires the field the moment an agent is picked.

**An agent-backed widget's agent cannot be detached.** `set_chatbot_data_agent` refuses
it, and the picker does not render the "No data agent" option (`agent_required`, which
rides through the cascade URL so it survives a workspace change). Clearing it would
leave a published key answering nothing, with no way back — the datasource target is
immutable after creation. Swapping one agent for another is still allowed, which is the
operation that case actually needs.

Stored on `chatbot_api_keys` as `data_agent_id` plus the `workspace_id` it was picked
through (remembered only so the picker re-opens on the right branch; it is not used at
answer time, and is deliberately not required to match the agent's own workspace — an
agent may have none, or be moved later, and must not silently detach itself from every
chatbot using it).

Both FKs are `ON DELETE SET NULL`. Deleting a workspace or an agent degrades a live
widget to the default behaviour rather than breaking it mid-conversation.

### Turn routing

In `chatbot_reply_service.generate_reply`, the non-flow branch:

```
data_agent_id set?  -> deep_agent_service.answer_for_chatbot(...)
                    -> on HTTPException: log, fall back to the profile answer
otherwise           -> chatbot_service.answer_message(...)   # unchanged
```

**No agent attached means nothing changes.** That is the back-compat guarantee: every
existing chatbot has `data_agent_id IS NULL` and takes the identical path it took
before this feature existed.

The fallback matters. A visitor is mid-conversation, and a misconfigured agent — no
enabled tools, a key with no model name, a disabled agent — must not become an error
bubble in a published widget. It cannot leak anything the agent was gating: the profile
path is scoped to the chatbot's own datasource target, chosen by the operator at
creation and unchanged by attaching an agent.

**An `agent` widget has no such target, so there is nothing to fall back to.** It
answers with `_NO_FALLBACK_REPLY` instead — "I can't reach that data at the moment, so
I'd rather not guess" — and the agent's actual reason goes to the log for the operator.
That is a worse visitor experience than a profile answer and a better one than a wrong
answer or an error bubble, and it is the trade the operator accepted by not nominating a
datasource. `chatbot_service.answer_message` guards on `datasource_id IS NULL` before it
looks anything up, so the case cannot reach a query filtering on `id = None` and report
"your data source is no longer available" — it never had one, which is a different
thing.

The Deep Agent's prose answer maps onto `AnalyticsResult.summary` alone. `insights` stays
empty and `table` stays `None` rather than being manufactured by splitting the text —
that would be putting words in the model's mouth.

### Two deliberate gaps

* **Webhook actions do not run on the agent-backed path.** The action router is a second
  model call that picks a webhook, and a Deep Agent already decides which tool to call;
  running both means two independent routers disagreeing about one turn.
* **Flow Builder's AI Fallback node** keeps its current behaviour. A flow still gets
  first refusal on every turn, and hands off via `AI_HANDOFF` as before.

---

## Providers and tool-calling

`model_factory.build_chat_model()` builds a LangChain chat model from the *same*
provider decision every other AI feature uses — `ai_analytics_service.resolve_provider()`,
promoted from private for this. Precedence is therefore unchanged: a pinned key, then
the user's active keys in provider-priority order, then `ANTHROPIC_API_KEY`.

| Resolved provider | Model | Notes |
|---|---|---|
| `anthropic` | `ChatAnthropic(ANTHROPIC_MODEL)` | Same Claude model as everywhere else |
| `openai` / `other` | `ChatOpenAI(model_name, base_url)` | `model_name` required — 503 with a fixable message if unset |
| in-built (Ollama) | `ChatOllama` | **Refused for small models** — see below |

Temperature is 0 so a question cannot route to a different tool on a retry.

The local path gets two floors, both because Ollama truncates rather than erroring:
`num_ctx` at 8192 (an over-long prompt is silently cut, and a truncated tool *result*
is a wrong answer) and `num_predict` at 1024 (a truncated tool *call* arrives as
malformed JSON, which the graph sees as a broken call rather than a cut-off answer).
The `.env` values for both are sized for short single-shot replies.

### `OLLAMA_DEEP_AGENT_MODEL`

The in-built model for data agents **only**, overriding `OLLAMA_CHAT_MODEL` in this one
place. Tool calling and single-shot answering want different models:

| | Model | Why |
|---|---|---|
| Data agents | `OLLAMA_DEEP_AGENT_MODEL` (e.g. `qwen3:8b`) | Has to hold a tool-calling loop |
| Chatbot replies, AI Fallback, KB extraction | `OLLAMA_CHAT_MODEL` (e.g. `qwen3:1.7b`) | One structured-output call; a small model is fine and ~3x faster on CPU |

Promoting the whole app to an 8B model to enable one feature would be a bad trade on a
CPU-only host. Unset, the override falls back to `OLLAMA_CHAT_MODEL`, so an existing
deployment behaves exactly as before.

The deep-agent model is deliberately **not** preloaded at startup — `keep_alive=-1`
would pin ~5 GB resident for a feature that may go unused, so the first data-agent turn
pays the model load instead.

#### Measured: usable from the console, not from a live widget

On a 6-core CPU-only host (i5-10400F, no GPU), `qwen3:8b`:

| | Measured |
|---|---|
| Generation rate | ~2.5 tok/s |
| One tool-calling round trip, 133-token prompt | 67–81 s |
| Full two-call turn over the real routing prompt | **242 s warm, 417 s cold** |

It routes correctly and reports the right figures — verified end-to-end, including
relaying a tool's fixed filter unprompted. It is simply minutes per turn. A hosted
provider does the same turn in seconds.

So the timeout is chosen by **who is waiting**, not by which provider answers:

| Caller | Budget | Env override |
|---|---|---|
| Chatbot turn (a visitor is waiting) | 120 s | `DEEP_AGENT_TIMEOUT_SECONDS` |
| Test console (an operator ran it deliberately) | 900 s | `DEEP_AGENT_CONSOLE_TIMEOUT_SECONDS` |

The visitor budget is deliberately **not** widened for the in-built model. An agent too
slow to answer within it degrades to the data-profile reply, which serves a visitor
better than a spinner — so on this class of hardware, in-built data agents are a console
feature. Use an API key for live widgets.

**Small in-built models are refused, not attempted.** A Deep Agent depends entirely on
the model choosing to emit a tool call, and `qwen3:1.7b` does that unreliably. The
failure mode is not an error — it is a confident answer with no tool call behind it,
which is precisely what this feature prevents. So
`_MODELS_WITHOUT_RELIABLE_TOOL_CALLING` refuses with a message naming the fix. It is a
denylist, not an allowlist: an operator who has pulled a model we have not heard of
should be able to try it.

Why not native tool-calling in `ai_analytics_service` instead? It has three provider
implementations and forces structured JSON output on all of them, which collides with
tool-calling. LangChain is imported **only** inside `app/services/deep_agents/`;
nothing else in the app changed provider library.

---

## deepagents' built-in tools

`create_deep_agent` binds eight tools of its own alongside ours. Verified against what
0.7.1 actually binds, not its documentation:

```
ls  read_file  write_file  edit_file  delete  glob  grep  task
```

None is a data path:

* the default backend is `StateBackend` — that filesystem lives in the conversation's
  own state, in memory, empty at the start of every turn, never the host's disk;
* the `execute` shell tool is **not bound at all** without a sandbox backend, which
  this module does not supply.

They cannot be removed: `FilesystemMiddleware` and `SubAgentMiddleware` are required
scaffolding in 0.7.x, and `excluded_middleware` raises `ValueError` rather than
dropping them. So the routing prompt tells the model explicitly that they are private
scratch space, start empty, and must never be used in place of a data tool — without
that, a model will read an empty file and report "no data" instead of calling a tool.

Bounds on a run: `recursion_limit=25` (roughly a dozen tool calls) and an
`asyncio.wait_for` timeout set by the caller — see
[the measured table above](#measured-usable-from-the-console-not-from-a-live-widget).

---

## Running it

`deepagents` requires **Python ≥ 3.11** (it imports `typing.Required`); this project's
local virtualenv is 3.10, which is why the app is containerised:

```bash
docker compose up --build       # app on :8003, postgres on :5433, ollama on :11435
```

No separate migration step — `on_startup` runs `alembic upgrade head` itself
(see [MIGRATIONS.md](MIGRATIONS.md)).

The image is `python:3.12-slim`. Postgres is `pgvector/pgvector:pg16` because the
in-built LLM's `knowledge_chunks` table needs the `vector` extension, created on first
boot by `docker/postgres-init.sql`.

The Ollama container has **its own model volume**, separate from any Ollama installed
on the host. The two models the app preloads are pulled into it automatically by the
`ollama-init` one-shot service, which `app` waits on, so there is no manual step and no
preload failure on a fresh volume.

The Deep Agents model is the exception — 5.2 GB for an optional feature, so it is left
opt-in:

```bash
docker compose exec ollama ollama pull qwen3:8b          # OLLAMA_DEEP_AGENT_MODEL
```

It is only needed to run data agents on the in-built model rather than an API key; see
[`OLLAMA_DEEP_AGENT_MODEL`](#ollama_deep_agent_model).

To reuse a host Ollama that already has them instead, see the commented alternative in
`docker-compose.yml`. It needs the host's Ollama started with `OLLAMA_HOST=0.0.0.0` —
it binds `127.0.0.1` by default and is otherwise unreachable from a container — which
also exposes it beyond localhost. That is why the self-contained container is the
default.

### Test console

`/deep-agents/{agent_uuid}/console`, reachable from the **Test** button on each row of
the Data Agents list (disabled until the agent has a tool). Shows the agent's tools,
anything that would stop it running, and — on every answer — which tools were called.
It exists so an agent can be verified before a visitor talks to it, and so the
no-data-to-the-model claim is checkable rather than trusted.

#### Two ways to ask, and only ever one of them

The form declares `hx-post` to `/ask` **and** `data-stream-url` to `/ask-stream`.
`static/js/deep_agent_stream.js` opens the stream; the POST is the fallback, and it is
what still runs if that script fails to load, if the browser has no `EventSource`, or if
the stream dies before it delivers anything. The widget's turn
(`chatbot_service.py`, `streamSend`) is built the same way.

Both must never run for one submit — that is two complete agent turns for one question:
two sets of model calls, two sets of queries, and two download offers for the same
result set. Calling `preventDefault()` does not achieve that, because htmx's listener is
on the form itself and `preventDefault` does not stop other listeners. Nor does removing
`hx-post`: htmx captures the verb and the path in a closure when it *processes* the
node, so the attribute is only ever read at page load. The script therefore listens for
`submit` on `document` in the **capture** phase and calls `stopPropagation()`, so the
event never reaches htmx's own handler, and issues the fallback itself through
`htmx.ajax()` with the same target and swap the form declares.

Three properties of `EventSource` shape the client, and each was observed in a browser
against this server rather than assumed:

* **It reconnects by itself.** A stream that ends — *including one that ended perfectly,
  having sent `done`* — makes the browser open it again, which re-runs the whole agent
  turn. Left alone it does this indefinitely. Only `close()` stops it, which is why the
  `done` handler closes before anything that could throw.
* **Every close arrives as an `error` event carrying no data, success included.** A
  disconnect therefore says nothing on its own about whether the turn worked; a
  `finished` flag is what separates the expected close from a lost connection. Without
  it a completely successful answer can be replaced by "The connection to the agent was
  lost".
* **A server-sent `error` event lands on that same listener, but with a payload.** That
  one is a sentence the service wrote for the operator, and is always shown — never
  re-POSTed, because it reports work that already happened and running it again bills the
  owner twice for one question.

`_stream_as_agent` marks the one exception with `"stage": "setup"`: a refusal raised
*before* the turn started (a switched-off agent, no enabled tools, an AI key with no model
name), where nothing ran and nothing was streamed. The console shows those like any other
error — the operator is the audience there. The **widget** must not: that sentence is a
configuration instruction, and its reader is a visitor on somebody else's website. So
`chatbot_turn_service.stream_turn` converts a `setup` error into a `fallback` event, and the
blocking path's degradation answers instead. See [FLOW_BUILDER.md](FLOW_BUILDER.md).

### Files

| File | Role |
|---|---|
| `services/deep_agents/query_executor.py` | The only thing that touches user data |
| `services/deep_agents/tool_factory.py` | `ToolConfig` → zero-argument `StructuredTool` |
| `services/deep_agents/prompt_builder.py` | Pure prompt composition |
| `services/deep_agents/prompt_sync_service.py` | Background regeneration + staleness |
| `services/deep_agents/model_factory.py` | Provider decision → LangChain chat model |
| `services/deep_agents/deep_agent_service.py` | Public entry points, result extraction |
| `routes/deep_agents/deep_agent_routes.py` | Cascade fragment + console |
| `db/tool_configs/queries.py` | `fetch_enabled_tools_for_agent` — one source for prompt and tools |

`collect_agent_tools()` returns one list that both the prompt builder and the tool
factory consume. That is what makes it impossible for an agent to be told about a tool
it cannot call, or handed one the prompt never mentioned.

---

## Related

* [SQL_ASSIST.md](SQL_ASSIST.md) — drafting a tool config from natural language
* [QUERY_JOINS.md](QUERY_JOINS.md) — the join rules the stored config uses
* [CHATBOT_AI_SETTINGS.md](CHATBOT_AI_SETTINGS.md) — prompts, LLM modes, the action router
* [AI_INBUILT.md](AI_INBUILT.md) — the local Ollama path
* [DOWNLOADER_AGENTS.md](DOWNLOADER_AGENTS.md) — the other half of the row cap: the
  100-row display budget, the exact count, the two extra tools (`confirm_download`,
  `download_status`), the batched export behind them, and the download card the widget
  draws from the turn payload rather than from anything the model wrote
* [DOCKER_AND_LOCAL_LLM.md](DOCKER_AND_LOCAL_LLM.md) — why this runs on Python 3.12 in a
  container, the local-model tuning, and the measurements behind the timeouts
