# INTEGRATIONS.md
The Integration Platform — a workflow somebody drew, moving records between systems nobody here owns

> **Status: Phase 0 and Phase 1 are built, plus the Shopify connector.** Everything on this
> page up to and including the AI layer describes code you can read and tests you can run.
>
> **Shopify (Admin GraphQL, read-only) has since landed** and has its own page:
> [SHOPIFY_CONNECTOR.md](SHOPIFY_CONNECTOR.md). It is the first *vendor* connector, and
> building it required four changes to the shared runtime — the seams described here for
> exactly that moment had never executed, and two of them were broken. Those changes are
> recorded inline below as **Reconciled** notes.
>
> Still specification: GoHighLevel, inbound webhooks, SAP, the `agent` node, and **Shopify
> writes** (its mutations take no idempotency key). The sections describing them — §Webhooks
> and the OAuth half of §Credentials — say so where they begin.
>
> This page was written **before the first commit**, so the reasoning is on record rather than
> reconstructed afterwards, and it has now been **reconciled against what was actually built** —
> a document describing the design that got abandoned in week three is worse than no document.
> Where the implementation departed from the plan, the departure is recorded here with its
> reason rather than being quietly edited out; those notes are marked **Reconciled**.
>
> Where it cites an existing file, that file is real and was read.

---

# What it is

A canvas at `/integrations` where a user composes a workflow out of a trigger, reads from one
system, transformations, validations, filters, branches, batches and writes into another
system — then publishes it, points a schedule at it, and watches records move in a dock below
the canvas.

```
GET    /integrations/                              → the library
POST   /integrations/create
POST   /integrations/{id}/settings                 → name, batch size, redacted fields
POST   /integrations/{id}/active                   → switch on / off (needs a version)
POST   /integrations/{id}/delete
GET    /integrations/{id}/canvas                   → the canvas + the run dock
POST   /integrations/{id}/save                     → validate + replace the drawing
POST   /integrations/{id}/publish                  → freeze it as a version
POST   /integrations/{id}/unpublish                → withdraw it, and switch the flow off
GET    /integrations/{id}/versions                 → the history, without the drawings
GET    /integrations/{id}/triggers
POST   /integrations/{id}/triggers                 → create or update one, per node
DELETE /integrations/{id}/triggers/{trigger}
GET    /integrations/{id}/runs                     → the history page
POST   /integrations/{id}/runs                     → start a run (live or dry), return a handle
GET    /integrations/vocabulary                    → what the palette and the pickers offer

GET    /integrations/runs/{run}                    → one whole frame, for polling
GET    /integrations/runs/{run}/events             → the same frame as SSE, until it ends
GET    /integrations/runs/{run}/steps              → the full step log, paginated
GET    /integrations/runs/{run}/records            → the records that failed, with payloads
POST   /integrations/runs/{run}/stop               → stop at the next record boundary
POST   /integrations/runs/{run}/replay             → the same version again, as a new run

GET    /integrations/connections/                  → the connections library
POST   /integrations/connections/create
GET    /integrations/connections/{id}/edit-form
POST   /integrations/connections/{id}/update
POST   /integrations/connections/{id}/active
POST   /integrations/connections/{id}/revoke       → delete the credential, keep the row
POST   /integrations/connections/{id}/delete
POST   /integrations/connections/{id}/test         → one live call, reporting what came back
POST   /integrations/connections/{id}/private-hosts
GET    /integrations/connections/{id}/operations
POST   /integrations/connections/{id}/operations   → create or replace a REST operation
DELETE /integrations/connections/{id}/operations/{operation}
GET    /integrations/connections/{id}/schema       → one operation's fields, for the mapper

POST   /integrations/ai/generate                   → a sentence in, a checked draft out
POST   /integrations/ai/save-draft                 → store a draft somebody accepted
```

**Reconciled.** Three routes in the pre-build sketch of this list do not exist, and each
absence is a decision rather than a slip. `/{id}/graph` is gone because the drawing is handed
to the canvas as JSON *in the page*, so it paints on first load rather than flashing empty; a
second endpoint serving the same document would be a second thing to keep in step. `/{id}/edit`
is `/{id}/canvas`, which says what it opens. `/runs/{run}/cancel` is `/runs/{run}/stop`,
matching the button and matching the contract — it is a request, not an instruction. The OAuth
and webhook routes belong to Phases 2 and 3 and are not registered.

Fifteen tables. Four are the drawing and its executions (`integration_flows`,
`integration_flow_versions`, `integration_runs`, `integration_run_steps`); one is the
record-level audit (`integration_run_records`); two are the machinery that makes a schedule
real (`integration_triggers`, `integration_run_jobs`); the rest hold connections, credentials,
user-authored operations and the small amounts of state a vendor forces us to keep. Three —
`integration_cursors`, `integration_oauth_states`, `integration_rate_counters` — are created
and not yet written to, because one migration is cheaper than two and their columns were
designed alongside the rest.

---

# Why it exists

Everything this application has built so far *reads*. [DEEP_AGENTS.md](DEEP_AGENTS.md) runs a
data agent's tool configs against the user's own database and hands the rows to a model.
[GRAPH_DESIGNER.md](GRAPH_DESIGNER.md) composes those queries into a drawn LangGraph. Both
state the same limit out loud: **no writes to the user's data, and no non-relational
datasources.**

That is the right boundary for a query tool and the wrong one for a business. A customer's
orders are in Shopify, their contacts are in GoHighLevel, and their invoices are in SAP; the
question they actually have is not "how many orders" but "why is this order not in the ERP".
Answering it means moving records, which means writing into somebody else's system, on a
schedule, with a record of what happened to each one.

So this module is the first thing here that makes an outbound call as its *purpose* rather
than as a side effect, and the first that can be wrong in a way the customer's other software
has to live with. Nearly every decision below follows from that one difference.

---

# The four decisions

**A standalone engine, not more Graph Designer nodes.** Graph Designer's invariant is that it
does not write to the user's data. Adding a `connector_write` node to `_RUNNERS` would delete
that invariant for every existing graph, and the run semantics an integration needs — a
pinned topology, retries, per-node timeouts, a queue, a scheduler — are ones a query canvas
was deliberately built without. What *is* shared is [static/js/graph_canvas.js](../static/js/graph_canvas.js),
242 stateless lines already serving two canvases, and every design decision worth copying,
copied on purpose and cited where it appears.

**Deterministic at run time, AI at the edges.** The same input produces the same API calls, in
the same order, so a run can be audited and replayed. The language model authors workflows,
proposes field mappings and explains failures — it does not choose steps while records are
moving. §"Always act as Agent AI" says why that phrasing and this design are compatible, and
where they genuinely are not.

**Generic REST first, then the named vendors.** A configurable REST connector is the one every
other connector is a special case of. Building Shopify first would produce two request
builders, two pagination implementations and two retry paths, and the user-facing one would be
the one that rots.

**Phased, with the prerequisites first.** Three defects in existing code block any credential
storage, and they are the opening section rather than a footnote.

---

# Before any of this ships: three defects — Phase 0, done

These were real defects in existing code, not preparatory refactors, and no credential could
be stored until they were fixed. All three have shipped and are described below in the past
tense; the code they name is real and readable now.

## The Fernet key was hardcoded and committed

[app/utils/crypto.py](../app/utils/crypto.py) read `FERNET_KEY` into `SECRET_KEY` and then
never used it, constructing `Fernet(...)` from a literal that is in this repository's git
history. It never called `load_dotenv()`, so even the discarded read depended on whoever
imported first.

Three columns were already encrypted with it — `datasources.password_encrypted`,
`ai_api_keys.api_key_encrypted`, `chatbot_actions.headers_encrypted` — and
[tests/conftest.py](../tests/conftest.py) set `FERNET_KEY` to the same literal, which is why
no test ever noticed.

That is survivable for a database password on a single-tenant install. It is not survivable
for an OAuth refresh token that grants standing write access to a merchant's storefront. So
`crypto.py` now **fails at import when `FERNET_KEY` is unset** — the way
[app/db/auth/auth.py](../app/db/auth/auth.py) already refuses to import without
`JWT_SECRET_KEY` — and uses `MultiFernet` over `FERNET_KEY` plus a comma-separated
`FERNET_KEY_OLD`, so a key change is a background re-encryption instead of a data-loss event.
`encrypt_secret`/`decrypt_secret` are aliases for the same functions, because this module
stores tokens rather than passwords and calling them "password" at a dozen call sites would
be a small lie repeated. `rotate()` and `is_readable()` exist for the re-encryption pass.

**The migration was deterministic precisely because the bug was total.** Because the old code
used the literal regardless of the environment, every ciphertext in every deployment is under
that one key. There is no deployment whose data is under something else, so the re-encryption
is one decrypt-with-literal / encrypt-with-env pass with no per-deployment branching:
[`c4b19e7a5f83`](../alembic/versions/c4b19e7a5f83_reencrypt_secrets_under_env_fernet_key.py).
Its `downgrade()` raises — reverting every secret in the database to a published key is a
security regression wearing a rollback's clothes.

**The key has not actually been changed yet, and that matters.** `FERNET_KEY` is currently set
to the legacy literal, which keeps existing data readable and makes the migration a verified
no-op. It means the deployment is no more exposed than it was, but the leaked key is still the
live key. Rotating away from it is an outstanding task with a written procedure:
[SECRETS_AND_KEY_ROTATION.md](SECRETS_AND_KEY_ROTATION.md). **Do that before the first OAuth
token is stored**, because that is the credential class the whole argument above is about.

## The egress guard was trapped inside the chatbot

`chatbot_action_service._validate_outbound_url_shape` and `_assert_public_host` were the only
SSRF defence in the application, and they were private functions in a 1053-line chatbot
module. They are now [app/utils/outbound_http.py](../app/utils/outbound_http.py) — shared
rules belong in `utils/`, never in a sibling feature — taking an explicit `EgressPolicy` so
SAP's on-premise reality has somewhere to be declared rather than somewhere to be bypassed.

Promoting them also let two things be added that the chatbot's version did not have, both of
which this module needs: a `_NEVER_ALLOWED` set checked **after** the allow-list, so no
configuration can open the cloud instance-metadata endpoint; and `same_origin()`, for the
paginated read that follows a `next` URL chosen by the server being read. The chatbot's own
call sites are untouched — the service keeps module-level wrappers that translate
`EgressError` into its existing `HTTPException` wording, so every message a chatbot user can
see is byte-identical to before.

## Type coercion was trapped with it

`_coerce_param` handled `string`/`number`/`boolean` for values a language model supplied.
Field mapping needs the same job over `date`, `datetime`, `integer` and `json`, so it is now
[app/utils/type_coercion.py](../app/utils/type_coercion.py) and the chatbot's version is a
wrapper keeping its exact `ValueError` sentences — including "the AI supplied", which is true
there and would be a lie in a field mapping.

The property the general version holds, stated as the failure it prevents: **nothing is
guessed.** `"abc"` for a number field is a refusal, not `0`; `10.5` for an integer is a
refusal, not `10`; `True` for a number is a refusal rather than a quantity of one, even though
`float(True)` is `1.0`. A record written into somebody's CRM with a silently-zeroed amount is
a wrong record with nothing in the log to find it by, which is strictly worse than a record
that failed. `None` passes through at every type, because required-ness is a separate rule
that runs first and conflating them reports an unsent optional field as a type error.

---

# Module layout

```
app/services/integrations/
  errors.py            NodeFailure, IntegrationFailure, RunCancelled
  engine/              the run: state, rules, compiler, runners, queue, scheduler
  connectors/          spec.py, registry.py, rest_generic/, shopify/, gohighlevel/, sap_odata/
  runtime/             the HTTP layer: pooling, request building, rate limits, retries, paging
  credentials/         encryption per column, token refresh
  mapping/             paths, field maps, record validation, dedupe
  nodes/               the connector-backed node runners
  ai/                  catalogue, prompts, workflow generation, mapping, triage
  flow_service.py      flow CRUD, publish → version, trigger CRUD
  connection_service.py
```

`services/integrations/__init__.py` stays empty and call sites import by full path, as
everywhere else. The sub-packages follow `downloader_agents/base/`'s precedent for a large
service folder with distinct areas inside it.

**`flow_rules.py` is split out of `flow_service.py` from the first commit.**
[app/services/graph_designer/graph_service.py](../app/services/graph_designer/graph_service.py)
is 2079 lines and gets imported by the compiler, the runners *and* the routes purely because
its validator shares a file with its CRUD. The validator has three importers and the CRUD has
one; that is a seam, and it is cheaper to take it now than to find it at 2000 lines.

**Exactly one module imports langgraph** — `engine/flow_compiler.py`, the rule
`graph_compiler.py`, `tool_chain_graph.py` and `download_graph.py` each follow. Everything
else in the engine is importable and testable without it, which is what keeps
`pytest.importorskip("langgraph", …)` down to two test modules.

---

# The data model

Every table carries the mandated pair: `id BigInteger` for the primary key and every foreign
key, `uuid` for everything a browser ever sees.

## The drawing, and the frozen copy of it

`integration_flows` is the editable head — `graph_data` JSONB replaced wholesale on save,
`is_active`, `default_batch_size`, `redacted_fields`, and a functional unique index on
`(user_id, lower(name))` that must be hand-written in the migration because autogenerate
cannot see it and will propose it again forever.

`integration_flow_versions` is the part Graph Designer does not have, and the reason it exists
is worth stating plainly. `graph_run_service._resume` recompiles from `graph.graph_data` —
the *live* drawing — so editing a graph while one of its runs is paused resumes a different
topology than the one that started. For a query tool that is a curiosity. For something that
writes into a CRM at 3am on a schedule, an audit trail whose drawing can change underneath it
is not an audit trail, and a replay that recompiles the current drawing is not a replay.

So publishing freezes a snapshot with a `graph_hash`, a run points at
`flow_version_id`, and a replay recompiles that same frozen JSON. One published version per
flow, enforced by a partial unique index **and** in `flow_service.publish_flow` — see the note
on SQLite in §Open risks for why the index alone is not enough.

## The run, and the three grains of "what happened"

`integration_runs` holds the pinned version, the trigger that fired it, `mode` (`live` or
`dry_run`), `thread_id`, four exact counters (`records_read/written/failed/skipped`), a
durable `cancel_requested`, `replay_of_run_id`, and `scheduled_for` — *the slot the run is
for*, which is not when it started.

`idempotency_key` has a partial unique index, and **the insert is the dedupe**. A schedule's
key is `{trigger_uuid}:{scheduled_for}` and a webhook's is the vendor's event id, so two
replicas ticking in the same second cannot produce two runs even if `SKIP LOCKED` somehow
did not stop them.

`integration_run_steps` copies `ToolGraphRunStep` — including its denormalised `node_type` and
`node_label`, because a log that changes when somebody edits the drawing is a log nobody can
trust — and adds `attempt`, `batch_index`, `records_in`, `records_out`.

`integration_run_records` is the grain neither existing engine has. One row per record that
*did not* simply work: `outcome` is `failed`, `invalid`, `skipped` or `sample`, and for a
failure the `payload` is the **whole record**, not a preview, because replay-failed-records
reads it back.

**Successes are counted, never logged.** Fifty thousand rows saying "fine" is not an audit
trail, it is a table nobody can query. Failures are capped too — 1000 per run, 20 samples per
node — after which `records_log_truncated` flips and the counters keep counting. The run view
returns `records_failed` (exact) *and* `failed_logged` (how many rows exist) as two separate
numbers, because a capped log reported as a total is the same lie as a 20-row sample reported
as a result set.

## The queue and the schedule

`integration_run_jobs` is `download_jobs`' shape plus `available_at` and `priority`.
`integration_triggers` holds `next_run_at`, indexed.

Graph Designer has neither, and its docstring gives the reason: a run there is "watched live
by the person who pressed the button, so there is nothing to gain by making them queue." A
3am sync has nobody watching, several schedules come due in the same second, and a restart
mid-run must not lose the work. That is the same argument `job_queue.py` already makes for
exports, so the same answer applies: **a table used as a queue, drained by an in-process
asyncio worker.** No Redis, no Celery, no arq — this module does not add one either.

**`next_run_at` is the whole point of the trigger table.** Nothing about a schedule lives in
memory. A schedule held in an `asyncio.sleep` dies with the process and comes back wrong; a
column comes back right, and a test proves it by pointing a *freshly constructed* scheduler at
a database with a due row.

## Connections, and why credentials are a separate table

`integration_connections` holds the user's configured instance — `connector_id`, `label`,
`external_account_id` (a shop domain, a GHL location id: an identity, stored plaintext),
`parent_connection_id` for GHL's agency→location fan-out, and `status`.

Unlike `ai_api_keys`, which enforces one active key per `(user, provider)` in the service,
**many connections per connector is the point**: three Shopify stores, forty GHL locations.
What is unique is `(user_id, connector_id, external_account_id)` — the account, not the
connector — so reconnecting the same shop updates rather than duplicates.

`integration_credentials` is a separate table with a unique FK, every secret in its own
`*_encrypted` column, following the convention `datasources` already sets by keeping host,
port and username plaintext and encrypting only the password. Three reasons for the split:
a token refresh writes a row the UI never touches, so it cannot contend with a rename;
`build_connection_views()` selects from the connection alone and therefore *cannot*
accidentally serialise a secret; and a revoke is one `DELETE` that provably leaves nothing
behind, where nulling columns leaves a row indistinguishable in a backup diff from one that
never had a token.

`integration_oauth_states` stores `sha256(state)` and never the state value, so reading the
database does not let you complete somebody's install.

---

# The node vocabulary

Ports reuse Graph Designer's names where the meaning is identical — `default`, `error`,
`body`, `done`, `else` — and add `valid`/`invalid` and `kept`/`dropped`.

| type | ports out | holds |
|---|---|---|
| `trigger` | `default` | how the flow starts: manual, a schedule, or a webhook |
| `connector_read` | `default`, `error` | a connection, an operation, its arguments, a page size |
| `connector_write` | `default`, `error` | a connection, an operation, a write mode, an idempotency template |
| `transform` | `default`, `error` | field mappings through a fixed function table |
| `validate` | `valid`, `invalid`, `error` | required/type/pattern/enum/range rules |
| `filter` | `kept`, `dropped`, `error` | `filter_algebra` specs |
| `branch` | one per condition, plus `else` | an ordered list of comparisons |
| `batch` | `body`, `done` | which node's records to walk, and how many at a time |
| `success` / `failure` | — | a message; ends the run |

Later: `join`, `aggregate`, `error_handler`, `delay`, `approval`, and `agent`.

**The vocabulary lives in one place**, the same rule Graph Designer states: `flow_rules` owns
it, the routes serve it at `/integrations/vocabulary`, and the canvas builds its palette and
its property forms from what it was sent. This module extends the rule to **ports** — the
canvas does not carry a hardcoded `PORTS` table the way `graph_designer.js` does, because a
second list of ports in JavaScript is a second list that can drift from the validator.

**There is no `http_request` node.** Generic REST is a connector family. Two ways to call an
HTTP endpoint is two places to get authentication wrong, and one of them would be the one
without a rate limiter.

**Reconciled — a connector node names its connection in `data.connection_uuid`.** Worth stating
because the validator and the runner briefly disagreed about it: `flow_rules` required
`connection_id` while `connector_nodes.resolve_target` read `connection_uuid`, so a workflow
could save green and fail on its first record, or save red while being perfectly runnable. Both
sides had passing tests against their own spelling; what caught it was a test that drove save
and publish through the same drawing.

`connection_uuid` won, for the reason CLAUDE.md gives: a field called `connection_id` invites
somebody to put the internal bigint in it, and the bigint never reaches a payload. The validator
now also *parses* the value rather than merely requiring it — the shape a hallucination takes is
a plausible word, and a model writing `"shopify-prod"` where an identifier belongs is caught
while the canvas is still open instead of at 3am in a log. The regression test asserts against
`connector_nodes`' own reader rather than against a string, so renaming one without the other
fails.

## `dry_run` instead of running one node

Graph Designer lets you run a selection — one node, or a group — which is exactly right for
debugging a query. Here it would mean running the write node against a live CRM with no
upstream data, which writes garbage into somebody's production system while looking like a
test.

So the equivalent affordance is a run **mode**. In `dry_run`, every `connector_write`
resolves its connection, builds its payload, validates it, records what it *would* have sent
as a `sample` record row — and calls nothing. Same diagnostic value, no blast radius.

---

# The engine

## Records never travel in state

LangGraph serialises the whole state to the checkpointer on every super-step. Graph Designer's
`_run_sql` puts every row it read into `outputs`, and its own docstring concedes that "an
unfiltered select over a large table is a large checkpoint."

At fifty thousand records that is not a large checkpoint, it is a run that dies. So a
`connector_read` writes a **handle** — `{"kind": "recordset", "key": …, "count": …}` — and the
records themselves live in a run-scoped process registry, `record_buffer.py`, built on the
pattern `agent_recursive_dataframes/frame_buffer.py` already uses to pass frames between
graph nodes by key. State stays proportional to the number of nodes and does not grow with the
data.

The test that keeps it that way asserts `len(json.dumps(final_state)) < 32_768` after a 50k
run. Anyone who reintroduces rows-in-state fails it immediately, rather than in production
six months later.

## The unit of a loop pass is a batch, not a record

Fifty thousand records through a per-record loop is fifty thousand super-steps, fifty thousand
checkpoint writes and fifty thousand step rows. At `batch_size = 500` it is a hundred passes.

Everything expensive lives inside one batch: the concurrency, the retries, the per-node
timeout, the record-log flush. `MAX_BATCH_SIZE = 5000` is enforced in **validation**, not
merely defaulted, because the buffer is process memory and an operator can type 50000.

## `counts` accumulates, and this is the most important reducer in the module

`outputs`, `batches` and `errors` use the merge reducer Graph Designer's `graph_state._merge`
established — without it a node with two upstreams sees only one of them.

`counts` cannot use it. It is `{node_id: {"read": n, "written": n, …}}` and it must **sum**
across passes. With a last-wins merge, batch 100 writing `{"written": 500}` replaces batch
99's, and a run that moved fifty thousand records reports five hundred. That is the same class
of quietly-wrong number that `preview_of`'s separate `count` field exists to prevent, one
level up and considerably more expensive.

The consequence is a contract every runner keeps: **a runner returns deltas, never totals.**
It is stated in `run_node`'s docstring and asserted directly in `test_flow_state.py`.

## Previews are redacted before they are written

The caps are Graph Designer's — 20 rows, 500 characters, 50 keys — applied at write time so
they are a property of the table rather than of one renderer.

The addition is `redact()`, running *inside* `preview_of`, stripping keys matching
`authorization|token|password|secret|api[_-]?key|client[_-]?secret|card|cvv` plus whatever the
flow lists in `redacted_fields`. Graph Designer previews rows from the operator's own
database. This previews a webhook body and a third-party API response, either of which can
contain a bearer token — and those previews are later handed to a language model for triage.
It is a security control, not tidiness, which is why it lives at the point of writing.

**Reconciled — a deny-list over key names cannot see a secret in prose, and there is a second
scrub for that.** `redact()` is the right tool for a body carrying a credential in a *field*.
It is useless against `{"error": "invalid key sk-live-…"}`, where the key is `error` and the
secret is in the value — and a surprising number of APIs answer exactly that, or quote back the
query string they were handed. This was found by a test that searched the whole rendered
connection-test result for the plaintext key rather than checking that no *named* field held
it; the searching form is what caught it and the naming form would not have.

So `sender.scrubbed` removes the credential **by value**, in the one layer that knows what the
credential is, from every message `send()` produces. It handles both the rendered header and
the bare token inside it, because `Bearer sk-1` is what goes out and `sk-1` is what comes back.
Below eight characters it leaves the message alone: a short value matches ordinary words and
would turn a useful error into a row of asterisks, and anything that short is not a secret
worth the message.

## Routing precedence

Every non-terminal node gets `add_conditional_edges`, uniformly — Graph Designer's decision,
for its reason: a node that gains an error path must not change edge *kind*. The recursion
limit is computed from the work (`nodes × Σ max_batches + 100`), never left at LangGraph's
default of 25.

The router decides in this exact order:

1. **`cancelled` → `END`.** Above everything, including the error channels, so a cancelled run
   cannot take an error edge into a notification node and do more work on its way out. Graph
   Designer has no cancellation channel in state at all.
2. Handled failure — `errors[node_id]` is set → the drawn `error` edge.
3. Unhandled failure — `failed_at` → `END`.
4. `validate` → `valid` / `invalid`.
5. `filter` → `kept` / `dropped`.
6. `branch` → the first matching condition, else `else`.
7. `batch` → `body` while the cursor is live, `done` when exhausted.
8. Otherwise the `default` edge, else `END`.

Rules 4–7 each call the same function the runner called, so the log and the route cannot
disagree about what happened. Rule 3 sits above them for Graph Designer's reason: a node that
failed produced no output for a branch to read.

## Three failure levels, never conflated

| level | signal | what it does to the run |
|---|---|---|
| a record failed | `counts.failed` and a `run_records` row | nothing, unless the node says `on_record_error="fail"` |
| a node failed | `errors[id]`, or `failed_at` | the drawn error edge, or the run ends |
| the run failed | `integration_runs.status` | terminal |

Graph Designer has the last two, because it has no records. Collapsing the first into the
second is how "3 of 50,000 records had a malformed email address" becomes "the sync failed",
which is both useless and wrong.

**A run with any skipped or invalid record ends `partial`, never `success`.** This is
`downloader_agents/base/retry.py`'s argument about part files, transposed: an export that
silently contains some of the data is the one outcome worse than no export, because nothing
about the file says so. A green run that dropped eighty-eight records is exactly that failure.

## Retries, and the one that would duplicate a merchant's orders

`engine/retry.py` mirrors `downloader_agents/base/retry.py`'s shape — 0.5s doubling,
`asyncio.CancelledError` re-raised immediately and never swallowed, permanent failures not
retried, an explicit exhaustion exception carrying the last error — but does not import it.
That module's permanent-failure escape is hard-wired to `ToolQueryError` from the deep-agents
query executor, and its `on_discard` exists to delete part files; neither concept belongs
here, and importing it would drag the agent's failure vocabulary into the connector layer.

Two things are added:

**Jitter.** Four chunks running in parallel that all hit a 429 will otherwise retry in
lockstep and hit it again. A synchronised retry storm from our own fan-out is self-inflicted.

**`Retry-After` is honoured**, clamped at 120 seconds. A vendor telling you how long to wait
and being ignored is how a rate limit becomes a ban. A vendor asking for an hour is a
permanent failure with a message saying so, not a sleeping worker.

And then the rule that matters most:

> A write is retried **only** on failures that provably never reached the server —
> `ConnectError` and `ConnectTimeout` — unless the operation declares `idempotent: true` or
> supplies an idempotency header. **A `ReadTimeout` on a non-idempotent write is a permanent
> failure**, and its message tells the operator to check the target before re-running.

Shopify's `POST /orders.json` has no idempotency header. Retrying a timed-out create silently
duplicates orders in a merchant's store, and no amount of backoff makes it not happen. A
failed run somebody has to look at is a far better outcome than a duplicate order nobody
notices.

The second layer is `integration_sync_keys`: before a create, look up the record's natural key
for that connection and operation; if it is present, switch to the update operation. That is
the dedupe/upsert helper this repository does not otherwise have.

## Per-node timeouts

Graph Designer has none. A hung node hangs the run forever and the only evidence is a step row
stuck at `running`. Every dispatch in `run_node` is wrapped in `asyncio.wait_for` — 300
seconds by default, overridable per node up to an hour — and a timeout becomes a
`NodeFailure`, so it takes the drawn error path like every other failure rather than being a
special case the author cannot handle.

## Cancellation is two mechanisms because one is not enough

Graph Designer cancels by calling `task.cancel()` on an in-process registry. That does nothing
when the run is being driven by another replica's queue worker.

So there are two. The **durable** one is `integration_runs.cancel_requested`, written by the
request and checked at the top of every node and between chunks inside a write. Correct
everywhere, slow. The **fast** one is the local `_RUNNING` task dict and `task.cancel()`,
exactly as `graph_run_service` does it, plus `stop_all_runs()` on shutdown. Instant, local
only.

The row is marked **before** the task is cancelled — Graph Designer's ordering, for its
reason: otherwise the teardown races the write and the dock shows a run that stopped for no
stated reason.

The contract, stated in the UI because it is a promise to a person: **cancel stops the run at
the next record boundary, not mid-request.**

## A crash is not resumed, it is replayed

A job whose heartbeat goes stale is requeued and its run is marked failed with "the worker
running this sync stopped responding", and the operator presses Replay.

That is `requeue_stale_jobs`' own decision — "starting again rather than resuming is
deliberate" — applied to a harder case. LangGraph checkpoints per super-step, so a node that
died mid-batch would re-run from its top anyway, and half-resuming a write into somebody's CRM
is worse than a clear failure with a button next to it. Automatic resume is a later phase, and
it is gated on every write node in the flow having an idempotency template.

## Watching it

`run_store.py` follows `graph_designer/run_store.py`: `open_session()` as the seam, a short
session per write (a node's work is an HTTP call to someone else's server, and holding a pool
slot across it is waste), and a logging failure that is swallowed and logged because a failure
to log must not fail the node.

`bump_counts()` is a bare `UPDATE … SET x = x + :n`, never a read-modify-write — two nodes add
at once while the poll loop reads the row.

**Step rows collapse.** A hundred batches across six nodes is six hundred rows, which is fine.
Ten thousand batches is not. After 500 passes for one `(run_id, node_id)`, `finish_step` stops
inserting and updates a single rollup row instead — "batches 501–3,204 rolled up: 1,602,000
records".

The SSE contract is Graph Designer's: `ServerSentEvent`, the frame named after the run status,
ownership resolved *before* the generator is handed over (a stream's status is committed with
its first byte), polling on the same shape at the same URL, and the three `EventSource` rules
that have each bitten this codebase already — `close()` before anything that can throw, a
`finished` flag because every close arrives as an `error` with no data, and a server `error`
*with* data being a sentence for the operator. Named events must be registered by name; one
that is missing from `FRAME_EVENTS` is a state the dock silently ignores.

**One deviation.** Graph Designer's frame carries every step row; at six hundred steps that is
a large payload every second. Here the frame carries the run, all four counters and a per-node
rollup — whole state, never a delta, so a client that missed a frame is not holding a wrong
total — while the step list is paginated at `/runs/{id}/steps?after=`. The numbers are what a
missed frame would corrupt. The append-only list is not.

**`run_service.watch_run` polls the database, not this process's task table.** Graph Designer
can read its own `_RUNNING` dict because it starts every run in the process that watches it.
Here a run is claimed by whichever worker is free — that is the entire point of the queue — so
a stream reading the local table would show nothing at all for the commonest case in a
multi-worker deployment. One small query a second against a page somebody has deliberately
opened to watch something happen is the right trade; a session held open across the stream is
not, because it would keep serving the rows it first read and the dock would sit still while
the run moved.

---

# What this does differently from Graph Designer, and why

| Graph Designer | Here | Because |
|---|---|---|
| rows in `outputs` | handles into a buffer | 50k rows checkpointed per super-step |
| runs the live drawing | runs a pinned version | an audit trail whose drawing can change is not one |
| no queue | a queue | a 3am sync has nobody watching, and a restart must not lose it |
| no scheduler | a scheduler | "on a schedule" is the product |
| no retries | two levels, jittered, `Retry-After`-aware | networks |
| no per-node timeout | 300s, overridable | a hung HTTP call is not a hung query |
| no cancellation in state | `cancelled` short-circuits the router | cancel must not do more work on the way out |
| run a selection of nodes | `dry_run` | running one node against a live CRM writes garbage |
| two failure channels | three | records exist |
| `PORTS` hardcoded in JS | ports served from the server | a second list is a list that can drift |

---

# Connectors

## An operation is data, not a function

A connector is a code-side `ConnectorSpec`; an operation within it is a frozen
`OperationSpec` describing a method, a path, its parameters, how the response paginates and
where the records are in it. Generic REST stores its operations as `integration_rest_operations`
rows whose columns *are* those fields, and `load_operation()` returns the same frozen
dataclass either way. **The runtime has one code path.**

Three reasons, in order of weight.

**Determinism is a property of data.** Every run step records
`operation_hash = sha256(canonical_json(op))`. A replay that produces a different hash is
*detectably* not the same run, and the audit says why. If an operation were a Python function,
the only thing recordable is a module path and a git SHA, and a hotfix to that function
silently changes what "replay" means with nothing in the record noticing.

**The user authors REST operations in a form anyway.** If Shopify were Python and generic REST
were data, there would be two request builders, two pagination implementations and two retry
paths — and the well-exercised one would be the vendor path.

**`build_request(op, args, ctx) -> PreparedRequest` becomes pure.** No HTTP, no database, no
credential. Every URL-injection, missing-argument and escaping bug lives in that function, and
it becomes a table-driven unit test.

What data cannot express — Shopify's GraphQL cost accounting, SAP's `X-CSRF-Token: Fetch`
handshake, GHL's company→location exchange — gets a small named `ConnectorHooks` protocol.
**Its contract is enforced by assertion, not convention**, and in both directions. On the way
out, `send()` re-checks `(method, host, path)` and the body against the pre-hook request and
raises if a hook changed any of them. On the way back, `after_response` may only *raise* — its
return value is discarded. Together that is what keeps the recorded operation an honest
description of the request that actually went out and the response that actually came back.

> **Reconciled with what was built (Shopify, this phase).** Three of these seams had never
> executed once, because `rest_generic` is the one connector that uses none of them, and two
> were broken: `ResolvedTarget.base_url` read a `ConnectorSpec.base_url` that does not exist,
> and `base_url_template` had no substitution site anywhere. Both are fixed via
> `ConnectorSpec.render_base_url`. `after_response` is new — see the pagination and
> [SHOPIFY_CONNECTOR.md](SHOPIFY_CONNECTOR.md) notes below. `throttle_from_response` was
> **removed** from the protocol: it had zero implementations and zero callers, and its job
> belongs in `after_response`. `classify_error` remains declared and still unwired.

## The HTTP runtime

**One pooled client per connector or host**, following `ollama_client._get_client()`, with
`follow_redirects=False` (a 301 to `169.254.169.254` after a clean IP check is the classic
bypass) and `trust_env=False`. `chatbot_action_service` builds a client per call, which is
correct for one webhook and wrong for a forty-page sync: that is forty TLS handshakes,
several seconds of pure setup, and enough source-port churn for a merchant's WAF to notice.

**Rate limiting is a leaky bucket seeded from the spec and corrected from the response.**
Shopify returns `X-Shopify-Shop-Api-Call-Limit: 32/40`; GraphQL returns a `throttleStatus`
object. This is not an optimisation. **The bucket is per shop and shared with every other app
the merchant has installed** — a locally computed bucket that ignores the header will send
into a bucket someone else already drained, collect a wall of 429s, and risk being throttled
at the platform level.

GHL has two limits, and the second one is the dangerous one: a burst allowance, and **200,000
requests per day per location**. That counter is persisted in `integration_rate_counters` with
an atomic upsert, and requests are refused locally at 95% of the cap. An in-memory daily
counter resets on every deploy, and a marketplace app that blows its daily cap gets suspended.
This is the single most account-endangering item in the module.

**Pagination is declarative**, seven kinds, with three rules that are each somebody's
afternoon:

* `link_header` (Shopify REST) — **use the `rel="next"` URL verbatim.** Rebuilding it from
  parsed parameters drops `page_info`, and Shopify returns a 400 if `page_info` is combined
  with filters. This is the most common Shopify paging bug and the rule belongs in a comment
  at the call site.
* `next_url` (SAP's `@odata.nextLink`) — an absolute URL from the response body, so it is
  **re-validated through the egress guard and asserted same-origin as page 1**. A next-URL the
  vendor controls is otherwise a clean SSRF vector.
* `input_cursor` (Shopify Admin GraphQL) — the cursor is handed back as one of the
  operation's **declared inputs** rather than written into the query string, so the
  operation's own templates decide where it lands. `PageWalk` keeps `params` and `arguments`
  apart and a kind writes to exactly one.

Every kind stops on three conditions: `max_pages`, `max_records`, and **a repeated cursor**.
Without the last one, a malformed vendor response is an infinite loop that burns the rate
limit while nobody is watching. `input_cursor` shares the *same* `seen_cursors` set as the
query-string kind, deliberately: a vendor handing out one token forever does not care which
carrier it travelled in, and two guards would be two places to forget.

> **Reconciled (Shopify).** `input_cursor` is the seventh kind and it was added because
> `cursor` cannot reach a POST body's `variables` — and `before_request` cannot patch that
> either, since the hook fence refuses any change to `json_body`. Two more general
> capabilities landed with it: `OperationSpec.body_literals`, which lets a brace-heavy
> literal such as a GraphQL document through the substituter untouched while its siblings
> still substitute, and `ConnectorHooks.after_response`, without which a GraphQL error — an
> HTTP **200** carrying an `errors` array — read as an empty page and ended the run green.
> All three are general; none mentions Shopify. See
> [SHOPIFY_CONNECTOR.md](SHOPIFY_CONNECTOR.md).

**Responses raise rather than truncate.** `chatbot_action_service` caps at 256 KB and shows
the model what it got, which is right when the payload is prose. Here a truncated JSON page is
invalid JSON at best and a silently short record list at worst, and moving records is the
entire job. A 2xx with `text/html` is likewise a permanent failure naming the content type —
that is what a WAF challenge, a captive portal and an expired-session redirect all look like,
and parsing one as an empty record list reports "0 records synced" as a success.

## Mapping and validation

`mapping/paths.py` reads `a.b.c`, `a[0]` and `a[*].b`. No filters, no expressions, no
recursive descent, nothing to evaluate — the same posture `node_runners._condition_holds`
takes for conditions. Full JSONPath implementations disagree with each other and several
evaluate expressions; neither is acceptable on user-authored input.

Transforms are a **fixed named table**, not expressions. Applying one is `TRANSFORMS[name](v)`
and an unknown name is a save-time refusal naming the ones that exist.

`filter_algebra` is **imported as-is** from `agent_recursive_dataframes` — the operator
vocabulary, `needs_values`, `unsupported_operator`, `wrong_arity`. It is deliberately
polars-free and database-free, its own docstring says so, and one operator table for the whole
application means a user who has met one has met both. `frame_ops._require_column`'s message
shape is **copied, not imported**: same sentence, different exception, because importing it
would drag polars and a deep-agents error type into the connector layer.

A validation failure never coerces past itself. `"abc"` for a number field is not `0`.

---

# Credentials

## Refreshing a token without two runs racing

`ensure_fresh_token` refreshes 120 seconds before expiry and claims the right to do it with a
**compare-and-set `UPDATE`** on a lock column with a TTL, not a row lock. Losers poll for up to
thirty seconds and then re-check.

`claim_next_job` uses `FOR UPDATE SKIP LOCKED` and is right to, but this is a different job. A
refresh is an outbound HTTP call taking seconds, and a row lock holds an open transaction and
a pooled connection for that entire time across every concurrent node — which is how a burst
exhausts the pool. A TTL column also survives a refresher that crashed, which a transaction
lock only does by accident. And usefully, CAS works on SQLite, so the two-concurrent-refreshers
test runs in the existing suite.

**The ordering is load-bearing.** GoHighLevel rotates the refresh token on every use, and so do
Shopify's online tokens. If the exchange succeeds and the write fails, the stored refresh
token is already dead and the connection is locked out permanently. So: **exchange, write,
commit, and only then use the access token.** Never use-then-commit.

A `400 invalid_grant` is **never retried**. Retrying against a rotated-away refresh token is
precisely how a connection gets burned; it sets `needs_reauth` and raises permanently.

## The OAuth callback, in the order that matters

Shopify's `hmac` query parameter is verified first — its callback is itself signed and skipping
that is a documented app-review failure. Then `sha256(state)` is looked up, a consumed or
expired state is refused, and **the state is marked consumed in the same transaction as the
lookup, before the token exchange**. Then the state's user is cross-checked against the
session's, because a state minted by one user must not complete in another's. Only then is the
code exchanged, through the same `send()` runtime as everything else rather than a bare
`httpx.post`, so it gets the pool, the egress check and the retry classification.

## When a token expires mid-run

Three things, in this order: the node fails with a sentence naming the connection and telling
the operator to reconnect it; the connection goes to `needs_reauth` with a red badge and a
Reconnect button on the list, linked from the run page; and a `reauth_required` audit event
records the run it happened in.

A replay of that run may now succeed. That is correct: **the determinism contract is over
requests, not over the world.** The audit records the outcome — "credential refused at 14:02" —
not the token, so the two runs remain distinguishable.

## SSRF, and the door SAP needs

The egress guard keeps its existing rejection set — private, loopback, link-local, reserved,
multicast, unspecified — and gains an `EgressPolicy`. A private host is permitted only when
the resolved IP is inside an allow-listed CIDR **and** the `host:port` is allow-listed. Both,
not either.

Some things survive any allow-list, checked *after* it so ordering cannot bypass them:
loopback, `169.254.0.0/16` and `fe80::/10` (cloud instance metadata — `169.254.169.254` is why
`is_link_local` was in the original list), `0.0.0.0/8`, multicast, the IPv4-mapped forms of all
of those, and **the host resolved from `DATABASE_URL`**. A connector that can reach this
application's own Postgres is not an integration.

The door is gated three ways. Only an admin may open it, checked **in the service and not the
route**, because a business rule in a route is a rule a second route can skip. Only on a
connector whose spec sets `allows_private_hosts`, which is SAP and nothing else — so a generic
REST connection can never be aimed inside the network, which closes the "user builds a REST
operation pointed at the metadata service" path entirely. And at most ten explicit
`host:port` plus CIDR entries, no wildcards. Every change writes an audit event with the old
and the new list, and every request made through the door records the policy and the resolved
IP on its step row.

**The residual risk, stated rather than hidden:** this is check-then-connect, so DNS rebinding
is narrowed and not closed — unchanged from `_assert_public_host`'s own admission, and *worse*
for an allow-listed private target, because internal reachability has already been conceded.
Pinning the resolved IP and passing the hostname as an explicit `Host` and SNI value is the
fix, and it is worth doing for SAP and not for Shopify: it breaks against SNI-routed hosts
with strict certificate matching and interacts badly with hostname-keyed connection pooling.

---

# Triggers, scheduling and webhooks

There is no scheduler anywhere in this application and no `croniter` dependency. The only
recurring primitive is `download_service.run_expiry_reaper`'s `while True: sleep(interval)`,
registered as an asyncio task in `main.on_startup`, and the scheduler is built in that shape
with `job_queue.run_worker`'s error discipline: **every failure inside a tick is logged and
the loop continues.** A scheduler that exits because one flow was misconfigured takes the
whole feature down silently.

**The claim is one transaction.** Select due triggers `FOR UPDATE SKIP LOCKED`, then set
`last_fired_at`, compute and store the next `next_run_at`, insert the run row and insert the
queue job — and commit once. A crash between advancing the schedule and enqueueing the work
cannot happen because there is no "between". The `idempotency_key` unique index is the second
line of defence.

**Catch-up is off, and that is the only supported behaviour for now.** A trigger whose
`next_run_at` is an hour stale because the application was down fires **once** and jumps to
the next slot after now. Firing the twelve missed slots is twelve times the API quota for zero
new data, and for an incremental sync the single catch-up run reads everything the twelve
would have.

**An overlapping tick writes a row.** With the default `skip` policy, a schedule that comes due
while its previous run is still going inserts a run with status `skipped` and a message saying
why, rather than doing nothing. One row per skipped tick is the only way an operator ever
discovers that their five-minute sync takes seven minutes. Silently doing nothing hides
exactly the problem worth surfacing.

**A manual run goes through the same queue and the same code path** as a scheduled one, so the
run tested at 11am is the run that fires at 3am. An `asyncio.Event` wakes the worker
immediately so the canvas still feels responsive.

## Webhooks

`POST /public/integrations/webhooks/{endpoint_id:uuid}` is unauthenticated, registered beside
`PublicChatbotController`. **It carries no CORS middleware**, unlike its neighbour — vendors
are servers, not browsers, and CORS there would only widen the surface for a caller that does
not exist.

The handler reads the raw body **before any parsing**, because HMAC is computed over exactly
those bytes and a re-serialised body will not match; rejects anything over 1 MiB, because an
unbounded read on an unauthenticated route is a memory denial-of-service; verifies with
`hmac.compare_digest`; checks for a replay; writes the delivery; and returns 200.

**Nothing else. No workflow, no vendor call, under 100 milliseconds.** Shopify times out a
webhook at five seconds and removes the subscription after 19 consecutive failures over 48
hours. A slow handler does not degrade gracefully — it silently unsubscribes you, and the sync
stops with nothing in our logs explaining why.

The obvious guess about secrets is wrong in two of three cases. **Shopify's HMAC uses the
app's client secret from the environment, not a per-connection value.** GoHighLevel signs
RS256 against a public key, so there is no secret to store at all. Only generic REST has a
genuine per-endpoint secret.

Replay protection leans on the database: `(endpoint_id, vendor_event_id)` is unique and the
insert **relies on catching the integrity error** rather than selecting first, because
select-then-insert is racy exactly when concurrent redelivery makes it matter. A duplicate
returns **200** — a vendor that receives a 4xx for a redelivery retries harder.

---

# "Always act as Agent AI"

Stated plainly, because pretending otherwise would be worse: **an agent choosing each step at
run time and a deterministic, replayable runtime cannot both be true.** If the model decides,
the same input can produce different API calls on Tuesday than on Monday, and "why did this
customer get charged twice" becomes unanswerable. For a tool whose job is to write records
into somebody else's system, that is not flexibility, it is an audit trail that does not exist.

The honest version, and the one this module implements:

> The user is never more than one sentence away from an agent, and the agent is always the
> thing that **changes** the workflow — never the thing that runs it.

Four surfaces:

**Generation.** A sentence becomes a draft workflow on the canvas. It runs through
`ai_analytics_service.answer_structured` rather than the LangChain stack, because it is
single-shot, because it then works on the in-built local model — which `build_chat_model`
refuses outright via its tool-calling denylist — and because its tests run without LangChain
installed.

**Field mapping.** Two schemas in, a proposed mapping out, with confidence as three words
rather than a number (a model asked for a float returns 0.85 for everything). It degrades to
deterministic name matching, labelled as such and never as an AI suggestion, so the mapping
panel works with no AI configured at all.

**Failure triage.** Deterministic first: `build_failure_report` reads the run, the steps and
the failed records with no model involved and always works. **The first step with status
`failed` is the recorded cause, and that is a database fact.** The AI explanation is a second
panel below it, visually distinct, with an `unknown` option so a model faced with a 500 and an
empty body can say it does not know rather than inventing something plausible. **Nothing the
model says is ever written onto the run** — a guess sitting in the column where the recorded
cause belongs is worse than no guess at all. A proposed fix drives a *link*, never an action;
auto-repairing a failed sync is the shape of feature that quietly changes what a workflow does
at 3am.

**The agent node**, later, as the explicit escape hatch for a step where judgement is genuinely
required — classify this support email into one of three queues. Its contract is what makes it
safe: it is the only non-deterministic node and is marked as such on the canvas and in the run
log; its output is constrained to a closed set declared on the node and validated before
anything downstream sees it, so an answer outside the set is a failed step rather than a coin
flip; **it may not call connectors**; and its prompt and full response are recorded on the step
row. Everything with an external effect stays deterministic, so "the same input makes the same
API calls" remains true in the sense that matters — only which branch is taken varies.

## How generation is stopped from inventing things

Two layers, following `aggregate_planner`'s: Pydantic bounds the **shape**, and `validate_draft`
bounds the **meaning**.

The draft schema subclasses `RequestSchema` — an LLM's structured output is an untrusted
request, in the same class hierarchy as a browser form post — and carries `unsupported` and
`reason` so the model has words to decline with. Without them, a model asked to sync Stripe to
Xero with neither connector present will emit a node that is *correctly shaped* and points at
a URL it invented.

**The model never writes node ids, edge ids or positions.** It writes reference handles and the
validator assigns the rest. A model-chosen id that collides silently rewires the graph, and
this is `PlannedAggregation.alias`'s rule where the consequences are larger.

`validate_draft` then resolves every name against reality: connection names against the user's
actual rows — exact, then lowercase, then stop, with **no fuzzy matching**, because "Shopify
Prod" quietly becoming "Shopify EU" writes customers into the wrong store — and the model's
spelling is *replaced* with the real uuid. Operations against the connector's real operation
list. Every mapping's target field against the operation's real inputs, which is the
hallucination that matters most: `customer_email` where the operation expects `email` produces
a workflow that runs green and silently drops the address. Every source reference against what
exists earlier in the order.

Then it runs **the same `validate_flow` a hand-drawn workflow goes through**, which is what
makes a generated workflow exactly as trustworthy as a drawn one, and what makes a rule added
next year automatically bind the generator too.

One repair attempt, then refusal. This departs from `sql_assist._regrouped`, which degrades to
a warning attached to the flawed output, and the reason is that the artefacts differ: a flawed
SQL string is readable and the user fixes it, whereas rendering a canvas with a node pointed at
a connection that does not exist invites them to press Publish.

**The catalogue is built from real rows**, so a connector the user has no connection for is
simply absent — the single most effective anti-hallucination measure available. It is built
per call and never stored: the `_RULES_FINGERPRINT` machinery exists because a Deep Agent's
prompt is stored on a row and must track two independently-moving things, and none of that
applies here. A user who adds a Shopify connection and immediately asks for a Shopify workflow
must not meet a cached catalogue. Staleness here is not a risk to manage; it is a bug avoided
by not caching.

**A generated workflow is always a draft a human publishes.** The AI request schemas contain no
`is_active` field at all — a field that cannot be set cannot be set wrongly. Generating and
saving are **two requests**: `POST /ai/generate` returns a drawing and writes nothing, and
`POST /ai/save-draft` is a second press. That is what makes "a refused draft leaves zero rows
behind" a property rather than a hope, and it is what the tests assert — an implementation that
saved first and validated after would pass a test that only checked the refusal.

**Reconciled — the model draws no edges either.** The plan said the model writes reference
handles and the validator assigns ids and positions; building it added the wiring to that list.
The model emits an ordered list of steps and `_wire` computes the connections, because a
model-drawn batch whose body never returns is one batch of a hundred reported as a success —
and the drawing looks entirely reasonable, so nobody reviewing it has a reason to doubt it.
Computing the wiring makes that failure unrepresentable rather than merely unlikely.

**Reconciled — three step types a draft may not use.** `validate`, `branch` and `filter` are
absent from the draftable set. All three decide *which records go where*, and a generator
cannot make that decision from prose without guessing. `filter` is the subtle one and it was
the tests that forced it: the plan had the model's free-text condition recorded as a warning
for somebody to finish, but `validate_flow` refuses a filter with no conditions — because one
that lets every record through looks like it is working — so that draft could not have been
saved anyway. All three are added on the canvas, by somebody who can see where each side of the
split goes.

**Reconciled — the in-built model cannot do this task at the current context size.** Measured
against `qwen3:1.7b` with one connection and two operations: the JSON schema
`_json_only_instruction` appends is 971 tokens, the system prompt is 710, and the catalogue plus
the request are about 100 — against a 1536-token budget (`OLLAMA_NUM_CTX` 2048 minus
`OLLAMA_NUM_PREDICT` 512). The irreducible cost exceeds the budget before any catalogue exists,
so the local path needs `OLLAMA_NUM_CTX` raised to be usable at all; a smaller catalogue does
not fix it, and the constant that caps the catalogue for the local path says so in as many
words. The feature degrades correctly — a sentence in the panel, nothing saved, the canvas
untouched.

That live attempt is also the best evidence the validation layer works. The model produced a
draft containing three hallucinations — an operation id in the connection field and two forward
references to handles it never declared — and `validate_draft` caught all three, named the real
alternative for each, repaired once and then refused, with nothing written.

---

# The canvas

`static/js/integrations.js` reuses [static/js/graph_canvas.js](../static/js/graph_canvas.js)
unchanged and unforked — the Bezier geometry, the port anchoring, the id generator and the
escaping trio. It is 242 stateless lines already serving two canvases; a third is what it is
for. It must load first, because the module reads `window.GraphCanvas` at module scope. If the
mapping grid needs a new primitive, that primitive goes in `integrations.js`; only something a
*fourth* canvas would also want earns a place in the shared file.

Copied from `graph_designer.js`: the gesture model — drag-from-port *and* click-then-click,
with a 4px threshold so a click is not a tiny drag — the midpoint delete buttons, the endpoint
reattach handles, and the property-panel field builders.

New: the **mapping grid**, and a **counters strip** the dock repaints from every frame. The
interesting state of a fifty-thousand-record run is numbers moving, not a step list scrolling
past.

Every label on this canvas is a connector name, a field name out of somebody's CRM schema, or
a record value. `createElement` and `textContent` everywhere; the two places that assemble
markup as a string go through the shared escaping helpers.

---

# What is deliberately absent

Each of these is a decision, not an oversight.

* **No parallelism across nodes.** LangGraph's `Send` exists and the aggregate graph uses it,
  but fanning out across the drawing makes "the same input produces the same API calls in the
  same order" untrue. Concurrency lives *inside* a write node, bounded by a semaphore held on
  the run context so total in-flight requests are capped per run, and results are reassembled
  in input order. An operation may declare itself order-sensitive, which forces concurrency to
  one.
* **No partial runs.** `dry_run` instead — see §The node vocabulary.
* **No automatic resume after a crash.** Requeue and replay — see §The engine.
* **No cron expressions yet.** Interval schedules only, which avoids adding a dependency this
  project does not have.
* **No dead-letter table.** `run_records` where the outcome is a retryable failure already
  holds the full payload; a second table would be a copy that can disagree.
* **No JSONPath and no JSON Schema.** A restricted path grammar and the operation's declared
  field list, respectively — see §Mapping and validation.
* **No inline webhook execution.** Fast-ack then queue, always — see §Webhooks.
* **No `count()` from `CRUDQueryBuilder` anywhere in this module.** It materialises every row
  to return an integer, and one of these tables holds a thousand JSONB payloads.

---

# Phases

| phase | what lands |
|---|---|
| **0** | The three defects: `crypto.py`, the re-encryption migration, the promoted egress guard and type coercion |
| **1** | Generic REST with API keys. The engine, the queue, the scheduler, the canvas, the run dock, dry runs, replay, and AI generation. Exit criterion: a published flow moves 50,000 records on a schedule, survives a restart, and reports exactly what happened to each one |
| **2** | Shopify. `join`, `aggregate`, `error_handler`, `delay`, `approval`. Incremental cursors. Replay-failed-records. AI field mapping and run triage |
| **3** | GoHighLevel. Inbound webhooks. Cron schedules |
| **4** | SAP, including the private-host door and mTLS. The canvas copilot. Automatic resume |
| **5** | The `agent` node. Workflows as Deep Agent tools. DNS pinning for SAP |

---

# Open risks

1. **The shared checkpointer pool is sized 1–2**, chosen for "a handful of small writes per
   export". Integration runs checkpoint every super-step with two workers concurrent. The size
   becomes environment-driven.
2. **Nothing prunes LangGraph's checkpoint tables**, which no Alembic revision owns and which
   `alembic/env.py` deliberately excludes. Six hundred super-steps per run against a
   five-minute schedule is unbounded growth. A terminal run must forget its thread — and
   whether the pinned `langgraph-checkpoint-postgres` exposes a delete for that needs checking
   before the design depends on it. **Genuinely unresolved.**
3. **The record buffer is process memory.** Hence a validated maximum batch size and a test
   fixture that asserts no keys are left open after every test.

   Related, and found while building: **the test database does not enforce foreign keys at
   all.** SQLite ignores `ON DELETE CASCADE` without `PRAGMA foreign_keys=ON`, which
   `tests/conftest.py` does not set, so any test asserting that deleting a flow removes its
   versions would be testing the harness rather than the schema. The service test therefore
   asserts only what the service controls, with the reason written into it — and the cascade
   itself was checked by hand against Postgres at the end of Phase 1: deleting one flow that
   had a published version, an enabled schedule and six scheduled runs left zero rows in
   `integration_flow_versions`, `integration_triggers`, `integration_runs`,
   `integration_run_steps` and `integration_run_jobs`.
4. **Per-flow serialisation in the claim query** is a correlated `NOT EXISTS` under
   `FOR UPDATE OF … SKIP LOCKED` — the subtlest query here, capable of starving or escalating.
   **This one bit, exactly as predicted.** The first version nested a scalar subquery inside the
   `EXISTS`, and at two levels of nesting SQLAlchemy's auto-correlation picked the wrong
   enclosing `SELECT`: the condition silently stopped meaning "*this* flow is busy" and started
   meaning "*any* flow is busy", so one running sync would have blocked every workflow in the
   system. It is now an explicit `.correlate(IntegrationRun)` over a joined outer query. Both
   halves are tested and only the second failed — "a busy flow is not claimed again" passed all
   along. **Still outstanding: the `EXPLAIN` at a realistic row count.**
5. **Partial unique indexes silently do nothing on SQLite unless `sqlite_where` is also set.**
   "Two published versions are refused" would pass against Postgres and enforce nothing in the
   test database, which is worse than having no test. Handled as planned: both dialect
   arguments, the rule enforced in `publish_flow`, and a test aimed at the service that
   publishes three times and asserts the *count* of published rows is one — checking that the
   newest is published would pass with all three published, which is the state the whole
   arrangement exists to prevent.
6. **In-process rate limiting is per worker**, so under `uvicorn --workers N` the effective
   send rate is N times the configured one. Mitigated by running the sync worker as a single
   in-process loop — `job_queue`'s decision, for a related reason — and by keeping the daily
   counters in Postgres regardless.
7. **Cancellation is polled**, so a node in the middle of a request finishes it.
8. **Five background loops per worker process** once this lands: the download worker, the
   expiry reaper, the sync workers, the scheduler and the webhook drain. The claims are safe
   under `SKIP LOCKED`, but the scheduler's tick rate multiplies by the worker count. Four of
   the five are running as of Phase 1; the webhook drain arrives with Phase 3.

9. **The in-built local model cannot draft a workflow at the shipped context size.** Measured,
   not estimated — see §How generation is stopped from inventing things. The generated
   `WorkflowDraft` schema alone costs 971 of the 1536 available tokens. The feature degrades
   correctly and the hosted providers are unaffected, but a deployment that wants the local path
   to work has to raise `OLLAMA_NUM_CTX`, and no amount of trimming this module's own prompt
   substitutes for that.

---

# Related

* [GRAPH_DESIGNER.md](GRAPH_DESIGNER.md) — the drawn LangGraph this module is not, and the
  source of most of its structural decisions
* [DOWNLOADER_AGENTS.md](DOWNLOADER_AGENTS.md) — the table-as-a-queue, the heartbeat, the
  batch retry and the SSE-over-polling contract, all reused in shape here
* [CHATBOT_AI_SETTINGS.md](CHATBOT_AI_SETTINGS.md) — webhook actions, whose SSRF guard,
  templating modes and never-raise outcome are this module's starting point
* [AGENT_RECURSIVE_DATAFRAMES.md](AGENT_RECURSIVE_DATAFRAMES.md) — `filter_algebra`, the frame
  buffer and the two-layer plan validation, all borrowed
* [SQL_ASSIST.md](SQL_ASSIST.md) — generating a structured artefact from a sentence, declining
  rather than approximating, and repairing exactly once
* [ERROR_HANDLING.md](ERROR_HANDLING.md), [SERVICE_PATTERNS.md](SERVICE_PATTERNS.md),
  [SCHEMAS.md](SCHEMAS.md), [MIGRATIONS.md](MIGRATIONS.md), [TESTING.md](TESTING.md)
