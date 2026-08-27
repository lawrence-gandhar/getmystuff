import asyncio
from pathlib import Path
from typing import List
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

from app.routes.agent_recursive_dataframes import AggregationController
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
from app.routes.tool_graphs import ToolGraphController
from app.routes.graph_designer import GraphDesignerController
from app.routes.integrations import (
    IntegrationAIController,
    IntegrationAppController,
    IntegrationConnectionController,
    IntegrationsController,
)
from app.routes.email_dispatch import (
    EmailMessageController,
    EmailSmtpController,
    EmailTemplateController,
    EmailTriggerController,
    EmailWebhookController,
)
from app.routes.sql_assist import SqlAssistController
from app.routes.query_test import QueryTestController
from app.routes.deep_agents import DeepAgentController
from app.routes.downloader_agents import (
    DownloadController,
    FileDownloadController,
    PublicDownloadController,
)
from app.routes.file_delivery import (
    GeneratedFileController,
    PublicGeneratedFileController,
)

from app.services.agent_recursive_dataframes import frame_buffer
from app.services.ai_inbuilt import ollama_client
from app.services.downloader_agents.base import download_service, job_queue
from app.services.file_delivery import file_service as generated_file_service
from app.services.integrations.engine import (
    queue as integration_queue,
    run_service as integration_run_service,
    scheduler as integration_scheduler,
)
from app.services.email_dispatch import queue as email_queue
# Imported for the side effect of subscribing its handler to every event on the bus. Same
# import-for-registration pattern as `connectors/registry` and `app/db/models` — without
# this line every email trigger is stored correctly and never fires.
from app.services.email_dispatch import triggers as email_triggers  # noqa: F401
from app.services.integrations.runtime import http_client as integration_http
from app.services.downloader_agents.base.checkpointer import close_checkpointer
from app.services.downloader_agents.base.record_reader import release_all_readers
from app.services.graph_designer import graph_run_service

from app.db.db_sessions import get_db
from app.db.migrations import upgrade_to_head
from app.db.auth import create_fake_user


# Long-running asyncio tasks this process owns, so on_shutdown can cancel them. The
# export queue worker keeps its own handle (job_queue.stop_worker); this is for the
# reapers, which have nothing else to hold them.
_background_tasks: List[asyncio.Task] = []


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
    Bring the schema up to date, seed the dev admin account, start the background work.

    Migrating here rather than in a separate deploy step means the schema can never be
    older than the code running against it: the app that needs a column is the same one
    that applies it. A failure raises, so a database that cannot be migrated stops
    startup instead of leaving the app serving queries against a stale schema — the
    failure mode `create_all` used to produce, since it adds missing tables but never a
    missing column. See app/db/migrations.py.
    """
    await upgrade_to_head()

    # Seed the test admin so a fresh database (a new `pgdata` volume, say) is
    # immediately loggable-into instead of bouncing every login back to the form
    # with "Invalid credentials". Idempotent — a second boot is a no-op.
    await create_fake_user()

    # Load the local Ollama models now so the first AI request doesn't wait on
    # a cold model load. Best effort — never blocks startup (see docstring).
    await ollama_client.preload_models()

    # Drain the export queue and expire finished downloads. Both are asyncio tasks in
    # this process rather than a separate worker container — see the module docstring in
    # app/services/downloader_agents/base/job_queue.py for why. An export is queued by a
    # chat turn and built here; without this, a confirmed download would sit in the
    # queue and the user would be told it was coming.
    job_queue.start_worker()
    _background_tasks.append(
        asyncio.create_task(
            download_service.run_expiry_reaper(), name="download-expiry-reaper",
        )
    )

    # And the same for files a Create File block wrote. Its own reaper rather than a
    # branch inside that one: the two have different TTLs for different reasons (see
    # file_service.FILE_TTL_SECONDS) and neither module should have to know the other's
    # table to sweep its own.
    _background_tasks.append(
        asyncio.create_task(
            generated_file_service.run_expiry_reaper(),
            name="generated-file-expiry-reaper",
        )
    )

    # The integration queue and its scheduler, in this process for the same reason. A
    # workflow set to run every hour has nobody watching it, so "on a schedule" is only a
    # feature if something ticks — and both hold their state in columns rather than in
    # memory, so N replicas each running these is safe: the claims are `FOR UPDATE SKIP
    # LOCKED` and a trigger fires once however many schedulers see it due.
    integration_queue.start_workers()
    integration_scheduler.start_scheduler()

    # The email send workers. Safe across replicas for the same reason: the claim is
    # `FOR UPDATE SKIP LOCKED` and it additionally refuses a second message for an SMTP
    # server that already has one in flight, so N replicas do not become N simultaneous
    # connections to one provider.
    #
    # No scheduler alongside it, deliberately. Email has no scheduled triggers — an
    # event or a webhook fires it — so this adds exactly one background loop rather
    # than two. See documentations/EMAIL_DISPATCH.md on the loop budget.
    email_queue.start_workers()


async def on_shutdown() -> None:
    """Release the Ollama connection, stop the worker, and close the checkpoint pool."""
    await ollama_client.close_client()

    # Stop the worker before the pool it writes through. A job cancelled mid-export is
    # left saying "running" and is requeued by the stale-job reaper on the next boot —
    # the same recovery a crash gets, deliberately, so there is only one path to test.
    await job_queue.stop_worker()

    # And the graph runs, for the same reason and before the checkpointer they write
    # through: a run torn down with the event loop is one whose sessions never unwind,
    # whereas a cancelled one at least gets to.
    await graph_run_service.stop_all_runs()

    # The integration side, in dependency order: stop the scheduler so nothing new is
    # enqueued, then the workers so nothing new is claimed, then the runs themselves.
    # Reversing any pair means shutting down while something is still creating work.
    await integration_scheduler.stop_scheduler()
    await integration_queue.stop_workers()
    await integration_run_service.stop_all_runs()

    # The email workers, after the things that enqueue for them. A message cancelled
    # mid-send stays `sending` and is *failed* by the stale reaper rather than retried:
    # the relay may already have taken it, and re-sending on the next boot would deliver
    # it twice. See queue.stop_workers.
    await email_queue.stop_workers()

    # The pooled outbound clients an integration run keeps per origin. Closed here rather
    # than per run, which is the point of pooling them — a forty-page sync must not pay
    # forty TLS handshakes.
    await integration_http.close_all_clients()

    for task in _background_tasks:
        task.cancel()

    _background_tasks.clear()

    await close_checkpointer()
    await release_all_readers()

    # The other half of release_all_readers: an aggregation run holds its records in
    # a module registry while the graph passes keys around, and a process going down
    # mid-run leaves them there. Not async — this drops references, it does not close
    # cursors — but it belongs beside the readers so the two are read together.
    frame_buffer.release_all()


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
        ToolGraphController,
        GraphDesignerController,
        # The connections controller is registered before the workflows one so its
        # `/integrations/connections/...` paths are matched before
        # `/integrations/{flow_id:uuid}/...` gets a chance to try parsing "connections" as
        # a uuid. Litestar resolves literal segments ahead of typed ones anyway, but the
        # ordering makes that independent of the router's internals.
        IntegrationAIController,
        IntegrationAppController,
        IntegrationConnectionController,
        IntegrationsController,
        # Three literal prefixes under /emails, so no ordering hazard here — none of them
        # takes a uuid in the segment that would otherwise collide.
        EmailMessageController,
        EmailSmtpController,
        EmailTemplateController,
        EmailTriggerController,
        SqlAssistController,
        AggregationController,
        QueryTestController,
        DeepAgentController,
        DownloadController,
        PublicChatbotController,
        PublicDownloadController,
        EmailWebhookController,
        FileDownloadController,
        GeneratedFileController,
        PublicGeneratedFileController,
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