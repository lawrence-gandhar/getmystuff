# Docker, Python 3.12 and the local LLM

A record of why the app moved into a container on Python 3.12, and how the in-built
Ollama path is configured for the Deep Agents feature. Written as a decision log:
what forced each change, what was measured, and what was tried and rejected.

For the feature itself see [DEEP_AGENTS.md](DEEP_AGENTS.md); for the local LLM client
see [AI_INBUILT.md](AI_INBUILT.md).

---

## Summary

| Change | Reason in one line |
|---|---|
| Runtime moved to **Python 3.12** in Docker | `deepagents` requires ≥ 3.11 on every release; the project venv is 3.10.12 |
| App **containerised** (`Dockerfile`, `docker-compose.yml`) | Get 3.11+ without disturbing a working 3.10 venv, and pin postgres/pgvector/ollama with it |
| `alembic/env.py` now prefers `DATABASE_URL` | `alembic.ini` hardcodes a localhost URL, so migrations could not run in a container at all |
| New **`OLLAMA_DEEP_AGENT_MODEL`** | Tool calling needs a bigger model than single-shot answering; one variable for the whole app would have been a bad trade |
| `num_ctx` floored at 8192, `num_predict` at 1024 | Ollama *truncates* rather than erroring, and a truncated tool call is a wrong answer, not a visible failure |
| Small models **refused**, not attempted | A model that silently skips its tools produces a confident invented answer — the exact failure the feature exists to prevent |
| Timeout split **by who is waiting** | Measured 242–417 s per local turn vs seconds for a hosted provider; one budget cannot serve a visitor and an operator |

---

## Why Python 3.12

### The forcing constraint

`deepagents` cannot run on Python 3.10. This is not a packaging preference — the code
uses 3.11 syntax:

```
$ python3.10 -c "from typing import Required"
ImportError: cannot import name 'Required' from 'typing'
```

`typing.Required` landed in 3.11. Verified against PyPI, **all 113 published
`deepagents` releases** (0.0.1 → 0.7.1, including every rc/alpha) declare the same
floor:

```
requires_python: <4.0,>=3.11      # identical across all 113 releases
```

So there is no older version to pin back to. Forcing the wheel into a 3.10
`site-packages` by hand reproduces the `ImportError` above, confirming the metadata is
honest rather than conservative.

Everything else the feature needs **does** support 3.10 — verified by installing the
full stack into a scratch 3.10 venv, where `langgraph`, `langchain-core`,
`langchain-anthropic`, `langchain-openai` and `langchain-ollama` all import cleanly.
`deepagents` was the single blocker.

### Note on the existing venv

`CLAUDE.md` already states the stack is **Python 3.11+**. The 3.10.12 virtualenv was
what had drifted from the documented target, so moving to 3.12 brings the runtime into
line with the project's own stated requirement rather than raising it.

### Alternatives considered and rejected

| Option | Why not |
|---|---|
| **Build the Deep Agent on `langgraph` directly** (`create_react_agent`), staying on 3.10 | Verified to work, and was the initial recommendation. Rejected on the explicit instruction to use `deepagents` proper. |
| **Use the system `/usr/bin/python3.11`** | It is `3.11.0rc1` — a release candidate shipped by Ubuntu 22.04, not a stable 3.11. Not an interpreter to put an analytics platform on. |
| **`uv python install 3.12` + a local `venv312`** | Attempted, so the app could still run outside Docker. The CPython download timed out repeatedly (`operation timed out` during unpack), including with `UV_HTTP_TIMEOUT` raised. Abandoned in favour of the container. |
| **Force-install `deepagents` on 3.10** | Fails at import, as above. |

### Result

The image is `python:3.12-slim`; the running container reports **Python 3.12.13**.
Pinned versions currently installed:

```
deepagents 0.7.1     langgraph 1.2.10      langchain-core 1.5.3
langchain-anthropic 1.5.3    langchain-openai 1.4.1    langchain-ollama 1.1.0
litestar 2.21.1      SQLAlchemy 2.0.51     alembic 1.18.5
```

`alembic` was added to `requirements.txt` — it was an implicit dev-installed dependency
before, despite ten revisions being in the repo.

---

## Why Docker

Getting to 3.11+ was the trigger, but the container solves four problems at once:

1. **A working environment is not disturbed.** The 3.10 `venv/` is untouched and still
   on disk. Nothing about the existing local setup had to be torn down to try this.
2. **The interpreter is not the host's problem.** No PPA, no `deadsnakes`, no
   RC interpreter, no dependency on a CPython download succeeding on this network.
3. **Postgres arrives correctly configured.** The image is `pgvector/pgvector:pg16`,
   not plain postgres, because `knowledge_chunks` stores 768-dim embeddings in a
   `vector` column. `docker/postgres-init.sql` creates the extension on first boot —
   necessary because `main.py`'s `create_all` runs *before* any migration and would
   otherwise fail on that column.
4. **Ollama is versioned with the app** rather than being whatever the host happens to
   have installed.

### What the container required fixing

**`alembic.ini` hardcodes `postgresql+asyncpg://postgres:1234@localhost/getmystuff`.**
Inside a container that host does not resolve, so `alembic upgrade head` could not run
at all. `alembic/env.py` now prefers `DATABASE_URL` when set, falling back to
`alembic.ini`:

```python
load_dotenv()
if os.getenv("DATABASE_URL"):
    config.set_main_option("sqlalchemy.url", os.environ["DATABASE_URL"])
```

Same precedence as `app/db/db_sessions.py`, so the app and its migrations can no longer
point at different databases. Verified: the full ten-revision chain applies to an empty
database, downgrades, and re-upgrades cleanly.

**Compose env must win over `.env`.** `load_dotenv()` does not override variables
already present in the real environment, so `docker-compose.yml`'s `environment:` block
takes precedence while the same `.env` file keeps working unchanged. That is what lets
`DATABASE_URL` point at `db:5432` and `OLLAMA_BASE_URL` at `ollama:11434` without
editing `.env`.

**The local venv must not leak into the image.** `venv/` is in `.dockerignore`, and
compose masks it with an anonymous volume (`- /app/venv`) — a 3.10 tree bind-mounted
over the image's 3.12 `site-packages` would shadow every installed package.

### Layout

| Service | Image | Host port | Notes |
|---|---|---|---|
| `app` | built from `Dockerfile` | 8003 | `--reload`, source bind-mounted |
| `db` | `pgvector/pgvector:pg16` | **5433** | 5433 so it cannot collide with a local postgres on 5432 |
| `ollama` | `ollama/ollama:latest` | 11435 | Own model volume — see below |

`db` has a `pg_isready` healthcheck and `app` waits on it, because `on_startup` runs
`create_all` immediately and would otherwise race the database.

---

## Local LLM configuration

The in-built path is `app/services/deep_agents/model_factory.py` for data agents, and
the pre-existing `app/services/ai_inbuilt/ollama_client.py` for everything else. Host
for all measurements below: **Intel i5-10400F, 6 physical cores / 12 threads, 31 GB
RAM, no GPU.**

### 1. Two models, not one — `OLLAMA_DEEP_AGENT_MODEL`

The single most important change. Tool calling and single-shot answering want
different models:

| Workload | Model | Why |
|---|---|---|
| Data agents (Deep Agent) | `OLLAMA_DEEP_AGENT_MODEL` — `qwen3:8b` | Must hold a tool-calling loop |
| Chatbot replies, AI Fallback, KB extraction | `OLLAMA_CHAT_MODEL` — `qwen3:1.7b` | One structured-output call; small is fine and ~3× faster |
| Embeddings | `OLLAMA_EMBED_MODEL` — `nomic-embed-text` | Unchanged |

The obvious move — set `OLLAMA_CHAT_MODEL=qwen3:8b` — was **rejected**: it would drag
every in-built feature onto a model roughly 3× slower on CPU in order to enable one
feature. The override applies in `_build_ollama_model()` and nowhere else, and falls
back to `OLLAMA_CHAT_MODEL` when unset, so an existing deployment is unaffected.

### 2. Refuse small models rather than let them skip tools

```python
_MODELS_WITHOUT_RELIABLE_TOOL_CALLING = frozenset({
    "qwen3:0.6b", "qwen3:1.7b", "llama3.2:1b", "tinyllama", "gemma3:1b",
})
```

A Deep Agent depends entirely on the model choosing to emit a tool call. When a model
too small for that fails, it does not raise — it answers confidently with no tool call
behind it, which is precisely the invented-figure failure the whole feature exists to
prevent. So this refuses with a 503 naming the fix.

It is a **denylist, not an allowlist**: an operator who has pulled a model we have
never heard of should be able to try it.

### 3. Two floors, because Ollama truncates silently

| Setting | `.env` value | Deep Agent floor | Why the floor |
|---|---|---|---|
| `OLLAMA_NUM_CTX` | 2048 | **8192** | An over-long prompt is silently cut. A truncated tool *result* is a wrong answer, not an error. |
| `OLLAMA_NUM_PREDICT` | 512 | **1024** | A truncated tool *call* arrives as malformed JSON — the graph sees a broken call rather than a cut-off answer. |

Both `.env` values are correctly sized for the short single-shot prompts they were
tuned for; a Deep Agent turn is a much larger prompt (routing prompt + 10 tool schemas
+ deepagents' own instructions) and at least two round trips. The floors take the
larger of configured-and-required, so raising `.env` still works.

### 4. `temperature = 0`

A routing decision must be near-deterministic: the same question must not pick a
different tool on a retry.

### 5. The deep-agent model is *not* preloaded

`ollama_client.preload_models()` runs at startup with `keep_alive=-1` for
`OLLAMA_CHAT_MODEL` and `OLLAMA_EMBED_MODEL`. `qwen3:8b` is deliberately excluded —
`keep_alive=-1` would pin ~5 GB resident permanently for a feature that may go unused.
The first data-agent turn pays the model load instead (measured below as the cold/warm
difference).

### 6. Measured performance, and what it implies

`qwen3:8b`, measured directly:

| | Result |
|---|---|
| Generation rate | **~2.5 tok/s** |
| One tool-calling round trip, 133-token prompt | **67–81 s** |
| Full two-call agent turn over the real routing prompt | **417 s cold / 242 s warm** |

It works correctly — it routed to the right tool out of two, reported the real figures,
and relayed a tool's fixed filter unprompted:

```
Q: How many units did each customer receive on paid orders?
tools called: ['paid_units_by_customer']
A: Acme: 18 units, Initech: 2 units.  This data is restricted to paid orders only.
```

It is simply minutes per turn. A hosted provider does the same turn in seconds.

**This is why the timeout is keyed to who is waiting, not to which provider answers:**

| Caller | Budget | Env override |
|---|---|---|
| Chatbot turn — a visitor is waiting on a web request | **120 s** | `DEEP_AGENT_TIMEOUT_SECONDS` |
| Test console — an operator ran it deliberately | **900 s** | `DEEP_AGENT_CONSOLE_TIMEOUT_SECONDS` |

The visitor budget is deliberately **not** widened for the local model. An agent too
slow to answer inside it degrades to the data-profile reply, which serves a visitor
better than a seven-minute spinner.

**Operational conclusion: on CPU-only hardware, in-built data agents are a test-console
feature. Use a saved API key for live widgets.** The pre-existing in-built chatbot path
(single call on `qwen3:1.7b`) is unaffected and remains usable.

### 7. Models live in the container's own volume

The `ollama` service has a named volume separate from any Ollama installed on the host,
so models have to be pulled into it.

The two models the app uses on every boot are pulled automatically by the `ollama-init`
service — a one-shot container that waits for the Ollama server, pulls, and exits.
`app` gates on it with `condition: service_completed_successfully`, so `preload_models()`
never races the download on a fresh volume. Nothing to run by hand:

```bash
docker compose up -d          # ollama-init pulls OLLAMA_CHAT_MODEL + OLLAMA_EMBED_MODEL
```

It reads the model names from the same `OLLAMA_CHAT_MODEL` / `OLLAMA_EMBED_MODEL`
values the app uses, so changing one in `.env` changes what gets pulled. Later boots
cost nothing — an already-present model returns immediately.

The Deep Agents model is **not** pulled automatically. It is 5.2 GB for a feature that
may go unused, and on CPU-only hardware in-built data agents are a test-console feature
anyway (section 6). Pull it only if you want it:

```bash
docker compose exec ollama ollama pull qwen3:8b          # OLLAMA_DEEP_AGENT_MODEL
```

Skipping that one is not fatal — everything except in-built *data agents* works.

**Reusing the host's Ollama was considered and rejected.** The host already had all
four models, which would have saved a 5.2 GB download. But host Ollama binds
`127.0.0.1:11434` by default and is therefore unreachable from a container (verified:
`--add-host=host.docker.internal:host-gateway` returns nothing). Making it reachable
requires starting it with `OLLAMA_HOST=0.0.0.0`, which also exposes it beyond
localhost — a host-level change with a security consequence, so the self-contained
container is the default. The alternative is documented in `docker-compose.yml` for
anyone who wants it.

### 8. Pre-existing `.env` tuning, left alone

These were already measured for this host and are unchanged:

```
OLLAMA_NUM_THREAD=6     # physical cores, NOT the 12 hyperthreads:
                        # 6.0 tok/s at 6 threads vs 2.0 tok/s at 12 — oversubscribing contends
OLLAMA_KEEP_ALIVE=-1    # keep the small model resident
OLLAMA_NUM_CTX=2048     # fits the ~1270-token worst-case KB prompt with headroom
```

---

## Steps taken, in order

Including the dead ends, because they are the evidence for the decisions above.

1. `pip install deepagents` into the 3.10 venv → *"Could not find a version that
   satisfies the requirement"*.
2. Confirmed it was not a network fault — `pip download langgraph` succeeded.
3. Queried PyPI metadata for **all 113** `deepagents` releases → every one declares
   `>=3.11`.
4. Installed the full LangChain/LangGraph stack into a scratch 3.10 venv → all imports
   fine. Isolated `deepagents` as the sole blocker.
5. Extracted the `deepagents` wheel into 3.10 `site-packages` by hand →
   `ImportError: cannot import name 'Required' from 'typing'`. Metadata confirmed
   honest.
6. Surveyed host interpreters → only `3.11.0rc1`. Rejected.
7. `uv python install 3.12` for a local `venv312`, so the app could still run outside
   Docker → repeated download timeouts, including with `UV_HTTP_TIMEOUT=900`.
   Abandoned.
8. Wrote `Dockerfile` (python:3.12-slim), `docker-compose.yml`, `.dockerignore`,
   `docker/postgres-init.sql`. Verified in-image: Python 3.12.13, `deepagents` 0.7.1,
   and `create_deep_agent`'s `system_prompt` kwarg present (it was `instructions` in
   older releases).
9. Fixed `alembic/env.py` to honour `DATABASE_URL`; verified the ten-revision chain
   upgrades on an empty database, downgrades and re-upgrades.
10. First startup logged Ollama 404s — the fresh volume had no models. Pulled
    `qwen3:1.7b` and `nomic-embed-text`; startup then preloaded both with `200 OK`.
11. Investigated reusing the host Ollama → it binds `127.0.0.1` only. Rejected;
    documented.
12. Pulled `qwen3:8b`. **The first attempt failed** with `Error: unexpected EOF` (a
    truncated download) and was initially misread as success because the output was
    piped through `tail`, which masked the exit status. Retried with `pipefail` and a
    retry loop.
13. Added `OLLAMA_DEEP_AGENT_MODEL` and the `num_predict` floor. Verified model
    selection across seven cases: override set/unset, small model refused via either
    variable, floors applied, large values respected, unparseable values ignored.
14. Ran a live agent turn → **hit the 120 s timeout**.
15. Measured raw latency to get real numbers rather than guess a bigger timeout.
16. Reframed the timeout around who is waiting, and made both budgets env-overridable.
    First attempt keyed it off the provider — corrected, because that would have let a
    chatbot visitor wait seven minutes.
17. Re-ran the live turn → passed, on both a cold and a warm model.

---

## Operating it

```bash
docker compose up --build          # app :8003, postgres :5433, ollama :11435
docker compose exec app alembic upgrade head
docker compose logs -f app
```

Then log in at <http://localhost:8003/auth/login> with **`admin@test.com` / `admin123`**.

That account is seeded by `on_startup`, which calls
`app/db/auth/create_fake_user.py` after `create_all`. It exists because the compose
stack has its own `pgdata` volume: a fresh volume means an empty `users` table, and
every login attempt then bounces back to the form as "Invalid credentials" with nothing
in the logs to say the account was simply never created. Seeding on boot removes that
failure mode. It is idempotent — later boots log `already exists — skipping seed`.

**This is DEV ONLY, for the same reason `create_all` is.** A known admin password
created automatically on boot must not reach production; that whole block goes away in
favour of Alembic plus a real provisioning step.

Environment variables introduced by this work:

| Variable | Default | Effect |
|---|---|---|
| `OLLAMA_DEEP_AGENT_MODEL` | `qwen3:8b` (set in compose) | In-built model for data agents only |
| `DEEP_AGENT_TIMEOUT_SECONDS` | 120 | Chatbot-turn budget |
| `DEEP_AGENT_CONSOLE_TIMEOUT_SECONDS` | 900 | Test-console budget |

`POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` default to `getmystuff` for local
development and are overridable from the shell. They are convenience defaults, not an
endorsement of a shared password — set real values before the stack is reachable from
anywhere but localhost.

---

## Still open

* **The hosted provider path is unexercised.** `ANTHROPIC_API_KEY` is empty in `.env`,
  so no live Anthropic turn has been run. The agent machinery is proven by the local
  `qwen3:8b` run and by a stub-model integration test; the first real Anthropic call is
  not yet verified.
* **`main.py` still calls `create_all` at startup** ("DEV ONLY" per its own docstring)
  alongside Alembic. Unchanged by this work, but worth resolving before production:
  the two can disagree.
* **`docker-compose.yml` is development-shaped** — `--reload`, source bind-mounted,
  `debug=True` in `main.py`. A production compose file would drop all three.
* **`db_utils` circuit-breaker constants are still un-cast `os.getenv` strings**
  (`ENGINE_TTL_SECONDS`, `CIRCUIT_FAILURE_LIMIT`, `CIRCUIT_RESET_SECONDS`), so the
  breaker cannot trip, and the two `cleanup_idle_*` coroutines are never scheduled.
  Pre-existing and untouched here, but the container makes it easy to set those values.

---

## Related

* [DEEP_AGENTS.md](DEEP_AGENTS.md) — the feature this runtime work was for
* [AI_INBUILT.md](AI_INBUILT.md) — the Ollama client and the in-built LLM path
* [ARCHITECTURE.md](ARCHITECTURE.md) — layering and project structure
