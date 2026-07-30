"""
The Actions library — user-owned webhook actions, created once here and attached
to any number of chatbots from each chatbot's Actions tab.

Lives under routes/chatbot/ rather than its own feature folder because an action
is still a chatbot action: its model sits in app.models.chatbot and its runtime
is part of the chatbot answer path (chatbot_action_service.maybe_run_action).
"""

import uuid

from litestar import Controller, get, post
from litestar.connection import Request
from litestar.exceptions import HTTPException
from litestar.response import Template
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.auth import require_auth
from app.db.chatbot.queries import count_action_attachments
from app.models.chatbot import ACTION_HTTP_METHODS, ACTION_PARAMETER_TYPES
from app.models.user import User
from app.services.chatbot.chatbot_action_service import (
    ActionInput,
    build_action_views,
    create_action,
    delete_action,
    get_user_actions,
    toggle_action_active,
    update_action,
)

_ROWS_TEMPLATE = "actions/partials/action_rows_response.htm"


def action_form_context() -> dict:
    """
    Choices the shared action form needs. Reused by the chatbot settings page,
    which renders the same form partial for its quick-create flow.
    """
    return {
        "action_methods": ACTION_HTTP_METHODS,
        "action_parameter_types": ACTION_PARAMETER_TYPES,
    }


class ChatbotActionController(Controller):
    path = "/actions"
    dependencies = {"user": require_auth}

    @get("/")
    async def index(self, db: AsyncSession, user: User) -> Template:
        return Template(
            template_name="actions/index.htm",
            context={
                "user": user,
                "active": "actions",
                "actions": await self._views(db, user),
                "form_action": "/actions/create",
                **action_form_context(),
            },
        )

    @post("/create")
    async def create(self, request: Request, db: AsyncSession, user: User) -> Template:
        error = None
        try:
            await create_action(db, user.id, read_action_form(await request.form()))
        except HTTPException as e:
            error = str(e.detail)

        return await self._rows(db, user, error)

    @post("/{action_id:uuid}/update")
    async def update(
        self, action_id: uuid.UUID, request: Request, db: AsyncSession, user: User
    ) -> Template:
        error = None
        try:
            await update_action(db, user.id, action_id, read_action_form(await request.form()))
        except HTTPException as e:
            error = str(e.detail)

        return await self._rows(db, user, error)

    @post("/{action_id:uuid}/toggle-active")
    async def toggle_active(
        self, action_id: uuid.UUID, db: AsyncSession, user: User
    ) -> Template:
        error = None
        try:
            await toggle_action_active(db, user.id, action_id)
        except HTTPException as e:
            error = str(e.detail)

        return await self._rows(db, user, error)

    @post("/{action_id:uuid}/delete")
    async def delete(self, action_id: uuid.UUID, db: AsyncSession, user: User) -> Template:
        error = None
        try:
            await delete_action(db, user.id, action_id)
        except HTTPException as e:
            error = str(e.detail)

        return await self._rows(db, user, error)

    @staticmethod
    async def _views(db: AsyncSession, user: User) -> list:
        actions = await get_user_actions(db, user.id)
        counts = await count_action_attachments(db, user.id)
        return build_action_views(actions, counts)

    async def _rows(self, db: AsyncSession, user: User, error: str | None) -> Template:
        """The HTMX response every mutation returns: error banner + rebuilt table body."""
        return Template(
            template_name=_ROWS_TEMPLATE,
            context={"actions": await self._views(db, user), "error": error},
        )


def read_action_form(form) -> ActionInput:
    """
    Read the shared action form. Also used by the chatbot settings controller's
    quick-create endpoint, which posts the very same fields.
    """
    return ActionInput(
        name=form.get("name", ""),
        description=form.get("description", ""),
        http_method=form.get("http_method", ""),
        url=form.get("url", ""),
        headers_json=form.get("headers_json", ""),
        body_template=form.get("body_template", ""),
        parameters_json=form.get("parameters_json", ""),
        timeout_seconds=form.get("timeout_seconds", "10"),
    )
