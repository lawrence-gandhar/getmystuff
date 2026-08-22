"""
The Email node's behaviour inside a Graph Designer run.

**The implementation lives here rather than in ``app/services/graph_designer/``.** A new
module does not put its files inside another feature's folder, even when only that feature
calls them — so the graph_designer package contributes one registry entry and one validator
call, and everything about what an Email node *does* is in the email module. That keeps the
answer to "how does this application send email" in one folder.

**The node queues; it does not send.** The runner renders and enqueues, and returns
immediately. The alternative — waiting for SMTP inside the node — would make a graph run's
wall-clock depend on somebody else's mail server, hold the run's session open across a
network call, and turn a greylisting relay into a failed graph. The retry machinery already
exists in the queue; putting a second, worse copy inside a node would be the mistake.

So the node's output is *what was queued*, not *whether it arrived*. That distinction is in
the output payload and in the docs, because "the Email node succeeded" must not be read as
"the customer got it" — the delivery log is where that question is answered.

**The error port is taken for a refusal, not for a failed delivery.** A binding that cannot
resolve, a template that has been switched off, a recipient list that renders empty: those
are all knowable now and route down ``error`` so the graph can do something about them. A
relay refusing the message tomorrow is not knowable now and cannot route anywhere — it is a
row in the delivery log.
"""

import logging
from typing import Any, Mapping

from app.models.email_dispatch import SOURCE_NODE
from app.services.email_dispatch import dispatch_service, message_store, queue
from app.services.email_dispatch.errors import EmailFailure
from app.services.email_dispatch.variable_sources import (
    VariableContext,
    resolve_bindings,
)

logger = logging.getLogger(__name__)

#: The sources an Email node in a *graph* can offer. Upstream node outputs and literals —
#: a graph has no chat session and no record in hand. Agent variables are absent for the
#: same reason: a graph is not attached to a chatbot, so there is no agent whose prompt
#: variables it could read. `graph_service` validates against this exact set at save time.
GRAPH_BINDING_SOURCES = frozenset({"node", "literal"})


async def run_email_node(
    node: dict,
    state: Mapping[str, Any],
    user_id: int,
    run_ref: str = "",
) -> dict:
    """
    Render and queue the node's email. Returns what to put in ``outputs``.

    Raises :class:`EmailFailure` for anything the caller should route down the ``error``
    port. The graph_designer runner wraps that in its own ``NodeFailure`` — this module does
    not import that class, because doing so would make the email module depend on the
    graph_designer package it is being called from.

    ``state["outputs"]`` is what a ``node`` binding reads, keyed by node id. Passed whole
    rather than pre-flattened so a binding can reach into a nested field of an upstream
    node's result with a path.
    """
    data = node.get("data") or {}
    node_id = str(node.get("id") or "")

    template_uuid = str(data.get("template_id") or "").strip()
    config_uuid = str(data.get("smtp_config_id") or "").strip()
    if not template_uuid or not config_uuid:
        raise EmailFailure(
            "This Email node has no template or no server chosen. Open it and pick both."
        )

    # Its own session. The node runs on the graph's background task, which has no request
    # session, and the enqueue has to commit on its own so the message survives whatever
    # the rest of the graph does afterwards — including failing.
    async with message_store.open_session() as db:
        template = await dispatch_service.resolve_template(db, user_id, template_uuid)
        config = await dispatch_service.resolve_config(db, user_id, config_uuid)

        values = resolve_bindings(
            data.get("variable_bindings"),
            VariableContext(
                node_outputs=(state or {}).get("outputs") or {},
                agent_variables=None,  # type: ignore[arg-type]
                session_variables=None,  # type: ignore[arg-type]
            ),
        )

        message = await dispatch_service.enqueue_email(
            db,
            user_id=user_id,
            template=template,
            config=config,
            recipients=data.get("recipients"),
            values=values,
            source=SOURCE_NODE,
            # Names the run and the node, so a message in the log can be traced back to the
            # exact node of the exact run that queued it.
            source_ref=f"graph run {run_ref} node {node_id}" if run_ref else node_id,
            workspace_id=template.workspace_id,
        )

        queued = {
            # `uuid`, never the bigint id — this goes into graph state, which is previewed
            # into the run dock and is therefore something a browser sees.
            "message_uuid": str(message.uuid),
            "to": list(message.to_addresses or []),
            "subject": message.subject,
            # Deliberately explicit. A downstream branch reading `queued: true` as
            # "delivered" is the misreading this node most invites, so the payload says what
            # actually happened and nothing more.
            "queued": True,
            "delivered": None,
        }

        await db.commit()

    # After the commit, so a woken worker cannot look before the row has landed.
    queue.wake()

    logger.info("Email node %s queued message %s", node_id, queued["message_uuid"])
    return queued


def wrap_failure(exc: BaseException) -> str:
    """
    The sentence a caller should put on its own node-failure exception.

    Here rather than in each canvas's runner so all three read the same way in a log, and so
    an :class:`EmailFailure`'s carefully written message is not replaced by ``str(exc)`` on
    some path that forgot.
    """
    if isinstance(exc, EmailFailure):
        return exc.message
    return (
        "Something went wrong queueing this email. It has not been sent. "
        "Please contact support if this keeps happening."
    )
