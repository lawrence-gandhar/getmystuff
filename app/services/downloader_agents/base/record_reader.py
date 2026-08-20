"""
Reading a tool's **whole** result set, fifty records at a time.

app.services.deep_agents.query_executor answers a question: it reads a tool's rows and
hands them back in one list, because something is about to reason over them. An export
is the other job — move the entire result set into a file, which may be far more than
fits in memory — so it needs a different read path. What it must not need is a
different set of *rules*, and that is what this module is careful about: every
statement it runs is
assembled by ``query_executor`` (``assemble_built_query`` /
``assemble_sql_statement``), so an export reads exactly what the tool is permitted to
read, re-validated on this run, with the same active-table and active-column checks.
Nothing here builds SQL.

**Why one streaming cursor rather than LIMIT/OFFSET.** Paging with
``LIMIT 50 OFFSET n`` looks like the obvious way to read a set in batches and is the
wrong one here, twice over:

* It needs a total order or it is simply incorrect — without one the database may
  return a row in two batches and another in none. A tool's query is often grouped or
  has no unique key among its output columns, so there is not always an order to
  impose.
* Even with an order, the database re-runs and re-sorts the whole result for every
  batch. An export of 500,000 records is 10,000 batches; that is 10,000 sorts of half
  a million rows to read each of them once.

So both query modes are read the same way instead: open one server-side cursor and
pull ``BATCH_SIZE`` rows off it at a time. One pass, one consistent snapshot, every
row exactly once, no ordering required — and it is the cursor
``query_executor._execute_sql_query`` already opens, held open for longer.

**The cost of that, said out loud.** The cursor holds a connection and a read
transaction for the duration of the export. That is bounded by
:data:`MAX_EXPORT_ROWS` rather than unbounded, and it is why the ceiling exists at all
— an export nobody could finish is refused up front instead of pinning a connection
for an hour. A retried batch pays for the design once: the cursor is gone after a
failure, so the reader re-opens and discards its way back to the failed batch. That is
linear in what was already read, and it only ever happens on the failure path.

**Readers outlive a node but not the run.** A LangGraph node returns state that has
to be JSON-serialisable for the checkpointer, and an open cursor is neither. So the
reader itself is held in a module-level registry keyed by the export's uuid — the
same shape as ``db_utils``'s engine cache — and graph state carries only the key.
:func:`release_reader` is called by the cleanup node whichever way the run ended,
because a cursor nobody closes is a connection nobody gets back.
"""

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Union

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from app.db.db_utils import get_engine
from app.services.datasource.metadata_service import rdbms_url
from app.services.deep_agents.query_executor import (
    NOT_AVAILABLE,
    ToolQueryError,
    assemble_built_query,
    assemble_sql_statement,
    labelled_rows,
    require_active_tables,
)
from app.utils.query_joins import RDBMS_DB_TYPES

logger = logging.getLogger(__name__)


# How many records go into one part file. The user's number, and a deliberately
# small one: a part is the unit of retry, so a batch that fails costs one small
# re-read rather than a large one.
BATCH_SIZE = 50

# The largest result set this application will export. A ceiling rather than a
# guess-and-hope: every export holds a database connection open for its whole run,
# and the honest way to bound that is to refuse the ones that cannot finish instead
# of starting them. An operator raises it when their hardware justifies it.
MAX_EXPORT_ROWS = int(os.getenv("DOWNLOAD_MAX_EXPORT_ROWS", "500000"))

# How many rows a streamed count pulls per round trip. Nothing is kept — only
# counted — so this trades round trips against nothing at all, and can be much
# larger than BATCH_SIZE.
_COUNT_STREAM_CHUNK = 5000


@dataclass(frozen=True)
class RecordSource:
    """
    Everything needed to read one tool's result set, and nothing more.

    Deliberately not a ``ToolConfig`` row: this is handed to the reader by the graph,
    which got it from the database in a request that has since ended. Holding plain
    values (plus the datasource row, which the executor needs for its url and its
    ``configuration_data``) means a reader never touches the application's session
    and cannot lazy-load a detached attribute halfway through an export.

    ``sql_query`` picks the mode, exactly as it does in ``query_executor``: present
    means run that statement, absent means build from ``config``. Passing the stored
    value rather than a mode flag is what makes the two impossible to disagree.

    ``value_bindings``, ``sql_params`` and ``agent_values`` are what a nested or
    parameterised tool is run *with*. They are on the source rather than on the
    reader because they are part of the query: a source without them is a different,
    wider query than the tool the agent calls, and reading that instead would export
    — or total — rows the tool itself would never return.

    ``label`` is merged into every row this source yields, and is how one iteration
    of a fan-out is told from another once the results are concatenated. See
    ``query_executor.labelled_rows``, which is where the collision rule lives.
    """

    datasource: Any
    config: Dict[str, Any]
    table_name: str
    sql_query: Optional[str] = None
    table_names: List[str] = field(default_factory=list)
    value_bindings: List[Dict[str, Any]] = field(default_factory=list)
    sql_params: List[Dict[str, Any]] = field(default_factory=list)
    agent_values: Dict[str, Any] = field(default_factory=dict)
    label: Optional[Dict[str, Any]] = None

    @property
    def is_sql_mode(self) -> bool:
        return bool((self.sql_query or "").strip())

    def require_relational(self) -> None:
        """
        Refuse a datasource that cannot be read this way, before anything opens.

        The same refusal ``query_executor._run_query`` makes, made here too rather
        than relied upon: this module opens its own connections, so it is this
        module's job to establish that there is something relational to connect to.
        """
        db_type = (self.datasource.db_type or "").strip().lower()

        if db_type not in RDBMS_DB_TYPES:
            raise ToolQueryError(
                f"This tool reads a {self.datasource.db_type or 'unknown'} "
                "datasource, and only relational databases (PostgreSQL, MySQL, "
                "SQLite) can be exported to a file.",
                advice=NOT_AVAILABLE,
            )


@dataclass(frozen=True)
class RecordCount:
    """
    How many records an export would contain.

    ``is_lower_bound`` is the honest half. It is true in exactly one situation — the
    set is larger than :data:`MAX_EXPORT_ROWS`, so counting stopped — and in that
    situation the export is refused anyway. Which means every count this application
    ever *offers* to a user is exact, and the flag exists to keep that sentence true
    rather than to hedge it.
    """

    total: int
    is_lower_bound: bool = False

    @property
    def exceeds_ceiling(self) -> bool:
        return self.is_lower_bound or self.total > MAX_EXPORT_ROWS


async def count_records(source: RecordSource) -> RecordCount:
    """
    The exact number of records the tool's query matches.

    This is the number the offer is made with, so an approximation would not do:
    "there are about 4,800 records" invites the same misreading that capped row
    counts already caused, which is the thing this feature exists to fix.

    Two ways to get it, one per mode:

    **Builder mode wraps the statement in a ``COUNT(*)``.** The statement is a
    ``Select`` assembled from reflected columns, so wrapping it is safe and the
    database does the counting — one cheap round trip, and correct for a grouped
    query too (it counts groups, which is what such a tool returns).

    **SQL mode counts by streaming.** The operator's statement cannot be wrapped:
    ``query_executor._execute_sql_query`` documents why (MySQL rejects a derived
    table with duplicate output column names, which is the sort of query the mode
    exists to permit), and rewriting approved SQL is not something this application
    does. So the cursor is opened and drained in large chunks, counting and keeping
    nothing. It stops one row past the ceiling: past that the export is refused, so
    reading further would be work done to produce a number nobody is allowed to act
    on.
    """
    source.require_relational()

    engine = await get_engine(rdbms_url(source.datasource))

    async with engine.connect() as connection:
        if source.is_sql_mode:
            require_active_tables(
                source.table_names or [source.table_name],
                source.datasource.configuration_data,
            )
            return await _count_by_streaming(
                connection,
                assemble_sql_statement(
                    source.sql_query,
                    source.value_bindings,
                    source.sql_params,
                    source.agent_values,
                ),
            )

        statement, _tables = await assemble_built_query(
            connection,
            source.config,
            source.table_name,
            (source.datasource.db_type or "").strip().lower(),
            source.datasource.configuration_data,
            source.value_bindings,
            source.agent_values,
        )

        try:
            total = await connection.scalar(
                select(func.count()).select_from(statement.subquery()),
            )
        except SQLAlchemyError:
            # A grouped or windowed statement some dialect will not accept as a
            # derived table. Falling back rather than failing: an exact count is
            # still reachable by reading, and a count that cannot be produced would
            # cost the user the whole feature for a query that works fine otherwise.
            logger.warning(
                "COUNT(*) over the built query for %s was rejected; counting by "
                "streaming instead",
                source.table_name,
            )
            return await _count_by_streaming(connection, statement)

    return RecordCount(total=int(total or 0))


async def _count_by_streaming(connection, statement) -> RecordCount:
    """
    Count by reading, in large chunks, keeping nothing.

    Stops at :data:`MAX_EXPORT_ROWS` + 1 so "more than the ceiling" is observed
    rather than inferred, and reports it as a lower bound. The cursor is closed in a
    ``finally``: the whole point of streaming is that the rest of the result set is
    never read, and that only holds if the cursor is released rather than drained by
    the connection going back to the pool.
    """
    ceiling = MAX_EXPORT_ROWS + 1
    total = 0

    result = await connection.stream(statement)

    try:
        while total < ceiling:
            chunk = await result.fetchmany(
                min(_COUNT_STREAM_CHUNK, ceiling - total),
            )
            if not chunk:
                return RecordCount(total=total)
            total += len(chunk)
    finally:
        await result.close()

    return RecordCount(total=total, is_lower_bound=True)


class BatchReader:
    """
    One open cursor over a tool's result set, read in batches.

    Batches are numbered from 1, matching the part files and the progress messages a
    person reads. :meth:`read` is normally called with consecutive numbers and is
    then a single ``fetchmany`` — the cheap path, and the only one a successful
    export takes.

    Asking for a batch out of order is the retry path, and it re-opens: after a
    failure the cursor is gone, and a cursor that raised cannot be assumed to have
    left the connection in a usable state. Re-opening means the statement runs again
    and the reader discards its way forward, which is linear in the rows already
    read. That cost is real and it is the right place for it — a successful export
    never pays it.
    """

    def __init__(self, source: RecordSource, batch_size: int = BATCH_SIZE) -> None:
        self._source = source
        self._batch_size = max(1, int(batch_size))
        self._connection = None
        # The streamed result, and the mappings view over it. Two references to one
        # cursor: `fetchmany` is taken from the view because a Row is a tuple and a
        # record has to be a dict, and `close` is called on the result itself, which
        # is what actually owns the server-side cursor.
        self._result = None
        self._rows = None
        # The number of the batch :meth:`read` would serve next off the open cursor.
        self._next_batch = 1

    @property
    def batch_size(self) -> int:
        return self._batch_size

    async def read(self, batch_number: int) -> List[Dict[str, Any]]:
        """
        The records for ``batch_number``, as dictionaries. Empty means the end.

        A short batch is not the end — a database may return fewer rows than asked
        for and still have more — so the caller decides it has finished when this
        returns nothing at all, which is what :meth:`read` guarantees an exhausted
        cursor does.
        """
        if batch_number < 1:
            raise ValueError("Batch numbers start at 1")

        if self._rows is None or batch_number != self._next_batch:
            await self._reopen_at(batch_number)

        rows = await self._rows.fetchmany(self._batch_size)
        self._next_batch = batch_number + 1

        if not rows:
            return []

        return labelled_rows([dict(row) for row in rows], self._source.label)

    async def close(self) -> None:
        """
        Release the cursor and the connection.

        Safe to call twice, and called on every path out of an export — including
        the aborted one, which is the path where forgetting would matter most.
        """
        result, connection = self._result, self._connection
        self._result, self._rows, self._connection = None, None, None

        if result is not None:
            try:
                await result.close()
            except SQLAlchemyError:
                # The cursor is being discarded either way; a driver complaining
                # about closing something already broken is not worth propagating
                # over the top of whatever actually went wrong.
                logger.debug("Ignoring failure closing an export cursor", exc_info=True)

        if connection is not None:
            try:
                await connection.close()
            except SQLAlchemyError:
                logger.debug(
                    "Ignoring failure closing an export connection", exc_info=True,
                )

    async def _reopen_at(self, batch_number: int) -> None:
        """
        Open a fresh cursor positioned at the start of ``batch_number``.

        Discarding is done with ``fetchmany`` in large chunks rather than row by row,
        and the rows are not converted to dictionaries on the way past — they are
        being thrown away, and building objects for them would be the expensive part
        of an already unwelcome operation.
        """
        await self.close()

        source = self._source
        source.require_relational()

        engine = await get_engine(rdbms_url(source.datasource))
        self._connection = await engine.connect()

        if source.is_sql_mode:
            require_active_tables(
                source.table_names or [source.table_name],
                source.datasource.configuration_data,
            )
            statement = assemble_sql_statement(
                source.sql_query,
                source.value_bindings,
                source.sql_params,
                source.agent_values,
            )
        else:
            statement, _tables = await assemble_built_query(
                self._connection,
                source.config,
                source.table_name,
                (source.datasource.db_type or "").strip().lower(),
                source.datasource.configuration_data,
                source.value_bindings,
                source.agent_values,
            )

        self._result = await self._connection.stream(statement)
        self._rows = self._result.mappings()
        self._next_batch = 1

        to_skip = (batch_number - 1) * self._batch_size

        if to_skip:
            logger.info(
                "Re-opening the export cursor for %s and skipping %d row(s) to reach "
                "batch %d",
                source.table_name,
                to_skip,
                batch_number,
            )

        while to_skip > 0:
            chunk = await self._rows.fetchmany(min(_COUNT_STREAM_CHUNK, to_skip))
            if not chunk:
                # The result set shrank between runs. Not an error to raise here:
                # the read that follows returns nothing, and "there are no more
                # records" is a state the caller already handles.
                break
            to_skip -= len(chunk)

        self._next_batch = batch_number


class ChainedBatchReader:
    """
    One cursor at a time over **several** sources, read as if they were one.

    An iterating chain runs the same statement once per value, so its whole result
    set is not one query's rows but the concatenation of N queries' rows. Nothing
    downstream should have to know that: the aggregation folds batches, and a batch
    is a batch whichever source it came from.

    So this holds a queue of sources and one :class:`BatchReader` at a time. When the
    current source is exhausted it is closed and the next is opened, and
    :meth:`read` returns nothing only once **every** source is spent — which is the
    contract ``aggregate_graph.read_wave`` already relies on to decide it has
    finished.

    Batch numbers are global and consecutive. That is the only path the aggregation
    takes, and it is the cheap one: each ``read`` is a single ``fetchmany`` off the
    open cursor. An out-of-order read is the retry path and restarts from the first
    source, discarding forward — the same cost model, and the same reasoning, as
    :meth:`BatchReader._reopen_at`, except that here the discarding may cross a
    source boundary.
    """

    def __init__(
        self,
        sources: Sequence[RecordSource],
        batch_size: int = BATCH_SIZE,
    ) -> None:
        if not sources:
            raise ToolQueryError(
                "There is nothing to read: the query produced no source to run.",
            )

        self._sources = list(sources)
        self._batch_size = max(1, int(batch_size))
        self._reader: Optional[BatchReader] = None
        self._index = 0
        # Two counters, because they are two different things: `_next_batch` is what
        # the caller asks for and runs across every source, `_source_batch` is what
        # the open cursor is up to and restarts at 1 with each new source.
        self._next_batch = 1
        self._source_batch = 1

    @property
    def batch_size(self) -> int:
        return self._batch_size

    async def read(self, batch_number: int) -> List[Dict[str, Any]]:
        """
        The records for ``batch_number``. Empty means every source is exhausted.

        A source returning nothing rolls forward rather than ending the run, and the
        loop is a ``while`` rather than an ``if`` because a source may legitimately
        return **no rows at all** — a department with no projects — and two of those
        in a row must not be read as the end.
        """
        if batch_number < 1:
            raise ValueError("Batch numbers start at 1")

        if batch_number != self._next_batch:
            await self._restart_at(batch_number)

        while self._index < len(self._sources):
            if self._reader is None:
                self._reader = BatchReader(
                    self._sources[self._index], batch_size=self._batch_size,
                )
                self._source_batch = 1

            rows = await self._reader.read(self._source_batch)

            if rows:
                self._source_batch += 1
                self._next_batch = batch_number + 1
                return rows

            await self._close_current()
            self._index += 1

        self._next_batch = batch_number + 1

        return []

    async def close(self) -> None:
        """Release whichever cursor is open. Safe to call twice."""
        await self._close_current()
        self._index = len(self._sources)

    async def _close_current(self) -> None:
        reader, self._reader = self._reader, None

        if reader is not None:
            await reader.close()

    async def _restart_at(self, batch_number: int) -> None:
        """
        Rewind to the first source and read forward, discarding, to ``batch_number``.

        Only the retry path reaches this. Discarding through whole sources is
        expensive, and saying so here is better than hiding it: a successful run
        never pays it, and a failed one is re-reading data it has already proved it
        can read.
        """
        logger.info(
            "Restarting a chained read at batch %d across %d source(s)",
            batch_number,
            len(self._sources),
        )

        await self._close_current()
        self._index = 0
        self._next_batch = 1

        for number in range(1, batch_number):
            if not await self.read(number):
                break


async def count_all(sources: Sequence[RecordSource]) -> RecordCount:
    """
    How many records every source holds between them.

    Summed rather than estimated, and short-circuited the moment the total passes the
    ceiling: past that the run is refused anyway, so counting the remaining sources
    would be work done to produce a number nobody is allowed to act on. That is the
    same judgement :func:`_count_by_streaming` makes within one source.
    """
    total = 0

    for source in sources:
        counted = await count_records(source)

        if counted.is_lower_bound:
            return RecordCount(total=total + counted.total, is_lower_bound=True)

        total += counted.total

        if total > MAX_EXPORT_ROWS:
            return RecordCount(total=total, is_lower_bound=True)

    return RecordCount(total=total)


# --------------------------------------------------------------------------
# Reader registry
#
# A live cursor cannot travel in graph state — the checkpointer serialises state to
# JSON — so it lives here and the state carries the key. Keyed by the export's uuid
# string, which is unique per run and is what every node already has.
# --------------------------------------------------------------------------

_readers: Dict[str, Union[BatchReader, ChainedBatchReader]] = {}


def get_reader(
    key: str,
    source: Union[RecordSource, Sequence[RecordSource]],
    batch_size: int = BATCH_SIZE,
) -> Union[BatchReader, ChainedBatchReader]:
    """
    The reader for one run, created on first use.

    Not async: creating a reader opens nothing. The cursor is opened by the first
    ``read``, so a graph that is interrupted before it reads anything leaves no
    connection behind.

    A sequence of sources gets a :class:`ChainedBatchReader`, which presents exactly
    the same two methods — so the caller reads a fan-out and a single query with the
    same three lines, and neither the wave loop nor the cleanup node has to know
    which it has.
    """
    reader = _readers.get(key)

    if reader is None:
        reader = (
            ChainedBatchReader(source, batch_size=batch_size)
            if isinstance(source, (list, tuple))
            else BatchReader(source, batch_size=batch_size)
        )
        _readers[key] = reader

    return reader


async def release_reader(key: str) -> None:
    """
    Close and forget the reader for one export.

    Called by the cleanup node on every path — success, abort, and the abort's own
    failure. A reader left in this dict is a connection that never returns to the
    pool, so this is not tidying up, it is the other half of :func:`get_reader`.
    """
    reader = _readers.pop(key, None)

    if reader is not None:
        await reader.close()


async def release_all_readers() -> None:
    """
    Close every open reader. For application shutdown, and for tests.

    Iterates over a copy of the keys because :func:`release_reader` mutates the
    registry.
    """
    for key in list(_readers):
        await release_reader(key)
