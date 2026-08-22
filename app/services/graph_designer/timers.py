"""
Stopwatches, and the one place a graph run is allowed to wait.

Two node types are served from here. A ``timer`` measures how long the run took to
get from one box to another — started once, paused and resumed any number of times,
stopped once — and never blocks. A ``wait`` blocks, and measures nothing.

**The clock and the sleep are seams.** ``now()`` and ``sleep()`` are module-level
functions so a test can replace them; the alternative is a test suite whose runtime is
the sum of the durations it exercises. The same call
``app/services/integrations/engine/scheduler.py`` makes at its own ``now()``.

Callers must reach them through the module::

    from app.services.graph_designer import timers
    moment = timers.now()

and never ``from app.services.graph_designer.timers import now``. The second form
binds the function object at import, so ``monkeypatch.setattr(timers, "now", ...)``
replaces something the caller is no longer looking at and the test silently uses the
real clock. That is the whole reason this file exists as a file.

Nothing here touches the database, LangGraph or Litestar, and every transition takes
its ``moment`` as an argument rather than reading the clock itself — so the unit tests
pass fixed datetimes and patch nothing at all. ``TimerError`` is raised here and
translated to a ``NodeFailure`` by the runner, the split ``email_dispatch`` already
makes with ``EmailFailure``: this module does not import the thing that calls it.

Times are stored as ISO 8601 strings, never ``datetime`` objects. The run's state is
checkpointed and previewed into JSONB, and a ``datetime`` in there is a value whose
round trip depends on which driver wrote it.
"""

import asyncio
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence

# --------------------------------------------------------------------------
# Limits
# --------------------------------------------------------------------------

# The longest a `wait` node may pause a run for.
#
# 900 seconds, and the number is chosen against three others. It is under
# `graph_run_service.MAX_STREAM_SECONDS` (3600), so the dock's progress stream outlives
# any single wait rather than disconnecting mid-pause. It is under the integrations
# engine's `MAX_NODE_TIMEOUT_SECONDS` (3600), which keeps this canvas the more
# conservative of the two. And it is short enough that a deploy rarely lands inside one
# — which matters, because a wait does **not** survive a restart: `stop_all_runs()`
# cancels every live run on shutdown and nothing resumes it.
#
# A longer wait is refused at save, never clamped. Clamping would leave the drawing
# saying two hours while the run waited fifteen minutes, and a picture that lies about
# what the run does is worse than a refusal that says why. Anything measured in hours
# belongs in an Integrations schedule, which is persisted and does survive a restart.
MAX_WAIT_SECONDS = 900

# What a new `wait` node is created holding. Long enough to be useful for the common
# case (giving another system a moment to catch up), short enough that dropping one in
# by accident is not a fifteen-minute mistake.
DEFAULT_WAIT_SECONDS = 30


# --------------------------------------------------------------------------
# Where a timer is in its life
# --------------------------------------------------------------------------
# Three phases, not a pair of booleans. "Running" and "paused" and "stopped" are
# mutually exclusive, and two booleans can represent a fourth state that means nothing.
PHASE_RUNNING = "running"
PHASE_PAUSED = "paused"
PHASE_STOPPED = "stopped"


class TimerError(Exception):
    """
    A timer was asked to do something its current phase does not allow.

    Always an authoring mistake — stopping a timer nobody started, resuming one that
    was never paused — so the message names the node and says what to do instead. The
    runner turns it into a ``NodeFailure``; this module does not know what a node is.
    """


# --------------------------------------------------------------------------
# The seams
# --------------------------------------------------------------------------

def now() -> datetime:
    """
    The current instant, timezone-aware in UTC.

    A function rather than a call site so tests can replace it. Aware rather than naive
    because these values are subtracted from each other and serialised, and a naive
    datetime makes both operations depend on where the server is.
    """
    return datetime.now(timezone.utc)


async def sleep(seconds: float) -> None:
    """
    Wait, without blocking the event loop.

    A function rather than a call site so tests can replace it — see the module
    docstring. Nothing in the test suite may await the real one.
    """
    await asyncio.sleep(seconds)


# --------------------------------------------------------------------------
# Reading and writing the stored form
# --------------------------------------------------------------------------

def _iso(moment: datetime) -> str:
    """One instant, as it is stored."""
    return moment.isoformat()


def _parsed(raw: Any, *, label: str) -> datetime:
    """
    One stored instant, back as a datetime.

    A stored value that will not parse means the run's state was written by something
    other than this module, so it is a failure with a sentence rather than a
    ``ValueError`` from deep inside the arithmetic.
    """
    try:
        moment = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError) as exc:
        raise TimerError(
            f"The timer '{label}' has a recorded time that could not be read. "
            "Start it again."
        ) from exc

    # A value written before this module always wrote aware datetimes, or by a store
    # that dropped the offset. UTC is the only assumption available and it is the one
    # every writer here makes.
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)

    return moment


def _seconds_between(start: datetime, end: datetime) -> float:
    """A span, in seconds, never negative and never noisy.

    Clamped at zero because a clock that steps backwards should shorten a measurement
    to nothing rather than report a negative duration nobody can act on. Rounded
    because these land in JSONB and a preview full of float noise is harder to read
    for no gain.
    """
    return round(max(0.0, (end - start).total_seconds()), 3)


def _pass_seconds(record: Mapping[str, Any], moment: datetime) -> float:
    """
    How long **this pass** has actually been counting, excluding paused spans.

    The segment list holds the spans the clock was ticking, so the sum of the segments
    *is* the elapsed time and the paused time is what is left over. One list and one
    subtraction, rather than a second list of pauses that could disagree with the first.
    """
    label = str(record.get("label") or "")
    total = 0.0

    for segment in record.get("segments") or []:
        if segment.get("ended_at"):
            total += float(segment.get("seconds") or 0.0)
            continue

        # The open segment, if there is one: it has been running until `moment`.
        started = _parsed(segment.get("started_at"), label=label)
        total += _seconds_between(started, moment)

    return round(total, 3)


def _measured(record: dict, moment: datetime) -> dict:
    """
    Stamp a record with what it currently measures.

    Done once per transition rather than on every read, so the number in the state, the
    number in the snapshot and the number in the preview are the same number and cannot
    drift by the microseconds between three separate calls to the clock.
    """
    label = str(record.get("label") or "")
    elapsed = _pass_seconds(record, moment)

    ended = record.get("ended_at")
    finish = _parsed(ended, label=label) if ended else moment
    wall = _seconds_between(_parsed(record.get("started_at"), label=label), finish)

    record["elapsed_seconds"] = elapsed
    # `wall` and `elapsed` are two independent subtractions of the same clock, so on a
    # timer that was never paused they can disagree in the last microsecond. The
    # clamp is what stops that showing up as "paused for -0.000001 seconds".
    record["paused_seconds"] = round(max(0.0, wall - elapsed), 3)
    record["total_elapsed_seconds"] = round(
        float(record.get("carried_seconds") or 0.0) + elapsed, 3
    )

    return record


def _open_segment(segments: Sequence[Mapping[str, Any]]) -> Optional[int]:
    """The index of the segment still running, or ``None`` if the clock is not ticking."""
    for index, segment in enumerate(segments):
        if not segment.get("ended_at"):
            return index

    return None


def _closed(record: Mapping[str, Any], moment: datetime) -> list:
    """This record's segments with the open one shut at ``moment``."""
    label = str(record.get("label") or "")
    segments = [dict(segment) for segment in (record.get("segments") or [])]
    index = _open_segment(segments)

    if index is None:
        return segments

    started = _parsed(segments[index].get("started_at"), label=label)
    segments[index]["ended_at"] = _iso(moment)
    segments[index]["seconds"] = _seconds_between(started, moment)

    return segments


def _phase_of(record: Mapping[str, Any]) -> str:
    """Which phase a stored record is in, tolerating one written by an older deploy."""
    phase = str(record.get("phase") or "")

    return phase if phase in {PHASE_RUNNING, PHASE_PAUSED, PHASE_STOPPED} else PHASE_RUNNING


# --------------------------------------------------------------------------
# Transitions
# --------------------------------------------------------------------------
# Every one returns a **new** record rather than editing the one it was handed. The
# run's state belongs to LangGraph, which merges what a node returns; editing the
# mapping in place would change a value the reducer has not been told about.

def started(label: str, node_id: str, moment: datetime) -> dict:
    """Begin timing. One open segment, nothing carried, nothing paused."""
    record = {
        "timer_node": node_id,
        "label": label,
        "started_at": _iso(moment),
        "ended_at": None,
        "phase": PHASE_RUNNING,
        "segments": [{"started_at": _iso(moment), "ended_at": None, "seconds": None}],
        "restarts": 0,
        "carried_seconds": 0.0,
    }

    return _measured(record, moment)


def restarted(record: Mapping[str, Any], moment: datetime) -> dict:
    """
    Begin timing again, on a later pass of a loop.

    What the earlier passes measured is folded into ``carried_seconds`` rather than
    thrown away, so ``elapsed_seconds`` answers "how long did *this* pass take" and
    ``total_elapsed_seconds`` answers "how long has this loop spent in here altogether".
    Reporting only one of those would make the other unobtainable.
    """
    carried = round(
        float(record.get("carried_seconds") or 0.0) + _pass_seconds(record, moment), 3
    )

    restarted_record = {
        "timer_node": record.get("timer_node"),
        "label": record.get("label"),
        "started_at": _iso(moment),
        "ended_at": None,
        "phase": PHASE_RUNNING,
        "segments": [{"started_at": _iso(moment), "ended_at": None, "seconds": None}],
        "restarts": int(record.get("restarts") or 0) + 1,
        "carried_seconds": carried,
    }

    return _measured(restarted_record, moment)


def paused(record: Mapping[str, Any], moment: datetime) -> dict:
    """Stop the clock without ending the measurement. Refuses anything but a running timer."""
    label = str(record.get("label") or "")
    phase = _phase_of(record)

    if phase == PHASE_PAUSED:
        raise TimerError(f"The timer '{label}' is already paused.")

    if phase == PHASE_STOPPED:
        raise TimerError(
            f"The timer '{label}' has already been stopped, so it cannot be paused."
        )

    paused_record = dict(record)
    paused_record["segments"] = _closed(record, moment)
    paused_record["phase"] = PHASE_PAUSED

    return _measured(paused_record, moment)


def resumed(record: Mapping[str, Any], moment: datetime) -> dict:
    """Start the clock again after a pause. Refuses a timer that was not paused."""
    label = str(record.get("label") or "")
    phase = _phase_of(record)

    if phase == PHASE_RUNNING:
        raise TimerError(
            f"The timer '{label}' is already running. A timer can only be resumed "
            "after it has been paused."
        )

    if phase == PHASE_STOPPED:
        raise TimerError(
            f"The timer '{label}' has already been stopped, so it cannot be resumed. "
            "Use a Timer set to Start if you want to measure again."
        )

    resumed_record = dict(record)
    resumed_record["segments"] = [
        *(dict(segment) for segment in (record.get("segments") or [])),
        {"started_at": _iso(moment), "ended_at": None, "seconds": None},
    ]
    resumed_record["phase"] = PHASE_RUNNING

    return _measured(resumed_record, moment)


def stopped(record: Mapping[str, Any], moment: datetime) -> dict:
    """
    End the measurement.

    Legal from running *and* from paused: stopping a paused timer is an ordinary thing
    to draw, and the elapsed time is already correct because the paused span was never
    in a segment.
    """
    label = str(record.get("label") or "")

    if _phase_of(record) == PHASE_STOPPED:
        raise TimerError(f"The timer '{label}' has already been stopped.")

    stopped_record = dict(record)
    stopped_record["segments"] = _closed(record, moment)
    stopped_record["ended_at"] = _iso(moment)
    stopped_record["phase"] = PHASE_STOPPED

    return _measured(stopped_record, moment)


# --------------------------------------------------------------------------
# What a timer node reports
# --------------------------------------------------------------------------

def snapshot(record: Mapping[str, Any], action: str) -> dict:
    """
    What one timer node writes into ``outputs``, for a later node to read.

    **Flat, deliberately.** A downstream binding reads it with a dotted path, so
    ``elapsed_human`` has to be one hop from the top or an email's variable row has to
    know this module's internal nesting. The record itself is not returned; this is the
    reportable view of it.

    Takes no ``moment``: the record was already stamped by the transition that produced
    it, and reading the clock a second time here is how the log ends up disagreeing
    with the state by a microsecond.
    """
    return {
        "action": action,
        "timer": record.get("label") or "",
        "started_at": record.get("started_at"),
        "ended_at": record.get("ended_at"),
        "elapsed_seconds": record.get("elapsed_seconds"),
        "total_elapsed_seconds": record.get("total_elapsed_seconds"),
        "paused_seconds": record.get("paused_seconds"),
        "elapsed_human": elapsed_text(record.get("elapsed_seconds")),
        "running": _phase_of(record) == PHASE_RUNNING,
        "phase": _phase_of(record),
        "restarts": int(record.get("restarts") or 0),
        "segments": list(record.get("segments") or []),
    }


def elapsed_text(seconds: Any) -> str:
    """
    A duration a person can read, for an email to quote.

    This exists because the obvious binding — ``elapsed_seconds`` — renders
    ``3852.117`` into a sentence, and no operator wants to write a template that turns
    that into "1h 4m 12s" by hand. Formatting the *datetimes* for humans is deliberately
    not done here: that needs a timezone and a locale, which are the reader's, not the
    run's.
    """
    try:
        total = max(0.0, float(seconds))
    except (TypeError, ValueError):
        return ""

    if total < 1:
        return f"{total:.2f}s"

    hours, rest = divmod(int(total), 3600)
    minutes, secs = divmod(rest, 60)

    parts = []

    if hours:
        parts.append(f"{hours}h")

    if minutes:
        parts.append(f"{minutes}m")

    if secs or not parts:
        parts.append(f"{secs}s")

    return " ".join(parts)


# --------------------------------------------------------------------------
# What a wait node is allowed to be
# --------------------------------------------------------------------------

def validated_wait_seconds(raw: Any, label: str) -> int:
    """
    How long a ``wait`` node pauses for, or a refusal saying why not.

    Shared by the save-time validator and the runner rather than trusted once. A
    ``graph_data`` row is JSONB and can be edited by hand, and this function's ceiling
    is the only thing standing between that and a run parked for a week — there is no
    ``asyncio.wait_for`` around a runner in this package. The same reason ``_run_value``
    re-parses its JSON instead of trusting the save.
    """
    if isinstance(raw, bool) or raw is None or raw == "":
        raise TimerError(
            f"'{label}' does not say how long to wait. Enter a number of seconds."
        )

    try:
        seconds = int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise TimerError(
            f"'{label}' has '{raw}' as its wait, which is not a number of seconds."
        ) from exc

    if seconds < 1:
        raise TimerError(
            f"'{label}' is set to wait {seconds} seconds. A wait must be at least "
            "one second — remove the node if it should not pause at all."
        )

    if seconds > MAX_WAIT_SECONDS:
        raise TimerError(
            f"'{label}' is set to wait {seconds} seconds, which is longer than the "
            f"{MAX_WAIT_SECONDS} a graph run may pause for. A run does not survive a "
            "restart, so a wait measured in hours belongs in an Integrations schedule "
            "rather than in a graph."
        )

    return seconds
