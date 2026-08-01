import uuid

from litestar import Controller, get, post
from litestar.connection import Request
from litestar.exceptions import HTTPException
from litestar.response import Response, Template
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.auth import require_auth
from app.models.user import User
from app.schemas.ai_analytics import (
    AiAnalyticsGenerateRequest,
    AiAnalyticsHistoryQuery,
)
from app.services.ai_analytics.ai_analytics_service import generate_analytics, get_prompt_history


class AIAnalyticsController(Controller):
    path = "/datasource"
    dependencies = {"user": require_auth}

    # --------------------------
    # GENERATE AI ANALYTICS
    # --------------------------
    @post("/{datasource_id:uuid}/ai-analytics")
    async def generate(
        self,
        datasource_id: uuid.UUID,
        request: Request,
        db: AsyncSession,
        user: User,
    ) -> Template | Response:

        try:
            payload = await AiAnalyticsGenerateRequest.from_form(request)
            history = await generate_analytics(
                db=db,
                user_id=user.id,
                datasource_id=datasource_id,
                target_type=payload.target_type,
                target_name=payload.target_name,
                prompt=payload.prompt,
                file_id=payload.file_id,
            )
            return Template(
                template_name="datasources/ai_analytics_result.htm",
                context={"entry": history},
            )
        except HTTPException as e:
            # A rejected payload and a failed run render into the same inline
            # alert, always with 200, because the panel stays open either way.
            return Response(
                f"<div class='alert alert-danger' data-success='false'>{e.detail}</div>",
                media_type="text/html",
                status_code=200,
            )

    # --------------------------
    # AI ANALYTICS HISTORY
    # --------------------------
    @get("/{datasource_id:uuid}/ai-analytics/history")
    async def history(
        self,
        datasource_id: uuid.UUID,
        request: Request,
        db: AsyncSession,
        user: User,
    ) -> Template:

        query = AiAnalyticsHistoryQuery.from_query(request)

        entries = await get_prompt_history(
            db=db,
            user_id=user.id,
            datasource_id=datasource_id,
            target_type=query.target_type,
            target_name=query.target_name,
        )

        return Template(
            template_name="datasources/ai_analytics_history.htm",
            context={"entries": entries},
        )
