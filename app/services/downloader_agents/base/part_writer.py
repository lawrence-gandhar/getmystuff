"""
The contract every export format implements, and the registry that finds it.

Three formats, three packages — ``csv/``, ``xls/``, ``parquet/`` — and this module is
the only thing ``base/`` knows about any of them. The graph asks for "the writer for
this format" and gets something with two methods; it never imports a format package,
never branches on a format string, and does not know that Parquet needs a schema or
that XLSX cannot be concatenated.

**Two operations, because a batched export has exactly two.**

``write_part(rows, path)``
    Turn one batch of records into one file. Called once per batch, up to three times
    per batch on the retry path. It must be **whole or nothing**: a part file that
    exists is a part file that can be merged, so a writer that fails partway must not
    leave a readable fragment behind. (The retry deletes it anyway — see ``retry.py``
    — but a writer that relies on that is a writer that breaks the day someone calls
    it from somewhere else.)

``merge_parts(paths, destination)``
    Fold every part into the finished artifact. Called once, and this is where the
    formats genuinely differ: CSV concatenates bytes and drops repeated headers,
    Parquet opens a writer over the first part's schema and appends row groups, XLSX
    has to read its parts back and write one workbook. That is precisely why the
    operation is on the writer and not in the graph.

**Why not pandas for all three.** ``pd.concat`` over every part would be one line and
would hold the entire export in memory — the thing this feature is built to avoid. Each
implementation streams instead, and the contract is written so that it can: paths in,
path out, nothing returned but a count.

**Registry, not a dict of imports.** The lookup imports the format package lazily, on
first use. Two reasons: ``pyarrow`` and ``openpyxl`` are heavy enough that importing
both to write a CSV is waste, and a format whose dependency is missing should fail
when someone asks for *that* format rather than at application start.
"""

from importlib import import_module
from pathlib import Path
from typing import Any, Dict, List, Protocol, runtime_checkable

from litestar.exceptions import HTTPException

from app.models.downloader_agents import (
    EXPORT_FORMAT_EXTENSIONS,
    EXPORT_FORMAT_VALUES,
    FORMAT_CSV,
    FORMAT_PARQUET,
    FORMAT_XLS,
)

# format value -> the module implementing it. Module paths rather than imported
# objects so nothing here imports pyarrow or openpyxl until a caller asks for a format
# that needs them.
_WRITER_MODULES: Dict[str, str] = {
    FORMAT_CSV: "app.services.downloader_agents.csv.csv_writer",
    FORMAT_XLS: "app.services.downloader_agents.xls.xls_writer",
    FORMAT_PARQUET: "app.services.downloader_agents.parquet.parquet_writer",
}

# Resolved writers, so the import cost is paid once per process rather than per batch.
_writers: Dict[str, "PartWriter"] = {}


@runtime_checkable
class PartWriter(Protocol):
    """
    What a format module must provide.

    A ``Protocol`` rather than a base class to inherit: each format module is a module
    of functions, in the house style, and there is nothing for a base class to give
    them. Declaring the shape here means the graph can be typed against it and
    :func:`writer_for` can check a module actually implements it before handing it back
    — a format package missing ``merge_parts`` should fail on being registered, not
    ten minutes into an export.
    """

    #: The extension the format's files carry, including the dot.
    extension: str

    #: What the download route sends as the response's ``Content-Type``.
    media_type: str

    async def write_part(self, rows: List[Dict[str, Any]], path: Path) -> int:
        """Write one batch to ``path``. Returns the number of records written."""
        ...

    async def merge_parts(self, paths: List[Path], destination: Path) -> int:
        """Fold ``paths`` into ``destination``. Returns the number of records."""
        ...


def writer_for(file_format: str) -> Any:
    """
    The writer module for one format.

    Raises the project's 400 for a format we do not write. That should be unreachable
    from a request — ``ConfirmDownloadArgs`` validates the format before it gets here —
    and is raised anyway, because this function is also reachable from the worker
    resuming a row that was written by an older version of the application.
    """
    key = (file_format or "").strip().lower()

    if key not in EXPORT_FORMAT_VALUES:
        allowed = ", ".join(sorted(EXPORT_FORMAT_VALUES))
        raise HTTPException(
            status_code=400,
            detail=(
                f"'{file_format}' is not a file format this application can create. "
                f"Please choose one of: {allowed}."
            ),
        )

    writer = _writers.get(key)

    if writer is None:
        module = import_module(_WRITER_MODULES[key])
        _require_contract(key, module)
        writer = module
        _writers[key] = writer

    return writer


def extension_for(file_format: str) -> str:
    """
    The file extension for one format, including the dot.

    Read from the model's mapping rather than from the writer module, so a path can be
    built (and a part file named) without importing pyarrow.
    """
    key = (file_format or "").strip().lower()
    return EXPORT_FORMAT_EXTENSIONS.get(key, ".csv")


def forget_export_caches(parts_directory: Path) -> None:
    """
    Let every format drop whatever it cached for one export.

    Only Parquet has anything to forget — the schema it pinned from the first batch —
    and it is asked by attribute (``forget_schema``) rather than by name, so a future
    format with its own cache does not need this function edited.

    Iterates the writers **already resolved** rather than the registry, which is the
    difference between this being free and it importing pyarrow and openpyxl to clean up
    after a CSV.
    """
    for writer in _writers.values():
        forget = getattr(writer, "forget_schema", None)

        if forget is not None:
            forget(parts_directory)


def _require_contract(file_format: str, module: Any) -> None:
    """
    Refuse a format module that does not implement the contract.

    Checked at registration rather than at call time, and by attribute rather than by
    ``isinstance``: a module cannot be an instance of a Protocol, and the useful
    failure is "this module is missing merge_parts", named, right now — not an
    ``AttributeError`` from inside a graph node forty batches in.
    """
    missing = [
        name
        for name in ("extension", "media_type", "write_part", "merge_parts")
        if not hasattr(module, name)
    ]

    if missing:
        raise HTTPException(
            status_code=500,
            detail=(
                "That file format is not available at the moment. Please try CSV "
                "instead."
            ),
            extra={
                "format": file_format,
                "module": getattr(module, "__name__", "?"),
                "missing": missing,
            },
        )
