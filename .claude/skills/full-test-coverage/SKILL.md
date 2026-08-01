---
name: full-test-coverage
description: Run the GetMyStuff test suite with full code coverage, write tests for any module that lacks them (including newly added ones), and record the run — pass/fail counts, coverage, and every failure with its cause — in a timestamped markdown report. Use when the user asks to "run the tests", "test the application", "check coverage", "full code coverage", "add tests for X", or after a new module or feature has been written.
version: 1.0.0
---

# Full Test Coverage

Runs the whole suite, measures honest coverage over all of `app/` and `main.py`,
closes coverage gaps by writing new tests, and leaves behind a dated record of
what happened.

The target is 100%. It is reached by ratchet, not in one run: a stored baseline
can never fall, and every invocation is expected to raise it. Read
[documentations/TESTING.md](../../../documentations/TESTING.md) for the design
rationale behind the harness.

## Non-negotiables

1. **Never lower the bar to make a run pass.** Do not add `omit` entries to
   `[tool.coverage.run]`, do not delete or `xfail` a failing test, and do not
   hand-edit `tests/coverage_baseline.json`. If coverage regressed, either write
   the missing tests or report the regression.
2. **Never change application code to make a test pass.** This skill tests the
   application; it does not edit it. A test that fails because the application
   is wrong is a *finding* — record it in the report and tell the user. Fixing
   it is a separate, explicitly-requested task.
3. **The scripts own the numbers.** Coverage percentages, timestamps and the
   pass/fail verdict come from `make_report.py`. Never type a percentage or a
   date from memory or from reading terminal output — quote what the script
   wrote.
4. **A failing suite is reported as failing.** Never describe a run as
   successful when `run_coverage.sh` exited non-zero.

## Procedure

### 1. Run the suite

```bash
bash .claude/skills/full-test-coverage/scripts/run_coverage.sh
echo "pytest exit: $?"
```

The script starts the `app` container if it is down and installs
`requirements-dev.txt` if pytest is missing, so it is safe to run from a cold
machine. Capture the exit code — the next step needs it.

To iterate on one area while writing tests, run pytest directly (much faster,
no coverage overhead):

```bash
docker compose exec -T app python -m pytest tests/unit/services/test_x.py -q --no-cov
```

### 2. Write the report

```bash
python3 .claude/skills/full-test-coverage/scripts/make_report.py --tests-exit-code <exit code from step 1>
```

This writes `tests/reports/<UTC timestamp>-report.md`, appends a row to
`tests/reports/HISTORY.md`, and updates `tests/coverage_baseline.json` only when
the run was green and did not regress.

Its exit code is the verdict:

| Code | Meaning |
|---|---|
| `0` | clean — suite green, no regression, every source file measured |
| `1` | coverage fell below the stored baseline |
| `2` | the suite itself failed |
| `3` | source files exist that coverage never measured |

Exit `3` matters more than it looks. Coverage cannot see a module that nothing
imports — `app/services`, `app/models`, `app/utils` and `app/schemas` have no
`__init__.py`, so coverage's scan skips them entirely. Such a file is not
reported at 0%; it is absent, contributing nothing to the denominator. A brand
new untested module is therefore invisible to the percentage, which is exactly
the failure this skill exists to prevent. The report takes its file list from
the filesystem instead and calls these out under **Unmeasured source files**.

### 3. Act on the verdict

**If tests failed** — diagnose each one. Read the failing test and the code it
exercises, then decide which of these it is:

- the test is wrong → fix the test
- the application is wrong → **do not fix it**; record it and report it
- the harness is wrong (a fixture, a mock) → fix `tests/conftest.py`

Then edit the report's `## Failures` section, replacing the
`_to be filled in by the reviewing agent_` placeholders under each failure with
a real **Root cause** and **Fix** line. That is the part of the record that has
value later; leaving the placeholders in defeats the purpose of the report.

**If coverage regressed** — find what dropped by comparing `per_file` in
`tests/coverage_baseline.json` against the report's gap table, and write tests
to close it. Report the regression to the user either way.

**If files are unmeasured** — each one is a source file nothing imports. Either
write a test that imports and exercises it (which makes it measurable), or, if
it is genuinely dead, tell the user it should be deleted. Do not delete
application code yourself. Four such files exist today and are all unfixable by
testing — they are listed in
[TESTING.md](../../../documentations/TESTING.md#known-unmeasured-files).

**If the run was clean** — go to step 4.

### 4. Close the biggest gaps

The report's `## Coverage gaps` table is the work queue, already ordered by
uncovered statement count. Its `## New modules this run` section lists files
absent from the previous baseline — **these take priority**, because a new
module arriving untested is exactly what this skill exists to catch.

Pick the highest-value targets and write tests for them, then return to step 1.
Every invocation should move the number up.

Prefer, in this order:
1. New modules with no tests at all.
2. Pure functions and validators — the cheapest coverage per line of test.
3. Services — the business logic, tested directly rather than through a route.
4. Routes — one integration test per handler for the success path and the main
   failure path.

## Writing tests for this codebase

Mirror the source layout. `app/services/workspaces/workspace_service.py` is
tested by `tests/unit/services/workspaces/test_workspace_service.py`; a route
module goes under `tests/integration/routes/<feature>/`. Per the project rule,
a new feature gets its own subfolder — never file it under another feature's.
Add an `__init__.py` to any new test directory.

Use the fixtures in `tests/conftest.py` rather than rebuilding them:

| Need | Fixture |
|---|---|
| A database session | `db` |
| A persisted user | `user`, or `make_user(email)` for a second one |
| An unauthenticated client | `client_factory(Controller, ...)` |
| An authenticated client | `auth_client_factory(Controller, ...)` |
| Local LLM calls stubbed | `mock_ollama` |
| Anthropic/OpenAI stubbed | `mock_llm_sdks` |
| Outbound webhooks stubbed | `mock_outbound_http` |
| Deep-agent runtime stubbed | `mock_deep_agent` |
| External user databases stubbed | `mock_external_datasources` |
| File writes redirected to tmp | `upload_root` |

Things that will bite you, all of them already load-bearing in the harness:

- **Authentication cannot be faked by injection.** Controllers set
  `dependencies = {"user": require_auth}` as a class attribute, which overrides
  any app-level provider. Use `auth_client_factory`, which mints a real JWT
  cookie.
- **An unauthenticated route returns a redirect, not a 401.** `main.py`'s
  exception handler converts it. Assert 302/307, or a 200 with an `HX-Redirect`
  header for an HTMX request.
- **Routes take `uuid`, never the bigint `id`.** Path params are typed
  `:uuid`. Likewise `CRUDQueryBuilder.get_by_uuid()` takes the public uuid while
  `.update()` / `.delete()` take the internal `id`.
- **No real network.** An autouse guard fails any outbound connection. If a test
  hits it, mock the boundary — do not mark it `external` to get around it.
- **pgvector similarity does not work on SQLite.** The `Vector` column can be
  created but `<=>` cannot run, so tests touching `retrieve_similar_chunks` must
  mock the query layer.
- **Assert real behaviour, not hoped-for behaviour.** If the code returns `None`
  where you expected an exception, test what it does and note the oddity in a
  docstring. Tests are a record of how the system actually behaves.

Match the surrounding style: a docstring explaining *why* a test exists where
that is not obvious, `pytest.mark.parametrize` for table-driven cases, and
classes grouping tests for one function.

## Reporting back to the user

State the numbers the script produced: tests passed/failed, coverage percentage,
the change against the baseline, and the path to the report. If anything failed
or regressed, say so plainly and name the cause. If any test revealed an
application bug, list it separately — that is the most valuable output of a run
and it should not be buried.
