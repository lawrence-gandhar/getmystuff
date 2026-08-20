"""
app/schemas/tool_graphs/tool_graph_schemas.py

Pydantic schemas for Tool Graphs — the selection that comes in, and the two
drawings that go back out.

The request side is one query schema for both endpoints. It has to be one: the
toggle above the canvas switches which view is drawn *without* changing what is
selected, so a chain graph and a join view that parsed their selection differently
could end up describing two different sets of tools under the same heading.

The response side exists because everything on it is user data — tool names, table
names, column names, all read out of the customer's own database — being handed to
a renderer. Building it through a schema is what guarantees the browser gets the
fields it expects and nothing else. In particular, **no internal id is ever on the
wire**: a node's ``key`` is a tool's public uuid, or the literal ``start``/``end``,
which is what lets the drawing be clicked back into a Tool Configs link.

Nothing here decides what is *true* about a graph. Which tools are in scope, which
edges are drawn and which layer each node sits on are all
``tool_graph_service``'s answers — this layer only guarantees the shape they travel
in.
"""

import uuid as uuid_pkg
from typing import List, Optional

from pydantic import Field

from app.schemas.base import (
    MAX_NAME_LENGTH,
    OptionalUUID,
    QueryRequest,
    ResponseSchema,
)


class ToolGraphQuery(QueryRequest):
    """
    ``?workspace=&agent=&tool=`` — one node of the side tree.

    All three are optional and all three may arrive together, because the page keeps
    the branch above a selection expanded and a deep link may carry the whole path.
    The service takes the most specific one, so clicking a tool means that tool
    whatever else is open.

    No selection at all is not an error: it is the state the page opens in, and it
    draws the empty canvas.
    """

    workspace: OptionalUUID = Field(default=None, title="Workspace")
    agent: OptionalUUID = Field(default=None, title="Data agent")
    tool: OptionalUUID = Field(default=None, title="Tool config")


# --------------------------------------------------------------------------
# The chain graph
# --------------------------------------------------------------------------

class ToolGraphNode(ResponseSchema):
    """
    One box on the canvas.

    ``key`` is a tool's public uuid, or ``start``/``end`` for the two ends of the
    run — those are drawn as nodes because that is what they are in the compiled
    LangGraph, not as decoration.

    ``layer`` and ``row`` are the position, computed on the server. They are plain
    integers rather than pixels so the renderer stays free to change spacing, and so
    the layout itself can be asserted in a test — see the module docstring in
    ``tool_graph_service``.

    ``is_enabled`` is carried because a disabled tool is drawn, not hidden: it is the
    single most likely reason a chain someone is looking at returns nothing.
    """

    key: str = Field(title="Node")
    kind: str = Field(title="Kind")
    label: str = Field(title="Label", max_length=MAX_NAME_LENGTH)
    datasource: str = Field(default="", title="Datasource")
    query_mode: str = Field(default="", title="Query mode")
    is_enabled: bool = Field(default=True, title="Enabled")
    agent_name: str = Field(default="", title="Data agent")
    layer: int = Field(default=0, title="Layer")
    row: int = Field(default=0, title="Row")


class ToolGraphEdge(ResponseSchema):
    """
    One connector, from the tool that runs first to the tool it restricts.

    ``label`` is what crosses the edge — ``child_column → parent_reference`` — and is
    empty on the ``START`` and ``END`` connectors, which carry nothing.
    """

    source: str = Field(title="From")
    target: str = Field(title="To")
    kind: str = Field(default="value", title="Kind")
    label: str = Field(default="", title="Label")


class ToolGraphResponse(ResponseSchema):
    """
    The JSON body of ``GET /tool-graphs/graph``.

    ``error`` rather than a status code, and a 200 with it set: the canvas sits
    beside a tree the user is clicking through, and a stale bookmark or a tool
    deleted in another tab should put a sentence next to the canvas rather than
    replace the whole page with an error. Same reason
    ``ChildToolOptionsResponse`` answers that way.
    """

    scope_label: str = Field(default="", title="Selection")
    nodes: List[ToolGraphNode] = Field(default_factory=list, title="Nodes")
    edges: List[ToolGraphEdge] = Field(default_factory=list, title="Edges")
    error: Optional[str] = Field(default=None, title="Error")

    @classmethod
    def failure(cls, message: str) -> "ToolGraphResponse":
        """An empty canvas plus the reason it is empty."""
        return cls(scope_label="", nodes=[], edges=[], error=message)


# --------------------------------------------------------------------------
# The join sets
# --------------------------------------------------------------------------

class JoinView(ResponseSchema):
    """
    One join, as the two sets it intersects.

    ``type`` drives which region of the diagram is shaded; ``type_label`` is the SQL
    keyword it stands for, so the caption reads as the clause the query runs.
    """

    type: str = Field(title="Join type")
    type_label: str = Field(title="Join")
    left_table: str = Field(title="Left table")
    left_column: str = Field(title="Left column")
    table: str = Field(title="Joined table")
    right_column: str = Field(title="Joined column")


class ToolJoinsView(ResponseSchema):
    """
    One tool's joins, or the reason it has none to draw.

    ``note`` is filled in exactly when ``joins`` is empty, and the two cases are not
    the same: a builder query over one table has nothing to intersect, while a SQL
    tool has a statement this application does not parse. Saying which is the point
    — a blank card would imply the query has no joins, which for a SQL tool would be
    a guess presented as a fact.
    """

    tool_uuid: uuid_pkg.UUID = Field(title="Tool")
    tool_name: str = Field(title="Tool name", max_length=MAX_NAME_LENGTH)
    query_mode: str = Field(title="Query mode")
    base_table: str = Field(default="", title="Base table")
    tables: List[str] = Field(default_factory=list, title="Tables")
    joins: List[JoinView] = Field(default_factory=list, title="Joins")
    note: str = Field(default="", title="Note")


class ToolJoinsResponse(ResponseSchema):
    """The JSON body of ``GET /tool-graphs/joins`` — the same selection, as sets."""

    scope_label: str = Field(default="", title="Selection")
    tools: List[ToolJoinsView] = Field(default_factory=list, title="Tools")
    error: Optional[str] = Field(default=None, title="Error")

    @classmethod
    def failure(cls, message: str) -> "ToolJoinsResponse":
        """No diagrams plus the reason there are none."""
        return cls(scope_label="", tools=[], error=message)
