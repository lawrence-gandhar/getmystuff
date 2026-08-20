"""
Tests for row_supply — the two places a run's records can come from.

The graph's nodes count, open and release, and stop knowing whether they are reading a
server-side cursor or a graph's finished result. What has to hold for that to be safe is
that the materialised side keeps **the cursor's contract**, not merely something like it.
Two details of that contract are load-bearing and both are copied from
``record_reader.BatchReader`` rather than invented:

* batch numbers start at **1**, because ``initial_state`` starts ``next_batch`` there;
* an **empty** list means exhausted, a **short** one does not — the wave loop checks for
  nothing rather than for less, so a final partial batch must not look like the end.

Get the second wrong and a run silently stops at the last full batch, reporting a total
that is short by a few records and says nothing about it.
"""

from __future__ import annotations

from typing import Dict, List

import pytest

from app.services.agent_recursive_dataframes import row_supply


def _records(count: int) -> List[Dict[str, int]]:
    return [{"n": index} for index in range(1, count + 1)]


class TestCounting:
    async def test_the_count_is_exact_and_never_a_lower_bound(self) -> None:
        """
        ``is_lower_bound`` exists for a count that stopped early. Nothing stops early
        here — the rows are in hand — so the flag must be false, or the ceiling check in
        ``get_count`` would refuse a perfectly readable graph result.
        """
        counted = await row_supply.for_rows(_records(7)).count()

        assert counted.total == 7
        assert counted.is_lower_bound is False

    async def test_no_rows_at_all_counts_zero(self) -> None:
        counted = await row_supply.for_rows([]).count()

        assert counted.total == 0
        assert counted.is_lower_bound is False

    async def test_none_is_read_as_nothing(self) -> None:
        """A graph whose result could not be read hands back ``None``, not a list."""
        counted = await row_supply.for_rows(None).count()

        assert counted.total == 0


class TestTheReaderContract:
    async def test_batches_are_numbered_from_one(self) -> None:
        reader = row_supply.for_rows(_records(5)).open("k", 2)

        assert await reader.read(1) == [{"n": 1}, {"n": 2}]
        assert await reader.read(2) == [{"n": 3}, {"n": 4}]

    async def test_batch_zero_is_a_programming_error(self) -> None:
        reader = row_supply.for_rows(_records(5)).open("k", 2)

        with pytest.raises(ValueError, match="start at 1"):
            await reader.read(0)

    async def test_a_short_final_batch_is_not_the_end(self) -> None:
        """
        The detail that matters most. Five records at two per batch ends with a batch of
        one, and a loop treating "fewer than asked for" as exhausted would drop it —
        losing a record from the total with nothing saying so.
        """
        reader = row_supply.for_rows(_records(5)).open("k", 2)

        await reader.read(1)
        await reader.read(2)

        assert await reader.read(3) == [{"n": 5}]
        assert await reader.read(4) == []

    async def test_past_the_end_is_empty_rather_than_an_error(self) -> None:
        reader = row_supply.for_rows(_records(2)).open("k", 10)

        assert await reader.read(1) == [{"n": 1}, {"n": 2}]
        assert await reader.read(2) == []
        assert await reader.read(99) == []

    async def test_every_record_is_read_exactly_once_across_the_batches(self) -> None:
        supply = row_supply.for_rows(_records(23))
        reader = supply.open("k", 4)
        seen: List[int] = []
        batch = 1

        while True:
            rows = await reader.read(batch)

            if not rows:
                break

            seen.extend(row["n"] for row in rows)
            batch += 1

        assert seen == list(range(1, 24))

    async def test_the_same_key_gets_the_same_reader(self) -> None:
        """
        ``open`` is called once per wave, and a fresh reader each time would restart the
        numbering — which the graph would not notice, because it tracks the batch number
        itself. It would simply read the first batch forever.
        """
        supply = row_supply.for_rows(_records(5))

        assert supply.open("k", 2) is supply.open("k", 2)

    async def test_a_batch_size_of_zero_does_not_divide_by_zero(self) -> None:
        reader = row_supply.for_rows(_records(3)).open("k", 0)

        assert await reader.read(1) == [{"n": 1}]


class TestReleasing:
    async def test_releasing_is_safe_and_repeatable(self) -> None:
        """
        ``cleanup`` releases and so does ``run_aggregation``'s ``finally`` — the tidy path
        and the guarantee — so a double release is the ordinary case, not an edge one.
        """
        supply = row_supply.for_rows(_records(3))
        supply.open("k", 2)

        await supply.release("k")
        await supply.release("k")

    async def test_releasing_a_key_that_was_never_opened_is_fine(self) -> None:
        """A run cancelled before its first read never opened anything."""
        await row_supply.for_rows(_records(3)).release("never")


class TestTheSubjects:
    """
    The word a refusal uses for the source. Not cosmetic: ``too_large_message`` tells the
    operator what to narrow, and pointing a graph's owner at "the tool's filters" sends
    them to a page that has nothing to do with it.
    """

    def test_a_query_supply_calls_itself_a_tool(self) -> None:
        assert row_supply.for_sources([]).subject == "tool"

    def test_a_materialised_supply_calls_itself_a_graph(self) -> None:
        assert row_supply.for_rows([]).subject == "graph"
