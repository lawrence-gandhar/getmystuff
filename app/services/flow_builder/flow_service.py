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

import re
import uuid
from typing import Any, Dict, List, Optional

from litestar.exceptions import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.db_utils import CRUDQueryBuilder
from app.db.flow_builder.queries import (
    fetch_attached_chatbot_name,
    fetch_flows_with_chatbot_names,
)
from app.models.file_delivery import (
    FILE_FORMAT_VALUES,
    SOURCE_BLOCK,
    SOURCE_VARIABLE,
)
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
    # Queues an email mid-conversation. Like `run_graph`, its work happens outside this
    # feature — see `engine_service._step_send_email` — but unlike it, it never ends the
    # turn: it queues and hops on, saying nothing to the visitor.
    "send_email",
    # Runs *another flow* as one step of this one, passing values in and bringing named
    # values back — see `engine_service._step_run_flow` and `subflow_service`. The only
    # node type whose work is this same feature recursively, which is why it is the only
    # one needing a depth and a cycle guard.
    "run_flow",
    # Writes rows to a file, and hands that file over. Their work lives in
    # `app/services/file_delivery/` — a new module does not put its files inside another
    # feature's folder — and both behave like `send_email` here: they do their work and
    # hop on, saying nothing to the visitor. The one exception is a Download File block
    # with its button switched on, which is the only block on this canvas that adds
    # something to a turn without being the turn.
    "create_file",
    "download_file",
}
_VALID_OPERATORS = {"equals", "contains", "not_empty"}
_VALID_CONTEXT_SOURCES = {"datasource", "knowledge_base", "prompt"}
_VALID_LLM_MODES = {"in_built", "attached"}

#: A chatbot's own conversation: attachable to one agent, and what every flow was before
#: the kind existed — hence the default, here and on the column.
KIND_AGENT = "agent"

#: A child flow. Never attached to an agent; exists to be run by another flow's Run Flow
#: block, and is the only kind `callable_flow_choices` offers.
KIND_GENERIC = "generic"

VALID_FLOW_KINDS = frozenset({KIND_AGENT, KIND_GENERIC})

#: ``{{ NAME }}`` in a block's prompt or message text. Matches
#: `email_dispatch/rendering.py`'s pattern so an operator who has learned the placeholder
#: syntax in one place has learned it in the other; `engine_service._render_text` is what
#: substitutes them, and `flow_io` reads them to work out what a flow *consumes*.
PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")

#: Node data keys whose text is interpolated, and therefore whose placeholders count as
#: variables a flow reads.
#:
#: ``file_name`` and ``button_text`` are here because both are operator-authored text a
#: visitor ends up seeing — ``orders-{{ORDER_REF}}.csv`` on their disk and *Download
#: {{ORDER_REF}}* on the button — and a placeholder that is substituted anywhere has to be
#: counted everywhere, or a Run Flow block's panel would not offer the parameter the flow
#: plainly needs.
_INTERPOLATED_KEYS = ("prompt_text", "message_text", "file_name", "button_text")

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
            "kind": flow.kind,
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
    Flows a chatbot could be given: agent-kind, active, and not attached to anything yet.

    A flow already attached elsewhere is deliberately absent — it can only run on one
    chatbot, so it has to be detached there first. A **generic** flow is absent for a
    stronger reason: it is a child of another flow and cannot be a chatbot's own
    conversation at all, which the check constraint on the table says as well.
    """
    return await flow_crud.get_many(
        db,
        filters={
            "user_id": user_id,
            "is_active": True,
            "chatbot_key_id": None,
            "kind": KIND_AGENT,
        },
        order_by="name",
    )


async def get_attached_flow(db: AsyncSession, user_id: int, key_id: uuid.UUID) -> Optional[ChatbotFlow]:
    """The flow attached to one chatbot, active or not (the settings dropdown shows both)."""
    key = await chatbot_service.get_chatbot_key(db, user_id, key_id)  # ownership check
    return await flow_crud.get_one(db, filters={"chatbot_key_id": key.id})


def flow_io(graph_data: dict) -> Dict[str, List[str]]:
    """
    What a flow **writes** and what it **reads**, derived from its own graph.

    This is what a Run Flow block's property panel draws its two lists of rows from — the
    values it can bring back, and the values worth passing in — and deriving them beats
    asking an operator to declare them twice: a list typed into a caller cannot drift from
    what the callee actually does if it was read off the callee.

    ``writes`` is every storing block's ``variable_name``: an Ask-for-Input answer, a Menu
    choice, an AI Fallback answer, a Run Graph row count, an Email's message id, another Run
    Flow's returned value. Anything a later block in that flow could read is something a
    caller could be handed back. An If/Else is the one block whose ``variable_name`` is not
    one of these — it compares rather than stores, so its name belongs to ``reads``.

    ``reads`` is every If/Else's ``variable_name`` plus every ``{{PLACEHOLDER}}`` in a
    prompt or message, minus whatever the flow writes for itself — a flow that asks for an
    email and then says "thanks {{email}}" needs nothing passed in, and offering it as a
    parameter would invite an operator to overwrite the answer they are about to collect.

    Both lists keep first-appearance order rather than being sorted: that is the order the
    blocks appear on the canvas, which is information about how the flow is meant to be
    read. The same rule the email module states for a template's declared variables.

    Not exhaustive, and it cannot be. A Run Graph block inside the callee is handed the
    whole variable map (``_step_run_graph``), so it can consume a name that appears nowhere
    in that flow's own graph — which is why the panel also lets a value be added by hand.
    """
    writes: List[str] = []
    reads: List[str] = []

    def _add(target: List[str], name: Any) -> None:
        name = str(name or "").strip()
        if name and name not in target:
            target.append(name)

    for node in (graph_data or {}).get("nodes") or []:
        data = node.get("data") or {}

        if node.get("type") == "if_else":
            # The one block whose `variable_name` is a value it *reads*: it stores nothing,
            # it compares. Counting it as written would both offer a caller something this
            # flow never produces and hide the parameter the flow actually needs.
            _add(reads, data.get("variable_name"))
        else:
            _add(writes, data.get("variable_name"))

        # A Run Flow block's returned values are written by *this* flow under the caller's
        # chosen names, so they are part of what this flow can hand on in turn.
        for destination in (data.get("outputs") or {}).values():
            _add(writes, destination)

        for key in _INTERPOLATED_KEYS:
            for match in PLACEHOLDER_RE.finditer(str(data.get(key) or "")):
                _add(reads, match.group(1))

    return {"writes": writes, "reads": [name for name in reads if name not in writes]}


async def callable_flow_choices(
    db: AsyncSession,
    user_id: int,
    exclude_flow_id: Optional[uuid.UUID] = None,
) -> List[Dict[str, Any]]:
    """
    The flows a Run Flow block may pick, each carrying what it reads and writes.

    **Generic flows only.** That is what the kind is for: an agent flow is somebody's live
    conversation with its own visitors, and running one as a child of another flow would put
    two callers inside the same drawing for two different reasons. A flow meant to be reused
    is marked as such, and marking it is what puts it in this list.

    Published only, and never the flow being edited — a flow cannot run itself, so offering
    it would be offering a choice the validator refuses. The self-exclusion still earns its
    place even with the kind filter, because a generic flow may call another generic flow.

    The two variable lists ride along on each entry rather than being fetched when a flow is
    chosen, for the reason ``email_dispatch/template_service.choices`` states about a
    template's declared variables: a second request would let somebody save the block before
    its rows had loaded.
    """
    flows = await flow_crud.get_many(
        db,
        filters={"user_id": user_id, "is_active": True, "kind": KIND_GENERIC},
        order_by="name",
    )
    return [
        {
            "id": str(flow.uuid),
            "label": flow.name,
            **flow_io(flow.graph_data),
        }
        for flow in flows
        if exclude_flow_id is None or flow.uuid != exclude_flow_id
    ]


async def get_flow_by_uuid_for_run(db: AsyncSession, flow_uuid: uuid.UUID) -> Optional[ChatbotFlow]:
    """
    Runtime-facing lookup of a flow a Run Flow block points at, by its public uuid.

    No ownership check, mirroring ``get_active_flow``'s precedent: the caller has already
    resolved the chatbot key, and the reference itself was ownership-checked at save time by
    ``_assert_run_flow_targets``. What is *not* assumed is that it is still valid — a flow
    can be deleted or unpublished after a block was saved pointing at it, so the runner
    treats both as a failed call and takes the ``error`` port rather than pretending the
    step succeeded.
    """
    return await flow_crud.get_by_uuid(db, flow_uuid)


async def get_flow_by_id_for_run(db: AsyncSession, flow_id: int) -> Optional[ChatbotFlow]:
    """
    Runtime-facing lookup by internal id — how a parked call stack frame finds the flow it
    was running. Keyed on the bigint because a frame stores the internal id: it is written
    and read only by the engine and never reaches a browser.
    """
    return await flow_crud.get_one(db, filters={"id": flow_id})


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

def _validated_kind(kind: Optional[str]) -> str:
    """
    One of :data:`VALID_FLOW_KINDS`, defaulting to ``agent`` for an unset value.

    Unset means agent rather than being refused: this is the kind every flow had before the
    column existed, so a caller that predates it — or a form that has not been updated —
    keeps creating what it always created.
    """
    kind = (kind or "").strip().lower() or KIND_AGENT
    if kind not in VALID_FLOW_KINDS:
        raise HTTPException(
            status_code=400,
            detail=(
                "A flow is either an agent flow or a generic one. "
                f"'{kind}' is neither."
            ),
        )
    return kind


async def create_flow(
    db: AsyncSession,
    user_id: int,
    name: str,
    kind: str = KIND_AGENT,
) -> ChatbotFlow:
    """
    Create a draft flow of the given kind.

    Attaching an agent flow to a chatbot is a separate, later step; a **generic** flow is
    never attached at all and is instead picked by another flow's Run Flow block. Both start
    as drafts either way — the Active switch is one rule for both kinds, so a half-drawn
    child flow is no more callable than a half-drawn agent flow is answerable.
    """
    name = (name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Flow name is required")

    return await flow_crud.create(db, {
        "user_id": user_id,
        "name": name,
        "graph_data": dict(_DEFAULT_GRAPH),
        "is_active": False,
        "kind": _validated_kind(kind),
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
    # Shape first, then the one check that needs the database. Ordered that way so a
    # malformed graph is refused by its own sentence rather than by a lookup failing on a
    # field the operator has not filled in yet.
    _validate_graph(graph_data, self_uuid=flow.uuid)
    await _assert_run_flow_targets(db, user_id, graph_data)
    return await flow_crud.update(db, flow.id, {"graph_data": graph_data})


async def _assert_run_flow_targets(db: AsyncSession, user_id: int, graph_data: dict) -> None:
    """
    Every Run Flow block points at a flow this user owns, has marked generic, and has
    published.

    Separate from ``_validate_graph`` because it needs the database and that validator is
    synchronous like the rest of the file — the same split ``_validate_send_email_data``
    documents for a template's declared variables, and for the same reason: the check that
    can be made without I/O is made where the shape is checked, and the one that cannot is
    made here, before anything is written.

    Ownership is the point of doing it at all. Without it a saved graph could name any
    flow's uuid and the runtime lookup — which does no ownership check, by design — would
    run somebody else's conversation inside this one.

    All three conditions are checked **again** at run time by
    ``engine_service._run_flow_refusal``, because a flow can be switched to agent-kind,
    unpublished or deleted *after* a block was saved pointing at it. Here they are a refusal
    the operator reads while looking at the canvas; there they are a failed call taking the
    ``failed`` port. Neither makes the other redundant.
    """
    for node in (graph_data or {}).get("nodes") or []:
        if node.get("type") != "run_flow":
            continue

        raw = str((node.get("data") or {}).get("flow_id") or "").strip()
        try:
            target_uuid = uuid.UUID(raw)
        except ValueError:
            raise HTTPException(
                status_code=400, detail="Run Flow block is missing a flow to run",
            ) from None

        target = await flow_crud.get_by_uuid(db, target_uuid, extra_filters={"user_id": user_id})
        if target is None:
            raise HTTPException(
                status_code=400,
                detail="A Run Flow block points at a flow that no longer exists",
            )
        if target.kind != KIND_GENERIC:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"The flow {target.name} is an agent flow, so it cannot be run from "
                    "inside another one. Mark it Generic in the flow library, or point this "
                    "block at a generic flow."
                ),
            )
        if not target.is_active:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"The flow {target.name} is not published, so it cannot be run from "
                    "here. Publish it first."
                ),
            )


async def set_flow_kind(
    db: AsyncSession,
    user_id: int,
    flow_id: uuid.UUID,
    kind: str,
) -> ChatbotFlow:
    """
    Switch a flow between being an agent's conversation and being a callable child.

    The single write path for the library's Generic/Agent toggle, shaped like
    ``set_flow_active`` beside it. Two things it deliberately does *not* do:

    * **It does not detach anything.** Making an attached flow generic would silently take a
      live conversation away from an agent, so it is refused instead, naming the agent — the
      same courtesy ``attach_flow`` extends when a flow is already used elsewhere. Detaching
      is a decision made on the agent's own page, where whoever makes it can see what else
      that agent is running.
    * **It does not touch ``is_active``.** Publishing is one rule for both kinds, and a flow
      that was ready to run before this switch is still ready to run after it.

    Going the other way — generic back to agent — needs no guard. A Run Flow block still
    pointing at it is refused on the caller's next save and fails the call at run time with a
    sentence saying which flow and why, which is a better place to find out than a refusal
    here about a block on a canvas the operator is not looking at.
    """
    kind = _validated_kind(kind)
    flow = await get_flow(db, user_id, flow_id)

    if kind == KIND_GENERIC and flow.chatbot_key_id is not None:
        attached_to = await fetch_attached_chatbot_name(db, flow.id)
        named = f" from {attached_to}" if attached_to else ""
        raise HTTPException(
            status_code=400,
            detail=(
                f"{flow.name} is attached to an agent, so it cannot become a generic child "
                f"flow. Detach it{named} first, then mark it generic."
            ),
        )

    return await flow_crud.update(db, flow.id, {"kind": kind})


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
        if new_flow.kind != KIND_AGENT:
            # `get_attachable_flows` already keeps generic flows out of the dropdown, so
            # reaching this needs a hand-made request — but the dropdown should not be the
            # only thing standing between a mistake and a live agent, and the check
            # constraint on the table would otherwise refuse this with a database error
            # instead of a sentence.
            raise HTTPException(
                status_code=400,
                detail=(
                    f"The flow {new_flow.name} is a generic child flow, so it cannot be a "
                    "chatbot's own conversation. Mark it as an agent flow in Flow Builder "
                    "first, or pick a different flow."
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

def _validate_graph(graph_data: dict, self_uuid: Optional[uuid.UUID] = None) -> None:
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
        _validate_node(node, node_by_id, self_uuid)

    start_id = start_nodes[0]["id"]
    end_ids = {n["id"] for n in nodes if n.get("type") == "end"}
    _validate_edges(edges, node_by_id, start_id, end_ids)


def _validate_node(node: dict, node_by_id: dict, self_uuid: Optional[uuid.UUID] = None) -> None:
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
    elif node_type == "send_email":
        _validate_send_email_data(data)
    elif node_type == "run_flow":
        _validate_run_flow_data(data, self_uuid)
    elif node_type == "create_file":
        _validate_create_file_data(data)
    elif node_type == "download_file":
        _validate_download_file_data(data, node_by_id)


def _validate_send_email_data(data: dict) -> None:
    """
    An Email node: a template, a server, a recipient, and bindings a flow can actually
    satisfy.

    The available sources come from the runner's own module rather than being restated here,
    so a node the validator accepts cannot be one the runner refuses. A flow has the
    conversation's variables and the agent's prompt variables and nothing else — no upstream
    node outputs, because this engine's state is one flat string map.

    The template's declared variables are not checked: that needs the database, and this
    validator is synchronous like the rest of the file. A binding naming a variable the
    template no longer declares is refused at enqueue, with a sentence naming it.
    """
    from app.services.email_dispatch.errors import RenderError
    from app.services.email_dispatch.nodes.flow_builder_runner import (
        FLOW_BINDING_SOURCES,
    )
    from app.services.email_dispatch import variable_sources

    if not str(data.get("template_id") or "").strip():
        raise HTTPException(
            status_code=400, detail="Email node is missing an email template",
        )
    if not str(data.get("smtp_config_id") or "").strip():
        raise HTTPException(
            status_code=400, detail="Email node is missing an SMTP server",
        )

    recipients = data.get("recipients") or {}
    if not isinstance(recipients, dict) or not (recipients.get("to") or []):
        raise HTTPException(
            status_code=400,
            detail="Email node needs at least one TO address (it may be a {{VARIABLE}})",
        )

    bindings = data.get("variable_bindings") or {}
    if not isinstance(bindings, dict):
        raise HTTPException(
            status_code=400, detail="Email node's variable bindings could not be read",
        )

    for name, binding in bindings.items():
        shown = str(name).upper()
        if not isinstance(binding, dict):
            raise HTTPException(
                status_code=400,
                detail=f"Email node's binding for {{{{{shown}}}}} could not be read",
            )
        source = str(binding.get("source") or "").strip().lower()
        if source not in FLOW_BINDING_SOURCES:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Email node binds {{{{{shown}}}}} to '{source}', which a conversation "
                    "cannot provide. Use a value the chat collected, an agent variable, or "
                    "a fixed value."
                ),
            )
        path = str(binding.get("path") or "").strip()
        if path:
            try:
                variable_sources.assert_path(path, name=shown)
            except RenderError as exc:
                raise HTTPException(status_code=400, detail=exc.message) from exc


def _validate_create_file_data(data: dict) -> None:
    """
    A Create File block: a format, and somewhere to get the rows.

    The available formats and sources come from the runner's own module rather than being
    restated here, so a block the validator accepts cannot be one the runner refuses —
    the arrangement `_validate_send_email_data` states. A flow has the results of blocks
    that have already run and its own variables, and nothing else: there are no upstream
    node outputs on this canvas, because this engine's state is one flat string map plus
    the block-results record beside it.

    What is deliberately **not** checked: whether the named block actually produces rows,
    or whether a variable will hold a dataset when the conversation reaches this block.
    Neither is knowable from a drawing — a Run Graph block's result depends on somebody's
    data — so both are refusals at run time, down the ``error`` port, with a sentence
    naming the block. The same division `_validate_send_email_data` makes about a
    template's declared variables.
    """
    from app.services.file_delivery.nodes.flow_builder_runner import FLOW_DATA_SOURCES

    file_format = str(data.get("file_format") or "").strip()

    if file_format not in FILE_FORMAT_VALUES:
        raise HTTPException(
            status_code=400,
            detail=(
                "Create File block needs a file format — CSV, Excel, Text or Parquet."
            ),
        )

    source_data = data.get("data")

    if not isinstance(source_data, dict):
        raise HTTPException(
            status_code=400,
            detail="Create File block's data source could not be read",
        )

    source = str(source_data.get("source") or "").strip().lower()

    if source not in FLOW_DATA_SOURCES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Create File block reads its data from '{source}', which a conversation "
                "cannot provide. Use a block earlier in the flow, or a variable."
            ),
        )

    if source == SOURCE_BLOCK and not str(source_data.get("block_id") or "").strip():
        raise HTTPException(
            status_code=400,
            detail="Create File block has no block chosen to take its data from",
        )

    if source == SOURCE_VARIABLE and not str(source_data.get("name") or "").strip():
        raise HTTPException(
            status_code=400,
            detail="Create File block has no variable chosen to take its data from",
        )


def _validate_download_file_data(data: dict, node_by_id: dict) -> None:
    """
    A Download File block: a Create File block on this canvas, and a legal button.

    **The named block must exist and must be a Create File block**, checked here rather
    than left to the runtime. A Goto node's target is checked the same way and for the same
    reason: a reference to a block that is not there is a mistake at the keyboard, and
    saying so while the operator is looking at it is worth more than a failed conversation
    later.

    The **colour** is the security-relevant field. It reaches the widget and lands in an
    inline ``style`` attribute on a page this application does not own, so anything that is
    not ``#rrggbb`` is refused here — the first of three gates, the others being the
    runner and the payload schema.
    """
    from app.services.file_delivery.nodes.flow_builder_runner import COLOUR_PATTERN

    source_id = str(data.get("create_file_node_id") or "").strip()

    if not source_id:
        raise HTTPException(
            status_code=400,
            detail=(
                "Download File block must name the Create File block whose file it "
                "hands over"
            ),
        )

    target = node_by_id.get(source_id)

    if target is None or target.get("type") != "create_file":
        raise HTTPException(
            status_code=400,
            detail=(
                "Download File block must point at a Create File block on this canvas"
            ),
        )

    if not data.get("show_button"):
        # No button, nothing else to check. The block still does its work — it puts the
        # link in a variable — so an unticked button is a complete configuration, not an
        # unfinished one.
        return

    colour = str(data.get("button_colour") or "").strip()

    if colour and not COLOUR_PATTERN.fullmatch(colour):
        raise HTTPException(
            status_code=400,
            detail=(
                f"'{colour}' is not a colour. A button colour looks like #0d6efd."
            ),
        )


#: What a Run Flow block's input may read. The same three a flow's Email node has
#: (`FLOW_BINDING_SOURCES`) and for the same reason — a conversation has its own variables,
#: the agent's prompt variables, and literals, and nothing else.
RUN_FLOW_INPUT_SOURCES = frozenset({"session", "agent", "literal"})


def _validate_run_flow_data(data: dict, self_uuid: Optional[uuid.UUID]) -> None:
    """
    A Run Flow block: a flow to run, values to pass in, values to bring back.

    **A flow may not run itself.** Refused here by name rather than left to the runtime
    depth guard, because a direct self-call is a mistake at the keyboard and saying so while
    the operator is looking at the block is worth more than a failed call later. A cycle
    through two flows cannot be seen from one graph and is caught at run time instead
    (`subflow_service.guard`) — the shapes of the two checks differ because the shapes of
    the two mistakes do.

    Whether the target exists, is owned, and is published needs the database and is checked
    by ``_assert_run_flow_targets``.
    """
    raw = str(data.get("flow_id") or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="Run Flow block is missing a flow to run")

    try:
        target_uuid = uuid.UUID(raw)
    except ValueError:
        raise HTTPException(
            status_code=400, detail="Run Flow block's chosen flow could not be read",
        ) from None

    if self_uuid is not None and target_uuid == self_uuid:
        raise HTTPException(
            status_code=400,
            detail=(
                "A Run Flow block cannot run the flow it is in — that would call itself "
                "forever. Point it at a different flow."
            ),
        )

    _validate_run_flow_inputs(data.get("inputs"))
    _validate_run_flow_outputs(data.get("outputs"))


def _validate_run_flow_inputs(inputs: Any) -> None:
    """
    ``{"child_variable": {"source": ..., "path"|"value": ...}}``, with the source one a
    conversation can actually serve.

    Names are **not** upper-cased anywhere in this block, unlike an email template's
    variables: a flow variable is whatever an operator typed into a "store this in" field,
    ``email`` and ``EMAIL`` are two different variables to every other block, and folding
    the case here would hand the callee a name it never reads.
    """
    if inputs is None:
        return
    if not isinstance(inputs, dict):
        raise HTTPException(
            status_code=400, detail="Run Flow block's values-passed-in could not be read",
        )

    for name, binding in inputs.items():
        shown = str(name).strip()
        if not shown:
            raise HTTPException(
                status_code=400,
                detail="A Run Flow block passes in a value with no name. Name it or remove it.",
            )
        if not isinstance(binding, dict):
            raise HTTPException(
                status_code=400,
                detail=f"Run Flow block's value for '{shown}' is not filled in correctly",
            )
        source = str(binding.get("source") or "").strip().lower()
        if source not in RUN_FLOW_INPUT_SOURCES:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Run Flow block's '{shown}' has no value source chosen. Use a value "
                    "the chat collected, an agent variable, or a fixed value."
                ),
            )


def _validate_run_flow_outputs(outputs: Any) -> None:
    """
    ``{"callee_variable": "name_to_store_it_under"}``.

    Two destinations with the same name are refused: one would silently overwrite the
    other, and which one won would depend on dictionary order in a JSONB column — a result
    nobody could predict from looking at the block.
    """
    if outputs is None:
        return
    if not isinstance(outputs, dict):
        raise HTTPException(
            status_code=400, detail="Run Flow block's values-brought-back could not be read",
        )

    seen: Dict[str, str] = {}
    for source_name, destination in outputs.items():
        if not str(source_name).strip():
            raise HTTPException(
                status_code=400,
                detail="A Run Flow block brings back a value with no name",
            )
        target = str(destination or "").strip()
        if not target:
            # Left blank on purpose is how the panel says "do not return this one".
            continue
        if target in seen:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Run Flow block stores two different values in '{target}'. Give them "
                    "different names."
                ),
            )
        seen[target] = str(source_name)


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
