"""
Routes for the Chatbot Analytics module — the performance dashboard for every
agent the signed-in user owns.

Two handlers only: the full page, and the same dashboard body on its own for
HTMX to swap in when a filter changes. Both build their context through the
one service call, so the filtered view and the first paint can never drift
apart.
"""

import uuid
from typing import Optional

from litestar import Controller, get
from litestar.connection import Request
from litestar.exceptions import HTTPException
from litestar.response import Template
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.auth import require_auth
from app.models.user import User
from app.services.chatbot.chatbot_service import get_user_chatbot_keys
from app.services.chatbot_analytics.chatbot_analytics_service import DEFAULT_PERIOD, build_dashboard

_PAGE_TEMPLATE = "chatbot_analytics/index.htm"
_BODY_TEMPLATE = "chatbot_analytics/partials/dashboard.htm"


def _parse_chatbot_filter(raw: str) -> Optional[uuid.UUID]:
    """
    Read the agent filter. An empty value means "all agents"; anything that
    isn't a valid identifier is refused rather than quietly ignored, so a
    broken link never shows figures for the wrong scope.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="That chatbot selection was not valid. Please pick one from the list.",
        )


class ChatbotAnalyticsController(Controller):
    path = "/chatbot-analytics"
    dependencies = {"user": require_auth}

    async def _context(self, request: Request, db: AsyncSession, user: User) -> dict:
        period = request.query_params.get("period", DEFAULT_PERIOD)
        chatbot_uuid = _parse_chatbot_filter(request.query_params.get("chatbot_id", ""))
        return await build_dashboard(db, user.id, period, chatbot_uuid)

    @get("/")
    async def index(self, request: Request, db: AsyncSession, user: User) -> Template:
        """The full page: filter bar plus the dashboard body."""
        error = None
        try:
            dashboard = await self._context(request, db, user)
        except HTTPException as e:
            # A bad filter must still render the page — with its defaults and a
            # readable explanation — rather than dropping the user on an error.
            error = str(e.detail)
            dashboard = await build_dashboard(db, user.id, DEFAULT_PERIOD, None)

        return Template(
            template_name=_PAGE_TEMPLATE,
            context={
                "user": user,
                "active": "chatbot_analytics",
                "chatbots": await get_user_chatbot_keys(db, user.id),
                "error": error,
                **dashboard,
            },
        )

    @get("/data")
    async def data(self, request: Request, db: AsyncSession, user: User) -> Template:
        """The dashboard body alone — the HTMX target when a filter changes."""
        error = None
        try:
            dashboard = await self._context(request, db, user)
        except HTTPException as e:
            error = str(e.detail)
            dashboard = await build_dashboard(db, user.id, DEFAULT_PERIOD, None)

        return Template(
            template_name=_BODY_TEMPLATE,
            context={"error": error, **dashboard},
        )
