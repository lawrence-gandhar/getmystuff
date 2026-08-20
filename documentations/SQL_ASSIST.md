# SQL_ASSIST.md

Ask AI — turn a plain-English request into a SQL query for one of the user's own relational
datasources.

---

# The contract

**The model is shown structure, never data.**

What it receives is the reflected schema of the tables the user picked — table names, column
names and types, primary keys, foreign keys — and nothing else. No row is sampled, no count is
taken, and **the generated query is not run**. It is handed to the user to read, refine and
use.

That last point is what keeps the promise true of the whole feature rather than only of the
prompt: there is no code path in this module that executes what the model wrote.

This still holds now that Deep Agents can execute a tool config. What becomes runnable is the
*validated builder config* — five known clause types, every identifier reflected and every value
bound — not the SQL string the model produced, which is only ever shown to the user. A drafted
tool goes through `tool_config_service.create_tool_config` like any hand-made one, so the model's
SQL text never reaches a database. See [DEEP_AGENTS.md](DEEP_AGENTS.md).

A refinement turn re-sends the same schema plus the conversation so far, so a follow-up cannot
reach any further into the datasource than the first attempt did.

---

# The active-column contract

The structure the model is shown is not the whole schema — it is the part the user has left
switched on in Data Sources (see
[SERVICE_PATTERNS.md](SERVICE_PATTERNS.md#who-reads-the-status--apputilsdatasource_statuspy)).
`_load_metadata` prunes the reflected metadata before `_build_prompts` ever sees it:

* an inactive **table** named in the form post is refused by name — the picker no longer offers
  it, but the field is a form field and can still carry it;
* inactive **columns** are removed from `columns`;
* inactive names are removed from `primary_key`, and any **foreign key** whose own column or
  whose referenced column is inactive is dropped — otherwise the model is invited to join on a
  column it may not select;
* a table left with **no columns** is refused, naming that table.

Pruning rather than post-checking is the design. A model cannot select, join on or filter by a
column it was never told exists, and there is no SQL parser in this application to police its
output if it were shown one. The system prompt says so in those terms: *a column that is not
listed does not exist for you*.

### The projection rule

The prompt asks for every column, spelled out, and puts a literal per-table list ahead of the
schema JSON so the model copies rather than derives it. Two carve-outs, both deliberate:

* **Aggregates are exempt.** "Include every column" cannot hold for
  `SELECT COUNT(*) … GROUP BY status` without changing what the query counts, and a rule the
  model must break to answer the question is a rule it learns to ignore everywhere else. The
  exemption has to be stated together with the grouping rule below: told to select every column
  *and* to group, a model does both, and produces exactly the query the database refuses.
* **`SELECT *` is banned outright**, not discouraged. `*` is the one selection whose column list
  the database resolves at run time, so a query approved today starts returning a switched-off
  column tomorrow without the query changing.

### Enforced versus advised

| Check | Where | Enforced? |
|---|---|---|
| The model only sees active tables and columns | `_load_metadata` pruning | **Enforced** — structurally, by omission |
| No `SELECT *` / `table.*` in the generated query | `sql_guard.star_selection_violation`, via `_validated_sql` | **Enforced** — 502, query not shown |
| A drafted tool config references only active columns | `_reference_resolver` against the pruned metadata | **Enforced** — 400, tool not created |
| Only active columns are actually *read* | `query_executor` on every run | **Enforced** — see [DEEP_AGENTS.md](DEEP_AGENTS.md) |
| The query includes *all* active columns | `sql_guard.missing_identifiers`, via `_omitted_columns` | **Advised** — reported as `omitted_columns` and shown as an amber note |
| The grouping is one the database will accept | `sql_guard.group_by_violation`, via `_regrouped` | **Retried, then advised** — one regeneration, then a red note in `warnings` |

The last row is a text search, not a parse: it cannot tell a SELECT list from a WHERE clause,
and it cannot tell that a CTE's outer query legitimately narrows what the inner one read.
Refusing on it would reject every aggregate and every CTE the panel exists to help write, so the
user is told and the decision stays theirs. The only place requirement "all active columns" is
*guaranteed* is the executor's builder mode, which builds the column list itself instead of
asking a model for one.

---

# Why it is its own module

The panel is opened from the Tool Configs page, but generating SQL from a schema needs a
datasource and nothing else — any page with one in view can call it. So it lives in
`app/services/sql_assist/` and `app/routes/sql_assist/`, not inside `tool_configs`. Tool
Configs *calls* it; it does not belong to it.

It is also **not** another mode of `ai_analytics_service`, which deliberately profiles real
rows to answer questions *about* the data (see its module docstring). This module answers a
question about the *schema*. Opposite contracts, separate services — but it reuses that
module's provider plumbing, so model selection behaves identically to everywhere else in the
app.

---

# Metadata by reflection — `app/db/db_utils.py`

Nothing here hand-writes SQL. Reflection goes through SQLAlchemy's `Inspector`, which emits its
own dialect-correct catalog queries — so there is no query string in this application for a
table or column name to be interpolated into, and adding a dialect adds no branch. `Inspector`
is a sync API, so it runs inside `conn.run_sync` on the async engine's connection.

| Function | Returns |
|---|---|
| `fetch_rdbms_table_names(url)` | Every table **and view** name in the default schema. A view is as queryable as a table, so it is an equally valid target for a SELECT. |
| `fetch_rdbms_metadata(url, table_names)` | One entry per table: columns (name, type, nullable), `primary_key`, `foreign_keys`. |

```json
{"table": "orders", "kind": "table",
 "columns": [{"name": "id", "type": "INTEGER", "nullable": false}],
 "primary_key": ["id"],
 "foreign_keys": [{"columns": ["customer_id"],
                   "references_table": "customers",
                   "references_columns": ["id"]}]}
```

**Foreign keys are included** because they are what make a generated join correct rather than
guessed. **Column defaults and comments are excluded**: they can carry literal values from the
database, and this path exists precisely to send nothing but structure. Keys and constraints
are only asked for on real tables — a view has none, and some dialects raise rather than return
empty when asked about one.

`MAX_REFLECTED_TABLES = 25`. Reflecting a table costs a catalog round-trip and every table
reflected also lands in a prompt, so the count is bounded rather than "however many the caller
asked for".

Only names that actually exist are reflected. The service compares what it asked for against
what came back and names the difference — a query generated against three tables when the user
picked four looks correct and is not.

### Relationship to the older metadata helpers

`fetch_rdbms_tables` / `fetch_rdbms_schema` still exist, still keep a per-dialect hand-written
query, and still back the Configurations page, the Tool Configs cascades and AI analytics.
Those callers depend on their exact output, so the reflected path was added *beside* them
rather than replacing a hot path. `metadata_service` exposes both:

| Service function | Backed by |
|---|---|
| `get_rdbms_tables` | raw-SQL `fetch_rdbms_tables` (existing callers) |
| `get_table_schema` | raw-SQL `fetch_rdbms_schema` (existing callers) |
| `get_rdbms_reflected_tables` | `fetch_rdbms_table_names` (this module) |
| `get_rdbms_reflected_metadata` | `fetch_rdbms_metadata` (this module) |

Migrating the older two to reflection would be a behaviour change for several features and was
left alone.

---

# Service — `app/services/sql_assist/sql_assist_service.py`

`generate_sql(db, user_id, datasource_id, table_names, prompt, llm_mode, llm_api_key_id,
history_json)` → `{"draft", "history", "dialect", "tables", "omitted_columns", "warnings"}`.

The returned `history` **includes this turn**, ready to be posted back with the next
refinement. `omitted_columns` names the active columns the query never mentions, and `warnings`
carries anything that stops the query running at all — today only the grouping check, see
[The grouping rule](#the-grouping-rule--_regrouped).

### `SqlDraft`

The pydantic shape the provider is forced to return:

* `sql` — one read-only statement, no trailing semicolon. **Empty when the schema cannot answer
  the request** — that is a valid, useful answer ("there is no order date column"), not a
  failure, and the panel presents it as one.
* `explanation` — what the query returns and how, or what the schema is missing.
* `assumptions` — up to 5 notes on anything guessed: a join inferred without a foreign key, a
  column read as a date, an ambiguous word in the request.

### The system prompt

States the one hard fact about the feature — that the model has been given structure and
nothing else — because a model that believes it has seen the data will happily describe rows
that do not exist. It then requires: only the tables and columns in the metadata; an empty
`sql` plus an explanation when the request cannot be met; one read-only statement; joins on the
given foreign keys, with anything else declared in `assumptions`; qualified columns once more
than one table is involved; explicit column lists; a grouping the database will accept; and the
target dialect's syntax.

### Language model choice

`LLM_MODES` mirrors the chatbot's own pair — `api_key` ("My LLM API key") and `in_built`
("In-built LLM") — with the same values, so the concept reads the same to users. It is spelled
out locally rather than imported from `app.models.chatbot`: the concept is shared, the two
features are not.

`_validated_llm_choice` turns the form's choice into the two flags
`ai_analytics_service.answer_structured` takes, which are mutually exclusive by construction:

| Mode | `use_inbuilt_llm` | `forced_key_uuid` |
|---|---|---|
| `in_built` | `True` | `None` — any key is ignored |
| `api_key` | `False` | the pinned key, or `None` for "whichever key is active in AI Settings" |

See [CHATBOT_AI_SETTINGS.md](CHATBOT_AI_SETTINGS.md) for how the same choice is expressed
elsewhere, and [AI_INBUILT.md](AI_INBUILT.md) for the local model behind `in_built`.

### Refinement

The conversation lives in a hidden field that the **server** writes into the result partial,
which the form pulls back in with `hx-include`. So the history is whatever the server last
confirmed, and a turn that fails does not discard it (`error.htm` re-renders it unchanged).

Bounds, because it all becomes prompt:

| Cap | Value | Why |
|---|---|---|
| `_MAX_PROMPT_LEN` | 2000 | Matches AI analytics' prompt cap. |
| `_MAX_TABLES` | 25 (= `MAX_REFLECTED_TABLES`) | Over the cap is **refused, not trimmed** — silently reflecting the first 25 would generate a query against a schema the user believed was larger. |
| `_MAX_HISTORY_TURNS` | 6 | The model needs the last few attempts to improve on them, not the whole session. |
| `_MAX_HISTORY_SQL_LEN` | 4000 | Per stored turn. |
| `sql_guard.MAX_SQL_LENGTH` | 8000 | Beyond this something has gone wrong with the response, not the request. Shared with Tool Configs and the executor — see [TOOL_QUERY_MODES.md](TOOL_QUERY_MODES.md). |

`_validated_sql` — the check that a generated query is a single read-only statement before it
is displayed — is `app/utils/sql_guard.read_only_violation` with a 502 wrapped round it. The
same rule decides what a tool config may store and what the executor may run, because a query
shown here is likely to be run and may well be saved: three different ideas of "read-only"
would mean the loosest one wins.

---

# Auto Create Tool

The button on a result saves the generated query as a Tool Config an agent can run. It asks
for the two things only the user can answer — what to call it, and which agent gets it — and
fills in everything else.

### Why a second AI call

Converting the query into the builder's shape is a **separate, narrow call**
(`draft_tool_config`), not one more field on `SqlDraft`. It only costs anything when the user
actually asks for a tool, and converting one known query against one known schema is a far
smaller task than writing SQL. That matters most for the in-built local model — a 1.7B
parameter model (see [AI_INBUILT.md](AI_INBUILT.md)) — where one request producing prose, SQL,
assumptions *and* a nested builder config is exactly the kind of prompt that comes back
malformed.

### `ToolDraft`, and `fits`

A Tool Config can store its query two ways: the **builder** — columns, aggregations,
group_by, filters, joins (see [QUERY_JOINS.md](QUERY_JOINS.md)) — or the **statement itself**.
`fits` decides which of the two this query lands in. It does **not** decide whether the tool
can be created: every valid read-only query can. See
[TOOL_QUERY_MODES.md](TOOL_QUERY_MODES.md).

Plenty of valid SQL needs more than the builder — ORDER BY, LIMIT, HAVING, DISTINCT,
subqueries, CTEs, window functions, CASE, UNION, expressions in the SELECT list, OR between
filters, a non-equality join condition, a filter compared against another column. So `fits`
false is a real answer the model is expected to give, with `reason` naming what is in the way,
and the panel says so above the create form. **A tool that quietly differs from the query the
user just read would be worse than no tool** — but refusing to save a query the user has read
and approved is worse than either, which is what SQL mode is for.

The builder is tried first because it is the stronger artefact: identifier-checked, filter
values bound as parameters, and reopening fully editable in the builder afterwards.

The model suggests a `tool_name` (a lowercase identifier) and a `description` either way, so
the "ask for the name" step arrives prefilled in both modes.

### What the server fixes, and what it refuses

The model's answer is not trusted as a config. `_validated_tool_draft`:

* **checks the base table** is one the user actually selected;
* **resolves every column reference against the reflected schema** — see below;
* runs the result through `tool_config_service.validated_query_config`, the *same* validator
  the query builder's own output goes through. One validator, so an AI-made config cannot be
  less trustworthy than a hand-made one.

`_reference_resolver` is the part worth reading. A bare column name in a joined query is
**looked up, not assumed**:

| The model gives | Outcome |
|---|---|
| `orders.total` | Kept, after checking `orders` is in the query and really has `total`. |
| `total`, and only `orders` has it | Qualified to `orders.total` (left bare when the query has no joins, matching the builder). |
| `id`, and both tables have it | **Rejected as ambiguous**, naming both tables. |
| `profit_margin`, in no table | **Rejected** — the model invented it, *or* the column is switched off in Data Sources and so was never in the metadata. The message says both, because from here the two are indistinguishable and only the user knows which. |
| `recent_orders.total`, not joined | **Rejected** — the query does not read that table. |

"Rejected" above means *rejected as a builder config*, not rejected as a tool. Each of those
outcomes raises, `draft_tool_config` catches it, and the tool is drafted in SQL mode instead
with the rejection message as the reason shown to the user. The model's reading of the query
was wrong; the query itself never was.

Qualifying a bare name with the base table would be a guess, and a wrong guess is the worst
outcome available here: the tool would be created, would validate, would open in the builder,
and would quietly answer a different question than the SQL the user approved. The user has the
query in front of them and can refine it or build the tool by hand; what they cannot do is
notice that a saved tool silently reads the wrong table's column.

### Creation

`create_tool_from_draft` goes through `tool_config_service.create_tool_config` rather than
writing the row itself, so an AI-created tool is subject to every rule a hand-made one is:
agent and datasource ownership, the per-agent unique name, and full query-config validation.
**Nothing on the row records that an AI drafted it** — there is no second kind of tool config
to maintain.

A rejected name (already taken on that agent) re-renders the same form with the config and
preview intact, so it can be fixed without converting the query again.

**Which tables the tool records** is decided per mode, and neither is "everything the user
selected in the panel":

* **SQL mode** records every selected table, with the model's chosen primary one moved to the
  front (`_primary_first`). The statement reads what it reads and nothing here parses it, so the
  user's selection is the best record available — and it is what lets the routing prompt state
  the tool's real scope and the executor check each table is still active.
* **Builder mode** records the base table plus whatever its joins bring in — `query_tables` of
  the validated config, not the selection. A built query reads exactly what it joins, and
  recording a table it never touches would overstate the tool's scope as surely as the old
  single-table record understated it.

See [TOOL_QUERY_MODES.md](TOOL_QUERY_MODES.md#a-tool-records-every-table-it-reads).

### Edit shows what was created

This is a guarantee, not a hope, and it follows from the two decisions above: the stored
`config` is in exactly the form the builder writes, and every reference in it was resolved
against the real schema. So `GET /tool-configs/{uuid}/edit-form` repopulates every control —
the join row, each column and alias, the aggregation function, the grouping, the filter
operator and value — and saving without edits stores a byte-identical config.

The edit form already preloads each *joined* table's columns (`get_column_map`), which is what
lets its dropdowns match a qualified reference like `orders.total`.

### Dependency direction

`sql_assist` depends on `tool_configs`, never the reverse. Tool Configs works with no AI
involved; the assistant is what knows how to produce one. Only `create_tool_from_draft` and
`draft_tool_config` touch `tool_config_service` — `generate_sql` does not, so the panel is
still usable on a page that has nothing to do with tool configs.

---

# The read-only guard — `_validated_sql`

The person reading the panel is likely to run what it shows, and a tool config's query is a
read by definition, so a generated write is a bug to surface rather than display.

Order matters:

1. Strip a markdown fence the model added anyway (asked against in the system prompt, but some
   models fence regardless — formatting is not a reason to reject a good query) and any trailing
   semicolon.
2. **Strip string literals and comments** (`_strip_literals`). Without this,
   `WHERE action = 'delete'` reads as a DELETE and `WHERE body = 'a;b'` as two statements.
3. Must start with `SELECT` or `WITH` — a CTE is the natural shape here, and a `WITH` that goes
   on to write is caught by the next step.
4. Must contain no `;` — no second statement.
5. Must contain no write verb.

`_WRITE_KEYWORDS` is deliberately short: `insert`, `update`, `delete`, `into`, `drop`, `alter`,
`create`, `truncate`, `replace`, `merge`, `grant`, `revoke`. It covers writes reachable *from a
position a read could reach* — `WITH … INSERT`, `SELECT … INTO`, appended DDL. `PRAGMA`, `COPY`,
`CALL`, `SET` and friends are only valid at the start of a statement (refused by step 3) or
after a `;` (refused by step 4); listing them would add nothing but false rejections of valid
queries — a column named `call`, a table named `copy`.

Word boundaries mean `created_at` is not a `CREATE` and `OFFSET` is not a `SET`.

A sixth step runs after those five, and only here — not in Tool Configs, not in the executor:
`star_selection_violation` refuses `SELECT *` and `table.*`, also as a 502. It matches on the
literal-stripped text, so a filter value like `'select * from x'` cannot trip it, and it
deliberately does **not** match `COUNT(*)` — an aggregate over all rows names no columns, and
refusing it would break every "how many" question there is.

---

# The grouping rule — `_regrouped`

The read-only guard asks whether a query *may* run. This asks whether it **can**. MySQL's
default `sql_mode` includes `ONLY_FULL_GROUP_BY` and PostgreSQL enforces the same rule, so a
grouped query that selects a column it neither aggregates nor groups is refused by the database:

> SELECT list is not in GROUP BY clause and contains nonaggregated column
> 'teamtracking.project_details.client_name' which is not functionally dependent on columns in
> GROUP BY clause; this is incompatible with sql_mode=only_full_group_by

That query never had a chance of running. Left alone it fails in front of the user, or — worse,
once **Auto Create Tool** has saved it — in front of a visitor talking to an agent. Three things
happen about it, in the order they take effect:

**1. The prompt states the rule.** Two bullets in the system prompt, next to the aggregate
carve-out: every column in a `GROUP BY` query must be aggregated or grouped, and a SELECT list
may not mix an aggregate with a plain column even without a `GROUP BY`. Most attempts never
break it.

**2. A query that breaks it anyway is written again.** `_regrouped` runs
`sql_guard.group_by_violation` over the draft, passing `_primary_keys(metadata)` so the shape
both databases *do* allow — group by a table's key, select its other columns — is not treated as
a fault. On a violation the model is called a second time with the failed statement, the
offending column, and the three ways out (group it, aggregate it, drop it).

**Asked again, never patched.** Adding the column to the `GROUP BY` here would be a change to
what the query counts — one row per group becoming one row per pair — and the explanation beside
it would then describe a different query than the one shown. Regenerating keeps the SQL and the
words about it coming from the same place.

**3. A second failure becomes a note, not a refusal.** The original draft is returned with a
message in `warnings`, which `result.htm` renders as a red alert above the query: *"This query
will not run as written."* The check is a heuristic and the panel does not execute anything, so
the user reading the SQL with the problem named beside it is better off than a 502 that leaves
them nothing to refine.

One retry, not a loop. A second call is worth it; a third is a model that is not going to get
there, and the user can say so faster by rewording the prompt.

The **Auto Create Tool** path is covered from the other end: the conversion prompt is told to
copy the query's own `GROUP BY`, and `tool_config_service.validated_query_config` refuses a
builder config that selects an ungrouped column — which falls back to storing the statement as
SQL rather than erroring, exactly as any other conversion failure does. See
[TOOL_QUERY_MODES.md](TOOL_QUERY_MODES.md).

---

# Test Query

The generated query's card header carries a **Test Query** button beside *Copy* and
*Auto Create Tool*. It posts the statement to `/query-test`, which runs it once
against the datasource and reports whether the database accepted it — see
[QUERY_TEST.md](QUERY_TEST.md).

This is the one thing in the panel that touches data, and it is worth being exact
about what that does and does not change. The feature's promise is that **the model**
is shown structure and never contents: the prompt is built from reflected metadata,
no row is sampled, and nothing is run to produce the query. Pressing Test runs the
finished query, because the user asked it to, and what comes back is the column names
and the row count — never a value, and nothing that goes anywhere near a prompt. A
query the model wrote is a query somebody is about to save as a tool, and the checks
above it (read-only, no `SELECT *`, the grouping rule) can only rule things out. The
database is what says yes.

---

# Relational datasources only

`get_datasource_choices` **filters** rather than flags: a file or collection datasource has no
SQL to generate — they are queried through pandas and aggregation pipelines respectively — and
reflection is a relational concept. Offering one would only produce an error on submit.
`_resolve_datasource` enforces the same rule on the way in, via `supports_joins` from
[QUERY_JOINS.md](QUERY_JOINS.md)'s shared module.

When the user has none, the panel explains that instead of showing an empty dropdown.

---

# Routes — `app/routes/sql_assist/sql_assist_routes.py`

`SqlAssistController`, path `/sql-assist`.

| Endpoint | Renders |
|---|---|
| `GET /form` | `partials/form.htm` — the panel body: datasource, tables, model, prompt. Takes `?agent=` (the host page's filter). |
| `GET /tables` | `partials/tables_field.htm` — the table picker for the datasource just chosen (HTMX cascade). |
| `POST /generate` | `partials/result.htm`, or `partials/error.htm`. |
| `POST /tool-form` | `partials/tool_form.htm` — Auto Create Tool: the query as a Tool Config plus the name to save it under, or the reason it cannot be one. |
| `POST /create-tool` | `partials/tool_created.htm`, or `tool_form.htm` again with the error. |

`tool-form` is a POST rather than a GET because it carries the query and re-reads the schema to
convert it — and the SQL is too long for a query string.

`_echo` carries the four fields every step hands on (datasource, model choice, key, agent
filter). They are re-read from the form each time rather than held anywhere, so a tampered value
is just another value the services validate.

Errors from `generate` are **rendered as an inline alert rather than raised**, so a rejected
prompt or an unreachable datasource leaves the panel — and everything typed into it — exactly
where it was.

The table list arrives as repeated form fields from a multi-select, so `_table_names` reads it
with `FormMultiDict.getall` (with an explicit default — it raises on a missing key). A plain
`get` would return only the first of several, silently generating a query against one table
when the user picked four.

---

# Templates and JS

| File | Role |
|---|---|
| `templates/sql_assist/panel.htm` | The offcanvas shell. **A second host page needs only this include plus a button pointing at `#sqlAssistOffcanvasContent`.** |
| `partials/form.htm` | The panel body. One cascade (datasource → tables); the prompt box doubles as the refinement box. |
| `partials/tables_field.htm` | Multi-select, so the AI can join. Options come from the same reflection the prompt's schema does, so the picker can never offer a table the model would not be shown. |
| `partials/result.htm` | The query, the explanation, the assumptions, the hidden history field, and the **Auto Create Tool** button. Also the two notes about the query itself: `omitted_columns` in amber, and `warnings` — a query the database would refuse — in red above it. |
| `partials/tool_form.htm` | The drafted tool config: name (prefilled), agent, description, and the query as the builder will hold it. Or the reason it cannot be a tool. |
| `partials/tool_created.htm` | Success, with an **Edit tool config** button, plus an out-of-band rebuild of the host page's tool-configs table. |
| `partials/error.htm` | A failed turn, with the conversation preserved. |
| `partials/panel_error.htm` | The panel could not be opened at all. |
| `static/js/sql_assist.js` | Copy-to-clipboard, and clearing the panel on close so reopening does not carry the previous conversation's history into a new question. |

The Auto Create Tool controls sit in their **own `<form>`** inside the result partial —
`#sqlAssistResult` is outside the main form, and nesting forms is invalid HTML. The `Auto
Create Tool` button reaches it with `form="sqlAssistToolForm"`.

`tool_created.htm`'s Edit button and its out-of-band table refresh both target elements that
belong to the Tool Configs page (`#toolConfigOffcanvasContent`, `#toolConfigsTable`). A future
host page that adopts the panel without those still gets a working create — it just won't get
those two affordances.

`result.htm` writes the history with `value='{{ history | tojson }}'` — **single-quoted on
purpose**. `tojson` escapes `<`, `>`, `&` and `'` but deliberately leaves double quotes alone,
so the JSON would break out of a double-quoted attribute.

`sql_assist.js` falls back to a selection-based copy when `navigator.clipboard` is unavailable,
which is the case on a page served over plain HTTP to anything but localhost — exactly how this
app runs in development.

---

# Not covered

* **A query that needs more than the builder is not approximated into it.** ORDER BY, LIMIT,
  HAVING, subqueries, window functions and the rest are saved as the statement itself instead
  — see [Auto Create Tool](#auto-create-tool) and
  [TOOL_QUERY_MODES.md](TOOL_QUERY_MODES.md). What is still refused is anything that is not a
  single read.
* **The conversation is not persisted.** No history table; it lives in the panel for as long as
  it is open. Contrast `PromptHistory`, which AI analytics writes for every run. A *created tool*
  is of course persisted — as an ordinary tool config.
* **Auto Create Tool only creates.** There is no "update this existing tool config from a new
  query"; editing an existing tool is the query builder's job.
* **The SQL the model writes is never executed *by this feature*.** By design — see
  [The contract](#the-contract). A tool config *created* from a draft is executable by the
  Deep Agents runtime, and there the mode matters: a builder tool runs the validated config
  rebuilt from reflected columns, a SQL tool runs the statement the user read and approved
  before saving it. In neither case does anything run without a person having chosen to
  create the tool.
