"""
Runtime execution engine for Flow Builder — interprets a saved flow graph
against a visitor's per-session state and decides what to send back on
each turn of a live chatbot conversation.

Kept separate from flow_service.py (the builder CRUD side): this module's
concern is stateless-looking interpretation of a graph plus one visitor
session row, not authoring/ownership — the same relationship
ai_analytics_service.py has to chatbot_service.py.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional

from litestar.exceptions import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.db_utils import CRUDQueryBuilder
from app.models.chatbot import ChatbotApiKey
from app.models.flow_builder import ChatbotFlow, ChatbotFlowSession
from app.services import chatbot_service

flow_session_crud = CRUDQueryBuilder(ChatbotFlowSession)

_SESSION_STALE_HOURS = 12
_MAX_INTERNAL_HOPS = 25

# Node types that end a turn waiting for a specific kind of visitor reply.
_AWAITING_TEXT_TYPES = {"ask_input"}
_AWAITING_SELECTION_TYPES = {"menu", "dropdown"}
_AWAITING_NODE_TYPES = _AWAITING_TEXT_TYPES | _AWAITING_SELECTION_TYPES


@dataclass
class FlowEngineResult:
    type: str  # "text" | "buttons" | "dropdown" | "text_prompt" | "flow_ended"
    text: Optional[str] = None
    options: List[dict] = field(default_factory=list)
    insights: List[str] = field(default_factory=list)
    table: Optional[dict] = None


# --------------------------------------------------------------------------
# Graph lookups
# --------------------------------------------------------------------------

def _find_node(graph_data: dict, node_id: str) -> Optional[dict]:
    for node in graph_data.get("nodes", []):
        if node.get("id") == node_id:
            return node
    return None


def _find_start_node(graph_data: dict) -> dict:
    for node in graph_data.get("nodes", []):
        if node.get("type") == "start":
            return node
    raise HTTPException(status_code=400, detail="This flow has no Start node")


def _find_edge(graph_data: dict, source: str, port: str) -> Optional[dict]:
    for edge in graph_data.get("edges", []):
        if edge.get("source") == source and edge.get("source_port", "default") == port:
            return edge
    return None


def _evaluate_condition(value: Any, operator: str, compare_value: str) -> bool:
    value = "" if value is None else str(value)
    if operator == "not_empty":
        return value.strip() != ""
    if operator == "equals":
        return value == compare_value
    if operator == "contains":
        return compare_value in value
    return False


# --------------------------------------------------------------------------
# Session lifecycle
# --------------------------------------------------------------------------

async def _load_or_create_session(
    db: AsyncSession,
    chatbot_key: ChatbotApiKey,
    flow: ChatbotFlow,
    session_token: str,
) -> ChatbotFlowSession:
    session = await flow_session_crud.get_one(
        db, filters={"chatbot_key_id": chatbot_key.id, "session_token": session_token}
    )

    start_node_id = _find_start_node(flow.graph_data)["id"]

    if session is None:
        return await flow_session_crud.create(db, {
            "chatbot_key_id": chatbot_key.id,
            "flow_id": flow.id,
            "session_token": session_token,
            "current_node_id": start_node_id,
            "variables": {},
            "status": "active",
        })

    if _session_needs_restart(session, flow):
        # Different/edited/expired flow since the visitor's last turn —
        # restart in place rather than erroring or spawning a new row.
        session.flow_id = flow.id
        session.current_node_id = start_node_id
        session.variables = {}
        session.status = "active"

    return session


def _session_needs_restart(session: ChatbotFlowSession, flow: ChatbotFlow) -> bool:
    if session.flow_id != flow.id:
        return True
    if session.status == "completed":
        # A completed session (an End node, or any other dead end) starts
        # the flow over on the visitor's next message rather than handing
        # off to AI — while a flow is active for this chatbot, it always
        # answers; only an explicit AI Fallback node inside the flow itself
        # hands a single turn to plain AI answering.
        return True
    if _find_node(flow.graph_data, session.current_node_id) is None:
        return True
    return _session_is_stale(session)


def _session_is_stale(session: ChatbotFlowSession) -> bool:
    if session.updated_at is None:
        return False
    updated_at = session.updated_at
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - updated_at) > timedelta(hours=_SESSION_STALE_HOURS)


async def _persist_session(db: AsyncSession, session: ChatbotFlowSession) -> None:
    await flow_session_crud.update(db, session.id, {
        "current_node_id": session.current_node_id,
        "variables": session.variables,
        "status": session.status,
        "flow_id": session.flow_id,
    })


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

async def advance_flow_session(
    db: AsyncSession,
    chatbot_key: ChatbotApiKey,
    flow: ChatbotFlow,
    session_token: str,
    incoming_message: Optional[str],
    selected_value: Optional[str],
) -> FlowEngineResult:
    graph_data = flow.graph_data
    session = await _load_or_create_session(db, chatbot_key, flow, session_token)

    early_result = _deliver_reply_to_waiting_node(graph_data, session, incoming_message, selected_value)
    if early_result is not None:
        await _persist_session(db, session)
        return early_result

    result = await _run_internal_hops(db, chatbot_key, graph_data, session, incoming_message)
    await _persist_session(db, session)
    return result


def _deliver_reply_to_waiting_node(
    graph_data: dict,
    session: ChatbotFlowSession,
    incoming_message: Optional[str],
    selected_value: Optional[str],
) -> Optional[FlowEngineResult]:
    """
    If the session's current node was waiting on visitor input and this
    call supplies it, consume it and advance. Returns a FlowEngineResult
    only for the "stale/unknown selection, re-emit the same prompt" case;
    None otherwise (meaning: proceed to the normal internal-hop loop).
    """
    node = _find_node(graph_data, session.current_node_id)
    if node is None:
        return None
    node_type = node.get("type")

    if node_type in _AWAITING_TEXT_TYPES and incoming_message:
        variable_name = (node.get("data") or {}).get("variable_name")
        if variable_name:
            # Reassign to a new dict rather than mutating in place — `variables`
            # is a plain (non-Mutable) JSONB column, so an in-place `[key] = ...`
            # on the existing object is invisible to SQLAlchemy's change
            # tracking and silently would not persist.
            session.variables = {**session.variables, variable_name: incoming_message.strip()}
        edge = _find_edge(graph_data, node["id"], "default")
        if edge:
            session.current_node_id = edge["target"]
        return None

    if node_type in _AWAITING_SELECTION_TYPES and selected_value:
        edge = _find_edge(graph_data, node["id"], selected_value)
        if edge:
            session.current_node_id = edge["target"]
            return None
        return _visitor_facing_result(node)  # unknown selection — re-ask

    return None


def _visitor_facing_result(node: dict) -> FlowEngineResult:
    data = node.get("data") or {}
    node_type = node["type"]

    if node_type == "ask_input":
        return FlowEngineResult(type="text_prompt", text=data.get("prompt_text", ""))
    if node_type in ("menu", "dropdown"):
        # Wire protocol type is "buttons" for a Menu node (WhatsApp-style quick
        # replies) and "dropdown" for a Dropdown node — the widget dispatches
        # on this value, so it must not be the raw node_type ("menu").
        response_type = "buttons" if node_type == "menu" else "dropdown"
        options = [
            {"label": o.get("label", ""), "value": o.get("id", o.get("value", ""))}
            for o in data.get("options", [])
        ]
        return FlowEngineResult(type=response_type, text=data.get("prompt_text", ""), options=options)
    if node_type == "send_message":
        return FlowEngineResult(type="text", text=data.get("message_text", ""))

    return FlowEngineResult(type="text", text="")


# --------------------------------------------------------------------------
# Internal-hop loop — one step per node type, each either advances the
# session in place and returns None (loop continues) or returns the
# turn's FlowEngineResult (loop stops).
# --------------------------------------------------------------------------

def _advance_or_complete(session: ChatbotFlowSession, edge: Optional[dict], node_id: str) -> bool:
    if edge:
        session.current_node_id = edge["target"]
        return True
    session.current_node_id = node_id
    session.status = "completed"
    return False


def _step_start(graph_data: dict, session: ChatbotFlowSession, node: dict) -> Optional[FlowEngineResult]:
    edge = _find_edge(graph_data, node["id"], "default")
    if not _advance_or_complete(session, edge, node["id"]):
        return FlowEngineResult(type="flow_ended", text="")
    return None


def _step_goto(graph_data: dict, session: ChatbotFlowSession, node: dict) -> Optional[FlowEngineResult]:
    target = (node.get("data") or {}).get("target_node_id")
    if not target or _find_node(graph_data, target) is None:
        session.status = "completed"
        return FlowEngineResult(type="flow_ended", text="")
    session.current_node_id = target
    return None


def _step_if_else(graph_data: dict, session: ChatbotFlowSession, node: dict) -> Optional[FlowEngineResult]:
    data = node.get("data") or {}
    value = session.variables.get(data.get("variable_name"), "")
    is_true = _evaluate_condition(value, data.get("operator", "not_empty"), data.get("compare_value", ""))
    edge = _find_edge(graph_data, node["id"], "true" if is_true else "false")
    if not _advance_or_complete(session, edge, node["id"]):
        return FlowEngineResult(type="flow_ended", text="")
    return None


def _step_send_message(graph_data: dict, session: ChatbotFlowSession, node: dict) -> FlowEngineResult:
    edge = _find_edge(graph_data, node["id"], "default")
    _advance_or_complete(session, edge, node["id"])
    return FlowEngineResult(type="text", text=(node.get("data") or {}).get("message_text", ""))


def _step_awaiting(session: ChatbotFlowSession, node: dict) -> FlowEngineResult:
    session.current_node_id = node["id"]
    return _visitor_facing_result(node)


def _step_end(session: ChatbotFlowSession, node: dict) -> FlowEngineResult:
    """
    Explicit flow terminator — an End node always completes the session
    (never has an outgoing edge, enforced by flow_service._validate_graph).
    The next visitor message will see session.status == "completed" and be
    handed off to plain AI answering rather than replaying this message.
    """
    session.current_node_id = node["id"]
    session.status = "completed"
    return FlowEngineResult(type="text", text=(node.get("data") or {}).get("message_text", ""))


async def _step_ai_fallback(
    db: AsyncSession,
    chatbot_key: ChatbotApiKey,
    graph_data: dict,
    session: ChatbotFlowSession,
    node: dict,
    incoming_message: Optional[str],
) -> FlowEngineResult:
    edge = _find_edge(graph_data, node["id"], "default")
    _advance_or_complete(session, edge, node["id"])
    try:
        ai_result = await chatbot_service.answer_message(db, chatbot_key, incoming_message or "")
        return FlowEngineResult(
            type="text",
            text=ai_result.summary,
            insights=ai_result.insights or [],
            table=ai_result.table.model_dump() if ai_result.table else None,
        )
    except HTTPException as exc:
        return FlowEngineResult(type="text", text=str(exc.detail))


# Node types that auto-advance without producing visitor-facing output —
# each handler mutates `session` in place and returns None to keep looping,
# or a FlowEngineResult to end the turn (e.g. an unconnected branch).
_CONTINUE_STEP_HANDLERS = {
    "start": _step_start,
    "goto": _step_goto,
    "if_else": _step_if_else,
}


def _loop_guard_result(graph_data: dict, session: ChatbotFlowSession, hops: int) -> Optional[FlowEngineResult]:
    """Terminal result if the hop budget is exhausted or we've walked off the graph, else None."""
    if hops > _MAX_INTERNAL_HOPS:
        session.status = "completed"
        return FlowEngineResult(
            type="text",
            text="Something went wrong continuing this conversation. Let's start over.",
        )
    if _find_node(graph_data, session.current_node_id) is None:
        session.status = "completed"
        return FlowEngineResult(type="flow_ended", text="")
    return None


async def _run_one_hop(
    db: AsyncSession,
    chatbot_key: ChatbotApiKey,
    graph_data: dict,
    session: ChatbotFlowSession,
    node: dict,
    incoming_message: Optional[str],
) -> Optional[FlowEngineResult]:
    """Run one node. Returns None to keep looping, else the turn's terminal result."""
    node_type = node.get("type")

    continue_handler = _CONTINUE_STEP_HANDLERS.get(node_type)
    if continue_handler:
        return continue_handler(graph_data, session, node)

    if node_type in _AWAITING_NODE_TYPES:
        return _step_awaiting(session, node)

    if node_type == "send_message":
        return _step_send_message(graph_data, session, node)

    if node_type == "end":
        return _step_end(session, node)

    if node_type == "ai_fallback":
        return await _step_ai_fallback(db, chatbot_key, graph_data, session, node, incoming_message)

    # Unknown/unsupported node type reached at runtime — end gracefully.
    session.status = "completed"
    return FlowEngineResult(type="flow_ended", text="")


async def _run_internal_hops(
    db: AsyncSession,
    chatbot_key: ChatbotApiKey,
    graph_data: dict,
    session: ChatbotFlowSession,
    incoming_message: Optional[str],
) -> FlowEngineResult:
    hops = 0

    while True:
        hops += 1
        guard_result = _loop_guard_result(graph_data, session, hops)
        if guard_result is not None:
            return guard_result

        node = _find_node(graph_data, session.current_node_id)
        result = await _run_one_hop(db, chatbot_key, graph_data, session, node, incoming_message)
        if result is not None:
            return result
