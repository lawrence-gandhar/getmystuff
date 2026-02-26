import uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy import String, Boolean, ForeignKey, UniqueConstraint, DateTime
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.sql import func

from app.db.base import Base


class DataSource(Base):
    __tablename__ = "datasources"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False
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

class DatasourceToolBaseConfig(Base):
    __tablename__ = "datasource_base_config"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4
    )

    datasource_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("datasources.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    tool_name: Mapped[str] = mapped_column(
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
            "tool_name",
            name="uq_ds_tool_base"
        ),
    )

class DataSourceAgentConfig(Base):
    __tablename__ = "datasource_tool_config"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4
    )

    datasource_base_config_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("datasource_base_config.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    agent_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        index=True
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


