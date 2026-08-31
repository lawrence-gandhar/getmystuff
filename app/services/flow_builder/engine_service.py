"""
Runtime execution engine for Flow Builder — interprets a saved flow graph
against a visitor's per-session state and decides what to send back on
each turn of a live chatbot conversation.

Kept separate from flow_service.py (the builder CRUD side): this module's
concern is stateless-looking interpretation of a graph plus one visitor
session row, not authoring/ownership — the same relationship
ai_analytics_service.py has to chatbot_service.py.

*A* graph, not *the* graph: a Run Flow block runs another flow as a step of this
one, so which graph is being interpreted is re-decided on every hop from the
session's call stack. `subflow_service` owns that stack and the variable scope
each call gets; `_run_internal_hops` is the one place here that knows about it.
"""

import logging
import re  # noqa: F401 — the `re.Match` annotation in _render_text
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional

from litestar.exceptions import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.db_utils import CRUDQueryBuilder
from app.models.chatbot import ChatbotApiKey
from app.models.flow_builder import ChatbotFlow, ChatbotFlowSession
from app.services.flow_builder import ai_fallback_service, flow_service, subflow_service

logger = logging.getLogger(__name__)

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
    # The download button this turn is offering, or None — which is nearly every turn.
    # Set only by a Download File block whose operator ticked *show a button*, and it is
    # deliberately **not** a `type`: the button is drawn under whatever the turn already
    # said rather than instead of it, so a Send Message block before it still speaks and a
    # Menu block after it still offers its options. A type would have made those exclusive.
    file_download: Optional[dict] = None


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
            "call_stack": [],
            "node_results": {},
            "dead_end_ai_context": {},
            "status": "active",
        })

    if _session_needs_restart(session, flow):
        # Different/edited/expired flow since the visitor's last turn —
        # restart in place rather than erroring or spawning a new row.
        session.flow_id = flow.id
        session.current_node_id = start_node_id
        session.variables = {}
        # And what blocks produced. These point at a graph run and an AI answer from a
        # conversation that is being started over, so keeping them would let a Create File
        # block in the *new* run write a file out of the old one's data.
        session.node_results = {}
        # Same reason: a dead-end AI Fallback's last answer belongs to the conversation
        # that is being started over, not the new one.
        session.dead_end_ai_context = {}
        # Any Run Flow call in progress goes with them: a frame points into a call that
        # began under the graph being replaced, and returning a visitor into the middle of a
        # flow they never entered is worse than starting them over.
        subflow_service.clear(session)
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
    if _session_is_stale(session):
        return True

    # "The position no longer exists" — but only about *this* graph. A visitor inside a Run
    # Flow call is parked on a node in the **callee's** graph, which is not in this one, so
    # checking it here would report every such session as lost and restart it from the top on
    # every single turn (it did: a sub-flow's question was re-asked forever, because the
    # restart re-entered the call before the answer was read). The same situation one level
    # down is handled where the callee's graph is actually in hand — `advance_flow_session`
    # for a deleted flow, `_run_internal_hops` for a deleted node — and both route it to the
    # caller's `failed` port rather than silently starting over.
    if subflow_service.in_subflow(session):
        return False

    return _find_node(flow.graph_data, session.current_node_id) is None


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
        # Which Run Flow calls this visitor is inside, and whose variables to restore on the
        # way out. Written here with everything else so a turn that ended halfway down a
        # sub-flow is one atomic row: a stack saved without the position it refers to, or a
        # position saved without its stack, would resume the next turn in the wrong flow.
        "call_stack": session.call_stack,
        # What blocks produced this turn, for a Create File block later in the conversation
        # to read. Written with everything else for the same reason the stack is: a file
        # block that resolved against a result the turn then failed to save would write a
        # file out of data the conversation does not have.
        "node_results": session.node_results,
        "dead_end_ai_context": session.dead_end_ai_context,
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


async def _continue_dead_end_ai_fallback(
    db: AsyncSession,
    chatbot_key: ChatbotApiKey,
    flow: ChatbotFlow,
    session: ChatbotFlowSession,
    incoming_message: Optional[str],
) -> Optional[FlowEngineResult]:
    """
    If this visitor's session is "completed" because it dead-ended on an AI Fallback
    node (one with no outgoing edge), answer this message with that same node — again
    — instead of handing off. Returns None for every other terminal point (an explicit
    End node, a dead end on any other block type), which leaves AI_HANDOFF exactly as
    it was.

    Only reachable with an empty call stack: a dead end reached *inside* a Run Flow
    call is converted back into a normal call return before `advance_flow_session`'s
    top-level `status == "completed"` check can ever see it (see
    `_hop_until_the_turn_ends`'s subflow-unwind branch). The `in_subflow` check below
    is a defensive guard against the one gap in that invariant — the
    `_MAX_INTERNAL_HOPS` bailout sets `status = "completed"` directly without going
    through the unwind — rather than resting entirely on it holding.
    """
    if subflow_service.in_subflow(session):
        return None

    node = _find_node(flow.graph_data, session.current_node_id)
    if node is None or node.get("type") != "ai_fallback":
        return None

    return await _step_ai_fallback(
        db, chatbot_key, flow.id, flow.graph_data, session, node,
        incoming_message, from_selection=False,
    )


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
    session = await _load_or_create_session(db, chatbot_key, flow, session_token)

    if session.status == "completed":
        # This visitor already reached the end of the flow, and
        # _load_or_create_session found no reason to re-run it. Usually that means
        # handing off instead of answering — but if they finished on a dead-end AI
        # Fallback node (one with no outgoing edge, drawn that way on purpose), that
        # node keeps answering rather than going quiet or falling back to a generic
        # off-flow reply. See _continue_dead_end_ai_fallback.
        dead_end_result = await _continue_dead_end_ai_fallback(
            db, chatbot_key, flow, session, incoming_message,
        )
        if dead_end_result is not None:
            await _persist_session(db, session)
            return dead_end_result

        # Every other terminal point: the caller switches this turn (and every later
        # one, until the flow changes or the session ages out) to plain AI answering.
        # Nothing is persisted — the session is untouched.
        return FlowEngineResult(type=AI_HANDOFF)

    # Which flow this visitor is actually standing in, which is `flow` unless they are
    # inside a Run Flow call. Everything below reads the node the session is *parked* on —
    # the menu whose button was just clicked, the prompt being answered — and that node
    # lives in the callee's graph when there is one. Resolved once here and re-resolved on
    # every hop inside `_run_internal_hops`, sharing this cache so the same flow is fetched
    # at most once for the turn.
    flow_cache: dict = {}
    current_flow = await subflow_service.current_flow(db, session, flow, flow_cache)

    if current_flow is None:
        # The callee has been deleted while this visitor was inside it. There is no graph to
        # read their reply against, so the call is abandoned and the caller's `error` port
        # decides what they are told.
        result = await _fail_call(
            db, session, flow, flow_cache,
            "the flow it was running has been deleted",
        )
        if result is None:
            result = await _run_internal_hops(
                db, chatbot_key, flow, session, incoming_message, flow_cache=flow_cache,
            )
        await _persist_session(db, session)
        return result

    graph_data = current_flow.graph_data

    # A graph asked this visitor something on an earlier turn, so their message is the
    # answer to it rather than input to the flow. Checked **first**, before anything else
    # reads the message: the session is sitting on a Run-Graph node, which the ordinary
    # waiting-node path knows nothing about, and running that node again would ask the
    # same question a second time.
    if session.awaiting_graph_run:
        answered = await _answer_waiting_graph(
            db, chatbot_key, session, graph_data, incoming_message, flow, flow_cache,
        )

        if answered is not None:
            await _persist_session(db, session)
            return answered

        # The answer landed and the graph finished. The session now points at whatever
        # follows the Run-Graph node, so the ordinary loop below carries the turn on —
        # which is what makes the pause invisible in the rest of the conversation.
        result = await _run_internal_hops(
            db, chatbot_key, flow, session, incoming_message, flow_cache=flow_cache,
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

    # Whether this turn's "question" is a button label rather than the visitor's own
    # words. An AI Fallback node searching a knowledge base needs to know: a label is
    # written to be clicked, not to be searched for. See
    # `ai_fallback_service._retrieval_query`.
    from_selection = not (incoming_message or "").strip() and selected_option is not None

    result = await _run_internal_hops(
        db, chatbot_key, flow, session,
        _effective_message(incoming_message, selected_option),
        from_selection=from_selection, flow_cache=flow_cache,
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
    Record what a node produced under its configured variable name, if it has
    one. A node with no variable name stores nothing.

    Shared by every storing node rather than repeated per type: what the visitor
    typed or clicked (Ask Input, Menu, Dropdown), what an AI Fallback answered,
    and the uuid of an email an Email node queued.

    Reassigns to a new dict rather than mutating in place — `variables` is a
    plain (non-Mutable) JSONB column, so an in-place `[key] = ...` on the
    existing object is invisible to SQLAlchemy's change tracking and silently
    would not persist.
    """
    variable_name = (node.get("data") or {}).get("variable_name")
    if not variable_name:
        return
    session.variables = {**session.variables, variable_name: value}


#: How many rows of an AI Fallback's answer table the session keeps for a Create File
#: block to write. Two thousand: far above anything a model actually returns in the
#: `AnalyticsTable` it authors, and low enough that one conversation's row cannot become a
#: pathological JSONB column.
#:
#: A table larger than this is stored **marked** rather than silently cut — see below.
_MAX_STORED_RESULT_ROWS = 2000


def _store_node_result(session: ChatbotFlowSession, node: dict, record: dict) -> None:
    """
    Record what a block produced, for a later Create File block to write.

    Keyed by **node id**, not by variable name, because a Create File block points at one
    particular block on the canvas — which is a different question from "what is the
    current value of X", and two blocks may share a variable name.

    Written for every block that produces one, whether or not anything reads it. The
    alternative — only recording when some Create File block names this node — would make
    the record depend on the rest of the drawing, so adding a file block would silently
    change what an *earlier* block does, and a flow edited mid-conversation would have a
    session with a hole in it.

    Reassigns to a new dict rather than mutating in place, for the reason
    :func:`_store_answer` gives: `node_results` is a plain (non-Mutable) JSONB column and
    an in-place write is invisible to SQLAlchemy's change tracking, so it would silently
    not persist.
    """
    node_id = str(node.get("id") or "")

    if not node_id:
        return

    session.node_results = {**(session.node_results or {}), node_id: record}


def _next_file_sequence(session: ChatbotFlowSession) -> int:
    """
    The next value for a Create File result's ``sequence``, one higher than any seen yet.

    Only file results carry this — see :func:`_step_download_file` for why: a Download
    File block may now name *several* Create File blocks (a menu of formats, each its own
    branch, sharing one hand-over block), and when more than one has run in the
    conversation, the most recently written file is the right one to offer. ``node_results``
    is a plain dict with no ordering guarantee once it has round-tripped through JSONB, so
    "most recent" is a number stored on the record rather than dict position.
    """
    existing = session.node_results or {}
    return max(
        (int(r.get("sequence", 0)) for r in existing.values() if isinstance(r, dict)),
        default=0,
    ) + 1


def _table_result(table) -> dict:  # noqa: ANN001 — AnalyticsTable
    """
    One ``AnalyticsTable`` as a stored record, honest about being cut short.

    The cap is a storage limit, not a display one, so a table past it is kept **with
    ``truncated`` set** rather than quietly shortened. That flag is the whole point:
    `file_delivery.row_source` refuses to build a file out of a truncated table, so an
    operator gets a failed block and a sentence instead of a spreadsheet that is missing
    rows and says nothing about it. The same honesty rule `_store_graph_result` applies to
    a preview versus a real total.
    """
    rows = list(table.rows or [])

    return {
        "kind": "table",
        "columns": [str(column) for column in table.columns or []],
        "rows": rows[:_MAX_STORED_RESULT_ROWS],
        "truncated": len(rows) > _MAX_STORED_RESULT_ROWS,
        "total_rows": len(rows),
    }


def _render_text(text: Optional[str], variables: Optional[dict]) -> str:
    """
    Substitute ``{{NAME}}`` in a block's visitor-facing text from the conversation's
    variables.

    This is what makes a collected or returned value worth having: a Run Flow block that
    brings back ``CUSTOMER_EMAIL``, an Ask-for-Input answer, an AI Fallback's summary can
    all now be *said*, not only branched on by an If/Else or bound into an email.

    **An unknown placeholder is left standing**, and logged. That is the third set of
    semantics in this codebase for the same syntax and it is deliberate, following
    ``chatbot_ai_settings_service.render_system_prompt`` rather than
    ``email_dispatch/rendering.render``: an email that has gone out saying "Dear
    {{CUSTOMER}}" cannot be recalled, so that renderer refuses the whole send — but a chat
    bubble is one message in a live conversation, and a visible ``{{ORDER_REF}}`` tells the
    operator exactly which name is wrong in a way a blank space never would. Blanking it
    would hide the mistake; refusing the turn would break a conversation over a typo.

    Names are matched **exactly**, not case-insensitively, because every other block in this
    feature treats ``email`` and ``EMAIL`` as two different variables — an If/Else compares
    the name as typed, and ``_store_answer`` writes it as typed. Folding case only here
    would make the same name mean two things in one flow.
    """
    text = text or ""
    if "{{" not in text:
        return text

    values = variables or {}

    def _substitute(match: "re.Match[str]") -> str:
        name = match.group(1)
        if name in values:
            return str(values[name])
        logger.warning(
            "Flow text references {{%s}}, which this conversation has no value for", name,
        )
        return match.group(0)

    return flow_service.PLACEHOLDER_RE.sub(_substitute, text)


def _visitor_facing_result(node: dict, variables: Optional[dict] = None) -> FlowEngineResult:
    data = node.get("data") or {}
    node_type = node["type"]

    if node_type == "ask_input":
        return FlowEngineResult(
            type="text_prompt", text=_render_text(data.get("prompt_text"), variables),
        )
    if node_type in ("menu", "dropdown"):
        # Wire protocol type is "buttons" for a Menu node (WhatsApp-style quick
        # replies) and "dropdown" for a Dropdown node — the widget dispatches
        # on this value, so it must not be the raw node_type ("menu").
        response_type = "buttons" if node_type == "menu" else "dropdown"
        # Option *labels* are deliberately left alone. A label is not only display text:
        # `_option_text` stores it as the visitor's answer and `_effective_message` hands it
        # to an AI Fallback as the question, so substituting here would quietly change three
        # things at once — including what a knowledge base gets searched for.
        options = [
            {"label": o.get("label", ""), "value": o.get("id", o.get("value", ""))}
            for o in data.get("options", [])
        ]
        return FlowEngineResult(
            type=response_type,
            text=_render_text(data.get("prompt_text"), variables),
            options=options,
        )
    if node_type == "send_message":
        return FlowEngineResult(
            type="text", text=_render_text(data.get("message_text"), variables),
        )

    # Unknown node type — sign off rather than return an empty "text", which
    # the widget would render as a blank chat bubble.
    return FlowEngineResult(type="text", text=_DEFAULT_END_MESSAGE)


# --------------------------------------------------------------------------
# Internal-hop loop — one step per node type, each either advances the
# session in place and returns None (loop continues) or returns the
# turn's FlowEngineResult (loop stops).
# --------------------------------------------------------------------------

def _message_text(node: dict, variables: Optional[dict] = None) -> str:
    """
    A node's configured message, interpolated and normalized — "" for unset/whitespace-only.

    Interpolating *before* the emptiness test matters in one case and it is the right way
    round: a message that is nothing but ``{{VALUE}}`` for a value this conversation never
    set stays non-empty, because `_render_text` leaves the placeholder standing. The
    operator sees which name is wrong instead of a Send Message block that silently does
    nothing.
    """
    return _render_text((node.get("data") or {}).get("message_text"), variables).strip()


def _with_download(
    session: ChatbotFlowSession, result: FlowEngineResult,
) -> FlowEngineResult:
    """
    Attach the download button a Download File block produced this turn, if any.

    **Why it is attached here rather than returned by the block.** A Download File block
    does not end the turn — it does its work and hops on, like an Email block — so whatever
    ends the turn is a *later* block, and it knows nothing about a button. Threading the
    payload through every handler in between would put a parameter nobody else uses in
    eight signatures.

    So the block leaves it on the session object and this reads it off at the single point
    a turn is returned. A plain attribute rather than a column, for the reason
    ``_email_queued`` gives: it is true for the rest of this turn only, and a column would
    be state nobody ever reads twice.

    The **last** block wins if two ran in one turn. That is the honest reading of a drawing
    with two Download File blocks in a row: the second one is what the operator most
    recently told the visitor about, and drawing two buttons for one message is not
    something either block claims to do.
    """
    payload = getattr(session, "_file_download", None)

    if payload is None:
        return result

    session._file_download = None  # noqa: SLF001
    result.file_download = payload

    return result


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
    message_text = _message_text(node, session.variables)
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
    return _visitor_facing_result(node, session.variables)


async def _step_end(
    db: AsyncSession,
    session: ChatbotFlowSession,
    node: dict,
    root_flow: ChatbotFlow,
    flow_cache: dict,
) -> Optional[FlowEngineResult]:
    """
    Explicit flow terminator — or, inside a sub-flow, an explicit **return**.

    In the root flow this is unchanged: an End node always completes the session (it never
    has an outgoing edge, enforced by ``flow_service._validate_graph``) and signs off in the
    same turn. It must never continue or replay the flow — the visitor chose to finish — and
    an End node with no message of its own falls back to _DEFAULT_END_MESSAGE so the goodbye
    still lands.

    Inside a Run Flow call it means the callee is done, and the *conversation* is not. The
    call is closed, the caller's variables come back with the returned values merged in, and
    the caller carries on from the block after its Run Flow one. Two cases, split exactly
    the way ``_step_send_message`` splits a blank message:

    * **it has a message** — that message is this turn's reply and the caller resumes on the
      visitor's next one. An operator wrote that text; a sub-flow's ending is a real thing
      to say, and discarding it because the flow happened to be called rather than attached
      would make the same block behave differently in two places for no reason the operator
      can see.
    * **it is blank** — nothing is said and the hop loop carries straight on into the
      caller, in this same turn.

    The message is rendered against the **callee's** variables, before the call is closed:
    it is the callee's own text and refers to the callee's own names.
    """
    session.current_node_id = node["id"]

    if not subflow_service.in_subflow(session):
        return _end_of_flow(session, _message_text(node, session.variables))

    message = _message_text(node, session.variables)
    outcome = await _return_from_call(db, session, root_flow, flow_cache)

    if message:
        return FlowEngineResult(type="text", text=message)
    return outcome


async def _return_from_call(
    db: AsyncSession,
    session: ChatbotFlowSession,
    root_flow: ChatbotFlow,
    flow_cache: dict,
    port: str = "default",
) -> Optional[FlowEngineResult]:
    """
    Close the innermost Run Flow call and put the session back on the caller, past its
    Run Flow block.

    Returning to the *node after* the Run Flow block rather than to the block itself is what
    keeps ``_step_run_flow`` unambiguous: arriving at a Run Flow node always means "make the
    call", so there is no "am I going in or coming out" state for anything else to get
    wrong.

    ``port`` is ``"default"`` for a call that finished and ``"error"`` for one that could
    not run or could not be resumed. A caller with no edge on that port ends the
    conversation, which is `_advance_or_complete`'s ordinary behaviour — never a silent hop
    to the other port, the rule ``_step_run_graph`` states.

    ``None`` keeps the hop loop going, now in the caller's graph.
    """
    return_node_id, _callee_variables = subflow_service.pop(session)

    caller = await subflow_service.current_flow(db, session, root_flow, flow_cache)
    if caller is None or not return_node_id:
        # The caller's own flow has been deleted from under a live conversation, or the
        # frame was written without a return node. Nothing to go back to.
        return _end_of_flow(session)

    node = _find_node(caller.graph_data, return_node_id)
    if node is None:
        # The Run Flow block was deleted while the visitor was inside the call it started.
        # `_session_needs_restart` catches an edited *root* flow between turns; this is the
        # same situation one level down and inside a single turn.
        return _end_of_flow(session)

    edge = _find_edge(caller.graph_data, return_node_id, port)
    if not _advance_or_complete(session, edge, return_node_id):
        return _end_of_flow(session)
    return None


async def _failed_step(
    db: AsyncSession,
    session: ChatbotFlowSession,
    graph_data: dict,
    node: dict,
    root_flow: Optional[ChatbotFlow],
    flow_cache: Optional[dict],
    message: str = "",
) -> Optional[FlowEngineResult]:
    """
    One step failed. Where the conversation goes, in the only order that can be honest.

    1. **The block's own ``error`` port**, if the operator drew one. Their route for this.
    2. **The enclosing call's ``failed`` port**, if this flow is running as a sub-flow. A
       step that failed with no route of its own means the *call* failed, and the caller has
       a port for exactly that — so the failure crosses the boundary instead of stopping at
       it. Without this a callee that broke would return through the caller's ``done`` edge
       and the caller would carry on as though it had worked, which is the thing every
       failure path in this file exists to prevent.
    3. **Sign off**, in the root flow with nowhere left to go.

    Never the ``default``/``done`` port at any level.
    """
    error_edge = _find_edge(graph_data, node["id"], "error")
    if error_edge:
        session.current_node_id = error_edge["target"]
        return None

    if root_flow is not None and subflow_service.in_subflow(session):
        return await _fail_call(
            db, session, root_flow, flow_cache if flow_cache is not None else {},
            f"a {node.get('type')} block inside it failed with no error route of its own",
        )

    return _end_of_flow(session, message)


async def _fail_call(
    db: AsyncSession,
    session: ChatbotFlowSession,
    root_flow: ChatbotFlow,
    flow_cache: dict,
    reason: str,
) -> Optional[FlowEngineResult]:
    """
    Abandon the innermost call and leave the caller by its ``error`` port.

    Used for the two things that can go wrong to a call *already in progress*: the flow it
    was running has been deleted, or the node it was parked on no longer exists because that
    flow was edited. Both are the operator's doing rather than the visitor's, so the reason
    is logged in full and the conversation takes a route the operator drew.
    """
    logger.warning(
        "Abandoning a Run Flow call in session %s: %s", session.id, reason,
    )
    return await _return_from_call(db, session, root_flow, flow_cache, port="error")


async def _step_ai_fallback(
    db: AsyncSession,
    chatbot_key: ChatbotApiKey,
    flow_id: int,
    graph_data: dict,
    session: ChatbotFlowSession,
    node: dict,
    incoming_message: Optional[str],
    from_selection: bool = False,
) -> FlowEngineResult:
    """
    Hand this turn to the AI, say what it answered, and keep that answer.

    The answer is **both** sent to the visitor and stored under the node's variable name
    if it has one — this node is the one place those two are the same thing. That is what
    lets a later Email node mail what the AI worked out ("email me the data"): the email's
    binding reads the conversation's variables, and the answer is now one of them. It is
    also what lets an If/Else branch on whether the AI managed to answer at all.

    The turn **ends here**, so a Send Email node wired after this one runs on the
    visitor's *next* message, not this one. That is not a special case to work around: the
    variable lives on the session row, so it is still there a turn later. A flow wanting
    the email sent without another visitor message puts the Email node before this one and
    mails something the conversation already collected.

    What gets stored is the whole answer as text — see :func:`_ai_answer_text` — not the
    ``AnalyticsResult``: a flow variable is a string, and the insights and table are the
    part somebody asking to be emailed the data actually wanted.

    A node with no outgoing edge is a deliberate dead end, and this is the one place
    that matters: it is not just where the turn completes, it is also where the visitor's
    *next* message comes back to (see ``_continue_dead_end_ai_fallback``). On a dead-end
    turn only, this also reads and updates ``session.dead_end_ai_context`` — a rolling
    one-answer memory so the conversation past the dead end stays coherent instead of
    every message being answered from scratch. A connected node never touches that field.
    """
    edge = _find_edge(graph_data, node["id"], "default")
    is_dead_end = edge is None
    _advance_or_complete(session, edge, node["id"])

    previous_answer = (
        (session.dead_end_ai_context or {}).get(node["id"]) if is_dead_end else None
    )
    try:
        ai_result = await ai_fallback_service.run_ai_fallback(
            db, chatbot_key, flow_id, node["id"], node.get("data") or {}, incoming_message or "",
            from_selection=from_selection,
            session_variables=dict(session.variables or {}),
            previous_answer=previous_answer,
        )
    except HTTPException as exc:
        # Nothing is stored. The variable stays *absent* rather than being set to the
        # error sentence, so a later Email node falls back to the template's declared
        # default (or refuses the send if the variable was required) instead of mailing
        # a customer an internal failure message as though it were the answer. An
        # If/Else on the variable reads absent as empty, so "did the AI answer?" still
        # branches correctly. `dead_end_ai_context` is left untouched for the same
        # reason — a failed turn has nothing worth remembering for the next one.
        logger.warning(
            "AI Fallback node %s in flow session %s could not answer: %s",
            node.get("id"),
            session.id,
            exc.detail,
        )
        return FlowEngineResult(type="text", text=str(exc.detail))

    answer_text = _ai_answer_text(ai_result)
    _store_answer(session, node, answer_text)

    if is_dead_end:
        session.dead_end_ai_context = {
            **(session.dead_end_ai_context or {}), node["id"]: answer_text,
        }

    # And the table separately, in its own shape. The variable holds the whole answer as
    # *text* — which is what an email or a chat bubble wants — and a CSV wants columns and
    # rows, so the same answer is kept twice in the two forms the two consumers need. A
    # single stored form would mean one of them parsing the other's, which for a pipe-
    # separated block of prose is guessing at somebody's data.
    if ai_result.table and (ai_result.table.columns or ai_result.table.rows):
        _store_node_result(session, node, _table_result(ai_result.table))

    return FlowEngineResult(
        type="text",
        text=ai_result.summary,
        insights=ai_result.insights or [],
        table=ai_result.table.model_dump() if ai_result.table else None,
    )


#: How many table rows the stored answer keeps. A variable is text that gets mailed or
#: put in a chat bubble, not a result set, and the rows beyond this are summarised by a
#: count rather than dropped silently — the same honesty rule ``_store_graph_result``
#: applies to a preview versus a real total.
_MAX_STORED_TABLE_ROWS = 20


def _table_as_text(table) -> str:  # noqa: ANN001 — AnalyticsTable, imported lazily by caller
    """One ``AnalyticsTable`` as pipe-separated plain text, capped and honest about it."""
    rows = list(table.rows or [])
    lines = [" | ".join(str(column) for column in table.columns or [])]
    lines += [
        " | ".join("" if cell is None else str(cell) for cell in row)
        for row in rows[:_MAX_STORED_TABLE_ROWS]
    ]

    dropped = len(rows) - _MAX_STORED_TABLE_ROWS
    if dropped > 0:
        lines.append(f"(+{dropped} more rows)")

    return "\n".join(lines)


def _ai_answer_text(result) -> str:  # noqa: ANN001 — AnalyticsResult
    """
    One AI Fallback answer as the single block of text a variable can hold.

    The whole answer, not just the narrative: the visitor who picked "email me the data"
    means the figures, and a variable holding only the summary would mail them a sentence
    about a table they never received. So the summary, then the insights as a bullet list,
    then the table as pipe-separated rows — in the order the widget draws them, so the
    email and the chat bubble say the same thing in the same sequence.

    Plain text with newlines rather than markup, for the reason ``rendering.py`` gives: an
    email's HTML body escapes every value it substitutes, so markup smuggled in through a
    variable would arrive as visible tag soup. Formatting belongs in the template, where
    whoever reviews the template can see it.

    Empty for an answer with nothing in it, which ``_store_answer`` stores as an empty
    string — an If/Else ``not_empty`` on the variable is then false, which is true.
    """
    parts: List[str] = []

    summary = (result.summary or "").strip()
    if summary:
        parts.append(summary)

    parts += [
        f"- {text}"
        for text in ((str(insight) or "").strip() for insight in result.insights or [])
        if text
    ]

    if result.table and (result.table.columns or result.table.rows):
        parts.append(_table_as_text(result.table))

    return "\n".join(parts).strip()


async def _step_run_graph(
    db: AsyncSession,
    chatbot_key: ChatbotApiKey,
    graph_data: dict,
    session: ChatbotFlowSession,
    node: dict,
    root_flow: Optional[ChatbotFlow] = None,
    flow_cache: Optional[dict] = None,
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
        return await _failed_step(
            db, session, graph_data, node, root_flow, flow_cache,
            "Sorry — something went wrong working that out. Please try again later.",
        )

    _store_graph_result(session, node, outcome)

    # The run's *id*, not its rows. A Create File block pointing at this block re-reads the
    # whole result through `graph_runner.full_result` at file time, because `outcome.rows`
    # is a twenty-row preview and a file made from it would be a twenty-row file with
    # nothing about it saying so. The total travels along so an impossible file can be
    # refused before a single row is read back.
    _store_node_result(
        session,
        node,
        {
            "kind": "graph_run",
            "run_id": str(outcome.run_id or ""),
            "total_rows": int(outcome.total_rows or 0),
        },
    )

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
    db: AsyncSession,
    chatbot_key: ChatbotApiKey,
    session: ChatbotFlowSession,
    graph_data: dict,
    incoming_message: Optional[str],
    root_flow: Optional[ChatbotFlow] = None,
    flow_cache: Optional[dict] = None,
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
        if node is None:
            return _end_of_flow(
                session,
                "Sorry — something went wrong working that out. Please try again later.",
            )
        return await _failed_step(
            db, session, graph_data, node, root_flow, flow_cache,
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



async def _step_send_email(
    db: AsyncSession,
    chatbot_key: ChatbotApiKey,
    graph_data: dict,
    session: ChatbotFlowSession,
    node: dict,
    root_flow: Optional[ChatbotFlow] = None,
    flow_cache: Optional[dict] = None,
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
        return await _failed_step(db, session, graph_data, node, root_flow, flow_cache)

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


async def _step_create_file(
    db: AsyncSession,
    chatbot_key: ChatbotApiKey,
    graph_data: dict,
    session: ChatbotFlowSession,
    node: dict,
    root_flow: Optional[ChatbotFlow] = None,
    flow_cache: Optional[dict] = None,
) -> Optional[FlowEngineResult]:
    """
    Write a file out of what an earlier block produced, and hop on without saying anything.

    **Nothing is said to the visitor**, the same call ``_step_send_email`` and a quietly
    finished ``run_graph`` both make: making a file is a step in a conversation, not a
    message in it. What the block leaves behind is the file's path under its variable name,
    and — separately — the file itself for a Download File block to hand over.

    The file *name* is interpolated from the conversation's variables before it is written,
    so ``invoice-{{ORDER_REF}}`` becomes ``invoice-10432.csv``. That is what makes a file
    per visitor possible; without it every conversation would produce a file called the
    same thing. Interpolation happens here rather than in the runner because
    ``_render_text`` is this module's, and its "leave an unknown placeholder standing"
    semantics are deliberate and documented — a file called ``invoice-{{ORDER_REF}}.csv``
    is a visible mistake, where a file called ``invoice-.csv`` is a silent one.

    On failure: the ``error`` port if one is drawn, otherwise the enclosing call's
    ``failed`` port, otherwise sign off — all three through ``_failed_step``. Never a
    silent hop onward to a Download File block that would then have nothing to offer.
    """
    from app.services.file_delivery.nodes import flow_builder_runner

    prepared = dict(node)
    prepared["data"] = {
        **(node.get("data") or {}),
        "file_name": _render_text(
            (node.get("data") or {}).get("file_name"), session.variables,
        ),
    }

    try:
        written = await flow_builder_runner.run_create_file_node(
            db, prepared, chatbot_key=chatbot_key, session=session,
        )
    except Exception as exc:  # noqa: BLE001 — routed, not raised
        logger.warning(
            "Create File node %s in flow session %s failed: %s",
            node.get("id"),
            session.id,
            flow_builder_runner.wrap_failure(exc),
        )
        return await _failed_step(db, session, graph_data, node, root_flow, flow_cache)

    # The path under the block's variable name, so an operator can put it in a log line or
    # an email. The *link* is the Download File block's business — a path is a fact about
    # this server and is no use to a visitor, and handing one out in a chat bubble would
    # tell them where the file lives without letting them fetch it.
    _store_answer(session, node, written["file_path"])

    # And the file itself, for a Download File block naming this one. Keyed by node id like
    # every other block result, so the Download File block finds it by pointing at the box
    # rather than by guessing at a variable name.
    _store_node_result(
        session, node,
        {
            "kind": "file",
            "file_uuid": written["file_uuid"],
            "sequence": _next_file_sequence(session),
        },
    )

    edge = _find_edge(graph_data, node["id"], "default")
    if not _advance_or_complete(session, edge, node["id"]):
        return _end_of_flow(session)
    return None


async def _step_download_file(
    db: AsyncSession,
    chatbot_key: ChatbotApiKey,
    graph_data: dict,
    session: ChatbotFlowSession,
    node: dict,
    root_flow: Optional[ChatbotFlow] = None,
    flow_cache: Optional[dict] = None,
) -> Optional[FlowEngineResult]:
    """
    Hand over a file an earlier Create File block wrote.

    **The link always goes into the variable; the button is optional.** With the button
    switched off this block says nothing at all — the Email node's rule — and the operator
    writes their own sentence with a Send Message block (*"Your file is ready:
    {{FILE_URL}}"*) or mails the link. With it switched on, the widget draws a button under
    whatever the turn says, in the operator's words and colour.

    **The button does not end the turn.** It is attached to whatever result the turn
    eventually produces (see :func:`_with_download`), so a Send Message block after this one
    still speaks and a Menu after it still offers its options — the button appears under
    them rather than instead of them.

    Which file is handed over comes from the *named* Create File block, not from the wire:
    an operator may put a Send Message between the two, and a named reference survives that
    while "the block wired into me" does not.

    **More than one Create File block may be named.** A menu offering CSV / XLSX / Parquet,
    each option running its own Create File block, can share one Download File block rather
    than needing one per branch — the alternative is a Download File block per format,
    which is both more drawing and more to keep in sync. When several of the named blocks
    have run in the conversation, the one whose file was written **most recently** —
    `sequence` on its stored result, not dict order, which a JSONB round trip does not
    guarantee — is the one handed over.
    """
    from app.services.file_delivery.nodes import flow_builder_runner

    data = node.get("data") or {}
    raw_source = data.get("create_file_node_id")
    # A single id is the older shape, still saved by flows from before a Download File
    # block could name more than one — read as a one-item list rather than migrated, so
    # those flows keep working untouched.
    source_ids = (
        [str(s).strip() for s in raw_source if str(s or "").strip()]
        if isinstance(raw_source, list)
        else ([str(raw_source).strip()] if str(raw_source or "").strip() else [])
    )

    results = session.node_results or {}
    candidates = [
        (source_id, results[source_id])
        for source_id in source_ids
        if isinstance(results.get(source_id), dict) and results[source_id].get("file_uuid")
    ]

    if not candidates:
        # None of the Create File blocks it names has run in this conversation — every
        # branch took another route, or the blocks are wired the wrong way round. Said out
        # loud on the failure port rather than skipped: a Download File block that quietly
        # does nothing is a button an operator drew and never sees, with nothing to explain
        # why.
        logger.warning(
            "Download File node %s in flow session %s has no file: none of the named "
            "Create File blocks %r has run in this conversation",
            node.get("id"),
            session.id,
            source_ids,
        )
        return await _failed_step(db, session, graph_data, node, root_flow, flow_cache)

    _source_id, record = max(candidates, key=lambda pair: pair[1].get("sequence", 0))
    file_uuid = str(record.get("file_uuid") or "")

    prepared = dict(node)
    prepared["data"] = {
        **data,
        # Interpolated for the same reason the file name is: *Download {{ORDER_REF}}* is a
        # button label worth having, and an unknown placeholder left standing is a visible
        # mistake rather than a blank word.
        "button_text": _render_text(data.get("button_text"), session.variables),
    }

    try:
        offered = await flow_builder_runner.run_download_file_node(
            db, prepared, chatbot_key=chatbot_key, session=session, file_uuid=file_uuid,
        )
    except Exception as exc:  # noqa: BLE001 — routed, not raised
        logger.warning(
            "Download File node %s in flow session %s failed: %s",
            node.get("id"),
            session.id,
            flow_builder_runner.wrap_failure(exc),
        )
        return await _failed_step(db, session, graph_data, node, root_flow, flow_cache)

    _store_answer(session, node, offered["url"])

    if offered["button"] is not None:
        # Left on the session for `_with_download` to attach to whatever ends the turn.
        session._file_download = offered["button"]  # noqa: SLF001

    edge = _find_edge(graph_data, node["id"], "default")
    if not _advance_or_complete(session, edge, node["id"]):
        return _end_of_flow(session)
    return None


async def _step_run_flow(
    db: AsyncSession,
    chatbot_key: ChatbotApiKey,
    graph_data: dict,
    session: ChatbotFlowSession,
    node: dict,
    root_flow: Optional[ChatbotFlow] = None,
    flow_cache: Optional[dict] = None,
) -> Optional[FlowEngineResult]:
    """
    Run another flow as one step of this one, and carry straight on into it.

    The Azure Data Factory *Execute Pipeline* shape, for conversations: values are passed
    in, the callee runs as an ordinary flow — it may ask questions, show menus, hand a turn
    to the AI — and named values come back. What makes it a *call* rather than a jump is the
    frame ``subflow_service.push`` leaves behind: where to come back to, and whose variables
    to restore when we do.

    **Nothing is said to the visitor here, and no turn is spent.** This returns ``None`` and
    the hop loop simply finds itself in a different graph on its next iteration — which is
    why entering a call needs no special case in the loop, in `_persist_session`, or in the
    widget. The callee's own blocks decide what the visitor sees, exactly as they would if
    the flow had been attached to the chatbot directly.

    Everything that can refuse the call routes to the ``error`` port if one is drawn and
    signs off if not — never a silent hop to ``default``, the rule ``_step_run_graph``
    states: a flow carrying on as though a step had succeeded is how a visitor gets told
    something that is not true. Five things can refuse it, and each is knowable *now*: no
    flow chosen, a flow since deleted, a flow since unpublished, a flow belonging to somebody
    else, and a call that would loop or nest too deep.
    """
    data = node.get("data") or {}
    target_uuid = subflow_service.parse_flow_uuid(data)

    child = (
        await flow_service.get_flow_by_uuid_for_run(db, target_uuid)
        if target_uuid is not None
        else None
    )

    reason = _run_flow_refusal(chatbot_key, session, target_uuid, child)
    if reason is None:
        try:
            start_node = _find_start_node(child.graph_data)
        except HTTPException:
            # A flow with no Start node cannot be entered. Saving one is refused by
            # `_validate_graph`, so this is a flow whose graph predates that rule or was
            # written directly — treated as a failed call rather than allowed to raise
            # through a live conversation.
            reason = f"the flow {child.name} has no Start block"

    if reason is not None:
        logger.warning(
            "Run Flow node %s in session %s could not run: %s",
            node.get("id"), session.id, reason,
        )
        return await _failed_step(db, session, graph_data, node, root_flow, flow_cache)

    inputs = await subflow_service.resolve_inputs(db, chatbot_key, data, session.variables)

    # Order matters: `push` reads the caller's variables off the session before replacing
    # them, so the inputs have to be resolved from the caller's scope first.
    subflow_service.push(session, node, child, inputs)
    session.current_node_id = start_node["id"]
    return None


def _run_flow_refusal(
    chatbot_key: ChatbotApiKey,
    session: ChatbotFlowSession,
    target_uuid,  # noqa: ANN001 — Optional[uuid.UUID]
    child: Optional[ChatbotFlow],
) -> Optional[str]:
    """Why this Run Flow block cannot run, as a sentence for the log, or None."""
    if target_uuid is None:
        return "no flow is chosen on the block"
    if child is None:
        return "the flow it points at no longer exists"
    if int(child.user_id) != int(chatbot_key.user_id):
        # The runtime lookup does no ownership check by design — `_assert_run_flow_targets`
        # does it at save time. This is the belt to that braces: a graph_data written or
        # copied outside the save path must not be able to run somebody else's flow.
        return "the flow it points at belongs to another account"
    if child.kind != flow_service.KIND_GENERIC:
        # Marked as an agent's own conversation since this block was saved. Refused rather
        # than run: an agent flow is somebody's live front door, with its own visitors, and
        # running it as a child would put two callers inside one drawing for two different
        # reasons. The save-time check said the same thing; a flow's kind can change after a
        # block referencing it was saved, which is why both exist.
        return f"the flow {child.name} is an agent flow, not a generic one"
    if not child.is_active:
        return f"the flow {child.name} is not published"
    return subflow_service.guard(session, child)


async def _run_one_hop(
    db: AsyncSession,
    chatbot_key: ChatbotApiKey,
    flow_id: int,
    graph_data: dict,
    session: ChatbotFlowSession,
    node: dict,
    incoming_message: Optional[str],
    from_selection: bool = False,
    root_flow: Optional[ChatbotFlow] = None,
    flow_cache: Optional[dict] = None,
) -> Optional[FlowEngineResult]:
    """
    Run one node. Returns None to keep looping, else the turn's terminal result.

    ``flow_id``/``graph_data`` are the flow being interpreted *right now*, which is not
    necessarily the one attached to the chatbot — see `_run_internal_hops`. Two handlers need
    the root flow as well, and only because they can change which flow that is: an End node
    inside a call has to get back to the caller, and a Run Flow node has to enter a callee.
    """
    node_type = node.get("type")

    continue_handler = _CONTINUE_STEP_HANDLERS.get(node_type)
    if continue_handler:
        return continue_handler(graph_data, session, node)

    if node_type in _AWAITING_NODE_TYPES:
        return _step_awaiting(session, node)

    if node_type == "send_message":
        return _step_send_message(graph_data, session, node)

    if node_type == "end":
        return await _step_end(
            db, session, node, root_flow, flow_cache if flow_cache is not None else {},
        )

    if node_type == "run_flow":
        return await _step_run_flow(
            db, chatbot_key, graph_data, session, node, root_flow, flow_cache,
        )

    if node_type == "ai_fallback":
        return await _step_ai_fallback(
            db, chatbot_key, flow_id, graph_data, session, node, incoming_message,
            from_selection=from_selection,
        )

    if node_type == "run_graph":
        return await _step_run_graph(
            db, chatbot_key, graph_data, session, node, root_flow, flow_cache,
        )

    if node_type == "send_email":
        return await _step_send_email(
            db, chatbot_key, graph_data, session, node, root_flow, flow_cache,
        )

    if node_type == "create_file":
        return await _step_create_file(
            db, chatbot_key, graph_data, session, node, root_flow, flow_cache,
        )

    if node_type == "download_file":
        return await _step_download_file(
            db, chatbot_key, graph_data, session, node, root_flow, flow_cache,
        )

    # Unknown/unsupported node type reached at runtime — end gracefully.
    return _end_of_flow(session)


async def _run_internal_hops(
    db: AsyncSession,
    chatbot_key: ChatbotApiKey,
    root_flow: ChatbotFlow,
    session: ChatbotFlowSession,
    incoming_message: Optional[str],
    from_selection: bool = False,
    flow_cache: Optional[dict] = None,
) -> FlowEngineResult:
    """
    Walk the graph until something ends the turn, and carry any download button out with it.

    The wrapper exists for the button and nothing else. A Download File block does not end
    the turn — it leaves its payload on the session and hops on — so *something later*
    returns the result the button has to ride on, and every one of those returns is inside
    the loop below. Attaching here means each of them does not have to remember to.

    This is also the only path that can produce one: the parked-node paths in
    `advance_flow_session` answer a prompt or a graph's question without running any block.
    """
    return _with_download(
        session,
        await _hop_until_the_turn_ends(
            db, chatbot_key, root_flow, session, incoming_message,
            from_selection=from_selection, flow_cache=flow_cache,
        ),
    )


async def _hop_until_the_turn_ends(
    db: AsyncSession,
    chatbot_key: ChatbotApiKey,
    root_flow: ChatbotFlow,
    session: ChatbotFlowSession,
    incoming_message: Optional[str],
    from_selection: bool = False,
    flow_cache: Optional[dict] = None,
) -> FlowEngineResult:
    """
    Walk the graph until something ends the turn.

    **Which graph is re-decided on every hop**, from the session's Run Flow call stack, and
    that is the whole of how sub-flows work: `_step_run_flow` pushes a frame and points the
    session at another flow's Start node, `_step_end` pops one, and this loop simply notices
    on its next iteration that it is somewhere else. No other function in this file has to
    know that more than one flow exists — including `_step_ai_fallback`, which gets the
    *current* flow's id and so keeps resolving a sub-flow's own knowledge bases (keyed on
    flow id and node id) correctly.

    ``flow_cache`` makes that cheap: re-resolving on every hop costs one query per distinct
    flow per turn, not one per hop.
    """
    hops = 0
    if flow_cache is None:
        flow_cache = {}

    while True:
        hops += 1

        if hops > _MAX_INTERNAL_HOPS:
            session.status = "completed"
            return FlowEngineResult(
                type="text",
                text="Something went wrong continuing this conversation. Let's start over.",
            )

        flow = await subflow_service.current_flow(db, session, root_flow, flow_cache)
        if flow is None:
            # The frame names a flow that has been deleted while this visitor was inside it.
            result = await _fail_call(
                db, session, root_flow, flow_cache,
                "the flow it was running has been deleted",
            )
            if result is not None:
                return result
            continue

        node = _find_node(flow.graph_data, session.current_node_id)
        if node is None:
            # Walked off the graph. In a call that is a failed call — the callee was edited
            # under a parked visitor — and the caller's `error` port decides what happens.
            # In the root flow it is the end, as it has always been.
            if subflow_service.in_subflow(session):
                result = await _fail_call(
                    db, session, root_flow, flow_cache,
                    "the block it was waiting on no longer exists in that flow",
                )
                if result is not None:
                    return result
                continue
            return _end_of_flow(session)

        result = await _run_one_hop(
            db, chatbot_key, flow.id, flow.graph_data, session, node, incoming_message,
            from_selection=from_selection, root_flow=root_flow, flow_cache=flow_cache,
        )
        if result is None:
            continue

        if subflow_service.in_subflow(session) and session.status == "completed":
            # The callee reached the end of its path without an End block — its last block
            # simply had no outgoing edge, which is how most flows are actually drawn. That
            # is the end of the **call**, not of the conversation, so the frame is closed
            # and the caller carries on.
            #
            # Whether the visitor is told anything depends on what the callee produced: its
            # own text (a final Send Message) is said, and the caller resumes on the next
            # turn; the generic sign-off is not, because "Goodbye!" in the middle of a
            # conversation that is still going is a lie about what just happened.
            session.status = "active"
            said_something = bool(result.text) and result.text != _DEFAULT_END_MESSAGE
            returned = await _return_from_call(db, session, root_flow, flow_cache)

            if said_something:
                return result
            if returned is not None:
                return returned
            continue

        return result
