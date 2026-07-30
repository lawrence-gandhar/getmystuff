# CHATBOT_ANALYTICS.md

Per-turn performance logging for embeddable chatbots, and the dashboard that
reads it.

Sidebar entry: **Chatbot Analytics** → `/chatbot-analytics/`

---

## What gets measured

Every visitor turn writes exactly one `chatbot_messages` row carrying both the
conversation and what the turn cost:

| Column | Meaning |
|---|---|
| `response_time_ms` | Server-side wall time for the whole turn — flow steps, webhook action, model call(s), response assembly. Excludes network latency to the visitor's browser. |
| `request_tokens` / `response_tokens` / `total_tokens` | Summed across **every** model call the turn made (an action-router call plus the answer both count). |
| `llm_call_count` | How many model calls that was. |
| `tokens_estimated` | True when a provider reported no usage and counts were derived from text length. Surfaced as a caveat on the dashboard — never treat those as billing figures. |
| `llm_provider` / `llm_model` | Who answered (`anthropic`, `openai-compatible`, `in_built`). Null on a flow turn that called no model. |
| `turn_type` | `ai` (a model answered) or `flow` (the Flow Builder graph answered from its own script). |

The same `response_time_ms` is returned to the widget in the `/public/chatbot/message`
payload and rendered under the reply bubble, so what a visitor sees and what
the owner sees can never disagree.

---

## How the numbers are collected

One turn can make model calls in several layers — the action router, the
grounded answer, an AI Fallback node inside a flow — and the layer that knows a
call's token cost sits well below the layer that writes the log row. Rather
than threading a "usage" argument through every signature in between, the
totals accumulate in a context-local record:

```
app/utils/turn_recorder.py

record_turn()            opened once, at the turn boundary
record_llm_call(...)     called by each provider path in ai_analytics_service
record_action(...)       called by chatbot_action_service when an action runs
```

Recording is a no-op when no record is open, so code paths outside a chatbot
turn (the authenticated "Ask AI" flow, background jobs) are unaffected.

### The turn boundary

`app/services/chatbot/chatbot_turn_service.py` is the top of the answer stack
and the **only** place a turn is logged:

```
public_chatbot_routes.message
  └─ chatbot_turn_service.answer_turn        ← opens the record, writes the row
       ├─ flow_service / engine_service      (flow answers, if a flow is active)
       │    └─ ai_fallback_service           (an AI Fallback node inside the flow)
       └─ chatbot_reply_service.generate_reply
            ├─ chatbot_action_service.maybe_run_action
            └─ chatbot_service.answer_message → ai_analytics_service
```

Two consequences worth knowing:

* `chatbot_service.answer_message` does **not** persist anything. A single turn
  can reach it twice (once through an AI Fallback node), which is how the
  earlier design produced duplicate rows.
* The turn service is also where flow-only turns get logged, so an agent driven
  entirely by a flow still appears in the dashboard.

Logging is best effort: a visitor who has already been answered is never shown
a failure because the log write failed — the error is logged for the operator
and swallowed.

---

## Module layout

```
app/db/chatbot_analytics/queries.py                     grouped aggregates over chatbot_messages
app/services/chatbot_analytics/chatbot_analytics_service.py   filters, view shaping, formatting
app/routes/chatbot_analytics/analytics_routes.py        page + HTMX body
templates/chatbot_analytics/index.htm                   page shell, filters, chart CSS
templates/chatbot_analytics/partials/dashboard.htm      the swappable dashboard body
```

Ownership is enforced in SQL: every query joins `chatbot_api_keys` and filters
on `user_id`, so no caller can read another account's traffic by passing the
wrong id. Percentiles use `percentile_cont` in the database rather than sorting
rows in Python.

### Filters

`?period=` accepts `24h`, `7d` (default), `30d`, `90d` — anything else is
rejected with a readable message rather than coerced. `?chatbot_id=` takes an
agent's public `uuid`; empty means all agents. Both are read by the page and by
`/chatbot-analytics/data`, the HTMX target that re-renders the body alone.

Ranges under two days bucket the chart by hour, longer ranges by day. Empty
buckets are filled in by the service so a quiet day reads as a quiet day rather
than disappearing from the axis.

### What the page shows

* Headline tiles — messages, success rate, average and 95th-percentile response
  time, tokens in/out, tokens per message.
* Messages per bucket (with failures overlaid) and average response time per
  bucket, drawn as plain CSS bars so the page needs no charting library and
  renders identically inside an HTMX swap.
* Per-agent table, model/provider spend, and the flow-vs-AI split.
* The ten slowest turns and the ten most recent failures — the two lists worth
  looking at first when something is wrong.

---

## Migration

`d41c7a63b902_add_chatbot_message_performance_columns` adds the columns above.
Rows written before it are backfilled with `0` / `false` / `'ai'`, so a
historical turn honestly reports "no measurement recorded" (the dashboard shows
`—` for its timings) instead of a fabricated zero-millisecond answer.
