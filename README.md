# GetMyStuff

An enterprise-grade AI-powered analytics platform. Connect the databases and files a business
already has, and let people ask questions of them in ordinary English — in a chat window, on a
website, without writing SQL and without waiting for a dashboard to be built.

## The one design decision everything else follows from

> **The AI can only ever run a query a human wrote or approved. It cannot see your schema,
> cannot write SQL, and cannot invent a number.**

Most "chat with your data" tools hand a model a sample of your rows and hope it reasons
correctly. Ask such a tool for a total and it computes one from whatever rows were sampled —
an answer's clothes over a guess. Here, *you* write the queries, or approve the ones the AI
drafts, and the model's only power is choosing which approved query to run.

That single constraint is why the rest of the product looks the way it does. It is explained
in full in [USER_GUIDE.md §2](documentations/USER_GUIDE.md) and argued from the code in
[ENGINEERING_TECHNOLOGY.md §10](documentations/ENGINEERING_TECHNOLOGY.md).

## Core capabilities

- **Multi-source connectivity** — PostgreSQL, MySQL, SQLite, MongoDB, and uploaded CSV, XLSX,
  JSON, Parquet and Avro files
- **Approved-query analytics** — tool configs an operator authors in a query builder or raw
  SQL, with joins, nesting, per-value iteration and a shared guard on what may run
- **Ask AI** — plain English to SQL from reflected schema only, never from the data, saveable
  as a tool config
- **Data agents** — a chatbot answers from tool results, run as a LangGraph; the model reads
  rows, never the database
- **Embeddable chatbot widget** — with a visual conversation-flow builder, a local knowledge
  base for AI fallback, and Markdown rendering so a result arrives as a real table
- **Pipelines** — a canvas where a LangGraph is drawn out of statements, values, tools,
  branches, loops and questions put to a person, then run and watched node by node
- **Integration Platform** — drawn workflows that move records into systems this application
  does not own, published as frozen versions a schedule runs; Shopify (read) and Brevo
  (read/write) connectors
- **Large results** — whole-result aggregation in memory, and an offer to send the full set as
  a CSV, Excel or Parquet file when it is too big to print
- **Email dispatch** — SMTP servers, templates, event and webhook triggers, one permanent row
  per message
- **Per-turn analytics** — token and timing cost recorded for every conversation turn

## Tech stack

| Layer | Technology |
|---|---|
| Runtime | Python 3.12, in Docker |
| Backend | Litestar 2.21 + `litestar-htmx`, uvicorn, SQLAlchemy (asyncio), Alembic |
| Frontend | HTMX, Bootstrap 5, HTML5, CSS3 — server-rendered fragments, no SPA framework |
| Databases | PostgreSQL (+ pgvector), MySQL, SQLite, MongoDB |
| Data processing | Pandas, PyArrow, polars, fastavro, openpyxl |
| AI | LangGraph, LangChain, `deepagents`, Ollama (local), Anthropic, OpenAI |
| Validation | Pydantic — one schema package per feature |
| Secrets | `cryptography` (Fernet) for every stored credential |

Python 3.12 is a hard floor, not a preference: `deepagents` uses 3.11 syntax and every one of
its published releases declares `>=3.11`. See
[DOCKER_AND_LOCAL_LLM.md](documentations/DOCKER_AND_LOCAL_LLM.md).

## Running it

```bash
docker compose up -d
```

The app serves on `http://localhost:8003`. Alembic runs `upgrade head` on startup, so the
schema is built and migrated by the app itself — see
[MIGRATIONS.md](documentations/MIGRATIONS.md).

**Two environment variables are required, and the process refuses to start without them.**
That refusal is deliberate: neither may ever fall back to a guessable default.

| Variable | What it is | Generate with |
|---|---|---|
| `JWT_SECRET_KEY` | Signs session tokens. `app/db/auth/auth.py` raises at import when unset. | `python -c "import secrets; print(secrets.token_urlsafe(64))"` |
| `FERNET_KEY` | Encrypts every stored credential — datasource passwords, AI provider keys, action headers, OAuth tokens. `app/utils/crypto.py` raises at import when unset. | `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |

The two commands are not interchangeable: a Fernet key must be a 32-byte urlsafe-base64 value,
so a `token_urlsafe` string will be rejected as malformed.

Put each in `.env`, which is gitignored so every environment needs its own. **Read
[SECRETS_AND_KEY_ROTATION.md](documentations/SECRETS_AND_KEY_ROTATION.md) before changing
`FERNET_KEY`** — changing it on its own is not a rotation, it is a data-loss event that
surfaces hours later as "this action's saved headers could not be read".

Optional variables configure the local Ollama models, the download/email/integration workers
and the export ceilings; the per-feature docs below cover each.

## Tests

The suite runs **in the container**, against SQLite — 6,045 tests. A local 3.10 virtualenv
cannot import `deepagents`, so 17 test modules fail to collect there and 246 tests never run.
That is an environment artefact, not a failure: run the suite in the container before
concluding anything from a red result.

```bash
# the suite, while iterating
docker compose exec -T app python -m pytest tests/ -q --no-cov

# suite + coverage + timestamped report
bash .claude/skills/full-test-coverage/scripts/run_coverage.sh
python3 .claude/skills/full-test-coverage/scripts/make_report.py --tests-exit-code $?
```

There is **no JavaScript test runner in this repository**, which is a real gap: the canvas
code is covered only by Python tests asserting script order, plus manual checklists in the
canvas docs. See [TESTING.md](documentations/TESTING.md).

## Project structure

`db/`, `models/`, `routes/`, `services/` and `schemas/` are each organized into per-feature
subfolders, named identically across every layer a feature appears in. A feature adds a
subfolder only to the layers it actually needs — `query_test/` and `tool_graphs/` own no
tables, `deep_agents/` owns none either, and `agent_recursive_dataframes/` persists nothing at
all. Shared infrastructure stays at the top level of its layer.

```
app/
├── db/          base.py, db_sessions.py, db_utils.py, models.py + per-feature subfolders
├── models/      user/, datasource/, chatbot/, flow_builder/, graph_designer/,
│                integrations/, email_dispatch/, file_delivery/, tool_configs/, …
├── routes/      auth/, dashboard/, datasource/, chatbot/, flow_builder/, graph_designer/,
│                tool_configs/, sql_assist/, deep_agents/, downloader_agents/,
│                integrations/, email_dispatch/, file_delivery/, …
├── services/    the same features, plus canvas_layout/ — one layered layout shared by two
│                canvases and owned by neither
├── schemas/     base.py, common.py + one package per feature
├── utils/       crypto, validators, sql_guard, query_joins, events (the in-process bus),
│                file/CSV/Parquet helpers, turn_recorder
├── templates/   Jinja templates, grouped by feature
└── static/      css/, js/
```

**All CRUD goes through `db/db_utils.py`.** Business logic lives in services, never routes.
Every model carries an internal `id` (BigInteger PK, used for joins and foreign keys) and a
public `uuid` — and only the `uuid` may ever reach a URL, a template or a JSON response.
`CLAUDE.md` is the authoritative rule set;
[ARCHITECTURE.md](documentations/ARCHITECTURE.md) explains which layers each feature
deliberately lacks and why.

## Documentation

37 documents in [documentations/](documentations/). Two of them are consolidated and sit above
the rest.

**Start here**

| Document | What it is |
|---|---|
| [USER_GUIDE.md](documentations/USER_GUIDE.md) | The product as workflows, in plain language — what each object is, the order you build them in, every limit in one table, and what each refusal means. The one page to give somebody who has to operate this without reading the source. |
| [ENGINEERING_TECHNOLOGY.md](documentations/ENGINEERING_TECHNOLOGY.md) | The engineering record — every dependency and what forced it, the module topology, the four LangGraph runtimes compared, the security argument behind query execution, a 35-point invariant checklist, and every alternative tried and rejected. Read before changing anything that crosses two features. |
| [ARCHITECTURE.md](documentations/ARCHITECTURE.md) | The layered architecture and the annotated map of every document below. |

**Authoring queries**

| Document | What it is |
|---|---|
| [TOOL_CONFIGS.md](documentations/TOOL_CONFIGS.md) | The how-to for the Tool Configs page — every field, one worked example per scenario, and every refusal with its fix. Also served in-app at `/tool-configs/help`. |
| [TOOL_QUERY_MODES.md](documentations/TOOL_QUERY_MODES.md) | The two ways a tool holds its query — builder and raw SQL — and `sql_guard`, the one definition of a statement this application will run. |
| [QUERY_JOINS.md](documentations/QUERY_JOINS.md) | Joining several tables into one authored query, in both places a query is built. |
| [QUERY_TEST.md](documentations/QUERY_TEST.md) | The **Test Query** button — running an unsaved query once, so a query the database refuses is found while the form is open. |
| [TOOL_CHAINING.md](documentations/TOOL_CHAINING.md) | Nested tools — a tool embeds others as sub-queries, run inside-out as a LangGraph that stops the moment a level matches nothing. |
| [TOOL_CHAIN_ITERATION.md](documentations/TOOL_CHAIN_ITERATION.md) | The other two shapes: running a parent once per value, and a `:name` the assistant fills in. |
| [SQL_ASSIST.md](documentations/SQL_ASSIST.md) | Ask AI — plain English to SQL from reflected schema, never from the data — and Auto Create Tool. |
| [TOOL_GRAPHS.md](documentations/TOOL_GRAPHS.md) | The read-only canvas over both — a nested tool drawn as the graph it compiles to, and its joins drawn as sets. |

**Agents, models and the widget**

| Document | What it is |
|---|---|
| [DEEP_AGENTS.md](documentations/DEEP_AGENTS.md) | Running a data agent's tool configs as real queries, so the model sees rows and never the database. |
| [DOWNLOADER_AGENTS.md](documentations/DOWNLOADER_AGENTS.md) | A hundred printed rows, the exact `COUNT(*)`, and the rest as a file — a checkpointed LangGraph that pauses on the user's "yes". |
| [AGENT_RECURSIVE_DATAFRAMES.md](documentations/AGENT_RECURSIVE_DATAFRAMES.md) | Totals and averages over an entire result set, folded in memory with polars as a map-reduce; aggregations with no exact fold are refused rather than approximated. |
| [CHATBOT_AI_SETTINGS.md](documentations/CHATBOT_AI_SETTINGS.md) | Per-agent prompt, prompt variables, model choice, webhook actions and flow attachment. |
| [CHATBOT_ANALYTICS.md](documentations/CHATBOT_ANALYTICS.md) | Per-turn token and timing logging, and the dashboard over it. |
| [WIDGET_RENDERING.md](documentations/WIDGET_RENDERING.md) | How an answer becomes what a visitor sees — escaping the model's text *before* any parsing, so every tag is one the renderer wrote. |
| [AI_INBUILT.md](documentations/AI_INBUILT.md) | The local Ollama + pgvector knowledge-base pipeline behind AI fallback. |
| [DOCKER_AND_LOCAL_LLM.md](documentations/DOCKER_AND_LOCAL_LLM.md) | Why the app runs in a container on 3.12, and how the local models are configured and measured. |

**Things you draw**

| Document | What it is |
|---|---|
| [FLOW_BUILDER.md](documentations/FLOW_BUILDER.md) | The drawn conversation the widget walks before the AI gets a turn — twelve blocks, a visitor's session as a flat string map, and the call stack that lets one flow run another. |
| [GRAPH_DESIGNER.md](documentations/GRAPH_DESIGNER.md) | **Pipelines** — a LangGraph drawn out of statements, values, tools, branches, loops and questions, run whole or one node at a time; once published, four different things can run it. |
| [CANVAS_LAYOUT.md](documentations/CANVAS_LAYOUT.md) | What decides where the blocks go — a layered arrangement computed in Python, because layout can be wrong without looking wrong. |
| [CANVAS_SELECTION.md](documentations/CANVAS_SELECTION.md) | Rubber-band select, Ctrl+A and moving many boxes as one across all three canvases, plus routing a connector by hand — and the drag-repaint fix that made a group move possible. |
| [FILE_NODES.md](documentations/FILE_NODES.md) | Create File and Download File — what reaches the file is everything or the block fails, and why the button is not a result type. |

**Integration Platform**

| Document | What it is |
|---|---|
| [INTEGRATIONS.md](documentations/INTEGRATIONS.md) | The engine — a drawn workflow published as a frozen version, deterministic at run time so a run can be audited and replayed, with a queue that survives a restart and three grains of failure never conflated. |
| [INTEGRATIONS_AI.md](documentations/INTEGRATIONS_AI.md) | The AI layer, kept separate because the interesting part is what a model is *not* allowed to write. |
| [SHOPIFY_CONNECTOR.md](documentations/SHOPIFY_CONNECTOR.md) | The first vendor connector — read-only Admin GraphQL, and the four runtime seams built for this moment that had never executed, two of them broken. |
| [BREVO_CONNECTOR.md](documentations/BREVO_CONNECTOR.md) | The first connector that **writes** — how one literal, `updateEnabled: true`, makes `idempotent=True` a fact rather than a claim. |

**Notifications**

| Document | What it is |
|---|---|
| [EMAIL_DISPATCH.md](documentations/EMAIL_DISPATCH.md) | SMTP servers, templates, triggers and one permanent row per message — rendering at *enqueue* so the log says what was actually sent, and a dead worker that fails its message rather than resuming it. |
| [EVENT_BUS.md](documentations/EVENT_BUS.md) | The in-process publish/subscribe the email triggers ride on — strict on subscribe, forgiving on publish, and why that asymmetry is right. |

**Cross-cutting conventions**

| Document | What it is |
|---|---|
| [SERVICE_PATTERNS.md](documentations/SERVICE_PATTERNS.md) | What belongs in a service, and the one reading of the per-table and per-column switches set in Data Sources. |
| [SCHEMAS.md](documentations/SCHEMAS.md) | The Pydantic layer, the error bridge that keeps a validation failure from reaching a user verbatim, and where a rule belongs when it is split between a schema and a service. |
| [ERROR_HANDLING.md](documentations/ERROR_HANDLING.md) | Custom exception types, what may reach a user versus what stays in the log, and the embedded widget — the one component whose failures the server cannot see. |
| [HTMX_PATTERNS.md](documentations/HTMX_PATTERNS.md) | Swap patterns, panel-local error banners, and the global handler that lets a non-2xx response display at all. |
| [SECRETS_AND_KEY_ROTATION.md](documentations/SECRETS_AND_KEY_ROTATION.md) | The one symmetric key every stored credential uses, and the step ordering that separates a background re-encryption from every secret becoming unreadable. |
| [MIGRATIONS.md](documentations/MIGRATIONS.md) | Alembic applied at startup, why `create_all` was removed after it silently skipped a new column, and the three database states. |
| [TESTING.md](documentations/TESTING.md) | The suite and its coverage ratchet — why it runs in the container against SQLite, and how an authenticated route is reached. |

## Contributing

`CLAUDE.md` is the authoritative development guide and overrides anything inferred from
surrounding code. Its standing rules, in short: no silent failures — every failure raises with
a human-readable message; all CRUD through `db_utils.py`; no business logic in routes;
validate on both the client and the server; never expose a stack trace; and where existing
project patterns conflict with something new, **the existing pattern wins**.
