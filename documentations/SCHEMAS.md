# SCHEMAS.md

The Pydantic schema layer: one package per feature, every request parsed through a
request schema and every response built from a response schema.

Maintained by the `module-schemas` skill
(`.claude/skills/module-schemas/SKILL.md`). Run it whenever a module is added or a
route's payload changes; its audit script fails if a schema here is undocumented,
so this page and `app/schemas/` cannot drift apart.

```bash
python3 .claude/skills/module-schemas/scripts/audit_schemas.py
```

---

## Why this layer exists

Before it, validation lived in three places at once: a regex compiled at the top of
a route module, an `if not x: raise HTTPException` inside a service, and a
`try: uuid.UUID(...) except ValueError` in a handler. The same rule was written
more than once, the copies disagreed, and several of them failed in ways a user
could not act on:

- `?page=abc` fell back to page 1 — a broken link showed the wrong data silently.
- A malformed `subquery_configs` payload was swallowed into `[]`, discarding the
  query the user had just built, and the save reported success.
- A JSON body that parsed to a list turned `(body or {}).get("message", "")` into
  an `AttributeError` and a 500.
- A bad table/column status produced a bare `HTTPException(status_code=400)` — a
  failed request with no explanation on screen.

Each of those is now one declared field with one message.

---

## The rules, in order of how often they matter

**1. Validation errors are `HTTPException`, never `ValidationError`.**
Every route in this application renders `exc.detail` straight into a Bootstrap
alert. `app.schemas.base` catches Pydantic's `ValidationError` and re-raises the
project's own exception with a message built from the field's `title`. Nothing in
`app/schemas/` may let a raw `ValidationError` escape.

**2. Only the first failure is reported.** A form posts every field at once, so one
missing value can cascade into several errors. The user gets the one thing to fix.

**3. Schemas validate shape; services own business rules.** Required, length,
pattern, type, enum membership → schema. Whether a name is taken, whether the
caller owns the row, whether a datasource is reachable → service, because those
need the database. Where a rule is split, the *service* half is authoritative and
the schema deliberately lets the value through.

**4. Public identifiers only.** A request schema accepts `uuid.UUID`; a response
schema exposes `uuid`. No bigint `id` reaches a browser. Two response schemas
(`DatasourceFileView`, `KnowledgeBaseDocumentView`) use the *key name* `id` because
the frontend scripts read it under that name — the value in both is the public
uuid.

**5. `""` and `None` are not always the same thing.** For most fields a blank input
means "no value" and `OptionalText` normalizes it to `None`. But `update_api_key`
and `update_chatbot_key` read an absent field as "leave it" and a blank one as
"clear it" — so `AIApiKeyUpdateRequest` and `ChatbotUpdateRequest` use plain
`Optional[str]` and keep the two apart. Collapsing them would turn "clear this
base URL" or "empty this origin allow-list" into a silent no-op.

**6. Every field needs a `title`.** It becomes the user-facing name in the error
message. Some messages come from `ValidationInfo.field_name` instead, which is why
field names in this layer read as English (`tool_name`, not `tname`).

**7. A multi-select must be declared.** `multi_fields = ("table_names",)` on the
schema. Read as a single value, a query built against four tables silently becomes
a query against one.

**8. Files are not schema-validated.** A file part is a stream, not a value.
`form_to_dict` drops upload parts, and routes read them with
`app.utils.file_utils.read_upload_payloads` / `read_upload_field`. Their rules
(extension, size) belong to the ingestion service that has the bytes.

---

## Layout

```
app/schemas/
├── base.py                      shared infrastructure (no feature owns it)
├── common.py                    cross-feature response shapes
├── ai_analytics/
├── ai_settings/
├── auth/
├── chatbot/
├── chatbot_analytics/
├── data_agents/
├── datasource/
├── deep_agents/
├── flow_builder/
├── sql_assist/
├── tool_configs/
└── workspaces/
```

`base.py` and `common.py` sit at the top level the same way `db_utils.py` sits at
the top level of `db/` — they belong to no feature and every feature depends on
them. Each feature subfolder holds `<feature>_schemas.py` plus an `__init__.py`
that re-exports its public names, mirroring `models/` and `routes/`.

`dashboard/` has no schema package: its one route renders a page from the session
user and reads nothing from the request. The audit re-checks that claim on every
run rather than treating it as a permanent exemption.

---

## Shared infrastructure — `app/schemas/base.py`

### Base classes

| Class | Use | Notes |
|---|---|---|
| `AppBaseSchema` | everything | `str_strip_whitespace=True`, so a whitespace-only value reaches the required-check as empty |
| `RequestSchema` | anything read off a request | `extra="ignore"` — HTMX forms carry fields a given handler does not want (CSRF, the page's filter, hidden partial state); rejecting one would break every form on the site |
| `FormRequest` | `application/x-www-form-urlencoded`, `multipart/form-data` | `await X.from_form(request)` |
| `JsonRequest` | JSON bodies | `await X.from_json(request)`; refuses a non-object body with the schema's own `invalid_body_message` |
| `QueryRequest` | query strings | `X.from_query(request)`; every field must have a default, since a partial query string means "unfiltered", not "bad request" |
| `ResponseSchema` | anything sent back | `from_attributes=True`, so a schema builds straight from an ORM row |

Entry points: `parse(mapping)` (what tests use), `from_form` / `from_json` /
`from_query` (what routes use), `from_form_data(form)` for an already-parsed form.
On the response side: `build`, `build_many`, `payload_for`, `payload_for_many`.
`payload()` dumps with `mode="json"`, which is what turns a `UUID` or `datetime`
into a string.

A response that fails its own schema raises **500**, not 400 — a malformed
response is this application's defect, not the caller's — and the internal reason
travels in the exception's `extra` so it reaches the log without reaching the
screen.

### Reusable field types

| Type | Meaning |
|---|---|
| `RequiredText` | trimmed, non-empty |
| `OptionalText` | trimmed; blank becomes `None` so the column is cleared |
| `RequiredUUID` | a dropdown selection that must be made |
| `OptionalUUID` | a dropdown that may be blank, meaning "none" |
| `CheckboxBool` | reads `on`/`true`/`yes`/`1` and `off`/`false`/`no`/`0`/`""`; absent is `False`; anything else is **refused** rather than read as false |
| `IdentifierName` | lowercase identifier, via `validators.require_identifier` |
| `ObjectName` | a table/collection/column name in the user's own database, via `validators.require_object_name` |
| `JsonObjectField` | hidden field holding a JSON object; blank → `{}` |
| `JsonArrayField` | hidden field holding a JSON array; blank → `[]`; malformed is refused |

`IdentifierName` and `ObjectName` wrap `app/utils/validators.py` rather than
restating its regexes, so a schema field and a service check reject the same input
with the same sentence.

Shared caps: `MAX_NAME_LENGTH` 255, `MAX_DESCRIPTION_LENGTH` 2000,
`MAX_PROMPT_LENGTH` 20000, `MAX_URL_LENGTH` 2048.

### Helpers

`form_to_dict(form, multi_fields)` — flattens a `FormMultiDict`, reads repeated
keys with `getall`, drops upload parts, and defaults a declared-but-unsent
multi-field to `[]` (because "nothing selected" is a state, not an absent field).
`validation_error_detail`, `raise_request_error`, `raise_response_error` are the
error bridge itself.

---

## Cross-feature responses — `app/schemas/common.py`

| Schema | Fields | Purpose |
|---|---|---|
| `StatusResponse` | `status` (`success`\|`error`), `message` | the `{"status", "message"}` envelope CLAUDE.md specifies; `.success()` / `.error()` constructors |
| `ErrorResponse` | `message` | for endpoints whose success body has its own shape, so merging would make every success field optional |
| `ChoiceView` | `uuid`, `name`, `is_active` | one dropdown option. `is_active` is load-bearing: an archived row stays listed so a record already pointing at it can be edited without being silently moved off it |
| `LabelledChoiceView` | `uuid`, `label`, `provider` | AI Settings keys, shown by label + provider |
| `DatasourceChoiceView` | `ChoiceView` + `db_type`, `supports_joins` | a query builder needs both before it has fetched anything |
| `FragmentResponse` | `error` | the HTMX mutation-fragment marker shared by six modules; `error is None` **is** the success signal, kept so no template changes |

---

## Feature packages

### auth — `app/schemas/auth/auth_schemas.py`

The only payload reached before there is a signed-in user, so its messages are the
only ones an anonymous caller sees. They say what to fix about the form and nothing
about which addresses are registered — the deliberately vague "Invalid credentials"
in `app.db.auth` stays the whole story.

| Schema | Field | Rule |
|---|---|---|
| `LoginRequest` | `email` | required, ≤255, must contain `@` with something either side, lowercased |
| | `password` | required, ≤255 |

The email check is permissive on purpose: a strict RFC-5322 pattern rejects
addresses that are valid and deliverable, and all this buys is skipping a database
round trip for something that cannot be an address at all.

### datasource — `app/schemas/datasource/datasource_schemas.py`

`DatasourceCreateSchema` / `DatasourceUpdateSchema` are the original pair and are
still what `datasource_service` validates the name with. They subclass `BaseModel`
rather than the request bases because the service catches their `ValidationError`
directly; the request schemas below reuse their normalizer, so there is exactly one
definition of what a datasource name may be.

**Requests**

| Schema | Fields | Rules |
|---|---|---|
| `DatasourceCreateRequest` | `datasource_name`, `db_type`, `host`, `port`, `database_name`, `username`, `password` | name via the shared normalizer; `db_type` ∈ `ALL_DB_TYPES`; `port` numeric 1–65535. Which connection fields are *required* depends on `db_type` and stays in `create_datasource`, which owns the reachability test too |
| `DatasourceNameRequest` | `datasource_name` | the rename form and the blur availability check, one schema |
| `ObjectStatusRequest` | `status` | ∈ `{active, inactive}`; replaces a bare 400 with no message |
| `TableListQuery` | `search`, `status_filter`, `sort_by` | search lowercased here so every caller agrees; filters ∈ `STATUS_FILTERS` / `SORT_ORDERS` |
| `FileExistsRequest` | `filename` | required, ≤255 |
| `FileUploadRequest` | `override` | the widget's `yes`/`no`; files read separately |
| `FilePreviewQuery` | `page`, `file_id` | `page` ≥ 1 and bounded — previously `?page=abc` fell back to page 1 |
| `ToolNameRequest` | `tool_name` | identifier; shared with the save so the blur check cannot mispredict it |
| `ToolBaseConfigCreateRequest` | `tool_name`, `table_name`, `base_config`, `subquery_configs` | both JSON fields refused when malformed rather than saved empty |

**Responses**

| Schema | Notes |
|---|---|
| `DatasourceFileView` | `id` (the public uuid — the key name the preview script reads), `filename` |
| `TableStatusView` | `table_name`, `status` |
| `DatasourceDetailsResponse` | `datasource_name`, `objects`, `configuration_data` (open dict — its keys are the user's own table names) |
| `FilePreviewResponse` | covers all three shapes the readers produce (table / document / error) because the widget reads one payload and branches on `type`; `.failure(message)` for the error branch |
| `FileUploadResultView` | per-file outcome; `stored_filename`/`version` only populated on the ok branch |
| `FileExistsResponse` | `exists`, `version`, plus `next_version` |

Vocabularies: `CONNECTION_DB_TYPES`, `ALL_DB_TYPES` (built from
`FILE_BASED_TYPES`, so adding a file format in one place is enough),
`OBJECT_STATUSES`, `STATUS_FILTERS`, `SORT_ORDERS`.

### workspaces — `app/schemas/workspaces/workspace_schemas.py`

The simplest package, and the reference the others follow.

| Schema | Fields |
|---|---|
| `WorkspaceCreateRequest` | `name` (required, ≤255), `description` (optional, ≤2000) |
| `WorkspaceUpdateRequest` | same |
| `WorkspaceSetActiveRequest` | `is_active` |
| `WorkspaceView` | `uuid`, `name`, `description`, `is_active`, `agent_count` |

Name uniqueness per user is *not* here — it needs a query, and the race behind it
needs the unique index. Both stay in `workspace_service`.

### data_agents — `app/schemas/data_agents/data_agent_schemas.py`

Two non-obvious points. The **workspace filter** rides on every mutation as a
hidden field, so a rebuilt table keeps the subset the user was looking at;
`DataAgentFilterMixin` is what stops one of the four forgetting it. And
`workspace_id` / `llm_api_key_id` are **genuinely nullable** columns — an agent may
belong to no workspace and may resolve its model from the user's active keys — so a
blank dropdown means "none", not "invalid".

| Schema | Fields |
|---|---|
| `DataAgentFilterMixin` | `workspace_filter` |
| `DataAgentCreateRequest` | + `name`, `description`, `system_prompt` (≤20000), `workspace_id`, `llm_api_key_id` |
| `DataAgentUpdateRequest` | same as create |
| `DataAgentSetActiveRequest` | + `is_active` |
| `DataAgentDeleteRequest` | filter only — it still gets a schema, because the filter has to be validated somewhere |
| `DataAgentListQuery` | `workspace` (`?workspace=<uuid>`, what the Workspaces page links to) |
| `DataAgentView` | `uuid`, `name`, `description`, `system_prompt`, `is_active`, `workspace_id`/`workspace_name`, `llm_api_key_id`/`llm_api_key_label`, `tool_count` |

Related ids default to `""` rather than `None` in the view, because the edit form
compares them against an unselected `<option value="">`.

### tool_configs — `app/schemas/tool_configs/tool_config_schemas.py`

`config_json` is the highest-value input in the application to get wrong: it holds
the query the user built, referring to real tables and columns that are
interpolated into generated SQL rather than bound as parameters. The split is
deliberate — this schema guarantees a JSON object of bounded size, and
`tool_config_service.validated_query_config` validates every reference inside it,
because only it has just reflected the tables. A query naming an unknown column
therefore *passes* this layer.

| Schema | Fields |
|---|---|
| `ToolConfigFilterMixin` | `agent_filter` |
| `ToolConfigCreateRequest` | + `data_agent_id`, `datasource_id`, `tool_name` (identifier), `table_name` (object name), `description`, `config_json` |
| `ToolConfigUpdateRequest` | same as create |
| `ToolConfigSetEnabledRequest` | + `is_enabled` |
| `ToolConfigDeleteRequest` | filter only |
| `ToolConfigListQuery` | `agent` |
| `SchemaCascadeQuery` | `datasource_id`, `table_name`; `.table` exposes a string so `None` never reaches a service as `"None"` |
| `ToolConfigView` | `uuid`, `tool_name`, `table_name`, `description`, `is_enabled`, `agent_id`/`agent_name`, `datasource_id`/`datasource_name`, `config`, `preview` |
| `TableColumnsResponse` | `table_name`, `columns`, `error`; `.failure()` reports a connection error in the payload so the join builder shows it beside the row instead of the offcanvas being replaced mid-edit |

`data_agent_id` and `datasource_id` are optional *here* and required by the
service, where "required" and "you don't own that" are the same query — splitting
them would report "Data agent is required" for an agent that exists but belongs to
someone else.

### ai_settings — `app/schemas/ai_settings/ai_settings_schemas.py`

| Schema | Fields | Rules |
|---|---|---|
| `AIApiKeyCreateRequest` | `provider`, `label`, `api_key`, `is_active`, `base_url`, `model_name` | provider ∈ `AI_PROVIDER_VALUES`; key required, ≤512 |
| `AIApiKeyUpdateRequest` | `label`, `api_key`, `base_url`, `model_name` | plain `Optional[str]` — see rule 5. Provider is absent: a key belongs to the issuer |
| `AIApiKeyView` | `uuid`, `label`, `provider`, `provider_display`, `masked_key`, `base_url`, `model_name`, `is_active` | **no secret**; a test walks the field names so a future `api_key` field fails |

### ai_analytics — `app/schemas/ai_analytics/ai_analytics_schemas.py`

| Schema | Fields | Rules |
|---|---|---|
| `AiAnalyticsGenerateRequest` | `target_type`, `target_name`, `prompt`, `file_id` | type ∈ `TARGET_TYPES`; `target_name` an object name, required for every type; prompt ≤2000; a `file` target must carry `file_id` |
| `AiAnalyticsHistoryQuery` | `target_type`, `target_name` | both default to `""` — the panel opens before a target is chosen |

The `file_id` rule replaces a failure deep inside `_load_one_target`
("file_id is required for file targets") that arrived only after a history row had
been written.

### chatbot — `app/schemas/chatbot/chatbot_schemas.py`

The largest package, and the only one covering an **unauthenticated** payload.

| Schema | Fields | Rules |
|---|---|---|
| `ChatbotCreateRequest` | `name`, `datasource_id`, `target_type`, `target_selection[]`, `allowed_origins`, `workspace_id`, `data_agent_id` | cross-field: a `file` selection must be a uuid, a `table`/`collection` selection an object name, and a `datasource` target needs no selection at all |
| `ChatbotUpdateRequest` | `name`, `allowed_origins` | plain `Optional[str]` (rule 5). The datasource target is absent by design — repointing a published widget changes what every embedded copy answers about |
| `ChatbotSettingsTabQuery` | `tab` | unknown falls back to `appearance` so a stale bookmark still opens |
| `ChatbotAiSettingsRequest` | `agent_name`, `system_prompt`, `variables_json`, `llm_mode`, `llm_api_key_id` | mode ∈ `LLM_MODE_VALUES`; the key id stays a string so `""` keeps its "any active key" meaning |
| `ChatbotFlowRequest` | `flow_id` | blank clears the attachment; keeps the module's own wording for a bad selection |
| `ChatbotDataAgentRequest` | `workspace_id`, `data_agent_id` | blank clears |
| `ChatbotActionRequest` | `name`, `description`, `http_method`, `url`, `headers_json`, `body_template`, `parameters_json`, `timeout_seconds` | method ∈ `ACTION_HTTP_METHODS` (uppercased first); URL must be `http(s)://`; timeout 1–30, mirroring `chatbot_action_service._TIMEOUT_RANGE` |
| `ChatbotActionAttachRequest` | `action_id` | required; empty picker keeps "Please select an action to add." |
| `WidgetAppearanceRequest` | 20 appearance fields + 5 `remove_*` flags | shape and bounds only — the colour/size/font rules live in `chatbot_widget_settings_service` with their own messages. `.appearance_values()` / `.removal_values()` feed its dataclasses |
| `PublicWidgetConfigQuery` | `api_key` | required rather than defaulted, so a misconfigured embed is distinguishable from a wrong key |
| `PublicChatbotMessageRequest` | `api_key`, `message` (≤4000), `session_id` (≤128), `selected_value` (≤1000) | **the untrusted body.** Bounded because every accepted message is a paid model call |
| `ChatbotTurnResponse` | `status`, `type`, `summary`, `text`, `insights`, `table`, `options`, `message`, `response_time_ms` | `.from_turn(TurnResult)`; `text` duplicates `summary` for the flow node types; an error payload carries only message + timing |
| `WidgetConfigResponse` | `status`, `title`, `extra="allow"` | `build_widget_public_config` owns the key set; narrowing it here would drop a key the widget script needs |
| `ChatbotKeyView` | `uuid`, `name`, `api_key`, `target_type`, `allowed_origins`, `is_active` | `api_key` is *publishable* — it goes in the embed snippet, and its protection is the per-key origin allow-list, not secrecy |
| `ChatbotActionView` | `uuid`, `name`, `description`, `http_method`, `url`, `is_active`, `parameter_count`, `attached_count` | |

The key and origin checks are **not** here: they need the database and the request's
`Origin` header, and `chatbot_service` owns both. What the schema guarantees is
that a body reaching them is a JSON object with fields of sane types and sizes.

### chatbot_analytics — `app/schemas/chatbot_analytics/chatbot_analytics_schemas.py`

| Schema | Fields | Rules |
|---|---|---|
| `AnalyticsDashboardQuery` | `period`, `chatbot_id` | absent period → the default; **present but unknown → refused**; a blank `chatbot_id` means "all agents", an unreadable one is refused |

The absent/unreadable distinction matters more here than in a form: a filter that
silently falls back does not fail visibly — it renders real figures for the wrong
scope, and nothing on screen says so.

### flow_builder — `app/schemas/flow_builder/flow_schemas.py`

The canvas is entirely client-rendered, so this module's endpoints exchange JSON.

| Schema | Fields | Rules |
|---|---|---|
| `FlowCreateRequest` | `name` | required, ≤255 |
| `FlowRenameRequest` | `name` | same |
| `FlowSetActiveRequest` | `is_active` | the publish/draft flag; attachment is separate |
| `FlowGraphSaveRequest` | `nodes` (≤500), `edges` (≤2000), `extra="allow"` | bounded but not narrowed — the node vocabulary belongs to `flow_builder.js` and `flow_service.update_flow_graph`. `.graph_data()` returns the whole document, extras included |
| `KnowledgeBaseManualTextRequest` | `label`, `text` (≤100000) | replaces `payload.get(...)` on a body that might not be an object |
| `FlowView` | `uuid`, `name`, `is_active`, `updated_at`, `chatbot_name` | |
| `KnowledgeBaseDocumentView` | `id` (public uuid), `label`, `source_type`, `size_bytes`, `extraction_status`, `error_message`, `created_at` | |
| `KnowledgeBaseStateResponse` | `status`, `trained_at`, `error_message`, `documents` | the body of the state, upload and train endpoints; serializes a `datetime` rather than leaving it to an encoder |

### sql_assist — `app/schemas/sql_assist/sql_assist_schemas.py`

Ask AI is a four-step conversation carried entirely in form fields. Each step
re-posts the context of the one before, so nothing is held server-side and a
tampered value is just another value the services validate.

| Schema | Fields |
|---|---|
| `SqlAssistEchoMixin` | `datasource_id`, `llm_mode`, `llm_api_key_id`, `agent_filter`, `table_names[]` (≤`MAX_REFLECTED_TABLES`); `.echo()` renders them as strings for the hidden inputs |
| `SqlAssistFormQuery` | `agent` |
| `SqlAssistTablesQuery` | `datasource_id` |
| `SqlAssistGenerateRequest` | + `prompt` (≤2000), `history_json` |
| `SqlAssistToolFormRequest` | + `sql` (≤20000), `history_json` |
| `SqlAssistCreateToolRequest` | + `data_agent_id`, `tool_name`, `table_name`, `description`, `config_json`, `preview` |

`.echo()` returns strings deliberately: the partials put these into hidden inputs,
and a `None` would render as the text "None" and be posted back as a selection on
the next step. `table_names` is capped at the *reflection* limit so the schema can
never accept more tables than the schema-reader will read. The generated SQL is
bounded but not pattern-checked — whether it is safe to run is decided by
`sql_assist_service` against the reflected schema, and a regex over SQL would be a
false reassurance.

### deep_agents — `app/schemas/deep_agents/deep_agent_schemas.py`

| Schema | Fields | Rules |
|---|---|---|
| `AgentOptionsQuery` | `workspace_id`, `selected`, `field_name` | a blank `workspace_id` lists **every** agent, not none — `data_agents.workspace_id` is nullable, so an unassigned agent has to stay pickable. `.select_name` falls back to `data_agent_id` |
| `DeepAgentAskRequest` | `question` | required, ≤2000 — the same cap Ask AI and AI Analytics use, so a person typing into a console and a person typing into a prompt box don't hit two different limits |

---

## Adding a schema

Invoke the skill; it will run the audit and tell you what is outstanding. The
procedure in short:

1. `app/schemas/<feature>/` with `<feature>_schemas.py` and a re-exporting
   `__init__.py`.
2. One request schema per handler that takes input, one response schema per
   distinct response body. Inherit from `base.py`, never from `BaseModel`.
3. Wire the route: `payload = await XRequest.from_form(request)` inside the
   existing `try/except HTTPException`, so validation failures render through the
   error path the handler already has.
4. Add the schemas to this page.
5. `tests/unit/schemas/<feature>/test_<feature>_schemas.py` — assert on the
   *messages*, since the message is the product.
6. Re-run the audit until it is `0`.

---

## Tests

`tests/unit/schemas/` mirrors `app/schemas/`. The suite asserts on error messages
rather than only on "it raised", because a readable message is what this layer
delivers. It also pins the places where a schema deliberately does *not* validate:
a query naming an unknown column, a graph with an unrecognised node type, a
provider key that happens to be a duplicate. Those tests exist so a future change
cannot quietly duplicate a service's rule here — where it would be the copy without
the database in hand.

See [TESTING.md](TESTING.md) for the harness and the coverage ratchet.
