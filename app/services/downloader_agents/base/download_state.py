"""
What travels along the export graph's edges.

Every field here is JSON-serialisable, and that is not a style preference — the graph is
compiled with a checkpointer, so its state is written to PostgreSQL between the
confirmation interrupt and the worker that resumes it. A live cursor, an open file
handle or a SQLAlchemy row in state would fail to serialise, or worse, serialise into
something that no longer works when it is read back in another process.

So the state carries **identifiers and counters, nothing else**. The things it stands in
for live elsewhere and are looked up from ``export_id``:

* the open cursor — ``record_reader``'s registry, keyed by ``export_id``;
* the tool, the datasource and the format writer — ``download_service.load_context``,
  which reloads them from the database because the worker is not the process that
  created them;
* everything a user or an operator sees afterwards — the ``download_exports`` row,
  which is the durable record and is written by the nodes as they go.

``part_paths`` is the only accumulating field, so it is the only one with a reducer. The
rest are last-write-wins, which is what a linear pipeline wants: ``batch_number`` is
where the run is now, not a history of where it has been.
"""

from typing import Annotated, Any, Dict, List, Optional, TypedDict


def _append_paths(current: Optional[List[str]], incoming: Optional[List[str]]) -> List[str]:
    """
    Accumulate part paths instead of replacing them.

    Without a reducer each ``write_part`` node would return the one path it just wrote
    and the state would hold only the newest — so the merge would receive one part of a
    ninety-seven part export and produce a file that looks complete and is not.

    Duplicates are dropped rather than appended. A retried batch writes to the *same*
    path it discarded, and a path listed twice would be merged twice: the artifact would
    contain fifty records that are not in the source, which is the kind of wrong that
    passes every check downstream.
    """
    merged = list(current or [])

    for path in incoming or []:
        if path not in merged:
            merged.append(path)

    return merged


class DownloadState(TypedDict, total=False):
    """
    One export run, as the graph sees it.

    ``total=False`` throughout: the graph is entered with only ``export_id`` and
    ``file_format`` set, and each node fills in what it establishes. A field being
    absent is meaningful — ``confirmed`` unset is "not asked yet", which is not the same
    as ``False`` ("asked, and told no").
    """

    #: The export's public uuid, as a string. The single key everything else is looked
    #: up by: the reader registry, the checkpointer thread, the part directory, the
    #: database row.
    export_id: str

    #: "csv" | "xls" | "parquet". Present from the start because the offer names CSV,
    #: and overwritten on resume if the user asked for something else.
    file_format: str

    #: The exact COUNT(*), and whether it is exact. See
    #: ``record_reader.RecordCount``.
    total_rows: int
    count_is_lower_bound: bool

    #: Set by the resume. ``True`` runs the pipeline, ``False`` ends the run without
    #: building anything, absent means the interrupt has not been answered.
    confirmed: bool

    #: The batch the run is on, 1-based. Also the part number.
    batch_number: int

    #: How many attempts the current batch has had. Reset to 0 when a batch succeeds,
    #: because the three-attempt budget is per batch and not per export.
    attempts: int

    #: Every part file written so far, in order. See :func:`_append_paths`.
    part_paths: Annotated[List[str], _append_paths]

    #: Records actually written. Counted from the files, not assumed from the batches.
    rows_written: int

    #: True once a read returned nothing, which is what ends the read loop.
    finished_reading: bool

    #: The finished artifact — its path on disk and the name it downloads as.
    file_path: str
    file_name: str
    byte_size: int
    checksum: str

    #: Why the export stopped, in the operator's words. Present only on the abort path;
    #: what the *user* is told is a fixed sentence and lives in ``download_service``.
    failure: str

    #: Anything a node wants on the record that is not worth a field of its own.
    #: Written to the log, never to a response.
    details: Dict[str, Any]


def initial_state(export_id: str, file_format: str) -> DownloadState:
    """
    The state an export graph is entered with.

    Written as a function rather than a literal at the call site so the counters start
    from one place. ``batch_number`` starts at 1 because part files and progress
    messages are 1-based — a person reads "part 1 of 97".
    """
    return {
        "export_id": str(export_id),
        "file_format": file_format,
        "batch_number": 1,
        "attempts": 0,
        "part_paths": [],
        "rows_written": 0,
        "finished_reading": False,
    }
