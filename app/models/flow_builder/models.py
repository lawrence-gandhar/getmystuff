import uuid as uuid_pkg
from typing import Optional

from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.sql import func

from app.db.base import Base


class ChatbotFlow(Base):
    """
    One saved conversation-flow graph, owned by a user and built independently
    of any chatbot (see the Flow Builder page in the sidebar).

    Ownership and association are separate concerns:

    * ``user_id`` is who owns the flow — the only thing needed to read or edit
      it, so a flow can exist with no chatbot at all.
    * ``chatbot_key_id`` is which chatbot currently *runs* it, nullable and
      unique: a flow belongs to at most one chatbot, and a chatbot runs at most
      one flow. Postgres allows many NULLs in a unique column, so that single
      constraint expresses both halves — replacing the old "at most one active
      flow per key" rule that flow_service had to enforce by hand.
    * ``is_active`` is an independent published/draft toggle. A chatbot only
      runs its attached flow while that flow is active (see get_active_flow),
      so a flow can be parked without being detached.
    * ``kind`` is what the flow is *for*: ``"agent"`` — a chatbot's own
      conversation, attachable, the default and what every flow was before this
      column existed — or ``"generic"``, a child flow that exists to be run by
      another flow's Run Flow block and is never attached to a chatbot at all.

    The third switch is the one worth reading twice, because it constrains the
    second: **a generic flow must not be attached.** Said in the check
    constraint below as well as in the service, for the reason the unique
    constraint above is a constraint rather than a service rule — an invariant
    the database states cannot be got round by a code path that forgot it. A
    generic flow is therefore absent from an agent's Conversation Flow dropdown
    (``get_attachable_flows``) and is the only kind a Run Flow block will offer
    (``callable_flow_choices``).

    Deleting a chatbot detaches its flow (ON DELETE SET NULL) rather than
    destroying work the user may want to point somewhere else.
    """
    __tablename__ = "chatbot_flows"

    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=True,
    )

    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True),
        default=uuid_pkg.uuid4,
        unique=True,
        index=True,
        nullable=False,
    )

    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    chatbot_key_id: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        ForeignKey("chatbot_api_keys.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)

    # Whole-graph JSON {"nodes": [...], "edges": [...]}, replaced wholesale
    # on every save — plain JSONB (write-once/replace-whole-value), not
    # MutableDict, matching PromptHistory's precedent for this shape.
    graph_data: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    is_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # "agent" | "generic" — see the class docstring. A string rather than an
    # `is_generic` boolean, matching how every other state in this schema is
    # spelled (ChatbotFlowSession.status, FlowNodeKnowledgeBase.status) and
    # leaving room for a third kind without a migration that renames a column.
    # The vocabulary itself lives in flow_service.VALID_FLOW_KINDS.
    kind: Mapped[str] = mapped_column(
        String(20), nullable=False, default="agent", server_default="agent",
    )

    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        index=True,
    )

    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        # One flow per chatbot, one chatbot per flow — see the class docstring.
        UniqueConstraint("chatbot_key_id", name="uq_chatbot_flows_chatbot_key"),
        # A generic flow is a child of another flow, so it is never a chatbot's
        # own conversation. Stated here as well as in `set_flow_kind` and
        # `attach_flow` on purpose: those two are the readable refusals, and
        # this is the guarantee that no third write path can undo them.
        CheckConstraint(
            "kind = 'agent' OR chatbot_key_id IS NULL",
            name="ck_chatbot_flows_generic_unattached",
        ),
    )


class ChatbotFlowSession(Base):
    """
    Per-visitor execution state for a live flow conversation. The visitor
    browser mints and persists an opaque session_token (localStorage; see
    the widget-script changes in app.services.chatbot.chatbot_service), sent as a
    plain field on every POST /public/chatbot/message. This is NOT the
    row's own public `uuid` and is never trusted as a lookup key by itself
    — every query scopes it by chatbot_key_id (see the unique index below).
    """
    __tablename__ = "chatbot_flow_sessions"

    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=True,
    )

    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True),
        default=uuid_pkg.uuid4,
        unique=True,
        index=True,
        nullable=False,
    )

    chatbot_key_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("chatbot_api_keys.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    flow_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("chatbot_flows.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Visitor-generated opaque token (widget localStorage UUID) — not this
    # row's own `uuid` column.
    session_token: Mapped[str] = mapped_column(String(100), nullable=False)

    # References a node's "id" string inside flow.graph_data["nodes"] —
    # nodes are JSONB entries, not rows, so this is a string pointer, not a FK.
    current_node_id: Mapped[str] = mapped_column(String(100), nullable=False)

    # Values captured from Ask-for-Input nodes: {"var_name": "value", ...}
    variables: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    # The uuid of a Graph Designer run this session is waiting on an answer for, or
    # NULL. Set by a Run-Graph node whose graph stopped at an *Ask a human* node: the
    # question went to the visitor, and their next message is the answer to it.
    #
    # Its own column rather than a reserved key in `variables`, for two reasons. That
    # dict is the visitor's own namespace — it is interpolated into message text — so a
    # key in it can be read back out in a chat bubble, and a name reserved by the
    # application is a name an operator can collide with. And this is the same kind of
    # thing as `ToolGraphRun.thread_id` and `DownloadExport.thread_id`: a handle to work
    # parked between two requests, which every other feature stores as a column of its
    # own.
    awaiting_graph_run: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True,
    )

    # The stack of Run-Flow calls this session is inside, outermost first, or []. One
    # frame per call in progress: which flow is running, which node in the *caller* to
    # come back to, the caller's own variables while the callee runs, and which of the
    # callee's variables to bring back and under what names.
    #
    # A stack rather than a single frame because a sub-flow may itself call one, and a
    # column rather than a key in `variables` for the reason `awaiting_graph_run` gives
    # above: that dict is the visitor's namespace and is interpolated into chat text, so
    # a name the application reserves is a name an operator can collide with.
    #
    # `flow_id` above deliberately keeps pointing at the **root** flow (the one attached
    # to the chatbot) even while a sub-flow is running — `_session_needs_restart` compares
    # it against the attached flow, and a session whose flow_id had become a callee's
    # would restart itself on every turn. The flow actually being interpreted comes from
    # the top of this stack; see `subflow_service.current_flow`.
    #
    # Plain JSONB, replaced wholesale on every write like `variables` — see
    # `engine_service._store_answer` for why an in-place mutation would not persist.
    call_stack: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]",
    )

    # What a block produced that is too big, or too structured, to be a variable:
    # {"<node id>": {"kind": "graph_run"|"table", ...}}. Written by the Run Graph and AI
    # Fallback blocks, read by a Create File block that names one of them as its data.
    #
    # Keyed by **node id**, not by variable name. Two blocks can share a variable name —
    # and a Create File block points at one particular block on the canvas, which is a
    # different question from "what is the current value of X".
    #
    # A separate column from `variables` for the reason `awaiting_graph_run` and
    # `call_stack` above both give: that dict is the visitor's own namespace and gets
    # interpolated into chat text, so anything the application reserves in it is a name
    # an operator can collide with. It is also the wrong shape — `variables` is a flat
    # string map, and these are records.
    #
    # What is stored is deliberately small: a graph run's **id** rather than its rows, so
    # a Create File block re-reads every row at file time instead of writing a preview
    # (see `file_delivery.row_source`), and an AI Fallback's own small answer table.
    #
    # Plain JSONB, replaced wholesale on every write like `variables`.
    node_results: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}",
    )

    # One rolling turn of context for a dead-end AI Fallback node — one with no
    # outgoing edge — that the visitor keeps talking to after the flow itself has
    # finished: {"<node id>": "<that node's last answer, as text>"}. See
    # `engine_service._continue_dead_end_ai_fallback` and `_step_ai_fallback`.
    #
    # Keyed by node id like `node_results`, though in practice at most one key is ever
    # populated per session: `current_node_id` cannot move to a different node once
    # `status` is "completed" except via a restart, which clears this too.
    #
    # A column of its own rather than a key in `variables` or a value in `node_results`,
    # for two different reasons. `variables` is the visitor's own interpolated
    # namespace — the same reason `awaiting_graph_run` and `call_stack` above give.
    # `node_results[node_id]` already means "what this block produced, for a Create
    # File block to read" — written only when there is a table, and read by
    # `file_delivery.row_source`'s strict kind-dispatch — which is a different concern
    # from "conversational memory that must persist even when there is no table".
    # Keeping them separate means a Create File block naming this node can never see
    # (or be confused by) what the dead-end loop remembers.
    #
    # Plain JSONB, replaced wholesale on every write like `variables`.
    dead_end_ai_context: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}",
    )

    # "active" | "completed"
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")

    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        index=True,
    )

    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        index=True,
    )

    __table_args__ = (
        Index(
            "ux_chatbot_flow_sessions_key_token",
            "chatbot_key_id", "session_token",
            unique=True,
        ),
    )


class FlowNodeKnowledgeBase(Base):
    """
    One AI Fallback node's knowledge base — the uploaded/typed supporting
    documents it was "trained" on. Scoped per node rather than per flow or
    per chatbot key, so each AI Fallback block in a flow can ground its
    answers in different material.

    `node_id` references a node's "id" string inside the owning flow's
    `graph_data["nodes"]` — nodes are JSONB entries, not rows, so this is a
    string pointer, not a FK, matching ChatbotFlowSession.current_node_id's
    precedent for this exact shape. It can outlive the node being deleted
    from the graph (only cleaned up if the flow itself is deleted, via the
    flow_id FK's CASCADE) — the runtime lookup in
    flow_builder.knowledge_base_service.retrieve_context simply finds
    nothing for a stale/removed node_id.
    """
    __tablename__ = "flow_node_knowledge_bases"

    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=True,
    )

    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True),
        default=uuid_pkg.uuid4,
        unique=True,
        index=True,
        nullable=False,
    )

    flow_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("chatbot_flows.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    node_id: Mapped[str] = mapped_column(String(100), nullable=False)

    # "untrained" | "trained" | "failed" — set by
    # flow_builder.knowledge_base_service.train_knowledge_base.
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="untrained")

    trained_at: Mapped[Optional[DateTime]] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)

    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        index=True,
    )

    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        Index("ux_flow_node_kb_flow_node", "flow_id", "node_id", unique=True),
    )


class FlowNodeKnowledgeDocument(Base):
    """
    One supporting document (an uploaded pdf/txt/docx file, or a block of
    manually-typed text) belonging to a FlowNodeKnowledgeBase.

    Uploaded files are stored on disk (mirrors DatasourceFile's pattern);
    `content` holds the extracted plain text once training has run
    (populated immediately at creation time for manual entries, since
    there's nothing to extract).
    """
    __tablename__ = "flow_node_knowledge_documents"

    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=True,
    )

    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True),
        default=uuid_pkg.uuid4,
        unique=True,
        index=True,
        nullable=False,
    )

    knowledge_base_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("flow_node_knowledge_bases.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # "upload" | "manual"
    source_type: Mapped[str] = mapped_column(String(10), nullable=False)

    # Manual-entry display label (e.g. "Return policy"). Null for uploads,
    # which use original_filename instead.
    label: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    original_filename: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    stored_filename: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    file_path: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    mime_type: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    size_bytes: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)

    content: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # "pending" | "extracted" | "error" — manual entries start "extracted"
    # (their `content` is already the full text); uploads start "pending"
    # until train_knowledge_base extracts their text.
    extraction_status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    error_message: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        index=True,
    )

    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )
