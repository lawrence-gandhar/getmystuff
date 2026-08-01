from pathlib import Path
import uvicorn

from litestar import Litestar, get
from litestar.contrib.jinja import JinjaTemplateEngine
from litestar.template.config import TemplateConfig
from litestar.plugins.htmx import HTMXPlugin, HTMXRequest
from litestar.middleware.session.server_side import ServerSideSessionConfig
from litestar.plugins.flash import FlashConfig, FlashPlugin
from litestar.response import Template, Redirect, Response
from litestar.static_files.config import StaticFilesConfig
from litestar.exceptions import HTTPException
from litestar.exceptions.responses import (
    create_debug_response,
    create_exception_response,
)
from litestar.status_codes import HTTP_500_INTERNAL_SERVER_ERROR
from litestar.connection import Request

from app.routes.auth import AuthController
from app.routes.dashboard import DashboardController
from app.routes.datasource import DataSourceController, DataSourceConfigurations
from app.routes.ai_analytics import AIAnalyticsController, QueryRunnerController
from app.routes.ai_settings import AISettingsController
from app.routes.chatbot import (
    ChatbotActionController,
    ChatbotSettingsController,
    PublicChatbotController,
)
from app.routes.chatbot_analytics import ChatbotAnalyticsController
from app.routes.flow_builder import FlowBuilderController, KnowledgeBaseController
from app.routes.workspaces import WorkspaceController
from app.routes.data_agents import DataAgentController
from app.routes.tool_configs import ToolConfigController
from app.routes.sql_assist import SqlAssistController
from app.routes.deep_agents import DeepAgentController

from app.services.ai_inbuilt import ollama_client

from app.db.db_sessions import get_db
from app.db.base import Base
from app.db.db_sessions import engine
from app.db.auth import create_fake_user


# -----------------------------
# Template configuration
# -----------------------------
template_config = TemplateConfig(
    engine=JinjaTemplateEngine,
    directory=Path("templates"),
)

# -----------------------------
# Session middleware
# -----------------------------
session_config = ServerSideSessionConfig()

# -----------------------------
# Flash plugin
# -----------------------------
flash_plugin = FlashPlugin(
    config=FlashConfig(template_config=template_config)
)


# -----------------------------
# Routes
# -----------------------------
@get("/")
async def root() -> Template:
    return Template(
        template_name="base/index.htm",
        context={"app_name": "GetMyStuff"},
    )


# -----------------------------
# App Initialization Hook
# -----------------------------
async def on_startup() -> None:
    """
    DEV ONLY:
    Creates tables automatically and seeds the test admin account.
    Remove in production and use Alembic.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Seed the test admin so a fresh database (a new `pgdata` volume, say) is
    # immediately loggable-into instead of bouncing every login back to the form
    # with "Invalid credentials". Idempotent — a second boot is a no-op.
    await create_fake_user()

    # Load the local Ollama models now so the first AI request doesn't wait on
    # a cold model load. Best effort — never blocks startup (see docstring).
    await ollama_client.preload_models()


async def on_shutdown() -> None:
    """Release the pooled HTTP connection to the local Ollama server."""
    await ollama_client.close_client()


# -----------------------------
# Exception Handler for Token Expiry
# -----------------------------
def http_exception_handler(request: Request, exc: HTTPException) -> Response | Redirect:
    """
    Handle 401 errors by redirecting to login page.
    For HTMX requests, return 200 with HX-Redirect so the browser does a full
    page navigation instead of swapping the login page into the current target.

    Every other status is handed back to Litestar's own renderer. It must be
    RETURNED, not re-raised: a handler that raises escapes the exception
    middleware entirely, so uvicorn reports an uncaught ASGI exception and the
    client gets a 500 — a plain 404 was being served that way.
    """
    if exc.status_code == 401:
        if request.headers.get("HX-Request"):
            return Response(
                content="",
                status_code=200,
                headers={"HX-Redirect": "/auth/login"},
            )
        return Redirect(path="/auth/login")

    # Same rule as Litestar's own default_http_exception_handler: a traceback is
    # only ever rendered for a genuine 500 in debug mode. A 404 or a 403 renders
    # as its real status with the exception's human-readable detail.
    if exc.status_code == HTTP_500_INTERNAL_SERVER_ERROR and request.app.debug:
        return create_debug_response(request, exc)

    return create_exception_response(request, exc)


# -----------------------------
# Create App
# -----------------------------
app = Litestar(
    route_handlers=[
        root,
        AuthController,
        DashboardController,
        DataSourceController,
        DataSourceConfigurations,
        AIAnalyticsController,
        AISettingsController,
        QueryRunnerController,
        ChatbotSettingsController,
        ChatbotActionController,
        ChatbotAnalyticsController,
        FlowBuilderController,
        KnowledgeBaseController,
        WorkspaceController,
        DataAgentController,
        ToolConfigController,
        SqlAssistController,
        DeepAgentController,
        PublicChatbotController,
    ],
    debug=True,
    request_class=HTMXRequest,
    template_config=template_config,
    plugins=[
        HTMXPlugin(),
        flash_plugin,
    ],
    middleware=[
        session_config.middleware,
    ],
    static_files_config=[
        StaticFilesConfig(
            directories=["static"],
            path="/static",
        )
    ],
    dependencies={
        "db": get_db,
    },
    exception_handlers={
        HTTPException: http_exception_handler,
    },
    on_startup=[on_startup],
    on_shutdown=[on_shutdown],
)


# -----------------------------
# Main Entry Point
# -----------------------------
if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8003,
        reload=True,
        log_level="info",
    )