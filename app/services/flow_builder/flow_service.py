"""
Business logic for the Flow Builder — creating, editing, publishing and
attaching saved conversation-flow graphs.

A flow belongs to a **user**, not to a chatbot: it is built standalone from the
Flow Builder page and then attached to at most one chatbot (see attach_flow).
Ownership is therefore checked directly against user_id, while attaching also
checks the chatbot key through chatbot_service.get_chatbot_key.

Two independent switches decide whether a flow drives a conversation:
``is_active`` (published vs. draft, set here) and the attachment itself. Both
must be in place — get_active_flow filters on both — so a live flow can be
parked without detaching it, and a draft can sit attached while it is finished.
"""

import uuid
from typing import List, Optional

from litestar.exceptions import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.db_utils import CRUDQueryBuilder
from app.db.flow_builder.queries import fetch_flows_with_chatbot_names
from app.models.flow_builder import ChatbotFlow
from app.services.chatbot import chatbot_service

flow_crud = CRUDQueryBuilder(ChatbotFlow)

_VALID_NODE_TYPES = {
    "start", "if_else", "goto", "menu", "dropdown",
    "ask_input", "send_message", "ai_fallback", "end",
    # Runs a published Graph Designer graph mid-conversation. The one node type whose
    # work happens outside this feature entirely — see `engine_service._step_run_graph`,
    # and note that a graph containing an "Ask a human" node makes this node end the turn
    # waiting for a reply, which no other non-prompt node does.
    "run_graph",
}
_VALID_OPERATORS = {"equals", "contains", "not_empty"}
_VALID_CONTEXT_SOURCES = {"datasource", "knowledge_base", "prompt"}
_VALID_LLM_MODES = {"in_built", "attached"}

_DEFAULT_GRAPH = {
    "nodes": [
        {"id": "start", "type": "start", "position": {"x": 60, "y": 60}, "data": {}},
    ],
    "edges": [],
}


# --------------------------------------------------------------------------
# Read
# --------------------------------------------------------------------------

async def get_user_flow_views(db: AsyncSession, user_id: int) -> List[dict]:
    """
    Every flow this user owns, shaped for the Flow Builder list: public uuid
    only, plus the name of the chatbot it is attached to (None when unattached).
    """
    rows = await fetch_flows_with_chatbot_names(db, user_id)
    return [
        {
            "uuid": str(flow.uuid),
            "name": flow.name,
            "is_active": flow.is_active,
            "updated_at": flow.updated_at,
            "chatbot_name": chatbot_name,
        }
        for flow, chatbot_name in rows
    ]


async def get_flow(db: AsyncSession, user_id: int, flow_id: uuid.UUID) -> ChatbotFlow:
    flow = await flow_crud.get_by_uuid(db, flow_id, extra_filters={"user_id": user_id})
    if not flow:
        raise HTTPException(status_code=404, detail="Flow not found")
    return flow


async def get_attachable_flows(db: AsyncSession, user_id: int) -> List[ChatbotFlow]:
    """
    Flows a chatbot could be given: active and not attached to anything yet.
    A flow already attached elsewhere is deliberately absent — it can only run on
    one chatbot, so it has to be detached there first.
    """
    return await flow_crud.get_many(
        db, filters={"user_id": user_id, "is_active": True, "chatbot_key_id": None}, order_by="name"
    )


async def get_attached_flow(db: AsyncSession, user_id: int, key_id: uuid.UUID) -> Optional[ChatbotFlow]:
    """The flow attached to one chatbot, active or not (the settings dropdown shows both)."""
    key = await chatbot_service.get_chatbot_key(db, user_id, key_id)  # ownership check
    return await flow_crud.get_one(db, filters={"chatbot_key_id": key.id})


async def get_active_flow(db: AsyncSession, chatbot_key_id: int) -> Optional[ChatbotFlow]:
    """
    Runtime-facing lookup — used by the public message handler, keyed on the
    internal id. Both switches are checked here: the flow must be attached to
    this chatbot *and* published.
    """
    return await flow_crud.get_one(db, filters={"chatbot_key_id": chatbot_key_id, "is_active": True})


# --------------------------------------------------------------------------
# Write
# --------------------------------------------------------------------------

async def create_flow(db: AsyncSession, user_id: int, name: str) -> ChatbotFlow:
    """Create a draft flow. Attaching it to a chatbot is a separate, later step."""
    name = (name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Flow name is required")

    return await flow_crud.create(db, {
        "user_id": user_id,
        "name": name,
        "graph_data": dict(_DEFAULT_GRAPH),
        "is_active": False,
    })


async def rename_flow(
    db: AsyncSession,
    user_id: int,
    flow_id: uuid.UUID,
    name: str,
) -> ChatbotFlow:
    flow = await get_flow(db, user_id, flow_id)

    name = (name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Flow name is required")

    return await flow_crud.update(db, flow.id, {"name": name})


async def update_flow_graph(
    db: AsyncSession,
    user_id: int,
    flow_id: uuid.UUID,
    graph_data: dict,
) -> ChatbotFlow:
    flow = await get_flow(db, user_id, flow_id)
    _validate_graph(graph_data)
    return await flow_crud.update(db, flow.id, {"graph_data": graph_data})


async def set_flow_active(
    db: AsyncSession,
    user_id: int,
    flow_id: uuid.UUID,
    is_active: bool,
) -> ChatbotFlow:
    """
    Publish or unpublish a flow.

    Unpublishing leaves any attachment in place — the chatbot simply stops
    running the flow, because get_active_flow requires both. Publishing does not
    attach anything either; that is attach_flow's job.
    """
    flow = await get_flow(db, user_id, flow_id)
    return await flow_crud.update(db, flow.id, {"is_active": is_active})


async def attach_flow(
    db: AsyncSession,
    user_id: int,
    key_id: uuid.UUID,
    flow_id: Optional[uuid.UUID],
) -> Optional[ChatbotFlow]:
    """
    Point one chatbot at one flow — the single write path for the dropdown on the
    chatbot's settings page. `flow_id=None` clears the chatbot's flow.

    Whatever the chatbot currently runs is detached first, because
    ``chatbot_flows.chatbot_key_id`` is unique: a chatbot has at most one flow
    and a flow has at most one chatbot. The detached flow stays in the library.
    """
    key = await chatbot_service.get_chatbot_key(db, user_id, key_id)  # ownership check

    new_flow: Optional[ChatbotFlow] = None
    if flow_id is not None:
        new_flow = await get_flow(db, user_id, flow_id)

        if new_flow.chatbot_key_id not in (None, key.id):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"The flow {new_flow.name} is already used by another chatbot. "
                    "Detach it there first, or pick a different flow."
                ),
            )
        if not new_flow.is_active:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"The flow {new_flow.name} is still a draft — mark it active in "
                    "Flow Builder before attaching it."
                ),
            )

    current = await flow_crud.get_one(db, filters={"chatbot_key_id": key.id})
    if current and (new_flow is None or current.id != new_flow.id):
        current.chatbot_key_id = None
        await db.flush()  # free the unique slot before the new flow claims it

    if new_flow is None:
        await db.commit()
        return None

    new_flow.chatbot_key_id = key.id
    await db.commit()
    await db.refresh(new_flow)
    return new_flow


async def delete_flow(db: AsyncSession, user_id: int, flow_id: uuid.UUID) -> None:
    flow = await get_flow(db, user_id, flow_id)  # ownership check
    await flow_crud.delete(db, flow.id)


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

def _validate_graph(graph_data: dict) -> None:
    if not isinstance(graph_data, dict):
        raise HTTPException(status_code=400, detail="Invalid flow graph")

    nodes = graph_data.get("nodes")
    edges = graph_data.get("edges")
    if not isinstance(nodes, list) or not isinstance(edges, list):
        raise HTTPException(status_code=400, detail="Flow graph must contain 'nodes' and 'edges' lists")
    if not nodes:
        raise HTTPException(status_code=400, detail="Flow must contain at least one node")

    node_ids = [n.get("id") for n in nodes]
    if len(node_ids) != len(set(node_ids)) or any(not nid for nid in node_ids):
        raise HTTPException(status_code=400, detail="Every node must have a unique, non-empty id")

    node_by_id = {n["id"]: n for n in nodes}
    start_nodes = [n for n in nodes if n.get("type") == "start"]
    if len(start_nodes) != 1:
        raise HTTPException(status_code=400, detail="Flow must contain exactly one Start node")

    for node in nodes:
        _validate_node(node, node_by_id)

    start_id = start_nodes[0]["id"]
    end_ids = {n["id"] for n in nodes if n.get("type") == "end"}
    _validate_edges(edges, node_by_id, start_id, end_ids)


def _validate_node(node: dict, node_by_id: dict) -> None:
    node_type = node.get("type")
    if node_type not in _VALID_NODE_TYPES:
        raise HTTPException(status_code=400, detail=f"Unknown node type: {node_type!r}")

    data = node.get("data") or {}
    if node_type == "if_else":
        if not data.get("variable_name"):
            raise HTTPException(status_code=400, detail="If/Else node is missing a variable name")
        if data.get("operator") not in _VALID_OPERATORS:
            raise HTTPException(status_code=400, detail="If/Else node has an invalid operator")
    elif node_type == "goto":
        target = data.get("target_node_id")
        if not target or target not in node_by_id:
            raise HTTPException(status_code=400, detail="Goto node must target a valid node")
    elif node_type in ("menu", "dropdown") and not data.get("options"):
        raise HTTPException(
            status_code=400,
            detail=f"{node_type.capitalize()} node must have at least one option",
        )
    elif node_type == "ai_fallback":
        _validate_ai_fallback_data(data)


def _validate_ai_fallback_data(data: dict) -> None:
    context_source = data.get("context_source")
    if context_source is not None and context_source not in _VALID_CONTEXT_SOURCES:
        raise HTTPException(status_code=400, detail=f"Invalid AI Fallback context source: {context_source!r}")

    llm_mode = data.get("llm_mode")
    if llm_mode is not None and llm_mode not in _VALID_LLM_MODES:
        raise HTTPException(status_code=400, detail=f"Invalid AI Fallback LLM mode: {llm_mode!r}")

    if llm_mode == "attached" and not data.get("llm_api_key_id"):
        raise HTTPException(
            status_code=400,
            detail="AI Fallback is set to use an attached LLM API but no key is selected",
        )


def _validate_edges(edges: list, node_by_id: dict, start_id: str, end_ids: set) -> None:
    seen_ports_by_source: dict = {}
    for edge in edges:
        source, target = edge.get("source"), edge.get("target")
        port = edge.get("source_port", "default")
        if source not in node_by_id or target not in node_by_id:
            raise HTTPException(status_code=400, detail="Edge references an unknown node")
        if target == start_id:
            raise HTTPException(status_code=400, detail="Start node cannot have incoming edges")
        if source in end_ids:
            raise HTTPException(status_code=400, detail="End node cannot have outgoing edges")

        key = (source, port)
        if key in seen_ports_by_source:
            raise HTTPException(
                status_code=400,
                detail=f"Node {source!r} has more than one edge on the same output ({port!r})",
            )
        seen_ports_by_source[key] = True
