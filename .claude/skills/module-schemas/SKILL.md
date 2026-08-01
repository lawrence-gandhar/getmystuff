---
name: module-schemas
description: Build and maintain the Pydantic schema layer for GetMyStuff — one app/schemas/<feature>/ package per module, every request parsed through a request schema and every response built from a response schema, with validations, docs and tests. Use when a new module/feature/route is added, when a module has no schemas, when the user asks to "add schemas", "validate the payload", "check schema coverage", or after any change to a route's request or response shape.
version: 1.0.0
---

# Module Schemas

Every module gets a Pydantic schema package. Nothing untrusted reaches a service
without passing a request schema first, and nothing reaches the client that was
not built from a response schema.

Run this whenever a module is added or changed. The audit script decides what is
outstanding; you fix what it names.

Read [documentations/SCHEMAS.md](../../../documentations/SCHEMAS.md) for the
schema catalogue and the design rationale behind the base classes.

## Non-negotiables

1. **Never relax a validation to make something pass.** If a payload is rejected,
   either the payload is wrong or the rule is wrong. Widening a pattern, raising a
   length cap, or switching a required field to optional is an application change
   that needs its own justification — not a way to get a green audit.
2. **Never add a feature to `NO_PAYLOAD_FEATURES` in the audit script** to silence
   verdict 1. That list is for routes that genuinely read nothing from the
   request, and the script re-checks the claim on every run.
3. **Never suppress a raw payload read** with `# audit: raw-payload-ok` unless the
   read is genuinely un-schemable *and* you write the reason on the same line.
   Multipart file objects are the only case that qualifies today.
4. **Validation errors must stay `HTTPException` with a human-readable detail.**
   The whole application renders `e.detail` straight into a Bootstrap alert. A
   raw Pydantic `ValidationError` reaching a route means a user sees
   `1 validation error for X / field / Value error, ...`. Always parse through the
   `app.schemas.base` helpers, which do the conversion.
5. **The script owns the numbers.** Class counts, coverage and the verdict come
   from `audit_schemas.py`. Quote what it printed; never type a count from memory.
6. **Documentation is part of the change, not a follow-up.** A schema that is not
   in `SCHEMAS.md` fails the audit at verdict 3. Update the doc in the same pass.

## Procedure

### 1. Audit

```bash
python3 .claude/skills/module-schemas/scripts/audit_schemas.py
echo "verdict: $?"
```

The verdict is the work queue, and the codes are ordered by what has to be fixed
first:

| Code | Meaning | What to do |
|---|---|---|
| `0` | clean | nothing outstanding — stop |
| `1` | a feature with payloads has no schemas package | go to step 2 |
| `2` | a route still reads a raw payload | go to step 3 |
| `3` | schemas exist that `SCHEMAS.md` does not document | go to step 4 |
| `4` | schema modules with no test file | go to step 5 |

Fix the named condition, then re-run. Every invocation should move the verdict
closer to `0`.

### 2. Create the missing schema package

For feature `<feature>`:

```
app/schemas/<feature>/
├── __init__.py              re-exports every public schema
└── <feature>_schemas.py     the schemas themselves
```

The `__init__.py` re-export is not optional — it is the same rule `models/` and
`routes/` follow, and it is what lets callers import
`from app.schemas.<feature> import XCreateRequest` without knowing the module
name. Mirror [app/schemas/datasource/__init__.py](../../../app/schemas/datasource/__init__.py).

Then read the feature's routes and write one schema per payload:

- **One request schema per handler that takes input.** Name it after the handler:
  `WorkspaceCreateRequest`, `ToolConfigUpdateRequest`, `PublicChatbotMessageRequest`.
- **One response schema per distinct response body.** `WorkspaceView`,
  `KnowledgeBaseStateResponse`, `ChatbotTurnResponse`.
- Inherit from `FormRequest`, `JsonRequest`, `QueryRequest` or `ResponseSchema` in
  [app/schemas/base.py](../../../app/schemas/base.py) — never from `BaseModel`
  directly. The base classes carry the strip-whitespace config, the `extra`
  policy appropriate to that source, and the `from_form` / `from_json` /
  `from_query` constructors that convert failures into `HTTPException`.

Rules that are easy to get wrong here, all of them load-bearing:

- **Public identifiers are `uuid`, never the bigint `id`.** A request schema
  accepts `uuid.UUID`; a response schema exposes `uuid`. Putting an `id` in a
  response schema is the bug CLAUDE.md's identifier section exists to prevent.
- **Give every field a `title`.** The base class builds its user-facing messages
  from it — `title="Datasource name"` becomes "Datasource name is required".
  Without a title the user sees the raw field name.
- **An unselected `<option value="">` is `None`, not `""`.** Use
  `OptionalUUID` / `OptionalText` from `app.schemas.base` so a blank dropdown
  clears the column rather than storing an empty string.
- **A multi-select needs `multi_fields`.** `from_form(form, multi_fields=("table_names",))`
  — a plain read takes the first value only and silently queries one table when
  the user picked four.
- **Reuse the validators.** `app/utils/validators.py` already owns the identifier,
  object-name, UUID and JSON-object rules with the exact wording used everywhere
  else. Wrap those in your validators; do not restate the regex.
- **Don't duplicate a service's business rule.** A schema validates *shape and
  format* — required, length, pattern, type, enum membership. Whether a name is
  already taken, whether the user owns the row, whether the datasource is
  reachable: those stay in the service, because they need the database.

### 3. Wire the route

Replace the raw reads. The shape is always the same:

```python
@post("/create")
async def create(self, request: Request, db: AsyncSession, user: User) -> Template:
    error = None
    try:
        payload = await WorkspaceCreateRequest.from_form(request)
        await workspace_service.create_workspace(db, user.id, payload)
    except HTTPException as exc:
        error = str(exc.detail)

    return await self._rows(db, user, error)
```

`from_form` raises `HTTPException(400, "…")` on a bad payload, so the existing
`except HTTPException` already handles validation failures — the route keeps the
error-rendering it had, and gets validation for free.

For a JSON endpoint, build the body from a response schema:

```python
return Response(
    KnowledgeBaseStateResponse.model_validate(state).model_dump(mode="json"),
    media_type="application/json",
)
```

`mode="json"` matters: it turns `UUID` and `datetime` into strings, which the raw
dict path was doing by accident before.

Service signatures take the schema object rather than a bag of keyword strings.
That is what makes the validation unbypassable — there is no longer a way to call
the service with an unvalidated name.

### 4. Update the documentation

`documentations/SCHEMAS.md` is the catalogue. For each feature it lists every
schema, its fields, and the rule each field enforces. Add the new schemas to the
feature's section — or add the section if the feature is new — and link the
feature's deep-dive page if it has one.

The audit greps `SCHEMAS.md` for every class name it found in `app/schemas/`, so a
schema you forget to write up fails at verdict 3. Keep the table format already
used there; it is what makes the doc diffable when a field changes.

### 5. Write the tests

One test module per schema module, mirroring the source layout:

```
tests/unit/schemas/<feature>/test_<feature>_schemas.py
```

Add an `__init__.py` to any new test directory. Test the boundary, not the happy
path alone:

- every rejection produces an `HTTPException` whose `detail` is a sentence a user
  could act on — assert on the message, since that message is the product
- normalization actually happened (trimmed, lowercased, `""` became `None`)
- a multi-select with several values keeps all of them
- a field absent from the form behaves as the schema declares, rather than
  raising `AttributeError` from a validator that assumed a string

`tests/unit/schemas/datasource/test_datasource_schemas.py` is the reference for
the parametrized style. The `full-test-coverage` skill will pick these up on its
next run.

### 6. Re-run the audit

Back to step 1. Stop at verdict `0`.

## Reporting back to the user

State the verdict the script printed, which features gained schemas, how many raw
payload reads were removed, and the path to the updated documentation. If you
left a raw read in place, name it and say why it cannot be schema'd. If a
validation you added rejects input the application previously accepted, call that
out separately — it is a behaviour change, and it is the most likely thing to
surprise someone.
