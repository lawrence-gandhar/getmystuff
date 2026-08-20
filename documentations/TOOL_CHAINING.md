# TOOL_CHAINING.md
Nested tool configs — tools that embed tools, run as a LangGraph

---

# What it is

A tool config may **embed** other tool configs. The inner tool runs first, one named
column of its result becomes a list of values, and the outer query is restricted to
them. It is a sub-query written as two reusable tools instead of one large statement.

```
paid_invoices          → client_id  ─┐        (deepest)
active_clients         → id        ─┐│
projects_by_client   WHERE projects.client_id IN (…)   (root — the tool the agent calls)
```

* `paid_invoices` runs → 41 `client_id` values → restrict `active_clients`
* `active_clients` runs → 12 `id` values → restrict `projects_by_client`
* `projects_by_client` runs → **its rows are the tool's answer**
* any level returning **nothing** → the chain stops there, the tool returns 0 rows,
  and nothing above it runs

Every tool in a chain is still a tool: `active_clients` keeps its own name,
description and agent, and can be called on its own.

| Where | File |
|---|---|
| The edge | `app/models/tool_configs/tool_chain.py` — `tool_config_links` |
| Structure and rules | `app/services/tool_configs/tool_chain_service.py` |
| Execution | `app/services/tool_configs/tool_chain_graph.py` |
| Value binding | `app/services/deep_agents/query_executor.py` |
| Form | `templates/tool_configs/partials/nested_tools_field.htm`, `static/js/tool_chain.js` |

---

# A child may be a graph instead of a tool

An embedded child can be a published [Graph Designer](GRAPH_DESIGNER.md) graph rather than
another tool config. `tool_config_links` carries two nullable child columns and exactly one
is set — `ck_tool_config_links_one_child` makes that true rather than conventional.

The edge means exactly the same thing: something runs first, one named part of its result
becomes a list of values, and the parent's query is filtered to them. What differs is what
"runs first" involves — one query, or a whole drawn sequence of queries, loops and checks.

**Three things about a graph child are worth knowing before using one.**

**1. It can stop and ask a person a question.** No tool config can, and it gives a chain a
*third* outcome besides rows and "nothing matched": `ChainResult.asked`, carrying the
question, the run it is parked on and the node that asked. The parent tool's output becomes
the question, relayed word for word with a run id; a companion `answer_<tool>` tool takes
the reply, finishes the graph, and then **re-runs the chain with the graph's values
supplied** (`run_chain`'s `resolved`) — because re-running the graph would ask the same
question of somebody who has just answered it.

**2. Its values are read from the whole result, never the preview.** `graph_runner.rows` is
a twenty-row sample with the real total beside it, which is right for *describing* a result
and catastrophic for building a filter out of: a parent restricted to the first twenty of
five hundred ids answers a different question than the one asked and looks exactly like an
answer. So `graph_runner.full_result` reads the uncapped output from the checkpointer —
nothing re-runs — and `graph_values` takes the named key out of it.

**3. Two rules are relaxed, and neither is a loophole.** A graph child is *not* held to the
same-datasource rule, because a graph's nodes each name their own datasource and there is
no single one to compare — that rule is unanswerable here rather than waived, and the
judgement it protected moves to the graph's author, node by node. And its `child_column` is
not checked against a column list, because nothing knows what a graph's last node returns
until it runs; a name that matches nothing comes back as "no values", exactly as it does
for a SQL-mode tool config.

## The cycle rule that prevents a hang

A graph's `tool_config` node runs that tool **including its chain**. So:

```
tool P ──embeds──▶ graph G ──tool_config node──▶ tool P
```

is unbounded recursion across separate LangGraph runs, where neither run's recursion limit
nor any loop ceiling applies to the other. Nothing would report it; the turn would simply
never end.

`_graph_reaches_tool` refuses it when the link is saved, and it walks **both kinds of edge**
because the cycle can alternate between them — a graph's node may name a tool that embeds
another graph that reads the first tool. The same walk filters the picker, so the choice is
never offered in the first place.

**A graph embedded in a tool cannot be deleted or unpublished.** Either would drop the
parent's filter and let it quietly return more rows than it should — the failure this whole
feature is designed against, and the same reason a tool config that something embeds cannot
be deleted either.

---

# The graph

```
START → paid_invoices → active_clients → projects_by_client → END
              │                │
              └── no values ───┴───────────────────────────→ END
```

One **node** per tool, added deepest-first. **Edges** run upward in topological
order. A **conditional edge** after every inner node is where the short circuit
lives: a node that produced no values sends the run to `END`, so the tools above it
are never executed and no `IN ()` is ever built.

Why a graph and not a loop: the behaviour asked for — evaluate inside-out, propagate
outward, stop the moment a level produces nothing — *is* a control-flow graph, and
writing it as one makes the control flow the thing you read rather than something
reconstructed from `if`s and `break`s. It also puts the chain on the same footing as
the agent that calls it; both are LangGraph runs.

**Siblings run in sequence, not in parallel.** LangGraph would fan them out happily,
and it is the wrong trade here: the first sibling to return nothing ends the run, so
running them in order means the second is never executed at all. Chains are short by
construction, so parallelism would buy a fraction of one query's latency in exchange
for always paying for queries whose answer cannot matter.

The graph is compiled **once**, in `tool_factory._build_tool`, and kept in the tool's
closure — a nested tool call costs an `ainvoke`, not a rebuild.

---

# What crosses an edge

**Values. Never rows, never text.**

| Parent mode | How the values arrive |
|---|---|
| builder | `_value_conditions` adds `column.in_([…])` over a **reflected** column — the same resolver every stored filter goes through, so an embedded tool cannot reach a column that does not exist or one switched off in Data Sources |
| sql | the statement names `:active_clients`; the list is bound as an **expanding bind parameter**. The statement is not rewritten — it is the exact text the operator approved, re-checked by `validated_tool_sql` on every run |

A link may instead bind **one value at a time** and run the parent once per value —
`binding_mode` `each`, which is what an `IN` cannot express and what "for each
department" actually asks for. The values still cross the edge the same way; what
changes is how many times the parent runs and what shape the parameter takes. See
[TOOL_CHAIN_ITERATION.md](TOOL_CHAIN_ITERATION.md).

A value shaped like an injection (`1) OR (1=1`) is compared as a value and matches
nothing, in both modes. That is asserted in `test_query_executor.py`.

The inner tool's *rows* are discarded at the edge. They are not returned to the
agent, not carried up the chain, and not logged — exactly as a sub-query's inner
rows are not part of an outer query's result.

### Each tool is still run as itself

The chain is **not** compiled into one nested SQL statement. Every node goes through
`query_executor` with its own validation, its own active-table and active-column
checks and its own cap, so a tool behaves identically whether it was called directly
or embedded. That is what makes "the child works on its own too" true rather than
approximately true — and it is also what makes the short circuit possible, since a
single compiled statement would just return zero rows without anyone knowing why.

---

# The limits

| Limit | Value | What it bounds |
|---|---|---|
| `MAX_CHAIN_DEPTH` | 5 | every level is a database round trip inside a turn a visitor is waiting on |
| `MAX_CHILDREN_PER_TOOL` | 5 | more than a handful is a query that wants writing as SQL |
| `MAX_CHAIN_NODES` | 20 | the whole tree, root included |
| `MAX_CHAIN_ITERATIONS` | 50 | how many times an `each` link may **re-run** the parent |

Every one of them bounds the **shape or the cost** of a chain. None of them bounds how
much data it returns, and that is the change: a chain reads every value and returns
every row.

**Two row caps used to be here and both are gone.** `MAX_CHAIN_VALUES` capped an inner
tool at 2,000 values, and `MAX_TOOL_ROWS` capped the root's result at 200. They did the
same damage from opposite ends. A truncated `IN` list builds a filter that answers a
*different question* than the one asked — it runs, it returns rows, and nothing about
the result says so — which is why the chain was refused rather than truncated. And a
root result cut to 200 was a sample of somebody's data reported as their answer.

Refusing was the honest response to a cap, but the cap was the problem: a tool over the
limit simply could not be embedded. One was found live — `fetch_project_details`
embedding `fetch_projects`, failing every call at 2,921 values. It now runs.

**The diagnostic that came out of that case is still worth keeping**, because it is
about a chain that does nothing rather than a chain that is too big. A child reading the
*same table* as its parent, matching that table's key against itself, is a tautology:
`project_details.id IN (SELECT id FROM project_details)` selects exactly the rows it
started with. It cannot change a result, and it costs a full round trip and a full `IN`
list to discover that — more now than before, since neither is bounded. That case was
exactly this shape: both tools on `project_details`, `id` joined to `id`, the link
achieving nothing. A self-referencing chain is almost always a link added by accident,
so look at the tables and the two columns.

**A chain that is genuinely too expensive** — `MAX_CHAIN_ITERATIONS`, the one refusal
left — is a parent re-run once per value, so fifty values is fifty statements inside one
chat turn. Rewriting the parent in SQL mode as a `JOIN` answers the same question in one.

**Who is being told to narrow something matters, and the advice used to get this
wrong.** It said *"the inner query needs narrowing"* — the inner query is a **tool
config**, so that is addressed to the operator. A model relaying it to a visitor turns
it into *"please specify a date range"*, and a tool takes no arguments, so there is no
date range to specify. Every rephrasing routes to the same tool, hits the same refusal
and returns the same sentence: observed live as *"latest projects"*, *"August"*, *"August
2026"* producing one identical refusal each, with the tool never running once.

The advice now says the tool needs reconfiguring by whoever set it up and explicitly
forbids asking the visitor to narrow anything. Grounding rule 11 in `prompt_builder`
makes that general — the model is told outright that tools take no arguments and that
rephrasing cannot change a result — so the next tool failure to carry hopeful advice
cannot reopen the same loop.

**A failing chain must not silence an agent that has a working tool.** Grounding rule
13 tells the model that `TOOL FAILED` means *that tool* could not run, not that the
data is unreachable, and to try one alternative before giving up. Without it, the
agent above answered "I cannot provide a list of projects" while `fetch_projects` sat
beside it, enabled, reading the same table, and working.

---

# Agents

**Giving an agent a nested tool gives it the whole chain.** `collect_agent_tools`
returns the agent's own enabled tools *plus every transitive child*, so:

* the children run as part of the parent, as they must;
* the agent can also call a child directly, which is the point of a child being a
  tool rather than a sub-query buried in someone else's SQL;
* the routing prompt describes all of them, because the prompt and the tool list are
  built from one list — they cannot describe different sets.

**No row is moved.** A child keeps its own `data_agent_id`, so embedding a shared
tool never takes it away from the agent that owns it. Two agents can each hold a
parent that embeds the same child, and the child stays where it is.

The prompt tells the model what a chain means:

> Runs active_clients first and reports only the rows matching what they return. That
> restriction is fixed. If any of them finds nothing this tool returns no rows, which
> means nothing matched — say so plainly rather than treating it as an error, and do
> not call those tools separately to work around it.

Without that last clause a model reads an empty result as a broken tool and
apologises for the data instead of reporting the answer.

---

# What is refused, and why

Each of these produces a *plausible wrong answer* rather than an error, which is why
none of them is left to run time.

| Refused | Because |
|---|---|
| A cycle, direct or transitive | the chain runs depth-first with no visited set — a cycle is a hang, not a wrong number |
| A child on another datasource | only values cross, so it would *run*; matching an id from one system against an id in another is a coincidence, not a join |
| A child owned by someone else | ownership runs tool → agent → user, and a tool you do not own is **404**, not 403 |
| A disabled child | `is_enabled` is the operator's "stop using this"; a parent running it anyway makes the switch a lie |
| Deleting or disabling an embedded tool | the parent would keep running with its filter gone — more rows than it should return, and nothing saying so |
| A `:name` no child fills, or a child naming a `:name` the statement lacks | either way the statement cannot run; the two checks exist separately because they are the same fault from opposite ends |
| A column the child does not return | checked when the child's output is knowable; a SQL child's columns are not, so the name is verified against the real result at run time |
| The same child bound to the same target twice | it would AND a list against itself. The same child on *two* targets is allowed — one tool returning client ids can restrict both `owner_id` and `billed_to_id` |

Deleting the **parent** is fine: the link described that tool's query and goes with
it. Both foreign keys cascade, so a deleted agent or datasource cannot strand rows —
but the delete guard, not the cascade, is what protects a live parent.

---

# The form

A **Nested Tools** card between the query mode and the builder, because it belongs to
both modes. One row is one sub-query: *run `child`, take its `column`, and filter this
query at `target`*.

* `target` follows the query mode live — a column of this query in builder mode, a
  `:placeholder` name in SQL mode. Switching mode re-draws the rows rather than
  leaving a control that means nothing.
* The picker is filled by `GET /tool-configs/child-options`, which applies the same
  rules the save would: same owner, same datasource, enabled, not this tool, and
  never a tool that already embeds it. **The cycle rule is applied before the
  operator can build one.**
* The card is reset out of band when the datasource changes — a child must read the
  same datasource as its parent.
* Everything posts as one hidden `children_json` array, for the same reason the
  builder posts one `config_json`: three parallel controls could arrive at different
  lengths, and a row would then pair the wrong column with the wrong tool.

**Test Query runs the whole chain.** A nested tool tested without its children is a
different, unrestricted query, and a pass on it would say nothing. If an inner tool
matches nothing the test *passes* and says so — every query ran and the database
accepted them all, which is what was asked; the tool would simply return no rows
today. See [QUERY_TEST.md](QUERY_TEST.md).

### The list

One row per tool, as before — so the filter, the enable toggle and delete keep
meaning what they did, and a child embedded by two parents is not listed twice. A
parent's row shows its chain indented beneath the name with a `nested` badge; a
child's row carries *embedded in …*. The child is listed on its own because it is
callable on its own.

---

# Testing

| File | Runs |
|---|---|
| `tests/unit/services/tool_configs/test_tool_chain_service.py` | anywhere — the rules deliberately do not import LangGraph |
| `tests/unit/services/tool_configs/test_tool_chain_graph.py` | container only (`pytest.importorskip("langgraph")`) |
| `tests/unit/services/deep_agents/test_query_executor.py` | anywhere — the binding and value-query cases |

The graph tests use a real SQLite database whose data disagrees on purpose — client 1
is paid *and* active, client 2 is paid but churned, client 3 is active but unpaid — so
a chain that skips a level returns more rows and the test notices.

```bash
docker compose exec app python -m pytest tests/unit/services/tool_configs -q
```

---

# Related

* [TOOL_CHAIN_ITERATION.md](TOOL_CHAIN_ITERATION.md) — the other binding mode (run the
  parent once per value), the value alias, and `:name` values the assistant supplies
* [TOOL_QUERY_MODES.md](TOOL_QUERY_MODES.md) — the two ways a tool query is written
* [DEEP_AGENTS.md](DEEP_AGENTS.md) — the runtime, the row cap, and what a model may see
* [QUERY_TEST.md](QUERY_TEST.md) — the Test Query button, chain included
* [TOOL_GRAPHS.md](TOOL_GRAPHS.md) — this chain drawn as the graph it compiles to,
  including the shared child and the disabled node a list row cannot show
* [GRAPH_DESIGNER.md](GRAPH_DESIGNER.md) — the other authored LangGraph in this
  application. A chain expresses one idea (the child's values restrict the parent); a
  designed graph expresses control flow, and can run a tool config as one of its nodes.
  It reuses the one-list rule below for its own agent tools
* [MIGRATIONS.md](MIGRATIONS.md) — `a4d6b28f7e15_add_tool_config_links`,
  `b9c4e7f21a08_add_tool_config_link_binding_mode`
* [SCHEMAS.md](SCHEMAS.md) — `children_json`, `ChildToolOptionsResponse`
