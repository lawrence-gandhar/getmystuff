"""
app/schemas/integrations/workflow_draft_schemas.py

What a language model is allowed to say when it drafts a workflow.

**A model's structured output is an untrusted request.** These subclass ``RequestSchema``,
the same class hierarchy as a browser form post, exactly as ``AggregationPlan`` does — and
for the same reason: the model is a client, its output arrives over a network, and nothing
about it having been generated makes it safer than something somebody typed.

**The model never writes node ids, edge ids or positions.** It writes ``ref`` handles —
short names of its own choosing — and ``validate_draft`` assigns the real ids and lays the
drawing out. Two reasons, and the first is the serious one: a model-chosen id that collides
with another silently rewires the graph, joining two steps that were never meant to meet.
The second is budget — a small local model asked to emit coordinates spends its tokens on
arithmetic and returns malformed output.

**Nothing here can switch a workflow on.** There is no ``is_active`` field, no schedule and
no trigger interval anywhere in this file. A field that cannot be set cannot be set wrongly,
and the alternative is a generated workflow that starts writing into somebody's CRM before
a person has read it.

``unsupported`` + ``reason`` is the decline mechanism, lifted from ``AggregationPlan``.
A model asked to sync Stripe to Xero with neither connector present, and with no way to say
so, will emit a correctly-*shaped* step pointed at a URL it invented. Given the words, it
says it cannot.
"""

from typing import List, Optional

from pydantic import Field, field_validator

from app.schemas.base import (
    MAX_DESCRIPTION_LENGTH,
    MAX_NAME_LENGTH,
    RequestSchema,
)

#: How many steps one drafted workflow may hold. Twelve is well past anything the
#: generation prompt describes and well short of a drawing nobody can read — and a draft
#: that long is one a person will not check, which defeats the point of it being a draft.
MAX_DRAFT_STEPS = 12

#: How many field mappings one step may carry. Past this the mapping panel is unusable and
#: the model is guessing.
MAX_DRAFT_MAPPINGS = 20

#: How many assumptions the model may report. Five, because the list is there to be read
#: before pressing Save, and a list of thirty is one nobody reads.
MAX_DRAFT_ASSUMPTIONS = 5

#: How long a ``ref`` may be. These are the model's own handles and never reach the
#: database — ``validate_draft`` replaces every one with a generated node id.
MAX_REF_LENGTH = 64


class DraftMapping(RequestSchema):
    """
    One field the model proposes to carry from a record into a destination.

    ``target`` is the hallucination that matters most, and it is why this is resolved
    against the operation's *real* input list rather than trusted: a model writing
    ``customer_email`` where the operation takes ``email`` produces a workflow that runs
    green and silently drops the address. Nothing about the run says anything is wrong.
    """

    source: str = Field(default="", title="From the record", max_length=255)
    target: str = Field(title="To the destination", max_length=255)
    const: Optional[str] = Field(default=None, title="Fixed value", max_length=1000)
    transform: str = Field(default="", title="Transform", max_length=64)

    @field_validator("source", "target", "transform")
    @classmethod
    def _trim(cls, value: str) -> str:
        return str(value or "").strip()


class DraftStep(RequestSchema):
    """
    One step, as the model describes it.

    ``connection`` is the model's **spelling** of a connection's name — "Shopify EU" — and
    is deliberately not a uuid: a model asked for an identifier invents one, whereas a model
    asked for a name either gets it right or gets it recognisably wrong. ``validate_draft``
    resolves it against the user's real rows and *replaces* it with the real uuid, so what
    is saved never contains a name the model chose.

    ``source_ref`` names an earlier step whose records this one reads. Resolved against what
    exists *earlier in order*, so a forward reference is refused rather than silently
    producing a step that reads nothing.
    """

    ref: str = Field(title="Step handle", max_length=MAX_REF_LENGTH)
    type: str = Field(title="Step type", max_length=32)
    label: str = Field(default="", title="Step name", max_length=MAX_NAME_LENGTH)

    connection: str = Field(default="", title="Connection", max_length=MAX_NAME_LENGTH)
    operation: str = Field(default="", title="Operation", max_length=64)
    source_ref: str = Field(default="", title="Reads from", max_length=MAX_REF_LENGTH)

    mappings: List[DraftMapping] = Field(
        default_factory=list, title="Field mappings", max_length=MAX_DRAFT_MAPPINGS
    )
    batch_size: Optional[int] = Field(default=None, title="Records per batch")
    conditions: List[str] = Field(
        default_factory=list, title="Conditions", max_length=MAX_DRAFT_MAPPINGS
    )

    @field_validator("ref", "type", "label", "connection", "operation", "source_ref")
    @classmethod
    def _trim(cls, value: str) -> str:
        return str(value or "").strip()

    @field_validator("ref")
    @classmethod
    def _ref_present(cls, value: str) -> str:
        if not value:
            raise ValueError("Every step needs a handle so later steps can refer to it")
        return value


class WorkflowDraft(RequestSchema):
    """
    A whole workflow, as a model proposes it — before anything about it is believed.

    Pydantic bounds the **shape**; ``workflow_author.validate_draft`` bounds the
    **meaning**, and the split is the same one ``aggregate_planner`` makes. Neither layer
    is sufficient: a draft whose steps all exist can still name a connection the user does
    not have, and a draft naming real connections can still have thirty steps.

    There is no ``edges`` field. The model describes steps **in order** and the wiring is
    computed — a model that drew its own edges could produce a batch whose body never
    returns, which is one batch of a hundred reported as a success, and a person looking at
    a plausible drawing would have no reason to suspect it.
    """

    name: str = Field(default="", title="Workflow name", max_length=MAX_NAME_LENGTH)
    description: str = Field(
        default="", title="Description", max_length=MAX_DESCRIPTION_LENGTH
    )

    steps: List[DraftStep] = Field(
        default_factory=list, title="Steps", max_length=MAX_DRAFT_STEPS
    )

    #: What the model had to decide for itself. Shown above the Save button, because a
    #: draft that guessed at a date field is one somebody should look at before publishing
    #: — and a model that had to guess and did not say so is indistinguishable from one
    #: that knew.
    assumptions: List[str] = Field(
        default_factory=list, title="Assumptions", max_length=MAX_DRAFT_ASSUMPTIONS
    )

    unsupported: bool = Field(default=False, title="Cannot be built")
    reason: str = Field(default="", title="Why not", max_length=MAX_DESCRIPTION_LENGTH)

    @field_validator("steps")
    @classmethod
    def _refs_are_unique(cls, value: List[DraftStep]) -> List[DraftStep]:
        """
        Two steps with the same handle is not a mistake a person makes and is one a model
        makes. Left alone, the second silently wins every ``source_ref`` pointed at either
        — so the drawing reads correctly and the records come from the wrong place.
        """
        refs = [step.ref for step in value]
        if len(refs) != len(set(refs)):
            raise ValueError("Two steps were given the same handle")
        return value
