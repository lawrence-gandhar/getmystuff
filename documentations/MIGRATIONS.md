# MIGRATIONS.md

How the database schema gets built and kept current: Alembic, applied by the app itself
at startup.

Owner: `app/db/migrations.py`, called from `main.py`'s `on_startup`.

---

## Why this exists

Startup used to call `Base.metadata.create_all`. That creates tables which do not exist
yet and does **nothing else** — it never alters a table it already found. So a column
added to a model never reached an existing database:

1. `extra_tables` was added to `ToolConfig` and a migration was written for it.
2. The migration was never run — nothing ran migrations.
3. `create_all` saw `tool_configs` already existed and skipped it entirely.
4. The app booted clean. SQLAlchemy then put `tool_configs.extra_tables` in every
   SELECT against that table, and PostgreSQL answered `UndefinedColumnError`.

The result was a 500 on `GET /tool-configs` with nothing at startup hinting that the
schema was stale — from `create_all`'s point of view there was nothing left to do. The
failure surfaced one page at a time, as whichever query happened to name the new column.

`alembic upgrade head` inverts that. The migration chain *is* the schema's definition, so
a column arrives with its revision, and a database that is behind is a state Alembic can
name rather than one inferred from a failing query. Because the app applies it, the schema
can never be older than the code running against it.

A migration that cannot be applied **raises, and startup stops.** That is the point: a
database nobody can account for should not be served requests.

---

## The three states

`upgrade_to_head()` decides what to do from the shape of the database alone.

| State | What it looks like | What happens |
|---|---|---|
| **empty** | no tables | the whole chain runs, from the first revision |
| **tracked** | `alembic_version` present | pending revisions are applied; already at head costs one query |
| **untracked** | tables but no `alembic_version` | **refused** — see below |

**empty** builds the same schema `create_all` used to, including the `vector` extension:
revision `a3f5c9d21b47` issues `CREATE EXTENSION IF NOT EXISTS vector`. So this path needs
no help from `docker/postgres-init.sql`, which is now belt-and-braces rather than load-
bearing.

**tracked** runs on every boot. Under `uvicorn --reload` that is every code change, which
is affordable precisely because a no-op upgrade is a single query.

**untracked** is a database built entirely by the old `create_all` path — which is what
every developer's database was before this change. Its schema cannot be matched to a
revision by inspection, and both ways of guessing are worse than stopping:

* replaying the chain over existing tables fails on the first `CREATE TABLE`;
* stamping a revision that does not describe the schema hides real drift until something
  reads a column that is not there.

So it raises, naming the command to run. Resolve it once:

```bash
# 1. Confirm the schema really is up to date with the models first.
# 2. Then record the revision it matches:
docker compose run --rm app alembic stamp head
```

`head` is only correct if the schema is already current. If it is not, stamp the revision
that actually describes it and let the app apply the rest.

---

## Two things about how it runs

**It runs in a worker thread.** `alembic/env.py` drives an async engine through
`asyncio.run()`, which cannot be called from a thread that already has a running event
loop — and startup does. `asyncio.to_thread` gives it a thread with no loop of its own, so
`asyncio.run()` there behaves exactly as it does on the command line.

**An advisory lock serialises it.** `pg_advisory_xact_lock` is taken *before* the state is
inspected, so booting several workers at once (`uvicorn --workers N`) applies the chain
once instead of racing on `alembic_version`. Taking it after the inspection would let two
workers both read "pending" and both proceed. The lock is transaction-scoped, so it is
released when the transaction ends — including when it ends because the migration raised.

---

## `env.py` and logging

`alembic/env.py` calls `fileConfig()` to apply `alembic.ini`'s logging config. That is
right on the command line and wrong in-process: `alembic.ini` pins the root logger to
`WARNING`, so applying it during startup would silence the app's own INFO logging for the
rest of its life.

`app/db/migrations.py` sets `config.attributes["configure_logger"] = False`, and `env.py`
skips `fileConfig()` when it sees that. Nothing else about the CLI behaviour changes.

`env.py` also prefers `DATABASE_URL` over `alembic.ini`'s `sqlalchemy.url`, which is
hardcoded to a localhost address that resolves nowhere inside a container.
`_build_alembic_config()` sets the URL from the app's own `DATABASE_URL` as well, so the
app and its migrations cannot end up pointed at different databases.

### Tables in our database that are not ours

`env.py` passes an `include_name` hook that excludes any table whose name begins with
`checkpoint`. Those belong to **langgraph**, whose checkpoint store lives in this database
and creates its own schema through its own `setup()` (see
[DOWNLOADER_AGENTS.md](DOWNLOADER_AGENTS.md) and
[DOCKER_AND_LOCAL_LLM.md](DOCKER_AND_LOCAL_LLM.md)).

They are not in `Base.metadata`, so without the hook every `--autogenerate` run proposes
dropping their indexes — and a revision that carried those drops would break the export
confirmation the first time it was applied. Owned by langgraph, versioned by langgraph,
upgraded by langgraph. Matched by prefix because the set grows with its releases.

Filtering by *name* rather than with `include_object`: that hook only sees objects alembic
already decided to compare, and a foreign table has no object on our side to match against.

---

## Writing a migration

```bash
docker compose exec app alembic revision --autogenerate -m "what changed and why"
docker compose restart app          # startup applies it
```

Always read what autogenerate produced before keeping it. Conventions in this repo:

* **A rich module docstring.** Every revision in `alembic/versions/` explains what was
  wrong, what the change means for existing rows, and what the downgrade costs. See
  `e7b3f5a91c26_add_tool_config_extra_tables.py`.
* **Say whether the downgrade loses data**, and refuse in the migration if the loss would
  be silent — `c3a7d5e18b64` refuses rather than dropping authored SQL.
* **Additive and nullable is the cheap case.** A new nullable column with no backfill
  means a release running only that migration behaves identically to the one before it.
* **Functional indexes are hand-written.** Autogenerate does not detect
  `Index(..., text("lower(col)"))`; `uq_tool_config_agent_name_lower` and
  `uq_workspace_user_name_lower` are written out by hand.
* **Remove what belongs to another revision.** Because a functional index cannot be
  compared, autogenerate re-proposes swapping `uq_datasource_name_lower` for
  `uq_datasource_user_name_lower` on every run — a change `b1f7c2d94a05` already made. It
  was stripped out of `fc462a9f1e5d` rather than carried along; a revision that quietly
  owns somebody else's change is a revision nobody can reason about.

### Keeping the chain honest

`create_all` used to cover for gaps in the chain, because it built whatever the models
declared regardless of what the migrations said. Two had accumulated by the time it was
removed, both closed by `2abb54ec1a3b`:

* `chatbot_widget_settings` existed in the models but **no revision created it**;
* `created_at` / `updated_at` on nine tables were `NOT NULL` in the models and nullable
  in the chain.

Nothing prevents that recurring, so verify from scratch after a schema change — build an
empty database from the chain and diff it against the models:

```bash
docker compose exec db psql -U getmystuff -d postgres -c "CREATE DATABASE migtest"
docker compose exec -e DATABASE_URL=postgresql+asyncpg://getmystuff:getmystuff@db:5432/migtest \
  app alembic upgrade head
```

Then compare `Base.metadata` against the result with SQLAlchemy's inspector — tables,
columns, nullability and indexes. A chain-built database should match the models exactly.

---

## The Graph Designer's revision

`e4c9b7d05f31_add_graph_designer_tables` creates `tool_graphs`, `tool_graph_runs` and
`tool_graph_run_steps` — see [GRAPH_DESIGNER.md](GRAPH_DESIGNER.md).

Two things about it are worth knowing before the next autogenerate run.

`uq_tool_graphs_user_name_lower` is a **functional** unique index (`lower(name)`), so
Alembic cannot compare it against the model's declaration and **will re-propose it on every
future run**. That is the same pre-existing behaviour `uq_datasource_user_name_lower` and
`uq_tool_config_agent_name_lower` have, and it is noted here so the next person does not
carry it into their revision — see the rule above about removing what belongs to somebody
else.

`tool_graphs.data_agent_id` is nullable **and unique**, expressed as one unique index rather
than an index plus a constraint (which is what `unique=True, index=True` on the column
actually emits). Postgres exempts NULL from a unique index, so that single object expresses
both halves of "one graph per agent, one agent per graph" while leaving an unattached graph
legal.

LangGraph's own checkpoint tables are **not** created here. The saver builds its own schema
in `setup()`, and `alembic/env.py` already excludes them.

## The four revisions that gave a graph four owners, and a fifth thing to do

Each adds one nullable column, in a chain — see [GRAPH_DESIGNER.md](GRAPH_DESIGNER.md) for
what they are for.

`f7a2c95e3d10_add_tool_graph_workspace_id` adds `tool_graphs.workspace_id`. **Nullable and
deliberately not unique**, which is the one thing to read twice: `data_agent_id` beside it is
unique because an agent holding two graphs was a state nothing could describe, while a
workspace is a shelf and may hold several. So it carries a plain index and no constraint.
Their mutual exclusivity is a `graph_service` rule rather than a CHECK, because the refusal
has a sentence to say.

`a3e81b6c94d2_add_tool_config_link_child_graph` makes `tool_config_links.child_id` nullable
and adds `child_graph_id`, with `ck_tool_config_links_one_child` — a CHECK, unlike the rule
above, and the difference is the audience: a link with two children or none is a bug in this
application rather than anything a form can produce. It also adds a second unique constraint
for the graph target, because with `child_id` NULL Postgres treats every row as distinct and
graph links would never collide in the original one. **Its downgrade deletes the rows that
used the new column** before restoring `NOT NULL`, which they would otherwise violate — the
one destructive downgrade in this chain, and unavoidable: the earlier schema cannot express
those links.

`b8d40f2ca719_add_flow_session_awaiting_graph_run` adds
`chatbot_flow_sessions.awaiting_graph_run`, a plain `String(64)` with no foreign key. The
value is written by the flow engine, which runs for an anonymous visitor rather than inside
the owner's session, and is validated on use by `graph_runner.answer_graph_run`. A foreign
key would couple a visitor's transient session to a run log in the direction that helps
nobody.

`d5f1a9e2c437_add_tool_graph_allow_recursive_aggregate` adds
`tool_graphs.allow_recursive_aggregate` — the opt-in that lets an agent read a graph's whole
result and filter or total it in polars, see
[AGENT_RECURSIVE_DATAFRAMES.md](AGENT_RECURSIVE_DATAFRAMES.md). `NOT NULL` with
`server_default false` and **no backfill**, which is the pattern worth copying: there is no
state to preserve, only a default to establish, and a server default means Postgres needs no
`UPDATE` pass over the table. It is named to match `tool_configs.allow_recursive_aggregate`
rather than something graph-specific, so the service layer filters both kinds of source with
one expression instead of two — a second key would be a second thing to remember, and the
forgotten one would silently opt nothing in.

---

## Tests

`tests/unit/db/test_migrations.py` covers the three states by their observable
consequence — whether `alembic upgrade` was invoked at all — plus the config (which
database it targets, that logging is left alone) and the lock ordering.
`command.upgrade` itself is never run: it is Alembic's, and it needs a real PostgreSQL.

`tests/test_main.py` covers `on_startup` — that it migrates before seeding, and that a
migration failure aborts startup instead of continuing.

The test suite builds its own schema with `create_all` against SQLite
(`tests/conftest.py`) and does not go through Alembic at all. See
[TESTING.md](TESTING.md) for why, and for the four type shims that makes possible.
