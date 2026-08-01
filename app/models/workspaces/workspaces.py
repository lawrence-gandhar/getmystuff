"""
Workspaces — a user-owned grouping for data agents.

Workspaces, Data Agents and Tool Configs are three independent modules, each with
its own sidebar entry and its own CRUD:

    Workspace   (here)                          grouping, owns nothing outright
    DataAgent   (app.models.data_agents)        optionally assigned to a workspace
    ToolConfig  (app.models.tool_configs)       belongs to exactly one data agent

The association points *up*: ``data_agents.workspace_id`` is nullable, so an agent
can exist without a workspace and be moved between them. That is why a workspace
does not cascade-delete its agents — see the relationship note below.
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


class Workspace(Base):
    """
    A user-owned container that groups data agents for one project, team or
    environment.

    ``is_active`` is an archive switch, not a delete: an inactive workspace stays
    listed with its agents intact, but the Data Agents module refuses to assign
    new agents to it (see data_agent_service), so work parked here can be resumed
    rather than rebuilt.
    """
    __tablename__ = "workspaces"

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

    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

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

    # No delete cascade: the FK on the agent side is ON DELETE SET NULL, so
    # deleting a workspace unassigns its agents instead of destroying them. An
    # agent is an independent, separately-managed thing that merely *belongs to* a
    # workspace — the same relationship ChatbotFlow has with a chatbot.
    data_agents = relationship(
        "DataAgent",
        back_populates="workspace",
        passive_deletes=True,
    )

    __table_args__ = (
        # Case-insensitive uniqueness per owner: two users may both have a
        # "Sales" workspace, one user may not. Functional indexes are invisible
        # to Alembic autogenerate (same caveat as uq_datasource_name_lower) —
        # the hand-written migration e5b8c1d47f22 creates this explicitly.
        Index(
            "uq_workspace_user_name_lower",
            "user_id",
            text("lower(name)"),
            unique=True,
        ),
    )
