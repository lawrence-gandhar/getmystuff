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
)

#: A canvas this large is a runaway client, not a flow someone drew. Bounded so a
#: malformed save cannot put an unbounded document into the graph_data column.
MAX_GRAPH_NODES = 500
MAX_GRAPH_EDGES = 2000

#: A knowledge-base entry pasted in by hand. Long enough for a policy document,
#: short enough that the chunker's work stays bounded.
MAX_MANUAL_TEXT_LENGTH = 100_000


class FlowCreateRequest(FormRequest):
    """The new-flow form."""

    name: RequiredText = Field(title="Flow name", max_length=MAX_NAME_LENGTH)


class FlowRenameRequest(FlowCreateRequest):
    """The rename form — the same single field."""


class FlowSetActiveRequest(FormRequest):
    """The publish / unpublish toggle. Attachment to a chatbot is separate."""

    is_active: CheckboxBool = Field(default=False, title="Published")


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
    count.
    """

    uuid: str = Field(title="Flow")
    name: str = Field(title="Name")
    is_active: bool = Field(default=False, title="Published")
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
