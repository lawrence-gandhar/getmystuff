"""
app/schemas/canvas_layout/layout_schemas.py

The request and reply for "arrange this drawing", shared by both canvases.

One schema rather than a copy in each feature's schema module, because the two canvases are
not coupling to *each other* here — they are both coupling to the layout feature, which is
what a shared request shape is for. The near-identical ``FlowGraphSaveRequest`` and
``GraphSaveRequest`` are two copies for the opposite reason: each describes a *document its
own feature owns and stores*, and those two documents are genuinely different.

**What is posted is the canvas's live state, not what is stored.** An operator arranges a
drawing while it has unsaved changes, so the endpoint cannot read the row and lay that out
instead — it would return positions for a graph that is one edit behind. Which also means
this is the one place in the feature that reads a graph it will never write.
"""

from typing import Any, ClassVar, Dict, List

from pydantic import Field

from app.schemas.base import JsonRequest, ResponseSchema

#: The bound on a drawing this endpoint will arrange. Mirrors
#: ``flow_schemas.MAX_GRAPH_NODES`` / ``MAX_GRAPH_EDGES``, which is the published limit on
#: what a canvas may save — a drawing too large to store is not one worth arranging. Stated
#: here rather than imported so this feature does not depend on the Flow Builder's schema
#: module, which the Graph Designer has no other reason to load.
MAX_LAYOUT_NODES = 500
MAX_LAYOUT_EDGES = 2000

#: The block a reader starts from. Both canvases spell it the same way — Flow Builder's
#: Start block and the Graph Designer's Start node are each ``type: "start"`` — which is the
#: only node-type knowledge in this feature, and it is here rather than in the service so
#: the algorithm stays about ids and edges.
ENTRY_NODE_TYPE = "start"


class CanvasLayoutRequest(JsonRequest):
    """
    A canvas asking where its blocks should go.

    ``extra="allow"`` so a canvas can post the document it is already holding — nodes with
    their data, positions and ports included — instead of building a stripped copy. Nothing
    beyond ``nodes`` and ``edges`` is read.
    """

    model_config = {"extra": "allow"}

    invalid_body_message: ClassVar[str] = (
        "That canvas could not be read, so it has been left as it is."
    )

    nodes: List = Field(default_factory=list, title="Nodes", max_length=MAX_LAYOUT_NODES)
    edges: List = Field(default_factory=list, title="Edges", max_length=MAX_LAYOUT_EDGES)

    def entry_ids(self) -> List[str]:
        """
        The ids of the Start blocks, read off the posted nodes.

        Derived here rather than accepted as a field: a client that named its own entries
        could put the Start block anywhere it liked in the picture, and the drawing already
        says which block it is.
        """
        return [
            str(node.get("id") or "")
            for node in self.nodes
            if isinstance(node, dict)
            and str(node.get("type") or "") == ENTRY_NODE_TYPE
            and node.get("id")
        ]


class CanvasLayoutResponse(ResponseSchema):
    """
    Where every block goes, plus which connectors run backwards.

    ``positions`` is keyed by node id and carries a ``layer`` and a ``column``, not pixels.
    The browser multiplies those by its own gaps, because the vertical gap depends on how
    tall each rendered block turned out to be and only the browser can measure that — the
    same split ``static/js/tool_graphs.js`` describes for its own drawing.

    ``back_edges`` are **indices into the posted edge list**. An index rather than an id
    because an id is optional on an edge and a position in the array is not, and because
    the caller is looking at the very array it just sent.

    Its field names are ``layered_layout``'s own keys, so a route builds one with the
    inherited ``payload_for`` and there is no adapter to keep in step.
    """

    positions: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    back_edges: List[int] = Field(default_factory=list)
