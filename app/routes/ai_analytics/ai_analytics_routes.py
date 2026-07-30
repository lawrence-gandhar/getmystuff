import uuid

from litestar import Controller, get, post
from litestar.connection import Request
from litestar.exceptions import HTTPException
from litestar.response import Response, Template
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.auth import require_auth
from app.models.user import User
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

        form = await request.form()

        target_type = form.get("target_type", "")
        target_name = form.get("target_name", "")
        prompt = form.get("prompt", "")
        file_id_raw = form.get("file_id", "")

        file_id = None
        if file_id_raw:
            try:
                file_id = uuid.UUID(file_id_raw)
            except ValueError:
                return Response(
                    "<div class='alert alert-danger' data-success='false'>Invalid file reference.</div>",
                    media_type="text/html",
                    status_code=200,
                )

        try:
            history = await generate_analytics(
                db=db,
                user_id=user.id,
                datasource_id=datasource_id,
                target_type=target_type,
                target_name=target_name,
                prompt=prompt,
                file_id=file_id,
            )
            return Template(
                template_name="datasources/ai_analytics_result.htm",
                context={"entry": history},
            )
        except HTTPException as e:
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

        target_type = request.query_params.get("target_type", "")
        target_name = request.query_params.get("target_name", "")

        entries = await get_prompt_history(
            db=db,
            user_id=user.id,
            datasource_id=datasource_id,
            target_type=target_type,
            target_name=target_name,
        )

        return Template(
            template_name="datasources/ai_analytics_history.htm",
            context={"entries": entries},
        )
