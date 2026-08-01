# TESTING.md

The automated test suite, its coverage ratchet, and the run history it leaves
behind.

Driven by the `full-test-coverage` skill (`.claude/skills/full-test-coverage/`),
which runs the suite, writes tests for whatever is not yet covered, and records
each run. This document explains the machinery the skill drives.

---

## Running it

```bash
# Everything: suite + coverage + timestamped report
bash .claude/skills/full-test-coverage/scripts/run_coverage.sh
python3 .claude/skills/full-test-coverage/scripts/make_report.py --tests-exit-code $?

# Just the suite, while iterating
docker compose exec -T app python -m pytest tests/ -q --no-cov

# One file
docker compose exec -T app python -m pytest tests/unit/utils/test_validators.py -q --no-cov
```

Test dependencies live in `requirements-dev.txt`, installed into the image by
the `Dockerfile`. `run_coverage.sh` reinstalls them into a running container if
they are missing, so a container started from an older image still works.

### Environment variables the suite needs

`tests/conftest.py` sets a default for each of these *before* importing anything
under `app/`, using `os.environ.setdefault` so a real value always wins:

| Variable | Why the suite needs it |
|---|---|
| `DATABASE_URL` | `app/db/db_sessions.py` calls `create_async_engine` at module scope, so an unset or Postgres URL would explode on import or point the suite at a real database. |
| `JWT_SECRET_KEY` | **`app/db/auth/auth.py` raises at import when this is unset** — deliberately, so a deployment can never run on a guessable signing key. The conftest default is a fixed test key; it signs nothing outside the run. |
| `FERNET_KEY` | Datasource password encryption. |
| `OLLAMA_BASE_URL`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` | Set to unreachable/dummy values so a missed mock fails loudly rather than reaching a real provider. |

`JWT_SECRET_KEY` is the one that will bite a new checkout: the application
refuses to start without it. Generate one with

```bash
python -c "import secrets; print(secrets.token_urlsafe(64))"
```

and put it in `.env` as `JWT_SECRET_KEY=…`. `.env` is gitignored, so each
environment needs its own — which is the point.

---

## Why the tests run in the container

The local `venv` is Python 3.10. `deepagents` requires 3.11 or newer, so
`app/services/deep_agents/` cannot be imported at all on the host — a host run
would silently skip part of the codebase and report a coverage number that was
not true. The container is Python 3.12 with every dependency installed.

The repository is bind-mounted read-write at `/app`, so tests written on the
host run in the container immediately, and reports written in the container
appear on the host. No copying, no rebuild.

---

## The SQLite test database, and the four shims that make it possible

Every test gets a fresh in-memory SQLite database. It is created per test and
thrown away after, so no test can see another's rows and the suite has no shared
state to reset.

The models are written against PostgreSQL, and four column types have no SQLite
rendering. `tests/conftest.py` registers a `@compiles` shim for each, at import
time, before any metadata is compiled. Without them
`Base.metadata.create_all()` fails outright:

| Type | Rendered as | Why it is needed |
|---|---|---|
| `JSONB` | `JSON` | Used by 7 model modules. Without it: `CompileError: can't render element of type JSONB`. |
| `postgresql.UUID` | `CHAR(36)` | Every model's public identifier column. |
| `pgvector.Vector` | `BLOB` | Lets `knowledge_chunks` be created. See the limitation below. |
| `BigInteger` | `INTEGER` | **The one that is easy to miss.** Every model uses a `BigInteger` autoincrement primary key, but SQLite only auto-assigns a rowid for a column declared exactly `INTEGER PRIMARY KEY`. Without this shim every single insert fails with `NOT NULL constraint failed: <table>.id`. |

With all four in place all 22 tables create cleanly.

**The limitation worth knowing**: the `Vector` shim only lets the table be
*created*. Vector similarity search — the `<=>` operator — has no SQLite
equivalent and cannot run. Tests that touch `retrieve_similar_chunks()` or
`app/db/ai_inbuilt/queries.py` must mock the query layer. If real pgvector
behaviour ever needs testing, the compose `db` service (pgvector on host port
5433) is the place to do it.

A second non-obvious piece: the engine uses `StaticPool`. `sqlite+aiosqlite://`
gives *each new connection* its own empty database, so without a pool that holds
one connection open, the tables created by the fixture would be invisible to the
next query.

---

## Reaching an authenticated route

Every controller except `AuthController` and `PublicChatbotController` sets:

```python
dependencies = {"user": require_auth}
```

as a **class attribute**, which overrides any app-level `user` provider. Passing
a fake user into `Litestar(dependencies=...)` is therefore silently ignored and
the request 401s. Injection-based auth faking does not work here.

What works is minting a real token, which `auth_client_factory` does:

```python
client.cookies.set("access_token", create_access_token(str(user.uuid)))
```

This exercises the genuine `require_auth` → `decode_token` → user-lookup path
rather than bypassing it, so the authentication code is covered too.

**An unauthenticated request does not return 401.** `main.http_exception_handler`
turns a 401 into a redirect to `/auth/login`, or — for an HTMX request — a 200
carrying an `HX-Redirect` header, because a plain redirect would swap the login
page into whatever element issued the request. Tests must assert the redirect,
not the 401.

---

## What is mocked, and the guard that enforces it

Five places in the application reach outside the process. Each has a fixture:

| Boundary | Module | Fixture |
|---|---|---|
| Local LLM (Ollama) | `app/services/ai_inbuilt/ollama_client.py` | `mock_ollama` |
| Anthropic / OpenAI SDKs | `app/services/ai_analytics/ai_analytics_service.py` | `mock_llm_sdks` |
| Outbound webhooks | `app/services/chatbot/chatbot_action_service.py` | `mock_outbound_http` |
| LangChain / deepagents | `app/services/deep_agents/` | `mock_deep_agent` |
| User-supplied databases | `app/db/db_utils.py` | `mock_external_datasources` |

An autouse fixture blocks outbound TCP connections and raises a named error
naming the host. A missed mock would otherwise hang until a timeout or, worse,
quietly succeed against a real service and make the suite depend on the network.
Loopback stays allowed. `@pytest.mark.external` opts a test out, and should be
rare.

`main.app` is never served by a test. Its `on_startup` runs `create_all` against
the real engine, seeds a user, and calls `ollama_client.preload_models()` over
the network. `tests/conftest.py:build_test_app()` assembles an equivalent app
from the same controllers, middleware and exception handler; `main.py`'s own
functions are covered directly in `tests/test_main.py`.

---

## The coverage ratchet

Coverage is measured over **all** of `app/` and `main.py`. `[tool.coverage.run]`
in `pyproject.toml` carries **no `omit` list**, deliberately: excluding awkward
files inflates the number into something meaningless. If a file genuinely should
not be measured it should be deleted or moved out of `app/`, not hidden.

`tests/coverage_baseline.json` stores the high-water mark plus a per-file
breakdown. On each run `make_report.py`:

- fails with exit code `1` if total coverage fell below the stored baseline
- fails with exit code `2` if the suite itself failed
- updates the baseline **only** on a green, non-regressing run — a broken run can
  never lower the bar
- diffs the per-file list to detect modules that did not exist at the last run,
  which the skill then prioritises

The target is 100%. It is approached monotonically rather than demanded at once:
the number can only go up.

### The blind spot the ratchet has to compensate for

coverage.py cannot see a module that nothing imports. Its scan for
never-executed files only walks real packages, and **`app/services`,
`app/models`, `app/utils` and `app/schemas` have no `__init__.py`** — they are
namespace packages, so the scan skips them.

The consequence is worse than a low score: such a file is not reported at 0%, it
is absent from the report altogether. It contributes nothing to the denominator,
so a brand-new untested module leaves the percentage completely unchanged. The
one thing a coverage tool is supposed to catch, it silently misses.

`make_report.py` therefore builds its file list by walking the filesystem
(`discover_source_files()`) and cross-checks it against the coverage data.
Anything on disk but missing from the report is listed under **Unmeasured source
files** and forces exit code `3`, so a run in that state can never be mistaken
for a complete one.

Adding `__init__.py` to those four directories would let coverage find them by
itself. That is an application change rather than a test change, so it has been
left to a deliberate decision rather than done as a side effect.

### Known unmeasured files

**None. The blind spot is closed** — as of the 2026-08-01 11:26 UTC run every
source file on disk is measured, and `make_report.py` exits `0` rather than `3`.

It took four files to get there, and the way each was closed is worth keeping,
because the same choice will come up again:

| File | How it was closed |
|---|---|
| `app/utils/csv_to_db.py` | **Tested.** It was assumed untestable because of its hardcoded Windows `CSV_FOLDER`, but that constant is only read under `if __name__ == "__main__"` — importing the module never touches it. Its functions take an engine and a path as arguments, so `clean_column`, `create_table`, `copy_chunk`, `process_csv_file` and `seed_folder` are all directly testable. See `tests/unit/utils/test_csv_to_db.py`. |
| `app/models/ai_analytics/prompt_configurations.py` | **Imported.** Zero bytes, so importing it is harmless and makes coverage count it (at 0 statements). Done from `tests/unit/models/test_model_contracts.py`, which also records that the file is an unwritten placeholder. |
| `app/models/subscriptions/` | **Deleted.** The module could not be imported at all (chained assignment → `TypeError`) and duplicated the `UserSubscription` already mapped in `app/models/user/user.py`. It was measured for one run by a test asserting its import failure, then removed. `tests/unit/models/test_model_contracts.py::TestSubscriptionsModuleIsGone` asserts it stays gone. |

The general lesson: *"nothing imports it"* is not the same as *"it cannot be
tested"*. Before declaring a file dead, check whether its unrunnable parts are
confined to a `__main__` guard or to module-level constants that no function
actually reads. Only one of the four turned out to be genuinely dead.

`app/utils/csv_to_parquet.py` was a fifth such file, closed the same way earlier.

---

## The run history

Two committed artifacts, both under `tests/reports/`:

- **`<UTC timestamp>-report.md`** — one per run. Records the UTC *and* local
  start time, duration, git commit and branch, pass/fail/error/skip counts,
  coverage against the baseline, every failure (test id, `file:line`, exception
  class, message, traceback, and a written root cause), any new modules, and a
  gap table of every file below 100% with its uncovered line ranges.
- **`HISTORY.md`** — append-only, one row per run, newest last.

They are committed on purpose: a timestamped record of what broke and when is
only useful if it survives. Only the raw intermediates
(`.junit.xml`, `.coverage.json`) are gitignored, along with `htmlcov/`.

The scripts, not the model, produce every number and timestamp in those files.
A model asked to read a terminal dump and write down a percentage will
eventually write down the wrong one, and a coverage report nobody can trust is
worse than none.

---

## Layout

```
tests/
  conftest.py                      shims, fixtures, mocks, app/client factories
  test_harness_smoke.py            proves the fixture layer itself works
  test_main.py                     app assembly, exception handler, lifecycle
  unit/
    utils/                         validators, query_joins, crypto, file_utils,
                                   turn_recorder, csv_to_parquet, csv_to_db
    schemas/                       Pydantic DTO validation
    models/                        cross-model contracts (id/uuid rule, FKs)
    db/                            CRUDQueryBuilder, db_utils helpers,
                                   db_utils file datasources, engine/Mongo pool
      auth/                        hashing, JWT, require_auth
      queries/                     the per-feature db/<feature>/queries.py modules
    services/<feature>/            business logic, tested without routes
  integration/
    routes/<feature>/              one client test per handler
  reports/                         generated run history (committed)
  coverage_baseline.json           the ratchet's stored high-water mark
```

`tests/unit/models/test_model_contracts.py` is worth knowing about: it walks
`Base.registry` and asserts CLAUDE.md's identifier rule (bigint `id` primary key,
unique indexed `uuid`, every foreign key pointing at an `id`) against *every*
mapped model. A model added tomorrow is checked the day it lands, without anyone
remembering to write a test for it.

Test files mirror the module they cover, and — per the project rule that a new
module gets its own feature folder — a new feature gets its own subfolder rather
than being filed under an existing one. Every test directory needs an
`__init__.py`.

If `test_harness_smoke.py` fails, fix that before anything else: every other
test in the suite depends on the fixtures it guards.
