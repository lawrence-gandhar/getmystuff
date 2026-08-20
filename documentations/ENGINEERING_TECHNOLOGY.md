# ENGINEERING_TECHNOLOGY.md

**The engineering record: what was built, how it works, and why it was built that way.**

This is the technical companion to [USER_GUIDE.md](USER_GUIDE.md). Where that document
explains the product as workflows, this one explains the machine: the dependency choices and
what forced them, the module topology, the four LangGraph runtimes and how they differ, the
security argument behind query execution, the concurrency model, the failure taxonomy, and
the decisions that were tried and rejected.

It is written to be read by somebody who has to change this code. Every claim here is
traceable to a per-feature document, linked at the point it matters; those documents remain
the authority on their own feature and go deeper still.

---

## Contents

**Foundations**
1. [The engineering thesis](#1-the-engineering-thesis)
2. [The stack, and what forced each choice](#2-the-stack-and-what-forced-each-choice)
3. [Runtime and container topology](#3-runtime-and-container-topology)
4. [Module topology and the layering contract](#4-module-topology-and-the-layering-contract)
5. [Identifier discipline](#5-identifier-discipline)
6. [The validation layer](#6-the-validation-layer)
7. [Data access](#7-data-access)
8. [The datasource status cascade](#8-the-datasource-status-cascade)

**The query core**
9. [How a query is represented](#9-how-a-query-is-represented)
10. [The executor, and the security argument](#10-the-executor-and-the-security-argument)
11. [Tool chaining and iteration](#11-tool-chaining-and-iteration)

**LangGraph**
12. [LangGraph in this codebase: four graphs, one set of rules](#12-langgraph-in-this-codebase-four-graphs-one-set-of-rules)
13. [Deep Agents](#13-deep-agents)
14. [Downloader Agents](#14-downloader-agents)
15. [Whole-result aggregation](#15-whole-result-aggregation)
16. [Graph Designer](#16-graph-designer)

**The AI surfaces**
17. [SQL Assist](#17-sql-assist)
18. [Query Test](#18-query-test)
19. [Flow Builder and the conversation engine](#19-flow-builder-and-the-conversation-engine)
20. [The in-built LLM path](#20-the-in-built-llm-path)
21. [The chatbot turn pipeline](#21-the-chatbot-turn-pipeline)

**Delivery**
22. [The embeddable widget](#22-the-embeddable-widget)
23. [Frontend engineering](#23-frontend-engineering)
24. [Tool Graphs, and derived-versus-authored](#24-tool-graphs-and-derived-versus-authored)
25. [Error handling architecture](#25-error-handling-architecture)
26. [Migrations](#26-migrations)
27. [Testing architecture](#27-testing-architecture)

**Closing**
28. [Cross-cutting invariants](#28-cross-cutting-invariants)
29. [Decision log: tried and rejected](#29-decision-log-tried-and-rejected)
30. [Known gaps](#30-known-gaps)
31. [Reading map](#31-reading-map)

---

# 1. The engineering thesis

Everything in this codebase follows from one sentence:

> **No text produced by a language model ever enters a query.**

That is not a policy enforced by review. It is a structural property, and it is worth being
precise about how it is achieved, because the naive reading ("we validate the model's SQL")
is exactly what this design rejects.

There are three ways a model could influence a database read, and each is closed differently:

| Vector | How it is closed |
|---|---|
| The model writes SQL | It is never asked to. A tool's query is authored by a human, stored, and re-validated on every run |
| The model chooses a column, table or operator | It cannot. Those come from the stored config and are resolved against reflected schema objects |
| The model supplies a value | Permitted, **only** where an operator explicitly opened one comparison — and the value is bound as a parameter, never interpolated |

The one place a model *does* supply request payloads — download tool arguments and
aggregation plans — those payloads go through the same Pydantic schema layer as a browser
request, because a model is exactly as untrusted as a browser: it invents uuids, proposes
formats that do not exist, and passes the word "latest".

### The second thesis: refuse rather than approximate

The first thesis is about security. The second is about correctness, and it shapes far more
code:

> **A plausible wrong number is the worst available outcome, so every bound refuses rather
> than truncating.**

Rows from the first fifty of eighty departments are indistinguishable from rows for all
eighty. A total over them is a number that survives review. So:

- `TOOL_CHAIN_MAX_ITERATIONS` (50) refused — a truncated *union* is short of whole
  iterations, and "200 rows" tells a model nothing about six departments being missing.
- `AGGREGATE_MAX_GROUPS` (100,000) aborts and **discards** the running aggregate — a list of
  the first hundred thousand groups looks exactly like a complete answer.
- A loop ceiling in Graph Designer stops the run and names the node.
- `RIGHT JOIN` in builder mode is refused rather than substituted with a left or full outer
  join, because substitution would quietly change which rows come back.
- Median, percentile, mode and count-distinct are refused rather than approximated.
- A reference to a switched-off column **fails the tool** rather than being dropped from the
  query.

**The strongest form of that thesis is having no bound at all, and two were removed for it.**
A tool query was capped at 200 rows (`MAX_TOOL_ROWS`) and an inner tool's `IN` list at 2,000
values (`MAX_CHAIN_VALUES`). Neither could be made honest, because both stood between somebody
and their own data: 200 rows of 5,275 is an ordinary-looking answer, and a 2,000-value filter
answers a *different question* than the one asked. Refusing was the correct response to the
second, and it meant a tool about a lot of records simply could not be embedded. Now every
query returns every matching row and every value crosses every edge.

The one place a row count is still cut is the **prompt** — `PROMPT_ROW_LIMIT` (200) in
`describe_result` — because a context window is a physical size and the alternative is a turn
that fails outright rather than a longer answer. Reading every row is what makes that honest:
the header states the exact total beside the sample (`200 row(s) out of 5275 matching
record(s)`), where the old text could only warn that a total was unknowable.

### The third thesis: two audiences, one fault

Every failure has to be phrased for whoever can act on it, and those are different people.
`ToolQueryError` carries the fault as its message and the instruction separately in `advice`:

```python
raise ToolQueryError(inactive_column_message("orders.total"))   # the fault
exc.for_agent   # the fault + "Tell the user the tool needs reconfiguring."
```

`tool_factory` renders `for_agent`, because a model handed a bare fault tries to work around
it. `probe_tool_query` (the Test Query path) renders `str(exc)`, because the operator reading
it *is* the user somebody would be told to tell. The same split governs driver errors:
`execute_tool_query` substitutes "the query could not be run against the database" because a
driver message can name schema objects and echo values and it is going into a prompt;
`probe_tool_query` lets it through untouched because *"Unknown column 'crm_id' in 'on
clause'"* is the only sentence that says where to look.

The third audience is the one with no stake at all: a **visitor** on somebody else's website.
The blocking chatbot turn has always known that — `chatbot_reply_service` degrades a failed
agent to a data-profile answer, or to a sentence naming no system the visitor can see. The
streamed turn could not, because `_stream_as_agent` cannot raise once the status code is
sent, so it reports every failure as an `error` event the widget paints verbatim; a chatbot
whose agent had no tools was showing the public *"Add a tool for it in the Tool Configs
section."* The fix is not a second wording but a marker for the one class of failure that can
be re-routed: `"stage": "setup"` means nothing ran, so `chatbot_turn_service.stream_turn`
declines to stream and lets the blocking path's degradation answer instead. Errors raised
after real work stay where they are — retrying those bills the owner twice for one question.
See [FLOW_BUILDER.md](FLOW_BUILDER.md).

---

# 2. The stack, and what forced each choice

| Layer | Choice | Why this one |
|---|---|---|
| Web framework | **Litestar 2.21.1** | Controller classes with class-level `dependencies`, native async, typed path params (`{id:uuid}`) |
| ORM / Core | **SQLAlchemy 2.0.51** | Two capabilities this design depends on: `Table(autoload_with=…)` reflection, and Core `Select` assembled from `Column` objects — see §10 |
| Primary driver | **asyncpg** | The app's own PostgreSQL traffic |
| Second driver | **psycopg 3.3.4** | Forced by `langgraph-checkpoint-postgres` — see below |
| Migrations | **Alembic 1.18.5** | Applied by the app at startup, not by a deploy step — see §26 |
| Agent runtime | **deepagents 0.7.1** over **langgraph 1.2.10** | The tool-calling loop; the reason the runtime is 3.12 |
| Providers | **langchain-anthropic 1.5.3**, **langchain-openai 1.4.1**, **langchain-ollama 1.1.0** | Uniform chat-model interface for the agent path only |
| Local models | **Ollama**, native REST | No SDK, no LangChain shim on the non-agent path — see §20 |
| Vectors | **pgvector** (`pgvector/pgvector:pg16`) | 768-dim embeddings with an HNSW index in the same database as everything else |
| Dataframes | **pandas**, **pyarrow**, **polars** | Three, deliberately — see below |
| Frontend | **HTMX**, **Bootstrap 5**, **D3 7** (one page) | Server-rendered fragments; no SPA framework |

### Why three DataFrame libraries

This looks like accretion and is not. Each is load-bearing for a different reason.

- **pandas** — file ingestion and the statistical profile on the non-agent chatbot path.
- **pyarrow** — Parquet part-writing and merging in exports. Explicitly *not* pandas there:
  `pd.concat` over every part would hold the entire export in memory (the thing the feature
  exists to avoid) and would turn an integer column containing a NULL into floats, so
  `qty: 3` becomes `3.0` and the file quietly disagrees with the answer the agent gave.
- **polars** — the aggregation fold, and the reason is the GIL. `group_by`/`agg` run in Rust
  **with the GIL released**, so several slices genuinely fold concurrently under
  `asyncio.to_thread`. pandas holds the GIL through `DataFrame.from_records` over dicts and
  through string-key factorisation, which would serialise the fan-out and leave the wave
  pattern doing nothing at all.

Two operational notes on polars: `AGGREGATE_WAVE_WIDTH` threads × polars' own Rayon pool is
`4 × N` threads on an N-core box, so `POLARS_MAX_THREADS` may want setting — polars reads it
at **import**, so it cannot be set from Python inside the module. And the default wheel
targets a modern x86-64 baseline; on an older host it dies with `Illegal instruction` and
**no Python traceback**, where the fix is `polars-lts-cpu`.

### Why two PostgreSQL drivers

Downloader Agents pauses a LangGraph run on `interrupt()` while it asks the user whether they
want a file. That pause has to survive the gap between two chat turns *and* be resumable from
the queue worker rather than from the request that created it. So the checkpoint store has to
be the database, and `AsyncPostgresSaver` is langgraph's own store — built on psycopg 3,
unable to use the asyncpg engine.

The alternative was writing a checkpointer over the existing SQLAlchemy session: roughly two
hundred lines of `aput`/`aget_tuple`/`alist`/`aput_writes` that we would own and have to keep
correct against langgraph's evolving protocol. **A second driver against the same database is
the cheaper honesty.**

Containment: only `app/services/downloader_agents/base/checkpointer.py` imports psycopg. Its
pool is two connections at most, because it serves checkpoint writes rather than traffic. It
falls back to langgraph's in-memory saver whenever `DATABASE_URL` is not PostgreSQL — which
is what keeps the SQLite test suite working — and **the choice is made from the DSN rather
than from a setting**, so you cannot end up on the in-memory saver while pointed at a real
database.

LangGraph creates its own tables (`checkpoints`, `checkpoint_blobs`, `checkpoint_writes`,
`checkpoint_migrations`) through its own `setup()`, not through Alembic. The schema belongs to
langgraph and changes when langgraph changes — see §26 for how autogenerate is kept from
proposing to drop them.

### Why LangChain is quarantined

`langchain*` is imported **only** inside `app/services/deep_agents/`,
`app/services/graph_designer/graph_compiler.py`, and the two other graph modules. Nothing
else in the application changed provider library.

The reason is that `ai_analytics_service` has three provider implementations and **forces
structured JSON output on all of them**, which collides with native tool-calling. Rather than
rewrite three provider paths to support tools, the agent path got its own model factory that
reuses only the *provider decision* (`resolve_provider()`, promoted from private for exactly
this). Precedence is therefore identical everywhere: a pinned key, then the user's active keys
in provider-priority order, then the environment key.

---

# 3. Runtime and container topology

### Why Python 3.12, and why in a container

The forcing constraint is one import:

```
$ python3.10 -c "from typing import Required"
ImportError: cannot import name 'Required' from 'typing'
```

`typing.Required` landed in 3.11, and **all 113 published `deepagents` releases** (0.0.1 →
0.7.1, including every rc and alpha) declare `requires_python: <4.0,>=3.11`. There is no older
version to pin back to; extracting the wheel into a 3.10 `site-packages` by hand reproduces
the `ImportError`, confirming the metadata is honest rather than conservative.

Everything *else* the feature needs supports 3.10 — verified by installing langgraph,
langchain-core and all three provider packages into a scratch 3.10 venv, where they import
cleanly. `deepagents` was the single blocker.

Containerising rather than upgrading the host solved four problems at once: the working 3.10
venv is untouched, the interpreter is not the host's problem, PostgreSQL arrives correctly
configured as `pgvector/pgvector:pg16`, and Ollama is versioned with the app rather than being
whatever the host happens to have.

| Service | Image | Host port | Notes |
|---|---|---|---|
| `app` | `python:3.12-slim` (reports 3.12.13) | 8003 | `--reload`, source bind-mounted |
| `db` | `pgvector/pgvector:pg16` | **5433** | Deliberately not 5432, so it cannot collide with a local PostgreSQL |
| `ollama` | `ollama/ollama:latest` | 11435 | Its own model volume |
| `ollama-init` | one-shot | — | Waits, pulls chat + embed models, exits. `app` gates on `service_completed_successfully` |

`db` has a `pg_isready` healthcheck and `app` waits on it, because `on_startup` migrates the
schema immediately and would otherwise race the database.

### Three container fixes that were required

**`alembic.ini` hardcodes a localhost URL.** Inside a container that host does not resolve, so
`alembic upgrade head` could not run at all. `alembic/env.py` now prefers `DATABASE_URL`,
falling back to `alembic.ini` — the same precedence `db_sessions.py` uses, so the app and its
migrations can no longer point at different databases.

**Compose env must win over `.env`.** `load_dotenv()` does not override variables already
present in the real environment, so `docker-compose.yml`'s `environment:` block takes
precedence while the same `.env` keeps working unchanged. That is what lets `DATABASE_URL`
point at `db:5432` and `OLLAMA_BASE_URL` at `ollama:11434` without editing `.env`.

**The local venv must not leak into the image.** `venv/` is in `.dockerignore`, *and* compose
masks it with an anonymous volume (`- /app/venv`) — a 3.10 tree bind-mounted over the image's
3.12 `site-packages` would shadow every installed package.

### Where files live, and one trap

```python
UPLOAD_BASE   = Path("app/uploads")               # → /app/app/uploads  (the bind mount!)
EXPORT_BASE   = Path("uploads/exports")           # → /app/uploads      (the named volume)
DOWNLOAD_BASE = Path("uploads/file_downloaders")  # → /app/uploads
```

Note the asymmetry. `docker-compose.yml` mounts the named `uploads` volume at `/app/uploads`,
while `UPLOAD_BASE` resolves to `/app/app/uploads` — *inside* the `.:/app` bind mount, i.e.
the host's source tree. Datasource uploads landing there is pre-existing behaviour; generated
exports must not, so the two export bases point at the volume that actually survives a
rebuild.

**Neither is under `static/`,** and this is worth being blunt about because it looks like the
obvious simplification. `main.py` mounts `static/` with no authentication at all: a file placed
there is fetchable by anyone with the URL — no key, no session token, no expiry check, because
a static mount bypasses the route that enforces all three. See §14 for the full authorisation
argument.

### Dev-only startup seeding

`on_startup` runs `alembic upgrade head`, then seeds `admin@test.com` / `admin123`. That
exists because the compose stack has its own `pgdata` volume: a fresh volume means an empty
`users` table, and every login then bounces back to the form as "Invalid credentials" with
nothing in the logs to say the account was simply never created.

**The seed is DEV ONLY** and goes away in favour of real provisioning. The migration beside it
does not — applying the schema at startup is the production-shaped half of that hook.

---

# 4. Module topology and the layering contract

```
Client → Routes → Services → db/ helpers → Database
```

Four rules, and the third is the one that gets violated by accident:

1. **Routes** handle HTTP, parse a request schema, call a service, render a response. No
   business logic.
2. **Services** hold every rule. They never touch a request object and never return HTML.
3. **All CRUD goes through `db/`.** No hand-written SQL in a route or a service.
4. **Shared rules live in `utils/`**, never in a sibling feature.

### The per-feature folder rule

`db/`, `models/`, `routes/`, `services/` and `schemas/` are each organised into per-feature
subfolders, named identically across every layer a feature appears in. `models/` and `routes/`
subpackages re-export their public symbols from `__init__.py`; `services/` subpackages keep an
**empty** `__init__.py` because service callers import specific functions by full module path.

**A new module always gets its own same-named folder, in every layer it needs — even when only
one existing feature calls it.** Being called from a feature is not a reason to live inside it;
the folder boundary tracks what the module *is*, not who calls it.

`sql_assist` is the reference case: the Ask AI panel is opened from the Tool Configs page, but
generating SQL from a schema needs a datasource and nothing else, so it lives in its own
service, route and template folders. Tool Configs *calls* it; it does not belong to it.

### Which layers a feature deliberately lacks

This is documentation, not omission. Each absence is an assertion about the feature:

| Feature | Missing | Because |
|---|---|---|
| `deep_agents` | `models/` | Runs what Data Agents and Tool Configs already define; owns no table |
| `agent_recursive_dataframes` | `models/`, `db/` | Nothing is persisted at all — a run lives inside one request — and the single column it needs belongs to `tool_configs` |
| `query_test` | `models/`, `db/` | Stores nothing: runs a query the user is still writing and reports what the database said |
| `tool_graphs` | `models/`, `db/` | Only draws tool configs that already exist; a node's position is **computed on every request** rather than stored |
| `graph_designer` | — (owns three tables) | The deliberate opposite: its graph is *authored*, so the drawing — positions included — is the source of truth and nothing can recompute it |
| `downloader_agents` | `templates/` | No pages; reaches the user through an agent's conversation plus two download routes |
| `dashboard` | `schemas/` | One route rendering a page from the session user; reads nothing from the request |

### The one service folder with subfolders

`services/downloader_agents/` splits into `base/`, `csv/`, `xls/` and `parquet/`. The split is
by **format** because that is the only axis along which those three genuinely differ — a CSV
merge concatenates bytes, a Parquet merge appends row groups, an XLSX merge has to rewrite the
workbook — and it keeps `base/` structurally unable to know which format it is building.
`base/part_writer.py` defines the contract (`extension`, `media_type`, `write_part`,
`merge_parts`) and resolves a format's module lazily by name.

### Dependency direction, enforced by convention

`flow_builder → chatbot, ai_settings, ai_analytics, ai_inbuilt`. Never the reverse. The one
edge pointing back is the attachment UI: `ChatbotSettingsController` calls `flow_service` to
render an agent's flow dropdown — a **routes-layer** dependency only, so the service-layer
direction stays one-way.

`ai_inbuilt` takes plain scalars (`document_id`, `knowledge_base_id`, `content`) rather than
ORM instances, and its `KnowledgeChunk` foreign keys reference the flow-builder tables **by
table name only** (resolved lazily via `Base.metadata`), so it has no Python import dependency
on `app.models.flow_builder` at all.

`sql_assist → tool_configs`, never the reverse. Only `create_tool_from_draft` and
`draft_tool_config` touch `tool_config_service`; `generate_sql` does not, so the panel stays
usable on a page that has nothing to do with tool configs.

Two service-to-service circularities were resolved by moving the shared query down rather than
by importing across: `get_or_create_ai_settings` lives in `app/db/chatbot/queries.py` because
both `chatbot_service` (creating the row with the chatbot) and `chatbot_ai_settings_service`
(reading/updating it) need it. And `chatbot_reply_service` sits *above* `chatbot_service`
rather than beside it, so composition flows one way.

---

# 5. Identifier discipline

Every model carries two identifiers with strictly separate jobs:

```python
id:   Mapped[int]       = mapped_column(BigInteger, primary_key=True, autoincrement=True)
uuid: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), default=uuid.uuid4,
                                        unique=True, index=True, nullable=False)
```

| | `id` | `uuid` |
|---|---|---|
| Used for | The PK, and **every** foreign key between tables | Every path param, form field, JSON key, DOM id |
| Rationale | Fast joins, small index footprint | Unguessable, and safe to expose |
| Ever sent to a browser | **Never** | Always |

The service pattern is fixed: accept the public `uuid`, resolve it once via
`CRUDQueryBuilder.get_by_uuid()` with an ownership filter, then use `row.id` for everything
internal.

```python
datasource = await datasource_crud.get_by_uuid(db, datasource_id,
                                               extra_filters={"user_id": user_id})
if not datasource:
    raise HTTPException(status_code=404, detail="Datasource not found")
# from here on, datasource.id
```

Two consequences worth knowing. Routes use the Litestar `:uuid` path type and a `uuid.UUID`
hint, never `:int`. And **this is asserted mechanically**:
`tests/unit/models/test_model_contracts.py` walks `Base.registry` and checks the rule against
*every* mapped model — bigint `id` primary key, unique indexed `uuid`, every foreign key
pointing at an `id`. A model added tomorrow is checked the day it lands, without anyone
remembering to write a test for it.

Two response schemas (`DatasourceFileView`, `KnowledgeBaseDocumentView`) use the **key name**
`id` because the frontend scripts read it under that name — the *value* in both is the public
uuid.

Ownership failures return **404, not 403**, because answering differently confirms the uuid is
real.

---

# 6. The validation layer

Before `app/schemas/` existed, validation lived in three places at once: a regex at the top of
a route module, an `if not x: raise HTTPException` inside a service, and a
`try: uuid.UUID(...) except ValueError` in a handler. The copies disagreed, and several failed
in ways a user could not act on:

- `?page=abc` fell back to page 1 — a broken link showed the wrong data **silently**;
- a malformed `subquery_configs` payload was swallowed into `[]`, discarding the query the
  user had just built, and the save **reported success**;
- a JSON body that parsed to a list turned `(body or {}).get("message", "")` into an
  `AttributeError` and a 500;
- a bad table/column status produced a bare `HTTPException(status_code=400)` — a failed
  request with no explanation on screen.

### The eight rules

1. **Validation errors are `HTTPException`, never Pydantic's `ValidationError`.** Every route
   renders `exc.detail` into a Bootstrap alert. `app.schemas.base` catches Pydantic's error and
   re-raises the project's own with a message built from the field's `title`. Nothing in
   `app/schemas/` may let a raw `ValidationError` escape.
2. **Only the first failure is reported.** A form posts every field at once; one missing value
   cascades. The user gets the one thing to fix.
3. **Schemas validate shape; services own business rules.** Required, length, pattern, type,
   enum → schema. Name taken, caller owns the row, datasource reachable → service, because
   those need the database. Where a rule is split, **the service half is authoritative and the
   schema deliberately lets the value through.**
4. **Public identifiers only.**
5. **`""` and `None` are not always the same thing.** Most fields treat blank as "no value"
   (`OptionalText` → `None`). But `AIApiKeyUpdateRequest` and `ChatbotUpdateRequest` use plain
   `Optional[str]` because absent means "leave it" and blank means "clear it" — collapsing
   them would turn "clear this base URL" into a silent no-op.
6. **Every field needs a `title`**, because it becomes the user-facing name in the message.
   That is why field names in this layer read as English.
7. **A multi-select must be declared** (`multi_fields = ("table_names",)`). Read as a single
   value, a query built against four tables silently becomes a query against one. Where the
   *order* carries meaning — a tool config's first table is its primary one — the list is
   filtered and de-duplicated but **never reordered**.
8. **Files are not schema-validated.** A file part is a stream, not a value; `form_to_dict`
   drops upload parts and the ingestion service that holds the bytes owns the extension and
   size rules.

### The base classes

| Class | Config that matters |
|---|---|
| `AppBaseSchema` | `str_strip_whitespace=True`, so a whitespace-only value reaches the required-check as empty |
| `RequestSchema` | `extra="ignore"` — HTMX forms carry fields a given handler does not want (the page's filter, hidden partial state); rejecting one would break every form on the site |
| `QueryRequest` | Every field must have a default, since a partial query string means "unfiltered", not "bad request" |
| `ResponseSchema` | `from_attributes=True`, so a schema builds straight from an ORM row |

A response that fails **its own** schema raises **500**, not 400 — a malformed response is
this application's defect, not the caller's — and the internal reason travels in the
exception's `extra` so it reaches the log without reaching the screen.

### Where the split is deliberately visible

The highest-value inputs in the application *pass* this layer on purpose, and there are tests
pinning that:

| Input | Schema guarantees | Service decides |
|---|---|---|
| `config_json` | A JSON object of bounded size | Every column/table reference, because only it has just reflected the tables |
| `sql_query` | Length, and `query_mode` ∈ a closed set | Whether it is a single read-only statement (shared with the executor and Ask AI via `sql_guard`) |
| `children_json` | Shape and count | Same owner, same datasource, enabled, no cycle, within depth caps, binding mode valid for the parent's statement |
| `sql_params_json` | Shape and count | Whether each name appears as a `:placeholder` in the statement |
| `AggregationPlan` | At most 4 group columns, 8 measures; only foldable functions | Whether the named columns exist in what the tool actually returns |

A second copy of the read-only guard in the schema layer would be **the copy nobody checks
against the one that runs at query time.** That is the whole argument.

### One conditionally-required field, done properly

`ChatbotCreateRequest.check_target` is the clearest case in the codebase. A widget reads either
a datasource target it nominates, or an attached data agent's tool configs — never both,
because two answers to "what can this reach?" would be resolved differently depending on
whether the agent happened to run. The rule spans three fields, so no single field can express
it, and it **rejects an `agent` target that also carries a datasource rather than quietly
dropping one of the two**. Three places have to agree: this validator,
`chatbot_service.create_chatbot_key`, and the form (which hides the datasource block and
de-requires the field the moment an agent is picked).

The layer is maintained by the `module-schemas` skill, whose audit script fails if a schema is
undocumented — so [SCHEMAS.md](SCHEMAS.md) and `app/schemas/` cannot drift apart.

---

# 7. Data access

### `db_utils.py` and `CRUDQueryBuilder`

Generic CRUD for the application's own tables: `get_by_uuid`, `get_many`, `create`, `update`,
`delete`, bulk inserts. `update()` and `delete()` take the bigint PK, which is why the service
pattern in §5 resolves the uuid first.

A feature gets a `db/<feature>/queries.py` only when generic CRUD **structurally cannot**
express what it needs. Two worked examples:

- `db/ai_inbuilt/queries.py` — `CRUDQueryBuilder.get_many` orders by a plain column-name
  string, so a pgvector distance `ORDER BY` is out of reach. Raw SQLAlchemy Core lives here
  rather than leaking into the service layer.
- `db/downloader_agents/queries.py` — `FOR UPDATE SKIP LOCKED` job claiming.

### Reflection versus hand-written catalogue queries

Both exist, deliberately, and `metadata_service` exposes both:

| Service function | Backed by | Used by |
|---|---|---|
| `get_rdbms_tables` / `get_table_schema` | Hand-written per-dialect SQL | Configurations page, Tool Config cascades, AI analytics |
| `get_rdbms_reflected_tables` / `get_rdbms_reflected_metadata` | SQLAlchemy `Inspector` | Ask AI, and the executor |

The reflected path was added **beside** the older one rather than replacing a hot path,
because several existing callers depend on the older output's exact shape. Migrating them
would be a behaviour change for multiple features and was left alone.

Reflection matters for one specific reason: `Inspector` emits its own dialect-correct catalogue
queries, so **there is no query string in this application for a table or column name to be
interpolated into**, and adding a dialect adds no branch. It is a sync API, so it runs inside
`conn.run_sync` on the async engine's connection.

`fetch_rdbms_table_names` returns tables **and views** — a view is as queryable as a table, so
it is an equally valid SELECT target. Keys and constraints are only requested for real tables:
a view has none, and some dialects raise rather than return empty when asked.

**Foreign keys are included** in reflected metadata because they are what make a generated join
correct rather than guessed. **Column defaults and comments are excluded**, because they can
carry literal values from the database and that path exists precisely to send nothing but
structure.

`MAX_REFLECTED_TABLES = 25`. Reflecting a table costs a catalogue round-trip *and* lands in a
prompt, so the count is bounded rather than "however many the caller asked for". The service
compares what it asked for against what came back and **names the difference** — a query
generated against three tables when the user picked four looks correct and is not.

---

# 8. The datasource status cascade

Two switches per object, both stored in `DataSource.configuration_data`:

```python
configuration_data = {
    "orders": {
        "status": "active",
        "column_data": {"id": {"column_name": "id", "status": "active"}},
    },
}
```

### The write side

`toggle_table_status_service()` **cascades in both directions**: every column is written to the
table's new status, whether that status is active or inactive. Both directions, because either
disagreement is incoherent — an *active* table whose columns are all inactive contributes no
data, so leaving columns alone on activation reads as the activation having silently done
nothing; an *inactive* table with active columns is the mirror image.

The consequence is deliberate: re-activating a table discards the per-column choices made
before it was switched off. The table switch is the coarse control and it wins.
`toggle_column_status_service()` enforces the same ownership from the other side, refusing to
activate a column while its table is inactive with a 400 and a readable message rather than a
silent no-op.

### The read side — `app/utils/datasource_status.py`

The switches are only worth having if every feature honours them, so the reading is done in one
shared module. It lives in `utils/` alongside `query_joins.py` and `sql_guard.py` for the same
reason: four features depend on it, so it belongs to none of them.

Four rules, all load-bearing:

1. **Absent means active.** Only the literal `"inactive"` switches something off. A missing
   entry, an empty `column_data`, a `configuration_data` of `None`, an unrecognised value — all
   active. Every datasource created before metadata collection worked has an empty
   `configuration_data`, so "active only if it says active" would empty every dropdown in the
   application for those users.
2. **Nothing raises.** `configuration_data` is user-written, hand-editable JSON, so an
   unrecognised shape reads as unconfigured rather than taking a page down. **Callers raise** —
   services as `HTTPException`, the executor as `ToolQueryError` — which is why the message
   strings live in this module and not in either of them (see §25).
3. **The cascade is re-applied on read.** `active_column_names` returns `[]` for an inactive
   table whatever its `column_data` says, because a row edited straight in psql can disagree
   with itself.
4. **The caller owns the list of names.** Every function filters names the caller read from the
   live database and never returns one it was not given, so a column dropped from the real
   table but still recorded in `configuration_data` is never offered.

### Where it is enforced, and what "enforced" means in each place

| Surface | Behaviour |
|---|---|
| Data Sources listing | **Opt-in filter, not a rule** — this is where switches are set, so it must show inactive tables |
| Tool Config pickers | Inactive tables and columns are **not offered at all**. All-inactive raises a named message rather than showing an empty dropdown |
| Ask AI table picker | Same |
| Ask AI schema build | Inactive tables refused by name; inactive columns, PK entries **and any foreign key whose own or referenced column is inactive** are pruned from the metadata before the prompt is built |
| **Agent execution** | **The real guarantee.** Checked on every run, because a tool config is a standing permission written once and run for months |

**Saving a tool config deliberately does not re-check status.** The pickers are the only place a
status read touches authoring, so a momentarily unreachable datasource never makes an existing
config uneditable. It makes it *unrunnable until fixed*, which is the executor's business.

Note the two halves of the run-time check: builder mode validates tables **and** columns; SQL
mode validates only the tables the tool records (`table_name` + `extra_tables`), because
nothing parses the statement. **Choosing SQL mode means the statement is the permission at
column level** — a trade stated rather than half-solved.

---

# 9. How a query is represented

A tool config holds its query one of two ways, recorded on `tool_configs.query_mode`. **Exactly
one of the two columns is populated** — saving in either mode clears the other, so a tool
switched from SQL back to the builder cannot leave a stale statement behind for the executor to
prefer.

| Mode | Stores | Written by |
|---|---|---|
| `builder` | `config` — columns, aggregations, `group_by`, filters, joins | The query builder, or Auto Create Tool |
| `sql` | `sql_query` — one read-only statement | The SQL editor, or Auto Create Tool when the builder cannot hold the query |

### Why both exist

The builder is a deliberate **subset** of SQL, and it is the stronger artefact: every
identifier is checked against the tables the query reads, every filter value becomes a bound
parameter, and the whole query is rebuilt from reflected `Column` objects at run time rather
than assembled as text.

But it is a subset. `DISTINCT`, `ORDER BY`, `LIMIT`, `HAVING`, subqueries, CTEs, window
functions, `UNION`, `CASE` and expressions in the SELECT list all fall outside it. Before SQL
mode existed, a query needing any of those could not be saved as a tool at all — **even when
the operator had read the SQL and approved it.** Ask AI would write the query, show it, and
then refuse to save it: an assistant that produces something it will not let you use.

So the rule is now: **if it is a valid read-only query, a tool can run it.** Which mode it
lands in is a question of how well the builder can hold it, never of whether it is allowed.

### The shared guard — `app/utils/sql_guard.py`

Three features hand SQL around and all three need the same answer to the same question. It is
answered once, here, and each caller phrases the refusal in its own words:

| Caller | When | On refusal |
|---|---|---|
| `sql_assist_service._validated_sql` | Before a generated query is displayed | **502** — the model returned something unusable; the user did not cause it |
| `tool_config_service.validated_tool_sql` | Before a statement is stored | **400** — the operator can fix it, and the message says how |
| `query_executor._execute_sql_query` | Before **every single run** | `ToolQueryError` — the agent is told the tool needs reconfiguring |

`read_only_violation` refuses four things, in order:

1. over-length (`MAX_SQL_LENGTH = 8000` — long enough for any hand-written or generated query,
   short enough that a pasted dump is refused before it is stored, previewed and run);
2. not starting with `SELECT` or `WITH` (a CTE is the natural shape here, and a `WITH` that goes
   on to write is caught by step 4);
3. containing a `;`;
4. containing a write verb.

**Literal-stripping is what makes those checks readable at all.** `stripped_literals` blanks
quoted spans and comments *before* the structural checks run. Without it,
`WHERE action = 'delete'` reads as a `DELETE` and `WHERE note = 'a;b'` as two statements — both
perfectly ordinary reads. It is also why a PHP-serialised `LIKE '%s:6:"depart";%'` pattern
works and why `--` comments survive into the saved statement where the next person reads them.

The write list is deliberately short: `insert`, `update`, `delete`, `into`, `drop`, `alter`,
`create`, `truncate`, `replace`, `merge`, `grant`, `revoke`. It covers writes reachable *from a
position a read could reach* — `WITH … INSERT`, `SELECT … INTO`, appended DDL. `PRAGMA`, `COPY`,
`CALL`, `SET` and `VACUUM` are only valid at the start of a statement (refused by step 2) or
after a `;` (refused by step 3); listing them would add nothing but false rejections of a column
named `call` or a table named `copy`. Word boundaries mean `created_at` is not a `CREATE` and
`OFFSET` is not a `SET`.

### The advisory checks, and why they are honest about being heuristics

| Function | Semantics |
|---|---|
| `star_selection_violation` | Refuses `SELECT *` / `table.*`. Ask AI only. **`COUNT(*)` is not a star selection** — it names no columns, and refusing it would break every "how many" query |
| `forbidden_identifier` | The first forbidden name mentioned. A bare `id` deliberately does not match inside `orders.id`, so forbidding a column on one table cannot reject a query reading another's |
| `missing_identifiers` | Which required names are absent. **Advisory** — presence in the text is not proof of being in the SELECT list, so callers report rather than refuse |
| `group_by_violation` | The first column selected without being aggregated or grouped. Ask AI only. **Deliberately silent when unsure** |

`group_by_violation` answers a different kind of question from the rest: not "may this run" but
"**can** this run". MySQL's default `sql_mode` includes `ONLY_FULL_GROUP_BY` and PostgreSQL
enforces the same rule, so a grouped query selecting an ungrouped column is refused **by the
database** — surfacing at the worst possible moment, as a saved tool failing mid-conversation
with a visitor.

It takes `primary_keys` (table → key columns, straight from the reflection) so the shape both
databases *do* allow — group by a table's key, select its other columns, functionally dependent
— is not treated as a fault.

And it returns `None` whenever the statement is more than a text check can read: more than one
`SELECT` (a CTE, a subquery, a `UNION`), a `GROUP BY` holding an ordinal or an expression, or a
selected item that is not a plain column reference. **That asymmetry is on purpose:** a missed
violation ends as a clear message from the database, while a false one sends the model off
rewriting a query that was already right.

### What the guard does not check: syntax

Dialects differ, and a parser strict enough to be worth trusting would reject valid queries —
`DISTINCT ON`, `LATERAL`, `QUALIFY`, vendor functions. A syntax error surfaces when the tool
runs, named by the database. **A query that passes the guard and is then rejected by the driver
is the expected way a typo is found**, which is what Query Test exists to make cheap (§18).

### Filter operators, and the trap the first four could not avoid

`=`, `!=`, `>`, `<`, `LIKE`, plus four that compare against nothing:

| Operator | Matches |
|---|---|
| `IS NULL` | absent |
| `IS NOT NULL` | present — **including `''` and `'   '`** |
| `IS BLANK` | null, empty, or nothing but whitespace |
| `IS NOT BLANK` | has a real value |

The last two exist because of a specific silent failure. A text column can be absent, empty or
whitespace, and to a person reading a report those are one thing. Expressing that took
`!= ''` — and **the builder ANDs its conditions, so that filter silently keeps every NULL
row.** There is no second filter that fixes it and nothing in the form that shows it.

Two implementation details:

- **`TRIM` is applied only to a text column.** PostgreSQL has no `btrim(integer)`, so trimming a
  number is not a stricter check, it is an error — and a number has no empty string to catch. For
  a non-text column `IS BLANK` *is* `IS NULL`, which is the whole of what blank can mean for one.
  The decision is made from the **reflected** type, so it follows the column the database has
  now rather than the one it had when the tool was saved.
- **These store no value at all**, not an empty one. Switching a filter to `IS NULL` drops
  whatever was in its value box *and* drops the "Agent fills in" flag with it — there is no value
  for an agent to supply, and a stored field that provably cannot affect the query is one
  somebody reads as meaningful later.

The generated preview shows what they stand for (`(technology IS NOT NULL AND TRIM(technology)
<> '')`) rather than the dropdown label, because that preview is read as SQL — by the operator
checking the query and by the model in its routing prompt. The routing prompt itself says it in
English: *"technology has a real value (not empty or blank)"*.

### Joins — `app/utils/query_joins.py`

Two authoring surfaces (Tool Configs, and the Configurations page's Tool Base Config) plus one
non-interactive producer (Auto Create Tool) emit the identical shape, and all three import the
rules from `utils/` rather than from each other.

```json
"joins": [{"type": "inner", "table": "orders", "left_table": "customers",
           "left_column": "id", "right_column": "customer_id"}]
```

**Order matters and is preserved.** Each entry may only match against a table already in the
query — the base table, or a table joined *before* it — so the list always reads as a connected
chain that becomes SQL in exactly the order it is stored. `validated_joins` grows its set of
known tables inside the loop, which is what enforces that.

`join_types_for(db_type)` returns the join types **this dialect actually has**. MySQL has no
`FULL OUTER JOIN`, so it is not offered and not accepted — an option that only produces a query
failing at run time is worse than no option. It returns empty for a non-relational datasource
*and* before a datasource is chosen, so one template check covers both cases and no second flag
is needed. `MAX_JOINS = 10`, because the payload arrives from a form field and "however many the
client sent" is never an acceptable number of tables to join.

**Column qualification is handled in both builders rather than left to the user.** Adding the
*first* join qualifies every existing reference with the base table; removing the *last* strips
the prefix back off. A config saved before a join was added keeps its bare names, and
`validated_column_reference` still accepts them as meaning the base table — rejecting them would
make an existing config uneditable.

**Removing a join is never silent.** It also removes any join that matched against the table it
brought in (transitively), plus every column reference to any of them, and the panel says so:
*"Removed the join on 'orders', along with 2 selection(s) that referred to a table it brought
in."*

`require_object_name` checks every table and column name on the way in even though they were
chosen from live dropdowns, because those names are **interpolated into a generated query rather
than bound**. Its pattern spells out `[A-Za-z0-9_]` rather than using `\w` on purpose: `\w`
matches Unicode letters, which would let through homoglyphs and combining characters that have
no business in an identifier.

### The recorded table list, and why it is not derivable

```sql
tool_configs
  table_name    VARCHAR(255) NOT NULL   -- "projects"            the primary table
  extra_tables  JSONB NULL              -- ["project_details"]   the rest
```

The **first** selection is the primary table, and that ordering carries meaning rather than
being presentation — in builder mode it is the base table joins hang off and what every bare
column reference means. So the list is never sorted, and re-ordering it re-points the query.

Why record the extras at all, when a builder query's joins already name them? Because **a SQL
query's tables live only inside its statement, and nothing in this application parses a FROM
clause.** Before the column existed, a tool whose statement read
`projects LEFT JOIN project_details` recorded `projects` and no more. Two things were wrong: the
routing prompt told the agent it read one table when it read two, and nothing could check the
others were still switched on.

In builder mode the two fields are held to agreeing — `_builder_context` offers only the selected
tables as join candidates, and `_require_joins_within_selection` refuses a saved join onto a
table outside the list. In SQL mode they are simply what the operator says the statement reads;
nothing verifies it against the text, which is why the form asks.

`table_name` stays a scalar column rather than being folded into the list because its role
really is singular. `tool_config_service.tables_read()` is the single place the two are put back
together, so the list page, the edit form, the routing prompt and the executor cannot disagree
about which tables a tool reads.

### The preview is a display artefact, permanently

`tool_config_service.build_query_preview()` and `query_joins.build_join_sql()` render a config as
SQL text and **inline filter values with f-strings**. They are display-only and always were —
executing them would make every stored filter value an injection vector.

The executor mirrors them clause for clause and **shares no code with them**, so the preview an
operator reads and the query that runs describe the same thing without the preview becoming a
code path. The preview is built twice from the same rules — server-side for the list, client-side
for the live panel — so the list and the form describe a config identically.

An empty Columns list previews as `SELECT *`. At run time that expands to every active column of
every table the query reads, **spelled out** — the preview's `*` is shorthand for that, not for
whatever the table happens to hold.

---

# 10. The executor, and the security argument

`app/services/deep_agents/query_executor.py` is the only module in the application that touches
user data on the agent path. Everything above rests on its properties, so they are worth stating
as a proof rather than a description.

### Builder mode: the query is never text

The executor **never emits SQL text**. It reflects the real tables (`Table(autoload_with=…)`,
the pattern already in `db_utils._reflect_one`) and assembles a SQLAlchemy Core `Select` from
actual `Column` objects. Four properties follow:

| Property | Why it holds |
|---|---|
| Identifiers cannot be read as syntax | They are `Column`/`Table` objects, quoted by the dialect |
| Filter values cannot be SQL | They become **bound parameters** — `col == value`, `col.like(value)` |
| A missing column fails readably | Reflection resolves names before the driver sees anything |
| A switched-off column is never read | The active set is derived from the reflection on **this run**, not from what was true when the config was saved |

Verified behaviourally: a stored filter value of `x' OR 1=1 --` comes back as **zero rows**, and
`%'; DROP TABLE customers; --` through a `LIKE` filter leaves the table intact. Both assertions
live in `test_query_executor.py`.

### What an empty selection means

A config naming no columns selects **every active column of every table the query reads, joined
tables included** — spelled out, never a literal `*`. Two consequences:

- **A joined tool returns its joined tables' data.** Previously it returned only the base
  table's columns, so a tool built to join customers to orders answered every question about the
  customer with nothing but order rows.
- **With a join in play every field is named `table_column`** (`orders_id`, `customers_id`).
  Rows go back as a dict, so two tables both having an `id` would otherwise collapse into one key
  and the agent would be handed a row that quietly lost a column. Unjoined queries keep bare
  names, and `prompt_builder` states the convention in the routing prompt because the field names
  are what the model has to quote back.

### A reference to a switched-off column fails the tool

Loudly, with a message the agent relays. It is **not** dropped from the query. A dropped filter
widens the result set; a dropped group-by changes what each row counts; either way the query
still returns a number the model states as fact.

That covers selected columns, aggregations, filters, group-by and join keys alike, because they
all resolve through one function (`_resolve_column` → `_table_column`). One resolver is what
makes the guarantee total rather than five guarantees that might disagree.

### SQL mode: the statement runs as written

Running an approximation of a query the operator approved would defeat the point of the mode.
The safety comes from elsewhere and it is the same place: nothing the model produces is in the
statement. It was written and saved in advance, and it is re-checked against `sql_guard` on
every run.

**The row cap is applied by streaming, not by rewriting the SQL.** `connection.stream()` opens a
server-side cursor where the driver supports one and `fetchmany(limit)` stops at the cap.
Wrapping the statement as `SELECT * FROM (…) LIMIT n` would have been simpler and is wrong twice
over: it changes the SQL the operator approved, and **MySQL rejects a derived table with
duplicate output column names** — so `SELECT a.id, b.id FROM a JOIN b`, a query this mode exists
to make possible, would fail for a reason having nothing to do with the query.

The statement still *runs* in full on the database; an unfiltered aggregate scans what it scans.
The cap bounds what crosses the wire and what reaches the prompt, which is what it is for.

### Re-validation on every run

The stored query is re-validated at execution time — `validated_query_config()` in builder mode,
`validated_tool_sql()` in SQL mode — **not trusted from the row.** A row edited directly in psql
gets the same treatment as one from the form, and a row that no longer passes becomes a
`ToolQueryError` the agent can relay, never a 500.

A tool saved before a validation rule existed is therefore caught on its next run rather than
silently grandfathered.

### The two prompt budgets, and why one is enforced by instruction

Neither of these is a cap on a query — a query returns every matching row. Both are about the
text a model is handed.

| Budget | Value | Enforced by |
|---|---|---|
| `PROMPT_ROW_LIMIT` | 200 | Truncation of the prompt — what the model may reason over |
| `DISPLAY_ROW_LIMIT` | 100 | **Instruction (grounding rule 8)** — what may go in a chat bubble |

The first truncates because a context window is a fixed size and the alternative is a turn that
fails outright. The second does not, because cutting at 100 would take the other rows away from
the model as well and leave it unable to answer the question it was asked.

`describe_result` reports one of two headers, because a model cannot tell them apart from the
rows alone:

| Situation | Header |
|---|---|
| The rows are everything | `12 row(s), which is the complete result:` |
| The rows are a sample | `200 row(s) out of 5275 matching record(s). These are a sample; the total is the figure to report:` |

There used to be a third — `30 row(s):`, with a warning that the number might be a cap rather
than a total — and removing the fetch cap removed it. Every caller now either ran a `COUNT(*)`
or is holding every row it matched, so the figure in that line is always a real number of
records. A model asked to reason about a total it was told is unknowable has nothing to reason
with.

### Parameterised filters: the one opening, and its exact boundary

The default is an **empty argument schema** — the model's only decision is *which* tool to call.
That default matters and is unchanged, for two reasons: an argument would put model-generated
text into the query, and it would let the model widen a filter the operator narrowed
deliberately.

The cost was real: one tool config per question shape. An agent could not answer "projects for
August" unless a tool already filtered to August, so a visitor rephrasing got the same answer
every time — and a model handed a tool failure would **improvise the remedy it could not have**,
promising to filter by a date range nothing could accept.

Ticking *Agent fills in* on one filter stores no value and names a parameter instead. What the
model supplies is the right-hand side of one comparison the operator chose to open:

- the **column** comes from the stored reference, resolved by `_resolve_column` exactly as a
  fixed filter's is;
- the **operator** comes from the stored config;
- the **value** is coerced to the column's own Python type by `_coerced_value` and **bound**;
- **every other filter still applies.**

`test_query_executor.py` asserts that `0 OR 1=1 --` passed as a value matches nothing, and that a
column reference passed as a value compares against the *string* rather than switching which
column is filtered.

**A missing required value refuses the query.** Dropping the clause would return every row and
look like a working answer, so `_filter_conditions` raises with the parameter named and tells the
model to call again with a real value rather than invent one. An omitted *optional* value drops
that one clause and leaves every other filter standing.

**The prompt describes parameters twice, deliberately.** The JSON schema says a field exists and
what type it is; `_parameter_description` says which column it narrows and with which comparison.
A model choosing between two tools reads the prompt, not the schema.

**Every field is declared a string and typed at the database.** A schema typed from the reflected
column would need a reflection at prompt-build time for a tool that may never be called, and
would *still* have to be re-checked at execution because the column can change under a saved
config. One answer to "what type is this", not two that can disagree.

### The SQL-mode equivalent

Builder mode opens a *filter*, which has a column and an operator the operator chose. A statement
has neither, because nothing parses one — so values are **declared beside the statement**
(`tool_configs.sql_params`) and the operator writes the `:name` and the comparison themselves.

`_declared_bindparams` iterates the **declarations**, so an invented argument has nowhere to
land. The one difference is typing: there is no reflected column to coerce against, so the
declaration carries a `type` (`text` | `number` | `boolean`). A value that will not convert
**falls back to the string rather than raising** — `"abc"` for a number is a value that matches
nothing, which is the right answer to what was asked.

### RIGHT JOIN, refused rather than approximated

SQLAlchemy expresses joins as `isouter`/`full` flags with **no right variant**. A right join is
only expressible by swapping the operands, which this accumulating builder cannot do once the
base table is fixed. Substituting a left or full outer join would quietly change which rows come
back, in the direction of a plausible wrong figure.

So it is refused in builder mode, flagged by `find_unsupported_tools()` on the agent console
before a visitor can hit it, and **remains authorable and previewable** — a SQL-mode tool may
right-join freely, because its statement is not reassembled.

### Relational only

`query_joins.RDBMS_DB_TYPES` = `{postgres, mysql, sqlite}`. A tool config pointed at Mongo or a
file is refused with a message the agent relays, and SQL mode is refused **at save time** by
`_validated_query_mode` rather than being stored and failing on the agent's first call: a tool
that can never run is a configuration mistake, and the operator is standing in front of the form.

---

# 11. Tool chaining and iteration

A tool config may **embed** other tool configs. The inner tool runs first, one named column of
its result becomes a list of values, and the outer query is restricted to them.

```
paid_invoices          → client_id  ─┐        (deepest)
active_clients         → id        ─┐│
projects_by_client   WHERE projects.client_id IN (…)   (root — the tool the agent calls)
```

### What crosses an edge: values, never rows, never text

| Parent mode | How values arrive |
|---|---|
| builder | `_value_conditions` adds `column.in_([…])` over a **reflected** column — the same resolver every stored filter goes through |
| sql | The statement names `:active_clients`; the list is bound as an **expanding bind parameter**. The statement is not rewritten |

The inner tool's *rows* are discarded at the edge. Not returned to the agent, not carried up the
chain, not logged — exactly as a sub-query's inner rows are not part of an outer query's result.

### Each tool is still run as itself

The chain is **not** compiled into one nested SQL statement. Every node goes through
`query_executor` with its own validation, its own active-table and active-column checks and its
own cap, so a tool behaves identically whether it was called directly or embedded. That is what
makes "the child works on its own too" true rather than approximately true — and it is what makes
the short circuit possible, since a single compiled statement would just return zero rows without
anyone knowing why.

### The two binding modes

| `binding_mode` | SQL mode renders | Builder mode builds | Parent runs |
|---|---|---|---|
| `in_list` *(default)* | `bindparam(name, values, expanding=True)` → `IN (?, ?, ?)` | `column.in_(values)` | once |
| `each` | `bindparam(name, value)` — a plain scalar | `column == value` | **once per value** |

`in_list` is the **server default**, so every link that existed before this feature behaves
exactly as it did. Nothing about a saved tool changes until an operator chooses otherwise.

`each` exists because an expanding bind parameter always renders parenthesised, making it a
syntax error anywhere but the right of an `IN`:

```sql
LEFT JOIN departments dd ON dd.id = :department_id            -- a comparison
AND p.departments LIKE CONCAT('%s:1:"', :department_id, '"%') -- inside a string
```

— and because "how many projects does *each* department have" is not one query restricted to a
list of departments, it is one query run once per department with the results put together.

**At most one `each` child per tool**, refused on save. Two would run the parent once per
*combination* — ten departments and eight regions is eighty statements inside one chat turn, past
`MAX_CHAIN_ITERATIONS` and so refused anyway, and expensive where it is not. The query that
actually wants writing there is one statement joining both.

### `value_alias` — which run a row came from

Rows from twenty runs of one statement are indistinguishable once concatenated, and a statement
that filters on a department without *selecting* it is perfectly ordinary SQL. `value_alias`
closes that: every row of iteration *i* gets `{alias: value_i}` merged in, **in Python**
(`labelled_rows`), never by rewriting the statement.

Optional, because a query that already returns the value needs no second copy. Asking for one
anyway is **refused at run time as a column collision**, not resolved silently — overwriting
would replace a real database value with one from the chain, and skipping would leave rows whose
label says nothing about them. Both produce a result that looks right and is not.

### The caps, and why they differ

| Cap | Value | Bounds |
|---|---|---|
| `MAX_CHAIN_DEPTH` | 5 | Every level is a database round trip inside a turn a visitor is waiting on |
| `MAX_CHILDREN_PER_TOOL` | 5 | More than a handful is a query that wants writing as SQL |
| `MAX_CHAIN_NODES` | 20 | The whole tree, root included |
| `TOOL_CHAIN_MAX_ITERATIONS` | 50 | Iterations of an `each` parent |

Every one of them bounds the **shape or the cost** of a chain. None bounds how much data it
returns: a chain reads every value and returns every row.

**Two row caps used to be in this table.** `MAX_CHAIN_VALUES` (2,000) on an inner tool's values
and `MAX_TOOL_ROWS` (200) on the root's result. Both are gone — see the second thesis above.

What is left bounds **round trips, not rows**. An expanding `IN` hands any number of values to the
database in one statement, while an iterating link is one statement per value, each with its own
planning, its own cursor and its own share of a chat turn. Removing it would not return more
data; it would spend the turn and time out with none.

It **refuses rather than truncating**, and that is the load-bearing distinction: a union short of
its last iterations is short of whole *departments*, not of rows, and no row count says so.

### Who is being told to narrow it

This was got wrong once, in a way worth recording. The refusal advice said *"the inner query
needs narrowing"*. The inner query is a **tool config**, so that is addressed to the operator. A
model relaying it to a visitor turns it into *"please specify a date range"* — and a tool takes
no arguments, so there is no date range to specify. Every rephrasing routes to the same tool,
hits the same refusal and returns the same sentence.

Observed live: *"latest projects"*, *"August"*, *"August 2026"* producing one identical refusal
each, **with the tool never running once.**

The advice now says the tool needs reconfiguring by whoever set it up and explicitly forbids
asking the visitor to narrow anything. Grounding rule 11 makes that general — the model is told
outright that tools take no arguments and that rephrasing cannot change a result — so the next
tool failure to carry hopeful advice cannot reopen the same loop.

### A chain that does nothing, which is a different problem from a chain that is too big

A chain whose child reads the **same table** as its parent, matching that table's key against
itself, is a tautology: `project_details.id IN (SELECT id FROM project_details)` selects exactly
the rows it started with. It cannot change a result, and it costs a full round trip and a full
`IN` list to discover that — more now than before, since neither is bounded.

One was found live: `fetch_project_details` embedded `fetch_projects`, both on
`project_details`, joined `id` to `id`, and the only thing the link achieved was failing every
call at 2,921 values. It runs now that the value cap is gone, but it still achieves nothing.
**A self-referencing chain is almost always a link added by accident**, so look at the tables
and the two columns.

A chain that is genuinely too expensive is the iterating kind — one statement per value. There,
rewriting the parent in SQL mode as a `JOIN` answers the same question in one.

### What is refused, and why none is left to run time

Each of these produces a *plausible wrong answer* rather than an error:

| Refused | Because |
|---|---|
| A cycle, direct or transitive | The chain runs depth-first with no visited set — a cycle is a hang, not a wrong number |
| A child on another datasource | Only values cross, so it would *run*; matching an id from one system against an id in another is a coincidence, not a join |
| A child owned by someone else | **404**, not 403 |
| A disabled child | `is_enabled` is the operator's "stop using this"; a parent running it anyway makes the switch a lie |
| Deleting or disabling an embedded tool | The parent would keep running with its filter gone — **more** rows than it should return, and nothing saying so |
| A `:name` no child fills, or a child naming a `:name` the statement lacks | Either way the statement cannot run. Two checks, because it is the same fault from opposite ends |
| A column the child does not return | Checked when the child's output is knowable; a SQL child's columns are not, so the name is verified against the real result at run time |
| The same child bound to the same target twice | It would AND a list against itself. The same child on *two* targets is allowed — one tool returning client ids can restrict both `owner_id` and `billed_to_id` |
| `IN :name` bound by an `each` link | Renders `IN ?` — a syntax error the *database* would report mid-conversation |
| `= :name` bound by an `in_list` link | Renders `= (?, ?, ?)` — the same, the other way round |
| A declared `:name` the statement never uses | A field the model is asked to fill on every call for no effect |
| Embedding a tool that **requires** a value | An inner tool is never called by the model, so nothing would fill it |

The arity checks (`IN :x` / `= :x`) are text checks over the literal-blanked statement — they see
the shape immediately next to the placeholder and nothing cleverer. **That is the mistake
operators actually make**, and it is worth twenty lines to catch at save time rather than months
later.

Deleting the **parent** is fine: the link described that tool's query and goes with it. Both
foreign keys cascade, so a deleted agent or datasource cannot strand rows — but the delete
guard, not the cascade, is what protects a live parent.

### The form, and one shape decision

Everything posts as **one hidden `children_json` array**, for the same reason the builder posts
one `config_json`: five parallel controls could arrive at different lengths, and a row would then
pair the wrong column with the wrong tool.

`GET /tool-configs/child-options` applies the same rules the save would — same owner, same
datasource, enabled, not this tool, and never a tool that already embeds it — so **the cycle rule
is applied before the operator can build one.** The card resets out of band when the datasource
changes, because a child must read the same datasource as its parent.

`ChildToolOption.columns` is **empty** when the tool's output cannot be known without running it
(a SQL tool, or a builder tool selecting everything), and the form then takes a typed name.

---

# 12. LangGraph in this codebase: four graphs, one set of rules

Four separate LangGraph runtimes exist, plus the `deepagents` agent itself. They are not
variations on one thing — each was chosen because the behaviour it needed *is* a control-flow
graph, and writing it as one makes the control flow the thing you read rather than something
reconstructed from scattered `if`s and `break`s.

| Graph | Module | Shape | Checkpointed | Why a graph |
|---|---|---|---|---|
| **Tool chain** | `tool_configs/tool_chain_graph.py` | Linear, deepest-first, conditional edge after every inner node | No | The short circuit — a level matching nothing must stop everything above it |
| **Export** | `downloader_agents/base/download_graph.py` | Loop + branch, one cleanup node with many inbound edges | **Yes (PostgreSQL)** | A genuine pause across two HTTP requests, and one cleanup path |
| **Aggregation** | `agent_recursive_dataframes/aggregate_graph.py` | Map-reduce: `Send` fan-out, implicit barrier | No | Concurrent folding with an exact merge |
| **Designed** | `graph_designer/graph_compiler.py` | Compiled from a user's drawing; conditional edge on **every** node | **Yes (PostgreSQL)** | The drawing *is* the graph; plus human-in-the-loop |
| **Deep Agent** | `deep_agents/` via `create_deep_agent` | The tool-calling loop | No | Supplied by `deepagents` |

### Six rules that apply to all of them

**1. `recursion_limit` must be computed, never left at the default.** LangGraph's default is 25.
That silently caps real work:

- `download_graph` loops `write_batch` back to itself once per batch, so 25 would stop an
  export at 1,250 records by raising `GraphRecursionError` — an internal error a long way from
  the cause. `_RECURSION_LIMIT` is derived from `MAX_EXPORT_ROWS` and the batch size.
- `graph_compiler` derives it from the drawing, because 25 would stop a valid loop over 30 rows.
- The Deep Agent keeps 25 deliberately — roughly a dozen tool calls is the intended bound there.

**2. State is copied on every super-step, so nothing large travels in it.** A 200,000-record
aggregation is 250 waves; carrying records in state would copy them 250 times. So records live
in `frame_buffer`, a module-level registry keyed by the run, and **the state carries only keys.**
Same shape as `record_reader`'s reader registry and `db_utils`'s engine cache, for the same
reason.

**3. A new task gets a *copy* of the context, so a `ContextVar.set` inside a node is invisible to
the parent.** This is the single most expensive lesson in the codebase, and it bit twice:

- `download_notice` therefore holds a **mutable box, not a value.** Rebinding the ContextVar
  inside a tool is invisible; mutating an object the copy inherited by reference is not.
- `turn_recorder` **appends to** a `TurnRecord` rather than replacing it, for the identical
  reason, reached from the identical depth.

Getting it wrong is silent: the export builds correctly and no card ever appears. Every Python
test passed the broken version, because they all called the setter and the getter in one
context. The regression test now calls the setter inside a real `asyncio.create_task` and
asserts the parent sees it — **nothing weaker crosses that seam.**

**4. A node that raises inside a `Send` super-step routes nowhere, so cleanup never runs.**
Hence the aggregation graph's nodes **return** `{"failure", "advice"}` rather than raising, and
`run_aggregation` *additionally* releases in a `finally` — the cleanup node is the tidy path, the
`finally` is the guarantee, because a cancelled node also routes nowhere and a chat turn timing
out cancels mid-node.

**5. Fields written by several concurrent nodes need a reducer.** A plan that is wrong is wrong
in all four slices of a wave, so all four write `failure` in one super-step, which LangGraph
refuses on a plain field. First write wins — one fault seen four times is still one fault.

**6. The pause is read off the invoke result, not caught.** LangGraph reports an interrupt under
`__interrupt__`, and both `download_graph._interrupt_payload` and
`graph_compiler.interrupt_payload` read it **defensively**, because that key's shape is
langgraph's to change.

### Checkpointing: only where a pause genuinely crosses a request

Two graphs checkpoint and two do not, and the line is exact: **`interrupt()` is the only reason.**

`aggregate_graph` compiles without a checkpointer because there is nothing to resume across
requests — checkpointing would write the whole state 750 times for a large run to buy a resume
nobody asks for. `tool_chain_graph` likewise.

`graph_checkpointer` in the tests earns its keep twice: `get_checkpointer` chooses its store from
`DATABASE_URL`, so without the fixture a test writes real checkpoint rows into the development
database — and the saver is cached in a module global, where `AsyncPostgresSaver` holds an
`asyncio.Lock` bound to the loop that created it, so the **second** test to reuse a cached saver
fails inside `asyncio.locks` on a loop that no longer exists.

### Graph compilation is separated from graph decisions

`graph_compiler` is the **only** module in the Graph Designer package that imports langgraph;
everything it needs to make a decision lives next door and is testable without it. Same split
`tool_chain_service` / `tool_chain_graph` makes, and the reason `pytest.importorskip("langgraph")`
guards only one test file per feature.

That separation is why the rules carrying a feature's *correctness* stay runnable anywhere:
`partial_algebra`, `frame_ops`, the planners and every schema import neither langgraph nor a
provider SDK, so `test_partial_algebra.py` checks the arithmetic against SQLite with no DataFrame
library and no graph in the process at all.

### The tool chain graph, concretely

```
START → paid_invoices → active_clients → projects_by_client → END
              │                │
              └── no values ───┴───────────────────────────→ END
```

One node per tool, added deepest-first. Edges run upward in topological order. **A conditional
edge after every inner node is where the short circuit lives:** a node that produced no values
sends the run to `END`, so the tools above it are never executed and no `IN ()` is ever built.

**Siblings run in sequence, not in parallel.** LangGraph would fan them out happily, and it is
the wrong trade: the first sibling to return nothing ends the run, so running them in order means
the second is never executed at all. Chains are short by construction, so parallelism would buy a
fraction of one query's latency in exchange for **always paying for queries whose answer cannot
matter.** (The Graph Designer makes the same argument for its own sequencing.)

The graph is compiled **once**, in `tool_factory._build_tool`, and kept in the tool's closure —
a nested tool call costs an `ainvoke`, not a rebuild.

Its tests use a real SQLite database whose data disagrees **on purpose** — client 1 is paid *and*
active, client 2 is paid but churned, client 3 is active but unpaid — so a chain that skips a
level returns more rows and the test notices.

---

# 13. Deep Agents

`app/services/deep_agents/`, built on `deepagents` 0.7.x over LangGraph.

### The two prompt columns, and why they never mix

An agent has two prompt columns on `data_agents`:

| Column | Written by | Contents |
|---|---|---|
| `system_prompt` | The operator, via the agent form | Standing instructions, tone, refusals |
| `tool_routing_prompt` | `prompt_sync_service` | Generated description of the agent's tools |
| `tool_prompt_synced_at` | `prompt_sync_service` | Staleness marker |

Separate columns because a single one would have **two writers racing**: an operator with the
edit form open would clobber a regenerated block, or the job would overwrite words the operator
wrote.

The runtime prompt is composed at answer time by `compose_runtime_prompt()` — **operator text
first, generated block second**, so the grounding rules are the most recent instruction the model
reads. Same reasoning as `ai_analytics_service._GROUNDING_ADDENDUM`. No owner-authored persona
can opt out of the display limit or the link ban.

### Composed in Python, not by an LLM

`build_tool_routing_prompt()` is a **pure function** over the agent's enabled tool configs. No
model call. Four reasons:

- **It cannot describe a tool the agent does not have** — the list *is* the tool list.
- **It is reproducible** — an unchanged configuration regenerates a byte-identical prompt, so
  behaviour does not drift between two saves.
- **It is free**, so it can be regenerated on every tool change.
- **Nothing leaves the box** to produce it.

Per tool it states the purpose, the datasource and table(s), the exact field names in the result
(including how an unaliased aggregation is labelled, so what the prompt promises matches what
arrives), the grouping, and any fixed filter **with a note that it cannot be widened**.

The two modes are described differently, deliberately:

| | Builder mode | SQL mode |
|---|---|---|
| Reads | Base table, joined tables named | Every recorded table named |
| Returns | Every field named, with its alias | "whatever columns the query below selects" |
| Query | Rendered from the config | The statement, quoted in full |

The field list is what the model quotes back in an answer, and **nothing has parsed a stored
statement's SELECT list.** Guessing one would have the model naming a column that is not in the
result, so it is told to read the field names off the result instead — which is the truthful
instruction.

An agent with **no** enabled tools gets an explicit "you have no data tools" prompt, so it
refuses rather than answering from the model's own knowledge — which would look like a working
answer and be entirely invented.

### The grounding rules, and the ones with history

The standing rules: answer only from tool output, never estimate, one tool per question, say so
when nothing covers it, no rows means no matching data, a capped result is not a total, never
print more than 100 rows, repeat a download offer word for word, never write a link or a URL,
describe the rows you actually got rather than the ones that were asked for, and put rows in a
Markdown table.

Four of those were added in response to observed misbehaviour, and the reasoning is worth keeping:

**Rule 10 (no URLs).** The interface draws its own download button, and the answer used to render
as plain text — so a model writing markdown produced a *visible* `[Download CSV](/public/…)`,
which is exactly what it did before the rule existed. **The download tools stopped handing it a
URL at the same time, because a rule the tool output contradicts is a rule that loses.**

**Rule 13 (describe the result you have).** Asked for *"the list of projects in a department"*,
an agent whose projects tool filters on nothing called it anyway and headed the result
**"Projects in the department"**. Every row was real, the query ran correctly, and the reply was
false: the reader was told they were looking at one department when they were looking at all of
them, and nothing in the answer gave them any way to notice. **A wrong number at least looks like
a number somebody could check.** Rules 11 and 12 already governed what the model may *pass* to a
tool; neither governed what it may *claim* about the rows that came back, and the heading above a
table is a claim.

**Rule 15 (Markdown).** New, and new because for a long time the interface told the model the
opposite: the widget escaped every reply, so a table arrived as a wall of `|` characters and
prose was the only thing that read correctly. Rule 8's display limit is **restated inside rule
15**, because a formatting rule that invites a table without restating the cap is an invitation
to paste two hundred rows into a chat bubble.

**Rules 8 and 9 name two tools by their constants** (`CONFIRM_DOWNLOAD_TOOL`,
`DOWNLOAD_STATUS_TOOL`) rather than by literal strings — a rule naming a tool the agent does not
have is worse than no rule — and `download_tools.py` builds them from the same constants so the
pair cannot drift.

### Staleness has two sources, and a timestamp only sees one

A stored prompt is half the agent's tools and half `prompt_builder`'s standing rules, and the two
go out of date for unrelated reasons.

**The tool half is a timestamp comparison** — `tool_prompt_synced_at` against the newest tool
config's `updated_at`. That works because saving a tool writes a row.

**The rules half writes nothing.** Editing a grounding rule changes no database row, so every
agent that already existed kept answering from its stored copy of the *old* rules —
indefinitely, and invisibly, until somebody happened to re-save one of its tools for an unrelated
reason. A rule corrected in response to a real misbehaviour **simply did not take effect, and
nothing said so.** A deploy was not a trigger either, because sync is only ever triggered by a
Tool Configs mutation.

So every generated prompt ends with a marker:

```
<!-- grounding-rules:a1b2c3d4e5f6 -->
```

— a truncated SHA-256 of the filled-in rules text. `is_prompt_stale()` returns True when the
stored prompt does not carry the current one, so the next answer rebuilds it and the following
ones do not. A prompt written before the marker existed has no marker and is **stale by
definition**, which is the correct answer for exactly the prompts this was written to fix.

A hash rather than a version constant on purpose: the thing that must not drift is the rules
text, and a number somebody has to remember to bump is the thing that gets forgotten in the same
commit that edits a rule. It is an **HTML comment** so a model reading its own prompt has nothing
to act on, and truncated because it is only ever compared for equality.

### Sync is an optimisation, not a dependency

`sync_tool_routing_prompt()` runs as a Litestar `BackgroundTask` from every Tool Configs mutation
— create, update, set-enabled, delete — *after* the response is sent, in its own session (the
request's is closed by then). **It swallows every exception: there is nothing left to report to.**

That is safe because `deep_agent_service` calls `is_prompt_stale()` on every answer and
**regenerates inline** if it is behind. So a failed task, a restart mid-flight, or a task that
never ran costs one extra write on the next answer and is never wrong.

This is why the feature needs no queue table, no scheduler and no retry logic — **the first
background work in this codebase, and it stays that simple only because correctness does not rest
on it.**

Moving a tool between agents syncs **both**: the tool joins one agent and leaves another, and the
one it left is still describing it. `update_tool_config()` returns both ids for exactly that
reason.

### One list feeds the prompt and the tools

`collect_agent_tools()` returns one list that both `prompt_builder` and `tool_factory` consume.
**That is what makes it impossible for an agent to be told about a tool it cannot call, or handed
one the prompt never mentioned.**

It returns the agent's own enabled tools *plus every transitive child*, so children are described
and callable in their own right without their rows being moved to this agent. A published Graph
Designer graph attached to the agent appends as **one entry marked `kind: "graph"`**, and both
consumers read it from there — three additive edits, each a no-op for a user with no graph.
Because the entry carries `updated_at`, `is_prompt_stale` already invalidates the prompt when a
graph is edited; no new staleness path had to be written. And `find_unsupported_tools` **skips a
graph entirely**: it has no single datasource, so without the skip a graph would be reported as
"not a relational datasource" on every agent console.

### Model selection and retries

`model_factory.build_chat_model()` reuses `ai_analytics_service.resolve_provider()`, so precedence
is unchanged everywhere: a pinned key, then the user's active keys in provider-priority order,
then `ANTHROPIC_API_KEY`.

| Resolved provider | Model | Notes |
|---|---|---|
| `anthropic` | `ChatAnthropic` | Same Claude model as everywhere else |
| `openai` / `other` | `ChatOpenAI(model_name, base_url)` | `model_name` required — **503 with a fixable message** if unset |
| in-built | `ChatOllama` | Refused for small models — see §20 |

`temperature = 0`, so a question cannot route to a different tool on a retry.

**The retry lives on the model client, not around the graph.** `max_retries=MAX_RETRIES` (4) on
both chat models, raised from the SDKs' default of 2 — that default is sized for a provider that
rate-limits *per key*, where two fast retries put you past your own burst, and it is not enough
for a gateway that queues under load and answers `queue_exceeded` (Cerebras and other
OpenAI-compatible hosts do this; the queue drains in seconds).

**The layer matters more than the number.** A Deep Agent turn is a loop — call a tool, read the
rows, answer — so retrying `deep_agent.ainvoke` would **re-execute every tool call that had
already succeeded**, running the user's SQL again for a failure that happened after it. Retrying
one HTTP call retries one HTTP call. `test_rate_limits.py` asserts this **by reading the source**,
because the tempting wrong version passes every behavioural test. The turn timeout is unchanged
and still the outer bound.

### A busy provider is not a broken agent

A 429 is told apart from every other turn failure, and it earns the separation. The catch-all says
*"please try again, or check the agent's AI key in AI Settings"*, which is right for a wrong key
and a dead endpoint — and **wrong for a provider having a busy minute**, where nothing is
misconfigured and the advice sends someone hunting a fault that does not exist.
`_RATE_LIMIT_ERRORS` catches `anthropic.RateLimitError` and `openai.RateLimitError` first and
answers **503** with a message that names the cause and explicitly says nothing needs changing.

The visitor never sees either sentence — `chatbot_reply_service` degrades to
*"I can't reach that data at the moment, so I'd rather not guess"*, which is true regardless of
the cause and names no system they can see.

### Timeouts are chosen by who is waiting

| Caller | Budget | Env override |
|---|---|---|
| Chatbot turn (a visitor is waiting) | 120 s | `DEEP_AGENT_TIMEOUT_SECONDS` |
| Test console (an operator ran it deliberately) | 900 s | `DEEP_AGENT_CONSOLE_TIMEOUT_SECONDS` |

**Not by which provider answers.** The first attempt keyed it off the provider and was corrected,
because that would have let a chatbot visitor wait seven minutes. The visitor budget is
deliberately not widened for the in-built model: an agent too slow to answer within it degrades to
the data-profile reply, which serves a visitor better than a spinner.

### deepagents' built-in tools

`create_deep_agent` binds eight tools of its own alongside ours. Verified against what 0.7.1
actually binds, **not against its documentation**:

```
ls  read_file  write_file  edit_file  delete  glob  grep  task
```

None is a data path: the default backend is `StateBackend`, so that filesystem lives in the
conversation's own state, in memory, empty at the start of every turn, never the host's disk —
and the `execute` shell tool is **not bound at all** without a sandbox backend, which this module
does not supply.

They cannot be removed: `FilesystemMiddleware` and `SubAgentMiddleware` are required scaffolding
in 0.7.x, and `excluded_middleware` **raises `ValueError`** rather than dropping them. So the
routing prompt tells the model explicitly that they are private scratch space, start empty, and
must never be used in place of a data tool — without that, a model will read an empty file and
report "no data" instead of calling a tool.

### Attaching an agent to a chatbot

`chatbot_api_keys.target_type` says which kind of widget it is:

| `target_type` | `datasource_id` | What the widget may read | If the agent can't run |
|---|---|---|---|
| `datasource`/`table`/`collection`/`file` | set | That datasource target | Falls back to a profile answer |
| `agent` | **NULL** | Its agent's tool configs | Says it can't reach the data |

The `agent` row exists because an attached agent **already carries its datasources** — one per
tool config. Requiring a datasource *as well* asked the operator the same question twice, and for
an agent reading three of them the second answer could only be arbitrary. Worse, the two answers
could disagree, and which one applied would depend on whether the agent happened to run that turn.

`set_chatbot_data_agent` **refuses to detach** an agent-backed widget's agent, and the picker does
not render the "No data agent" option (`agent_required`, which rides through the cascade URL so it
survives a workspace change). Clearing it would leave a published key answering nothing, with no
way back — the datasource target is immutable after creation. Swapping one agent for another is
still allowed, which is the operation that case actually needs.

Both FKs are `ON DELETE SET NULL`, so deleting a workspace or an agent degrades a live widget to
default behaviour rather than breaking it mid-conversation. `workspace_id` is stored only so the
picker re-opens on the right branch — it is **not used at answer time** and deliberately not
required to match the agent's own workspace, because an agent may have none or be moved later and
must not silently detach itself from every chatbot using it.

### The fallback, and the case with nothing to fall back to

```
data_agent_id set?  → deep_agent_service.answer_for_chatbot(...)
                    → on HTTPException: log, fall back to the profile answer
otherwise           → chatbot_service.answer_message(...)   # unchanged
```

**No agent attached means nothing changes.** That is the back-compat guarantee: every existing
chatbot has `data_agent_id IS NULL` and takes the identical path it took before this feature
existed.

The fallback cannot leak anything the agent was gating, because the profile path is scoped to the
chatbot's own datasource target — chosen by the operator at creation and unchanged by attaching an
agent.

**An `agent` widget has no such target, so there is nothing to fall back to.** It answers with
`_NO_FALLBACK_REPLY` and the agent's actual reason goes to the log. That is a worse visitor
experience than a profile answer and a better one than a wrong answer or an error bubble, and it
is the trade the operator accepted by not nominating a datasource. `answer_message` guards on
`datasource_id IS NULL` **before** it looks anything up, so the case cannot reach a query
filtering on `id = None` and report "your data source is no longer available" — it never had one,
which is a different thing.

The prose answer maps onto `AnalyticsResult.summary` **alone**. `insights` stays empty and `table`
stays `None` rather than being manufactured by splitting the text — that would be putting words in
the model's mouth.

### The console, and the two-transports problem

The test console form declares `hx-post` to `/ask` **and** `data-stream-url` to `/ask-stream`.
The stream is primary; the POST is the fallback that still runs if the script fails to load, the
browser has no `EventSource`, or the stream dies before delivering anything.

**Both must never run for one submit** — that is two complete agent turns for one question: two
sets of model calls, two sets of queries, and two download offers for the same result set.

Two obvious fixes do not work, and knowing why saves rediscovering it:

- `preventDefault()` does not achieve it, because htmx's listener is on the form itself and
  `preventDefault` does not stop other listeners.
- Removing `hx-post` does not either, because **htmx captures the verb and path in a closure when
  it processes the node** — the attribute is only ever read at page load.

So the script listens for `submit` on `document` in the **capture** phase and calls
`stopPropagation()`, so the event never reaches htmx's handler, then issues the fallback itself
through `htmx.ajax()` with the same target and swap the form declares.

Three properties of `EventSource` shape every stream client in this codebase, each **observed in a
browser against this server** rather than assumed:

1. **It reconnects by itself.** A stream that ends — *including one that ended perfectly, having
   sent `done`* — makes the browser open it again, which re-runs the whole agent turn. Left alone
   it does this indefinitely. Only `close()` stops it, which is why the `done` handler closes
   **before anything that could throw.**
2. **Every close arrives as an `error` event carrying no data, success included.** A disconnect
   therefore says nothing about whether the turn worked; a `finished` flag is what separates the
   expected close from a lost connection. Without it, a completely successful answer can be
   replaced by "The connection to the agent was lost".
3. **A server-sent `error` event lands on that same listener, but with a payload.** That one is a
   sentence the service wrote for the operator, and is always shown. The payload is the *only*
   thing separating it from rule 2's empty disconnect, so a handler that does not branch on `data`
   cannot tell "the turn failed for this reason" from "the connection dropped" — and the widget,
   which answers the second by re-POSTing, spent a while answering the first that way too: **every
   failing turn ran and billed twice**, with the real reason replaced by the POST path's generic
   text. Its signature in `chatbot_messages` is an `error` and a `success` row written in the same
   millisecond for the same `visitor_message`.

The console renders the raw answer in a **monospaced block** rather than rendering Markdown,
because the point of the page is that the tools-called list and the answer can be checked against
each other — and because a proportional font made a Markdown table's columns wander, which was the
one thing `pre-wrap` could not survive.

---

# 14. Downloader Agents

The other half of the row cap: an answer capped at 100 printed rows plus the exact `COUNT(*)`,
and an offer to send the whole set as a file.

### Why it exists

A tool's rows were capped at 200 and handed to the model, and two things followed which were both
bad for the person reading the answer.

**The answer dumped whatever it got.** Nothing bounded how many rows the model printed, so a broad
question produced a two-hundred-row wall of text in a chat bubble. That is not an answer; it is a
data dump with a sentence on top.

**The count was a lie by omission.** There was no `COUNT(*)` anywhere on the path, so "200 rows
(capped)" was the most the model could know. It could not tell the user how many records actually
matched, and it had nothing to offer them instead.

So the display budget became a fixed number of rows, the total became an exact figure, and the
remainder became a file.

### The flow

```
a data tool returns > 100 rows
  → describe_tool_result → count_records (exact COUNT(*)) → create_offer
      → start_export_offer  runs the graph to interrupt(), returns the sentence
  → the agent repeats: "There are 125 records. Do you want me to create a
     downloadable CSV file containing the list of all the records."

user: "yes" → confirm_download → mark_queued + enqueue_export
            → the in-process worker → resume_export
                → read 50 → write part → … → merge → publish → cleanup
```

### The offer sentence is not the model's to write

Produced by `download_service.offer_sentence`, delivered as the payload of the graph's
`interrupt()`, and passed to the model with an instruction to repeat it **word for word**. Two
reasons that are really one: it contains the record count, and **a model rewording it is how a
user gets told the wrong number.** And it asks a plain yes/no question, which is what makes a bare
"yes" on the next turn something the application can act on.

`DISPLAY_ROW_LIMIT = 100` is also **the number the offer keys off** — a result of 100 or fewer
arrives whole with no download step in the way. It was 20 originally and was raised on request;
the number is a judgement about *reading*, not a safety limit (the safety limits are
`PROMPT_ROW_LIMIT` above it and `MAX_EXPORT_ROWS` on the file), so changing it is a one-line change.

A set past `MAX_EXPORT_ROWS` gets a refusal instead, naming the limit, and **no export row is
written at all**: there would be nothing to confirm.

### The graph

```
    START → count_records ──too large──→ notify_failure ──┐
                  │                                       │
                 ask                                      │
                  ↓                                       │
        await_confirmation ──declined──────────────────────┼──→ cleanup → END
                  │                                       │
              confirmed                                   │
                  ↓                                       │
          ┌── write_batch ──more batches──┐               │
          └───────────────────────────────┘               │
                  │            │                          │
               finished      failed ────────────→ notify_failure
                  ↓
            merge_parts ──failed──────────────────→ notify_failure
                  │
              publish_artifact ──────────────────────→ cleanup → END
```

| Node | Does |
|---|---|
| `count_records` | The exact `COUNT(*)`. **Refuses a set past the ceiling before anybody is asked**, because offering a file and then withdrawing it is worse than saying no up front |
| `await_confirmation` | `interrupt()`. The run stops here, its state goes to PostgreSQL, and the payload is the sentence |
| `write_batch` | Reads 50 records and writes one part file, both inside the retry |
| `merge_parts` | The format's own merge. The count that comes back is counted **from the files** |
| `publish_artifact` | Marks the export `ready` and sets its expiry. The download route serves `ready` and nothing else, so until this runs there is no way to fetch a half-written file |
| `notify_failure` | Stores one fixed sentence for the agent to relay |
| `cleanup` | Deletes parts, closes the cursor, drops caches. **Reached by every terminal path** — which is why it is a node with several inbound edges rather than a `finally` block that has to be right in five places |

**Why a graph and not a function with a loop.** Two of those edges are the feature.
`await_confirmation` is a genuine pause: it stops inside one HTTP request and is resumed by a
different task after a different request. And every way an export can end passes through one
cleanup node.

**Where the interrupt goes and comes back.** `start_export_offer` runs the graph in the request
that answered the question — it counts, it pauses, and the payload it returns is what the agent
says. The user's "yes" enqueues a job; the worker calls `resume_export` with the same `thread_id`
and the run continues from the pause. **The request side never builds anything and the worker side
never asks anything.**

### Reading the records: one cursor, both modes

Every statement is assembled by `query_executor` (`assemble_built_query` /
`assemble_sql_statement`), so an export reads exactly what the tool is permitted to read,
re-validated on this run, with the same active-table and active-column checks. **Nothing in the
module builds SQL.**

`LIMIT 50 OFFSET n` is the obvious way to read a set in batches and it is the wrong one here,
twice over:

- **it needs a total order or it is simply incorrect** — without one the database may return a row
  in two batches and another in none, and a grouped tool query does not always have a unique key
  among its output columns;
- **even with an order, the database re-runs and re-sorts the whole result for every batch.**
  500,000 records is 10,000 batches: ten thousand sorts of half a million rows, to read each of
  them once.

So both modes open one server-side cursor and pull 50 rows at a time. One pass, one snapshot,
every row exactly once, no ordering required — and it is what `_execute_sql_query` already does
for the row cap, held open longer.

**The cost, stated.** The cursor holds a connection and a read transaction for the export's whole
run. `MAX_EXPORT_ROWS` (default 500,000, `DOWNLOAD_MAX_EXPORT_ROWS`) is what bounds that — an
export nobody could finish is refused up front rather than pinning a connection for an hour. A
retried batch re-opens the cursor and discards its way back, which is linear in what was already
read and only ever happens on the failure path.

**Counting.** Builder mode wraps the statement in a `COUNT(*)`. SQL mode counts **by streaming**,
because the operator's statement cannot be wrapped (MySQL rejects a derived table with duplicate
output column names — the sort of query the mode exists to permit) and rewriting approved SQL is
not something this application does. The streamed count stops one row past the ceiling, so
`count_is_lower_bound` is true in exactly the situation where the export is refused anyway. Which
means **every count a user is ever shown is exact.**

### Batches, parts and retries

Fifty records per batch, `MAX_BATCH_ATTEMPTS = 3`, and the part file **deleted before each retry**.

**Why deleted first.** A batch fails somewhere inside writing its part — after the header, after
twenty rows, mid-row. What is on disk is then a fragment that *looks exactly like a part file* and
is not one. Deleting is what makes an attempt an attempt rather than an edit.

**Why it retries at all.** A batch reads from *someone else's* database over a connection this
application does not control: a dropped connection, a lock timeout, a failover. Those are
transient. What is **not** transient is a query that no longer validates or a table that was
switched off — those raise `ToolQueryError`, which is **not retried**, because three attempts at a
permanent failure is three times the wait for the same answer.

**Why it gives up out loud.** After the third failure the export stops. No partial file and no
"here are the first 2,000 records": an export that silently contains *some* of the data is the one
outcome worse than no export, because nothing about the file says so. `cleanup` then removes the
whole export directory — the parts *and* any partial artifact.

Discarded attempts are kept as `download_export_parts` rows. **Three rows with the same
`part_number` is what "this batch failed twice before it worked" looks like afterwards**; without
them a recovered export is indistinguishable from a clean one.

**The retry loop is inside the node, not around it.** Making it an edge
(`write_batch → discard_part → write_batch`) would mean a checkpoint write per attempt, a router
that had to tell "retry this batch" from "next batch" from "give up", and a crash mid-retry
resuming into a state the cursor no longer matches. A worker that dies is already handled one level
up, by the job being requeued.

### The three formats, and three load-bearing details

| Package | Writes | Merges by |
|---|---|---|
| `csv/` | `.csv` via the stdlib `csv` module | Concatenating bytes in 1 MiB chunks, keeping the first header |
| `xls/` | `.xlsx` via openpyxl `write_only` | Reading each part back `read_only` and streaming rows into one workbook |
| `parquet/` | `.parquet` via pyarrow | `ParquetWriter` over the last part's schema, one `write_table` per part |

`xls/` writes `.xlsx` — the folder name is the format as people ask for it, and legacy `.xls` caps
at 65,536 rows, which an export whose whole purpose is "more records than fit in a message" would
hit routinely. `csv` as a package name is safe because Python 3 uses absolute imports.

Three things were each found by a failing test and are load-bearing:

- **pyarrow and openpyxl are imported at module scope, never inside the worker function.**
  pyarrow's C extension must not be first imported on a thread that is later destroyed;
  `asyncio.to_thread` uses the loop's executor, so the first export in a process would initialise
  it on a pool thread and the next pyarrow call in a fresh loop would **segfault** in
  `ParquetWriter`'s constructor. The format registry already provides the laziness the
  function-level imports were for. (`frame_ops.py` imports polars at module scope for the same
  reason, and `TestOneImportSite` asserts there is exactly one import site.)
- **openpyxl creates no cell for a `None`**, so a record whose final column is NULL is saved
  narrower than the header and read back with that field missing entirely. Every row goes through
  `_rectangular`, in the writer *and* in the merge.
- **Parquet pins a schema per export**, derived from the first batch and widened one-way to text.
  Without it, a batch that happens to be all NULLs infers a null column that cannot hold the next
  batch's values, and the export dies thousands of records into a query that works everywhere else.

### The queue: a table, not a broker

A `download_jobs` row, claimed with `FOR UPDATE SKIP LOCKED`, drained by an asyncio task started
in `main.on_startup`.

**A locked row is a queue** that is durable across restarts, safe across processes, and visible in
the same database as everything it is about. What a broker would add is throughput this feature
will never need and a service to operate that it would not justify. There is no Redis, Celery or
arq in this project and this does not add one.

**In-process, deliberately.** One container to deploy, and the worker runs the same code the
requests do.

**One job at a time.** An export holds a cursor open against the user's own database. Draining two
would double that against a server this application does not own, to finish a background job
sooner than anybody is waiting for.

**A dead worker is recovered, not resumed.** `heartbeat_at` is written while a job runs; a job
whose heartbeat goes stale is requeued and the next worker **starts the build again from the
confirmation**. Starting again rather than resuming is deliberate: the dead worker's part files are
on disk and its cursor is not, and a resume would have to trust files it cannot verify were written
completely. The checkpointed *confirmation* survives either way — it is from before any file
existed.

### File layout, and why each key is what it is

```
uploads/exports/<export-uuid>/
    parts/part-000001.csv …          ← scratch, deleted once the merge succeeds

uploads/file_downloaders/<session-id>/
    items_2026-08-06.csv             ← the artifact, and what a visitor fetches
```

Two roots, because the two kinds of file have different lifetimes and different audiences.

**Parts are keyed by export**, because cleanup has to remove everything this export created and
nothing anyone else's created — and "everything under this directory" is a rule that cannot get
that wrong.

**Artifacts are keyed by chat session**, because a session's files being cleaned up has to be one
operation over one directory. Keyed by export uuid they would be scattered across as many
directories as the visitor asked for exports, and "remove everything this conversation produced"
would be a query rather than an `rmtree`.

The cost of that choice: two exports in one session can want the same file name, because
`artifact_name` is the table plus the date. `available_artifact_name` is what stops the second
overwriting the first — it returns `orders_2026-08-07-1.csv` when the plain name is taken, and the
name it returns is the one stored on the row and put in the URL. **Without it the first download
would serve the second export's bytes** — the same number of records, from a different query, with
nothing to show anything was wrong.

The session token is minted by the browser, so it is caller-supplied and **never joined onto a path
as it stands**: `session_folder` normalises it first, so a token of `../../etc` becomes a harmless
flat name, and `resolve_within_downloads` re-checks on **every request** that the row's stored path
still sits inside the folder the URL named.

### Expiry: two mechanisms doing different jobs

`DOWNLOAD_EXPORT_TTL_SECONDS` defaults to **30 minutes**. Short on purpose, with a consequence
worth stating plainly: a visitor who closes the tab and comes back an hour later has no file. That
is the intended trade — asking again is cheap, and a server that keeps every export anybody ever
requested is an archive nobody asked for.

- **The download route refuses a lapsed export** on every request. This is the *rule*, and it is
  what makes the window exact rather than "thirty minutes, give or take however long since the last
  sweep".
- **`expire_lapsed_exports` deletes the bytes** and marks the row `expired`. It prunes the session
  folder when that was its last file — a session that asked for fifty exports would otherwise leave
  fifty empty directories per visitor, forever — and removes the export's own directory, normally
  already empty but left behind by an export that failed after writing parts. This is the
  *housekeeping*, run by `run_expiry_reaper` at `REAPER_INTERVAL_SECONDS`: **a tenth of the TTL,
  floored at a minute and capped at a quarter of an hour**, so it is derived from the TTL rather
  than being a second number to keep in step.

**The row is kept rather than deleted**, so a visitor returning to a dead link is told the file
expired and that they can ask again; a missing row produces "that download could not be found",
which reads like the application lost it. `has_lapsed` treats "the clock has passed" and "the
reaper has already swept" as **one state** — those are two states minutes apart, and checking the
row's status before the clock is how the second used to fall through to the wrong sentence.

Everything that states the lifetime to a user derives it from the setting through `ttl_phrase()`.
The agent's "available for the next 30 minutes" was hard-coded as *24 hours* once, which was true
and then silently was not — **a user told a file lasts a day when it lasts half an hour is worse
served than one told nothing.**

### Authorisation: two controllers, two audiences

`DownloadController` — `require_auth`, ownership resolved export → data agent → `user_id`.

`PublicDownloadController` — no session and no cookie. The chatbot key's **uuid** *and* the
conversation's `session_token`, both required. **The token is the part that matters:** a widget key
identifies a public website, not a person, so a key alone would let any visitor of that widget read
every export ever produced for it. The key's **uuid** is used rather than its publishable
`api_key` because this link is spoken aloud into a chat transcript, and a link carrying the
widget's credential would put that credential in the transcript.

The same rule applies inside the conversation: `confirm_download` and `download_status` resolve an
export against the *asking* conversation, so a model handed another visitor's uuid finds nothing.

Note the asymmetry in the visitor's three routes. **The file** is named by session and file name,
because that is how it is stored on disk — so the URL and the directory are the same two facts and
cannot drift apart. **Progress and status** are named by export uuid, because both are asked
*while the export is being built*: there is no file name yet, and finding out when there will be
one is the whole point of the call.

`GET /public/downloads/{export_id}` still serves the file too. Nothing this application generates
points at it any more, but a link handed out before the move is in somebody's chat transcript, and
breaking it would turn a working download into an error for no reason the visitor could understand.

### Streaming, three surfaces

**The download.** `Stream` over a 64 KiB async chunk generator with
`Content-Disposition: attachment`. The repo's only previous download built its content in memory,
which is right for a 4 KB script and wrong for a file that can be hundreds of megabytes.

**Build progress.** `GET …/events` emits `progress` per completed part, `retry` per failed attempt,
then `ready` or `failed`. Read from the `download_export_parts` **rows** rather than an in-memory
bus, because the worker writing the files and the request streaming the feed are different tasks —
and under more than one replica, different processes. A browser that reconnects halfway through
sees the whole story. **A retry surfaces as its own frame:** it is the difference between "this
export is big" and "this export is struggling", which is the only question somebody watching one
has.

**The agent's answer.** `astream_events` rather than a single blocking `ainvoke`, exposed as SSE.
An agent turn runs real queries and can take a minute; a spinner that says nothing for that long is
indistinguishable from a hang, so `tool` events say which tool is running and `token` events paint
the answer as it lands.

Both blocking endpoints remain the fallback. A turn that cannot stream — an active Flow Builder
node, or a chatbot with no data agent — yields one `fallback` event and the client posts instead.

One detail: **`_chunk_text` does not strip.** A chunk boundary falls wherever the provider's
tokeniser put it, very often on a space, so trimming each chunk concatenates "Here" and "are" into
"Hereare". Whitespace inside a stream is content.

### The download card

Not a link in a sentence — a block under the reply, showing progress and then a button.

**The words rotate; the numbers do not.** `WORKING_WORDS` cycles every 2.6 s under a shimmer,
because a long build with a static line reads as a stuck one. The figures come from the progress
stream and are exactly the records written so far. The bar is capped at **99 %** until the artifact
exists — a full bar next to "still working" is the one thing a progress bar must never say.
`prefers-reduced-motion` drops the shimmer and keeps the words.

**The card is not part of the message.** It is its own block, appended after the bubble, and it
outlives the turn that created it. That is what makes the next line literally true: **a visitor can
keep asking while the file is written.** Nothing in the card touches the input, the send button or
the typing indicator — there is a test that asserts exactly that, by reading the card's source for
those four names.

**The button is an anchor, not a button.** `<a href download>`, so middle-click, "save as" and
keyboard Enter all behave. A `<button>` with a click handler looks identical and does none of them.

### The relative-URL compatibility boundary

**The server sends paths. It must keep sending paths.** This is a compatibility boundary rather
than a style preference: `widget.js` is *downloaded* — the operator saves it and hosts it on their
own website — so the copy running in a visitor's browser can be arbitrarily older than the server
answering it. Every version of it does `API_BASE + url`. That is correct for a path and
catastrophic for an absolute URL: `https://api.example.com/https://api.example.com/…` is a string
the browser never sends, **so nothing reaches the access log, nothing throws, and nothing is
logged anywhere.**

That is not hypothetical. `SITE_URL` was prefixed onto these URLs to fix a download link that had
resolved against the embedding page, and it left every progress card stuck on *"Gathering the
records…"* forever — the export finished in four seconds, the file was written, the row said
`ready`, and the browser never asked. **The only symptom was silence.**

Naming the host is the embed snippet's job: `apiBase` is the one piece of configuration that lives
next to the script actually running, so it cannot disagree with it. A server-side declaration of
the same fact is exactly what goes stale when a tunnel rotates or a domain changes.
`download_service.site_url()` still exists for server-side callers with no request to derive a host
from, but **nothing the widget consumes goes through it.**

`apiUrl()` is the belt to that braces: it passes an already-absolute URL through untouched and
prefixes `API_BASE` onto a path, so a future server that *does* send an absolute URL cannot
double-prefix it on any script from this version onward. `test_widget_script.py` sweeps every
`EventSource(`, `fetch(` and `.href =` in the card and requires `apiUrl(` in each, **so a fourth
network call cannot be added without one.**

### How the interface finds out about an export

A tool returns a string to the model and nothing else — that is LangChain's contract — and the
layer that renders the reply is four calls above the layer that queues an export. So
`confirm_download` and `download_status` record the export in `base/download_notice.py`, a
context-local read once at the top of the turn and attached to the reply as `download`.
`chatbot_turn_service` opens one `download_scope()` per turn and reads it.

Two things about that module are load-bearing, and both are §12's rule 3 in practice: **it holds a
mutable box, not a value**, and **the scope is reset per turn** — a notice left set is a download
shown to whoever asks next, which is worse than showing none.

**The model is never given a URL, in any state.** It is told a button is already on screen. Two
reasons: a second copy of a control the user has is worse than none, and the answer renders as text
— so a model writing markdown produces a visible `[Download CSV](/public/…)`, which is precisely
what it used to do. Grounding rule 10 says so in the prompt, and `_describe_status` **no longer has
a URL to leak.**

**When the stream drops, the card polls.** A build can outlast one SSE connection —
`MAX_STREAM_SECONDS` bounds ours at an hour and a proxy may bound it harder. On a data-less `error`
the card **closes the socket first** (a browser reopens a stream that ended on its own, which would
re-run the progress feed forever), then falls back to `…/status` every four seconds, warning the
operator's console **once**.

The operator console does **not** draw a card. It has no conversation history, so it cannot resolve
a "yes" in the first place; the notice carries console URLs for the day that changes, and nothing
renders them yet.

### Failures are answers

| What happened | What the user is told |
|---|---|
| Result larger than the ceiling | *"There are 1,200,000 records, which is more than the 500,000 this application can put into one file. Please narrow the question down and ask again."* |
| Three failed attempts, or a failed merge | *"The file cannot be created at the moment. Please try again."* The export is `failed` with no artifact — **never `ready` with a missing file** |
| Expired | *"That download has expired. Please ask for it again."* |
| Someone else's export, an unknown uuid, a missing file, a path outside the export | 404, *"That download could not be found."* — **the same sentence for all four**, because distinguishing them confirms which uuids are real |

The failure message is **fixed**. The real reason — a dropped connection, a lock timeout, a driver
error — is not something to put in front of a visitor, and "try again" is the only useful
instruction either way. The real reason goes to `error_message` and the log.

**An offer that cannot be prepared is not mentioned at all:** the user still gets their answer, and
the log gets the reason. An export is an extra; the answer is not.

---

# 15. Whole-result aggregation

Reading every record a tool returns and grouping it in memory, rather than asking the database for
a grouped query.

### The honest framing first

A builder-mode tool already pushes `count`, `sum`, `avg`, `min`, `max` plus a `GROUP BY` into SQL.
A SQL-mode tool runs any read-only statement including percentiles, window functions and pivots. In
both cases the database does the grouping over its own indexes, in one pass, exactly, with no
ceiling.

**For those cases this module is a slower reimplementation of `GROUP BY` and should not be used.**
It earns its keep in three situations only:

- re-grouping the output of an approved SQL statement, where the statement is fixed and the
  question that arrived is about a different axis of it;
- deriving a measure the operator's statement did not produce, without editing an approved
  statement;
- a datasource the operator can read but not tune — no index, no permission to add one, and a
  server-side `GROUP BY` that times out.

That is why it is **off by default and switched on per tool config** rather than being something
every agent can do. It lives on `tool_configs` rather than `data_agents` because it is a judgement
about one query.

### The shape

```
START → get_count → read_wave ──Send×4──→ aggregate_slice ─┐
                        ▲                                  │ (barrier)
                        └───────── merge_wave ◄────────────┘
                                        │
                                finalise → cleanup → END
    any failure ──────────→ notify_failure → cleanup → END
```

One wave is `AGGREGATE_WAVE_WIDTH` slices of `AGGREGATE_CHUNK_ROWS` — **four batches of 200, so
800 records** — read in order, then folded concurrently, then merged into the running aggregate.

**The divider reads; the workers aggregate. This division is forced, not chosen.**
`record_reader.BatchReader` is **one server-side cursor**: `read()` advances it, and asking for a
batch out of order re-runs the statement and rescans from the top. Two tasks reading it at once
would turn a linear scan into repeated full rescans, or collide inside the driver. So `read_wave`
reads its whole wave itself, sequentially — which is cheap, being four `fetchmany` calls on an
already-open cursor — and what fans out is the **folding**. A wave costs `read + max(fold)` rather
than `read + Σ fold`.

**The barrier is free.** Every `Send` returned by one router runs in a single LangGraph super-step,
so the plain edge out of `aggregate_slice` schedules `merge_wave` exactly **once**, after every
worker of that wave has written. There is no barrier to implement, and writing one would be writing
a second, worse one. *(Asserted: `partial_aggregate` is instrumented, and every slice of a wave
must finish before that wave is merged.)*

### One tool may be several sources

`record_sources(entry)` turns one tool entry into the things the reader reads:

| The tool | Sources |
|---|---|
| No children | one, unrestricted |
| List children | one, **carrying the children's values** |
| An iterating child | **one per value** — same statement, different bind, its own label |
| A chain that matched nothing | none, and `stopped_by` names the tool |

`ChainedBatchReader` presents that list as one reader: one cursor at a time, rolling to the next
when the current is exhausted, returning nothing only once **every** source is spent. **A source
that legitimately matches nothing rolls forward rather than ending the run** — otherwise a
department with no projects would silently truncate the answer at that department. `count_all` sums
across them and checks the ceiling before anything opens.

Nothing in `partial_algebra` or `frame_ops` changed for this: the fold is already order-independent
and mergeable, so N cursors read in order fold into one running frame exactly as N waves of one
cursor do.

**This closed a real bug.** Before `record_sources` existed, the source was built from the tool's
stored config alone and **dropped the chain's values** — so an aggregation over a nested tool
totalled a *wider* result set than the tool has ever returned, with nothing about the answer saying
so. `test_aggregate_sources.py` exists to keep that from coming back.

### Why a filter may live *inside* the fold — `filter_algebra.py`

The narrowing half rests on a second identity, and it is the reason a condition is applied per
batch rather than in front of the pipeline:

    filter(b₁ ⧺ b₂) == filter(b₁) ⧺ filter(b₂)

True for any **row-wise** predicate — one decidable from a single record. That is the entire
admission test for the operator vocabulary, and it is what excludes the four things somebody will
ask for next: `> the average`, the top N, the latest per group, and a semi-join against another
result. Each is a fact about the *set*, so per batch it would be computed against that batch —
`amount > average` filtered batch one against batch one's average, which is a plausible answer and
the wrong one. Filtering in front of the pipeline instead would mean materialising the whole result
set first, which is what the wave loop exists to avoid.

The same split as the fold: `filter_algebra` owns the rule with no polars import, `frame_ops` says
it in polars. Asserted against SQLite at five batch sizes.

**One refusal in it is worth copying elsewhere.** A date part on a text column is parsed
**strictly**. `str.to_datetime(strict=False)` turns anything unreadable into null, a null has no
month, and the filter then matches *no records* — "there was no revenue in March" is a sentence
somebody repeats in a meeting. So it raises, and the message quotes **the value that actually
failed** rather than the first value in the column: in a column of ISO dates with one `"n/a"` in
it, the first value is a date and the message would be false.

### Two kinds of source behind one interface — `row_supply.py`

The pipeline counts, opens and reads batches. A tool config is a **server-side cursor**; a designed
graph's result is **already in memory**, produced by running it. Neither is the other's special
case — a graph has nothing to probe for its columns, and no cursor to release — so both implement
`count` / `open` / `release` and the graph's nodes stop knowing which they hold.

The materialised side keeps the cursor's contract rather than something like it. Two details are
copied from `record_reader.BatchReader`, not invented: batch numbers start at **1**, and an
**empty** batch means exhausted while a **short** one does not. The second is the one whose failure
is silent — a run would stop at the last full batch and report a total a few records short.

### Why the answer is exact — `partial_algebra.py`

A batched aggregate equals a single-pass one **if and only if** every aggregation is an associative
fold over a carried intermediate. **The carried intermediate is not always the answer**, and that
gap is the whole subject of the module.

| Requested | Carried per group per slice | Merged by | Finalised as |
|---|---|---|---|
| `count` (records) | `n` | sum | `n` |
| `count(col)` | `c` — non-null count | sum | `c` |
| `sum(col)` | `s` **and** `c` | sum | `s` if `c > 0` else NULL |
| `min` / `max` | `mn` / `mx` | min / max | as carried |
| `avg(col)` | `s` **and** `c` | sum | `s / c` if `c > 0` else NULL |

```
slice 1: [10, 20]     carries (30, 2)
slice 2: [60]         carries (60, 1)
merged:               (90, 3)  →  avg = 30      correct
mean of means: (15 + 60) / 2 = 37.5             wrong
```

Three rules that are easy to get wrong:

1. **`avg` divides by the non-null count of the averaged column, never the group's record count.**
   SQL `AVG` ignores NULLs, so dividing by the record count turns "the average order value across
   the 40 orders that have one" into "…across all 100" — a number that looks entirely reasonable
   and is wrong.
2. **`sum` over an all-NULL group is NULL in SQL and `0` in polars.** That is why `sum` carries a
   count it does not appear to need: without it the answer reads "£0 of revenue" where the database
   would say "no revenue recorded", and those are different facts.
3. **NULL is its own group** in both SQL `GROUP BY` and polars `group_by`. The two already agree, so
   nothing substitutes a sentinel — `"null"` would collide with the literal string and silently
   merge two groups.

### What is refused rather than approximated

`median`, `percentile`, `mode` and `count_distinct` have **no bounded fold**: an exact answer needs
every value, or every distinct value, resident at once — at which point the batching bought nothing
and the memory ceiling is gone.

**The refusal is unreachable by construction rather than merely enforced:** `validate_plan` checks
every function against `AGGREGATION_FUNCTION_VALUES` from the tool-config model, which is exactly
the foldable set, and **a test asserts the two sets are equal** — so a sixth function added to the
tool config vocabulary fails that test until somebody comes here and gives it a fold.

`stddev` and `variance` *are* decomposable, through Chan's `(n, mean, M2)` merge. They are absent
only because they are not in the tool config vocabulary; if they are added, `partial_algebra` is the
file that gains them.

Two more refusals: **grouping by a float column** (`NaN != NaN`, `-0.0 == 0.0`; two values that
display identically are not necessarily equal), and **averaging what the tool already averaged**
(mean-of-means through the back door, because the first mean happened in the database where the
counts behind it are gone). **In SQL mode the second is undetectable**, because nothing parses
operator SQL anywhere — a real limitation, stated rather than papered over.

### Planning: cheapest path first

1. **Choose the tool.** A `tool_name` that resolves case-insensitively, or a single available tool,
   means **no LLM call at all** — and between them those cover nearly every real request, because
   the agent was told the tool names before it was asked anything. Only an ambiguous choice costs a
   call, over a catalogue of names, descriptions and tables, **and no records**.
2. **Read the real columns** with `probe_tool_query`: one row fetched, column **names only, no
   values**, applying every validator and every active-table/column rule the real run will. It is
   also the only way to know the columns at all — a builder config with an empty selection means
   *every active column*, and a SQL statement is not parsed.
3. **One structured-output call.** The allowed functions are rendered into the prompt from
   `sorted(SUPPORTED_FUNCTIONS)`, so the prompt cannot drift from the validator. **The model is
   given the words to decline** (`unsupported: true`), because a model with no way to say "I can't
   express that" will produce a plan that is *shaped* right and answers a different question.
4. **Validate, whatever produced the plan.** Every column is matched case-insensitively and then
   **replaced by the probed spelling** — polars matches names byte for byte, so `Region` left as
   typed is the difference between a grouping and a refusal three nodes later, where the column can
   no longer be explained. **Aliases are assigned here, never taken from the model**: an alias is an
   output column name, and one colliding with a group key would overwrite it.

**There is no internal retry.** A refusal names the tool's real columns and goes back as a tool
failure; the agent's own loop is the correction path and it already exists. Re-asking here would
spend a second call to make the same mistake more expensively.

### Ceilings

| Setting | Default | Bounds |
|---|---|---|
| `AGGREGATE_CHUNK_ROWS` | 200 | Records per slice |
| `AGGREGATE_WAVE_WIDTH` | 4 | Slices folded concurrently |
| `AGGREGATE_MAX_SOURCE_ROWS` | 200,000 | Records a run may read |
| `AGGREGATE_MAX_GROUPS` | 100,000 | Groups the running aggregate may hold |

| Cap | Where | What happens |
|---|---|---|
| Too many records | `get_count` | Refused **before a single record is read**, naming the real count and the ceiling, and suggesting a SQL-mode tool — which has no such limit |
| Too many groups | `merge_wave` | Aborted, and the running aggregate is **discarded, not returned**. A list of the first hundred thousand groups looks exactly like a complete answer |
| More than 200 result rows | `finalise` | **Sorted first, then capped**, then rendered through `describe_result`, which already says "200 rows out of 4,821" |

`finalise` sorts — first aggregation descending, group keys ascending as a stable tiebreak. A hash
`group_by` returns groups in arbitrary order, so without this the same question gives a
differently-ordered answer each time. **Every group is returned**; the 200-row ceiling that used to
sit here was the worst cap in the application, since this feature exists to be exact and then
reported the first 200 groups of however many there were.

**Why 200 records a batch:** a slice is an internal unit of work and nothing but memory depends on
its size — the fold is associative, so the same numbers come out whatever it is. It is small, and the cost is worth stating —
200,000 records is 250 waves and a thousand round trips, against eight waves at 25,000. At this size
polars' fixed setup per slice dominates the aggregation itself, so the fan-out buys little and the
run is round-trip bound. **The answer is exact either way.** It is an env var precisely so the trade
can be measured on real hardware rather than argued about.

**Why the ceilings exist at all:** a run holds one database cursor open for its whole length and
happens inside a chat turn (120 s for a visitor, 900 for the console). A run nobody can finish is
refused before it starts rather than abandoned halfway. Raising `AGGREGATE_MAX_SOURCE_ROWS` without
measuring turns a working answer into a timeout; if millions of records are genuinely wanted, the
answer is the `job_queue` pattern, not a bigger number.

### One tool, not one per config

`aggregate_records` is a single tool. Every other tool in this application is a standing permission
with a fixed question — the model chooses *which* tool, never what it asks. This one takes an
instruction, which is the opposite shape, and minting a variant per tool config would put several
free-text tools in front of a model that the grounding rules have just told to pick the single tool
matching the question.

**It still cannot choose its own query.** The instruction decides the *grouping*, not the SQL: the
tool config's stored query runs, re-validated on this run like any other, and the plan is checked
against the columns that query actually returns. **The model widens what can be asked of a
permitted result set; it does not widen the permission.**

### Off by default is what makes it additive

With no tool opted in, `aggregate_context` returns `None` so `build_agent_tools` binds nothing
extra, and `_aggregate_note` returns `""` so the generated routing prompt is **byte-identical** to
the one that agent had before the capability existed. Both are asserted by
`TestNothingChangesWhenItIsOff`.

Switching the flag on moves `tool_configs.updated_at`, so `is_prompt_stale` rebuilds that agent's
prompt on its own — no extra sync step and no staleness marker were needed.

### Tests that carry the module

- **Exactness.** A real SQLite datasource of 12,347 records with a skewed distribution and a column
  full of NULLs, compared group by group and value by value against `SELECT … GROUP BY …` **run by
  SQLite itself** — not against a Python re-implementation, because the promise is that the answer
  matches what the database would have said.
- **Fan-out width does not change the answer.** The same fixture at widths 1, 2, 3, 4 and 7;
  likewise batch sizes 1, 7, 200 and 5,000. **This is the test that proves the parallelism is safe.**
- **Every record is read exactly once**, checked at every batch and wave boundary (1, 199, 200,
  201, 799, 800, 801) — a dropped tail batch shows up as too few, a re-read one as too many.
- **Every terminal path releases.** An autouse fixture asserts both registries are empty after
  every test, with named tests for success, refusal, worker failure and **cancellation**.

The reader's registry key is prefixed `agg:` so it cannot be mistaken for an export's uuid in the
registry the two features share.

---

# 16. Graph Designer

A canvas where a LangGraph is *drawn* out of SQL statements, literal values, existing tool configs,
branches, loops and questions put to a person — then run, whole or in part, with the flow, the state
and a capped output in a dock below.

Three tables: `tool_graphs` (the drawing), `tool_graph_runs` (one execution), `tool_graph_run_steps`
(one node, one pass).

### Why it exists

Tool Graphs draws graphs it did not author — derived from links every time the page opens. So the
only graph a user could *build* was a nested tool chain, authored through repeating form rows, and
that shape expresses exactly one idea: *the child's values restrict the parent.* There was no way
to say:

> run this SQL, loop over what it returns, ask me to confirm before the last step, and take a
> different path if nothing matched.

Every one of those is control flow, and control flow is a drawing.

### The vocabulary lives in one place

`graph_service` owns it, the routes send it to the browser, and the canvas builds its palette **and
its property forms** from what it was sent. A palette offering a node type the service refuses is a
form that can only be filled in wrongly, so **there is no second copy in JavaScript.**

That is also why `GraphSaveRequest` deliberately does *not* pin the node types: declaring them in
two places would mean two edits every time one is added, so the schema checks only what can be
decided without the vocabulary.

The three Value kinds — `list` (flat scalars, the shape an `IN` takes), `array` (nesting allowed),
`dict` (named values) — exist separately because **they validate differently, and validating them
alike lets a shape through that the node downstream cannot use.** A `dict` where a `list` was
promised would surface as an `IN` built from an object.

### A SQL node must declare its tables

Not bureaucracy. Nothing in this application parses a raw statement, so
`query_executor.require_active_tables` can only honour the list the operator recorded. A node with
no declared tables would run with the active-table check **silently skipped** — which would make a
graph a way *around* the Data Sources switches rather than a way to use them.

`validated_tables` and `validated_tool_sql` are the tool config form's own validators, so the same
statement declares the same tables in both places and a read-only violation is described in words
the operator has already seen.

### Conditions are compared, never evaluated

Every operator is a name from a fixed table and the comparison happens in Python. **There is no
expression language on this path and nothing reaches `eval`**, so a graph cannot be used to run
arbitrary code even by its own author. Same decision as the flow engine's condition evaluation.

**`0` and `False` are not empty.** A SQL node returning a count of zero has produced a real answer,
and treating it as empty would send a graph down its nothing-found path when the thing it found was
zero — which is why `_is_empty` is a function rather than `not value`.

### The cycle rule

This is the rule worth the most explanation, because **the obvious version is wrong in both
directions.**

Banning cycles bans loops, and a loop is the thing the user asked to be able to draw. Allowing them
lets a plain `A → B → A` compile, and that run has no cursor and no ceiling — it stops when
LangGraph raises `GraphRecursionError`, which arrives as an internal error a long way from the two
edges that caused it.

So: **cut every edge out of a loop node's `body` port, and require what is left to be acyclic.** A
loop's back edge is the edge that closes its cycle, so removing the body edges removes exactly the
cycles a loop is responsible for bounding, and anything still cyclic afterwards is a cycle nobody
bounds. A cycle through a loop's `done` port is refused too — `done` is the way *out*.

The walk is **iterative with an explicit stack**: a graph has no node ceiling, and a recursive
depth-first search would hit Python's recursion limit on a long chain. (A 1,200-node chain is an
explicit test case, as is a 200-node graph.)

### There is no cap on nodes or edges

Deliberately. `FlowGraphSaveRequest` caps a conversation flow at 500 nodes because a flow that large
is a runaway client; **a data pipeline is not.** What bounds a *run* is the per-loop iteration
ceiling — a bound on *work* rather than on *drawing*, and the one that actually protects anything.

### Validation runs on save, publish and run

`validate_graph` is called by all three. **A run that validated more loosely would be a run of a
graph its author could not have stored.**

Every message names the node **by its label**, because the person reading it is looking at the
drawing and a generated id like `n_msoez780_1` means nothing to them.

### Every node gets a conditional edge

That is the central compilation decision and it is what makes the rest simple: one router per node
answers one question — where does the run go from here — and answers it for the ordinary case, the
branch, the loop and the failure **in the same place.** Mixing `add_edge` for "simple" nodes with
`add_conditional_edges` for the rest would mean a node that gained an error path had to change edge
*kind*, and the failure path would be the one that never got tested.

### How a failure travels — two channels, not one flag

A runner raises `NodeFailure`. The wrapper catches it and writes **one of two things**, and which
one depends on whether the author drew an error path for that node — a fact known at **compile
time**, so the wrapper is *told* it rather than working it out:

- an error path exists → `errors[node_id]`, and the router takes it. **The run is not marked
  failed**, because the author said what to do about it.
- no error path → `failed_at` / `failure_message`, and the router ends the run.

Two channels because "this node failed and we handled it" and "this run failed" are different
facts, and one flag cannot hold both. With a single flag, a graph that recovered from a failed node
would still report the whole run as failed — **the opposite of what drawing a recovery path means.**
Both halves are asserted, on the same broken node.

### Loops, and human-in-the-loop

A loop node is entered once from the START side and then re-entered by its own back edge. Its runner
tells the two apart by `started` on the cursor, which is why loading the list and advancing it are
**one function rather than two nodes**: the drawing has one box there, so the compiled graph has one
node there.

**A ceiling refuses rather than truncating** — the same argument `MAX_CHAIN_ITERATIONS` is written
about. The run stops and names the node, with *no* body pass recorded.

For a Human node, `interrupt()`, and the run's state goes to the checkpointer. Two things are
load-bearing:

**The pause is in the compiler, the handling is in the runner.** `interrupt()` unwinds the whole
call, and on resume LangGraph **re-runs the node from the top** — so anything before the interrupt
happens twice. The step row is written *after* it, which is what keeps the log from showing the
question asked twice. There is a test asserting the human node writes **no** step until it has an
answer.

**The answer is validated before the run resumes**, so an answer that does not fit is refused while
the person is still looking at the prompt. Resuming and failing a node three steps later would be
technically equivalent and much less useful.

### Testing part of a graph

**Testing a node, testing a group and running the whole graph are the same function** with a
different `scope`. That is the guarantee the feature rests on: *a node that passes a test is the
node that will run* — the same insistence Query Test makes.

A selection compiles as the **induced subgraph**. Choosing nodes that are not connected is an
ordinary thing to do ("does this query work, and does that one"), so the disconnected pieces are
chained in the drawing's topological order, worked out at compile time.

A node in the selection that reads a node *outside* it **fails, naming what is missing.** A
`for_each` over an absent list would otherwise loop zero times and report success — **a green tick
on a test that tested nothing.** Nodes left out get a `skipped` step row, because a node missing
from the log is indistinguishable from one the run never reached.

### Why a run is a background task, and why no queue

A graph queries somebody else's database and may pause on a question for as long as a person takes
to answer. A request holding the run would time out or hold a worker for minutes, and a paused run
has no request to belong to — the answer arrives in a *different* one. So the request starts a task
and returns a handle.

Same division `downloader_agents` makes. It does **not** add a queue: an export is background work
nobody is waiting for, whereas **a run is watched live by the person who pressed the button.** A run
in flight when the process stops is cancelled, not resumed.

### The dock

Three tabs — Output (one row per step: node, type, pass, status, duration, capped preview on
expand), State (what the run knows after the selected step), Log (the timeline). Node status is
painted back onto the canvas as frames arrive, which is what makes the page **a monitor rather than
a diagram.** A node inside a loop has one step row per pass and one box, so the box shows its latest
pass and the dock lists them all.

**Why the log is rows in a table:** the task driving the run and the request streaming the dock are
different tasks — and behind more than one replica, different processes. An in-memory bus works only
in the configuration this application is not guaranteed to run in, and a browser that reconnected
mid-run would see half the story.

**Every frame is a whole state, not a delta.** A client that missed one is not left with a wrong
picture, and the polling fallback consumes the same shape as the stream — so a dock whose connection
dropped does not have to understand a second payload.

**Output is capped where it is written.** `preview_of` caps before the row is written, so it is a
property of the table rather than of one renderer that has to remember. Without it, a graph over a
two-hundred-row query would put that result set into PostgreSQL once per node and once per loop
iteration — **a log that grows faster than the data it describes.** A preview always states the
**real** count, not the sample size.

### The fourth EventSource rule

`graph_designer.js` honours the three rules from §13 and added one: **the server names every frame
after the run's status, and a named SSE event does not reach `onmessage`** — which fires only for
unnamed `message` frames. Listening on `onmessage` alone is a dock that never moves while the run
completes perfectly well behind it. `FRAME_EVENTS` is the list; `_event_name` in the route is its
other half.

**That bug was found by driving the page in headless Chromium**, not by a Python test.

### Four owners, one classifier

A published graph can be run by a data agent as one of its tools, by every agent in a workspace it
is shared with, by a tool config that embeds it as a nested child, and by a `run_graph` node in a
conversation flow. All four need the same three questions answered — finished, asked something, or
failed — and only the wording differs, so the answering happens **once** in `graph_runner` as a
`GraphOutcome` and each owner phrases it for its own audience.

> **A pause is an outcome, not an error.**

That is the decision the shape rests on. None of the four can treat it as a failure, because
nothing failed; none can ignore it, because the rows they wanted do not exist yet. Failures are
likewise *returned* rather than raised, because every owner is mid-something — a conversation turn,
a parent tool's query, a flow — and raising hands somebody a 500 for a state that could have been
explained.

`graph_runner` was extracted from `graph_tool_factory`, which had the classification and the model's
sentences tangled together — so a second owner had to either import a model-facing string or write
the polling, the pause detection and the failure handling again. Both are ways for two callers to
disagree about what a paused run *is*.

**`GraphOutcome.rows` is a sample; `graph_runner.full_result` is not.** The first comes off
`result_preview`, capped at twenty rows with the real total beside it, which is right for an owner
*describing* a result and wrong for one that will **use** the values: a tool config restricted to
the first twenty of five hundred ids answers a different question than the one asked and looks
exactly like an answer. `full_result` reads the uncapped output from the checkpointer — recompiling
the drawing to get at it, the move `_resume` already makes, and re-running nothing. Storing the full
output on the run row was the alternative, and it is precisely what `preview_of` exists to prevent.

### Attached to an agent, or shared with a workspace

Callable when **both** switches are set: `is_active` and one of the two attachments — the same rule a
conversation flow enforces, so a graph can be parked mid-edit without being detached, and a draft
can sit attached while it is finished. **Attaching a draft is refused rather than
accepted-and-ignored:** a control that appears to work and does nothing is worse than one that says
no.

**The two attachments are mutually exclusive, and setting either clears the other.** Holding both
hands one agent the same graph twice — once as its own, once through its workspace — and a model
offered two identically named tools cannot choose. Clearing rather than refusing is what the
operator meant by pressing the second control. Enforced in `graph_service` rather than by a
constraint, because the rule has a sentence to say.

`data_agent_id` is unique; `workspace_id` deliberately is **not**, because a workspace is a team's
shelf and an agent added to it next month inherits every graph on it with nobody attaching anything.

`fetch_agent_graphs` knows both routes, and the workspace half is a **correlated subquery** rather
than a join: an agent in no workspace reads `NULL`, and matching `workspace_id` on both sides would
hand every unshared graph to every unassigned agent, because `NULL = NULL` was never the question.

**One tool name per shelf.** `_graph_tool_name` collapses every non-alphanumeric character, so
"Monthly revenue" and "monthly-revenue" are two permitted names — graph names are already unique
per user, case-insensitively — that become one identifier. The second is refused, quoting the first.
Checked from the destination and **including drafts**, since a colliding draft becomes a live
collision the moment somebody presses Publish, and refusing it then, from a different control, is
how a refusal ends up looking arbitrary.

### A graph as a tool config's nested child

`tool_config_links` carries two nullable child columns with `ck_tool_config_links_one_child` making
"exactly one" true rather than conventional. A CHECK and not a service rule, unlike the attachment
exclusivity above, and the difference is the audience: that one has an operator who needs a
sentence, while a link with two children or none is a bug in this application rather than anything a
form can produce.

The chain gains a **third outcome**: `ChainResult.asked`. Answering it resumes the graph and then
**re-runs the chain with the graph's values supplied** (`run_chain`'s `resolved`), because
re-running the graph would ask the same question of somebody who has just answered it.

Two rules are unanswerable rather than waived. A graph has no single datasource to compare against
the parent's — its nodes each name their own — and nothing knows what its last node returns until it
runs, so the child column is checked for shape and taken at its word, exactly as a SQL-mode tool
config's is.

**The cycle rule here prevents a hang, not a wrong answer.** A graph's `tool_config` node runs that
tool *including its chain*, so `tool P → graph G → tool P` is unbounded recursion across separate
LangGraph runs, where neither run's recursion limit nor any loop ceiling applies to the other.
Nothing would report it; the turn would never end. `_graph_reaches_tool` walks **both kinds of
edge**, because the cycle can alternate between them.

### A graph as a flow step

`run_graph`, with `default` and `error` ports. A failure takes `error` if one is drawn and otherwise
signs off — **never a silent hop to `default`**, because a flow carrying on as though a step had
succeeded is how a visitor gets told something untrue.

A question ends the turn and parks the run on `chatbot_flow_sessions.awaiting_graph_run`, which
`advance_flow_session` checks **before anything else reads the message**: the session is sitting on a
Run-Graph node, which the ordinary waiting-node path knows nothing about, and running that node
again would ask the same question twice. Its own column rather than a reserved key in `variables`,
because that dict is the visitor's namespace and is interpolated into message text.

The variable a Run-Graph node fills holds `total_rows` — a **count**, and the real one. A flow
variable is text that goes into a message and is compared by If/Else, so a result set in one would
produce a chat bubble of JSON; and `len(rows)` would be the preview's length, which is how a visitor
gets told "20" when there were 5,275.

A graph's human name becomes an identifier a model can address — *Monthly revenue check* →
`monthly_revenue_check` — because a name a model cannot address is a tool it cannot use.

**A question, inside somebody's conversation.** There is no dock there, so the graph runs to its
`interrupt()` and the payload's question is returned **for the model to relay word for word**, with
the run id; a companion `answer_<graph>` tool resumes the parked thread on a later turn. That is
`start_export_offer` / `confirm_download` / `resume_export` with the nouns changed, and two rules
carry over unchanged: **the question is not paraphrased** (a model rewording a question asks the
user the wrong thing, and a paraphrase makes the next turn's answer unmatchable), and **the run is
parked on a persisted `thread_id`.**

The answering tool is offered **only** when the graph contains a question node, so an agent whose
graph never pauses gets exactly one new tool.

**An answer that does not fit is not a tool failure.** It is the one failure on this path the user
can fix, so the model is told to ask again. Reporting it through the ordinary failure wording would
tell the model that nothing the user says can change it and that an operator has to look at it —
which is what it did before that branch existed.

**What the model is told about a result** is the last node that *produced data*, not simply the last
node to run. A graph almost always ends at a Success node whose output is `{"succeeded": true}`, so
"the last output" reports a graph that read two hundred rows as having returned nothing — **observed
doing exactly that.** A `human` node is deliberately not counted either: its output is an answer
supplied *to* the graph, and because it usually runs late, counting it would let a yes/no shadow the
rows a query read earlier.

Rows go through `describe_result`, the same function every tool's rows go through, so **a graph does
not get its own vocabulary for "here are some rows".**

### The node box, and why three CSS rules are load-bearing

A node's outgoing ports are absolutely positioned down its right edge, each carrying an **opaque**
label (`each`/`done` on a loop, `else` on a branch) — opaque because a connector passing behind one
would otherwise be unreadable. That makes their position load-bearing rather than cosmetic:

1. The header is given an **exact** height from a CSS variable rather than being allowed to size to
   its content, because the port stack is offset from that variable and the header's height has to
   be a number something else can read.
2. The stack starts **below** the header, never across it. When it started near the top of the box
   instead, the first port's label sat on top of the Settings and Delete buttons and **swallowed the
   clicks meant for them.** The buttons were painted; they were simply unreachable, which is the
   worst version of that bug.
3. `renderNode` grows the node's `min-height` so the stack fits, and the height is **measured** from
   the rendered stack rather than computed from the port count: a row is as tall as its label
   (~15px), not as tall as its dot (12px), so arithmetic over the port size comes out a few pixels
   short per row — enough that a branch with four conditions hung its last port below the box.

The invariant: **every node's header is fully visible and clickable, whatever its type and however
many ports it has.** Checked by hit-testing — `document.elementFromPoint` at the centre of both
header buttons, on one node of all ten types, with the branch node pushed to four ports — rather
than by looking at the page, since the failure mode here is a control that *looks* present and is
not.

### Two gestures for one connector

**Drag** from an output port and release on the target (a dashed rubber band follows the cursor, the
target highlights, releasing over empty canvas abandons the attempt). Or **click** the output port
then click the target — its incoming dot, its body or its header — with the canvas cursor becoming a
crosshair while armed, so the mode is *visible* rather than remembered.

One dot serves both. A press that travels less than `DRAG_THRESHOLD_PX` does nothing on release,
leaving the `click` that follows to arm the click-then-click gesture; a press that travels further
releases over another element and so produces no `click` on the port at all. **That is why the two
cannot both fire for one press.**

Two suppressions keep the forgiving version honest: a node accepts a click as "I am the target" only
**while a connector is armed**, and for one tick after a drag that actually moved a node, node
clicks are ignored — a mouseup always trails a click, so without this, dragging a node while a
connector was armed would silently connect it.

This was originally click-only, and the only thing that accepted the click was the node's **body** —
so the incoming dot, the obvious place to aim at, was inert, and dragging did nothing at all. Three
of the four things a user would try failed silently, **which reads as "connectors don't work" rather
than as "that is not the gesture".** All ten gestures are checked by counting the *change* in
connectors, never the total: a graph that already had an edge would otherwise let a refused gesture
pass.

Both offcanvas panels open from the **right**, because the application's sidebar owns the left edge.

### The shared canvas core

`static/js/graph_canvas.js` holds the **stateless** half of a node-graph editor: the Bezier maths,
the port measurement, the escaping and the id generator. Both canvases use it — this one and Flow
Builder's — and it must load **before** either feature's script, which both templates do and a route
test asserts.

**Nothing stateful moved.** The node registry, the properties panel, the palette, save/load and the
selection model are per-feature, because a conversation flow's nodes and a data pipeline's nodes have
nothing in common beyond being boxes joined by curves. **What they share is the curves.**

Every function there is pure or measures the DOM it is handed — none reads a module-level
`wrapperEl`, a `state` object or an id prefix. That is what makes one copy safe for two canvases with
different markup, ids and CSS.

**Verifying the extraction**, since there is no JavaScript test harness: the shared functions were
compared against the pre-extraction arithmetic copied verbatim out of git (83 assertions, all
identical), then both canvas pages were driven in headless Chromium. **One real bug was found that
way:** two id generators created in the same millisecond both started at 1 and minted identical ids,
so the counter is now module-wide.

---

# 17. SQL Assist

Ask AI — plain English to SQL for one of the user's own relational datasources.

### The contract, and why it is a property rather than a claim

**The model is shown structure, never data.** It receives the reflected schema of the tables the user
picked — table names, column names and types, primary keys, foreign keys — and nothing else. No row
is sampled, no count is taken, and **the generated query is not run.**

That last point is what keeps the promise true of the whole feature rather than only of the prompt:
**there is no code path in this module that executes what the model wrote.**

This still holds now that Deep Agents can execute a tool config. What becomes runnable is the
*validated builder config* — five known clause types, every identifier reflected, every value bound —
not the SQL string the model produced, which is only ever shown to the user. A drafted tool goes
through `create_tool_config` like any hand-made one, so **the model's SQL text never reaches a
database.**

A refinement turn re-sends the same schema plus the conversation so far, so a follow-up cannot reach
any further into the datasource than the first attempt did.

### Pruning, not post-checking

`_load_metadata` prunes the reflected metadata **before** `_build_prompts` ever sees it:

- an inactive **table** named in the form post is refused by name — the picker no longer offers it,
  but the field is a form field and can still carry it;
- inactive **columns** are removed;
- inactive names are removed from `primary_key`, and **any foreign key whose own column or whose
  referenced column is inactive is dropped** — otherwise the model is invited to join on a column it
  may not select;
- a table left with **no columns** is refused, naming that table.

**Pruning rather than post-checking is the design.** A model cannot select, join on or filter by a
column it was never told exists, and there is no SQL parser in this application to police its output
if it were shown one. The system prompt says so in those terms: *a column that is not listed does
not exist for you.*

### The projection rule, and its two carve-outs

The prompt asks for every column, spelled out, and puts a literal per-table list **ahead of** the
schema JSON so the model copies rather than derives it. Two carve-outs, both deliberate:

- **Aggregates are exempt.** "Include every column" cannot hold for
  `SELECT COUNT(*) … GROUP BY status` without changing what the query counts, and **a rule the model
  must break to answer the question is a rule it learns to ignore everywhere else.** The exemption
  has to be stated *together with* the grouping rule: told to select every column **and** to group, a
  model does both, and produces exactly the query the database refuses.
- **`SELECT *` is banned outright**, not discouraged. `*` is the one selection whose column list the
  database resolves at run time, so a query approved today starts returning a switched-off column
  tomorrow **without the query changing.**

### Enforced versus advised

| Check | Enforced? |
|---|---|
| The model only sees active tables and columns | **Enforced — structurally, by omission** |
| No `SELECT *` / `table.*` in the generated query | **Enforced** — 502, query not shown |
| A drafted tool config references only active columns | **Enforced** — 400, tool not created |
| Only active columns are actually *read* | **Enforced** — by the executor, on every run |
| The query includes *all* active columns | **Advised** — reported as `omitted_columns`, shown as an amber note |
| The grouping is one the database will accept | **Retried, then advised** — one regeneration, then a red note |

The last row is a text search, not a parse: it cannot tell a SELECT list from a WHERE clause, and it
cannot tell that a CTE's outer query legitimately narrows what the inner one read. **Refusing on it
would reject every aggregate and every CTE the panel exists to help write**, so the user is told and
the decision stays theirs.

The only place "all active columns" is *guaranteed* is the executor's builder mode, which builds the
column list itself instead of asking a model for one.

### `SqlDraft`, and why an empty answer is a good answer

The pydantic shape the provider is forced to return:

- `sql` — one read-only statement, no trailing semicolon. **Empty when the schema cannot answer the
  request** — that is a valid, useful answer ("there is no order date column"), not a failure, and the
  panel presents it as one.
- `explanation` — what the query returns and how, or what the schema is missing.
- `assumptions` — up to 5 notes on anything guessed: a join inferred without a foreign key, a column
  read as a date, an ambiguous word in the request.

Bounds, because it all becomes prompt: prompt 2,000 chars (matching AI analytics); tables 25 —
**over the cap is refused, not trimmed**, because silently reflecting the first 25 would generate a
query against a schema the user believed was larger; history 6 turns, 4,000 chars of SQL per stored
turn; statement 8,000 chars, shared with Tool Configs and the executor.

### The grouping rule, and why it regenerates rather than patches

Three things happen about a `ONLY_FULL_GROUP_BY` violation, in the order they take effect:

**1. The prompt states the rule** — two bullets next to the aggregate carve-out. Most attempts never
break it.

**2. A query that breaks it anyway is written again.** `group_by_violation` runs over the draft with
`_primary_keys(metadata)` passed, so the shape both databases *do* allow is not treated as a fault. On
a violation the model is called a second time with the failed statement, the offending column, and
the three ways out.

**Asked again, never patched.** Adding the column to the `GROUP BY` here would be a change to what
the query counts — one row per group becoming one row per pair — **and the explanation beside it
would then describe a different query than the one shown.** Regenerating keeps the SQL and the words
about it coming from the same place.

**3. A second failure becomes a note, not a refusal.** The original draft is returned with a red
alert above it: *"This query will not run as written."* The check is a heuristic and the panel does
not execute anything, so the user reading the SQL with the problem named beside it is better off than
a 502 that leaves them nothing to refine.

**One retry, not a loop.** A second call is worth it; a third is a model that is not going to get
there, and the user can say so faster by rewording the prompt.

### Auto Create Tool

**Why a second AI call.** Converting the query into the builder's shape is a **separate, narrow
call**, not one more field on `SqlDraft`. It only costs anything when the user actually asks for a
tool, and converting one known query against one known schema is a far smaller task than writing SQL.
That matters most for the in-built local model — a 1.7B-parameter model — where one request producing
prose, SQL, assumptions *and* a nested builder config is exactly the kind of prompt that comes back
malformed.

`fits` decides which of the two modes the query lands in. **It does not decide whether the tool can
be created: every valid read-only query can.** So `fits: false` is a real answer the model is
expected to give, with `reason` naming what is in the way. **A tool that quietly differs from the
query the user just read would be worse than no tool — but refusing to save a query the user has read
and approved is worse than either**, which is what SQL mode is for.

The builder is tried first because it is the stronger artefact: identifier-checked, values bound, and
reopening fully editable.

**`_reference_resolver` is the part worth reading.** A bare column name in a joined query is looked
up, not assumed — see §7's table for the five outcomes. Qualifying a bare name with the base table
would be a guess, and **a wrong guess is the worst outcome available here**: the tool would be
created, would validate, would open in the builder, and would quietly answer a different question
than the SQL the user approved. The user has the query in front of them and can refine it; what they
cannot do is notice that a saved tool silently reads the wrong table's column.

"Rejected" there means *rejected as a builder config*, not rejected as a tool. Each outcome raises,
`draft_tool_config` catches it, and the tool is drafted in SQL mode with the rejection message as the
reason shown. **The model's reading of the query was wrong; the query itself never was.**

`create_tool_from_draft` goes through `create_tool_config` rather than writing the row itself, so an
AI-created tool is subject to every rule a hand-made one is. **Nothing on the row records that an AI
drafted it** — there is no second kind of tool config to maintain.

**Which tables the tool records** is decided per mode, and neither is "everything the user selected":

- **SQL mode** records every selected table, with the model's chosen primary one moved to the front.
  The statement reads what it reads and nothing here parses it, so the user's selection is the best
  record available.
- **Builder mode** records the base table plus whatever its joins bring in — `query_tables` of the
  *validated* config, not the selection. Recording a table it never touches would overstate the
  tool's scope as surely as the old single-table record understated it.

**Edit shows what was created**, and that is a guarantee rather than a hope: the stored config is in
exactly the form the builder writes, and every reference in it was resolved against the real schema —
so the edit form repopulates every control and **saving without edits stores a byte-identical
config.**

### Template details that were bugs once

- The Auto Create Tool controls sit in their **own `<form>`** inside the result partial, because the
  result element is outside the main form and nesting forms is invalid HTML.
- `result.htm` writes the history with `value='{{ history | tojson }}'` — **single-quoted on
  purpose.** `tojson` escapes `<`, `>`, `&` and `'` but deliberately leaves double quotes alone, so
  the JSON would break out of a double-quoted attribute.
- The table list arrives as repeated form fields, so it is read with `getall` **and an explicit
  default** (it raises on a missing key). A plain `get` would return only the first of several,
  silently generating a query against one table when the user picked four.
- `sql_assist.js` falls back to a selection-based copy when `navigator.clipboard` is unavailable —
  which is the case on a page served over plain HTTP to anything but localhost, **exactly how this app
  runs in development.**
- Errors from `generate` are **rendered as an inline alert rather than raised**, so a rejected prompt
  or an unreachable datasource leaves the panel — and everything typed into it — exactly where it was.

### Relational only, and filtered rather than flagged

`get_datasource_choices` **filters**: a file or collection datasource has no SQL to generate — they
are queried through pandas and aggregation pipelines respectively — and reflection is a relational
concept. Offering one would only produce an error on submit. When the user has none, the panel
explains that instead of showing an empty dropdown.

### Not covered

The conversation is **not persisted** — no history table; it lives in the panel for as long as it is
open (contrast `PromptHistory`, which AI analytics writes for every run). And **Auto Create Tool only
creates**: there is no "update this existing tool config from a new query", because editing an
existing tool is the query builder's job.

---

# 18. Query Test

### Why it exists

Everything between writing a tool config and an agent calling it is checked — the shape of the
config, every identifier, that the statement is a single read, that each table is still switched on —
**and all of it twice, on save and again on every run.**

None of it can answer the question that decides whether the tool works: **will this database run this
query?** That is the database's answer to give. A grouping MySQL refuses under
`ONLY_FULL_GROUP_BY`, a column that exists in staging and not in production, a join whose `ON` clause
names the wrong side — every one passes every check the application can honestly make, and fails at
run time.

Before this button, that sentence first appeared **in a chatbot conversation, addressed to a
visitor**, as *"I cannot retrieve that figure right now."* The operator who could fix it never saw
it.

### The three things that make "tested = saved" true

**The same validators.** `_validated_query` calls `validated_tables`, `validated_query_config` and
`validated_tool_sql` — the functions the save itself calls. **A test with looser rules would pass
queries that then cannot be created, which is worse than no test.**

**The same execution.** `probe_tool_query` sits beside `execute_tool_query`, sharing everything below
it: the reflection, the bound parameters, the active-table and active-column rules, the read-only
guard. A test that ran the **rendered preview string** instead would be testing a different query —
and the preview is explicitly a display artefact that inlines filter values.

**The same fields.** The button posts the form with `hx-include`, so `query_mode` decides which query
is tested exactly as it decides which is stored. Both are always in the payload; testing in builder
mode while the SQL panel holds a broken statement **passes**, because that statement is not what will
be saved.

### A nested tool is tested as a chain

The whole chain runs — the same graph an agent's call would run, compiled with a one-row limit on the
root. Testing the outer query with the children skipped would test a different, unrestricted query,
and **a pass on it would say nothing.**

The children are validated by the same function the save uses, against the query **as the form
currently has it** — the mode, the statement and the tables may all be changing in this same edit.

If an inner tool matches nothing, the test **passes** and says so: *"The chain ran, but
'paid_invoices' matched nothing, so this query was not reached. Every query is valid — the tool would
return no rows until that inner tool matches something."* That is the honest answer to what the
button asks. An iterating link runs the root once per value here too, so a test of one is a test of
the loop rather than of one pass through it.

### The operator's test value

A SQL-mode tool holding `:department_id` cannot run without a value, so the Values card carries a
**test value** beside each declared parameter. It is used by this button and **is not saved** — it
goes into its own hidden field, never onto the tool config.

**The split is the point.** The button's whole claim is that it ran the query the tool will run, and
the only honest value to run it with is one the operator typed. Filling it with something invented
would prove the statement runs for a value nobody chose.

### What a test does not do

- **Writes nothing.** The guard runs before the connection is used, so a write is refused without
  reaching the database.
- **Reads one row.** `PROBE_ROWS = 1`. The query still *runs* in full, as any real call would.
- **Shows no values.** The verdict is the column names and the row count. Proving a query runs needs
  a row *fetched*, not *displayed* — and in Ask AI, printing one would break the single promise that
  feature makes.
- **Saves nothing.** Pressing Test on a half-finished form leaves no trace.
- **Is not offered where it cannot run** — the builder's button appears only for a relational
  datasource, because offering it for Mongo or a CSV would only produce a refusal.

### Every outcome is a result

`test_query` **never raises.** The route has no `try/except` and no error branch, because "the
database refused it" is the answer the endpoint was asked for, not a fault in the request. Four shapes
of failure, each fixed somewhere different:

| What went wrong | What the panel says |
|---|---|
| No datasource, an inactive table, a non-relational datasource | "Activate them in Data Sources or remove them from the query" |
| An invalid config or statement | The validator's own sentence, naming the field |
| The tool could not be assembled | The fault, **without** the agent-facing advice |
| The database refused it | The driver's own words, trimmed and stripped of SQLAlchemy's `[SQL: …] [parameters: …]` tail, which is noise when the statement is on screen above the alert |
| The datasource is unreachable | "Could not connect to the datasource, so the query was not run" |

That last row is deliberately **not phrased as a query problem**: telling someone their SQL was
refused when the host was simply down sends them off editing a query that is already correct.

### Why its own module

Both callers are peers. The Tool Configs form and the Ask AI panel ask the identical question and must
get the identical answer — **a query that passes in one panel and fails in the other would be worse
than no button at all.** Hanging the logic off Tool Configs would make Ask AI a client of a feature it
has nothing else to do with.

---

# 19. Flow Builder and the conversation engine

A flow is a graph of nodes (JSONB, authored on a client-rendered canvas) that decides what the widget
says at each turn.

### Ownership expressed as one constraint

**Flows are owned by a user, not by a chatbot.** Built standalone, then *attached* to one agent from
that agent's settings page. Ownership is checked directly against `user_id`; **no chatbot key appears
in any Flow Builder URL.**

| | Meaning | Set where |
|---|---|---|
| `is_active` | Published vs draft | The Flow Builder list |
| `chatbot_key_id` | Which agent runs it | The agent's settings page |

`get_active_flow` requires **both**, so a finished flow can be parked without detaching it, and a
draft can sit attached while it is being finished.

`chatbot_key_id` is nullable **and unique**, and that single constraint expresses **both halves** of
the relationship — one flow per agent, one agent per flow — replacing an older service-enforced "at
most one active flow per key" rule. Deleting an agent **detaches** its flow (`ON DELETE SET NULL`)
rather than destroying it.

`attach_flow` is the single write path for the dropdown: it refuses a draft or a flow already used
elsewhere, and **detaches whatever the agent currently runs** before claiming the unique slot.

### The four models, and one non-FK pointer

- **ChatbotFlow** — the saved graph.
- **ChatbotFlowSession** — per-visitor execution state. The visitor browser mints and persists an
  opaque `session_token` in localStorage. **This is not the row's public `uuid` and is never trusted
  as a lookup key by itself** — every query scopes it by `chatbot_key_id`.
- **FlowNodeKnowledgeBase** — scoped **per node**, not per flow or per chatbot key, via a string
  `node_id` pointer into the owning flow's `graph_data["nodes"]`. **Not a foreign key, because nodes
  are JSONB entries rather than rows.** Status is `untrained` / `trained` / `failed`.
- **FlowNodeKnowledgeDocument** — one uploaded pdf/txt/docx, or typed text.

### Four services, split by concern

| Service | Responsibility |
|---|---|
| `flow_service` | Builder CRUD, ownership, publishing, attaching |
| `engine_service` | Runtime graph interpretation — given a saved flow and one visitor session, what to send back this turn |
| `ai_fallback_service` | One AI Fallback node's answer orchestration: its guardrails and prompt, its context source, and its LLM choice |
| `knowledge_base_service` | Upload, train (extract → chunk → embed → store vectors), retrieve by similarity |

The AI Fallback node layers its guardrails on top of the chatbot's own configured system prompt as
the base persona, and the chatbot's actions can still run for the turn — but **the node's LLM choice
wins** for the turns it handles.

Both controllers return **JSON rather than HTML partials**, matching the fact that the canvas and its
properties panel are entirely client-rendered.

### A selection is a message with no text in it

A Menu or Dropdown reply is the one visitor turn that carries no words: the widget sends an empty
`message` and puts the chosen option's **id** in `selected_value`. That id is also the edge's
`source_port`, and for a long time routing was the only thing it was used for — after which it was
dropped.

Two things downstream need it, and both were silently broken by that.

**An AI Fallback node wired straight off a Menu was asked `""`** — it searched its knowledge base for
nothing and prompted the model with an empty question. A chatbot whose persona scopes it then
answered with its out-of-scope refusal, which presents as a broken flow rather than as a missing
question. `_effective_message` fixes the class of bug rather than the instance: typed text wins when
there is any, and a selection turn hands on the option's **label** — precisely what the visitor sees
in their own chat bubble, so the model is asked what the visitor believes they asked.

**An If/Else further down had nothing to branch on.** Menu and Dropdown now take the same optional
`variable_name` Ask Input has, and share `_store_answer` with it — which keeps the JSONB rule in one
place: `variables` is a plain, non-`Mutable` column, so the helper **reassigns a new dict** and an
in-place `variables[key] = ...` would never persist.

The label is resolved **before** the selection is consumed, since consuming it moves the session off
the node that owns the options.

### The knowledge-base pipeline

`chunking.py` is **pure text chunking — no I/O, no DB** — kept separate so the splitting logic can be
unit tested and tuned in isolation. Paragraph-aware: it packs consecutive paragraphs up to
`max_chars` (default 1200, ~300 tokens, comfortably inside the embedding model's 2048-token window)
and hard-splits any single paragraph longer than the limit with `overlap_chars` of carried context.
Chunks per document are capped at 400.

`embed_document` is **delete-then-insert in one transaction**, which makes re-embedding idempotent.
`documents_with_current_chunks` drives the staleness check: skip documents already current under the
configured embed model, re-embed everything if that model changed.

`KnowledgeChunk` is worth three notes:

- `embedding` is a pgvector `Vector(768)`, matching `nomic-embed-text`'s output size. **Changing
  `OLLAMA_EMBED_MODEL` to a model with a different output size requires a migration to alter the
  column's dimension, plus a full re-embed of every existing chunk.**
- `knowledge_base_id` is **denormalized** alongside `document_id` rather than requiring a join, so
  the hot retrieval path — filter by knowledge base, order by vector distance — can use the HNSW
  index directly. Safe because a document's owning knowledge base is set once at creation and never
  changes, and both FKs cascade from the same delete.
- `embed_model` records which model produced each vector, **so a future embed-model change is
  detected as staleness rather than silently mixing incompatible vectors in similarity search.**

Indexed with an HNSW cosine-distance index (`m=16, ef_construction=64`).

### Retrieval breadth was measured, then left alone

Chunk count is the largest remaining latency lever — prompt evaluation is ~9.7s of the ~17.4s answer:

| `_MAX_CONTEXT_CHUNKS` | Prompt eval | Total | vs 8 |
|---|---|---|---|
| 4 | 4.7s | 11.3s | 35% faster |
| 6 | 7.1s | 14.1s | 19% faster |
| **8** (current) | 9.7s | **17.4s** | — |

**Lowering it would be a pure loss of recall, not a trim of waste:** a fact planted at *every* rank
from 1 to 8 was recovered **8/8** times, so there is no "lost in the middle" effect and the 5th–8th
chunks are genuinely used. Trading half the retrieval depth for 6s is a bad deal — particularly as
the model choice already cut the same answer from 47.4s to 17.4s.

### Known limits

- The prompt's `EXIT` rule is honoured **as text only** — the model sends the closing message, but
  the widget session is not terminated; there is no signal channel for that yet.
- Turns are **stateless**: no conversation history is sent to the model (pre-existing behaviour).
- **An AI Fallback node does not use an attached data agent.** It keeps answering through the profile
  path; only the non-flow branch of `generate_reply` routes to the agent.

---

# 20. The in-built LLM path

A single locally-running Ollama server serving the whole app. **Not a per-user credential** like the
AI Settings provider keys: configuration is app-wide env vars with defaults, not a per-user settings
UI.

### The client, and what it deliberately is not

`ollama_client.py` talks Ollama's **native REST API** (`POST /api/chat`, `POST /api/embed`) over a
pooled `httpx.AsyncClient`. **No `ollama` SDK, no LangChain, no OpenAI-compatible shim.** It has no
knowledge of this app's data shapes, so it stays reusable; callers own interpreting the raw text and
vectors it returns. `preload_models()` / `close_client()` are wired to `on_startup` / `on_shutdown`.

`db/ai_inbuilt/queries.py` exists because `CRUDQueryBuilder.get_many` orders by a plain column-name
string, so a **vector-distance `ORDER BY` is structurally out of reach for it.**

### Three models, for three jobs

| Workload | Variable | Value | Why |
|---|---|---|---|
| Data agents | `OLLAMA_DEEP_AGENT_MODEL` | `qwen3:8b` | Must hold a tool-calling loop |
| Chatbot replies, AI Fallback, KB extraction | `OLLAMA_CHAT_MODEL` | `qwen3:1.7b` | One structured-output call; small is fine and ~3× faster on CPU |
| Embeddings | `OLLAMA_EMBED_MODEL` | `nomic-embed-text` | 768 dimensions |

The obvious move — set `OLLAMA_CHAT_MODEL=qwen3:8b` — was **rejected**: it would drag every in-built
feature onto a model roughly 3× slower on CPU in order to enable one feature. The override applies in
one function and falls back to `OLLAMA_CHAT_MODEL` when unset, so an existing deployment is
unaffected.

`qwen3:1.7b` was chosen over `qwen3:4b` on measurement, not preference: **21.8s vs 47.4s** on the
real 8-chunk prompt, while matching it on both acceptance checks (valid JSON, and recovering facts
planted at each end of the retrieved context) across 8 trials. **The caveat is recorded too:** that
is a fact-extraction task both models passed perfectly, so it demonstrates *no quality gap* rather
than proving none exists on harder reasoning. Swap to `qwen3:4b` if answer quality disappoints.

**The deep-agent model is deliberately not preloaded.** `keep_alive=-1` would pin ~5 GB resident for
a feature that may go unused, so the first data-agent turn pays the model load instead. It is also
not pulled automatically by `ollama-init` — 5.2 GB for an optional feature.

### App-side tuning, all measured on one host

Host for every figure: Intel i5-10400F, 6 physical cores / 12 threads, 31 GB RAM, **no GPU** (the
installed GT218 is far too old for CUDA, so `ollama ps` reports `100% CPU`). All figures used
**distinct prompts per trial** — repeating an identical prompt hits llama.cpp's prompt cache and
roughly halves the apparent latency, which real chatbot traffic never benefits from.

| Variable | Default | Why |
|---|---|---|
| `OLLAMA_KEEP_ALIVE` | `-1` | Never unload. Sent as a JSON **number** — the API rejects the string `"-1"` with `time: missing unit in duration "-1"` |
| `OLLAMA_NUM_THREAD` | `6` | Physical cores. Ollama has **no** `OLLAMA_NUM_THREADS` env var; thread count is the per-request `options.num_thread`. Measured: 4 → 6.0, 6 → 6.0, 8 → 5.9, **12 → 2.0 tok/s** — using all 12 hyperthreads is **3× slower**, because the siblings contend for the same 6 cores |
| `OLLAMA_NUM_CTX` | `2048` | **Not a speed knob** — 2048 vs 4096 on an identical prompt measured 4.6 vs 4.5 tok/s, because Ollama sizes the KV cache from this but only evaluates the tokens actually sent. What it controls is **truncation**, and getting it wrong is silent — see below |
| `OLLAMA_NUM_PREDICT` | `512` | Caps generation. ~9.3 tok/s on `qwen3:1.7b`, so each permitted token is ~0.1s of worst-case latency |

Also in the payload: `stream: false`, `think: false` (suppresses qwen3's reasoning block, which would
otherwise be generated and then discarded), and `format: "json"` for JSON-mode callers.

`preload_models()`'s `options` **must keep matching what `chat()` sends** — Ollama reloads a model
when a request asks for a different `num_ctx`, which would defeat the preload entirely.

### Truncation detection has to be pre-flight

`_warn_if_context_too_small` estimates the **outgoing** text, and this is deliberately not a check on
the response.

**Ollama truncates an over-long prompt and then reports `prompt_eval_count` for only what survived**,
so the reported count sits *below* `num_ctx` and can never reveal the loss. Measured: the 8-chunk
prompt reported **514 tokens at `num_ctx=1024`** against **1257 at 2048**, with no post-hoc signal
that 743 tokens had been dropped. **Any check written against the response would have stayed silent
through it** — and the visible symptom was invalid JSON on every trial, surfacing to the visitor as
*"The local AI model returned an unreadable response."*

### Two floors for the agent path

| Setting | `.env` | Agent floor | Why the floor |
|---|---|---|---|
| `num_ctx` | 2048 | **8192** | An over-long prompt is silently cut. A truncated tool **result** is a wrong answer, not an error |
| `num_predict` | 512 | **1024** | A truncated tool **call** arrives as malformed JSON — the graph sees a broken call rather than a cut-off answer |

Both `.env` values are correctly sized for the short single-shot prompts they were tuned for; a Deep
Agent turn is a much larger prompt (routing prompt + tool schemas + deepagents' own instructions) and
at least two round trips. The floors take the larger of configured-and-required, so raising `.env`
still works.

### Small models are refused, not attempted

```python
_MODELS_WITHOUT_RELIABLE_TOOL_CALLING = frozenset({
    "qwen3:0.6b", "qwen3:1.7b", "llama3.2:1b", "tinyllama", "gemma3:1b",
})
```

A Deep Agent depends **entirely** on the model choosing to emit a tool call. When a model too small
for that fails, **it does not raise — it answers confidently with no tool call behind it**, which is
precisely the invented-figure failure the whole feature exists to prevent. So this refuses with a 503
naming the fix.

**A denylist, not an allowlist:** an operator who has pulled a model we have never heard of should be
able to try it.

### Measured: usable from the console, not from a live widget

`qwen3:8b` on that host:

| | Measured |
|---|---|
| Generation rate | ~2.5 tok/s |
| One tool-calling round trip, 133-token prompt | 67–81 s |
| Full two-call turn over the real routing prompt | **242 s warm, 417 s cold** |

It routes correctly and reports the right figures — verified end to end, including relaying a tool's
fixed filter unprompted:

```
Q: How many units did each customer receive on paid orders?
tools called: ['paid_units_by_customer']
A: Acme: 18 units, Initech: 2 units.  This data is restricted to paid orders only.
```

**It is simply minutes per turn.** Operational conclusion: **on CPU-only hardware, in-built data
agents are a test-console feature. Use a saved API key for live widgets.** The pre-existing in-built
chatbot path (one call on `qwen3:1.7b`) is unaffected and remains usable.

### Server-side configuration (systemd, needs root)

These are Ollama *server* variables and cannot be set from the app:

```ini
[Service]
Environment="OLLAMA_KEEP_ALIVE=-1"          # anything not going through our client also stays resident
Environment="OLLAMA_FLASH_ATTENTION=1"      # cheaper attention; prerequisite for the next one
Environment="OLLAMA_KV_CACHE_TYPE=q8_0"     # quantized KV cache, less memory traffic per token
Environment="OLLAMA_MAX_LOADED_MODELS=2"    # exactly the chat + embed pair, so neither evicts the other
Environment="OLLAMA_NUM_PARALLEL=1"         # on CPU, concurrent requests split the same 6 cores and each gets slower
```

`OLLAMA_NUM_PARALLEL=1` is the counter-intuitive one: **serialize instead, and let Ollama queue the
rest.**

---

# 21. The chatbot turn pipeline

### The layering, and why the turn service sits at the top

```
public_chatbot_routes.message
  └─ chatbot_turn_service.answer_turn        ← opens the record, writes the row
       ├─ flow_service / engine_service      (flow answers, if a flow is active)
       │    └─ ai_fallback_service           (an AI Fallback node inside the flow)
       └─ chatbot_reply_service.generate_reply
            ├─ chatbot_action_service.maybe_run_action
            └─ chatbot_service.answer_message → ai_analytics_service
```

`chatbot_reply_service` sits **above** `chatbot_service` so the dependency direction stays one-way.

Two consequences of that shape are worth knowing:

- **`answer_message` does not persist anything.** A single turn can reach it **twice** (once through
  an AI Fallback node), which is how an earlier design produced duplicate analytics rows.
- **The turn service is also where flow-only turns get logged**, so an agent driven entirely by a flow
  still appears in the dashboard.

`answer_turn` is the **only** place a turn is logged. Logging is **best effort**: a visitor who has
already been answered is never shown a failure because the log write failed — the error is logged for
the operator and swallowed.

### Token and timing accumulation without threading arguments

One turn can make model calls in several layers — the action router, the grounded answer, an AI
Fallback node — and **the layer that knows a call's token cost sits well below the layer that writes
the log row.** Rather than threading a "usage" argument through every signature in between, totals
accumulate in a context-local record:

```
record_turn()            opened once, at the turn boundary
record_llm_call(...)     called by each provider path
record_action(...)       called when an action runs
```

**Recording is a no-op when no record is open**, so code paths outside a chatbot turn — the
authenticated Ask AI flow, background jobs — are unaffected. And per §12's rule 3, it **appends to** a
`TurnRecord` rather than replacing it, because a new task gets a copy of the context.

### What one row records

| Column | Meaning |
|---|---|
| `response_time_ms` | Server-side wall time for the whole turn. **Excludes network latency to the visitor's browser** |
| `request_tokens` / `response_tokens` / `total_tokens` | Summed across **every** model call the turn made |
| `llm_call_count` | How many that was |
| `tokens_estimated` | True when a provider reported no usage and counts were derived from text length. **Surfaced as a caveat — never treat those as billing figures** |
| `llm_provider` / `llm_model` | Who answered. Null on a flow turn that called no model |
| `turn_type` | `ai` or `flow` |

The same `response_time_ms` is returned to the widget and rendered under the reply bubble, **so what a
visitor sees and what the owner sees can never disagree.**

### The action router

1. **No active actions attached → nothing happens.** No extra LLM call for the majority of agents.
2. **Router pass** — one structured call with an `ActionSelection` schema: which action, with which
   parameters?
3. **Execute** — parameters type-checked against the action's declared schema, then the request is
   rendered and sent.
4. **Answer** — the bounded response is injected into the answering call as context.

**This is a router pass rather than native tool calling** because native tools would need three
separate provider implementations *and* would collide with the forced structured output every
provider path already uses. The trade-offs, accepted deliberately: one extra round-trip, and one
action per turn with no chaining.

`execute_action` **never raises** — a broken endpoint degrades the answer instead of breaking the
conversation. Every failure is logged with detail, described to the model in general terms, and
recorded on the message as `ai_response["action"] = {"name", "status", "http_status"}`, which the
history renders as a badge. **Visitors never see endpoint URLs, status text or misconfiguration
messages.**

### Actions: ownership, and the header asymmetry

An action belongs to the **user** (max 30) and lives in a shared library; a `ChatbotActionLink` row
attaches it to an agent, and the same action can serve any number of agents. Names are **unique per
user**, because the name is the model-facing tool name and duplicates would make routing ambiguous.

Every chatbot-scoped question about actions is therefore a **join**, kept in
`app/db/chatbot/queries.py` — and the answer path passes `active_only=True`.

| Target | Allowed | Escaping |
|---|---|---|
| URL | `{{VAR}}`, `{{param.name}}` | Percent-encoded per value |
| **Headers** | `{{VAR}}` **only** | Rejected if the rendered value contains a line break |
| Body | `{{VAR}}`, `{{param.name}}` | JSON-escaped; quote string placeholders, leave number/boolean bare |

**Headers exclude `{{param.*}}` on purpose:** parameter values are derived from visitor text, and such
a value must never be able to forge an auth header or split a request. Any parameter used in a URL or
body must be marked **Required**, so a rendered request can never contain a hole.

Egress safety: `https://` only, no credentials in the URL; the host is resolved and checked
**immediately before the request** and any private, loopback, link-local (this covers
`169.254.169.254`), reserved, multicast or unspecified address is rejected — checked at save time too,
for a readable error; `follow_redirects=False`, because **a redirect is the standard way past an IP
check**; per-action timeout 1–30 s; response capped at 256 KB read / 4,000 characters shown to a
model.

**Stated limitation:** check-then-request narrows but does not fully close DNS rebinding. Closing it
needs IP pinning at the transport layer, which httpx does not expose.

Header lists are Fernet-encrypted at rest, because that is where bearer tokens live. They are
decrypted for the owner's own edit form — **the encryption protects the database, not the owner.**

### Analytics queries

Ownership is enforced **in SQL**: every query joins `chatbot_api_keys` and filters on `user_id`, so no
caller can read another account's traffic by passing the wrong id. Percentiles use `percentile_cont`
**in the database** rather than sorting rows in Python.

`?period=` accepts `24h`, `7d` (default), `30d`, `90d` — **anything else is rejected with a readable
message rather than coerced.** The absent/unreadable distinction matters more here than in a form: a
filter that silently falls back does not fail visibly — **it renders real figures for the wrong
scope, and nothing on screen says so.**

Ranges under two days bucket by hour, longer by day, and **empty buckets are filled in by the
service** so a quiet day reads as a quiet day rather than disappearing from the axis.

Charts are **plain CSS bars**, so the page needs no charting library and renders identically inside
an HTMX swap.

Rows written before the performance columns existed are backfilled with `0` / `false` / `'ai'`, so a
historical turn honestly reports "no measurement recorded" (the dashboard shows `—`) **instead of a
fabricated zero-millisecond answer.**

---

# 22. The embeddable widget

`widget.js` is generated by `chatbot_service.build_widget_script`. It is the one part of the
application whose failures the server **cannot see**, because it runs on a third-party site — and it
is the one file whose deployed copy can be arbitrarily older than the server answering it (§14).

### The Markdown renderer, and its single safety rule

**The text is escaped first, before a single Markdown pattern is examined.**

```js
var lines = escapeHtml(String(text == null ? "" : text)).split(/\r?\n/);
```

After that line **there is no `<` or `>` left in the string.** A model that emitted `<script>` is
holding `&lt;script&gt;` and will still be holding it when the function returns. Every tag in the
output was written by the code below it, from a fixed set, and **no attribute is ever built from
message text** — the only attributes emitted are three known class names.

That ordering is what makes the result safe to assign to `innerHTML` **on a page this application does
not control.** The inverse — parse first and escape after, or take the model's raw HTML and
"sanitise" it — produces **byte-identical output for every benign input** and is a cross-site
scripting hole in the operator's own website. No allowlist bolted on afterwards recovers from it.

`inlineMarkdown` is the sharp edge: it applies emphasis to text that is **already escaped** and must
never be handed raw input. It has one caller outside `renderMarkdown` — the `insights` list — which
escapes first for exactly this reason, and **there is a test asserting that call site specifically.**

### What is deliberately not supported

**Links and images.** `[text](javascript:alert(1))` is the classic route from Markdown to script
execution, and supporting links would mean a URL-scheme check to get wrong. Grounding rule 10 already
forbids the model writing a URL at all — the interface draws its own download button — so the syntax
is left as **the literal text the model wrote, which is honest and inert.** Rule 15 says so to the
model as well, so it does not spend tokens producing something that will not render.

**Raw HTML passthrough.** There is no case where a model's `<b>` becomes bold. **This falls out of
escape-first rather than being a separate rule, which is the point: it cannot be forgotten.**

Supported: tables, headings, bullet and numbered lists, bold, italic, inline code. **Tables are the
reason the renderer exists.** A table needs its `|---|---|` divider row to be recognised — requiring
it is what stops a sentence like *"use the | character to split the file"* becoming a one-cell table.
Tables render inside a wrapper that scrolls horizontally, because a widget is around 340px wide and a
six-column result is not.

### Both reply paths render, and only one of them used to

The widget answers a turn two ways, invisibly to the visitor: **streamed** (SSE) for a chatbot with a
data agent attached, and **posted** for everything else — a flow answer, a chatbot with no agent, or a
stream that could not be opened.

`renderBotMessage` handled the posted reply through `renderMarkdown` from the day the renderer landed.
**The streamed one did not**: its painter assigned `bubble.textContent = answer`, so the *same answer*
displayed as a rendered table when it arrived by POST and as a wall of `|` characters when it streamed.
Since **streaming is exactly the path a data-agent chatbot takes, and a data agent is what produces
tables**, effectively every table answer in a published widget was shown unrendered — the original bug
the renderer was written to fix, reintroduced through a transport that did not exist yet when the
renderer was written.

`test_widget_script.py` now pins it: `paint()` must contain `innerHTML = renderMarkdown(answer)` and
must **not** contain `textContent` or `innerHTML = answer`. **The second half matters as much as the
first** — the obvious "fix" of assigning `answer` to `innerHTML` renders the table correctly and is
the XSS hole this whole design prevents.

**The whole answer is re-rendered on every token**, not appended to. Markdown is block-structured, so
the meaning of the last line can change when the next arrives: `| a | b |` is a paragraph until its
divider is read, at which point it is a table header. An incremental renderer would have to buffer for
that, and **re-parsing a few KB per token costs less than being wrong about it.**

### Degrading versus disappearing

The widget is built to **degrade rather than break**:

| Failure | Visitor sees |
|---|---|
| Settings fetch returns non-success | Default appearance + welcome text |
| Settings fetch never completes | Default appearance + welcome text |
| `/message` rejected by the server | The server's own message |
| `/message` never completes | "Could not reach the chatbot service." |

Degrading is right — refusing to render, or showing a visitor a stack trace, would be worse. But it
made **a misconfigured widget indistinguishable from a working one**, and three unrelated causes
produced the identical symptom (a healthy-looking widget titled "Chat with us"):

1. the page's origin was not on the chatbot's Allowed Domains list → 403;
2. an HTTPS page pointed at an `http://` `apiBase` → **blocked by the browser before sending**, so the
   server logged nothing and DevTools reported a **CORS error with no status and no body** — pointing
   at a server misconfiguration that did not exist;
3. `apiBase` could not be omitted for a same-origin embed, forcing an absolute URL whose scheme had to
   be maintained by hand — which is what caused (2).

So the rule for this file is the application's own rule, **with the console standing in for the server
log**:

> **The visitor gets a sentence. The operator gets the request URL, the status, the server's message,
> and — where it is knowable — the likely cause.**

`warnFailure(what, url, detail)` is the single place that happens. Every `.catch` and every
non-success branch calls it; **a silent one is a regression**, and
`test_no_failure_path_is_silent` fails if a `.catch` stops reporting.

**One failure reports once rather than every time:** the download card's status poll, which runs every
four seconds for as long as a file is being built. The operator still needs to know the card has gone
blind — a frozen progress bar otherwise reads as a stalled export rather than a lost connection — but a
poll that failed will almost certainly keep failing. **The rule is "no failure path is silent", not
"every occurrence is printed";** a `state` flag is how the difference is expressed.

`blockedRequestHint()` covers case (2) specifically, because **it is the only failure invisible from
both sides.** It fires only when the page is HTTPS **and** `apiBase` is `http://`, and it names both
fixes rather than describing the problem. **The hint goes on its own labelled line** — appended to
`reason` it read as `Failed to fetch This page is served over HTTPS…`, one broken sentence with the
actionable half buried mid-clause.

Three smaller fixes, each of which had produced a failure that looked like something else:

- **`apiBase` is optional.** Omit it and every request is same-origin relative, which cannot suffer a
  scheme mismatch and needs no CORS at all. Only `apiKey` is required.
- **A trailing slash is stripped, not rejected.** Every request appends a path starting with `/`, so
  `https://api.example.com/` used to produce `//public/chatbot/...` — a 404 resembling no
  configuration mistake in particular.
- **A non-JSON error body no longer throws.** A proxy's HTML error page rejected inside `r.json()` and
  fell through to the generic catch, so **a 502 that *was* answered got reported as "could not reach
  the API".** Both fetches recover the status code instead.

### Widget authorisation

`ChatbotKeyView.api_key` is **publishable** — it goes in the embed snippet, and its protection is the
per-key origin allow-list, not secrecy. The key and origin checks are **not** in the schema layer:
they need the database and the request's `Origin` header.

`PublicChatbotMessageRequest` is **the untrusted body**, and it is bounded (message ≤4000,
`session_id` ≤128, `selected_value` ≤1000) because **every accepted message is a paid model call.**

---

# 23. Frontend engineering

Server-rendered fragments, no SPA framework. Five patterns carry the whole interface.

### Cascading selects, and out-of-band resets

One choice re-renders the field that depends on it, **swapping the whole field so its options are
always real**, with `hx-include` sending the fields the server needs alongside the one that changed.

When a cascade invalidates more than its own field, the response carries the extra element with
`hx-swap-oob="true"` — **one request, two swaps, no second round trip.** Changing the datasource
replaces the Table field *and* resets the query builder *and* the mode selector, because a query — its
joins especially — belongs to one datasource, and leaving the previous one's builder on screen under a
newly chosen datasource would offer columns that are no longer there.

`_builder_context` / `_builder_defaults` are **one source** for the builder's context, shared by the
first render and the cascade, so a mid-edit swap produces exactly what the first render did.

### A control that answers with the list it lives in must round-trip its own state

A `<select hx-post hx-trigger="change">` inside a table row, answering with the refreshed table, is a
control that **replaces itself with its own re-render**. It looks like the cheapest possible editor —
one field, no submit button, nothing to forget to press — and it has a failure mode the pattern above
does not:

> the control's state now depends on the *list serialiser* marking it, and if the view feeding that
> template does not report the current value, a successful write renders as an unset field.

That is not a cosmetic bug. From the user's side it is **indistinguishable from a control that does
nothing** — and the natural conclusion is that saving is broken, not that a template lost a
`selected`. The Graph Designer's *Callable by* pickers shipped exactly this way: `get_graph_views`
returned `agent_id: None` for every row, so choosing an agent saved it and the refreshed row came back
blank. See [GRAPH_DESIGNER.md](GRAPH_DESIGNER.md).

Two ways out, and the choice is about where a refusal can land:

| | When it fits |
|---|---|
| Report the value from the view, and **assert `selected` in a test** — not merely that the option was rendered | the write cannot be refused, so there is nothing to explain |
| Move the field into a **dialog whose body is fetched per open**, and let the row become a read-only statement | the write *can* be refused. A form in a modal is not part of the list it refreshes, so the rebuilt row is the only thing that has to be right, and the reason has somewhere to appear that is not above a control that has already reset |

The test-shape lesson generalises past HTMX: asserting `value="…"` appears proves an **option exists**,
which is true whether or not it is the chosen one. A preselect is only tested by asserting the
`selected` attribute — over a whitespace-tolerant match, so the test fails on a defect rather than on a
reflow of the template.

### Repeating rows as one JSON field

Builder rows — query columns, joins, action parameters, nested tools, SQL values — are serialised into
a **single hidden JSON field** rather than parallel form arrays, so the server has exactly one place to
parse and validate their shape. **Parallel controls could arrive at different lengths, and a row would
then pair the wrong column with the wrong tool.**

Whatever is submitted is re-validated server-side regardless of what the form displayed.

### Error responses must be opted back into the swap

**HTMX only swaps `2xx`.** A route answering `400`/`409`/`422`/`500` with a human-readable alert would
have that alert **silently discarded** — the user clicks the button and nothing at all happens.

`templates/base/layout.htm` installs **one global `htmx:beforeSwap` handler** that re-enables the swap
for error responses, so every route gets this for free:

```js
document.addEventListener('htmx:beforeSwap', function (event) {
    var xhr = event.detail.xhr;
    if (xhr.status < 400) return;
    if (xhr.status === 401) return;          // handled by the login redirect instead

    var isHtml = (xhr.getResponseHeader('Content-Type') || '').includes('text/html');
    if (!isHtml || !xhr.responseText.trim()) {
        event.detail.serverResponse = '<div class="alert alert-danger">...</div>';
    }
    event.detail.shouldSwap = true;
    event.detail.isError = false;            // swap into the element's own hx-target
});
```

Two guards matter. **`401` is left alone** so the session-expiry redirect still wins and the login page
is never swapped into a partial. **A non-HTML body** (raw JSON from an unhandled exception) is replaced
with a generic sentence, so a payload or stack trace never reaches the user.

The consequence for route authors: **return the real status code, not `200`, and return HTML.** The
message will display.

### Offcanvas panels close only on their own close button

Every offcanvas stays open until the user clicks its own close/cancel/save control. **A backdrop click
and `Esc` are both inert** — these panels hold configuration forms, and losing a half-filled form to a
stray click is not an acceptable failure mode.

**Nothing is required of a new panel.** `layout.htm` locks it globally, right after the Bootstrap
bundle loads, because **Bootstrap resolves the options per instance and a panel can be created either
way**:

```js
bootstrap.Offcanvas.Default.backdrop = 'static';   // panels created from JS
bootstrap.Offcanvas.Default.keyboard = false;
// …plus data-bs-backdrop="static" / data-bs-keyboard="false" stamped onto every .offcanvas,
//   since data attributes outrank the defaults for data-bs-toggle panels.
```

Panels swapped in by HTMX are stamped on `htmx:load` / `htmx:afterSwap`, so a partial-delivered panel
behaves identically. Two rules for new panels: **always give it a close control** (with dismissal
locked, a panel without one is a trap), and `data-bs-backdrop="false"` is still allowed and left alone
— it means "no backdrop, keep the page behind usable", and with no backdrop element there is nothing to
click through.

Programmatic closes are unaffected.

### A panel that covers the viewport renders its own errors

An alert swapped into the page behind it is invisible. Mutations answer with a marker plus the rebuilt
list; failures answer with an alert into the same in-panel target, **leaving everything the user typed
untouched.**

### `createElement`, never `innerHTML`

Row markup — join rows, nested-tool rows, graph nodes, Venn labels — is built with `createElement` and
filled with `textContent`. **Table and column names come from the user's own database and must never be
re-parsed as markup.** `tool_configs.js`, `tool_chain.js` and `tool_graphs.js` each state that rule at
the top of the file, and `tool_graphs.js` never calls `.html()` or `innerHTML` even though it loads D3.

D3 7 is used on one page, for **three things only**: the zoom/pan behaviour, the curve generator for
connectors, and nothing else. **If the library fails to load the pane says so, rather than sitting
empty.**

### Server-owned conversation state

For a multi-turn panel, the server writes the state into the response and the form pulls it back in
with `hx-include`. **The state is then whatever the server last confirmed** — not whatever the browser
has been holding — and a failed turn can re-render it unchanged instead of discarding it.

### The browser check is the earlier, gentler half

`tool_configs.js` says the same thing in the builder as the rows change (`groupingProblem`, mirroring
the server's `_require_grouped_selection` **down to the wording**), in its own alert rather than through
the one-off notice channel — that carries messages about an action just taken, this is a standing
statement about the query as it currently reads. **It warns, and the server still refuses.**

---

# 24. Tool Graphs, and derived-versus-authored

A read-only page. **The module owns no model and no `db/` subfolder**: it composes four queries that
already existed and writes nothing.

### Why the split from Graph Designer is principled

| | Tool Graphs | Graph Designer |
|---|---|---|
| The picture is | **Derived** from tool configs every time it is drawn | **Authored** — the drawing is the source of truth |
| Positions | **Computed per request**, never stored | Stored, because nothing can recompute them |
| Writes | Nothing | Three tables |

Derived means **it cannot fall out of step with the tools** — which is the failure mode of a persisted
`{x, y}`, and is acceptable in Flow Builder because those positions are the user's own authored layout.

### What the drawing adds over the list

The list view shows a chain as indented text, and text lines cannot show the two facts that matter most
when a chain misbehaves:

- **a child embedded in two parents.** The list necessarily repeats it under each one, so nothing there
  says that editing it changes both tools. Here it is **one node with two outgoing edges** — that is the
  thing this view adds.
- **where a disabled tool sits.** A disabled child is the most common reason a chain returns nothing,
  and in a list it is a word at the end of a line. Here it is **dashed and red, not hidden** — a chain
  that stops is what someone opens this page to find.

`START` and `END` are drawn as rectangles because **that is what they are in the compiled graph, not
decoration.** `START` attaches to every tool that embeds nothing; every tool nothing embeds feeds
`END`. A standalone tool therefore draws as `START → tool → END`, which is exactly the graph it compiles
to.

**Scope always includes descendants.** Selecting an agent draws its own tools *and every tool below
them*, even when a child belongs to a different agent — not a convenience, but because
`collect_agent_tools` gives the agent every tool below it at runtime, so a graph that stopped at the
agent's own rows would draw a chain **with its lower half missing.** Each node carries its own
`agent_name` so a borrowed child is identifiable.

### Layout is computed on the server

`tool_graph_service` returns a `layer` and a `row` per node; the browser multiplies them by a gap.

**Layout is the part of a drawing that can be wrong without *looking* wrong**, this repository has no
JavaScript test harness, and the coverage ratchet only measures `app/` — so computing it in Python is
what makes it **assertable.**

- `layer = 1 + max(layer of children)`, so every edge points forward.
- A chain runs along one row; a second branch drops to the next. **A node keeps the row it was first
  given**, so a shared child does not jump between reloads.
- Both passes are **cycle-safe** — the layer relaxation is bounded by the node count and the row walk
  carries a `visited` set. Links cannot be saved in a cycle, but **a page that only displays must not be
  the thing that hangs** if a row ever arrived another way.

### Why a SQL-mode tool shows no Venn diagram

It shows its declared tables and this sentence:

> This tool's query is a SQL statement. Its joins are not read from the statement — only the tables it
> declares are known here.

Nothing in this application parses joins out of a raw statement. `sql_guard` is explicit that its checks
are text heuristics rather than a parse, and `child_output_columns` already returns `[]` for a SQL tool
for the same reason. **A Venn drawn from a regex over a statement with a CTE or a subquery in it would
be a confident picture of something nobody verified — and unlike a wrong number, a wrong picture is not
argued with.**

A builder query over one table gets its **own** note — *"This query reads one table, so there is
nothing to intersect"* — because a blank card would read as "no joins" for both cases, **and those are
not the same case.**

**One diagram per join, never a combined three-set Venn.** A query joining `orders → clients → regions`
is two pairwise conditions applied in sequence; three circles would imply an `orders ∩ regions` region
the query never computes. The pairwise form is also the only one that still reads at `MAX_JOINS` of 10.

### Failures are answers

Neither view endpoint raises. **A selection that cannot be resolved comes back as a 200 with `error`
set** and an empty drawing, so a stale bookmark or a tool deleted in another tab puts one sentence
beside the canvas instead of replacing the page the user is working in. `GET
/tool-configs/child-options` and the Graph Designer's node-options endpoint answer the same way, for the
same reason.

A tool belonging to someone else gets the same "not found" sentence a missing one gets — **answering
differently would confirm the uuid is real.** Ownership is not re-implemented here: it comes from the
existing service getters and from the `DataAgent.user_id` filter inside every query this module
composes.

A selection is kept in the address bar with `replaceState`, so the URL is a link someone can paste into
a ticket **without filling the back button with twelve canvas states.**

### It does not gate on activity

A disabled tool, a disabled agent and an inactive workspace are all drawn and flagged. **Hiding them
would make the page unable to answer the question it is most often opened for.** Empty branches are kept
too — an empty branch is how someone notices the thing they just created is empty, and hiding it would
make this tree disagree with the Data Agents page about what exists.

---

# 25. Error handling architecture

### The philosophy, in four words

Explicit, readable, logged, recoverable. **Silent failures are forbidden.**

Custom types: `ValidationError`, `DatabaseError`, `AuthenticationError`, `AuthorizationError`,
`ResourceNotFoundError`, `ServiceError`.

### One rule, two exception types, one sentence

Some rules are enforced in two places that owe the user **different exception types**. The per-table and
per-column switches are the worked example: a service refuses with an `HTTPException` a person reads in
a form, while the executor refuses with a `ToolQueryError` an agent relays to a visitor — **and neither
module should import the other just to agree on wording.**

The fix is to put the **message** with the rule and leave the raising to the caller:

```python
# a service, to a person filling in a form
raise HTTPException(status_code=400, detail=inactive_table_message(table_name))

# the executor, to a model that will relay it
raise ToolQueryError(f"{inactive_table_message(name)} Tell the user the tool needs reconfiguring.")
```

A reword then lands in both at once, and **the tests that pin those strings catch a change to either.**
Naming the thing that is wrong — the table, the column — is what makes a message actionable; "not
available" is not.

### HTML error responses

Routes answering an HTMX request return an HTML fragment. **Never build that fragment with an
f-string** — the message often embeds values the user typed (datasource name, host, database name), and
interpolating them raw makes the alert an injection point.

`html_error_response` / `html_success_response` escape the message and **preserve the status code**.
`html_error_response` falls back to a generic sentence when the detail is empty, so a blank alert is
never rendered. Returning the real code rather than `200` is **required** — see §23's `beforeSwap`
handler, which is what lets a non-2xx body reach the page at all.

### What is logged versus what is shown

Connection testers log the driver exception at `WARNING` and return `False`. The driver's own text —
`ConnectionRefusedError: [Errno 111]`, authentication failures, DNS errors — **stays in the server log**;
the service turns the `False` into one sentence naming what was attempted (*"Could not connect to
'pantry_mate' at localhost:5432 …"*) and nothing more.

Never expose stack traces. A validation catch-all is placed **before** any broad `try` block that would
turn it into a 500 — `validated_base_config` is called before the `try` in both create paths for exactly
that reason, because that catch-all would bury a perfectly readable validation message.

One adjacent fix worth recording: a malformed `base_config` was previously **swallowed and saved as
`{}`**, discarding the query the user had just built. It now returns a readable error.

---

# 26. Migrations

Owner: `app/db/migrations.py`, called from `main.py`'s `on_startup`.

### Why the app applies them

Startup used to call `Base.metadata.create_all`. That creates tables which do not exist and **does
nothing else** — it never alters a table it already found. So a column added to a model never reached an
existing database:

1. `extra_tables` was added to `ToolConfig` and a migration was written for it.
2. The migration was never run — nothing ran migrations.
3. `create_all` saw `tool_configs` already existed and **skipped it entirely.**
4. The app booted clean. SQLAlchemy then put `tool_configs.extra_tables` in every SELECT against that
   table, and PostgreSQL answered `UndefinedColumnError`.

The result was a 500 on one page with **nothing at startup hinting that the schema was stale** — from
`create_all`'s point of view there was nothing left to do. The failure surfaced one page at a time, as
whichever query happened to name the new column.

`alembic upgrade head` inverts that: **the migration chain *is* the schema's definition**, so a column
arrives with its revision, and a database that is behind is a state Alembic can name rather than one
inferred from a failing query. Because the app applies it, **the schema can never be older than the code
running against it.**

**A migration that cannot be applied raises, and startup stops.** That is the point: a database nobody
can account for should not be served requests.

### The three states

| State | Looks like | What happens |
|---|---|---|
| **empty** | No tables | The whole chain runs. Revision `a3f5c9d21b47` issues `CREATE EXTENSION IF NOT EXISTS vector`, so this path needs no help from `docker/postgres-init.sql` — which is now belt-and-braces rather than load-bearing |
| **tracked** | `alembic_version` present | Pending revisions applied; already-at-head costs **one query**, which is what makes running it on every `--reload` boot affordable |
| **untracked** | Tables but no `alembic_version` | **Refused**, naming the command to run |

**untracked** is a database built entirely by the old `create_all` path — which is what every
developer's database was before this change. Its schema cannot be matched to a revision by inspection,
and **both ways of guessing are worse than stopping**: replaying the chain over existing tables fails on
the first `CREATE TABLE`, and stamping a revision that does not describe the schema **hides real drift
until something reads a column that is not there.**

```bash
docker compose run --rm app alembic stamp head    # only if the schema really is current
```

### Two things about how it runs

**It runs in a worker thread.** `alembic/env.py` drives an async engine through `asyncio.run()`, which
cannot be called from a thread that already has a running event loop — and startup does.
`asyncio.to_thread` gives it a thread with no loop of its own, so `asyncio.run()` there behaves exactly
as it does on the command line.

**An advisory lock serialises it.** `pg_advisory_xact_lock` is taken **before** the state is inspected,
so booting several workers at once applies the chain once instead of racing on `alembic_version`. Taking
it *after* the inspection would let two workers both read "pending" and both proceed. The lock is
transaction-scoped, so it is released when the transaction ends — **including when it ends because the
migration raised.**

### Logging, and tables that are not ours

`env.py` calls `fileConfig()` to apply `alembic.ini`'s logging config. That is right on the command line
and **wrong in-process**: `alembic.ini` pins the root logger to `WARNING`, so applying it during startup
would **silence the app's own INFO logging for the rest of its life.** `migrations.py` sets
`config.attributes["configure_logger"] = False` and `env.py` skips `fileConfig()` when it sees that.

`env.py` passes an `include_name` hook excluding any table whose name begins with `checkpoint`. Those
belong to langgraph. They are not in `Base.metadata`, so without the hook **every `--autogenerate` run
proposes dropping their indexes — and a revision carrying those drops would break the export
confirmation the first time it was applied.** Owned by langgraph, versioned by langgraph, upgraded by
langgraph; matched by prefix because the set grows with its releases.

**Filtering by *name* rather than with `include_object`:** that hook only sees objects alembic already
decided to compare, and a foreign table has no object on our side to match against.

### Conventions a new revision follows

- **A rich module docstring** — what was wrong, what the change means for existing rows, what the
  downgrade costs.
- **Say whether the downgrade loses data, and refuse in the migration if the loss would be silent.**
  `c3a7d5e18b64`'s downgrade would lose every SQL-mode tool, because their query lives nowhere else — so
  it **reports how many would be lost and refuses.** Contrast `e7b3f5a91c26`, whose downgrade loses no
  query (a builder query's joins are still in `config`, and a statement is its own record of what it
  reads) — what comes back is the old *understatement* of a tool's scope, so it reports and proceeds.
- **Additive and nullable is the cheap case.** `NULL` and `[]` both meaning "one table", or "no
  arguments", is what lets a release running only that migration behave **identically** to the one
  before it. Three columns were added that way: `extra_tables`, `sql_params`,
  `allow_recursive_aggregate`.
- **`query_mode` is a `VARCHAR`, not a database enum** — a third mode would otherwise need a migration
  on the type itself, and the value is validated on every write anyway.
- **Functional indexes are hand-written**, because autogenerate cannot detect
  `Index(..., text("lower(col)"))` — and **will re-propose them on every future run.** Three exist
  (`uq_tool_config_agent_name_lower`, `uq_workspace_user_name_lower`, `uq_tool_graphs_user_name_lower`).
- **Remove what belongs to another revision.** Because a functional index cannot be compared,
  autogenerate re-proposes a change `b1f7c2d94a05` already made; it was **stripped out** of
  `fc462a9f1e5d` rather than carried along. **A revision that quietly owns somebody else's change is a
  revision nobody can reason about.**

One modelling note worth copying: `tool_graphs.data_agent_id` is nullable **and** unique, expressed as
**one unique index** rather than an index plus a constraint (which is what `unique=True, index=True`
actually emits). PostgreSQL exempts NULL from a unique index, so that single object expresses both
halves of "one graph per agent, one agent per graph" while leaving an unattached graph legal. Same trick
as `ChatbotFlow.chatbot_key_id`.

### Keeping the chain honest

`create_all` used to **cover for gaps in the chain**, because it built whatever the models declared
regardless of what the migrations said. Two had accumulated by the time it was removed, both closed by
`2abb54ec1a3b`: `chatbot_widget_settings` existed in the models but **no revision created it**, and
`created_at`/`updated_at` on **nine tables** were `NOT NULL` in the models and nullable in the chain.

**Nothing prevents that recurring**, so after a schema change: build an empty database from the chain
and diff it against `Base.metadata` with SQLAlchemy's inspector — tables, columns, nullability, indexes.
A chain-built database should match the models exactly.

```bash
docker compose exec db psql -U getmystuff -d postgres -c "CREATE DATABASE migtest"
docker compose exec -e DATABASE_URL=…/migtest app alembic upgrade head
```

---

# 27. Testing architecture

Driven by the `full-test-coverage` skill, which runs the suite, writes tests for what is not yet
covered, and records each run.

### Why the tests run in the container

The local venv is 3.10, so `app/services/deep_agents/` **cannot be imported at all on the host** — a
host run would silently skip part of the codebase and **report a coverage number that was not true.**

`langgraph` is in the same position, handled the same way: anything that compiles or runs a graph opens
with `pytest.importorskip("langgraph", reason="…container only…")`, so a host run **skips it loudly
rather than erroring.**

And this is why the correctness-carrying modules are split out from the ones that implement them:
`partial_algebra`, `frame_ops`, the planners and every schema import neither langgraph nor a provider
SDK, so **the rules that carry a feature's correctness stay runnable anywhere.**
`test_partial_algebra.py` checks the arithmetic against SQLite with no DataFrame library and no graph in
the process at all.

The repository is bind-mounted read-write at `/app`, so tests written on the host run in the container
immediately and reports appear on the host. No copying, no rebuild.

### Environment defaults, set before any app import

`tests/conftest.py` uses `os.environ.setdefault` **before importing anything under `app/`**, so a real
value always wins:

| Variable | Why the suite needs it |
|---|---|
| `DATABASE_URL` | `db_sessions.py` calls `create_async_engine` **at module scope**, so an unset or PostgreSQL URL would explode on import or point the suite at a real database |
| `JWT_SECRET_KEY` | **`auth.py` raises at import when this is unset** — deliberately, so a deployment can never run on a guessable signing key |
| `FERNET_KEY` | **`crypto.py` raises at import when this is unset**, for the same reason as `JWT_SECRET_KEY`. Every stored credential is encrypted with it — datasource passwords, AI provider keys, Action headers. The suite's default is the legacy literal, which is what makes the re-encryption migration a no-op under test |
| `OLLAMA_BASE_URL`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` | Set to unreachable/dummy values **so a missed mock fails loudly** rather than reaching a real provider |

`JWT_SECRET_KEY` and `FERNET_KEY` are the two that bite a new checkout, because the application
refuses to start without either. `FERNET_KEY` has the sharper failure mode of the two: an unset
value stops the process, but a *changed* one starts cleanly and then makes every stored secret
unreadable the next time each is used — nothing decrypts at boot, so there is no early warning.
The retired-key variable `FERNET_KEY_OLD` is what turns a key change into a background
re-encryption instead; see [SECRETS_AND_KEY_ROTATION.md](SECRETS_AND_KEY_ROTATION.md) for the
step ordering and the verification that has to pass before the old key is dropped.

### SQLite, and the four shims that make it possible

Every test gets a fresh in-memory SQLite database, created per test and thrown away, so no test can see
another's rows and the suite has no shared state to reset.

The models are written against PostgreSQL, and four column types have no SQLite rendering. Four
`@compiles` shims are registered **at import time, before any metadata is compiled** — without them
`create_all()` fails outright:

| Type | Rendered as | Why it is needed |
|---|---|---|
| `JSONB` | `JSON` | Used by 7 model modules. Without it: `CompileError: can't render element of type JSONB` |
| `postgresql.UUID` | `CHAR(36)` | Every model's public identifier |
| `pgvector.Vector` | `BLOB` | Lets `knowledge_chunks` be created |
| `BigInteger` | `INTEGER` | **The one that is easy to miss.** SQLite only auto-assigns a rowid for a column declared exactly `INTEGER PRIMARY KEY`, so without this **every single insert fails** with `NOT NULL constraint failed: <table>.id` |

With all four, all 22 tables create cleanly.

**The limitation worth knowing:** the `Vector` shim only lets the table be *created*. The `<=>` operator
has no SQLite equivalent, so tests touching vector similarity must mock the query layer. If real pgvector
behaviour ever needs testing, the compose `db` service is the place.

**A second non-obvious piece:** the engine uses `StaticPool`. `sqlite+aiosqlite://` gives *each new
connection* its own empty database, so without a pool holding one connection open, the tables created by
the fixture would be **invisible to the next query.**

### Reaching an authenticated route

Every controller except the auth and public ones sets `dependencies = {"user": require_auth}` as a
**class attribute**, which overrides any app-level provider. **Passing a fake user into
`Litestar(dependencies=…)` is therefore silently ignored and the request 401s** — injection-based auth
faking does not work here.

What works is minting a real token, which exercises the genuine
`require_auth → decode_token → user-lookup` path rather than bypassing it, **so the authentication code
is covered too.**

**An unauthenticated request does not return 401.** `main.http_exception_handler` turns it into a
redirect to the login page, or — for an HTMX request — a **200 carrying `HX-Redirect`**, because a plain
redirect would swap the login page into whatever element issued the request. Tests must assert the
redirect, not the 401.

### The five mocked boundaries, and the guard that enforces them

| Boundary | Fixture |
|---|---|
| Local LLM (Ollama) | `mock_ollama` |
| Anthropic / OpenAI SDKs | `mock_llm_sdks` |
| Outbound webhooks | `mock_outbound_http` |
| LangChain / deepagents | `mock_deep_agent` |
| User-supplied databases | `mock_external_datasources` |

**An autouse fixture blocks outbound TCP and raises a named error naming the host.** A missed mock would
otherwise hang until a timeout or — worse — **quietly succeed against a real service and make the suite
depend on the network.** Loopback stays allowed; `@pytest.mark.external` opts out and should be rare.

**That guard is how two real bugs were found**, both in the next category.

### Code that opens its own session

Most application code gets its session injected. Three paths do not: the export graph's nodes, the queue
worker, and the progress SSE stream — which **outlives the handler that returned it, so it *cannot* use
the request's session.** They open their own from an engine built at import time from `DATABASE_URL`.

**In the container that variable is the development PostgreSQL database.** And the conftest defaults use
`setdefault`, which does not override a value already set. So without help, **those paths read and write
the development database while the assertions look at the in-memory one.**

| Fixture | Redirects |
|---|---|
| `background_sessions` | The export nodes, the worker, the SSE stream |
| `graph_sessions` | A designed graph's nodes, its background task and its poll loop (autouse in that package) |
| `graph_checkpointer` | Forces `InMemorySaver` and clears the cached saver per test |

The Graph Designer package has its own autouse copies plus **one more: a fixture that cancels any run
still in flight when a test ends.** A run is a background task, and one outliving its test keeps writing
through a session bound to an engine the teardown has disposed of — **which surfaces as an unrelated
*later* test failing on a closed connection.** Autouse rather than opt-in, because every test in that
package runs a graph and the failure from forgetting one is confusing rather than obvious.

### What the suite cannot see

**JavaScript.** There is no JS harness. What *can* be asserted is asserted from route tests (that the
shared canvas script is included before the feature's; that a refusal quoting a user's node label comes
back escaped) and from **reading the generated widget source** — that a helper exists, that a socket is
closed, that the card names none of the four widget-input variables. **That catches an edit that removes
a property; it cannot catch one that leaves the property in place and broken.**

So the rest was verified outside pytest, and **how** is recorded because both methods found real bugs
every Python test passed straight through:

- the shared canvas primitives were compared against the pre-extraction arithmetic copied verbatim out
  of git — **83 assertions, all identical** — which found two id generators minting identical ids;
- both canvas pages and the download flow were **driven in headless Chromium**, which found the
  named-SSE-event bug and, in the download card, the ContextVar bug.

Two traps in slicing a generated script for assertions, both of which produced **tests that passed while
asserting nothing**: anchor the slice on a string that appears **once** (`"// The download card"` also
opens that card's CSS, so slicing on it returned the stylesheet), and **prefer a sweep over a list of
fixed strings**, so a new call site cannot be added without satisfying the rule.

And one lesson about *how* to verify a link: an earlier run verified the download by **curling its
target**, which proved the *route* worked and said nothing about whether the *page* could reach it —
which is exactly how the relative `href` shipped. **A link is only verified by following it from the page
it is rendered on**, so the flow is driven end to end and the button is *clicked*, with assertions on
the browser's own download events.

**Context propagation across tasks** is the other blind spot, and §12's rule 3 is its statement. Every
Python test passed a version of `download_notice` with exactly that bug, because they all called the
setter and the getter in one context. The regression test now calls the setter inside a real
`asyncio.create_task` and asserts the parent sees it. **Any new context-local read at the top of a turn
wants the same test.**

`main.app` is never served by a test — its `on_startup` migrates, seeds and calls out over the network.
`build_test_app()` assembles an equivalent app from the same controllers, middleware and exception
handler; `main.py`'s own functions are covered directly.

**The suite builds its schema with `create_all` against SQLite and never goes through Alembic**, because
the chain is PostgreSQL-specific (JSONB, `vector`, functional indexes) and could not apply to SQLite
anyway. That means the tests do **not** verify the chain matches the models — §26 describes the
from-scratch diff that does.

### Nothing that produces or consumes data is mocked

The datasource under every export, chain and aggregation test is a **real SQLite file** and the writers
**really write**. Mocking them would prove the graph calls them, where running them proves an export of
125 records contains 125 records.

Three habits from those suites are worth copying:

- **Assert on the set of ids, not the count.** Batch boundaries are checked at 1, 49, 50, 51, 100 and
  125 by the *set of ids* read back — **a reader that repeated one row and dropped another would pass a
  length check.**
- **Make the fixture data disagree on purpose.** The chain tests use client 1 paid *and* active, client
  2 paid but churned, client 3 active but unpaid — **so a chain that skips a level returns more rows and
  the test notices.**
- **Compare against the database, not a re-implementation.** Aggregation exactness is checked against
  `GROUP BY` **run by SQLite itself**, because the promise is that the answer matches what the database
  would have said.

`FOR UPDATE SKIP LOCKED` **cannot be proved on SQLite** — the dialect has no locking clause, so
SQLAlchemy drops it. What the tests cover is the claim's bookkeeping; the locking itself is a PostgreSQL
guarantee only a concurrent PostgreSQL test could demonstrate.

### The coverage ratchet, and the blind spot it compensates for

Coverage is measured over **all** of `app/` and `main.py`, with **no `omit` list**, deliberately:
excluding awkward files inflates the number into something meaningless. If a file genuinely should not be
measured it should be deleted or moved out of `app/`, not hidden.

`make_report.py`:

- fails with **1** if total coverage fell below the stored baseline;
- fails with **2** if the suite itself failed;
- **updates the baseline only on a green, non-regressing run** — a broken run can never lower the bar;
- diffs the per-file list to detect modules that did not exist at the last run.

**The blind spot:** coverage.py cannot see a module that nothing imports. Its scan for never-executed
files only walks real packages, and **`app/services`, `app/models`, `app/utils` and `app/schemas` have
no `__init__.py`** — they are namespace packages, so the scan skips them.

**The consequence is worse than a low score:** such a file is not reported at 0%, it is **absent from the
report altogether.** It contributes nothing to the denominator, so a brand-new untested module leaves the
percentage **completely unchanged.** The one thing a coverage tool is supposed to catch, it silently
misses.

So `make_report.py` builds its file list by **walking the filesystem** and cross-checks it against the
coverage data. Anything on disk but missing from the report is listed under **Unmeasured source files**
and forces exit code **3**, so a run in that state can never be mistaken for a complete one.

**The blind spot is closed** — every source file on disk is measured. It took four files, and how each
was closed is worth keeping, because the same choice recurs:

| File | How it was closed |
|---|---|
| `app/utils/csv_to_db.py` | **Tested.** Assumed untestable because of a hardcoded Windows path — but that constant is only read under `if __name__ == "__main__"`, so importing never touches it, and its functions take an engine and a path as arguments |
| `app/models/ai_analytics/prompt_configurations.py` | **Imported.** Zero bytes, so importing is harmless and makes coverage count it |
| `app/models/subscriptions/` | **Deleted.** Could not be imported at all and duplicated a model already mapped elsewhere. Measured for one run by a test asserting its import failure, then removed — with a test asserting it stays gone |

**The general lesson: *"nothing imports it"* is not the same as *"it cannot be tested"*.** Before
declaring a file dead, check whether its unrunnable parts are confined to a `__main__` guard or to
module-level constants no function actually reads. Only one of the four turned out to be genuinely dead.

Adding `__init__.py` to those four directories would let coverage find them by itself. **That is an
application change rather than a test change**, so it has been left to a deliberate decision.

### The run history

Two committed artifacts under `tests/reports/`: one **timestamped report per run** (UTC *and* local
start time, duration, commit, branch, pass/fail/error/skip counts, coverage against baseline, every
failure with test id, `file:line`, exception class, message, traceback and a written root cause, plus a
gap table of every file below 100% with its uncovered line ranges), and an append-only **`HISTORY.md`**.

Committed on purpose: **a timestamped record of what broke and when is only useful if it survives.** Only
the raw intermediates are gitignored.

**The scripts, not the model, produce every number and timestamp.** A model asked to read a terminal dump
and write down a percentage will eventually write down the wrong one, and **a coverage report nobody can
trust is worse than none.**

If `test_harness_smoke.py` fails, fix that before anything else: every other test depends on the fixtures
it guards.

---

# 28. Cross-cutting invariants

A checklist. Each of these is a property the codebase maintains everywhere, and each has at least one
test or structural guarantee behind it. **If you are changing code and one of these would stop being
true, that is the review conversation.**

### Data and queries

1. **No model-produced text reaches a query.** Values only, bound, on the right of a comparison an
   operator wrote.
2. **Identifiers are resolved against the live schema**, never interpolated from a payload without
   passing `require_object_name`.
3. **Every stored query is re-validated on every run**, not trusted from the row.
4. **Active-table and active-column status is checked on every run**, not at save time.
5. **A switched-off reference fails the tool**; it is never dropped from the query.
6. **The SQL preview is never executed.** It inlines values; the executor shares no code with it.
7. **No feature writes to a user's datasource.** Every path goes through the read-only guard.
8. **Non-relational datasources are refused at save time** where the feature cannot serve them, not on
   first call.

### Numbers and honesty

9. **Every count shown to a user is exact**, or explicitly labelled as a sample with the real
    total beside it.
10. **Bounds refuse rather than truncate**, and a bound that could not be made honest was removed
    instead: no query is capped. The one exception is `PROMPT_ROW_LIMIT`, which shortens a *prompt*
    rather than a result and states the exact total beside what it shows.
11. **A result is described as what it is**, not as what was asked for.
12. **A preview states the real count**, never the sample size.

### Identity and authorisation

13. **No bigint `id` reaches a browser.** Asserted for every mapped model by
    `test_model_contracts.py`.
14. **Ownership failures are 404, not 403.** Not-found, someone-else's and malformed all get the same
    sentence.
15. **Public download access requires the widget key uuid *and* the conversation token.**
16. **Caller-supplied strings never become path components** without normalisation *and* a
    containment re-check on every request.
17. **No secret is in a response schema.** A test walks the AI-key view's field names.

### Failures

18. **Every failure path reports somewhere.** Server-side to the log, widget-side to the console; a
    silent `.catch` is a regression with a test against it.
19. **No stack trace or driver message reaches a prompt or a visitor.** The Test Query path is the one
    deliberate exception, and its audience is the operator.
20. **Every user-facing message names the thing to change.**
21. **A message shared by two enforcement points lives with the rule**, not in either caller.
22. **A validation failure is an `HTTPException` with a readable detail** and the real status code.

### Concurrency and lifecycle

23. **Nothing large travels in graph state.** Registries keyed by run, state carries keys.
24. **A context-local written inside a task is a mutable box**, never a rebind.
25. **Every terminal path releases its resources** — a cleanup node *and* a `finally`, because
    cancellation routes nowhere.
26. **`recursion_limit` is computed from the work**, never left at the default.
27. **An `EventSource` is closed before anything that can throw**, and an expected end is distinguished
    from a lost connection by an explicit flag.
28. **Progress and run state are read from rows**, not from an in-memory bus.

### Frontend

29. **Row markup is built with `createElement`**, never `innerHTML`.
30. **The widget escapes before it parses.** Every emitted tag is the renderer's own; no attribute comes
    from message text.
31. **Server-sent URLs the widget consumes are relative paths.** Swept by a test over every network call
    in the card.
32. **A repeating-row group posts as one JSON field.**
33. **A panel renders its own errors**, and a non-2xx HTML body is opted back into the swap globally.

### Documentation

34. **A new module gets its own folder in every layer it needs**, and its own deep-dive document, linked
    from `ARCHITECTURE.md`.
35. **A cap, a message or a limit changed in code is changed in `USER_GUIDE.md` and here.** The in-app
    Tool Configs help page is a third copy and moves with them.

---

# 29. Decision log: tried and rejected

The alternatives matter as much as the choices, because most of them look better on paper.

### Runtime

| Considered | Why not |
|---|---|
| Build the agent on `langgraph` directly (`create_react_agent`), staying on 3.10 | **Verified to work**, and was the initial recommendation. Rejected on the explicit instruction to use `deepagents` proper |
| Use the system `/usr/bin/python3.11` | It is `3.11.0rc1` — a release candidate shipped by Ubuntu 22.04. Not an interpreter to put an analytics platform on |
| `uv python install 3.12` + a local venv, so the app could still run outside Docker | Attempted. The CPython download **timed out repeatedly**, including with `UV_HTTP_TIMEOUT=900` |
| Force-install `deepagents` on 3.10 | Fails at import. Metadata confirmed honest |
| Reuse the host's Ollama (it already had all four models — would have saved 5.2 GB) | Host Ollama binds `127.0.0.1` and is unreachable from a container (verified: `--add-host=host.docker.internal:host-gateway` returns nothing). Making it reachable needs `OLLAMA_HOST=0.0.0.0`, **which also exposes it beyond localhost** — a host-level change with a security consequence |
| `OLLAMA_CHAT_MODEL=qwen3:8b` for everything | Drags every in-built feature onto a model ~3× slower on CPU to enable one feature |
| Preload the deep-agent model at startup | `keep_alive=-1` would pin ~5 GB resident for a feature that may go unused |
| Widen the visitor timeout for the local model | Would let a chatbot visitor wait seven minutes. Degrading to the profile reply serves them better |

### Query execution

| Considered | Why not |
|---|---|
| Reuse `build_query_preview` as the execution path | It inlines filter values with f-strings. **Executing it would make every stored filter value an injection vector** |
| Wrap a SQL-mode statement as `SELECT * FROM (…) LIMIT n` | Changes the SQL the operator approved, **and MySQL rejects a derived table with duplicate output column names** — exactly the query the mode exists to permit |
| Substitute a LEFT/FULL join for an unsupported RIGHT JOIN | Quietly changes which rows come back |
| Drop a reference to a switched-off column from the query | A dropped filter widens the result; a dropped group-by changes what each row counts |
| Type the agent-supplied argument schema from the reflected column | Needs a reflection at prompt-build time for a tool that may never be called, **and would still need re-checking at execution.** One answer to "what type is this", not two that can disagree |
| Parse operator SQL to extract joins / the SELECT list / already-averaged columns | No parser here is strict enough to trust. A confident wrong picture is worse than an honest note |
| A second copy of the read-only guard in the schema layer | It would be **the copy nobody checks against the one that runs at query time** |

### Graphs and concurrency

| Considered | Why not |
|---|---|
| A function with a loop instead of the export graph | Two of its edges *are* the feature: a pause across two requests, and one cleanup node every terminal path passes through |
| Retry the export batch as a graph edge | A checkpoint write per attempt, a three-way router, and a crash mid-retry resuming into a state the cursor no longer matches |
| Resume a half-built export after a worker dies | The dead worker's part files are on disk and its cursor is not; a resume would have to **trust files it cannot verify were written completely** |
| `LIMIT/OFFSET` paging for the export reader | Incorrect without a total order, and re-sorts the whole result per batch — 10,000 sorts of half a million rows |
| Two tasks reading one `BatchReader` | It is one cursor; out-of-order reads re-run the statement and rescan from the top |
| Implement a barrier after the aggregation fan-out | LangGraph's super-step **already is one.** Writing one would be writing a second, worse one |
| A checkpointer on the chain / aggregation graphs | Nothing to resume across requests. It would write the whole state 750 times for a large run |
| Parallel siblings in a chain, or parallel loop iterations | The first empty sibling ends the run, so sequencing means the rest is **never paid for**; and a parallel fan-out needs a barrier to know the row budget is spent, by which point every query has already run |
| Ban all cycles in a designed graph | Bans loops, which is the thing the feature exists for |
| Allow all cycles | `A → B → A` compiles and stops only at `GraphRecursionError`, far from the cause |
| Redis / Celery / arq for the export queue | Throughput this feature will never need, and a service to operate that it would not justify. **A locked row is durable, cross-process, and visible in the same database** |
| `pd.concat` to merge export parts | Holds the whole export in memory — the thing the feature avoids — and turns an int column with a NULL into floats |
| pandas for the aggregation fold | Holds the GIL, which would serialise the fan-out and leave the wave pattern doing nothing |

### Prompts and models

| Considered | Why not |
|---|---|
| Generate the routing prompt with an LLM | It could describe a tool the agent does not have, would drift between saves, would cost money per tool change, and would send data out of the box |
| One prompt column instead of two | Two writers racing: an operator clobbers a regeneration, or a regeneration overwrites authored words |
| A version constant instead of a rules hash | **The number is the thing that gets forgotten in the same commit that edits a rule** |
| Retry the whole agent turn on a rate limit | Re-executes every tool call that already succeeded, for a failure that happened after them |
| Native tool-calling inside `ai_analytics_service` | Three provider implementations, all forcing structured JSON output, which collides with tool-calling |
| Native tool-calling for webhook actions | Same three implementations, same collision. The router pass costs one round trip and is provider-agnostic |
| Patch a `GROUP BY` violation instead of regenerating | Changes what the query counts, and the explanation beside it would then describe a different query |
| Loop the grouping retry | A third call is a model that is not going to get there |
| One `aggregate_records` variant per opted-in tool config | Several free-text tools in front of a model just told to pick the single tool matching the question |
| Retry inside the aggregation planner | The agent's own loop is the correction path and already exists |
| Let the model choose an aggregation alias | An alias is an output column name; one colliding with a group key would overwrite it |

### Delivery

| Considered | Why not |
|---|---|
| Serve exports from `static/` | **No authentication at all** — a static mount bypasses the route enforcing key, session and expiry. And it is *also* a relative path, so it would have produced the identical 404 while giving the data away |
| Prefix `SITE_URL` onto widget URLs | `API_BASE + url` on a deployed older script produces a URL the browser never sends — **nothing logged, nothing thrown, only silence** |
| Assign the model's answer to `innerHTML` directly | Renders the table correctly and is the XSS hole the escape-first renderer exists to prevent |
| Support Markdown links in the widget | `[text](javascript:…)`, plus a URL-scheme check to get wrong |
| `preventDefault()` to stop the double-submit | htmx's listener is on the form itself, and `preventDefault` does not stop other listeners |
| Remove `hx-post` at runtime to stop it | **htmx captures the verb and path in a closure when it processes the node** |
| Give the model a download URL | A second copy of a control the user already has, and it renders as visible markdown |
| A three-circle Venn for a three-table join | Implies an intersection the query never computes |
| Store node positions in Tool Graphs | Derived positions cannot fall out of step with the tools |
| Compute graph layout in JavaScript | Layout can be wrong without looking wrong, and there is no JS test harness |
| Verify a download by curling its target | Proves the *route* works and says nothing about whether the *page* can reach it. **That is how the relative `href` shipped** |

---

# 30. Known gaps

Stated rather than hidden. Each is a real limitation somebody will otherwise rediscover.

### Feature gaps

- **Writing into a system this application does not own is the Integration Platform's job, and
  only against a REST API so far.** Every *query* engine — the agent executor, Graph Designer,
  tool chaining — reads from the user's own relational datasources and says so in its own
  invariant; that has not changed. What has is that the [Integration Platform](INTEGRATIONS.md)
  now exists alongside them: a drawn workflow, published as a frozen version, run by a queue on
  a schedule, moving records into a third-party system a batch at a time.
  **Phase 1 ships generic REST with API-key authentication**, and
  [Shopify](SHOPIFY_CONNECTOR.md) has since landed on top of it — the Admin GraphQL API,
  **read-only** across orders, products and customers, authenticated by a custom app's access
  token. Read-only is asserted rather than assumed: the connector declares no write
  operations, so a `connector_write` node cannot select it, because Shopify's mutations take
  no idempotency key and a create that times out after reaching the server has already
  happened.

  Still specified rather than built: **Shopify writes**, GoHighLevel, SAP, and with them
  OAuth, inbound webhooks and cron schedules. Shopify needed no OAuth — a custom app token
  does not expire — so the OAuth path remains half-present: the `integration_oauth_states`
  table, the CAS refresh lock and the `AUTH_OAUTH2` send branch all exist and are all unused,
  and `ensure_fresh_token` has zero callers.

  The three defects that blocked storing a third-party credential at all (a hardcoded Fernet
  key, an SSRF guard trapped inside the chatbot service, type coercion trapped with it) are
  fixed; see [SECRETS_AND_KEY_ROTATION.md](SECRETS_AND_KEY_ROTATION.md) for what rotating the
  key now requires.
- **Vendor rate limiting has no cost dimension.** The leaky bucket spends one token per
  request whatever the request costs. That is right for a per-request limit and only
  approximate for Shopify's Admin API, which is priced in points: a 900-point query and a
  5-point query are charged alike. Mitigated by correcting the bucket from
  `extensions.cost.throttleStatus` in every response — a correction that only ever *lowers*
  the local view — and by retrying `THROTTLED` with a wait computed from the vendor's own
  numbers. A points dimension on the bucket is the honest fix and is not done.
- **Webhook actions do not run on the data-agent path.** The action router is a second model call that
  picks a webhook, and a Deep Agent already decides which tool to call — running both puts two
  independent routers in charge of one turn.
- **A Flow Builder AI Fallback node does not use an attached data agent.** It answers through the profile
  path — and since an agent-backed chatbot has no `datasource_id`, a node left on the `datasource`
  context source raises rather than answering. Those nodes have to be pointed at a knowledge base or
  the prompt until the path learns about agents.
- **One action per turn, no chaining.**
- **Turns are stateless:** no conversation history is sent to the model.
- **The prompt's `EXIT` rule is text only** — the model sends the closing message, but the widget session
  is not terminated; there is no signal channel for that.
- **Whole-result grouping does not support non-relational datasources.** That is the largest genuine gap
  in the platform's aggregation story, and closing it means writing file and Mongo readers — a different
  piece of work.
- **No aggregation across two datasources.**
- **No export from a non-relational datasource.**
- **Subqueries on the Configurations page cannot join**, and that page has no edit form for a Tool Base
  Config.
- **Auto Create Tool only creates** — no "update this existing tool from a new query".
- **Graph Designer has no scheduling and no parallel branches.**
- **The operator console draws no download card.** It has no conversation history, so it cannot resolve a
  "yes"; the notice carries console URLs for the day that changes.

### Correctness limitations, honestly bounded

- **In SQL mode, an already-averaged column cannot be detected**, because nothing parses operator SQL. So
  averaging an average is refusable in builder mode and undetectable in SQL mode.
- **A declared SQL value's `type` is the operator's claim**, not a fact derived from the statement.
- **SQL mode has no column-level active check.** The statement is the permission at column level.
- **`group_by_violation` and `missing_identifiers` are text heuristics**, deliberately silent when unsure.
- **Syntax is never checked.** Query Test is the answer.
- **DNS rebinding is narrowed, not closed.** Check-then-request leaves a window; closing it needs IP
  pinning at the transport layer, which httpx does not expose.

### Operational

- **`docker-compose.yml` is development-shaped** — `--reload`, source bind-mounted, `debug=True`. A
  production compose file drops all three.
- **The startup admin seed is dev-only** and must not reach production.
- **The hosted provider path is unexercised end to end.** The agent machinery is proven by the local
  `qwen3:8b` run and by a stub-model integration test; the first real Anthropic call is not yet verified.
- **`db_utils` circuit-breaker constants are un-cast `os.getenv` strings** (`ENGINE_TTL_SECONDS`,
  `CIRCUIT_FAILURE_LIMIT`, `CIRCUIT_RESET_SECONDS`), **so the breaker cannot trip**, and the two
  `cleanup_idle_*` coroutines are never scheduled.
- **`db_utils.get_engine` raises a bare `Exception("Database temporarily unavailable (circuit open)")`
  that no tool-path handler catches**, so it becomes a 502 for the whole turn rather than a recoverable
  tool failure.
- **polars oversubscription** — `POLARS_MAX_THREADS` may want setting, and it is read at import.
- **polars needs `polars-lts-cpu` on an older CPU**, where the default wheel dies with
  `Illegal instruction` and no traceback.
- **Changing `OLLAMA_EMBED_MODEL` to a different output size needs a migration** on the vector column
  plus a full re-embed.

### Testing

- **The Alembic chain is not verified against the models by the suite.** The from-scratch diff in §26 is
  the manual check.
- **pgvector similarity cannot be tested on SQLite.**
- **`FOR UPDATE SKIP LOCKED` cannot be proved on SQLite.**
- **There is no JavaScript test harness.** The headless-browser runs are manual.

---

# 31. Reading map

Where to go for the next level of detail on each topic.

| Topic | Document |
|---|---|
| The product as workflows, in plain language | [USER_GUIDE.md](USER_GUIDE.md) |
| Layering and folder structure | [ARCHITECTURE.md](ARCHITECTURE.md) |
| Where a rule belongs; the status cascade | [SERVICE_PATTERNS.md](SERVICE_PATTERNS.md) |
| The agent runtime, prompt composition, staleness, providers | [DEEP_AGENTS.md](DEEP_AGENTS.md) |
| Building a tool config, one worked example per scenario | [TOOL_CONFIGS.md](TOOL_CONFIGS.md) |
| The two query modes and the shared guard | [TOOL_QUERY_MODES.md](TOOL_QUERY_MODES.md) |
| Join rules, shared by both authoring surfaces | [QUERY_JOINS.md](QUERY_JOINS.md) |
| Nesting: the graph, the caps, the refusals | [TOOL_CHAINING.md](TOOL_CHAINING.md) |
| Iterating links and assistant-supplied SQL values | [TOOL_CHAIN_ITERATION.md](TOOL_CHAIN_ITERATION.md) |
| Test Query | [QUERY_TEST.md](QUERY_TEST.md) |
| Ask AI and Auto Create Tool | [SQL_ASSIST.md](SQL_ASSIST.md) |
| The authored canvas, its runs and its dock | [GRAPH_DESIGNER.md](GRAPH_DESIGNER.md) |
| The derived, read-only diagrams | [TOOL_GRAPHS.md](TOOL_GRAPHS.md) |
| Exports: the count, the offer, the file | [DOWNLOADER_AGENTS.md](DOWNLOADER_AGENTS.md) |
| Whole-result grouping and its exactness proof | [AGENT_RECURSIVE_DATAFRAMES.md](AGENT_RECURSIVE_DATAFRAMES.md) |
| Prompts, variables, model modes, the action router | [CHATBOT_AI_SETTINGS.md](CHATBOT_AI_SETTINGS.md) |
| Scripted conversations, AI fallback, knowledge bases | [FLOW_BUILDER.md](FLOW_BUILDER.md) |
| The local Ollama client and the embedding pipeline | [AI_INBUILT.md](AI_INBUILT.md) |
| Per-turn measurement and the dashboard | [CHATBOT_ANALYTICS.md](CHATBOT_ANALYTICS.md) |
| How an answer becomes what a visitor sees | [WIDGET_RENDERING.md](WIDGET_RENDERING.md) |
| The validation layer, schema by schema | [SCHEMAS.md](SCHEMAS.md) |
| What reaches a user versus what stays in the log | [ERROR_HANDLING.md](ERROR_HANDLING.md) |
| HTMX swap patterns and the global handlers | [HTMX_PATTERNS.md](HTMX_PATTERNS.md) |
| Schema application, the three states, conventions | [MIGRATIONS.md](MIGRATIONS.md) |
| The container, the local-model tuning, the measurements | [DOCKER_AND_LOCAL_LLM.md](DOCKER_AND_LOCAL_LLM.md) |
| The suite, the fixtures, the coverage ratchet | [TESTING.md](TESTING.md) |

---

## Keeping this document true

Two consolidated documents exist and both must move with the code:

- **[USER_GUIDE.md](USER_GUIDE.md)** — the workflows, the limits table, the message-to-fix table.
- **ENGINEERING_TECHNOLOGY.md** — this one: the *why*, the invariants, the rejected alternatives.

A change to a cap, a message, a default or a refusal touches both. A new feature adds a section to each,
plus its own deep-dive page linked from [ARCHITECTURE.md](ARCHITECTURE.md) and from §31 above. A new
invariant goes in §28; a rejected alternative goes in §29; a limitation goes in §30 rather than being
left for somebody to rediscover.
