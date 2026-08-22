"""
HTTP layer for the Apps gallery — the page somebody lands on when they open Integrations.

**It is a third view of things that already exist, not a fourth kind of thing.** An "app"
is a connector from the registry with this user's connection counts beside it, and
connecting one creates an ordinary ``integration_connections`` row through the same
``connection_service.create_connection`` the Connections page calls. There is no app
table, no app model and no second way to store a credential — which is the only reason a
gallery is worth having at all.

**The connector comes from the path, never from the form.** The connect handler passes
``connector_id`` to ``ConnectionCreateRequest.from_form`` as an override, and overrides are
applied after the body — so a posted ``connector_id`` cannot decide which connector's rules
a credential is stored under. Connecting Brevo from the Brevo tile creates a Brevo
connection or nothing.

**A refusal is a 200 carrying the sentence, and the dialog stays open.** Same contract as
every other form in this feature: the response is swapped into ``#apps-response``, the
modal closes only when it finds ``data-success=true``, and a rejected shop domain leaves
what was typed on screen. Only a connector that does not exist is a refusal the *page* has
to show, and it arrives as the registry's own sentence naming what is available.
"""

import logging
from typing import Optional

from litestar import Controller, get, post
from litestar.connection import Request
from litestar.exceptions import HTTPException
from litestar.response import Template
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.auth import require_auth
from app.models.user import User
from app.schemas.integrations import AppView, ConnectionCreateRequest
from app.services.integrations import connection_service

logger = logging.getLogger(__name__)

_LIST_TEMPLATE = "integrations/apps.htm"
_FORM_TEMPLATE = "integrations/partials/app_connect_form.htm"
_RESULT_TEMPLATE = "integrations/partials/app_connect_result.htm"
_MODAL_ERROR_TEMPLATE = "integrations/partials/modal_error.htm"


class IntegrationAppController(Controller):
    """The apps a workflow can be pointed at, and connecting one."""

    path = "/integrations/apps"
    dependencies = {"user": require_auth}

    # --------------------------
    # GALLERY
    # --------------------------
    @get("/")
    async def index(self, db: AsyncSession, user: User) -> Template:
        """Every app, with how many connections this user has to each."""
        apps = await connection_service.list_apps(db, user.id)

        return Template(
            template_name=_LIST_TEMPLATE,
            context={
                "user": user,
                "apps": AppView.payload_for_many(apps),
                "active": "integrations",
                "tab": "apps",
            },
        )

    @get("/{connector_id:str}/connect-form")
    async def connect_form(
        self, connector_id: str, db: AsyncSession, user: User
    ) -> Template:
        """
        The credential dialog's body for one app, fetched per open.

        Rendered from the connector's spec rather than switched with JavaScript, which is
        the difference between this and the Add connection dropdown: there the user
        chooses a system in the same form, so the fields have to rearrange in the browser;
        here the app was already chosen by clicking its tile, and the server knows which
        questions it has. Nothing to keep in step, and nothing to break if a script fails
        to load.
        """
        try:
            app = connection_service.describe_app(connector_id)
        except HTTPException as exc:
            return Template(
                template_name=_MODAL_ERROR_TEMPLATE, context={"error": str(exc.detail)}
            )

        return Template(
            template_name=_FORM_TEMPLATE,
            context={"user": user, "app": AppView.build(app).payload()},
        )

    @post("/{connector_id:str}/connect")
    async def connect(
        self, connector_id: str, request: Request, db: AsyncSession, user: User
    ) -> Template:
        """
        Add a connection to this app.

        The connector is the one in the path. See the module docstring for why that is not
        negotiable from the body.
        """
        error = None

        try:
            payload = await ConnectionCreateRequest.from_form(
                request, connector_id=connector_id
            )
            await connection_service.create_connection(
                db,
                user.id,
                connector_id=payload.connector_id,
                label=payload.label,
                base_url=payload.base_url,
                external_account_id=payload.external_account_id,
                api_key=payload.api_key,
                username=payload.username,
                password=payload.password,
            )
        except HTTPException as exc:
            error = str(exc.detail)

        return await self._result(db, user, connector_id, error)

    # --------------------------
    # HELPERS
    # --------------------------
    async def _result(
        self, db: AsyncSession, user: User, connector_id: str, error: Optional[str]
    ) -> Template:
        """
        The outcome of one connect, plus the whole grid again.

        The grid rides along out-of-band because the counts on it are now wrong — a tile
        that still says "Not connected" under a dialog that just said "Connected" is the
        kind of stale screen that has somebody adding the same credential twice.
        """
        apps = await connection_service.list_apps(db, user.id)
        connected = next(
            (app for app in apps if app["connector_id"] == connector_id), None
        )

        return Template(
            template_name=_RESULT_TEMPLATE,
            context={
                "user": user,
                "apps": AppView.payload_for_many(apps),
                "error": error,
                "app_label": (connected or {}).get("label", connector_id),
                "tab": "apps",
            },
        )
