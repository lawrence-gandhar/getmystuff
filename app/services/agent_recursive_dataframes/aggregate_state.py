"""
What travels along the edges of the aggregation graph.

Almost nothing, deliberately. A run of 200,000 records is 250 waves, and LangGraph
copies state on every super-step — so the records themselves live in
:mod:`app.services.agent_recursive_dataframes.frame_buffer` and the state carries
their keys. Putting a wave's records or a 100,000-group running aggregate in here
would mean copying them 250 times to move them three nodes.

**Reducers on the three fields a fan-out writes at once.** A wave's workers all
return in the same super-step, and LangGraph refuses two writes to a plain field
rather than silently keeping one — which is the right refusal, and the reason these
three are annotated:

* ``wave_slots`` accumulates. Without a reducer the last worker to finish would
  replace the others rather than join them, and the merged aggregate would be
  quietly missing three quarters of its batches.
* ``failure`` and ``advice`` keep the first. One bad column fails all four slices of
  a wave in the same step, and those are one fault seen four times — showing a
  visitor the same sentence four times would be worse than showing it once.

Every other field has a single writer, so last-write-wins is not a compromise
there, it is the truth.

``wave_slots``'s reducer also has to *clear*, because a wave's slots must not
survive into the next wave. LangGraph gives no way to reset an accumulating field
except through the reducer itself, so :data:`RESET` is a value ``merge_wave`` writes
to mean "this wave is folded in, start the next one empty".
"""

from typing import Annotated, Any, Dict, List, TypedDict, Union

# What merge_wave writes to wave_slots to end a wave. A sentinel rather than an
# empty list, because an empty list is what a wave that read nothing legitimately
# produces, and the reducer must be able to tell "nothing arrived" from "clear it".
RESET = "__reset__"


def collect_slots(
    current: Union[List[str], None],
    incoming: Union[List[str], str, None],
) -> List[str]:
    """
    Accumulate the buffer keys a wave's workers wrote; :data:`RESET` empties it.

    De-duplicated by key. A worker whose node is re-entered would otherwise appear
    twice in the list and have its slice merged twice — which does not silently
    lose records, it silently doubles them, and a doubled total is harder to
    notice than a missing one.
    """
    if incoming == RESET or incoming is None:
        return []

    merged = list(current or [])

    for key in incoming:
        if key not in merged:
            merged.append(key)

    return merged


def keep_first(current: Union[str, None], incoming: Union[str, None]) -> str:
    """
    The first thing written wins; later writes in the same step are dropped.

    For the failure and its advice. Several workers of one wave fail together —
    a column that is not numeric is not numeric in any of the four slices — and
    they are one fault, not four. Keeping the first is also what makes the message
    the *earliest* explanation rather than whichever thread happened to finish
    last.
    """
    return current if current else (incoming or "")


class AggregateState(TypedDict, total=False):
    """
    The run, as the graph sees it.

    ``total=False`` because nodes return partial updates — a node that only read a
    wave says so and nothing else, which is what makes the routers readable.
    """

    # Identity. Every frame_buffer key and the reader's registry key are built from
    # this, so releasing the run means releasing everything named after it.
    run_id: str

    # The validated plan, as plain JSON. Validated before the graph starts, so no
    # node has to re-check it and none of them can disagree about what it says.
    plan: Dict[str, Any]

    # What the count node established, before anything was read.
    total_rows: int

    # The wave loop. `next_batch` is the reader's own numbering, which starts at 1
    # and is never restarted — that is what makes each record read exactly once.
    wave: int
    next_batch: int
    rows_read: int
    finished_reading: bool

    # The keys read_wave stashed this wave, in read order. Written by one node and
    # consumed by the fan-out router, so no reducer.
    pending_slots: List[str]

    # The keys the workers wrote their partial aggregates to. The only field with
    # more than one writer, and so the only one with a reducer.
    wave_slots: Annotated[List[str], collect_slots]

    # How many groups the running aggregate holds. The aggregate itself is in the
    # frame buffer; this is the part the cap is checked against and the part worth
    # copying between super-steps.
    group_count: int

    # Row mode only: how many records matched the plan's filters, across every wave.
    # Counted rather than measured off the running frame, because that frame is
    # truncated to what will be shown — so after the merge there is nothing left to
    # count, and this is the number the answer's "out of" figure comes from. Written by
    # merge_wave alone, so last-write-wins is the truth here as it is for the counters
    # above.
    matched_rows: int

    # The finalised rows, set once by finalise.
    result: List[Dict[str, Any]]

    # A failure in the operator's words, plus the instruction for a model relaying
    # it to a visitor — the same split ToolQueryError makes, carried through state
    # because nodes return failures rather than raising them. Reduced, because a
    # whole wave of workers can fail in one super-step.
    failure: Annotated[str, keep_first]
    advice: Annotated[str, keep_first]


class SliceState(TypedDict):
    """
    What one ``Send`` carries to one worker.

    A strict subset of :class:`AggregateState`'s key names plus ``slot``, so a
    worker cannot read anything the run has not decided yet — it gets the plan, the
    key to its own records, and nothing else.
    """

    run_id: str
    plan: Dict[str, Any]
    wave: int
    slot: str


def initial_state(run_id: str, plan: Dict[str, Any]) -> AggregateState:
    """
    The state a run starts from.

    Written out rather than left to ``total=False`` defaults: a node reading
    ``state["wave"]`` on the first super-step should find 1, not raise a KeyError,
    and the counters that must start at a particular number are exactly the ones
    worth seeing together.
    """
    return {
        "run_id": run_id,
        "plan": plan,
        "total_rows": 0,
        "wave": 1,
        # Batch numbers start at 1 — record_reader.BatchReader.read refuses 0.
        "next_batch": 1,
        "rows_read": 0,
        "finished_reading": False,
        "pending_slots": [],
        "wave_slots": [],
        "group_count": 0,
        "matched_rows": 0,
        "result": [],
        "failure": "",
        "advice": "",
    }
