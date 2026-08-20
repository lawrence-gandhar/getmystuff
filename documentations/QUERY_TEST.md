# QUERY_TEST.md
Test Query — running an unsaved query once, before anyone depends on it

---

# What it is

A **Test Query** button on every panel that holds a query which is not saved yet:

* the Tool Configs form, in the header of the **Generated SQL** card (builder mode);
* the same form's **SQL Query** card (SQL mode);
* the Ask AI panel, in the header of the generated query.

Pressing it runs that query once against the datasource, reads a single row, and
reports what happened — the columns that came back, or the reason it did not run.

```
POST /query-test        (app/routes/query_test/query_test_routes.py)
  → query_test_service.test_query
      → tool_config_service.validated_tables / validated_query_config / validated_tool_sql
      → query_executor.probe_tool_query          ← the real query, really run
  → templates/query_test/partials/result.htm     ← one alert, green or red
```

---

# Why it exists

Everything between writing a tool config and an agent calling it is checked. The
shape of the config, every identifier in it, that the statement is a single read,
that each table is still switched on — all of that is enforced twice, on save and
again on every run.

None of it can answer the question that decides whether the tool works: **will this
database run this query?** That is the database's answer to give. A grouping MySQL
refuses under `ONLY_FULL_GROUP_BY`, a column that exists in staging and not in
production, a join whose ON clause names the wrong side — every one of those passes
every check the application can honestly make, and fails at run time:

> SELECT list is not in GROUP BY clause and contains nonaggregated column
> 'teamtracking.project_details.client_name' which is not functionally dependent on
> columns in GROUP BY clause; this is incompatible with sql_mode=only_full_group_by

Before this button, that sentence first appeared in a chatbot conversation, addressed
to a visitor, as *"I cannot retrieve that figure right now."* The operator who could
fix it never saw it. Now it appears under the query, while the form is still open.

---

# The query that is tested is the query that will be saved

Three things make that true, and each of them would be easy to get wrong:

**The same validators.** `_validated_query` calls
`tool_config_service.validated_tables`, `validated_query_config` and
`validated_tool_sql` — the functions the save itself calls. A query that fails the
test fails the save with the same sentence; one that passes the test is one the form
will accept. A test with looser rules would pass queries that then cannot be created,
which is worse than no test.

**The same execution.** `query_executor.probe_tool_query` is the entry point beside
`execute_tool_query`, sharing everything below it: the reflection, the bound
parameters, the active-table and active-column rules, the read-only guard. A test
that ran a rendered preview string instead would be testing a different query — and
the preview is explicitly a display artefact that inlines filter values, never
executed anywhere.

**The same fields.** The button posts the form with `hx-include`, so `query_mode`
decides which query is tested exactly as it decides which query is stored. Both are
always in the payload; testing in builder mode while the SQL panel holds a broken
statement passes, because that statement is not what will be saved.

---

# Two audiences, one failure

`query_executor.ToolQueryError` carries the fault as its message and the *advice*
separately:

```python
raise ToolQueryError(inactive_column_message("orders.total"))          # the fault
exc.for_agent   # "… Tell the user the tool needs reconfiguring."      # + the advice
```

`tool_factory` renders `for_agent`, because a model that reads a bare fault will try
to work around it. The test panel renders `str(exc)`, because the person reading it
*is* the user someone would be told to tell.

The same split decides what happens to a driver error. `execute_tool_query` replaces
it with "the query could not be run against the database" — the driver's text can
name schema objects and echo values, and it is going into a prompt.
`probe_tool_query` lets it out untouched, and the service shows it: this is the
operator's own datasource, and "Unknown column 'crm_id' in 'on clause'" is the only
sentence that says where to look. It is trimmed to `MAX_DATABASE_MESSAGE` and
stripped of SQLAlchemy's `[SQL: …] [parameters: …]` tail, which is noise when the
statement is already on screen above the alert.

---

# A nested tool is tested as a chain

A tool that embeds others is tested by running **the whole chain** — the same graph an
agent's call would run, compiled with a one-row limit on the root. Testing the outer query
with the children skipped would test a different, unrestricted query, and a pass on it would
say nothing about the tool that is about to be saved.

The children are validated by the same function the save uses, against the query *as the
form currently has it* — the mode, the statement and the tables may all be changing in this
same edit — so a nesting the save would refuse is refused here, in the same words.

If an inner tool matches nothing, the test **passes** and says so:

> The chain ran, but 'paid_invoices' matched nothing, so this query was not reached. Every
> query is valid — the tool would return no rows until that inner tool matches something.

That is the honest answer to what the button asks. Every query ran and the database accepted
all of them; the tool would simply return nothing today, which is worth knowing and is not
the same as being broken. See [TOOL_CHAINING.md](TOOL_CHAINING.md).

An iterating link runs the root once per value here too, so a test of one is a test of the
loop rather than of one pass through it.

---

# A statement that asks for a value gets one from the operator

A SQL-mode tool holding `:department_id` cannot run without a value for it, so the
Values card carries a **test value** beside each declared parameter. It is used by this
button and **is not saved** — it goes into its own hidden field, never onto the tool
config.

The split is the point. The button's whole claim is that it ran the query the tool will
run, and the only honest value to run it with is one the operator typed. Filling it with
something invented would prove the statement runs for a value nobody chose. See
[TOOL_CHAIN_ITERATION.md](TOOL_CHAIN_ITERATION.md).

---

# What a test does not do

* **It writes nothing.** The statement is read-only by construction — the guard runs
  before the connection is used — and a write is refused without reaching the
  database.
* **It reads one row.** `PROBE_ROWS = 1`. The query still *runs* in full on the
  database, as any real call would; the cap bounds what crosses the wire.
* **It shows no values.** The verdict is the column names and the row count. Proving
  a query runs needs a row fetched, not a row displayed, and in Ask AI printing one
  would break the single promise that feature makes — that the panel shows structure
  and never contents.
* **It does not save anything.** Pressing Test on a half-finished form leaves no
  trace; nothing is written to `tool_configs`.
* **It is not offered where it cannot run.** The builder's button appears only for a
  relational datasource (`supports_sql`, the same flag that enables SQL mode): a
  Mongo or CSV datasource has no query the executor can run, and offering the button
  would only produce a refusal.

---

# Every outcome is a result

`test_query` never raises. The route has no `try/except` and no error branch,
because "the database refused it" is the answer the endpoint was asked for, not a
fault in the request. Four shapes of failure, each fixed somewhere different:

| What went wrong | Where it comes from | What the panel says |
|---|---|---|
| No datasource, an inactive table, a non-relational datasource | the service's own checks, worded for the form | "Activate them in Data Sources or remove them from the query" |
| An invalid config or statement | `HTTPException` from the save's validators | the validator's own sentence, naming the field |
| The tool could not be assembled | `ToolQueryError` — inactive column, missing table, RIGHT JOIN | the fault, without the agent-facing advice |
| The database refused it | `SQLAlchemyError` | the driver's own words |
| The datasource is unreachable | anything else, logged in full | "Could not connect to the datasource, so the query was not run" |

That last row is deliberately not phrased as a query problem: telling someone their
SQL was refused when the host was simply down sends them off editing a query that is
already correct.

---

# Why its own module

Both callers are peers. The Tool Configs form and the Ask AI panel ask the identical
question and must get the identical answer — a query that passes in one panel and
fails in the other would be worse than no button at all. Hanging the logic off Tool
Configs would make Ask AI a client of a feature it has nothing else to do with, so
`query_test` is its own schema package, service, controller and template, and the
execution it shares lives where execution already lived.

---

# Related

* [TOOL_QUERY_MODES.md](TOOL_QUERY_MODES.md) — the two ways a tool query is written,
  and the shared read-only guard
* [SQL_ASSIST.md](SQL_ASSIST.md) — the Ask AI panel the second button lives in
* [DEEP_AGENTS.md](DEEP_AGENTS.md) — `query_executor`, and what an agent is told when
  a tool fails for real
* [SCHEMAS.md](SCHEMAS.md#query_test--appschemasquery_testquery_test_schemaspy) —
  `QueryTestRequest` / `QueryTestResponse`
