# AI_INBUILT.md

In-built local LLM integration — a single locally-running Ollama server serving the whole
app, used for embedding and grounding Flow Builder's AI Fallback knowledge-base nodes. Not a
per-user credential like the AI Settings provider keys: configuration is app-wide env vars
with defaults, not a per-user settings UI.

---

# Dependency direction

`flow_builder` depends on `ai_inbuilt`, never the reverse. `ai_inbuilt` modules take plain
scalars (`document_id`, `knowledge_base_id`, `content`) rather than ORM instances, and its
`KnowledgeChunk` model's foreign keys reference `flow_node_knowledge_documents` /
`flow_node_knowledge_bases` by table name only (resolved lazily by SQLAlchemy via
`Base.metadata`) — so this module has no Python import dependency on
`app.models.flow_builder`. See [FLOW_BUILDER.md](FLOW_BUILDER.md) for the feature that
consumes this pipeline.

---

# Model — `app/models/ai_inbuilt/models.py`

**KnowledgeChunk** (`ai_inbuilt_knowledge_chunks`) — one embedded text chunk belonging to a
Flow Builder knowledge-base document.

* `embedding` — a pgvector `Vector(768)` column (`EMBEDDING_DIMENSIONS = 768`, matching
  `nomic-embed-text`'s output size). Changing `OLLAMA_EMBED_MODEL` to a model with a
  different output size requires a new migration to alter this column's dimension, plus a
  full re-embed of every existing chunk.
* `knowledge_base_id` is denormalized alongside `document_id` (rather than requiring a join)
  so the hot retrieval path — filter by knowledge base, order by vector distance — can use
  the HNSW index directly. Safe because a document's owning knowledge base is set once at
  creation and never changes, and both FKs cascade from the same delete.
* `embed_model` records which embedding model produced each vector, so a future embed-model
  change is detected as staleness rather than silently mixing incompatible vectors in
  similarity search.
* Indexed with an HNSW cosine-distance index (`m=16, ef_construction=64`) for fast
  approximate nearest-neighbor search.

---

# Services — `app/services/ai_inbuilt/`

| File | Responsibility |
|---|---|
| `ollama_client.py` | Generic async client for the local Ollama server — chat completions and text embeddings. Has no knowledge of this app's data shapes (`AnalyticsResult`, Flow Builder models, etc.), so it stays reusable; callers own interpreting the raw text/vectors it returns. Config: `OLLAMA_BASE_URL` (default `http://localhost:11434`), `OLLAMA_CHAT_MODEL` (default `qwen3:8b`), `OLLAMA_EMBED_MODEL` (default `nomic-embed-text`), plus timeout/keep-alive env vars. |
| `chunking.py` | Pure text chunking (`split_text`), no I/O, no DB — kept separate from `embedding_service.py` so the splitting logic can be unit tested/tuned in isolation. Paragraph-aware: packs consecutive paragraphs up to `max_chars` (default 1200, ~300 tokens — comfortably inside `nomic-embed-text`'s 2048-token context window), and hard-splits any single paragraph longer than the limit with `overlap_chars` of carried-over context. |
| `embedding_service.py` | Chunk + embed + store (`embed_document`), and similarity retrieval, for the knowledge base pipeline. Delete-then-insert in one transaction makes re-embedding idempotent. Caps chunks per document at 400. |

---

# DB layer — `app/db/ai_inbuilt/queries.py`

`ai_inbuilt`-specific data access that doesn't fit the generic `CRUDQueryBuilder` (see
`app/db/db_utils.py`): batched chunk inserts/deletes and pgvector similarity ordering —
`CRUDQueryBuilder.get_many` only orders by a plain column-name string, so a vector-distance
`ORDER BY` is structurally out of reach for it. Raw SQLAlchemy Core construction lives here
instead of leaking into the service layer, matching `app/db/flow_builder/queries.py`'s
precedent.

* `insert_chunks` — batched multi-row insert, one round-trip/commit for the whole document.
* `delete_chunks_for_document` — used by the delete-then-insert re-embed flow.
* `documents_with_current_chunks` — which `document_id`s already have chunks embedded under
  a given `embed_model`; drives `train_knowledge_base`'s re-embed staleness check (skip
  already-current docs, re-embed everything if the configured embed model has changed).
* `search_similar_chunks` — top-`limit` chunks for a knowledge base, nearest first by cosine
  distance; this is what `knowledge_base_service.retrieve_context` calls at answer time.
