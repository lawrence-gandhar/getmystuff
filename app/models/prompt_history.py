import uuid

from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import String, Text, DateTime, ForeignKey, Index
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.sql import func

from app.db.base import Base


class PromptHistory(Base):
    """
    Stores every AI analytics prompt a user runs against a datasource object
    (a file, an RDBMS table, or a MongoDB collection) together with the
    generated response, so past analyses can be revisited.
    """
    __tablename__ = "prompt_history"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    datasource_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("datasources.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # "file" | "table" | "collection"
    target_type: Mapped[str] = mapped_column(String(20), nullable=False)

    # File original_filename / RDBMS table name / Mongo collection name.
    target_name: Mapped[str] = mapped_column(String(500), nullable=False, index=True)

    # Only set when target_type == "file" — the specific DatasourceFile version
    # the analysis was run against.
    file_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("datasource_files.id", ondelete="SET NULL"),
        nullable=True,
    )

    prompt: Mapped[str] = mapped_column(Text, nullable=False)

    # "success" | "error"
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="success")

    # Markdown/plain-text narrative answer from Claude.
    summary: Mapped[str] = mapped_column(Text, nullable=True)

    # Short bullet-point insights: ["...", "..."].
    insights: Mapped[list] = mapped_column(JSONB, nullable=True)

    # Structured, computed analytics grounded in the real data:
    # {"columns": [...], "rows": [[...], ...]} or None when the answer was
    # narrative-only.
    result_table: Mapped[dict] = mapped_column(JSONB, nullable=True)

    error_message: Mapped[str] = mapped_column(Text, nullable=True)

    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        index=True,
    )

    __table_args__ = (
        Index("ix_prompt_history_target", "datasource_id", "target_type", "target_name"),
    )
