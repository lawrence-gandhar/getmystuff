import uuid

from litestar import Controller, get, post
from litestar.connection import Request
from litestar.exceptions import HTTPException
from litestar.response import Response, Template
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.auth import require_auth
from app.models.ai_settings import AI_PROVIDERS
from app.models.user import User
from app.services.ai_settings.ai_settings_service import (
    create_api_key,
    delete_api_key,
    get_user_api_keys,
    toggle_active_status,
    update_api_key,
)


class AISettingsController(Controller):
    path = "/ai-settings"
    dependencies = {"user": require_auth}

    # --------------------------
    # LIST / PAGE
    # --------------------------
    @get("/")
    async def index(self, db: AsyncSession, user: User) -> Template:
        keys = await get_user_api_keys(db, user.id)
        return Template(
            template_name="ai_settings/index.htm",
            context={
                "user": user,
                "keys": keys,
                "providers": AI_PROVIDERS,
                "active": "ai_settings",
            },
        )

    # --------------------------
    # CREATE
    # --------------------------
    @post("/create")
    async def create(self, request: Request, db: AsyncSession, user: User) -> Template | Response:
        form = await request.form()

        try:
            await create_api_key(
                db=db,
                user_id=user.id,
                provider=form.get("provider", ""),
                label=form.get("label", ""),
                api_key=form.get("api_key", ""),
                is_active=form.get("is_active") == "on",
                base_url=form.get("base_url"),
                model_name=form.get("model_name"),
            )
        except HTTPException as e:
            return Response(
                f"<div class='alert alert-danger' data-success='false'>{e.detail}</div>",
                media_type="text/html",
                status_code=200,
            )

        keys = await get_user_api_keys(db, user.id)
        return Template(
            template_name="ai_settings/key_save_response.htm",
            context={"keys": keys},
        )

    # --------------------------
    # UPDATE (label / key value)
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
            await update_api_key(
                db=db,
                user_id=user.id,
                key_id=key_id,
                label=form.get("label"),
                api_key=form.get("api_key") or None,
                base_url=form.get("base_url"),
                model_name=form.get("model_name"),
            )
        except HTTPException as e:
            return Response(
                f"<div class='alert alert-danger' data-success='false'>{e.detail}</div>",
                media_type="text/html",
                status_code=200,
            )

        keys = await get_user_api_keys(db, user.id)
        return Template(
            template_name="ai_settings/key_save_response.htm",
            context={"keys": keys},
        )

    # --------------------------
    # TOGGLE ACTIVE / INACTIVE
    # --------------------------
    @post("/{key_id:uuid}/toggle-active")
    async def toggle_active(
        self,
        key_id: uuid.UUID,
        db: AsyncSession,
        user: User,
    ) -> Template:
        error = None
        try:
            await toggle_active_status(db, user.id, key_id)
        except HTTPException as e:
            error = str(e.detail)

        keys = await get_user_api_keys(db, user.id)
        return Template(
            template_name="ai_settings/key_rows_response.htm",
            context={"keys": keys, "error": error},
        )

    # --------------------------
    # DELETE
    # --------------------------
    @post("/{key_id:uuid}/delete")
    async def delete(
        self,
        key_id: uuid.UUID,
        db: AsyncSession,
        user: User,
    ) -> Template:
        error = None
        try:
            await delete_api_key(db, user.id, key_id)
        except HTTPException as e:
            error = str(e.detail)

        keys = await get_user_api_keys(db, user.id)
        return Template(
            template_name="ai_settings/key_rows_response.htm",
            context={"keys": keys, "error": error},
        )
