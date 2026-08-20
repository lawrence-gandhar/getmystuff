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

Two independent switches decide whether a flow drives a conversation:

| | meaning | set where |
|---|---|---|
| `is_active` | published vs. draft | Flow Builder list (Make Active / Make Draft) |
| `chatbot_key_id` | which agent runs it | the agent's settings page (Conversation Flow dropdown) |

`flow_service.get_active_flow` requires **both**, so a finished flow can be parked without
detaching it, and a draft can sit attached while it is being finished. Only flows that are
active *and* unattached are offered in an agent's dropdown — a flow runs on at most one agent.

**Neither switch involves tools.** `attach_flow` checks ownership, the unique
`chatbot_key_id` slot and `is_active`, and nothing else — a flow is a conversation, not a
query, and a Send Message / Menu / AI Fallback graph reads a knowledge base rather than a
datasource. An operator can build and run a complete flow without ever opening Tool Configs.

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
* **ChatbotFlowSession** — per-visitor execution state for a live conversation. The visitor
  browser mints and persists an opaque `session_token` (localStorage), sent on every
  `POST /public/chatbot/message`. This is **not** the row's public `uuid` and is never
  trusted as a lookup key by itself — every query scopes it by `chatbot_key_id`.
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
  (`POST /chatbot-settings/{key_id:uuid}/flow`).
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
