# FLOW_BUILDER.md

Visual conversation-flow builder for the embeddable chatbot widget.

---

# What it is

A flow is a graph of nodes (JSONB, authored on a client-rendered canvas —
`static/js/flow_builder.js` — not server-templated partials) that decides what the chatbot
widget says at each turn of a live conversation. Every flow is owned by a `ChatbotApiKey`
(see `app.services.chatbot.chatbot_service.get_chatbot_key`), so a flow can never be
read/edited/activated by a user who doesn't own the chatbot key it belongs to.

---

# Models — `app/models/flow_builder/models.py`

* **ChatbotFlow** — a saved flow graph (`graph_data` JSONB) owned by a `ChatbotApiKey`; at
  most one flow per key can be `is_active` at a time (see
  `flow_service.deactivate_other_flows`).
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
| `flow_service.py` | Builder CRUD/ownership — creating, editing, activating flows, always routed through the owning `ChatbotApiKey`. |
| `engine_service.py` | Runtime graph interpretation — given a saved flow and one visitor session, decides what to send back on each turn. Stateless-looking; has the same relationship to `flow_service.py` that `ai_analytics_service.py` has to `chatbot_service.py`. |
| `ai_fallback_service.py` | One AI Fallback node's answer orchestration — reads the node's guardrails/prompt, context source (attached datasource, its own knowledge base, or prompt-only), and LLM choice (the user's AI Settings key, or the in-built default), then asks the right provider. Has the same relationship to `engine_service.py` that `knowledge_base_service.py` has to `flow_service.py`. |
| `knowledge_base_service.py` | One AI Fallback node's knowledge base — uploading documents/text, "training" (extract → chunk → embed via the in-built local Ollama model → store vectors in pgvector, see [AI_INBUILT.md](AI_INBUILT.md)), and retrieving grounding context via vector similarity search. |

---

# Routes — `app/routes/flow_builder/`

* **FlowBuilderController** (`routes.py`, path `/flow-builder`) — the builder canvas: load/save
  a flow's graph, list a user's AI Settings keys for the node property panel.
* **KnowledgeBaseController** (`knowledge_base_routes.py`, path
  `/flow-builder/{key_id:uuid}/{flow_id:uuid}/nodes/{node_id:str}/knowledge-base`) — per-node
  knowledge base management (upload, train, list, delete documents). Returns JSON rather than
  HTML partials, matching `FlowBuilderController`'s own convention, since the canvas and its
  properties panel are entirely client-rendered.

---

# Dependency direction

`flow_builder` depends on `chatbot` (ownership checks via `chatbot_service.get_chatbot_key`),
`ai_settings` (LLM key resolution), `ai_analytics` (freeform prompt answering), and
`ai_inbuilt` (the local embedding/knowledge-base pipeline). Nothing outside `flow_builder`
depends back on it.
