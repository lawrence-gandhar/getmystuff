# The Brevo connector

Reading contacts, lists, orders, products and categories out of a Brevo account and writing
all of them back into one — the first connector in this application that writes anywhere.

This is a companion to [INTEGRATIONS.md](INTEGRATIONS.md), which describes the engine, and
a sibling of [SHOPIFY_CONNECTOR.md](SHOPIFY_CONNECTOR.md), which describes the first vendor
connector. Read the first if you have not: everything here assumes the connector layer, the
send path and the paging walk it describes.

---

## What it is

| | |
|---|---|
| **Connector id** | `brevo` |
| **API** | Brevo v3 REST, `https://api.brevo.com/v3` |
| **Sections** | **Contacts** (`contacts`, `lists`, `create_contact`, `add_to_list`) and **eCommerce** (`orders`, `products`, `categories`, `upsert_order`, `upsert_product`, `upsert_category`) |
| **Direction** | **Reads and writes**, in both sections |
| **Authentication** | An account API key, in an `api-key` header |
| **Address** | Fixed. Not typed, not computed |
| **Paging** | Offset, from zero, stepped by what came back |
| **Rate limits** | **Per operation**, not per connector — see [Rate limits](#rate-limits) |
| **Files** | `app/services/integrations/connectors/brevo/connector.py`, `.../brevo/ecommerce.py` |

Brevo was formerly Sendinblue. The API is the same one; the hostname changed and the old
`api.sendinblue.com` is not used here.

### Two sections, one connector

Contacts and eCommerce are one connector rather than two because they are **one account
behind one key**. Splitting them would make an operator create the same connection twice
and keep two copies of the same credential in step, and give the Apps page two Brevo tiles
that mean the same thing.

What that costs is a single connector-wide send rate spanning endpoints whose published
limits differ by 180×. That cost is what `OperationSpec.rate_limits` exists to pay, and it
is the one piece of shared runtime this section of the connector needed — see
[Rate limits](#rate-limits).

The eCommerce endpoints answer **403 until the account switches the eCommerce app on** in
the Brevo dashboard. That is why `contacts` is declared first in `SPEC.operations`:
`connection_service._operation_to_test` picks the first *read* positionally, and a Test
button that happened to call `orders` would tell somebody with a perfectly good key that
their connection does not work.

### The simplest connection in the application

Brevo is one multi-tenant API at one hostname for every account. There is no shop domain,
no region, no instance and no tenant path — so:

* `base_url_template` is a literal and `base_url_is_user_supplied` is `False`
* `account_id_required` is `False`, and there is no `account_id_pattern` to check

Which means the connect dialog asks for **a name and a key**, and nothing else. That is
worth stating because it is the shape most connectors do *not* have, and the Apps page reads
it off the spec (`asks_for_nothing_else`) rather than hard-coding it.

The corollary is the security property: a stored `base_url` cannot override the fixed one.
`ConnectorSpec.render_base_url` consults the hook, then the template, and only then the
stored column — so a connection that arrived carrying an address (a restored database, a
connector that used to ask for one) still sends its Brevo key to `api.brevo.com` and nowhere
else. There is a test for exactly that.

### Authentication

The account holder generates a key in the Brevo dashboard under **SMTP & API → API keys**
and pastes it in. It goes out as:

```
api-key: xkeysib-…
```

No `Bearer` prefix. Brevo reads the whole header value as the key, so a prefix is sent as
part of it and answered with a 401 that reads exactly like a bad key — an afternoon of
debugging avoided by one `value_template` and one test.

Keys do not expire. `IntegrationCredential.expires_at` stays null, `needs_refresh` returns
false, and none of the refresh machinery runs. A key deleted in the dashboard answers 401,
which `sender` raises, `run_service` records, and the Connections page shows next to a
**Reconnect** button.

---

## Why this is the connector that writes

Shopify is read-only, and [its own page](SHOPIFY_CONNECTOR.md) explains why: Shopify's
mutations take no idempotency key, so a create that times out *after* reaching the server has
already happened and retrying duplicates a real order in a real merchant's store.

Brevo's contact create does not have that problem, because of one field:

```python
body_template = {
    "email": "{email}",
    ...
    "updateEnabled": True,   # a literal
}
```

`updateEnabled: true` makes `POST /contacts` an **upsert**: an address Brevo already holds
is updated rather than refused. That single value buys two things at once.

1. **Re-running a sync works.** Without it, Brevo answers `400 duplicate_parameter` for
   every contact it already has, so yesterday's sync run again fails on almost every record
   — while being, in every meaningful sense, a correct request.
2. **`idempotent=True` becomes true.** A create that timed out after reaching Brevo has
   already happened; the retry the engine is allowed to send lands on the *same* contact
   rather than creating a second one.

It is a literal and not an input on purpose. A node that left it unmapped would silently get
the other behaviour, and the operation's `idempotent` flag — which the retry rules read —
would be a claim the request could not support. It is also inside `canonical()`, so it is
part of the operation's fingerprint: a replay that dropped it cannot claim to be the same
operation.

`add_to_list` is idempotent for a simpler reason: adding a contact already on a list is not
an error and does not create a second membership, so a retried request leaves the same state
behind.

### What is deliberately absent

**Sending email.** Brevo's `POST /smtp/email` is a transactional send, and no property of it
can be made retry-safe: an email that went out cannot be un-sent, and every retry is another
copy in somebody's inbox. Sending mail is the [Email module's](EMAIL_DISPATCH.md) job. A
workflow step that quietly duplicated a customer's receipt is not worth the convenience of
having it here as well.

---

## The eCommerce section

`app/services/integrations/connectors/brevo/ecommerce.py`. Orders, products and categories
— three reads and three writes.

This is the half of Brevo a shop **pushes into**. Brevo's abandoned-cart automations,
product recommendations and revenue attribution run off this data and off nothing else, so
a workflow that syncs a storefront here is what turns the rest of the account on. It is
also the first genuine destination in the application: before it, the Shopify connector
could read a merchant's orders and there was nowhere for them to go.

### Every write is an upsert, by two different mechanisms

The same argument as `create_contact`, and worth repeating because the mechanism differs
per endpoint and the inconsistency is Brevo's, not ours:

| Write | Upserts because |
|---|---|
| `upsert_order` | `POST /orders/status` keys on the `id` you supply, natively. **No flag is sent** |
| `upsert_product` | `POST /products` answers 400 for a duplicate id *unless* `updateEnabled: true` |
| `upsert_category` | `POST /categories` — same |

So two of the three carry a literal `"updateEnabled": True` in `body_template` and one
deliberately does not. Adding the flag to the order write for symmetry would be sending a
field Brevo does not document for that endpoint, and a field the vendor does not document
is a field whose meaning can change under us.

All three declare `idempotent=True`, and in each case the upsert is what *earns* it — that
flag is what permits the engine to retry a write after a read timeout that may already have
arrived. A test asserts the pairing directly: every write in this file must either post to
`/orders/status` or carry the flag.

### The line-item array

`upsert_order` sends the only nested structure in this connector:

```python
"products": "{products}",   # input typed `json`
```

A leaf that is *exactly* a placeholder keeps its type through `request_builder._fill`, and
`type_coercion` returns a `dict`/`list` unchanged — so a mapped array of line items goes
out as a real JSON array. As the string `"[{...}]"` Brevo would reject the order and
nothing in the resulting message would say why, which is why there is a test on the
serialised body rather than only on the structure.

What this connector **cannot** check is what is *inside* that array. Brevo requires every
line to carry `productId`, `price` and one of `quantity`/`quantityFloat`, and rejects the
whole order if one line is short a field. Those are mapped values inside a JSON blob, not
declared inputs, so a **`validate` node upstream of the write** is where that check
belongs.

### Money is text

`amount` on an order and `price` on a product are declared `string`, not `number` — the
same call [the Shopify connector](SHOPIFY_CONNECTOR.md) makes, for the same reason. A
merchant's revenue must not round-trip through binary floating point on its way between two
systems that both had it right.

### What a full sync looks like

Categories, then products, then orders — in that order, because Brevo does not invent a
category from a product that mentions one, and an order's line items refer to product ids.

Nothing enforces the ordering; it is three workflows or three branches of one, and getting
it wrong produces products filed under nothing rather than an error.

### An updated record answers 204 with no body

`POST /products` and `POST /categories` return `201` and `{"id": …}` when they *created*
something and a bare `204` when they *updated*. Both are successes.

The consequence is in `connector_nodes._target_id`: on a 204 there is no id to read, so no
dedupe sync key is written for that record. That is the existing intended behaviour — "a
key pointing at an id we guessed is worse than no key" — and it is harmless here, because
the natural key *is* the id we sent.

---

## Paging

All five reads page by **offset**, because Brevo offers nothing better — there is no cursor
on any of these endpoints.

```python
PageRule(kind=PAGE_OFFSET, param="offset", size_param="limit", size=500, start_at=0)
```

Two details, both of them the kind of paging bug that reports success:

* **`start_at=0`.** Brevo counts from zero and `PageRule.start_at` defaults to **one**, so
  taking the default would skip the first record of every sync, for ever, silently.
* **The offset steps by what came back**, not by the page size. That rule lives in
  `pagination.advance` and applies to every offset connector: a vendor is free to return
  fewer records than it was asked for, and an offset computed from the *request* skips the
  difference — as a gap in the middle of a sync that nothing reports.

Page sizes are the endpoints' own maximums, and asking for more is a 400 rather than a
clamp:

| Read | Page size | Brevo's ceiling |
|---|---|---|
| `contacts` | 500 | 1000 |
| `lists` | 50 | 50 |
| `orders` | 100 | undocumented — 100 is the conservative choice |
| `products` | 1000 | 1000 |
| `categories` | 100 | 100 |

On the eCommerce reads the page size is doing more work than usual, because those endpoints
share the hundred-an-hour allowance: at 1000 a page, a 10,000-product catalogue is ten
requests rather than a hundred.

Offset paging over a list being written to can skip or repeat a record. For a contact list
that is a re-processed contact rather than a lost one — and `modified_since` is the input to
narrow a sync with when it matters, which is also what turns a nightly full read into a few
hundred records.

---

## Operations

| Operation | Kind | Call | Notes |
|---|---|---|---|
| `contacts` | read | `GET /contacts` | Filters: `modified_since`, `created_since`. Records at `contacts` |
| `lists` | read | `GET /contacts/lists` | Records at `lists`. Read this to find a list id |
| `create_contact` | write | `POST /contacts` | Upsert. Idempotent. Requires `email` |
| `add_to_list` | write | `POST /contacts/lists/{list_id}/contacts/add` | Idempotent. Requires `list_id`, `emails` |
| `orders` | read | `GET /orders` | Records at `orders`. Line items flattened as outputs as well as whole |
| `products` | read | `GET /products` | Records at `products`. Also filters `ids`, `search`, `categories` |
| `categories` | read | `GET /categories` | Records at `categories`. Read this to find the ids a product is filed under |
| `upsert_order` | write | `POST /orders/status` | Idempotent, natively. Requires `id`, `created_at`, `updated_at`, `status`, `amount`, `products` |
| `upsert_product` | write | `POST /products` | Idempotent via `updateEnabled`. Requires `id`, `name` |
| `upsert_category` | write | `POST /categories` | Idempotent via `updateEnabled`. Requires `id` |

Every read takes `modified_since`, `created_since` and `sort`, and **none of them is
required**. That is load-bearing rather than lenient: `test_connection` builds its one real
call with `pagination.first_page_arguments`, which is empty for an offset read, so a
required read input would make every Test press fail with a sentence about a missing value
instead of telling the owner whether their key works.

### Attributes

Brevo keeps per-account contact fields in an `attributes` object whose keys are whatever
that account defined — `FIRSTNAME` on one, `PRENOM` on another. The connector declares the
three Brevo creates on every account (`FIRSTNAME`, `LASTNAME`, `SMS`) as named outputs so the
mapping grid can offer them, **and** exposes the whole object as `attributes`, so a field
nobody predicted is still reachable by path. A key the account has not defined is refused by
Brevo, so map to fields that exist.

### The one path with a placeholder

`add_to_list` puts the list id in the path. `request_builder._render_path` escapes it with
`quote(safe="")` and refuses to build a URL with an unfilled hole in it — so a step that
failed to map a list id is a refusal naming the field, not a `POST` to
`/contacts/lists//contacts/add`.

---

## Rate limits

**This connector is why `OperationSpec.rate_limits` exists**, and it is the only shared
runtime change the eCommerce section needed.

Brevo publishes quotas per endpoint family, and across the ten operations on one connection
they differ by 180×:

| Family | Brevo's figure | Declared as | Group |
|---|---|---|---|
| Contacts — all four operations | 36,000/hour | 10 rps, burst 20 | `contacts` |
| `POST /orders/status` | 18,000/hour | 5 rps, burst 10 | `orders` |
| `POST /products` | 7,200/hour | 2 rps, burst 4 | `products` |
| Everything else — the three reads and `upsert_category` | **100/hour** | 0.0278 rps, burst 100 | `other` |

A single connector-wide figure has to be one of those, and both ends are wrong. The slowest
throttles an order sync to one record every 36 seconds — weeks, for a real catalogue. The
fastest exhausts the retry engine's three attempts against the hourly ceiling and fails the
run outright.

### 100 per hour, as a leaky bucket

```python
OTHER_LIMITS = RateLimitSpec(requests_per_second=100 / 3600, burst=100)
```

The bucket refills at one token every 36 seconds and holds a hundred, and
`RateLimiter.bucket` starts a bucket **full** — so a read of a few pages goes straight out
and only a sustained one waits. That is precisely the allowance Brevo grants: a hundred
whenever you like, then the drip. A `burst` of 1 would have made a three-page read take two
minutes for no reason.

It is also why the page sizes matter more here than elsewhere. `products` asks for 1000 a
page, so a 10,000-product catalogue is ten requests out of the hundred rather than a
hundred of them.

### The group is the part that is easy to get wrong

`sender._bucket_key` keys the bucket on `rate_limit_group`, not on the operation id:

```
brevo-<connection-uuid>#other
```

Brevo's hundred-an-hour is **one shared pool** across everything it does not name
individually. Three reads and a category write with a bucket each would send four hundred
an hour against a hundred-an-hour limit — and read as entirely correct right up until the
429s. `OperationSpec.validated` therefore refuses a group that names no allowance, because
that combination reads like an operation with its own budget and silently has not got one.

An operation that declares **no** allowance keeps the bare connection key, so every
connector written before this field behaves exactly as it did. That is not merely
conservative: Shopify's limit is per shop and shared across its three operations, so
splitting them would send three times what the merchant's store permits. There is a
regression test asserting precisely that for Shopify.

### Still uncorrected

Brevo's `x-sib-ratelimit-*` response headers are not among the shapes
`rate_limiter.read_vendor_view` reads, so nothing lowers these buckets from a response. They
are purely local. Contrast Shopify, whose true bucket state arrives in every response body
and *is* read. The mitigation is that the numbers above are Brevo's own published figures
rather than guesses, and that a 429 is retried with the vendor's `Retry-After` honoured.

The remaining exposure is a shared pool: if the same API key is used elsewhere against
Brevo's miscellaneous endpoints, the local bucket is spending from a pool something else has
already drained.

---

## Testing it

`tests/unit/services/integrations/connectors/test_brevo.py`, with `respx` intercepting at
the transport layer so the real pooling, byte-cap, paging and retry code runs.

Three assertions carry the contacts half:

1. **`updateEnabled` really goes out** — asserted on the *sent body*, not on the template,
   because a non-string literal has to survive substitution.
2. **Offset paging starts at zero and steps by what came back.**
3. **A stored base URL cannot override the fixed one.**

And three more carry the eCommerce half:

4. **The upsert flag is on the two writes that need it and absent from the one that does
   not**, and every write claiming `idempotent` can point at the mechanism that earns it.
5. **A line-item array survives as an array**, through substitution *and* through
   `serialise_body`.
6. **The four operations sharing Brevo's hourly pool land on one bucket**, the two metered
   writes get their own, and an operation with no allowance still spends the connection's —
   the last of those asserted against Shopify, as the regression guard for every connector
   that predates the field.

Everything else — the header name, the omitted-not-nulled body fields, the refusal when a
required input is missing, the 204-with-no-body write — is a supporting check on those six.

`tests/unit/services/integrations/connectors/test_spec.py::TestPerOperationRateLimits`
covers the field itself: a group naming no allowance is refused, a database row cannot grant
itself one, and the allowance stays out of the fingerprint.

---

## Adding a Brevo connection

**Apps → Brevo → Connect**, which asks for a name and the key. Then **Connections → Test**,
which makes one real `GET /contacts` and reports what came back — because a connection that
saves is not a connection that works, and a key with the wrong permissions looks identical
until something is sent.

To use the eCommerce operations the account also needs the **eCommerce app switched on** in
the Brevo dashboard. Until it is, those six endpoints answer 403 while contacts keeps
working — so a Test that passes is not on its own proof that an order sync will run.

The Apps gallery, the tabs and what "connected" means on a tile are described in
[INTEGRATIONS.md](INTEGRATIONS.md#the-apps-gallery).

---

## Risks

**The timestamp format on `upsert_order` is inferred, not confirmed.** `created_at` and
`updated_at` are declared `datetime`, so `type_coercion` renders `2026-08-20T09:30:00+00:00`.
Brevo documents `YYYY-MM-DDTHH:mm:ssZ`. The two are the same instant in RFC 3339 and should
be interchangeable, but these are required fields on the most important write here and the
format has not been checked against a live account. A test pins the rendered value, so if
Brevo does refuse it the fix — declaring both `string` and passing the source system's own
text through — is one line and visible rather than archaeological.

**The `/batch` endpoints are not used.** Brevo offers `POST /orders/status/batch` (1,000
orders a call) and `POST /products/batch` (100). `connector_nodes._send_one` sends one
request per record, which is what buys one outcome per record — a chunk of fifty where
record seven was rejected is forty-nine written and one failed, with a row naming it. The
batch endpoints would trade that reporting for throughput, and the single-record endpoints
are upserts, so the only thing given up is speed against an hourly quota the writes do not
share.

**The hourly pool is shared with traffic this connector cannot see.** The `other` bucket
models Brevo's hundred-an-hour, but that hundred covers every miscellaneous endpoint on the
account. Another integration using the same key spends from the same pool, and nothing here
can observe it — see [Still uncorrected](#still-uncorrected).

**Per-worker buckets.** As everywhere in this layer, the leaky bucket is in-process, so
`uvicorn --workers N` gives N times the configured rate. The mitigation is structural — the
sync worker runs as a single in-process loop — and is described in `runtime/rate_limiter.py`.

**`spec.py` and `sender.py` are shared.** `rate_limits` and `rate_limit_group` are additive
with defaults of `None` and `""`, and `sender._bucket_key` returns the bare connection key
for any operation that declares neither — so every connector written before the field is
byte-identical, which the Shopify regression test asserts directly rather than assuming.
The same caution [SHOPIFY_CONNECTOR.md](SHOPIFY_CONNECTOR.md) records applies: a shared spec
is where a vendor-specific bulge does the most damage, and the reason this one is acceptable
is that nothing in either field names Brevo.

**Not verified against a live account.** Everything here is tested against `respx` at the
transport layer, which runs the real pooling, paging, retry, byte-cap and rate-limiting code
but not a real Brevo. Response and request shapes are taken from Brevo's published
documentation. The two things most worth checking first on a real account are the timestamp
format above and whether an order with a well-formed `products` array is accepted as
documented.
