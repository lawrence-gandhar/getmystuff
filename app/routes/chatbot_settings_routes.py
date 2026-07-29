import uuid
from typing import List

from litestar import Controller, get, post
from litestar.connection import Request
from litestar.exceptions import HTTPException
from litestar.response import Response, Template
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.auth import require_auth
from app.models.user import User
from app.services.chatbot_service import (
    build_widget_script,
    create_chatbot_key,
    delete_chatbot_key,
    get_chatbot_key,
    get_conversation_history,
    get_user_chatbot_keys,
    toggle_active_status,
    update_chatbot_key,
)
from app.services.chatbot_widget_settings_service import (
    ALLOWED_HEADER_FONTS,
    ALLOWED_SEND_BUTTON_STYLES,
    WidgetAppearanceInput,
    WidgetImageRemovals,
    WidgetImageUploads,
    get_widget_settings,
    resolve_send_button_icon_url,
    update_widget_settings,
)
from app.services.datasource_service import get_user_datasources


class ChatbotSettingsController(Controller):
    path = "/chatbot-settings"
    dependencies = {"user": require_auth}

    # --------------------------
    # LIST / PAGE
    # --------------------------
    @get("/")
    async def index(self, db: AsyncSession, user: User) -> Template:
        keys = await get_user_chatbot_keys(db, user.id)
        datasources = await get_user_datasources(db=db, user_id=user.id)
        return Template(
            template_name="chatbot_settings/index.htm",
            context={
                "user": user,
                "keys": keys,
                "datasources": datasources,
                "active": "chatbot_settings",
            },
        )

    # --------------------------
    # CREATE
    # --------------------------
    @post("/create")
    async def create(self, request: Request, db: AsyncSession, user: User) -> Template | Response:
        form = await request.form()

        target_type = form.get("target_type", "")
        selections = form.getall("target_selection", [])

        target_names: List[str] = []
        file_ids: List[uuid.UUID] = []

        if target_type == "file":
            try:
                file_ids = [uuid.UUID(s) for s in selections]
            except ValueError:
                return Response(
                    "<div class='alert alert-danger' data-success='false'>Invalid file reference.</div>",
                    media_type="text/html",
                    status_code=200,
                )
        elif target_type in ("table", "collection"):
            target_names = selections

        try:
            datasource_id = uuid.UUID(form.get("datasource_id", ""))
        except ValueError:
            return Response(
                "<div class='alert alert-danger' data-success='false'>Please select a data source.</div>",
                media_type="text/html",
                status_code=200,
            )

        try:
            await create_chatbot_key(
                db=db,
                user_id=user.id,
                name=form.get("name", ""),
                datasource_id=datasource_id,
                target_type=target_type,
                target_names=target_names,
                file_ids=file_ids,
                allowed_origins_raw=form.get("allowed_origins", ""),
            )
        except HTTPException as e:
            return Response(
                f"<div class='alert alert-danger' data-success='false'>{e.detail}</div>",
                media_type="text/html",
                status_code=200,
            )

        keys = await get_user_chatbot_keys(db, user.id)
        return Template(
            template_name="chatbot_settings/key_save_response.htm",
            context={"keys": keys},
        )

    # --------------------------
    # UPDATE (name / allowed origins)
    # --------------------------
    @post("/{key_id:uuid}/update")
    async def update(
        self,
        key_id: uuid.UUID,
        request: Request,
        db: AsyncSession,
        user: User,
    ) -> Template | Response:
        form = await request.form()

        try:
            await update_chatbot_key(
                db=db,
                user_id=user.id,
                key_id=key_id,
                name=form.get("name"),
                allowed_origins_raw=form.get("allowed_origins"),
            )
        except HTTPException as e:
            return Response(
                f"<div class='alert alert-danger' data-success='false'>{e.detail}</div>",
                media_type="text/html",
                status_code=200,
            )

        keys = await get_user_chatbot_keys(db, user.id)
        return Template(
            template_name="chatbot_settings/key_save_response.htm",
            context={"keys": keys},
        )

    # --------------------------
    # TOGGLE ACTIVE / INACTIVE
    # --------------------------
    @post("/{key_id:uuid}/toggle-active")
    async def toggle_active(self, key_id: uuid.UUID, db: AsyncSession, user: User) -> Template:
        error = None
        try:
            await toggle_active_status(db, user.id, key_id)
        except HTTPException as e:
            error = str(e.detail)

        keys = await get_user_chatbot_keys(db, user.id)
        return Template(
            template_name="chatbot_settings/key_rows_response.htm",
            context={"keys": keys, "error": error},
        )

    # --------------------------
    # DELETE
    # --------------------------
    @post("/{key_id:uuid}/delete")
    async def delete(self, key_id: uuid.UUID, db: AsyncSession, user: User) -> Template:
        error = None
        try:
            await delete_chatbot_key(db, user.id, key_id)
        except HTTPException as e:
            error = str(e.detail)

        keys = await get_user_chatbot_keys(db, user.id)
        return Template(
            template_name="chatbot_settings/key_rows_response.htm",
            context={"keys": keys, "error": error},
        )

    # --------------------------
    # CONVERSATION HISTORY
    # --------------------------
    @get("/{key_id:uuid}/history")
    async def history(self, key_id: uuid.UUID, db: AsyncSession, user: User) -> Template:
        entries = await get_conversation_history(db, user.id, key_id)
        return Template(
            template_name="chatbot_settings/history.htm",
            context={"entries": entries},
        )

    # --------------------------
    # WIDGET SETTINGS — branding, background, header style, submit button,
    # widget size, and lifecycle copy
    # --------------------------
    @staticmethod
    def _widget_settings_context(user, key, settings, api_base_url, **extra) -> dict:
        return {
            "user": user,
            "key": key,
            "settings": settings,
            "fonts": ALLOWED_HEADER_FONTS,
            "send_button_styles": ALLOWED_SEND_BUTTON_STYLES,
            "send_button_icon_url": resolve_send_button_icon_url(settings, ""),
            "api_base_url": api_base_url,
            "active": "chatbot_settings",
            **extra,
        }

    @get("/{key_id:uuid}/widget-settings")
    async def widget_settings_page(
        self, key_id: uuid.UUID, request: Request, db: AsyncSession, user: User
    ) -> Template:
        key = await get_chatbot_key(db, user.id, key_id)
        settings = await get_widget_settings(db, user.id, key_id)
        return Template(
            template_name="chatbot_settings/widget_settings.htm",
            context=self._widget_settings_context(user, key, settings, str(request.base_url).rstrip("/")),
        )

    @post("/{key_id:uuid}/widget-settings")
    async def save_widget_settings(
        self,
        key_id: uuid.UUID,
        request: Request,
        db: AsyncSession,
        user: User,
    ) -> Template:
        form = await request.form()

        def _file(field: str):
            uf = form.get(field)
            return uf if uf is not None and getattr(uf, "filename", "") else None

        fields = WidgetAppearanceInput(
            brand_color=form.get("brand_color", ""),
            background_color=form.get("background_color", ""),
            header_background_color=form.get("header_background_color", ""),
            header_font=form.get("header_font", ""),
            welcome_text=form.get("welcome_text", ""),
            idle_text=form.get("idle_text", ""),
            closing_text=form.get("closing_text", ""),
            send_button_style=form.get("send_button_style", ""),
            send_button_text=form.get("send_button_text", ""),
            send_button_font_size=form.get("send_button_font_size", ""),
            send_button_font_color=form.get("send_button_font_color", ""),
            send_button_icon_svg=form.get("send_button_icon_svg", ""),
            send_button_border_radius=form.get("send_button_border_radius", ""),
            input_border_radius=form.get("input_border_radius", ""),
            watermark_opacity=form.get("watermark_opacity", ""),
            bot_message_bg_color=form.get("bot_message_bg_color", ""),
            bot_message_text_color=form.get("bot_message_text_color", ""),
            user_message_text_color=form.get("user_message_text_color", ""),
            widget_width=form.get("widget_width", ""),
            widget_height=form.get("widget_height", ""),
        )
        uploads = WidgetImageUploads(
            logo=_file("logo"),
            background_image=_file("background_image"),
            bot_icon=_file("bot_icon"),
            send_button_icon=_file("send_button_icon"),
            watermark_image=_file("watermark_image"),
        )
        removals = WidgetImageRemovals(
            logo=form.get("remove_logo") == "on",
            background_image=form.get("remove_background_image") == "on",
            bot_icon=form.get("remove_bot_icon") == "on",
            send_button_icon=form.get("remove_send_button_icon") == "on",
            watermark_image=form.get("remove_watermark_image") == "on",
        )

        error = None
        try:
            await update_widget_settings(db, user.id, key_id, fields, uploads, removals)
        except HTTPException as e:
            error = str(e.detail)

        key = await get_chatbot_key(db, user.id, key_id)
        settings = await get_widget_settings(db, user.id, key_id)
        return Template(
            template_name="chatbot_settings/widget_settings.htm",
            context=self._widget_settings_context(
                user, key, settings, str(request.base_url).rstrip("/"),
                error=error, success=error is None,
            ),
        )

    # --------------------------
    # DOWNLOAD WIDGET JS
    # --------------------------
    @get("/{key_id:uuid}/widget.js")
    async def download_widget(
        self,
        key_id: uuid.UUID,
        db: AsyncSession,
        user: User,
    ) -> Response:
        await get_chatbot_key(db, user.id, key_id)  # ownership check (404 if not owned)
        script = build_widget_script()

        return Response(
            content=script,
            media_type="application/javascript",
            # Always the same filename — the file is now generic (config is
            # fetched at runtime, see chatbot_service.build_widget_script), so
            # it can be re-downloaded and dropped into the same path on the
            # embedder's site without updating their <script src> reference.
            headers={"Content-Disposition": 'attachment; filename="widget.js"'},
        )
