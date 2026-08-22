# The event bus

`app/utils/events.py`. A small in-process publish/subscribe: features announce that
something happened, and anything interested reacts.

Added for Email Dispatch's event triggers — "when a sync fails, email me" — but shared
infrastructure rather than part of that module, because the *publishers* are graph runs and
integration runs and none of them should import an email module in order to say that
something happened to them.

---

## What it is

```python
EVENT_NAMES          # the catalogue: (name, label) pairs, plus a frozenset and label dict
subscribe(name, handler)                              # at import, strict
await publish(name, payload, *, user_id, workspace_id=None) -> int
```

That is the whole surface. A handler is `async def handler(event: Event) -> None`, and
`Event` carries `name`, `payload`, `user_id` and an optional `workspace_id`.

Names are `subject.past_tense` — the subject is the thing that changed, not the feature that
changed it, because a subscriber cares about the run, not about which code path touched it.

## The rules

### Publishing can never break the publisher

Every handler runs inside its own `try/except` and a failure is logged, not raised. This is
the discipline `run_store._quietly` applies to step logging, and the reasoning is identical:
the bus is an *observation* of the work, not part of it. A sync finishing must not fail to be
recorded because somebody's email template has a typo in it.

`publish` also **ignores an unknown event name** rather than refusing it, which is the one
place this module is deliberately more forgiving than `subscribe`: a publisher is usually
somewhere important — a run settling, a shutdown path — and taking it down over a mistyped
constant would trade a missing email for a broken feature. The subscribe side is strict,
which is where a typo actually gets caught.

### Subscribing to an unknown name is refused

The opposite call, for the opposite reason. A silent no-op subscription is the worst outcome
available: everything looks wired up, nothing ever arrives, and there is no error anywhere to
find.

### Publish *after* your own commit, never inside the transaction

A handler runs immediately and opens its own session. If the change that prompted the event
has not landed, the handler reads a database that does not agree with the event it was just
given.

A handler must not share the publisher's session either — a failed statement in a handler
would leave that session in a failed transaction and roll back the business change that
prompted the event.

**The honest cost of that ordering:** a crash in the window between the commit and the
handler loses that one notification. It is a real gap and it is the right trade — the
alternative puts a stranger's template error inside the transaction that was recording a
sync failure. Nothing here is a delivery guarantee and no caller should treat it as one. A
durable outbox would close the gap at the price of a second background loop, which
[EMAIL_DISPATCH.md](EMAIL_DISPATCH.md) §11 explains this feature is deliberately not
spending.

### Events are per-owner

Every publish carries the `user_id` the event concerns, and subscribers filter on it.
Without that, one tenant subscribing to an event would be told about every other tenant's
data — a leak rather than a bug, so it has its own test.

### No table

An event is not a record; it is a notification that a record changed. Anything worth keeping
is already kept by whoever published it, and on the receiving side by
`EmailMessage.source` / `source_ref`.

### Handlers run sequentially

There are a handful at most and each is a short database write, so sequential execution means
a slow one delays only the publish rather than competing for the connection pool with its
siblings. `asyncio.gather` is the change if that stops being true — but it would need a bound
on concurrency, which is the complexity this module is avoiding.

## The catalogue

| Name | Published from |
|---|---|
| `graph_run.settled` | `graph_run_service._announce_settled`, after `_settle` commits |
| `integration_run.settled` | `run_service._announce_settled`, after `_settle` commits |

**Every name in `EVENT_NAMES` has a real publisher**, and the list is short because of it. An
event nothing publishes is a choice the trigger form offers and that never fires — the same
silent no-op `subscribe` refuses, arrived at from the other direction.

Two candidates were drafted and dropped rather than faked, and both refusals are recorded in
the source so they are not re-proposed:

- **`datasource.status_changed`** — `datasources` has no status column and nothing
  transitions one, so there was nowhere honest to publish from. Add the column and the
  transition first, then the event.
- **`download_job.finished`** — an export has no owner of its own (ownership runs export →
  data agent → user), and the person who asked for it is a chatbot visitor who is already
  told in the conversation. The event would have emailed the agent's owner about somebody
  else's download.

Both `settled` events carry the run's uuid, its status, the flow or graph's name and uuid,
and any failure message; the integration one also carries the record counters, because "the
sync finished" and "the sync finished having failed 3 of 50,000 records" are the two
different things somebody would want to be emailed about and `partial` alone does not say
which.

## Adding an event

Three steps, and none of them touch the email module:

1. a constant and an `EVENT_NAMES` entry in `app/utils/events.py`;
2. one `await events.publish(...)` at the point the thing actually happens — after the
   commit, and wrapped so it cannot fail the work it is reporting on;
3. nothing else. The email module's handler is already subscribed to every name on the list,
   and the trigger form is built from it.

Wrap the publish the way the two existing publishers do — a small `_announce_*` helper with
its own `try/except` and its own session — so the publish site stays one line and the "this
must never fail the run" rule is stated once, next to the code it applies to.

## Adding a subscriber

Subscribe at import, and make sure something imports you. `main.py` imports
`app.services.email_dispatch.triggers` purely for that side effect, the same
import-for-registration pattern `connectors/registry.py` uses for connectors and
`app/db/models.py` for models — and there is a comment on that import line saying so,
because deleting it as "unused" would leave every email trigger stored correctly and never
firing.

A handler should: filter on `event.user_id`, open its own session, and never raise. The bus
catches anyway, but a handler that relies on being caught is a handler whose failures are
invisible.

## Testing

The registry is process-local, so a handler registered by one test can fire during another —
invisible in either of them. `tests/unit/services/email_dispatch/test_triggers.py` snapshots
`events._handlers` and restores it, rather than calling `clear_subscribers()`: clearing would
leave every later test in the session with a bus that does nothing, since the real
subscription is made once at import.
