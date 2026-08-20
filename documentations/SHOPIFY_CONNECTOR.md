# The Shopify connector

Reading orders, products and customers out of a Shopify store through the Admin GraphQL
API — and the four things the integrations runtime had to learn before it could.

This is a companion to [INTEGRATIONS.md](INTEGRATIONS.md), which describes the engine. Read
that first if you have not: everything here assumes the connector layer, the send path and
the paging walk it describes.

---

## What it is

| | |
|---|---|
| **Connector id** | `shopify` |
| **API** | Admin GraphQL, `POST /admin/api/{version}/graphql.json` |
| **Direction** | **Read-only.** Three operations: `orders`, `products`, `customers` |
| **Authentication** | A custom app's Admin API access token, in `X-Shopify-Access-Token` |
| **Address** | Computed from the shop domain. Never typed |
| **Paging** | Cursor, in the request body's `variables` |
| **Files** | `app/services/integrations/connectors/shopify/{connector,documents,hooks}.py` |

### Read-only is a property, not a stage

The connector declares **no write operations**, so `writable_operations()` returns an empty
tuple and a `connector_write` node cannot select it — the palette for that node is built
from exactly that list. There is a test asserting the empty tuple, because a property
nothing checks is a habit.

The reason is Shopify's own. Its mutations take no idempotency key. A `POST` that times out
*after reaching the server* has already happened, and retrying it creates a second record
in a real merchant's real store. No amount of backoff fixes that; what fixes it is looking
up the natural key in `integration_sync_keys` before every create and switching to an
update when it is found. That is worth building and it is not what a first vendor connector
should be proving.

### Why a custom app token and not OAuth

The merchant creates a custom app inside their own Shopify admin, grants it the scopes they
want, and copies the Admin API access token — a `shpat_…` string. It goes in an
`X-Shopify-Access-Token` header, which the existing `AUTH_API_KEY` path already sends
correctly, so this connector needed **no credential code at all**.

Offline tokens do not expire. `IntegrationCredential.expires_at` stays null,
`token_service.needs_refresh` returns false, and none of the refresh machinery ever runs.

What OAuth would add, and what is already there for it, is in
[the OAuth section](#what-oauth-would-add) below.

---

## The four things that had to change first

`rest_generic` was the only connector the runtime had ever run, and it is the one connector
that exercises none of the vendor-connector machinery: it supplies its own base URL,
defines its own operations, and has no hooks. Three seams built in Phase 1 for exactly this
moment had **never executed once**. Two of them were broken.

Everything below is a general runtime capability. None of it mentions Shopify. A
`if connector_id == "shopify"` in `runtime/` would fork precisely the code that the whole
Phase 1 suite covers, and the module's stated thesis is one request path shared by the
vendor connectors and the user-authored ones.

### 1. `ResolvedTarget.base_url` raised `AttributeError`

```python
# app/services/integrations/nodes/connector_nodes.py, before
return str(self.connection.base_url or self.connector.base_url or "")
```

`ConnectorSpec` has no `base_url`. It has `base_url_template`. `rest_generic` never reached
the second operand because `base_url_is_user_supplied=True` guarantees the first is set —
so the bug sat there, unreachable, until the first connector that needs it. `{account}` had
no substitution site anywhere in the codebase, and `ConnectorHooks.resolve_base_url` was
never called.

**Now:** `ConnectorSpec.render_base_url(connection)` tries three sources in order — the
connector's hook, then `base_url_template` with the account id substituted, then whatever
the connection stored. The order is the point: putting the stored value **last** is what
stops a typed URL overriding a computed one, which would undo the entire reason a vendor
connector refuses a user-supplied address.

When nothing resolves it raises with a sentence naming the connection, rather than
returning `""` and letting the egress guard reject the request for a reason that describes
the URL's shape instead of the connection that caused it.

### 2. A GraphQL document could not survive `build_request`

Any string leaf containing `{` that is not *exactly* `{name}` goes to `_substitute`, which
reads the first `{…}` span as an input name. A GraphQL query is nothing but braces, so an
operation carrying one was refused before a request was ever built. There was no escape
syntax.

**Now:** `OperationSpec.body_literals` names top-level body keys copied across verbatim.
Shopify sets `body_literals=("query",)`; the `variables` sibling still substitutes
normally, which is how the cursor, the page size and the filter get in.

Three alternatives were considered and rejected:

- **`{{`-escaping.** Doubling every brace in a thirty-line document is unreadable, and one
  brace missed is not an error — it is a silently different query.
- **Putting the document in an operation input.** Then the document is not part of
  `canonical()`, and an operation whose fingerprint does not cover its own query text makes
  the replay-determinism claim false.
- **A GraphQL-shaped `OperationSpec`.** A vendor-specific bulge in the shared spec, for a
  problem that is really "this string is a literal".

`body_literals` is inside `canonical()`, so the document is inside the fingerprint. A
declared literal name that matches no body key is refused at import.

**Not available to user-authored REST operations yet.** `integration_rest_operations` has
no `body_literals` column and this change added no migration. `_as_mapping` lists the name,
so a row simply yields `()` and behaves exactly as before; adding the column later is a
migration and nothing else.

### 3. The cursor could not reach a request body

`PAGE_CURSOR` writes into `walk.params`, which becomes the query string. Shopify's `after:`
belongs in the POST body's `variables`. `before_request` cannot help —
`assert_hook_kept_the_target` explicitly refuses a hook that changes `json_body`.

**Now:** a seventh page kind, `PAGE_INPUT_CURSOR`. The cursor is supplied as one of the
operation's **declared inputs**, so the operation's own templates decide where it lands —
body, header or query. That is strictly more general than a `body_cursor` kind, and it
needed no `extra_body` plumbing through `build_request` at all.

`PageWalk` gained `arguments` beside `params`; a kind writes to exactly one of them. The
repeated-cursor guard is the *same* `seen_cursors` set, shared deliberately: a vendor
handing out one token forever does not care which carrier it travelled in, and two guards
would be two places to forget.

`OperationSpec.validated()` cross-checks that `param` and `size_param` name declared
inputs. Without that check a typo is not an error — `build_request` drops undeclared
arguments silently, so every page would be a fresh request for page one, read as new
records each time, until the repeat guard finally tripped several thousand duplicated
records later.

### 4. A GraphQL failure is an HTTP 200, and nothing looked at it

**This is the one that mattered most.** Shopify answers a missing access scope with:

```json
{ "errors": [{ "message": "Access denied for orders field. Required access: `read_orders`.",
               "extensions": { "code": "ACCESS_DENIED" } }],
  "data": null }
```

under a `200 OK`. `ReadResponse.ok` is purely `200 <= status < 300` and `sender` only
raises on non-2xx, so the failure flowed straight through: `read_records` found `None` and
returned `[]`, `pagination.advance` stopped with *"the last page was empty"*, and the run
ended **green with zero records**.

A store that refused us and a store with no orders looked identical. That is the worst
shape a failure can take, because it is the one nobody investigates.

**Now:** `ConnectorHooks.after_response(read, operation, context)`, called in `_send_once`
between the rate-limiter observation and the status check. **The hook may only raise; its
return value is discarded.** That is the fence on this side, mirroring
`assert_hook_kept_the_target`: a hook able to rewrite a response could make the recorded
step disagree with what the vendor actually sent, and an audit trail editable by the thing
it audits is not one.

`throttle_from_response` was **removed** from the protocol at the same time. It had zero
implementations and zero callers, and its job — correcting the bucket from a response —
belongs in `after_response`, which is a side effect on the limiter rather than a change to
the response. Two hooks that both fire after a response where only one is wired is exactly
the drift this design avoids elsewhere.

`classify_error` remains declared and **still unwired**. It is commented as such. Do not
implement it expecting it to fire.

---

## The shop domain is the security control

`external_account_id` holds `your-store.myshopify.com`. It is user-supplied text that
becomes **the host of a request carrying the merchant's access token**. An unconstrained
one hands both the destination and the credential to whoever typed it.

```python
SHOP_DOMAIN_PATTERN = r"[a-z0-9]([a-z0-9-]{0,58}[a-z0-9])?\.myshopify\.com"
```

Matched with `re.fullmatch`, so a pattern that forgot its anchors cannot be prefixed or
suffixed past. Deliberately narrow: lowercase only (Shopify's own domains are, and mixed
case would mean two connections for one shop), no port, no path, no userinfo, no scheme,
no subdomain.

**Checked in two places, and the duplication is deliberate:**

| Where | Why |
|---|---|
| `connection_service.create_connection` | So the person typing gets a sentence rather than a failed run. Refuses **before** the row is written |
| `ConnectorSpec.render_base_url` | So the *request* is safe even if some future code path writes the column without going through a form |

Only the second is load-bearing for security. Only the first is any use to a human. The
browser gets the pattern as an HTML `pattern` attribute too, which is the frontend half
CLAUDE.md asks for — a convenience, trusted by nothing.

The test for this is table-driven and the case that matters is
`demo-store.myshopify.com.evil.com` — the one a "contains `myshopify.com`" reading lets
through.

There is also a test that a `base_url` written directly onto a Shopify connection row is
ignored in favour of the computed one.

---

## Rate limiting, honestly

Shopify's Admin API limit is a **points bucket per shop**. Not per app — *per shop*. Every
other app the merchant has installed draws on the same allowance, so a locally-computed
requests-per-second rate is always optimistic, and the only real number available arrives
in every response:

```json
"extensions": { "cost": { "requestedQueryCost": 500,
    "throttleStatus": { "maximumAvailable": 2000.0,
                        "currentlyAvailable": 100.0, "restoreRate": 100.0 } } }
```

`ShopifyHooks.after_response` feeds that to `Bucket.apply_vendor_view`, which **only ever
lowers** the local view. That direction is what makes accepting a number from the far end
safe.

The correction runs **before** the failure is raised. Getting that the other way round
would mean the one response that knows the bucket is empty is the one whose reading is
discarded.

A `THROTTLED` error is retryable, and the wait is computed from Shopify's own numbers
rather than left to the retry engine's fixed backoff — 500 points needed against 100
available restoring at 100/s is four seconds, and `0.5 → 1.0 → 2.0` spends three requests
discovering that. Clamped at 60 seconds.

### The known gap: no cost dimension

**This is not cost accounting.** `rate_limiter.acquire` spends one token per request
whatever the query costs, so a 900-point products query and a 5-point query are charged
alike. The response correction plus the `THROTTLED` retry absorbs the difference in
practice, but a workflow reading products with deep variant nesting can exceed the bucket
on a single query and will rely entirely on the retry.

The honest fix is a points dimension on the bucket, plus a declared or measured cost per
operation. It is not done. `RateLimitSpec.usage_header` is likewise declared and never
read — that predates this work.

---

## Error codes and what they mean here

| Shopify `extensions.code` | Treated as |
|---|---|
| `THROTTLED` | Retryable, with a wait computed from `throttleStatus` |
| `ACCESS_DENIED`, `UNAUTHENTICATED`, `FORBIDDEN` | **Permanent.** A missing scope stays missing; eight backoffs is eight requests spent proving what the first one said |
| `MAX_COST_EXCEEDED`, `SHOP_INACTIVE` | Permanent |
| Anything else | Neither retryable nor permanent — reported, not repeated |

**Errors beside partial data are still a failure.** Shopify returns both when a nullable
field is refused. Taking the rows would mean a sync that quietly dropped whichever field
the token could not read, which is the same silent-partial-success shape the whole
`after_response` seam exists to prevent.

The failure records **no status code**. A GraphQL error has no status of its own;
recording the 200 would be a lie in the audit and inventing a 4xx a worse one.

Vendor messages are de-duplicated and capped at three, because a run whose error message is
forty lines of GraphQL is a run whose error message does not get read.

---

## The operations

All three are structurally identical — same path, same method, same three inputs, same
paging rule shape. Only the document, the record path and the outputs differ. That is what
lets one page rule and one hook cover all of them, and what makes adding a fourth resource
a copy of a block rather than a new code path.

| | `orders` | `products` | `customers` |
|---|---|---|---|
| Records at | `data.orders.edges[*].node` | `data.products.edges[*].node` | `data.customers.edges[*].node` |
| Outputs | 22 | 16 | 18 |

Every operation takes three inputs, and **none is required**:

| Input | |
|---|---|
| `page_size` | Supplied by the paging walk. Shopify's maximum is 250; asking for more is an error, not a clamp |
| `cursor` | Supplied by the paging walk |
| `search` | Shopify's own filter syntax — `updated_at:>2026-08-01`, `financial_status:paid` |

**Why nothing is required.** `open_supply` builds one request *before* any walk exists,
purely to learn the origin URL for the same-origin check. A required `page_size` would make
that call raise for a value pagination was about to supply.

**Why the filter is called `search` and not `query`.** `query` is already the body key
holding the GraphQL document. Two different things under one word in one operation is a
trap for whoever edits it next. There is a test asserting no operation declares an input
named `query`.

The `search` input is also what makes incremental reads a configuration change rather than
new operations, once `integration_cursors` is wired.

**Money is a connection in Shopify's schema, not a scalar**, so amounts arrive at
`…Set.shopMoney.amount` and each output's `FieldSpec.path` says so. They are typed `string`
rather than `number` on purpose: Shopify sends decimal strings deliberately, and parsing
them to a float here would put a rounding error into somebody's revenue figures before the
mapping layer ever saw them.

Each operation stays under the AI catalogue's 25-field ceiling, above which fields are
silently dropped from the model's view.

---

## The API version

```python
API_VERSION = "2026-07"
```

A **module constant, not an environment variable.** The version and the query documents are
one thing — a field that exists in one version and not the next is a changed document — so
a version the operator can move without moving the documents is a version that can silently
stop matching them. Bumping it is a code change with tests, which is the correct amount of
ceremony.

Shopify ships quarterly (January, April, July, October) and supports each release for about
twelve months. A deprecated field comes back as an error inside a 200, which
`after_response` now turns into a loud failure rather than an empty sync — so falling
behind is visible rather than silent. **There is no automatic warning before that point.**
The review is manual and belongs on a quarterly rhythm.

---

## Adding a Shopify connection

1. In the Shopify admin: **Settings → Apps and sales channels → Develop apps → Create an
   app**, then configure Admin API scopes — `read_orders`, `read_products`,
   `read_customers` — and install it.
2. Copy the **Admin API access token**. Shopify shows it once.
3. In GetMyStuff: **Integrations → Connections → New connection → Shopify**. The form
   swaps the API-address field for a **Shop domain** field; paste
   `your-store.myshopify.com` and the token.
4. **Test connection.** It calls `orders` with page one's arguments.

The form is driven by `registry.describe_connectors()` — the `account_id_*` fields ride on
each `<option>` as data attributes and `static/js/integration_connections.js` switches the
inputs. A hidden field is also `disabled`, so it is not submitted; if that script fails to
load, the shop-domain field stays hidden and the server refuses the connection with a
sentence. That is the correct way for it to break — nothing is created wrongly.

### Test connection sends page one's arguments, not none

`connection_service.test_connection` seeds `pagination.first_page_arguments(page_rule)`.
Shopify's documents declare `$first: Int!`, so a test that omitted the page size would fail
on **every** Shopify connection ever made — and fail with a message about a GraphQL
variable, which tells the owner nothing about their connection. Testing a read the way it
will actually be read is also the more honest test.

---

## What OAuth would add

Nothing here needs it, but the path is short and half of it exists.

**Already in place:** the `integration_oauth_states` table and its migration, including the
`state_hash` column that stores only `sha256(state)` so a database read cannot complete
somebody's install. The CAS refresh lock in `credentials/token_service.py`.
`credential_service` already sends an `AUTH_OAUTH2` access token correctly. `AuthSpec`
already carries `authorize_url`, `token_url`, `scopes` and `rotates_refresh_token`.

**Missing:** the authorize redirect, Shopify's `hmac` verification, marking the state
consumed **in the same transaction as the lookup and before the token exchange**, the
exchange itself, and an `IntegrationOAuthController`. Also note that `ensure_fresh_token`
has **zero callers** — an expiring online token would need it wired into `resolve_target`
as well. The ordering that matters is written up in
[INTEGRATIONS.md](INTEGRATIONS.md) under the OAuth callback.

---

## Out of scope, so it does not read as an oversight

- **Writes and mutations.** See above. Also needs `userErrors` handling — Shopify puts
  per-record validation failures inside a successful `data`, which is a *record*-level
  failure and must not fail the node.
- **The Bulk Operations API.** `bulkOperationRunQuery` is asynchronous: submit, poll,
  then download JSONL from a signed URL on a different host than the egress guard
  approved. It does not fit the paged-supply shape at all.
- **Webhooks.** No `app/uninstalled` handling, so an uninstalled app's connection stays
  `active` until a run fails against it. Mandatory GDPR webhooks are an app-review
  requirement for a *public* app; a custom app does not need them.
- **Incremental cursors.** `integration_cursors` exists and is still unwired. The `search`
  input is where a watermark will go.
- **True cost accounting.** See the rate-limiting section.

---

## Risks

1. **API version drift.** Manual, quarterly, with no warning surface. Mitigated only by
   the fact that a deprecated field now fails loudly.
2. **Rate limiting is per worker.** Under `uvicorn --workers N` the effective send rate is
   N×. Unchanged from the engine's existing risk, and more visible with a vendor whose
   bucket is shared with other apps.
3. **No cost dimension.** A deeply nested products query can exceed the bucket on one
   request and relies entirely on the `THROTTLED` retry.
4. **`spec.py` is shared.** `body_literals`, `PAGE_INPUT_CURSOR` and the protocol change
   are all additive with defaults, and the full suite was green before and after. Removing
   `throttle_from_response` was safe only because it had zero implementations.
5. **Not verified against a live store.** Everything here is tested against `respx` at the
   transport layer, which runs the real pooling, paging, retry and byte-cap code but not a
   real Shopify. The live acceptance steps are in the plan; the response shapes are taken
   from Shopify's documented ones.
