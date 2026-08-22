"""
The Email node's behaviour inside an integration run.

**This is the canvas where an Email node is genuinely dangerous, and the whole module is
shaped by that.** Every other canvas runs a node once. An integration node runs against a
*batch of records*, and a batch is routinely fifty thousand rows — so the obvious
implementation, "one email per record", is one careless drawing away from sending fifty
thousand emails from somebody's transactional relay. That does not merely spam people: it
gets the sending domain blocked, which breaks every other email the platform sends and is
not something a retry fixes.

So there are two modes and the safe one is the default:

``once`` (default)
    One email for the whole batch. Bindings read the batch's own facts — how many records
    there were, which node produced them — and a binding to ``record`` is refused, because
    "the record" is not a thing that exists when there are forty thousand of them.

``per_record``
    One email per record, and **bounded**. ``max_emails`` caps it, the cap is checked
    *before* anything is queued, and a batch larger than the cap **fails the node** rather
    than sending the first N. Truncating silently would be the worst of the three options:
    the operator would believe everyone had been emailed. The house rule — no silent caps —
    with real consequences behind it.

**The cap is checked before the loop, not inside it.** Discovering at record 51 that there
were 4,000 would leave 50 emails already queued and no way to un-queue them, so the whole
node refuses first and queues nothing.

**Records failing individually are counted, not raised** — the module-wide rule for record
work. One address that will not render is a fact about the data; failing the node over it
would turn "3 of 500 could not be emailed" into "the sync failed".
"""

import logging
from typing import Any, Dict, List, Mapping, Sequence

from app.models.email_dispatch import SOURCE_NODE
from app.services.email_dispatch import dispatch_service, message_store, queue
from app.services.email_dispatch.errors import EmailFailure, RenderError
from app.services.email_dispatch.variable_sources import (
    VariableContext,
    resolve_bindings,
)

logger = logging.getLogger(__name__)

#: Send once for the batch, or once per record.
MODE_ONCE = "once"
MODE_PER_RECORD = "per_record"

EMAIL_MODES = (
    (MODE_ONCE, "One email for the whole batch"),
    (MODE_PER_RECORD, "One email per record"),
)
EMAIL_MODE_VALUES = frozenset(value for value, _ in EMAIL_MODES)

#: The default cap in ``per_record`` mode, when the node does not set one. Deliberately
#: small: somebody who wants more has to say so, which makes the number a decision rather
#: than an accident.
DEFAULT_MAX_EMAILS = 50

#: The ceiling on ``max_emails``, whatever a node asks for. A node cannot raise its own
#: limit past this — the same shape as ``MAX_BATCH_SIZE`` in the integrations engine, and
#: for a stronger reason: past a few hundred messages from one workflow, the thing at risk
#: is the sending domain's reputation rather than this application's memory.
MAX_EMAILS_CEILING = 500

#: What bindings may read, per mode. `record` is available only per-record — see the module
#: docstring on why "the record" is meaningless for a batch of forty thousand.
BINDING_SOURCES_ONCE = frozenset({"literal", "node"})
BINDING_SOURCES_PER_RECORD = frozenset({"literal", "node", "record"})


def mode_of(data: Mapping[str, Any]) -> str:
    """The node's mode, defaulting to the safe one. An unknown value is refused."""
    mode = str(data.get("mode") or MODE_ONCE).strip().lower()
    if mode not in EMAIL_MODE_VALUES:
        raise EmailFailure(
            f"'{mode}' is not a way to send. Choose one email for the batch, or one per "
            "record."
        )
    return mode


def binding_sources_for(mode: str) -> frozenset:
    """Which binding sources are legal in this mode. Read by the validator and the panel."""
    return (
        BINDING_SOURCES_PER_RECORD if mode == MODE_PER_RECORD else BINDING_SOURCES_ONCE
    )


def max_emails_of(data: Mapping[str, Any]) -> int:
    """
    The cap for this node, bounded by :data:`MAX_EMAILS_CEILING`.

    Silently clamped rather than refused, because a node asking for 10,000 has a clear
    intent and the ceiling is the platform's answer to it — and the *batch* check below is
    where the operator is told a number, so they find out either way.
    """
    raw = data.get("max_emails")
    try:
        asked = int(raw) if raw not in (None, "") else DEFAULT_MAX_EMAILS
    except (TypeError, ValueError):
        asked = DEFAULT_MAX_EMAILS
    return max(1, min(asked, MAX_EMAILS_CEILING))


async def run_email_node(
    node: dict,
    state: Mapping[str, Any],
    records: Sequence[Any],
    *,
    user_id: int,
    node_id: str,
    run_ref: str = "",
) -> Dict[str, Any]:
    """
    Queue this node's email or emails.

    Returns ``{"queued": int, "failed": int, "message": str, "message_uuids": [...]}`` for
    the caller to fold into its counters and its step row. Raises :class:`EmailFailure` for
    anything that should fail the whole node — a missing template, an over-large batch —
    and counts per-record refusals instead of raising them.
    """
    data = node.get("data") or {}
    mode = mode_of(data)

    template_uuid = str(data.get("template_id") or "").strip()
    config_uuid = str(data.get("smtp_config_id") or "").strip()
    if not template_uuid or not config_uuid:
        raise EmailFailure(
            "This Email step has no template or no server chosen. Open it and pick both."
        )

    node_outputs = (state or {}).get("outputs") or {}
    bindings = data.get("variable_bindings") or {}
    recipients = data.get("recipients") or {}

    if mode == MODE_PER_RECORD:
        cap = max_emails_of(data)
        if len(records) > cap:
            # Before anything is queued. See the module docstring: truncating would leave
            # the operator believing everyone had been emailed.
            raise EmailFailure(
                f"This step would send {len(records):,} emails and its limit is {cap:,}. "
                "Raise the limit if that is really intended, or filter the records first — "
                "nothing has been sent."
            )
        contexts = [
            VariableContext(
                record=record,
                node_outputs=node_outputs,
                agent_variables=None,  # type: ignore[arg-type]
                session_variables=None,  # type: ignore[arg-type]
            )
            for record in records
        ]
    else:
        contexts = [
            VariableContext(
                node_outputs=node_outputs,
                agent_variables=None,  # type: ignore[arg-type]
                session_variables=None,  # type: ignore[arg-type]
                record=None,
            )
        ]

    queued_uuids: List[str] = []
    failures: List[str] = []

    async with message_store.open_session() as db:
        template = await dispatch_service.resolve_template(db, user_id, template_uuid)
        config = await dispatch_service.resolve_config(db, user_id, config_uuid)

        for position, context in enumerate(contexts):
            try:
                values = resolve_bindings(bindings, context)
                message = await dispatch_service.enqueue_email(
                    db,
                    user_id=user_id,
                    template=template,
                    config=config,
                    recipients=recipients,
                    values=values,
                    source=SOURCE_NODE,
                    source_ref=(
                        f"integration run {run_ref} node {node_id}"
                        if run_ref
                        else node_id
                    ),
                    workspace_id=template.workspace_id,
                )
                queued_uuids.append(str(message.uuid))
            except RenderError as exc:
                # One record that will not render. Counted, not raised — the module-wide
                # rule for record work.
                failures.append(f"record {position}: {exc.message}")

        if not queued_uuids and failures:
            # Nothing at all could be sent. That is no longer "some records were bad", it is
            # the step not working, so it fails the node with the first reason — which is
            # almost always the same reason as all the others.
            raise EmailFailure(
                f"None of the {len(failures):,} records could be emailed. {failures[0]}"
            )

        await db.commit()

    if queued_uuids:
        queue.wake()

    return {
        "queued": len(queued_uuids),
        "failed": len(failures),
        "message": _summary(mode, len(queued_uuids), len(failures)),
        # Capped, because this goes into a step row's preview. A per-record send of 500 would
        # otherwise put 500 uuids in the log.
        "message_uuids": queued_uuids[:20],
        "delivered": None,
    }


def _summary(mode: str, queued: int, failed: int) -> str:
    """The step row's one-line message. Says *queued*, never *sent*."""
    if mode == MODE_ONCE:
        return "1 email queued." if queued else "No email queued."
    if not failed:
        return f"{queued:,} emails queued."
    return f"{queued:,} emails queued, {failed:,} records could not be emailed."
