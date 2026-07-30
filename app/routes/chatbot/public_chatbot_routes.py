from litestar import Controller, get, post
from litestar.config.cors import CORSConfig
from litestar.connection import Request
from litestar.middleware.base import DefineMiddleware
from litestar.middleware.cors import CORSMiddleware
from litestar.response import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.chatbot.chatbot_service import get_active_key_by_value, validate_origin
from app.services.chatbot.chatbot_turn_service import TurnResult, answer_turn
from app.services.chatbot.chatbot_widget_settings_service import (
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
        session_token = (body or {}).get("session_id", "")
        selected_value = (body or {}).get("selected_value")

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

        result = await answer_turn(db, chatbot_key, text, session_token, selected_value)
        return _turn_response(result)


def _turn_response(result: TurnResult) -> Response:
    """
    Serialize one answered turn for the widget.

    Always HTTP 200, including for an answering failure: the widget renders the
    payload either way, and a non-2xx here would be indistinguishable from the
    key/origin rejections above.

    ``response_time_ms`` is the server-side time the turn took — the same
    number stored on the log row — so what a visitor sees in the widget and
    what the owner sees in Chatbot Analytics can never disagree.
    """
    if result.status == "error":
        return Response(
            {
                "status": "error",
                "message": result.message,
                "response_time_ms": result.response_time_ms,
            },
            media_type=_JSON,
            status_code=200,
        )

    return Response(
        {
            "status": "success",
            "type": result.type,
            "summary": result.summary,
            "insights": result.insights,
            "table": result.table,
            # Duplicated as `text` for the flow node types (menu/dropdown/
            # ask-input) whose prompt the widget reads from this field.
            "text": result.summary,
            "options": result.options,
            "response_time_ms": result.response_time_ms,
        },
        media_type=_JSON,
        status_code=200,
    )
