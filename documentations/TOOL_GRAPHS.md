# TOOL_GRAPHS.md
Tool Graphs — a tool chain drawn as the graph it compiles to, and its joins drawn as sets

---

# What it is

A read-only page at `/tool-graphs`, reached from the sidebar. A tree of
**Workspaces → Data Agents → Tools** on the left, a canvas on the right, and a toggle
above the canvas that switches between two drawings of whatever is selected:

* **Tool Graph** — the chain a nested tool config compiles to.
  `START → paid_invoices → active_clients → projects_by_client → END`.
* **SQL Graph** — the same tools' joins, one two-circle Venn diagram per join.

```
GET /tool-graphs               → templates/tool_graphs/index.htm + partials/tree.htm
GET /tool-graphs/graph?tool=   → {scope_label, nodes[], edges[], error}
GET /tool-graphs/joins?tool=   → {scope_label, tools[], error}
                                  (app/routes/tool_graphs/tool_graph_routes.py)
     → tool_graph_service.get_graph_tree / get_chain_graph / get_join_views
     → static/js/tool_graphs.js draws it
```

The module owns **no model and no `db/` subfolder**. It composes four queries that
already existed — `fetch_workspaces_with_agent_counts`, `fetch_agents_with_details`,
`fetch_tool_configs_with_details` and `fetch_links_for_tools` — and writes nothing.

---

# Why it exists

Two things in this application are shapes, and both were only ever shown as form
rows.

A nested tool config **is** a graph. `tool_chain_graph.py` compiles it into a real
LangGraph: one node per tool, edges running deepest-first, and a conditional edge
after every inner node carrying the rule that a level matching nothing stops the run
(see [TOOL_CHAINING.md](TOOL_CHAINING.md)). That graph decides what an agent gets
back. The only view of it was the indented text under a tool's name in the list, and
text lines cannot show the two facts that matter most when a chain misbehaves:

* **a child embedded in two parents.** The list necessarily repeats it under each
  one, so nothing there says that editing it changes both tools.
* **where a disabled tool sits.** A disabled child is the most common reason a chain
  returns nothing, and in a list it is a word at the end of a line.

A join is set arithmetic. `inner`, `left`, `right` and `full` are precise about which
rows survive, and a dropdown row saying `LEFT JOIN clients` is a much weaker
statement of that than two circles with one of them filled in.

---

# The tree

Server-rendered, because it is a list of things the user owns and nothing about it
needs a renderer — only the canvas beside it does.

Three levels, and **every level is selectable**: a tool draws its own chain, an agent
draws every tool it owns, a workspace draws every agent's. `workspace_id` on a data
agent is nullable by design, so agents in no workspace are grouped under
**Unassigned** rather than dropped.

Empty branches are kept. A workspace with no agents, and an agent with no tools, both
still appear — an empty branch is how someone notices the thing they just created is
empty, and hiding it would make this tree disagree with the Data Agents page about
what exists.

Each tool carries the same two badges the Tool Configs list uses — `nested` and
`embedded` — so the word means the same thing in both places.

---

# The tool graph

**Edges run child → parent**, which is the direction values actually travel and the
direction the LangGraph compiles: the child runs first and the column it returns
restricts the parent. An edge is labelled with what crosses it,
`child_column → parent_reference`, which is the whole contract of a nested tool in
one line.

`START` and `END` are drawn as rectangles of their own because that is what they are
in the compiled graph, not decoration. `START` attaches to every tool that embeds
nothing — the ones that go first — and every tool nothing embeds feeds `END`. A tool
that stands alone therefore draws as `START → tool → END` rather than as a lone box,
which is exactly the graph it compiles to.

**A tool appears once.** A child embedded by two parents is one node with two
outgoing edges. That is the thing this view adds over the list.

**A disabled tool is drawn dashed and red, not hidden.** A chain that stops is what
someone opens this page to find.

## Scope always includes descendants

Selecting an agent draws the agent's own tools **and every tool below them**, even
when a child belongs to a different agent. That is not a convenience: an agent given
a nested tool is given every tool below it at runtime
(`prompt_sync_service.collect_agent_tools`), so a graph that stopped at the agent's
own rows would draw a chain with its lower half missing. Each node carries its own
`agent_name` so a borrowed child is identifiable.

## Layout is computed on the server

`tool_graph_service` returns a `layer` and a `row` per node; the browser multiplies
them by a gap. That split is deliberate. Layout is the part of a drawing that can be
wrong without *looking* wrong, this repository has no JavaScript test harness, and
the coverage ratchet only measures `app/` — computing it in Python is what makes it
assertable, and `tests/unit/services/tool_graphs/` asserts it.

* `layer = 1 + max(layer of children)`, so every edge points forward. `START` is
  layer 0 and `END` is one past the deepest tool.
* A chain runs along one row; a second branch drops to the next. A node keeps the row
  it was first given, so a shared child does not jump between reloads.
* Both passes are **cycle-safe** — the layer relaxation is bounded by the node count
  and the row walk carries a `visited` set. Links cannot be saved in a cycle, but a
  page that only displays must not be the thing that hangs if a row ever arrived
  another way.

## D3, and what it is used for

D3 7 is loaded from a CDN on this page only, matching every other third-party
dependency here (Bootstrap, htmx, Line Awesome are all loaded the same way from
`templates/base/layout.htm`).

It is used for three things: the zoom/pan behaviour, the curve generator for the
connectors, and nothing else. Elements are created with
`createElementNS`/`createElement` and filled with `textContent` — **`.html()` and
`innerHTML` are never called**. Every label on this page is a tool, table or column
name out of the user's own database, and that is the same rule, for the same reason,
that `tool_configs.js` and `tool_chain.js` state at the top of their files.

If the library fails to load the pane says so, rather than sitting empty.

---

# The SQL graph

One card per tool, and inside it one diagram per join, in the order the query applies
them. Two circles, with the region the join keeps filled in:

| Type | Shaded |
|---|---|
| `INNER JOIN` | the lens only — rows on both sides |
| `LEFT JOIN` | the left circle, lens included |
| `RIGHT JOIN` | the right circle, lens included |
| `FULL OUTER JOIN` | both circles |

The shading *is* the definition, not decoration. Under each diagram: the SQL keyword,
the two tables, and the `ON` condition as it is stored.

**One diagram per join, never a combined three-set Venn.** A query joining
`orders → clients → regions` is two pairwise conditions applied in sequence; a
three-circle Venn would imply an `orders ∩ regions` region the query never computes.
The pairwise form is also the only one that still reads at `MAX_JOINS` of 10.

## Why a SQL-mode tool shows no diagram

It shows its declared tables and this sentence:

> This tool's query is a SQL statement. Its joins are not read from the statement —
> only the tables it declares are known here.

Nothing in this application parses joins out of a raw statement. `utils/sql_guard.py`
is explicit that its checks are text heuristics rather than a parse, `_table_aliases`
exists only to find a primary key for the grouping check, and
`tool_chain_service.child_output_columns` already returns `[]` for a SQL tool for the
same reason. A Venn drawn from a regex over a statement with a CTE or a subquery in
it would be a confident picture of something nobody verified — and unlike a wrong
number, a wrong picture is not argued with.

A builder query over one table gets its own note — *"This query reads one table, so
there is nothing to intersect"* — because a blank card would read as "no joins" for
both cases, and those are not the same case.

---

# What it does not do

* **It writes nothing.** No save path, no drag-to-move, no stored position. The
  picture is derived from the tool configs every time it is drawn, so it cannot fall
  out of step with them — which is the failure mode of the Flow Builder canvas's
  persisted `{x, y}`, and is acceptable there because those positions are the user's
  own authored layout. To *draw* a graph rather than read one derived, use
  [GRAPH_DESIGNER.md](GRAPH_DESIGNER.md) — reached from the **Design a Graph** button in
  this page's header. It stores positions for exactly the reason this page does not: its
  layout is authored, so nothing can recompute it.
* **It runs no query.** It draws what a tool *would* do. To find out whether the
  database will accept it, use [QUERY_TEST.md](QUERY_TEST.md).
* **It shows no data.** Tool, table and column names only — never a row.
* **It does not gate on activity.** A disabled tool, a disabled agent and an inactive
  workspace are all drawn and flagged. Hiding them would make the page unable to
  answer the question it is most often opened for.

---

# Failures are answers

Neither view endpoint raises. A selection that cannot be resolved comes back as a
**200 with `error` set** and an empty drawing, so a stale bookmark or a tool deleted
in another tab puts one sentence beside the canvas instead of replacing the page the
user is working in. `GET /tool-configs/child-options` answers the same way for the
same reason.

A tool belonging to someone else is refused with the same "not found" sentence a
missing one gets — answering differently would confirm the uuid is real. Ownership is
not re-implemented here: it comes from `tool_config_service.get_tool_config`,
`data_agent_service.get_data_agent` and `workspace_service.get_workspace`, and from
the `DataAgent.user_id` filter inside every query this module composes.

A selection is kept in the address bar with `replaceState`, so `/tool-graphs?tool=…`
is a link someone can paste into a ticket without filling the back button with twelve
canvas states.

---

# Testing

```bash
docker compose exec -T app python -m pytest tests/unit/services/tool_graphs \
    tests/unit/routes/tool_graphs tests/unit/schemas/tool_graphs -q
```

The service tests are where the drawing is actually verified — the layering of a
three-level chain, a shared child resolving to one node with two edges, `START`
attaching only to leaves, a disabled tool surviving, descendants crossing an agent
boundary, and a hand-inserted cycle terminating. The route tests cover the JSON
shapes the renderer reads and the 200-with-`error` contract.

---

# Related

* [TOOL_CHAINING.md](TOOL_CHAINING.md) — the nesting this page draws, and the
  LangGraph it compiles to
* [QUERY_JOINS.md](QUERY_JOINS.md) — the joins this page draws, and the rules that
  validate them
* [TOOL_QUERY_MODES.md](TOOL_QUERY_MODES.md) — why a SQL-mode statement is not parsed
* [QUERY_TEST.md](QUERY_TEST.md) — the other read-only view of a tool query, which
  runs it instead of drawing it
* [GRAPH_DESIGNER.md](GRAPH_DESIGNER.md) — the writable canvas this page links to, where a
  graph is drawn rather than derived, and run
* [SCHEMAS.md](SCHEMAS.md#tool_graphs--appschemastool_graphstool_graph_schemaspy) —
  `ToolGraphQuery`, `ToolGraphResponse`, `ToolJoinsResponse`
