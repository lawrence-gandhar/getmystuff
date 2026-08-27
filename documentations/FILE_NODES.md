# FILE_NODES.md
The file blocks — Create File and Download File, on both canvases

Module: `app/{models,schemas,services,routes}/file_delivery/`, with
`services/file_delivery/nodes/` holding one runner per canvas.
The pattern it follows is [EMAIL_DISPATCH.md](EMAIL_DISPATCH.md) §2.

---

# 1. What it is

Two blocks that appear on the Flow Builder canvas and the Graph Designer canvas alike.

**Create File** takes rows from an earlier block, writes them as CSV, XLSX, TXT or
Parquet, and leaves the path behind. **Download File** turns a file the first one wrote
into something a person can click: a coloured button in the chat for a visitor, an
authorised link on the node's output for an operator.

```
In a flow (a visitor):

  Run Graph "Orders" ─→ Create File ────→ Download File ───→ Send Message
                        CSV of that       [x] button        "Your file is ready."
                        block's rows      "Download my
                        FILE_PATH          orders" #198754
                                           FILE_URL
                                                 │
                                                 └─→ the widget draws a button
                                                     under that sentence

In a pipeline (an operator):

  SQL "orders" ─→ Create File ─→ Download File ─→ Email node
                                 url on its       binds {{LINK}} to it
                                 output
```

Routes:

```
GET /generated_files/{file_id:uuid}                              → Stream, attachment
GET /generated_files/{file_id:uuid}/status                        → GeneratedFileView
GET /public/generated_files/{file_id:uuid}?key=&session_token=    → Stream, attachment
```

The module owns one table (`generated_files`), one new column on
`chatbot_flow_sessions` (`node_results`), two route controllers and a reaper. It owns **no
templates**: the only front-end work is one renderer in the widget script and two property
panels, one per canvas.

---

# 2. Why it exists

Nothing in this application could hand somebody a file it made *as a step*.

The one file path that existed is [DOWNLOADER_AGENTS.md](DOWNLOADER_AGENTS.md), and it
belongs end-to-end to a data agent's own conversation: a tool matched more rows than a chat
bubble can hold, the agent offers an export, a background job builds it. Its row requires a
`data_agent_id`, a `tool_config_id` and a checkpointer `thread_id`, all `NOT NULL`.

So an operator who had *drawn* the work — a pipeline that reconciles yesterday's orders, a
flow that collects a reference and looks it up — could not say "…and put that in a CSV".
They could email a number. They could not hand over a file.

---

# 3. The five decisions worth knowing

## 3.1 Its own table, not four nullable columns on `download_exports`

Those three `NOT NULL` foreign keys are what say *what an export is*. A file a block wrote
has no tool, no agent and no checkpointer thread, so fitting it in would mean making all
three nullable — deleting the invariant that table's own docstring states, and leaving
every reader of it asking which kind of row they had.

What is reused instead is everything that is not the row: the two-audience URL shape, the
expiry-plus-reaper arrangement, the streaming route, `file_utils`' base-directory
reasoning, and `PublicDownloadQuery`. The layouts are deliberately similar because the
problems are the same; the tables are separate because the things are not.

**Two statuses, not five.** An export moves through offered → queued → building → ready
because a worker builds it over minutes. Here the row is inserted **after** the bytes are
on disk, so "a row exists" means "a file exists", and the only later transition is the
reaper's: `ready` → `expired`.

## 3.2 What reaches the file is everything, or the block fails

This is the decision the whole module is shaped around, because every source offers the
same temptation: a smaller file that looks complete.

| Source | Canvas | Why it is exact |
| --- | --- | --- |
| a **Run Graph** block's result | flow | `graph_runner.full_result` re-reads **every** row when the file is written. `GraphOutcome.rows` — what the conversation saw — is a twenty-row preview |
| an earlier **node's** output | graph | a SQL node's output already *is* every matching row: `_run_sql` passes `max_rows=None` and nothing on that path caps |
| an **AI Fallback** block's answer table | flow | complete by construction — it is the small table the model itself authored. A table the engine had to cut short is marked and **refused** |
| a **variable** holding a dataset | flow | JSON rows become columns; anything else is text, and text is TXT-only |

Past `FILE_MAX_ROWS` (`FILE_NODE_MAX_ROWS`, default 500,000 — `record_reader`'s figure and
its reasoning) the block **fails** and takes its `error` port. It does not write the first
N. An operator who believes a file holds everything is worse off than one whose block went
red, which is the rule `integration_runner` already applies to its email cap.

For a Run Graph source the ceiling is checked twice: against the total the run already
reported, *before* a single row is read back, and again against what actually arrived. The
first is what makes an impossible file cheap to refuse.

## 3.3 polars and pyarrow, where `downloader_agents` uses the stdlib

`csv_writer` avoids pandas for a stated reason: `DataFrame.from_records` over dictionaries
infers dtypes, so an integer column containing one NULL becomes floats and `qty: 3` reaches
the file as `3.0` — the export quietly disagreeing with the answer the agent gave in the
chat.

That reason does not apply to polars, which keeps an integer column with nulls an integer
column. There is a test per format asserting exactly that, because it is what makes the
divergence safe. What a dataframe buys is consistent quoting, encodings and dates across
three formats in twelve lines each.

**Do not "fix" one module to match the other.** They write for different callers under
different constraints, and both docstrings say so.

Two retries are worth knowing about. A column holding two unrelated types, or a nested
value from a JSON column, passes `from_dicts` happily as a Struct and then fails **at the
write** ("CSV format does not support nested data"). So the fallback — write every value as
its string form — wraps the frame *and* the write. A first version wrapped only the frame,
passed its one-format test, and broke on the other three.

`xlsxwriter` is a dependency of this module and nothing else: polars writes Excel through
it, openpyxl does not cover it, and it is not pulled in by polars itself.

## 3.4 Neither block says anything; the button is opt-in

A Create File block writes and hops on. A Download File block puts the link in a variable
and hops on. Both are the Email node's rule and it is the same reason: a block that
announced "I have made your file" would be putting words in the operator's mouth. The
sentence is a Send Message block, which they wrote.

With *show a download button* switched on, the Download File block is the only block on the
flow canvas that adds something to a turn without being the turn. That matters in the
engine: it is **not** a result type. The payload is attached to whatever ends the turn (see
`_with_download`), so a Send Message after it still speaks, a Menu after it still offers its
options, and the button appears underneath rather than instead. A type would have made those
mutually exclusive.

There is no button on the graph canvas at all, and the fields are **refused** at save
rather than accepted and ignored: a pipeline has no chat, and a setting somebody chose and
this application silently dropped is worse than one that was never offered.

## 3.5 The colour is validated three times

It lands in an inline `style` attribute on a page this application does not own, so
`^#[0-9a-fA-F]{6}$` is checked by the canvas validator at save, by the runner at run time,
and by `FileButtonView` on the way to the browser.

Three, because the value is operator-authored, the target is a style attribute, and two of
the gates can be bypassed — by a node saved by an older version, or edited straight in the
database. The third cannot: every turn goes through it. It **drops** a bad button rather
than raising, because the answer the visitor is waiting for is fine and losing a button
beats losing the turn.

---

# 4. Where a file lives, and who may fetch it

```
uploads/generated_files/<file-uuid>/orders-a-1001.csv
```

One directory per file, named from the row's own uuid. Cleanup is then "remove this
directory", a rule that cannot take somebody else's file with it — the reasoning
`part_store` gives about its export directories. **No path is ever assembled from anything
a visitor, an operator or a model supplied**: the operator's name reaches only the
*filename*, through `normalize_filename`, so `../../etc/passwd` becomes a flat name. The
extension comes from the format, never from the name, so a Parquet block cannot produce
`orders.csv`.

Not under `static/`, which `main.py` serves with no authentication whatsoever. Under the
`uploads` volume, so a rebuild does not take the files with it — both the constraints
`EXPORT_BASE` already carries.

Two audiences, two routes, and the difference is the whole security model:

**The owner** — `GET /generated_files/{uuid}`, `require_auth`, and the ownership filter is
part of the lookup rather than a check after it, so there is no version of `owner_file` that
can hand back somebody else's row to a caller who forgot to look. Either origin: a flow's
file is as much this operator's data as a pipeline's.

**A visitor** — `GET /public/generated_files/{uuid}?key=…&session_token=…`. All four facts
are in the query: the file, the widget key (active only — switching a widget off has to
stop its links working, not just its chat), the session token, and that the file came from
a **flow**. That last one is not decoration: a graph's file has no visitor, and without it
every pipeline file would be one guessed uuid away from anonymous with any valid widget key.

The key travels as the chatbot key's **uuid**, never its publishable `api_key` value: this
link is handed to a visitor and lives in a chat transcript, and a link carrying the widget's
credential would put that credential in the transcript.

Every refusal that could confirm which uuids are real is the same 404 with the same
sentence. A **lapsed** file is the deliberate exception — a 410 saying it has expired,
because "could not be found" reads as though the application lost the file and sends
somebody looking for a link that worked yesterday.

---

# 5. How long a file lasts

`NODE_FILE_TTL_SECONDS`, default **24 hours**, against the export queue's thirty minutes.

The difference is deliberate. An export is a sample somebody asked for mid-conversation and
can trivially ask for again. This is a deliverable an operator built into a flow and offered
as a button, and a visitor who is handed one, closes the tab and comes back after lunch
should still get their file.

The sweep deletes bytes and marks the row `expired`, keeping the row so a dead link can say
so. Every route calls `assert_servable` on **every** request, so a lapsed file is refused
whether or not the reaper has been round — the sweep deletes bytes, the route enforces the
window. `ttl_phrase()` derives every sentence that states the figure, so a deployment that
changes the TTL does not leave a help page promising the old one.

---

# 6. `node_results`, and why a flow needed a new column

A flow session's `variables` is a flat map of **strings**. "The rows the previous block
produced" has nowhere to live in it — and must not live in it anyway: that dict is the
visitor's own namespace and is interpolated into chat text, so a key the application
reserves is a key an operator can collide with. It is the argument `awaiting_graph_run` and
`call_stack` each make for being columns.

So `chatbot_flow_sessions.node_results` holds one small record per block, **keyed by node
id** — not by variable name, because a Create File block points at one particular box on the
canvas, which is a different question from "what is the current value of X", and two blocks
may share a name.

```
{"n3": {"kind": "graph_run", "run_id": "…", "total_rows": 5275},
 "n5": {"kind": "table", "columns": [...], "rows": [...], "truncated": false},
 "n7": {"kind": "file", "file_uuid": "…"}}
```

What is stored is deliberately small: a run's **id** rather than its rows, and an AI
Fallback's own small table. It is written for every block that produces one, whether or not
anything reads it — the alternative would make an earlier block's behaviour depend on
whether a Create File block elsewhere happened to name it, so adding one would silently
change what another block does.

The AI Fallback's answer is kept **twice**, in two shapes: as text under its variable name
(what an email or a chat bubble wants) and as columns and rows here (what a CSV wants). One
stored form would mean one consumer parsing the other's, and for a pipe-separated block of
prose that is guessing at somebody's data.

---

# 7. What each block leaves behind

| | Flow | Graph |
| --- | --- | --- |
| Create File | the **path** under its variable name; the file in `node_results` | `{file_uuid, file_name, file_path, file_format, row_count, byte_size}` on its output |
| Download File | the **link** under its variable name; a button payload on the turn, if asked for | `{url, file_uuid, file_name, file_format, byte_size, row_count, expires_at}` on its output |

The path and the link are two variables rather than one because they are for two different
audiences. A path is a fact about this server — useful in a log line or an email to your own
team — and is no use to a visitor; handing one out in a chat bubble would tell them where
the file lives without letting them fetch it.

A Download File block **names** its Create File block; it does not infer one from the wire.
An operator can put a Send Message between the two, and a named reference survives that
while "the block wired into me" does not. It is the arrangement a Timer node's `timer_node`
already uses, and the reason is the one `_validate_timer_node` gives about node ids versus
typed-in names.

The file is **resolved** at offer time rather than trusted from the session: a window can
close between the turn that made the file and the turn that offers it, and a button linking
to a lapsed file is worse than no button.

---

# 8. Ports, and what a failure does

Two ports on each block, on both canvases: `written`/`offered` and `failed`. The reason
`send_email`, `run_graph` and `run_flow` all carry two — a file that could not be written
must not leave by the same edge as one that was, or the conversation offers a download of
nothing.

On the flow canvas a failure goes through `_failed_step`, so it takes the block's own
`error` port, else the enclosing Run Flow call's `failed` port, else signs off. On the graph
canvas it becomes a `NodeFailure`, which takes the `error` path if one is drawn and settles
the run as failed if not.

What takes the failure port:

* no rows anywhere the block was pointed at, or a variable the conversation never set;
* text asked to become a spreadsheet;
* rows with no column names, which would need invented `column_1..n` headers in a file
  somebody sends on;
* a result past the ceiling;
* a truncated AI table;
* a Create File block a branch skipped, so the Download File block has nothing;
* a file whose window has closed.

Every one of those is knowable at the moment the block runs, which is what makes it
routable. Nothing in this module fails a *later* turn for a decision made in an earlier one.

---

# 9. Selection runs and the reference table

`node_runners.referenced_nodes` collects every node another node's settings read from, and
both new types are in it — a Create File node's source node, and the Create File node a
Download File node names.

Without that, testing a **selection** of just the file node would read nothing and fail
inside the runner claiming the upstream produced no rows, instead of saying the upstream was
not ticked. That function's docstring is explicit that a new reference kind must be added
there, and this is the eighth and ninth field it collects.

---

# 10. Reading the code

| To answer | Read |
| --- | --- |
| what a format writes | `services/file_delivery/file_writer.py` |
| where the rows come from, and every refusal | `services/file_delivery/row_source.py` |
| paths, URLs, lookups, expiry | `services/file_delivery/file_service.py` |
| what the blocks do in a chat | `services/file_delivery/nodes/flow_builder_runner.py` |
| what they do in a pipeline | `services/file_delivery/nodes/graph_designer_runner.py` |
| how a turn carries the button | `services/flow_builder/engine_service.py` — `_with_download` |
| how the widget draws it | `services/chatbot/chatbot_service.py` — `renderFileButton` |
| who may fetch a file | `routes/file_delivery/routes.py` |
