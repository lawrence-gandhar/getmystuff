from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import String, Boolean, DateTime, func
from sqlalchemy.dialects.postgresql import UUID
import uuid

from app.db.base import Base

class User(Base):
    __tablename__ = "users"

    # UUID primary key (real UUID type)
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4
    )

    # Email (indexed + unique)
    email: Mapped[str] = mapped_column(
        String(255),
        unique=True,
        index=True,
        nullable=False
    )

    # Hashed password
    password: Mapped[str] = mapped_column(
        String(255),
        nullable=False
    )

    # Optional role (admin, user, etc.)
    role: Mapped[str] = mapped_column(
        String(50),
        default="user"
    )

    # Account status
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True
    )

    # Timestamps
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now()
    )

    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now()
    )