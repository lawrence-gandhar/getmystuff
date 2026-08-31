# GRAPH_DESIGNER.md
Graph Designer — a LangGraph somebody drew, run and watched

---

# What it is

A canvas at `/graph-designer` where a user composes a graph out of SQL statements, literal
values, existing tool configs, questions for a person, branches and loops — then runs it,
or any part of it, and watches the flow, the state and a limited output in a dock below the
canvas.

**It is called *Pipelines* in the product and `graph_designer` in the code.** The sidebar
entry, the page headings and every operator-facing sentence say "Pipelines"; the route
(`/graph-designer`), the package, the templates, the CSS and the JS all keep the original
name. Renaming those would break bookmarks and every stored path for a label change, so the
split is deliberate rather than half-finished work — when this file says "Graph Designer"
below, it is naming the module.

```
GET  /graph-designer/                    → the library
GET  /graph-designer/help                → this file, browsable, for the operator
GET  /graph-designer/{id}/edit-form      → the record's edit dialog (name, who may call it)
POST /graph-designer/{id}/update          → save that dialog
GET  /graph-designer/{id}/edit           → the canvas + the run dock
GET  /graph-designer/{id}/graph          → the stored drawing, as JSON
POST /graph-designer/{id}/save            → validate + replace it
POST /graph-designer/{id}/runs            → start a run, return a handle
GET  /graph-designer/runs/{run}/events    → the run as SSE frames
GET  /graph-designer/runs/{run}           → the same frame, for polling
POST /graph-designer/runs/{run}/resume    → a human's answer
POST /graph-designer/runs/{run}/cancel    → stop
GET  /graph-designer/node-options         → what the property pickers offer
```

Three tables: `tool_graphs` (the drawing), `tool_graph_runs` (one execution) and
`tool_graph_run_steps` (one node, one pass).

## The in-app help page

`/graph-designer/help` (`templates/graph_designer/help.htm`) is this file written for the
person in front of the canvas: the same node vocabulary, the same ceilings and the same
refusal messages, said as *what to fill in* and *what happens*. Sixteen worked scenarios —
a straight line, a parameter, a branch, a `for_each`, collecting the passes, `IN :ids`, the
Union node, `do_until`, a question, an error path, timers, waits, `{{VARIABLES}}`, an email,
running and testing, and publishing — plus the limits in one table and every refusal with
its fix.

Three decisions it copies verbatim from `tool_configs/help.htm`, for the same reasons:

* **A route, not a link to the markdown.** A help page has to arrive inside the
  application's own layout, behind the same auth as the page it explains.
* **A literal path**, so it can never be read as `/{graph_id:uuid}/…`.
* **The whole body inside `{% raw %}`.** The page is largely SQL, JSON and `{{VARIABLE}}`
  samples, every one of which Jinja would otherwise try to read — and `{{TABLE}}` in
  particular would render as an undefined variable, teaching the opposite of what the
  section says. A route test asserts the samples arrive as written.

It is linked from **both** the library and the canvas. The canvas is where a port or a
binding mode actually needs explaining, and going back to the library for it would mean
leaving unsaved work; both links open a new tab for the same reason the Tool Configs one
does — the page is read *while* a panel is open, so it must not be the thing covering it.

A route test walks `NODE_TYPES` and requires every palette label to appear on the page, so a
node type added later fails the suite until the help describes it. Keep the two files in
step whenever a ceiling, a port name or a validator's wording changes.

---

# How the canvas draws itself

The nodes are not where somebody put them, unless somebody put them there. The canvas asks
the server for a **layer and a column per node** and arranges itself top to bottom — on
open, and again after anything that changes the wiring. Drag a node and the canvas leaves
your arrangement alone from then on; **Tidy up** hands the arranging back.

That decision is one field on the saved drawing, `layout`, and a drawing without it is
`auto` — which is every pipeline saved before this existed. So an existing hand-arranged
canvas *is* re-arranged the first time it is opened; nothing is written until Save, and
Reload restores what the database holds.

Three things about how a node is drawn are worth knowing:

* **A node with a choice shows a labelled pill per way out**, and each pill *is* that
  output port. A Branch's conditions, a loop's `each` and `done`, and any node's
  `on error` are the same thing drawn the same way — and the pill replaced the label that
  used to be painted on the connector itself, which said the same word twice. A node with
  one unnamed way out shows a plain dot on its bottom edge instead. The pills sit **side
  by side** and wrap only when they run out of room; a condition too long for its pill is
  truncated, with the whole of it in the pill's tooltip. Columns are spaced by the widest
  pill row on the canvas, so a node carrying two written conditions pushes its neighbours
  apart instead of overlapping them.
* **Green means it worked, red means it did not.** A node with exactly two ways out, one of
  them a failure, draws the other green — a SQL node's ordinary exit, `written`, `queued`.
  A Branch's conditions, a loop's `each` / `done` and a union's `next` / `execute` stay grey,
  because none of them says the node did or did not do its work. The Success and Failure
  nodes' discs are the same green and red, and the Failure red is the one the Flow Builder
  now ends a flow in.
* **A connector carries a red ✕ and a blue + on its midpoint.** The ✕ deletes it; the +
  inserts a node *into* it, so `A → B` becomes `A → new → B` with the original connector
  replaced by two. Which of the new node's ports carries the connection onward is
  `GC.continuationPort`'s decision, and a **For each** is the case that makes it matter: it
  must leave by `done`, because wiring the old target to `body` would move that node inside
  the loop and run it once per item. Success and Failure are not offered on a connector that
  already leads to one — see [CANVAS_SELECTION.md](CANVAS_SELECTION.md) §5.
* **A connector's ✕ and its two end handles are visible at all times**, as they always
  were. They were briefly made hover-only, on the argument that a twelve-connector
  pipeline carries thirty-six controls competing with the nodes. That reasoning was sound
  and the change was still wrong: nobody had asked for it, and an operator who had learned
  where the delete button was now saw a connector with no way to remove it. A control you
  cannot see has been removed, whatever the stylesheet says. Only the **bends** a hand
  places are hover-revealed, because they are new and a wire can carry four of them.
* **A connector that runs back up the canvas takes a lane to the right of every node.**
  A `for each` or `do until` body sending the run round again has no downward step to
  take — its target is above it, which is what makes it a loop — so it is routed round
  rather than drawn through the nodes it passes.

The layering itself, the loop detection and why the arithmetic is in Python rather than in
the browser are in [CANVAS_LAYOUT.md](CANVAS_LAYOUT.md).

---

# Why it exists

[TOOL_GRAPHS.md](TOOL_GRAPHS.md) draws graphs it did not author: `tool_graph_service`
derives them from `tool_config_links` every time the page opens, which is why that module
owns no table and states plainly that it has "no save path, no drag-to-move and no stored
position".

So the only graph a user could *build* was a nested tool chain, and that is authored
through repeating form rows in `nested_tools_field.htm` — a shape that expresses exactly
one idea: *the child's values restrict the parent*. There was no way to say

> run this SQL, loop over what it returns, ask me to confirm before the last step, and take
> a different path if nothing matched.

Every one of those is control flow, and control flow is a drawing.

---

# The node vocabulary

Sixteen types. The five that hold something, six that decide where the run goes, and five
that act on the world or on the clock.

| type | ports out | holds |
|---|---|---|
| `start` | `default` | nothing — it says where the run begins |
| `sql` | `default`, `error` | a statement, its datasource, **the tables it reads**, declared parameters |
| `sql_union` | `default`, `execute`, `error` | the same, appended once per pass and run on the last |
| `value` | `default` | JSON: a `list`, an `array` or a `dict` |
| `tool_config` | `default`, `error` | an existing tool config, run exactly as an agent would |
| `human` | `default` | a question, and what kind of answer it expects |
| `branch` | one per condition, plus `else` | an ordered list of comparisons |
| `for_each` | `body`, `done` | which node's result to loop over, and a ceiling |
| `do_until` | `body`, `done` | a condition to repeat until, and a ceiling |
| `email` | `default`, `error` | a template, a server, recipients, and a value per declared variable |
| `create_file` | `default`, `error` | which node's rows to write, in which format, called what |
| `download_file` | `default`, `error` | which `create_file` node's file to hand over |
| `timer` | `default` | one of *start / pause / resume / stop*, and which timer |
| `wait` | `default` | a number of seconds to pause the run for |
| `success` | `default` | a message; records the run as having worked |
| `failure` | `default` | a message; records the run as having failed |

**The two outcome nodes have an exit.** They used to have none, on the reasoning that they
end the run — but what they actually do is *decide what the run is reported as*, and that is
the moment there is finally something worth announcing. Announcing it takes a node (an
Email, a tool config, a statement that writes a row), so forbidding a successor forced the
author to put the announcement *before* the box that says what happened. On the failure side
especially, that reads backwards.

Leaving the exit connected to nothing is the ordinary case and ends the run, exactly as
before. What a successor cannot do is change the verdict: `failed_at` is already set by then
and nothing clears it, so **one outcome node may not lead to another** — a `success` drawn
after a `failure` would picture a green run that reports as failed. `_validate_edges`
refuses it by name, and the canvas refuses the gesture while the connector is still in your
hand.

**The vocabulary lives in one place.** `graph_service` owns it, the routes send it to the
browser as `#gdVocabulary`, and the canvas builds its palette and its property forms from
what it was sent. A palette offering a node type the service refuses is a form that can
only be filled in wrongly, so there is no second copy in JavaScript.

## The two file nodes

Implemented in `app/services/file_delivery/` and shared with the Flow Builder canvas, the
arrangement the Email node established: this package contributes two registry entries and
two validator calls, and everything about what a file *is* lives in that module. See
[FILE_NODES.md](FILE_NODES.md).

`create_file` writes an earlier node's rows — CSV, XLSX, TXT or Parquet — and puts
`file_uuid`, `file_name`, `file_path`, `file_format`, `row_count` and `byte_size` on its
output. `download_file` names one of those nodes and puts an **owner-only** `url` on its
output, which is what lets an Email node bind `{{LINK}}` to it and mail the file on.

Three things are specific to this canvas.

**The rows are already whole here.** A SQL node's output *is* every matching row —
`_run_sql` passes `max_rows=None` and nothing on that path caps — so there is no
preview-versus-total distinction to get wrong. The flow canvas's equivalent has to re-read a
graph run's result precisely because what a conversation can reach is a preview.

**There is no button, and the fields are refused rather than ignored.** A pipeline has no
visitor and no chat, so `_validate_download_file_node` says so by name if a graph arrives
carrying `show_button` or `button_text`. A setting an author chose and this application
silently dropped is worse than one that was never offered.

**Both nodes declare what they read**, in `node_runners.referenced_nodes` — a `create_file`
node's source node and the `create_file` node a `download_file` node names. Without that,
running a **tested selection** of just the file node would read nothing and fail inside the
runner claiming the upstream produced no rows, rather than saying the upstream was not
ticked.

`file_name` is the one field on either node that takes a `{{VARIABLE}}`, and it earns it: a
pipeline that runs nightly wants `orders-{{RUN_DATE}}` rather than one name every run
overwrites. The format is a picker's value and the node references are resolved by the
validator before any state exists, which is why neither is in `VARIABLE_FIELDS`.

## The three value kinds are not decoration

`list`, `array` and `dict` are all parsed from JSON. They exist separately because they
validate differently, and validating them alike lets a shape through that the node
downstream cannot use:

* `list` — a flat array of scalars. The shape an `IN` comparison takes, so this is the kind
  that can feed a SQL parameter directly.
* `array` — an array, nesting allowed. Rows, tuples, a matrix.
* `dict` — an object. Named values.

A `dict` where a `list` was promised would otherwise surface as an `IN` built from an
object.

## A SQL node must declare its tables

Not bureaucracy. Nothing in this application parses a raw statement — see
[TOOL_QUERY_MODES.md](TOOL_QUERY_MODES.md) — so `query_executor.require_active_tables` can
only honour the list the operator recorded. A node with no declared tables would run with
the active-table check silently skipped, which would make a graph a way **around** the Data
Sources switches rather than a way to use them.

`validated_tables` and `validated_tool_sql` are the tool config form's own validators, so
the same statement declares the same tables in both places and a read-only violation is
described in words the operator has already seen.

## Passing values into a statement

A statement writes `:name` where a value belongs, declares that name as a **parameter**, and
the parameter is filled from one of three places. Declarations use the same
`{param, type, required, description}` shape a SQL-mode tool config stores and go through
`tool_config_service.validated_sql_params`, so the name rules, the type coercion and the
refusal of a parameter the statement never mentions are all the code that already existed.

**Where a value comes from, first one wins:**

| Source | When |
|---|---|
| A **wiring** — an upstream node, optionally one field of it | The most specific thing available: the author drew that line about this parameter |
| The run's **inputs** | What the test panel supplied, or what a data agent passed when it called the graph as a tool |
| The **enclosing loop's item** | A parameter whose name is the loop's *item name* is filled with the item of the current pass, with nothing to wire |

The loop comes last so a wiring or an explicit input always overrides it. A parameter that is
declared and *not* wired is not a mistake — that is how a value reaches a graph from outside,
and `graph_tool_factory` builds an agent tool's arguments out of exactly those declarations.

**The item's value follows one rule everywhere.** A scalar item is itself; a single-column
row — `{"id": 7}`, which is what `SELECT id FROM departments` yields — is that column's
value, so the ordinary case needs no wiring at all; a row with several columns has no single
value and is **refused rather than guessed at**, because binding an arbitrary column would
filter the statement on the wrong thing and the result would look entirely normal. To use one
column of such a row, wire the parameter to the loop and name the column.

**Wiring a parameter to a loop means the item**, not the `{item, index, total}` envelope the
loop publishes. That envelope is right for a branch condition, which may well want to test
`index`; it is never what somebody putting a value into a statement meant. So the envelope is
unwrapped first, and a `field` on the binding therefore names a column of the *item*.

**A field the source has not got stops the pass and lists the fields it has.** This cannot be
checked when the graph is saved — nothing here knows a statement's columns — so it is checked
where the value is read, and it needs its own refusal because `_field_of` answers `None` both
for "no such key" and for "the key is there and null". That is right for a condition, where
both compare as empty, and wrong here: a parameter that resolves to nothing is *dropped*, so
what came back was `query_executor`'s "this tool needs a value for 'id' and none was given" —
a sentence about an input nobody supplied, when a line had been drawn and was reading the
wrong key. For an **optional** parameter it was worse than confusing: the filter left the
statement and the run succeeded over every row. The way to get there is a field left behind
from before — renaming a loop's item, or re-pointing the wiring, does not empty the box.

### One value, or the whole list

A wiring says which, and it is not a detail of binding — it decides what shape the statement
must be written in:

| Takes | Renders | Runs |
|---|---|---|
| `one` *(default)* | a plain scalar — `dept_id = :dept_id` | once |
| `in_list` | an expanding parameter — `dept_id IN :dept_ids` → `IN (?, ?, ?)` | once, for the whole list |

`in_list` is the alternative to a loop: one round trip instead of N. Both go to
`query_executor.assemble_sql_statement`, which already binds a `value_bindings` list and a
declared `sql_params` list together — nothing in that module changed.

**Written as `IN :name`, never `IN (:name)`.** An expanding parameter renders its own
parentheses, so the second is a syntax error. Getting it the wrong way round in either
direction is refused **when the graph is saved**, by a text check over the statement with
literals blanked (`sql_guard.placeholder_shape`) — the same check a nested tool config gets,
for the reason [TOOL_CHAIN_ITERATION.md](TOOL_CHAIN_ITERATION.md) gives: the alternative is a
syntax error the *database* reports, mid-run, long after the form that caused it was closed.

**`:name`, with no space after the colon.** `where id = : item` is refused at save time too,
and it is worth its own check rather than being left to the two above: the space hides the
placeholder from both of them, so the statement passes as one that uses no parameters at all,
saves clean, and then comes back from the database as `near ': item'`. It is also the mistake
an author makes specifically when there is nowhere to put a value — which is what the
parameters editor now is — so the refusal says to close the space and declare the name
(`sql_guard.spaced_placeholder`). It is not added to `validated_tool_sql`, which is also what
re-checks a *stored* statement before a tool runs: a rule there could stop an existing tool
mid-run rather than at a form, and that function's promise is that it checks a statement is a
single read and does not check syntax.

The statement is **never rewritten**. Values are bound, which is what keeps the guarantee
[TOOL_QUERY_MODES.md](TOOL_QUERY_MODES.md) makes about SQL mode true here too: what runs is
the text the operator approved and `validated_tool_sql` re-checked.

## Conditions are compared, never evaluated

Every operator is a name from a fixed table and the comparison happens in Python. There is
no expression language on this path and nothing reaches `eval`, so a graph cannot be used
to run arbitrary code even by its own author. Same decision as
`engine_service._evaluate_condition`.

`0` and `False` are **not** empty. A SQL node returning a count of zero has produced a real
answer, and treating it as empty would send a graph down its nothing-found path when the
thing it found was zero — which is why `_is_empty` is a function rather than `not value`.

---

# Variables on any node

What a node produced has always been readable by the node after it — that is
`GraphState.outputs`, keyed by node id. What was missing was a way to write it *into*
something. A node can now declare named variables and use them as `{{NAME}}` in its own
text: a table name in a statement, a duration in a Success message, a figure in the
question a Human node asks.

```jsonc
"data": {
  "sql_query": "SELECT region, SUM(total) FROM {{TABLE}} GROUP BY region",
  "variables": {
    "TABLE":  {"source": "node", "node_id": "n_3", "path": "rows[0].table_name"},
    "REGION": {"source": "node", "node_id": "n_3", "path": "region", "default": "unknown"},
    "LIMIT":  {"source": "literal", "value": "50"}
  }
}
```

**This is substitution, not evaluation.** There is no expression language, no filters, no
conditionals, and nothing reaches `eval`. `{{NAME}}` is replaced by a value a structured
binding resolved, and everything else in the text is text. That rule is now stated in four
places — `email_dispatch/variable_sources.py`, `email_dispatch/rendering.py`,
`integrations/engine/transform.py` and `graph_designer/node_variables.py` — and the
machinery is shared rather than rebuilt: the same renderer, the same binding resolver, the
same restricted path reader the Email node already used.

## Which fields, and which do not

| node type | fields | treated as |
|---|---|---|
| `sql`, `sql_union` | the statement, each table name | SQL — see below |
| `value` | the JSON | escaped as a JSON string body first |
| `human` | the question, each choice | text |
| `success`, `failure` | the message | text |
| everything else | — | none |

Deliberately absent: any `*_id` (a picker's uuid, not prose); `source_node`,
`collect_from` and `timer_node` (node ids the validator resolves against the drawing);
`item_name` and `label_item_as` (read at compile time, before any state exists to
substitute from); `params` and `bindings` (the typed parameter system, which is safer than
interpolation and which this must not give anyone a reason to abandon); and everything on
an Email node, whose subject, body and recipients are rendered by the email module against
the *template's* declaration — a pass here would eat `{{CUSTOMER}}` before that renderer
ever saw it.

**A `branch`'s and a `do_until`'s condition values are excluded for a sharper reason.**
`branch_port` and `loop_continues` are each called twice per visit — once by the runner and
once by the compiler's router, after the node's own output has merged — and they are one
function precisely so the port in the log and the port the run takes cannot differ. A
rendered condition could resolve differently between those two calls. A condition already
has a typed way to read another node.

## A variable with no value

`resolve_bindings` leaves a binding that found nothing *out* rather than blanking it, so
something has to decide what that means. An email defers to its template's declared
default; a graph node has no template, so the author decides per variable: **fail and name
the variable**, or **use this default**. The presence of the `default` key is the switch,
which is why the panel offers a select rather than a text box — an empty default and no
default are different instructions.

## SQL and variables — the honest part

A statement has always been raw author-authored text. What changes is that a `{{VAR}}` can
carry a value out of **a database row nobody reviewed**, and putting one into statement
text is string-concatenated SQL by another name. Four things stand in the way:

1. **A placeholder may not sit inside a string literal or a comment** (refused at save,
   using `sql_guard.stripped_literals`). This is what gives the feature a reason to exist
   *and* takes away its reason to be dangerous: a bind parameter already expresses every
   **value** and the driver binds it, so it cannot change what the statement does. The one
   thing a bind parameter cannot express is an **identifier** — no driver lets `FROM :table`
   name a table. So `{{VAR}}` is for identifiers, and one found inside quotes is somebody
   reaching for it to do a value's job.
2. **A substituted value must be a name or a whole number** (refused at run time): a dotted
   identifier up to three parts, or up to 18 digits. That refuses a space, a quote, a
   semicolon, a parenthesis, the empty string, and the `…` the resolver appends when it
   truncates an over-long value. Applied to fixed values too — one rule is easier to reason
   about than two, and an author who needs something exotic can type it into the statement
   where a reviewer can see it.
3. **The statement is re-validated after substitution.** `_run_sql` runs
   `validated_tool_sql` over the node it was handed, which is the rendered one — so a
   second statement or a write verb is still refused. This is the net, not the fence.
4. **The table allow-list still binds.** A substituted table name must still appear in
   `table_names`, and a table switched off in Data Sources still stops the node.

Two consequences worth knowing: a value that renders a `:something` into the text becomes
an unbound placeholder and the driver errors (readably, as a failed step); and a variable
named `UPDATE` or `DELETE` is refused at save by the write-verb check, so name it
something else.

## The drawing is never edited

`render_node` returns a copy. That is load-bearing rather than tidy: the compiler captures
each node in a closure once per run, and a loop body re-enters the *same* closure on every
pass. Writing the rendered text back would bake the first pass's values in, and every later
pass would substitute into text that had already been substituted.

---

# Measuring how long something took

A `timer` is a stopwatch. One node type carrying four actions — **start**, **pause**,
**resume**, **stop** — rather than four node types, because a timer is a single state
machine and four palette boxes sharing one machine is four validators to keep in step.

The instance set to *start* **is** the timer; the other three name its node id. A node id
rather than a typed-in name so that a save can prove the reference is real and
`referenced_nodes` can report the dependency — a free-text name could do neither, and a
typo would surface at run time as "that timer has not been started", which is
indistinguishable from a branch the run did not take.

Every action records `datetime.now(timezone.utc)`. Nothing here waits.

## Where the record lives, and why not in `outputs`

The shared record goes in a `timers` channel on the state, keyed by the *starting* node's
id. Not in `outputs`, for two reasons. A pause writing the shared record into `outputs`
would have to write it under the **start** node's key — misreporting what that node
produced, since a step row's `output_preview` is built from exactly that key. And
`_merge` is last-write-wins per key, so inside a loop each instance would keep only its
final visit and the segment history would vanish silently.

Each box still writes its own **snapshot** into `outputs` under its own id, and that
snapshot is deliberately **flat** — a downstream binding reads it with a dotted path, so
`elapsed_human` has to be one hop from the top or an email's variable row would depend on
this module's internals.

```jsonc
{"started_at": "...", "ended_at": "...", "elapsed_seconds": 3852.117,
 "total_elapsed_seconds": 3852.117, "paused_seconds": 40.0,
 "elapsed_human": "1h 4m 12s", "running": false, "phase": "stopped",
 "action": "stop", "restarts": 0, "segments": [...]}
```

`elapsed_human` earns its place because the obvious binding renders `3852.117` into a
sentence. Formatting the *datetimes* for a reader is deliberately not done — that needs a
timezone and a locale, which are the reader's, not the run's.

## The maths

`segments` are the spans the clock was **ticking**, not the paused ones. One list, one
definition, and the paused time falls out as arithmetic rather than needing a second list
that could disagree with the first:

```
elapsed = Σ(segments)                      # this pass
wall    = (ended_at or now) − started_at
paused  = max(0, wall − elapsed)
total   = carried + elapsed                # across loop restarts
```

`max(0, …)` because `wall` and `elapsed` are independent subtractions of the same clock and
can disagree in the last microsecond on a timer that was never paused.

## Inside a loop

A second *start* means different things in different places, and the difference is a
compile-time fact rather than a guess: `enclosing_loop` is set by the compiler, which is
the only thing that knows the drawing's nesting.

* **Inside a loop body** — this pass begins now. The timer restarts, folding what earlier
  passes measured into `carried_seconds`, so `elapsed_seconds` answers "how long did *this*
  pass take" and `total_elapsed_seconds` answers "how long has the loop spent in here
  altogether". Reporting only one would make the other unobtainable.
* **Anywhere else** — the author has started a timer twice, which has no sensible reading,
  and it is refused.

Every other illegal transition is refused too: pausing a paused timer, resuming one that is
running, stopping one twice, and acting on one that was never started. None of them is a
quiet no-op, because a timer that silently ignored a Stop would report a number that looks
plausible and is wrong.

## What the save can and cannot prove

It refuses a pause/resume/stop whose *start* cannot reach it by any path — that is
provably dead, since no run could have started the timer before arriving. **Reachability
proves impossible, never certain**: a branch whose other port also leads there means a run
can still arrive without passing the start, and that is left to run time, where the message
names the branch as the likely reason.

It also refuses a pause/resume/stop **inside** a loop body whose start sits outside it.
That combination goes green on pass one and red on pass two — the worst failure mode
available — and it is statically detectable with the same body definition the collection
rule uses.

It does **not** require a stop for every start. A graph that only wants to know when
something began is a legitimate graph.

---

# Waiting

A `wait` node pauses the run for a fixed number of seconds. **It does not survive a
restart.** `stop_all_runs` cancels every live run on shutdown, so a deploy landing inside a
wait leaves the run cancelled and nothing resumes it. The panel says so, the node box says
so, and it is the first thing to know about the node.

That is the price of not building a second scheduler. Parking and resuming would need a new
run status across eight consumers, a migration, a scheduler loop in this package, and —
the hard part — LangGraph's only park mechanism here is `interrupt()`, so a parked wait
becomes a Human node that answers itself. For a node whose common case is thirty seconds.
Anything measured in hours belongs in an Integrations schedule, which *is* persisted.

The ceiling is **900 seconds**, chosen against three other numbers: under
`MAX_STREAM_SECONDS` (3600) so the dock's stream outlives any wait; under the integrations
engine's node timeout (3600), keeping this canvas the more conservative of the two; and
short enough that a deploy rarely lands inside one. Longer is **refused at save, never
clamped** — clamping would leave the drawing saying two hours while the run waited fifteen
minutes, and a picture that lies about the run is worse than a refusal that explains
itself. The duration is re-validated in the runner too, because `graph_data` is JSONB that
can be edited by hand and there is no `asyncio.wait_for` around a runner in this package.

One consequence for graphs called as an agent's tool: `graph_runner.WAIT_SECONDS` is 90, so
any wait beyond about a minute and a half makes the tool report "still being worked out".

## Stopping a run mid-node

`cancel_run` marks the row and then cancels the task. `CancelledError` derives from
`BaseException`, so it sails past `run_node`'s `except Exception` — which meant the step row
it had opened was never closed, leaving a node spinning forever in the dock underneath a run
the list already showed as cancelled. That was true of **every** node; a node that sleeps
merely makes it easy to hit rather than a matter of timing. `run_node` now closes the row
with "The run was stopped while this step was running." and re-raises untouched.

Known limitation, unchanged: `_RUNNING` is per-process, so `cancel_run` in one replica
cannot cancel a task in another. Already true of every long node here.

---

# What is refused, and why

Each of these produces a *plausible wrong run* rather than an obvious error, which is why
none is left until execution. `graph_service.validate_graph` is called by the save, by the
publish **and** by the run — a run that validated more loosely would be a run of a graph its
author could not have stored.

| Refused | Because |
|---|---|
| No start node, or two | a drawing has no reading order, so "where does it begin" cannot be inferred, and two starts is two graphs |
| An edge naming a node that is not there | the compiled graph would have a dangling transition and the run would stop somewhere nobody drew |
| Two edges on one output port | the run would take one of them, and which one would depend on dict ordering |
| An edge into `start` | a lie about the compiled graph: START has no inbound edge |
| An edge from one outcome node to the other | the second cannot undo the first, so the picture would promise a verdict the run does not report. An edge *out of* an outcome node to anything else is allowed — see [the node vocabulary](#the-node-vocabulary) |
| **A cycle that no loop node sits on** | see below |
| A `value` whose JSON does not match its kind | a `dict` where a list was promised feeds a downstream `IN` nothing usable |
| A `sql` node with no statement, no datasource, no tables, or one that is not a single read | the guarantee above |
| A `branch` with no conditions, a duplicate outcome, or one named `else` | an unreachable port, an undefined overlap, or a fall-through that can never be taken |
| A loop whose source is not in the graph, or a `do_until` with no condition | a loop over a value that does not exist, or one with no way out |
| A loop ceiling below 1 or above 100,000 | a stray zero |
| A `human` node with no question, or a `choice` with no choices | a run that pauses silently, or a prompt with no answers |
| A `:name` in a statement that nothing declares | a binding fills a parameter and a parameter is what a declaration creates, so it is bound by nothing — and the driver's complaint arrives mid-run naming nothing the author would recognise |
| `= :x` wired to a list, or `IN :x` wired to one value | an expanding parameter always renders parenthesised, so one is `= (?, ?, ?)` and the other is `IN ?`. Both are syntax errors the *database* reports, long after the form was closed |
| A wiring for a parameter the node does not declare, or to a node not in the graph | a value with nowhere to go, or from nowhere |
| A loop with nothing on its `each` output, or a body that never comes back to it | the run would take one pass and carry on as though the loop had finished — see [the cycle rule](#and-the-same-rule-from-the-other-side) |
| A `for_each` collecting a node outside its own body, or itself | that node ran once, so every pass would collect the same rows again |
| A `do_until` that collects | only a loop that knows which pass is its last can publish a union — see [Unioning the passes](#unioning-the-passes) |
| `Record the item as` with nothing collected, or not a plain column name | a field that silently does nothing is one the operator will swear they set; and it becomes a key in the result rows |
| A `{{NAME}}` nothing on the node declares | it would render as itself into a statement, a question or a message — or, if it were emptied instead, turn `FROM {{TABLE}}` into `FROM ` |
| A `{{NAME}}` inside quotes or a comment in a statement | a value belongs in a `:parameter`, which the driver binds and which therefore cannot change what the statement does — see [SQL and variables](#sql-and-variables--the-honest-part) |
| A variable on an Email node | its variables come from the template it sends, and a second pass would consume them before the email renderer saw them |
| A variable bound to `session`, `agent`, `record` or `event` | a graph has no chat session, no agent and no record in hand. Refused **by name** rather than resolving to blank |
| A variable bound to a node that is not in the graph, or with no node chosen | a value from nowhere |
| More than 30 variables on one node | a node needing more named values than that is describing a data structure, and the `value` node already exists for it |
| A Timer with no action, or a Start that also points at a timer | a Start *is* the timer, so a reference on it draws a link that does nothing |
| A Pause/Resume/Stop naming no timer, one not in the graph, or one that is not a Start | there would be nothing to act on, and the run-time failure looks identical to a branch not taken |
| A Pause/Resume/Stop its Start cannot reach | provably dead: no run could have started the timer before arriving |
| A Pause/Resume/Stop inside a loop whose Start is outside it | pass one is green and pass two finds the timer already finished — the worst failure mode available |
| A Wait of less than a second, more than 900, or not a number | see [Waiting](#waiting). Refused rather than clamped, so the drawing cannot say one thing while the run does another |

**Not** refused: an Email node sending a template shared with a different workspace. A
template is *owned* by its user and merely shared with a team, so the owner's own graph may
always send it — the picker names the team on the row and nothing is blocked. An earlier pass
refused this and was wrong twice: it made sharing a template remove the owner's access to it,
and since attaching a graph to an agent and sharing it into a workspace are mutually
exclusive, it left every agent-attached graph unable to send any shared template.

Every message names the node **by its label**, because the person reading it is looking at
the drawing and a generated id like `n_msoez780_1` means nothing to them.

## The cycle rule

This is the rule worth the most explanation, because the obvious version is wrong in both
directions.

Banning cycles bans loops, and a loop is the thing the user asked to be able to draw.
Allowing them lets a plain `A → B → A` compile, and that run has no cursor and no ceiling —
it stops when LangGraph raises `GraphRecursionError`, which arrives as an internal error a
long way from the two edges that caused it.

So: **cut every edge out of a loop node's `body` port, and require what is left to be
acyclic.** A loop's back edge is the edge that closes its cycle, so removing the body edges
removes exactly the cycles a loop is responsible for bounding, and anything still cyclic
afterwards is a cycle nobody bounds. A cycle through a loop's `done` port is refused too —
`done` is the way *out*.

The walk is iterative with an explicit stack. A graph has no node ceiling, and a recursive
depth-first search would hit Python's recursion limit on a long chain.

### And the same rule from the other side

The check above refuses a cycle with no loop node in it. `_require_looping_bodies` refuses
the mirror image — **a loop node with no cycle around it** — and it is the more expensive of
the two to get wrong. Nothing about that drawing looks broken: the loop has a source, a
ceiling and a body, and the body is wired onward to Success. What the router does with it is
send the loop to its `body` port, run the body once, and then carry straight on, because
nothing leads back. One pass of eighty-two, and the run reports success. Nothing in the log
says the other eighty-one were never attempted, because as far as the graph is concerned
nobody asked for them.

So a loop must have something on its `each` output, and something in the body must have an
edge back to the loop. **Any** edge back counts, from any port: a body that returns down its
error path still iterates, and a branch inside the body needs only one of its ports to
return — refusing those would be inventing a rule about *how* a pass may end rather than
whether it ends.

This also explains a picker that looks empty for no reason. *Collect the result of* offers
only body nodes, and "in the body" means reachable from `each` **and** able to reach the loop
again — so before the back edge is drawn there is nothing to offer, and the empty list was
the first thing telling the truth about the drawing.

## There is no cap on nodes or edges

Deliberately. `FlowGraphSaveRequest` caps a conversation flow at 500 nodes because a flow
that large is a runaway client; a data pipeline is not. What bounds a **run** is the
per-loop iteration ceiling — a bound on *work* rather than on *drawing*, and the one that
actually protects anything. A 200-node graph is an explicit test case, and so is a
1,200-node chain through the cycle walk.

---

# The compiled graph

`graph_compiler` is the only module in the package that imports `langgraph`. Everything it
needs to make a decision lives next door and is testable without it — the same split
`tool_chain_service` / `tool_chain_graph` makes, and the reason
`pytest.importorskip("langgraph")` guards only one test file.

**Every node gets a conditional edge**, not a plain one — and since the outcome nodes gained
an exit, that is true with no exceptions at all. It is the central decision and what makes
the rest simple: one router per node answers one question — where does the run go from here —
and answers it for the ordinary case, the branch, the loop and the failure in the same place.
Mixing `add_edge` for "simple" nodes with `add_conditional_edges` for the rest would mean a
node that gained an error path had to change edge *kind*, and the failure path would be the
one that never got tested.

**The router asks about outcome nodes first**, ahead of both failure checks, and that
ordering is load-bearing rather than tidy. A `failure` node sets `failed_at` to *its own id*
as its entire job, which is indistinguishable — from inside the router — from a node whose
runner blew up. Asking the generic question first would send every `failure` node to `END`
and silently drop whatever the author drew after it: no error, no step row, nothing in the
log to look at. Pinned by
`test_a_failure_node_still_runs_what_the_author_drew_after_it`.

Removing the outcome nodes' old plain edge to `END` also fixed a quiet bug in **tested
selections**. Chaining sends each disconnected piece's dead ends into the next piece's entry,
and an outcome node with nothing after it is such a dead end — but `add_edge(node_id, END)`
ignores the chaining, so the second piece never ran, and because it *was* chosen it was not
marked skipped either. Its nodes were simply absent from the log with nothing to explain
them.

```
START → start ─→ sql ─→ for_each ──body──→ tool_config ─┐
                  │        │                            │
                error      └──done──→ success            └──→ (back to for_each)
                  ↓
               failure
```

## How a failure travels — two channels, not one flag

A runner raises `NodeFailure`. The wrapper catches it and writes **one of two things**, and
which one depends on whether the author drew an error path for that node — a fact known at
compile time, so the wrapper is told it rather than working it out:

* an error path exists → `errors[node_id]`, and the router takes it. **The run is not
  marked failed**, because the author said what to do about it.
* no error path → `failed_at` / `failure_message`, and the router ends the run.

Two channels because "this node failed and we handled it" and "this run failed" are
different facts, and one flag cannot hold both. With a single flag, a graph that recovered
from a failed node would still report the whole run as failed — the opposite of what drawing
a recovery path means. Both halves are asserted, on the same broken node.

## Loops

A loop node is entered once from the START side and then re-entered by its own back edge.
Its runner tells the two apart by `started` on the cursor, which is why loading the list and
advancing it are one function rather than two nodes: the drawing has one box there, so the
compiled graph has one node there.

**A ceiling refuses rather than truncating.** Rows from the first two of three departments
are indistinguishable from rows for all three, and a total taken over them is a plausible
number that is wrong — the argument `MAX_CHAIN_ITERATIONS` is written about. The run stops
and names the node.

`recursion_limit` is **computed** from the drawing, not left at LangGraph's default of 25 —
which would stop a valid loop over 30 rows. Same mistake `download_graph._RECURSION_LIMIT`
documents.

## Unioning the passes

Without collection, a loop body's output is overwritten every pass: `outputs` is keyed by node
id, so after the loop only the last pass survives. A `for_each` can therefore be told to
**collect** one node from its body, and every pass's rows are put together.

Two optional fields on the loop:

* **Collect the result of** — a node inside this loop's body. Blank means collect nothing,
  which is what every loop drawn before this existed did.
* **Record the item as** — a column added to each collected row, holding the item that
  produced it. Optional, because a statement that already returns the value needs no second
  copy; asking for one anyway is **refused as a column collision** by
  `query_executor.labelled_rows` rather than one value quietly replacing the other.

**The loop's own output becomes the union once the loop is over.** While it turns,
`outputs[loop]` is the item envelope; on the visit that finds the cursor spent — the one that
routes to `done` — it is the collected rows. So a node after `done` wired to the loop reads
every pass, and reads them *as rows*, which means `rows_of`, a downstream loop, a branch
condition, a parameter binding and the dock's preview all work with nothing further arranged.
The body can never see the swap, because the body does not run again once the cursor is spent.

The collecting happens **on the way round**: the body runs after the loop node, so the rows of
pass *k* are in the state when the loop is re-entered for pass *k+1*. That is why no extra node
or port is needed at the end of the body.

**Only a node inside the body may be collected.** One outside it ran once, before the loop, and
its output does not change while the loop turns — so every pass would append the same rows, and
a union of duplicates looks exactly like a union. "Inside the body" means reachable from the
`body` port *and* able to reach the loop again, minus the loop itself; the picker offers exactly
that set rather than offering everything and letting the save refuse.

**There is no row cap on the union**, for the same reason a SQL node has none — see
[A node returns every row](#a-node-returns-every-row-only-the-log-is-capped). What is still
true is that a union is only ever *whole* or *refused*: nothing truncates one and reports
success, because a union short of its last passes is short of whole **passes** — four
departments missing, not four employees — and no row count says so.

[TOOL_CHAIN_ITERATION.md](TOOL_CHAIN_ITERATION.md) is the same feature one layer down: a nested
tool config with an iterating link runs its parent once per value and concatenates the rows,
and this reuses its `labelled_rows` and its reasoning. Its cap it does **not** reuse, and the
difference is the audience — a nested tool's rows go into a prompt, a graph's go into its next
node.

**A `do_until` cannot collect**, and is refused when the graph is saved rather than accepting
the field and ignoring it. A loop can only publish its union on the visit it knows is its last.
For a `for_each` that is a fact about the cursor its runner holds; for a `do_until` it is
`loop_continues`' decision, taken by the compiler as a router *after* the runner returns.
Deciding it in the runner as well would put one decision in two places, and the pass where the
two disagreed is the pass whose rows go missing.

## Building one statement instead of many

A **Union** node is a SQL node that does not run when it is reached. It sits in a `for_each`
body, adds one copy of its statement per pass, and on the pass the loop hands it the last item
it runs the whole thing as a **single query** and leaves by its `execute` output instead of
going round again.

```
select id from departments        82 ids
        │
   ┌────▼─────┐  each   ┌───────────────┐
   │ For each  ├────────►│ Union         │  next ──┐  appends fragment 1…81
   │           │◄────────┤               │         │
   └────┬──────┘         │  execute ─────┼──► runs fragments 1…82 as one query
        │ done           └───────────────┘
        ▼
   (only reached when the list is empty)
```

### Which of the three unions

They look interchangeable in the palette and are not:

| | Queries | Bounded by | Reach for it when |
|---|---|---|---|
| `IN :ids` on one SQL node | 1 | nothing | every item goes into the *same* comparison |
| A loop's **Collect the result of** | N | nothing | the per-item statement differs and N round trips are acceptable |
| A **Union** node | 1 | `MAX_BUILT_SQL_LENGTH` — a cap on *text*, not on rows | the per-item statement differs and one round trip matters |

The first is almost always the right answer. A union of eighty-two copies of one shape is a
join written out longhand — `... from project_details pd join departments d on pd.departments
like concat('%s:departs:', d.id, '%')` is the same question in one short statement the planner
can work with. The union node is for the case where the fragments genuinely differ per item.

### Values are still bound

This is the only place in the application that **writes** SQL, so it is the only place that has
to say how it keeps the promise [TOOL_QUERY_MODES.md](TOOL_QUERY_MODES.md) makes. Each pass's
copy has its placeholders **renamed** — pass 7's `:id` becomes `:id__p7`
(`sql_guard.suffixed_placeholders`) — and pass 7's value is bound under that name. Eighty-two
fragments therefore carry eighty-two bind parameters and no value is ever rendered into text.
Concatenating the values instead would have been shorter and would have made every looped
statement an injection site; the test that holds the line gives one pass `') OR ('1'='1` and
asserts the answer is **nothing**.

The rename steps *over* string literals and comments rather than blanking them, which every
other reader in `sql_guard` can afford to do. That is load-bearing, not theoretical: the
statement this was built for holds `concat('%s:departs:', :id, '%')` — a colon inside a string.

A parameter the author names `id__p7` is **refused when the graph is saved**: it would collide
with a generated name, and one pass would be filled with another pass's value.

### Where the fragments live, and what the node shows

In the node's own `outputs` entry. `outputs` is merged per node id, so reading last pass's
entry and writing the extension needs no new state channel and survives the checkpointer like
everything else. While it accumulates, that entry **is** the statement under construction, so
the dock's per-pass previews are where the author watches it being written. On the executing
pass the entry is replaced by **the rows** — exactly as a collecting `for_each` replaces its
item envelope with the union on its last visit, and for the same reason: it makes the node after
`execute` an ordinary consumer of rows.

### Three rules, and why each one exists

**It must sit inside a `for_each` body.** Which pass of how many is a fact only a loop's cursor
has. Outside a loop there is no last pass, so the node would add one fragment, take `next`, and
the statement it built would never run — a silent nothing from a box that says it succeeded. A
`do_until` cannot host one either, for the reason it cannot collect: it learns which pass was
its last from the router, after the runner has returned.

**No `ORDER BY` or `LIMIT` in the fragment.** Unparenthesised, either one binds to the whole
union rather than to the member it was written on, so it would quietly sort or truncate every
pass at once. Parentheses would fix that on PostgreSQL and MySQL and are **invalid** around a
compound-select operand in SQLite — so it is refused on the form, with the message saying to
sort in a node after `execute`, rather than working on two databases out of three.

**The length ceiling refuses rather than truncating**, the same argument the row cap makes one
section up: a union short of its last fragments is short of whole *departments*.
`MAX_BUILT_SQL_LENGTH` is a separate number from `MAX_SQL_LENGTH` and not a raised one, because
8,000 is guarding against a pasted dump — text whose length is itself the evidence nobody wrote
it on purpose — and a union this application composed from an already-checked fragment is not
that. It reaches the driver through an optional `max_length` on `read_only_violation`,
`validated_tool_sql`, `assemble_sql_statement` and `execute_tool_query`, defaulted at every
step so no other caller's rule changes. Nothing else about the re-validation is relaxed: a
built statement is still one read, still has no second statement in it, still contains no write
verb.

**A failed query takes the `error` path, never `execute`.** The router asks about the failure
before it asks about the union, so a query that did not run cannot leave by the port that means
"here are the rows".

`union_executes` answers "is this the pass that runs it" for both the runner and the router —
the arrangement `branch_port` and `loop_continues` already have, so the log cannot describe a
route the run did not take.

## Human in the loop

`interrupt()`, and the run's state goes to the checkpointer. Two things about it are
load-bearing:

**The pause is in the compiler, the handling is in the runner.** `interrupt()` unwinds the
whole call, and on resume LangGraph **re-runs the node from the top** — so anything before
the interrupt happens twice. The step row is written after it, which is what keeps the log
from showing the question asked twice. There is a test asserting the human node writes *no*
step until it has an answer.

**The pause is read off `ainvoke`'s result, not caught.** LangGraph reports it under
`__interrupt__`; `graph_compiler.interrupt_payload` reads it defensively, the same way
`download_graph._interrupt_payload` does and for the same reason — that key's shape is
langgraph's to change.

The answer is validated **before** the run resumes, so an answer that does not fit is
refused while the person is still looking at the prompt. Resuming and failing a node three
steps later would be technically equivalent and much less useful.

---

# Running one, and testing part of one

**Testing a node, testing a group and running the whole graph are the same function** with a
different `scope`. That is the guarantee the feature rests on: a node that passes a test is
the node that will run — the same insistence [QUERY_TEST.md](QUERY_TEST.md) makes about the
query that is tested being the query that is saved.

A selection compiles as the **induced subgraph**. Choosing nodes that are not connected is
an ordinary thing to do ("does this query work, and does that one"), so the disconnected
pieces are chained in the drawing's topological order, worked out at compile time.

A node in the selection that reads a node *outside* it **fails, naming what is missing**. A
`for_each` over an absent list would otherwise loop zero times and report success — a green
tick on a test that tested nothing. Nodes left out get a `skipped` step row, because a node
missing from the log is indistinguishable from one the run never reached.

## Why a run is a background task

A graph queries somebody else's database and may pause on a question for as long as a person
takes to answer. A request holding the run would time out or hold a worker for minutes, and
a paused run has no request to belong to — the answer arrives in a *different* one. So the
request starts a task and returns a handle.

Same division `downloader_agents` makes between the request that offers an export and the
worker that builds it. It does **not** add a queue: an export is background work nobody is
waiting for, whereas a run is watched live by the person who pressed the button.

---

# The dock

Below the canvas, resizable, three tabs — the Azure Synapse / Fabric split:

* **Output** — one row per step: node, type, pass, status, duration, and the capped preview
  on expand.
* **State** — what the run knows after the selected step.
* **Log** — the timeline, one line per step.

Node status is painted back onto the canvas as frames arrive, which is what makes the page a
monitor rather than a diagram. A node inside a loop has one step row per pass and one box, so
the box shows its latest pass and the dock lists them all.

## Why the log is rows in a table

The task driving the run and the request streaming the dock are different tasks — and behind
more than one replica, different processes. So the nodes write step rows and the stream polls
them. `progress.py` makes the same argument for export progress: an in-memory bus works only
in the configuration this application is not guaranteed to run in, and a browser that
reconnects mid-run would see half the story.

**Every frame is a whole state, not a delta.** A client that missed one is not left with a
wrong picture, and the polling fallback consumes the same shape as the stream — so a dock
whose connection dropped does not have to understand a second payload.

## A node returns every row; only the log is capped

**There is no row cap on a SQL node or a Union node.** This is no longer a graph's exemption
from anything: nothing in the application caps a query any more — see
[TOOL_QUERY_MODES.md](TOOL_QUERY_MODES.md) — so a node and a tool config read alike. The nodes
still pass `max_rows=None` explicitly, because a node's guarantee about the operator's data
should not rest on a default that could be changed in another module.

**A `LIMIT` in the statement is the only thing that bounds a node**, which puts the size of the
answer where the author can see it. The statement is still never rewritten to add one.

Two consequences, stated because they are the cost of the above:

* the rows land in the run's `state`, so they are serialised to the checkpointer at every
  superstep — an unfiltered select over a large table is a large checkpoint;
* a collecting loop's union is unbounded too. It is still only ever *whole* or *refused* —
  nothing truncates it and reports success — but the refusal that used to fire past 200 rows is
  gone, because 200 was a number two passes could reach and a feature for putting every pass
  together cannot stop inside one.

A **`tool_config` node keeps the cap**, deliberately. Its whole purpose is to run an existing
tool exactly as an agent would, and that tool's 200 is part of what the tool *is* — a graph
should not be a way to make a tool behave differently from the tool.

### The log is a different question

`output_preview` and `state_preview` are capped by `graph_state.preview_of` **before** the
row is written, so it is a property of the table rather than of one renderer that has to
remember. Without it, a graph over a large query would put that result set into Postgres once
per node and once per loop iteration — a log that grows faster than the data it describes.

That cap is also what keeps an uncapped node safe for an agent: a graph called as a tool
reports through `result_preview`, so the model sees twenty sample rows and the real total
however many rows the node fetched.

A preview always states the **real** count, not the sample size. A dock showing twenty rows
and saying "20" when there were two thousand is the class of quietly-wrong number
[DOWNLOADER_AGENTS.md](DOWNLOADER_AGENTS.md) was written about.

## The stream, and the three things about EventSource

`static/js/graph_designer.js` honours the rules `deep_agent_stream.js` documents, each of
which has bitten this codebase:

1. the browser **reconnects** to a stream that ended, so `close()` runs before anything that
   can throw;
2. **every** close arrives as an `error` event with no data — success included — so a
   `finished` flag is the only way to tell an expected end from a dropped connection;
3. a server-sent `error` *with* data carries a sentence meant for the operator.

And one this feature added: the server names every frame after the run's status, and **a
named SSE event does not reach `onmessage`** — which fires only for unnamed `message`
frames. Listening on `onmessage` alone is a dock that never moves while the run completes
perfectly well behind it. `FRAME_EVENTS` is the list; `_event_name` in the route is its other
half.

When the stream drops mid-run the dock falls back to polling `…/runs/{id}`, warning the
console **once** rather than every tick — as the download card does.

---

# The shared canvas core

`static/js/graph_canvas.js` holds the stateless half of a node-graph editor: the Bezier
maths, the right-angle maths, the rectangle maths a selection box needs, the port
measurement, the escaping, and the id generator. Three canvases use it — this one, the Flow
Builder's, and the Integrations workflow canvas.

Nothing stateful moved. The node registry, the properties panel, the palette and save/load
are per-feature, because a conversation flow's nodes and a data pipeline's nodes have nothing
in common beyond being boxes joined by lines. What they share is the lines.

Every function there is pure or measures the DOM it is handed — none reads a module-level
`wrapperEl`, a `state` object or an id prefix. That is what makes one copy safe for three
canvases with different markup, ids and CSS.

**The selection model used to be on that per-feature list, and is not any more.** Worth
stating rather than quietly dropping: selecting several things and moving them together is now
shared, but from a second module rather than this one. `static/js/graph_selection.js` holds
it, because a gesture *is* state — where the press began, what was selected before it, which
frame is pending — and holding that in `graph_canvas.js` would break every promise the
paragraph above makes. So the line moved rather than blurred: **the geometry and the gesture
are shared; what a selection means is not.** See `documentations/CANVAS_SELECTION.md`.

`graph_canvas.js` must load **before** `graph_selection.js`, which must load before any
feature's script. Every template does that and a route test on each of the three asserts it —
which matters because getting it wrong produces a blank canvas and one `undefined` in the
console, with nothing in any server log.

**Verifying the extraction.** There is no JavaScript test harness in this repository, so the
move was checked two ways: the shared functions were compared against the pre-extraction
arithmetic copied verbatim out of git (83 assertions, all identical), and both canvas pages
were then driven in headless Chromium — add a node, draw a connector, delete it, drag a node
and watch the connector follow, save — with no page errors and no console errors. One real
bug was found by that: two id generators created in the same millisecond both started at 1
and minted identical ids, so the counter is now module-wide.

---

# The node box

## Why the header height is pinned

A node's outgoing ports are absolutely positioned down its right edge, and each carries a
label — `each` / `done` on a loop, `else` on a branch. Those labels are **opaque** on
purpose: a connector passing behind one would otherwise be unreadable. That makes their
position load-bearing rather than cosmetic, and it is the reason for three rules that look
fussy in isolation:

1. `.gd-node` declares `--gd-header-h`, and `.gd-node-header` is given exactly that height
   rather than being allowed to size to its content. The port stack is offset from that
   variable, so the header's height has to be a number something else can read.
2. The stack starts at `calc(var(--gd-header-h) + var(--gd-port-gap))` — **below** the
   header, never across it. When it started near the top of the box instead, the first port's
   label sat on top of the Settings and Delete buttons and swallowed the clicks meant for
   them. The buttons were painted; they were simply unreachable, which is the worst version
   of that bug.
3. `renderNode` then grows the node's `min-height` so the stack fits inside the box. The
   height is **measured** from the rendered stack, not computed from the port count: a row is
   as tall as its label (~15px), not as tall as its dot (12px), so arithmetic over
   `--gd-port-size` comes out a few pixels short per row — enough that a branch with four
   conditions hung its last port below the box.

The incoming port is centred on the body for the same reason, at
`calc(50% + var(--gd-header-h) / 2)`.

The invariant worth keeping: **every node's header is fully visible and clickable, whatever
its type and however many ports it has.** It was checked by hit-testing —
`document.elementFromPoint` at the centre of both header buttons, on one node of all ten
types, with the branch node pushed to four ports — rather than by looking at the page, since
the failure mode here is a control that looks present and is not.

## Joining two nodes

Two gestures for *making* one, both supported because users arrive expecting one or the other:

* **Drag** from an output port and release anywhere on the target node. A dashed rubber band
  follows the cursor, the node under it highlights, and releasing over empty canvas abandons
  the attempt rather than leaving something half-made.
* **Click** the output port, then click the target — its incoming dot, its body or its header.
  The canvas cursor becomes a crosshair while a connector is armed, so the mode is visible
  rather than remembered, and clicking empty canvas cancels.

One dot serves both. A press that travels less than `DRAG_THRESHOLD_PX` does nothing on
release, leaving the `click` that follows to arm the click-then-click gesture; a press that
travels further releases over another element and so produces no `click` on the port at all.
That is why the two cannot both fire for one press.

**A third gesture now lands on the connector itself: dragging the line bends it**, routing it
by hand round whatever is in the way. Same split and so no collision — a press on the wire
that travels less than the threshold does nothing on release, leaving the `click` to select
the connector; one that travels further routes it and swallows the trailing click. The grab
target is the invisible 16px twin of the line that already existed for hovering it.

What makes a press on a wire a *group move* instead of a bend is that the connector is part
of a **multi**-item selection, not merely the selected one. Clicking a wire selects it, so if
"selected" alone meant group-drag, the ordinary sequence of clicking a wire and then dragging
it would move two nodes rather than bending it.

Three suppressions keep the forgiving version honest:

* A node accepts a click as "I am the target" **only while a connector is armed**. Otherwise
  clicking a header just drags, as it always did.
* For one tick after a drag that actually moved a node, node clicks are ignored
  (`suppressNodeClick`). A mouseup always trails a click, and without this, dragging a node
  while a connector was armed would silently connect it.
* For one tick after a rubber-band selection or a group move, the canvas's own
  click-to-deselect is ignored. This one is load-bearing rather than defensive: a selection
  box begins and ends on the wrapper, so a `click` **will** fire there afterwards and clear
  the selection the box just made — without the suppression the whole feature appears to do
  nothing. The module and this file share one flag rather than keeping two, so they cannot
  drift.

This was originally click-only, and the only thing that accepted the click was the node's
**body** — so the incoming dot, the obvious place to aim at, was inert, and dragging did
nothing at all. Three of the four things a user would try failed silently, which reads as
"connectors don't work" rather than as "that is not the gesture". All ten gestures — the five
that must connect, the four that must not, and the save/reload round-trip — are checked by
counting the *change* in connectors, never the total: a graph that already had an edge would
otherwise let a refused gesture pass.

## The parameters editor derives `bindings`, it does not edit them

`bindings` is keyed by parameter *name*, and the name is a text box. The panel first tried to
keep the key in step as the box changed — carry the wiring across on rename, drop it on
remove — and that has to be right for every intermediate state a person types through. It was
not. Clearing the box to retype moved the wiring to the key `""`, and typing the name back
created a second entry beside it. The result was an orphan the panel could not show and
Remove could not reach, refused on save with *"wires a value into ':', which it does not
declare as a parameter"* — accurate, and impossible to act on from the form that caused it.

So the rows are the truth and `bindings` is **rebuilt from them** after every keystroke: a row
with no name or no source contributes nothing, a renamed row comes out under its new name, and
a removed row is gone. No key can outlive what it was named after. The rebuild also runs when
the panel opens, so a document whose keys had already drifted is healed by pressing Apply
rather than needing the JSON edited.

The general lesson, which is why this is written down: **a form that keeps two structures in
step is a form with a bug in it.** One structure, derived on read.

## Both panels open from the right

Both offcanvas panels — the palette and the node's properties — open from the **right**
(`offcanvas-end`). The application's own sidebar owns the left edge, so a panel sliding in
from there covers the navigation.

---

# Five things can run a published graph

A graph starts life as something you draw and run yourself. Once published, five other
things can run it, and the interesting part is that all five need the same three questions
answered — did it finish, did it stop to ask something, or did it fail — while only the
*wording* differs between them.

| Owner | How it is connected | What a pause means there |
|---|---|---|
| A **data agent**, as one of its tools | `tool_graphs.data_agent_id` | the model relays the question and calls `answer_<graph>` |
| Every agent in a **workspace** | `tool_graphs.workspace_id` | the same; the graph is simply reached by a different route |
| A **tool config**, as a nested child | `tool_config_links.child_graph_id` | the question becomes the *tool's* output, and answering finishes the tool |
| A **flow builder** step | a `run_graph` node | the turn ends with the question; the visitor's next message answers it |
| An **AI Fallback** node's knowledge base | `data.kb_pipeline_ids` | the odd one out — there is no visitor turn to hand a question to, so a pause is treated the same as a failure: omitted from the answer's context and logged, not resumed. See `ai_fallback_service._one_pipeline_text`. |

So the answering happens **once**, in `graph_runner`, as a `GraphOutcome` — and each owner
phrases it for its own audience. That module is where the decision that shapes all five is
written down:

> **A pause is an outcome, not an error.**

None of the five can treat it as a failure, because nothing failed; and none can ignore it,
because the rows they wanted do not exist yet — though the AI Fallback source above is the
one that cannot *wait* for it either, and settles for treating it the same as a failure.
Every other failure is likewise *returned* rather than raised, because each owner is
mid-something — a conversation turn, a parent tool's query, a flow — and raising would hand
somebody a 500 for a state that could have been explained.

| | |
|---|---|
| Running and classifying | `app/services/graph_designer/graph_runner.py` |
| Wording for a model | `app/services/graph_designer/graph_tool_factory.py` |
| Wording for a parent tool | `app/services/tool_configs/tool_chain_graph.py` — `describe_question` |
| Wording for a visitor | `app/services/flow_builder/engine_service.py` — `_step_run_graph` |

## Attached to one agent, or shared with a workspace

A graph is callable by a data agent when **both** switches are set: `is_active` and one of the
two attachments. Same rule `flow_service.get_active_flow` enforces for a conversation flow,
so a graph can be parked mid-edit without being detached, and a draft can sit attached while
it is finished. Attaching a draft is refused rather than accepted-and-ignored — a control
that appears to work and does nothing is worse than one that says no.

**The two attachments are mutually exclusive, and setting either clears the other.** Holding
both would hand the same graph to one agent twice — once as its own, once through its
workspace — and a model offered two identically named tools cannot choose between them.
Clearing rather than refusing is what the operator meant by pressing the second control.

The difference between them is who has to remember something:

* `data_agent_id` is **unique**, so one graph belongs to one agent and vice versa. Adding a
  new agent means attaching a graph to it.
* `workspace_id` is **not unique**, because a workspace is a team's shelf. Three shared
  graphs give each of its agents three more tools — and an agent added to the workspace next
  month inherits them with nobody attaching anything. That is the whole point of it.

`queries.fetch_agent_graphs` is the one place that knows both routes, and the workspace half
is a **correlated subquery** rather than a join for a reason worth copying: an agent in no
workspace reads `NULL`, and `workspace_id IS NULL` matched on both sides would hand every
unshared graph to every unassigned agent.

**One name per shelf.** A graph's tool name is derived from a name a person wrote, so
"Monthly revenue" and "monthly-revenue" are two permitted names that both become
`monthly_revenue`. Sharing the second is refused, quoting the first — because a model handed
two tools of one name has nothing to choose on, and nothing about the answer it gives would
say why. Checked from the destination, drafts included, since a colliding draft becomes a
live collision the moment somebody presses Publish.

### Both are set in one dialog, not in the row

```
GET  /graph-designer/{id}/edit-form   → the dialog's body
POST /graph-designer/{id}/update      → name + description + attachment, one submit
```

The library's *Callable by* column used to be two `<select>`s posting on `change`. That shape
has a defect built into it: the response is the **refreshed table**, so the control replaces
itself, and its state has to survive a round trip through the row template to be visible at
all. It did not — `get_graph_views` returned `agent_id: None` for every row and omitted
`workspace_id` — so an attachment saved correctly and every re-render reported it as unset.
From the outside that is indistinguishable from a picker that does nothing.

Two things changed, and the second one is the fix:

1. **The column is a statement, not a control.** It names whichever attachment is set, and
   the pickers moved into the row's **Edit** dialog with the name and the description. A form
   in a modal is not part of the table it refreshes, so the row it rebuilds is the only thing
   that has to be right, and a refusal has somewhere to land.
2. **`fetch_graphs_with_owner_names` selects the two uuids**, not only the two names. The
   identifier a form field needs and the label a row shows now come from the same joins, so
   they cannot disagree — and neither costs a query per row.

`update_graph` is **composed from** `rename_graph`, `attach_graph` and `share_graph` rather
than writing those columns itself, so every rule above still holds on the form's path. Three
decisions in it are worth knowing:

| | |
|---|---|
| The rename happens **before** the attachment | `attach_graph` checks the graph's name against the destination's shelf, and that check has to see the name just typed rather than the one being replaced |
| An **unchanged** attachment is not rewritten | unpublishing keeps an attachment, so a draft holding one is a real state; re-submitting it must not trip the draft refusal, or a parked graph could never be renamed |
| Both fields at once are **refused**, before anything is written | this is the one form that can carry both, and resolving it would silently discard one — refusing before the rename is what stops a rejected submit leaving a half-applied edit |

The two single-field endpoints (`/attach`, `/share`) are unchanged and remain the only write
paths for those columns. That is deliberate: the reason they were split — a request carrying
both would have one dropped — is preserved by `update_graph` refusing the pair rather than
choosing between them.

## One list feeds the prompt and the tools

[TOOL_CHAINING.md](TOOL_CHAINING.md) is explicit that the routing prompt and the callable
tool list are built from one list, because two lists can describe different sets. So a graph
becomes an **entry in the existing list**, marked `kind: "graph"`, and both consumers read it
from there. Three additive edits, each a no-op for a user with no graph:

| Where | What |
|---|---|
| `prompt_sync_service.collect_agent_tools` | appends the attached, published graph as one entry |
| `prompt_builder._describe_graph` | describes a graph entry instead of a tool config |
| `tool_factory.build_agent_tools` | dispatches such an entry to `graph_tool_factory` |

Because the entry carries `updated_at`, `is_prompt_stale` already invalidates the prompt when
a graph is edited — no new staleness path had to be written. And `find_unsupported_tools`
skips a graph entirely: it has no single datasource, so the relational check means nothing
for it and without the skip a graph would be reported as "not a relational datasource" on
every agent console.

### The cost of one list: every consumer must branch

One list is the right call — two could describe different sets — but it has a price worth
naming, because it was paid: **every reader of that list has to know both kinds exist.** A
graph entry has no `table_name`, no `datasource_name`, no `db_type` and no `datasource` row,
and its public identifier is `graph_uuid` rather than `uuid` (in an entry holding both a graph
and the agent it was collected for, a bare `uuid` would be ambiguous).

`deep_agent_service.get_agent_runtime_view` — the test console's payload — did not branch. It
read the four tool-config keys off every entry, so opening the console of an agent that could
call a graph raised `KeyError: 'uuid'`: the whole page, a 500, for no reason but the graph
being there. Now `_console_tool` shapes the row per kind, and the template describes the
drawing ("A designed graph, 4 nodes, and it can stop to ask a question") where a tool config
names its table.

**The tempting fix was the wrong one.** Defaulting the missing keys to `""` also stops the
crash, and produces a console row reading `in ()` — a *broken tool config*, sending the
operator to check a datasource that was never involved. A crash is a bad outcome; a plausible
wrong answer is a worse one. The tests assert the absence of those keys, not just the absence
of the exception.

Five consumers, and what each does with a graph entry:

| Consumer | With `kind: "graph"` |
|---|---|
| `prompt_builder` | `_describe_graph` — its own paragraph |
| `tool_factory.build_agent_tools` | dispatches to `graph_tool_factory`, plus an `answer_` companion |
| `tool_factory.find_unsupported_tools` | skipped: no datasource, no assembled query |
| `deep_agent_service.get_agent_runtime_view` | `_console_tool` — describes the drawing |
| `aggregate_service.readable_tools` | included when `allow_recursive_aggregate` is on — see below |

Anything added to that list later belongs in this table.

## Its whole result, read and filtered

```
tool_graphs.allow_recursive_aggregate    off by default (revision d5f1a9e2c437)
```

Switched on in the graph's Edit dialog, this lets an agent read **every** record the graph
produces and narrow or total them in polars — so the graph can answer a question it takes
no parameter for. "Revenue for the Python department in March" against a graph that returns
every department and every month is the case it exists for; see
[AGENT_RECURSIVE_DATAFRAMES.md](AGENT_RECURSIVE_DATAFRAMES.md).

Three things about it are specific to a graph rather than to a tool config:

* **`full_result`, never `outcome.rows`.** The latter is a twenty-row preview. Filtering a
  sample and reporting how many matched in it is a wrong number with nothing about it
  saying so — the same trap a tool config embedding a graph avoids the same way.
* **There is nothing to probe.** A tool config's columns come from a one-row fetch before
  anything is read; a drawing has no such thing, so the graph is run first and the plan is
  made against the columns its result actually has.
* **A graph that stops to ask is refused**, with the question quoted and the graph's own
  tool named. Resuming would mean holding a half-read result set across two turns, which is
  a second kind of state for a feature whose shape is "read it all now". The graph's own
  tool carries the pause; this one points at it.

The flag is deliberately **off by default**, and it means slightly more here than on a tool
config: a tool config is one statement, while a graph can be a loop over eighty-two
departments, so "run the whole thing and hold the result" is a larger promise to make on
an operator's behalf. `AGGREGATE_MAX_SOURCE_ROWS` still bounds it, and refuses rather than
truncating — naming the *graph's own query nodes* as the place to narrow, because telling
somebody to edit "the tool's filters" would send them to the wrong page.

A graph's human name becomes an identifier a model can address —
*Monthly revenue check* → `monthly_revenue_check` — because a name a model cannot address is
a tool it cannot use.

## A question, inside somebody's conversation

There is no dock in a conversation. So the graph runs to its `interrupt()`, and the payload's
question is returned **for the model to relay word for word**, with the run id; a companion
`answer_<graph>` tool resumes the parked thread on a later turn. That is
`start_export_offer` / `confirm_download` / `resume_export` with the nouns changed, and two
rules carry over unchanged:

* **the question is not paraphrased** — `offer_sentence`'s reason: a model rewording a
  question asks the user the wrong thing, and a paraphrase makes the next turn's answer
  unmatchable;
* **the run is parked on a persisted `thread_id`**, because the interrupt fires in one
  request and the answer arrives in another.

The answering tool is offered **only** when the graph contains a question node, so an agent
whose graph never pauses gets exactly one new tool.

**An answer that does not fit is not a tool failure.** It is the one failure on this path the
user can fix, so the model is told to ask again. Reporting it through the ordinary failure
wording would tell the model that nothing the user says can change it and that an operator
has to look at it — which is what it did before that branch existed.

## What the model is told about a result

The run's result is **the last node that produced data**, not simply the last node to run. A
graph almost always ends at a Success node whose output is `{"succeeded": true}`, so "the
last output" reports a graph that read two hundred rows as having returned nothing — observed
doing exactly that. A `human` node is deliberately not counted either: its output is an
answer supplied *to* the graph, and because it usually runs late, counting it would let a
yes/no shadow the rows a query read earlier.

Rows go through `query_executor.describe_result`, the same function every tool's rows go
through, so the caps, the labelling and the exact total are the ones
`prompt_builder._GROUNDING_RULES` was written against. A graph does not get its own
vocabulary for "here are some rows".

---

# What it does not do

* **No scheduling.** A run starts because somebody pressed a button or an agent called it.
* **No parallel branches.** Nodes run in sequence, for the reason
  [TOOL_CHAINING.md](TOOL_CHAINING.md) gives about siblings: the first failure ends the run,
  so running in order means the rest is never paid for.
* **No writes to the user's data.** Every SQL node goes through `validated_tool_sql`, so a
  statement is one read-only statement, checked again on every run.
* **No non-relational datasources**, the same refusal `query_executor` gives.
* **No queue.** A run is watched live, so there is nothing to gain from making runs wait
  behind each other. A run in flight when the process stops is cancelled, not resumed.
* **No editing from the read-only Tool Graphs page**, which still draws only what it derives.

---

# Testing

```bash
docker compose exec -T app python -m pytest tests/unit/services/graph_designer \
    tests/unit/routes/graph_designer tests/unit/schemas/graph_designer -q
```

Nothing that produces or consumes data is mocked. The datasource under every `sql` node test
is a real SQLite file and the checkpointer is real, because the interesting questions are
whether a loop iterates, whether a failure takes the error path and whether a paused run
resumes — and a mock would only prove the module calls what it calls.

Two fixtures in `tests/unit/services/graph_designer/conftest.py` are autouse and both would
be confusing to forget:

* `graph_sessions` redirects `run_store.open_session` at the test database. Every node and
  the poll loop open their own session, and in the container `DATABASE_URL` is the
  *development* database — the same trap `tests/conftest.background_sessions` documents.
* `graph_checkpointer` gives each test a fresh `InMemorySaver`. `AsyncPostgresSaver` holds an
  `asyncio.Lock` bound to the loop that created it, and pytest-asyncio gives each test its
  own loop.

A third cancels anything still in flight, so a run outliving its test cannot fail an
unrelated later one on a disposed engine.

The cases that carry the suite:

* **the cycle rule from both sides** — a plain `A → B → A` refused, the same shape through a
  `for_each` body accepted, and a cycle through `done` refused;
* **no cap** — a 200-node graph saved, a 1,200-node chain walked;
* **the two failure channels**, asserted on the same broken node: the run fails when nothing
  handles it and succeeds when something does, with the node's step `failed` either way;
* **a loop's ceiling refusing rather than truncating**, with *no* body pass recorded;
* **a `do_until` that never satisfies**, stopped by its ceiling rather than by the recursion
  limit;
* **zero is not empty** — a `COUNT(*)` of zero takes the found path;
* **a paused run resuming from its checkpoint**, and the human node writing exactly one step;
* **selection scope** — only the chosen nodes run, the rest are `skipped`, and a chosen node
  reading an omitted one fails naming it *in the log as well as on the run*;
* **an injection-shaped parameter matching nothing**, re-asserted here because a graph is a
  new way to reach the same executor;
* **the one-list guarantee** — every name in the routing prompt is a callable tool;
* **the escaping** — a `<script>` in a node label comes back escaped from the save endpoint.

The canvas itself has no Python coverage, so it was driven in headless Chromium: 12
assertions over adding a node, connecting, refusing a second connector on one port,
shift-picking for a test, switching dock tabs, saving, running, and the node statuses
painting. That run is what found the named-SSE-event bug.

The selection, the group move and the hand-routed connectors added later are covered the same
way — by hand, against a real drawing — with two exceptions that a Python test *can* hold:

* **`tests/unit/schemas/graph_designer/test_graph_designer_schemas.py`** pins the bend
  payload: bends survive a save round trip, four is inclusive, a fifth is refused, and a
  non-finite or out-of-range coordinate is refused with a sentence. That last one is the
  reason the validator exists — `NaN` satisfies every other rule in the schema layer and then
  makes PostgreSQL refuse the `jsonb`, so without it a hand-made request is a 500 with a stack
  trace instead of a 400 with an explanation.
* **the script-order test** in `tests/unit/routes/graph_designer/` now asserts three files
  rather than two: `graph_canvas.js` before `graph_selection.js` before `graph_designer.js`.
  It earns its place because getting that wrong produces a blank canvas and one `undefined`
  in the console, with nothing in any server log — and it is exactly the sort of thing an edit
  reorders without noticing.

The manual checklist, including the interactions most likely to regress (a box dragged
right-to-left, the trailing click that would undo a selection, a group clamped at the left
wall without deforming, and the absence of forced-layout warnings during a five-node drag) is
in [CANVAS_SELECTION.md](CANVAS_SELECTION.md) §7.

---

# Related

* [TOOL_GRAPHS.md](TOOL_GRAPHS.md) — the read-only page this one is reached from, and why it
  deliberately stores no position
* [TOOL_CHAINING.md](TOOL_CHAINING.md) — the other LangGraph in this application, the
  one-list rule, and why siblings are sequenced
* [TOOL_CHAIN_ITERATION.md](TOOL_CHAIN_ITERATION.md) — **the same idea one layer down**: a
  nested tool config that runs its parent once per value and concatenates the rows. The
  binding modes, `labelled_rows`, the row cap and the refuse-rather-than-truncate argument
  are that feature's, reused here rather than restated
* [FLOW_BUILDER.md](FLOW_BUILDER.md) — the other canvas, and the primitives now shared with it
* [CANVAS_SELECTION.md](CANVAS_SELECTION.md) — selecting several nodes and moving them as one,
  routing a connector by hand, and where the shared-versus-per-feature line moved to make that
  possible across three canvases
* [QUERY_TEST.md](QUERY_TEST.md) — the insistence that what is tested is what will run
* [DOWNLOADER_AGENTS.md](DOWNLOADER_AGENTS.md) — the `interrupt()` / resume pattern, the
  DB-backed progress stream, and the relative-URL rule
* [TOOL_QUERY_MODES.md](TOOL_QUERY_MODES.md) — why a SQL node must declare its tables
* [MIGRATIONS.md](MIGRATIONS.md) — `e4c9b7d05f31_add_graph_designer_tables`
* [SCHEMAS.md](SCHEMAS.md) — `GraphSaveRequest`, `GraphRunRequest`, `GraphRunView`
* [TESTING.md](TESTING.md) — the fixtures above
