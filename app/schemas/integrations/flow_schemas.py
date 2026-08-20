"""
app/schemas/integrations/flow_schemas.py

Pydantic schemas for workflows, their versions, their triggers and their runs.

Three rules shape this file, and the third is the one worth reading.

**Node types, ports, operators and transforms are not declared here.**
``engine/flow_rules`` owns that vocabulary — it is read by the validator, the palette
endpoint and the AI prompt renderer from one place, which is what makes "the palette can
never offer something the validator refuses" true. A schema restating the list would be a
fourth copy, and the first to fall behind.

**The graph is checked for shape here and for meaning in the service.** This layer
guarantees ``graph_data`` is a JSON object of bounded size; whether it is a *workflow* —
one trigger, no edge into it, every batch body returning, every write reachable — is
``flow_rules.validate_flow``, which raises ``FlowValidationError`` carrying the node at
fault. That exception is what lets the canvas highlight a step, and flattening it into a
schema error here would throw the ``node_id`` away.

**No response schema carries a bigint ``id``, and that is enforced rather than reviewed.**
``ResponseSchema`` is configured ``extra="ignore"``, so a view function that starts
emitting ``id`` has it *dropped at the boundary* instead of leaking it — the response
schema is the last gate, not a description of what the dict happens to contain today.
Every identifier in this file is the public ``uuid``.
"""

from typing import Any, ClassVar, Dict, List, Optional

from pydantic import Field, field_validator

from app.models.integrations import (
    MAX_BATCH_SIZE,
    MIN_BATCH_SIZE,
    MIN_INTERVAL_SECONDS,
    OVERLAP_POLICY_VALUES,
    OVERLAP_SKIP,
    RUN_MODE_LIVE,
    RUN_MODES,
    TRIGGER_KIND_VALUES,
    TRIGGER_MANUAL,
)
from app.schemas.base import (
    MAX_DESCRIPTION_LENGTH,
    MAX_NAME_LENGTH,
    CheckboxBool,
    FormRequest,
    JsonObjectField,
    JsonRequest,
    OptionalText,
    QueryRequest,
    RequiredText,
    ResponseSchema,
)

#: How many extra field names a workflow may add to the preview deny-list. Generous —
#: naming ``ssn`` and ``iban`` costs nothing — but bounded, because the list is applied to
#: every field of every previewed record and an unbounded one is a slow redaction on a
#: hot path.
MAX_REDACTED_FIELDS = 50

#: How long a workflow's node id may be. The column's own width, so a drawing whose ids
#: would not fit is refused with a sentence rather than truncated by the database.
MAX_NODE_ID_LENGTH = 64

#: How long a timezone name may be.
MAX_TIMEZONE_LENGTH = 64


def _one_of(value: Any, allowed: Any, label: str) -> str:
    """
    Membership in a vocabulary the models own.

    The service checks the same thing, and that is not duplication worth removing: the
    service is the guarantee for callers that never touch a route — the scheduler, the AI
    generator — and this is the readable message for the one that does.
    """
    text = str(value or "").strip()
    if text not in allowed:
        raise ValueError(
            f"{label} is not one of the allowed values: {', '.join(sorted(allowed))}"
        )
    return text


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


class FlowCreateRequest(FormRequest):
    """The New workflow form. Nothing here can switch a workflow on — see
    ``flow_service.create_flow`` for why ``is_active`` is not a field anywhere."""

    name: RequiredText = Field(title="Workflow name", max_length=MAX_NAME_LENGTH)
    description: OptionalText = Field(
        default=None, title="Description", max_length=MAX_DESCRIPTION_LENGTH
    )


class FlowSettingsRequest(FormRequest):
    """
    The settings form: what a workflow is called, how big its batches are, and which
    extra fields never appear in a preview.

    ``redacted_fields`` is a repeated key, so it is read with ``getall``. Read with a
    plain ``get`` it would silently become the first entry — a deny-list of one, which
    looks like it is working.
    """

    multi_fields: ClassVar[tuple] = ("redacted_fields",)

    name: RequiredText = Field(title="Workflow name", max_length=MAX_NAME_LENGTH)
    description: OptionalText = Field(
        default=None, title="Description", max_length=MAX_DESCRIPTION_LENGTH
    )
    default_batch_size: int = Field(
        default=MIN_BATCH_SIZE * 500,
        title="Batch size",
        ge=MIN_BATCH_SIZE,
        le=MAX_BATCH_SIZE,
    )
    redacted_fields: List[str] = Field(
        default_factory=list,
        title="Fields to hide from previews",
        max_length=MAX_REDACTED_FIELDS,
    )

    @field_validator("redacted_fields")
    @classmethod
    def _clean(cls, values: List[str]) -> List[str]:
        """Blank entries dropped rather than stored. An HTML list control posts an empty
        row for the one somebody is halfway through typing."""
        return [str(v).strip() for v in values if str(v or "").strip()]


class FlowGraphRequest(JsonRequest):
    """
    The canvas's Save.

    JSON rather than a form: a drawing is a nested document and round-tripping it through
    a hidden text input is a second encoding to get wrong. The bound here is *shape* —
    see the module docstring on where meaning is decided.
    """

    invalid_body_message: ClassVar[str] = (
        "This workflow could not be read. Reload the page and try saving again."
    )

    graph_data: JsonObjectField = Field(
        default_factory=dict, title="Workflow drawing"
    )


class TriggerRequest(FormRequest):
    """
    The schedule panel.

    ``interval_seconds`` is optional at this layer and required by the service *for a
    schedule*, because that is a rule about the combination of two fields and the service
    is where the combination is already being decided. The floor is stated here as well as
    there, so somebody typing 30 hears about it before the round trip.
    """

    node_id: RequiredText = Field(title="Trigger step", max_length=MAX_NODE_ID_LENGTH)
    kind: str = Field(default=TRIGGER_MANUAL, title="Trigger type")
    is_enabled: CheckboxBool = Field(default=False, title="Enabled")
    interval_seconds: Optional[int] = Field(
        default=None, title="Run every", ge=MIN_INTERVAL_SECONDS
    )
    timezone_name: str = Field(
        default="UTC", title="Time zone", max_length=MAX_TIMEZONE_LENGTH
    )
    overlap_policy: str = Field(
        default=OVERLAP_SKIP, title="If the last run is still going"
    )

    @field_validator("kind")
    @classmethod
    def _kind(cls, value: str) -> str:
        return _one_of(value, TRIGGER_KIND_VALUES, "Trigger type")

    @field_validator("overlap_policy")
    @classmethod
    def _overlap(cls, value: str) -> str:
        return _one_of(
            value, OVERLAP_POLICY_VALUES, "If the last run is still going"
        )


class RunStartRequest(FormRequest):
    """
    Run now, or dry-run now.

    Defaulting to ``live`` is deliberate even though it is the more consequential of the
    two: the button that posts this says Run, and a Run button that quietly did nothing to
    anybody's data would be worse — a person would press it, see a green run, and believe
    the sync happened. Dry run is its own button and says so.
    """

    mode: str = Field(default=RUN_MODE_LIVE, title="Run mode")

    @field_validator("mode")
    @classmethod
    def _mode(cls, value: str) -> str:
        return _one_of(value, RUN_MODES, "Run mode")


class RunListQuery(QueryRequest):
    """How much of a workflow's history to show."""

    limit: int = Field(default=50, title="Runs to show", ge=1, le=200)


class StepPageQuery(QueryRequest):
    """
    The paginated step log behind the frame's hundred-row window.

    ``after`` is a sequence number rather than an offset, so a page that arrives while the
    run is still writing steps does not repeat or skip rows the way an offset into a
    growing table does.
    """

    after: int = Field(default=-1, title="After step", ge=-1)
    limit: int = Field(default=200, title="Steps per page", ge=1, le=500)


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------


class FlowView(ResponseSchema):
    """One workflow as the list page reads it."""

    uuid: str = Field(title="Workflow")
    name: str = Field(title="Name")
    description: str = Field(default="", title="Description")
    is_active: bool = Field(default=False, title="Switched on")
    is_published: bool = Field(default=False, title="Published")
    version_number: Optional[int] = Field(default=None, title="Published version")
    created_by_ai: bool = Field(default=False, title="Written by AI")
    node_count: int = Field(default=0, title="Steps")
    created_at: Optional[Any] = Field(default=None, title="Created")
    updated_at: Optional[Any] = Field(default=None, title="Last edited")
    last_run_status: str = Field(default="", title="Last run")
    last_run_at: Optional[Any] = Field(default=None, title="Last run at")
    last_run_uuid: str = Field(default="", title="Last run id")


class FlowVersionView(ResponseSchema):
    """One published or archived snapshot.

    ``graph_data`` is deliberately absent. The history panel lists versions; the drawing
    behind one is fetched on demand, and shipping every version's full document with the
    list would send a megabyte to render ten rows of dates."""

    uuid: str = Field(title="Version")
    version_number: int = Field(title="Version number")
    status: str = Field(title="Status")
    is_published: bool = Field(default=False, title="Currently published")
    graph_hash: str = Field(default="", title="Fingerprint")
    short_hash: str = Field(default="", title="Fingerprint (short)")
    published_at: Optional[Any] = Field(default=None, title="Published")


class TriggerView(ResponseSchema):
    """One trigger as the schedule panel reads it."""

    uuid: str = Field(title="Trigger")
    node_id: str = Field(title="Trigger step")
    kind: str = Field(title="Trigger type")
    is_enabled: bool = Field(default=False, title="Enabled")
    interval_seconds: Optional[int] = Field(default=None, title="Run every")
    timezone: str = Field(default="UTC", title="Time zone")
    overlap_policy: str = Field(default=OVERLAP_SKIP, title="Overlap policy")
    next_run_at: Optional[Any] = Field(default=None, title="Next run")
    last_fired_at: Optional[Any] = Field(default=None, title="Last fired")
