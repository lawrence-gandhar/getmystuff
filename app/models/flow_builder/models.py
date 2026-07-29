import uuid as uuid_pkg

from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import BigInteger, String, Boolean, DateTime, ForeignKey, Index
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.sql import func

from app.db.base import Base


class ChatbotFlow(Base):
    """
    One saved conversation-flow graph (draft or active) for a chatbot
    widget. A chatbot key can own several flows (drafts/variants); at most
    one has is_active == True at a time, enforced in
    app.services.flow_builder.flow_service.activate_flow rather than a DB
    constraint, matching this codebase's existing ChatbotApiKey.is_active
    pattern.
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

    chatbot_key_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("chatbot_api_keys.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)

    # Whole-graph JSON {"nodes": [...], "edges": [...]}, replaced wholesale
    # on every save — plain JSONB (write-once/replace-whole-value), not
    # MutableDict, matching PromptHistory's precedent for this shape.
    graph_data: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    is_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

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
        Index("ix_chatbot_flows_key_active", "chatbot_key_id", "is_active"),
    )


class ChatbotFlowSession(Base):
    """
    Per-visitor execution state for a live flow conversation. The visitor
    browser mints and persists an opaque session_token (localStorage; see
    the widget-script changes in app.services.chatbot_service), sent as a
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
