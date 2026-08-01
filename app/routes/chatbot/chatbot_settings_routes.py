import uuid
from typing import List

from litestar import Controller, get, post
from litestar.connection import Request
from litestar.exceptions import HTTPException
from litestar.response import Response, Template
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.auth import require_auth
from app.models.chatbot import DEFAULT_SYSTEM_PROMPT, LLM_MODES
from app.models.user import User
from app.routes.chatbot.action_routes import action_form_context, read_action_form
from app.services.ai_settings.ai_settings_service import get_user_api_keys
from app.services.chatbot.chatbot_action_service import (
    attach_action,
    build_action_views,
    create_and_attach_action,
    detach_action,
    get_actions_for_chatbot,
    get_attachable_actions,
)
from app.services.chatbot.chatbot_ai_settings_service import (
    AiSettingsInput,
    get_ai_settings,
    reset_system_prompt,
    update_ai_settings,
)
from app.services.chatbot.chatbot_service import (
    build_widget_script,
    create_chatbot_key,
    delete_chatbot_key,
    get_chatbot_key,
    get_conversation_history,
    get_user_chatbot_keys,
    set_chatbot_data_agent,
    toggle_active_status,
    update_chatbot_key,
)
from app.services.data_agents import data_agent_service
from app.services.chatbot.chatbot_widget_settings_service import (
    ALLOWED_HEADER_FONTS,
    ALLOWED_SEND_BUTTON_STYLES,
    WidgetAppearanceInput,
    WidgetImageRemovals,
    WidgetImageUploads,
    get_widget_settings,
    resolve_send_button_icon_url,
    update_widget_settings,
)
from app.services.datasource.datasource_service import get_user_datasources
from app.services.flow_builder import flow_service
from app.services.workspaces import workspace_service
from app.utils.validators import parse_optional_uuid

_SETTINGS_TEMPLATE = "chatbot_settings/widget_settings.htm"


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
                # The create form's optional Workspace -> Data Agent picker. The agent
                # list starts unfiltered ("All workspaces") and is re-rendered by
                # /deep-agents/agent-options when a workspace is chosen — the same
                # fragment, so the two states cannot disagree.
                "workspaces": await workspace_service.get_workspace_choices(db, user.id),
                "agents": await data_agent_service.get_agent_views(db, user.id),
                "selected_agent_id": "",
                "field_name": "data_agent_id",
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
                # Optional. Blank means "no agent", which is the pre-existing
                # behaviour, so an untouched form creates the chatbot it always did.
                workspace_id=parse_optional_uuid(form.get("workspace_id"), "Workspace"),
                data_agent_id=parse_optional_uuid(
                    form.get("data_agent_id"), "Data agent",
                ),
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
    # CHATBOT CONFIGURATION PAGE — one page, three tabs: appearance
    # (branding/colors/copy/size), AI & prompt, and actions
    # --------------------------
    async def _settings_page_context(
        self,
        db: AsyncSession,
        user: User,
        key_id: uuid.UUID,
        api_base_url: str,
        active_tab: str = "appearance",
        **extra,
    ) -> dict:
        """
        Everything the tabbed settings page renders. Built in one place so all
        four handlers that return this template stay in sync.
        """
        key = await get_chatbot_key(db, user.id, key_id)
        settings = await get_widget_settings(db, user.id, key_id)
        ai_settings = await get_ai_settings(db, user.id, key_id)
        actions = await get_actions_for_chatbot(db, user.id, key_id)
        attachable_actions = await get_attachable_actions(db, user.id, key_id)
        attached_flow = await flow_service.get_attached_flow(db, user.id, key_id)
        attachable_flows = await flow_service.get_attachable_flows(db, user.id)
        ai_api_keys = await get_user_api_keys(db, user.id)

        # The template only ever exposes the AI key's public uuid, never the
        # bigint FK stored on the settings row.
        selected_llm_key_uuid = next(
            (str(k.uuid) for k in ai_api_keys if k.id == ai_settings.llm_api_key_id),
            "",
        )

        # The attached data agent, as public uuids for the picker. The agent list is
        # deliberately not filtered to the stored workspace: the agent may have been
        # moved out of it, or have none at all, and either way it must still appear as
        # the current selection rather than vanish from its own form.
        selected_agent_uuid = await data_agent_service.get_agent_public_id(
            db, user.id, key.data_agent_id,
        )
        selected_workspace_uuid = await workspace_service.get_workspace_public_id(
            db, user.id, key.workspace_id,
        )

        return {
            "user": user,
            "key": key,
            "settings": settings,
            "fonts": ALLOWED_HEADER_FONTS,
            "send_button_styles": ALLOWED_SEND_BUTTON_STYLES,
            "send_button_icon_url": resolve_send_button_icon_url(settings, ""),
            "api_base_url": api_base_url,
            "active": "chatbot_settings",
            "active_tab": active_tab,
            "ai_settings": ai_settings,
            "ai_api_keys": ai_api_keys,
            "selected_llm_key_uuid": selected_llm_key_uuid,
            "llm_modes": LLM_MODES,
            "default_prompt": DEFAULT_SYSTEM_PROMPT,
            "actions": build_action_views(actions),
            "attachable_actions": build_action_views(attachable_actions),
            "attached_flow": attached_flow,
            "attachable_flows": attachable_flows,
            # Deep Agent attachment picker (AI & Prompt tab). `field_name` and
            # `agents` feed the shared deep_agents/partials/agent_options.htm include.
            "workspaces": await workspace_service.get_workspace_choices(db, user.id),
            "agents": await data_agent_service.get_agent_views(db, user.id),
            "selected_agent_id": selected_agent_uuid,
            "selected_workspace_id": selected_workspace_uuid,
            "field_name": "data_agent_id",
            **action_form_context(),
            **extra,
        }

    @get("/{key_id:uuid}/widget-settings")
    async def widget_settings_page(
        self, key_id: uuid.UUID, request: Request, db: AsyncSession, user: User
    ) -> Template:
        tab = request.query_params.get("tab", "appearance")
        return Template(
            template_name=_SETTINGS_TEMPLATE,
            context=await self._settings_page_context(
                db, user, key_id, str(request.base_url).rstrip("/"), active_tab=tab,
            ),
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

        return Template(
            template_name=_SETTINGS_TEMPLATE,
            context=await self._settings_page_context(
                db, user, key_id, str(request.base_url).rstrip("/"),
                active_tab="appearance", error=error, success=error is None,
            ),
        )

    # --------------------------
    # AI & PROMPT — agent name, system prompt, prompt variables, LLM choice
    # --------------------------
    @post("/{key_id:uuid}/ai-settings")
    async def save_ai_settings(
        self,
        key_id: uuid.UUID,
        request: Request,
        db: AsyncSession,
        user: User,
    ) -> Template:
        form = await request.form()

        fields = AiSettingsInput(
            agent_name=form.get("agent_name", ""),
            system_prompt=form.get("system_prompt", ""),
            variables_json=form.get("variables_json", ""),
            llm_mode=form.get("llm_mode", ""),
            llm_api_key_id=form.get("llm_api_key_id", ""),
        )

        error = None
        try:
            await update_ai_settings(db, user.id, key_id, fields)
        except HTTPException as e:
            error = str(e.detail)

        return Template(
            template_name=_SETTINGS_TEMPLATE,
            context=await self._settings_page_context(
                db, user, key_id, str(request.base_url).rstrip("/"),
                active_tab="ai", error=error, success=error is None,
            ),
        )

    @post("/{key_id:uuid}/ai-settings/reset-prompt")
    async def reset_ai_prompt(
        self,
        key_id: uuid.UUID,
        request: Request,
        db: AsyncSession,
        user: User,
    ) -> Template:
        error = None
        try:
            await reset_system_prompt(db, user.id, key_id)
        except HTTPException as e:
            error = str(e.detail)

        return Template(
            template_name=_SETTINGS_TEMPLATE,
            context=await self._settings_page_context(
                db, user, key_id, str(request.base_url).rstrip("/"),
                active_tab="ai", error=error, success=error is None,
            ),
        )

    # --------------------------
    # CONVERSATION FLOW — which Flow Builder flow this agent runs
    # --------------------------
    @post("/{key_id:uuid}/flow")
    async def save_flow(
        self,
        key_id: uuid.UUID,
        request: Request,
        db: AsyncSession,
        user: User,
    ) -> Template:
        """
        Attach a flow to this agent, or clear it with an empty selection. Flows
        are built in the Flow Builder (sidebar) and owned by the user; this only
        points the agent at one.
        """
        form = await request.form()
        raw_flow_id = (form.get("flow_id") or "").strip()

        error = None
        try:
            flow_id = uuid.UUID(raw_flow_id) if raw_flow_id else None
        except ValueError:
            flow_id = None
            error = "That flow selection was not valid. Please pick a flow from the list."

        if error is None:
            try:
                await flow_service.attach_flow(db, user.id, key_id, flow_id)
            except HTTPException as e:
                error = str(e.detail)

        return Template(
            template_name=_SETTINGS_TEMPLATE,
            context=await self._settings_page_context(
                db, user, key_id, str(request.base_url).rstrip("/"),
                active_tab="ai", error=error, success=error is None,
            ),
        )

    # --------------------------
    # DATA AGENT — which data agent (if any) answers this chatbot's data questions
    # --------------------------
    @post("/{key_id:uuid}/data-agent")
    async def save_data_agent(
        self,
        key_id: uuid.UUID,
        request: Request,
        db: AsyncSession,
        user: User,
    ) -> Template:
        """
        Attach a data agent to this chatbot, or clear it with an empty selection.

        Unlike the datasource target, this is editable after creation: swapping which
        agent answers is a normal change, whereas repointing a published widget at
        different data is not.
        """
        form = await request.form()
        error = None

        try:
            await set_chatbot_data_agent(
                db,
                user.id,
                key_id,
                workspace_id=parse_optional_uuid(form.get("workspace_id"), "Workspace"),
                data_agent_id=parse_optional_uuid(
                    form.get("data_agent_id"), "Data agent",
                ),
            )
        except HTTPException as e:
            error = str(e.detail)

        return Template(
            template_name=_SETTINGS_TEMPLATE,
            context=await self._settings_page_context(
                db, user, key_id, str(request.base_url).rstrip("/"),
                active_tab="ai", error=error, success=error is None,
            ),
        )

    # --------------------------
    # ACTIONS — attach/detach library actions (HTMX partials). Creating and
    # editing an action itself lives in the Actions library
    # (see ChatbotActionController), since one action can serve many agents.
    # --------------------------
    async def _action_rows(
        self, db: AsyncSession, user: User, key_id: uuid.UUID, error: str | None
    ) -> Template:
        actions = await get_actions_for_chatbot(db, user.id, key_id)
        return Template(
            template_name="chatbot_settings/partials/action_rows_response.htm",
            context={
                "key": await get_chatbot_key(db, user.id, key_id),
                "actions": build_action_views(actions),
                # The picker is refreshed out of band alongside the table, so the
                # options it offers stay in step with what is already attached.
                "attachable_actions": await get_attachable_actions(db, user.id, key_id),
                "error": error,
            },
        )

    @post("/{key_id:uuid}/actions/attach")
    async def attach_chatbot_action(
        self, key_id: uuid.UUID, request: Request, db: AsyncSession, user: User
    ) -> Template:
        form = await request.form()
        raw_action_id = (form.get("action_id") or "").strip()

        error = None
        try:
            await attach_action(db, user.id, key_id, uuid.UUID(raw_action_id))
        except ValueError:
            error = "Please select an action to add."
        except HTTPException as e:
            error = str(e.detail)

        return await self._action_rows(db, user, key_id, error)

    @post("/{key_id:uuid}/actions/create-and-attach")
    async def create_and_attach_chatbot_action(
        self, key_id: uuid.UUID, request: Request, db: AsyncSession, user: User
    ) -> Template:
        """Quick-create: save to the user's library and add it to this agent at once."""
        error = None
        try:
            await create_and_attach_action(db, user.id, key_id, read_action_form(await request.form()))
        except HTTPException as e:
            error = str(e.detail)

        return await self._action_rows(db, user, key_id, error)

    @post("/{key_id:uuid}/actions/{action_id:uuid}/detach")
    async def detach_chatbot_action(
        self, key_id: uuid.UUID, action_id: uuid.UUID, db: AsyncSession, user: User
    ) -> Template:
        error = None
        try:
            await detach_action(db, user.id, key_id, action_id)
        except HTTPException as e:
            error = str(e.detail)

        return await self._action_rows(db, user, key_id, error)

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
