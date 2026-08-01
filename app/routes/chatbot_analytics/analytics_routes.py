"""
Routes for the Chatbot Analytics module — the performance dashboard for every
agent the signed-in user owns.

Two handlers only: the full page, and the same dashboard body on its own for
HTMX to swap in when a filter changes. Both build their context through the
one service call, so the filtered view and the first paint can never drift
apart.
"""

from litestar import Controller, get
from litestar.connection import Request
from litestar.exceptions import HTTPException
from litestar.response import Template
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.auth import require_auth
from app.models.user import User
from app.schemas.chatbot_analytics import AnalyticsDashboardQuery
from app.services.chatbot.chatbot_service import get_user_chatbot_keys
from app.services.chatbot_analytics.chatbot_analytics_service import DEFAULT_PERIOD, build_dashboard

_PAGE_TEMPLATE = "chatbot_analytics/index.htm"
_BODY_TEMPLATE = "chatbot_analytics/partials/dashboard.htm"


class ChatbotAnalyticsController(Controller):
    path = "/chatbot-analytics"
    dependencies = {"user": require_auth}

    async def _context(self, request: Request, db: AsyncSession, user: User) -> dict:
        """
        The dashboard for whichever scope the URL asks for.

        Both filters are validated by :class:`AnalyticsDashboardQuery`, which
        refuses an unreadable value rather than falling back — a dashboard showing
        real figures for the wrong scope is worse than an error, because nothing on
        screen says it is wrong.
        """
        query = AnalyticsDashboardQuery.from_query(request)
        return await build_dashboard(db, user.id, query.period, query.chatbot_id)

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
