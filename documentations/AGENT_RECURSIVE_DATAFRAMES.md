# AGENT_RECURSIVE_DATAFRAMES.md

Reading every record a source returns, **narrowing them to what was asked**, and either
returning those records or grouping them in memory — rather than asking the database for
a different query. Totals, averages, counts and conditions over the whole result set
instead of over the rows that fit in a prompt.

*(A tool query is no longer capped — it returns every matching row. But only
`PROMPT_ROW_LIMIT` of them are written into the model's prompt, so a model counting what
it can see still counts a sample. This module computes over the rows themselves, which
is why it survives the caps going away.)*

**The complaint this most recently answered.** An agent whose only source returns every
month's revenue, asked about March, replied:

> I'm unable to filter the data by month because the available tool does not accept a
> date parameter.

That is true of the *source* and false of what the agent can do. The records can be
narrowed after they are read, and there is no reason a source needs a parameter for
every question somebody might ask of its output. Two things were missing and both are
now here: **filters in the plan** (§ Narrowing), and **a designed graph as a source**
(§ Two kinds of source).

---

# When this is the wrong tool, and that is most of the time

Say this first, because the honest answer decides whether the rest of the page is
worth reading.

A builder-mode tool config already pushes `count`, `sum`, `avg`, `min` and `max`
plus a `GROUP BY` into SQL — see `query_executor._aggregated_columns` and
[TOOL_QUERY_MODES.md](TOOL_QUERY_MODES.md). A SQL-mode tool runs any read-only
statement the operator approves, including percentiles, window functions and
pivots. In both cases the database does the grouping over its own indexes, in one
pass, exactly, with no ceiling.

**For those cases this module is a slower reimplementation of `GROUP BY`, and it
should not be used.** It earns its keep where the query cannot be asked again:

* re-grouping the output of an approved SQL-mode statement, where the statement is
  fixed and the question that arrived is about a different axis of it;
* deriving a measure the operator's statement did not produce, without editing an
  approved statement to get it;
* a datasource the operator can read but not tune — no index, no permission to add
  one, and a server-side `GROUP BY` that times out.

That is why the capability is **off by default and switched on per source**
rather than being a thing every agent can do.

**Filtering shifts that balance, and it is worth being clear where.** "Narrow the rows
and total them" is still a `WHERE` clause the database would do better — *if the query
can be changed*. The cases above are exactly the ones where it cannot, and in those cases
a condition the source takes no parameter for is not an inefficiency, it is the
difference between an answer and an apology. So: if you can add the filter to the tool's
own query, do that. If you cannot, this is what that question costs.

---

# Narrowing, before the fold

`filter_algebra.py` owns which conditions may be applied and `frame_ops.apply_filters`
says them in polars — the same split `partial_algebra` / `frame_ops` already makes, for
the same reason: the correctness argument is testable without a DataFrame library.

**The rule is one sentence.** A predicate may be applied per batch if and only if it is
**row-wise** — decidable from one record, with no reference to any other record:

    filter(b₁ ⧺ b₂) == filter(b₁) ⧺ filter(b₂)

That identity is what lets a filter live *inside* the fold rather than in front of it.
Filtering in front would mean materialising the whole result set first, which is the thing
the wave loop exists to avoid. It is asserted against SQLite at five different batch
sizes (`test_frame_ops_filters.TestAFilterDistributesOverBatches`).

| Operators | |
|---|---|
| `==` `!=` `<` `<=` `>` `>=` | one value |
| `in` `not_in` | a list, up to `MAX_IN_VALUES` |
| `contains` `starts_with` | one value, **literal** — a regex reading would match a wider set than was asked for |
| `between` | two values, **inclusive at both ends**, which is what a person means by "between 100 and 500" |
| `is_null` `is_not_null` | no value |

Conditions are **conjunctive** — every one must hold. "March or April" is one `in` filter,
not two filters, and `describe_filters` joins with "and" so the sentence cannot imply
otherwise.

### What is deliberately absent

Each of these is a thing somebody will ask for, and each breaks the identity above:

| Asked for | Why not |
|---|---|
| `amount > the average amount` | needs the average of the whole set, unknown until the last batch — so batch one would be filtered against batch one's average |
| the ten largest | a rank is a fact about the set; per batch it returns the ten largest *of each batch* |
| the latest row per department | the same, with a group boundary. It is a window function |
| rows whose id appears in another result | a semi-join, and this pipeline has one source |

None is refused by accident. The planner's prompt lists them by name under
`unsupported`, so a model asked for "above average" says so instead of producing a plan
that is shaped right and answers a different question.

### Dates get parts, not arithmetic

"In March" is `part=month, operator===, value=3` on a date column. It is **not** a range
the model computes, and that is the single most deliberate narrowing in the feature: a
model writing `>= 2026-03-01 AND < 2026-04-01` is doing month-boundary arithmetic, and
February, December and leap years are exactly where it gets that wrong — silently, as a
smaller result set that still looks like an answer. Extracting the part is the same
question with no arithmetic in it.

`year`, `month`, `quarter` and `day`, each compared to a whole number, each range-checked:
month 13 matches nothing, and "there was no revenue in that month" is a sentence somebody
repeats in a meeting.

**The one place a loose parse would be worse than a failure.** A driver may hand dates back
as text — SQLite always does. `frame_ops._temporal` parses those **strictly**:
`str.to_datetime(strict=False)` turns anything it cannot read into null, a null has no
month, and the filter then matches **no records at all**. So the refusal names the column,
the part, and *the value that actually failed* — not the first value in the column, which
in a column of ISO dates with one `"n/a"` in it would be a date, and the message would be
false.

One limit stays a limit rather than a guard: text dates are read by polars' format
inference, and `05/03/2026` is read **day-first**. ISO dates — what SQLite and every
migration here write — have no such reading. Refusing every ambiguous format would refuse
most text date columns outright, so the mitigation is `plan_summary`: a filtered answer
always says what it was filtered by.

### Every filter value is a string, and the provider is the reason

`AggregationPlan` is an LLM's structured output, so its JSON schema goes to the provider as
`response_format` — and a **strict** validator refuses shapes pydantic emits happily. A field
typed `Any` renders as an **empty** schema `{}`:

```
400 Unsupported JSON schema fields in schema with keys: dict_keys([])
    param: response_format, code: wrong_api_format
```

That was Cerebras rejecting every planning call the moment filters existed. The visible
symptom was the agent reporting that it could not filter by month — *the same sentence the
feature exists to remove*, arriving one layer further in, which is why
`tests/unit/schemas/agent_recursive_dataframes/test_plan_schema_is_provider_safe.py` walks
the whole schema document and fails on any empty property, any `anyOf`/`oneOf`, and any
list-valued `type`. A union would have been refused too, so one concrete type is the only
shape that travels everywhere.

**So the typing moved to where the column is known.** `frame_ops._coerced` casts each value
against the frame's real dtype — int, float, bool, date, or text — and refuses with the
column and the value named when it cannot. That is strictly better than trusting the model's
own JSON types: `"1000"` against an integer column used to be a polars type error surfaced
as a refusal, and is now a comparison against 1000. Three details are deliberate:

| | |
|---|---|
| a **date part** is always a whole number | the month of anything is 1–12; the column's own type says nothing about it |
| `true`/`yes`/`1` and `false`/`no`/`0` are **parsed**, not truthy | `bool("false")` is `True`, and a filter that selects the opposite of what was asked is silent and total |
| a date value must be **ISO** | this is a value a *model* wrote, not a column a driver returned, so there is no legacy format to accommodate — `01/08/2026` has two readings and picking one would be inventing a boundary |

One consequence: `value=""` means *no value given*, not "equals the empty string". Emptiness
is asked for with `is_null`, which is the question somebody actually means.

### Two shapes of answer

`validate_plan` records the shape on the plan as `mode`, once, so the four places that
behave differently read one field instead of each deciding what an empty `aggregations`
list meant.

| `mode` | asked for | `rows` | `group_count` |
|---|---|---|---|
| `groups` | measures | one per group | how many groups |
| `rows` | filters, no measures | the matching records, capped | how many **matched** |

`rows` mode is how "show me the Python department's March revenue" comes back as records
rather than as a single total. The records are returned **in read order** and not sorted:
"the first two hundred" only means something if the order is the query's, and re-sorting
would answer a different question from the one the count beside it describes.

**The cap on retained rows is the prompt's, not the query's**, and the distinction is what
makes the mode honest. Every matching record is read and counted — `matched_rows` is exact
— and `KEEP_MATCHED_ROWS` (which *is* `query_executor.PROMPT_ROW_LIMIT`) bounds only how
many travel back to be shown. The answer says "200 of 4,317". A cap that changed the total
would be the other thing entirely, and is what the previous release removed.

---

# The shape

    START → get_count → read_wave ──Send×4──→ aggregate_slice ─┐
                            ▲                                  │ (barrier)
                            └───────── merge_wave ◄────────────┘
                                            │
                                    finalise → cleanup → END
        any failure ──────────→ notify_failure → cleanup → END

One wave is `AGGREGATE_WAVE_WIDTH` slices of `AGGREGATE_CHUNK_ROWS` records —
**four batches of 200, so 800 records** — read in order, then folded concurrently,
then merged into the running aggregate. The loop repeats until the cursor is
exhausted.

### The divider reads; the workers aggregate

This division is forced, not chosen. `record_reader.BatchReader` is **one
server-side cursor**: `read()` advances it, and asking for a batch out of order
re-runs the statement and rescans from the top (`record_reader._reopen_at`). Two
tasks reading it at once would turn a linear scan into repeated full rescans, or
collide inside the driver.

So `read_wave` reads its whole wave itself, sequentially — which is cheap, being
four `fetchmany` calls on an already-open cursor — and what fans out is the
**folding** of what it read. A wave costs `read + max(fold)` rather than
`read + Σ fold`.

### The barrier is free

Every `Send` returned by one router runs in a single LangGraph super-step, so the
plain edge out of `aggregate_slice` schedules `merge_wave` exactly **once**, after
every worker of that wave has written. There is no barrier to implement, and
writing one would be writing a second, worse one.

### One tool may be several sources

`aggregate_service.record_sources(entry)` turns a tool entry into the things the
reader reads, and a nested tool is not always one query:

| The tool | Sources |
|---|---|
| No children | one, unrestricted |
| List children | one, **carrying the children's values** |
| An iterating child (`binding_mode` `each`) | **one per value** — same statement, different bind, its own label |
| A chain that matched nothing | none, and `stopped_by` names the tool |

`record_reader.ChainedBatchReader` presents that list as one reader: one cursor at a
time, rolling to the next when the current is exhausted, and returning nothing only
once **every** source is spent. A source that legitimately matches nothing rolls
forward rather than ending the run — otherwise a department with no projects would
silently truncate the answer at that department. `count_all` sums across them and
checks the ceiling before anything opens.

Nothing in `partial_algebra` or `frame_ops` changed for this. The fold is already
order-independent and mergeable, so N cursors read in order fold into one running
frame exactly as N waves of one cursor do — a partial aggregate does not care which
cursor its rows came from. See
[TOOL_CHAIN_ITERATION.md](TOOL_CHAIN_ITERATION.md).

**This closed a real bug.** Before `record_sources` existed, the source was built from
the tool's stored config alone and dropped the chain's values, so an aggregation over
a nested tool totalled a **wider** result set than the tool has ever returned — with
nothing about the answer saying so. `test_aggregate_sources.py` exists to keep that
from coming back.

### Nothing large travels in state

A run of 200,000 records is 250 waves, and LangGraph copies state on every
super-step. So the records live in `frame_buffer` — a module-level registry keyed by
the run — and the state carries only their keys. Same shape as `record_reader`'s
reader registry and `db_utils`'s engine cache, for the same reason.

---

# Why the answer is exact — `partial_algebra.py`

A batched aggregate equals a single-pass one **if and only if** every aggregation is
an associative fold over a carried intermediate. The carried intermediate is not
always the answer, and that gap is the whole subject of the module.

| Requested | Carried per group per slice | Merged by | Finalised as |
|---|---|---|---|
| `count` (records) | `n` | sum | `n` |
| `count(col)` | `c` — non-null count | sum | `c` |
| `sum(col)` | `s` **and** `c` | sum | `s` if `c > 0` else NULL |
| `min` / `max` | `mn` / `mx` | min / max | as carried |
| `avg(col)` | `s` **and** `c` | sum | `s / c` if `c > 0` else NULL |

`avg` cannot be merged from averages:

    slice 1: [10, 20]     carries (30, 2)
    slice 2: [60]         carries (60, 1)
    merged:               (90, 3)  ->  avg = 30      correct
    mean of means: (15 + 60) / 2 = 37.5              wrong

### Three rules that are easy to get wrong

1. **`avg` divides by the non-null count of the averaged column, never the group's
   record count.** SQL `AVG` ignores NULLs, so dividing by the record count turns
   "the average order value across the 40 orders that have one" into "…across all
   100" — a number that looks entirely reasonable and is wrong.
2. **`sum` over an all-NULL group is NULL in SQL and `0` in polars.** That is why
   `sum` carries a count it does not appear to need: without it the answer reads
   "£0 of revenue" where the database would say "no revenue recorded", and those are
   different facts.
3. **NULL is its own group** in both SQL `GROUP BY` and polars `group_by`. The two
   already agree, so nothing substitutes a sentinel — `"null"` would collide with
   the literal string and silently merge two groups.

### What is refused rather than approximated

`median`, `percentile`, `mode` and `count_distinct` have no bounded fold: an exact
answer needs every value, or every distinct value, resident at once — at which point
the batching bought nothing and the memory ceiling is gone. They are **refused with
a message naming the five that are available**.

The refusal is unreachable by construction rather than merely enforced:
`validate_plan` checks every function against `AGGREGATION_FUNCTION_VALUES` from
`app/models/tool_configs/tool_configs.py`, which is exactly the foldable set. A test
asserts the two sets are equal, so a sixth function added to the tool config
vocabulary fails that test until somebody comes here and gives it a fold.

`stddev` and `variance` *are* decomposable, through Chan's `(n, mean, M2)` merge.
They are absent only because they are not in the tool config vocabulary. If they are
ever added, `partial_algebra` is the file that gains them.

### Two more refusals worth naming

* **Grouping by a float column.** `NaN != NaN` and `-0.0 == 0.0`; two values that
  display identically are not necessarily equal, so the groups would not be
  trustworthy.
* **Averaging what the tool already averaged.** If a builder-mode config contains
  `{"type": "avg", ...}` for a column, averaging its output averages averages —
  mean-of-means arriving through the back door, because the first mean happened in
  the database where the counts behind it are gone. Refused, since unlike the batch
  case there is no fix available. **In SQL mode this is undetectable**: this
  application does not parse operator SQL anywhere, so an already-averaged column
  coming out of a SQL tool cannot be seen. That is a real limitation, stated rather
  than papered over.

---

# polars, and why the dependency

`group_by`/`agg` run in Rust **with the GIL released**, so several slices genuinely
fold at once under `asyncio.to_thread`. pandas holds the GIL through
`DataFrame.from_records` over dicts and through string-key factorisation, which
would serialise the fan-out and leave the wave pattern doing nothing at all. That is
the whole reason for a third DataFrame library in an image that already has pandas
and pyarrow.

**`import polars` appears at module scope in exactly one file, `frame_ops.py`.** Both
halves matter. Module scope, because
`downloader_agents/parquet/parquet_writer.py:37-47` documents what happens when a
compiled extension is first imported on a pool thread that is later destroyed, and
this module is reached from `asyncio.to_thread`. One file, because a second import
site is a second chance to get the first one wrong — asserted by
`test_frame_ops.TestOneImportSite`.

### Two operational notes

* **Oversubscription.** `AGGREGATE_WAVE_WIDTH` threads × polars' own Rayon pool is
  `4 × N` threads on an N-core box. Set `POLARS_MAX_THREADS` in the environment if
  that matters — polars reads it at **import**, so it cannot be set from Python
  inside this module.
* **CPU baseline.** The default wheel targets a modern x86-64 baseline. On an older
  host it dies with `Illegal instruction` and **no Python traceback**; the fix there
  is to swap `polars` for `polars-lts-cpu` in `requirements.txt`.

---

# Two kinds of source

The pipeline reads batches, counts before it starts, and closes what it opened. Until
recently one thing could do all three. Then a **Graph Designer graph** became something
whose result can be read, and a designed graph is not a query:

| | a tool config | a published graph |
|---|---|---|
| the records | a statement, streamed off one server-side cursor | already produced, held in memory |
| the count | `COUNT(*)`, before anything is read | `len()` of what was produced |
| the columns | **probed** — one row fetched, names reported, no values | taken off the result, because there is nothing to probe on a drawing |
| when the plan is made | before a single record is read | after the graph has run |
| what "close" means | release the cursor to the pool | nothing; there was never a cursor |

`row_supply.py` is the interface that makes both legitimate without either being the
other's special case: `QuerySupply` and `MaterialisedSupply` both `count`, `open` and
`release`, and the graph's nodes stop knowing which they have. The wave loop keeps reading
"until there are no more records" rather than gaining a second version of itself.

`MaterialisedSupply` keeps the **cursor's contract**, not merely something like it. Two
details are copied from `record_reader.BatchReader` rather than invented, because the
graph depends on both: batch numbers start at **1**, and an **empty** list means exhausted
while a **short** one does not. Get the second wrong and a run silently stops at the last
full batch, reporting a total short by a few records with nothing saying so.

### Reading a graph

`aggregate_service.graph_rows` runs the graph and takes `graph_runner.full_result` —
**never `outcome.rows`**, which is off `result_preview` and capped at twenty. Filtering a
twenty-row sample and reporting how many matched *in the sample* is a wrong number with
nothing about it saying so. That is the distinction `full_result`'s own docstring draws, and
this is exactly the caller it is addressed to: one that uses the values rather than
describing them. `test_graph_as_dataframe_source` uses sixty records against a twenty-row
preview so a regression cannot pass.

The graph runs **as its author**, not as whoever is asking, because a graph shared with a
workspace reads datasources scoped to whoever built it. `_graph_entry` records that
`user_id` for the purpose.

**A graph that stops to ask a question is refused.** Every other owner of a graph carries
the pause — `graph_tool_factory` relays the question and offers an `answer_` tool, a flow
ends the turn on it — and this one deliberately does not: resuming would mean holding a
half-read result set across two conversation turns, a second kind of state for a feature
whose whole shape is "read it all now". The refusal quotes the question and names the
graph's own tool, which does carry the pause. One step away, not a dead end.

A graph's last node need not produce records at all — it may produce a list, a dict or a
scalar. Those are **lifted into one-column records** under the name `value` rather than
refused, because "the departments the graph picked, filtered to the ones starting with P"
is a reasonable request and a list is what that graph returns. The name then appears in the
columns the planner is shown, so a model filtering on it filters on something it was
actually told about.

---

# Turning it on

Both kinds carry `allow_recursive_aggregate`, off by default, **under the same key** —
`tool_configs` (revision `d5f0a83c26b7`) and `tool_graphs` (revision `d5f1a9e2c437`) — so
`aggregate_service.readable_tools` is one expression rather than two. Two keys would be two
things to remember, and the forgotten one would opt nothing in.

| Source | Where |
|---|---|
| a tool config | **Allow whole-result grouping** on the tool form |
| a published graph | **Let an agent read and filter its whole result** in the graph's Edit dialog |

It lives on the source rather than on `data_agents` because it is a judgement
about one result set — "reading every record this returns is acceptable" — not about an
agent's capabilities. It means slightly more for a graph than for a tool config: a tool
config is one statement, while a graph can be a loop over eighty-two departments whose
result is assembled from eighty-two queries, so "run the whole thing and hold the result"
is a larger promise to make on an operator's behalf.

**Off by default is what makes the feature additive.** With no tool opted in:

* `aggregate_tools.aggregate_context` returns `None`, so `build_agent_tools` binds
  nothing extra and the agent's tool list is exactly what it was;
* `prompt_builder._aggregate_note` returns `""`, so the generated routing prompt is
  **byte-identical** to the one that agent had before the capability existed.

Both are asserted by `test_aggregate_tools.TestNothingChangesWhenItIsOff`.

Switching the flag on moves `tool_configs.updated_at`, so
`prompt_sync_service.is_prompt_stale` rebuilds that agent's prompt on its own. No
extra sync step and no staleness marker were needed.

---

# The two entry points

### The agent tool — `aggregate_records`

One tool, not one per opted-in tool config. Every other tool in this application is
a standing permission with a fixed question — the model chooses *which* tool, never
what it asks. This one takes an instruction, which is the opposite shape, and
minting a variant per tool config would put several free-text tools in front of a
model that `_GROUNDING_RULES` has just told to pick the single tool matching the
question.

**It still cannot choose its own query.** The instruction decides the grouping, not
the SQL: the tool config's stored query runs, re-validated on this run like any
other, and the plan is checked against the columns that query actually returns. The
model widens what can be *asked* of a permitted result set; it does not widen the
permission.

A `ToolQueryError` comes back as `TOOL FAILED: …` tool output rather than being
raised, exactly as `tool_factory` does — a raise would end the whole chat turn with
a 500 for something the model can say out loud and move on from.

### The console — `GET /aggregations`

| Handler | Route | Renders |
|---|---|---|
| `index` | `GET /aggregations` | `index.htm` — agent picker, tool picker, instruction |
| `tools` | `GET /aggregations/tools` | `partials/tool_field.htm` — the HTMX cascade |
| `run` | `POST /aggregations/run` | `partials/result.htm` or `partials/error.htm` |

Its own page rather than a panel on the Deep Agents console, because the two answer
different questions. That console asks "what does this agent say"; this one asks
"what is the actual total", and the second is something an operator wants to check
against their own database without spending a chat turn on it.

---

# Planning — how an instruction becomes a plan

1. **Choose the tool**, cheapest path first. A `tool_name` that resolves
   case-insensitively, or a single available tool, means **no LLM call at all** —
   and between them those cover nearly every real request, because the agent was
   told the tool names before it was asked anything. Only an ambiguous choice costs
   a call, over a catalogue of names, descriptions and tables — the same fields the
   routing prompt already showed it, and no records.
2. **Read the real columns** with `query_executor.probe_tool_query`: one row
   fetched, column **names only, no values**, applying every validator,
   active-table and active-column rule the real run will. It is also the only way to
   know the columns at all — a builder config with an empty selection means *every
   active column*, and a SQL-mode statement is not parsed.
3. **One structured-output call.** The allowed functions are rendered into the
   prompt from `sorted(SUPPORTED_FUNCTIONS)`, so the prompt cannot drift from the
   validator. The model is given the words to decline (`unsupported: true`), because
   a model with no way to say "I can't express that" will produce a plan that is
   *shaped* right and answers a different question.
4. **Validate, whatever produced the plan.** Every column is matched
   case-insensitively and then **replaced by the probed spelling** — polars matches
   names byte for byte, so `Region` left as typed is the difference between a
   grouping and a refusal three nodes later, where the column can no longer be
   explained. Aliases are **assigned here, never taken from the model**: an alias is
   an output column name, and one colliding with a group key would overwrite it.

**There is no internal retry.** A refusal names the tool's real columns and goes back
as a tool failure; the agent's own loop is the correction path, and it already
exists. Re-asking here would spend a second call to make the same mistake more
expensively.

---

# Ceilings, and why each one refuses rather than truncates

| Setting | Default | What it bounds |
|---|---|---|
| `AGGREGATE_CHUNK_ROWS` | 200 | records per slice |
| `AGGREGATE_WAVE_WIDTH` | 4 | slices folded concurrently |
| `AGGREGATE_MAX_SOURCE_ROWS` | 200,000 | records a run may read |
| `AGGREGATE_MAX_GROUPS` | 100,000 | groups the running aggregate may hold |

| Cap | Where | What happens |
|---|---|---|
| too many records | `get_count` | Refused **before a single record is read**, naming the real count and the ceiling, and suggesting a SQL-mode tool — which has no such limit. |
| too many groups | `merge_wave` | Aborted, and the running aggregate is **discarded, not returned**. A list of the first hundred thousand groups looks exactly like a complete answer. |
| *(no result-row ceiling)* | `finalise` | Every group is reported. This used to stop at 200, which was the worst place in the application for a cap: the feature exists to be exact, and then returned the first 200 groups of however many there were. |

`finalise` **sorts** — first aggregation descending, group keys ascending as a stable
tiebreak. A hash `group_by` returns groups in arbitrary order, so without this the same
question gives a differently-ordered answer each time. That still matters with every group
returned: it is what makes the order of the answer repeatable.

### Why 200 records a batch

A slice is an internal unit of work, and nothing but memory depends on its size: the
fold is associative, so the same groups and the same numbers come out whatever it is.

It is small, and the cost is worth stating: 200,000 records is 250 waves and a
thousand round trips, against eight waves at 25,000. At this size polars' fixed
setup per slice dominates the aggregation itself, so the fan-out buys little and the
run is round-trip bound. **The answer is exact either way.** It is an env var
precisely so the trade can be measured on real hardware rather than argued about.

### Why the ceilings exist at all

A run holds one database cursor open for its whole length and happens inside a chat
turn — 120 seconds for a visitor, 900 for the console. A run nobody can finish is
refused before it starts rather than abandoned halfway. Raising
`AGGREGATE_MAX_SOURCE_ROWS` without measuring turns a working answer into a timeout;
if millions of records are genuinely wanted, the answer is the `job_queue` pattern,
not a bigger number.

---

# Failing, and releasing

**Nodes return failures; they do not raise.** A raise inside a `Send` super-step
ends the run with no route to `cleanup`, and cleanup is what closes the cursor. So
every node catches and returns `{"failure", "advice"}` — the same two-audience split
`ToolQueryError` makes — and `_after_merge` checks for a failure **before** checking
whether reading finished. A wave whose slice failed has neither aggregated nor
finished, and merging what did succeed would produce a total quietly short by one
batch.

`failure` and `advice` carry a **reducer** for the same reason `wave_slots` does: a
plan that is wrong is wrong in all four slices of a wave, so all four write in one
super-step, which LangGraph refuses on a plain field. The first write wins — one
fault seen four times is still one fault.

**Cancellation is the leak the cleanup node cannot cover.** A chat turn timing out
cancels the task mid-node, and a cancelled node routes nowhere. So
`run_aggregation` releases in a `finally` as well: the cleanup node is the tidy
path, the `finally` is the guarantee. Both the reader and the buffer are released,
and the reader's registry key is prefixed `agg:` so it cannot be mistaken for an
export's uuid in the registry the two features share.

**No checkpointer.** There is no `interrupt()` here and nothing to resume across
requests, so the graph compiles without one — as `tool_chain_graph` does, and unlike
the export graph, which genuinely pauses between turns. Checkpointing would write the
whole state 750 times for a large run to buy a resume nobody asks for.

---

# Files

| File | Role |
|---|---|
| `app/services/agent_recursive_dataframes/partial_algebra.py` | the decomposition rules. Pure — no polars, no langgraph, no database |
| `app/services/agent_recursive_dataframes/filter_algebra.py` | which conditions may narrow a batch, and why only row-wise ones may. Pure, for the same reason |
| `app/services/agent_recursive_dataframes/frame_ops.py` | the only module that imports polars |
| `app/services/agent_recursive_dataframes/row_supply.py` | where a run's records come from: a cursor, or a graph's finished result |
| `app/services/agent_recursive_dataframes/frame_buffer.py` | the run-scoped record registry |
| `app/services/agent_recursive_dataframes/aggregate_state.py` | the graph state and its reducers |
| `app/services/agent_recursive_dataframes/aggregate_graph.py` | the nodes, the routers, `run_aggregation` |
| `app/services/agent_recursive_dataframes/aggregate_planner.py` | instruction → validated plan |
| `app/services/agent_recursive_dataframes/aggregate_service.py` | ceilings, messages, and the one call both callers use |
| `app/services/agent_recursive_dataframes/aggregate_tools.py` | `AggregateContext` and the agent tool |
| `app/schemas/agent_recursive_dataframes/aggregate_schemas.py` | see [SCHEMAS.md](SCHEMAS.md) |
| `app/routes/agent_recursive_dataframes/aggregate_routes.py` | the console |
| `templates/agent_recursive_dataframes/` | `index.htm` and three partials |

The module owns **no model** and **no `db/` subfolder**: nothing is persisted, a run
lives inside one request, and every read it needs is already expressed by
`collect_agent_tools` and `CRUDQueryBuilder`. The two columns it adds belong to
`tool_configs` and `tool_graphs`.

---

# Tests

`tests/unit/services/agent_recursive_dataframes/`. The load-bearing ones, in the
order they matter:

* **Exactness.** A real SQLite datasource of 12,347 records with a skewed
  distribution and a column full of NULLs, compared group by group and value by
  value against `SELECT … GROUP BY …` **run by SQLite itself** — not against a
  Python re-implementation, because the promise is that the answer matches what the
  database would have said.
* **Fan-out width does not change the answer.** The same fixture at widths 1, 2, 3,
  4 and 7 must give identical results; likewise batch sizes 1, 7, 200 and 5,000.
  This is the test that proves the parallelism is safe.
* **Every record is read exactly once**, checked at every batch and wave boundary
  (1, 199, 200, 201, 799, 800, 801) — a dropped tail batch shows up as too few, a
  re-read one as too many.
* **The barrier is real.** `partial_aggregate` is instrumented; every slice of a
  wave must finish before that wave is merged, and `merge_wave` must run once per
  wave rather than once per slice.
* **Every terminal path releases.** An autouse fixture asserts both registries are
  empty after every test in the module, and named tests cover success, refusal,
  worker failure and **cancellation**.
* **Nothing changes when it is off.** With every tool opted out, the routing prompt
  is byte-identical to the one built from an entry that has no such key at all —
  which is what every stored tool looked like before the column existed.
* **A filter distributes over batches**, asserted against SQLite at batch sizes 1, 2, 3,
  5 and 100 — the identity the narrowing rests on. Plus the ordering test that would catch
  the worst version of getting it wrong: filtering *after* the fold, which would report
  Python's whole-year figure as its March figure.
* **A filter never quietly matches nothing.** One test per refusal, each asserting the
  message names the column and quotes a value from the operator's own data — including the
  one that proves the *offending* value is quoted rather than the first one.
* **A graph's whole result, not its preview.** `test_graph_as_dataframe_source` runs a
  graph returning **sixty** records against a twenty-row `result_preview`, so a regression
  to `outcome.rows` cannot pass. It lives in `tests/unit/services/graph_designer/` because
  reading a graph means running one, which needs that package's three autouse fixtures.
* **The materialised reader keeps the cursor's contract** — batch numbers from 1, and a
  short final batch that is not the end. The second is the one whose failure is silent.

One note on a fixture, because it cost time and the code was innocent. The ledger in
`test_graph_as_dataframe_source` first derived its department from `index % 3` and its
month from `index % 12`; 3 divides 12, so the cycles were locked and **no Python row could
fall in March**. The tests failed with zero matching records against correct code. It is
now a written-out list that both the fixture and the expected figures are read from, with
coprime cycles — a reminder that a generated fixture can encode a correlation nobody
intended.

Anything touching the graph is guarded by `pytest.importorskip("langgraph")`, per the
repo convention: langgraph is installed in the container only. `partial_algebra`,
`frame_ops` and the planner tests need neither langgraph nor a provider SDK and run
anywhere.

---

# Not covered

* **Non-relational datasources.** `RecordSource.require_relational` refuses anything
  that is not PostgreSQL, MySQL or SQLite, so a CSV, Parquet or Mongo datasource
  cannot be grouped this way. That is the largest genuine gap in the platform's
  aggregation story and closing it means writing file and Mongo readers — a
  different piece of work, not an extension of this one.
* **Aggregating across two datasources.** One run reads one tool — though "one tool"
  may now be several queries, when that tool's chain iterates.
* **Runs longer than a chat turn.** Everything here is inline and bounded by
  `AGGREGATE_MAX_SOURCE_ROWS`; a background variant would be the `job_queue`
  pattern, with a table, a results store and a progress UI.
* **A known pre-existing hazard, unrelated but adjacent:** `db_utils.get_engine`
  raises a bare `Exception("Database temporarily unavailable (circuit open)")` that
  no tool-path handler catches, so it becomes a 502 for the whole turn rather than a
  recoverable tool failure. Not this module's to fix, and noted so it is not
  rediscovered here.
