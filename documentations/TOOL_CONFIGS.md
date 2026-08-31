# TOOL_CONFIGS.md

How to use Tool Configs — the practical guide, one worked example per scenario.

A **tool config** is one query a data agent is allowed to run. This page is about *using*
the feature: what to type into each field, what gets stored, what the agent then does with
it, and which scenario each option exists for. The internals live elsewhere and are linked
where they matter — [TOOL_QUERY_MODES.md](TOOL_QUERY_MODES.md) (why there are two modes and
what `sql_guard` refuses), [QUERY_JOINS.md](QUERY_JOINS.md) (the join rules),
[TOOL_CHAINING.md](TOOL_CHAINING.md) and [TOOL_CHAIN_ITERATION.md](TOOL_CHAIN_ITERATION.md)
(nesting), [DEEP_AGENTS.md](DEEP_AGENTS.md) (how a tool becomes something a chatbot calls).

---

# The mental model

```
Data Agent            one assistant, one job                (Data Agents page)
  └── Tool Config     one question it can answer with one query   ← this page
        ├── Datasource + tables it may read
        ├── The query — built, or written as SQL
        ├── Optional: values the assistant fills in per call
        └── Optional: other tools embedded as sub-queries
```

Three things follow from that, and every scenario below is a consequence of one of them:

1. **The operator writes the query; the agent only chooses when to call it.** A tool runs
   exactly as saved. The model never writes SQL, never picks a column, and never sees the
   database — it sees a tool name, a description, and whatever arguments the operator
   deliberately opened.
2. **A tool is a standing permission.** Saving one says "this agent may read this, in this
   shape, whenever it judges the question calls for it." That is why tables switched off in
   Data Sources are not even offered, and why a disabled tool is refused rather than
   quietly skipped.
3. **One tool = one question.** Ten narrow tools with good descriptions route far better
   than one wide tool with a vague one. When a query starts needing "and also", the answer
   is usually a second tool — or a nested one.

---

# Where it lives

| | |
|---|---|
| Page | `/tool-configs` — the library, one row per tool |
| Help | The **Help** button beside *New Tool Config* opens `/tool-configs/help` in a new tab — this page, browsable, with the same fields and the same worked examples. A new tab rather than an offcanvas because it is read *while* the form is open, so it must not be the thing covering it. `templates/tool_configs/help.htm`; keep the two in step when a cap or a validator's wording changes |
| Filtered | `/tool-configs?agent=<agent-uuid>` — what the tool count on the Data Agents page links to. The filter is carried through every save, so the table rebuilds on the same subset |
| New / Edit | The offcanvas panel — `GET /tool-configs/new-form`, `GET /tool-configs/{uuid}/edit-form` |
| Row actions | Edit, Enable/Disable, Delete. Every one answers with a marker plus an out-of-band rebuild of the table |

Saving anything regenerates the owning agent's routing prompt in a background task, so the
agent's description of its own tools never lags behind the library. Moving a tool to another
agent regenerates **both** — the one it left is still describing it.

---

# The fields, once

| Field | Required | Notes |
|---|---|---|
| **Data Agent** | yes | Who owns the tool. Managed on the Data Agents page |
| **Tool Name** | yes | `a-z`, `0-9`, `_`, starts with a letter, automatically lowercased. Unique per agent, case-insensitively — two agents may each have a `total_units` |
| **Datasource** | yes | Inactive ones are still offered (a tool is often defined before its datasource is switched on) but flagged. Changing it resets the query: a query belongs to the datasource it was written against |
| **Tables** | yes, ≥1 | Multi-select, max 25. **The first selection is the primary table** — in builder mode it is the base table every join hangs off and every bare column name means. Only tables left active in Data Sources are offered at all |
| **Description** | no, but write one | Max 2000 chars. This is what the model routes on. See below |
| **Allow whole-result grouping** | no | Off by default. Scenario 12 |
| **Query** | yes | `Query builder` or `SQL query`. SQL is offered for relational datasources only |
| **Nested Tools** | no | Relational datasources only. Scenarios 9–11 |

## Write the description as the question it answers

The description and the name are the entire basis on which the model decides to call the
tool. Two rules cover most routing problems:

```
Bad   summary            "Inventory data"
Bad   implementation     "SELECT with a GROUP BY on category"
Good  the question       "How many units are in stock, broken down by product category.
                          Use for stock levels and 'how much of X do we have' questions."
```

Then say what it is **not** for, if a sibling tool is close by: *"Current stock only — for
what was ordered, use `order_totals`."* That single sentence removes most of the wrong-tool
calls in a library with overlapping tools.

---

# Scenario 1 — A single number

> *"How many items do we have?"*

Builder mode. Tables: `inventory_items`. One aggregation, nothing else.

| Card | Entry |
|---|---|
| Aggregations | `COUNT` of `id`, alias `total_items` |

Stored `config`:

```json
{"columns": [], "aggregations": [{"type": "count", "column": "id", "alias": "total_items"}],
 "group_by": [], "filters": [], "joins": []}
```

Runs as `SELECT COUNT(id) AS total_items FROM inventory_items`. The agent calls
`total_items()` with no arguments and gets one row back.

**The alias matters more than it looks.** It is the key the model sees in the result, so
`total_items` reads back as an answer where `count` reads as a column name.

---

# Scenario 2 — A filtered list

> *"Which items are out of stock?"*

Builder mode. Tables: `inventory_items`.

| Card | Entry |
|---|---|
| Columns | `sku`, `name`, `category` |
| Filters | `qty` `=` `0` |

```json
{"columns": [{"column": "sku", "alias": ""}, {"column": "name", "alias": ""},
             {"column": "category", "alias": ""}],
 "aggregations": [], "group_by": [],
 "filters": [{"column": "qty", "operator": "=", "value": "0"}], "joins": []}
```

**Leaving Columns empty is legal and means every active column** — the same default the
Configurations builder uses. Prefer naming them: a `SELECT *` tool sends the model columns
it does not need, and any column switched off later in Data Sources makes the tool fail
loudly rather than silently narrow.

A filter value is a **bound parameter**, not text spliced into SQL. Max 500 characters.

---

# Scenario 3 — A grouped report

> *"How many units per category?"*

Builder mode. Tables: `inventory_items`.

| Card | Entry |
|---|---|
| Columns | `category` |
| Aggregations | `SUM` of `qty`, alias `units` |
| Group By | `category` |

```
SELECT category, SUM(qty) AS units FROM inventory_items GROUP BY category
```

**The one rule that catches people here:** once a query aggregates or groups, every plain
column in Columns must also be in Group By. Add `name` to Columns without grouping it and
the save is refused:

> Column 'name' is selected but not grouped. A query that aggregates can only select
> columns that are also in Group By — add 'name' to Group By, aggregate it instead (COUNT,
> SUM, AVG, MIN, MAX), or remove it from Columns.

It is refused rather than fixed, because grouping by `name` too is a *different* query — one
row per name instead of one per category — and only you know which you meant. MySQL
(`ONLY_FULL_GROUP_BY`) and PostgreSQL would both refuse it at run time, i.e. mid-conversation
in front of a visitor; this moves that failure to the form.

Same reason: grouping with Columns *and* Aggregations both empty is refused. "Every column,
grouped" is the same violation written shorter.

---

# Scenario 4 — One tool, many questions (agent-supplied filter)

> *"Projects created in August"* … *"…and in September?"*

Two configs would be wrong. Tick **Agent fills in** on the filter instead.

Builder mode. Tables: `projects`.

| Card | Entry |
|---|---|
| Columns | `name`, `status` |
| Filters | `created_at` `>` — **Agent fills in** ticked, parameter `created_after`, Required ✓, description *"Only projects created after this date. ISO format, e.g. 2026-08-01."* |

```json
{"filters": [{"column": "created_at", "operator": ">", "agent_supplied": true,
              "required": true, "param": "created_after",
              "description": "Only projects created after this date. ISO format, e.g. 2026-08-01."}]}
```

The tool's argument schema now has one field, `created_after`, and the preview reads
`created_at > :created_after`. What the model supplies is **the value only** — the column,
the comparison and every other filter stay exactly as you set them, and the value is bound
by the same code path that binds a stored one. The parameter name defaults to the column
(`projects.created_at` → `created_at`) and must be a plain identifier: it becomes a field
name in the tool's schema.

Untick **Required** for an optional narrowing. A required value the user did not give makes
the assistant ask rather than guess.

**Mix freely.** A tool can have three fixed filters and one agent-supplied — that is the
common shape: the fixed ones are the policy, the open one is the question.

---

# Scenario 5 — "Has a value" / "is empty"

> *"Which clients have no region set?"*

Four operators compare against **nothing**, and picking the right one is the whole scenario:

| Operator | Matches |
|---|---|
| `IS NULL` | absent |
| `IS NOT NULL` | present — **including `''` and `'   '`** |
| `IS BLANK` | absent **or** empty **or** whitespace |
| `IS NOT BLANK` | actually has something in it |

Use `IS BLANK` / `IS NOT BLANK` for text. `IS NOT BLANK` is what "not empty" means when
someone says it out loud, and it is what the older two-filter workaround could never
express: the builder ANDs its conditions, so `region != ''` silently keeps every NULL row.

These filters store no value at all — `{"column": "region", "operator": "IS BLANK"}` — and
preview as the SQL they become:

```sql
(region IS NULL OR TRIM(region) = '')
```

---

# Scenario 6 — Two tables (a join)

> *"Which orders are for discontinued products?"*

Relational datasources only. Tables: `orders`, `inventory_items` — **`orders` first**, so it
is the base table.

| Card | Entry |
|---|---|
| Joins | `INNER JOIN inventory_items` ON `orders.product_sku` = `inventory_items.sku` |
| Columns | `orders.order_ref`, `inventory_items.name` |
| Filters | `inventory_items.status` `=` `discontinued` |

**Once a query has a join, every column reference is qualified** — `orders.order_ref`, not
`order_ref`. With two tables in play a bare name is ambiguous. (Configs saved before a join
was added keep their bare names and still mean the base table, so adding a join to an
existing tool means re-picking its columns.)

The Joins card only offers the tables in the Tables field, and the save refuses a join onto
one that is not:

> The query joins 'clients', which is not one of the tables selected for this tool. Add it
> to the Tables field or remove the join.

That is what keeps the two fields from disagreeing. The recorded table list is what the
routing prompt reports and what the run-time active-table check looks at — a tool reading
three tables while reporting one understates its own scope. Join types are whatever the
dialect actually has; see [QUERY_JOINS.md](QUERY_JOINS.md).

---

# Scenario 7 — The builder cannot express it (SQL mode)

> *"List every distinct technology we use, alphabetically."*

`DISTINCT` and `ORDER BY` are outside the builder. Switch **Query** to `SQL query`:

```sql
SELECT DISTINCT technology
FROM inventory_items
WHERE technology IS NOT NULL AND TRIM(technology) <> ''
ORDER BY technology
```

Tables: still fill it in — `inventory_items`. **Nothing parses the statement**, so the table
list is what you say it reads; it is what the routing prompt reports and what the active-table
check uses.

The rule is: **if it is a valid read-only query, a tool can run it.** `DISTINCT`, `ORDER BY`,
`LIMIT`, `HAVING`, subqueries, CTEs, window functions, `UNION`, `CASE` — all fine. What is
refused is anything that is not a single read:

> The SQL query contains more than one statement. A tool config runs one read-only
> statement — the agent can read data, never change it.

Syntax is *not* checked and cannot honestly be — dialects differ. That is what the
**Test Query** button in the panel header is for: it runs the statement once against the
datasource, reading a single row, so a statement the database refuses is found while the form
is open rather than by a visitor months later ([QUERY_TEST.md](QUERY_TEST.md)).

Max 8000 characters. No trailing semicolon needed. Results are capped at 200 rows like every
other tool.

**Switching modes is free.** Both panels stay in the DOM, so a switch made by mistake costs
nothing — but a *save* in one mode clears the other, so a tool never holds two queries.

---

# Scenario 8 — A SQL tool the assistant parameterises

> *"Show me the salary spread for engineering"* … *"…and for sales?"*

SQL mode's equivalent of scenario 4. Write the placeholder yourself, then declare it in
**Values the assistant supplies**:

```sql
SELECT department_id,
       AVG(salary)  AS avg_salary,
       MAX(salary) - MIN(salary) AS spread
FROM employees
WHERE department_id = :department_id
GROUP BY department_id
```

| Field | Value |
|---|---|
| Name | `department_id` |
| Type | **Number** |
| Required | ✓ |
| Description | *"The department to report on."* |

Stored as `[{"param": "department_id", "type": "number", "required": true, "description": "…"}]`.

Three things to know:

* **Type is not decoration.** There is no reflected column to coerce against here, so you
  say what the value holds. asyncpg refuses `id = $1` with a string against an integer
  column — that failure would otherwise happen mid-conversation. Text is the default and is
  correct wherever the database will coerce.
* **A declared name must appear in the statement.** Otherwise the model is asked to fill a
  field that does nothing:
  > The SQL query does not use ':region' anywhere, so a value for 'region' would go nowhere.
  > Add it to the statement, for example: WHERE department_id = :region
* **The value is bound, never written into the SQL.** It can only ever be the right-hand
  side of a comparison you wrote. Max 5 values per tool — every one is another field the
  model has to fill correctly on every call.

The **test value** beside each row is used by Test Query only and is never saved.

---

# Scenario 8b — A serialised column, and how to let the assistant choose the ids

> *"Which projects are in the PHP department and use WordPress?"* — where
> `projects.departments` holds a PHP-serialised blob rather than proper columns.

SQL mode. The query as written by hand:

```sql
SELECT p.name, p.crm_id, pd.client_name, pd.client_email
FROM projects p
LEFT JOIN project_details pd ON p.crm_id = pd.crm_id
WHERE
    -- PHP Department
    p.departments LIKE '%s:6:"depart";%'
    AND p.departments LIKE '%s:1:"1"%'
    -- WordPress Technology
    AND p.departments LIKE '%s:4:"tech";%'
    AND p.departments LIKE '%s:1:"7"%'
ORDER BY p.name
```

| Field | Value |
|---|---|
| Tool Name | `php_wordpress_projects` |
| Tables | `projects`, `project_details` — **`projects` first** |
| Query | SQL query |
| Description | *"Projects in the PHP department that use WordPress, with the client's name and email."* |

**Why SQL mode:** `ORDER BY` is outside the builder, and so is a `LEFT JOIN` whose `ON` is a
business key rather than a foreign key.

**The `;` inside the patterns is not a problem, and neither are the `--` comments.**
`sql_guard` blanks string literals and comments *before* looking for a second statement or a
write verb, so `s:6:"depart";` reads as what it is — one character inside one string — and
the notes stay in the saved statement where the next person reads them.

## Making it answer more than one question

As saved, that tool answers exactly one question: department `1`, technology `7`. To let the
visitor name the department, the ids become **Values the assistant supplies** — and this is
the case where the obvious attempt does not work, because the ids sit *inside* a string.

```sql
-- Does NOT work: a :name in quotes is text, not a placeholder
AND p.departments LIKE '%s:1:":department_id"%'
```

`_placeholders_in_sql` blanks quoted spans first, exactly as the read-only guard does, so it
finds no placeholder here and declaring one is refused:

> The SQL query does not use ':department_id' anywhere, so a value for 'department_id' would
> go nowhere.

Which is the right answer — had it been accepted, the database would have gone looking for
projects containing the literal text `:department_id`.

**Build the pattern around the bound value instead.** MySQL:

```sql
AND p.departments LIKE CONCAT('%s:', CHAR_LENGTH(:department_id), ':"', :department_id, '"%')
```

PostgreSQL, same thing with `||`:

```sql
AND p.departments LIKE '%s:' || length(:department_id) || ':"' || :department_id || '"%'
```

**Why the length function is there.** PHP writes the string's length into the serialised
form: `s:1:"7"` for `7`, but `s:2:"12"` for `12`. Hard-coding `s:1:` gives you a tool that
works perfectly until the tenth department is added and then silently matches nothing.

The whole statement, parameterised:

```sql
SELECT p.name, p.crm_id, pd.client_name, pd.client_email
FROM projects p
LEFT JOIN project_details pd ON p.crm_id = pd.crm_id
WHERE
    -- Department: the key is fixed, the id is supplied
    p.departments LIKE '%s:6:"depart";%'
    AND p.departments LIKE CONCAT('%s:', CHAR_LENGTH(:department_id), ':"', :department_id, '"%')
    -- Technology: same shape
    AND p.departments LIKE '%s:4:"tech";%'
    AND p.departments LIKE CONCAT('%s:', CHAR_LENGTH(:technology_id), ':"', :technology_id, '"%')
ORDER BY p.name
```

| Name | Type | Required | Description |
|---|---|---|---|
| `department_id` | Text | ✓ | *"Department id, digits only. 1 = PHP, 2 = .NET, 3 = Mobile."* |
| `technology_id` | Text | ✓ | *"Technology id, digits only. 7 = WordPress, 8 = Laravel."* |

Rename the tool to match what it now does — `projects_by_department_and_technology` — because
the name is half of how the model decides to call it.

* **Declare them as Text, not Number.** The value goes into a string pattern rather than
  against a numeric column, so a numeric type buys nothing and costs a cast in some drivers.
  Number is for a placeholder against an integer column, as in scenario 8.
* **The id mapping goes in the description**, because that sentence is all the model has to
  turn "PHP" into `1`. Short, and only for a small stable list.
* **Ask for digits only, and mean it.** The value is bound, so it can never become SQL — but
  `%` and `_` are wildcards to `LIKE`, so a value containing one would widen the match. The
  description is what stops the model getting clever.

## Where should the id actually come from?

| If… | Do this | Why |
|---|---|---|
| Only a few combinations are ever asked about | Separate tools with the ids as literals — the first statement above, once per pairing | Best routing there is: the tool name *is* the question, and the model has no value to get wrong |
| The visitor names one, out of a short fixed list | Assistant-supplied values, mapping in the description | One tool, many questions, no lookup |
| The mapping lives in a table and you want *every* department | A nested child returning the ids, bound with **run once per value** and `record the value as department_id` (scenario 11) | The parent runs once per id and the rows stay attributable. This is precisely why the iterating binding exists — an expanding `IN` cannot go inside a `CONCAT` |

**What you cannot do: have the assistant name the id on a nested child.** An embedded tool is
never called by the assistant — the tool that embeds it is — so a child runs exactly as
saved, with no arguments. Embedding one that requires a value is refused when the link is
saved:

> 'department_by_name' needs 'name' to be supplied by the assistant, and an embedded tool is
> never called by the assistant — the tool that embeds it is. Make those values optional, or
> give the inner query fixed ones.

So the division is: **nesting gives breadth** (every department, from a lookup),
**assistant-supplied values give selection** (the one department the visitor named). Reach
for the second when the question contains the choice.

**One honest caveat about this query, parameterised or not.** `LIKE '%s:1:"1"%'` searches the
whole blob, so it also matches a *technology* whose id is `1` — neither test is anchored to
its own key. Parameterising changes nothing either way. For strictness, put the value
immediately after its key in one pattern:

```sql
LIKE CONCAT('%s:6:"depart";%s:', CHAR_LENGTH(:department_id), ':"', :department_id, '"%')
```

which relies on the value following the key in the serialised form. Try both with
**Test Query** against real rows before saving — this is exactly the kind of pattern no
validator here can check for you.

---

# Scenario 9 — A sub-query as two tools (nested, `match any`)

> *"Show me the projects of our active clients."*

You could write one statement with an `IN (SELECT …)`. Nesting gets you the same result plus
a reusable `active_clients` tool the agent can also call on its own.

**First** save the child:

| | |
|---|---|
| Name | `active_clients` |
| Tables | `clients` |
| Columns | `id`, `name` |
| Filters | `is_active` `=` `true` |

**Then** the parent, with a **Nested Tools** row:

```
Run [active_clients] take [id] and filter [projects.client_id] [match any]
```

Read the row as a sentence. At run time the child goes first, its `id` column becomes a list
of values, and the parent's query gains `projects.client_id IN (…)` — one expanding bound
parameter, one round trip, one result set. The parent still runs **once**.

Things worth knowing before you build a chain:

* **An empty child empties the parent.** If `active_clients` returns nothing, the parent
  returns no rows and anything above it never runs. That is deliberate — the alternative is
  a parent silently dropping its restriction and returning *more* than it should.
* **Children stay tools.** They keep their own name, description and callability, and the
  parent's agent gains every transitive child.
* **Order matters.** Children run in the order listed and the first empty one stops the
  chain — put the cheapest or most selective first.
* **Limits:** same datasource, same owner, enabled, no cycles; ≤5 children per tool, ≤5
  levels deep, ≤20 tools per chain, ≤2000 values crossing a level (refused, never
  truncated).
* The same child may feed a parent **twice** on different targets — a client-id tool can
  restrict both `owner_id` and `billed_to_id`. What is refused is the same child on the same
  target twice.

---

# Scenario 10 — A nested child feeding a SQL parent

Same idea, parent in SQL mode. The target is a **bind name**, not a column, and the row's
middle label changes to *fill*:

```sql
SELECT c.name, COUNT(p.id) AS project_count
FROM projects p
JOIN clients c ON c.id = p.client_id
WHERE p.client_id IN :active_clients
GROUP BY c.name
ORDER BY project_count DESC
```

```
Run [active_clients] take [id] fill [active_clients] [match any]
```

The statement is **never rewritten** — the list is bound at execution as an expanding
parameter. Which is exactly why `match any` needs the placeholder on the right of an `IN`:
an expanding parameter always renders parenthesised, so `p.client_id = :active_clients`
would be a syntax error. When your placeholder is not on the right of an `IN`, you want the
next scenario.

---

# Scenario 11 — One run per value (`run once per value`)

> *"For each active department, the highest-paid employee."*

Some placeholders cannot take a list: `dd.id = :x`, or a value the database builds a string
around — `LIKE CONCAT('%s:1:"', :x, '"%')`. Set the row's binding to **run once per value**:

```
Run [active_departments] take [id] fill [dept] [run once per value]
      record the value as [department_id]
```

The parent then runs **once per value**, each with a plain scalar bound, and the rows are
concatenated. Two consequences:

* **`record the value as` is how you keep the result readable.** Rows from twenty runs of one
  statement are otherwise indistinguishable once concatenated — a statement filtering on a
  department without selecting it is perfectly ordinary SQL. Leave it blank only when the
  query already returns the value; asking for a name the query already returns is refused as
  a column collision rather than overwriting the database's answer.
* **At most one iterating child per parent.** Two would be a cartesian product of two result
  sets, and the row cap would make that a *truncated* answer rather than a bigger one.

`in_list` is one round trip; `each` is N statements, each with its own planning and its own
latency, inside a turn a visitor is waiting on. Prefer `match any` and reach for this when
the SQL leaves you no choice. Details in
[TOOL_CHAIN_ITERATION.md](TOOL_CHAIN_ITERATION.md).

---

# Scenario 12 — Grouping the database cannot do (whole-result grouping)

> *"Average deal size by region"* — against a CSV or a Mongo collection, where there is no
> `GROUP BY` to write.

Tick **Allow whole-result grouping**. That lets the agent read *every* record the tool
returns and fold them in memory — totals, averages, counts by category — instead of the 200
rows every other tool call is capped at.

Off by default, and worth leaving off for a relational datasource: `GROUP BY` in the database
is faster and equally exact, so scenario 3 is the better tool. Ticking it is a judgement
about **this query's result set** — "reading every record this returns is acceptable" — not
about the agent. Only aggregations with an exact fold are offered; the rest are refused
rather than approximated. See
[AGENT_RECURSIVE_DATAFRAMES.md](AGENT_RECURSIVE_DATAFRAMES.md).

---

# Scenario 13 — A CSV, Excel, Parquet or Mongo datasource

Pick one and the form narrows itself:

| | Relational (PostgreSQL / MySQL / SQLite) | File or Mongo |
|---|---|---|
| Query builder | ✓ | ✓ |
| SQL query mode | ✓ | — radio disabled, *"This datasource is not relational, so it has no SQL to run"* |
| Joins card | ✓ | — |
| Nested Tools | ✓ | — |
| Agent-supplied filters | ✓ | ✓ |
| Whole-result grouping | ✓ (rarely needed) | ✓ (the main reason it exists) |

So on a file datasource you have columns, aggregations, grouping, filters, agent-supplied
filter values, and whole-result grouping. That is the whole surface — and it is enough for
most "how many / how much / which ones" questions over a spreadsheet.

The refusal happens at save time rather than on the agent's first call, because a tool that
can never run is a configuration mistake and you are standing in front of the form:

> 'Sales export' is not a relational datasource, so it cannot run a SQL query. Use the query
> builder instead, or pick a PostgreSQL, MySQL or SQLite datasource.

---

# Scenario 14 — Let Ask AI write it

On the SQL Assist page, ask in plain English, read the SQL it produced, then **Auto Create
Tool**. It lands in the query builder when the query fits it, and as the statement itself
when it does not — held to exactly the same validators as anything typed here, because it
goes through the same service. Then open it and check the name, the description and the
table list, which are the parts a generator guesses at.

Ask AI reads reflected schema, never your data. See [SQL_ASSIST.md](SQL_ASSIST.md).

---

# Scenario 15 — Living with tools that already exist

| You want to | Do this | What happens |
|---|---|---|
| Revoke a capability, keep the definition | **Disable** | The tool stops being offered to the agent; the query is untouched. Refused if another tool embeds it — the parent would keep running with its filter gone, returning *more* rows, silently |
| Hand a tool to a different agent | Change **Data Agent** on the edit form | Both agents' routing prompts regenerate — the old one is still describing a tool it no longer has |
| Point a tool at a different datasource | Change **Datasource** | The query resets. A query belongs to the datasource it was written against |
| Remove it | **Delete** | Agent and datasource untouched. Refused while something embeds it, for the reason above |
| See what a tool actually runs | The **Query** column | The rendered query for a builder tool, the statement itself for a SQL tool, with a badge saying which. Nested tools show their chain indented, and a child shows which tools embed it |
| Check a query before saving | **Test Query** | Runs it once against the datasource through the executor itself |
| Work on one agent's library | `/tool-configs?agent=<uuid>` | The filter survives every save |

**A datasource being unreachable never makes a tool uneditable.** The form shows a warning
above the saved values and lets you edit them. Likewise, switching a column off in Data
Sources does not block editing — it makes the tool fail loudly when *run*, which is where
that check belongs.

---

# Limits, in one place

| | |
|---|---|
| Tables per tool | 25 |
| Columns / aggregations / group-by / filters | 200 / 50 / 50 / 50 |
| Filter value | 500 chars |
| Description | 2000 chars |
| SQL statement | 8000 chars |
| Assistant-supplied values (SQL mode) | 5 |
| Nested children per tool | 5 |
| Chain depth / total tools in a chain | 5 / 20 |
| Values crossing one chain level | 2000 (refused, not truncated) |
| Rows returned to the model | 200 |
| Rows the model may print | 100, plus an exact `COUNT(*)` and an offer to download the rest ([DOWNLOADER_AGENTS.md](DOWNLOADER_AGENTS.md)) |

---

# When a save is refused

Every message is written to be actionable. The ones worth recognising:

| Message | What to do |
|---|---|
| *Agent 'X' already has a tool named 'y'* | Names are unique per agent, case-insensitively. Rename, or put it on another agent |
| *Select at least one table for this tool to read* | Tables is required in both modes |
| *Column 'x' is selected but not grouped* | Scenario 3 — group it, aggregate it, or drop it |
| *This query groups rows but selects every column* | Name the grouped columns and the aggregations you want |
| *The query joins 'x', which is not one of the tables selected* | Add it to Tables, or remove the join |
| *Every filter needs a value to compare against, or must be marked as supplied by the agent* | Fill the value, or tick **Agent fills in** |
| *The SQL query does not use ':x' anywhere* | Write the placeholder into the statement, or drop the declared value — and check it is not inside quotes (scenario 8b) |
| *The SQL query uses ':x', which nothing fills* | The other direction: declare it as an assistant-supplied value, embed a nested tool that fills it, or take it out |
| *'y' needs 'x' to be supplied by the assistant, and an embedded tool is never called by the assistant* | Make the child's value optional, or give the inner query a fixed one (scenario 8b) |
| *The SQL query contains more than one statement* / *is not a read* | One read-only statement. See [TOOL_QUERY_MODES.md](TOOL_QUERY_MODES.md) |
| *'X' is not a relational datasource, so it cannot run a SQL query* | Scenario 13 |
| *Every table in this datasource is switched off* | Switch tables back on in Data Sources |
| *…cannot be disabled / cannot be deleted* | Another tool embeds it; the message names which |

Syntax errors and unknown columns in a SQL-mode statement are **not** in this list, by
design: no parser here is strict enough to be trusted with them. **Test Query** is how you
find those, and it reports what the database said.

---

# A worked library

What a small, well-routed set looks like for one agent — narrow tools, explicit
descriptions, nesting instead of one wide query:

| Tool | Mode | Reads | Answers |
|---|---|---|---|
| `stock_by_category` | builder | `inventory_items` | *"How much of each category is in stock."* Grouped totals |
| `out_of_stock_items` | builder | `inventory_items` | *"Which specific items are at zero."* SKU-level list |
| `items_missing_technology` | builder | `inventory_items` | *"Items with no technology recorded."* `IS BLANK` |
| `distinct_technologies` | sql | `inventory_items` | *"Every technology we use, alphabetically."* Needs `DISTINCT` |
| `active_clients` | builder | `clients` | *"Our currently active clients."* Also a child of the two below |
| `active_client_projects` | builder | `projects`, `clients` | *"Projects belonging to active clients."* Embeds `active_clients` |
| `projects_since` | builder | `projects` | *"Projects created after a date the user names."* Agent-supplied `created_after` |
| `department_salary_spread` | sql | `employees` | *"Salary spread for a department the user names."* Agent-supplied `:department_id` |

Eight tools, eight sentences, no overlap the model has to guess at.

---

# Where this fits

* [DEEP_AGENTS.md](DEEP_AGENTS.md) — how a saved tool becomes something a chatbot calls, and
  how results reach the answer
* [FLOW_BUILDER.md](FLOW_BUILDER.md) — an AI Fallback node's knowledge base can also attach a
  tool config as a live source: its own stored query runs fresh on every visitor message and
  is injected as plain text, never embedded into the vector store
* [TOOL_QUERY_MODES.md](TOOL_QUERY_MODES.md) — the two modes and the shared read-only guard
* [QUERY_JOINS.md](QUERY_JOINS.md) — join rules, shared with the Configurations page
* [TOOL_CHAINING.md](TOOL_CHAINING.md) / [TOOL_CHAIN_ITERATION.md](TOOL_CHAIN_ITERATION.md) —
  nesting, and the two shapes a value can take
* [TOOL_GRAPHS.md](TOOL_GRAPHS.md) — the read-only canvas that draws a chain as the graph it
  compiles to
* [QUERY_TEST.md](QUERY_TEST.md) — the Test Query button
* [SQL_ASSIST.md](SQL_ASSIST.md) — Ask AI and Auto Create Tool
* [AGENT_RECURSIVE_DATAFRAMES.md](AGENT_RECURSIVE_DATAFRAMES.md) — whole-result grouping
* [SCHEMAS.md](SCHEMAS.md) — the request/response schemas behind this form
