# DOWNLOADER_AGENTS.md
Downloader Agents — a hundred rows, the real total, and the rest of them as a file

---

# What it is

A data agent that matches more records than an answer can print now says how many there
are and offers to send the whole set as a file. Saying yes queues a background job that
reads the records in batches of fifty, writes one part file per batch, merges the parts
into one artifact, and gives the user a link.

No pages and no menus. The feature reaches the user entirely through the agent's own
conversation and two download routes.

```
a data tool returns > 100 rows
  → download_tools.describe_tool_result
      → record_reader.count_records            exact COUNT(*)
      → download_service.create_offer          the download_exports row
      → download_graph.start_export_offer      runs to interrupt(), returns the sentence
  → the agent says: "There are 125 records. Do you want me to create a
     downloadable CSV file containing the list of all the records."

user: "yes"
  → confirm_download tool  → mark_queued + job_queue.enqueue_export
  → the in-process worker  → download_graph.resume_export
      → read 50 → write part → … → merge → publish → cleanup

user: "is it ready?"
  → download_status tool → "/downloads/<uuid>"

GET /downloads/{export_id:uuid}                  → Stream, attachment    (operator)
GET /downloads/{export_id:uuid}/status           → DownloadExportView
GET /downloads/{export_id:uuid}/events           → ServerSentEvent

GET /file_downloaders/{session_id}/{file_name}   → Stream, attachment    (visitor)
GET /public/downloads/{export_id:uuid}/status    → DownloadExportView
GET /public/downloads/{export_id:uuid}/events    → ServerSentEvent
```

Note the asymmetry in the visitor's three. The **file** is named by session and file
name, because that is how it is stored on disk — one folder per conversation — so the
URL and the directory are the same two facts and cannot drift apart. **Progress and
status** are still named by export uuid, because both are asked *while the export is
being built*: there is no file name yet, and finding out when there will be one is the
whole point of the call.

`GET /public/downloads/{export_id:uuid}` still serves the file too. Nothing this
application generates points at it any more, but a link handed out before the move is in
somebody's chat transcript, and breaking it would turn a working download into an error
for no reason the visitor could understand.

The module owns three tables (`download_exports`, `download_jobs`,
`download_export_parts`), a `db/` subpackage for the one query generic CRUD cannot
express, schemas, and three route controllers. It owns **no templates**: the only
front-end work is an `EventSource` consumer in the agent console and in the widget
script.

---

**This is not the only file path any more.** A Create File block on the Flow Builder or
Graph Designer canvas writes a file because an operator *drew* one, which is a different
thing from an agent noticing that an answer is too big to print — different rows, different
audience, and no offer to accept. It owns its own table and its own routes; see
[FILE_NODES.md](FILE_NODES.md), which states why it is not four nullable columns on
`download_exports`.

---

# Why it exists

A tool's rows were capped at 200 and handed to the model, and two things followed from
that which were both bad for the person reading the answer.

**The answer dumped whatever it got.** Nothing bounded how many rows the model printed,
so a broad question produced a two-hundred-row wall of text in a chat bubble. That is
not an answer; it is a data dump with a sentence on top.

**The count was a lie by omission.** There was no `COUNT(*)` anywhere on the path, so
"200 rows (capped)" was the most the model could know. It could not tell the user how
many records actually matched, and it had nothing to offer them instead — see rule 6 of
`prompt_builder._GROUNDING_RULES`, which could only say *this might not be all of them*.

*(That fetch cap has since been removed — a tool query now reads every matching row, so
the total in the prompt is exact even without a `COUNT(*)`; see
[TOOL_QUERY_MODES.md](TOOL_QUERY_MODES.md). Neither reason above goes away: 5,275 rows
still cannot go into a chat bubble, and a file is still the right way to hand somebody a
result of that size. What changed is that the count is now free on the ordinary path —
this feature's own `count_records` still earns its place on the export path, where the
rows are streamed to a file and never held.)*

So the display budget became a fixed number of rows, the total became an exact figure,
and the remainder became a file.

---

# The display-row rule

`DISPLAY_ROW_LIMIT = 100`, in `query_executor` next to `PROMPT_ROW_LIMIT`.

The two are for different things. **200 is what the model may reason over** — count,
compare, aggregate — and is how many rows are serialised into the prompt. **100 is what
may go into a chat bubble**, which the widget renders as a Markdown table with its own
scroll rather than as prose (see [WIDGET_RENDERING.md](WIDGET_RENDERING.md)).

It was 20 originally and was raised on request. The number is a judgement about reading,
not a safety limit — the safety limits are `PROMPT_ROW_LIMIT` above it and
`record_reader.MAX_EXPORT_ROWS` on the file — so changing it is a one-line change and
nothing else in this document depends on the value.

**It is also the number the download offer keys off.** `describe_tool_result` runs its
`COUNT(*)` and prepares an offer only past this limit, so raising it to 100 moved the
offer with it: a result of 100 or fewer now arrives whole, with no download step in the
way, and past 100 the answer is 100 rows plus the real total plus the offer.

It is enforced **by instruction, not by truncation**. Truncating here would take the other
100 rows away from the model as well and leave it unable to answer the question it was
asked. So `describe_result` still returns every row it has, states the exact total, and
rules 8 and 9 of `_GROUNDING_RULES` do the rest:

```
8. Never print more than 100 rows of data in one answer. If there are more, show the
   first 100, say how many there are in total, and stop. …
9. When a tool result gives you a sentence to end your answer with, repeat it exactly
   as written. …
```

`describe_result` gained three parameters — `total_rows`, `count_is_lower_bound` and
`offer` — and now reports one of three headers, because a model cannot tell them apart
from the rows alone:

| Situation | Header |
| --- | --- |
| No count was run | `30 row(s):` (the old wording, with the capped warning) |
| The rows are everything | `12 row(s), which is the complete result:` |
| The rows are a sample | `30 row(s) out of 4821 matching record(s). These are a sample; the total is the figure to report:` |

---

# The offer sentence

> There are {n} records. Do you want me to create a downloadable CSV file containing the
> list of all the records.

Produced by `download_service.offer_sentence`, delivered as the payload of the graph's
`interrupt()`, and passed to the model with an instruction to repeat it **word for word**.

It is not composed by the model and not paraphrased on the way, for two reasons that are
really one reason. It contains the record count — a model rewording it is how a user gets
told the wrong number. And it asks a plain yes/no question, which is what makes a bare
"yes" on the next turn something the application can act on.

A set past `MAX_EXPORT_ROWS` gets a refusal instead, naming the limit, and no export row
is written at all: there would be nothing to confirm.

---

# The graph

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
| --- | --- |
| `count_records` | The exact `COUNT(*)`. Refuses a set past the ceiling **before** anybody is asked, because offering a file and then withdrawing it is worse than saying no up front. |
| `await_confirmation` | `interrupt()`. The run stops here, its state is written to PostgreSQL, and the payload is the sentence above. |
| `write_batch` | Reads 50 records and writes one part file, both inside the retry. |
| `merge_parts` | The format's own merge. The count that comes back is counted from the *files*. |
| `publish_artifact` | Marks the export `ready` and sets its expiry. The download route serves `ready` and nothing else, so until this runs there is no way to fetch a half-written file. |
| `notify_failure` | Stores one fixed sentence for the agent to relay. |
| `cleanup` | Deletes the part files, closes the cursor, drops the caches. Reached by **every** terminal path — which is why it is a node with several inbound edges rather than a `finally` block that has to be right in five places. |

**Why a graph and not a function with a loop.** Two of those edges are the feature.
`await_confirmation` is a genuine pause: it stops inside one HTTP request and is resumed
by a different task after a different request, which is what a checkpointed graph and
`interrupt()` are for. And every way an export can end passes through one cleanup node.

**Where the interrupt goes and comes back.** `start_export_offer` runs the graph in the
request that answered the question — it counts, it pauses, and the payload it returns is
what the agent says. The user's "yes" enqueues a job; the worker calls `resume_export`
with the same `thread_id`, and the run continues from the pause. The request side never
builds anything and the worker side never asks anything.

**The recursion limit is computed.** `write_batch` loops back to itself once per batch and
LangGraph's default limit is 25 — which would stop an export at 1,250 records, by raising
`GraphRecursionError`. `_RECURSION_LIMIT` is derived from the ceiling and the batch size.

---

# Reading the records

`base/record_reader.py`. Every statement it runs is assembled by `query_executor`
(`assemble_built_query` / `assemble_sql_statement`), so an export reads exactly what the
tool is permitted to read, re-validated on this run, with the same active-table and
active-column checks. Nothing in the module builds SQL.

**One streaming cursor, both query modes.** `LIMIT 50 OFFSET n` is the obvious way to read
a set in batches and it is the wrong one here, twice over:

* it needs a total order or it is simply incorrect — without one the database may return a
  row in two batches and another in none, and a grouped tool query does not always have a
  unique key among its output columns;
* even with an order, the database re-runs and re-sorts the whole result for every batch.
  500,000 records is 10,000 batches: ten thousand sorts of half a million rows, to read
  each of them once.

So both modes open one server-side cursor and pull 50 rows at a time. One pass, one
snapshot, every row exactly once, no ordering required — and it is what
`query_executor._execute_sql_query` already does for the row cap, held open longer.

**The cost, stated.** The cursor holds a connection and a read transaction for the export's
whole run. `MAX_EXPORT_ROWS` (default 500,000, `DOWNLOAD_MAX_EXPORT_ROWS`) is what bounds
that — an export nobody could finish is refused up front rather than pinning a connection
for an hour. A retried batch re-opens the cursor and discards its way back, which is linear
in what was already read and only ever happens on the failure path.

**Counting.** Builder mode wraps the statement in a `COUNT(*)`. SQL mode counts by
streaming, because the operator's statement cannot be wrapped — `_execute_sql_query`
documents why (MySQL rejects a derived table with duplicate output column names, which is
the sort of query the mode exists to permit) and rewriting approved SQL is not something
this application does. The streamed count stops one row past the ceiling, so
`count_is_lower_bound` is true in exactly the situation where the export is refused
anyway. Which means **every count a user is ever shown is exact**.

---

# Batches, parts and retries

Fifty records per batch, `MAX_BATCH_ATTEMPTS = 3` attempts per batch, and the part file
deleted before each retry.

**Why the file is deleted first.** A batch fails somewhere inside writing its part — after
the header, after twenty rows, mid-row. What is on disk is then a fragment that looks
exactly like a part file and is not one. Deleting is what makes an attempt an attempt
rather than an edit.

**Why it retries at all.** A batch reads from *someone else's* database over a connection
this application does not control: a dropped connection, a lock timeout, a failover. Those
are transient, and abandoning a whole export over one of them is a worse answer than trying
again. What is **not** transient is a query that no longer validates or a table that was
switched off — those raise `ToolQueryError`, which is not retried, because three attempts
at a permanent failure is three times the wait for the same answer.

**Why it gives up out loud.** After the third failure the export stops. There is no partial
file and no "here are the first 2,000 records": an export that silently contains some of
the data is the one outcome worse than no export, because nothing about the file says so.
The `cleanup` node then removes the whole export directory — the parts *and* any partial
artifact.

Discarded attempts are kept as `download_export_parts` rows. Three rows with the same
`part_number` is what "this batch failed twice before it worked" looks like afterwards;
without them a recovered export is indistinguishable from a clean one.

**The retry loop is inside the node, not around it.** Making it an edge
(`write_batch → discard_part → write_batch`) would mean a checkpoint write per attempt, a
router that had to tell "retry this batch" from "next batch" from "give up", and a crash
mid-retry resuming into a state the cursor no longer matches. A worker that dies is already
handled one level up, by the job being requeued.

---

# The three formats

`base/part_writer.py` defines the contract — `extension`, `media_type`, `write_part`,
`merge_parts` — and resolves a format's module lazily by name. `base/` knows nothing else
about any format.

| Package | Writes | Merges by |
| --- | --- | --- |
| `csv/` | `.csv` via the stdlib `csv` module | Concatenating bytes in 1 MiB chunks, keeping the first header |
| `xls/` | `.xlsx` via openpyxl `write_only` | Reading each part back `read_only` and streaming its rows into one workbook |
| `parquet/` | `.parquet` via pyarrow | `ParquetWriter` over the last part's schema, one `write_table` per part |

**Not pandas.** `pd.concat` over every part would be one line and would hold the entire
export in memory — the thing this feature exists to avoid. It would also turn an integer
column containing a NULL into floats, so `qty: 3` becomes `3.0` and the file quietly
disagrees with the answer the agent gave in the chat.

**`xls/` writes `.xlsx`.** The folder name is the format as people ask for it. Legacy
`.xls` caps at 65,536 rows, which an export whose whole purpose is "more records than fit
in a message" would hit routinely.

**`csv` as a package name is safe.** Python 3 uses absolute imports, so `import csv` inside
`app/services/downloader_agents/csv/` is the standard library.

Three things in these modules are load-bearing and were each found by a failing test:

* **pyarrow and openpyxl are imported at module scope, never inside the worker function.**
  pyarrow's C extension must not be first imported on a thread that is later destroyed;
  `asyncio.to_thread` uses the loop's executor, so the first export in a process would
  initialise it on a pool thread and the next pyarrow call in a fresh loop would
  **segfault** in `ParquetWriter`'s constructor. The registry already provides the laziness
  the function-level imports were for.
* **openpyxl creates no cell for a `None`**, so a record whose final column is NULL is
  saved narrower than the header and read back with that field missing entirely. Every row
  goes through `_rectangular`, in the writer *and* in the merge.
* **Parquet pins a schema per export**, derived from the first batch and widened one-way to
  text. Without it, a batch that happens to be all NULLs infers a null column that cannot
  hold the next batch's values, and the export dies thousands of records into a query that
  works everywhere else.

---

# The queue

A `download_jobs` row, claimed with `FOR UPDATE SKIP LOCKED`
(`app/db/downloader_agents/queries.py`), drained by an asyncio task started in
`main.on_startup`.

**A table, not a broker.** There is no Redis, Celery or arq in this project and this does
not add one. A locked row is a queue that is durable across restarts, safe across
processes, and visible in the same database as everything it is about. What a broker would
add is throughput this feature will never need and a service to operate that it would not
justify.

**In-process, deliberately.** One container to deploy, and the worker runs the same code
the requests do. It is the shape `db_utils.cleanup_idle_connections` describes and —
unlike those two — it is actually started.

**One job at a time.** An export holds a cursor open against the user's own database.
Draining two would double that against a server this application does not own, to finish a
background job sooner than anybody is waiting for.

**A dead worker is recovered, not resumed.** `heartbeat_at` is written while a job runs; a
job whose heartbeat goes stale is requeued and the next worker starts the build again from
the confirmation. Starting again rather than resuming is deliberate: the dead worker's part
files are on disk and its cursor is not, and a resume would have to trust files it cannot
verify were written completely. The checkpointed *confirmation* survives either way — it is
from before any file existed.

---

# Where the files live

```python
EXPORT_BASE   = Path("uploads/exports")           # app/utils/file_utils.py
DOWNLOAD_BASE = Path("uploads/file_downloaders")
```

Two roots, because the two kinds of file have different lifetimes and different
audiences. Part files are scratch, keyed by export, deleted the moment the merge
succeeds. The finished artifact is the deliverable, keyed by the **chat session** that
asked for it, and it is what the visitor's URL names.

Note the paths: `uploads/…`, **not** `app/uploads/…` like the two bases beside them.
docker-compose mounts the named `uploads` volume at `/app/uploads`, while `UPLOAD_BASE`
resolves to `/app/app/uploads` — inside the `.:/app` bind mount, i.e. the host's source
tree. Datasource uploads landing there is pre-existing behaviour; generated exports must
not, so these bases point at the volume that actually survives a rebuild.

Also deliberately **not** under `static/`, and this is worth being blunt about because it
looks like the obvious simplification. `main.py` mounts `static/` with no authentication at
all: a file placed there is fetchable by anyone with the URL — no key, no session token, no
expiry check, because a static mount bypasses the route that enforces all three. An export
is somebody's business data, 2,921 rows of client names and revenue in the case that
prompted this note, so it stays behind a handler that can say no.

It is also not the fix it appears to be. When a download button once failed with *"file
wasn't available on site"*, the cause was a relative `href` resolving against the embedding
page — and `/static/downloadable_items/…` is exactly as relative. It would have produced
the identical 404 while giving the data away. The fix was an absolute URL; see
[the download link](#the-download-link).

**`/file_downloaders/…` reads like a static path and is nothing of the kind.**
`FileDownloadController` resolves every request to an export row and refuses it unless
four things hold: the widget key is active, the session in the path is the session that
produced the file, the export is `ready`, and its window has not closed. The path is
named after the folder for legibility, not because the folder is exposed.

```
uploads/exports/<export-uuid>/
    parts/part-000001.csv …          ← scratch, deleted once the merge succeeds

uploads/file_downloaders/<session-id>/
    items_2026-08-06.csv             ← the artifact, and what a visitor fetches
```

One directory per export for the parts, because cleanup has to remove everything this
export created and nothing anyone else's created — and "everything under this directory"
is a rule that cannot get that wrong.

One directory per **session** for the artifacts, because a session's files being cleaned
up has to be one operation over one directory. Keyed by export uuid they would be
scattered across as many directories as the visitor asked for exports, and "remove
everything this conversation produced" would be a query rather than an `rmtree`. The cost
is that two exports in one session can want the same file name — `artifact_name` is the
table plus the date, so asking for the same tool twice in one afternoon does exactly that.
`part_store.available_artifact_name` is what stops the second overwriting the first: it
returns `orders_2026-08-07-1.csv` when `orders_2026-08-07.csv` is already there, and the
name it returns is the one stored on the row and put in the URL. Without it the first
download would serve the second export's bytes — the same number of records, from a
different query, with nothing to show anything was wrong.

The session token is minted by the browser, so it is caller-supplied and never joined onto
a path as it stands: `part_store.session_folder` normalises it first, and a token of
`../../etc` becomes a harmless flat name. `resolve_within_downloads` is the belt to that
braces, re-checking on every request that the row's stored path still sits inside the
folder the URL named.

`DOWNLOAD_EXPORT_TTL_SECONDS` (**default 30 minutes**) decides how long an artifact stays
downloadable. Short on purpose, with a consequence worth stating plainly: a visitor who
closes the tab and comes back an hour later has no file. That is the intended trade —
asking again is cheap, and a server that keeps every export anybody ever requested is an
archive nobody asked for.

Two mechanisms honour the TTL and they do different jobs:

* **The download route refuses a lapsed export** on every request, via
  `download_service.is_expired`. This is the *rule*, and it is what makes the window exact
  rather than "thirty minutes, give or take however long since the last sweep".
* **`expire_lapsed_exports` deletes the bytes** and marks the row `expired`. It takes the
  artifact out of `uploads/file_downloaders/<session>/` and prunes that folder when it was
  the last file in it — a session that asked for fifty exports would otherwise leave fifty
  empty directories per visitor, forever, and nothing else comes back for them. It also
  removes the export's own directory, normally already empty, but left behind by an export
  that failed after writing parts and before merging. This is the
  *housekeeping*. `run_expiry_reaper` calls it on a timer started in `on_startup` beside
  the queue worker, at `REAPER_INTERVAL_SECONDS` — a tenth of the TTL, floored at a minute
  and capped at a quarter of an hour, so it is derived from the TTL rather than a second
  number to keep in step. At the default that is every three minutes.

The row is kept rather than deleted, so a visitor returning to a dead link is told the file
expired and that they can ask again; a missing row produces "that download could not be
found", which reads like the application lost it.

Everything that states the lifetime to a user derives it from the setting through
`ttl_phrase()`. The agent's "available for the next 30 minutes" was hard-coded as *24
hours* once, which was true and then silently was not — a user told a file lasts a day when
it lasts half an hour is worse served than one told nothing.

---

# Streaming

Three surfaces stream, each because the alternative is a silence somebody cannot interpret.

**The download.** `Stream` over a 64 KiB async chunk generator with
`Content-Disposition: attachment`. The repo's only previous download built its content in
memory (`chatbot_settings_routes.download_widget`), which is right for a 4 KB script and
wrong for a file that can be hundreds of megabytes.

**Build progress.** `GET …/events` returns `ServerSentEvent`, emitting `progress` per
completed part, `retry` per failed attempt, then `ready` or `failed`. Read from the
`download_export_parts` rows rather than an in-memory bus, because the worker writing the
files and the request streaming the feed are different tasks — and under more than one
replica, different processes. A browser that reconnects halfway through sees the whole
story. A retry surfaces as its own frame: it is the difference between "this export is big"
and "this export is struggling", which is the only question somebody watching one has.

**The agent's answer.** `deep_agent_service.stream_answer_*` uses `astream_events` instead
of the single blocking `ainvoke`, exposed as SSE at `/deep-agents/{id}/ask-stream` and
`/public/chatbot/message-stream`. An agent turn runs real queries and can take a minute; a
spinner that says nothing for that long is indistinguishable from a hang, so the `tool`
events say which tool is running and the `token` events paint the answer as it lands.

Both blocking endpoints are unchanged and are the fallback. `static/js/deep_agent_stream.js`
and the widget script fall back to them when `EventSource` is unavailable or the stream
fails before the first token. A turn that cannot stream — an active Flow Builder node, or a
chatbot with no data agent — yields one `fallback` event and the client posts instead.

One thing worth knowing about chunk handling: `_chunk_text` does **not** strip. A chunk
boundary falls wherever the provider's tokeniser put it, very often on a space, so trimming
each chunk concatenates "Here" and "are" into "Hereare". Whitespace inside a stream is
content.

---

# The download card

What a visitor actually sees. Not a link in a sentence — a block under the reply that
announced the file, showing what is happening and then a button.

```
┌──────────────────────────────────────────────┐   ┌──────────────────────────────────────┐
│ 📄 CSV file                                  │   │ 📄 project_details_2026-08-07.csv    │
│ Reading the next batch…  2,100 of 2,921      │ → │ 2,921 records  ·  44.6 KB            │
│ ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓░░░░░░░  72%              │   │ [ ⬇  Download CSV ]                  │
└──────────────────────────────────────────────┘   └──────────────────────────────────────┘
```

**The words rotate; the numbers do not.** `WORKING_WORDS` cycles every 2.6 s under a
shimmer, because a long build with a static line reads as a stuck one. The figures beside
them come from the progress stream and are exactly the records written so far. The bar is
that fraction, capped at 99 % until the artifact exists — a full bar next to "still
working" is the one thing a progress bar must never say. `prefers-reduced-motion` drops
the shimmer and keeps the words.

**The card is not part of the message.** It is its own block, appended after the bubble,
and it outlives the turn that created it. That is what makes the next line literally true.

**A visitor can keep asking while the file is written.** The turn ended when the reply
arrived; the build is a queued job and is not a turn. Nothing in the card touches the
input, the send button or the typing indicator — there is a test that asserts exactly
that, by reading the card's source for those four names.

**The button is an anchor, not a button.** `<a href download>`, so middle-click, "save
as" and keyboard Enter all behave. A `<button>` with a click handler looks identical and
does none of them.

<a id="the-download-link"></a>
**The server sends paths. It must keep sending paths, and this is a compatibility
boundary rather than a style preference.** `widget.js` is *downloaded* — the operator
saves it and hosts it on their own website — so the copy running in a visitor's browser
can be arbitrarily older than the server answering it. Every version of it does
`API_BASE + url`. That is correct for a path and catastrophic for an absolute URL:
`https://api.example.com/https://api.example.com/…` is a string the browser never sends,
so nothing reaches the access log, nothing throws, and nothing is logged anywhere.

That is not hypothetical. `SITE_URL` was prefixed onto these URLs to fix a download link
that had resolved against the embedding page, and it left every progress card stuck on
*"Gathering the records…"* forever — the export finished in four seconds, the file was
written, the row said `ready`, and the browser never asked. The only symptom was silence.

**Naming the host is the embed snippet's job.** `apiBase` is the one piece of
configuration that lives next to the script actually running, so it cannot disagree with
it. `SITE_URL` is a server-side declaration of the same fact, and a server-side
declaration is exactly what goes stale when a tunnel rotates or a domain changes —
`download_service.site_url()` still exists for server-side callers that have no request
to derive a host from, but nothing the widget consumes goes through it.

`apiUrl()` in the widget script is the belt to that braces: it passes an already-absolute
URL through untouched and prefixes `API_BASE` onto a path, so a future server that does
send an absolute URL cannot double-prefix it on any script from this version onward.
`test_widget_script.py` sweeps every `EventSource(`, `fetch(` and `.href =` in the card and
requires `apiUrl(` in each, so a fourth network call cannot be added without one.

**A widget that stops working after an upgrade wants a fresh `widget.js`.** The file is
generic — all configuration is fetched at runtime — so re-downloading it and dropping it
in the same path is the whole procedure, with no `<script src>` change.

## How the interface finds out

A tool returns a string to the model and nothing else — that is LangChain's contract —
and the layer that renders the reply is four calls above the layer that queues an export.
So `confirm_download` and `download_status` record the export in
`base/download_notice.py`, a context-local read once at the top of the turn and attached
to the reply as `download` (`DownloadNoticeView`: status, counts, and the three URLs this
asker may use). `chatbot_turn_service` opens one `download_scope()` per turn and reads it.

Two things about that module are load-bearing:

* **It holds a mutable box, not a value.** LangGraph runs its nodes as their own tasks,
  and a new task gets a *copy* of the context — so rebinding the ContextVar inside a tool
  is invisible to the parent. Mutating an object the copy inherited by reference is not.
  This is the same constraint, reached from the same depth, that makes
  `utils/turn_recorder.py` append to a `TurnRecord` rather than replace it. Getting it
  wrong is silent: the file builds correctly and no card ever appears.
* **The scope is reset per turn.** A notice left set is a download shown to whoever asks
  next, which is worse than showing none.

**The model is never given a URL, in any state.** It is told a button is already on
screen. Two reasons: a second copy of a control the user has is worse than none, and the
answer renders as plain text — so a model writing markdown produces a visible
`[Download CSV](/public/downloads/…)`, which is precisely what it used to do. Grounding
rule 10 says so in the prompt, and `_describe_status` no longer has a URL to leak.

**When the stream drops, the card polls.** A build can outlast one SSE connection —
`MAX_STREAM_SECONDS` bounds ours at an hour and a proxy may bound it harder. On a
data-less `error` the card closes the socket and falls back to `…/status` every four
seconds, warning the operator's console once. Closing first matters: a browser reopens a
stream that ended on its own, which would re-run the progress feed forever.

The operator console does **not** draw a card. It has no conversation history, so it
cannot resolve a "yes" in the first place; the notice carries console URLs for the day
that changes, and nothing renders them yet.

---

# Authorisation

Two controllers, because the two audiences authenticate in genuinely different ways.

`DownloadController` — `require_auth`, ownership resolved export → data agent → `user_id`.

`PublicDownloadController` — no session and no cookie. The chatbot key's **uuid** *and* the
conversation's `session_token`, both required. The token is the part that matters: a widget
key identifies a public website, not a person, so a key alone would let any visitor of that
widget read every export ever produced for it. The key's uuid is used rather than its
publishable `api_key` because this link is spoken aloud into a chat transcript, and a link
carrying the widget's credential would put that credential in the transcript.

The same rule applies inside the conversation: `confirm_download` and `download_status`
resolve an export against the asking conversation, so a model handed another visitor's uuid
finds nothing.

---

# Failures are answers

| What happened | What the user is told |
| --- | --- |
| Result larger than the ceiling | `There are 1,200,000 records, which is more than the 500,000 this application can put into one file. Please narrow the question down and ask again.` |
| Three failed attempts at one batch | `The file cannot be created at the moment. Please try again.` |
| The merge failed | The same sentence. The export is `failed` with no artifact — never `ready` with a missing file. |
| The artifact expired | `That download has expired. Please ask for it again.` — the same sentence whether the clock has merely passed or the reaper has already swept the file. Those are two states minutes apart, and `download_service.has_lapsed` is what treats them as one; checking the row's status before the clock is how the second one used to fall through to *could not be found*, which reads like we lost the file. |
| Someone else's export, an unknown uuid, a missing file, a path outside the export | 404, `That download could not be found.` — the same sentence for all four, because distinguishing them confirms which uuids are real. |

The failure message is **fixed**. The real reason — a dropped connection, a lock timeout, a
driver error — is not something to put in front of a visitor, and "try again" is the only
useful instruction either way. The real reason goes to `error_message` and the log, for the
operator.

An offer that cannot be prepared is not mentioned at all: the user still gets their answer,
and the log gets the reason. An export is an extra; the answer is not.

---

# What it does not do

* **No pages, no menus, no forms.** The feature is reached through the agent's conversation
  and two download routes.
* **No non-relational datasources.** A tool reading Mongo or a file is refused with a
  sentence the agent relays, the same as `query_executor` does.
* **No resume of a half-built export.** A dead worker's job restarts from the confirmation.
* **No second file for a repeated "yes".** A confirmation for an export already underway
  reports its state instead.
* **No `.xls` (BIFF)**, and no zero-column Parquet file — an empty Parquet export declares
  one `no_records` column, because a file with no columns is unusable and crashes pyarrow.

---

# Testing

```bash
docker compose exec -T app python -m pytest tests/unit/services/downloader_agents \
    tests/unit/routes/downloader_agents tests/unit/schemas/downloader_agents -q --no-cov
```

Nothing that produces or consumes data is mocked. The datasource under every test is a real
SQLite file and the writers really write: mocking them would prove the graph calls them,
where running them proves an export of 125 records contains 125 records.

The cases that carry the suite:

* **exact counts** in both query modes, and for a grouped query, where the honest total is
  the number of groups;
* **batch boundaries** at 1, 49, 50, 51, 100 and 125, asserted by the *set of ids* read
  back — a reader that repeated one row and dropped another would pass a length check;
* **the retry path**: a batch that fails twice and recovers with every record still present
  exactly once, and one that fails three times, stops the export, stores the fixed
  sentence, and leaves an empty directory;
* **all three formats** round-tripped through the library a user would open them with, plus
  the all-NULL column that breaks a typed format;
* **the claim**, which cannot be claimed twice, and the stale job, which is requeued;
* **the scoping**, tried directly: one visitor's token against another's export;
* **the notice crossing a task boundary** — `note_export` called inside a real
  `asyncio.create_task`, asserted to be visible to the parent. That is the exact seam
  that broke, and nothing weaker crosses it.

The card's own behaviour is asserted twice over, in two different ways, because neither
alone is enough. `test_widget_script.py` reads the generated script for the handful of
properties that are cheap to edit away and expensive to notice — the anchor, the 99 % cap,
the teardown of every timer and socket, and the four names the card must never mention
(`inputEl`, `sendBtn`, `typingEl`, `armIdleTimer`). What that cannot tell you is whether
the thing works, so the flow is also driven end to end in a real headless browser against
a real embed page: ask, say yes, watch the shimmer and the bar move, ask something else
while it builds, then **click the button** and assert on the browser's own download
events. There is no JavaScript test harness in this repo, so that run is manual — it is
what found the ContextVar bug, which every Python test passed straight through.

Clicking rather than fetching is the point, and is itself a lesson. An earlier run
verified the link by curling its target, which proved the *route* worked and said nothing
about whether the *page* could reach it — which is exactly how the relative `href` above
shipped. A link is only verified by following it from the page it is rendered on.

Three fixtures are load-bearing and each says why in its docstring —
`background_sessions` (the nodes and the SSE stream open their own sessions, which in the
container point at the *development* database), `graph_checkpointer` (same problem, plus a
saver cached across event loops), and `upload_root`. `pytest.importorskip("langgraph")`
comes **before** the graph imports, as in `test_tool_chain_graph.py`.

`FOR UPDATE SKIP LOCKED` cannot be proved on SQLite — the dialect has no locking clause, so
SQLAlchemy drops it. What the tests cover is the claim's bookkeeping; the locking itself is
a PostgreSQL guarantee only a concurrent PostgreSQL test could demonstrate.

---

# Related

* [DEEP_AGENTS.md](DEEP_AGENTS.md) — the agent whose tools make the offer, and the row cap
  this feature is the other half of
* [TOOL_QUERY_MODES.md](TOOL_QUERY_MODES.md) — why a SQL-mode statement is never rewritten,
  which is why the reader holds a cursor instead of paging
* [TOOL_CHAINING.md](TOOL_CHAINING.md) — a nested tool is exportable too; the export re-runs
  the chain the same way the tool call did
* [DOCKER_AND_LOCAL_LLM.md](DOCKER_AND_LOCAL_LLM.md) — the two dependencies this feature
  adds, and the volume the exports live in
* [MIGRATIONS.md](MIGRATIONS.md) — the two revisions, and why `alembic/env.py` now excludes
  langgraph's own tables
* [TESTING.md](TESTING.md) — the fixtures above, and the layout
* [SCHEMAS.md](SCHEMAS.md#downloader_agents--appschemasdownloader_agentsdownloader_agent_schemaspy)
  — `ConfirmDownloadArgs`, `DownloadStatusArgs`, `PublicDownloadQuery`,
  `DownloadExportView`, `DownloadProgressEvent`
