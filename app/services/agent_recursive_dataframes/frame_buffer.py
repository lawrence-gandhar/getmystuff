"""
Where a run's records live while the graph is passing keys around.

A LangGraph node returns state, and state has to stay small: it is copied on every
super-step, and a run of 200,000 records is 250 waves of them. Putting a wave's raw
records or a 100,000-group running aggregate *in* the state would mean copying it
250 times to move it three nodes. So the records live here, in a module-level
registry keyed by the run, and the state carries the key — the same shape
``record_reader``'s reader registry and ``db_utils``'s engine cache already use,
and for the same reason.

**Keys are namespaced by run and released together.** Every key starts with the
run's id, so :func:`release_run` can drop everything a run left behind without
knowing what it stashed. The cleanup node calls it on the happy path; the
``try/finally`` in ``aggregate_graph.run_aggregation`` calls it on every other
path, including the one that matters most — a chat turn timing out cancels the
task mid-node, and a cancelled node does not route to cleanup.

**:func:`take` raises when a key is missing rather than returning nothing.** A
slice that quietly disappears is an aggregate short of 200 records that looks
complete, which is the failure this whole feature is least able to afford.
"""

import logging
from typing import Any, Dict, Optional

from app.services.deep_agents.query_executor import (
    NEEDS_RECONFIGURING,
    ToolQueryError,
)

logger = logging.getLogger(__name__)


# Every stashed object, keyed by "<run_id>:<what>". Values are whatever a node put
# there — a list of record dictionaries for a slice, a polars frame for the running
# aggregate — and this module deliberately does not care which: it is a scratch
# space with a lifecycle, not a typed store.
_frames: Dict[str, Any] = {}


def slot_key(run_id: str, wave: int, index: int) -> str:
    """
    The key for one slice of one wave.

    The wave and the slice index are both in it so a key read in a log line says
    where in the run it came from, and so two waves cannot collide if a later
    change ever lets them overlap.
    """
    return f"{run_id}:w{int(wave)}:s{int(index)}"


def partial_key(slot: str) -> str:
    """The key a worker writes its partial aggregate to, given the slice it read."""
    return f"{slot}:partial"


def running_key(run_id: str) -> str:
    """The key holding the merged aggregate so far."""
    return f"{run_id}:running"


def stash(key: str, value: Any) -> str:
    """Put ``value`` under ``key`` and return the key, so callers can inline it."""
    _frames[key] = value
    return key


def take(key: str) -> Any:
    """
    Remove and return what is under ``key``.

    Removing rather than reading is what bounds memory: a slice's raw records are
    wanted exactly once, by the worker that folds them, and holding them until the
    end of the run would make the ceiling meaningless.
    """
    if key not in _frames:
        # Loud on purpose — see the module docstring. There is no sensible
        # recovery: the records this key stood for have not been aggregated, and
        # carrying on would report a total that is quietly short.
        logger.error("Aggregation buffer key %s was gone before it was read", key)
        raise ToolQueryError(
            "Part of the data being aggregated was lost before it could be "
            "counted, so the totals would have been wrong. Nothing has been "
            "reported. Please try again.",
            advice=NEEDS_RECONFIGURING,
        )

    return _frames.pop(key)


def peek(key: str) -> Optional[Any]:
    """
    What is under ``key``, or ``None``, without removing it.

    For the running aggregate, which is read and written every wave and only
    released when the run ends.
    """
    return _frames.get(key)


def release_run(run_id: str) -> None:
    """
    Forget everything one run stashed.

    Iterates over a copy of the keys because the loop mutates the registry, and
    matches by prefix rather than tracking what was stashed — a node that adds a
    new kind of key should not also have to be added here.
    """
    prefix = f"{run_id}:"
    dropped = [key for key in list(_frames) if key.startswith(prefix)]

    for key in dropped:
        _frames.pop(key, None)

    if dropped:
        logger.debug("Released %d aggregation buffer entries for %s",
                     len(dropped), run_id)


def release_all() -> None:
    """
    Drop every run's buffers. For application shutdown, and for tests.

    Not async, unlike ``record_reader.release_all_readers``: that one closes
    database cursors and this one drops references. Called from the same place in
    ``main.py``'s shutdown so the two are read together.
    """
    _frames.clear()


def open_keys() -> int:
    """How many entries are held. For shutdown logging and for tests."""
    return len(_frames)
