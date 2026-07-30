import secrets
import uuid as uuid_pkg
from typing import Optional

from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import BigInteger, String, Text, Boolean, DateTime, ForeignKey
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.sql import func

from app.db.base import Base


def generate_chatbot_key() -> str:
    """
    A publishable widget key — unlike AI Settings provider keys, this is
    meant to be embedded in a downloadable client-side JS file, so it's
    generated as a plain (non-secret) token rather than encrypted at rest.
    """
    return "cb_pub_" + secrets.token_urlsafe(32)


class ChatbotApiKey(Base):
    """
    A publishable key that powers one embeddable chatbot widget, scoped to
    one or more datasource targets (the whole datasource, one or more
    tables/collections, or one or more files) chosen at creation time, and
    locked to an allow-list of embedding origins — since the key itself is
    visible to anyone who views the embedding page's source, scope + origin
    restriction (not secrecy) is the real security boundary here.
    """
    __tablename__ = "chatbot_api_keys"

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

    name: Mapped[str] = mapped_column(String(255), nullable=False)

    api_key: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        unique=True,
        index=True,
        default=generate_chatbot_key,
    )

    datasource_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("datasources.id", ondelete="CASCADE"),
        nullable=False,
    )

    # "datasource" (whole datasource) | "table" | "collection" | "file"
    target_type: Mapped[str] = mapped_column(String(20), nullable=False)

    # Table/collection names, or (for "file") the resolved display names of
    # the selected files. Empty when target_type == "datasource".
    target_names: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    # File ids, only populated when target_type == "file",
    # index-aligned with target_names. Empty otherwise.
    file_ids: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    # List of allowed embedding origins, e.g. ["https://mysite.com"].
    # Requests whose Origin header isn't in this list are rejected.
    allowed_origins: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

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
    )


class ChatbotWidgetSettings(Base):
    """
    Visual/behavioral customization for one embeddable chatbot widget —
    branding (logo, brand color), background (color or image), header style
    (background color, font, bot icon), and lifecycle copy (welcome / idle /
    closing text). One-to-one with ChatbotApiKey; applied when the widget
    script is generated (see chatbot_service.build_widget_script).
    """
    __tablename__ = "chatbot_widget_settings"

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
        unique=True,
        index=True,
    )

    # ---- Branding ----
    logo_path: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    brand_color: Mapped[str] = mapped_column(String(20), nullable=False, default="#2563eb")

    # ---- Background ----
    background_color: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    background_image_path: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    # ---- Watermark (overlaid on the conversation area, behind messages) ----
    watermark_image_path: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    watermark_opacity: Mapped[int] = mapped_column(nullable=False, default=15)  # percent, 0-100

    # ---- Header style ----
    header_background_color: Mapped[str] = mapped_column(String(20), nullable=False, default="#2563eb")
    header_font: Mapped[str] = mapped_column(String(100), nullable=False, default="system-ui")
    bot_icon_path: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    # ---- Message colors ----
    bot_message_bg_color: Mapped[str] = mapped_column(String(20), nullable=False, default="#e9ecef")
    bot_message_text_color: Mapped[str] = mapped_column(String(20), nullable=False, default="#212529")
    user_message_text_color: Mapped[str] = mapped_column(String(20), nullable=False, default="#ffffff")

    # ---- Lifecycle copy ----
    welcome_text: Mapped[str] = mapped_column(
        Text, nullable=False, default="Hi! How can I help you today?"
    )
    idle_text: Mapped[str] = mapped_column(
        Text, nullable=False, default="Still there? Ask me anything about your data."
    )
    closing_text: Mapped[str] = mapped_column(
        Text, nullable=False, default="Thanks for chatting! Have a great day."
    )

    # ---- Send button ----
    # "icon-only" | "text-only" | "icon-with-text"
    send_button_style: Mapped[str] = mapped_column(String(20), nullable=False, default="text-only")
    # At most one of these two is populated at a time — an upload always
    # replaces a pasted SVG and vice versa (see chatbot_widget_settings_service).
    send_button_icon_path: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    send_button_icon_svg: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    send_button_text: Mapped[str] = mapped_column(String(50), nullable=False, default="Send")
    send_button_font_size: Mapped[int] = mapped_column(nullable=False, default=13)
    send_button_font_color: Mapped[str] = mapped_column(String(20), nullable=False, default="#ffffff")
    send_button_border_radius: Mapped[int] = mapped_column(nullable=False, default=6)  # px

    # ---- Chat input box ----
    input_border_radius: Mapped[int] = mapped_column(nullable=False, default=6)  # px

    # ---- Widget size (px) ----
    widget_width: Mapped[int] = mapped_column(nullable=False, default=340)
    widget_height: Mapped[int] = mapped_column(nullable=False, default=460)

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


class ChatbotMessage(Base):
    """A single visitor question + AI answer exchanged through a chatbot widget."""
    __tablename__ = "chatbot_messages"

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

    visitor_message: Mapped[str] = mapped_column(Text, nullable=False)

    # "success" | "error"
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="success")

    # {"summary": str, "insights": [str, ...], "table": {...} | None}
    ai_response: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        index=True,
    )
