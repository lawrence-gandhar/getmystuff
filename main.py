from pathlib import Path
import uvicorn

from litestar import Litestar, get
from litestar.contrib.jinja import JinjaTemplateEngine
from litestar.template.config import TemplateConfig
from litestar.plugins.htmx import HTMXPlugin, HTMXRequest
from litestar.middleware.session.server_side import ServerSideSessionConfig
from litestar.plugins.flash import FlashConfig, FlashPlugin
from litestar.response import Template
from litestar.static_files.config import StaticFilesConfig

from app.routes.base_routes import AuthController
from app.routes.dashboard_routes import DashboardController
from app.routes.datasource_routes import DataSourceController

from app.db.db_sessions import get_db
from app.db.base import Base
from app.db.db_sessions import engine


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
    Creates tables automatically.
    Remove in production and use Alembic.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


# -----------------------------
# Create App
# -----------------------------
app = Litestar(
    route_handlers=[
        root,
        AuthController,
        DashboardController,
        DataSourceController
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
    on_startup=[on_startup],
)


# -----------------------------
# Main Entry Point
# -----------------------------
if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info",
    )