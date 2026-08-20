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

**Builder mode has one capability SQL mode does not: a filter whose value the agent
supplies.** Ticking *Agent fills in* on a filter stores no value for it and names a
parameter instead, so one tool answers "projects in August" and "projects in September"
rather than needing a config for each. The agent supplies the value only — the column,
the comparison and every other filter stay exactly as the operator set them, and the
value is bound as a parameter by the same code path that binds a stored one.

SQL mode reaches the same place by a different route, because the mode's usual reason
still applies: nothing here parses the statement, so there is no clause to open. So
the values are **declared beside the statement** (`sql_params`) and the operator
writes the `:name` and the comparison themselves. One field per declared value on the
tool's argument schema, exactly as a filter produces — and the same guarantee, that
what the model supplies is a value on the right-hand side of a comparison the operator
wrote. See
[DEEP_AGENTS.md](DEEP_AGENTS.md#tools-take-no-arguments--unless-an-operator-opens-one-filters-value)
and [TOOL_CHAIN_ITERATION.md](TOOL_CHAIN_ITERATION.md).

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

## Filter operators

`=`, `!=`, `>`, `<`, `LIKE`, and four that compare against **nothing**:

| Operator | What it matches |
|---|---|
| `IS NULL` | the column is absent |
| `IS NOT NULL` | the column is present — **including `''` and `'   '`** |
| `IS BLANK` | null, empty, or nothing but whitespace |
| `IS NOT BLANK` | has a real value — what "not empty" means when asked for out loud |

The last two exist because of a trap the first set could not avoid. A text column can be
absent, empty or whitespace, and to a person reading a report those are one thing: no
value. Expressing that took `!= ''`, and the builder ANDs its conditions, so that filter
**silently keeps every NULL row** — there is no second filter that fixes it and nothing in
the form that shows it. `IS NOT BLANK` is one filter that means what was intended.

Two details worth knowing:

* **`TRIM` is applied only to a text column.** Postgres has no `btrim(integer)`, so
  trimming a number is not a stricter check, it is an error — and a number has no empty
  string to catch. For a non-text column `IS BLANK` is exactly `IS NULL`, which is the
  whole of what blank can mean for one. The decision is made from the *reflected* type, so
  it follows the column the database has now rather than the one it had when the tool was
  saved.
* **These store no value at all**, not an empty one. Switching an existing filter to
  `IS NULL` drops whatever was in its value box, and drops the *Agent fills in* flag with
  it — there is no value for an agent to supply. A stored field that provably cannot
  affect the query is one somebody reads as meaningful later.

The generated-SQL preview shows what they stand for (`(technology IS NOT NULL AND
TRIM(technology) <> '')`) rather than the dropdown's label, because that preview is read as
SQL — by the operator checking the query, and by the model in its routing prompt. The
routing prompt itself says it in English instead: *"technology has a real value (not empty
or blank)"*.

Ask AI knows about them too, so `col IS NULL` and the `col IS NOT NULL AND TRIM(col) <> ''`
pair now convert into a builder tool instead of falling back to SQL mode.

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
| `star_selection_violation(sql)` | The offending text when the statement selects `*` or `table.*`, or `None`. Used by Ask AI only. `COUNT(*)` is **not** a star selection — it names no columns, and refusing it would break every "how many" query. |
| `forbidden_identifier(sql, forbidden)` | The first forbidden name the statement mentions. A bare `id` deliberately does not match inside `orders.id`, so forbidding a column on one table cannot reject a query reading another's. |
| `missing_identifiers(sql, required)` | Which required names are absent. **Advisory** — presence in the text is not proof of being in the SELECT list, so callers report rather than refuse. |
| `group_by_violation(sql, primary_keys)` | The first column the statement selects without aggregating or grouping it, or `None`. Used by Ask AI only. **Deliberately silent when unsure** — see below. |

`read_only_violation` refuses four things, in order: over-length, not starting with `SELECT`
or `WITH`, containing a `;`, and containing a write verb. Everything else passes.

The last three are text checks one level up, not a parse. They can tell that a name appears in
a statement and that a `*` is being selected; they cannot tell which clause a name is in, or
that a name in a subquery belongs to a different scope. That is enough for what Ask AI needs to
know about a generated query, and honest about being a heuristic — the guarantee that only
active columns are ever *read* lives in `query_executor`, not here.

### The grouping check

`group_by_violation` answers a different kind of question from the rest: not "may this run"
but "**can** this run". MySQL's default `sql_mode` includes `ONLY_FULL_GROUP_BY` and PostgreSQL
has the same rule built in, so

```sql
SELECT project_details.client_name, COUNT(*) FROM project_details GROUP BY status
```

is refused by the database itself:

> SELECT list is not in GROUP BY clause and contains nonaggregated column
> 'teamtracking.project_details.client_name' which is not functionally dependent on columns in
> GROUP BY clause; this is incompatible with sql_mode=only_full_group_by

Nobody who did not write that query can be expected to spot it, and left alone it surfaces at
the worst moment — as a saved tool failing mid-conversation with a visitor.

`primary_keys` (table → its key columns, straight from the reflection) is what keeps the check
honest about the shape both databases *do* allow: group by a table's primary key and its other
columns come along, functionally dependent. Pass it whenever the schema is to hand.

It stays silent — returns `None` — whenever the statement is more than a text check can read:
more than one `SELECT` (a CTE, a subquery, a `UNION`), a `GROUP BY` holding an ordinal or an
expression, or a selected item that is not a plain column reference. That asymmetry is on
purpose: a missed violation ends as a clear message from the database, a false one sends the
model off rewriting a query that was already right.

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
3. **A row limit, where a caller asks for one, is applied by streaming rather than by
   rewriting the SQL.** `connection.stream()` opens a server-side cursor where the driver
   supports one, and `fetchmany(limit)` stops there. Wrapping the statement as
   `SELECT * FROM (…) LIMIT n` would have been simpler and is wrong twice over: it changes
   the SQL the operator approved, and MySQL rejects a derived table with duplicate output
   column names — so `SELECT a.id, b.id FROM a JOIN b`, a query this mode exists to make
   possible, would fail for a reason having nothing to do with the query.

## No query is capped

**Every matching row comes back, in either mode.** There was a flat ceiling of 200 rows on
every tool query — `MAX_TOOL_ROWS` — and it has been removed, because that one number meant
two incompatible things: how much a language model can be handed, and how much of somebody's
data exists. On any real table those came apart. A tool answering a question about 5,275
records returned 200 of them, and nothing about the result said which of the two numbers it
was, so a total taken over it was a plausible figure that was wrong.

The **operator's own `LIMIT`**, or its absence, is now the only statement about how much data
a question is about — which puts the size of the answer where the person writing the query can
see it.

One bound remains, and it is not on a query. `PROMPT_ROW_LIMIT = 200` is how many rows
`describe_result` serialises into a **prompt**, where a context window is a physical size and
the alternative to shortening is not a longer answer but a turn that fails outright. What the
uncapped fetch bought is the honesty of that line: every row was read, so the header states
the exact total beside the sample — `200 row(s) out of 5275 matching record(s)` — where the old
text could only warn that a total was unknowable. `DISPLAY_ROW_LIMIT = 100` still governs how
many the model may *print*, by instruction rather than truncation.

Everything that moves rows between components rather than into a prompt sees all of them: a
chain's root query, an inner tool's `IN` list, an export, a Graph Designer node, an
aggregation. What is still bounded elsewhere is never the size of an answer — round trips
(`MAX_CHAIN_ITERATIONS`), statement length (`MAX_SQL_LENGTH`), records an aggregation may read
and groups it may hold in memory (`AGGREGATE_MAX_SOURCE_ROWS`, `MAX_GROUPS`), and an export's
ceiling (`MAX_EXPORT_ROWS`). Every one of those **refuses loudly** rather than trimming a
result, and the last four are operator-settable.

SQL mode gets the **table** half of the active rule and not the column half. Every table the
tool records — `table_name` plus `extra_tables` — is checked before the statement runs, which is
what the multi-select exists to make possible. The columns cannot be checked without rewriting
the statement, and rewriting the operator's SQL is precisely what this mode exists not to do. The
trade is stated rather than half-solved: choosing SQL mode means the statement is the permission
at column level.

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
| Reads | Base table, joined tables named | Every recorded table named |
| Returns | Every field named, with its alias | "whatever columns the query below selects" |
| Query | Rendered from the config | The statement, quoted in full |

The field list is what the model quotes back in an answer, and nothing has parsed a stored
statement's SELECT list. Guessing one would have the model naming a column that is not in the
result — so it is told to read the field names off the result instead, which is the truthful
instruction.

The *table* list is a different matter: it is recorded on the row rather than inferred, so a
SQL-mode entry names all of it. It used to read "the primary table, and any tables its query
joins", which told the model a two-table tool was a one-table tool.

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

### A tool records every table it reads

The **Tables** field is a multi-select in both modes, and the row records all of it:

```sql
tool_configs
  table_name    VARCHAR(255) NOT NULL   -- "projects"           the primary table
  extra_tables  JSONB NULL              -- ["project_details"]  the rest
```

The **first** selection is the primary table. That ordering carries meaning rather than being
presentation — in builder mode it is the base table every join hangs off and every bare column
reference means — so the list is never sorted, and re-ordering it re-points the query.

Why the extras are recorded at all, when a builder query's joins already name them: a **SQL**
query's tables live only inside its statement, and nothing in this application parses a FROM
clause. Before the column existed, a tool whose statement read
`projects LEFT JOIN project_details` recorded `projects` and no more. Two things were wrong as a
result — the routing prompt told the agent it read one table when it read two, and nothing could
check the others were still switched on in Data Sources. `NULL` and `[]` both mean "one table",
which is what every row written before the column means.

In **builder mode** the extras are the tables the Joins card may join to, and the two fields are
held to agreeing: `_builder_context` offers only the selected tables as join candidates, and
`_require_joins_within_selection` refuses a saved join onto a table outside the list. Without
that check a built query's recorded scope could be narrower than what it reads, which is the same
bug from the other direction.

In **SQL mode** they are simply what the operator says the statement reads. Nothing verifies it
against the text — that is why the form asks.

### Test Query

Both query cards carry a **Test Query** button in their header — the builder's
*Generated SQL* card (for a relational datasource; `supports_sql`, the same flag that
enables SQL mode) and the SQL panel. It posts the form as it stands to `/query-test`,
which runs the query once against the datasource through the executor itself and
swaps a verdict in below the card.

It matters most in **SQL mode**, where nothing in this application parses the
statement: the database is the only thing that can say whether it works, and until
the button existed it said so for the first time inside a conversation with a
visitor. Full detail in [QUERY_TEST.md](QUERY_TEST.md); the short version is that the
test uses the save's own validators and the agent's own execution path, reads one
row, shows no values, and reports the driver's own message when the database refuses.

### Nested tools

A tool may embed other tools as sub-queries: the inner tool runs first and its
values restrict this query — an extra `IN` on a column in builder mode, a
`:placeholder` bound at execution in SQL mode. The **Nested Tools** card sits above
the query for that reason: it belongs to both modes.

The statement is never rewritten to make that work. `:active_clients` is an
*expanding bind parameter*, so what runs is still the exact text `validated_tool_sql`
approved, with a list bound to it. Every `:name` in a statement must be filled by a
child or by a declared value, and every child must name a `:name` that is in it, both
checked on save — either way round the statement could not run.

A link may also bind **one value at a time** and run the query once per value, which
is the shape needed wherever a list cannot go — `dd.id = :x`, or a pattern the
database builds around it. And a SQL-mode statement may declare values the assistant
fills in from the user's question, which is this mode's answer to builder mode's
"Agent fills in" filter: the mode has no filters to open, so the value is declared
beside the statement and the operator writes the comparison.

Full detail in [TOOL_CHAINING.md](TOOL_CHAINING.md) (the LangGraph, the value caps,
the refusals, and why a tool something embeds cannot be deleted or disabled) and
[TOOL_CHAIN_ITERATION.md](TOOL_CHAIN_ITERATION.md) (the two binding modes, the
assistant-supplied values, and a worked example).

**A second caller reaches this same path.** A `sql` node in the Graph Designer declares
parameters in the same `{param, type, required}` shape, binds them through the same
`assemble_sql_statement` — one value through the declaration, a whole list as an expanding
`IN` — and passes the same `validated_tool_sql` and `validated_tables` on the way in. So a
statement that saves as a tool saves as a node, and neither of them is ever rewritten. See
[GRAPH_DESIGNER.md](GRAPH_DESIGNER.md).

A graph node still passes `max_rows=None` explicitly, even though that is now the default for
every caller. It is left in deliberately: a node's guarantee about the operator's data should
not depend on a default somebody could change in another module. A graph run *as* an agent's
tool reports through a preview capped at twenty rows with the real total beside it, so what a
model reads off a graph is bounded by `graph_state.preview_of` and not by anything a node did.

**And one caller now *composes* a statement.** A Graph Designer `sql_union` node joins one copy
of its fragment per pass of a loop into a single query — the only place in the application that
writes SQL rather than passing on what somebody wrote. It is worth recording here because this
page is where the guarantee lives, and the guarantee is unchanged: each copy's placeholders are
**renamed** (`:id` → `:id__p7`) and each pass's value is bound under its own name, so N
fragments carry N bind parameters and nothing is substituted into the text. What the composed
statement gets is a larger *length* ceiling — `MAX_BUILT_SQL_LENGTH`, threaded as an optional
`max_length` and defaulted everywhere else — and nothing more: it is still re-validated as one
read-only statement with no second statement and no write verb in it, on every run.

### A grouped query may only select what it groups

`validated_query_config` refuses a builder config that aggregates or groups while selecting a
column outside the grouping (`_require_grouped_selection`), with the message naming both ways
out — add it to Group By, or aggregate it. The same rule as the SQL-mode check above, but here
it is a structured config rather than text, so it is **refused rather than reported**: a config
like this can never run, and letting it be saved only moves the failure to a conversation.

An **empty Columns list is not the exception it looks like**. It means "every column", which
`query_executor._selected_columns` expands to every active column of every table the query
reads — so a grouped query that chooses no columns is the same violation written shorter, and
is refused with its own message.

A column functionally dependent on a grouped primary key is legal SQL and is refused here
anyway. Nothing at this layer knows which columns are keys — the config is validated without
touching the datasource — and the fix the message asks for, grouping by that column too,
returns exactly the same rows when the dependency is real.

`static/js/tool_configs.js` says the same thing in the builder as the rows change
(`groupingProblem`, mirroring `_require_grouped_selection` down to the wording), in its own
alert rather than through `showNotice` — that one carries one-off messages about an action just
taken, this is a standing statement about the query as it currently reads. It warns, and the
server still refuses: the browser check is the earlier, gentler half.

A tool saved before this rule existed is caught on its next run: the executor re-validates a
stored config, the refusal arrives as an `HTTPException`, and the agent is told the tool needs
reconfiguring instead of the driver error reaching a visitor.

### Only active tables and columns are offered

The Table dropdown, the field pickers and the join column pickers all show only what the user
has left switched on in Data Sources — see
[SERVICE_PATTERNS.md](SERVICE_PATTERNS.md#who-reads-the-status--apputilsdatasource_statuspy).
Not greyed out, not flagged: a tool config is a standing permission for an agent to read
something, and a switched-off table is exactly the thing the user has said an agent may not
read. A datasource with everything switched off says so, rather than presenting an empty
dropdown with no explanation.

Saving does not re-check the status, deliberately — the pickers are the only place a status read
touches authoring, so a datasource that is momentarily unreachable, or a column switched off
after the fact, never makes an existing tool config uneditable. It makes it unrunnable until it
is fixed, which is `query_executor`'s business.

An empty selection in the builder previews as `SELECT *`. At run time that expands to **every
active column of every table the query reads, joined tables included** — the preview's `*` is
shorthand for that, not for whatever the table happens to hold.

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

```sql
ALTER TABLE tool_configs
    ADD COLUMN extra_tables JSONB NULL;
```

Migration `e7b3f5a91c26` — the other tables a tool reads. Nullable with no default and no
backfill: `NULL` and `[]` both mean "one table", which is what every pre-existing row means, so
again a release that only runs the migration behaves identically.

Its downgrade drops the column and **loses no query** — a builder query's joins are still in
`config`, and a SQL statement is its own record of what it reads. What comes back is the old
understatement of a tool's scope, not data loss, so unlike `sql_query` it reports the affected
rows and proceeds rather than refusing.

```sql
ALTER TABLE tool_configs ADD COLUMN sql_params JSONB NULL;
```

Migration `c1d8f3a06b47` — the values a SQL-mode statement asks the assistant for.
Nullable with no default and no backfill: `NULL` and `[]` both mean "this tool takes
no arguments", which is what every pre-existing row means, so no routing prompt
changes and no tool's argument schema gains a field.

Its downgrade drops the column. A tool whose statement used a declared placeholder
then has nothing to fill it, so it stops being savable and fails when it next runs —
a downgrade to run before such a tool exists, not after.

`table_name` stays a scalar column rather than being folded into the list, because its role
really is singular: the primary table, the base table joins hang off, the one a bare column
reference means. `tool_config_service.tables_read()` is the single place the two are put back
together, so the list page, the edit form, the routing prompt and the executor cannot disagree
about which tables a tool reads.
