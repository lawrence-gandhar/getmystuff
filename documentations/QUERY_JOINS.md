# QUERY_JOINS.md

Joining several tables into one authored query — offered wherever a query is built, for
relational datasources only.

---

# What it is

A query config (a Tool Config, or a Tool Base Config on the Configurations page) reads one
table by default. On a relational datasource it may also **join** further tables in, so a
single tool can answer a question that spans them.

A join is only offered where the datasource can execute one. A CSV file or a Mongo
collection has no second table to join to, so the Joins section is not rendered for them and
a payload carrying joins for one is rejected rather than stored — see
`validated_joins`.

Two authoring surfaces produce the identical shape:

| Surface | Panel | Builder |
|---|---|---|
| **Tool Configs** (`/tool-configs`) | New/Edit Tool Config offcanvas | `static/js/tool_configs.js` |
| **Configurations** (`/datasource/configurations`) | New Tool Base Config offcanvas | inline script in `templates/datasources/configuration.htm` |

Both are backed by the same rules module, so a join means the same thing wherever it was
authored.

A third producer writes the same shape without a builder: **Auto Create Tool** converts an
AI-generated query into a Tool Config, joins included, and is held to this module's rules by the
same validator the builders are — see [SQL_ASSIST.md](SQL_ASSIST.md).

---

# Shared rules — `app/utils/query_joins.py`

Deliberately a util rather than either feature's module: `app/models/tool_configs`'s docstring
states that Tool Configs and the Configurations section reference nothing of each other's, and
that stays true — neither imports the other, they both import this.

| Name | Purpose |
|---|---|
| `RDBMS_DB_TYPES` | `{"postgres", "mysql", "sqlite"}` — the datasource types that can join at all. |
| `supports_joins(db_type)` | The single answer to "may this datasource join?", used by both forms and by validation. |
| `JOIN_TYPES` / `JOIN_TYPE_SQL` | `(value, SQL keyword)` pairs. The keyword doubles as the dropdown label, so the form reads as the SQL it produces. |
| `join_types_for(db_type)` | The join types **this dialect** actually has. Empty for a non-relational datasource, which is also true before a datasource is chosen — so one check (`{% if join_types %}`) covers both and the template needs no second flag. |
| `validated_joins(raw, base_table, db_type)` | Rebuilds the join list field by field. Only known keys are persisted and every name has passed `require_object_name`. |
| `query_tables(joins, base_table)` | Every table the query reads, or `[]` when there are no joins — the signal that column references are bare. |
| `validated_column_reference(value, label, allowed_tables)` | One column reference, optionally `table.column`, checked against the tables the query actually reads. |
| `build_join_sql(joins)` | The JOIN clauses for the query preview. Display only; never executed — the Deep Agents executor builds the real joins from reflected tables instead. |

`MAX_JOINS = 10`, and the payload arrives from a form field, so "however many the client sent"
is never an acceptable number of tables to join.

### Per-dialect join types

MySQL has no `FULL OUTER JOIN`, so it is not offered and not accepted — an option that only
produces a query failing at run time is worse than no option. The dropdown's contents come
from `join_types_for` on the server in both surfaces, so the form can never offer a join the
save would then reject.

---

# Stored shape

Appended to the existing query config; `config` / `base_config` are JSONB, so **no migration
was needed**.

```json
"joins": [
  {
    "type": "inner",
    "table": "orders",
    "left_table": "customers",
    "left_column": "id",
    "right_column": "customer_id"
  }
]
```

Order matters and is preserved. Each entry may only match against a table already in the query
— the base table, or a table joined *before* it — so the list always reads as a connected chain
that turns into SQL in exactly the order it is stored. `validated_joins` grows its set of known
tables inside the loop, which is what enforces that.

---

# Column qualification

Once a query has at least one join, every column reference becomes `table.column`. With two
tables in play a bare `id` is ambiguous, and the generated SQL has to say which one it means.

The transition is handled in both builders rather than left to the user:

* Adding the **first** join qualifies every reference already selected with the base table.
* Removing the **last** join strips that prefix back off.

So nothing the user picked is lost in either direction. A config saved *before* a join was
added keeps its bare names, and `validated_column_reference` still accepts them as meaning the
base table — rejecting them would make an existing config uneditable.

A qualified reference naming a table that is not in the query is rejected: that is a reference
to a table the user never joined.

### Removing a join is never silent

Removing a join also removes any join that matched against the table it brought in (and, in
turn, the tables *those* joins brought in), plus every column reference to any of them. The
user is losing selections they made, so the panel says so:

> Removed the join on 'orders', along with 2 selection(s) that referred to a table it brought in.

`data-builder-notice` in `query_builder.htm`, `#joinsNotice` in `configuration.htm`.

---

# Tool Configs surface

* **`templates/tool_configs/partials/query_builder.htm`** — the Joins card, rendered only when
  `join_types` is non-empty, above Columns.
* **`static/js/tool_configs.js`** — join rows, qualification, the removal cascade, and the SQL
  preview. Row markup is built with `createElement`, never `innerHTML`: table and column names
  come from the user's own database and must never be re-parsed as markup.
* **`GET /tool-configs/columns`** — a joined table's columns as JSON. Which table that is only
  becomes known when the user picks it, so the builder fetches on demand and re-renders the
  dropdowns. Connection failures come back in an `error` field and are shown next to the join
  row, rather than replacing the offcanvas mid-edit.
* **`_builder_context` / `_builder_defaults`** (`tool_config_routes.py`) — one source for the
  builder's context, shared by the first render and the `/tool-configs/fields` cascade, so a
  mid-edit swap produces exactly what the first render did. The edit form preloads every
  *joined* table's columns via `get_column_map`, so a saved joined query returns intact.
* **`GET /tool-configs/tables`** now renders `table_field_response.htm`: the Table field **plus
  an out-of-band reset of the builder**. A query — its joins especially — belongs to one
  datasource, so leaving the previous datasource's builder on screen under a newly chosen one
  would offer columns that are no longer there.

The default match for a new join prefers a foreign key (`customers.id` → `orders.customer_id`,
then the singular form, then the same column name) before falling back to the table's first
column. The one-click case is overwhelmingly a foreign key; offering column one instead reads
as a wrong answer.

---

# Configurations surface

The Tool Base Config panel gets the same Joins card (`#joinsSection`), hidden unless
`joinTypeChoices()` returns anything for the open datasource.

It needs no column-fetch endpoint: `globalConfigurationData` already holds every table's
`column_data` for the whole datasource, loaded for the table list behind the panel. Only
tables whose status is `active` are offered as join candidates, matching the fact that only
active tables are viewable there.

The join types are handed to the page as `join_types_by_db_type` (built from `join_types_for`
in `datasource_config_routes._JOIN_TYPES_BY_DB_TYPE`) because one panel serves every
datasource the user opens, without a round trip.

### Backend

`datasource_config_service.validated_base_config` is called **before** the `try` block in both
create paths — that catch-all turns anything it sees into a 500, which would bury a perfectly
readable validation message.

* No joins → the stored shape is left exactly as it was before joins existed (the key is
  removed, not written as `[]`).
* Joins present → they are validated, and every column reference in `columns`,
  `aggregations`, `group_by` and `filters` is checked against the tables the query reads.
  Aliases, functions and operators are passed through untouched; they are the builder's
  business.

One adjacent fix went in with it: a malformed `base_config` was previously swallowed and saved
as `{}`, discarding the query the user had just built. It now returns a readable error via
`parse_json_object`.

---

# What is rejected, and what the user is told

Every message below is written to be read by the person who hit it.

| Situation | Response |
|---|---|
| Joins on a file or Mongo datasource | "Joins are only available for relational datasources (PostgreSQL, MySQL or SQLite)." |
| A join type the dialect lacks | "Every join needs a valid type (INNER JOIN, LEFT JOIN, RIGHT JOIN)" |
| Matching against a table not yet in the query | "The join on 'items' matches against table 'orders', which is not part of this query…" |
| The same table joined twice | "Table 'orders' is already part of this query — join each table only once" |
| A column on a table never joined | "Column 'invoices.total' refers to table 'invoices', which is not part of this query" |
| A name that isn't a safe identifier | "Join table 'orders; DROP TABLE users' is not a valid name" |
| More than `MAX_JOINS` | "A query cannot join more than 10 tables" |

Every message above comes from `validated_joins` / `validated_column_reference`, so a config
arriving from Auto Create Tool is held to the identical rules — the public entry point both go
through is `tool_config_service.validated_query_config`.

Table and column names are interpolated into a generated query rather than bound as
parameters, so `require_object_name` (`app/utils/validators.py`) checks every one on the way
in even though they were chosen from live dropdowns. Its pattern spells out `[A-Za-z0-9_]`
rather than using `\w` on purpose: `\w` matches Unicode letters, which would let through
homoglyphs and combining characters that have no business in an identifier.

---

# The SQL preview

Read-only, and never executed. It is built twice from the same rules — server-side by
`tool_config_service.build_query_preview` (for the Tool Configs list) and client-side by each
builder (for the live panel) — so the list and the form describe a config the same way.

The preview is **not** what the Deep Agents runtime runs, and must not become it: it inlines
filter values with f-strings, which is harmless for display and would be an injection vector if
executed. `app/services/deep_agents/query_executor.py` mirrors it clause for clause but builds
the query from reflected `Column` objects with bound parameters — see
[DEEP_AGENTS.md](DEEP_AGENTS.md).

```sql
SELECT customers.name AS who, SUM(items.qty) AS units
  FROM customers
  INNER JOIN orders ON customers.id = orders.customer_id
  LEFT JOIN items ON orders.id = items.order_id
 WHERE orders.status = 'paid'
 GROUP BY customers.name
```

---

# Not covered

* **Subqueries** on the Configurations page cannot join. The requirement was the main query,
  and the subquery panel is a separate builder with its own state.
* **The Configurations page has no edit form** for a Tool Base Config — only
  `config/create` and `config/validate-tool-name` exist. Joins are in its add form because
  there is no edit form there to put them in.
* **Joins are no longer definition-time only.** `ToolConfig.config` — joins included — is now
  executed by the Deep Agents runtime when a chatbot is attached to a data agent, via
  `app/services/deep_agents/query_executor.py`. Two consequences for this module: `left_table`
  ordering is load-bearing at runtime rather than only in the form, and **`RIGHT JOIN` cannot be
  executed** — SQLAlchemy expresses joins as `isouter`/`full` flags with no right variant, and
  substituting one would change which rows come back. Such a tool is refused with a message
  rather than approximated, and flagged on the agent's console up front. Right joins remain
  authorable (both builders still offer them, and the preview renders them); they are simply not
  runnable by a data agent. See [DEEP_AGENTS.md](DEEP_AGENTS.md).
