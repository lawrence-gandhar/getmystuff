"""
Data Agents — a user-owned natural-language agent, managed from its own module.

An agent is independent of any workspace: ``workspace_id`` is nullable, so it can
be created unassigned, assigned later, moved, or left behind when a workspace is
deleted. Ownership therefore lives on the agent itself (``user_id``) rather than
being inherited from a workspace that may not be there.

What an agent can actually *do* is defined by its Tool Configs
(app.models.tool_configs), managed in their own module too. An agent with no tool
configs is valid but inert, which is the normal state right after creation.
"""

import uuid as uuid_pkg
from typing import Optional

from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from app.db.base import Base


class DataAgent(Base):
    """
    One natural-language agent.

    ``llm_api_key_id`` reuses the keys managed on the AI Settings page rather than
    storing another secret here. It is nullable so an agent can be drafted before
    a key exists, and ``ON DELETE SET NULL`` means deleting a key leaves the agent
    in place (unconfigured) instead of destroying it — the same reasoning applies
    to ``workspace_id``.
    """
    __tablename__ = "data_agents"

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

    # Optional grouping — an agent may sit outside any workspace, and deleting a
    # workspace unassigns rather than deletes its agents.
    workspace_id: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        ForeignKey("workspaces.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    llm_api_key_id: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        ForeignKey("ai_api_keys.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Prepended to every request the agent makes — the operator's standing
    # instructions ("answer only from the sales schema", tone, refusals).
    #
    # Written ONLY by the agent form. The prompt-sync background job never
    # touches this column: the operator's words are theirs, and a job that
    # rewrote them would race with whoever has the edit form open.
    system_prompt: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Generated, never operator-editable: the description of this agent's tools
    # that tells the Deep Agent which one answers which question. Composed
    # deterministically from the agent's enabled tool configs by
    # app.services.deep_agents.prompt_builder and written by prompt_sync_service
    # whenever a tool config changes.
    #
    # The runtime prompt is system_prompt + tool_routing_prompt, assembled at
    # answer time — the two are deliberately stored apart so neither writer can
    # clobber the other.
    tool_routing_prompt: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # When tool_routing_prompt was last generated. Compared against the newest
    # tool config's updated_at to detect staleness: if this is NULL or older,
    # deep_agent_service regenerates inline before answering. That fallback is
    # what makes the background job an optimisation rather than something
    # correctness depends on.
    tool_prompt_synced_at: Mapped[Optional[DateTime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

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

    workspace = relationship("Workspace", back_populates="data_agents")

    # Tool configs have no meaning without their agent, so they do cascade.
    tool_configs = relationship(
        "ToolConfig",
        back_populates="data_agent",
        cascade="all, delete-orphan",
        order_by="ToolConfig.created_at",
    )

    __table_args__ = (
        # Agent names are unique per owner, case-insensitively — the name is what
        # the operator refers to when picking an agent for a tool config, so
        # "Revenue" and "revenue" living side by side would be a trap. Scoped to
        # the user rather than the workspace because the workspace is optional.
        # See the note on uq_workspace_user_name_lower about Alembic and
        # functional indexes.
        Index(
            "uq_data_agent_user_name_lower",
            "user_id",
            text("lower(name)"),
            unique=True,
        ),
    )
