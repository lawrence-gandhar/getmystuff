from litestar import Controller, get
from litestar.connection import Request
from litestar.response import Template
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.auth import require_auth
from app.models.user import User
from app.services.datasource_service import get_user_datasources


class QueryRunnerController(Controller):
    path = "/query-runner"
    dependencies = {"user": require_auth}

    @get("/")
    async def index(
        self,
        db: AsyncSession,
        request: Request,
        user: User,
    ) -> Template:
        """Standalone page: pick a datasource + file/table/collection, submit a
        natural-language prompt, and get AI analytics computed against it.

        Reuses the same AIAnalyticsController endpoints (generate / history)
        that already back the "Ask AI" modal in Configurations — this page is
        just a different entry point onto the same service logic.
        """

        datasources = await get_user_datasources(db=db, user_id=user.id)

        return Template(
            template_name="query_runner/index.htm",
            context={
                "request": request,
                "datasources": datasources,
                "user": user,
                "active": "queries",
            },
        )
