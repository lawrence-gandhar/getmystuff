# INTEGRATIONS_AI.md
The AI layer of the Integration Platform — what a model is allowed to write, and what checks it

> **Status: the generation surface is built (Phase 1).** AI field mapping and run triage are
> Phase 2; the canvas copilot is Phase 4; the `agent` node is Phase 5. Each of those has a
> section below saying what it will be and what it must not become, because the constraints are
> easier to agree now than to retrofit.
>
> The engine itself is [INTEGRATIONS.md](INTEGRATIONS.md). This page is only the part where a
> language model is involved.

---

# "Always act as Agent AI", read honestly

The instruction this module was built under says the application should always act as an agent.
Taken literally — a model deciding each step while records are moving — it cannot hold here, and
saying so plainly is better than quietly building something else and calling it agentic.

An integration engine has one property the rest of this application does not need: **the same
input has to produce the same API calls, in the same order.** Without it, "why did this customer
get charged twice" is unanswerable, a replay is not a replay, and an audit trail records what a
model felt like doing on Tuesday. A model choosing steps at run time deletes that property, and
nothing else here can restore it.

So the design keeps both halves, split by *when*:

> **The user is never more than one sentence away from an agent, and the agent is always the
> thing that changes a workflow — never the thing that runs one.**

A model can write the workflow, propose its field mappings, explain why it failed and eventually
edit it in conversation. What executes is a frozen version, node by node, the same way every
time. That is not a weaker form of agency; it is agency at the point where being wrong is
recoverable, and determinism at the point where it is not.

| surface | phase | the model's output is |
|---|---|---|
| **Generate a workflow from a sentence** | 1 | a draft a person publishes |
| **AI field mapping**, with a confidence | 2 | a suggestion in a grid somebody edits |
| **Run-failure triage** over the real rows | 2 | an explanation beside a deterministic report |
| **Canvas copilot** — a chat panel docked to the drawing | 4 | always a proposal with a Save button |
| **The `agent` node** | 5 | one value, from a closed set, validated before anything sees it |
| **Workflows as Deep Agent tools** | 5 | a tool call into a deterministic run |

Every one of those is *additive to a working non-AI surface*. The canvas works with the AI panel
never opened; the mapping grid has "map matching names", which is a string comparison labelled
as one; the run page shows a report built without a model. If `resolve_provider` returns a 503,
what breaks is a panel.

---

# Generation, which is the only one built

## The shape

```
a sentence  →  catalogue built from the user's real rows
            →  one call through ai_analytics_service.answer_structured
            →  WorkflowDraft            (Pydantic bounds the shape)
            →  validate_draft           (bounds the meaning, against the same catalogue)
            →  flow_rules.validate_flow (the rules a hand-drawn workflow passes)
            →  a drawing on screen, saved by nobody until a person presses Save
```

Four files: [catalogue.py](../app/services/integrations/ai/catalogue.py) builds what the model is
told exists, [draft_prompts.py](../app/services/integrations/ai/draft_prompts.py) holds the text,
[workflow_author.py](../app/services/integrations/ai/workflow_author.py) does the asking and the
checking, and [workflow_draft_schemas.py](../app/schemas/integrations/workflow_draft_schemas.py)
is what the model is allowed to say.

## Why `answer_structured` and not `build_chat_model`

Three reasons, and the third is the one that decided it. Generation is single-shot, so an agent
loop buys nothing. `answer_structured` already owns provider resolution, retries and error
mapping for all three providers. And it covers **the in-built Ollama path**, which
`build_chat_model` refuses outright via its tool-calling denylist — so choosing the other one
would have meant the feature silently not existing for anybody running locally.

## The catalogue is the constraint

A model with no Shopify connection and a request to sync Shopify orders will emit a
correctly-shaped step pointed at a URL it invented. Telling it not to is a suggestion; **not
giving it the vocabulary is a constraint.** The catalogue is built from the user's active
connections and their real operations, so a connector they have not connected is simply absent
from the prompt.

Rendered as compact Markdown rather than JSON Schema, because `_json_only_instruction` already
appends the draft's own schema for providers without strict structured output, and a second JSON
blob in one prompt is what makes a small model return something unparseable.

**Built per call and never stored.** The `_RULES_FINGERPRINT` / `is_prompt_stale` machinery in
the Deep Agents module exists because a stored prompt has to track two independently moving
things. Nothing here is stored, and the catalogue *must* be fresh by construction: the ordinary
sequence is somebody adding a connection and immediately asking for a workflow that uses it.
Staleness here is not a risk to manage — it is a bug avoided by not caching.

## What the model does not write

**No node ids, no edge ids, no positions, and no edges.** It writes short `ref` handles and an
ordered list of steps; `validate_draft` assigns the rest.

Ids, because a model-chosen id that collides silently rewires the graph, joining two steps that
were never meant to meet. Positions, because a model spending tokens on coordinates is a model
returning malformed output. **Edges**, because a model-drawn batch whose body never returns is
one batch of a hundred reported as a success — and the drawing looks entirely reasonable, so
nobody reviewing it has a reason to doubt it. Computing the wiring makes that failure
*unrepresentable* rather than merely unlikely, and there is a test that asserts the body always
returns to its batch.

## What the model may not use

`validate`, `branch` and `filter` are absent from the draftable set. All three decide **which
records go where**, and that is not a decision to take from prose.

`filter` is the subtle one, and the tests forced it. The plan had the model's free-text
condition ("only EU orders") recorded as a warning for a person to finish. But `validate_flow`
refuses a filter with no conditions — because a filter that lets every record through looks like
it is working — so that draft could never have been saved. Compiling the prose into an operator
and a typed value was the alternative, and it is exactly the guess that comes out meaning the
opposite. All three are added on a canvas, by somebody who can see both edges.

## Two layers, and what each one is for

Structured like `aggregate_planner.validate_plan`.

**Pydantic bounds the shape.** Twelve steps, twenty mappings, five assumptions, every field a
string of bounded length, and handles that are unique — two steps sharing a handle is not a
mistake a person makes and is one a model makes, and left alone the second silently wins every
reference pointed at either.

`WorkflowDraft` subclasses `RequestSchema`: **an LLM's structured output is an untrusted
request**, in the same class hierarchy as a browser form post, exactly as `AggregationPlan` is.
Nothing about having been generated makes it safer than something somebody typed.

**`validate_draft` bounds the meaning**, against the same catalogue the model was shown, so what
was offered and what is enforced cannot disagree. It is pure — no database, no network — which
is what makes every one of its refusals a table-driven unit test.

| the lie | what happens without the check |
|---|---|
| a connection that does not exist | fails loudly on the first record |
| an operation the connector does not have | fails loudly on the first record |
| **a mapping target the operation does not accept** | **nothing. The sync runs, reports success, and does not carry that field** |
| a required field with nothing mapped | the run fails at the destination's own validation |
| a forward reference | reads an empty set and reports success |

The third row is why this module exists in the form it does. `customer_email` where the
operation takes `email` produces a workflow that is green in every dashboard and quietly loses
the address, because as far as the engine is concerned nobody asked for that field.

Then it runs **the same `flow_rules.validate_flow` a hand-drawn workflow goes through**, which is
what makes a generated workflow exactly as trustworthy as a drawn one — and what makes a rule
added next year bind the generator automatically.

## Resolution replaces the spelling

A resolved connection name becomes the real `connection_uuid` on the node. Not tolerated, not
recorded alongside — **replaced**, so nothing downstream can ever act on a name a model chose.
There is a test asserting the saved node holds the uuid and not the label.

**No fuzzy matching**: exact, then case-insensitive, then stop. "Shopify Prod" quietly resolving
to "Shopify EU" writes somebody's customers into the wrong store, silently, at 3am, on a
schedule. One more exchange is cheaper than a data migration. The refusal lists the real names,
because "that connection does not exist" leaves somebody guessing and the same sentence followed
by their three connections is one they can act on.

## One repair, then refuse

A departure from `sql_assist._regrouped`, which degrades to a warning attached to the flawed
output. The artefacts differ: a flawed SQL string is readable and its author fixes it, whereas a
canvas rendered with a step pointed at a connection that does not exist invites them to press
Publish.

**Only resolvable faults are retried.** `unsupported = true` is never retried — the model has
answered the question, and asking the same model the same thing again to get a different answer
is how a decline becomes a hallucination.

## Nothing is saved until a person saves it

Generating and saving are **two requests**. `POST /ai/generate` returns a drawing in a hidden
field and writes nothing; `POST /ai/save-draft` is a second press, and it goes through
`flow_service.create_flow` like every other new workflow — same name rule, same validation,
same `is_active = False`.

Every refusal test asserts *twice*: that the reason came back, and that
`SELECT count(*) FROM integration_flows` is still zero. **The row count is the point.** Asserting
the refusal alone would pass an implementation that saved first and validated after, which is a
difference nobody can see from the outside until the day it matters.

The draft schemas contain no `is_active` field, no schedule and no interval, anywhere. A field
that cannot be set cannot be set wrongly.

`created_by_ai` is recorded and shown as a badge on the list page. It is the only thing about the
row that differs, and somebody looking at a workflow that fires at 3am is entitled to know a
model drafted it.

---

# What running it against a real model showed

Worth recording, because it is the only evidence that any of the above works under the conditions
it was designed for rather than against a stub.

`qwen3:1.7b`, one real connection with two operations, one real request. The model returned a
draft containing three hallucinations:

```
'List Contacts' uses a connection called 'list_contacts', which does not exist.
                You have: Acme CRM.
'Map Email'      reads from 'step-1', which is not a step that comes before it.
'Create Contact' reads from 'step-2', which is not a step that comes before it.
```

An operation id in the connection field, and two forward references to handles it had never
declared. All three caught, each named with its real alternative, one repair attempted, then
refused — with nothing written.

**And a limitation, measured rather than estimated.** The prompt did not fit:

| | tokens |
|---|---|
| the JSON schema `_json_only_instruction` appends | **971** |
| the system prompt | 710 |
| the catalogue and the user's sentence together | ~100 |
| **available** (`OLLAMA_NUM_CTX` 2048 − `OLLAMA_NUM_PREDICT` 512) | **1536** |

The generated `WorkflowDraft` schema costs more than everything else combined. **This task is out
of reach for a 1.7B model at a 2048-token context**, and trimming this module's own prompt does
not change that — a deployment that wants the local path has to raise `OLLAMA_NUM_CTX`. Ollama
truncates from the *end*, which is where the user's own sentence goes, so an over-long prompt
does not crowd the request out, it deletes it and the model answers the catalogue.

The catalogue is still capped harder for the local path, because twenty connections with fifteen
operations each really would be 12,000 characters. But that cap bounds the *variable* part, and
the constant that holds it says in as many words that it is not what makes the local path work.
An earlier version of that comment claimed otherwise, on one measurement, and was wrong.

Hosted providers are unaffected. The feature degrades correctly: a sentence in the panel, nothing
saved, the canvas untouched.

---

# The trap that costs money

All three provider paths already call `record_llm_call`, and it is **a no-op when no turn is
open.** Every AI route handler wraps its service call in `with record_turn():`. Miss it and the
feature's entire token spend is invisible — not wrong, *invisible*, with nothing anywhere saying
so and no error to notice.

---

# Later surfaces, and their constraints

Written now because they are easier to agree than to retrofit.

## AI field mapping — Phase 2

A suggestion in a grid somebody edits, carrying a confidence. It sits beside "map matching
names", which is a string comparison and is **labelled as one** — never dressed up as
intelligence, because a name that happens to coincide is not a match somebody should trust
without looking.

## Run-failure triage — Phase 2

**The deterministic half comes first and always runs.** `build_failure_report` uses no model at
all: the run row, every step in sequence, the recorded status and redacted body, the per-record
counts and up to eight failed records. **The first step with `status = failed` is the recorded
cause, and it is a database fact.**

The AI explanation is a second partial on top, visually distinct, with an `unknown` decline
field. **Nothing the model says is ever persisted onto the run** — `likely_cause` never touches
`run.error_message`, because a stored guess is indistinguishable from a finding a week later.
`fix_kind` drives a *link*, never an action: auto-repairing a failed run is the shape of feature
that quietly changes what a workflow does at 3am.

## The canvas copilot — Phase 4

`create_deep_agent` with read-only tools, docked to the drawing. Its terminal state is **always a
proposal with a Save button** — it never writes the drawing itself, for the same reason
generation does not.

## The `agent` node — Phase 5

The explicit escape hatch, and the only non-deterministic node. It exists so that "the runtime is
deterministic" stays a true statement with a named exception rather than a claim somebody erodes.
Constraints, all of which are conditions of it shipping at all:

* output constrained to a **closed set**, validated before anything downstream sees it
* **may not call a connector** — it decides, it does not act
* the prompt and the response recorded on the step row, so a run holding one is still auditable
* `flow_rules` already reserves the type and `validate_flow` refuses it until
  `node_runners.register_runner("agent", …)` exists, so the slot cannot be used early by accident

---

# Related

* [INTEGRATIONS.md](INTEGRATIONS.md) — the engine, the connectors, the credentials and the canvas
* [AGENT_RECURSIVE_DATAFRAMES.md](AGENT_RECURSIVE_DATAFRAMES.md) — `aggregate_planner`'s
  two-layer validation, which this copies, and `AggregationPlan`'s decline mechanism
* [SQL_ASSIST.md](SQL_ASSIST.md) — generating a structured artefact from a sentence, and
  repairing exactly once
* [DEEP_AGENTS.md](DEEP_AGENTS.md) — stored prompts and the staleness machinery this
  deliberately does not need
* [CHATBOT_ANALYTICS.md](CHATBOT_ANALYTICS.md) — `record_turn` and what it measures
