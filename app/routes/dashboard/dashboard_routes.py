from datetime import datetime
from litestar import (
    get,
    post,
    Controller
)

from litestar.response import Template, Redirect

from app.db.auth import require_auth
from app.db.models import User



class DashboardController(Controller):
    path = "/user"
    dependencies = {"user": require_auth}

    @get("/dashboard")
    async def dashboard(self, user: User) -> Template:
        return Template(
            template_name="base/dashboard.htm",
            context={
                "user": user,
                "now": datetime,
            },
        )