"""
app/schemas/integrations/run_schemas.py

Pydantic schemas for a run in flight and a run afterwards.

**One shape for the stream and for the poll.** :class:`RunFrameView` is what an SSE frame
carries and what the polling endpoint returns, because a client whose stream dropped and
fell back to polling must not have to understand a second payload. The engine builds the
dict — ``run_store.run_view`` — and this validates it on the way out.

**Whole state for the numbers, a window for the list.** Every counter is absolute, so a
consumer that missed a frame is not left holding a wrong total; a delta-based frame is
wrong for anything somebody bills on. The steps are the last hundred plus ``steps_total``,
with the rest paginated, because a fifty-thousand-step run must not arrive on every
one-second poll.

**Two numbers for records, deliberately.** ``counts.failed`` is how many records failed;
``logged.failed`` is how many of those the log kept. They diverge once a run passes the
cap, and showing only the second would quietly under-report a bad sync. The difference is
the honest statement that some detail is missing, which is why
``records_log_truncated`` is on the frame rather than inferred.
"""

from typing import Any, Dict, List, Optional

from pydantic import Field

from app.schemas.base import ResponseSchema


class RunCountsView(ResponseSchema):
    """The four numbers the counters strip repaints from. Absolute, never deltas."""

    read: int = Field(default=0, title="Records read")
    written: int = Field(default=0, title="Records written")
    failed: int = Field(default=0, title="Records failed")
    skipped: int = Field(default=0, title="Records skipped")


class RunStepView(ResponseSchema):
    """
    One step of a run.

    ``node_type`` and ``node_label`` are the run's own copies, not looked up from the
    drawing — a log that changes when somebody edits the canvas is a log nobody can trust.

    ``rollup_count`` is what a collapsed row means: after five hundred passes for one node
    the engine stops inserting and updates a single row, and this is how many passes it
    stands for. Without it, a 50,000-record run's log would read as though it stopped at
    pass five hundred.
    """

    uuid: str = Field(title="Step")
    sequence: int = Field(default=0, title="Order")
    node_id: str = Field(title="Step id")
    node_type: str = Field(default="", title="Step type")
    node_label: str = Field(default="", title="Step name")
    batch_index: int = Field(default=0, title="Batch")
    attempt: int = Field(default=1, title="Attempt")
    status: str = Field(title="Status")
    records_in: int = Field(default=0, title="Records in")
    records_out: int = Field(default=0, title="Records out")
    is_rollup: bool = Field(default=False, title="Collapsed")
    rollup_count: int = Field(default=0, title="Passes")
    duration_ms: Optional[int] = Field(default=None, title="Took")
    message: Optional[str] = Field(default=None, title="Message")
    output_preview: Optional[Dict[str, Any]] = Field(default=None, title="Output")
    state_preview: Optional[Dict[str, Any]] = Field(default=None, title="State")

    #: The sha256 of the operation as it was when this step ran. Half of the determinism
    #: claim: a replay producing a different hash is detectably not the same run, which is
    #: the answer to "did this do what it did last Tuesday" when the operation is a row
    #: somebody could have edited since.
    operation_hash: Optional[str] = Field(default=None, title="Operation fingerprint")

    #: Only set when the request went to an allow-listed private address. Recorded because
    #: the question asked after an incident is which requests used the escape hatch and
    #: where they actually landed.
    egress_policy: Optional[str] = Field(default=None, title="Egress")
    resolved_ip: Optional[str] = Field(default=None, title="Resolved address")

    started_at: Optional[Any] = Field(default=None, title="Started")
    finished_at: Optional[Any] = Field(default=None, title="Finished")


class RunFrameView(ResponseSchema):
    """
    One run, as the dock reads it — over SSE and over the poll alike.

    No bigint ``id`` anywhere in it, enforced by ``extra="ignore"`` rather than by review.
    """

    uuid: str = Field(title="Run")
    flow_uuid: str = Field(title="Workflow")
    flow_name: str = Field(default="", title="Workflow name")
    status: str = Field(title="Status")
    mode: str = Field(title="Run mode")
    trigger_kind: str = Field(default="", title="Started by")
    attempt: int = Field(default=1, title="Attempt")

    #: Cancelling is a request, not an instruction: a node already waiting on somebody
    #: else's server finishes that call first. This is what lets the page say "stopping"
    #: rather than showing a Stop button that appears not to have worked.
    cancel_requested: bool = Field(default=False, title="Stopping")

    counts: RunCountsView = Field(default_factory=RunCountsView, title="Records")
    records_log_truncated: bool = Field(default=False, title="Log truncated")

    interrupt_payload: Optional[Dict[str, Any]] = Field(
        default=None, title="Waiting for"
    )
    result_preview: Optional[Dict[str, Any]] = Field(default=None, title="Result")
    error_message: Optional[str] = Field(default=None, title="Why it failed")

    steps: List[RunStepView] = Field(default_factory=list, title="Recent steps")
    steps_total: int = Field(default=0, title="Steps")

    scheduled_for: Optional[Any] = Field(default=None, title="Scheduled for")
    started_at: Optional[Any] = Field(default=None, title="Started")
    finished_at: Optional[Any] = Field(default=None, title="Finished")


class RunRecordView(ResponseSchema):
    """
    One record the run could not move, as the dead-letter page reads it.

    ``record`` is the **whole** payload for a failure, which is what makes Replay possible
    — a truncated copy would be a button that resubmits something subtly different from
    what failed. It has been through ``redact`` before it was stored, so a webhook body or
    an API response that carried a bearer token does not sit in this table.

    It is called ``record`` here and ``payload`` on the row it is read from, and the
    ``validation_alias`` is what bridges the two. Not cosmetic: ``payload`` is a *method*
    on every schema in this application — the one that renders a model as JSON — and a
    field of that name would shadow it, so ``RunRecordView(...).payload()`` would raise
    ``TypeError`` in whichever handler reached for it first.
    """

    uuid: str = Field(title="Record")
    node_id: str = Field(title="Step id")
    batch_index: int = Field(default=0, title="Batch")
    outcome: str = Field(title="Outcome")
    source_key: Optional[str] = Field(default=None, title="Source id")
    target_key: Optional[str] = Field(default=None, title="Destination id")
    message: Optional[str] = Field(default=None, title="Why")
    record: Optional[Dict[str, Any]] = Field(
        default=None, title="The record", validation_alias="payload"
    )
    retryable: bool = Field(default=False, title="Worth retrying")
    created_at: Optional[Any] = Field(default=None, title="When")
