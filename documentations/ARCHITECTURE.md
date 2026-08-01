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
             tool_configs/, sql_assist/, deep_agents/)
services/    per-feature subfolders (datasource/, ai_settings/, ai_analytics/, chatbot/,
             chatbot_analytics/, flow_builder/, ai_inbuilt/, workspaces/, data_agents/,
             tool_configs/, sql_assist/, deep_agents/)
models/      per-feature subfolders (user/, datasource/, ai_settings/, chatbot/, ai_analytics/,
             subscriptions/, flow_builder/, ai_inbuilt/, workspaces/, data_agents/, tool_configs/)
schemas/     shared infra (base.py, common.py) + per-feature subfolders (auth/, datasource/,
             workspaces/, data_agents/, tool_configs/, ai_settings/, ai_analytics/, chatbot/,
             chatbot_analytics/, flow_builder/, sql_assist/, deep_agents/)
utils/
templates/
static/
```

`deep_agents/` has no `models/` subfolder: it runs what Data Agents and Tool Configs
already define, and owns no table of its own. `dashboard/` has no `schemas/` subfolder:
its one route renders a page from the session user and reads nothing from the request.

Feature deep-dives: [FLOW_BUILDER.md](FLOW_BUILDER.md),
[CHATBOT_AI_SETTINGS.md](CHATBOT_AI_SETTINGS.md) (per-agent prompt, prompt variables,
language-model choice, webhook actions and flow attachment),
[CHATBOT_ANALYTICS.md](CHATBOT_ANALYTICS.md) (per-turn performance logging and the
dashboard over it), [AI_INBUILT.md](AI_INBUILT.md),
[QUERY_JOINS.md](QUERY_JOINS.md) (joining several tables into one authored query, in both
places a query is built), [SQL_ASSIST.md](SQL_ASSIST.md) (Ask AI — plain English to SQL from
reflected schema, never from the data; and Auto Create Tool, which saves the result as a tool
config that reopens fully editable in the query builder),
[DEEP_AGENTS.md](DEEP_AGENTS.md) (running a data agent's tool configs as real queries so a
chatbot answers from tool results and the language model never reads the database),
[DOCKER_AND_LOCAL_LLM.md](DOCKER_AND_LOCAL_LLM.md) (why the app runs in a container on
Python 3.12, and how the in-built Ollama models are configured and measured),
[SCHEMAS.md](SCHEMAS.md) (the Pydantic layer — one package per feature, every request parsed
through a request schema and every response built from a response schema; the error bridge
that keeps a Pydantic failure from reaching a user verbatim, and where a rule belongs when it
is split between a schema and a service),
[TESTING.md](TESTING.md) (the test suite and its coverage ratchet — why the tests run in the
container against an SQLite database, the four type shims that makes possible, how an
authenticated route is reached, and the timestamped run history).

Four objects the sidebar exposes separately, because their ownership differs:

| Sidebar entry | Path | Owned by | Attached to |
|---|---|---|---|
| **Agents** | `/chatbot-settings` | user | — (each agent *has* a prompt, a flow, actions) |
| **Chatbot Analytics** | `/chatbot-analytics/` | user | read-only view over every agent's turns |
| **Flow Builder** | `/flow-builder/` | user | at most one agent |
| **Actions** | `/actions` | user | any number of agents |

"Agents" is the user-facing name for `ChatbotApiKey`; its routes and templates still live under
`chatbot_settings` / `chatbot/`.

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
