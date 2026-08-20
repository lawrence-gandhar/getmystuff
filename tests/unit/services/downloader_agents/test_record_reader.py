"""
Tests for app/services/downloader_agents/base/record_reader.py.

Two behaviours carry this module and both are asserted against a real SQLite database
rather than a mock:

* **The count is exact.** The whole feature rests on being able to tell a user how many
  records there are, so a count that is off by one is a lie in front of a person. Both
  query modes are checked, and so is a grouped query — where the honest answer is the
  number of groups, not the number of underlying rows.
* **Every record is read exactly once.** Batching is where an export silently loses or
  duplicates data, so the boundaries (49, 50, 51, 100) are asserted by *identity* — the
  set of ids read back — not just by count. A reader that returned row 50 twice and
  dropped row 51 would pass a length check.

The out-of-order read is the retry path, and it gets its own test: it is the only code in
the module that re-opens a cursor, and the thing it must not do is come back with the
wrong window.
"""

from __future__ import annotations

from typing import Callable

import pytest

from app.services.deep_agents.query_executor import ToolQueryError
from app.services.downloader_agents.base import record_reader as reader


async def _read_all(batch_reader, batch_size: int = 50) -> list:  # noqa: ANN001
    """Every record a reader yields, and how many batches it took."""
    rows: list = []
    batches = 0
    number = 1

    while True:
        batch = await batch_reader.read(number)

        if not batch:
            return rows, batches

        assert len(batch) <= batch_size
        rows.extend(batch)
        batches += 1
        number += 1


# ---- Counting ----

class TestCountRecords:
    @pytest.mark.parametrize("rows", [0, 1, 49, 50, 51, 125])
    async def test_a_built_query_counts_exactly(
        self, datasource_row: Callable, rows: int,
    ) -> None:
        source = reader.RecordSource(
            datasource=datasource_row(rows), config={}, table_name="items",
        )

        counted = await reader.count_records(source)

        assert counted.total == rows
        assert counted.is_lower_bound is False

    async def test_a_sql_query_counts_exactly(
        self, datasource_row: Callable,
    ) -> None:
        source = reader.RecordSource(
            datasource=datasource_row(125),
            config={},
            table_name="items",
            sql_query="SELECT id, name FROM items WHERE qty > 0",
        )

        counted = await reader.count_records(source)

        # 125 records, one in every seven has qty 0.
        assert counted.total == 108
        assert counted.is_lower_bound is False

    async def test_a_grouped_query_counts_groups_not_rows(
        self, datasource_row: Callable,
    ) -> None:
        """
        The honest total for a grouped tool is the number of groups.

        It is what the tool returns, so it is what an export of the tool contains — and
        reporting 125 for a query that produces 7 rows would be a number the user could
        not reconcile with anything they were shown.
        """
        source = reader.RecordSource(
            datasource=datasource_row(125),
            config={
                "columns": [{"column": "qty", "alias": ""}],
                "aggregations": [{"type": "count", "column": "id", "alias": "total"}],
                "group_by": ["qty"],
            },
            table_name="items",
        )

        counted = await reader.count_records(source)

        assert counted.total == 7

    async def test_a_count_past_the_ceiling_is_a_lower_bound(
        self, datasource_row: Callable, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        Past the ceiling the count stops and says so.

        ``exceeds_ceiling`` is what refuses the export, and it must be true for both the
        "counted more than the limit" case and the "stopped counting" case — the caller
        should not have to know which happened.
        """
        monkeypatch.setattr(reader, "MAX_EXPORT_ROWS", 10)

        source = reader.RecordSource(
            datasource=datasource_row(125),
            config={},
            table_name="items",
            # SQL mode, because that is the path that counts by streaming.
            sql_query="SELECT id FROM items",
        )

        counted = await reader.count_records(source)

        assert counted.is_lower_bound is True
        assert counted.exceeds_ceiling is True
        assert counted.total == 11  # the ceiling plus the one row that proved it

    async def test_a_non_relational_datasource_is_refused(
        self, datasource_row: Callable,
    ) -> None:
        datasource = datasource_row(10)
        datasource.db_type = "mongodb"

        source = reader.RecordSource(
            datasource=datasource, config={}, table_name="items",
        )

        with pytest.raises(ToolQueryError) as excinfo:
            await reader.count_records(source)

        assert "only relational databases" in str(excinfo.value)


# ---- Reading ----

class TestBatchReader:
    @pytest.mark.parametrize(
        ("rows", "expected_batches"),
        [(1, 1), (49, 1), (50, 1), (51, 2), (100, 2), (125, 3)],
    )
    async def test_every_record_is_read_exactly_once(
        self, datasource_row: Callable, rows: int, expected_batches: int,
    ) -> None:
        source = reader.RecordSource(
            datasource=datasource_row(rows), config={}, table_name="items",
        )
        batch_reader = reader.BatchReader(source)

        try:
            read, batches = await _read_all(batch_reader)
        finally:
            await batch_reader.close()

        assert batches == expected_batches
        # By identity, not by count: a reader that repeated one row and dropped another
        # would have the right length and the wrong contents.
        assert [row["id"] for row in read] == list(range(1, rows + 1))

    async def test_an_empty_result_reads_no_batches(
        self, datasource_row: Callable,
    ) -> None:
        source = reader.RecordSource(
            datasource=datasource_row(0), config={}, table_name="items",
        )
        batch_reader = reader.BatchReader(source)

        try:
            read, batches = await _read_all(batch_reader)
        finally:
            await batch_reader.close()

        assert (read, batches) == ([], 0)

    async def test_a_sql_mode_query_is_read_in_batches(
        self, datasource_row: Callable,
    ) -> None:
        """
        SQL mode reads through the same held-open cursor as builder mode.

        Worth asserting separately: it is the mode that cannot be paginated with
        LIMIT/OFFSET at all, so if the cursor were not held the batches would restart
        from the beginning every time.
        """
        source = reader.RecordSource(
            datasource=datasource_row(125),
            config={},
            table_name="items",
            sql_query="SELECT id, name FROM items WHERE qty > 0",
        )
        batch_reader = reader.BatchReader(source)

        try:
            read, batches = await _read_all(batch_reader)
        finally:
            await batch_reader.close()

        assert batches == 3
        assert len(read) == 108
        assert len({row["id"] for row in read}) == 108

    async def test_re_reading_a_batch_returns_that_same_window(
        self, datasource_row: Callable,
    ) -> None:
        """
        The retry path: asking for a batch out of order re-opens and re-seeks.

        This is the one operation that could quietly return the wrong fifty records, and
        an export whose batch 2 contained batch 3's rows would be wrong in a way nothing
        downstream notices.
        """
        source = reader.RecordSource(
            datasource=datasource_row(125), config={}, table_name="items",
        )
        batch_reader = reader.BatchReader(source)

        try:
            first_pass = await batch_reader.read(2)
            await batch_reader.read(3)
            # Out of order — batch 2 again, after having moved past it.
            second_pass = await batch_reader.read(2)
        finally:
            await batch_reader.close()

        assert [row["id"] for row in first_pass] == list(range(51, 101))
        assert second_pass == first_pass

    async def test_batch_numbers_start_at_one(
        self, datasource_row: Callable,
    ) -> None:
        """
        Zero is refused rather than treated as one.

        Part files and progress messages are 1-based because people read them, and a
        caller passing 0 has an off-by-one somewhere that should surface here rather than
        as a duplicated first batch.
        """
        source = reader.RecordSource(
            datasource=datasource_row(10), config={}, table_name="items",
        )
        batch_reader = reader.BatchReader(source)

        with pytest.raises(ValueError):
            await batch_reader.read(0)

    async def test_closing_twice_is_safe(self, datasource_row: Callable) -> None:
        """
        Cleanup runs on every path out of an export, including the ones that already
        failed, so a second close must not raise on top of the original failure.
        """
        source = reader.RecordSource(
            datasource=datasource_row(10), config={}, table_name="items",
        )
        batch_reader = reader.BatchReader(source)

        await batch_reader.read(1)
        await batch_reader.close()
        await batch_reader.close()


# ---- The registry ----

class TestReaderRegistry:
    async def test_a_reader_is_reused_for_the_same_export(
        self, datasource_row: Callable,
    ) -> None:
        """
        One export, one cursor. A second reader would restart from the first record and
        the export would contain its first batch several times.
        """
        source = reader.RecordSource(
            datasource=datasource_row(125), config={}, table_name="items",
        )
        key = "export-a"

        try:
            first = reader.get_reader(key, source)
            assert reader.get_reader(key, source) is first
        finally:
            await reader.release_reader(key)

    async def test_releasing_forgets_the_reader(
        self, datasource_row: Callable,
    ) -> None:
        source = reader.RecordSource(
            datasource=datasource_row(10), config={}, table_name="items",
        )
        key = "export-b"

        first = reader.get_reader(key, source)
        await first.read(1)
        await reader.release_reader(key)

        assert reader.get_reader(key, source) is not first
        await reader.release_reader(key)

    async def test_releasing_an_unknown_export_is_a_no_op(self) -> None:
        """The cleanup node runs even when nothing was ever read."""
        await reader.release_reader("never-existed")


# ---- Bindings, labels, and reading across several sources ----

class TestASourceCarriesItsRestriction:
    """
    A source is what the tool will actually be *run as*, restriction included.

    Before this, an aggregation over a nested tool built its source from the tool's
    stored columns alone and dropped the child's values — so the totals were over a
    wider result set than the tool has ever returned, with nothing saying so. That is
    the failure this pins.
    """

    async def test_a_bound_value_narrows_the_count(
        self, datasource_row: Callable,
    ) -> None:
        source = reader.RecordSource(
            datasource=datasource_row(125),
            config={},
            table_name="items",
            value_bindings=[{"reference": "id", "values": [1, 2, 3]}],
        )

        assert (await reader.count_records(source)).total == 3

    async def test_a_bound_value_narrows_what_is_read(
        self, datasource_row: Callable,
    ) -> None:
        source = reader.RecordSource(
            datasource=datasource_row(125),
            config={},
            table_name="items",
            value_bindings=[{"reference": "id", "values": [4, 9]}],
        )

        batch_reader = reader.BatchReader(source)
        try:
            rows, _batches = await _read_all(batch_reader)
        finally:
            await batch_reader.close()

        assert sorted(row["id"] for row in rows) == [4, 9]

    async def test_a_scalar_binding_works_in_sql_mode_too(
        self, datasource_row: Callable,
    ) -> None:
        source = reader.RecordSource(
            datasource=datasource_row(125),
            config={},
            table_name="items",
            sql_query="SELECT id FROM items WHERE id = :wanted",
            value_bindings=[
                {"reference": "wanted", "values": [7], "expanding": False},
            ],
        )

        batch_reader = reader.BatchReader(source)
        try:
            rows, _batches = await _read_all(batch_reader)
        finally:
            await batch_reader.close()

        assert [row["id"] for row in rows] == [7]

    async def test_a_declared_value_is_bound_for_the_reader_too(
        self, datasource_row: Callable,
    ) -> None:
        """An export and an aggregation read the same query the agent's tool call
        does, arguments included."""
        source = reader.RecordSource(
            datasource=datasource_row(125),
            config={},
            table_name="items",
            sql_query="SELECT id FROM items WHERE id < :ceiling",
            sql_params=[{"param": "ceiling", "type": "number", "required": True}],
            agent_values={"ceiling": "4"},
        )

        batch_reader = reader.BatchReader(source)
        try:
            rows, _batches = await _read_all(batch_reader)
        finally:
            await batch_reader.close()

        assert [row["id"] for row in rows] == [1, 2, 3]

    async def test_a_label_is_written_onto_every_row(
        self, datasource_row: Callable,
    ) -> None:
        source = reader.RecordSource(
            datasource=datasource_row(3),
            config={},
            table_name="items",
            label={"department": 9},
        )

        batch_reader = reader.BatchReader(source)
        try:
            rows, _batches = await _read_all(batch_reader)
        finally:
            await batch_reader.close()

        assert [row["department"] for row in rows] == [9, 9, 9]

    async def test_a_label_that_collides_is_refused(
        self, datasource_row: Callable,
    ) -> None:
        source = reader.RecordSource(
            datasource=datasource_row(3),
            config={},
            table_name="items",
            label={"qty": 9},
        )

        batch_reader = reader.BatchReader(source)

        try:
            with pytest.raises(ToolQueryError, match="already returns a column"):
                await batch_reader.read(1)
        finally:
            await batch_reader.close()


class TestChainedBatchReader:
    """
    Several sources read as one — an iterating chain's whole result set.

    The contract the aggregation relies on is narrow and worth stating: ``read``
    returns nothing **only** when every source is spent. A source that legitimately
    matches nothing must roll forward rather than end the run, or a department with
    no projects would silently truncate the answer at that department.
    """

    def _sources(self, datasource, groups: list) -> list:  # noqa: ANN001
        """One source per group, each restricted to its own ids and labelled."""
        return [
            reader.RecordSource(
                datasource=datasource,
                config={},
                table_name="items",
                value_bindings=[{"reference": "id", "values": ids}],
                label={"group": name},
            )
            for name, ids in groups
        ]

    async def test_it_reads_every_source_in_order(
        self, datasource_row: Callable,
    ) -> None:
        sources = self._sources(
            datasource_row(20), [("a", [1, 2]), ("b", [5]), ("c", [9, 10, 11])],
        )

        rows, _batches = await _read_all(
            reader.ChainedBatchReader(sources, batch_size=50), batch_size=50,
        )

        assert [(row["id"], row["group"]) for row in rows] == [
            (1, "a"), (2, "a"), (5, "b"), (9, "c"), (10, "c"), (11, "c"),
        ]

    async def test_a_source_matching_nothing_does_not_end_the_run(
        self, datasource_row: Callable,
    ) -> None:
        """
        The `while` rather than an `if`: two empty sources in a row must not read as
        "every source is spent", or the answer stops at the first empty group.
        """
        sources = self._sources(
            datasource_row(20),
            [("a", [1]), ("empty", [999]), ("also_empty", [998]), ("z", [4])],
        )

        rows, _batches = await _read_all(reader.ChainedBatchReader(sources))

        assert [row["id"] for row in rows] == [1, 4]

    async def test_it_batches_within_a_source_as_well_as_across_them(
        self, datasource_row: Callable,
    ) -> None:
        """Batch numbers are global; a batch never straddles two sources, because a
        cursor cannot."""
        sources = self._sources(
            datasource_row(20), [("a", list(range(1, 8))), ("b", [11, 12])],
        )
        chained = reader.ChainedBatchReader(sources, batch_size=3)

        first = await chained.read(1)
        second = await chained.read(2)
        third = await chained.read(3)
        fourth = await chained.read(4)

        assert [row["id"] for row in first] == [1, 2, 3]
        assert [row["id"] for row in second] == [4, 5, 6]
        assert [row["id"] for row in third] == [7]
        assert [row["id"] for row in fourth] == [11, 12]

        await chained.close()

    async def test_an_out_of_order_read_comes_back_with_the_right_window(
        self, datasource_row: Callable,
    ) -> None:
        """The retry path: it restarts from the first source and discards forward,
        which may cross a source boundary."""
        sources = self._sources(
            datasource_row(20), [("a", [1, 2, 3]), ("b", [7, 8, 9])],
        )
        chained = reader.ChainedBatchReader(sources, batch_size=2)

        await chained.read(1)
        await chained.read(2)

        assert [row["id"] for row in await chained.read(2)] == [3]

        await chained.close()

    async def test_no_sources_at_all_is_refused_rather_than_read_as_empty(
        self,
    ) -> None:
        with pytest.raises(ToolQueryError, match="nothing to read"):
            reader.ChainedBatchReader([])

    async def test_the_registry_hands_back_a_chained_reader_for_a_list(
        self, datasource_row: Callable,
    ) -> None:
        """
        Same two methods either way, so neither the wave loop nor the cleanup node
        has to know which it has.
        """
        sources = self._sources(datasource_row(20), [("a", [1]), ("b", [2])])

        try:
            chained = reader.get_reader("chained-under-test", sources)

            assert isinstance(chained, reader.ChainedBatchReader)
            assert reader.get_reader("chained-under-test", sources) is chained
        finally:
            await reader.release_reader("chained-under-test")


class TestCountingEverySource:
    async def test_it_sums_across_the_sources(
        self, datasource_row: Callable,
    ) -> None:
        datasource = datasource_row(20)
        sources = [
            reader.RecordSource(
                datasource=datasource,
                config={},
                table_name="items",
                value_bindings=[{"reference": "id", "values": ids}],
            )
            for ids in ([1, 2, 3], [7, 8], [15])
        ]

        counted = await reader.count_all(sources)

        assert counted.total == 6
        assert counted.is_lower_bound is False

    async def test_it_stops_counting_once_the_ceiling_is_passed(
        self, datasource_row: Callable, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        Past the ceiling the run is refused anyway, so counting the rest would be
        work done to produce a number nobody is allowed to act on.
        """
        monkeypatch.setattr(reader, "MAX_EXPORT_ROWS", 4)

        datasource = datasource_row(20)
        counted = await reader.count_all([
            reader.RecordSource(
                datasource=datasource, config={}, table_name="items",
            ),
        ] * 3)

        assert counted.is_lower_bound is True

    async def test_no_sources_counts_to_nothing(self) -> None:
        counted = await reader.count_all([])

        assert counted.total == 0
        assert counted.is_lower_bound is False
