"""
The Email node's behaviour inside a chatbot conversation.

**This is the canvas where "dynamic variables from the Agents section" is most literal.** A
flow runs behind a chatbot, and that chatbot has an ``ChatbotAiSettings`` row carrying the
prompt variables an operator set up under Agents — ``{{COMPANY}}``, ``{{AGENT_NAME}}`` and
whatever else they declared. Those are offered here as the ``agent`` binding source
alongside ``session``, which is whatever the conversation itself has collected through
Ask-for-Input and Menu nodes.

So an Email node in a flow can say "email {{COMPANY}}'s support desk about
{{CUSTOMER_EMAIL}}" where the first comes from the agent's configuration and the second from
something the visitor typed two messages ago, and neither needed a rule engine.

**The node sends nothing to the visitor and says nothing in the chat.** It queues an email
and hops on, exactly as a ``run_graph`` node that finished quietly does. A node that
announced "I have emailed the team" would be putting words in the operator's mouth — if they
want the visitor told, that is a Send Message node next to this one, which they control.

**A failure takes the error port if one is drawn, and signs off if not.** Never a silent hop
onward: ``_step_run_graph`` states the rule and the reason — a flow carrying on as though a
step had succeeded is how a visitor gets told something that is not true.
"""

import logging
from typing import Any, Dict, Mapping, Optional

from app.models.email_dispatch import SOURCE_NODE
from app.services.email_dispatch import dispatch_service, message_store, queue
from app.services.email_dispatch.errors import EmailFailure
from app.services.email_dispatch.variable_sources import (
    VariableContext,
    resolve_bindings,
)

logger = logging.getLogger(__name__)

#: What a binding in a *flow* may read. The conversation's own variables, the agent's prompt
#: variables, and literals. No upstream node outputs — the flow engine has no such concept,
#: its state is one flat string map — and no records.
FLOW_BINDING_SOURCES = frozenset({"session", "agent", "literal"})


async def agent_variables_for(db, chatbot_key) -> Dict[str, str]:
    """
    The chatbot's prompt variables, as a flat substitution map.

    Goes through ``get_ai_settings_by_key_id`` — the *runtime* lookup, which does no
    ownership check because the caller already resolved the chatbot key, and which creates
    the settings row if it is missing. That last part is why this needs no "no settings"
    branch: a flow-only chatbot gets the default row with its seeded variables, so the map is
    always at least ``{"AGENT_NAME": ...}``.

    And it reuses ``variables_map`` rather than reading the JSONB column directly, so
    ``{{AGENT_NAME}}`` resolves the same way here as it does in the system prompt — it is
    synthesised from the ``agent_name`` field rather than declared, and reading the column
    would miss it and make renaming an agent update one place and not the other.
    """
    from app.services.chatbot import chatbot_ai_settings_service

    settings = await chatbot_ai_settings_service.get_ai_settings_by_key_id(
        db, chatbot_key.id
    )
    return chatbot_ai_settings_service.variables_map(settings)


async def run_email_node(
    db,
    node: dict,
    *,
    chatbot_key,
    session_variables: Mapping[str, Any],
    session_token: str = "",
) -> Dict[str, Any]:
    """
    Render and queue the node's email. Returns what was queued.

    Raises :class:`EmailFailure` for anything the caller should route down ``error``. Takes
    the caller's session — a chat turn *has* one, unlike a graph node running on a
    background task — so the message lands in the same transaction as the session's own
    variable updates and the turn is atomic.
    """
    data = node.get("data") or {}
    node_id = str(node.get("id") or "")

    template_uuid = str(data.get("template_id") or "").strip()
    config_uuid = str(data.get("smtp_config_id") or "").strip()
    if not template_uuid or not config_uuid:
        raise EmailFailure(
            "This Email step has no template or no server chosen."
        )

    template = await dispatch_service.resolve_template(
        db, chatbot_key.user_id, template_uuid
    )
    config = await dispatch_service.resolve_config(
        db, chatbot_key.user_id, config_uuid
    )

    values = resolve_bindings(
        data.get("variable_bindings"),
        VariableContext(
            session_variables=dict(session_variables or {}),
            agent_variables=await agent_variables_for(db, chatbot_key),
            node_outputs=None,  # type: ignore[arg-type]
        ),
    )

    message = await dispatch_service.enqueue_email(
        db,
        user_id=chatbot_key.user_id,
        template=template,
        config=config,
        recipients=data.get("recipients"),
        values=values,
        source=SOURCE_NODE,
        # The session token rather than the flow's id: when somebody asks "why did this
        # customer get an email", the answer is which conversation, and the token is what
        # identifies one.
        source_ref=(
            f"chat {session_token} node {node_id}" if session_token else node_id
        ),
        workspace_id=template.workspace_id,
    )

    logger.info("Flow email node %s queued message %s", node_id, message.uuid)
    return {
        "message_uuid": str(message.uuid),
        "subject": message.subject,
        "to": list(message.to_addresses or []),
        "queued": True,
        "delivered": None,
    }


def wake_worker() -> None:
    """
    Nudge the send worker.

    Called by the caller *after* it commits the turn, never by :func:`run_email_node`
    itself — this function does not own the transaction the message is in, and a worker woken
    before that commit lands looks, finds nothing, and sleeps for the full poll interval.
    """
    queue.wake()
