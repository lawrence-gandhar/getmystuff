import uuid
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import String, Boolean, ForeignKey
from sqlalchemy.dialects.postgresql import UUID, JSONB

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

    # encrypted password
    password_encrypted: Mapped[str] = mapped_column(String(500))

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    
    configuration_data: Mapped[dict] = mapped_column(
        JSONB,
        nullable=True
    )