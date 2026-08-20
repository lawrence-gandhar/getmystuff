"""
Parquet parts, and merging them by appending row groups.

Implements the contract in
:mod:`app.services.downloader_agents.base.part_writer`. Nothing else imports this
module — the registry there resolves it by format name.

**The merge is the good one.** ``pq.ParquetWriter(destination, schema)`` opened over the
last part's schema, then one ``write_table`` per part: the row groups are appended
without decoding the values, and only one part's worth is resident at a time. This is
the same loop ``app/utils/csv_to_parquet.py`` already uses for its CSV-to-Parquet
conversion, which is where the pattern comes from.

**Why every part is written from an explicit schema.** Parquet is typed, and a part
written from fifty dictionaries has its types inferred from those fifty values. Batch 1
sees ``qty`` as an integer; batch 2 happens to contain only NULLs and gets a null
column; the merge then refuses to append batch 2 to batch 1 because the schemas differ
— thousands of records in, on a query that is perfectly valid. So the schema is derived
once, from the first batch, and handed to every subsequent part. A value that does not
fit the column it landed in is the one thing this module converts to a string rather
than failing on: see :func:`_schema_for`.

**Why not one writer held open for the whole export.** It would skip the parts
entirely, and it would also skip the retry rule. A batch that fails has to leave no
trace, and a row group already flushed into a shared writer cannot be taken back out.
One file per batch is what makes "delete it and try again" true.
"""

import asyncio
import logging
from pathlib import Path
from typing import Any, Dict, List

# Imported at module scope, on whichever thread first asks for this format — NOT inside
# the functions below, which is where they were and which crashes the interpreter.
#
# pyarrow's C extension must not be first imported on a worker thread that is later
# destroyed. `asyncio.to_thread` uses the running loop's executor, so the first export in
# a process would initialise pyarrow on a pool thread; when that pool went away, the next
# pyarrow call in a fresh loop segfaulted in ParquetWriter's constructor. Reproducible in
# nine lines, and it took the whole process down rather than raising.
#
# Nothing is lost by importing here: `base/part_writer` only imports this module when a
# caller actually asks for Parquet, so the laziness the function-level imports were for is
# already provided one level up, by the registry.
import pyarrow as pa
import pyarrow.parquet as pq

logger = logging.getLogger(__name__)

#: Part of the PartWriter contract.
extension = ".parquet"
media_type = "application/vnd.apache.parquet"

# Compression for both parts and the artifact. Snappy rather than the heavier codecs:
# an export is written once and read once, so decompression speed and CPU cost matter
# more than the last few percent of size.
_COMPRESSION = "snappy"

# The single column an empty export declares. Parquet cannot hold a file with no columns
# at all, and an export that read no batches has no column names to use — see merge_parts.
_EMPTY_COLUMN = "no_records"

# The schema derived from the first batch, per destination directory. Keyed by the
# parts directory so two concurrent exports cannot hand each other a schema — the
# worker drains jobs one at a time today, and relying on that would be a bug waiting
# for the day it drains two.
_schemas: Dict[str, Any] = {}


async def write_part(rows: List[Dict[str, Any]], path: Path) -> int:
    """
    Write one batch as a Parquet file, reusing the export's established schema.

    The first batch of an export defines the schema; every later batch is coerced to
    it. That is what makes the parts appendable — see the module docstring.
    """
    if not rows:
        return 0

    def _write() -> int:
        key = str(Path(path).parent)
        schema = _schemas.get(key) or _schema_for(rows)

        table = _table_from(rows, schema)

        # The pinned schema follows the file, not the other way round. If a value in
        # this batch would not fit its column, _table_from widened that column to
        # text — so the table's schema is what was actually written, and pinning
        # anything else would make the *next* batch fail its ParquetWriter check
        # against a schema no part file on disk has. Widening is one-way (a type only
        # ever becomes text), which is what lets the merge rely on it.
        _schemas[key] = table.schema

        with pq.ParquetWriter(path, table.schema, compression=_COMPRESSION) as writer:
            writer.write_table(table)

        return len(rows)

    return await asyncio.to_thread(_write)


async def merge_parts(paths: List[Path], destination: Path) -> int:
    """
    Append every part's row groups into one Parquet file.

    Returns the number of records in the finished file, summed from each part's own
    metadata rather than by reading its rows — the footer knows, and asking it is free.

    The schema is read from a part file rather than from :data:`_schemas`, so a merge is
    correct even in a process that did not write the parts — a worker that picked the job
    up after a restart. Which part, and why it is the last one, is at the read itself.
    """
    def _merge() -> int:
        if not paths:
            # No parts: the query matched nothing between the offer and the build. A file
            # is still written, because a missing artifact is a download that 404s and
            # "no records" is a truthful result.
            #
            # It needs *a* schema, and there is none to be had: no batch was ever read, so
            # no column names are known. A zero-column schema is the literal answer and it
            # is not usable — pyarrow segfaults writing one, and no reader can do anything
            # with the result either. So the file declares one nullable column named for
            # the situation. It fabricates no records, and it opens.
            with pq.ParquetWriter(
                destination,
                pa.schema([pa.field(_EMPTY_COLUMN, pa.string())]),
                compression=_COMPRESSION,
            ) as writer:
                writer.write_table(
                    pa.table({_EMPTY_COLUMN: pa.array([], type=pa.string())}),
                )

            return 0

        # The LAST part's schema, not the first. write_part only ever widens a column
        # (to text, when a value would not fit), so the final part carries the widest
        # schema of the export — and casting an earlier int column up to text succeeds
        # where casting a later text column down to int would not.
        schema = pq.read_schema(paths[-1])
        rows_written = 0

        with pq.ParquetWriter(
            destination, schema, compression=_COMPRESSION,
        ) as writer:
            for part in paths:
                # Closed explicitly. A ParquetFile holds an open file handle and a
                # native reader; an export with ten thousand parts that left them to
                # the garbage collector would run out of descriptors, and a handle
                # finalised on a thread other than the one that opened it takes the
                # process down rather than raising.
                with pq.ParquetFile(part) as source:
                    for batch in source.iter_batches():
                        table = pa.Table.from_batches([batch])

                        if not table.schema.equals(schema):
                            # An earlier part written before a column widened to text.
                            # Cast rather than fail: the alternative is losing a
                            # finished export to a type change in the user's own data.
                            logger.info(
                                "Casting part %s to the export's final schema",
                                part.name,
                            )
                            table = table.cast(schema)

                        writer.write_table(table)
                        rows_written += table.num_rows

        return rows_written

    return await asyncio.to_thread(_merge)


def forget_schema(parts_directory: Path) -> None:
    """
    Drop the cached schema for one export.

    Called by the cleanup node. Without it the dict grows by one entry per export for
    the life of the process — small, but it is a cache with no eviction, and the export
    it belongs to is finished.
    """
    _schemas.pop(str(parts_directory), None)


def _schema_for(rows: List[Dict[str, Any]]) -> Any:
    """
    The schema for an export, derived from its first batch.

    Built by letting pyarrow infer each column from the batch, and falling back to a
    string column for anything it cannot: a JSON column arriving as a dict, a mixed
    column, a type with no Arrow equivalent. A string column is how that value already
    reaches the user in the chat answer and in the CSV, so it is the consistent
    outcome rather than a lossy one — and the alternative is refusing to export a
    query that works everywhere else.
    """
    fields = []

    for name in rows[0].keys():
        values = [row.get(name) for row in rows]

        try:
            field_type = pa.array(values).type
        except (pa.ArrowInvalid, pa.ArrowTypeError, pa.ArrowNotImplementedError):
            logger.info(
                "Column '%s' has no Arrow type; exporting it as text", name,
            )
            field_type = pa.string()

        if pa.types.is_null(field_type):
            # Every value in this batch was NULL. Inferring a null column would make
            # the schema unable to hold the values batch 2 brings, so it becomes text
            # — the only type every later value can be coerced into.
            field_type = pa.string()

        fields.append(pa.field(name, field_type))

    return pa.schema(fields)


def _table_from(rows: List[Dict[str, Any]], schema: Any) -> Any:
    """
    One batch as an Arrow table matching ``schema``.

    Each column is built on its own so a value that will not fit its column can be
    handled per column rather than failing the batch. The fallback is the same as the
    schema's: render the offending column as text. It costs the column its type in the
    finished file and keeps the export.
    """
    columns = []

    for field in schema:
        values = [row.get(field.name) for row in rows]

        try:
            columns.append(pa.array(values, type=field.type))
        except (pa.ArrowInvalid, pa.ArrowTypeError, pa.ArrowNotImplementedError):
            logger.info(
                "A value in column '%s' does not fit its type; exporting the batch's "
                "values as text",
                field.name,
            )
            columns.append(
                pa.array(
                    [None if value is None else str(value) for value in values],
                    type=pa.string(),
                )
            )

    return pa.Table.from_arrays(columns, schema=_coerced_schema(schema, columns))


def _coerced_schema(schema: Any, columns: List[Any]) -> Any:
    """
    ``schema`` with any column that had to fall back to text reflected in it.

    ``Table.from_arrays`` requires the schema and the arrays to agree, so a column that
    became text needs the schema to say so for *this* table. The export's pinned schema
    is untouched, and the merge casts if the two ever disagree.
    """
    return pa.schema(
        [
            pa.field(field.name, column.type)
            for field, column in zip(schema, columns)
        ]
    )
