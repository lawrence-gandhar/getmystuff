"""
A small in-process event bus: features announce that something happened, and anything
interested reacts.

Added for Email Dispatch's event triggers — "when a sync fails, email me" — but it is
shared infrastructure rather than part of that module, because the *publishers* are
datasources, graph runs, integration runs and exports, and none of them should import an
email module to say that something happened to them.

**No table.** An event is not a record; it is a notification that a record changed.
Anything worth keeping is already kept by whoever published it, and by
``EmailMessage.source`` / ``source_ref`` on the receiving side, which is what answers "why
was this sent". A durable outbox would buy at-least-once delivery at the cost of a second
background loop, and this module deliberately adds none — see the loop-budget note in
``documentations/EMAIL_DISPATCH.md``.

**Publishing can never break the publisher.** Every handler runs inside its own
``try/except`` and a failure is logged, not raised. This is the same discipline
``run_store._quietly`` applies to step logging, and the reasoning is identical: the bus is
an *observation* of the work, not part of it. A datasource going offline must not fail to be
recorded because somebody's email template has a typo in it.

**Handlers are called after the publisher has committed, and open their own sessions.** A
handler sharing the publisher's session could leave it in a failed transaction and roll back
the business change that prompted the event. So :func:`publish` is called *after* the
commit, and every handler is responsible for its own session.

The honest cost of that ordering: a crash in the window between the commit and the handler
loses that one notification. It is a real gap and it is the right trade — the alternative
puts a stranger's template error inside the transaction that was recording a datasource
failure. Nothing here is a delivery guarantee, and no caller should treat it as one.

**Events are per-owner.** Every publish carries the ``user_id`` the event concerns, and
subscribers filter on it. Without that, one tenant subscribing to
``datasource.status_changed`` would be told about every other tenant's datasources.
"""

import asyncio
import inspect
import logging
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# The catalogue
# ---------------------------------------------------------------------------
# `(name, label)` pairs plus a derived frozenset and label dict — the house convention for
# an enumerable vocabulary. The trigger form offers exactly this list, and `publish`
# refuses a name that is not on it, so a subscriber cannot be waiting for an event nobody
# publishes and a publisher cannot invent one no form can subscribe to.
#
# Names are `subject.past_tense`. The subject is the thing that changed rather than the
# feature that changed it, because a subscriber cares about the datasource, not about which
# code path touched it.

# **Every name here has a real publisher**, and the list is deliberately short because of
# it. An event nothing publishes is a choice the trigger form offers and that never fires —
# the silent no-op :func:`subscribe` refuses, arrived at from the other direction. Two
# candidates were drafted and dropped rather than faked:
#
#   datasource.status_changed  `datasources` has no status column and nothing transitions
#                              one, so there was nowhere honest to publish from. Add the
#                              column and the transition first, then the event.
#   download_job.finished      an export has no owner of its own — ownership runs export ->
#                              data agent -> user — and the person who asked for it is a
#                              chatbot visitor who is already told in the conversation. The
#                              event would have emailed the agent's owner about somebody
#                              else's download.
#
# Adding an event is: a constant here, an entry in EVENT_NAMES, and one `publish` call at
# the point the thing actually happens. Nothing in the email module needs touching — its
# handler is already subscribed to every name on this list.

EVENT_GRAPH_RUN_SETTLED = "graph_run.settled"
EVENT_INTEGRATION_RUN_SETTLED = "integration_run.settled"

EVENT_NAMES = (
    (
        EVENT_GRAPH_RUN_SETTLED,
        "A Pipelines run finished, failed or was cancelled",
    ),
    (
        EVENT_INTEGRATION_RUN_SETTLED,
        "An integration sync finished, failed or ended partial",
    ),
)
EVENT_NAME_VALUES = frozenset(name for name, _ in EVENT_NAMES)
EVENT_NAME_LABELS = dict(EVENT_NAMES)


#: What a handler is given. Async, returns nothing, and must not raise — though the bus
#: assumes it will and catches anyway.
Handler = Callable[["Event"], Awaitable[None]]


class Event:
    """
    One thing that happened.

    ``payload`` is a plain mapping of facts a subscriber may want to put in an email:
    ``{"datasource_name": "Warehouse", "status": "offline", "reason": "..."}``. Its keys are
    read through ``mapping/paths.py``'s restricted reader on the way into a template, so a
    nested dict is fine and nothing here is evaluated.

    ``user_id`` is who the event is about, and every subscriber filters on it.
    ``workspace_id`` is optional context, carried so a message can inherit sharing without
    the handler having to look it up.

    ``__slots__`` because one of these is created per published event and several publish
    points sit in hot paths — a run settling, a datasource being checked.
    """

    __slots__ = ("name", "payload", "user_id", "workspace_id")

    def __init__(
        self,
        name: str,
        payload: Mapping[str, Any],
        *,
        user_id: int,
        workspace_id: Optional[int] = None,
    ) -> None:
        self.name = name
        self.payload = dict(payload or {})
        self.user_id = user_id
        self.workspace_id = workspace_id

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Event {self.name} user={self.user_id} keys={sorted(self.payload)}>"


_handlers: Dict[str, List[Handler]] = {}


def subscribe(event_name: str, handler: Handler) -> None:
    """
    Register a handler for one event, at import time.

    Refuses an unknown name rather than accepting a subscription that can never fire. A
    silent no-op subscription is the worst outcome available here: everything looks wired
    up and nothing ever arrives, and there is no error anywhere to find.

    Not idempotent by design — registering the same function twice really does mean two
    calls, and quietly de-duplicating would hide a double-import problem worth seeing.
    Modules subscribe at import, and Python imports once.
    """
    if event_name not in EVENT_NAME_VALUES:
        raise ValueError(
            f"'{event_name}' is not a known event. Add it to EVENT_NAMES first — "
            f"known events are: {', '.join(sorted(EVENT_NAME_VALUES))}."
        )
    _handlers.setdefault(event_name, []).append(handler)
    logger.debug("Subscribed %s to %s", getattr(handler, "__name__", handler), event_name)


def subscriber_count(event_name: str) -> int:
    """How many handlers are listening. For a startup log line and for tests."""
    return len(_handlers.get(event_name, ()))


def clear_subscribers() -> None:
    """
    Forget every handler.

    For tests only. Process-local registries leak across tests in a way that is invisible
    in any single one — a handler registered by an earlier test firing during a later one
    — so the email tests reset this in a fixture rather than debugging it later.
    """
    _handlers.clear()


async def publish(
    event_name: str,
    payload: Optional[Mapping[str, Any]] = None,
    *,
    user_id: int,
    workspace_id: Optional[int] = None,
) -> int:
    """
    Tell every subscriber that something happened. Returns how many handlers ran.

    **Call this after your own ``commit()``**, never before and never inside the
    transaction. A handler runs immediately and opens its own session; if the change that
    prompted the event has not landed, the handler reads a database that does not agree with
    the event it was just given.

    Never raises. An unknown event name is logged and ignored rather than refused, which is
    the one place this module is deliberately more forgiving than :func:`subscribe`: a
    publisher is usually somewhere important — a run settling, a shutdown path — and taking
    it down over a mistyped constant would trade a missing email for a broken feature. The
    subscribe side is strict, which is where a typo actually gets caught.

    Handlers run **sequentially**, not gathered. There are a handful of them at most, each
    is a short database write, and sequential execution means a slow one delays only the
    publish rather than competing for the connection pool with its siblings. If that ever
    stops being true, ``asyncio.gather`` here is the change — but it would need a bound on
    concurrency, which is exactly the complexity this module is avoiding.
    """
    if event_name not in EVENT_NAME_VALUES:
        logger.error(
            "Ignoring publish of unknown event '%s'. Add it to EVENT_NAMES.", event_name
        )
        return 0

    handlers = list(_handlers.get(event_name, ()))
    if not handlers:
        return 0

    event = Event(
        event_name, payload or {}, user_id=user_id, workspace_id=workspace_id
    )

    ran = 0
    for handler in handlers:
        try:
            result = handler(event)
            if inspect.isawaitable(result):
                await result
            ran += 1
        except asyncio.CancelledError:
            # Shutdown, not a failure. Must propagate or a cancelled task keeps running.
            raise
        except Exception:  # noqa: BLE001 — see the module docstring
            logger.exception(
                "Handler %s failed for event %s. The publisher is unaffected.",
                getattr(handler, "__name__", handler),
                event_name,
            )

    return ran
