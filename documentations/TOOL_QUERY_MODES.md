# TOOL_QUERY_MODES.md

The two ways a tool config holds its query — the query builder, and raw SQL — and the shared
guard that decides what a tool is allowed to run.

---

# What it is

A **tool config** is one query a data agent is allowed to run (see
[DEEP_AGENTS.md](DEEP_AGENTS.md)). It holds that query one of two ways, recorded on
`tool_configs.query_mode`:

| Mode | Stores | Written by |
|---|---|---|
| `builder` | `config` — columns, aggregations, `group_by`, filters, joins | The query builder in the New/Edit Tool Config offcanvas, or Auto Create Tool |
| `sql` | `sql_query` — one read-only statement | The SQL editor in the same offcanvas, or Auto Create Tool when the builder cannot hold the query |

Exactly one of the two is populated. Saving in either mode clears the other, so a tool that
was switched from SQL back to the builder cannot leave a stale statement behind for the
executor to prefer.

**Why both exist.** The builder is a deliberate subset of SQL, and it is the stronger
artefact: every identifier in it is checked against the tables the query reads, every filter
value becomes a bound parameter, and the whole query is rebuilt from reflected `Column`
objects at run time rather than assembled as text. But it is a subset — `DISTINCT`,
`ORDER BY`, `LIMIT`, `HAVING`, subqueries, CTEs, window functions, `UNION`, `CASE` and
expressions in the SELECT list all fall outside it. Before SQL mode, a query needing any of
those could not be saved as a tool at all, even when the operator had read the SQL and
approved it. Ask AI would write the query, show it, and then refuse to save it — an
assistant that produces something it will not let you use.

So the rule is now: **if it is a valid read-only query, a tool can run it.** Which mode it
lands in is a question of how well the builder can hold it, never of whether it is allowed.

---

# The shared guard — `app/utils/sql_guard.py`

Three features hand SQL around, and all three need the same answer to the same question.
It is answered once, here, and each caller phrases the refusal in its own words.

| Caller | When | On refusal |
|---|---|---|
| `sql_assist_service._validated_sql` | Before a generated query is displayed | **502** — the model returned something unusable; the user did not cause it |
| `tool_config_service.validated_tool_sql` | Before a statement is stored | **400** — the operator can fix it, and the message says how |
| `query_executor._execute_sql_query` | Before every single run | `ToolQueryError` — the agent is told the tool needs reconfiguring |

| Name | Purpose |
|---|---|
| `MAX_SQL_LENGTH` | `8000`. Long enough for any hand-written or generated query; short enough that a pasted dump is refused before it is stored, previewed and run. |
| `normalised_sql(sql)` | The statement as it should be stored: trimmed, markdown fence removed, trailing semicolon dropped. Normalise rather than reject — a fence is a model's habit, a semicolon is a person's. |
| `stripped_literals(sql)` | Quoted spans and comments blanked out, so the structural checks read code and not content. |
| `read_only_violation(sql)` | Why the statement may not be run, as a phrase, or `None`. |

`read_only_violation` refuses four things, in order: over-length, not starting with `SELECT`
or `WITH`, containing a `;`, and containing a write verb. Everything else passes.

### What it does not check

**Syntax.** Dialects differ, and a parser strict enough to be worth trusting would reject
valid queries (`DISTINCT ON`, `LATERAL`, `QUALIFY`, vendor functions). A syntax error
surfaces when the tool runs, named by the database. A query that passes the guard and is then
rejected by the driver is the expected way a typo is found.

### Why the literal-stripping matters

Without it, `WHERE action = 'delete'` reads as a `DELETE` and `WHERE note = 'a;b'` as two
statements — both perfectly ordinary reads. The word-boundary regex covers the rest:
`created_at`, `updates` and `deleted` are not verbs.

The write list is deliberately short. `PRAGMA`, `COPY`, `CALL`, `SET` and `VACUUM` are only
valid at the start of a statement (already refused) or after a `;` (refused separately), so
listing them would add nothing but false rejections of a column named `call` or a table named
`copy`.

---

# Running a SQL-mode tool — `query_executor`

The statement runs **as written**. There is no config to rebuild, and running an
approximation of a query the operator approved would defeat the point of the mode.

Three properties hold it together:

1. **Nothing the model produces is in the statement.** It was written and saved in advance,
   and the tool takes no arguments — the same guarantee builder mode gives, arrived at
   differently. There is still no path from model output into a query.
2. **It is re-validated on every run**, not just when it was saved. A row edited straight in
   psql, or written by an older version of the application, is held to the same rule.
3. **The row cap is applied by streaming, not by rewriting the SQL.** `connection.stream()`
   opens a server-side cursor where the driver supports one and `fetchmany(limit)` stops at
   the cap. Wrapping the statement as `SELECT * FROM (…) LIMIT n` would have been simpler and
   is wrong twice over: it changes the SQL the operator approved, and MySQL rejects a derived
   table with duplicate output column names — so `SELECT a.id, b.id FROM a JOIN b`, a query
   this mode exists to make possible, would fail for a reason having nothing to do with the
   query.

The statement still *runs* in full on the database; an unfiltered aggregate scans what it
scans. The cap bounds what crosses the wire and what reaches the prompt, which is what it is
for. `MAX_TOOL_ROWS = 200`, and it applies to every tool query in either mode.

### Non-relational datasources

SQL mode is refused for a CSV file or a Mongo collection — there is no statement to run
against either. It is refused **at save time**, by `_validated_query_mode`, rather than
being stored and failing on the agent's first call: a tool that can never run is a
configuration mistake, and the operator is standing in front of the form. The mode is also
not offered in the UI for such a datasource (`supports_sql`).

### RIGHT JOIN

`tool_factory.find_unsupported_tools` flags a builder-mode tool that uses a RIGHT JOIN,
because the accumulating `Select` builder cannot express one. That is a *builder* limitation
and is not applied to a SQL-mode tool, whose statement may right-join freely.

---

# The routing prompt

`prompt_builder` describes a SQL-mode tool differently, and the difference is deliberate:

| | Builder mode | SQL mode |
|---|---|---|
| Reads | Base table, joined tables named | Primary table, "and any tables its query joins" |
| Returns | Every field named, with its alias | "whatever columns the query below selects" |
| Query | Rendered from the config | The statement, quoted in full |

The field list is what the model quotes back in an answer, and nothing has parsed a stored
statement's SELECT list. Guessing one would have the model naming a column that is not in the
result — so it is told to read the field names off the result instead, which is the truthful
instruction.

---

# The form

One offcanvas, two panels, both always in the DOM
(`templates/tool_configs/partials/query_mode_field.htm`). `static/js/tool_configs.js` shows
one of them and moves the `required` attribute with it — a hidden `required` textarea makes
the browser refuse to submit while pointing its validation bubble at something off screen.

Both queries are submitted every time and `query_mode` says which one is meant; the service
discards the other. That is why switching modes mid-edit loses nothing, and why a switch made
by mistake can be switched back.

Changing the **datasource** re-renders the mode selector out of band along with the Table
field and the query builder. All three go together for the same reason: a query — written or
built — belongs to the datasource it was written against.

---

# Auto Create Tool

Ask AI's conversion tries the builder's shape first, because a builder tool is the stronger
artefact and reopens fully editable in the builder. It falls back to SQL mode in two cases:

* the model answered `fits=false` and named what was in the way (`DISTINCT`, `ORDER BY`, a
  subquery…);
* the model answered `fits=true` and then described the query wrongly — an invented column,
  an ambiguous reference, a base table the user did not select. `_validated_tool_draft`
  catches that, and the SQL is saved rather than thrown away: it was never in doubt, only the
  model's reading of it was.

Either way the panel shows the same create form, with a note saying which mode it landed in
and why. See [SQL_ASSIST.md](SQL_ASSIST.md).

---

# Storage

```sql
ALTER TABLE tool_configs
    ADD COLUMN query_mode VARCHAR(16) NOT NULL DEFAULT 'builder',
    ADD COLUMN sql_query  TEXT NULL;
```

Migration `c3a7d5e18b64`. Every pre-existing row is a builder row, which is exactly what the
server default gives it — no backfill, and a release that only runs the migration behaves
identically to the one before it.

The **downgrade loses every SQL-mode tool**, because their query lives nowhere else, so it
reports how many would be lost and refuses rather than deleting them silently.

`query_mode` is a `VARCHAR`, not a database enum: a third mode would otherwise need a
migration on the type itself, and the value is validated by `tool_config_service` on every
write anyway.
