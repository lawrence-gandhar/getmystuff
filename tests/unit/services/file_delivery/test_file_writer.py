"""
Tests for app/services/file_delivery/file_writer.py.

Every format is written **and read back**, which is the only assertion worth making about a
file writer: a 200-byte file that no reader accepts passes every check short of opening it.

The one asserted hardest is the dtype. ``downloader_agents``' CSV writer avoids pandas
because ``DataFrame.from_records`` turns an integer column containing a NULL into floats —
so ``qty: 3`` reaches the file as ``3.0`` and the download disagrees with the answer the
agent gave in the chat. This module uses polars *instead of* that reasoning, so the claim
that polars does not do it is load-bearing and is pinned here, per format.

XLSX is read back through openpyxl rather than ``pl.read_excel``, which wants ``fastexcel``:
adding a dependency so that a *test* can read what the application only ever writes would
be waste.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pyarrow.parquet as pq
import pytest

from app.models.file_delivery import (
    FORMAT_CSV,
    FORMAT_PARQUET,
    FORMAT_TXT,
    FORMAT_XLSX,
)
from app.services.file_delivery import file_writer
from app.services.file_delivery.errors import SourceError, WriteError

# One row with an integer, one with that integer missing, and text that exercises every
# quoting rule at once: a comma, a double quote and a newline.
ROWS = [
    {"id": 1, "name": "Widget", "qty": 3},
    {"id": 2, "name": 'Gadget, "deluxe"', "qty": None},
    {"id": 3, "name": "line\nbreak", "qty": 7},
]


def read_back(path: Path, file_format: str) -> "pl.DataFrame":
    """One written file, back as a dataframe, using each format's own reader."""
    if file_format == FORMAT_CSV:
        return pl.read_csv(path)

    if file_format == FORMAT_TXT:
        return pl.read_csv(path, separator="\t")

    if file_format == FORMAT_PARQUET:
        return pl.from_arrow(pq.read_table(path))

    from openpyxl import load_workbook

    values = list(load_workbook(path, read_only=True).active.values)

    return pl.DataFrame(
        {name: [row[index] for row in values[1:]] for index, name in enumerate(values[0])}
    )


class TestEveryFormatRoundTrips:
    @pytest.mark.parametrize(
        "file_format", [FORMAT_CSV, FORMAT_XLSX, FORMAT_TXT, FORMAT_PARQUET],
    )
    async def test_the_rows_come_back_out(self, tmp_path: Path, file_format: str) -> None:
        path = tmp_path / f"probe{file_writer.extension_for(file_format)}"

        written = await file_writer.write_rows(ROWS, path, file_format)

        assert written == 3
        assert path.stat().st_size > 0

        frame = read_back(path, file_format)

        assert frame.columns == ["id", "name", "qty"]
        assert frame.height == 3
        assert frame["name"].to_list()[1] == 'Gadget, "deluxe"', (
            "a value containing a comma and quotes must survive the round trip — this is "
            "what the writer's quoting is for"
        )

    @pytest.mark.parametrize(
        "file_format", [FORMAT_CSV, FORMAT_XLSX, FORMAT_TXT, FORMAT_PARQUET],
    )
    async def test_an_integer_column_with_a_null_stays_an_integer(
        self, tmp_path: Path, file_format: str,
    ) -> None:
        """
        The reason this module may use a dataframe at all.

        pandas would make ``qty`` floats because one row is missing it, and the file would
        say ``3.0`` where the chat said ``3`` — the failure
        ``downloader_agents/csv/csv_writer.py`` avoids pandas over. polars does not, and
        that is asserted rather than assumed.
        """
        path = tmp_path / f"probe{file_writer.extension_for(file_format)}"

        await file_writer.write_rows(ROWS, path, file_format)

        frame = read_back(path, file_format)

        assert frame["qty"].dtype == pl.Int64
        assert frame["qty"].to_list() == [3, None, 7]


class TestEdges:
    async def test_an_empty_result_still_writes_a_file(self, tmp_path: Path) -> None:
        """
        A header and nothing under it, not a missing file.

        A Download File block pointing at nothing reads to a visitor as a broken link,
        where an empty file reads as "there were no matching records" — which is the truth.
        """
        path = tmp_path / "empty.csv"

        assert await file_writer.write_rows([], path, FORMAT_CSV) == 0
        assert path.is_file()

    @pytest.mark.parametrize(
        "file_format", [FORMAT_CSV, FORMAT_XLSX, FORMAT_TXT, FORMAT_PARQUET],
    )
    async def test_a_column_holding_two_types_is_written_as_text_not_refused(
        self, tmp_path: Path, file_format: str,
    ) -> None:
        """
        A JSON column or a database view produces this routinely, and a file that reads is
        worth more than a type that was inferred — the trade ``xls_writer`` and
        ``parquet_writer._schema_for`` both already make.

        Parametrised because the failure is **at the write**, not at the frame: polars
        accepts a nested value as a Struct quite happily and then refuses to serialise it
        ("CSV format does not support nested data"). A fallback that only wrapped the frame
        construction passed a one-format test and broke here.
        """
        rows = [{"value": 1}, {"value": {"nested": True}}]
        path = tmp_path / f"mixed{file_writer.extension_for(file_format)}"

        assert await file_writer.write_rows(rows, path, file_format) == 2

        frame = read_back(path, file_format)
        assert frame.columns == ["value"]
        assert frame.height == 2

    async def test_the_directory_is_created(self, tmp_path: Path) -> None:
        path = tmp_path / "not" / "there" / "yet.csv"

        await file_writer.write_rows(ROWS, path, FORMAT_CSV)

        assert path.is_file()

    async def test_an_unknown_format_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(WriteError):
            await file_writer.write_rows(ROWS, tmp_path / "x.doc", "doc")

        with pytest.raises(WriteError):
            file_writer.extension_for("doc")


class TestTheCeiling:
    """
    Past the ceiling the block **fails**. It does not write the first N.

    A truncated file looks complete, gets emailed to somebody, and is wrong in a way
    nothing about it announces — the same rule ``integration_runner`` applies to its email
    cap and ``record_reader`` to its export ceiling.
    """

    def test_a_count_past_the_ceiling_is_refused_by_name(self) -> None:
        with pytest.raises(SourceError) as raised:
            file_writer.assert_within_ceiling(
                file_writer.FILE_MAX_ROWS + 1, "Write the CSV",
            )

        assert f"{file_writer.FILE_MAX_ROWS:,}" in raised.value.message, (
            '"too large" with no number is not something an operator can act on'
        )
        assert raised.value.block == "Write the CSV"

    def test_the_ceiling_itself_is_allowed(self) -> None:
        file_writer.assert_within_ceiling(file_writer.FILE_MAX_ROWS)

    async def test_nothing_is_written_when_the_rows_are_past_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(file_writer, "FILE_MAX_ROWS", 2)
        path = tmp_path / "too-many.csv"

        with pytest.raises(SourceError):
            await file_writer.write_rows(ROWS, path, FORMAT_CSV)

        assert not path.exists(), "a refused write must leave no partial file behind"


class TestText:
    async def test_text_becomes_a_txt_file(self, tmp_path: Path) -> None:
        path = tmp_path / "answer.txt"

        lines = await file_writer.write_text("line one\nline two", path, FORMAT_TXT)

        assert lines == 2
        assert path.read_text() == "line one\nline two\n"

    @pytest.mark.parametrize(
        "file_format", [FORMAT_CSV, FORMAT_XLSX, FORMAT_PARQUET],
    )
    async def test_text_into_a_tabular_format_is_refused(
        self, tmp_path: Path, file_format: str,
    ) -> None:
        """
        There is no honest way to make prose a spreadsheet. One cell holding the lot is
        not a table, and splitting on whatever separators it happens to contain is
        guessing at somebody's data.
        """
        with pytest.raises(SourceError) as raised:
            await file_writer.write_text("some prose", tmp_path / "x", file_format)

        assert "TXT" in raised.value.message
