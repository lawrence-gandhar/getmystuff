"""
The Run Flow block's call stack — running one flow as a step of another.

``engine_service.py`` interprets *a* graph against *a* session. This module is what lets
that graph change mid-conversation: it owns the stack of calls a visitor is inside, the
variable scope each call gets, and the two guards that stop a flow calling itself forever.
Kept separate for the reason ``ai_fallback_service.py`` is: that file's concern is "which
node runs next", and this one's is "which flow are we in and what does it know".

**A frame, not a new session.** ``chatbot_flow_sessions`` is unique on
``(chatbot_key_id, session_token)`` — one row per visitor — and a sub-flow that can ask a
question has to survive between two HTTP requests. So the state lives in a JSONB
``call_stack`` column on that one row, outermost call first, the same way
``awaiting_graph_run`` parks a Graph Designer run between two turns.

**Every call gets its own variables.** On the way in, the caller's map is put in the frame
and the callee starts with the resolved inputs *and nothing else*; on the way out the
caller's map comes back with the named outputs merged into it. That isolation is the
feature, not a detail of it: without it a callee writes into its caller's namespace, two
calls to the same flow overwrite each other's answers, and a reusable flow's internal
variable names become part of its contract. The visible consequence is that an Email block
inside a sub-flow sees only that sub-flow's variables, which is the same rule and not a
special case.
"""

import logging
import uuid as uuid_pkg
from typing import Any, Dict, List, Mapping, Optional, Tuple

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chatbot import ChatbotApiKey
from app.models.flow_builder import ChatbotFlow, ChatbotFlowSession
from app.services.flow_builder import flow_service

logger = logging.getLogger(__name__)

#: How many Run Flow calls may be open at once. A conversation nested five deep is already
#: past what anybody can follow on a canvas, and the ceiling exists to turn a runaway into
#: a sentence rather than a hang. `_MAX_INTERNAL_HOPS` would eventually stop it either way,
#: but it would report a cycle as "something went wrong continuing this conversation",
#: which tells the operator nothing about what to fix.
MAX_CALL_DEPTH = 5

# Frame keys, named rather than spelled out at each use — a frame is JSONB read back by a
# later request, so a typo in one place and not another would be a bug that only appears on
# the second turn of a sub-flow.
_FLOW_ID = "flow_id"
_RETURN_NODE = "return_node_id"
_CALLER_VARIABLES = "caller_variables"
_OUTPUTS = "outputs"


# --------------------------------------------------------------------------
# Reading the stack
# --------------------------------------------------------------------------

def stack(session: ChatbotFlowSession) -> List[dict]:
    """The session's frames as a plain list — never None, so callers need no guard."""
    return list(session.call_stack or [])


def depth(session: ChatbotFlowSession) -> int:
    return len(stack(session))


def in_subflow(session: ChatbotFlowSession) -> bool:
    return bool(stack(session))


def running_flow_ids(session: ChatbotFlowSession) -> List[int]:
    """
    Every flow already running in this conversation, for the cycle check.

    **Including the root flow**, which is on no frame — ``session.flow_id`` is where the
    conversation started and nothing pushed it. Leaving it out is the difference between
    refusing A → B → A at the second A and refusing it one level later at the second B: both
    terminate, but only the first names the flow the operator has to look at.
    """
    ids = [int(frame.get(_FLOW_ID) or 0) for frame in stack(session)]
    if session.flow_id:
        ids.append(int(session.flow_id))
    return ids


async def current_flow(
    db: AsyncSession,
    session: ChatbotFlowSession,
    root_flow: ChatbotFlow,
    cache: Dict[int, ChatbotFlow],
) -> Optional[ChatbotFlow]:
    """
    The flow whose graph is being interpreted right now.

    The root flow when the stack is empty, otherwise the flow named by the innermost frame.
    ``cache`` is a per-turn dict keyed on the internal flow id, so re-resolving this at the
    top of every hop costs one query per distinct flow per turn rather than one per hop —
    the engine's loop asks on each iteration precisely so that entering or leaving a
    sub-flow needs no special case anywhere else.

    ``None`` means the frame names a flow that has since been deleted. The caller treats
    that as a failed call rather than raising: a conversation must not break because
    somebody tidied up the flow library while a visitor was inside one.
    """
    frames = stack(session)
    if not frames:
        return root_flow

    flow_id = int(frames[-1].get(_FLOW_ID) or 0)
    if flow_id == root_flow.id:
        # A flow can legitimately appear on the stack as the root's own callee only via a
        # cycle, which `guard` refuses — but resolving it from the cache keeps this function
        # total rather than relying on that.
        return root_flow
    if flow_id in cache:
        return cache[flow_id]

    flow = await flow_service.get_flow_by_id_for_run(db, flow_id)
    if flow is not None:
        cache[flow_id] = flow
    return flow


# --------------------------------------------------------------------------
# Entering a call
# --------------------------------------------------------------------------

def guard(session: ChatbotFlowSession, child_flow: ChatbotFlow) -> Optional[str]:
    """
    Why this call must not be made, or None.

    Two refusals, both returning the sentence the operator should see. A **cycle** — a flow
    already open further up the stack — is refused rather than depth-limited, because five
    passes through the same loop before stopping is five sets of the same messages sent to
    a visitor. **Depth** catches the legitimate-but-runaway case: chains that are each
    fine and collectively too deep to follow.
    """
    if child_flow.id in running_flow_ids(session):
        return (
            f"The flow {child_flow.name} is already running further up this conversation, "
            "so running it again here would loop forever."
        )
    if depth(session) >= MAX_CALL_DEPTH:
        return (
            f"This conversation is already {MAX_CALL_DEPTH} flows deep, which is as far as "
            "one flow may call another."
        )
    return None


async def resolve_inputs(
    db: AsyncSession,
    chatbot_key: ChatbotApiKey,
    node_data: Mapping[str, Any],
    caller_variables: Mapping[str, Any],
) -> Dict[str, str]:
    """
    The variables the callee starts with.

    Three sources, matching what a flow's Email node offers because a flow has exactly the
    same things to offer: a value the conversation collected, one of the agent's prompt
    variables from the Agents section, or a fixed value.

    **Deliberately not ``variable_sources.resolve_bindings``**, though the binding shape is
    the same. That resolver upper-cases the destination name, which is right for
    ``{{CUSTOMER}}`` in an email template and wrong here: a flow variable is whatever an
    operator typed into a "store this in" field, every other block treats ``email`` and
    ``EMAIL`` as two different variables, and folding the case would hand the callee a name
    it never reads. Reusing it would be reuse of the wrong half.

    A binding that resolves to nothing is **left out** rather than passed as ``""``, so the
    callee's own blocks see an unset variable — which an If/Else reads as empty and an
    Ask-for-Input can then fill. Passing an empty string would look identical to a visitor
    having answered with one.
    """
    agent_variables: Optional[Dict[str, str]] = None
    resolved: Dict[str, str] = {}

    for raw_name, binding in (node_data.get("inputs") or {}).items():
        name = str(raw_name).strip()
        if not name or not isinstance(binding, Mapping):
            continue

        source = str(binding.get("source") or "").strip().lower()

        if source == "literal":
            # A literal left blank is genuinely blank — the operator typed nothing on
            # purpose — so unlike the other two sources it is passed through as "".
            resolved[name] = str(binding.get("value") or "")
            continue

        key = str(binding.get("path") or "").strip() or name

        if source == "session":
            found = (caller_variables or {}).get(key)
        elif source == "agent":
            if agent_variables is None:
                from app.services.email_dispatch.nodes.flow_builder_runner import (
                    agent_variables_for,
                )

                # Read through the same helper the Email node uses, so {{AGENT_NAME}}
                # resolves identically in both — it is synthesised from the agent's name
                # rather than declared, and reading the JSONB column would miss it.
                agent_variables = await agent_variables_for(db, chatbot_key)
            found = agent_variables.get(key.upper())
        else:
            continue

        if found is not None and str(found) != "":
            resolved[name] = str(found)

    return resolved


def push(
    session: ChatbotFlowSession,
    node: Mapping[str, Any],
    child_flow: ChatbotFlow,
    inputs: Mapping[str, str],
) -> None:
    """
    Open a call: park the caller's position and variables, and give the callee its own.

    ``variables`` is **replaced**, not merged — see the module docstring. Both it and
    ``call_stack`` are reassigned rather than mutated in place, for the reason
    ``engine_service._store_answer`` gives: they are plain (non-``Mutable``) JSONB columns
    and an in-place write is invisible to SQLAlchemy's change tracking.
    """
    frame = {
        _FLOW_ID: int(child_flow.id),
        _RETURN_NODE: str(node.get("id") or ""),
        _CALLER_VARIABLES: dict(session.variables or {}),
        _OUTPUTS: dict((node.get("data") or {}).get("outputs") or {}),
    }
    session.call_stack = [*stack(session), frame]
    session.variables = dict(inputs or {})


# --------------------------------------------------------------------------
# Leaving a call
# --------------------------------------------------------------------------

def pop(session: ChatbotFlowSession) -> Tuple[Optional[str], Dict[str, Any]]:
    """
    Close the innermost call and return ``(return_node_id, the callee's variables)``.

    The caller's variables are restored here and the outputs merged into them, so by the
    time this returns the session is the caller again. The callee's own map is handed back
    for the engine to log or discard — nothing else keeps it, which is the point of a scope.
    """
    frames = stack(session)
    if not frames:
        return None, dict(session.variables or {})

    frame = frames[-1]
    callee_variables = dict(session.variables or {})

    restored = dict(frame.get(_CALLER_VARIABLES) or {})
    for source_name, destination in (frame.get(_OUTPUTS) or {}).items():
        target = str(destination or "").strip()
        if not target:
            # Blank is how the panel says "do not bring this one back".
            continue
        value = callee_variables.get(str(source_name))
        if value is None:
            # The callee never set it — a branch skipped that block, or it was renamed
            # after this call was configured. Left absent rather than written as "", so the
            # caller's If/Else reads it as empty and an Email binding falls back to its
            # template default instead of sending a blank line.
            continue
        restored[target] = str(value)

    session.call_stack = frames[:-1]
    session.variables = restored
    return str(frame.get(_RETURN_NODE) or "") or None, callee_variables


def clear(session: ChatbotFlowSession) -> None:
    """
    Abandon every open call — used when a session is restarted from the top.

    A restart re-enters the root flow's Start node, so a frame pointing into a call that
    began under the previous graph is state about a conversation that no longer exists.
    Left in place it would return the visitor into the middle of a flow they never entered.
    """
    session.call_stack = []


def parse_flow_uuid(node_data: Mapping[str, Any]) -> Optional[uuid_pkg.UUID]:
    """The block's chosen flow as a uuid, or None when unset or unreadable."""
    raw = str((node_data or {}).get("flow_id") or "").strip()
    if not raw:
        return None
    try:
        return uuid_pkg.UUID(raw)
    except ValueError:
        return None
