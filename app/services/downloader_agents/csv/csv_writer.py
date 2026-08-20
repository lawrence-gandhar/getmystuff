"""
CSV parts, and merging them by concatenating bytes.

Implements the contract in
:mod:`app.services.downloader_agents.base.part_writer`. Nothing else imports this
module — the registry there resolves it by format name.

**Why the stdlib ``csv`` module and not pandas.** A batch is fifty dictionaries. Pandas
would build a DataFrame, infer dtypes, and turn an integer column containing a NULL
into floats — so ``qty: 3`` becomes ``3.0`` in the file, and the export quietly
disagrees with the answer the agent gave in the chat. ``csv.DictWriter`` writes what it
is given.

Note the import resolves to the **standard library**, despite this file living in a
package called ``csv``: Python 3 uses absolute imports, so ``import csv`` inside
``app/services/downloader_agents/csv/`` is the stdlib module, not the package this file
is in. The package is named for the format because that is what the feature's folder
layout is organised by.

**Why the merge is a byte copy.** Every part was written by this module with the same
columns in the same order, so the finished file is the first part followed by every
later part minus its header line. Reading them back through the csv module to write
them out again would parse and re-serialise every value for no gain — and would
reintroduce the quoting question the writer already answered. Copying bytes in fixed
chunks means an export of any size costs one buffer.

The header is skipped by reading exactly the first line of each subsequent part rather
than by counting bytes, because a header's length depends on the column names.
"""

import asyncio
import csv
from pathlib import Path
from typing import Any, Dict, List

#: Part of the PartWriter contract.
extension = ".csv"
media_type = "text/csv"

# How much of a part file is moved per read during the merge. 1 MiB: large enough that
# a 500 MB artifact is a few hundred reads, small enough to be irrelevant to memory.
_COPY_CHUNK_BYTES = 1024 * 1024

# newline="" is required by the csv module on every platform — it does its own line
# ending handling, and letting the file object translate as well produces \r\r\n.
_OPEN_KWARGS = {"newline": "", "encoding": "utf-8"}


async def write_part(rows: List[Dict[str, Any]], path: Path) -> int:
    """
    Write one batch as a CSV file with a header row.

    Every part carries its own header. That is redundant in the finished file — the
    merge drops all but the first — and it is what makes a part file independently
    readable, which matters when someone is looking at a failed export's leftovers
    trying to work out what went wrong.

    The columns come from the first row's keys, in order. Every row in a batch comes
    from the same query, so they all have the same keys; ``extrasaction="ignore"`` is
    set anyway, because a driver returning an extra key on one row should not fail an
    export.
    """
    if not rows:
        # An empty batch is the end of the result set, not a file to write. Returning
        # 0 without creating anything keeps "a part file exists" equivalent to "a
        # batch had rows in it", which is what the merge and the cleanup both assume.
        return 0

    def _write() -> int:
        fieldnames = list(rows[0].keys())

        with open(path, "w", **_OPEN_KWARGS) as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)

        return len(rows)

    return await asyncio.to_thread(_write)


async def merge_parts(paths: List[Path], destination: Path) -> int:
    """
    Concatenate the parts into one CSV, keeping only the first header.

    Returns the number of data rows in the finished file, counted while copying. That
    count is what the export row records as ``rows_written``, and counting it here
    rather than trusting the sum of the batch sizes is deliberate: it is a fact about
    the file that was produced, not about the work that was intended.

    Opened in binary mode. The parts are already UTF-8 encoded by
    :func:`write_part`, so decoding them to re-encode them would be pure cost — and
    binary mode is what makes the chunked copy exact rather than subject to newline
    translation.
    """
    if not paths:
        # Nothing to merge means an export whose query matched nothing. Still write
        # the file: an empty CSV is a truthful answer and a missing file is a broken
        # download.
        await asyncio.to_thread(destination.write_bytes, b"")
        return 0

    def _merge() -> int:
        rows_written = 0

        with open(destination, "wb") as out:
            for index, part in enumerate(paths):
                rows_written += _copy_part(part, out, keep_header=index == 0)

        return rows_written

    return await asyncio.to_thread(_merge)


def _copy_part(part: Path, out, keep_header: bool) -> int:
    """
    Copy one part's data rows into the open artifact. Returns how many there were.

    Synchronous and file-handle level on purpose: it runs inside the one
    ``to_thread`` :func:`merge_parts` opens, so making it async would put an event
    loop hop between every megabyte of a byte copy.
    """
    rows_written = 0

    with open(part, "rb") as handle:
        header = handle.readline()

        if keep_header:
            out.write(header)
        elif not header:
            # An empty part file. write_part never creates one, so this is a leftover
            # from a batch that failed mid-write and was not discarded. Skipped
            # rather than copied: half a row is worse in the artifact than absent
            # from it.
            return 0

        last_byte = b""

        while True:
            chunk = handle.read(_COPY_CHUNK_BYTES)
            if not chunk:
                break
            rows_written += chunk.count(b"\n")
            last_byte = chunk[-1:]
            out.write(chunk)

        # A part whose last row has no trailing newline would otherwise run into the
        # next part's first row. write_part always ends with one, so this is for a
        # file some other version of this code, or a truncated write, produced.
        if last_byte and last_byte != b"\n":
            out.write(b"\n")
            rows_written += 1

    return rows_written
