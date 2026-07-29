from litestar import Controller, get, post
from litestar.config.cors import CORSConfig
from litestar.connection import Request
from litestar.exceptions import HTTPException
from litestar.middleware.base import DefineMiddleware
from litestar.middleware.cors import CORSMiddleware
from litestar.response import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.chatbot_service import answer_message, get_active_key_by_value, validate_origin
from app.services.chatbot_widget_settings_service import (
    build_widget_public_config,
    get_widget_settings_by_key_id,
)

_JSON = "application/json"

# Scoped to just this controller (via the `middleware` class attribute below),
# not the app-level `cors_config` — the rest of the app relies on
# cookie-based sessions and must not get a permissive cross-origin policy.
# allow_origins is wide open here because CORS itself can't be conditioned on
# the request body (the api_key); the actual per-key domain allow-list is
# enforced inside the POST handler via chatbot_service.validate_origin.
_public_cors = DefineMiddleware(
    CORSMiddleware,
    config=CORSConfig(
        allow_origins=["*"],
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type"],
        allow_credentials=False,
    ),
)


class PublicChatbotController(Controller):
    """
    Public, unauthenticated endpoint that powers embeddable chatbot widgets.
    No session/require_auth dependency — callers are anonymous website
    visitors, authenticated only by the widget's publishable api_key, scoped
    to one datasource target, and origin-restricted (see chatbot_service).
    """
    path = "/public/chatbot"
    middleware = [_public_cors]

    @get("/widget-config")
    async def widget_config(self, request: Request, db: AsyncSession) -> Response:
        """
        Appearance/behavior settings for one widget, fetched by the widget
        script itself at runtime (see chatbot_service._WIDGET_SCRIPT_TEMPLATE)
        so dashboard changes apply on next page load — no re-download needed.
        Same api_key + origin-allowlist check as /message; nothing here is
        trusted from the client.
        """
        origin = request.headers.get("origin")
        api_key = request.query_params.get("api_key", "")

        chatbot_key = await get_active_key_by_value(db, api_key) if api_key else None
        if not chatbot_key:
            return Response(
                {"status": "error", "message": "Invalid or inactive chatbot key"},
                media_type=_JSON,
                status_code=404,
            )

        if not validate_origin(chatbot_key, origin):
            return Response(
                {"status": "error", "message": "This domain is not authorized to use this chatbot key"},
                media_type=_JSON,
                status_code=403,
            )

        settings = await get_widget_settings_by_key_id(db, chatbot_key.id)
        api_base_url = str(request.base_url).rstrip("/")
        config = build_widget_public_config(settings, chatbot_key.name, api_base_url)

        return Response(
            {"status": "success", **config},
            media_type=_JSON,
            status_code=200,
        )

    @post("/message")
    async def message(self, request: Request, db: AsyncSession) -> Response:
        origin = request.headers.get("origin")

        try:
            body = await request.json()
        except Exception:
            return Response(
                {"status": "error", "message": "Invalid request body"},
                media_type=_JSON,
                status_code=400,
            )

        api_key = (body or {}).get("api_key", "")
        text = (body or {}).get("message", "")

        chatbot_key = await get_active_key_by_value(db, api_key) if api_key else None
        if not chatbot_key:
            return Response(
                {"status": "error", "message": "Invalid or inactive chatbot key"},
                media_type=_JSON,
                status_code=404,
            )

        if not validate_origin(chatbot_key, origin):
            return Response(
                {"status": "error", "message": "This domain is not authorized to use this chatbot key"},
                media_type=_JSON,
                status_code=403,
            )

        try:
            result = await answer_message(db, chatbot_key, text)
        except HTTPException as e:
            return Response(
                {"status": "error", "message": str(e.detail)},
                media_type=_JSON,
                status_code=200,
            )

        return Response(
            {
                "status": "success",
                "summary": result.summary,
                "insights": result.insights or [],
                "table": result.table.model_dump() if result.table else None,
            },
            media_type=_JSON,
            status_code=200,
        )
