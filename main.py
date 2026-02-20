from pathlib import Path

from litestar import Litestar, get
from litestar.contrib.jinja import JinjaTemplateEngine
from litestar.template.config import TemplateConfig
from litestar.plugins.htmx import HTMXPlugin, HTMXRequest
from litestar.middleware.session.server_side import ServerSideSessionConfig
from litestar.plugins.flash import FlashConfig, FlashPlugin

from litestar.response import Template

from app.routes.base_routes import auth_router


# Template configuration
template_config = TemplateConfig(
    engine=JinjaTemplateEngine,
    directory=Path("templates"),
)

# Session middleware
session_config = ServerSideSessionConfig()

# Flash plugin (depends on template config)
flash_plugin = FlashPlugin(
    config = FlashConfig(
        template_config=template_config
    )
)


@get("/")
async def root() -> Template:
    return Template(
        template_name="base/index.htm",
        context={"app_name": "GetMyStuff"},
    )

app = Litestar(
    route_handlers=[
        root,
        auth_router
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
)
