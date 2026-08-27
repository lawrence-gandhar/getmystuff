"""
app/schemas/flow_builder/flow_schemas.py

Pydantic schemas for Flow Builder — the flow library, the canvas's graph payload,
and one AI Fallback node's knowledge base.

Flow Builder is the one module whose canvas is entirely client-rendered, so its
endpoints exchange JSON rather than HTML partials. That makes the request schemas
here JSON-bodied, and it makes the graph payload the loosest shape in the
application: the node and edge vocabulary is owned by ``flow_builder.js`` and by
``flow_service.update_flow_graph``, which validates the structure it understands.

So ``FlowGraphSaveRequest`` checks the two things that can be decided without
knowing that vocabulary — the body is a JSON *object*, and it carries node and edge
collections of a sane size — and hands the rest to the service. Pinning the node
types here would mean two places to change every time a node type is added, and
the service's version is the one that has to be right.
"""

from datetime import datetime
from typing import Any, Optional

from pydantic import Field, field_validator

from app.schemas.base import (
    MAX_NAME_LENGTH,
    CheckboxBool,
    FormRequest,
    JsonRequest,
    RequiredText,
    ResponseSchema,
    validate_edge_waypoints,
)

#: A canvas this large is a runaway client, not a flow someone drew. Bounded so a
#: malformed save cannot put an unbounded document into the graph_data column.
MAX_GRAPH_NODES = 500
MAX_GRAPH_EDGES = 2000

#: A knowledge-base entry pasted in by hand. Long enough for a policy document,
#: short enough that the chunker's work stays bounded.
MAX_MANUAL_TEXT_LENGTH = 100_000

#: What a flow is *for*: an agent's own conversation, or a child flow another flow
#: runs. Mirrors ``flow_service.VALID_FLOW_KINDS`` — the service still enforces it,
#: since `create_flow` and `set_flow_kind` are reachable from tests and from any
#: future caller that never touches a route, and the column has a check constraint
#: of its own. This is the readable message for the one caller that *is* a form.
FLOW_KINDS: frozenset[str] = frozenset({"agent", "generic"})

#: The label both the create form and the toggle report a bad kind under, and the
#: title both render. One string so the two cannot say it differently.
_KIND_LABEL = "Flow kind"


def _one_of(value: Any, allowed: Any, label: str) -> str:
    """
    Membership in a vocabulary the service owns.

    Copied verbatim from ``integrations/flow_schemas.py`` rather than imported across
    two feature packages: it is four lines, and a schema module reaching into another
    module's schemas for a helper couples two features that have nothing else in
    common.
    """
    text = str(value or "").strip()
    if text not in allowed:
        raise ValueError(
            f"{label} is not one of the allowed values: {', '.join(sorted(allowed))}"
        )
    return text


class FlowCreateRequest(FormRequest):
    """
    The new-flow form.

    ``kind`` defaults to ``agent``, which is what every flow was before the field
    existed — so a form that does not send it creates what it always created.
    """

    name: RequiredText = Field(title="Flow name", max_length=MAX_NAME_LENGTH)
    kind: str = Field(default="agent", title=_KIND_LABEL)

    @field_validator("kind")
    @classmethod
    def _kind(cls, value: str) -> str:
        return _one_of(value or "agent", FLOW_KINDS, _KIND_LABEL)


class FlowRenameRequest(FormRequest):
    """
    The rename form — the name and nothing else.

    Deliberately no longer a subclass of ``FlowCreateRequest``: that now carries
    ``kind``, and inheriting it would let a rename quietly change what a flow is for.
    """

    name: RequiredText = Field(title="Flow name", max_length=MAX_NAME_LENGTH)


class FlowSetActiveRequest(FormRequest):
    """The publish / unpublish toggle. Attachment to a chatbot is separate."""

    is_active: CheckboxBool = Field(default=False, title="Published")


class FlowSetKindRequest(FormRequest):
    """
    The agent / generic toggle, beside ``FlowSetActiveRequest``.

    Required rather than defaulted, unlike the create form: a toggle that silently
    read "agent" from a malformed request would change what a flow is for without
    anybody asking it to.
    """

    kind: RequiredText = Field(title=_KIND_LABEL)

    @field_validator("kind")
    @classmethod
    def _kind(cls, value: str) -> str:
        return _one_of(value, FLOW_KINDS, _KIND_LABEL)


class FlowGraphSaveRequest(JsonRequest):
    """
    The canvas's save payload.

    ``nodes`` and ``edges`` are the two keys the canvas always sends;
    anything else it carries (viewport, zoom) is kept, because
    ``extra="allow"`` here — unlike everywhere else in this layer — is the
    correct policy for a document whose shape the client owns.
    """

    model_config = {"extra": "allow"}

    invalid_body_message = "Invalid graph data."

    nodes: list = Field(default_factory=list, title="Nodes", max_length=MAX_GRAPH_NODES)
    edges: list = Field(default_factory=list, title="Edges", max_length=MAX_GRAPH_EDGES)

    @field_validator("edges")
    @classmethod
    def _waypoints(cls, value: list) -> list:
        """
        The one key inside an edge this layer checks, and why it is the exception.

        The rest of the edge vocabulary is the service's, as the module docstring
        says. ``waypoints`` — where a connector was dragged to route it by hand — is
        bounded here because the failure it can cause is not a refusal but a 500:
        a non-finite coordinate passes every other rule, and PostgreSQL then refuses
        the ``jsonb`` it becomes. See ``app.schemas.base.validate_edge_waypoints``.
        """
        return validate_edge_waypoints(value)

    def graph_data(self) -> dict[str, Any]:
        """
        The whole posted document, extras included.

        The service stores and interprets the full graph, so this returns
        everything rather than only the declared fields — the schema's role here is
        to bound it, not to narrow it.
        """
        return self.model_dump()


class KnowledgeBaseManualTextRequest(JsonRequest):
    """
    A knowledge-base entry typed or pasted in rather than uploaded.

    Both fields were previously read with ``payload.get(field, "")``, which turned
    a body that parsed to a list into an ``AttributeError`` and a 500. The base
    class refuses a non-object body first, so the failure is a 400 with a sentence.
    """

    invalid_body_message = "Invalid request body"

    label: RequiredText = Field(title="Label", max_length=MAX_NAME_LENGTH)
    text: str = Field(title="Text", min_length=1, max_length=MAX_MANUAL_TEXT_LENGTH)


class FlowView(ResponseSchema):
    """
    One row of the flow library.

    ``chatbot_name`` is the agent this flow is attached to, or ``None`` — a flow
    can run on at most one, which is why the list shows the name rather than a
    count. For a ``generic`` flow it is always ``None`` and means something
    different: not "not attached yet" but "never attached", which is why the row
    template reads ``kind`` before it decides what to put in that column.
    """

    uuid: str = Field(title="Flow")
    name: str = Field(title="Name")
    is_active: bool = Field(default=False, title="Published")
    kind: str = Field(default="agent", title=_KIND_LABEL)
    updated_at: Optional[datetime] = Field(default=None, title="Last updated")
    chatbot_name: Optional[str] = Field(default=None, title="Attached chatbot")


class KnowledgeBaseDocumentView(ResponseSchema):
    """
    One document in a node's knowledge base.

    ``id`` carries the document's *public uuid*, not its bigint primary key — the
    key is named ``id`` because ``flow_builder.js`` reads it under that name. No
    internal identifier is exposed here.
    """

    id: str = Field(title="Document")
    label: str = Field(title="Label")
    source_type: str = Field(default="", title="Source")
    size_bytes: Optional[int] = Field(default=None, title="Size")
    extraction_status: str = Field(default="", title="Extraction status")
    error_message: Optional[str] = Field(default=None, title="Error")
    created_at: Optional[str] = Field(default=None, title="Added")


class KnowledgeBaseStateResponse(ResponseSchema):
    """
    The knowledge base's status plus its documents — the body of the state, upload
    and train endpoints, which all answer with the same thing.
    """

    status: str = Field(default="", title="Status")
    trained_at: Optional[str] = Field(default=None, title="Trained at")
    error_message: Optional[str] = Field(default=None, title="Error")
    documents: list[KnowledgeBaseDocumentView] = Field(
        default_factory=list, title="Documents"
    )

    @field_validator("trained_at", mode="before")
    @classmethod
    def stringify_timestamp(cls, v: object) -> object:
        """
        Accept a ``datetime`` as well as the ISO string the service sends today.

        This is a JSON response read by ``flow_builder.js``, so the timestamp is
        serialized here rather than depending on whichever encoder happens to run
        over it.
        """
        if v is None or isinstance(v, str):
            return v
        return v.isoformat() if hasattr(v, "isoformat") else str(v)
