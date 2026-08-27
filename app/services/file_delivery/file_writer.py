"""
Rows onto disk, in the four formats a block can ask for.

**polars for CSV, XLSX and TXT; pyarrow for Parquet.** That is a deliberate divergence
from ``downloader_agents``, whose CSV writer uses the stdlib ``csv`` module and whose Excel
writer uses openpyxl — and the divergence is safe because the *reason* those two avoid a
dataframe does not apply here. That reason is pandas: ``DataFrame.from_records`` over
dictionaries infers dtypes, and an integer column containing one NULL becomes floats, so
``qty: 3`` is written as ``3.0`` and the file quietly disagrees with the answer the agent
gave in the chat. polars does not do that — an integer column with nulls stays an integer
column — so a dataframe here buys consistent quoting, dates and encodings across three
formats without the coercion that made it the wrong tool there.

Do not "fix" one of the two to match the other. They write for different callers with
different constraints, and both docstrings say so.

**One file, not parts.** ``part_writer``'s contract has ``write_part`` *and*
``merge_parts`` because an export is built batch by batch by a background worker. A block
holds its rows in memory already — a SQL node's output, a graph run's result, a variable —
so there is one write and nothing to merge. What that buys is the whole of this module
being twelve lines per format; what it costs is the ceiling below, which is why the ceiling
is checked before anything is written rather than discovered halfway through.

**Nothing here is truncated, ever.** Past :data:`FILE_MAX_ROWS` the write is refused with a
sentence naming the limit. Writing the first N would produce a file that looks complete,
gets emailed to somebody, and is wrong in a way nothing about it announces — the same rule
``integration_runner`` applies to its email cap and ``record_reader`` to its export
ceiling.

**polars and pyarrow are imported at module scope, and that is load-bearing.** A compiled
extension first imported on a worker thread that is later destroyed can take the process
down on its next use — reproducible, and documented at length in
``downloader_agents/parquet/parquet_writer.py``. So the import happens when this module is
first imported, which callers do from a coroutine (``file_service.create_file``), never
from inside the ``to_thread`` below.
"""

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Sequence

import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq

from app.models.file_delivery import (
    FILE_FORMAT_EXTENSIONS,
    FORMAT_CSV,
    FORMAT_PARQUET,
    FORMAT_TXT,
    FORMAT_XLSX,
)
from app.services.file_delivery.errors import SourceError, WriteError

logger = logging.getLogger(__name__)


# How many rows a block may write. The same figure and the same reasoning as
# ``record_reader.MAX_EXPORT_ROWS``: the honest way to bound work that holds memory is to
# refuse what cannot finish rather than to start it and hope. Raised by an operator whose
# hardware justifies it.
#
# Unlike that one, this ceiling is about *resident* rows rather than a held connection —
# the rows are already in memory by the time this module sees them, and a dataframe over
# them is a second copy.
FILE_MAX_ROWS = int(os.getenv("FILE_NODE_MAX_ROWS", "500000"))

# The sheet name in a written workbook. Named rather than left as the default because it
# is what the person opening the file sees on the tab.
_SHEET_TITLE = "Records"

# The separator a TXT file of rows uses. Tab rather than comma, because the point of
# choosing TXT over CSV is to read it, and a tab-separated table lines up in a terminal
# and in Notepad where a comma-separated one does not.
_TXT_SEPARATOR = "\t"

# Formats that describe a table. TXT is the one that also accepts plain text, which is why
# it is not in here: see `write_text`.
TABULAR_FORMATS = frozenset({FORMAT_CSV, FORMAT_XLSX, FORMAT_PARQUET})


def extension_for(file_format: str) -> str:
    """The extension a format's files carry, including the dot."""
    try:
        return FILE_FORMAT_EXTENSIONS[file_format]
    except KeyError as exc:  # pragma: no cover — the validators refuse this at save
        raise WriteError(f"{file_format!r} is not a file format this can write.") from exc


def assert_within_ceiling(row_count: int, block: str = "") -> None:
    """
    Refuse a result too large to write, before anything is written.

    Its own function because both canvases call it at the point they *know the count* —
    which for a graph run is before the rows are even read back — so the refusal arrives
    before the work rather than after it.
    """
    if row_count <= FILE_MAX_ROWS:
        return

    raise SourceError(
        f"That is {row_count:,} rows, and a file block writes at most "
        f"{FILE_MAX_ROWS:,}. Narrow the result — a WHERE or a LIMIT in the query "
        "behind it — or raise FILE_NODE_MAX_ROWS.",
        block=block,
    )


async def write_rows(rows: Sequence[Dict[str, Any]], path: Path, file_format: str) -> int:
    """
    Write one table as one file. Returns how many data rows reached it.

    The count is taken from what was written rather than from what was intended, matching
    ``csv_writer.merge_parts``: it is a fact about the file that exists, and the two differ
    in exactly the cases somebody would want to know about.

    An **empty** result still writes a file — a header row and nothing under it. The
    alternative is a Download File block pointing at nothing, which reads to a visitor as a
    broken link rather than as "there were no matching records". A block that wants to say
    that instead can branch on the count with an If/Else first.
    """
    ordered = list(rows)

    assert_within_ceiling(len(ordered))

    path.parent.mkdir(parents=True, exist_ok=True)

    if file_format == FORMAT_PARQUET:
        await asyncio.to_thread(_write_parquet, ordered, path)
        return len(ordered)

    if file_format not in (FORMAT_CSV, FORMAT_XLSX, FORMAT_TXT):
        raise WriteError(f"{file_format!r} is not a file format this can write.")

    await asyncio.to_thread(_write_with_polars, ordered, path, file_format)

    return len(ordered)


async def write_text(text: str, path: Path, file_format: str) -> int:
    """
    Write one block of text as a TXT file. Returns the number of lines.

    Only TXT. A variable holding an AI Fallback's answer is prose with newlines in it, and
    there is no honest way to make that a spreadsheet: one cell containing the lot is not a
    table, and splitting on the pipes it happens to contain is guessing at somebody's data.
    So the refusal is here, by name, and the operator either chooses TXT or points the
    block at something that produced rows.
    """
    if file_format != FORMAT_TXT:
        raise SourceError(
            f"That is text, not rows, so it cannot be written as "
            f"{file_format.upper()}. Choose TXT, or point this block at a block that "
            "produced rows.",
        )

    path.parent.mkdir(parents=True, exist_ok=True)

    body = text if text.endswith("\n") else text + "\n"

    await asyncio.to_thread(path.write_text, body, "utf-8")

    return body.count("\n")


def _write_with_polars(rows: List[Dict[str, Any]], path: Path, file_format: str) -> None:
    """
    The CSV / XLSX / TXT path. Synchronous — it runs inside one ``to_thread``.

    Two attempts, and the second is not defensive padding — it is the case a JSON column or
    a database view produces routinely. A column holding two unrelated types, or a nested
    value, gets through ``from_dicts`` happily as a Struct and then fails *at the write*
    ("CSV format does not support nested data"). So the retry wraps the whole
    frame-and-write, not just the frame: catching only the construction would have left the
    real failure uncaught, which is what the test for it found.

    What the retry does is write every value as its string form. A file that reads is worth
    more than a type that was inferred, and the operator can see the strings — the same
    trade ``xls_writer`` makes for a cell type openpyxl cannot hold and
    ``parquet_writer._schema_for`` makes for a value that does not fit its column.
    """
    try:
        _write_frame(_frame_of(rows), path, file_format)
        return
    except Exception as exc:  # noqa: BLE001 — polars raises several unrelated types here
        logger.info(
            "Falling back to text columns for a %d-row %s file: %s",
            len(rows),
            file_format,
            exc,
        )

    _write_frame(_frame_of(_stringified(rows)), path, file_format)


def _write_frame(frame: "pl.DataFrame", path: Path, file_format: str) -> None:
    """One dataframe, in one format. The only place a polars writer is called."""
    if file_format == FORMAT_CSV:
        frame.write_csv(path)
        return

    if file_format == FORMAT_TXT:
        frame.write_csv(path, separator=_TXT_SEPARATOR)
        return

    frame.write_excel(path, worksheet=_SHEET_TITLE)


def _frame_of(rows: List[Dict[str, Any]]) -> "pl.DataFrame":
    """
    One list of dictionaries as a dataframe.

    ``infer_schema_length=None`` reads **every** row before deciding the column types,
    which is the call ``frame_ops.partial_aggregate`` already makes and for the same
    reason: the default looks at the first hundred, so a column that is null for the first
    hundred rows and a string afterwards would be typed from the nulls and then fail.
    """
    return pl.DataFrame() if not rows else pl.from_dicts(rows, infer_schema_length=None)


def _stringified(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Every cell as its string form, nulls left as nulls."""
    return [{key: _as_text(value) for key, value in row.items()} for row in rows]


def _write_parquet(rows: List[Dict[str, Any]], path: Path) -> None:
    """
    The Parquet path, through pyarrow rather than polars.

    Parquet is typed and pyarrow is what writes it here — the same library
    ``parquet_writer`` and ``csv_to_parquet`` already use, so a file this produces is read
    back by the rest of the application with no surprises. The stringifying fallback is
    the same trade :func:`_frame_of` makes, and for the same reason: pyarrow refuses a
    column holding two unrelated types outright.

    An empty result writes a valid Parquet file with no columns. Readers handle that; a
    missing file does not.
    """
    try:
        pq.write_table(pa.Table.from_pylist(rows), path)
        return
    except Exception as exc:  # noqa: BLE001 — arrow raises several unrelated types here
        logger.info(
            "Falling back to text columns for a %d-row Parquet file: %s", len(rows), exc,
        )

    # The retry covers the write as well as the table, for the reason
    # :func:`_write_with_polars` gives: a type Arrow accepts into a table is not
    # necessarily one it can serialise.
    pq.write_table(pa.Table.from_pylist(_stringified(rows)), path)


def _as_text(value: Any) -> Any:
    """One cell as text, leaving ``None`` alone so a null stays a null rather than "None"."""
    return None if value is None else str(value)
