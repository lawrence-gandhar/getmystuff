"""
Where a run's records live while the graph passes handles around.

LangGraph serialises the whole state to the checkpointer on **every super-step**. A
50,000-record sync at ``batch_size = 500`` is a hundred passes through a batch body, so
a batch of records sitting in ``outputs`` would be written to the checkpoint table a
hundred times to move it three nodes. That is not a tuning problem; it is the difference
between a sync that finishes and one that does not.

So the records live here, in a module-level registry keyed by the run, and the state
carries only a handle: ``{"kind": "recordset", "key": ..., "count": ...}``. Same shape
``agent_recursive_dataframes/frame_buffer.py`` uses, and the same shape ``db_utils``'
engine cache and ``record_reader``'s reader registry use — a scratch space with a
lifecycle rather than a typed store.

**Keys are namespaced by run and released together.** Every key begins with the run's
id, so :func:`release_run` can drop everything a run left behind without knowing what it
stashed. ``run_service`` calls it in a ``finally``, which is the path that matters most:
a cancelled task does not route to a cleanup node.

**:func:`take` raises when a key is missing rather than returning an empty list.** A
batch that quietly disappears is a sync that reports success having moved 49,500 of
50,000 records, and nothing about the run says otherwise. The whole three-level failure
model exists to keep that from happening, and it would be undone here by a
``dict.get(key, [])``.

**This is process memory**, which is why ``MAX_BATCH_SIZE`` is enforced in *validation*
rather than merely defaulted, and why the test suite asserts ``open_keys() == 0`` after
every run — a leak found where it happens is a test failure, and a leak found in
production is an out-of-memory kill with no explanation attached.
"""

import logging
from typing import Any, Dict, List, Optional

from app.services.integrations.errors import IntegrationFailure

logger = logging.getLogger(__name__)


# Everything any run has stashed, keyed by "<run_id>:<what>". Values are whatever a node
# put there — almost always a list of record dictionaries.
_buffers: Dict[str, Any] = {}


HANDLE_KIND = "recordset"


def batch_key(run_id: str, node_id: str, batch_index: int) -> str:
    """
    The key for one batch produced by one node.

    All three parts are in it so a key read in a log line says where in the run it came
    from, and so two nodes cannot collide by both being on their fourth pass.
    """
    return f"{run_id}:{node_id}:b{int(batch_index)}"


def handle(key: str, count: int) -> Dict[str, Any]:
    """
    The small thing that travels in state instead of the records.

    ``count`` is carried on the handle rather than looked up, so a node downstream can
    branch on "how many records is this" without touching the buffer — and so a run's
    final state still says how much moved after the buffer has been released.
    """
    return {"kind": HANDLE_KIND, "key": key, "count": int(count)}


def is_handle(value: Any) -> bool:
    return isinstance(value, dict) and value.get("kind") == HANDLE_KIND


def put(key: str, records: List[Any]) -> Dict[str, Any]:
    """Stash a batch and return the handle for it."""
    _buffers[key] = records
    return handle(key, len(records))


def stash(key: str, value: Any) -> str:
    """
    Keep something that is **not** a batch, and return the key rather than a handle.

    For an open paged supply, which a ``connector_read`` leaves for a ``batch`` node to
    pull from. A supply has no length — it has not read anything yet, and finding out
    would mean making the request this node exists to defer — so it cannot have a handle,
    and giving it one whose ``count`` was a guess would put a wrong number on the run page.

    Separate from :func:`put` rather than making that tolerant of anything: the handle's
    ``count`` is read by nodes that never touch the buffer, and a ``put`` that silently
    produced ``count: 0`` for a non-list is how a downstream branch decides there is
    nothing to do.
    """
    _buffers[key] = value
    return key


def take(key: str) -> Any:
    """
    Read a batch and **remove** it.

    Removing is the default because a batch is consumed by exactly one downstream node
    in a drawn workflow, and leaving it behind is how a hundred-pass loop ends up
    holding a hundred batches. Use :func:`peek` where a node genuinely needs to read
    without consuming.
    """
    if key not in _buffers:
        raise IntegrationFailure(
            "This step's records are no longer available. The run that produced them "
            "has finished or was stopped, so it cannot be continued from here — start "
            "it again."
        )
    return _buffers.pop(key)


def peek(key: str) -> Any:
    """Read a batch and leave it in place."""
    if key not in _buffers:
        raise IntegrationFailure(
            "This step's records are no longer available. The run that produced them "
            "has finished or was stopped, so it cannot be continued from here — start "
            "it again."
        )
    return _buffers[key]


def has(key: str) -> bool:
    return key in _buffers


def discard(key: str) -> None:
    """Drop one key, whether or not it is there. For a node cleaning up after itself."""
    _buffers.pop(key, None)


def release_run(run_id: str) -> int:
    """
    Drop everything this run stashed. Returns how many keys went.

    Called from a ``finally``, so it must never raise: a failure to clean up must not
    replace the failure that is actually being reported.
    """
    prefix = f"{run_id}:"
    keys = [key for key in _buffers if key.startswith(prefix)]
    for key in keys:
        _buffers.pop(key, None)

    if keys:
        logger.debug("Released %d buffered batch(es) for run %s", len(keys), run_id)
    return len(keys)


def open_keys(run_id: Optional[str] = None) -> List[str]:
    """
    Every key currently held, or every key held by one run.

    For the autouse test fixture that asserts a run left nothing behind, and for a
    diagnostic when somebody asks why a worker's memory is not coming back down.
    """
    if run_id is None:
        return sorted(_buffers)
    prefix = f"{run_id}:"
    return sorted(key for key in _buffers if key.startswith(prefix))


def clear_all() -> None:
    """
    Empty the whole registry.

    Test support and shutdown only. Calling this while a run is in flight loses its
    records, which is exactly the failure :func:`take` refuses to hide.
    """
    _buffers.clear()
