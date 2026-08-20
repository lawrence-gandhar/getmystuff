"""
Tests for ``engine/batching.py``.

**A batch is not a page.** That is the property the whole module exists to hold, and it
is the first class here. A vendor decides how many records come back in one response; the
author decides how many one pass of their loop handles. If those were the same thing,
"500 at a time" would silently mean 250, the loop would run twice as often as the canvas
says, and changing the vendor's page parameter would change the meaning of the workflow.

The rest follow from it: a short batch only ever means the source ran out, the carry
survives between passes, and the whole thing is exercised against a list of fake pages —
no network, no fixtures — because ``fetch_page`` is injected.
"""

from __future__ import annotations

from typing import List

import pytest

from app.models.integrations import MAX_BATCH_SIZE, MIN_BATCH_SIZE
from app.services.integrations.connectors.spec import PAGE_NONE, PAGE_NUMBER, PageRule
from app.services.integrations.engine import batching
from app.services.integrations.errors import NodeFailure
from app.services.integrations.runtime import pagination


def records(start: int, count: int) -> List[dict]:
    return [{"n": index} for index in range(start, start + count)]


def pager(pages: List[List[dict]], *, rule: PageRule = None):  # noqa: ANN201
    """A ``fetch_page`` over a fixed list of pages, counting the calls."""
    calls: List[int] = []

    async def fetch_page(walk: pagination.PageWalk) -> batching.Page:
        index = len(calls)
        calls.append(index)
        page = pages[index] if index < len(pages) else []
        return batching.Page(records=page, payload={"page": index}, headers={})

    walk = pagination.begin(
        rule or PageRule(kind=PAGE_NUMBER, param="page", max_pages=50), "https://x/y"
    )
    return fetch_page, walk, calls


class TestABatchIsNotAPage:
    async def test_a_small_page_size_does_not_shrink_the_batch(self) -> None:
        """
        The headline. Four pages of 250 against a batch of 500 gives two batches of 500,
        not four of 250 — otherwise the loop runs twice as often as the canvas says and
        the vendor's page parameter silently changes what the workflow means.
        """
        fetch, walk, calls = pager([records(i * 250, 250) for i in range(4)] + [[]])
        supply = batching.PagedSupply(fetch, walk)

        first = await supply.next_batch(500)
        second = await supply.next_batch(500)

        assert len(first) == 500
        assert len(second) == 500
        assert len(calls) == 4, "two pages were fetched for each batch"

    async def test_a_large_page_is_carried_over(self) -> None:
        """A page of 1,000 against a batch of 500 returns half and keeps the rest. Without
        the carry the second half would be refetched or lost."""
        fetch, walk, calls = pager([records(0, 1000), []])
        supply = batching.PagedSupply(fetch, walk)

        first = await supply.next_batch(500)
        assert [row["n"] for row in first] == list(range(500))
        assert len(calls) == 1

        second = await supply.next_batch(500)
        assert [row["n"] for row in second] == list(range(500, 1000))
        assert len(calls) == 1, "the carry was used rather than a second request"

    async def test_a_short_batch_only_ever_means_the_source_ran_out(self) -> None:
        fetch, walk, _ = pager([records(0, 250), records(250, 100), []])
        supply = batching.PagedSupply(fetch, walk)

        first = await supply.next_batch(500)

        assert len(first) == 350
        assert supply.exhausted is True

    async def test_nothing_is_requested_until_the_first_batch_is_asked_for(self) -> None:
        """Why a ``connector_read`` node cannot time out on a slow API, and why a run
        cancelled early has fetched one page rather than all of them."""
        fetch, walk, calls = pager([records(0, 10)])
        batching.PagedSupply(fetch, walk)

        assert calls == []


class TestExhaustion:
    async def test_the_reason_is_kept(self) -> None:
        """"Stopped at 1,000 pages" and "the API said that was the last one" are very
        different facts about a sync, and only one of them means everything was read."""
        fetch, walk, _ = pager([records(0, 5), []])
        supply = batching.PagedSupply(fetch, walk)

        await supply.next_batch(500)

        assert supply.exhausted is True
        assert supply.stopped_because

    async def test_a_page_limit_stops_the_walk(self) -> None:
        rule = PageRule(kind=PAGE_NUMBER, param="page", max_pages=2)
        fetch, walk, calls = pager([records(i * 10, 10) for i in range(9)], rule=rule)
        supply = batching.PagedSupply(fetch, walk)

        await supply.next_batch(500)

        assert len(calls) == 2
        assert "2 pages" in supply.stopped_because
        assert "there may be more" in supply.stopped_because.lower()

    async def test_an_unpaged_source_reads_once(self) -> None:
        fetch, walk, calls = pager(
            [records(0, 7), records(7, 7)], rule=PageRule(kind=PAGE_NONE)
        )
        supply = batching.PagedSupply(fetch, walk)

        assert len(await supply.next_batch(500)) == 7
        assert len(calls) == 1

    async def test_asking_after_exhaustion_gives_nothing(self) -> None:
        fetch, walk, _ = pager([records(0, 3), []])
        supply = batching.PagedSupply(fetch, walk)

        await supply.next_batch(500)
        assert await supply.next_batch(500) == []


class TestListSupply:
    async def test_a_list_is_handed_out_in_batches(self) -> None:
        supply = batching.ListSupply(records(0, 12))

        assert len(await supply.next_batch(5)) == 5
        assert len(await supply.next_batch(5)) == 5
        assert len(await supply.next_batch(5)) == 2
        assert supply.exhausted is True

    async def test_an_empty_list_is_exhausted_immediately(self) -> None:
        supply = batching.ListSupply([])

        assert supply.exhausted is True
        assert await supply.next_batch(10) == []

    async def test_records_read_is_counted(self) -> None:
        supply = batching.ListSupply(records(0, 7))
        await supply.next_batch(4)
        assert supply.records_read == 4


class TestBatchSize:
    def test_the_default_is_used_when_nothing_is_set(self) -> None:
        assert batching.batch_size_for({}, 250) == 250

    def test_an_out_of_range_size_is_refused_not_clamped(self) -> None:
        """
        A loop that quietly ran at 5,000 when the canvas says 50,000 is a loop whose
        behaviour and whose drawing disagree — and a batch is held in process memory, so
        the number is not merely cosmetic.
        """
        with pytest.raises(NodeFailure, match="has to be between"):
            batching.batch_size_for({"batch_size": MAX_BATCH_SIZE + 1})

        with pytest.raises(NodeFailure, match="has to be between"):
            batching.batch_size_for({"batch_size": MIN_BATCH_SIZE - 1})

    def test_something_that_is_not_a_number_is_refused(self) -> None:
        with pytest.raises(NodeFailure, match="not a whole number"):
            batching.batch_size_for({"batch_size": "lots"})

    def test_a_legal_size_is_honoured(self) -> None:
        assert batching.batch_size_for({"batch_size": 1000}) == 1000


class TestChunks:
    def test_a_batch_is_cut_into_request_sized_pieces(self) -> None:
        """The chunk is what one *request* carries; the batch is what one *pass* holds. A
        destination taking fifty at a time gets ten calls out of five hundred, and the
        loop still runs a hundred times rather than a thousand."""
        pieces = list(batching.chunks(records(0, 12), 5))
        assert [len(piece) for piece in pieces] == [5, 5, 2]

    def test_a_zero_size_does_not_loop_forever(self) -> None:
        assert len(list(batching.chunks(records(0, 3), 0))) == 3

    def test_an_empty_batch_yields_nothing(self) -> None:
        assert list(batching.chunks([], 10)) == []


class TestBounds:
    def test_a_loop_is_exhausted_at_its_limit(self) -> None:
        assert batching.loop_exhausted(999, 1000) is False
        assert batching.loop_exhausted(1000, 1000) is True

    def test_the_bound_message_cannot_be_read_as_completion(self) -> None:
        """"Read everything" and "stopped after 1,000 passes" are different facts, and a
        run reporting the second as the first is a backfill somebody believes finished."""
        message = batching.loop_bound_message("Loop", 1000)
        assert "there may be more" in message.lower()
        assert "limit" in message.lower()


class TestSupplyFrom:
    def test_a_list_becomes_a_supply(self) -> None:
        assert isinstance(batching.supply_from([1, 2]), batching.ListSupply)

    def test_a_supply_passes_through(self) -> None:
        supply = batching.ListSupply([1])
        assert batching.supply_from(supply) is supply

    def test_a_single_object_is_refused_rather_than_iterated(self) -> None:
        """A ``batch`` node wired to a step that produces one object would otherwise loop
        over its keys."""
        with pytest.raises(NodeFailure, match="not a list of records"):
            batching.supply_from({"id": 1})
