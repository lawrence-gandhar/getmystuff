# GetMyStuff — The User Guide

**Talk to your data. Get real answers, or an honest "I can't".**

This is the one document to read if you want to understand *what* GetMyStuff does, *how*
you use it, and *why* it behaves the way it does. It explains the workflows and the
technology in plain language. It deliberately does not walk through files, functions or
code — the other documents in this folder do that, and each is linked from here where it
goes deeper.

---

## Contents

1. [What this platform is](#1-what-this-platform-is)
2. [The one promise everything is built around](#2-the-one-promise-everything-is-built-around)
3. [The pieces, and how they fit together](#3-the-pieces-and-how-they-fit-together)
4. [Your first hour: the end-to-end journey](#4-your-first-hour-the-end-to-end-journey)
5. [Workflow: connecting your data](#5-workflow-connecting-your-data)
6. [Workflow: teaching an assistant what it may read](#6-workflow-teaching-an-assistant-what-it-may-read)
7. [Workflow: letting AI write the query for you](#7-workflow-letting-ai-write-the-query-for-you)
8. [Workflow: testing before anyone depends on it](#8-workflow-testing-before-anyone-depends-on-it)
9. [Workflow: publishing a chatbot on your website](#9-workflow-publishing-a-chatbot-on-your-website)
10. [Workflow: scripted conversations and knowledge bases](#10-workflow-scripted-conversations-and-knowledge-bases)
11. [Workflow: letting the assistant call your other systems](#11-workflow-letting-the-assistant-call-your-other-systems)
12. [Workflow: when the answer is too big to print](#12-workflow-when-the-answer-is-too-big-to-print)
13. [Workflow: filters and totals over an entire result set](#13-workflow-filters-and-totals-over-an-entire-result-set)
14. [Workflow: drawing a pipeline](#14-workflow-drawing-a-pipeline)
15. [Workflow: moving records between systems](#15-workflow-moving-records-between-systems)
16. [Workflow: telling someone when something happens](#16-workflow-telling-someone-when-something-happens)
17. [Workflow: seeing the shape of what you built](#17-workflow-seeing-the-shape-of-what-you-built)
18. [Workflow: watching how it performs](#18-workflow-watching-how-it-performs)
19. [The technology, explained without jargon](#19-the-technology-explained-without-jargon)
20. [Choosing a language model](#20-choosing-a-language-model)
21. [How your data is kept safe](#21-how-your-data-is-kept-safe)
22. [The house rules — why refusals happen](#22-the-house-rules--why-refusals-happen)
23. [Every limit, in one place](#23-every-limit-in-one-place)
24. [Troubleshooting: what a message means](#24-troubleshooting-what-a-message-means)
25. [Glossary](#25-glossary)
26. [Where to read more](#26-where-to-read-more)

---

# 1. What this platform is

GetMyStuff connects to the databases and files a business already has, and lets people ask
questions of them in ordinary English — in a chat window, on a website, without writing SQL
and without waiting for a dashboard to be built.

You can point it at PostgreSQL, MySQL, SQLite, MongoDB, and uploaded files (CSV, Excel,
JSON, Parquet, Avro). You then decide, precisely, which questions an assistant is allowed to
answer, and publish that assistant as a chat widget your visitors can use.

The unusual part is what happens underneath. Most "chat with your data" tools hand an AI
model a sample of your rows and hope it reasons correctly. This one does not. **The model
never touches your database.** Instead, *you* write the queries — or approve the ones the AI
drafts for you — and the model's only power is to decide which of your approved queries to
run.

That single design decision is the reason the rest of the product looks the way it does.

---

# 2. The one promise everything is built around

> **The AI can only ever run a query a human wrote or approved. It cannot see your
> schema, cannot write SQL, and cannot invent a number.**

Here's the difference in practice.

**The old way** (still available, and fine for exploratory work):

```
Question → grab up to 500 sample rows → summarise them statistically
         → paste that summary into the AI's prompt → AI reasons over the sample
```

The AI is looking at a *sample*. Ask it for a total and it will give you one, computed from
whatever rows happened to be sampled. It looks like an answer. It is a guess wearing an
answer's clothes.

**The GetMyStuff way**, once you attach a Data Agent:

```
Question → AI sees a list of tool names and descriptions (no data, no schema)
         → AI picks one tool
         → the platform runs the saved query against your database
         → the resulting rows go back to the AI
         → AI answers using only those rows
```

No tool call means no data. And that is visible, not just claimed: the test console lists
exactly which tools were called for every answer, so "figures with no tool call behind them"
is a bug you can *see* rather than a risk you have to trust somebody about.

Three consequences worth internalising, because they explain most of the product's
personality:

- **An assistant can only answer what you gave it a tool for.** If nobody built a tool for
  "revenue by region", the assistant will say so rather than approximate. That is the
  feature, not a gap.
- **The AI can't widen what you narrowed.** A tool scoped to `status = 'paid'` stays scoped
  to paid, no matter how the visitor phrases the question.
- **When something is misconfigured, you get told what to fix.** Not "sorry, something went
  wrong" — the actual table or column name, and the actual thing to change.

---

# 3. The pieces, and how they fit together

Seven concepts. Once these click, the whole app reads easily.

| Thing | In one sentence |
|---|---|
| **Data Source** | A connection to one database, or a set of uploaded files. |
| **Workspace** | A folder for organising Data Agents. Optional. |
| **Data Agent** | One assistant with one job, holding a set of tools and its own standing instructions. |
| **Tool Config** | **One question the assistant can answer, expressed as one query.** The most important object in the product. |
| **Agent (chatbot)** | A publishable chat widget with an API key, appearance settings, and an allowed-domains list. |
| **Flow** | A drawn conversation script — buttons, branches, scripted replies — that runs *before* the AI gets a turn. |
| **Action** | An outbound HTTP call (an order lookup, a ticket creation) the assistant may make mid-conversation. |
| **Connection** | One authenticated account on an outside system — a store, a CRM — that a workflow can read from and write to. |
| **Workflow** | A drawing that **moves records** between two systems, published as a frozen version and run on a schedule. |

And how they nest:

```
Data Source  ─────────────┐
  (your database)         │
                          ▼
Workspace                Tool Config ──── embeds ────► other Tool Configs
  └── Data Agent  ◄── owns ──┘                          (nested sub-queries)
        │                    ▲
        │                    └─ each one names: a datasource, the tables it may read,
        │                       the query, and optionally values the AI fills in
        ▼
   Agent (chatbot widget)  ── published on your website
        ├── optionally: a Flow (scripted conversation, gets first refusal each turn)
        ├── optionally: Actions (webhooks it may call)
        └── either a Data Agent, OR a plain datasource target — never both
```

**The exclusivity at the bottom matters.** A widget answers *either* from an attached Data
Agent's tools, *or* from a datasource you nominated directly. Not both. Two different
answers to "what can this thing read?" would be resolved differently depending on whether
the agent happened to run that turn — so the platform refuses to store both and the form
hides one when you pick the other.

### The sidebar, annotated

| Entry | What you do there |
|---|---|
| **Data Sources** | Add connections, upload files, switch tables and columns on or off |
| **Workspaces** | Group Data Agents |
| **Data Agents** | Create assistants, write their standing instructions, open their test console |
| **Tool Configs** | Build the queries an assistant is allowed to run (there's a **Help** button here) |
| **Tool Graphs** | See your tools drawn as diagrams (read-only) |
| **Pipelines** | Draw and run a data pipeline on a canvas — and use it from an agent, a workspace, a tool config or a flow |
| **Integrations** | Connect the outside systems you use (**Apps**), keep their credentials (**Connections**), and move records between them on a schedule (**Workflows**) |
| **Aggregations** | Ask for a total over an entire result set, without spending a chat turn |
| **Agents** | Create and configure publishable chat widgets |
| **Flow Builder** | Draw scripted conversations |
| **Actions** | The shared library of webhooks |
| **Chatbot Analytics** | Speed, token spend, failures, per-agent traffic |
| **AI Settings** | Your language-model API keys |

---

# 4. Your first hour: the end-to-end journey

The happy path, in order. Each step links to its own section.

```
1. Connect a data source                          → §5
       ↓
2. Switch off tables and columns nobody should read → §5
       ↓
3. Create a Data Agent, write its instructions      → §6
       ↓
4. Build 5–10 Tool Configs — one per question       → §6  (or let Ask AI draft them → §7)
       ↓
5. Press Test Query on each one                     → §8
       ↓
6. Open the agent's test console, ask real questions → §8
       ↓
7. Create an Agent (chatbot), attach the Data Agent  → §9
       ↓
8. Add your website's domain to Allowed Domains      → §9
       ↓
9. Download widget.js, paste the snippet, go live    → §9
       ↓
10. Watch Chatbot Analytics for a week               → §16
```

Steps 4 and 5 are where the work is, and where the quality of the whole thing is decided. A
library of eight narrow, well-described tools beats one wide vague tool every single time.

---

# 5. Workflow: connecting your data

### Adding a connection

Give it a name, pick the type, fill in the host, port, database, username and password.
The platform tests the connection there and then. If it fails you get a sentence naming
what was attempted — *"Could not connect to 'pantry_mate' at localhost:5432"* — and the
driver's own technical babble stays in the server log where it belongs.

Passwords are **encrypted at rest**. So are the header values on your Actions, because
that's where bearer tokens live.

### Uploading files

CSV, Excel, JSON, Parquet and Avro. Files are validated for type and size, and processed
in chunks rather than being loaded whole — so a large upload doesn't have to fit in memory.
Uploading a file with a name that already exists asks you whether you meant to replace it
or keep both as versions.

### The two switches that govern everything

This is the most consequential screen in the product, and it's easy to walk past.

Every table in a data source has an **on/off switch**, and so does every column inside it.

```
orders               [ON]
  ├── id             [ON]
  ├── customer_id    [ON]
  ├── total          [ON]
  └── internal_note  [OFF]   ← nobody builds a tool that reads this, ever
```

**What "off" actually means:** a switched-off table or column is not merely hidden. It is
not offered in any picker, it is stripped out of the schema the AI is shown when drafting
SQL, and — crucially — it is re-checked **every single time a tool runs**, not just when
the tool was saved.

Two behaviours follow that surprise people, and both are deliberate:

- **The table switch owns its columns.** Switching a table off switches all its columns
  off. Switching it back on switches them all back on — discarding the per-column choices
  you made before. The table switch is the coarse control and it wins. (An active table
  whose columns are all off contributes nothing, which reads as the activation having
  silently done nothing.)
- **Switching a column off does not break the tool that reads it — it makes that tool fail
  loudly.** The column isn't quietly dropped from the query. Dropping a filter would return
  *more* rows than it should; dropping a grouping would change what each row counts. Either
  way the query still comes back with a number the assistant would state as fact. A tool
  that says "I need reconfiguring" is recoverable. A plausible wrong figure is not.

**Absent means active.** A brand new data source, or one created before metadata collection
worked, has no switches recorded — everything is treated as on. Only the literal "off" turns
something off.

---

# 6. Workflow: teaching an assistant what it may read

This is the heart of the product. Read this section twice.

### The mental model

```
Data Agent            one assistant, one job
  └── Tool Config     one question it can answer with one query
        ├── which datasource, and which tables it may read
        ├── the query — built with dropdowns, or written as SQL
        ├── optional: values the assistant fills in per call
        └── optional: other tools embedded as sub-queries
```

Three principles, and every option in the form is a consequence of one of them:

1. **You write the query; the assistant only chooses when to call it.** It runs exactly as
   saved.
2. **A tool is a standing permission.** Saving one says "this agent may read this, in this
   shape, whenever it judges the question calls for it."
3. **One tool = one question.** When a query starts needing "and also…", the answer is
   usually a second tool.

### Write the description as the question it answers

The name and the description are the *entire* basis on which the AI decides to call your
tool. This one habit removes most routing problems:

```
✗ "Inventory data"
✗ "SELECT with a GROUP BY on category"
✓ "How many units are in stock, broken down by product category. Use for stock
   levels and 'how much of X do we have' questions. Current stock only — for
   what was ordered, use order_totals."
```

That last sentence — what it's *not* for — is what stops the AI reaching for the wrong tool
when two are close together.

### Two ways to hold a query

| | **Query builder** | **SQL query** |
|---|---|---|
| How you write it | Dropdowns: columns, aggregations, group-by, filters, joins | You type the statement |
| Available for | Every datasource type | Relational only (PostgreSQL/MySQL/SQLite) |
| Strength | Every name is checked against the real tables; every value is safely bound; rebuilt from scratch on each run | Anything a read-only query can express |
| Use it when | The query fits the builder's shapes | You need `DISTINCT`, `ORDER BY`, `LIMIT`, `HAVING`, subqueries, CTEs, window functions, `UNION`, `CASE`… |

The rule is: **if it's a valid read-only query, a tool can run it.** Which mode it lands in
is a question of how well the builder can hold it, never of whether it's allowed. Switching
modes mid-edit costs nothing (both panels stay live), but *saving* in one mode clears the
other — so a tool never holds two queries.

A statement must be **one read**. Multiple statements, or anything that writes, is refused
with a message saying so. Comments and semicolons *inside string values* are fine — the
check blanks out quoted text and comments before it looks for trouble, so a pattern like
`'%s:6:"depart";%'` reads as what it is.

### The scenarios, briefly

Each of these is a worked example in [TOOL_CONFIGS.md](TOOL_CONFIGS.md), which is also
served in-app at the **Help** button beside *New Tool Config* — a new tab, so it doesn't
cover the form you're reading it for.

| You want | Do this |
|---|---|
| A single number | One aggregation. `COUNT` of `id`, alias `total_items`. The **alias is the key the AI sees**, so `total_items` reads as an answer where `count` reads as a column name |
| A filtered list | Name your columns, add a filter. Prefer naming columns over leaving it blank (blank means "every active column") |
| A grouped report | Columns + aggregations + group-by. **Every plain column you select must also be grouped** — else the save is refused, because both PostgreSQL and MySQL would refuse it at run time, i.e. mid-conversation in front of a visitor |
| One tool answering many questions | Tick **Agent fills in** on one filter. It stores no value; the AI supplies the value per call. One `fetch_projects` then answers August, September and any other month |
| "Has a value" / "is empty" | Four operators compare against nothing: `IS NULL`, `IS NOT NULL`, `IS BLANK`, `IS NOT BLANK`. Use the BLANK pair for text — `IS NOT BLANK` is what "not empty" means when a person says it out loud, and it's the one thing the old `!= ''` workaround could never express |
| Two tables | Add a join. **Put the primary table first** in the Tables field — it's the base table everything hangs off. Once there's a join, every column reference becomes `table.column` |
| Something the builder can't express | SQL mode. Still fill in the Tables field: nothing parses your statement, so that list is what the platform reports and re-checks |
| A SQL tool the assistant parameterises | Write `:department_id` yourself, then declare it under *Values the assistant supplies* with a type and a description |
| A sub-query, reusable on its own | Nesting — see below |
| A total the assistant computes over every record, not the 200 it is shown | Tick **Allow whole-result grouping** — see [§13](#13-workflow-filters-and-totals-over-an-entire-result-set) |
| A filter the tool takes no parameter for — one month, one department | The same tick. The records are narrowed after they're read — see [§13](#13-workflow-filters-and-totals-over-an-entire-result-set) |
| A CSV, Excel, Parquet or Mongo source | The form narrows itself: builder only, no joins, no nesting. You still get columns, aggregations, grouping, filters and assistant-supplied filter values — enough for most "how many / how much / which ones" questions over a spreadsheet |

### What "Agent fills in" actually lets the AI do

This is the one place model-generated text reaches a query, so it's worth being exact.
What the AI supplies is **the right-hand side of one comparison you chose to open**:

- the **column** comes from your stored setting;
- the **operator** comes from your stored setting;
- the **value** is converted to the column's own type and **bound as a parameter** — the
  same mechanism that has always made a stored filter value data rather than SQL;
- **every other filter still applies.** Opening one cannot relax another.

So `0 OR 1=1 --` passed as a value matches nothing. A column name passed as a value is
compared against the *text* of that column name, not swapped in for the column. And if a
required value wasn't supplied, the query is **refused** rather than run without that
clause — dropping the clause would return every row and look like a working answer.

### Nesting: tools that embed tools

A tool may embed others. The inner tool runs first, one named column of its result becomes a
list of values, and the outer query is restricted to them.

```
paid_invoices        → client_id  ─┐        runs first
active_clients       → id        ─┐│        runs second, restricted by the above
projects_by_client   WHERE client_id IN (…) ← the tool the assistant actually calls
```

Read a nested row as a sentence: *run `active_clients`, take its `id`, and filter this
query's `projects.client_id` to match any of them.*

Things worth knowing before you build a chain:

- **An empty child empties the parent.** If `active_clients` finds nothing, the parent
  returns nothing and everything above it never runs. The alternative — a parent silently
  dropping its restriction and returning *more* than it should — is worse.
- **Children stay tools.** They keep their own name and description, and the assistant can
  call them directly too. Giving an agent a nested tool gives it the whole chain.
- **Order matters.** Children run in the order listed and the first empty one stops the
  chain. Put the cheapest or most selective first.
- **Two shapes of binding.** *Match any* builds one `IN (…)` and runs the parent **once**.
  *Run once per value* runs the parent **once per value** and concatenates the rows — which
  is what you need when the placeholder isn't on the right of an `IN` (`dd.id = :x`, or a
  value the database builds a string around). *Match any* is one round trip; *run once per
  value* is N statements, so prefer the first and reach for the second when the SQL leaves
  you no choice.
- **`record the value as`** is how you keep an iterated result readable. Rows from twenty
  runs of one statement are otherwise indistinguishable. Leave it blank when the query
  already returns the value — asking for a name the query already returns is refused as a
  collision rather than overwriting the database's own answer.
- **A tool that embeds another cannot be deleted or disabled** while that's true. The parent
  would keep running with its restriction gone, returning more rows than it should, silently.
  The refusal names which tool is in the way.
- **Check the chain is doing anything.** A child reading the *same table* as its parent,
  matching that table's key against itself, is a tautology: it can't change the result and
  it costs a full round trip and a full value list to discover that. One real case had a
  chain failing every call at 2,921 values — that no longer fails, since nothing caps the
  value list, but the link was still doing nothing and removing it is still the fix.

### Living with tools that already exist

| You want to | Do this | What happens |
|---|---|---|
| Revoke a capability, keep the definition | **Disable** | Stops being offered to the agent; the query is untouched |
| Hand a tool to a different agent | Change **Data Agent** | Both agents' descriptions of their own tools regenerate — the old one was still advertising it |
| Point it at a different datasource | Change **Datasource** | The query resets. A query belongs to the datasource it was written against |
| See what a tool actually runs | The **Query** column | The rendered query, or the statement, with a badge saying which. Chains show indented |
| Work on one agent's library | Use the agent filter | It survives every save |

**A datasource being unreachable never makes a tool uneditable.** You get a warning above
the saved values and you can still edit them. Likewise, switching a column off doesn't block
editing — it makes the tool fail when *run*, which is where that check belongs.

---

# 7. Workflow: letting AI write the query for you

**Ask AI** turns a plain-English request into SQL for one of your relational data sources.

### The promise, and it's a narrow one

**The model is shown structure, never data.** It receives the table names, column names and
types, primary keys and foreign keys of the tables you picked — and nothing else. No row is
sampled. No count is taken. **And the query it writes is not run.** It's handed to you to
read, refine and use.

That last point is what makes the promise true of the whole feature rather than only of the
prompt: there is no code path in that part of the application that executes what the model
wrote.

Only the tables and columns you left switched on are included — inactive ones are *pruned
out of the schema* rather than checked afterwards. A model cannot select, join on or filter
by a column it was never told exists.

### What you get back

- **The SQL**, or an *empty* result with an explanation — *"there is no order date column"*
  is a valid, useful answer and the panel presents it as one rather than as a failure.
- **An explanation** of what the query returns and how.
- **Assumptions** — up to five notes on anything guessed: a join inferred without a foreign
  key, a column read as a date, an ambiguous word in your request.
- **An amber note** listing active columns the query never mentions.
- **A red note** if the grouping is one the database will refuse. The platform asks the model
  to try again once, then tells you rather than hiding it. (It regenerates rather than
  patching, because adding a column to a `GROUP BY` changes what the query counts, and the
  explanation beside it would then describe a different query.)

You can refine conversationally. Each follow-up re-sends the same schema plus the
conversation so far, so a refinement cannot reach any further into your data than the first
attempt did.

### Auto Create Tool

The button that saves what you just read as a Tool Config. It asks for the two things only
you can answer — what to call it and which agent gets it — and fills in the rest.

It tries the **query builder** first, because that's the stronger artefact: every identifier
checked, every value bound, and it reopens fully editable. When the query needs more than
the builder can hold, it's saved as **the statement itself**, with a note saying which mode
it landed in and why.

The model's reading of the query is **not trusted**. A bare column name in a joined query is
looked up, not assumed:

| The model says | Outcome |
|---|---|
| `orders.total` | Kept, after checking `orders` is in the query and really has `total` |
| `total`, and only `orders` has it | Qualified to `orders.total` |
| `id`, and both tables have it | Rejected as ambiguous, naming both |
| `profit_margin`, in no table | Rejected — either invented, or switched off in Data Sources. The message says both, because from there the two are indistinguishable |

"Rejected" means rejected *as a builder config*, not rejected as a tool: it's saved in SQL
mode instead, with the reason shown. The model's reading of the query was wrong; the query
itself never was.

Then open the created tool and check the **name, the description and the table list** —
those are the parts a generator guesses at, and the description is what routing depends on.

---

# 8. Workflow: testing before anyone depends on it

Everything between writing a query and an assistant calling it is checked. What none of
those checks can answer is the question that decides whether the tool works: **will this
database run this query?**

That's the database's answer to give. A grouping MySQL refuses. A column that exists in
staging and not in production. A join whose `ON` clause names the wrong side. Every one of
those passes every check the application can honestly make — and fails at run time.

Before the **Test Query** button existed, that failure message first appeared inside a chat
conversation, addressed to a visitor, as *"I cannot retrieve that figure right now."* The
person who could fix it never saw it.

### Test Query

A button on every panel holding an unsaved query — both Tool Config query cards, and the Ask
AI result. It runs the query **once**, reads **one row**, and reports what happened.

Three guarantees:

- **The query that's tested is the query that will be saved.** Same validators, same
  execution path, same fields. A test with looser rules would pass queries the form then
  refuses, which is worse than no test.
- **A nested tool is tested as the whole chain** — the same run an assistant's call would
  make. Testing the outer query with the children skipped would test a different,
  unrestricted query.
- **It shows no values.** The verdict is the column names and the row count. Proving a query
  runs needs a row *fetched*, not a row *displayed*.

If an inner tool matches nothing, the test **passes** and says so: *"the chain ran, but
`paid_invoices` matched nothing, so this query was not reached. Every query is valid — the
tool would return no rows until that inner tool matches something."* Every query ran and the
database accepted them all. That's what was asked, and it's not the same as being broken.

For a SQL tool holding a `:placeholder`, there's a **test value** box beside each declared
value. It's used by the button and **never saved** — the button's whole claim is that it ran
the query the tool will run, and the only honest value to run it with is one you typed.

### The agent test console

From the **Test** button on each Data Agents row. It shows the agent's tools, anything that
would stop it running, and — on every answer — **which tools were called**.

It exists so an agent can be verified before a visitor talks to it, and so the
"no-data-to-the-model" claim is *checkable* rather than trusted. An answer full of figures
with an empty tool list is a bug you can see.

The console renders the answer as raw monospaced text rather than prettifying it, on
purpose: the point of the page is that the tools-called list and the answer can be checked
against each other.

**A published graph appears in that tool list too**, marked *Graph* and described by its
drawing — "A designed graph, 4 nodes" — rather than by a table and a datasource, because its
nodes each read their own. If it contains an **Ask a human** box the list says so, and that
matters here more than anywhere: on the console, the person who gets asked is you.

Operators get a **900-second** budget here, because you ran it deliberately. A visitor gets
**120 seconds**, because a visitor is waiting.

---

# 9. Workflow: publishing a chatbot on your website

### Creating an Agent

Give it a name, then decide what it may read — and this choice is **fixed at creation**:

| Choice | What the widget may read |
|---|---|
| A **Data Agent** | That agent's tool configs. No datasource is stored on the widget at all |
| A **datasource target** | The whole datasource, or named tables / collections / files |

The datasource target is immutable afterwards, because repointing a published widget at
different data changes what every embedded copy answers about. Swapping *which Data Agent
answers* is allowed — that's a normal operational change. What you can't do is detach the
agent from an agent-backed widget entirely: that would leave a published key answering
nothing with no way back.

### Configuring it

Under **AI & Prompt**:

- **The system prompt** — its persona, tone and refusals. Up to 20,000 characters.
- **Prompt variables** — `{{COMPANY}}`, `{{SUPPORT_HOURS}}` and so on, filled from values
  you declare. `{{AGENT_NAME}}` is built in. Up to 30 variables, 500 characters each. Saving
  is **rejected** if the prompt references a variable you haven't defined, or if a defined
  variable has no value — a live chatbot must never send a half-filled prompt to a model,
  and a stray `{{COMPANY}}` is a misconfiguration you need to see.
- **Which language model answers** — any active key from AI Settings, one pinned key, or the
  in-built local model. See [§18](#18-choosing-a-language-model).
- **A Conversation Flow**, optionally. See [§10](#10-workflow-scripted-conversations-and-knowledge-bases).
- **A Data Agent**, optionally, changeable later.

Under **Actions**: attach webhooks from your shared library. See
[§11](#11-workflow-letting-the-assistant-call-your-other-systems).

Under **Appearance**: twenty-odd settings — colours, sizes, fonts, welcome text.

**Your prompt cannot override the grounding rules.** They're appended after your text, so
they're the most recent instruction the model reads. Answers may only use figures the
platform actually computed, and the assistant must say so when the data doesn't cover the
question. No persona opts out of that.

**One important interaction:** when a Data Agent is attached, your chatbot-level prompt and
variables aren't used for data questions — the Data Agent has its own standing instructions,
and those are what pair with its tool list. Your model choice and pinned key *are* still
honoured.

### Embedding it

Add your website's domain to **Allowed Domains**, download `widget.js`, and paste the
snippet. Only `apiKey` is required. `apiBase` is optional — omit it and every request is
same-origin, which can't suffer a scheme mismatch and needs no cross-origin permission at
all.

### How the widget behaves in the wild

The widget is the one part of the platform whose failures the server cannot see — it runs on
someone else's website. So it is built to **degrade rather than break**: if it can't fetch
its settings, it renders with default appearance and welcome text rather than showing your
visitors a stack trace.

That created a problem worth knowing about: a misconfigured widget looked identical to a
working one. Three unrelated causes all produced a healthy-looking widget titled "Chat with
us". So the rule became:

> **The visitor gets a sentence. The operator gets the request URL, the status, the server's
> message, and — where it's knowable — the likely cause,** in the browser console.

Including the one failure that's invisible from both sides: an HTTPS page pointing at an
`http://` API. The browser blocks that *before sending*, so the server logs nothing and the
browser reports a cross-origin error for a request it never made. The widget detects exactly
that combination and names both fixes.

**If a widget stops working after a platform upgrade, download a fresh `widget.js`.** The
file is generic — all configuration is fetched at runtime — so dropping the new copy in the
same path is the whole procedure, with no snippet change.

### Answers arrive as formatted text

A query result is a table, and the widget renders it as one — real headers, real rows, with
its own horizontal scroll so a six-column result doesn't force the chat panel wider than
your page allows.

Bold, italic, code, headings and lists work too. **Links and images deliberately do not** —
a Markdown link is the classic route from formatted text to script execution, and the
platform draws its own download button anyway, so the assistant is told never to write a
URL. The safety mechanism is that the model's text is escaped **before** any formatting is
looked for, which means every tag on your visitor's screen was written by the renderer and
no attribute is ever built from message text.

### When the assistant can't answer

| Situation | What the visitor sees |
|---|---|
| A misconfigured Data Agent on a widget that *also* has a datasource target | It quietly falls back to the older statistical-profile answer |
| A misconfigured Data Agent on an agent-only widget | *"I can't reach that data at the moment, so I'd rather not guess"* |
| An agent-only widget whose flow has ended and whose agent has no tools | *"That's everything I can help with here…"* — its flow **is** its scope, so it says that rather than describing data it never had. See [§10](#10-workflow-scripted-conversations-and-knowledge-bases) |
| The AI provider is having a busy minute | The same sentence. The real cause goes to your log, and a rate limit is told apart from a wrong key — because "check your API key" sends you hunting a fault that doesn't exist |
| A broken webhook | The answer degrades; the conversation doesn't break. A badge records what happened |

The visitor never sees an endpoint URL, a status code, or a misconfiguration message.

---

# 10. Workflow: scripted conversations and knowledge bases

Not every turn should go to an AI. **Flow Builder** is a canvas where you draw what the
widget says at each step — a welcome, a set of buttons, a branch on what they picked, a
scripted reply.

> **There is a Help page.** The **Help** button on the Flow Builder list — and on the canvas
> toolbar, so you never have to leave a half-drawn flow to reach it — opens every block
> explained, nine worked flows to copy, all the limits, and every "save refused" message with
> what to do about it. It opens in its own tab.
>
> If you read one thing there first, make it **Variables, honestly**: writing `{{NAME}}` in a
> message shows the visitor those exact characters. Message text is not a template, and that
> catches almost everybody once.

### Ownership and the two switches

Flows belong to **you**, not to a chatbot. You build one standalone, then attach it to an
agent from that agent's settings page. Two independent switches decide whether it actually
drives a conversation:

| Switch | Meaning | Set where |
|---|---|---|
| Active / Draft | Published or still being written | The Flow Builder list |
| Attached | Which agent runs it | The agent's settings page |

Both are required. So a finished flow can be parked without detaching it, and a draft can
sit attached while you finish it. Only flows that are active *and* unattached are offered in
an agent's dropdown — **a flow runs on at most one agent**, and deleting an agent detaches
its flow rather than destroying it.

**A flow needs no tools.** Nothing about attaching one depends on the agent's Tool Configs:
a Send Message / Menu / If-Else / AI Fallback conversation reads a knowledge base, not a
database, and runs perfectly well on an agent with no tools at all. If your flow isn't
answering, the cause is one of the two switches above — check that the flow is **Active**
and that it's the one selected in the agent's *Conversation Flow* dropdown.

### What happens when the flow ends

A flow drives the conversation until the visitor reaches a terminal point. After that, the
agent's own prompt takes over for the rest of that conversation — which is the point of the
handover, and why a flow doesn't have to script every possible question.

Where there is nothing to hand over *to* — no data source of its own, and a data agent with
no enabled tools — the widget says so in the flow's own terms: *"That's everything I can help
with here. Use the restart button at the top of this chat to go through the options again."*
That is a supported setup, not a broken one. Build a flow-only chatbot on purpose and it
behaves like one; the restart control in the widget header takes the visitor back to the
Start node.

### The Run Graph node

A flow block whose work is a whole **Pipelines** graph: pick a published one and it runs
as a single step of the conversation. Two exits — *done* and *failed* — and you should draw
the failed one; without it a graph that could not run ends the conversation, deliberately,
because a flow carrying on as though a step succeeded is how a visitor gets told something
untrue.

It says nothing by itself. Give it a variable name and it stores **how many** rows the graph
found, for a later Send Message or If/Else block to use.

**It is the only block other than Ask for Input, Menu and Dropdown that can make the flow
wait.** If the graph contains a **Human** box, its question goes to the visitor word for word
and the flow pauses until they reply — then carries on from the block after this one. See
[§14](#14-workflow-drawing-a-pipeline) for the whole picture.

### The AI Fallback node, and its knowledge base

A flow gets first refusal on every turn. When it reaches a point where a script can't help,
an **AI Fallback** node takes over — and you choose what grounds it:

- the attached datasource,
- **its own knowledge base**, or
- the prompt alone.

A knowledge base belongs to **one node**, not to the flow or the chatbot. You upload PDFs,
text files or Word documents (or type text directly), then press **Train**. Training extracts
the text, cuts it into paragraph-aware chunks, and converts each chunk into a numeric
fingerprint using a model that **runs locally on your own server** — nothing leaves the box
for this. At answer time, the question is fingerprinted the same way and the most similar
chunks are retrieved and handed to the model as context.

Re-training is safe to repeat: chunks that are already current under the same embedding
model are skipped, and everything is re-done if the model changed.

Two useful facts about performance: a grounded answer uses the **8 nearest chunks**, and
that number was measured rather than guessed. A fact planted at every rank from 1 to 8 was
recovered 8 out of 8 times, so the 5th–8th chunks are genuinely used — lowering it would be
a pure loss of recall, not a trim of waste.

The node's **model choice wins** over the chatbot-level one for the turns it handles. Your
chatbot's persona is still the base it layers onto.

### What a button click asks the AI

Wiring a Menu straight into an AI Fallback — "pick a department, then ask about it" — is
the ordinary way to build one of these. But a button reply contains no typed words, so
the question the AI gets is **the label the visitor clicked**. Choosing *Python* asks the
node about "Python".

Two things follow from that. Give options labels that read as a subject on their own
(*Python*, not *Option 1*), and remember the sentence the visitor typed *before* the menu
appeared is not carried forward — that turn was spent showing the menu. If you need their
own words, put an **Ask Input** node in front of the AI Fallback instead.

Menu and Dropdown nodes also have an optional **Store choice in variable** field, the same
one Ask Input has. Fill it in when a later **If/Else** needs to branch on what was picked;
leave it blank when the connectors already say everything.

An option with no connector attached simply re-asks the menu, so check every option is
wired before publishing.

### Two current gaps, stated plainly

- **An AI Fallback node does not use an attached Data Agent.** It answers through the older
  profile path — and on an agent-backed chatbot, which has no datasource of its own, a node
  left on the *datasource* context source cannot answer at all. Point those nodes at a
  **knowledge base** or the **prompt** instead.
- **Webhook Actions do not run when a Data Agent is answering.** Both are covered in
  [§11](#11-workflow-letting-the-assistant-call-your-other-systems).

---

# 11. Workflow: letting the assistant call your other systems

An **Action** is an HTTP call the assistant can make before answering — an order lookup, an
availability check, a ticket creation.

### Ownership vs attachment

An action belongs to **you** and lives in the Actions library. Attaching it to an agent is a
separate thing, and the same action can serve any number of agents. Consequences:

- **Editing an action changes it for every agent using it.** The library shows how many that
  is, and confirms before you deactivate or delete a shared one.
- **The library's active switch is the master switch.** An inactive action can't be
  attached, isn't offered, and stops running everywhere it already is — without being
  detached.
- **An agent's Actions tab can only add and remove.** Create and edit live in the library.
  The one shortcut is *New Action* on that tab, which saves and attaches in a single step.
- **Names are unique per user**, because the name is what the model sees and duplicates
  would make routing ambiguous.

### How a turn with actions works

1. **No active actions attached → nothing happens.** No extra cost for the majority of
   agents.
2. **A routing pass** — one call asking "which action, with which parameters?"
3. **Execute** — parameters are type-checked against your declared schema, the request is
   built and sent.
4. **Answer** — the (bounded) response is handed to the answering call as context.

**One action per turn, no chaining.** That's the accepted trade for not needing three
separate provider implementations of native tool-calling, which would also collide with the
structured output every provider path already uses.

### Placeholders, and one asymmetry that matters

| Where | Allowed | How it's escaped |
|---|---|---|
| URL | Your variables, and parameters | Percent-encoded per value |
| **Headers** | **Your variables only** | Rejected if the rendered value contains a line break |
| Body | Your variables, and parameters | JSON-escaped |

Headers exclude parameters **on purpose**: a parameter value is derived from visitor text,
and such a value must never be able to forge an auth header or split a request. Any parameter
used in a URL or body must be marked **Required**, so a built request can never contain a
hole.

### Egress safety

An action is user-authored outbound HTTP from a server, which is textbook attack surface.
So:

- `https://` only, no credentials in the URL;
- the host is resolved and checked **immediately before** the request, and any private,
  loopback, link-local, reserved, multicast or unspecified address is rejected (this covers
  the cloud metadata endpoint) — checked at save time too, for a readable error;
- **redirects are not followed** — a redirect is the standard way past an IP check;
- per-action timeout, 1–30 seconds; response body capped at 256 KB read and 4,000 characters
  shown to a model.

Header lists are encrypted at rest, because that's where bearer tokens live. They're
decrypted for your own edit form — the encryption protects the database, not you.

### Failure handling

A broken endpoint **degrades the answer instead of breaking the conversation**. Every failure
is logged in detail, described to the model in general terms ("could not retrieve that
information right now"), and recorded on the message as a badge you can see in the history.
Visitors never see endpoint URLs or status text.

### The gap

**Actions do not run when a Data Agent is answering.** The action router is a second model
call that picks a webhook, and a Data Agent already decides which tool to call for itself.
Running both would put two independent routers in charge of one turn. Actions on an
agent-backed chatbot are a follow-up.

---

# 12. Workflow: when the answer is too big to print

**A query is never cut short.** Ask a tool a question about 5,275 records and it reads all
5,275 — there is no row ceiling anywhere in the platform, and the `LIMIT` you write in your own
SQL is the only thing that bounds a result. This changed: tools used to stop at 200 rows, which
meant a total over a big table was quietly a total over part of it.

What *is* limited is how much of that can be shown, and two numbers govern it:

- **200 rows** is what the assistant is handed to reason over — count, compare, aggregate.
  A chat message has a size limit; the data does not.
- **100 rows** is what may go into a chat bubble.

Because every row was read, the assistant is always told the **exact total** alongside the
sample — "200 of 5,275", never "200, and there may be more". Past 100 matching records it does
three things: shows the first 100, states the total, and offers to send the whole set as a
file.

```
"There are 2,921 records. Do you want me to create a downloadable CSV file
 containing the list of all the records."
```

That sentence is produced by the platform and the model is told to **repeat it word for
word**. Two reasons that are really one: it contains the record count, and a model rewording
it is how a user gets told the wrong number. And it asks a plain yes/no question, which is
what makes a bare "yes" on the next turn something the application can act on.

### What happens when you say yes

A background job reads the records **fifty at a time**, writes one part file per batch,
merges the parts into one file, and hands you a link. In **CSV, Excel (.xlsx) or Parquet**.

While that's happening you see a card under the reply — not a link in a sentence:

```
┌──────────────────────────────────────────────┐   ┌──────────────────────────────────────┐
│ 📄 CSV file                                  │   │ 📄 project_details_2026-08-07.csv    │
│ Reading the next batch…  2,100 of 2,921      │ → │ 2,921 records  ·  44.6 KB            │
│ ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓░░░░░░░  72%              │   │ [ ⬇  Download CSV ]                  │
└──────────────────────────────────────────────┘   └──────────────────────────────────────┘
```

The wording rotates so a long build doesn't read as a stuck one; **the numbers don't** —
they're exactly the records written so far. The bar is capped at 99% until the file actually
exists, because a full bar next to "still working" is the one thing a progress bar must never
say.

**You can keep asking questions the whole time.** The turn ended when the reply arrived; the
build is a background job, not a turn.

### The honest parts

- **Files expire after 30 minutes.** Short on purpose: asking again is cheap, and a server
  keeping every export anybody ever requested is an archive nobody asked for. A visitor who
  comes back an hour later is told the file expired and that they can ask again — not that
  it couldn't be found, which reads like the platform lost it.
- **A batch that fails is retried up to three times**, deleting its part file first so an
  attempt is an attempt rather than an edit. Retries happen because a batch reads from
  *your* database over a connection the platform doesn't control — dropped connections and
  lock timeouts are transient. A query that no longer validates is not retried, because
  three attempts at a permanent failure is three times the wait for the same answer.
- **If it gives up, it gives up out loud.** No partial file, no "here are the first 2,000
  records." An export that silently contains *some* of the data is the one outcome worse
  than no export, because nothing about the file says so.
- **The ceiling is 500,000 records.** Past that you're refused **up front**, naming the
  limit, with no offer made at all — offering a file and then withdrawing it is worse than
  saying no.
- **Relational data sources only.** A tool reading Mongo or a file is refused with a
  sentence the assistant relays.

Every count a user is ever shown is **exact**.

---

# 13. Workflow: filters and totals over an entire result set

### The problem it solves

You ask an assistant for the Python department's March revenue and it says:

> I'm unable to filter the data by month because the available tool does not accept a date
> parameter.

That's true of the tool and **not true of what the assistant can do**. Every record the
tool returns can be read and then narrowed — the tool needs no parameter for it. Switch
this on and the same question gets an answer: the records are read, the ones that aren't
March or aren't Python are dropped, and what's left is either shown to you or totalled.

Conditions available: `=` `≠` `<` `≤` `>` `≥`, one of a list, not one of a list, contains,
starts with, between (both ends included), is empty, is not empty. Several conditions all
have to hold — for "March **or** April", the assistant uses one *one-of* condition rather
than two.

**Months and years work without you doing anything.** Say "in March" or "in 2026" and the
assistant compares that part of the date. It deliberately can't write date ranges itself:
`>= 1 March and < 30 February` is the mistake that produces, and a slightly-too-small
result still looks like an answer.

Ask for a total and you get numbers. Ask to *see* the matching records — "show me", "list",
"which ones" — and you get the records, with the exact count beside them: **200 of 4,317**,
never 200 presented as all of them.

### Read this first: for grouping alone, it's usually the wrong tool

A normal Tool Config already pushes `count`, `sum`, `avg`, `min`, `max` and `GROUP BY` into
the database, which does the grouping over its own indexes, in one pass, exactly, with no
ceiling. **For those cases, whole-result grouping is a slower reimplementation of `GROUP BY`
and you shouldn't use it.**

It earns its keep in three situations:

- **Re-grouping an approved SQL statement** along a different axis, where the statement is
  fixed and the question that arrived is about something else;
- **Deriving a measure the statement didn't produce**, without editing an approved
  statement;
- **A datasource you can read but not tune** — no index, no permission to add one, and a
  server-side `GROUP BY` that times out.

Filtering shifts that balance a little. "Narrow the rows and total them" is still a `WHERE`
clause the database would do better — *if you can change the query*. The three situations
above are exactly the ones where you can't, and there a condition the tool takes no
parameter for is the difference between an answer and an apology. **If you can add the
filter to the tool's own query, do that instead.**

It's **off by default** and switched on **per source**, because ticking it is a judgement
about *this result set* — "reading every record this returns is acceptable" — not about the
agent:

| Source | Where to tick it |
|---|---|
| A **Tool Config** | *Allow whole-result grouping* on the tool form |
| A **Pipelines** graph | *Let an agent read and filter its whole result* in the graph's **Edit** dialog |

A graph is worth a second thought before ticking: a tool is one query, but a graph can be a
loop over eighty-two departments, so you're agreeing to run all of it and hold the result.
A graph that **stops to ask a question** can't be read this way at all — the assistant is
told to call that graph directly and answer it instead.

**How to tell whether it's on.** The agent's test console marks each source
**Filterable** when it is, and the *How this agent answers* panel says which of the two
situations you're in. If an agent tells you it can't filter by month, that panel is where
the reason is: an agent with nothing ticked can only work from the rows a tool returns.

**Your tool description works as instructions.** Whatever you write there reaches the
assistant, and once the source is Filterable those instructions become things it can carry
out. A description saying

> Always group by crm_id then by department. Always sum of the total revenue after
> grouping. If the user asks for a specific month, filter the data on created_at.

asked "what was the revenue generated in August", produces exactly that: grouped by those
two columns, revenue summed, filtered to August on that column. Write the rules; the
assistant applies them.

### What it does

Reads every record the source returns, drops the ones that don't match, and folds the rest
in memory — instead of working from the 200 rows the assistant is shown in a chat message.
Up to **200,000 records** and **100,000 groups**, and **every group is reported**.

Every answer says what it was filtered by: *"sum of revenue from 'ledger', where department
== Python and the month of invoice_date == 3"*. That sentence is there so a figure can't be
right about a set you have to guess at — and so that if the assistant narrowed further than
you asked, you can see it.

### Why the answer is exact

This is the interesting part. Batched arithmetic only equals single-pass arithmetic if each
measure can be carried forward correctly, and the thing carried is not always the answer:

```
Averaging averages is wrong:
  batch 1: [10, 20]  →  mean 15
  batch 2: [60]      →  mean 60
  mean of means: (15 + 60) / 2 = 37.5     ✗
  correct answer:  (30 + 60) / (2 + 1) = 30 ✓
```

So `avg` is carried as a **sum and a count** and divided only at the end. Same for `sum`,
which carries a count it appears not to need — because an all-null group is "no revenue
recorded" in SQL and `0` in the folding library, and those are different facts.

`avg` also divides by the non-null count of the averaged column, never the group's record
count. "The average order value across the 40 orders that have one" versus "…across all
100" is a difference that looks entirely reasonable and is wrong.

### What is refused rather than approximated

**Median, percentile, mode and count-distinct.** None of them can be carried forward in a
bounded way — an exact answer needs every value resident at once, at which point the
batching bought nothing. They're refused with a message naming the five that *are*
available.

Two more refusals:

- **Grouping by a decimal column.** Two values that display identically aren't necessarily
  equal, so the groups wouldn't be trustworthy.
- **Averaging what the tool already averaged.** That's mean-of-means arriving through the
  back door, because the first average happened in the database where the counts behind it
  are gone. (In SQL mode this is undetectable — nothing parses your statement. A real
  limitation, stated rather than papered over.)

### Where to use it

Two entry points. The assistant can call it mid-conversation. Or open the **Aggregations**
page, pick an agent and a tool, and type what you want — its own page rather than a panel on
the agent console, because that console asks "what does this agent say" and this one asks
"what is the actual total", and the second is something you want to check against your own
database without spending a chat turn on it.

**Not covered:** non-relational data sources (CSV, Parquet, Mongo), and aggregating across
two data sources.

---

# 14. Workflow: drawing a pipeline

**Pipelines** is a canvas where you compose a pipeline out of boxes and then run it —
whole, or one box at a time — watching the flow, the state and a capped output in a panel
below.

> **There is a Help button** on the Pipelines list *and* on the canvas itself. It opens
> `/graph-designer/help` in a new tab: every box explained once, sixteen worked scenarios
> (a straight line, a value in a statement, a branch, a loop, keeping every pass, the three
> ways to union, a question mid-run, an error path, timers, waits, `{{VARIABLES}}`, an
> email, running and testing, publishing), the limits in one table and every refusal with
> its fix. It is this section, browsable, without leaving the drawing.

### Why it exists

Nesting expresses exactly one idea: *the child's values restrict the parent.* There was no
way to say:

> run this SQL, loop over what it returns, ask me to confirm before the last step, and take
> a different path if nothing matched.

Every one of those is control flow, and control flow is a drawing.

### The boxes

| Box | What it holds |
|---|---|
| **Start** | Nothing — it says where the run begins |
| **SQL** | A statement, its datasource, the tables it reads, declared values |
| **Union** | The same, appended once per pass of a loop and run on the last |
| **Value** | A literal: a flat list, a nested array, or a named object |
| **Tool Config** | An existing tool, run exactly as an assistant would |
| **Human** | A question, and what kind of answer it expects |
| **Branch** | An ordered list of comparisons, plus an `else` path |
| **For each** | Loop over another box's result, with a ceiling |
| **Do until** | Repeat until a condition holds, with a ceiling |
| **Send an email** | A template, a server, who it goes to, and a value for each thing the template asks for |
| **Timer** | Start, pause, resume or stop a stopwatch |
| **Wait** | Pause the run for a number of seconds |
| **Success** | A message; records the run as having worked |
| **Failure** | A message; records the run as having failed |

**Success and Failure can lead on to other boxes.** Leave the `then` dot connected to
nothing and the run stops there, which is the usual case. But reaching an outcome is
generally the moment you have something worth telling somebody, so you can carry on into a
Send an email box, a Tool Config, or anything else — and on the Failure side that is exactly
where an alert belongs: after the box that says what went wrong, not before it.

Whatever follows cannot change the verdict. A run that passed through Failure is reported as
failed even if it goes on to do three more things successfully, so you cannot join Failure to
Success to "recover" — the canvas says so if you try. To recover from a failure, draw the
`error` path out of the box that actually failed instead.

Connect two boxes either by **dragging** from an output dot onto the target, or by
**clicking** the output dot and then clicking the target. Both work, because users arrive
expecting one or the other. While a connector is armed the cursor becomes a crosshair, so
the mode is visible rather than remembered.

### What it refuses, and why each one matters

Every one of these produces a *plausible wrong run* rather than an obvious error, which is
why none is left until execution:

| Refused | Because |
|---|---|
| No start box, or two | A drawing with no reading order; two starts is two graphs |
| Two connectors on one output | The run would take one of them, and which one would be arbitrary |
| A cycle that no loop box sits on | It would run until the engine gave up, reported far from the two connectors that caused it |
| A Value box whose JSON doesn't match its declared kind | An object where a list was promised feeds the next box something it can't use |
| A SQL box with no statement, no datasource, or **no declared tables** | See below |
| A Branch with no conditions, or a duplicate outcome | An unreachable path, or an undefined overlap |
| A loop over something not in the graph, or with no way out | A loop that can't finish |
| A Human box with no question | A run that pauses silently |
| A `{{NAME}}` nothing on that box declares | Nothing could fill it, so it would appear as itself in your statement, question or message |
| A `{{NAME}}` inside quotes in a SQL statement | A value belongs in a declared value, which the database is handed separately and which therefore can't change what your statement does |
| A Timer that stops one nothing can have started | No path reaches it from the Start box, so the drawing can't do what it appears to |
| A Timer stop inside a loop whose start is outside | The first pass would work and every pass after it would find the timer already finished |
| A Wait longer than 900 seconds | Refused rather than shortened, so the drawing never says one thing while the run does another |

Every message names the box **by its label**, because you're looking at a drawing and a
generated id means nothing to you.

**A SQL box must declare its tables.** This isn't bureaucracy: nothing in the platform parses
a raw statement, so the active-table check can only honour the list you recorded. A box with
no declared tables would run with that check silently skipped — which would make a graph a
way **around** the Data Sources switches rather than a way to use them.

**Conditions are compared, never evaluated.** Every operator is a name from a fixed list and
the comparison happens in ordinary code. There's no expression language here and nothing is
ever executed as code, so a graph cannot be used to run arbitrary code even by its own
author. And **zero and false are not empty** — a query returning a count of zero has produced
a real answer, and treating it as empty would send the graph down its nothing-found path
when the thing it found was zero.

### Using one box's answer inside another

Any box can take values from an earlier box and write them into its own text. Open the box,
scroll to **Variables**, press *Add variable*, and:

1. **Name** it — capitals, digits and underscores, e.g. `TABLE`.
2. **From**: *An earlier node's output* (then pick the box, and optionally a **Field** such
   as `rows[0].name`), or *A fixed value*.
3. **If it has no value**: stop the run and say which variable, or use a default you type.

Then write `{{TABLE}}` wherever you want it — in a SQL statement, a Human question, a
Success or Failure message, or a Value box's JSON. The panel tells you which fields on that
particular box accept one.

Two things to know. **There is no expression language** — `{{NAME}}` is replaced with a
value and everything else is text. There are no filters, no `if`, and nothing is ever
executed as code. And **inside a SQL statement a variable may only be a name or a whole
number**, and only where a table or column name goes: put one inside quotes and the save
refuses it, pointing you at a declared value instead. That is not an arbitrary rule — a
declared value is handed to the database separately and cannot change what your statement
does, whereas text pasted into a statement can. Use variables for the *shape* of a query
and declared values for the *contents*.

### Timing part of a run, and saying so in an email

This is the recipe for "how long did it take, and tell me".

1. Drop a **Timer** box where the work begins and leave it on **Start**. Give it a label —
   *Nightly import*, say. That box *is* the timer.
2. Do the work.
3. Drop another **Timer**, set it to **Stop**, and choose *Nightly import* in its **Timer**
   box.
4. Add a **Send an email** box after it. For each thing your template asks for, choose *An
   earlier node's output*, pick the Stop box, and type the **Field**:

| Field | What you get |
|---|---|
| `started_at` | when it began |
| `ended_at` | when it finished |
| `elapsed_human` | `1h 4m 12s` — the one to put in a sentence |
| `elapsed_seconds` | `3852.117`, if you want the number |
| `paused_seconds` | how long it spent paused |

**Pause and Resume** bracket a stretch that should not count — waiting for somebody to
answer a Human box, for instance. Two more Timer boxes pointing at the same Start, one set
to Pause and one to Resume. The paused time is reported separately, so nothing is lost.

Put a Start/Stop pair **inside a loop** and it measures each pass on its own, with
`elapsed_seconds` for the pass you're on and `total_elapsed_seconds` for all of them
together. Anywhere else, starting the same timer twice is refused — it has no sensible
meaning and it is much more likely to be a mistake.

### Making the run wait

A **Wait** box pauses the run for up to **900 seconds** (fifteen minutes), which is useful
when another system needs a moment to catch up.

**A waiting run does not survive a restart.** If the application is redeployed mid-wait, the
run is stopped and does not carry on. That is why the ceiling is fifteen minutes rather than
hours — for anything longer, use an **Integration** with a schedule, which is designed to
survive restarts. A longer wait is refused when you save rather than quietly shortened, so
the drawing never says one thing while the run does another.

### Testing part of one

**Testing a box, testing a group and running the whole graph are the same operation** with a
different scope. That's the guarantee the feature rests on: a box that passes a test is the
box that will run.

Pick some boxes that aren't connected to each other — an ordinary thing to do ("does this
query work, and does that one") — and they're chained in the drawing's own order. A box in
your selection that reads a box *outside* it **fails, naming what's missing**, because a
loop over an absent list would otherwise loop zero times and report success: a green tick on
a test that tested nothing. Boxes you left out are logged as `skipped`, because a box missing
from the log is indistinguishable from one the run never reached.

### Loops refuse rather than truncate

Rows from the first two of three departments are indistinguishable from rows for all three,
and a total taken over them is a plausible number that is wrong. So a loop that hits its
ceiling **stops and names the box**. Ceilings run from 1 to 100,000.

### Four ways to use a published graph

Publish a graph and there are four places it can be used. Publishing plus one of them is
always required — same rule as a conversation flow — so a graph can be parked mid-edit
without being disconnected, and a draft can sit connected while you finish it. Connecting a
draft is refused rather than accepted-and-ignored, because a control that appears to work and
does nothing is worse than one that says no.

| Use it as | Where you set it up | What it gives you |
|---|---|---|
| One **Data Agent's** tool | Pipelines list → **Edit** → *Data agent* | That agent can call the graph mid-conversation |
| Every agent in a **Workspace** | Pipelines list → **Edit** → *Workspace* | A team shelf: any agent in that workspace can call it, including ones you add later |
| A step inside a **Tool Config** | The tool's *Nested Tools* card | The graph runs first and its values filter that tool's query |
| A step inside a **Flow** | A *Run Graph* block on the flow canvas | The graph runs as one step of a scripted conversation |

Both live on the **Edit** button of the row, alongside the graph's name and description —
one dialog, one Save. The *Callable by* column then shows which of the two it is, or says
the graph isn't callable by anybody yet. Leaving both blank is the ordinary state, and a
graph that's callable by nobody is still fully usable from the canvas.

**A graph is attached to one agent or shared with a workspace, never both.** Picking one in
the dialog blanks the other, and submitting both is refused rather than resolved: otherwise
an agent in that workspace would be handed the same tool twice under the same name, and the
assistant would have nothing to choose on. For the same reason, two shared graphs on one
shelf cannot reduce to the same tool name — "Monthly revenue" and "monthly-revenue" both
become `monthly_revenue`, so the second is refused, naming the first.

If a name and an attachment are changed in one Save and the attachment is refused, **nothing
is written** — the rename doesn't go through on its own. A half-applied form is harder to
recover from than a rejected one.

**Sharing with a workspace is the one that saves you work later.** Attaching means
remembering to attach again for every new agent; sharing means the agent you add next month
picks the graph up on its own.

### A Human box, inside somebody's conversation

There's no dock inside somebody's conversation, so a graph with a **Human** box pauses and
the question goes to whoever is talking — **word for word**, never reworded, because a
reworded question asks them something else and makes their answer unmatchable.

What happens next depends on which of the four is running it:

- **an assistant's tool** — the assistant relays the question and calls a companion answer
  tool with what they said;
- **inside a Tool Config** — that tool returns the question instead of rows. Answering it
  finishes the graph *and* the tool, in one step, and returns the rows you originally asked
  for;
- **inside a Flow** — the flow waits. The visitor's next message is the answer, and then the
  conversation carries on from the block after the Run Graph one.

In every case, an answer that doesn't fit the question — "maybe" to a yes/no — is treated as
**ordinary input, not a failure**: the question comes back with a note about what kind of
answer is needed, and the run is still there to answer. Nothing is broken; somebody just has
to answer again.

### Using a graph inside a Tool Config

Pick a graph in the tool's **Nested Tools** card instead of another tool. The graph runs
first, one named value from its result becomes a list, and the tool's query is filtered to
it — exactly what a nested tool does, with a drawing on the far end of it instead of a
single query.

Two things differ from a nested tool, and both are because a graph is not one query:

- **no same-datasource rule.** A graph's boxes each name their own datasource, so there is no
  single one to compare against the tool's. That judgement is yours to make, box by box.
- **you type the value's name.** Nothing knows what a graph's last box returns until it runs,
  so there is no dropdown. A name that matches nothing comes back as "no values", which stops
  the chain the same way an empty result does.

A graph embedded in a tool **cannot be deleted or made a draft** while that's true, and a
graph that reads the very tool embedding it is refused outright — the two would run each
other forever.

### Using a graph inside a Flow

Drop a **Run Graph** block on the flow canvas and pick a published graph. It has two exits:
**done** and **failed**. Draw the *failed* one — without it a graph that could not run ends
the conversation, which is deliberate: a flow carrying on as though a step had succeeded is
how a visitor gets told something untrue.

The block says nothing to the visitor by itself. Give it a variable name and it stores **how
many** rows the graph found, so a later Send Message block can use it ("I found {{ count }}
matching orders") or an If/Else block can branch on it. It stores the count and not the rows
because a variable is text that goes into a message.

Whatever the visitor has already told the flow is passed into the graph, so a graph can filter
on something an earlier **Ask for Input** block collected.

**What the model is told about a result** is the last box that *produced data*, not simply
the last box to run. A graph almost always ends at a Success box whose output is "it worked",
so "the last output" would report a graph that read two hundred rows as having returned
nothing.

**What it does not do:** no scheduling (a run starts because somebody pressed a button or an
agent called it), no parallel branches, no writes to your data, no non-relational data
sources, and no queue — a run is watched live, so there's nothing to gain from making runs
wait behind each other.

---

# 15. Workflow: moving records between systems

**Integrations** is where a workflow reads records out of one system and writes them into
another — on a schedule, unattended, with a record of what happened to every single one.

Everything else in this platform *reads*. Pipelines runs your queries; an assistant
answers questions about your own database. This is the one part that **writes into somebody
else's software**, which is why almost everything about it is arranged around being able to
prove what it did.

### The three tabs

Opening **Integrations** lands you on **Apps**, and the three tabs across the top are three
views of the same feature.

**Apps** is the gallery of systems this platform can talk to — Shopify, Brevo, and a general
"REST API" tile for anything else that speaks JSON. Each tile says what that system can do
(what it can read, what it can write) and how many of your own connections it already has.
**Connect** on a tile asks only for what that system needs: Brevo wants a name and a key,
Shopify wants your shop domain, a REST API wants an address.

**Connections** is the list of systems you can reach: one entry per account, with the address
and the credential. You can have as many as you need of the same kind — three stores and forty
locations is the ordinary case, not an edge case. This is where **Test**, **Edit**, **Revoke**
and **Delete** live.

**Workflows** is the list of drawings. Each one opens on a canvas.

A tile counts your connections in three groups, and the split matters: **working**, **needs
attention**, and **switched off**. A connection whose key you revoked, or whose key the other
system has stopped accepting, still exists — workflows point at it, so deleting it would break
them — but it will not run until you reconnect it, and that is what "needs attention" means.
One you switched off yourself is listed separately and never as a problem, because that was
your decision; a page that nagged about it would teach you to ignore the badge that matters.

Connecting an app **moves no data**. It gives a workflow somewhere to read from and write to;
the workflow is what decides what travels where.

### Setting up a connection

1. **Apps → Connect** on the tile for the system you want, or **Connections → Add
   Connection** if you would rather pick from a list. Give it a name you will recognise on a
   workflow step ("Shopify EU", not "API 2"), then fill in what it asks for. **The questions
   change with the system** — a plain REST API asks for an address, Shopify asks for your shop
   domain instead because it works its own address out from that, and Brevo asks for neither.
2. **Press Test.** This makes one real call and tells you what came back. A connection that
   *saves* is not the same as one that *works* — a key with the wrong permissions, an address
   missing its version number, and a security page answering instead of the data all look
   identical until something is actually sent.
3. **Add its operations.** For a plain REST API you describe what it can do: a name, a method,
   a path, and which fields it accepts and returns. Those field lists are what the mapping grid
   offers you later, so a name typed wrong here is a field that will not appear there. **A
   built-in system such as Shopify already knows its own operations**, so there is nothing to
   fill in — they are on the step's dropdown as soon as the connection is saved.

#### Connecting a Shopify store

Shopify is built in, and it **reads only** — orders, products and customers. It cannot write
back to your store, deliberately: Shopify's write calls give no safe way to tell a request that
failed apart from one that succeeded but timed out on the way back, so a retry could create a
second copy of a real order.

1. In your Shopify admin: **Settings → Apps and sales channels → Develop apps → Create an
   app.** Give it the read permissions you want — orders, products, customers — and install it.
2. Copy the **Admin API access token**. Shopify shows it once.
3. In GetMyStuff: **Connections → Add Connection → Shopify.** Enter your shop domain — the
   `your-store.myshopify.com` one, not your own web address — and paste the token.
4. **Press Test.**

If the token is missing a permission, the test says so in Shopify's own words. That is worth
knowing because Shopify reports a refusal in a way that can otherwise look like a store with
nothing in it — so a sync that reads no records will tell you it was refused, rather than
quietly reporting success.

On a **Read** step you can also set a **Filter**, using Shopify's own search wording — for
example `updated_at:>2026-08-01` to read only what changed since a date, or
`financial_status:paid`. Leave it empty to read everything.

#### Connecting a Brevo account

Brevo (formerly Sendinblue) is built in, and it **reads and writes** — it is the first system
here a workflow can put records *into*. It covers two parts of your Brevo account:

* **Contacts** — read your contacts and contact lists; add or update a contact, and add
  contacts to a list.
* **Shop data** — read your orders, products and product categories; and send any of the
  three into Brevo.

1. In Brevo: **SMTP & API → API keys → Generate a new API key.** Copy it.
2. In GetMyStuff: **Apps → Brevo → Connect.** Give it a name and paste the key. There is
   nothing else to fill in — Brevo is one service at one address for every account.
3. **Press Test.**

One key covers both parts, so you only ever need one Brevo connection.

Every write is **safe to repeat**. Each one is an "add it, or update the one you already
have" — matched on the email address for a contact, and on your own id for an order, product
or category. So running yesterday's sync again updates the same records instead of creating
duplicates, and a write that times out on the way back can be retried without a second copy
appearing.

Things to know when mapping into Brevo:

* **Attributes are your account's own fields** (`FIRSTNAME`, `LASTNAME`, `SMS`, and whatever
  else you have defined). Brevo refuses a field it does not know, so map to fields that exist
  in your account.
* **Adding contacts to a list needs the list's id**, not its name. Read it with the **Contact
  lists** operation.

##### Sending shop data to Brevo

This is what powers Brevo's abandoned-cart emails, product recommendations and revenue
reporting — none of them have anything to work with until your orders and products are in
there.

* **Switch the eCommerce app on in Brevo first.** Until you do, those six operations are
  refused even though your key is perfectly good. Test only checks your contacts access, so
  it can pass while an order sync still cannot run.
* **Send categories first, then products, then orders.** Brevo will not invent a category
  from a product that mentions one, and an order's lines refer to your product ids. Getting
  the order wrong does not produce an error — it produces products filed under nothing.
* **An order needs its lines.** Every line item must carry a product id, a price and a
  quantity, and Brevo rejects the whole order if one line is missing any of them. Put a
  **Validate** step before the write so a bad line is reported as one failed record instead
  of taking the order down with it.
* **Live orders need Backfill turned off.** It defaults to on, which imports the order
  quietly. Leave it on for a one-off history import; set it to false for orders as they
  happen, or nothing in Brevo will react to them.
* **Reading shop data is rationed.** Brevo allows about a hundred of those reads an hour, so
  set **Changed since** on a repeating sync rather than reading the whole catalogue every
  time.

**Sending email is not part of this connection**, on purpose. An email that has gone out cannot
be recalled, so a retry would mean a second copy in somebody's inbox — sending belongs in
**Email**, where it is built to be counted and traced. This connection is for your contact
data.

**Your key is never shown again.** It is encrypted before it is stored, and the edit form comes
back empty with a note saying one is saved — leave it blank and it stays as it is. To remove it,
use **Revoke**, which deletes the credential and leaves the connection, so nothing has to be
rewired when you reconnect.

### Drawing a workflow

Open a workflow and you get a canvas with one **Trigger** on it. Add steps from the panel and
drag from a step's output dot to another step to connect them.

| Step | What it does |
|---|---|
| **Trigger** | Says how the workflow starts: by hand, or on a schedule |
| **Read** | Pulls records out of a connection, a page at a time |
| **Batch** | Loops over those records a batch at a time — the workhorse |
| **Transform** | Maps fields from the record onto the destination's fields |
| **Validate** | Splits a batch into the records that pass your rules and the ones that do not |
| **Filter** | Keeps only the records matching a condition |
| **Branch** | Sends records down different paths depending on a value |
| **Write** | Sends records into a connection |
| **Success / Failure** | How the run ends |

**A workflow that reads and then writes needs a Batch step between them.** Records travel a
batch at a time — a sync of fifty thousand is a hundred passes of five hundred, not fifty
thousand separate steps — and without one the write step gets nothing.

### The mapping grid

On a Write step, you say which field of the incoming record goes into which field of the
destination. Two buttons help:

* **Map matching names** pairs up fields whose names already agree. It is a name comparison and
  the page says so — it is not a suggestion, and a field whose name happens to coincide is still
  worth looking at.
* Fields the destination *requires* are marked. You can save a workflow with one unmapped —
  half-finished is a normal state for something you are still building — but you cannot
  **publish** it, because a scheduled run has nobody to ask.

### Publishing, and why it is a separate button

**Save** stores the drawing. **Publish** freezes a copy of it as the version that actually runs.

That is the most important thing on this page. Editing a published workflow changes *nothing*
about what runs until you publish again — so you can rework a live sync in the middle of the
afternoon without touching the one that fires at 3am. It also means a run from last week still
shows the workflow as it was then, rather than as it is now.

Publishing checks everything Save checks, plus the unmapped-required-field rule. If it refuses,
it names the step and highlights it.

### Running it

**Dry run** is the one to press first. Every request is built and validated, every payload
checked — and **nothing is sent anywhere**. It is how you test a workflow against a real
connection without putting a single record into somebody's production system.

**Run** does it for real, and needs a published version.

Both go through the same queue as a scheduled run, so what you test at 11am takes exactly the
path that fires at 3am.

### Watching a run

The dock below the canvas shows four numbers — **read**, **written**, **failed**, **skipped** —
and repaints as the run moves. Those four are the interesting part of a fifty-thousand-record
sync; the step list below them is the detail.

**A run with any failed or skipped record ends `partial`, not `succeeded`,** and the badge is
amber rather than green. A sync that moved most of the records and quietly dropped some is
exactly the one worth looking at, and calling it a success is how "3 of 50,000 had a bad email
address" stops being noticed.

**Stop** asks the run to stop. It is a request, not an instruction: a step already waiting on
somebody else's server finishes that call first, so it stops at the next record boundary rather
than mid-request. Everything it did up to that point is kept.

**Replay** runs the *same version* again — not the current drawing. A replay of last Tuesday's
failure has to be the thing that failed last Tuesday, or the result answers a different
question.

### Putting it on a schedule

**Schedule** on the canvas. Choose an interval — a minute is the shortest — and switch it on. It
needs a published version.

Three things worth knowing:

* **A missed slot fires once, not repeatedly.** If the app is off for twelve hours, an hourly
  sync fires once when it comes back and then carries on. Twelve catch-up runs would be twelve
  times the API quota for the same data.
* **If the last run is still going**, you choose what happens: skip this slot (the default),
  queue it up to three deep, or stop the running one.
* **A skipped slot writes a run saying why.** It is the only way you find out that your
  five-minute sync takes seven minutes — doing nothing quietly looks exactly like a schedule
  that is working.

The schedule survives a restart. It lives on the row, not in memory, so the next run lands when
it was due rather than an interval after the app came back.

### Describing one instead of drawing it

**Describe it** on the workflows page takes a sentence — "copy new orders from Shopify EU into
Acme CRM as contacts" — and drafts the workflow.

It can only use the connections you have already added. Ask for something needing one you have
not set up and it declines and tells you what you *do* have, rather than inventing a step
pointed at a system that is not there. Every name it produces is checked against your real
connections, their real operations and those operations' real fields before you see anything.

**What comes back is always a draft.** It arrives switched off and unpublished, it tells you
what it had to assume, and nothing runs until you have opened it, checked every step and
pressed Publish yourself. It also cannot add a Filter, Validate or Branch step — those decide
which records go where, and that is a decision to make on the canvas where you can see both
paths.

### What it does not do yet

Generic REST APIs with an API key, in this release. Shopify, GoHighLevel and SAP — and with
them "sign in with", incoming webhooks and cron expressions — are the next phases. There is no
automatic resume after a crash either: a run whose worker stopped is marked failed with a
sentence saying so, and you press Replay. Half-resuming a write into a CRM is worse than a
clear failure with a button next to it.

# 16. Workflow: telling someone when something happens

Everything up to here ends on a screen. **Email** is how a result leaves the building.

You set up three things once, and then anything can send:

```
SMTP servers   how mail physically leaves — one per sending identity
Templates      what it says, with {{PLACEHOLDERS}} for the parts that change
Triggers       when, without building a flow at all
```

### The server

Add one per sending identity — a transactional relay for receipts and a separate one for
internal alerts, so a blown quota on one does not take out the other. Port 587 usually wants
STARTTLS; port 465 wants SSL/TLS.

**Press Test.** It connects, signs in and hangs up without sending anything, because
operators press test buttons repeatedly and a probe that emails whoever was in the form is a
test with a side effect. The result is remembered on the row, so when somebody reports "the
email never arrived", the first question — did this ever work — already has an answer.

Passwords are stored encrypted and **never shown again**. Editing a server comes back with
the password box empty; leaving it empty keeps the stored one, and there is a separate tick
to remove it.

If your relay is inside your own network, it needs allow-listing in the environment rather
than in the form — a form field that granted itself permission to reach internal addresses
would be a security hole with a label on it. Your administrator sets
`EMAIL_ALLOWED_PRIVATE_HOSTS` and `EMAIL_ALLOWED_PRIVATE_CIDRS`.

### The template

Write the subject and body, and declare every `{{PLACEHOLDER}}` you use as a variable. The
declaration is what makes the placeholder fillable: every node, trigger and webhook that
uses the template gets one field per declared variable.

Mark a variable **required** if the email is wrong without it — the send is refused rather
than going out with a gap. Give it a **default** if it is naturally sometimes absent, and
that default is used instead. That is the one dial worth understanding: it decides, per
variable, whether a missing value stops the email or is quietly filled in.

Values are HTML-escaped in the HTML body, so a customer called `Bob & Sons` arrives intact
rather than breaking the markup. Put any styling in the template itself.

**Editing a template never changes an email already sent.** The log keeps the real text.

### Filling variables in from elsewhere

This is the part worth reading twice. A variable can take its value from:

- **a fixed value** — typed into the node;
- **the Agents section** — a prompt variable you set up under Agents, like `{{COMPANY}}`,
  available in Flow Builder;
- **the conversation** — something a visitor typed into an Ask-for-Input or Menu block,
  available in Flow Builder;
- **an earlier step's output** — in Pipelines and Integrations;
- **the current record** — in Integrations, when sending one email per record;
- **the incoming payload** — for event and webhook triggers.

Not every source is available everywhere, and the ones that are not are **refused by name**
rather than quietly left blank. A graph has no conversation behind it; a chat flow has no
earlier query results. An email addressed to "Dear ," is worse than one not sent, so the
form tells you which sources this place can actually offer.

### Sending from inside something you built

Drop an **Email** block into the Flow Builder, an **Send an email** node into the Graph
Designer, or an **Send an email** step into an Integration. Pick the template, pick the
server, fill in who it goes to, and bind each variable.

Every one of them **queues** the email and carries on — it does not wait for the mail
server, because a slow relay would otherwise hold up your whole flow. Nothing is said to a
chat visitor unless you say it yourself with a Send Message block.

Each has a **failed** exit you can draw an edge from. That covers the things knowable at the
time — no template chosen, a variable that would not resolve. Whether the mail was
*accepted* later is in the delivery log, not on the canvas.

**In an Integration, read the mode carefully.** "One email for the whole batch" is the
default. "One email per record" does what it says, and a batch here is routinely thousands
of rows — so it has a limit, and a batch over that limit **fails the step and sends
nothing** rather than emailing the first fifty. You will be told the numbers.

### Sending without building anything

A **trigger** is a standing instruction. Two kinds:

- **When something happens in the app** — a Pipelines run finishing or failing, an
  integration sync ending. You only ever get events about your own data.
- **When an external system calls in** — you get a private URL and a signing secret. The
  secret is shown **once**; copy it then. Rotating issues a new URL and secret together and
  breaks whatever is using the old pair, which is the point of rotating.

A webhook trigger needs a minimum gap between firings, because the URL is public and without
a floor anyone holding it could make this send mail as fast as they can post.

### The delivery log

Every email this application has queued, with the text that actually went out. A failed one
can be **retried**; one still waiting can be **cancelled**. Open any row to see every
attempt, with the reason and how long it took — which is what answers "why was this an hour
late".

One message you should read carefully if you see it: *"The worker sending this email stopped
responding, so whether it was delivered is unknown."* That means the process died in the
middle of handing the mail over. It may have arrived. Retrying may send it twice. That
choice is deliberately left to you rather than made for you.

# 17. Workflow: seeing the shape of what you built

**Tool Graphs** is a read-only page: a tree of Workspaces → Data Agents → Tools on the left,
a canvas on the right, and a toggle between two drawings.

### Tool Graph — a chain as a diagram

```
START → paid_invoices → active_clients → projects_by_client → END
```

The list view shows a chain as indented text, and text lines can't show the two facts that
matter most when a chain misbehaves:

- **A child embedded in two parents.** The list necessarily repeats it under each one, so
  nothing there says that editing it changes both tools. Here it's **one box with two
  outgoing arrows**.
- **Where a disabled tool sits.** A disabled child is the most common reason a chain returns
  nothing, and in a list it's a word at the end of a line. Here it's **dashed and red**.

Arrows run child → parent, which is the direction values actually travel, and each is
labelled with what crosses it. Selecting an agent draws its own tools *and every tool
below them*, even when a child belongs to a different agent — because that's exactly what
the agent gets at run time.

### SQL Graph — joins as sets

One two-circle Venn diagram per join, with the region the join keeps **filled in**:

| Type | Shaded |
|---|---|
| `INNER JOIN` | The overlap only — rows on both sides |
| `LEFT JOIN` | The left circle, overlap included |
| `RIGHT JOIN` | The right circle, overlap included |
| `FULL OUTER JOIN` | Both circles |

The shading *is* the definition. A dropdown row saying `LEFT JOIN clients` is a much weaker
statement of which rows survive.

**One diagram per join, never a combined three-circle Venn.** A query joining
`orders → clients → regions` is two pairwise conditions applied in sequence; three circles
would imply an `orders ∩ regions` region the query never computes.

**A SQL-mode tool shows no diagram** — just its declared tables and a sentence saying why.
Nothing here parses joins out of a raw statement, and a diagram drawn from a pattern-match
over a statement with a subquery in it would be a confident picture of something nobody
verified. Unlike a wrong number, a wrong picture is not argued with.

**It writes nothing, runs nothing and shows no data.** Tool, table and column names only.
Because the picture is derived every time it's drawn, it cannot fall out of step with your
tools. Selections are kept in the address bar, so a link to one specific chain is something
you can paste into a ticket.

---

# 18. Workflow: watching how it performs

**Chatbot Analytics** reads a record written for every single visitor turn.

| Measured | Meaning |
|---|---|
| Response time | Server-side wall time for the whole turn — flow steps, webhook, model calls, assembly. Excludes network time to the visitor |
| Tokens in / out / total | Summed across **every** model call the turn made |
| Model calls | How many that was |
| Estimated flag | True when the provider reported no usage and counts were derived from text length. Shown as a caveat — never treat those as billing figures |
| Provider and model | Who answered |
| Turn type | Whether a model answered, or the flow answered from its own script |

The same response time is shown to the visitor under the reply bubble, so what a visitor
sees and what you see can never disagree.

**Filters:** last 24 hours, 7 days (default), 30 days or 90 days, and one agent or all. An
unrecognised period is **rejected with a readable message rather than quietly defaulting** —
a filter that silently falls back doesn't fail visibly, it renders real figures for the wrong
scope and nothing on screen says so.

Ranges under two days bucket by hour, longer ones by day. Empty buckets are filled in, so a
quiet day reads as a quiet day rather than disappearing from the axis.

**The page shows:** headline tiles (messages, success rate, average and 95th-percentile
response time, tokens in/out, tokens per message); messages per bucket with failures
overlaid; average response time per bucket; a per-agent table; model and provider spend; the
flow-vs-AI split; and **the ten slowest turns and the ten most recent failures** — the two
lists worth looking at first when something is wrong.

Charts are drawn as plain bars, so the page needs no charting library and renders
identically when only part of it refreshes.

**Ownership is enforced in the query itself**, so no caller can read another account's
traffic by passing the wrong id. And logging is best effort: a visitor who has already been
answered is never shown a failure because the log write failed.

Turns recorded before this feature existed honestly report "no measurement recorded" and
show a dash, rather than a fabricated zero-millisecond answer.

---

# 19. The technology, explained without jargon

You don't need this section to use the product. It's here because the *why* behind several
of the platform's more opinionated behaviours lives in these choices.

### Layers, and why business rules never live in a web page

```
Your browser  →  Routes  →  Services  →  Database helpers  →  Database
                 (HTTP)     (the rules)   (the only place
                                           queries are made)
```

Routes accept a request, validate it, call a service and return a response. **Services hold
every rule** — and never touch HTTP or return HTML. All database access goes through one
shared set of helpers. The upshot for you: a rule is defined once, so the form, the API and
the background job can't disagree about it.

Every feature gets its own folder in every layer it needs, named the same across all of
them. And when two features need the same rule, the rule moves to a shared module both
import — never into one of the two features, because reaching into a sibling couples them
permanently and the coupling is invisible until one changes.

### Pages that update in place

Instead of a heavy front-end framework, the interface asks the server for **small HTML
fragments** and swaps them into the page. Change a dropdown, and the field that depends on
it re-renders — with real options, from the server, not guessed on the client.

Two behaviours you'll notice:

- **Panels close only on their own close button.** A stray backdrop click and the Escape key
  are both inert, everywhere. These panels hold configuration forms, and losing a half-filled
  form to a misclick is not an acceptable failure mode.
- **Errors appear inside the panel you're working in.** A panel covering the viewport has to
  render its own errors — an alert swapped into the page behind it is invisible.

Repeating rows (query columns, joins, action parameters) are submitted as **one structured
field** rather than several parallel lists, so the server has exactly one place to check
their shape. Three parallel lists could arrive at different lengths, and a row would then
pair the wrong column with the wrong tool.

### Why you never see an internal database number

Every record has two identifiers: an internal one used for speed inside the database, and a
public one — a long random-looking id — that is the *only* thing that ever appears in a URL,
a form field or a response. You can't guess another account's record by incrementing a
number, because the number is never on the wire.

### Bound values: why injection isn't possible

When a filter value goes into a query, it is **never pasted into the query text**. The query
and the value travel separately, and the database is told "here is a query with a hole, and
here is a value for the hole." The value can only ever be *data*.

The plumbing analogy: the query is a form with a blank, and the value goes in the blank. It
cannot become part of the form.

That's why a stored filter value of `x' OR 1=1 --` comes back as **zero rows**, and why
`%'; DROP TABLE customers; --` through a `LIKE` filter leaves the table intact. Both are
verified by tests that run on every change.

Table and column *names* can't be handled that way — they're part of the query's structure —
so they take a different route: they're resolved against the **real live tables** before the
query is assembled, so a name that doesn't exist fails cleanly before any database sees it.

**One important warning about the preview.** The generated-SQL preview you read on the form
inlines filter values as text, because it's for reading. It is a display artefact and is
**never executed**. The thing that runs is rebuilt from real column references with bound
values — clause for clause identical, sharing no machinery, so the preview can never become
a code path.

### Reading structure without reading data

When the platform needs to know what tables and columns exist, it asks the database's own
catalogue — the equivalent of reading the table of contents rather than the book. No rows
are read. That's what makes Ask AI's promise — *structure, never contents* — a property of
the design rather than a claim about a prompt.

### Graphs: control flow you can see

Several features are built as **graphs** — boxes with arrows, where each arrow is a decision
about where to go next. Nested tools, downloads, whole-result grouping and Pipelines
all work this way.

Why not just write loops and if-statements? Because the behaviour asked for — *evaluate
inside-out, propagate outward, stop the moment a level produces nothing* — **is** a
control-flow graph, and writing it as one makes the control flow the thing you read rather
than something reconstructed from scattered conditions. It also means every way a process can
end passes through one cleanup step, rather than cleanup having to be right in five places.

### Pausing mid-request, and resuming in a different one

The download offer and a graph's Human box both do something ordinary software finds
awkward: they **stop in the middle**, wait for a person, and carry on later — in a completely
different web request, possibly in a different process.

That works because the run's state is **written to the database** at the pause. Your "yes"
arrives minutes later, a background worker picks the run up where it stopped, and continues.
The half that asks the question never builds anything; the half that builds never asks
anything.

### Reading big results without re-reading them

The obvious way to read half a million records in batches is "give me 50 starting at row
N", repeatedly. That is wrong here, twice over:

- it needs a guaranteed total order or it's simply incorrect — without one, the database may
  hand you a row twice and another never, and a grouped query doesn't always have a unique
  key among its output columns;
- even with an order, the database re-runs and re-sorts the whole result for **every batch**.
  500,000 records is 10,000 batches: ten thousand sorts of half a million rows, to read each
  of them once.

So the platform opens **one cursor** — a live bookmark in the result — and pulls 50 rows at
a time. One pass, one consistent snapshot, every row exactly once, no ordering required.

The cost, stated: that cursor holds a connection and a read transaction for the export's
whole run. That's what the 500,000-record ceiling bounds — an export nobody could finish is
refused up front rather than pinning a connection for an hour.

### Live progress without guessing

Three things stream: the download, the build progress, and the assistant's answer. Each
because the alternative is a silence you can't interpret — an agent turn runs real queries
and can take a minute, and a spinner that says nothing for that long is indistinguishable
from a hang.

Progress is read from **rows the worker wrote**, not from an in-memory notice board. The
worker writing the files and the connection streaming your progress bar are different tasks
— and behind more than one server, different processes. Reading from the database means a
browser that reconnects halfway through sees the whole story rather than half of it.

A **retry appears as its own event**, because "this export is big" and "this export is
struggling" are the only question somebody watching one has.

If the live connection drops, the card **falls back to polling** every few seconds and warns
your console **once** rather than every tick.

### Files, and where they can't go

Generated exports are **not** served from the public static folder, and this is worth being
blunt about because it looks like the obvious simplification. A file placed there is
fetchable by anyone with the URL — no key, no session, no expiry check, because a static
mount bypasses the handler that enforces all three. An export is somebody's business data.
So exports sit behind a handler that can say no, and every request has to satisfy four
conditions: the widget key is active, the conversation in the URL is the one that produced
the file, the export is ready, and its window hasn't closed.

The session identifier in a download path is minted by the browser, so it's never trusted as
a folder name — it's normalised first, and every request re-checks that the file it's about
to serve is still inside the folder the URL named.

### Formatted answers, without opening a hole

The widget renders the assistant's Markdown as real formatting. The safety rule is the whole
design: **the text is escaped before a single formatting pattern is examined.** After that
first step there is no `<` or `>` left in the string. A model that emitted `<script>` is
holding harmless text and will still be holding it when the function returns.

Every tag your visitor sees was written by the renderer, from a fixed set, and **no attribute
is ever built from message text.** The inverse — parse first and sanitise after — produces
byte-identical output for every harmless input and is a security hole in *your* website. No
allowlist bolted on afterwards recovers from that.

### Two layers of input checking

Every form is checked in the browser *and* on the server, and the browser's check is never
trusted. On the server, checking is split deliberately:

- **Shape** — required, length, pattern, type, is-this-a-known-option — is declared once per
  field, with the field's human name in the message.
- **Meaning** — is this name taken, do you own this row, is this datasource reachable, does
  this column exist — is checked where the database is in hand.

You get **the first thing to fix**, not a wall of cascading errors from one missing value.

Two failure modes this replaced are worth naming, because they're the kind of thing that
erodes trust in a tool: `?page=abc` used to fall back to page 1, so a broken link showed the
wrong data silently; and a malformed query payload was once swallowed into an empty one,
discarding the query you'd just built while **reporting success**.

### The schema keeps itself current

The database structure is applied by the application itself at startup. If the structure is
behind the code, it's brought up to date before the first request. If it *can't* be, startup
**stops** — a database nobody can account for should not be served requests.

That replaced an approach that created missing tables but never altered existing ones — so a
newly added column never reached an existing database, the app booted clean, and the failure
surfaced one page at a time as a 500 with nothing at startup hinting the structure was
stale.

### How quality is kept honest

- Nothing that produces or consumes data is faked in the tests. The datasource under a test
  is a real database file and the file writers really write: faking them would prove the code
  calls them, where running them proves an export of 125 records contains 125 records.
- Batch boundaries are asserted by the **set of ids** read back, not by counting — a reader
  that repeated one row and dropped another would pass a length check.
- Aggregation exactness is checked **against the database's own `GROUP BY`** over 12,347
  records with a skewed distribution and a column full of nulls, and at five different
  fan-out widths, because the promise is that the answer matches what the database would have
  said.
- Outbound network access is **blocked** during tests, so a missed mock fails loudly by name
  rather than quietly succeeding against a real service.
- Test coverage is a **ratchet**: it can only go up. A failing run can never lower the bar,
  and a brand-new untested file forces an explicit failure rather than silently leaving the
  percentage unchanged.
- Every run leaves a **timestamped report** — pass/fail counts, coverage, every failure with
  its root cause — and those reports are committed, because a record of what broke and when
  is only useful if it survives.
- **Where automated tests can't reach, the method is recorded.** There's no JavaScript test
  harness here, so both drawing canvases and the download card were driven in a real headless
  browser — clicked, dragged, saved, run. That found two real bugs every automated test
  passed straight through, including a download link that had to be **clicked** to reveal it
  was broken: checking the link's target proved the route worked and said nothing about
  whether the page could reach it.

---

# 20. Choosing a language model

Three options, chosen per agent and per Ask AI request:

| Option | What it means |
|---|---|
| **Any active key** | Whichever of your AI Settings keys is active, in provider order. This is the default, and it's what chatbots did before pinning existed |
| **One pinned key** | That specific key. If you delete it, the agent degrades to "any active key" rather than erroring mid-chat |
| **In-built** | A model running locally on your own server. No credential, nothing leaves the box |

### The in-built option, honestly

The local models are genuinely useful for some jobs and genuinely too slow for others, and
the difference has been measured rather than guessed. On a six-core CPU-only machine:

| Job | Model | Measured |
|---|---|---|
| Knowledge-base answers, chatbot replies | Small (~1.7B) | **~17 seconds** per grounded answer over 8 chunks |
| Embeddings for a knowledge base | A dedicated embedding model | Fast |
| **Data agents** (tool calling) | Larger (~8B) | **242 s warm, 417 s cold** per turn |

That last row is the operative fact. The larger model **works** — verified end to end,
routing to the right tool, reporting the right figures, even relaying a tool's fixed filter
unprompted. It is simply minutes per turn, where a hosted provider does the same turn in
seconds.

So: **on CPU-only hardware, in-built data agents are a test-console feature. Use an API key
for live widgets.** The visitor budget is deliberately *not* widened for the local model — an
agent too slow to answer inside 120 seconds degrades to the older profile reply, which serves
a visitor better than a seven-minute spinner.

### Two things the platform refuses to do with a local model

- **Small models are refused for data agents**, not attempted. A data agent depends entirely
  on the model choosing to call a tool, and a model too small for that doesn't error — it
  answers **confidently with no tool call behind it**, which is precisely the invented-figure
  failure this whole product exists to prevent. It's a *deny*list, not an allowlist: if you've
  pulled a model nobody has heard of, you can try it.
- **Prompt sizes are floored**, because the local server truncates rather than erroring. A
  truncated tool *result* is a wrong answer; a truncated tool *call* is malformed and looks
  like a broken call rather than a cut-off answer. Both floors are raised specifically for
  data agents.

Temperature is zero everywhere, so the same question can't route to a different tool on a
retry.

### Retries live in the right place

When a provider is busy, the **individual HTTP call** is retried — up to four attempts,
raised from the SDKs' default of two, because that default is sized for a provider that rate
limits *per key* and isn't enough for a gateway that queues under load and drains in
seconds.

Retrying the whole agent turn instead would **re-run every query that had already
succeeded**, for a failure that happened after them. Retrying one call retries one call. The
overall turn timeout is unchanged and is still the outer bound: a turn that spends its whole
budget queueing ends with "took too long", not by hanging.

### The assistant's own description of its tools

Each Data Agent has two separate sets of instructions: **yours**, and a **generated
description of its tools**. They never mix in storage, because a single field would have two
writers racing — you'd clobber a regenerated block, or a regeneration would overwrite words
you wrote.

The generated half is composed by ordinary code, with **no model call**. Four reasons: it
cannot describe a tool the agent doesn't have (the list *is* the tool list); it's reproducible
byte for byte, so behaviour doesn't drift between two saves; it's free, so it can be
regenerated on every tool change; and nothing leaves the box to produce it.

Per tool it states the purpose, the datasource and tables, the exact field names in the
result, the grouping, and any fixed filter — **with a note that it cannot be widened**. Then
the standing rules. An agent with **no** enabled tools gets an explicit "you have no data
tools" instruction, so it refuses rather than answering from the model's own general
knowledge — which would look like a working answer and be entirely invented.

Regeneration happens in the background after every tool change, and **correctness does not
depend on it succeeding**: every answer checks whether the description is behind and rebuilds
it inline if so. A failed background task, a restart mid-flight, or a task that never ran
costs one extra write on the next answer and is never wrong.

---

# 21. How your data is kept safe

A consolidated view of what's already been mentioned in passing.

| Concern | What's done |
|---|---|
| **The AI writing a destructive query** | It can't write a query at all. Everything it can run was written or approved by a person, and re-checked as read-only on every single run |
| **Injection through a filter value** | Values are bound, never pasted. Verified by tests using real attack strings |
| **Injection through a table or column name** | Names are resolved against the live database before the query is built, and validated against a strict character set — spelled out explicitly rather than using a shorthand that would let through look-alike Unicode characters |
| **Reading something nobody should read** | Per-table and per-column switches, re-checked on **every run** — not just when the tool was saved |
| **Credentials at rest** | Datasource passwords and Action headers are encrypted |
| **API keys on screen** | Never shown after saving. A masked form only, and a test walks the response fields so a future change can't accidentally start exposing one |
| **Outbound calls from your server** | HTTPS only, no private or metadata addresses, no redirects followed, host re-checked immediately before the request, bounded timeout and response size |
| **Cross-site scripting in your website** | The assistant's text is escaped *before* any formatting is parsed; no attribute is ever built from message text; links and images are deliberately unsupported |
| **One visitor reading another's export** | A download needs the widget key **and** the conversation's own token. A key identifies a public website, not a person |
| **Guessing record ids** | Public identifiers are random, not sequential |
| **Exports sitting around** | 30-minute window, enforced on every request *and* swept by a background cleaner that deletes the bytes |
| **Leaking whether a record exists** | Someone else's record, an unknown id and a missing file all return the same "not found" sentence — distinguishing them would confirm which ids are real |
| **Stack traces reaching users** | Never. Users get a sentence naming what to fix; the technical detail goes to the log |
| **A guessable signing key** | The application refuses to start if its token-signing secret isn't set |

One limitation stated rather than hidden: checking a hostname immediately before making a
request narrows but does not fully close the window where DNS could change underneath it.
Closing it entirely needs a capability the HTTP library doesn't expose.

---

# 22. The house rules — why refusals happen

Six principles show up everywhere. Once you recognise them, the platform's refusals stop
feeling arbitrary.

### 1. A plausible wrong number is worse than an honest failure

This is the big one, and almost every limit in the product follows from it.

Rows from the first fifty of eighty departments are **indistinguishable** from rows for all
eighty, and a total taken over them is a number that looks entirely reasonable and is wrong.
So limits **refuse rather than truncate** — the run stops and names what to narrow.

The strongest version of that rule is having no limit at all, and it is why the row caps were
removed: a query that stopped at 200 rows produced exactly this kind of number, and no wording
could fix it while the other rows were never read.

A dropped filter widens the result set. A dropped grouping changes what each row counts.
Either way the query still comes back with a number the assistant would state as fact. A tool
that says "I need reconfiguring" is recoverable; a plausible wrong figure is not.

### 2. Describe the result you actually have

Asked for *"the list of projects in a department"*, an assistant whose tool filtered on
nothing called it anyway and headed the result **"Projects in the department"**. Every row was
real. The query ran correctly. And the reply was false: the reader was told they were looking
at one department when they were looking at all of them, and nothing in the answer gave them
any way to notice.

A wrong number at least looks like a number somebody could check. So the rule is: **describe
the result you have, name the narrowing you could not apply, then show the rows** — in one
reply.

### 3. Fail while the form is still open

A grouping MySQL will refuse, a join onto a table you didn't select, a value declared but
never used in the statement, a nested child that needs an argument nothing can supply —
every one of those is caught **at save time**, where you're standing in front of the form,
rather than mid-conversation with a visitor months later.

Where a check honestly *can't* be made in advance — SQL syntax, because dialects differ and a
parser strict enough to trust would reject valid queries — you get the **Test Query** button
instead, and it reports what the database said.

### 4. Name the thing that's wrong

"Not available" is not actionable. Every message names the table, the column, the tool, the
box's label — whatever the thing you have to go and change is.

And the message travels with the rule rather than being written twice. When the same rule is
enforced in two places that owe different audiences different exception types, the *sentence*
lives in one place, so a reword lands in both at once.

### 5. Two audiences, one failure

When a tool fails, two different sentences are produced from the same fault:

- **To the assistant**: the fault *plus* "tell the user the tool needs reconfiguring" —
  because a model handed a bare fault tries to work around it, and inventing a remedy it
  can't have is worse than relaying the problem.
- **To you, in the Test Query panel**: the fault alone, without the advice. You're the person
  somebody would be told to tell.

The same split governs raw database errors. They **never** reach a prompt — they can name
schema objects and echo values — so the assistant gets "the query could not be run against
the database". But the test button shows the driver's own words untouched, because *"Unknown
column 'crm_id' in 'on clause'"* is the only sentence that says where to look.

### 6. Never ask the visitor to narrow something they can't

A tool takes no arguments unless you opened one. So an assistant that responds to a refusal by
asking the visitor to "specify a date range" sends the conversation into a loop: every
rephrasing routes to the same tool, hits the same refusal, and returns the same sentence.

Observed live: *"latest projects"*, *"August"*, *"August 2026"* — three identical refusals,
with the tool never running once. The advice now says the tool needs reconfiguring by
whoever set it up, and explicitly forbids asking the visitor to narrow anything.

There's a companion rule from the other direction: **a failing tool must not silence an
assistant that has a working one.** The model is told that a tool failure means *that tool*
couldn't run, not that the data is unreachable, and to try one alternative before giving up.
Without it, one agent answered "I cannot provide a list of projects" while a perfectly good
projects tool sat beside it, enabled, reading the same table, and working.

---

# 23. Every limit, in one place

### Tool Configs

| | |
|---|---|
| Tables per tool | 25 |
| Columns / aggregations / group-by / filters | 200 / 50 / 50 / 50 |
| Filter value | 500 characters |
| Description | 2,000 characters |
| SQL statement | 8,000 characters |
| Assistant-supplied values (SQL mode) | 5 |
| Joins per query | 10 |
| Nested children per tool | 5 |
| Chain depth / total tools in a chain | 5 / 20 |
| Values crossing one chain level | no limit |
| Iterations of a "run once per value" parent | 50 (refused, not truncated) |

### What reaches an answer

| | |
|---|---|
| Rows a query returns | no limit — every matching row |
| Rows shown to the model | 200, always with the exact total beside them |
| Rows the model may print | 100, plus the exact total and a download offer |
| Tool calls in one turn | Roughly a dozen |
| Visitor turn budget | 120 seconds |
| Test console budget | 900 seconds |

### Downloads

| | |
|---|---|
| Records in one file | 500,000 |
| Records per batch | 50 |
| Attempts per batch | 3 |
| File lifetime | 30 minutes |
| Formats | CSV, Excel (.xlsx), Parquet |

### Whole-result grouping

| | |
|---|---|
| Records one run may read | 200,000 |
| Groups the running total may hold | 100,000 |
| Result rows | every group (sorted, so the order is repeatable) |

### Integrations

| | |
|---|---|
| Workflow steps | **No limit** — what bounds a run is the batch ceiling below |
| Records per batch | 1 to 5,000 (a whole batch is held in memory at once) |
| Batches one run may make | 1,000 by default, 100,000 at most |
| Shortest schedule | every 60 seconds |
| Runs queued behind a running one | 3, then the slot is skipped and says so |
| Step rows kept per step | 500, then they collapse into one row saying how many passes |
| Failed records logged / examples kept | 1,000 / 20 — the counters stay exact past both |
| Connections per system | **No limit** — several accounts of the same kind is the point |
| Connections to the *same* account | 1 — reconnecting a store updates it rather than duplicating it |
| Fields one operation may declare | 100 each way |
| Shopify records per request | 250, Shopify's own maximum |
| Shopify operations | 3, all read-only: orders, products, customers |
| Brevo operations | 4: contacts and contact lists (read), create-or-update contact and add-to-list (write) |
| Brevo records per request | 500 contacts, 50 lists — each endpoint's own maximum |
| Private-address allow-list | 10 hosts and 10 ranges, no wildcards, administrators only |
| A drafted workflow | 12 steps, 20 mappings per step, 5 assumptions |

### Chatbots, flows and actions

| | |
|---|---|
| System prompt | 20,000 characters |
| Prompt variables / value length | 30 / 500 characters |
| Visitor message | 4,000 characters |
| Actions per user | 30 |
| Action timeout | 1–30 seconds |
| Action response read / shown to a model | 256 KB / 4,000 characters |
| Actions per turn | 1, no chaining |
| Flow nodes / connectors | 500 / 2,000 |
| Pipelines boxes | **No limit** — what bounds a run is the per-loop ceiling |
| Loop ceiling | 1 to 100,000 |
| Variables declared per graph box | 30 |
| Wait box | 1 to 900 seconds (15 minutes) — and a waiting run does not survive a restart |

### Email

| | |
|---|---|
| Variables declared per template | 30 |
| Length of one substituted value | 500 characters (trimmed with `…`, not refused) |
| Variable name | Capitals, numbers and underscores — `{{company}}` and `{{COMPANY}}` are the same variable |
| Recipients per email (To + Cc + Bcc) | 50 |
| Subject | 998 characters |
| Body | 200,000 characters |
| Send attempts before giving up | 5, backing off 30s → 2m → 8m → 32m |
| Emails from one integration batch | 50 by default, 500 maximum (over the limit **fails the step and sends nothing**) |
| Webhook request body | 64 kB |
| Webhook replay window | 5 minutes either side of your clock |
| Minimum gap between webhook firings | 1 second floor; 60 seconds by default |

### Ask AI

| | |
|---|---|
| Tables per request | 25 (over the cap is refused, not trimmed) |
| Prompt | 2,000 characters |
| Conversation turns remembered | 6 |

---

# 24. Troubleshooting: what a message means

### Email

| Message | What it means |
|---|---|
| *"{{CUSTOMER}} is required by this template but has no value."* | The template marks that variable required and nothing bound it. Bind it, or give it a default so it can fill itself in. |
| *"{{X}} is bound to 'session', which is not available here."* | That source does not exist on this canvas — a graph has no conversation, a chat flow has no earlier query results. Pick a source the form offers. |
| *"{{X}} is bound to a node that did not produce anything on this path."* | The step it points at was deleted, or a branch skipped it. Re-point the binding. |
| *"This template uses {{GHOST}} but no matching variable is declared."* | Add it under Variables, or take it out of the template. |
| *"The value for {{X}} contains a line break, which is not allowed in the subject."* | A subject is a single line. Something upstream put a newline in the value. |
| *"'x' does not look like an email address."* | Checked deliberately strictly, so a bad address is refused while you are looking at the form rather than bouncing silently later. |
| *"This application will not connect to smtp.internal."* | The server is on a private address, which needs allow-listing in the environment by your administrator. |
| *"A password cannot be sent over an unencrypted connection."* | Choose STARTTLS or SSL/TLS, or clear the credentials if the relay authenticates by address. |
| *"smtp.example.com rejected the username and password."* | Fix the credentials on the server's settings page, then press Retry on the email. |
| *"smtp.example.com refused every recipient address."* | The addresses are wrong or the relay will not send to them. Not retried automatically — no later attempt would work either. |
| *"…did not respond within 30 seconds. It will be tried again."* | A slow or greylisting relay. It retries on its own. |
| *"The worker sending this email stopped responding, so whether it was delivered is unknown."* | The process died mid-handover. It may have arrived. Retrying may send it twice — which is why that choice is left to you. |
| *"This step would send 4,000 emails and its limit is 50."* | An integration set to one-email-per-record over a large batch. Nothing was sent. Raise the limit deliberately, or filter the records first. |
| *"This email would go to 60 addresses, and the limit is 50."* | A template is not a mailing list. Send to a distribution address instead. |
| *"'Alerts' cannot be deleted because 2 triggers send through it."* | Delete or re-point those triggers first. Asked up front so the database does not refuse it less helpfully. |
| *"This email is being sent right now and can no longer be stopped."* | Cancel only works while it is still queued. It may already have arrived. |
| **HTTP 401 from a webhook** | The signature did not match, or the timestamp header was missing. |
| **HTTP 429 from a webhook** | Called again inside the minimum gap. `Retry-After` says how long to wait. |
| **HTTP 422 from a webhook** | The call was authentic, but its payload does not carry a field a binding needs. |

### Saving a Tool Config


| Message | What to do |
|---|---|
| *Agent 'X' already has a tool named 'y'* | Names are unique per agent, case-insensitively. Rename, or put it on another agent |
| *Select at least one table for this tool to read* | Tables is required in both modes |
| *Column 'x' is selected but not grouped* | Group it, aggregate it (`COUNT`/`SUM`/`AVG`/`MIN`/`MAX`), or remove it from Columns. It's refused rather than fixed, because grouping by it too is a *different* query and only you know which you meant |
| *This query groups rows but selects every column* | Name the grouped columns and the aggregations you want. "Every column, grouped" is the same violation written shorter |
| *The query joins 'x', which is not one of the tables selected* | Add it to Tables, or remove the join |
| *Every filter needs a value to compare against, or must be marked as supplied by the agent* | Fill the value, or tick **Agent fills in** |
| *The SQL query does not use ':x' anywhere* | Write the placeholder into the statement, or drop the declared value — and **check it isn't inside quotes**, where it's text rather than a placeholder |
| *The SQL query uses ':x', which nothing fills* | The other direction: declare it as an assistant-supplied value, embed a nested tool that fills it, or take it out |
| *'y' needs 'x' to be supplied by the assistant, and an embedded tool is never called by the assistant* | Make the child's value optional, or give the inner query a fixed one. **The assistant calls the parent, not the child** |
| *The SQL query contains more than one statement* / *is not a read* | One read-only statement. The assistant can read data, never change it |
| *'X' is not a relational datasource, so it cannot run a SQL query* | Use the query builder, or pick PostgreSQL / MySQL / SQLite |
| *Every table in this datasource is switched off* | Switch tables back on in Data Sources |
| *…cannot be disabled / cannot be deleted* | Another tool embeds it; the message names which |

**Syntax errors and unknown columns are deliberately not in this list.** No checker here is
strict enough to be trusted with them. **Test Query** is how you find those, and it reports
what the database said.

### Running a tool

| Message | Meaning |
|---|---|
| *Column 'orders.total' is inactive in this datasource* | Switched off in Data Sources. Switch it back on, or edit the tool to stop reading it |
| *The query could not be run against the database* | A driver error. The detail is in your log; press **Test Query** on that tool to see the database's own words |
| *This query runs once per value and there are more than 50* | Rewrite the parent as a `JOIN` in SQL mode — one statement instead of fifty. Check the chain is doing anything at all first: a child reading the same table as its parent, key against key, is a tautology and the link can simply be removed |
| *A right join cannot be run in builder mode* | The query builder can't express one. Right joins stay authorable and previewable, and a SQL-mode statement may right-join freely. Substituting a left or full outer join would quietly change which rows come back |

### Downloads

| What happened | What you're told |
|---|---|
| Bigger than the ceiling | *"There are 1,200,000 records, which is more than the 500,000 this application can put into one file. Please narrow the question down and ask again."* |
| Three failed attempts, or a failed merge | *"The file cannot be created at the moment. Please try again."* The real reason is in your log |
| Expired | *"That download has expired. Please ask for it again."* — the same sentence whether the clock merely passed or the cleaner already swept the file |
| Not yours, unknown, or missing | *"That download could not be found."* — one sentence for all cases |

### The widget looks fine but doesn't work

Check the **browser console** — that's where the operator's half of every message goes.
Three common causes, all of which used to produce an identical healthy-looking widget:

1. Your page's domain isn't on the chatbot's **Allowed Domains** list.
2. Your page is HTTPS and `apiBase` is `http://`. The browser blocks that before sending, so
   the server logs nothing. The widget detects this exact combination and names both fixes.
3. `apiBase` has a trailing slash — harmless now (it's stripped), but it used to produce a
   404 resembling no particular configuration mistake.

**Simplest fix for a same-origin embed: omit `apiBase` entirely.** Only `apiKey` is required.

### An answer that should have figures has none

Open the agent's **test console** and ask the same question. Check the **tools called** list:

- **Empty list, confident answer** → a bug, and exactly the one this design exists to
  prevent. If you're on the in-built model, check it isn't one refused for unreliable tool
  calling.
- **Empty list, refusal** → no tool covers the question. Build one, or open one filter with
  **Agent fills in**.
- **A tool called, and it failed** → the console shows why. Fix the tool.
- **A tool called, right answer** → the problem is in the widget, not the agent. See above.

---

# 25. Glossary

| Term | Meaning |
|---|---|
| **Action** | An outbound HTTP call the assistant may make mid-conversation |
| **Agent** | A publishable chat widget with a key and appearance settings |
| **Agent fills in** | A filter whose *value* the AI supplies per call. Column and operator stay yours |
| **AI Fallback** | A flow box that hands the turn to an AI, optionally grounded in its own knowledge base |
| **Aggregation** | `COUNT`, `SUM`, `AVG`, `MIN`, `MAX` |
| **Allowed Domains** | The websites permitted to embed a given widget |
| **Bound value** | A value sent to the database separately from the query, so it can only ever be data |
| **Builder mode** | A query written with dropdowns |
| **Chain** | A tool plus the tools it embeds |
| **Data Agent** | One assistant with one job, holding a set of tools |
| **Data Source** | A connection to one database, or a set of uploaded files |
| **Display limit** | 100 — the rows the assistant may print in one answer |
| **Flow** | A drawn conversation script |
| **Graph** | Boxes and arrows where each arrow is a decision about where to go next |
| **Grounding rules** | The standing instructions appended after your prompt, which no persona can override |
| **Match any** | A nested link that builds one `IN (…)`. The parent runs once |
| **Nested tool** | A tool embedded in another as a sub-query. Still callable on its own |
| **Primary table** | The **first** table you select. The base a built query's joins hang off, and what a bare column name means |
| **Public identifier** | The random-looking id that appears in URLs. Internal numbers never leave the server |
| **Reflection** | Reading a database's structure from its own catalogue, without reading any rows |
| **Routing** | The AI deciding *which* tool to call. Names and descriptions are its only basis |
| **Run once per value** | A nested link that runs the parent once per value and concatenates the rows |
| **SQL mode** | A query written as a statement |
| **Test Query** | Runs an unsaved query once, reads one row, reports what happened |
| **Tool Config** | One question an assistant can answer, expressed as one query |
| **Whole-result grouping** | Off by default. Reads every record a tool returns and folds it in memory |
| **Workspace** | An optional folder for Data Agents |

---

# 26. Where to read more

**The engineering companion to this guide:**
[ENGINEERING_TECHNOLOGY.md](ENGINEERING_TECHNOLOGY.md) — the same system explained for somebody
who has to change the code. The dependency choices and what forced them, the module topology,
the four LangGraph runtimes and how they differ, the security argument behind query execution,
the concurrency model, a checklist of the invariants the codebase maintains, every alternative
that was tried and rejected, and the known gaps.

Each of the following goes deeper on one thing, with the reasoning and the measurements behind it.

**The core loop**
- [DEEP_AGENTS.md](DEEP_AGENTS.md) — how a saved tool becomes something a chatbot calls
- [TOOL_CONFIGS.md](TOOL_CONFIGS.md) — the practical how-to, one worked example per scenario
  (also in-app at the **Help** button)
- [TOOL_QUERY_MODES.md](TOOL_QUERY_MODES.md) — the two modes, and the shared read-only guard
- [QUERY_JOINS.md](QUERY_JOINS.md) — the join rules
- [TOOL_CHAINING.md](TOOL_CHAINING.md) / [TOOL_CHAIN_ITERATION.md](TOOL_CHAIN_ITERATION.md) —
  nesting, and the shapes a value can take
- [QUERY_TEST.md](QUERY_TEST.md) — the Test Query button

**Drafting and drawing**
- [SQL_ASSIST.md](SQL_ASSIST.md) — Ask AI, and Auto Create Tool
- [GRAPH_DESIGNER.md](GRAPH_DESIGNER.md) — the writable canvas
- [TOOL_GRAPHS.md](TOOL_GRAPHS.md) — the read-only diagrams

**Moving records between systems**
- [INTEGRATIONS.md](INTEGRATIONS.md) — the engine behind chapter 15: the frozen version, the
  queue, the scheduler, and why a batch is the unit of work
- [SHOPIFY_CONNECTOR.md](SHOPIFY_CONNECTOR.md) — how the Shopify connection works, why it
  reads and never writes, and why the shop domain is checked so carefully
- [BREVO_CONNECTOR.md](BREVO_CONNECTOR.md) — how the Brevo connection works, why writing a
  contact is safe to repeat, and why sending email is deliberately not part of it
- [INTEGRATIONS_AI.md](INTEGRATIONS_AI.md) — what the model is allowed to write when it drafts
  a workflow, and everything that checks it
- [SECRETS_AND_KEY_ROTATION.md](SECRETS_AND_KEY_ROTATION.md) — how a stored credential is
  encrypted, and what rotating the key involves

**Telling someone**
- [EMAIL_DISPATCH.md](EMAIL_DISPATCH.md) — the engine behind chapter 16: why the text is
  rendered and stored when the email is queued rather than when it is sent, why a worker that
  dies mid-send leaves the email *failed* rather than retrying it, and how sending is
  serialised per server so a burst does not get your domain blocked
- [EVENT_BUS.md](EVENT_BUS.md) — how "when a sync fails" reaches an email trigger, and why
  publishing can never break the thing that published

**Conversations**
- [CHATBOT_AI_SETTINGS.md](CHATBOT_AI_SETTINGS.md) — prompts, variables, model choice, actions
- [FLOW_BUILDER.md](FLOW_BUILDER.md) — scripted conversations
- [AI_INBUILT.md](AI_INBUILT.md) — the local model and the knowledge-base pipeline
- [WIDGET_RENDERING.md](WIDGET_RENDERING.md) — how an answer becomes what a visitor sees
- [CHATBOT_ANALYTICS.md](CHATBOT_ANALYTICS.md) — per-turn measurement and the dashboard

**Big results**
- [DOWNLOADER_AGENTS.md](DOWNLOADER_AGENTS.md) — the count, the offer, the file
- [AGENT_RECURSIVE_DATAFRAMES.md](AGENT_RECURSIVE_DATAFRAMES.md) — whole-result grouping and
  why it's exact

**Under the floor**
- [ARCHITECTURE.md](ARCHITECTURE.md) — layers and project structure
- [SERVICE_PATTERNS.md](SERVICE_PATTERNS.md) — where a rule belongs, and who reads the
  table/column switches
- [SCHEMAS.md](SCHEMAS.md) — the validation layer
- [ERROR_HANDLING.md](ERROR_HANDLING.md) — what reaches a user versus what stays in the log
- [HTMX_PATTERNS.md](HTMX_PATTERNS.md) — the in-place update patterns
- [MIGRATIONS.md](MIGRATIONS.md) — how the database structure is kept current
- [DOCKER_AND_LOCAL_LLM.md](DOCKER_AND_LOCAL_LLM.md) — the runtime, and the measurements
  behind the timeouts
- [TESTING.md](TESTING.md) — the suite and its coverage ratchet
