import uuid
from typing import Optional

from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import String, Boolean, DateTime, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from app.db.base import Base

# (value, display label) — value is what's stored/validated against;
# display label is what the "Provider" dropdown and table badges show.
AI_PROVIDERS = (
    ("anthropic", "Anthropic (Claude)"),
    ("openai", "OpenAI"),
    ("google_gemini", "Google Gemini"),
    ("azure_openai", "Azure OpenAI"),
    ("other", "Other"),
)
AI_PROVIDER_VALUES = frozenset(value for value, _ in AI_PROVIDERS)
AI_PROVIDER_DISPLAY = dict(AI_PROVIDERS)


class AIApiKey(Base):
    """
    A user-managed API key for an AI provider (Anthropic, OpenAI, ...).

    Only one key per (user_id, provider) may be active at a time — enforced
    in the service layer, not the schema, since "active" is a business rule
    (deactivate siblings on activation) rather than a uniqueness constraint.
    """
    __tablename__ = "ai_api_keys"

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

    provider: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    label: Mapped[str] = mapped_column(String(255), nullable=False)

    # Fernet-encrypted secret (see app/utils/crypto.py) — never stored or
    # returned in plaintext.
    api_key_encrypted: Mapped[str] = mapped_column(String(1000), nullable=False)

    is_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Only used for OpenAI-compatible providers ("openai" / "other"). Lets a
    # user point at an OpenAI-compatible endpoint (Cerebras, Groq, Together,
    # a self-hosted OpenAI-compatible server, ...) and pick its model name.
    # Unused (and left blank) for "anthropic".
    base_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    model_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)

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
