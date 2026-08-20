"""
Pulling records out of a source a batch at a time, and cutting a batch into chunks.

**The unit of a loop pass is a batch, not a record.** Fifty thousand records handled
one at a time is fifty thousand super-steps, fifty thousand checkpoint writes and fifty
thousand step rows; at ``batch_size = 500`` it is a hundred passes. That single decision
is what makes a large sync finish, and it is why ``MAX_BATCH_SIZE`` is enforced in
*validation* rather than merely defaulted — the batch sits in process memory.

**A batch is not a page**, and conflating the two is the mistake this module exists to
prevent. A vendor decides how many records come back in one response — Shopify says 250,
SAP says whatever the gateway feels like — and the flow's author decides how many records
one pass of their loop handles. If a batch were a page, "500 at a time" would silently
mean 250, the loop would run twice as often as the canvas says, and changing the vendor's
page parameter would change the meaning of the workflow. So :class:`PagedSupply`
accumulates across pages and carries the remainder forward.

**No HTTP here.** A supply is handed a ``fetch_page`` callable and knows nothing about
httpx, credentials or connectors; ``nodes/connector_nodes.py`` supplies the real one.
That is what lets the whole of the batching logic — the carry, the short final batch, the
page-size mismatch, the bound on passes — be tested against a list of fake pages with no
network and no fixtures.

**Bounded, always.** ``max_batches`` stops a loop whose source never says it is done.
Unbounded is not an option for something that runs unattended at three in the morning
against a rate-limited API: the failure mode is not a slow run, it is a suspended
account.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Iterator, List, Mapping, Optional, Sequence

from app.models.integrations import DEFAULT_BATCH_SIZE, MAX_BATCH_SIZE, MIN_BATCH_SIZE
from app.services.integrations.errors import NodeFailure
from app.services.integrations.runtime import pagination

logger = logging.getLogger(__name__)


@dataclass
class Page:
    """
    One response from a source.

    ``payload`` and ``headers`` are carried alongside the records because
    ``pagination.advance`` needs both — the cursor lives in the body for most APIs and in
    the ``Link`` header for Shopify — and a supply that threw them away would have to ask
    for the page twice.
    """

    records: List[Any] = field(default_factory=list)
    payload: Any = None
    headers: Mapping[str, str] = field(default_factory=dict)


class RecordSupply:
    """
    Something a ``batch`` node can pull records out of, a batch at a time.

    A class rather than an async generator, and that is deliberate: a generator would
    make "how many records has this read so far" and "why did it stop" into closure
    variables nobody can see, and both of those end up on a step row.
    """

    records_read: int = 0
    exhausted: bool = False
    stopped_because: str = ""

    async def next_batch(self, size: int) -> List[Any]:  # pragma: no cover - interface
        raise NotImplementedError

    def describe(self) -> Dict[str, Any]:
        """What the step row says about the source. Overridden where there is more."""
        return {
            "records_read": self.records_read,
            "exhausted": self.exhausted,
            "stopped_because": self.stopped_because,
        }


class ListSupply(RecordSupply):
    """
    Records already in hand.

    For a webhook body, for a trigger that carries its own payload, and for every test
    that wants to exercise the loop without a fake HTTP server. Slicing a list is not
    worth a class on its own; having *one* interface the ``batch`` node pulls from is,
    because the alternative is a runner that branches on where its records came from.
    """

    def __init__(self, records: Sequence[Any]) -> None:
        self._records = list(records)
        self._position = 0
        self.exhausted = not self._records
        self.stopped_because = "" if self._records else "there were no records to read"

    async def next_batch(self, size: int) -> List[Any]:
        taken = self._records[self._position : self._position + size]
        self._position += len(taken)
        self.records_read += len(taken)

        if self._position >= len(self._records):
            self.exhausted = True
            self.stopped_because = self.stopped_because or "every record was read"

        return taken


class PagedSupply(RecordSupply):
    """
    Records fetched a page at a time, handed out a batch at a time.

    The carry is the whole of it. A page of 250 against a batch of 500 fetches twice
    before returning; a page of 1,000 against a batch of 500 returns half and keeps the
    rest for the next pass. Either way the loop sees exactly the batch size its author
    chose, and the vendor's pagination is invisible above this line.

    ``fetch_page`` is injected. It is given the current :class:`~pagination.PageWalk` and
    returns a :class:`Page`; everything about authentication, rate limiting, retries and
    the byte cap happens inside it, in ``nodes/connector_nodes.py``. This class does the
    arithmetic and nothing else.
    """

    def __init__(
        self,
        fetch_page: Callable[[pagination.PageWalk], Awaitable[Page]],
        walk: pagination.PageWalk,
    ) -> None:
        self._fetch_page = fetch_page
        self._walk = walk
        self._carry: List[Any] = []

    @property
    def walk(self) -> pagination.PageWalk:
        """The pagination state, for the step row and for a cursor to be saved from."""
        return self._walk

    @property
    def pages_read(self) -> int:
        return self._walk.page_index

    async def next_batch(self, size: int) -> List[Any]:
        """
        Exactly ``size`` records, or fewer only because the source ran out.

        A short batch that is *not* the last one would be a lie about the page size and
        would make a hundred-pass loop take two hundred passes.
        """
        while len(self._carry) < size and not self._walk.finished:
            page = await self._fetch_page(self._walk)
            self._carry.extend(page.records)
            self._walk = pagination.advance(
                self._walk,
                payload=page.payload,
                headers=page.headers,
                records_in_page=len(page.records),
            )

        taken = self._carry[:size]
        self._carry = self._carry[size:]
        self.records_read += len(taken)

        if not self._carry and self._walk.finished:
            self.exhausted = True
            self.stopped_because = self._walk.stopped_because

        return taken

    def describe(self) -> Dict[str, Any]:
        described = super().describe()
        described.update(
            {"pages_read": self.pages_read, "carried_over": len(self._carry)}
        )
        return described


# ---------------------------------------------------------------------------
# Sizes and bounds
# ---------------------------------------------------------------------------


def batch_size_for(node_data: Mapping[str, Any], default: int = DEFAULT_BATCH_SIZE) -> int:
    """
    The batch size a node asks for, clamped and refused where it is nonsense.

    ``validate_flow`` already refuses a size outside the range, so this is the second
    line rather than the first — a version published before the rule existed, or a row
    edited by hand, must not be able to put fifty thousand records into process memory
    because a JSON field said so. Refused rather than silently clamped, because a loop
    that quietly ran at 5,000 when the canvas says 50,000 is a loop whose behaviour and
    whose drawing disagree.
    """
    raw = node_data.get("batch_size")
    if raw in (None, ""):
        return _in_range(default, DEFAULT_BATCH_SIZE)

    try:
        size = int(raw)
    except (TypeError, ValueError):
        raise NodeFailure(
            f"The batch size on this step is '{raw}', which is not a whole number. "
            f"It has to be between {MIN_BATCH_SIZE} and {MAX_BATCH_SIZE:,}."
        )

    if size < MIN_BATCH_SIZE or size > MAX_BATCH_SIZE:
        raise NodeFailure(
            f"The batch size on this step is {size:,}. It has to be between "
            f"{MIN_BATCH_SIZE} and {MAX_BATCH_SIZE:,} — a batch is held in memory while "
            "the step runs."
        )

    return size


def _in_range(value: Any, fallback: int) -> int:
    try:
        size = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(MIN_BATCH_SIZE, min(MAX_BATCH_SIZE, size))


def chunks(records: Sequence[Any], size: int) -> Iterator[List[Any]]:
    """
    Cut a batch into the pieces a write node sends concurrently.

    A separate size from the batch size, and separate for a reason: the batch is what one
    pass of the loop *holds*, and the chunk is what one request *carries*. A destination
    that accepts fifty records per call gets ten calls out of a batch of five hundred, and
    the loop still runs a hundred times rather than a thousand.
    """
    step = max(1, int(size))
    for start in range(0, len(records), step):
        yield list(records[start : start + step])


def loop_exhausted(passes: int, max_batches: int) -> bool:
    """Whether a loop has used up its allowance of passes."""
    return passes >= max(1, int(max_batches))


def loop_bound_message(node_label: str, passes: int) -> str:
    """
    What a loop says when it stops because of its bound rather than because the source
    finished.

    Worded so it cannot be mistaken for completion. "Stopped after 1,000 passes — there
    may be more" and "read everything" are very different facts about a sync, and a run
    that reports the first as the second is a backfill somebody believes finished.
    """
    return (
        f"'{node_label}' stopped after {passes:,} passes because it reached its limit. "
        "There may be more records to read — raise the limit on this step and run it "
        "again."
    )


def supply_from(value: Any) -> RecordSupply:
    """
    A supply for whatever a source node produced.

    A :class:`RecordSupply` passes through; a list becomes a :class:`ListSupply`. Anything
    else is a fault in the drawing rather than in the data, and it says so — a ``batch``
    node wired to a step that produces a single object would otherwise iterate its keys.
    """
    if isinstance(value, RecordSupply):
        return value
    if isinstance(value, (list, tuple)):
        return ListSupply(value)

    raise NodeFailure(
        "This step is set to loop over something that is not a list of records. Point it "
        "at a step that reads records."
    )


def size_of(supply: Optional[RecordSupply]) -> int:
    """How many records a supply has handed out so far. ``0`` for nothing."""
    return int(getattr(supply, "records_read", 0) or 0)
