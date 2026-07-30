"""
Business logic for embeddable chatbot widget branding/behavior — logo,
brand color, background (color, image, and an overlaid watermark), header
style (background color, font, bot icon), message colors (AI response vs.
visitor prompt), lifecycle copy (welcome / idle / closing text), the submit
button (icon-only / text-only / icon-with-text, with its own icon/text/font/
border radius), the chat input's border radius, and the widget panel's
width/height.

One ChatbotWidgetSettings row per ChatbotApiKey, created lazily with
defaults on first access. Image uploads are stored under
static/chatbot_widgets/<key_uuid>/ so the embedded widget script — which
runs on third-party sites — can fetch them over a public URL (see
app.utils.file_utils.ensure_widget_upload_dir).

Pasted SVG (for the submit button icon) is untrusted input that ends up
rendered inside every visitor's browser on every site the widget is
embedded on. Rather than trying to regex-scrub arbitrary markup (a known
anti-pattern for HTML/XML sanitization — regex can't reliably reason about
nesting), it's parsed as real XML via the stdlib ``xml.etree.ElementTree``,
stripped of script-capable elements/attributes, and re-serialized; anything
that doesn't parse as well-formed XML is rejected outright. The result is
then only ever exposed to clients as a base64 data: URI for an <img> tag —
never injected as raw markup — so no path here can execute attacker-
controlled script even if a sanitizer bypass slipped through.
"""

import base64
import re
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from litestar.exceptions import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.db_utils import CRUDQueryBuilder
from app.models.chatbot import ChatbotWidgetSettings
from app.services.chatbot.chatbot_service import get_chatbot_key
from app.utils.file_utils import (
    ALLOWED_IMAGE_EXTENSIONS,
    MAX_IMAGE_SIZE_BYTES,
    ensure_widget_upload_dir,
    normalize_filename,
)

widget_settings_crud = CRUDQueryBuilder(ChatbotWidgetSettings)

_HEX_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
_MAX_TEXT_LEN = 300

ALLOWED_HEADER_FONTS = (
    "system-ui", "Arial", "Helvetica", "Georgia", "Verdana", "Tahoma",
    "Trebuchet MS", "Courier New", "Times New Roman",
)

ALLOWED_SEND_BUTTON_STYLES = ("icon-only", "text-only", "icon-with-text")

_SEND_BUTTON_FONT_SIZE_RANGE = (10, 24)
_WIDGET_WIDTH_RANGE = (280, 600)
_WIDGET_HEIGHT_RANGE = (320, 800)
_WATERMARK_OPACITY_RANGE = (0, 100)
_BORDER_RADIUS_RANGE = (0, 30)

_MAX_SVG_LEN = 20_000
# Elements capable of running script or loading arbitrary embedded HTML.
_DISALLOWED_SVG_TAGS = {"script", "foreignobject", "iframe", "object", "embed", "style"}

ET.register_namespace("", "http://www.w3.org/2000/svg")
ET.register_namespace("xlink", "http://www.w3.org/1999/xlink")


# --------------------------------------------------------------------------
# Input shapes (grouped to keep update_widget_settings' signature sane)
# --------------------------------------------------------------------------

@dataclass
class WidgetAppearanceInput:
    brand_color: str
    background_color: str
    header_background_color: str
    header_font: str
    welcome_text: str
    idle_text: str
    closing_text: str
    send_button_style: str
    send_button_text: str
    send_button_font_size: str
    send_button_font_color: str
    send_button_icon_svg: str
    send_button_border_radius: str
    input_border_radius: str
    watermark_opacity: str
    bot_message_bg_color: str
    bot_message_text_color: str
    user_message_text_color: str
    widget_width: str
    widget_height: str


@dataclass
class WidgetImageUploads:
    logo: object = None
    background_image: object = None
    bot_icon: object = None
    send_button_icon: object = None
    watermark_image: object = None


@dataclass
class WidgetImageRemovals:
    logo: bool = False
    background_image: bool = False
    bot_icon: bool = False
    send_button_icon: bool = False
    watermark_image: bool = False


# --------------------------------------------------------------------------
# Validation helpers
# --------------------------------------------------------------------------

def _validate_color(value: str, label: str) -> str:
    value = (value or "").strip()
    if not _HEX_COLOR_RE.match(value):
        raise HTTPException(status_code=400, detail=f"{label} must be a valid hex color, e.g. #2563eb")
    return value


def _validate_text(value: str, label: str) -> str:
    value = (value or "").strip()
    if not value:
        raise HTTPException(status_code=400, detail=f"{label} is required")
    if len(value) > _MAX_TEXT_LEN:
        raise HTTPException(status_code=400, detail=f"{label} must not exceed {_MAX_TEXT_LEN} characters")
    return value


def _validate_choice(value: str, label: str, choices) -> str:
    value = (value or "").strip()
    if value not in choices:
        raise HTTPException(status_code=400, detail=f"Invalid {label}: {value!r}")
    return value


def _validate_int(value: str, label: str, min_value: int, max_value: int) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"{label} must be a whole number")
    if not (min_value <= parsed <= max_value):
        raise HTTPException(
            status_code=400,
            detail=f"{label} must be between {min_value} and {max_value}",
        )
    return parsed


def _local_name(qualified_name: str) -> str:
    """Strip an XML namespace off a tag/attribute name: '{uri}script' -> 'script'."""
    return qualified_name.rsplit("}", 1)[-1].lower()


def _strip_active_content(element: ET.Element) -> None:
    """Recursively remove script-capable attributes and child elements in place."""
    for attr_name, value in list(element.attrib.items()):
        local = _local_name(attr_name)
        if local.startswith("on") or (local == "href" and value.strip().lower().startswith("javascript:")):
            del element.attrib[attr_name]

    for child in list(element):
        if _local_name(child.tag) in _DISALLOWED_SVG_TAGS:
            element.remove(child)
        else:
            _strip_active_content(child)


def _sanitize_svg(raw: str) -> str:
    """
    Validate + strip active content from a user-pasted SVG icon by parsing
    it as XML (not regex) and rebuilding it from the cleaned tree. Anything
    that isn't well-formed XML, or whose root isn't an <svg> element, is
    rejected. Returns "" for blank input.
    """
    svg = (raw or "").strip()
    if not svg:
        return ""

    if len(svg) > _MAX_SVG_LEN:
        raise HTTPException(status_code=400, detail=f"Pasted SVG must not exceed {_MAX_SVG_LEN} characters")

    try:
        root = ET.fromstring(svg)
    except ET.ParseError:
        raise HTTPException(status_code=400, detail="Pasted icon must be valid, well-formed SVG markup")

    if _local_name(root.tag) != "svg":
        raise HTTPException(status_code=400, detail="Pasted icon must be an <svg> element")

    _strip_active_content(root)

    return ET.tostring(root, encoding="unicode")


# --------------------------------------------------------------------------
# Read
# --------------------------------------------------------------------------

async def get_widget_settings(db: AsyncSession, user_id: int, key_id: uuid.UUID) -> ChatbotWidgetSettings:
    """Ownership-checked fetch; creates a row with defaults on first access."""
    key = await get_chatbot_key(db, user_id, key_id)  # 404s if not owned

    existing = await widget_settings_crud.get_one(db, filters={"chatbot_key_id": key.id})
    if existing:
        return existing

    return await widget_settings_crud.create(db, {"chatbot_key_id": key.id})


async def get_widget_settings_by_key_id(
    db: AsyncSession, chatbot_key_id: int
) -> Optional[ChatbotWidgetSettings]:
    """
    Public lookup — no ownership check. Used by the public widget-config
    endpoint that the embedded widget script calls at runtime, so a visitor's
    browser always renders the widget's current settings without needing a
    re-downloaded widget.js after every dashboard change.
    """
    return await widget_settings_crud.get_one(db, filters={"chatbot_key_id": chatbot_key_id})


def _abs_url(path: Optional[str], api_base_url: str) -> str:
    return f"{api_base_url}{path}" if path else ""


def resolve_send_button_icon_url(settings: Optional[ChatbotWidgetSettings], api_base_url: str) -> str:
    """
    A single displayable URL for the submit button icon, regardless of
    whether it came from an upload or a pasted SVG. Uploaded images resolve
    to an absolute /static path; pasted SVG is returned as a base64
    data: URI (never as raw markup) so no caller can accidentally render it
    in a way that executes embedded script.
    """
    if not settings:
        return ""
    if settings.send_button_icon_path:
        return _abs_url(settings.send_button_icon_path, api_base_url)
    if settings.send_button_icon_svg:
        encoded = base64.b64encode(settings.send_button_icon_svg.encode("utf-8")).decode("ascii")
        return f"data:image/svg+xml;base64,{encoded}"
    return ""


_DEFAULT_WIDGET_CONFIG = {
    "brand_color": "#2563eb",
    "header_background_color": "#2563eb",
    "header_font": "system-ui",
    "background_color": "#f8f9fa",
    "background_image_url": "",
    "watermark_image_url": "",
    "watermark_opacity": 15,
    "logo_url": "",
    "bot_icon_url": "",
    "bot_message_bg_color": "#e9ecef",
    "bot_message_text_color": "#212529",
    "user_message_text_color": "#ffffff",
    "welcome_text": "Hi! How can I help you today?",
    "idle_text": "",
    "closing_text": "",
    "send_button_style": "text-only",
    "send_button_text": "Send",
    "send_button_font_size": 13,
    "send_button_font_color": "#ffffff",
    "send_button_icon_url": "",
    "send_button_border_radius": 6,
    "input_border_radius": 6,
    "widget_width": 340,
    "widget_height": 460,
}


def build_widget_public_config(
    settings: Optional[ChatbotWidgetSettings], title: str, api_base_url: str
) -> dict:
    """
    Build the JSON payload the widget script fetches at runtime (see
    GET /public/chatbot/widget-config). Centralized here — rather than a
    string of ``field if settings else default`` ternaries in the route —
    both because business logic belongs in the service layer, not routes,
    and because this is the one place the widget's default look/copy is
    defined.
    """
    config = dict(_DEFAULT_WIDGET_CONFIG)
    config["title"] = title

    if settings:
        config.update({
            "brand_color": settings.brand_color,
            "header_background_color": settings.header_background_color,
            "header_font": settings.header_font,
            "background_color": settings.background_color or _DEFAULT_WIDGET_CONFIG["background_color"],
            "background_image_url": _abs_url(settings.background_image_path, api_base_url),
            "watermark_image_url": _abs_url(settings.watermark_image_path, api_base_url),
            "watermark_opacity": settings.watermark_opacity,
            "logo_url": _abs_url(settings.logo_path, api_base_url),
            "bot_icon_url": _abs_url(settings.bot_icon_path, api_base_url),
            "bot_message_bg_color": settings.bot_message_bg_color,
            "bot_message_text_color": settings.bot_message_text_color,
            "user_message_text_color": settings.user_message_text_color,
            "welcome_text": settings.welcome_text,
            "idle_text": settings.idle_text,
            "closing_text": settings.closing_text,
            "send_button_style": settings.send_button_style,
            "send_button_text": settings.send_button_text,
            "send_button_font_size": settings.send_button_font_size,
            "send_button_font_color": settings.send_button_font_color,
            "send_button_icon_url": resolve_send_button_icon_url(settings, api_base_url),
            "send_button_border_radius": settings.send_button_border_radius,
            "input_border_radius": settings.input_border_radius,
            "widget_width": settings.widget_width,
            "widget_height": settings.widget_height,
        })

    return config


# --------------------------------------------------------------------------
# Write
# --------------------------------------------------------------------------

async def _save_image(key_uuid: uuid.UUID, field_label: str, upload) -> Optional[str]:
    """
    Validate + persist one uploaded image, returning its public /static path.
    Returns None when no file was submitted for this field.
    """
    if upload is None or not getattr(upload, "filename", ""):
        return None

    content = await upload.read()
    if not content:
        return None

    if len(content) > MAX_IMAGE_SIZE_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"{field_label} must not exceed {MAX_IMAGE_SIZE_BYTES // (1024 * 1024)}MB",
        )

    normalized = normalize_filename(upload.filename)
    ext = Path(normalized).suffix.lstrip(".")
    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{field_label}: unsupported image type '.{ext}'. "
                f"Allowed: {', '.join(sorted(ALLOWED_IMAGE_EXTENSIONS))}"
            ),
        )

    upload_dir = ensure_widget_upload_dir(str(key_uuid))
    stored_name = f"{field_label.lower().replace(' ', '_')}_{normalized}"
    file_path = upload_dir / stored_name
    file_path.write_bytes(content)

    return f"/static/chatbot_widgets/{key_uuid}/{stored_name}"


def _apply_image_field(data: dict, path_key: str, new_path: Optional[str], remove: bool) -> None:
    if new_path:
        data[path_key] = new_path
    elif remove:
        data[path_key] = None


def _apply_send_button_text_fields(data: dict, fields: WidgetAppearanceInput) -> None:
    data["send_button_text"] = _validate_text(fields.send_button_text, "Submit button text")
    data["send_button_font_size"] = _validate_int(
        fields.send_button_font_size, "Submit button font size", *_SEND_BUTTON_FONT_SIZE_RANGE
    )
    data["send_button_font_color"] = _validate_color(fields.send_button_font_color, "Submit button font color")


async def _apply_send_button_icon(
    data: dict,
    key_uuid: uuid.UUID,
    fields: WidgetAppearanceInput,
    uploads: WidgetImageUploads,
    removals: WidgetImageRemovals,
) -> None:
    """Upload / pasted SVG / removal are mutually exclusive — whichever was submitted this request wins."""
    send_icon_path = await _save_image(key_uuid, "send_button_icon", uploads.send_button_icon)
    pasted_svg = _sanitize_svg(fields.send_button_icon_svg)

    if send_icon_path:
        data["send_button_icon_path"] = send_icon_path
        data["send_button_icon_svg"] = None
    elif pasted_svg:
        data["send_button_icon_svg"] = pasted_svg
        data["send_button_icon_path"] = None
    elif removals.send_button_icon:
        data["send_button_icon_path"] = None
        data["send_button_icon_svg"] = None


async def update_widget_settings(
    db: AsyncSession,
    user_id: int,
    key_id: uuid.UUID,
    fields: WidgetAppearanceInput,
    uploads: WidgetImageUploads,
    removals: WidgetImageRemovals,
) -> ChatbotWidgetSettings:
    key = await get_chatbot_key(db, user_id, key_id)  # ownership check
    settings = await get_widget_settings(db, user_id, key_id)

    header_font = (fields.header_font or "").strip()
    if header_font not in ALLOWED_HEADER_FONTS:
        raise HTTPException(status_code=400, detail=f"Invalid header font: {header_font!r}")

    send_button_style = _validate_choice(
        fields.send_button_style, "submit button style", ALLOWED_SEND_BUTTON_STYLES
    )

    data = {
        "brand_color": _validate_color(fields.brand_color, "Brand color"),
        "header_background_color": _validate_color(fields.header_background_color, "Header background color"),
        "header_font": header_font,
        "welcome_text": _validate_text(fields.welcome_text, "Welcome text"),
        "idle_text": _validate_text(fields.idle_text, "Idle text"),
        "closing_text": _validate_text(fields.closing_text, "Closing text"),
        "send_button_style": send_button_style,
        "send_button_border_radius": _validate_int(
            fields.send_button_border_radius, "Submit button border radius", *_BORDER_RADIUS_RANGE
        ),
        "input_border_radius": _validate_int(
            fields.input_border_radius, "Chat input border radius", *_BORDER_RADIUS_RANGE
        ),
        "watermark_opacity": _validate_int(
            fields.watermark_opacity, "Watermark opacity", *_WATERMARK_OPACITY_RANGE
        ),
        "bot_message_bg_color": _validate_color(fields.bot_message_bg_color, "AI response background color"),
        "bot_message_text_color": _validate_color(fields.bot_message_text_color, "AI response text color"),
        "user_message_text_color": _validate_color(fields.user_message_text_color, "Prompt text color"),
        "widget_width": _validate_int(fields.widget_width, "Widget width", *_WIDGET_WIDTH_RANGE),
        "widget_height": _validate_int(fields.widget_height, "Widget height", *_WIDGET_HEIGHT_RANGE),
    }

    background_color = (fields.background_color or "").strip()
    data["background_color"] = _validate_color(background_color, "Background color") if background_color else None

    if send_button_style in ("text-only", "icon-with-text"):
        _apply_send_button_text_fields(data, fields)

    _apply_image_field(data, "logo_path", await _save_image(key.uuid, "logo", uploads.logo), removals.logo)
    _apply_image_field(
        data,
        "background_image_path",
        await _save_image(key.uuid, "background", uploads.background_image),
        removals.background_image,
    )
    _apply_image_field(
        data,
        "watermark_image_path",
        await _save_image(key.uuid, "watermark", uploads.watermark_image),
        removals.watermark_image,
    )
    _apply_image_field(
        data, "bot_icon_path", await _save_image(key.uuid, "bot_icon", uploads.bot_icon), removals.bot_icon
    )
    await _apply_send_button_icon(data, key.uuid, fields, uploads, removals)

    if send_button_style in ("icon-only", "icon-with-text"):
        final_icon_path = data.get("send_button_icon_path", settings.send_button_icon_path)
        final_icon_svg = data.get("send_button_icon_svg", settings.send_button_icon_svg)
        if not (final_icon_path or final_icon_svg):
            raise HTTPException(
                status_code=400,
                detail="Upload an icon file or paste SVG markup for an icon submit button style",
            )

    return await widget_settings_crud.update(db, settings.id, data)
