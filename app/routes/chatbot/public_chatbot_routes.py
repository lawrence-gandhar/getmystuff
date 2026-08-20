import json
from typing import AsyncIterator

from litestar import Controller, get, post
from litestar.config.cors import CORSConfig
from litestar.connection import Request
from litestar.exceptions import HTTPException
from litestar.middleware.base import DefineMiddleware
from litestar.middleware.cors import CORSMiddleware
from litestar.response import Response, ServerSentEvent
from litestar.response.sse import ServerSentEventMessage
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.chatbot import (
    ChatbotTurnResponse,
    PublicChatbotMessageRequest,
    PublicChatbotStreamQuery,
    PublicWidgetConfigQuery,
    WidgetConfigResponse,
)
from app.schemas.common import StatusResponse
from app.services.chatbot.chatbot_service import get_active_key_by_value, validate_origin
from app.services.chatbot.chatbot_turn_service import (
    TurnResult,
    answer_turn,
    stream_turn,
)
from app.services.chatbot.chatbot_widget_settings_service import (
    build_widget_public_config,
    get_widget_settings_by_key_id,
)

_JSON = "application/json"

_INVALID_KEY = "Invalid or inactive chatbot key"
_FORBIDDEN_ORIGIN = "This domain is not authorized to use this chatbot key"


def _error(message: str, status_code: int) -> Response:
    """
    A rejection, in the application-wide ``{"status", "message"}`` envelope.

    Every failure this controller can produce goes through here, so an anonymous
    caller sees one shape whether the key was wrong, the domain was not allowed, or
    the body could not be read.
    """
    return Response(
        StatusResponse.error(message).payload(),
        media_type=_JSON,
        status_code=status_code,
    )

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

        try:
            api_key = PublicWidgetConfigQuery.from_query(request).api_key
        except HTTPException as exc:
            return _error(str(exc.detail), 400)

        chatbot_key = await get_active_key_by_value(db, api_key)
        if not chatbot_key:
            return _error(_INVALID_KEY, 404)

        if not validate_origin(chatbot_key, origin):
            return _error(_FORBIDDEN_ORIGIN, 403)

        settings = await get_widget_settings_by_key_id(db, chatbot_key.id)
        api_base_url = str(request.base_url).rstrip("/")
        config = build_widget_public_config(settings, chatbot_key.name, api_base_url)

        return Response(
            WidgetConfigResponse.from_config(config).payload(),
            media_type=_JSON,
            status_code=200,
        )

    @post("/message")
    async def message(self, request: Request, db: AsyncSession) -> Response:
        origin = request.headers.get("origin")

        try:
            payload = await PublicChatbotMessageRequest.from_json(request)
        except HTTPException as exc:
            # The only untrusted body in the application. A 400 here covers both a
            # body that is not a JSON object and one whose fields are the wrong
            # type or over their bounds — the schema decides, so no field is read
            # before it has been checked.
            return _error(str(exc.detail), exc.status_code)

        chatbot_key = await get_active_key_by_value(db, payload.api_key)
        if not chatbot_key:
            return _error(_INVALID_KEY, 404)

        if not validate_origin(chatbot_key, origin):
            return _error(_FORBIDDEN_ORIGIN, 403)

        result = await answer_turn(
            db,
            chatbot_key,
            payload.message,
            payload.session_id,
            payload.selected_value,
        )
        return _turn_response(result)

    @get("/message-stream")
    async def message_stream(
        self, request: Request, db: AsyncSession,
    ) -> ServerSentEvent | Response:
        """
        The same turn as :meth:`message`, streamed as the agent writes it.

        A GET, because ``EventSource`` only issues GETs — so the message, the key and the
        session id arrive as query parameters and go through their own schema.

        The key and origin checks happen *before* the stream opens, and are ordinary
        rejections with real status codes: a stream's status is committed the moment it
        begins, so anything that should be a 403 has to be refused here. Failures after
        that point arrive as an ``error`` event instead.

        A turn that cannot be streamed — an active flow, or a chatbot with no data agent —
        yields one ``fallback`` event and the widget posts to :meth:`message` instead. See
        ``chatbot_turn_service.stream_turn``.
        """
        origin = request.headers.get("origin")

        try:
            payload = PublicChatbotStreamQuery.from_query(request)
        except HTTPException as exc:
            return _error(str(exc.detail), exc.status_code)

        chatbot_key = await get_active_key_by_value(db, payload.api_key)
        if not chatbot_key:
            return _error(_INVALID_KEY, 404)

        if not validate_origin(chatbot_key, origin):
            return _error(_FORBIDDEN_ORIGIN, 403)

        async def messages() -> AsyncIterator[ServerSentEventMessage]:
            events = stream_turn(
                db, chatbot_key, payload.message, payload.session_id,
            )

            async for event in events:
                yield ServerSentEventMessage(
                    data=json.dumps(event, default=str),
                    event=str(event.get("event") or "token"),
                )

        return ServerSentEvent(messages())


def _turn_response(result: TurnResult) -> Response:
    """
    Serialize one answered turn for the widget.

    Always HTTP 200, including for an answering failure: the widget renders the
    payload either way, and a non-2xx here would be indistinguishable from the
    key/origin rejections above.

    ``response_time_ms`` is the server-side time the turn took — the same
    number stored on the log row — so what a visitor sees in the widget and
    what the owner sees in Chatbot Analytics can never disagree.

    ``ChatbotTurnResponse`` owns the two payload shapes (the answer, and the error
    that carries only a message and the timing), including the ``text`` duplicate
    of ``summary`` that the flow node types read their prompt from.
    """
    return Response(
        ChatbotTurnResponse.from_turn(result).payload(),
        media_type=_JSON,
        status_code=200,
    )
