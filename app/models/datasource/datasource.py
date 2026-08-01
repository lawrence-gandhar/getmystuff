import uuid as uuid_pkg
from typing import Optional, List
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy import BigInteger, String, Boolean, Integer, ForeignKey, UniqueConstraint, DateTime, Index, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.sql import func

from app.db.base import Base


class DataSource(Base):
    __tablename__ = "datasources"

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
        nullable=False
    )

    # Human-readable unique identifier.
    # Stored normalized (lowercase, alphanum + underscore only).
    # The functional unique index uq_datasource_name_lower enforces
    # case-insensitive uniqueness at the database level.
    datasource_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        index=True,
    )

    db_type: Mapped[str] = mapped_column(String(50))
    host: Mapped[str] = mapped_column(String(255), nullable=True)
    port: Mapped[str] = mapped_column(String(20), nullable=True)
    database_name: Mapped[str] = mapped_column(String(255), nullable=True)
    username: Mapped[str] = mapped_column(String(255), nullable=True)
    password_encrypted: Mapped[str] = mapped_column(String(500))

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    configuration_data: Mapped[dict] = mapped_column(
        MutableDict.as_mutable(JSONB),
        nullable=True
    )

    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        index=True
    )

    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        index=True
    )

    tool_base_configs = relationship(
        "DatasourceToolBaseConfig",
        back_populates="datasource",
        cascade="all, delete-orphan"
    )

    files = relationship(
        "DatasourceFile",
        back_populates="datasource",
        cascade="all, delete-orphan",
        order_by="DatasourceFile.uploaded_at",
    )

    # Functional unique index: enforces case-insensitive uniqueness on
    # datasource_name *per owner*.  Alembic autogenerate does not detect
    # functional indexes — the hand-written migrations create them explicitly
    # (c7d1f4a8e3b9_add_datasource_name added the original, and
    # b1f7c2d94a05_scope_datasource_name_to_user replaced it with this one).
    #
    # user_id is part of the index deliberately.  Without it the name is unique
    # across every tenant, so one customer creating "sales_data" would stop every
    # other customer from ever using that name — and the conflict message would
    # name a row they cannot see.  This matches the scoping already used by
    # uq_workspace_user_name_lower, uq_data_agent_user_name_lower and
    # uq_tool_config_agent_name_lower.
    __table_args__ = (
        Index(
            "uq_datasource_user_name_lower",
            "user_id",
            text("lower(datasource_name)"),
            unique=True,
        ),
    )

class DatasourceToolBaseConfig(Base):
    __tablename__ = "datasource_base_config"

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

    datasource_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("datasources.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    tool_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        index=True
    )

    table_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        index=True
    )

    base_config: Mapped[dict] = mapped_column(
        MutableDict.as_mutable(JSONB),
        nullable=False,
        default=dict
    )

    version: Mapped[int] = mapped_column(
        default=1,
        index=True
    )

    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        index=True
    )

    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        index=True
    )

    datasource = relationship(
        "DataSource",
        back_populates="tool_base_configs"
    )

    agent_configs = relationship(
        "DataSourceAgentConfig",
        back_populates="base_config",
        cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint(
            "datasource_id",
            "table_name",
            "tool_name",
            name="uq_ds_table_tool_base"
        ),
    )

class DataSourceAgentConfig(Base):
    __tablename__ = "datasource_tool_config"

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

    datasource_base_config_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("datasource_base_config.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    agent_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        index=True
    )

    table_column: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
        index=True
    )

    config: Mapped[dict] = mapped_column(
        MutableDict.as_mutable(JSONB),
        nullable=False,
        default=dict
    )

    policy_json: Mapped[dict] = mapped_column(
        MutableDict.as_mutable(JSONB),
        nullable=False,
        default=dict
    )

    version: Mapped[int] = mapped_column(
        default=1,
        index=True
    )

    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        index=True
    )

    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now()
    )

    base_config = relationship(
        "DatasourceToolBaseConfig",
        back_populates="agent_configs"
    )

    __table_args__ = (
        UniqueConstraint(
            "datasource_base_config_id",
            "agent_name",
            name="uq_base_agent_override"
        ),
    )


class DatasourceFile(Base):
    """
    Tracks every file uploaded to a file-based datasource.

    Versioning:
      • ``normalized_base_name`` is the logical file identity
        (e.g. "sales_data.csv").  All versions of the same logical file
        share this value.
      • ``stored_filename`` is the physical name on disk
        (e.g. "sales_data.csv" for v1, "sales_data_v2.csv" for v2, …).
      • Only one record per ``(datasource_id, normalized_base_name)``
        should have ``is_active=True`` at any time.
    """
    __tablename__ = "datasource_files"

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

    datasource_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("datasources.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    uploaded_by: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Original name as supplied by the user.
    original_filename: Mapped[str] = mapped_column(String(500), nullable=False)

    # Physical filename on disk (may include _v2, _v3 suffix for newer versions).
    stored_filename: Mapped[str] = mapped_column(String(500), nullable=False)

    # Normalized form of original_filename — used as the logical file key
    # when resolving versions.
    normalized_base_name: Mapped[str] = mapped_column(
        String(500),
        nullable=False,
        index=True,
    )

    # Absolute (or project-relative) path to the stored file.
    file_path: Mapped[str] = mapped_column(String(1000), nullable=False)

    # SHA-256 hex digest; NULL when the file could not be read after write.
    checksum: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    # Monotonically increasing per logical file; starts at 1.
    version: Mapped[int] = mapped_column(
        Integer,
        default=1,
        nullable=False,
        index=True,
    )

    # Only the most-recent active record is used for queries / previews.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    uploaded_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        index=True,
    )

    datasource = relationship("DataSource", back_populates="files")
