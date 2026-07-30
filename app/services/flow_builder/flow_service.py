"""
Business logic for the Flow Builder — creating, editing, and activating
saved conversation-flow graphs for a chatbot widget.

Ownership of a flow is always routed through the owning ChatbotApiKey (see
chatbot_service.get_chatbot_key, reused here rather than duplicated) so a
flow can never be read/edited/activated by a user who doesn't own the
chatbot key it belongs to.
"""

import uuid
from typing import List, Optional

from litestar.exceptions import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.db_utils import CRUDQueryBuilder
from app.db.flow_builder.queries import deactivate_other_flows
from app.models.flow_builder import ChatbotFlow
from app.services.chatbot import chatbot_service

flow_crud = CRUDQueryBuilder(ChatbotFlow)

_VALID_NODE_TYPES = {
    "start", "if_else", "goto", "menu", "dropdown",
    "ask_input", "send_message", "ai_fallback", "end",
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

async def get_flows_for_key(db: AsyncSession, user_id: int, key_id: uuid.UUID) -> List[ChatbotFlow]:
    key = await chatbot_service.get_chatbot_key(db, user_id, key_id)  # ownership check
    return await flow_crud.get_many(
        db, filters={"chatbot_key_id": key.id}, order_by="created_at", desc=True
    )


async def get_flow(
    db: AsyncSession,
    user_id: int,
    key_id: uuid.UUID,
    flow_id: uuid.UUID,
) -> ChatbotFlow:
    key = await chatbot_service.get_chatbot_key(db, user_id, key_id)  # ownership check
    flow = await flow_crud.get_by_uuid(db, flow_id, extra_filters={"chatbot_key_id": key.id})
    if not flow:
        raise HTTPException(status_code=404, detail="Flow not found")
    return flow


async def get_active_flow(db: AsyncSession, chatbot_key_id: int) -> Optional[ChatbotFlow]:
    """Runtime-facing lookup — used by the public message handler, keyed on the internal id."""
    return await flow_crud.get_one(db, filters={"chatbot_key_id": chatbot_key_id, "is_active": True})


# --------------------------------------------------------------------------
# Write
# --------------------------------------------------------------------------

async def create_flow(db: AsyncSession, user_id: int, key_id: uuid.UUID, name: str) -> ChatbotFlow:
    key = await chatbot_service.get_chatbot_key(db, user_id, key_id)  # ownership check

    name = (name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Flow name is required")

    return await flow_crud.create(db, {
        "chatbot_key_id": key.id,
        "name": name,
        "graph_data": dict(_DEFAULT_GRAPH),
        "is_active": False,
    })


async def rename_flow(
    db: AsyncSession,
    user_id: int,
    key_id: uuid.UUID,
    flow_id: uuid.UUID,
    name: str,
) -> ChatbotFlow:
    flow = await get_flow(db, user_id, key_id, flow_id)

    name = (name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Flow name is required")

    return await flow_crud.update(db, flow.id, {"name": name})


async def update_flow_graph(
    db: AsyncSession,
    user_id: int,
    key_id: uuid.UUID,
    flow_id: uuid.UUID,
    graph_data: dict,
) -> ChatbotFlow:
    flow = await get_flow(db, user_id, key_id, flow_id)
    _validate_graph(graph_data)
    return await flow_crud.update(db, flow.id, {"graph_data": graph_data})


async def activate_flow(
    db: AsyncSession,
    user_id: int,
    key_id: uuid.UUID,
    flow_id: uuid.UUID,
) -> ChatbotFlow:
    """The 'at most one active flow per chatbot key' enforcement point."""
    key = await chatbot_service.get_chatbot_key(db, user_id, key_id)  # ownership check
    flow = await get_flow(db, user_id, key_id, flow_id)

    await deactivate_other_flows(db, key.id, flow.id)
    flow.is_active = True
    await db.commit()
    await db.refresh(flow)
    return flow


async def delete_flow(db: AsyncSession, user_id: int, key_id: uuid.UUID, flow_id: uuid.UUID) -> None:
    flow = await get_flow(db, user_id, key_id, flow_id)  # ownership check
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
