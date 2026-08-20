# TOOL_CHAIN_ITERATION.md
Running a tool's query once per value, and letting the assistant fill in a `:name`

---

# What this is for

[TOOL_CHAINING.md](TOOL_CHAINING.md) covers the ordinary nested tool: a child runs,
one column of its result becomes a list, and the parent's query is restricted to that
list with an `IN`. The parent runs **once**.

Two things that shape cannot express, and this page is about both.

**1. The value is not on the right of an `IN`.** An expanding bind parameter always
renders parenthesised — `IN (?, ?, ?)` — so it is a syntax error anywhere else:

```sql
LEFT JOIN departments dd ON dd.id = :department_id            -- a comparison
AND p.departments LIKE CONCAT('%s:1:"', :department_id, '"%') -- inside a string
```

**2. The question is "for each".** *How many projects does each department have* is
not one query restricted to a list of departments — it is one query run once per
department, with the results put together.

Both are one feature: a link records **how** its values are used.

| | |
|---|---|
| The column | `tool_config_links.binding_mode`, `tool_config_links.value_alias` |
| The rules | `app/services/tool_configs/tool_chain_service.py` |
| The loop | `app/services/tool_configs/tool_chain_graph.py` — `_root_rows` |
| The binding | `app/services/deep_agents/query_executor.py` — `_bindparams`, `_value_conditions` |
| Aggregation | `app/services/agent_recursive_dataframes/aggregate_service.py` — `record_sources` |
| Reading N sources | `app/services/downloader_agents/base/record_reader.py` — `ChainedBatchReader` |
| Form | `templates/tool_configs/partials/nested_tools_field.htm`, `static/js/tool_chain.js` |
| Migrations | `b9c4e7f21a08` (binding mode), `c1d8f3a06b47` (SQL parameters) |

---

# The two binding modes

| `binding_mode` | SQL mode renders | Builder mode builds | Parent runs |
|---|---|---|---|
| `in_list` *(default)* | `bindparam(name, values, expanding=True)` → `IN (?, ?, ?)` | `column.in_(values)` | once |
| `each` | `bindparam(name, value)` — a plain scalar | `column == value` | **once per value** |

`in_list` is the server default, so every link that existed before this feature
behaves exactly as it did. Nothing about a saved tool changes until an operator
chooses otherwise.

**At most one `each` child per tool**, refused on save. Two would run the parent once
per *combination* — ten departments and eight regions is eighty statements inside one
chat turn, past `MAX_CHAIN_ITERATIONS` and so refused anyway, and expensive where it is
not. The query that actually wants writing there is one statement joining both.

## `value_alias` — which run a row came from

Rows from twenty runs of one statement are indistinguishable once concatenated, and a
statement that filters on a department without *selecting* it is perfectly ordinary
SQL. `value_alias` closes that: every row of iteration *i* gets `{alias: value_i}`
merged in, in Python (`query_executor.labelled_rows`), never by rewriting the
statement.

Optional, because a query that already returns the value needs no second copy. Asking
for one anyway is **refused at run time as a column collision**, not resolved
silently:

* overwriting would replace a real value from the database with one from the chain;
* skipping would leave rows whose label says nothing about them.

Both produce a result that looks right and is not.

---

# Assistant-supplied SQL values

Builder mode has always been able to open a *filter* for the model to fill in — it
has a column and an operator the operator chose. A statement has neither, because
nothing in this application parses one. So a SQL-mode tool declares its values
separately, in `tool_configs.sql_params`:

```json
[{"param": "department_id", "type": "number", "required": true,
  "description": "The department to report on."}]
```

Each becomes one field on the tool's argument schema
(`tool_factory._arguments_schema`), exactly as an `agent_supplied` filter does. The
guarantee is the same and unchanged: **what the model supplies is a value, bound as a
parameter, on the right-hand side of a comparison the operator wrote.** It never
chooses a name, a column, an operator, or any SQL text.

`type` (`text` | `number` | `boolean`, default `text`) exists because there is no
reflected column to coerce against the way `_coerced_value` does for builder mode —
binding `"1"` into `dd.id = :x` fails on asyncpg. A value that will not convert falls
back to the string rather than raising: `"abc"` for a number is a value that matches
nothing, which is the right answer to what was asked.

**A required value that was not supplied refuses the run** and tells the model to call
again with a value the user actually gave — never to invent one. An optional one binds
`NULL`, which is what a statement written as `(:x IS NULL OR col = :x)` reads as "no
filter".

**A tool that requires a value cannot be embedded as a child.** An inner tool is never
called by the model — the model calls the parent — so nothing would ever fill it.
Refused when the link is saved, where the message can name the parameter.

---

# Worked example

The query this feature was built for: one department's projects, with the department
id hard-coded in two places.

```sql
SELECT projects.name, projects.crm_id, projects.client_name,
       projects.client_email, dd.id, dd.title
FROM (
    SELECT p.name, p.crm_id, pd.client_name, pd.client_email
    FROM projects p
    LEFT JOIN project_details pd ON p.crm_id = pd.crm_id
    WHERE p.departments LIKE '%s:6:"depart";%'
      AND p.departments LIKE '%s:1:"1"%'      -- department 1
    ORDER BY p.name
) AS projects
LEFT JOIN departments dd ON dd.id = 1;        -- department 1
```

## As two tool configs

**Child** `departments`, SQL mode:

```sql
SELECT id, title FROM departments
```

**Parent** `projects_by_department`, SQL mode (MySQL):

```sql
SELECT p.name, p.crm_id, pd.client_name, pd.client_email,
       dd.id AS department_id, dd.title AS department_title
FROM projects p
LEFT JOIN project_details pd ON p.crm_id = pd.crm_id
LEFT JOIN departments dd ON dd.id = :department_id
WHERE p.departments LIKE '%s:6:"depart";%'
  AND p.departments LIKE CONCAT('%s:1:"', :department_id, '"%')
ORDER BY p.name
```

*(PostgreSQL: `'%s:1:"' || :department_id || '"%'`.)*

**The link:** child `departments`, column `id`, placeholder `department_id`, mode
**run once per value**, `value_alias` **blank** — the statement already selects
`dd.id AS department_id`, so setting the alias would collide and be refused. That is
the collision rule doing its job rather than an inconvenience.

The derived table is gone. It only existed to hold the `ORDER BY`, and one statement
per department does not need it.

## What happens

The child runs once and returns every department id. The parent then runs once per
id, each with `:department_id` bound as a scalar, and the rows are concatenated in the
child's order. The same `:name` appearing twice in the statement is bound once —
SQLAlchemy handles the repetition.

## Then, in chat

> *"how many projects does each department have?"*

`aggregate_records` reads **every** row of **every** iteration and folds them into a
grouping the model plans from that sentence — here `group_by: ["department_title"]`,
`aggregations: [{"type": "count", "column": "crm_id", "alias": "projects"}]`. It reads
the whole result set and reports every group. See
[AGENT_RECURSIVE_DATAFRAMES.md](AGENT_RECURSIVE_DATAFRAMES.md).

---

# One ceiling, and why it refuses

| Ceiling | Default | Env |
|---|---|---|
| Iterations of the parent | 50 | `TOOL_CHAIN_MAX_ITERATIONS` |
| Records an aggregation may read | 200,000 | `AGGREGATE_MAX_SOURCE_ROWS` |
| Groups an aggregation may hold | 100,000 | `AGGREGATE_MAX_GROUPS` |

**Nothing bounds the union itself.** Two ceilings used to: 200 rows across the whole
union and 2,000 values across one `IN` edge. Both are gone, and the union of every
iteration comes back whole.

The 200 was the worse of the two, and this page is where its absurdity showed: a feature
whose entire purpose is to run a statement once per department and put the results
together stopped at a number two departments could reach. The 2,000 was subtler — a
truncated `IN` list produces a query that runs, returns rows, and answers a *different
question*, which is why the chain was refused rather than trimmed.

What is left bounds **round trips, not rows**. `MAX_CHAIN_ITERATIONS` is how many times
the parent may be re-run: an expanding `IN` hands any number of values to the database in
one statement, while an iterating link is one statement per value, each with its own
planning, its own cursor and its own share of a chat turn. Removing it would not return
more data — it would spend the turn and time out with none.

It **refuses rather than truncating**, and that distinction is the load-bearing one on
this page. Rows from the first fifty departments are indistinguishable from rows for
every department; a union short of its last iterations is short of whole *departments*,
not of rows, and no row count says so. A refusal does.

The refusal never asks the visitor to narrow anything. A tool takes no argument they
could change, so any suggestion that they rephrase sends the conversation back to the
same tool and the same refusal, forever.

---

# What is checked when you save

| Refused | Why |
|---|---|
| Two `each` children on one tool | A cartesian product: one statement per combination, past the iteration ceiling |
| `value_alias` on an `in_list` link | One result set has no single value to record, and a field that silently does nothing is one the operator will swear they set |
| `value_alias` that is not an identifier | It becomes a key in the result rows and is grouped by like any other output column |
| `IN :name` bound by an `each` link | Renders `IN ?` — a syntax error the *database* would report mid-conversation |
| `= :name` bound by an `in_list` link | Renders `= (?, ?, ?)` — the same, the other way round |
| A declared `:name` the statement never uses | A field the model is asked to fill on every call for no effect |
| A `:name` nothing fills | Unrunnable. Filled by a nested tool **or** a declared value — one question, one check |
| Embedding a tool that requires a value | An inner tool is never called by the model, so nothing would fill it |

The arity checks (`IN :x` / `= :x`) are text checks over the statement with literals
blanked, so they see the shape immediately next to the placeholder and nothing
cleverer. That is the mistake operators actually make, and it is worth twenty lines to
catch it at save time rather than months later.

---

# How the aggregation reads a fan-out

`aggregate_service.record_sources(entry)` turns one tool entry into the things the
reader reads:

| The tool | Sources |
|---|---|
| No children | one, unrestricted |
| List children | one, **carrying the children's values** |
| An `each` child | **one per value** — same statement, different bind, its own label |
| A chain that matched nothing | none, and `stopped_by` names the tool |

`record_reader.ChainedBatchReader` reads them as if they were one: one cursor at a
time, rolling to the next when the current is exhausted, returning nothing only once
**every** source is spent. A source that legitimately matches nothing rolls forward
rather than ending the run — otherwise a department with no projects would silently
truncate the answer at that department. `count_all` sums across them and checks the
ceiling before anything opens.

Nothing in `partial_algebra` or `frame_ops` changed. The fold is already
order-independent and mergeable, so N cursors read in order fold into one running
frame exactly as N waves of one cursor do.

**This also fixed a live bug.** Before `record_sources` existed, an aggregation over a
nested tool built its source from the stored config alone and dropped the child's
values — so the totals were over a wider result set than the tool has ever returned,
with nothing about the answer saying so. There is a test whose only job is to keep
that from coming back
(`tests/unit/services/agent_recursive_dataframes/test_aggregate_sources.py`).

---

# The same idea, one layer up

The Graph Designer reuses this feature's machinery rather than growing its own. A `for_each`
node loops over one node's rows and can **collect** a body node's result across passes,
labelling each row with the item that produced it through the same
`query_executor.labelled_rows`, and offering the same two binding shapes on a statement's
`:name` — one value, or the whole list as an expanding `IN`. The arity check is literally the same function
(`sql_guard.placeholder_shape`), which both this module and `graph_service` now call.

Two differences worth knowing, both consequences of a graph being drawn rather than nested:

* **A graph iterates explicitly.** Here the *link* says "run once per value" and the parent
  loops invisibly; there the author draws a loop node, so the binding never iterates — it
  supplies one value per pass, and the loop is what goes round.
* **A graph can carry the union forward.** An iterating link's rows are the answer and stop
  there. A collecting `for_each` publishes its union as its own output, so another node can
  read it, branch on it or loop over it again.

See [GRAPH_DESIGNER.md](GRAPH_DESIGNER.md).

---

# Not covered

* **No cartesian fan-out.** One iterating child per tool, by design.
* **The rows of an iteration are still discarded at the edge.** Only the root's rows
  are the answer, and only one column crosses any edge.
* **Nothing composes with `OR`.** Every binding adds a condition, and they are AND-ed.
* **The iterations are sequential.** They are not run in parallel: the row budget is
  spent in order, so the run has to be able to stop the moment the union would exceed
  what a tool may return, and a parallel fan-out would need a barrier to know that —
  by which point every query has already run.
* **A declared SQL value cannot be type-checked against the statement.** Nothing
  parses it, so `type` is the operator's claim about the value, not a fact derived
  from the query.
