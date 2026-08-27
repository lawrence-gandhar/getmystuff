"""
HTTP layer for Email Dispatch. Accepts the request, hands the raw form to a service, and
renders a template — no business rules live here.

Four controllers rather than one, split by the thing each manages: servers, templates,
triggers' cousin the delivery log, and the log's actions. The integrations module makes the
same split for the same reason — one controller per concern keeps each file readable and
keeps the route prefixes honest.

Every mutation answers with the same fragment shape: an alert div carrying a
``data-success`` marker for the page-level response target, plus an out-of-band refresh of
the table it changed. One response drives both, and the modal's after-request hook closes
itself on the success marker.

**Every failure is caught as ``HTTPException`` and rendered into the fragment**, never
raised to the browser. A raised error would replace a page holding a half-finished form with
a banner, losing the operator's work — the same rule the canvas pages follow. The one
exception is a 404 from a uuid that is not the user's, which is allowed to propagate: there
is no form to preserve and the global handler's page is the right answer.
"""

import uuid

from litestar import Controller, get, post
from litestar.connection import Request
from litestar.exceptions import HTTPException
from litestar.response import Template
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.auth import require_auth
from app.models.email_dispatch import (
    MESSAGE_SOURCES,
    SMTP_SECURITIES,
    TRIGGER_KINDS,
)
from app.models.user import User
from app.schemas.email_dispatch import (
    MessageFilterRequest,
    SendTestRequest,
    TriggerCreateRequest,
    TriggerSetEnabledRequest,
    TriggerUpdateRequest,
    SmtpConfigCreateRequest,
    SmtpConfigUpdateRequest,
    SmtpSetActiveRequest,
    TemplateCreateRequest,
    TemplateSetActiveRequest,
    TemplateUpdateRequest,
)
from app.services.email_dispatch import (
    message_service,
    smtp_service,
    template_service,
    trigger_service,
)
from app.services.workspaces import workspace_service
from app.utils.events import EVENT_NAMES

_SMTP_PAGE = "email_dispatch/smtp.htm"
_SMTP_ROWS = "email_dispatch/partials/smtp_rows.htm"
_SMTP_FORM = "email_dispatch/partials/smtp_form.htm"
_SMTP_TEST = "email_dispatch/partials/smtp_test.htm"

_TEMPLATE_PAGE = "email_dispatch/templates.htm"
_TEMPLATE_ROWS = "email_dispatch/partials/template_rows.htm"
_TEMPLATE_FORM = "email_dispatch/partials/template_form.htm"
_TEMPLATE_PREVIEW = "email_dispatch/partials/template_preview.htm"

_TRIGGER_PAGE = "email_dispatch/triggers.htm"
_TRIGGER_ROWS = "email_dispatch/partials/trigger_rows.htm"
_TRIGGER_FORM = "email_dispatch/partials/trigger_form.htm"
_TRIGGER_SECRET = "email_dispatch/partials/trigger_secret.htm"

_MESSAGE_PAGE = "email_dispatch/messages.htm"
_MESSAGE_ROWS = "email_dispatch/partials/message_rows.htm"
_MESSAGE_DETAIL = "email_dispatch/partials/message_detail.htm"

_MODAL_ERROR = "email_dispatch/partials/modal_error.htm"


class EmailSmtpController(Controller):
    """The SMTP servers mail may go out through."""

    path = "/emails/smtp"
    dependencies = {"user": require_auth}

    @get("/")
    async def index(self, db: AsyncSession, user: User) -> Template:
        return Template(
            template_name=_SMTP_PAGE,
            context={
                "user": user,
                "active": "emails",
                "configs": await smtp_service.list_views(db, user.id),
                "securities": SMTP_SECURITIES,
            },
        )

    @get("/new-form")
    async def new_form(self, db: AsyncSession, user: User) -> Template:
        """Blank create form — the same partial the edit form uses."""
        return Template(
            template_name=_SMTP_FORM,
            context={
                "config": None,
                "securities": SMTP_SECURITIES,
                "workspaces": await workspace_service.get_workspace_choices(db, user.id),
                "form_action": "/emails/smtp/create",
                "submit_label": "Add server",
            },
        )

    @get("/{config_id:uuid}/edit-form")
    async def edit_form(
        self, config_id: uuid.UUID, db: AsyncSession, user: User
    ) -> Template:
        try:
            config = await smtp_service.get_config(db, user.id, config_id)
        except HTTPException as exc:
            return Template(
                template_name=_MODAL_ERROR, context={"error": str(exc.detail)}
            )

        return Template(
            template_name=_SMTP_FORM,
            context={
                # The view, not the row: it is the thing that has no password on it.
                "config": smtp_service.build_view(config),
                "workspace_id": await workspace_service.get_workspace_public_id(
                    db, user.id, config.workspace_id
                ),
                "securities": SMTP_SECURITIES,
                "workspaces": await workspace_service.get_workspace_choices(db, user.id),
                "form_action": f"/emails/smtp/{config.uuid}/update",
                "submit_label": "Save changes",
            },
        )

    @post("/create")
    async def create(self, request: Request, db: AsyncSession, user: User) -> Template:
        error = None
        try:
            payload = await SmtpConfigCreateRequest.from_form(request)
            await smtp_service.create_config(
                db,
                user.id,
                name=payload.name,
                host=payload.host,
                port=payload.port,
                security=payload.security,
                from_email=payload.from_email,
                from_name=payload.from_name or "",
                reply_to=payload.reply_to or "",
                username=payload.username or "",
                password=payload.password or "",
                timeout_seconds=payload.timeout_seconds,
                workspace_uuid=payload.workspace_id,
            )
        except HTTPException as exc:
            error = str(exc.detail)

        return await self._rows(db, user, error)

    @post("/{config_id:uuid}/update")
    async def update(
        self, config_id: uuid.UUID, request: Request, db: AsyncSession, user: User
    ) -> Template:
        error = None
        try:
            payload = await SmtpConfigUpdateRequest.from_form(request)
            await smtp_service.update_config(
                db,
                user.id,
                config_id,
                name=payload.name,
                host=payload.host,
                port=payload.port,
                security=payload.security,
                from_email=payload.from_email,
                from_name=payload.from_name or "",
                reply_to=payload.reply_to or "",
                username=payload.username or "",
                password=payload.password or "",
                timeout_seconds=payload.timeout_seconds,
                workspace_uuid=payload.workspace_id,
                clear_password=payload.clear_password,
            )
        except HTTPException as exc:
            error = str(exc.detail)

        return await self._rows(db, user, error)

    @post("/{config_id:uuid}/set-active")
    async def set_active(
        self, config_id: uuid.UUID, request: Request, db: AsyncSession, user: User
    ) -> Template:
        error = None
        try:
            payload = await SmtpSetActiveRequest.from_form(request)
            await smtp_service.set_active(db, user.id, config_id, payload.is_active)
        except HTTPException as exc:
            error = str(exc.detail)

        return await self._rows(db, user, error)

    @post("/{config_id:uuid}/test")
    async def test(
        self, config_id: uuid.UUID, db: AsyncSession, user: User
    ) -> Template:
        """
        Connect, authenticate, hang up. Sends nothing.

        Answers 200 whether the server accepted the credentials or refused them: a refusal
        is the *result* of the test, not a failure to perform it. Only "that server is not
        yours" is an error here, and it propagates.
        """
        result = await smtp_service.test_config(db, user.id, config_id)
        return Template(template_name=_SMTP_TEST, context=result)

    @post("/{config_id:uuid}/delete")
    async def remove(
        self, config_id: uuid.UUID, db: AsyncSession, user: User
    ) -> Template:
        error = None
        try:
            await smtp_service.delete_config(db, user.id, config_id)
        except HTTPException as exc:
            error = str(exc.detail)

        return await self._rows(db, user, error)

    async def _rows(self, db: AsyncSession, user: User, error) -> Template:
        """The one response every mutation returns: alert marker plus an OOB table swap."""
        return Template(
            template_name=_SMTP_ROWS,
            context={
                "configs": await smtp_service.list_views(db, user.id),
                "error": error,
            },
        )


class EmailTemplateController(Controller):
    """The emails themselves, with their placeholders declared."""

    path = "/emails/templates"
    dependencies = {"user": require_auth}

    @get("/")
    async def index(self, db: AsyncSession, user: User) -> Template:
        return Template(
            template_name=_TEMPLATE_PAGE,
            context={
                "user": user,
                "active": "emails",
                "templates": await template_service.list_views(db, user.id),
            },
        )

    @get("/new-form")
    async def new_form(self, db: AsyncSession, user: User) -> Template:
        return Template(
            template_name=_TEMPLATE_FORM,
            context={
                "template": None,
                "workspaces": await workspace_service.get_workspace_choices(db, user.id),
                "form_action": "/emails/templates/create",
                "submit_label": "Create template",
            },
        )

    @get("/{template_id:uuid}/edit-form")
    async def edit_form(
        self, template_id: uuid.UUID, db: AsyncSession, user: User
    ) -> Template:
        try:
            template = await template_service.get_template(db, user.id, template_id)
        except HTTPException as exc:
            return Template(
                template_name=_MODAL_ERROR, context={"error": str(exc.detail)}
            )

        return Template(
            template_name=_TEMPLATE_FORM,
            context={
                "template": template_service.build_view(template),
                "workspace_id": await workspace_service.get_workspace_public_id(
                    db, user.id, template.workspace_id
                ),
                "workspaces": await workspace_service.get_workspace_choices(db, user.id),
                "unused": template_service.unused_variables(template),
                "form_action": f"/emails/templates/{template.uuid}/update",
                "submit_label": "Save changes",
            },
        )

    @post("/create")
    async def create(self, request: Request, db: AsyncSession, user: User) -> Template:
        error = None
        try:
            payload = await TemplateCreateRequest.from_form(request)
            await template_service.create_template(
                db,
                user.id,
                name=payload.name,
                description=payload.description or "",
                subject_template=payload.subject_template,
                body_html_template=payload.body_html_template,
                body_text_template=payload.body_text_template or "",
                variables=payload.variables_json,
                workspace_uuid=payload.workspace_id,
            )
        except HTTPException as exc:
            error = str(exc.detail)

        return await self._rows(db, user, error)

    @post("/{template_id:uuid}/update")
    async def update(
        self, template_id: uuid.UUID, request: Request, db: AsyncSession, user: User
    ) -> Template:
        error = None
        try:
            payload = await TemplateUpdateRequest.from_form(request)
            await template_service.update_template(
                db,
                user.id,
                template_id,
                name=payload.name,
                description=payload.description or "",
                subject_template=payload.subject_template,
                body_html_template=payload.body_html_template,
                body_text_template=payload.body_text_template or "",
                variables=payload.variables_json,
                workspace_uuid=payload.workspace_id,
            )
        except HTTPException as exc:
            error = str(exc.detail)

        return await self._rows(db, user, error)

    @post("/{template_id:uuid}/set-active")
    async def set_active(
        self, template_id: uuid.UUID, request: Request, db: AsyncSession, user: User
    ) -> Template:
        error = None
        try:
            payload = await TemplateSetActiveRequest.from_form(request)
            await template_service.set_active(
                db, user.id, template_id, payload.is_active
            )
        except HTTPException as exc:
            error = str(exc.detail)

        return await self._rows(db, user, error)

    @get("/{template_id:uuid}/preview")
    async def preview(
        self, template_id: uuid.UUID, db: AsyncSession, user: User
    ) -> Template:
        """
        Render with stand-in values, into the preview pane.

        A half-written template is the normal case while somebody is typing, so the pane
        shows what is wrong rather than the request failing — which would replace the pane
        they are working in.
        """
        template = await template_service.get_template(db, user.id, template_id)
        return Template(
            template_name=_TEMPLATE_PREVIEW,
            context=template_service.preview(template),
        )

    @post("/{template_id:uuid}/delete")
    async def remove(
        self, template_id: uuid.UUID, db: AsyncSession, user: User
    ) -> Template:
        error = None
        try:
            await template_service.delete_template(db, user.id, template_id)
        except HTTPException as exc:
            error = str(exc.detail)

        return await self._rows(db, user, error)

    async def _rows(self, db: AsyncSession, user: User, error) -> Template:
        return Template(
            template_name=_TEMPLATE_ROWS,
            context={
                "templates": await template_service.list_views(db, user.id),
                "error": error,
            },
        )


class EmailTriggerController(Controller):
    """
    Standing instructions: on this event, or on this webhook, send that template.

    The one route here that is not ordinary CRUD is ``rotate-secret``, which returns the new
    plaintext secret in a partial rendered exactly once. It is a POST rather than a GET for
    the obvious reason — it changes the endpoint id — and because a GET returning a secret
    ends up in a browser history and a proxy log.
    """

    path = "/emails/triggers"
    dependencies = {"user": require_auth}

    @get("/")
    async def index(self, db: AsyncSession, user: User) -> Template:
        return Template(
            template_name=_TRIGGER_PAGE,
            context={
                "user": user,
                "active": "emails",
                "triggers": await trigger_service.list_views(db, user.id),
                "kinds": TRIGGER_KINDS,
                "events": EVENT_NAMES,
            },
        )

    @get("/new-form")
    async def new_form(self, db: AsyncSession, user: User) -> Template:
        return Template(
            template_name=_TRIGGER_FORM,
            context={
                "trigger": None,
                "kinds": TRIGGER_KINDS,
                "events": EVENT_NAMES,
                "templates": await template_service.choices(db, user.id),
                "configs": await smtp_service.choices(db, user.id),
                "workspaces": await workspace_service.get_workspace_choices(db, user.id),
                "form_action": "/emails/triggers/create",
                "submit_label": "Create trigger",
            },
        )

    @get("/{trigger_id:uuid}/edit-form")
    async def edit_form(
        self, trigger_id: uuid.UUID, db: AsyncSession, user: User
    ) -> Template:
        try:
            trigger = await trigger_service.get_trigger(db, user.id, trigger_id)
        except HTTPException as exc:
            return Template(
                template_name=_MODAL_ERROR, context={"error": str(exc.detail)}
            )

        return Template(
            template_name=_TRIGGER_FORM,
            context={
                "trigger": trigger_service.build_view(trigger),
                "workspace_id": await workspace_service.get_workspace_public_id(
                    db, user.id, trigger.workspace_id
                ),
                "kinds": TRIGGER_KINDS,
                "events": EVENT_NAMES,
                "templates": await template_service.choices(db, user.id),
                "configs": await smtp_service.choices(db, user.id),
                "workspaces": await workspace_service.get_workspace_choices(db, user.id),
                "form_action": f"/emails/triggers/{trigger.uuid}/update",
                "submit_label": "Save changes",
            },
        )

    @post("/create")
    async def create(self, request: Request, db: AsyncSession, user: User) -> Template:
        """
        Create a trigger.

        On success this renders the secret partial rather than the rows partial, because a
        webhook trigger's signing secret can only be shown now. The partial refreshes the
        table out of band as well, so the operator still sees the new row behind it.
        """
        try:
            payload = await TriggerCreateRequest.from_form(request)
            view = await trigger_service.create_trigger(
                db,
                user.id,
                name=payload.name,
                kind=payload.kind,
                template_uuid=payload.template_id,
                smtp_uuid=payload.smtp_config_id,
                recipients=payload.recipients_json,
                variable_bindings=payload.bindings_json,
                event_name=payload.event_name,
                min_interval_seconds=payload.min_interval_seconds,
                workspace_uuid=payload.workspace_id,
            )
        except HTTPException as exc:
            return await self._rows(db, user, str(exc.detail))

        return Template(
            template_name=_TRIGGER_SECRET,
            context={
                "trigger": view,
                "triggers": await trigger_service.list_views(db, user.id),
                "error": None,
            },
        )

    @post("/{trigger_id:uuid}/update")
    async def update(
        self, trigger_id: uuid.UUID, request: Request, db: AsyncSession, user: User
    ) -> Template:
        error = None
        try:
            payload = await TriggerUpdateRequest.from_form(request)
            await trigger_service.update_trigger(
                db,
                user.id,
                trigger_id,
                name=payload.name,
                template_uuid=payload.template_id,
                smtp_uuid=payload.smtp_config_id,
                recipients=payload.recipients_json,
                variable_bindings=payload.bindings_json,
                event_name=payload.event_name,
                min_interval_seconds=payload.min_interval_seconds,
                workspace_uuid=payload.workspace_id,
            )
        except HTTPException as exc:
            error = str(exc.detail)

        return await self._rows(db, user, error)

    @post("/{trigger_id:uuid}/set-enabled")
    async def set_enabled(
        self, trigger_id: uuid.UUID, request: Request, db: AsyncSession, user: User
    ) -> Template:
        error = None
        try:
            payload = await TriggerSetEnabledRequest.from_form(request)
            await trigger_service.set_enabled(
                db, user.id, trigger_id, payload.is_enabled
            )
        except HTTPException as exc:
            error = str(exc.detail)

        return await self._rows(db, user, error)

    @post("/{trigger_id:uuid}/rotate-secret")
    async def rotate_secret(
        self, trigger_id: uuid.UUID, db: AsyncSession, user: User
    ) -> Template:
        """Issue a new secret and endpoint id, shown once. Every existing caller breaks —
        which is the point of a rotation, and the UI says so before it happens."""
        try:
            view = await trigger_service.rotate_secret(db, user.id, trigger_id)
        except HTTPException as exc:
            return await self._rows(db, user, str(exc.detail))

        return Template(
            template_name=_TRIGGER_SECRET,
            context={
                "trigger": view,
                "triggers": await trigger_service.list_views(db, user.id),
                "error": None,
                "rotated": True,
            },
        )

    @post("/{trigger_id:uuid}/delete")
    async def remove(
        self, trigger_id: uuid.UUID, db: AsyncSession, user: User
    ) -> Template:
        error = None
        try:
            await trigger_service.delete_trigger(db, user.id, trigger_id)
        except HTTPException as exc:
            error = str(exc.detail)

        return await self._rows(db, user, error)

    async def _rows(self, db: AsyncSession, user: User, error) -> Template:
        return Template(
            template_name=_TRIGGER_ROWS,
            context={
                "triggers": await trigger_service.list_views(db, user.id),
                "error": error,
            },
        )


class EmailMessageController(Controller):
    """The delivery log, and the two things an operator can do from it."""

    path = "/emails/messages"
    dependencies = {"user": require_auth}

    @get("/")
    async def index(self, request: Request, db: AsyncSession, user: User) -> Template:
        filters = MessageFilterRequest.from_query(request)
        page = await message_service.list_views(
            db,
            user.id,
            status=filters.status,
            source=filters.source,
            page=filters.page,
        )
        return Template(
            template_name=_MESSAGE_PAGE,
            context={
                "user": user,
                "active": "emails",
                "sources": MESSAGE_SOURCES,
                # Populate the test-send form without a second request.
                "templates": await template_service.choices(db, user.id),
                "configs": await smtp_service.choices(db, user.id),
                **page,
            },
        )

    @get("/rows")
    async def rows(self, request: Request, db: AsyncSession, user: User) -> Template:
        """The table on its own, for the filter chips and the auto-refresh poll."""
        filters = MessageFilterRequest.from_query(request)
        page = await message_service.list_views(
            db,
            user.id,
            status=filters.status,
            source=filters.source,
            page=filters.page,
        )
        return Template(template_name=_MESSAGE_ROWS, context={"error": None, **page})

    @get("/{message_id:uuid}")
    async def detail(
        self, message_id: uuid.UUID, db: AsyncSession, user: User
    ) -> Template:
        return Template(
            template_name=_MESSAGE_DETAIL,
            context={"message": await message_service.get_detail(db, user.id, message_id)},
        )

    @post("/{message_id:uuid}/retry")
    async def retry(
        self, message_id: uuid.UUID, request: Request, db: AsyncSession, user: User
    ) -> Template:
        error = None
        try:
            await message_service.retry(db, user.id, message_id)
        except HTTPException as exc:
            error = str(exc.detail)

        return await self._rows(request, db, user, error)

    @post("/{message_id:uuid}/cancel")
    async def cancel(
        self, message_id: uuid.UUID, request: Request, db: AsyncSession, user: User
    ) -> Template:
        error = None
        try:
            await message_service.cancel(db, user.id, message_id)
        except HTTPException as exc:
            error = str(exc.detail)

        return await self._rows(request, db, user, error)

    @post("/send-test")
    async def send_test(
        self, request: Request, db: AsyncSession, user: User
    ) -> Template:
        """
        Queue one real email, to prove the whole path end to end.

        Unlike the SMTP page's Test button, this genuinely sends — the two are separate so
        that checking a server does not email anybody.
        """
        error = None
        try:
            payload = await SendTestRequest.from_form(request)
            await message_service.send_test(
                db,
                user.id,
                template_id=payload.template_id,
                config_id=payload.smtp_config_id,
                to_address=payload.to_address,
            )
        except HTTPException as exc:
            error = str(exc.detail)

        return await self._rows(request, db, user, error)

    async def _rows(
        self, request: Request, db: AsyncSession, user: User, error
    ) -> Template:
        """
        The log table, preserving whatever filter the operator was looking at.

        Re-reading the filters from the query string matters: a retry pressed while filtered
        to Failed must come back still filtered to Failed, or the row the operator just
        acted on vanishes into an unfiltered list of everything.
        """
        filters = MessageFilterRequest.from_query(request)
        page = await message_service.list_views(
            db,
            user.id,
            status=filters.status,
            source=filters.source,
            page=filters.page,
        )
        return Template(template_name=_MESSAGE_ROWS, context={"error": error, **page})
