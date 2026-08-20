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
a query against one. Both `ToolConfigCreateRequest` and `SqlAssistEchoMixin` declare
it for that field.

Where the *order* of a multi-select carries meaning, say so and never sort it. A tool
config's first table is its primary one — the base table a built query's joins hang
off — so `table_names` is filtered for blanks and de-duplicated, never reordered.

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
├── query_test/
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

`sql_query` is the same split drawn once more for the other query mode (see
[TOOL_QUERY_MODES.md](TOOL_QUERY_MODES.md)). The schema bounds its length and
validates `query_mode` against a closed set it owns; whether the statement is a
single read-only one is `tool_config_service.validated_tool_sql`, which shares that
rule with the Deep Agents executor and with Ask AI via `app/utils/sql_guard.py`. A
`DELETE` therefore *passes* this layer too — a second copy of that guard here would
be the one nobody checks against the copy that runs at query time.

| Schema | Fields |
|---|---|
| `ToolConfigFilterMixin` | `agent_filter` |
| `ToolConfigCreateRequest` | + `data_agent_id`, `datasource_id`, `tool_name` (identifier), `table_names[]` (object names, ≤`MAX_REFLECTED_TABLES`, at least one — primary first), `description`, `query_mode` (`builder`/`sql`, blank means builder), `config_json`, `sql_query` (≤20000), `children_json` (≤`MAX_NESTED_TOOLS`), `sql_params_json` (≤`MAX_SQL_PARAMS`) |
| `ToolConfigUpdateRequest` | same as create |
| `ToolConfigSetEnabledRequest` | + `is_enabled` |
| `ToolConfigDeleteRequest` | filter only |
| `ToolConfigListQuery` | `agent` |
| `SchemaCascadeQuery` | `datasource_id`, `exclude`, `table_name`, `table_names[]`; `.table` exposes a string so `None` never reaches a service as `"None"`, and `.tables` the whole list (falling back to the single value) |
| `ToolConfigView` | `uuid`, `tool_name`, `table_name`, `extra_tables[]` and `sql_params[]` (`NULL` coerced to `[]`, so a row predating either column validates), `description`, `query_mode`, `is_enabled`, `agent_id`/`agent_name`, `datasource_id`/`datasource_name`, `config`, `sql_query`, `preview` |
| `TableColumnsResponse` | `table_name`, `columns`, `error`; `.failure()` reports a connection error in the payload so the join builder shows it beside the row instead of the offcanvas being replaced mid-edit |
| `ChildToolOption` | `uuid`, `tool_name`, `query_mode`, `columns[]` — one tool the Nested Tools picker may offer. `columns` is empty when the tool's output cannot be known without running it (a SQL tool, or a builder tool selecting everything), and the form then takes a typed name |
| `ChildToolOptionsResponse` | `tools[]`, `error`; the body of `GET /tool-configs/child-options` |

`children_json` is the tools this one embeds — `[{"child_id", "child_column",
"parent_reference", "binding_mode", "value_alias"}]` — from the Nested Tools card. A
JSON array rather than repeated form keys for the same reason `config_json` is one
object: five parallel controls could arrive at different lengths, and a row would then
pair the wrong column with the wrong tool. The schema checks the shape and the count;
whether a tool *may* be embedded — same owner, same datasource, enabled, not a cycle,
within the depth caps — and whether its binding mode makes sense for the parent's
statement is `tool_chain_service.validated_children`, which is the only layer that can
see the other tools. See [TOOL_CHAINING.md](TOOL_CHAINING.md) and
[TOOL_CHAIN_ITERATION.md](TOOL_CHAIN_ITERATION.md).

`sql_params_json` is the same division for the values a SQL-mode statement asks the
assistant for — `[{"param", "type", "required", "description"}]`. Bounded here;
whether each name actually appears as a `:placeholder` in the statement, and whether
the type is one this application can coerce to, is
`tool_config_service.validated_sql_params`, which has the statement in hand and this
schema does not.

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
| `ChatbotCreateRequest` | `name`, `datasource_id` (optional — see below), `target_type`, `target_selection[]`, `allowed_origins`, `workspace_id`, `data_agent_id` | cross-field: a `file` selection must be a uuid, a `table`/`collection` selection an object name, a `datasource` target needs no selection at all, and an `agent` target needs a **data agent and no datasource** |
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

`datasource_id` on `ChatbotCreateRequest` is the clearest case in the codebase of a
**conditionally** required field, and of why that belongs in a model validator rather
than on the field. A widget reads either a datasource target it nominates, or an
attached data agent's tool configs (`target_type == "agent"`) — never both, because
two answers to "what can this reach?" would be resolved differently depending on
whether the agent happened to run. The rule spans three fields, so no single one can
express it; `check_target` owns all of it, including rejecting an `agent` target that
*also* carries a datasource rather than quietly dropping one of the two. See
[DEEP_AGENTS.md](DEEP_AGENTS.md).
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
| `SqlAssistCreateToolRequest` | + `data_agent_id`, `tool_name`, `description`, `query_mode`, `config_json`, `sql_query`, `preview`; the tables are the mixin's `table_names[]`, primary first |

`.echo()` returns strings deliberately: the partials put these into hidden inputs,
and a `None` would render as the text "None" and be posted back as a selection on
the next step. `table_names` is capped at the *reflection* limit so the schema can
never accept more tables than the schema-reader will read. The generated SQL is
bounded but not pattern-checked — whether it is safe to run is decided by
`sql_assist_service` against the reflected schema, and a regex over SQL would be a
false reassurance.

### query_test — `app/schemas/query_test/query_test_schemas.py`

One endpoint, two callers: the Tool Configs form and the Ask AI panel both post the
form they are holding to `/query-test` and swap the same verdict partial in. See
[QUERY_TEST.md](QUERY_TEST.md).

| Schema | Fields | Rules |
|---|---|---|
| `QueryTestRequest` | `datasource_id`, `table_names[]` (≤`MAX_REFLECTED_TABLES`), `query_mode`, `config_json`, `sql_query` (≤20000), `children_json` | deliberately the same fields the *save* posts, so the query that is tested is the query that will be stored — nested tools included, since testing a chained tool without its chain tests a different query. `query_mode` blank means `builder`, matching the form's default; an unknown mode is refused here |
| `QueryTestResponse` | `passed`, `message`, `columns[]`, `row_count` | what the partial renders. **No row values** — a passing test needs a row fetched, not displayed, and in the Ask AI panel showing one would break the promise that the panel never displays data |

The request arrives via `hx-include`, so it carries the rest of whichever form sent
it — the tool name, the description, the agent. Those are ignored (`extra="ignore"`
on every request schema), which is what lets one endpoint serve two panels without
either building a payload by hand. `table_names` is in `multi_fields` for the usual
reason, with an extra edge to it here: a test that silently covered one of four
tables would report a pass for a query that has not been tested.

Nothing here judges the query. The references inside `config_json` are checked by
`tool_config_service.validated_query_config` (which has the reflected schema in
hand), the statement by `validated_tool_sql`, and whether it *runs* is the
database's answer — which is the whole point of the feature.

### tool_graphs — `app/schemas/tool_graphs/tool_graph_schemas.py`

One selection in, two drawings out. The read-only canvas over tool chains and their
joins — see [TOOL_GRAPHS.md](TOOL_GRAPHS.md).

| Schema | Fields | Rules |
|---|---|---|
| `ToolGraphQuery` | `workspace`, `agent`, `tool` | all optional, and all three may arrive together: the page keeps the branch above a selection expanded and a deep link can carry the whole path, so the *service* picks the most specific rather than the schema refusing the combination. Nothing selected is not an error — it is the state the page opens in |
| `ToolGraphNode` | `key`, `kind`, `label`, `datasource`, `query_mode`, `is_enabled`, `agent_name`, `layer`, `row` | `key` is a tool's public uuid or the literal `start`/`end`; **no internal id is ever on the wire**, which is what lets a node be clicked back into a Tool Configs link. `layer`/`row` are the computed position as plain integers — see below |
| `ToolGraphEdge` | `source`, `target`, `kind`, `label` | `label` is `child_column → parent_reference`, empty on the `START`/`END` connectors, which carry nothing |
| `ToolGraphResponse` | `scope_label`, `nodes[]`, `edges[]`, `error` | `.failure(message)` for an empty canvas plus the reason |
| `JoinView` | `type`, `type_label`, `left_table`, `left_column`, `table`, `right_column` | `type` picks the shaded region, `type_label` is the SQL keyword it stands for |
| `ToolJoinsView` | `tool_uuid`, `tool_name`, `query_mode`, `base_table`, `tables[]`, `joins[]`, `note` | `note` is filled exactly when `joins` is empty, and the two empty cases are different sentences: a builder query over one table has nothing to intersect, a SQL tool has a statement this application does not parse |
| `ToolJoinsResponse` | `scope_label`, `tools[]`, `error` | `.failure(message)` |

One query schema for both endpoints, deliberately. The toggle above the canvas
changes how a selection is drawn and never what is drawn, so two schemas that parsed
the selection differently could put two different sets of tools under one heading.

`layer` and `row` are positions computed by `tool_graph_service`, not pixels. Layout
is the part of a drawing that can be wrong without looking wrong, and keeping it on
the Python side is what makes it assertable — there is no JavaScript test harness in
this repository.

Both endpoints return **200 with `error` set** rather than raising, including for a
malformed uuid: the canvas is a panel inside a page the user is clicking through, and
one failure path that swapped in an error page would be the surprising one.
`ChildToolOptionsResponse` answers the same way.

### graph_designer — `app/schemas/graph_designer/graph_designer_schemas.py`

The authored canvas, its runs and its log — see
[GRAPH_DESIGNER.md](GRAPH_DESIGNER.md).

| Schema | Fields | Rules |
|---|---|---|
| `GraphCreateRequest` / `GraphRenameRequest` | `name`, `description` | the description is editable from both, because it is what a model reads when the graph is attached to an agent — a property of the graph rather than of the drawing |
| `GraphSetActiveRequest` | `is_active` | publishing validates the drawing; the toggle can therefore be refused |
| `GraphAttachRequest` | `data_agent_id` | optional — blank means detach, and unattached is the ordinary state |
| `GraphSaveRequest` | `nodes[]`, `edges[]` | `extra="allow"`, so a viewport or a dock height the canvas carries survives. **No node or edge ceiling** — see below |
| `GraphRunRequest` | `scope`, `node_ids[]`, `inputs` | ids are trimmed and de-duplicated (selecting a node twice is a click, not an instruction to run it twice); `selection()` returns `None` for a full run and a list for a selection, because the run row's nullable column means *everything* versus *exactly these* |
| `GraphResumeRequest` | `answer` | untyped beyond a length cap: what a valid answer *is* depends on the node's `expects`, which only the service holds |
| `GraphView` | `uuid`, `name`, `description`, `is_active`, `node_count`, `edge_count`, `agent_id`, `agent_name`, `updated_at` | the counts come from the stored document — two `len()` calls over JSON already in memory |
| `GraphRunStepView` | `sequence`, `node_id`, `node_type`, `node_label`, `iteration`, `status`, `duration_ms`, `message`, `output_preview`, `state_preview` | every preview is **already capped** by the service before the row was written, so this declares the contract rather than trimming anything |
| `GraphRunView` | `status`, `scope`, `selected_nodes[]`, `interrupt_payload`, `result_preview`, `error_message`, `steps[]` | one shape for the SSE frame **and** the polling body, so a client that lost its stream does not have to understand a second payload. Every frame is a whole state, never a delta |
| `GraphNodeOptionsResponse` | `datasources[]`, `tool_configs[]`, `data_agents[]`, `human_expects[]`, `error` | `.failure(message)` — a 200 with the reason, the contract `ChildToolOptionsResponse` established |
| `GraphSaveResponse` | `saved`, `message`, `node_count`, `edge_count` | `saved` is what the canvas keys its dirty flag off, carrying the same `data-success` values `flow_builder.js` already reads |
| `GraphRunStartedResponse` | `run`, `events_url`, `status_url` | both URLs are **relative paths**. An absolute URL built server-side is what goes stale when a tunnel rotates — see the note in [DOWNLOADER_AGENTS.md](DOWNLOADER_AGENTS.md) about `API_BASE + url` |

**The node vocabulary is deliberately not pinned here.** `graph_service.validate_graph`
owns it, because that is the version the compiler and the runners read a graph through, and
declaring the node types in two places would mean two edits every time one is added. So
`GraphSaveRequest` checks only what can be decided without the vocabulary: the body is a
JSON object carrying two collections.

**And there is no node or edge cap**, which is the one place this package deliberately
differs from `flow_builder`'s. `FlowGraphSaveRequest` bounds a conversation flow at 500
nodes because a flow that large is a runaway client; a data pipeline is not. What bounds a
*run* is the per-loop iteration ceiling in `graph_compiler` — a bound on work rather than on
drawing, and the one that actually protects anything.

### deep_agents — `app/schemas/deep_agents/deep_agent_schemas.py`

| Schema | Fields | Rules |
|---|---|---|
| `AgentOptionsQuery` | `workspace_id`, `selected`, `field_name`, `required` | a blank `workspace_id` lists **every** agent, not none — `data_agents.workspace_id` is nullable, so an unassigned agent has to stay pickable. `.select_name` falls back to `data_agent_id`. `required` drops the "no agent" option for a host that cannot accept one, and must ride through the cascade URL or it would come back on the first workspace change |
| `DeepAgentAskRequest` | `question` | required, ≤2000 — the same cap Ask AI and AI Analytics use, so a person typing into a console and a person typing into a prompt box don't hit two different limits |
| `AgentAskStreamQuery` | `question` | the same question and the same cap, off the **query string**: `EventSource` can only issue a GET, so the streaming console endpoint cannot take a form body. A separate class rather than a shared one because the source genuinely differs, and `QueryRequest` requires every field to default — so the empty case is caught by `min_length` rather than by absence |

---

### downloader_agents — `app/schemas/downloader_agents/downloader_agent_schemas.py`

The one place in this application where a **language model** supplies a request payload.
That is why the tool-argument classes are here and not inline in the tool factory: a model
is exactly as untrusted as a browser — it invents uuids, proposes formats that do not
exist, and passes the word "latest" — so its arguments go through the schema layer with
every other request. See [DOWNLOADER_AGENTS.md](DOWNLOADER_AGENTS.md).

Their field `description=` text is part of the prompt the model reads, which is why it is
written as instructions to a reader rather than as notes to a developer.

| Schema | Fields | Rules |
|---|---|---|
| `ConfirmDownloadArgs` | `export_id`, `file_format` | both default, so a model calling the tool with `{}` still produces a valid request — the common case, where the user said "yes" and nothing else. `wants_latest()` recognises `""`/`latest`/`last`/`previous`/`recent`/`none`, because that is what a model that did not keep the id actually sends. `file_format` maps the near-misses that unambiguously mean one of ours (`xlsx`/`excel`/`spreadsheet`/`sheet` → `xls`, `pq` → `parquet`) and refuses anything else **by name**: silently writing a CSV for someone who asked for Parquet is worse than saying no |
| `DownloadStatusArgs` | `export_id` | same defaulting and the same `wants_latest()`. A user asking "is it ready yet?" has given the model nothing to identify the export with |
| `PublicDownloadQuery` | `key`, `session_token` | both optional here and both **required by the route** — this pair *is* the authorisation on an unauthenticated path, and a key without a token would let any visitor of a public widget read every export ever produced for it. Blank arrives as `None` rather than `""` so that check reads clearly |
| `DownloadExportView` | `uuid`, `status`, `file_format`, `file_name`, `total_rows`, `count_is_lower_bound`, `part_count`, `rows_written`, `byte_size`, `error_message`, `created_at`, `expires_at`, `download_url` | `download_url` is **not** a column: the same export is reachable at two prefixes depending on who is asking, so `.of(export, url)` takes it and this schema only carries it. `None` until the export is ready, which makes "is there a link yet?" a question about the payload |
| `DownloadProgressEvent` | `event`, `export_id`, `status`, `part`, `of`, `attempt`, `rows_written`, `total_rows`, `message`, `download_url`, `file_name`, `byte_size` | one per completed batch, so deliberately flat and small — an export of a hundred thousand records emits two thousand of them. `event` is the SSE event name *and* a field, from one value, so a browser switching on `event.type` and one reading the payload cannot disagree. The four names are class constants (`PROGRESS`/`RETRY`/`READY`/`FAILED`) so the route and the tests spell them once. `file_name` and `byte_size` are set on the `ready` frame only, because that is when they first exist — a client watching since the build was queued has never seen either, and without them renders a finished file as an unnamed one of unknown size |
| `DownloadNoticeView` | `uuid`, `status`, `file_format`, `file_name`, `total_rows`, `rows_written`, `byte_size`, `error_message`, `download_url`, `progress_url`, `status_url` | the export **one reply is about**, serialised into `ChatbotTurnResponse.download` so the widget can draw a real download button and a live progress bar instead of leaving the model to write a URL into prose that renders as plain text. Smaller than `DownloadExportView` and different in kind: that one answers "tell me everything about this export" for a status call, this one answers "what goes under this message?" — which is why it carries `progress_url` and `status_url`, the two a status call has no use for. All three URLs are supplied rather than derived, and all three carry the asker's own scope |

`ChatbotTurnResponse.download` holds that payload as a plain `dict`, for the same reason
`table` does: so the chatbot schemas need not import another feature's package. It is
`None` on the overwhelming majority of turns and the widget renders nothing when it is.

---

### agent_recursive_dataframes — `app/schemas/agent_recursive_dataframes/aggregate_schemas.py`

The second place a language model supplies a request payload, and the first where it
supplies a whole *plan* rather than a few arguments. `AggregationPlan` is the structured
output of an LLM call, which makes it a request however it arrives: a model naming a column
that does not exist is the expected case here, not the exceptional one. See
[AGENT_RECURSIVE_DATAFRAMES.md](AGENT_RECURSIVE_DATAFRAMES.md).

The division of labour is worth stating, because neither half is sufficient alone: **the
schema bounds the shape, the planner bounds the meaning**. Pydantic can say "at most four
group columns" but not "`regoin` is not a column this tool returns" — and only one of those
is the mistake a model actually makes. So the column names are checked in
`aggregate_planner.validate_plan`, against what `probe_tool_query` says the tool really
returns.

| Schema | Fields | Rules |
|---|---|---|
| `PlannedAggregation` | `type`, `column`, `alias` | `type` is refused here for anything without an exact partial fold, and the rule is *asked of* `partial_algebra.unsupported_function` rather than restated — this layer knows a median is unavailable, not why. `column` defaults blank because `count` alone is meaningful and `count(col)` is a different question. `alias` is optional because it is **assigned by the planner, never taken from the model**: an alias is an output column name, and one colliding with a group key would silently overwrite it |
| `AggregationPlan` | `group_by`, `aggregations`, `filters`, `mode`, `unsupported`, `reason` | caps of 4 group columns and 8 measures, enforced by `max_length` rather than by a validator. Repeating a group column is refused: not a mistake a person makes, but one a model makes, and it would leave the sort order and the carried field names both depending on which copy is read. `unsupported` is how the model **declines** — given a request it cannot express and no way to say so, a model will produce a plan that is shaped right and answers a different question, which is the worst available outcome. `aggregations` may be **empty when `filters` is not**: that asks for the matching records themselves rather than numbers over them, and `mode` records which of the two the planner decided so nothing downstream has to infer it from an empty list. `mode` is assigned by the planner, never taken from the model — the same rule an alias follows |
| `PlannedFilter` | `column`, `part`, `operator`, `value`, `values` | **Every value is a `str`, and that is imposed from outside.** This schema is sent to a provider as `response_format`, and a field typed `Any` renders as an empty schema `{}` which a strict validator rejects outright — `400 Unsupported JSON schema fields in schema with keys: dict_keys([])`, which was Cerebras refusing every planning call the moment filters existed. A union would render as `anyOf` and is refused by several validators too, so one concrete type is the only shape that travels everywhere. Safe because the value is coerced against the **column's real dtype** in `frame_ops._coerced` and refused with the column named when it cannot be — which is better than trusting the model's JSON types anyway. `value` and `values` are two fields because a model handed one for both puts a single-element list where a scalar belongs about half the time, and `in: 5` then cannot be told from a typo; `""` therefore means *no value given*, and emptiness is asked for with `is_null`. `part` is what makes "in March" expressible without the model doing month-boundary arithmetic |
| `AggregateRecordsArgs` | `instruction`, `tool_name` | the agent tool's `args_schema`. `tool_name` is optional and, when given and resolvable, is what makes the common case cost no LLM call at all — the routing prompt already lists every tool by name. The `description=` text is part of the prompt the model reads, so it is written as instructions to a reader |
| `AggregateRunRequest` | `agent_id`, `tool_id`, `instruction` | the console form. `tool_id` is optional and blank means "let the instruction decide", which is what the agent does when it calls this itself |
| `AggregationResultView` | `tool_name`, `tool_id`, `datasource_name`, `summary`, `columns`, `rows`, `group_count`, `records_read`, `total_records` | `group_count` is separate from `len(rows)` deliberately and is the number that must be reported: the rows are capped at the tool row limit and the group count is not, so 200 rows out of 4,821 groups has to say so rather than reading as the whole answer. `.is_capped` decides that in one place instead of leaving a comparison written into the markup |

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
