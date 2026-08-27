# FLOW_BUILDER.md

Visual conversation-flow builder for the embeddable chatbot widget.

---

# What it is

A flow is a graph of nodes (JSONB, authored on a client-rendered canvas —
`static/js/flow_builder.js` — not server-templated partials) that decides what the chatbot
widget says at each turn of a live conversation.

The canvas's stateless primitives — the Bezier maths that draws a connector, the port
measurement, the HTML/CSS escaping and the id generator — live in
`static/js/graph_canvas.js`, shared with the Graph Designer's canvas (see
[GRAPH_DESIGNER.md](GRAPH_DESIGNER.md)). It must be loaded **before** `flow_builder.js`,
which reads `window.GraphCanvas` at module scope; `templates/flow_builder/canvas.htm` does
that. Nothing stateful is shared: `NODE_TYPES`, the properties panel, the option rows and
save/load are this feature's alone, because a conversation flow's nodes and a data
pipeline's nodes have nothing in common beyond being boxes joined by curves.

**Flows are owned by a user, not by a chatbot.** They are built standalone from the Flow
Builder page in the sidebar (`/flow-builder/`) and then *attached* to one agent from that
agent's settings page. Ownership is checked directly against `user_id`; no chatbot key appears
in any Flow Builder URL.

A **kind** and two independent switches decide what a flow does and whether it does it:

| | meaning | set where |
|---|---|---|
| `kind` | `agent` (a chatbot's conversation) vs. `generic` (a callable child) | Flow Builder list (Make Generic / Make Agent), and the New Flow modal |
| `is_active` | published vs. draft | Flow Builder list (Make Active / Make Draft) |
| `chatbot_key_id` | which agent runs it | the agent's settings page (Conversation Flow dropdown) |

`flow_service.get_active_flow` requires both switches, so a finished flow can be parked
without detaching it, and a draft can sit attached while it is being finished.

**The kind decides which of two disjoint lists a flow appears in**, and that is the whole
point of it:

* `get_attachable_flows` — agent-kind, active, unattached → an agent's dropdown.
* `callable_flow_choices` — generic-kind, active, not the flow being edited → a Run Flow
  block's list.

Before the kind existed the second list was "every published flow", which offered an agent's
live front door as a candidate child and offered a reusable child to an agent's dropdown. A
generic flow is **never attached at all**, which is why the two lists can no longer intersect
and why the row template reads `kind` before deciding what to put in its Attached Agent
column — for a generic flow, empty means *never*, not *not yet*.

That invariant is stated three times on purpose, at each place it can be broken and once in
the schema: `set_flow_kind` refuses to make an attached flow generic (naming the agent to
detach it from), `attach_flow` refuses to attach a generic one, and
`ck_chatbot_flows_generic_unattached` on the table means no fourth write path can undo
either. The two service refusals are the readable ones; the constraint is the guarantee.

**None of the three involves tools.** `attach_flow` checks ownership, the unique
`chatbot_key_id` slot, the kind and `is_active`, and nothing else — a flow is a conversation,
not a query, and a Send Message / Menu / AI Fallback graph reads a knowledge base rather than
a datasource. An operator can build and run a complete flow without ever opening Tool Configs.

---

# The in-app help page

`/flow-builder/help` (`templates/flow_builder/help.htm`) is this file written for the person
in front of the canvas: the same twelve blocks, the same ceilings and the same refusal
messages, said as *what to draw* and *what to fill in*. Nine worked scenarios — a greeting, a
collected name, a menu, a validated answer, a shared ending via `Goto`, an AI hand-over, a
`Run Graph` lookup, an email notification, and one whole triage flow — plus the limits in one
table and every validator refusal with its fix.

The third instance of a shape this codebase now has three times, after `tool_configs/help.htm`
and `graph_designer/help.htm`, and it copies their three decisions verbatim:

* **A route, not a link to the markdown.** A help page has to arrive inside the application's
  own layout, behind the same auth as the page it explains.
* **A literal path**, declared above `/{flow_id:uuid}/…` in the controller, so it can never be
  read as a uuid.
* **The whole body inside `{% raw %}`.** Here that is load-bearing in a way it is not on the
  other two: the page is full of `{{VARIABLE}}` samples somebody will copy, and one rendered
  to an empty string by this page's own template engine would teach syntax that does not
  exist. A route test asserts the samples arrive as written. (The reason has inverted since
  it was written — the page used to warn that message text did *not* interpolate, and now
  documents that it does — but the mechanism it protects is unchanged.)

Linked from **both** the library and the canvas, both `target="_blank"` — the canvas is where a
port or an operator actually needs explaining, and going back for it would mean leaving unsaved
work.

## The one place it deviates

GRAPH_DESIGNER.md recommends copying its completeness test, which walks the server-owned
`NODE_TYPES` tuple and requires every palette label to appear in the rendered page. Flow Builder
has no such tuple: the labels live in `static/js/flow_builder.js` and the server knows only
`flow_service._VALID_NODE_TYPES`, a set of bare type ids with no display names.

So the test pins a mapping instead — `_BLOCK_LABELS` in
`tests/unit/routes/flow_builder/test_flow_builder_routes.py` — and asserts its keys equal
`_VALID_NODE_TYPES` before checking each label appears. Adding a block type to the validator
fails the suite until both the mapping and the help page follow it. That is the same guarantee
by a weaker mechanism, and the honest way to close it would be to move the labels server-side
as the Graph Designer did.

---

# Where the flow stops, and what is behind it

`chatbot_turn_service._run_turn` gives an active flow first refusal on every turn. When the
visitor reaches a terminal point, `engine_service.advance_flow_session` reports
`AI_HANDOFF` and free AI answering takes over for the rest of the conversation.

That handover assumes there is something to hand over *to*, and one supported configuration
has nothing: a **flow-only chatbot** — `target_type == "agent"` (so no datasource of its own,
and no data profile to compute) whose data agent has no enabled tools (so
`deep_agent_service._prepared_turn` refuses it before a model is built). Both halves are
legitimate on their own; together they mean the flow *is* the chatbot's entire scope.

`_can_answer_off_flow` is the pre-check for exactly that, run only on the handoff turn:

| chatbot | can answer off-flow? |
|---|---|
| has a `datasource_id` | yes — the data-profile path always answers |
| agent-backed, agent has ≥1 enabled tool | yes |
| agent-backed, agent has no enabled tools | **no** — flow-only |

A flow-only chatbot answers the handoff turn with `_FLOW_ONLY_MESSAGE`, which describes its
real scope and points at the widget's restart control (the only route back into the flow).
It is logged as a **flow** turn, because no model ran — counting it as an AI turn would
overstate model usage in [CHATBOT_ANALYTICS.md](CHATBOT_ANALYTICS.md).

Checked *before* the AI path rather than left to fail inside it: the AI path can only report
that it cannot reach data, which for this chatbot describes a data source the operator never
attached.

## The streamed twin, and why it declines to stream

`_stream_as_agent` cannot raise once a stream has opened — the status code is already
sent — so it reports failures as `{"event": "error", ...}`, and the widget paints those
verbatim. That is right for a timeout or a rate limit and wrong for a **setup** refusal: a
toolless agent's message is written for the operator ("Add a tool for it in the Tool Configs
section"), and a published widget was showing it to members of the public.

So setup failures — and only setup failures — carry `"stage": "setup"`, meaning *nothing
ran*: no model built, no tool called, no token streamed. `stream_turn` turns that one case
into `{"event": "fallback", "reason": "agent_unavailable"}` and logs nothing; the widget's
existing `fallback` listener re-POSTs to `/message`, and the blocking path's degradation —
the flow-only sentence above, or `chatbot_reply_service._NO_FALLBACK_REPLY` — is what the
visitor sees.

Every other streamed error is still passed through and logged. Retrying one would re-run
work that already happened and bill the owner twice for a single question, which is why the
widget's own `error` listener refuses to re-POST.

---

# The Run Graph node

A flow step whose work is a whole [Graph Designer](GRAPH_DESIGNER.md) graph: a drawn
sequence of queries, loops, branches and checks, run as one node of the conversation.

It is the only node type whose work happens outside this feature, and the only one other
than Ask for Input, Menu and Dropdown that can **end a turn waiting for a reply** — which
is the interesting half of it.

| | |
|---|---|
| Node type | `run_graph`, with two output ports: `default` and `error` |
| Configuration | which published graph, and optionally a variable to keep the result count in |
| Runner | `engine_service._step_run_graph` |
| Resuming | `engine_service._answer_waiting_graph` |
| Parked run | `chatbot_flow_sessions.awaiting_graph_run` |

**Three outcomes, and each does something different.**

**It finished** → whatever it produced is stored under the variable name, if one was given,
and the flow hops on by `default`. Nothing is said to the visitor: a graph that read some
rows is a step in a conversation, not a message in it. Say something about it with a Send
Message block, using the variable.

**It stopped to ask something** → the turn ends with the operator's question, **word for
word**, and the run's id is parked on the session. The visitor's next message is the answer
to it: `advance_flow_session` checks `awaiting_graph_run` before anything else reads the
message, hands it to the paused run, and — if the graph then finishes — lets the ordinary
hop loop carry the turn on. That is what makes the pause invisible in the rest of the
conversation.

An answer that does not fit — "maybe" to a yes/no — asks **again** with the validator's own
sentence in front of it, and the run stays parked. It is ordinary input, not a fault, and
treating it as a failure would tell a visitor the conversation is broken when they need only
answer differently.

**It failed** → the `error` port if one is drawn, otherwise the flow signs off. Never a
silent hop to `default`: a flow carrying on as though a step had succeeded is how a visitor
gets told something that is not true.

## Two details worth stating

**The variable holds a count, not the rows.** A flow variable is text that gets interpolated
into a message and compared by an If/Else node, so what is useful there is *how many* —
"I found 12 matching orders", or a branch on whether there were any at all. Putting a result
set in one would produce a chat bubble containing JSON. The number is `total_rows`, the real
total rather than the length of a preview: telling a visitor "20" when there were 5,275 is
the failure this application keeps writing tests against.

**The graph runs as the chatbot's owner**, resolved from the chatbot key rather than taken
from the graph row — so a flow can only run a graph its own owner has, and the datasources
its nodes read are that person's. The visitor's captured variables are passed in as the
run's inputs, which is what lets a graph filter on something an Ask-for-Input node collected
earlier in the same conversation; a graph declares which of them it uses as parameters, so a
variable it did not ask for has nowhere to land.

---

# What an AI Fallback node leaves behind

The node has always **said** its answer and then forgotten it, which made one ordinary flow
undrawable: a Menu offering *"email me the data"* → AI Fallback → Send Email had nothing for
the email's bindings to read. `_step_ai_fallback` now stores the answer under the node's
optional `variable_name`, which is the same field Ask Input, Menu, Run Graph and Send Email
already have and goes through the same `_store_answer`.

| | |
|---|---|
| Node type | `ai_fallback`, one output port: `default` |
| Configuration | guardrails, prompt, context source, LLM choice, and optionally a variable to keep the answer in |
| Runner | `engine_service._step_ai_fallback` |
| Stored value | `engine_service._ai_answer_text(result)` |

**The answer is both sent and kept.** This node is the one place those are the same thing —
the visitor sees it, and the variable holds a copy for an Email node to bind to or an
If/Else to branch on. Keeping a copy is not a mode: the chat bubble is identical whether a
variable is named or not.

**The whole answer, not just `summary`.** An `AnalyticsResult` is a narrative, up to five
insights and possibly a table, and the visitor who picked "email me the data" meant the
figures — a variable holding only the narrative mails them a sentence about a table they
never received. So `_ai_answer_text` renders summary, then insights as `- ` bullets, then
the table as pipe-separated rows, in the order the widget draws them, so the email and the
chat bubble do not disagree about the same answer.

Plain text with newlines, never markup, for the reason `rendering.py` states: an email's
HTML body escapes every value it substitutes, so markup smuggled through a variable arrives
as visible tag soup. Formatting belongs in the template, where whoever reviews the template
can see it.

**A long table is capped at `_MAX_STORED_TABLE_ROWS` (20) and says so** with a
`(+N more rows)` line — the same honesty rule `_store_graph_result` applies to a preview
versus a real total. Email applies a second limit of its own: `MAX_VARIABLE_VALUE_LENGTH`
trims any single substituted value at 500 characters with an ellipsis.

**A failure stores nothing at all.** The variable stays *absent* rather than being set to the
error sentence, so `resolve_bindings` reports it MISSING and `render_message` fills it from
the template's declared default — or refuses the send if it was declared required, which
takes the Email node's `error` port. Storing the failure would mail a customer an internal
error dressed as an answer. An If/Else reads absent as `""`, so `not_empty` still means
"the AI managed to answer".

**The turn ends here**, so a Send Email node wired after this one runs on the visitor's
*next* message. That needs no special case — the variable lives on the session row, so it is
still there a turn later. A flow that wants the mail sent within the same turn puts the Email
node *before* the fallback, mailing something the conversation already collected.

---

# The Run Flow node — one flow calling another

A flow could call *out* to a Graph Designer pipeline and it could queue an email, but it
could not call **itself**. So anything a conversation does twice was drawn twice, and no flow
could hand a value back to a caller. Run Flow is Azure Data Factory's *Execute Pipeline*
activity for conversations: values in, the callee runs as an ordinary flow, named values out.

| | |
|---|---|
| Node type | `run_flow`, with two output ports: `default` and `error` |
| Configuration | which published **generic** flow, `inputs` (what to pass), `outputs` (what to keep, and under what name) |
| Runner | `engine_service._step_run_flow` |
| Call stack | `chatbot_flow_sessions.call_stack`, one frame per open call |
| Frame mechanics | `subflow_service` — `push` / `pop` / `guard` / `current_flow` |
| Discovery | `flow_service.flow_io` and `callable_flow_choices` |

## A call is a scope

**Which graph is being interpreted is re-decided on every hop**, from the call stack, and that
is the whole of how this works. `_step_run_flow` pushes a frame and points the session at the
callee's Start node; `_step_end` pops one; `_run_internal_hops` simply notices on its next
iteration that it is somewhere else. No other function in the engine has to know that more
than one flow exists — including `_step_ai_fallback`, which is handed the *current* flow's id
and so keeps resolving a sub-flow's own knowledge bases (keyed on flow id and node id)
correctly. Re-resolving is cheap: a per-turn cache means one query per distinct flow per turn.

**Each call gets its own variables.** On the way in, the caller's map goes into the frame and
the callee starts with the resolved inputs *and nothing else*; on the way out the caller's map
comes back with the named outputs merged in. That isolation is the feature rather than a
detail of it — without it a callee writes into its caller's namespace, two blocks calling the
same flow overwrite each other's answers, and a reusable flow's internal variable names become
part of its contract. The visible consequence: an Email block inside a sub-flow sees only that
sub-flow's variables, which is the same rule and not a special case.

**A frame, not a child session row.** `chatbot_flow_sessions` is unique on
`(chatbot_key_id, session_token)` — one row per visitor — and a sub-flow that can ask a
question has to survive between two HTTP requests. The `call_stack` JSONB column beside
`awaiting_graph_run` is the same kind of thing for the same reason: a handle to work parked
between two turns, in a column rather than a reserved key in `variables` (that dict is the
visitor's namespace and is interpolated into chat text, so a name the application reserves is
a name an operator can collide with).

**`session.flow_id` keeps pointing at the root flow.** `_session_needs_restart` compares it
against the attached flow, and a session whose `flow_id` had become a callee's would restart
on every turn.

## Ending a call

An **End block inside a sub-flow means return**, and so does simply running out of blocks —
which is how most flows are actually drawn. Two cases, split exactly the way
`_step_send_message` splits a blank message: with text, that text is the turn's reply and the
caller resumes on the visitor's next one; blank, nothing is said and the hop loop carries
straight on into the caller in the same turn. The text is rendered against the **callee's**
variables, before the frame is closed, because it is the callee's own text.

A callee that ran out with nothing of its own to say does **not** produce the generic
sign-off: "Goodbye!" in the middle of a conversation that is still going is a lie about what
just happened.

## Failure crosses the boundary

`_failed_step` is the router every failing block now goes through, and its order is the only
one that can be honest:

1. the block's own `error` port, if the operator drew one;
2. **the enclosing call's `failed` port**, if this flow is running as a sub-flow;
3. sign off, in the root flow with nowhere left to go.

Step 2 is the one worth reading twice, and a test found its absence: without it a callee that
broke returned through the caller's `done` edge and the caller carried on as though the call
had worked — the exact failure `_step_run_graph` has always refused one level down.

## Two guards, two shapes of mistake

**A flow may not run itself** — refused at save time, by name, while the operator is looking
at the block. **A cycle** (A → B → A) cannot be seen from one graph and is refused at run
time by `subflow_service.guard`, which counts the root flow as already running as well as
every frame: that is the difference between refusing at the second A and refusing one level
later at the second B. Both terminate; only the first names the flow to go and look at.
Depth is capped at `MAX_CALL_DEPTH` (5) for the legitimate-but-runaway case. `_MAX_INTERNAL_HOPS`
would eventually stop either, but it would report a cycle as "something went wrong continuing
this conversation", which tells nobody what to fix.

## What the panel offers, and where it comes from

`flow_io(graph_data)` derives what a flow **writes** (every storing block's `variable_name`,
plus any values it keeps from its own Run Flow blocks) and what it **reads** (every If/Else's
`variable_name` plus every `{{PLACEHOLDER}}` in a prompt or message, minus what it writes for
itself). An If/Else is the one block whose `variable_name` is a value it *reads* — it compares
rather than stores.

Deriving beats declaring: a list read off the callee cannot drift from what the callee does.
It is not exhaustive and cannot be — a Run Graph block inside the callee is handed the whole
variable map, so it can consume a name that appears nowhere in that flow's graph — which is
what the panel's add-by-hand row is for. The lists ride along on each entry of
`callable_flow_choices`, for the reason `email_dispatch/template_service.choices` states: a
second request would let somebody save the block before its rows had loaded.

## The target is checked twice, and it has to be

`_assert_run_flow_targets` checks at **save** time that every Run Flow block's target exists,
is owned by this user, is **generic**, and is **active** — the check that needs the database,
kept out of the synchronous validator exactly as `_validate_send_email_data` documents.
`_run_flow_refusal` checks the same four things again at **run** time.

Neither makes the other redundant, and the reason is that three of the four can change *after*
a block was saved pointing at a flow: it can be deleted, unpublished, or switched back to an
agent flow. At save time these are refusals an operator reads while looking at the canvas; at
run time they are a failed call taking the `failed` port, with the reason logged. The ownership
check is the odd one out — it cannot change — and it is re-checked anyway because the runtime
lookup deliberately does no ownership filtering, so a `graph_data` written outside the save
path must not be able to run another account's flow.

---

# How the canvas draws itself

The blocks are not where somebody put them, unless somebody put them there. The canvas asks
the server for a **layer and a column per block** and arranges itself top to bottom — on
open, and again after anything that changes the wiring. Drag a block and the canvas leaves
your arrangement alone from then on; **Tidy up** hands the arranging back.

That decision is one field on the saved drawing, `layout`, and a drawing without it is
`auto` — which is every flow saved before this existed. So an existing hand-arranged canvas
*is* re-arranged the first time it is opened; nothing is written until Save, and Reload
restores what the database holds.

Six things about how a block is drawn, and moved, are worth knowing:

* **A block with a choice shows a labelled pill per way out**, and each pill *is* that
  output port. A Menu's options, and Create File's `written` / `failed`, are the same thing
  drawn the same way. A block with one way out shows a plain dot on its bottom edge.
  The pills sit **side by side** and wrap only when they run out of room, so a two-option
  Menu is one row rather than three; a label too long for its pill is truncated, and the
  whole of it is in the pill's tooltip. Columns are spaced by the widest pill row on the
  canvas, which is why a Menu of long options pushes its neighbours further apart instead
  of overlapping them.
* **Green means it worked, red means it did not.** A block that can fail — Create File,
  Download File, Run Graph, Run Flow, Send Email — draws its success pill green and its
  `failed` pill red, and the connector out of the failed pill is red and dashed. A Menu's
  options and If / Else's True / False stay grey: they are choices, not outcomes, and a
  visitor pressing the second button has not made anything go wrong. **End Flow is red**,
  which it did not used to be — it shared Goto's grey, and "the conversation is over" and
  "carry on somewhere else" are not the same thing.
* **A connector's ✕ and its two end handles appear on hover.** They used to be on every
  connector at all times, which on this flow's eleven connectors meant thirty-three
  controls competing with nine blocks.
* **A Goto's jump is drawn**, dashed, round a lane to the right of every block. Before
  this it was drawn nowhere, so a flow that loops back to its own menu looked like a flow
  that stopped. It carries no ✕ and no handles — the way to change it is to edit the Goto
  block, which is where its destination lives.

* **Several blocks move together.** Drag a box on empty canvas to select everything it
  touches, Ctrl-click to add and remove one at a time, Ctrl+A or **Select all** for the lot,
  Escape to clear. Then drag any selected block and the whole set moves with its connectors
  following. Selecting a *connector* picks up the two blocks it joins, which is the quick way
  to move a step and the step it feeds without drawing a box round both. Escape mid-drag puts
  everything back — the only undo this canvas has.
* **A connector can be routed by hand.** Drag the line itself and it bends through the point
  you drop it at, up to four bends. Drop a bend back onto the straight line and it removes
  itself; double-click a connector to straighten it completely. Like moving a block, bending
  a wire switches the canvas to manual — and **Tidy up** asks before straightening
  hand-routed wires, because it is about to move the blocks they were routed around.

The layering itself, the loop detection and why the arithmetic is in Python rather than in
the browser are in [CANVAS_LAYOUT.md](CANVAS_LAYOUT.md). The selection, the group move, the
bends and the drag-repaint fix that made a many-block move smooth are in
[CANVAS_SELECTION.md](CANVAS_SELECTION.md).

---

# The file blocks — Create File and Download File

Two blocks whose implementation is in `app/services/file_delivery/`, called from here the
way the Email block is: a new module does not put its files inside another feature's folder,
so this feature contributes two step handlers, two validators and two panels, and everything
about what a file *is* lives in that module. [FILE_NODES.md](FILE_NODES.md) is the whole
story; what belongs here is how the engine drives them.

**Neither block ends the turn.** Create File writes and hops on; Download File puts the link
in a variable and hops on. That is the Email block's rule, and it is why a Send Message
after either of them speaks in the *same* turn — unlike the AI Fallback block, whose
docstring records exactly the opposite behaviour and why.

**The button is not a result type.** A Download File block with *show a download button* on
leaves its payload on the session, and `_with_download` attaches it to whatever ends the
turn. So the button appears under the operator's own sentence, and a Menu after it still
offers its options with the button underneath. A type would have made those exclusive; the
one thing a download button must not do is replace the words somebody wrote about it.

That attach happens in `_run_internal_hops` rather than at `advance_flow_session`'s returns,
and the reason is worth stating: the hop loop is the **only** path that can run a block at
all. The parked-node paths answer a prompt or a graph's question without visiting one, so
there is nowhere else a button can come from.

**`ChatbotFlowSession.node_results`** is what makes "the rows the previous block produced"
expressible at all. `variables` is a flat map of strings, and putting a result set in it
would produce a chat bubble containing JSON — the reason `_store_graph_result` stores a
*count*. The new column holds one small record per block, keyed by node id:

```
{"n3": {"kind": "graph_run", "run_id": "…", "total_rows": 5275},
 "n5": {"kind": "table",     "columns": [...], "rows": [...], "truncated": false},
 "n7": {"kind": "file",      "file_uuid": "…"}}
```

A **run id, not its rows**, because `GraphOutcome.rows` is a twenty-row preview and
`graph_runner.full_result` re-reads the whole result when the file is written. An AI
Fallback's answer is kept twice over — as text under its variable name for an email or a
chat bubble, and as columns and rows here for a CSV — because one stored form would mean one
consumer parsing the other's, and a pipe-separated block of prose is not a table.

**Interpolation happens on this side.** The file name and the button label are rendered
through `_render_text` before the runner sees them, so `invoice-{{ORDER_REF}}` becomes
`invoice-10432` and the "leave an unknown placeholder standing" semantics apply: a file
called `invoice-{{ORDER_REF}}.csv` is a visible mistake where `invoice-.csv` is a silent
one. The node's own `data` is **copied** for the render, never written back — the graph is
the saved drawing, and the next visitor's `ORDER_REF` is not this one's.

**A failure goes through `_failed_step`,** so it takes the block's `error` port, else the
enclosing Run Flow call's `failed` port, else signs off — the same routing Run Graph, Send
Email and Run Flow use, and for the same reason.

---

# Message text interpolates now

`{{NAME}}` in a Send Message, an End Flow's closing message, and an Ask-for-Input / Menu /
Dropdown **prompt** is substituted from the conversation's variables by
`engine_service._render_text`. Until now it was not, in either direction: the help page warned
that it did not and this document claimed it did.

Three decisions:

* **An unknown placeholder is left standing**, and logged. The third set of semantics in this
  codebase for one syntax, deliberately: `email_dispatch/rendering.render` refuses the whole
  send because an email cannot be recalled, while a chat bubble is one message in a live
  conversation and a visible `{{ORDR_REF}}` names the misspelling where a blank would look
  like a value that failed to arrive. Follows
  `chatbot_ai_settings_service.render_system_prompt`.
* **Names match exactly**, not case-insensitively, because every other block treats `email`
  and `EMAIL` as two different variables — `_store_answer` writes the name as typed and an
  If/Else compares it as typed.
* **Option labels are excluded.** A label is not only display text: `_option_text` stores it
  as the visitor's answer and `_effective_message` hands it to an AI Fallback as the question.
  Substituting there would change three things at once, including what a knowledge base gets
  searched for.

`_message_text` interpolates *before* testing for emptiness, so a message that is nothing but
an unknown `{{VALUE}}` stays non-empty and the operator sees which name is wrong instead of a
Send Message block that silently does nothing.

---

# The block box, and the three rules holding it together

The same three rules the [Graph Designer](GRAPH_DESIGNER.md#why-the-header-height-is-pinned)
canvas needs, for the same reason and after the same bug. A block's outgoing ports are
absolutely positioned down its right edge and each can carry a label — `queued` / `failed` on
Send Email, `done` / `failed` on Run Graph, `True` / `False` on If/Else — and those labels are
**opaque**, or a connector passing behind one is unreadable. That makes their position
load-bearing:

1. `.flow-node` declares `--fb-header-h` and `.flow-node-header` is given exactly that
   height rather than sizing to its content, because the port stack is offset from it and a
   header height has to be a number something else can read. `portMetrics()` reads it (and
   `--fb-port-gap`) back out of the computed style, so neither number is stated twice.
2. The stack starts at `calc(var(--fb-header-h) + var(--fb-port-gap))` — **below** the
   header, never across it. It used to start at `top: 8px`, which put the first port's label
   on top of the Edit and Delete buttons and swallowed the clicks meant for them: the buttons
   were painted and simply unreachable, which is the worst version of that bug. The header
   also carries a stacking context above the ports' `z-index`, so nothing can cover it even
   if a future block type outgrows its box.
3. `renderNode` grows the block's `min-height` so the stack fits, and the height is
   **measured** from the rendered stack rather than computed from the port count: a row is as
   tall as its label, not as tall as its 12px dot, so arithmetic over the port size comes out
   a few pixels short per row — which is exactly how far a two-port Send Email block hung its
   `failed` port below its own edge.

The incoming port is centred on the body for the same reason, at
`calc(50% + var(--fb-header-h) / 2)`.

The invariant: **every block's header is fully visible and clickable, whatever its type and
however many ports it has.** Menu and Dropdown are the exception that proves it — they draw a
connector per option *inside* the body, which grows the box on its own and never approaches
the header.

---

# Models — `app/models/flow_builder/models.py`

* **ChatbotFlowSession.awaiting_graph_run** — the uuid of a Graph Designer run this
  visitor's session is waiting on an answer for, or NULL. Its own column rather than a
  reserved key in `variables`: that dict is the visitor's namespace and is interpolated into
  message text, so a key in it can be rendered in a chat bubble, and a name the application
  reserves is a name an operator can collide with. The same kind of thing as
  `ToolGraphRun.thread_id` — a handle to work parked between two requests.
* **ChatbotFlow** — a saved flow graph (`graph_data` JSONB) owned by a user (`user_id`), with a
  nullable, **unique** `chatbot_key_id`. That single constraint expresses both halves of the
  relationship — one flow per agent, one agent per flow — replacing the old service-enforced
  "at most one active flow per key" rule. Deleting an agent detaches its flow
  (`ON DELETE SET NULL`) instead of destroying it.
  `kind` is `agent` or `generic` — a string rather than an `is_generic` boolean, matching how
  every other state in this schema is spelled (`ChatbotFlowSession.status`,
  `FlowNodeKnowledgeBase.status`) and leaving room for a third kind without a migration that
  renames a column. `ck_chatbot_flows_generic_unattached` carries the invariant that follows
  from it: a generic flow is never attached to an agent.
* **ChatbotFlowSession** — per-visitor execution state for a live conversation. The visitor
  browser mints and persists an opaque `session_token` (localStorage), sent on every
  `POST /public/chatbot/message`. This is **not** the row's public `uuid` and is never
  trusted as a lookup key by itself — every query scopes it by `chatbot_key_id`.
* **ChatbotFlowSession.node_results** — what a block produced that is too structured to be a
  variable: `{"<node id>": {...}}`, written by the Run Graph and AI Fallback blocks and read
  by a Create File block that names one of them. Its own column for the same reason
  `awaiting_graph_run` and `call_stack` have theirs, and keyed by **node id** rather than by
  variable name because a Create File block points at one particular box on the canvas —
  which is a different question from "what is the current value of X", and two blocks may
  share a name.
* **FlowNodeKnowledgeBase** — one AI Fallback node's knowledge base. Scoped per node (not per
  flow or per chatbot key) via a string `node_id` pointer into the owning flow's
  `graph_data["nodes"]` — not a FK, since nodes are JSONB entries, not rows. Status is one of
  `untrained` / `trained` / `failed`.
* **FlowNodeKnowledgeDocument** — one supporting document (uploaded pdf/txt/docx, or typed
  text) belonging to a `FlowNodeKnowledgeBase`.

---

# Services — `app/services/flow_builder/`

Three services, deliberately split by concern:

| File | Responsibility |
|---|---|
| `flow_service.py` | Builder CRUD/ownership — creating, editing, publishing (`set_flow_active`) and attaching (`attach_flow`) flows, all checked against `user_id`. `attach_flow` is the single write path for the agent dropdown: it refuses a draft or a flow already used elsewhere, and detaches whatever the agent currently runs before claiming the unique slot. |
| `engine_service.py` | Runtime graph interpretation — given a saved flow and one visitor session, decides what to send back on each turn. Stateless-looking; has the same relationship to `flow_service.py` that `ai_analytics_service.py` has to `chatbot_service.py`. |
| `ai_fallback_service.py` | One AI Fallback node's answer orchestration — reads the node's guardrails/prompt, context source (attached datasource, its own knowledge base, or prompt-only), and LLM choice (the user's AI Settings key, or the in-built default), then asks the right provider. The chatbot's own configured system prompt is the base persona it layers onto, and the chatbot's actions can still run for the turn, but the node's **LLM choice wins** over the chatbot-level one — see [CHATBOT_AI_SETTINGS.md](CHATBOT_AI_SETTINGS.md). Has the same relationship to `engine_service.py` that `knowledge_base_service.py` has to `flow_service.py`. |
| `knowledge_base_service.py` | One AI Fallback node's knowledge base — uploading documents/text, "training" (extract → chunk → embed via the in-built local Ollama model → store vectors in pgvector, see [AI_INBUILT.md](AI_INBUILT.md)), and retrieving grounding context via vector similarity search. |

---

# What a Menu selection carries with it

A button or dropdown reply is the one visitor message with no text in it: the widget
sends an empty `message` and puts the chosen option's **id** in `selected_value` (that id
is also each edge's `source_port`). The engine therefore has to decide what the rest of
the turn treats as the question, because there are no typed words to use.

`engine_service._effective_message` answers that: typed text wins when there is any, and
otherwise the chosen option's **label** — exactly what the visitor sees in their own chat
bubble — is handed to the internal-hop loop.

This matters most for an **AI Fallback node wired straight off a Menu**, which is the
ordinary way to build "pick a department → ask about it". Without it that node is asked
`""`: it searches its knowledge base for nothing and prompts the model with an empty
question. A chatbot whose system prompt scopes it ("answer only from retrieved content,
otherwise say you can't help") then answers with its out-of-scope refusal — which reads
as a broken flow, not as a missing question.

## A label is written to be clicked, not to be searched for

Handing the label on fixes *what the model is asked*. It does not fix **what the knowledge
base is searched for**, and that turned out to be the sharper edge of the same problem.
Measured against a real proposal document: a Menu option labelled "Email me the data"
retrieved that document's security and authentication sections — the nearest chunks to the
word "data" — and the model, told to answer strictly from what was retrieved, explained
that it could not share user data. Grounded, faithful, and about the wrong subject. The
same knowledge base searched with the node's own instructions returned the scope, the
deliverables and the estimates, which is what the operator wired the node to say.

So `ai_fallback_service._retrieval_query` puts the node's **Prompt / instructions** in
front of the label on a selection turn, and `engine_service` threads a `from_selection` flag
down to it — that being the only thing that distinguishes a click from a typed sentence by
the time either reaches the node, since both arrive as the same string.

The label stays in the query: two options wired to two nodes have to retrieve differently,
so it is the instructions that were missing, not the choice. And on a **typed** turn the
instructions stay out — the visitor's own question is a better query than any standing
instruction, and folding "answer in a friendly tone" into every search makes each one
slightly worse for no gain.

Menu and Dropdown nodes also take an optional **`variable_name`** ("Store choice in
variable"), the same field Ask Input has. The chosen label is written there before the
hop, so a later If/Else can branch on what was picked. Both node types share
`_store_answer`, so the JSONB-reassignment rule holds for either: `variables` is a plain
(non-`Mutable`) column, and an in-place `variables[key] = ...` is invisible to
SQLAlchemy's change tracking and would never persist.

An option with no outgoing connector — or a stale id left over from an edited flow —
still re-asks the same menu, and records nothing.

---

# Routes — `app/routes/flow_builder/`

* **FlowBuilderController** (`routes.py`, path `/flow-builder`) — the flow library
  (`GET /flow-builder/`) and the builder canvas: create/rename/publish/delete a flow, load/save
  its graph, list a user's AI Settings keys for the node property panel. No chatbot key in any
  URL; attaching happens in `ChatbotSettingsController.save_flow`
  (`POST /chatbot-settings/{key_id:uuid}/flow`). Also `GET /flow-builder/help`, a static
  template and the only handler here that touches neither the database nor a service — see
  *The in-app help page* above.
* **KnowledgeBaseController** (`knowledge_base_routes.py`, path
  `/flow-builder/{flow_id:uuid}/nodes/{node_id:str}/knowledge-base`) — per-node
  knowledge base management (upload, train, list, delete documents). Returns JSON rather than
  HTML partials, matching `FlowBuilderController`'s own convention, since the canvas and its
  properties panel are entirely client-rendered.

---

# Dependency direction

`flow_builder` depends on `chatbot` (agent ownership checks via
`chatbot_service.get_chatbot_key` when attaching, plus the agent's persona/actions at answer
time), `ai_settings` (LLM key resolution), `ai_analytics` (freeform prompt answering), and
`ai_inbuilt` (the local embedding/knowledge-base pipeline).

The one edge pointing the other way is the attachment UI: `ChatbotSettingsController` calls
`flow_service` to render and save an agent's flow dropdown. That is a routes-layer dependency
only — no `chatbot` *service* imports `flow_builder`, so the service-layer direction is still
one-way.
