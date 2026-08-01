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
history_json)` → `{"draft", "history", "dialect", "tables"}`.

The returned `history` **includes this turn**, ready to be posted back with the next
refinement.

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
than one table is involved; explicit column lists; and the target dialect's syntax.

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
| `_MAX_SQL_LEN` | 8000 | Beyond this something has gone wrong with the response, not the request. |

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

A Tool Config stores a **builder**, not SQL: columns, aggregations, group_by, filters, joins
(see [QUERY_JOINS.md](QUERY_JOINS.md)). Plenty of valid SQL needs more than that — ORDER BY,
LIMIT, HAVING, DISTINCT, subqueries, CTEs, window functions, CASE, UNION, expressions in the
SELECT list, OR between filters, a non-equality join condition, a filter compared against
another column.

So `fits` is a real answer the model is expected to give, with `reason` naming what is in the
way, and the panel shows that reason instead of a form. **A tool that quietly differs from the
query the user just read would be worse than no tool.**

When it does fit, the model also suggests a `tool_name` (a lowercase identifier) and a
`description`, so the "ask for the name" step arrives prefilled rather than blank.

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
| `profit_margin`, in no table | **Rejected** — the model invented it. |
| `recent_orders.total`, not joined | **Rejected** — the query does not read that table. |

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
| `partials/result.htm` | The query, the explanation, the assumptions, the hidden history field, and the **Auto Create Tool** button. |
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

* **A query that needs more than the builder cannot become a tool.** ORDER BY, LIMIT, HAVING,
  subqueries, window functions and the rest are refused with a reason rather than approximated
  — see [Auto Create Tool](#auto-create-tool). The SQL is still there to copy.
* **The conversation is not persisted.** No history table; it lives in the panel for as long as
  it is open. Contrast `PromptHistory`, which AI analytics writes for every run. A *created tool*
  is of course persisted — as an ordinary tool config.
* **Auto Create Tool only creates.** There is no "update this existing tool config from a new
  query"; editing an existing tool is the query builder's job.
* **The SQL the model writes is never executed.** By design — see
  [The contract](#the-contract). A tool config *created* from a draft is executable by the Deep
  Agents runtime, but what runs is the validated builder config, never the model's SQL string.
