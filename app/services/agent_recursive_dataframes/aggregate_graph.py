"""
Reading a tool's whole result set and folding it into an aggregate, as a graph.

    START → get_count → read_wave ──Send×W──→ aggregate_slice ─┐
                            ▲                                  │ (barrier)
                            └───────── merge_wave ◄────────────┘
                                            │
                                    finalise → cleanup → END
        any failure ──────────→ notify_failure → cleanup → END

**The divider reads; the workers aggregate.** That division is forced, not chosen.
``record_reader.BatchReader`` is one server-side cursor: ``read()`` advances it, and
asking for a batch out of order re-runs the statement and rescans from the top. Two
tasks reading it at once would turn a linear scan into repeated full rescans, or
collide inside the driver. So ``read_wave`` reads its whole wave itself, in order —
which is cheap, being ``AGGREGATE_WAVE_WIDTH`` ``fetchmany`` calls on an already
open cursor — and what fans out is the folding of what it read. A wave costs
``read + max(fold)`` rather than ``read + Σ fold``.

**The barrier is free.** Every ``Send`` returned by one router runs in a single
super-step, so the plain edge out of ``aggregate_slice`` schedules ``merge_wave``
exactly once, after every worker of that wave has written. There is no barrier to
implement, and writing one would be writing a second, worse one.

**Nodes return failures; they do not raise.** A raise inside a ``Send`` super-step
ends the run with no route to ``cleanup``, and cleanup is what closes the cursor.
So every node catches and returns ``{"failure": ..., "advice": ...}``, and the
routers check for that before anything else — the same discipline
``downloader_agents.base.download_graph`` keeps, for the same reason.

**Cancellation is the leak the cleanup node cannot cover.** A chat turn timing out
cancels the task mid-node, and a cancelled node routes nowhere. So
:func:`run_aggregation` releases in a ``finally`` as well: the cleanup node is the
tidy path, the ``finally`` is the guarantee.

**No checkpointer.** There is no ``interrupt()`` here and nothing to resume across
requests, so the graph compiles without one — as ``tool_chain_graph`` does, and
unlike the export graph, which genuinely pauses between turns. Checkpointing would
write the whole state 750 times for a large run to buy a resume nobody asks for.
"""

import asyncio
import logging
from typing import Any, Dict, List, Mapping, Sequence, Union

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from app.services.agent_recursive_dataframes import (
    filter_algebra as filters,
    frame_buffer,
    frame_ops,
)
from app.services.agent_recursive_dataframes.aggregate_state import (
    RESET,
    AggregateState,
    SliceState,
    initial_state,
)
from app.services.deep_agents.query_executor import (
    NEEDS_RECONFIGURING,
    ToolQueryError,
)
from app.services.downloader_agents.base import record_reader
from app.services.downloader_agents.base.record_reader import RecordSource

logger = logging.getLogger(__name__)


GET_COUNT = "get_count"
READ_WAVE = "read_wave"
AGGREGATE_SLICE = "aggregate_slice"
MERGE_WAVE = "merge_wave"
FINALISE = "finalise"
NOTIFY_FAILURE = "notify_failure"
CLEANUP = "cleanup"

# A wave is three super-steps: read, the whole fan-out (parallel Sends count once),
# and merge.
_STEPS_PER_WAVE = 3


def _recursion_limit() -> int:
    """
    How many super-steps a full run may take, computed rather than guessed.

    LangGraph's default is 25, which at these batch sizes is eight waves — a run
    would stop a few thousand records in and raise ``GraphRecursionError``, which
    reads like a bug in the graph rather than a ceiling being reached. So the
    limit is derived from the ceilings that actually bound the run, plus room for
    the fixed nodes at either end.
    """
    from app.services.agent_recursive_dataframes import aggregate_service

    per_wave = max(
        1, aggregate_service.AGGREGATE_WAVE_WIDTH * aggregate_service.AGGREGATE_CHUNK_ROWS,
    )
    waves = aggregate_service.AGGREGATE_MAX_SOURCE_ROWS // per_wave + 1

    return _STEPS_PER_WAVE * waves + 50


# --------------------------------------------------------------------------
# Nodes
# --------------------------------------------------------------------------


def _node_runner(supply):  # noqa: ANN001 — a QuerySupply or a MaterialisedSupply
    """
    The nodes, closed over the supply they read.

    A closure rather than state because a ``QuerySupply`` holds a live ORM datasource
    row with an encrypted password on it — not something to copy through a graph's
    state on every super-step, and not something a worker needs at all.

    A **supply** rather than a list of sources, because a designed graph's result is
    already in memory and a tool config's is a cursor. Nothing below this line knows
    which it has: ``open`` returns a reader with the same two methods either way, and a
    batch is a batch whoever produced it. See :mod:`row_supply`.
    """

    async def get_count(state: AggregateState) -> dict:
        """
        How many records this run will read, established before it reads any.

        The ceiling is checked here rather than while reading so a run that cannot
        finish is refused up front, with the real number in the message, instead of
        being abandoned after four minutes of holding a cursor open. It is checked for
        a materialised result too: those rows are already in memory, but folding
        200,001 of them still cannot finish inside a conversation turn.
        """
        from app.services.agent_recursive_dataframes import aggregate_service

        try:
            counted = await supply.count()
        except ToolQueryError as exc:
            return _failed(str(exc), exc.advice)
        except Exception as exc:  # noqa: BLE001 - see _failed
            logger.exception("Counting records for the aggregation failed")
            return _failed(
                f"The records to be grouped could not be counted: {exc}",
            )

        if counted.is_lower_bound or counted.total > aggregate_service.AGGREGATE_MAX_SOURCE_ROWS:
            return _failed(
                aggregate_service.too_large_message(
                    counted.total, counted.is_lower_bound, subject=supply.subject,
                ),
                NEEDS_RECONFIGURING,
            )

        return {"total_rows": int(counted.total)}

    async def read_wave(state: AggregateState) -> dict:
        """
        The divider: read this wave's batches off the one cursor, in order.

        Sequential is not a compromise made for simplicity — see the module
        docstring. Each batch is stashed under its own key and only the keys go
        into state, so a wave moves 800 records between nodes as four short
        strings.
        """
        from app.services.agent_recursive_dataframes import aggregate_service

        reader = supply.open(
            _reader_key(state["run_id"]),
            aggregate_service.AGGREGATE_CHUNK_ROWS,
        )

        slots: List[str] = []
        batch = int(state["next_batch"])
        read = 0

        try:
            for index in range(aggregate_service.AGGREGATE_WAVE_WIDTH):
                rows = await reader.read(batch)

                # Empty means the cursor is exhausted. A *short* batch does not —
                # a database may return fewer rows than asked for and still have
                # more, which is why this checks for nothing rather than for less.
                if not rows:
                    break

                slots.append(frame_buffer.stash(
                    frame_buffer.slot_key(state["run_id"], state["wave"], index),
                    rows,
                ))
                read += len(rows)
                batch += 1
        except ToolQueryError as exc:
            return _failed(str(exc), exc.advice)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Reading wave %s failed", state.get("wave"))
            return _failed(f"The records could not be read: {exc}")

        return {
            "pending_slots": slots,
            "next_batch": batch,
            "rows_read": int(state.get("rows_read") or 0) + read,
            "finished_reading": not slots,
        }

    async def aggregate_slice(state: SliceState) -> dict:
        """
        One slice folded into a partial aggregate, in a worker thread.

        ``asyncio.to_thread`` is what makes the fan-out real. polars runs
        ``group_by`` in Rust with the GIL released, so several of these overlap;
        called inline the fold would hold the event loop and the workers would run
        one after another — and every other request in the process would wait for
        them.

        Returns a key, never a frame. State stays a handful of strings.
        """
        try:
            rows = frame_buffer.take(state["slot"])
            partial = await asyncio.to_thread(
                frame_ops.partial_aggregate, rows, state["plan"],
            )
        except ToolQueryError as exc:
            return _failed(str(exc), exc.advice)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Aggregating a slice of wave %s failed", state["wave"])
            return _failed(f"A batch of records could not be grouped: {exc}")

        if partial is None:
            return {}

        return {
            "wave_slots": [
                frame_buffer.stash(frame_buffer.partial_key(state["slot"]), partial),
            ],
        }

    async def merge_wave(state: AggregateState) -> dict:
        """
        Fold this wave's partials into the running aggregate, then clear the wave.

        Failure is checked before anything else. A wave with a failed slice has
        neither aggregated nor finished, and merging what did succeed would produce
        a total quietly short by one batch — the one outcome worse than no answer.
        """
        from app.services.agent_recursive_dataframes import aggregate_service

        if state.get("failure"):
            return {"wave_slots": RESET}

        keys = list(state.get("wave_slots") or [])

        if not keys:
            # Nothing to fold: either the cursor is exhausted or this wave's slices
            # were all empty. Either way the run is finished reading.
            return {"wave_slots": RESET}

        running_key = frame_buffer.running_key(state["run_id"])

        try:
            partials = [frame_buffer.take(key) for key in keys]
            # The matched count is taken before the merge, because in row mode the
            # merge truncates to what will be shown — so afterwards there is nothing
            # left to count. This number, not the frame's height, is the "out of"
            # figure the answer reports.
            matched = sum(
                int(partial.height) for partial in partials if partial is not None
            )
            merged = await asyncio.to_thread(
                frame_ops.merge_partials,
                frame_buffer.peek(running_key),
                partials,
                state["plan"],
                aggregate_service.KEEP_MATCHED_ROWS,
            )
        except ToolQueryError as exc:
            return {"wave_slots": RESET, **_failed(str(exc), exc.advice)}
        except Exception as exc:  # noqa: BLE001
            logger.exception("Merging wave %s failed", state.get("wave"))
            return {
                "wave_slots": RESET,
                **_failed(f"Grouped results could not be combined: {exc}"),
            }

        matched_so_far = int(state.get("matched_rows") or 0) + matched

        if _rows_mode(state):
            # No group ceiling in row mode: what the running frame holds is bounded by
            # KEEP_MATCHED_ROWS, so the memory question MAX_GROUPS answers cannot arise.
            frame_buffer.stash(running_key, merged)

            return {
                "wave_slots": RESET,
                "matched_rows": matched_so_far,
                "wave": int(state.get("wave") or 1) + 1,
            }

        groups = frame_ops.group_count(merged)

        if groups > aggregate_service.MAX_GROUPS:
            # Discarded, not truncated. See too_many_groups_message.
            frame_buffer.release_run(state["run_id"])
            return {
                "wave_slots": RESET,
                "group_count": groups,
                **_failed(
                    aggregate_service.too_many_groups_message(groups),
                    NEEDS_RECONFIGURING,
                ),
            }

        frame_buffer.stash(running_key, merged)

        return {
            "wave_slots": RESET,
            "group_count": groups,
            "matched_rows": matched_so_far,
            "wave": int(state.get("wave") or 1) + 1,
        }

    async def finalise(state: AggregateState) -> dict:
        """
        The carried fields become the reported numbers, sorted.

        Reached only when every record has been read, which is what makes the answer
        exact rather than a sample of one. Every group is reported —
        ``MAX_RESULT_ROWS`` is ``None`` — so the answer is now exact in both
        directions: each group's number is over the whole set, and the set of groups
        is all of them.

        In row mode there are no carried fields and nothing to divide: the retained
        records come back in read order, and the number beside them is how many
        **matched**, which is the count merge_wave accumulated rather than the height of
        what is being shown.
        """
        from app.services.agent_recursive_dataframes import aggregate_service

        merged = frame_buffer.peek(frame_buffer.running_key(state["run_id"]))

        try:
            rows = await asyncio.to_thread(
                frame_ops.finalise,
                merged,
                state["plan"],
                aggregate_service.MAX_RESULT_ROWS,
            )
        except ToolQueryError as exc:
            return _failed(str(exc), exc.advice)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Finalising the aggregate failed")
            return _failed(f"The grouped results could not be produced: {exc}")

        if _rows_mode(state):
            logger.info(
                "Aggregation %s read %d record(s), %d matched, over %d wave(s)",
                state["run_id"],
                int(state.get("rows_read") or 0),
                int(state.get("matched_rows") or 0),
                int(state.get("wave") or 1) - 1,
            )

            return {"result": rows}

        logger.info(
            "Aggregation %s read %d record(s) into %d group(s) over %d wave(s)",
            state["run_id"],
            int(state.get("rows_read") or 0),
            frame_ops.group_count(merged),
            int(state.get("wave") or 1) - 1,
        )

        return {"result": rows, "group_count": frame_ops.group_count(merged)}

    async def notify_failure(state: AggregateState) -> dict:
        """
        The failure path's own node, so cleanup has one predecessor per outcome.

        Nothing partial survives it: whatever was folded is dropped rather than
        reported, because a total assembled from part of the records is wrong in a
        way nothing about it would show.
        """
        logger.warning(
            "Aggregation %s failed after %d record(s): %s",
            state.get("run_id"),
            int(state.get("rows_read") or 0),
            state.get("failure"),
        )

        return {"result": [], "group_count": 0}

    async def cleanup(state: AggregateState) -> dict:
        """
        Close the cursor and drop the buffers, whichever way the run ended.

        One node with several inbound edges rather than a ``finally`` repeated in
        five places — a cursor nobody closes is a connection that never goes back
        to the pool.
        """
        await supply.release(_reader_key(state["run_id"]))
        frame_buffer.release_run(state["run_id"])

        return {}

    return {
        GET_COUNT: get_count,
        READ_WAVE: read_wave,
        AGGREGATE_SLICE: aggregate_slice,
        MERGE_WAVE: merge_wave,
        FINALISE: finalise,
        NOTIFY_FAILURE: notify_failure,
        CLEANUP: cleanup,
    }


def _rows_mode(state: AggregateState) -> bool:
    """
    Whether this run answers with the matching records rather than with numbers.

    Read off the plan, which decided it once — see ``aggregate_planner.validate_plan``.
    A node re-deriving it from ``not plan["aggregations"]`` would be a fourth place with
    an opinion about what an empty measure list meant.
    """
    return filters.mode_of(state.get("plan") or {}) == filters.MODE_ROWS


def _failed(message: str, advice: str = NEEDS_RECONFIGURING) -> dict:
    """
    A node's failure, in the shape every node returns it.

    Two audiences in one update, the same split ``ToolQueryError`` makes: the
    message is the fault as an operator reads it, the advice is what a model
    relaying it to a visitor should do about it.
    """
    return {"failure": message, "advice": advice}


def _reader_key(run_id: str) -> str:
    """
    This run's key in the shared reader registry.

    Prefixed, because that registry is also the export path's: a key that could not
    be told apart from an export's uuid would hand one run the other's cursor
    position. ``release_all_readers`` at shutdown covers both.
    """
    return f"agg:{run_id}"


# --------------------------------------------------------------------------
# Routers
# --------------------------------------------------------------------------


def _after_count(state: AggregateState) -> str:
    if state.get("failure"):
        return "failed"

    # No records is an answer, not a failure: "nothing matched" is a fact a person
    # can act on, and finalise renders it as an empty result rather than an error.
    return "empty" if not int(state.get("total_rows") or 0) else "read"


def _fan_out(state: AggregateState) -> Union[List[Send], str]:
    """
    The map half: one ``Send`` per slice this wave read, all in one super-step.

    Returning ``MERGE_WAVE`` when there is nothing left is what ends the loop —
    merge sees an empty wave, writes nothing, and the router after it finds the
    run finished.
    """
    if state.get("failure"):
        return NOTIFY_FAILURE

    slots = list(state.get("pending_slots") or [])

    if not slots:
        return MERGE_WAVE

    return [
        Send(
            AGGREGATE_SLICE,
            {
                "run_id": state["run_id"],
                "plan": state["plan"],
                "wave": state["wave"],
                "slot": slot,
            },
        )
        for slot in slots
    ]


def _after_merge(state: AggregateState) -> str:
    """
    Failure first, then completion — and the order is the whole point.

    A wave whose slice failed has neither aggregated nor finished. Checking
    ``finished_reading`` first would send such a run to ``finalise``, which would
    report the groups that did merge as though they were all of them.
    """
    if state.get("failure"):
        return "failed"

    return "finished" if state.get("finished_reading") else "continue"


# --------------------------------------------------------------------------
# The graph
# --------------------------------------------------------------------------


def build_graph(supply):  # noqa: ANN001 — see _node_runner
    """
    Compile the aggregation graph for one run's supply of records.

    Built per run rather than cached, because the nodes close over the supply. That
    is cheap next to reading a result set, and it is what keeps a datasource row out
    of graph state.

    The graph is the same shape however many sources there are, and whether they are
    queries or a graph's finished result. Rolling from one to the next is the reader's
    job, not a node's, which is what keeps the wave loop reading "until there are no
    more records" rather than "until there are no more records in this query".
    """
    nodes = _node_runner(supply)
    graph = StateGraph(AggregateState)

    for name, runner in nodes.items():
        graph.add_node(name, runner)

    graph.add_edge(START, GET_COUNT)
    graph.add_conditional_edges(
        GET_COUNT,
        _after_count,
        {"read": READ_WAVE, "empty": FINALISE, "failed": NOTIFY_FAILURE},
    )
    graph.add_conditional_edges(
        READ_WAVE, _fan_out, [AGGREGATE_SLICE, MERGE_WAVE, NOTIFY_FAILURE],
    )
    # The barrier: one plain edge out of the fanned-out node, so merge_wave runs
    # once in the next super-step with every worker's write already applied.
    graph.add_edge(AGGREGATE_SLICE, MERGE_WAVE)
    graph.add_conditional_edges(
        MERGE_WAVE,
        _after_merge,
        {"continue": READ_WAVE, "finished": FINALISE, "failed": NOTIFY_FAILURE},
    )
    graph.add_edge(FINALISE, CLEANUP)
    graph.add_edge(NOTIFY_FAILURE, CLEANUP)
    graph.add_edge(CLEANUP, END)

    return graph.compile()


async def run_aggregation(
    supply,  # noqa: ANN001 — see _node_runner
    plan: Mapping[str, Any],
    run_id: str,
) -> Dict[str, Any]:
    """
    Run one aggregation to completion and return what it found.

    The ``finally`` is not belt and braces. A chat turn that times out cancels this
    task mid-node, and a cancelled node routes nowhere — so ``cleanup`` never runs,
    and without this the cursor it would have closed stays checked out of the pool
    for the life of the process.

    Raises ``ToolQueryError`` for a failed run rather than returning it, so the
    failure reaches its caller the way every other tool failure does: the tool
    wrapper turns it into tool output, the route renders it into an alert.

    ``group_count`` carries the **"out of" number in both modes** — how many groups
    there were, or how many records matched. One field rather than two because it has
    one job: it is what ``describe_result`` renders "200 of 4,317" from, and a second
    field would let a caller show the wrong one.
    """
    compiled = build_graph(supply)

    try:
        state = await compiled.ainvoke(
            initial_state(run_id, dict(plan)),
            config={"recursion_limit": _recursion_limit()},
        )
    finally:
        await supply.release(_reader_key(run_id))
        frame_buffer.release_run(run_id)

    if state.get("failure"):
        raise ToolQueryError(
            str(state["failure"]),
            advice=str(state.get("advice") or NEEDS_RECONFIGURING),
        )

    rows_mode = filters.mode_of(plan) == filters.MODE_ROWS
    counted = (
        int(state.get("matched_rows") or 0) if rows_mode
        else int(state.get("group_count") or 0)
    )

    return {
        "mode": filters.mode_of(plan),
        "rows": list(state.get("result") or []),
        "group_count": counted,
        "records_read": int(state.get("rows_read") or 0),
        "total_records": int(state.get("total_rows") or 0),
    }
