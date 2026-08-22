"""
app/schemas/graph_designer/graph_designer_schemas.py

Pydantic schemas for the Graph Designer — the graph library, the canvas's save
payload, a run request, a human's answer, and the run/step views the log dock reads.

The canvas is entirely client-rendered, so these endpoints exchange JSON rather than
HTML partials and the request schemas are mostly JSON-bodied. That puts this module in
the same position ``flow_builder``'s schemas are in, and it makes the same call for the
same reason: **the node vocabulary is not pinned here.** ``graph_service._validate_graph``
owns it, because that is the version that has to be right — the compiler and the runners
read the graph through it — and declaring the node types in two places would mean two
edits every time one is added.

So ``GraphSaveRequest`` checks only what can be decided without knowing the vocabulary:
the body is a JSON *object*, and it carries node and edge collections.

**There is no node or edge count ceiling, and that is deliberate.** A graph is allowed
to be as large as the problem it describes; ``MAX_GRAPH_NODES`` exists in the flow
schemas because a conversation flow that large is a runaway client, and the same is not
true of a data pipeline. What bounds a run is the per-loop iteration ceiling in
``app/services/graph_designer/graph_compiler.py``, which is a bound on *work* rather
than on *drawing* — and a bound on work is the one that actually protects anything.
"""

import uuid as uuid_pkg
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import Field, field_validator

from app.models.graph_designer import (
    HUMAN_EXPECTS_VALUES,
    RUN_SCOPES,
    SCOPE_FULL,
    SCOPE_SELECTION,
)
from app.schemas.base import (
    MAX_DESCRIPTION_LENGTH,
    MAX_NAME_LENGTH,
    CheckboxBool,
    FormRequest,
    JsonRequest,
    OptionalText,
    OptionalUUID,
    QueryRequest,
    RequiredText,
    ResponseSchema,
)

#: How long a node id may be. Matches ``ToolGraphRunStep.node_id``'s column width, so a
#: selection the designer accepts is one the log can record. Ids are minted by the
#: browser (``GraphCanvas.genId``) and are short by construction; a longer one is a
#: caller that is not the canvas.
MAX_NODE_ID_LENGTH = 64

#: How many nodes one run may be asked to cover. This is not a cap on the graph — see
#: the module docstring — it bounds one *request*, so a hand-built payload cannot ask
#: the server to plan a selection larger than any graph it could name.
MAX_SELECTED_NODES = 5000

#: A free-text answer to a ``human`` node. Long enough for a pasted reason, short
#: enough that it cannot be used to write an unbounded value into graph state.
MAX_HUMAN_ANSWER_LENGTH = 10_000


class GraphCreateRequest(FormRequest):
    """The new-graph form."""

    name: RequiredText = Field(title="Graph name", max_length=MAX_NAME_LENGTH)
    description: OptionalText = Field(
        default=None, title="Description", max_length=MAX_DESCRIPTION_LENGTH,
    )


class GraphRenameRequest(GraphCreateRequest):
    """
    The rename form — the same two fields.

    The description is editable here rather than only on the canvas because it is what
    a model reads when the graph is attached to an agent, so it is a property of the
    graph rather than of the drawing.
    """


class GraphUpdateRequest(GraphRenameRequest):
    """
    The library's edit form — the two text fields plus who may call the graph.

    One form carrying both attachment fields, unlike :class:`GraphAttachRequest` and
    :class:`GraphShareRequest`, which are deliberately separate. The reason those two
    are split is that a request carrying both would have one silently discarded; here
    both arrive together and ``graph_service.update_graph`` **refuses** the pair rather
    than choosing, so nothing is dropped and the split's purpose is kept.

    Both blank means "callable by nobody", which is a graph's ordinary state.
    """

    data_agent_id: OptionalUUID = Field(default=None, title="Data agent")
    workspace_id: OptionalUUID = Field(default=None, title="Workspace")
    #: A ``CheckboxBool`` and not a plain bool, because an unticked checkbox submits
    #: nothing at all — a required bool would make "off" a 422.
    allow_recursive_aggregate: CheckboxBool = Field(
        default=False, title="Allow its whole result to be read",
    )


class GraphSetActiveRequest(FormRequest):
    """The publish / unpublish toggle. Attaching to an agent is a separate request."""

    is_active: CheckboxBool = Field(default=False, title="Published")


class GraphAttachRequest(FormRequest):
    """
    Which data agent may call this graph as a tool.

    Blank means "detach". A graph attached to nothing is the ordinary state and is
    still fully usable from the designer, so this is optional rather than required.
    """

    data_agent_id: OptionalUUID = Field(default=None, title="Data agent")


class GraphShareRequest(FormRequest):
    """
    Which workspace this graph is shared with — every data agent in it may call it.

    A separate request from :class:`GraphAttachRequest` rather than one field of it, for
    the reason the two are mutually exclusive at all: they are two different answers to
    "who may call this", and one form carrying both would let somebody submit both and
    have one silently discarded. Blank means "stop sharing".
    """

    workspace_id: OptionalUUID = Field(default=None, title="Workspace")


class GraphSaveRequest(JsonRequest):
    """
    The canvas's save payload.

    ``nodes`` and ``edges`` are the two keys the canvas always sends; anything else it
    carries (viewport, zoom, the dock's height) is kept, because ``extra="allow"`` —
    unlike the rest of this layer — is the right policy for a document whose shape the
    client owns.
    """

    model_config = {"extra": "allow"}

    invalid_body_message = "That graph could not be read. Please try saving again."

    nodes: List = Field(default_factory=list, title="Nodes")
    edges: List = Field(default_factory=list, title="Edges")

    def graph_data(self) -> dict[str, Any]:
        """
        The whole posted document, extras included.

        The service stores and interprets the full graph, so this returns everything
        rather than only the declared fields — this schema's job is to guarantee it is
        an object with two collections, not to narrow it.
        """
        return self.model_dump()


class GraphRunRequest(JsonRequest):
    """
    Start a run: the whole graph, or a tested selection of it.

    ``node_ids`` is meaningful only when ``scope`` is ``selection``. It is validated
    here for shape — non-empty strings of a sane length — and for *membership* in the
    service, which is the only place that knows what nodes the graph actually has.
    """

    invalid_body_message = "That run request could not be read. Please try again."

    scope: str = Field(default=SCOPE_FULL, title="Scope")
    node_ids: List[str] = Field(
        default_factory=list, title="Selected nodes", max_length=MAX_SELECTED_NODES,
    )
    inputs: dict = Field(default_factory=dict, title="Inputs")

    @field_validator("scope")
    @classmethod
    def validate_scope(cls, value: str) -> str:
        """One of the two known scopes, refused rather than defaulted."""
        if value not in RUN_SCOPES:
            raise ValueError(
                "Scope must be either the whole graph or a selection of nodes.",
            )
        return value

    @field_validator("node_ids")
    @classmethod
    def validate_node_ids(cls, value: List[str]) -> List[str]:
        """
        Trimmed, non-empty, de-duplicated, and short enough to be a canvas id.

        De-duplicated because selecting a node twice is a click, not an instruction to
        run it twice — and a duplicate would otherwise produce two step rows for one
        pass, which is exactly the confusion ``iteration`` exists to avoid.
        """
        seen: List[str] = []

        for raw in value:
            node_id = str(raw or "").strip()

            if not node_id:
                raise ValueError("A selected node has no id, so it cannot be run.")

            if len(node_id) > MAX_NODE_ID_LENGTH:
                raise ValueError("A selected node's id is not one this canvas produced.")

            if node_id not in seen:
                seen.append(node_id)

        return seen

    def selection(self) -> Optional[List[str]]:
        """
        The node ids this run covers, or ``None`` for the whole graph.

        ``None`` rather than an empty list, because the two mean different things on
        the run row: NULL is "everything", and a list is "exactly these".
        """
        return list(self.node_ids) if self.scope == SCOPE_SELECTION else None


class GraphResumeRequest(JsonRequest):
    """
    A human's answer to a paused run.

    The answer is deliberately untyped beyond its length: what a valid answer *is*
    depends on the ``human`` node's ``expects``, which the service reads off the
    graph. Validating a choice against its options here would need the graph.
    """

    invalid_body_message = "That answer could not be read. Please try again."

    answer: str = Field(
        default="", title="Answer", max_length=MAX_HUMAN_ANSWER_LENGTH,
    )


class GraphRunQuery(QueryRequest):
    """
    The run a page or a stream was opened for.

    Used by the canvas so ``/graph-designer/{id}/edit?run=<uuid>`` reopens on a run
    somebody is watching — the same reason ``ToolGraphQuery`` keeps a selection in the
    address bar.
    """

    run: OptionalUUID = Field(default=None, title="Run")


class GraphView(ResponseSchema):
    """
    One row of the graph library.

    ``agent_name`` is the data agent this graph is attached to and ``workspace_name`` is
    the workspace it is shared with. **At most one of the two is ever set** — they are
    mutually exclusive, see ``graph_service`` — so together they read as one answer to
    "who may call this" rather than two that have to be reconciled. Names rather than
    counts, for the same reason.
    """

    uuid: str = Field(title="Graph")
    name: str = Field(title="Name")
    description: Optional[str] = Field(default=None, title="Description")
    is_active: bool = Field(default=False, title="Published")
    node_count: int = Field(default=0, title="Nodes")
    edge_count: int = Field(default=0, title="Connections")
    agent_id: Optional[str] = Field(default=None, title="Data agent")
    agent_name: Optional[str] = Field(default=None, title="Attached agent")
    workspace_id: Optional[str] = Field(default=None, title="Workspace")
    workspace_name: Optional[str] = Field(default=None, title="Shared with")
    allow_recursive_aggregate: bool = Field(
        default=False, title="Whole result readable",
    )
    updated_at: Optional[datetime] = Field(default=None, title="Last updated")


class GraphRunStepView(ResponseSchema):
    """
    One node, one pass — a row of the dock's Output tab.

    Every field here is already capped by the service before the row was written, so
    this schema does not trim anything: it declares the contract the dock reads, and a
    preview that arrived too large would be a defect upstream, not something to hide
    at render time.
    """

    uuid: str = Field(title="Step")
    sequence: int = Field(default=0, title="#")
    node_id: str = Field(title="Node")
    node_type: str = Field(default="", title="Type")
    node_label: str = Field(default="", title="Label")
    iteration: int = Field(default=0, title="Pass")
    status: str = Field(default="", title="Status")
    duration_ms: Optional[int] = Field(default=None, title="Duration")
    message: Optional[str] = Field(default=None, title="Message")
    output_preview: Optional[dict] = Field(default=None, title="Output")
    state_preview: Optional[dict] = Field(default=None, title="State")
    started_at: Optional[datetime] = Field(default=None, title="Started")
    finished_at: Optional[datetime] = Field(default=None, title="Finished")


class GraphRunView(ResponseSchema):
    """
    One run and every step it has taken so far.

    This is both the SSE frame and the polling response, and that is on purpose: a
    client that lost its stream and fell back to polling must not have to understand a
    second shape. **Every frame is a whole state, not a delta**, for the reason
    ``progress.py`` gives — a consumer that missed one frame is not left with a wrong
    total.
    """

    uuid: str = Field(title="Run")
    graph_uuid: str = Field(default="", title="Graph")
    status: str = Field(default="", title="Status")
    scope: str = Field(default="", title="Scope")
    selected_nodes: List[str] = Field(default_factory=list, title="Selected nodes")
    interrupt_payload: Optional[dict] = Field(default=None, title="Awaiting")
    result_preview: Optional[dict] = Field(default=None, title="Result")
    error_message: Optional[str] = Field(default=None, title="Error")
    steps: List[GraphRunStepView] = Field(default_factory=list, title="Steps")
    started_at: Optional[datetime] = Field(default=None, title="Started")
    finished_at: Optional[datetime] = Field(default=None, title="Finished")

    @field_validator("selected_nodes", mode="before")
    @classmethod
    def default_selected_nodes(cls, value: object) -> object:
        """
        ``NULL`` on the row means "the whole graph", which is an empty list here.

        The column is nullable because NULL and ``[]`` mean different things in the
        database; the dock only needs to know which nodes to highlight, and for a full
        run that is none of them in particular.
        """
        return [] if value is None else value


class GraphNodeOption(ResponseSchema):
    """
    One thing a node's properties panel can point at.

    Used for both datasources and tool configs, because the panel needs the same three
    facts about each: what to submit, what to show, and whether picking it is going to
    work. ``disabled_reason`` is filled for a datasource that is inactive or a tool
    config that is switched off — offered but flagged, rather than hidden, so an
    operator can see *why* the thing they are looking for is not selectable.
    """

    uuid: str = Field(title="Option")
    label: str = Field(title="Name")
    detail: str = Field(default="", title="Detail")
    disabled_reason: str = Field(default="", title="Unavailable because")


class GraphEmailTemplateOption(GraphNodeOption):
    """
    A template option, carrying what it declares.

    ``variables`` rides along so the property panel can draw one binding row per declared
    variable the instant a template is chosen. A second round trip would make the panel
    flicker, or — worse — let somebody save the node before its bindings had loaded. See
    ``template_service.choices``, which is where the shape comes from.
    """

    variables: List[Dict[str, Any]] = Field(
        default_factory=list, title="Declared variables",
    )


class GraphNodeOptionsResponse(ResponseSchema):
    """
    Everything the properties panel needs to fill its pickers, in one request.

    One request rather than three: the panel opens on a node the user just clicked, and
    three round trips would render it in three stages. ``error`` carries a readable
    sentence instead of a status code, the contract
    ``ChildToolOptionsResponse`` established — a picker that cannot be filled should
    put one line next to itself, not replace the canvas the user is working in.

    **Every list the service builds must be declared here.** ``ResponseSchema`` is
    ``extra="ignore"``, so a key the service returns and this class does not name is
    dropped from the JSON without a word. That is exactly what happened to
    ``email_templates`` and ``smtp_configs``: the service built them, the response threw
    them away, and an Email node's Template picker was empty in every browser with
    nothing anywhere saying why.
    """

    datasources: List[GraphNodeOption] = Field(
        default_factory=list, title="Datasources",
    )
    tool_configs: List[GraphNodeOption] = Field(
        default_factory=list, title="Tool configs",
    )
    data_agents: List[GraphNodeOption] = Field(
        default_factory=list, title="Data agents",
    )
    email_templates: List[GraphEmailTemplateOption] = Field(
        default_factory=list, title="Email templates",
    )
    smtp_configs: List[GraphNodeOption] = Field(
        default_factory=list, title="SMTP servers",
    )
    human_expects: List[str] = Field(
        default_factory=lambda: sorted(HUMAN_EXPECTS_VALUES), title="Answer types",
    )
    error: Optional[str] = Field(default=None, title="Error")

    @classmethod
    def failure(cls, message: str) -> "GraphNodeOptionsResponse":
        """Empty pickers and the reason, as a 200. See the class docstring."""
        return cls(error=message)


class GraphSaveResponse(ResponseSchema):
    """
    The verdict on one save.

    ``saved`` is what the canvas keys its dirty flag off — the same
    ``data-success`` contract ``flow_builder.js`` already reads, kept identical so the
    shared canvas core does not need two conventions.
    """

    saved: bool = Field(default=False, title="Saved")
    message: str = Field(default="", title="Message")
    node_count: int = Field(default=0, title="Nodes")
    edge_count: int = Field(default=0, title="Connections")

    @classmethod
    def failure(cls, message: str) -> "GraphSaveResponse":
        """A save that was refused, with the reason to show under the toolbar."""
        return cls(saved=False, message=message)


class GraphRunStartedResponse(ResponseSchema):
    """
    The handle on a run that has just been started.

    Only the uuid and where to watch it. The run's *state* is not returned here even
    though it exists by the time this is built: a caller that read it would have one
    frame from before the first node finished, and would then have to reconcile it with
    the stream. One source for run state, and it is the stream.
    """

    run: str = Field(title="Run")
    events_url: str = Field(default="", title="Events URL")
    status_url: str = Field(default="", title="Status URL")

    @classmethod
    def for_run(cls, run_uuid: uuid_pkg.UUID | str) -> "GraphRunStartedResponse":
        """
        Build the response, including the two URLs the dock will use.

        The URLs are **relative paths**, never absolute. Every URL this application
        hands to a browser is a path (see the note in
        ``documentations/DOWNLOADER_AGENTS.md`` about ``API_BASE + url``), and a
        server-side absolute URL is the thing that goes stale when a tunnel rotates.
        """
        identifier = str(run_uuid)
        return cls(
            run=identifier,
            events_url=f"/graph-designer/runs/{identifier}/events",
            status_url=f"/graph-designer/runs/{identifier}",
        )
