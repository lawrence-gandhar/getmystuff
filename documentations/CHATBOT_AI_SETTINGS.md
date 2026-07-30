# CHATBOT_AI_SETTINGS.md

Per-agent AI configuration: the agent's name and system prompt, the prompt variables
substituted into it, which language model answers, which webhook **actions** it may call
mid-conversation, and which Flow Builder flow (if any) drives the conversation.

An agent is a `ChatbotApiKey` — the sidebar calls them **Agents**, while the URLs are still
`/chatbot-settings/...`. Its prompt, variables and model choice belong to it alone; actions and
flows are shared/standalone objects that are *attached* to it. Everything is edited on the
agent's settings page (`/chatbot-settings/{key_uuid}/widget-settings`) under the
**AI & Prompt** and **Actions** tabs.

---

# Models — `app/models/chatbot/chatbot_ai.py`

| Model | Table | Cardinality |
|---|---|---|
| `ChatbotAiSettings` | `chatbot_ai_settings` | one per agent (created with the agent) |
| `ChatbotAction` | `chatbot_actions` | many per **user**, max 30 — a shared library |
| `ChatbotActionLink` | `chatbot_action_links` | joins one library action to one agent |

`DEFAULT_SYSTEM_PROMPT` and `default_variables()` live in the model module because they are
column defaults — a model must never import a service.

### LLM choice
`llm_mode` is `"api_key"` (default) or `"in_built"`:

* `api_key` + `llm_api_key_id = NULL` → whichever active AI Settings key
  `ai_analytics_service._resolve_provider` picks first. This is exactly what chatbots did
  before this feature existed, which is why it's the default.
* `api_key` + a pinned key → that key, by uuid. The FK is `ON DELETE SET NULL`, so deleting
  the key in AI Settings degrades to "any active key" instead of erroring mid-chat.
* `in_built` → the app-wide local Ollama model (see [AI_INBUILT.md](AI_INBUILT.md)); no
  credential involved, so any pin is cleared when this mode is saved.

---

# Prompt variables

`{{NAME}}` placeholders in the prompt are filled from the agent name plus the owner's declared
variables (`app/services/chatbot/chatbot_ai_settings_service.py`).

* `{{AGENT_NAME}}` is **built in** — bound to the `agent_name` column, never declared as a
  variable.
* Names are stored upper-cased and matched case-insensitively, so `{{company}}` and
  `{{COMPANY}}` are the same variable.
* Saving is **rejected** when the prompt references a variable that isn't defined, or when a
  defined variable has no value. That is deliberate: a live chatbot must never send a
  half-filled prompt to a model, and a stray `{{COMPANY}}` is a misconfiguration the owner
  needs to see rather than something to silently blank out.
* Limits: 30 variables, 500 characters per value, 20 000 characters of prompt.

## How the prompt reaches the model

`chatbot_reply_service.generate_reply` renders it and passes it as `system_prompt_override`
through `chatbot_service.answer_message` → `ai_analytics_service.run_grounded_prompt`. It
replaces the default data-analyst preamble, but `_GROUNDING_ADDENDUM` is always appended:
answers may only use figures from the computed data profile, and the assistant must say so
when the data doesn't cover the question. No owner-authored prompt can opt out of that.

A Flow Builder AI Fallback node uses the same prompt as its base persona and layers its own
guardrails/prompt on top — but its **LLM choice wins** for the turns it handles. See
[FLOW_BUILDER.md](FLOW_BUILDER.md).

---

# Conversation flow

The AI & Prompt tab's *Conversation Flow* card attaches one Flow Builder flow to this agent
(`POST /chatbot-settings/{key_id:uuid}/flow` → `flow_service.attach_flow`). It is a separate
form from the AI settings one because the attachment is stored on the flow row, not in
`ChatbotAiSettings`. Only flows that are **active** and not attached to another agent are
offered; an attached flow that has been made a draft still shows, marked as not running. When a
flow is attached and active it drives each turn until it ends, after which the agent's prompt
takes over — see [FLOW_BUILDER.md](FLOW_BUILDER.md).

---

# Actions

An action is an HTTP call the agent can make before answering — an order lookup, an
availability check, a ticket creation.

## Ownership vs. attachment

An action belongs to the **user** and lives in the Actions library (`/actions`, sidebar); a
`ChatbotActionLink` row attaches it to an agent, and the same action can serve any number of
agents. Consequences worth knowing:

* Editing an action changes it for **every** agent using it. The library page shows how many
  agents that is, and confirms before deactivating or deleting a shared one.
* `is_active` is the library switch: an inactive action cannot be attached, is not offered in an
  agent's dropdown, and stops running everywhere it is already attached — without being detached.
* An agent's Actions tab can only add and remove; create/edit/delete live in the library. The
  one shortcut is *New Action* on that tab (`create-and-attach`), which saves to the library and
  attaches in a single step.
* Names are unique per user, because the name is the model-facing tool name and duplicate names
  would make routing ambiguous.
* Every chatbot-scoped question about actions is therefore a join, kept in
  `app/db/chatbot/queries.py`: `fetch_actions_for_key` (the answer path passes
  `active_only=True`), `fetch_attachable_actions`, `count_action_attachments`,
  `fetch_action_attachment_names`.

## Turn flow (`chatbot_action_service.maybe_run_action`)

1. **No active actions attached → nothing happens.** No extra LLM call for the majority of agents.
2. **Router pass** — one structured call (`ai_analytics_service.answer_structured` with the
   `ActionSelection` schema) answering "which action, with which parameters?".
3. **Execute** — parameters are type-checked against the action's declared schema, then the
   request is rendered and sent.
4. **Answer** — the (bounded) response is injected into the answering call as context.

This is a router pass rather than native tool calling because native tools would need three
separate provider implementations *and* would collide with the forced structured output every
provider path already uses. The trade-offs, accepted deliberately: one extra round-trip, and
one action per turn with no chaining.

## Placeholders

| Target | Allowed | Escaping |
|---|---|---|
| URL | `{{VAR}}`, `{{param.name}}` | percent-encoded per value |
| Headers | `{{VAR}}` **only** | rejected if the rendered value contains a line break |
| Body | `{{VAR}}`, `{{param.name}}` | JSON-escaped; quote string placeholders, leave number/boolean bare |

Headers exclude `{{param.*}}` on purpose: parameter values are derived from visitor text, and
such a value must never be able to forge an auth header or split a request. Any parameter used
in a URL or body must be marked **Required**, so a rendered request can never contain a hole.

Header lists are Fernet-encrypted at rest (`headers_encrypted`, same helper as
`ai_api_keys.api_key_encrypted`) because that is where bearer tokens live. They are decrypted
for the owner's own edit form — the encryption protects the database, not the owner.

## Egress safety

An action is user-authored outbound HTTP from the server, i.e. textbook SSRF surface:

* `https://` only; no credentials in the URL.
* The host is resolved and checked immediately before the request; any private, loopback,
  link-local (this covers `169.254.169.254`), reserved, multicast or unspecified address is
  rejected. Checked at save time too, for a readable error.
* `follow_redirects=False` — a redirect is the standard way past an IP check.
* Per-action timeout, 1–30 s. Response body capped at 256 KB read / 4 000 characters shown to
  a model.
* Check-then-request narrows but does not fully close DNS rebinding; closing it needs IP
  pinning at the transport layer, which httpx doesn't expose.

## Failure handling

`execute_action` never raises — a broken endpoint degrades the answer instead of breaking the
conversation. Every failure is logged with detail, described to the model in general terms
("could not retrieve that information right now"), and recorded on the message as
`ChatbotMessage.ai_response["action"] = {"name", "status", "http_status"}`, which the
conversation history renders as a badge. Visitors never see endpoint URLs, status text or
misconfiguration messages.

---

# Layering

```
routes/chatbot/chatbot_settings_routes.py   (agent settings: attach flow/actions)
routes/chatbot/action_routes.py             (/actions library)
routes/chatbot/public_chatbot_routes.py     (widget)
        ↓
services/chatbot/chatbot_reply_service.py   compose one reply
        ↓                       ↓
chatbot_ai_settings_service   chatbot_action_service
        ↓                       ↓
services/chatbot/chatbot_service.py         answer + log
        ↓
services/ai_analytics/ai_analytics_service.py
```

`chatbot_reply_service` sits *above* `chatbot_service` so the dependency direction stays
one-way. `get_or_create_ai_settings` lives in `app/db/chatbot/queries.py` because both
`chatbot_service` (creating the row with the chatbot) and `chatbot_ai_settings_service`
(reading/updating it) need it, and a service-to-service import between those two would be
circular.

---

# Known limitations

* The prompt's `EXIT BEHAVIOUR` is honoured as text only — the model sends the closing message,
  but the widget session is not terminated; there is no signal channel for that yet.
* Turns are stateless: no conversation history is sent to the model (pre-existing behaviour).
* One action per turn, no chaining (see the router-pass trade-off above).
