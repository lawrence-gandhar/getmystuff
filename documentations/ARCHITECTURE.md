# ARCHITECTURE.md

## System Architecture

GetMyStuff follows a **layered enterprise architecture**.

```
Client
  ↓
Routes
  ↓
Services
  ↓
DB Utils
  ↓
Database
```

---

# Project Structure

```
app/

db/          shared infra (base.py, db_sessions.py, db_utils.py, models.py) + per-feature subfolders
routes/      per-feature subfolders (auth/, dashboard/, datasource/, ai_settings/, ai_analytics/,
             chatbot/, chatbot_analytics/, flow_builder/, workspaces/, data_agents/,
             tool_configs/, tool_graphs/, graph_designer/, sql_assist/, query_test/,
             deep_agents/, downloader_agents/, agent_recursive_dataframes/, integrations/,
             email_dispatch/ — four authenticated controllers under /emails plus one
             unauthenticated one under /public/emails for inbound webhooks)
services/    per-feature subfolders (datasource/, ai_settings/, ai_analytics/, chatbot/,
             chatbot_analytics/, flow_builder/, ai_inbuilt/, workspaces/, data_agents/,
             tool_configs/, tool_graphs/, graph_designer/, sql_assist/, query_test/,
             deep_agents/, downloader_agents/ — itself split into base/, csv/, xls/,
             parquet/ — agent_recursive_dataframes/, email_dispatch/ — with nodes/ holding
             one runner per canvas, so the Email node's behaviour lives with the email
             module rather than inside the three features that call it)
models/      per-feature subfolders (user/, datasource/, ai_settings/, chatbot/, ai_analytics/,
             subscriptions/, flow_builder/, ai_inbuilt/, workspaces/, data_agents/,
             tool_configs/, downloader_agents/, graph_designer/, integrations/,
             email_dispatch/)
schemas/     shared infra (base.py, common.py) + per-feature subfolders (auth/, datasource/,
             workspaces/, data_agents/, tool_configs/, ai_settings/, ai_analytics/, chatbot/,
             chatbot_analytics/, flow_builder/, sql_assist/, query_test/, tool_graphs/,
             graph_designer/, deep_agents/, downloader_agents/,
             agent_recursive_dataframes/, integrations/, email_dispatch/)
utils/       flat helper modules (crypto.py, file_utils.py, validators.py, query_joins.py,
             sql_guard.py, datasource_status.py, http_responses.py, csv_to_db.py,
             csv_to_parquet.py, turn_recorder.py, outbound_http.py, type_coercion.py,
             events.py — the in-process event bus, here rather than in the feature that
             consumes it because the publishers are graph and integration runs)
templates/
static/
```

`deep_agents/` has no `models/` subfolder: it runs what Data Agents and Tool Configs
already define, and owns no table of its own. `agent_recursive_dataframes/` has neither
`models/` nor `db/` for the same reason and one more: nothing it produces is persisted at
all — a run lives inside one request, and the single column it needs belongs to
`tool_configs`. `query_test/` has neither `models/` nor `db/`:
it stores nothing at all — it runs a query the user is still writing and reports what the
database said. `tool_graphs/` has neither `models/` nor `db/` either, for the same reason
read from the other direction: it only draws tool configs that already exist, so it writes
nothing and a node's position is computed on every request rather than stored.
`graph_designer/` is the deliberate opposite and owns three tables: its graph is *authored*
rather than derived, so the drawing — node positions included — is the source of truth and
nothing can recompute it. `dashboard/` has no `schemas/` subfolder:
its one route renders a page from the session user and reads nothing from the request.

`downloader_agents/` is the one feature whose `services/` subfolder has subfolders of its
own: `base/` holds the graph, the reader, the queue and the format-agnostic machinery, and
`csv/`, `xls/` and `parquet/` hold one module each implementing the writer contract in
`base/part_writer.py`. The split is by *format* because that is the only axis along which
those three genuinely differ — a CSV merge concatenates bytes, a Parquet merge appends row
groups, an XLSX merge has to rewrite the workbook — and it keeps `base/` unable to know
which format it is building. It also has no `templates/`: the feature has no pages, and
reaches the user through a data agent's own conversation plus two download routes.

**Two consolidated documents sit above the per-feature pages below, and both are kept in step
with the code:**

[ENGINEERING_TECHNOLOGY.md](ENGINEERING_TECHNOLOGY.md) — the engineering record. Why each
dependency is here and what forced it, the module topology and which layers a feature
deliberately lacks, the four LangGraph runtimes compared, the security argument behind query
execution, the concurrency and lifecycle rules, a 35-point invariant checklist, every
alternative tried and rejected, and the known gaps. Read this before changing anything that
crosses two features.

[USER_GUIDE.md](USER_GUIDE.md) — the consolidated user documentation. Every feature below,
explained as a workflow rather than as modules: what each object is, the order you build them
in, the technology and the reasoning in plain language, every limit in one table, and what
each refusal message means. It supersedes nothing — each page below stays the authority on
its own feature — but it is the one page to read first, and the one to give somebody who has
to operate this without reading the source.

Feature deep-dives: [FLOW_BUILDER.md](FLOW_BUILDER.md) (the drawn conversation the widget
walks before the AI gets a turn — eleven blocks, the two switches that make one live, and the
flat string map that is the whole of a visitor's session; also served in-app at
`/flow-builder/help`, from `templates/flow_builder/help.htm`, the third instance of the
help-page shape, whose central point is the one thing operators reliably assume wrongly:
message text is sent verbatim and does not interpolate `{{VARIABLES}}`),
[CHATBOT_AI_SETTINGS.md](CHATBOT_AI_SETTINGS.md) (per-agent prompt, prompt variables,
language-model choice, webhook actions and flow attachment),
[CHATBOT_ANALYTICS.md](CHATBOT_ANALYTICS.md) (per-turn performance logging and the
dashboard over it), [AI_INBUILT.md](AI_INBUILT.md),
[QUERY_JOINS.md](QUERY_JOINS.md) (joining several tables into one authored query, in both
places a query is built),
[TOOL_CONFIGS.md](TOOL_CONFIGS.md) (the how-to for the Tool Configs page — every field of the
New/Edit panel, and one worked example per scenario: a single number, a filtered list, a
grouped report, a filter the assistant fills in, "is empty" done properly, a join, a query
only raw SQL can express, a parameterised statement, the three shapes of nesting,
whole-result grouping, a non-relational datasource, plus the limits and every refusal message
with its fix — also served in-app at `/tool-configs/help`, from
`templates/tool_configs/help.htm`, so an operator never has to leave the page to find out
what a field does),
[TOOL_QUERY_MODES.md](TOOL_QUERY_MODES.md) (the two ways a tool config holds its query — the
builder and raw SQL — and `utils/sql_guard.py`, the one definition of a statement this
application will run, shared by Ask AI, Tool Configs and the Deep Agents executor),
[SQL_ASSIST.md](SQL_ASSIST.md) (Ask AI — plain English to SQL from
reflected schema, never from the data; and Auto Create Tool, which saves the result as a tool
config — in the query builder when it fits, as the statement itself when it does not),
[QUERY_TEST.md](QUERY_TEST.md) (the **Test Query** button on every panel holding an unsaved
query — runs it once against the datasource through the executor itself, so a query the
database refuses is found while the form is open rather than by a visitor months later),
[TOOL_CHAINING.md](TOOL_CHAINING.md) (nested tool configs — a tool may embed others as
sub-queries, or embed a published graph, run inside-out as a LangGraph whose conditional
edges stop the chain the moment a level matches nothing; the values propagate outward as
bound parameters and the children stay callable in their own right, and a graph child may
stop the chain to put a question to the person in the conversation),
[TOOL_CHAIN_ITERATION.md](TOOL_CHAIN_ITERATION.md) (the other two shapes a value can take —
a link that runs the parent once per value rather than matching a list, for a placeholder
that is not on the right of an `IN`, and a `:name` the assistant fills in from the user's
question; both bound as values, and the one ceiling left refusing rather than truncating —
nothing caps how many rows or values a chain moves),
[TOOL_GRAPHS.md](TOOL_GRAPHS.md) (the read-only canvas over both of those — a nested tool
drawn as the START-to-END graph it compiles to, and the same tools' joins drawn as the sets
they intersect, navigated by a workspace/agent/tool tree),
[GRAPH_DESIGNER.md](GRAPH_DESIGNER.md) (the writable one — a canvas where a LangGraph is
*drawn* out of SQL statements, literal values, existing tool configs, branches, For each /
Do until loops and questions put to a person, then run whole or one node at a time with the
flow, the state and a capped output in a dock below it; a loop's body can read the item it is
on and either concatenate every pass's rows or build one UNION of a statement per pass and run
it once, values bound rather than written in either way; and once published, **four** things
can run it — a data agent as one of its tools, every agent in a workspace it is shared with,
a tool config that embeds it as a nested child, or a Run Graph step in a conversation flow,
with the pause a question causes propagating into all four because a pause is an outcome
rather than an error — also served in-app at `/graph-designer/help`, from
`templates/graph_designer/help.htm`, the operator-facing form of that page: every node, one
worked example per scenario, the limits and every refusal with its fix, linked from both the
library and the canvas so nobody has to leave a drawing to find out what a port means),
[DEEP_AGENTS.md](DEEP_AGENTS.md) (running a data agent's tool configs as real queries so a
chatbot answers from tool results and the language model never reads the database),
[DOWNLOADER_AGENTS.md](DOWNLOADER_AGENTS.md) (an answer capped at 100 printed rows plus the
exact `COUNT(*)`, and an offer to send the whole set as a CSV, Excel or Parquet file — read
fifty records at a time as a checkpointed LangGraph that pauses on the user's "yes", retries a
failed batch three times and leaves nothing behind when it gives up; the widget draws it as a
card with live progress and a real download button, built from the turn payload rather than
from a link the model wrote, and the visitor can keep asking questions the whole time),
[WIDGET_RENDERING.md](WIDGET_RENDERING.md) (how an answer becomes what a visitor sees —
the embeddable widget renders Markdown so a query result arrives as a real table instead
of a wall of pipe characters, escaping the model's text *before* any parsing so that
every tag in the output is one the renderer wrote and no attribute is ever built from
message text),
[AGENT_RECURSIVE_DATAFRAMES.md](AGENT_RECURSIVE_DATAFRAMES.md) (reading every record a
tool returns and grouping them in memory with polars, as a LangGraph map-reduce — a divider
reads a wave of batches off the one cursor and fans their folding out, the merge is exact
because `avg` crosses a batch boundary as a sum and a count, and the aggregations that have
no exact fold are refused rather than approximated; off by default and switched on per tool
config, because for a relational datasource `GROUP BY` in the database is faster and equally
exact),
[INTEGRATIONS.md](INTEGRATIONS.md) (the Integration Platform — the first feature here that
*writes* into systems this application does not own: a canvas where a workflow is drawn out of
a trigger, reads from any REST API — Shopify / GoHighLevel / SAP are later phases — field
mappings, validations, filters, branches and batched writes, then published as a **frozen
version** that a schedule runs and a dock watches record by record, so editing the drawing
changes nothing until it is published again; deterministic at run time so a run can be audited
and replayed, with a queue that survives a restart, a batch as the unit of a loop pass rather
than a record, three grains of failure that are never conflated, and a `dry_run` that builds
every request and calls nobody — plus the reasons it is a standalone engine rather than more
Graph Designer nodes, and the three defects in existing code that had to be fixed before a
single third-party credential could be stored),
[SHOPIFY_CONNECTOR.md](SHOPIFY_CONNECTOR.md) (the first *vendor* connector — Shopify's Admin
GraphQL API, read-only across orders, products and customers, authenticated by a custom app's
access token rather than OAuth; read-only as an asserted property rather than a stage, because
Shopify's mutations take no idempotency key and a create that times out after reaching the
server has already happened. Worth reading for the four things the shared runtime had to learn
first, three of which were seams built for exactly this and never once executed, and two of
which were broken: a base URL that raised `AttributeError` for any connector that computes its
own address, a GraphQL document that could not survive a substituter which reads every `{` as
an input name, a cursor that could reach a query string but not a request body, and — the one
that mattered — a vendor that reports failure as an HTTP **200** with an `errors` array, which
before the fix read as an empty page and ended the run *green*, making a refused sync and an
empty store indistinguishable. Also the shop domain as the module's sharpest security control,
checked twice, because it is user-supplied text that becomes the host of a request carrying the
merchant's token),
[BREVO_CONNECTOR.md](BREVO_CONNECTOR.md) (the second vendor connector and the **first one that
writes** — Brevo's v3 REST API across two sections, contacts and eCommerce, on a fixed
address that a stored base URL cannot override and an account key in an `api-key` header. Worth
reading for the property Shopify lacks and this one earns with a single literal: `updateEnabled:
true` makes the contact create an upsert matched on the email address, which is what makes
`idempotent=True` a fact rather than a claim — a create that timed out *after* reaching Brevo
lands on the same contact when the engine retries it, and a re-run of yesterday's sync updates
people instead of failing on every one of them. The eCommerce half — orders, products and
categories — is the application's first real *destination*, and repeats that argument three
times with the twist that Brevo upserts orders natively and needs the flag for the other two, so
one of the three writes deliberately does not send it. Also why transactional email is not an
operation on it: a send cannot be made idempotent, and every retry is another copy in somebody's
inbox. The contacts half needed no runtime changes at all, which is the claim the Shopify work
was making; the eCommerce half needed exactly one, `OperationSpec.rate_limits` plus a
`rate_limit_group`, because Brevo meters the endpoints behind one connection at rates differing
by 180× — and the group matters more than the per-operation part, since its hundred-an-hour is
one shared pool and a bucket per endpoint would spend four times it while reading as correct.
It also arrived with the **Apps gallery**, where an "app" is a registry `ConnectorSpec`
plus this user's connection counts rather than a new entity, and a tile counts *working*,
*needs attention* and *switched off* as three separate numbers because a revoked connection
still has a row and a parked one is not a problem to report),
[INTEGRATIONS_AI.md](INTEGRATIONS_AI.md) (the AI layer of that platform, kept on its own page
because the interesting part is what a model is *not* allowed to write: the honest reading of
"always act as Agent AI" for an engine that has to be deterministic, the catalogue built from
real rows so a connector the user has not connected is simply absent from the prompt, the two
layers of validation a draft passes before anybody sees it, why resolution *replaces* a model's
spelling with a real uuid and why there is no fuzzy matching, and the measurement showing the
in-built local model cannot do the task at its shipped context size),
[EMAIL_DISPATCH.md](EMAIL_DISPATCH.md) (the platform's first outbound notification path —
before it, every workflow this application can build ended silently. SMTP servers, templates
with declared `{{PLACEHOLDERS}}`, an Email node on all three canvases, event and webhook
triggers, and one permanent row per message. Worth reading for the five decisions it turns
on: a table rather than a broker for the third time; rendering at *enqueue* so the log says
what was actually sent and a retry is byte-identical; a dead worker that **fails** its
message rather than resuming it, because the relay may already have delivered it and nobody
can tell; sending serialised per SMTP server *inside the claim*, because a burst of parallel
connections gets a sending domain blocked and that is not something a retry fixes; and a
binding that finds nothing yielding nothing, so the template's own default or required flag
decides rather than one hard-coded answer. Also where "dynamic variables from the Agents
section" actually resolves, and why the integrations Email node is the dangerous one),
[EVENT_BUS.md](EVENT_BUS.md) (the small in-process publish/subscribe the email triggers ride
on — shared infrastructure rather than part of that module, because the publishers are graph
runs and integration runs and none of them should import an email module to say something
happened. Strict on subscribe and forgiving on publish, and the two reasons that asymmetry is
right; why every name in the catalogue has a real publisher and which two candidates were
dropped rather than faked; and the honest cost of publishing after the commit instead of
building a durable outbox),
[DOCKER_AND_LOCAL_LLM.md](DOCKER_AND_LOCAL_LLM.md) (why the app runs in a container on
Python 3.12, and how the in-built Ollama models are configured and measured),
[SCHEMAS.md](SCHEMAS.md) (the Pydantic layer — one package per feature, every request parsed
through a request schema and every response built from a response schema; the error bridge
that keeps a Pydantic failure from reaching a user verbatim, and where a rule belongs when it
is split between a schema and a service),
[TESTING.md](TESTING.md) (the test suite and its coverage ratchet — why the tests run in the
container against an SQLite database, the four type shims that makes possible, how an
authenticated route is reached, and the timestamped run history),
[MIGRATIONS.md](MIGRATIONS.md) (how the schema is built and kept current — the app applies
`alembic upgrade head` at startup, why `create_all` was removed after it silently skipped a
new column, the three database states and what to do about an untracked one, and the
conventions a new revision follows).

Cross-cutting conventions, which apply to every feature above rather than to one:
[SERVICE_PATTERNS.md](SERVICE_PATTERNS.md) (what belongs in a service, and
`utils/datasource_status.py` — the one reading of the per-table and per-column switches set in
Data Sources, honoured by the Tool Config pickers, the Ask AI schema and the agent executor),
[ERROR_HANDLING.md](ERROR_HANDLING.md) (custom exception types, the escaped HTML-alert
helpers in `utils/http_responses.py`, what is allowed to reach a user versus what stays
in the log, and the embedded widget — the one component whose failures the server cannot
see, so its console is where the operator's half of the message goes), [HTMX_PATTERNS.md](HTMX_PATTERNS.md) (the swap patterns, panel-local error
banners, the global `htmx:beforeSwap` handler that lets a non-2xx response display at all,
and the global offcanvas lock that keeps a panel open until its own close button is
clicked),
[SECRETS_AND_KEY_ROTATION.md](SECRETS_AND_KEY_ROTATION.md) (the one symmetric key every
stored credential is encrypted with — what `FERNET_KEY` and `FERNET_KEY_OLD` mean, why the
process refuses to start without the first, the migration that moved existing rows off the
key that used to be a literal in the source, and the step ordering for changing the key that
is the difference between a background re-encryption and every secret becoming unreadable),
[SERVICE_PATTERNS.md](SERVICE_PATTERNS.md).

Four objects the sidebar exposes separately, because their ownership differs:

| Sidebar entry | Path | Owned by | Attached to |
|---|---|---|---|
| **Agents** | `/chatbot-settings` | user | — (each agent *has* a prompt, a flow, actions) |
| **Chatbot Analytics** | `/chatbot-analytics/` | user | read-only view over every agent's turns |
| **Flow Builder** | `/flow-builder/` | user | at most one agent |
| **Actions** | `/actions` | user | any number of agents |

"Agents" is the user-facing name for `ChatbotApiKey`; its routes and templates still live under
`chatbot_settings` / `chatbot/`.

An agent gets what it may read one of two ways, fixed at creation: a **datasource
target** (the whole datasource, or named tables/collections/files), or an attached
**data agent**, whose tool configs are the scope and whose widget stores no datasource
at all. The two are exclusive — see [DEEP_AGENTS.md](DEEP_AGENTS.md).

`db/`, `models/`, `routes/`, and `services/` group related files by feature — a feature's
subfolder is named the same across all four layers it appears in (e.g. `datasource/` exists
under both `routes/` and `services/`). Each subfolder's `__init__.py` re-exports its public
symbols for `models/` and `routes/`; `services/` subfolders use plain empty `__init__.py`
files since service callers import specific functions by full module path. See `CLAUDE.md`
for the full rule and `flow_builder/`/`ai_inbuilt/` for the reference implementation.

---

# Responsibilities

## Routes

Routes handle:

* HTTP requests
* validation
* response rendering

Routes must NOT contain business logic.

Example:

```
@post("/datasource")
async def create_datasource(data):
    sanitized = sanitize(data)
    return datasource_service.create_datasource(sanitized)
```

---

# Services

Services handle:

* business rules
* validation
* orchestration
* database operations

Services never return HTML.

---

# Utils

Utils contain reusable helpers.

Examples:

* db_utils.py
* validators.py
* exceptions.py
* query_joins.py — the join rules (which datasource types can join, which join types
  each dialect has, how a `table.column` reference is checked) shared by the two
  places a query is authored: the Tool Configs library and the Configurations page's
  Tool Base Config panel. Its `build_join_sql` renders joins for *display* only; the
  Deep Agents executor builds real joins from reflected tables
* turn_recorder.py — per-turn token and timing accumulation via a ContextVar, so the
  layer that knows a call's cost need not thread it up to the layer that logs the turn

---

# Templates

Templates must follow:

```
templates/
   base.html
   dashboard/
   datasource/
```

Each feature must have its own folder.

---

# Static Assets

```
static/css
static/js
static/img
```

Use Bootstrap 5.

Avoid inline styles where possible.
