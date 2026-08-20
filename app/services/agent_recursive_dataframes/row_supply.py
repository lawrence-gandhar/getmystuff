"""
Where a run's records come from, behind one shape the graph can read.

The aggregation graph reads batches, counts before it starts, and closes what it
opened. Until now there was one thing that could do all three — a list of
``RecordSource``s read through ``record_reader`` — and the graph called that module
directly. Then a **graph** became something whose result can be filtered, and a designed
graph is not a query:

| | a tool config | a Graph Designer graph |
|---|---|---|
| the records | a statement, streamed off one server-side cursor | already produced, held in memory, read from the checkpointer |
| the count | ``COUNT(*)``, before anything is read | ``len()`` of what was produced |
| the columns | probed with a one-row fetch | taken off the result |
| what "close" means | release the cursor to the pool | nothing; there was never a cursor |

Both are legitimate and neither is the other's special case, which is what this module
is for. The graph's nodes ask a **supply** to count, to open, and to release, and stop
knowing which of the two they have — so the wave loop keeps reading "until there are no
more records" rather than gaining a second version of itself.

**The materialised side is not a shortcut around the ceilings.** ``count()`` answers
before any folding starts exactly as the query side does, so
``AGGREGATE_MAX_SOURCE_ROWS`` refuses an oversized graph result in the same node with
the same sentence. What it genuinely does skip is the count *round trip*, because the
rows are already here — there is nothing to ask.
"""

import logging
from typing import Any, Dict, List, Mapping, Optional, Sequence

from app.services.downloader_agents.base import record_reader
from app.services.downloader_agents.base.record_reader import (
    RecordCount,
    RecordSource,
)

logger = logging.getLogger(__name__)


class QuerySupply:
    """
    Records from one or more stored queries — what this feature read before graphs.

    A thin pass-through to ``record_reader`` on purpose. Every decision about cursors,
    chaining and counting stays in that module, which is also the export path's, so an
    aggregation reads exactly what an export would.
    """

    #: How the run refers to its source in a refusal. "This tool returns 4m records…"
    subject = "tool"

    def __init__(self, sources: Sequence[RecordSource]) -> None:
        self.sources = list(sources)

    async def count(self) -> RecordCount:
        return await record_reader.count_all(self.sources)

    def open(self, key: str, batch_size: int) -> Any:
        """
        Not async, mirroring ``record_reader.get_reader``: creating a reader opens
        nothing, so a run cancelled before its first read leaves no connection behind.
        """
        return record_reader.get_reader(key, self.sources, batch_size=batch_size)

    async def release(self, key: str) -> None:
        await record_reader.release_reader(key)


class MaterialisedSupply:
    """
    Records that already exist — a graph's whole result, read from the checkpointer.

    ``open`` hands back a reader with the same two methods a cursor-backed one has, so
    the wave loop, the fan-out and the cleanup node are unchanged. The batching is real
    rather than decorative: a 200,000-row graph result folded in one go would hold the
    whole frame *and* the whole row list at once, and the point of the wave loop is that
    it does not.

    Nothing is registered globally. A cursor has to live outside graph state because it
    cannot be serialised; a list does not, and a registry entry for it would be a leak
    with no upside — so ``release`` genuinely has nothing to do.
    """

    subject = "graph"

    def __init__(self, rows: Sequence[Mapping[str, Any]]) -> None:
        self.rows = list(rows)
        self._readers: Dict[str, "_ListReader"] = {}

    async def count(self) -> RecordCount:
        """
        Exact, and never a lower bound.

        ``is_lower_bound`` exists for a count that stopped early — see ``RecordCount``.
        Nothing stopped early here: the rows are in hand, so ``len`` is the number.
        """
        return RecordCount(total=len(self.rows))

    def open(self, key: str, batch_size: int) -> "_ListReader":
        reader = self._readers.get(key)

        if reader is None:
            reader = _ListReader(self.rows, batch_size=batch_size)
            self._readers[key] = reader

        return reader

    async def release(self, key: str) -> None:
        """Drop the reader. There is no cursor to close and no pool to return to."""
        self._readers.pop(key, None)


class _ListReader:
    """
    ``read(batch_number)`` over a list, with ``record_reader.BatchReader``'s contract.

    Two details are copied from that class rather than invented, because the graph
    depends on both: batch numbers start at **1** (``next_batch`` in the initial state
    does), and an **empty list means exhausted** while a short one does not — the wave
    loop checks for nothing rather than for less, so a final partial batch must not look
    like the end.
    """

    def __init__(self, rows: Sequence[Mapping[str, Any]], batch_size: int) -> None:
        self._rows = rows
        self._batch_size = max(1, int(batch_size))

    async def read(self, batch_number: int) -> List[Mapping[str, Any]]:
        if batch_number < 1:
            raise ValueError("Batch numbers start at 1.")

        start = (batch_number - 1) * self._batch_size

        return list(self._rows[start:start + self._batch_size])

    async def close(self) -> None:
        """Nothing is open. Present because the graph's cleanup node calls it."""
        return None


def for_rows(rows: Optional[Sequence[Mapping[str, Any]]]) -> MaterialisedSupply:
    """A supply over an in-memory result, tolerating ``None`` as "nothing at all"."""
    return MaterialisedSupply(list(rows or []))


def for_sources(sources: Sequence[RecordSource]) -> QuerySupply:
    """A supply over stored queries."""
    return QuerySupply(sources)
