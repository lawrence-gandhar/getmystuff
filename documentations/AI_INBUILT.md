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
| `ollama_client.py` | Generic async client for the local Ollama server — chat completions and text embeddings over Ollama's native REST API (`POST /api/chat`, `POST /api/embed`) via a pooled `httpx.AsyncClient`. No `ollama` SDK, no LangChain, no OpenAI-compatible shim. Has no knowledge of this app's data shapes (`AnalyticsResult`, Flow Builder models, etc.), so it stays reusable; callers own interpreting the raw text/vectors it returns. Also exposes `preload_models()` / `close_client()`, wired to the app's `on_startup` / `on_shutdown` in `main.py`. See [Performance tuning](#performance-tuning-cpu-only-hosts) for config. |
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

---

# Performance tuning (CPU-only hosts)

The in-built LLM runs entirely on CPU on the current dev box (Intel i5-10400F, 6 physical
cores / 12 threads, no CUDA-capable GPU — the installed GT218 is far too old for CUDA, so
`ollama ps` reports `100% CPU`). A full AI Fallback answer grounded in 8 knowledge-base
chunks takes **~17s** in the configuration below, down from ~47s.

All figures here were measured on this host against the real prompt shape
(`_build_system_prompt` + `_build_user_content` + `_JSON_ONLY_INSTRUCTION`, 8 chunks capped
at 6000 chars), using **distinct prompts per trial** — repeating an identical prompt hits
llama.cpp's prompt cache and roughly halves the apparent latency, which real chatbot traffic
never benefits from.

### App-side config (`.env`, read by `ollama_client.py`)

| Variable | Default | Why |
|---|---|---|
| `OLLAMA_CHAT_MODEL` | `qwen3:1.7b` | **2.2× faster than `qwen3:4b`** on the real 8-chunk prompt — 21.8s vs 47.4s per answer — while matching it on both acceptance checks (valid `AnalyticsResult` JSON, and recovering facts planted at each end of the retrieved context) across 8 trials. Caveat: that is a fact-extraction task both models passed perfectly, so it demonstrates no quality gap rather than proving none exists on harder reasoning. Swap to `qwen3:4b` here if answer quality on real flows disappoints. |
| `OLLAMA_KEEP_ALIVE` | `-1` | Never unload. Sent as a JSON **number** — the API rejects the string `"-1"` with `time: missing unit in duration "-1"` (see `_parse_keep_alive`). |
| `OLLAMA_NUM_THREAD` | `6` | Physical cores. Ollama has **no** `OLLAMA_NUM_THREADS` env var; thread count is the per-request `options.num_thread`. Measured: 4 → 6.0, 6 → 6.0, 8 → 5.9, **12 → 2.0 tok/s**. Using all 12 hyperthreads is 3× *slower* — the siblings contend for the same 6 cores. |
| `OLLAMA_NUM_CTX` | `2048` | **Not a speed knob** — 2048 vs 4096 on an identical prompt measured 4.6 vs 4.5 tok/s, because Ollama sizes the KV cache from this but only evaluates the tokens actually sent. What it controls is truncation, and getting it wrong is silent: at `1024` an 8-chunk prompt was clipped from 1257 to 514 tokens and returned **invalid JSON on every trial**, surfacing to the visitor as *"The local AI model returned an unreadable response."* 2048 fits the measured ~1270-token worst case with headroom. |
| `OLLAMA_NUM_PREDICT` | `512` | Caps generation length. Generation runs ~9.3 tok/s on `qwen3:1.7b`, so each permitted token is ~0.1s of worst-case latency. |

Also in the request payload: `stream: false`, `think: false` (suppresses qwen3's reasoning
block, which would otherwise be generated and then discarded by `_strip_thinking`), and
`format: "json"` for JSON-mode callers.

`preload_models()` runs on app startup so the first user request doesn't pay a cold model
load. Its `options` must keep matching what `chat()` sends — Ollama reloads a model when a
request asks for a different `num_ctx`, which would defeat the preload.

### Detecting truncation

`_warn_if_context_too_small` is a **pre-flight** estimate on the outgoing text, deliberately
not a check on the response. Ollama truncates an over-long prompt and then reports
`prompt_eval_count` for only what survived, so the reported count sits *below* `num_ctx` and
can never reveal the loss — the 8-chunk prompt above reported 514 tokens at `num_ctx=1024`
against 1257 at 2048, with no post-hoc signal that 743 tokens had been dropped. Any check
written against the response would have stayed silent through it.

### Retrieval breadth — why `_MAX_CONTEXT_CHUNKS` stays at 8

Chunk count is the largest remaining latency lever, since prompt evaluation is ~9.7s of the
~17.4s answer. It was measured and deliberately left alone:

| `_MAX_CONTEXT_CHUNKS` | prompt eval | total | vs 8 |
|---|---|---|---|
| 4 | 4.7s | 11.3s | 35% faster |
| 6 | 7.1s | 14.1s | 19% faster |
| **8** (current) | 9.7s | **17.4s** | — |

Lowering it would be a pure loss of recall, not a trim of waste: a fact planted at *every*
rank from 1 to 8 was recovered **8/8** times, so there is no "lost in the middle" effect and
the 5th–8th chunks are genuinely used. Trading half the retrieval depth for 6s is a bad deal
— particularly as the model choice already cut the same answer from 47.4s to 17.4s.

### Server-side config (systemd, needs root)

These are Ollama *server* env vars and cannot be set from the app. Install as a drop-in:

```bash
sudo systemctl edit --force ollama    # or: /etc/systemd/system/ollama.service.d/override.conf
```

```ini
[Service]
Environment="OLLAMA_KEEP_ALIVE=-1"
Environment="OLLAMA_FLASH_ATTENTION=1"
Environment="OLLAMA_KV_CACHE_TYPE=q8_0"
Environment="OLLAMA_MAX_LOADED_MODELS=2"
Environment="OLLAMA_NUM_PARALLEL=1"
```

```bash
sudo systemctl daemon-reload && sudo systemctl restart ollama
```

* `OLLAMA_KEEP_ALIVE=-1` — server-wide default, so anything not going through
  `ollama_client.py` (a manual `ollama run`, another script) also keeps models resident.
* `OLLAMA_FLASH_ATTENTION=1` — cheaper attention; also a prerequisite for the next one.
* `OLLAMA_KV_CACHE_TYPE=q8_0` — quantized KV cache, less memory traffic per token.
* `OLLAMA_MAX_LOADED_MODELS=2` — exactly the chat + embed pair this app pins, so neither
  evicts the other.
* `OLLAMA_NUM_PARALLEL=1` — on a CPU-only host, concurrent requests split the same 6 cores
  and *each* gets slower. Serialize instead; Ollama queues the rest.

Verify with `ollama ps` — both models should show `UNTIL = Forever`, and `qwen3:4b` should
show `CONTEXT = 1024`.
