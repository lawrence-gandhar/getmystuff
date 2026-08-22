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
from app.services.flow_builder import ai_fallback_service

flow_session_crud = CRUDQueryBuilder(ChatbotFlowSession)

_SESSION_STALE_HOURS = 12
_MAX_INTERNAL_HOPS = 25

# Not a wire-protocol type the widget ever sees — an internal signal from
# advance_flow_session to the public message handler meaning "this visitor has
# finished the flow; answer with plain AI instead".
AI_HANDOFF = "ai_handoff"

# Sign-off used whenever a flow reaches a terminal point with nothing of its
# own to say — an End node whose message was left blank, or a branch that runs
# out of graph. Ending the conversation politely is the correct behaviour here:
# a terminal point must never replay the flow from the top, and must never
# answer with silence (which the widget would draw as a blank chat bubble).
_DEFAULT_END_MESSAGE = "Thank you for chatting with us. Goodbye!"

# Node types that end a turn waiting for a specific kind of visitor reply.
_AWAITING_TEXT_TYPES = {"ask_input"}
_AWAITING_SELECTION_TYPES = {"menu", "dropdown"}
_AWAITING_NODE_TYPES = _AWAITING_TEXT_TYPES | _AWAITING_SELECTION_TYPES


@dataclass
class FlowEngineResult:
    # "text" | "buttons" | "dropdown" | "text_prompt". A finished flow is a
    # plain "text" sign-off (see _end_of_flow), never a contentless result —
    # the widget still understands the legacy "flow_ended" type, but nothing
    # emits it, because every terminal point now says something.
    type: str
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
    """
    Whether this visitor's session should be thrown away and re-run from the
    flow's Start node.

    Note what is deliberately absent: `status == "completed"`. A visitor who
    ran the flow to its end is finished with it, and gets plain AI answering
    from then on (see AI_HANDOFF) rather than being looped back to the top
    menu. Every reason listed here outranks that — the flow itself changed
    under them, or the session aged out — so a finished visitor is still
    brought back into a flow that has since been edited or replaced, and the
    widget's restart button mints a fresh session token to re-enter on demand.
    """
    if session.flow_id != flow.id:
        return True
    if _flow_edited_since_last_turn(session, flow):
        return True
    if _find_node(flow.graph_data, session.current_node_id) is None:
        return True
    return _session_is_stale(session)


def _as_utc(value: Optional[datetime]) -> Optional[datetime]:
    """Normalize a possibly-naive timestamp column to an aware UTC datetime."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _flow_edited_since_last_turn(session: ChatbotFlowSession, flow: ChatbotFlow) -> bool:
    """
    True when the flow was saved in the builder after this visitor's last turn.

    Editing a flow rewrites graph_data on the existing row, so flow_id stays
    the same and can't detect it. Without this check an in-progress session
    keeps walking its old position — the visitor sees none of the edit until
    they happen to reach the end of the flow, and reloading the page doesn't
    help because the session token is persisted in the browser's
    localStorage.
    """
    flow_updated_at = _as_utc(flow.updated_at)
    session_updated_at = _as_utc(session.updated_at)
    if flow_updated_at is None or session_updated_at is None:
        return False
    return flow_updated_at > session_updated_at


def _session_is_stale(session: ChatbotFlowSession) -> bool:
    updated_at = _as_utc(session.updated_at)
    if updated_at is None:
        return False
    return (datetime.now(timezone.utc) - updated_at) > timedelta(hours=_SESSION_STALE_HOURS)


async def _persist_session(db: AsyncSession, session: ChatbotFlowSession) -> None:
    """
    Write the session's post-turn state back.

    `updated_at` is set explicitly rather than left to the column's
    `onupdate=func.now()`. CRUDQueryBuilder.update() assigns attributes on the
    loaded row and commits, so SQLAlchemy skips the UPDATE entirely when a turn
    happens to land on the state already stored — a menu re-asking itself, or a
    restart that walks back to the same node. No UPDATE means no `onupdate`,
    which would freeze `updated_at` at an old value and make every later turn
    look stale to _flow_edited_since_last_turn and _session_is_stale.
    """
    queued_email = getattr(session, "_email_queued", False)

    await flow_session_crud.update(db, session.id, {
        "current_node_id": session.current_node_id,
        "variables": session.variables,
        "status": session.status,
        "flow_id": session.flow_id,
        "updated_at": datetime.now(timezone.utc),
    })

    # An Email node ran this turn, and `update` has now committed — so the queued message is
    # visible to a worker. Waking it here rather than in the node is the whole point: a
    # worker woken *before* the commit looks, finds nothing, and sleeps for its full poll
    # interval, which turns an instant send into a five-second one.
    #
    # Nudged from this one place because this is the single function that commits a turn, and
    # there are four call sites that reach it. The flag is a plain attribute on the session
    # object rather than a column: it is true for the rest of this turn only, and a column
    # would be state nobody ever reads twice.
    if queued_email:
        from app.services.email_dispatch.nodes import flow_builder_runner

        session._email_queued = False  # noqa: SLF001
        flow_builder_runner.wake_worker()


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

    if session.status == "completed":
        # This visitor already reached the end of the flow, and
        # _load_or_create_session found no reason to re-run it. Report the
        # handoff instead of answering: the caller switches this turn (and
        # every later one, until the flow changes or the session ages out) to
        # plain AI answering. Nothing is persisted — the session is untouched.
        return FlowEngineResult(type=AI_HANDOFF)

    # A graph asked this visitor something on an earlier turn, so their message is the
    # answer to it rather than input to the flow. Checked **first**, before anything else
    # reads the message: the session is sitting on a Run-Graph node, which the ordinary
    # waiting-node path knows nothing about, and running that node again would ask the
    # same question a second time.
    if session.awaiting_graph_run:
        answered = await _answer_waiting_graph(
            chatbot_key, session, graph_data, incoming_message,
        )

        if answered is not None:
            await _persist_session(db, session)
            return answered

        # The answer landed and the graph finished. The session now points at whatever
        # follows the Run-Graph node, so the ordinary loop below carries the turn on —
        # which is what makes the pause invisible in the rest of the conversation.
        result = await _run_internal_hops(
            db, chatbot_key, flow.id, graph_data, session, incoming_message,
        )
        await _persist_session(db, session)
        return result

    # Resolved before the selection is consumed, because consuming it moves the
    # session off the node that owns the options.
    selected_option = _selected_option(graph_data, session, selected_value)

    early_result = _deliver_reply_to_waiting_node(
        graph_data, session, incoming_message, selected_value, selected_option,
    )
    if early_result is not None:
        await _persist_session(db, session)
        return early_result

    result = await _run_internal_hops(
        db, chatbot_key, flow.id, graph_data, session,
        _effective_message(incoming_message, selected_option),
    )
    await _persist_session(db, session)
    return result


def _selected_option(
    graph_data: dict,
    session: ChatbotFlowSession,
    selected_value: Optional[str],
) -> Optional[dict]:
    """
    The option the visitor just picked, as it is written in the graph, or None
    when this turn is not a selection.

    ``selected_value`` is the option's **id** — that is what a Menu/Dropdown node
    publishes as each button's value (see :func:`_visitor_facing_result`) and what
    the edges use as their ``source_port``.
    """
    if not selected_value:
        return None

    node = _find_node(graph_data, session.current_node_id)
    if node is None or node.get("type") not in _AWAITING_SELECTION_TYPES:
        return None

    for option in (node.get("data") or {}).get("options", []):
        if option.get("id") == selected_value:
            return option

    return None


def _option_text(option: dict) -> str:
    """What the visitor effectively said by picking this option."""
    return str(option.get("label") or option.get("value") or "").strip()


def _effective_message(incoming_message: Optional[str], selected_option: Optional[dict]) -> str:
    """
    The text the rest of this turn should treat as the visitor's message.

    A button or dropdown reply carries no typed text — the widget sends an empty
    ``message`` and puts the choice in ``selected_value`` — so anything downstream
    that needs a question would otherwise get an empty string. An AI Fallback node
    reached straight from a Menu is the case that matters: asking a model nothing
    at all makes it answer nothing at all (or, with a scoped system prompt, refuse),
    and searching a knowledge base for "" matches nothing.

    So a selection turn hands on the option's label, which is exactly what the
    visitor sees in their own chat bubble. Typed text always wins when both are
    present, since the visitor's own words are the better question.
    """
    message = (incoming_message or "").strip()
    if message:
        return message
    return _option_text(selected_option) if selected_option else ""


def _deliver_reply_to_waiting_node(
    graph_data: dict,
    session: ChatbotFlowSession,
    incoming_message: Optional[str],
    selected_value: Optional[str],
    selected_option: Optional[dict] = None,
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
        _store_answer(session, node, incoming_message.strip())
        edge = _find_edge(graph_data, node["id"], "default")
        if edge:
            session.current_node_id = edge["target"]
        return None

    if node_type in _AWAITING_SELECTION_TYPES and selected_value:
        edge = _find_edge(graph_data, node["id"], selected_value)
        if edge:
            # Recorded before the hop, so a later If/Else can branch on what was
            # picked. Without this a selection existed only as the edge it chose,
            # and nothing downstream could tell Python from PHP.
            if selected_option is not None:
                _store_answer(session, node, _option_text(selected_option))
            session.current_node_id = edge["target"]
            return None
        return _visitor_facing_result(node)  # unknown selection — re-ask

    return None


def _store_answer(session: ChatbotFlowSession, node: dict, value: str) -> None:
    """
    Record what the visitor just supplied under the node's configured variable
    name, if it has one. A node with no variable name stores nothing.

    Reassigns to a new dict rather than mutating in place — `variables` is a
    plain (non-Mutable) JSONB column, so an in-place `[key] = ...` on the
    existing object is invisible to SQLAlchemy's change tracking and silently
    would not persist.
    """
    variable_name = (node.get("data") or {}).get("variable_name")
    if not variable_name:
        return
    session.variables = {**session.variables, variable_name: value}


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

    # Unknown node type — sign off rather than return an empty "text", which
    # the widget would render as a blank chat bubble.
    return FlowEngineResult(type="text", text=_DEFAULT_END_MESSAGE)


# --------------------------------------------------------------------------
# Internal-hop loop — one step per node type, each either advances the
# session in place and returns None (loop continues) or returns the
# turn's FlowEngineResult (loop stops).
# --------------------------------------------------------------------------

def _message_text(node: dict) -> str:
    """A node's configured message, normalized — "" for unset/whitespace-only."""
    return ((node.get("data") or {}).get("message_text") or "").strip()


def _end_of_flow(session: ChatbotFlowSession, message: str = "") -> FlowEngineResult:
    """
    Close the conversation out: mark the session completed and sign off with
    `message`, or _DEFAULT_END_MESSAGE when the flow didn't supply one.
    """
    session.status = "completed"
    return FlowEngineResult(type="text", text=message or _DEFAULT_END_MESSAGE)


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
        return _end_of_flow(session)
    return None


def _step_goto(graph_data: dict, session: ChatbotFlowSession, node: dict) -> Optional[FlowEngineResult]:
    target = (node.get("data") or {}).get("target_node_id")
    if not target or _find_node(graph_data, target) is None:
        return _end_of_flow(session)
    session.current_node_id = target
    return None


def _step_if_else(graph_data: dict, session: ChatbotFlowSession, node: dict) -> Optional[FlowEngineResult]:
    data = node.get("data") or {}
    value = session.variables.get(data.get("variable_name"), "")
    is_true = _evaluate_condition(value, data.get("operator", "not_empty"), data.get("compare_value", ""))
    edge = _find_edge(graph_data, node["id"], "true" if is_true else "false")
    if not _advance_or_complete(session, edge, node["id"]):
        return _end_of_flow(session)
    return None


def _step_send_message(
    graph_data: dict, session: ChatbotFlowSession, node: dict
) -> Optional[FlowEngineResult]:
    message_text = _message_text(node)
    edge = _find_edge(graph_data, node["id"], "default")
    advanced = _advance_or_complete(session, edge, node["id"])

    if not message_text:
        # A Send Message node left blank has nothing to say — keep hopping so
        # the turn isn't spent on an empty chat bubble. With nowhere left to
        # hop, that makes it the end of the conversation.
        return None if advanced else _end_of_flow(session)

    return FlowEngineResult(type="text", text=message_text)


def _step_awaiting(session: ChatbotFlowSession, node: dict) -> FlowEngineResult:
    session.current_node_id = node["id"]
    return _visitor_facing_result(node)


def _step_end(session: ChatbotFlowSession, node: dict) -> FlowEngineResult:
    """
    Explicit flow terminator — an End node always completes the session
    (never has an outgoing edge, enforced by flow_service._validate_graph) and
    signs off in the same turn. It must never continue or replay the flow: the
    visitor chose to finish.

    An End node with no message of its own falls back to _DEFAULT_END_MESSAGE
    so the goodbye still lands.
    """
    session.current_node_id = node["id"]
    return _end_of_flow(session, _message_text(node))


async def _step_ai_fallback(
    db: AsyncSession,
    chatbot_key: ChatbotApiKey,
    flow_id: int,
    graph_data: dict,
    session: ChatbotFlowSession,
    node: dict,
    incoming_message: Optional[str],
) -> FlowEngineResult:
    edge = _find_edge(graph_data, node["id"], "default")
    _advance_or_complete(session, edge, node["id"])
    try:
        ai_result = await ai_fallback_service.run_ai_fallback(
            db, chatbot_key, flow_id, node["id"], node.get("data") or {}, incoming_message or "",
        )
        return FlowEngineResult(
            type="text",
            text=ai_result.summary,
            insights=ai_result.insights or [],
            table=ai_result.table.model_dump() if ai_result.table else None,
        )
    except HTTPException as exc:
        return FlowEngineResult(type="text", text=str(exc.detail))


async def _step_run_graph(
    chatbot_key: ChatbotApiKey,
    graph_data: dict,
    session: ChatbotFlowSession,
    node: dict,
) -> Optional[FlowEngineResult]:
    """
    Run a published Graph Designer graph, and decide what that means for this turn.

    The one node whose work happens outside this feature: the graph is somebody's drawn
    sequence of queries, loops and checks, and it can do things no flow node can. Three
    outcomes, and each does something different here:

    * **it finished** — whatever it produced is stored under the node's variable name, if
      it has one, and the flow hops on. Nothing is said to the visitor, exactly as a
      blank Send Message node says nothing: a graph that read some rows is a step in a
      conversation, not a message in it.
    * **it stopped to ask something** — the turn ends with the operator's question, and
      the run's id is parked on the session so the visitor's next message answers it. This
      is the only non-prompt node that can end a turn waiting, and
      :func:`_answer_waiting_graph` is the other half of it.
    * **it failed** — the ``error`` port if one is drawn, otherwise the flow signs off.
      Never a silent hop onward: a flow carrying on as though a step had succeeded is how
      a visitor gets told something that is not true.

    The graph is run **as its owner**, and the owner is resolved from the chatbot key's
    ``user_id`` rather than taken from the graph row — so a flow can only run a graph its
    own owner has, and the datasources its nodes read are that person's.

    The visitor's captured variables are passed in as the run's inputs, which is what lets
    a graph filter on something an Ask-for-Input node collected earlier in the same
    conversation. A graph declares which of them it will use, as parameters, so a variable
    it did not ask for has nowhere to land.
    """
    from app.services.graph_designer import graph_runner

    data = node.get("data") or {}
    graph_uuid = str(data.get("graph_id") or "").strip()

    if not graph_uuid:
        # A node nobody finished configuring. Said out loud rather than skipped: a flow
        # that quietly steps over a step is a flow whose author cannot tell it is broken.
        return _end_of_flow(
            session,
            "Sorry — this conversation is not set up correctly. Please try again later.",
        )

    outcome = await graph_runner.run_graph(
        int(chatbot_key.user_id),
        graph_uuid,
        inputs=dict(session.variables or {}),
    )

    if outcome.asks:
        session.current_node_id = node["id"]
        session.awaiting_graph_run = outcome.run_id
        question = str((outcome.question or {}).get("prompt") or "").strip()

        # The operator's words, unchanged. A paraphrase here would ask the visitor a
        # different question and make their answer unmatchable — the same rule
        # `graph_tool_factory` and `download_service.offer_sentence` both keep.
        return FlowEngineResult(
            type="text_prompt",
            text=question or "Could you confirm before I continue?",
        )

    if not outcome.finished:
        error_edge = _find_edge(graph_data, node["id"], "error")

        if error_edge:
            session.current_node_id = error_edge["target"]
            return None

        return _end_of_flow(
            session,
            "Sorry — something went wrong working that out. Please try again later.",
        )

    _store_graph_result(session, node, outcome)

    edge = _find_edge(graph_data, node["id"], "default")

    return None if _advance_or_complete(session, edge, node["id"]) else _end_of_flow(
        session,
    )


def _store_graph_result(session: ChatbotFlowSession, node: dict, outcome) -> None:  # noqa: ANN001
    """
    Record what a finished graph produced under the node's variable name, if it has one.

    Stored as a **count**, not as the rows. A flow variable is a string that gets
    interpolated into message text and compared by an If/Else node, so what is useful
    there is "how many" — *"I found 12 matching orders"*, or a branch on whether there
    were any at all. Putting a result set in it would produce a chat bubble containing
    JSON.

    The count is ``total_rows``, which is the real total rather than the length of the
    preview. Those differ whenever a graph returned more rows than a preview holds, and
    telling a visitor "20" when there were 5,275 is the exact failure this application
    keeps writing tests against.

    Reassigns ``variables`` rather than mutating it, for the reason
    :func:`_store_answer` gives: it is a plain JSONB column and an in-place write is
    invisible to change tracking.
    """
    variable_name = (node.get("data") or {}).get("variable_name")

    if not variable_name:
        return

    session.variables = {
        **(session.variables or {}),
        str(variable_name): str(outcome.total_rows),
    }


async def _answer_waiting_graph(
    chatbot_key: ChatbotApiKey,
    session: ChatbotFlowSession,
    graph_data: dict,
    incoming_message: Optional[str],
) -> Optional[FlowEngineResult]:
    """
    Hand the visitor's message to a graph that asked them something, and carry on.

    Called before the ordinary hop loop, and only when ``awaiting_graph_run`` is set. Four
    things can come of it, and the interesting one is the third:

    * **the answer fitted and the graph finished** — the flag is cleared and ``None`` is
      returned, so the hop loop takes over from the Run-Graph node and moves on. The
      visitor's answer has done its job and the conversation continues normally.
    * **the graph asked something else** — a second interrupt in the same graph. The new
      question goes out and the new run id is parked. Nothing about this is special-cased;
      it is the same branch as the first question.
    * **the answer did not fit** — "maybe" to a yes/no. The question is asked **again**
      with the validator's own sentence in front of it, and the run stays parked. This is
      the case worth being careful about: it is ordinary input, not a fault, and treating
      it as a failure would tell a visitor the conversation is broken when they need only
      answer differently.
    * **it failed** — the flag is cleared and the flow signs off, rather than leaving a
      session waiting forever on a run that will never finish.
    """
    from app.services.graph_designer import graph_runner

    run_id = str(session.awaiting_graph_run or "")
    node = _find_node(graph_data, session.current_node_id)

    # The key is passed in rather than reached through `session.chatbot_key`: that is a
    # lazy relationship, and touching one on an async session outside a load raises
    # instead of loading. The caller already holds it.
    outcome = await graph_runner.answer_graph_run(
        int(chatbot_key.user_id), run_id, incoming_message or "",
    )

    if outcome.asks and outcome.reason:
        # Not accepted. Ask again, saying why, and keep the run parked.
        question = str(
            (outcome.question or {}).get("prompt")
            or (node or {}).get("data", {}).get("prompt_text")
            or ""
        ).strip()

        return FlowEngineResult(
            type="text_prompt",
            text=f"{outcome.reason} {question}".strip(),
        )

    if outcome.asks:
        session.awaiting_graph_run = outcome.run_id
        return FlowEngineResult(
            type="text_prompt",
            text=str((outcome.question or {}).get("prompt") or "").strip(),
        )

    session.awaiting_graph_run = None

    if not outcome.finished:
        error_edge = (
            _find_edge(graph_data, node["id"], "error") if node is not None else None
        )

        if error_edge:
            session.current_node_id = error_edge["target"]
            return None

        return _end_of_flow(
            session,
            "Sorry — something went wrong working that out. Please try again later.",
        )

    if node is not None:
        _store_graph_result(session, node, outcome)
        edge = _find_edge(graph_data, node["id"], "default")

        if not _advance_or_complete(session, edge, node["id"]):
            return _end_of_flow(session)

    return None


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
        return _end_of_flow(session)
    return None



async def _step_send_email(
    db: AsyncSession,
    chatbot_key: ChatbotApiKey,
    graph_data: dict,
    session: ChatbotFlowSession,
    node: dict,
) -> Optional[FlowEngineResult]:
    """
    Queue an email, and hop on without saying anything.

    **Nothing is said to the visitor.** A node that announced "I have emailed the team"
    would be putting words in the operator's mouth; if they want the visitor told, that is a
    Send Message node next to this one, which they control. Same call a ``run_graph`` node
    that finished quietly makes.

    This is where "dynamic variables come from the Agents section" is most literal: the
    bindings can read the conversation's own variables *and* the chatbot's prompt variables
    from ``ChatbotAiSettings`` — ``{{COMPANY}}``, ``{{AGENT_NAME}}`` — so an email can mix
    something the visitor typed with something the operator configured under Agents.

    On failure: the ``error`` port if one is drawn, otherwise sign off. Never a silent hop
    onward — a flow carrying on as though a step had succeeded is how a visitor gets told
    something that is not true, which is the rule ``_step_run_graph`` states.

    The message is enqueued in **this turn's session**, so it lands with the session's own
    variable updates and the turn is atomic. The worker is woken after
    ``_persist_session`` commits, which is why ``wake_worker`` is a separate call the caller
    makes rather than something the runner does for itself.
    """
    from app.services.email_dispatch.errors import EmailFailure
    from app.services.email_dispatch.nodes import flow_builder_runner

    try:
        queued = await flow_builder_runner.run_email_node(
            db,
            node,
            chatbot_key=chatbot_key,
            session_variables=dict(session.variables or {}),
            session_token=session.session_token or "",
        )
    except Exception as exc:  # noqa: BLE001 — routed, not raised
        message = (
            exc.message
            if isinstance(exc, EmailFailure)
            else "an email step could not be completed"
        )
        logger.warning(
            "Email node %s in flow session %s failed: %s",
            node.get("id"),
            session.id,
            message,
        )
        error_edge = _find_edge(graph_data, node["id"], "error")
        if error_edge:
            session.current_node_id = error_edge["target"]
            return None
        return _end_of_flow(session)

    # Recorded under the node's variable name if it has one, so a later If/Else can branch on
    # whether an email went out. The uuid, never the bigint id — a flow variable can end up
    # interpolated into a chat bubble.
    _store_answer(session, node, queued["message_uuid"])

    # Marked on the session so `advance_flow_session` knows to wake the worker after it
    # commits. Set on the object rather than a column: it is true for the rest of this turn
    # only, and a column would be state nobody reads twice.
    session._email_queued = True  # noqa: SLF001

    edge = _find_edge(graph_data, node["id"], "default")
    if not _advance_or_complete(session, edge, node["id"]):
        return _end_of_flow(session)
    return None


async def _run_one_hop(
    db: AsyncSession,
    chatbot_key: ChatbotApiKey,
    flow_id: int,
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
        return await _step_ai_fallback(db, chatbot_key, flow_id, graph_data, session, node, incoming_message)

    if node_type == "run_graph":
        return await _step_run_graph(chatbot_key, graph_data, session, node)

    if node_type == "send_email":
        return await _step_send_email(db, chatbot_key, graph_data, session, node)

    # Unknown/unsupported node type reached at runtime — end gracefully.
    return _end_of_flow(session)


async def _run_internal_hops(
    db: AsyncSession,
    chatbot_key: ChatbotApiKey,
    flow_id: int,
    graph_data: dict,
    session: ChatbotFlowSession,
    incoming_message: Optional[str],
) -> FlowEngineResult:
    hops = 0

    while True:
        hops += 1
        result = _loop_guard_result(graph_data, session, hops)
        if result is not None:
            return result

        node = _find_node(graph_data, session.current_node_id)
        result = await _run_one_hop(db, chatbot_key, flow_id, graph_data, session, node, incoming_message)
        if result is not None:
            return result
