# Email Dispatch

How this application sends email: the servers it goes through, the templates it is written
from, the triggers and nodes that decide when, and the log that says what happened.

Module: `app/{models,db,schemas,services,routes}/email_dispatch/`,
`templates/email_dispatch/`, `static/js/email_dispatch.js`.
The shared event bus it subscribes to is [EVENT_BUS.md](EVENT_BUS.md).

---

## 1. Why this exists

Before it, nothing in the platform could tell a human anything. A Graph Designer run that
found a problem, an integration sync that failed at three in the morning, an agent that
produced an answer worth circulating — every one of them ended silently. The nearest thing
that existed was `ChatbotAction`, which makes an outbound *HTTP* call.

So the feature is not "an email helper". It is the platform's first outbound notification
path, and it is shaped by that: everything it sends is recorded permanently, because the
question people actually ask is not "can we send email" but "did that customer get the
thing we said we sent them".

## 2. The shape of it

```
An operator sets up:            SMTP servers          templates          triggers
                                (how it leaves)       (what it says)     (when, without a canvas)

Something asks for an email:    an Email node in a flow / graph / integration
                                an event trigger  (graph_run.settled, integration_run.settled)
                                a webhook trigger (an external system POSTs)
                                the Send-test button

Everything funnels through:     dispatch_service.enqueue_email()
                                  -> renders NOW, stores the finished text
                                  -> one email_messages row, status=queued

One background worker:            claim (FOR UPDATE SKIP LOCKED, serialised per server)
                                  -> sender.send_message()  [the only socket in the module]
                                  -> sent | queued-again-later | failed
                                  -> one email_message_attempts row per try
```

**There is exactly one send path.** `enqueue_email` is the only way a message row is
created, and `sender.send_message` is the only function that opens a socket. When something
is wrong with how mail goes out there is one function to read, and when a test needs to
intercept mail there is one name to patch.

## 3. The five decisions worth knowing

### 3.1 A table, not a broker

The queue is an `email_messages` row claimed with `FOR UPDATE SKIP LOCKED`. There is no
Redis, no Celery and no arq in this project and this does not add one — the third time that
decision has been made here, after the export queue and the sync queue, and the reasoning
has not changed: a locked row is durable across restarts, safe across processes, and visible
in the same database as everything it is about. See §29 of
[ENGINEERING_TECHNOLOGY.md](ENGINEERING_TECHNOLOGY.md).

### 3.2 Rendering happens at enqueue, and the finished text is stored

`subject`, `body_html` and `body_text` on the message row are the real text that went out,
not a template reference. The alternative fails twice over:

- the values come from a live run — a graph's `outputs`, a chat session's variables, an
  agent's prompt variables — and none of that exists by the time the worker picks the row up
  thirty seconds or three retries later;
- a log holding a template id cannot answer "what did we actually send them", because the
  template has been edited since.

A consequence worth stating: **editing a template never changes an email already sent**, and
a Retry re-sends bytes that are identical by construction rather than by hope.

### 3.3 A dead worker fails the message; it does not resume it

This is the most consequential decision in the module. A worker that stopped reporting
mid-`sending` may already have completed the SMTP conversation — the mail could be in
somebody's inbox right now — and there is no way from here to find out. So
`requeue_stale_emails` marks it **failed** with "delivery is unknown", and the Retry button
says, in as many words, that retrying may deliver it twice. The decision belongs to a
person.

`requeue_stale_jobs` in the downloader restarts an export instead, which is safe because
nothing outside this application has seen a part file. Email is the opposite: the side
effect is entirely outside, irreversible, and visible to a customer.

### 3.4 Sending is serialised per SMTP server, in the claim

`claim_next_email` carries a correlated `NOT EXISTS` refusing a message whose SMTP config
already has one in flight. `EMAIL_WORKER_CONCURRENCY` is a courtesy; **this** is the control.
Providers respond to a burst of parallel connections by throttling, greylisting or
classifying the sender as a spam source, and the last of those is not something a retry
fixes — it breaks every other email the platform sends.

It is in the claim rather than in the worker because a worker-side check is a check-then-act
with two workers in the window, which is precisely the case it exists to handle. **The
correlation is written explicitly** with `.correlate(EmailMessage)`: the integrations queue
was bitten by auto-correlation picking the wrong enclosing SELECT, which silently turned
"this server is busy" into "any server is busy". Both halves of the rule have a test.

### 3.5 A binding that finds nothing yields nothing, and the template decides

When a binding's path resolves to nothing, the variable is **omitted** from the resolved map
rather than set to `""`. `rendering.render_message` then fills it from the variable's
declared default, or refuses the whole send if it was declared required with no default.

That puts strictness where an operator can see and set it, one variable at a time, instead
of hard-coding one answer. A `{{REASON}}` that is naturally absent when a run succeeded gets
a default of "none given"; a `{{CUSTOMER}}` marked required stops the email rather than
addressing somebody as "Dear ,". A *malformed* path (`customer..name`) is different and is
refused outright — that is a typo, not an absent field, and no default should paper over it.

## 4. Dynamic variables: where a value can come from

A template declares its placeholders; a node or trigger **binds** each one to a source. The
closed list — there is no expression evaluator anywhere, the same discipline
`engine/transform.py` and `sql_guard` apply.

| Source | Reads | Available in |
|---|---|---|
| `literal` | a fixed value typed into the node | everywhere |
| `agent` | the chatbot's prompt variables, via `chatbot_ai_settings_service.variables_map` | Flow Builder |
| `session` | what the conversation collected (`ChatbotFlowSession.variables`) | Flow Builder |
| `node` | an earlier node's output, `state["outputs"][node_id]`, by path | Graph Designer, Integrations |
| `record` | a field on the record in hand | Integrations (per-record mode only) |
| `event` | a field on the event or webhook payload, by path | event and webhook triggers |

**The Agents-section path is the one to understand.** A flow runs behind a chatbot, and that
chatbot has a `ChatbotAiSettings` row carrying the prompt variables an operator set up under
Agents. `agent_variables_for` reads them through `variables_map` rather than off the JSONB
column — deliberately, because `{{AGENT_NAME}}` is *synthesised* from the `agent_name` field
rather than declared, so reading the column would miss it and the miss would be silent.

**A source a canvas cannot serve is refused by name, never resolved to blank.** A graph has
no chat session; a flow has no upstream node outputs (its whole state is one flat string
map). `VariableContext.available()` decides, and the refusal names both the variable and
why — at save time by each canvas's validator, and again at run time.

## 5. The Email node, per canvas

Every runner lives in `app/services/email_dispatch/nodes/`. The host feature contributes a
registry entry and a validator call and nothing else — a new module does not put its files
inside another feature's folder.

**All three queue rather than send.** Waiting on SMTP inside a node would make the run's
wall-clock depend on somebody else's mail server, hold a session open across a network call,
and turn a greylisting relay into a failed run. The node's output therefore says
`queued: True, delivered: None`, and that shape is pinned by a test: a downstream branch
reading the node's success as "delivered" is the misreading this node most invites.

| | Graph Designer | Integrations | Flow Builder |
|---|---|---|---|
| type | `email` | `email` | `send_email` |
| ports | `default` / `error` | `default` / `error` | `default` / `error` |
| sources | `node`, `literal` | `node`, `literal`, `record` | `session`, `agent`, `literal` |
| registered in | `node_runners._RUNNERS` | `register_runner()` + import-time assert | `_run_one_hop` chain |

### Where to put it in a Graph Designer graph

Directly after the Success or Failure box, in most cases. Those two used to forbid a
successor, which meant an alert had to be drawn *before* the box that said what happened —
so `GRAPH_DESIGNER.md` now records why they have a `then` exit. An Email node there runs with
the verdict already settled and cannot change it: a run that passed through Failure is still
reported as failed, however well the email goes.

The alternative for a whole run rather than one path is a `graph_run.settled` event trigger
(§6), which needs no node at all and fires for every run of every graph.

A **Timer** box before it makes "this took 1h 4m 12s" available to the template: bind a
variable to the Stop box with the path `elapsed_human`, or `started_at` / `ended_at` for the
two instants. See `GRAPH_DESIGNER.md`.

### The template list says which team, and never takes one away

`template_service.choices(db, user_id)` is the plain list, used by the Flow Builder canvas
and this module's own pages. `choices_for_workspace(db, user_id, workspace_id)` is the same
list annotated with each template's workspace, used by the Graph Designer's Email node.

**Annotated, not filtered — and this is worth reading twice, because the obvious version is
wrong.** `workspace_id` records *who else may use* a template. It widens access; it never
narrows what the owner may do. A graph and a template belonging to the same person can always
be used together, so a template shared with another team is **fully selectable** and simply
carries `shared with Analytics` in its `detail`. That is context for telling two similar
templates apart, not permission.

`disabled_reason` means "picking this will not work", so the only thing that earns it is a
switched-off template.

**The version that got this backwards, and how it failed.** An earlier pass refused, on save
and publish, an Email node whose template belonged to a different workspace than the graph.
It was wrong twice over. It inverted the model's own rule — "Ownership is the user; the
workspace is who else may use it" — so sharing a template with a team silently removed the
owner's own access to it. And because attaching a graph to a data agent and sharing it into a
workspace are *mutually exclusive*, every agent-attached graph has `workspace_id IS NULL`
permanently, which made it unable to send any shared template at all. It was reported within
minutes of reaching a real canvas. There is no server-side workspace rule now, and there
should not be one: the access control that matters is `user_id`, enforced by
`dispatch_service.resolve_template`, which 404s a template the sender does not own.

**A note on the response schema.** Both lists were being built by `node_options` and dropped
before they reached the browser, because `GraphNodeOptionsResponse` did not declare them and
`ResponseSchema` is `extra="ignore"`. The Email node's Template picker was therefore empty in
every browser with nothing anywhere saying why — which in turn hid a TypeError in the binding
editor that could not be reached while the picker stayed empty. If a picker is mysteriously
empty, check that the response schema names the key before checking anything else.

### The integrations node is the dangerous one

Every other canvas runs a node once. An integration node runs against a batch, and a batch
is routinely fifty thousand rows — so "one email per record" is one careless drawing away
from fifty thousand emails and a blocked sending domain. Two modes, and the safe one is the
default:

- **`once`** (default) — one email for the whole batch. A `record` binding is refused,
  because "the record" is meaningless when there are forty thousand of them.
- **`per_record`** — one per record, capped by `max_emails` (default 50, ceiling 500). A
  batch over the cap **fails the node and queues nothing**. Truncating silently would be the
  worst option available: the operator would believe everyone had been emailed.

The cap is checked *before* the loop, because discovering at record 51 that there were 4,000
would leave 50 emails already queued and no way to un-queue them.

## 6. Triggers without a canvas

### Event triggers

Subscribe to a name from `app/utils/events.EVENT_NAMES`. The subscriber is wired by
*importing* `app.services.email_dispatch.triggers`, which `main.py` does — the same
import-for-registration pattern as `connectors/registry`. Without that import, triggers store
correctly and never fire.

Each trigger fires in **its own session**, and that is load-bearing rather than tidy: a
shared session's rollback expires every other trigger's row, so the next iteration's
`trigger.template` becomes a lazy load from a synchronous attribute access and SQLAlchemy
raises `MissingGreenlet` — the loop that was supposed to carry on after one failure dies on
the next trigger instead. It also makes the claim true that one broken trigger cannot roll
back a working one's message.

### Webhook triggers

`POST /public/emails/webhooks/{endpoint_id:uuid}` — unauthenticated, which makes it the
highest-risk surface in the module. Five defences, in order:

1. **Endpoint id** — a *separate* rotatable uuid, not the trigger row's own, so leaking the
   URL is fixable by rotating one column rather than rebuilding every caller. Unknown,
   disabled and wrong-kind all answer the same **404**; a different status would tell an
   unauthenticated caller which ids are real.
2. **Body size** — capped at 64 kB before anything parses it → **413**.
3. **Signature** — HMAC-SHA256 over `"{timestamp}.{body}"`, compared with
   `hmac.compare_digest` → **401**. Signing the two together is what stops yesterday's body
   being paired with today's timestamp.
4. **Timestamp** — outside a 5-minute window → **400**, so a captured request cannot be
   replayed tomorrow.
5. **Throttle** — `min_interval_seconds` since `last_fired_at` → **429**. Checked *after* the
   signature, so an unauthenticated caller cannot learn when a trigger last fired.

A valid call returns **202**, not 200: the email is queued, not sent. A redelivery of the
same signed call returns 202 as well, having queued nothing — the idempotency key is the
signature, so a caller that did not see our answer and retried produces one email. A payload
that cannot satisfy the bindings is **422**: the request was authentic, and the fix is the
caller's.

The secret is generated by `secrets.token_urlsafe`, shown **once**, and unrecoverable
afterwards. Rotation issues a new URL and a new secret together, because they leak as a pair.

## 7. Secrets

`password_encrypted` and `webhook_secret_encrypted` are Fernet ciphertext via
`app/utils/crypto.py` (`MultiFernet`, so key rotation works — see
[SECRETS_AND_KEY_ROTATION.md](SECRETS_AND_KEY_ROTATION.md)).

They sit on their own rows rather than in a separate credentials table.
`integration_credentials` is a table of its own because it holds six secrets *plus* OAuth
refresh state and a compare-and-set lock; none of that applies here. What keeps a secret out
of a response is the schema layer instead: every view names its fields explicitly, so a new
secret column is absent from a response until somebody adds it deliberately, and a test
asserts no view carries one.

**The password is write-only from outside.** An edit form comes back with the field *empty*
and a placeholder, and blank on save means "leave it" — because the form posts an empty box
on every unrelated edit, so blank cannot mean "clear it". Removal is a separate explicit
tick.

## 8. Reaching a relay on a private network

An SMTP host is user-supplied text, so `sender` checks it through `assert_public_host` before
opening a socket — the same guard the integrations module uses, so one place decides what
"reachable" means.

By default nothing private is reachable. A corporate smarthost or a sidecar needs an
**environment** allow-list, not a form field: a form field that grants itself permission to
reach internal addresses is an SSRF hole with a label on it — anybody able to create an SMTP
config could point it at the cloud metadata endpoint and read the reply out of the test
button's error message.

```
EMAIL_ALLOWED_PRIVATE_HOSTS=smtp.internal:587,10.0.4.9:25
EMAIL_ALLOWED_PRIVATE_CIDRS=10.0.0.0/8
```

Both are required together and validated at import, so a half-configured allow-list stops
the application at startup rather than failing the first send at three in the morning. The
hostname must be one somebody wrote down *and* the address it resolves to must be inside a
range somebody wrote down.

**Loopback is never reachable, allow-list or not.** `outbound_http._NEVER_ALLOWED` refuses
`127.0.0.0/8`, `::1`, link-local (cloud metadata), `0.0.0.0/8` and multicast after the
allow-list, so ordering cannot be used to slip past them. Verifying the send path locally
therefore means binding a capture server to the container's own routable private address, not
to `localhost`.

## 9. Retry

Classified once, at the moment of failure, from the SMTP reply code — never re-derived later
from the message text, the rule `NodeFailure` states.

| | |
|---|---|
| retryable | 421, 450, 451, 452, 454, 455, any other 4xx, connection errors, timeouts |
| permanent | 550, 551, 553, 554, 556 (recipient), 530, 534, 535, 538 (auth) |
| neither | an unrecognised 5xx — not retried automatically, but nothing claims a later attempt is hopeless |

Backoff is `30s → 2m → 8m → 32m`, capped at an hour, **without jitter**. The usual reason for
jitter is a thundering herd, and that cannot happen here: the claim serialises per server, so
retries against one provider are single-file whatever their timings say. Leaving it out buys
a backoff that is exactly reproducible in a test.

A **timeout is retryable**, knowingly, even though the server may have accepted the message
after `DATA`. Treating it as permanent would lose mail every time a relay is merely slow,
which is far more common than the duplicate.

## 10. Limits

| | |
|---|---|
| Declared variables per template | 30 |
| Characters per substituted value | 500 (truncated with `…`, not refused) |
| Variable name | `^[A-Z][A-Z0-9_]{0,49}$` — upper-case, so `{{company}}` and `{{COMPANY}}` cannot be two variables |
| Recipients per email (to + cc + bcc) | 50 |
| Subject | 998 characters (RFC 5322's header line limit) |
| Body | 200,000 characters |
| Send attempts | 5 (`EMAIL_MAX_ATTEMPTS`) |
| Emails per integration batch | 50 by default, ceiling 500 |
| Webhook body | 64 kB |
| Webhook replay window | 5 minutes |

Environment: `EMAIL_WORKER_CONCURRENCY=2`, `EMAIL_WORKER_POLL_SECONDS=5`,
`EMAIL_WORKER_HEARTBEAT_SECONDS=10`, `EMAIL_WORKER_STALE_SECONDS=60`.

## 11. The loop budget

This module adds **exactly one** background loop per process — the send worker — bringing the
total to five (download worker, expiry reaper, integration sync workers, integration
scheduler, email sender). `INTEGRATIONS.md` flags loop count as a real cost, so:

- there is **no scheduler**, because there are no scheduled triggers. An event or a webhook
  fires a trigger, and both are push;
- the event bus is in-process and synchronous, so publishing costs no loop;
- the webhook is a route.

Adding scheduled email triggers means adding a second loop, and that trade should be made
deliberately rather than by adding a column.

## 12. Escaping, and the things that would otherwise go wrong

- **The HTML body escapes its values; the text body and subject do not.** A customer name of
  `Bob & Sons <bob@x.com>` must arrive intact in the plain-text part and as
  `Bob &amp; Sons &lt;bob@x.com&gt;` in the HTML one. The cost is that a variable cannot
  carry markup into the HTML body — put it in the template, where a reviewer can see it.
  Letting values carry markup would make every template an injection point for whatever an
  upstream agent produced.
- **A CR or LF in a subject or recipient value is refused.** That is header injection:
  everything after the newline is read as a new header, which is how an attacker-supplied
  name becomes an extra `Bcc`. Same check `chatbot_action_service` makes for header mode.
- **`Bcc` is an envelope recipient only and never a header.** A `Bcc` header is delivered to
  everyone on the message, which is the opposite of what bcc means.
- **A display name is written through `formataddr`**, which quotes it — a comma in
  `"GetMyStuff, Alerts"` would otherwise be read as an address separator and split one
  recipient into two.
- **A partial acceptance is recorded as sent, and names the refusals.** SMTP can accept a
  message while refusing some recipients without raising; recording that as an unqualified
  success is the half-truth this module exists to avoid.
- **The template preview and the log's "what was sent" pane show HTML as source, never
  rendered.** An operator can paste anything into a body, and rendering it into the page
  would execute it with their session — a stored-XSS hole opened by the *viewer* rather than
  by the email. The mail client is where it gets to be HTML.

## 13. Testing

`tests/unit/services/email_dispatch/`. Two autouse fixtures in its `conftest.py`, both
guarding a failure that looks like something else:

- **`email_sessions`** points `message_store.open_session` at the per-test database. Without
  it the worker reads and writes the *development* Postgres while the assertions look at the
  in-memory SQLite, and the failure reads "expected 1 message, got 0" with nothing to explain
  it.
- **`no_smtp`** replaces `sender.send_message` everywhere with a recorder, so no test can
  reach a real server (the root conftest's `block_network` would raise anyway). A test wanting
  a particular outcome sets `no_smtp.result` or `no_smtp.error`.

Traps that apply here specifically:

- `DateTime(timezone=True)` is aware on Postgres and **naive** on SQLite, so a stored
  timestamp compared to `datetime.now(timezone.utc)` raises `TypeError` in tests and not in
  production. The tests use an `aware()` helper, the same reasoning as `scheduler._aware`.
- The event-bus registry is process-local, so a handler registered by one test can fire
  during another. `test_triggers.py` snapshots and restores `events._handlers` rather than
  clearing it — clearing would leave every later test in the session with a bus that does
  nothing.

Verifying the real send path needs a capture server on a routable private address:

```bash
# inside the app container
python -m aiosmtpd -n -l "$(python -c 'import socket;print(socket.gethostbyname(socket.gethostname()))'):1025"
# and in .env
EMAIL_ALLOWED_PRIVATE_HOSTS=<that ip>:1025
EMAIL_ALLOWED_PRIVATE_CIDRS=172.16.0.0/12
```

## 14. Deliberately not built

- **Scheduled triggers.** They need a second background loop; see §11.
- **A bounce/complaint table.** SMTP tells us at send time whether a recipient was refused,
  and that goes in `smtp_response`. Asynchronous bounce handling needs an inbound mail path
  that does not exist.
- **An event table behind triggers.** Provenance is `EmailMessage.source` and `source_ref`,
  which is what anybody asking "why was this sent" actually needs.
- **Attachments.** Nothing asked for them, and they would need a decision about where the
  bytes come from — an export? an upload? — that is a feature of its own.
- **A durable outbox for events.** The honest cost of not having one: a crash between a
  publisher's commit and its subscriber loses that one notification. The alternative puts a
  stranger's template error inside the transaction that was recording a sync failure. See
  [EVENT_BUS.md](EVENT_BUS.md).
