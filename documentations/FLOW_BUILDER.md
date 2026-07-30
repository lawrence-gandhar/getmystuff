# FLOW_BUILDER.md

Visual conversation-flow builder for the embeddable chatbot widget.

---

# What it is

A flow is a graph of nodes (JSONB, authored on a client-rendered canvas —
`static/js/flow_builder.js` — not server-templated partials) that decides what the chatbot
widget says at each turn of a live conversation.

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

---

# Models — `app/models/flow_builder/models.py`

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
