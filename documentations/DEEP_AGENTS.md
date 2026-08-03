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

Note what is *not* reused: `tool_config_service.build_query_preview()` and
`query_joins.build_join_sql()` render a config as SQL text and inline filter values
with f-strings. They are display-only and always were — executing them would make
every stored filter value an injection vector. The executor mirrors them clause for
clause but shares no code with them, so the preview an operator reads and the query
that runs describe the same thing without the preview becoming a code path.

Verified: a stored filter value of `x' OR 1=1 --` comes back as zero rows, and
`%'; DROP TABLE customers; --` through a `LIKE` filter leaves the table intact.

**SQL mode.** The stored statement runs as written — running an approximation of a
query the operator approved would defeat the point of the mode. The safety comes from
elsewhere, and it is the same place: nothing the model produces is in the statement.
It was written and saved in advance, the tool takes no arguments, and it is re-checked
against `app/utils/sql_guard.py` on every run — one statement, a read, bounded length.
The 200-row cap is applied by *streaming* rather than by wrapping the SQL, so the
operator's query is never rewritten. Details, including why the wrap would be wrong,
in [TOOL_QUERY_MODES.md](TOOL_QUERY_MODES.md).

### Other bounds

* Every tool query is capped at 200 rows (`MAX_TOOL_ROWS`), in either mode, with no
  way for a config to raise it. `describe_result()` states the row count and says
  explicitly when the cap was hit, so a capped sample cannot be reported as a total.
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

---

## Tools take no arguments

A tool config already declares its whole query — the columns, aggregations, grouping
and filters in builder mode, or the statement in SQL mode. The tool built from it
exposes an empty argument schema, so the model's only decision is *which* tool to call.

Two reasons, and the first is the important one:

1. An argument would put model-generated text into the query. That is the single
   thing this feature exists to prevent, and no amount of validation on the argument
   makes it as safe as not having one.
2. It would let the model widen a filter the operator narrowed deliberately — a tool
   scoped to `status = 'paid'` is a decision, not a default.

The cost is real: the agent cannot answer "how many units for SKU A-1?" unless a tool
already filters to it. That is what the routing prompt's "say when no tool covers the
question" rule is for.

**Parameterised filters are the obvious next step** — an operator marks a filter value
as agent-supplied, and the value is coerced and bound against the reflected column
type. Not built, deliberately, because it is a design question (which filters may be
opened, and how the UI expresses it) rather than an omission.

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
matching data, a capped result is not a total.

An agent with **no** enabled tools gets an explicit "you have no data tools" prompt so
it refuses rather than answering from the model's own knowledge — which would look
like a working answer and be entirely invented.

### Sync is an optimisation, not a dependency

`sync_tool_routing_prompt()` runs as a Litestar `BackgroundTask` from every Tool
Configs mutation — create, update, set-enabled, delete — *after* the response is sent,
in its own `AsyncSessionLocal` session (the request's session is closed by then).
It swallows every exception: there is nothing left to report to.

That is safe because `deep_agent_service` compares `tool_prompt_synced_at` against the
newest tool config's `updated_at` and **regenerates inline** if it is behind. So a
failed task, a restart mid-flight, or a task that never ran costs one extra write on
the next answer and is never wrong. This is why the feature needs no queue table, no
scheduler and no retry logic — the first background work in this codebase, and it
stays that simple only because correctness does not rest on it.

Moving a tool between agents syncs **both**: the tool joins one agent and leaves
another, and the one it left is still describing it.
`update_tool_config()` returns both ids for exactly that reason.

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
docker compose exec app alembic upgrade head
```

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
* [DOCKER_AND_LOCAL_LLM.md](DOCKER_AND_LOCAL_LLM.md) — why this runs on Python 3.12 in a
  container, the local-model tuning, and the measurements behind the timeouts
