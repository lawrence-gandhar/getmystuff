"""
HTTP layer for connections, their credentials and their operations.

**A secret arrives in a request body and never leaves in a response.** No handler here
returns one, no view schema declares one, and the connections page renders a mask the
browser builds from the value it already had. That rule is enforced two layers down —
``credential_service.build_connection_views`` reads the connection row alone and the
credential table is deliberately not joined — so a handler here would have to work at it to
leak one.

**Testing a connection is a 200 whether it worked or not.** A 401 from a vendor is an
*answer*: the page needs to show it next to a Reconnect button, and raising would turn a
perfectly informative outcome into a 500. Only a connection this user does not have is a
404.

**The administrator check for private hosts is not in this file.** It is in
``connection_service.set_private_host_access``, because a business rule a second route
could skip is not a rule — and that is the one setting in the module that lets a request
reach an address inside the network. The handler passes ``is_admin`` and the service
decides.
"""

import logging
import uuid as uuid_pkg
from typing import Optional

from litestar import Controller, delete, get, post
from litestar.connection import Request
from litestar.exceptions import HTTPException
from litestar.response import Template
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.auth import require_auth
from app.models.user import User
from app.schemas.integrations import (
    ConnectionCreateRequest,
    ConnectionTestQuery,
    ConnectionTestView,
    ConnectionUpdateRequest,
    ConnectionView,
    ConnectorView,
    OperationSaveRequest,
    OperationSchemaView,
    OperationView,
    PrivateHostRequest,
)
from app.services.integrations import connection_service
from app.services.integrations.connectors import registry

logger = logging.getLogger(__name__)

_LIST_TEMPLATE = "integrations/connections.htm"
_ROWS_TEMPLATE = "integrations/partials/connection_rows.htm"
_TEST_TEMPLATE = "integrations/partials/connection_test.htm"
_MODAL_ERROR_TEMPLATE = "integrations/partials/modal_error.htm"
_FORM_TEMPLATE = "integrations/partials/connection_form.htm"

#: What decides whether somebody may switch on the on-premise escape hatch. Read here and
#: *passed* to the service, which is where it is enforced — see the module docstring.
ADMIN_ROLE = "admin"


class IntegrationConnectionController(Controller):
    """Connections to the systems a workflow reads from and writes to."""

    path = "/integrations/connections"
    dependencies = {"user": require_auth}

    # --------------------------
    # LIBRARY
    # --------------------------
    @get("/")
    async def index(self, db: AsyncSession, user: User) -> Template:
        """Every connection this user owns, and the connectors they can add another of."""
        connections = await connection_service.list_connections(db, user.id)

        return Template(
            template_name=_LIST_TEMPLATE,
            context={
                "user": user,
                "connections": ConnectionView.payload_for_many(
                    connection_service.build_views(connections)
                ),
                "connectors": ConnectorView.payload_for_many(
                    registry.describe_connectors()
                ),
                "active": "integrations",
                "tab": "connections",
            },
        )

    @post("/create")
    async def create(self, request: Request, db: AsyncSession, user: User) -> Template:
        """
        Add a connection and store its credential.

        ``auth_kind`` is not a form field: it comes from the connector's spec. A connection
        claiming to need no credential against a connector that does would send
        unauthenticated requests and get a 401 that reads like a bad key.
        """
        error = None

        try:
            payload = await ConnectionCreateRequest.from_form(request)
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

        return await self._rows(db, user, error)

    @get("/{connection_id:uuid}/edit-form")
    async def edit_form(
        self, connection_id: uuid_pkg.UUID, db: AsyncSession, user: User
    ) -> Template:
        """
        The edit dialog's body, fetched per open rather than rendered once per row.

        **The credential field comes back empty**, with a placeholder saying a key is
        stored. Not masked-with-the-real-value, which would put the secret in the DOM of a
        page anybody can read; not pre-filled, which would round-trip it through a form.
        Empty means "leave it alone" on save, which is the behaviour the service already
        implements.
        """
        try:
            connection = await connection_service.get_connection(
                db, user.id, connection_id
            )
        except HTTPException as exc:
            return Template(
                template_name=_MODAL_ERROR_TEMPLATE, context={"error": str(exc.detail)}
            )

        view = connection_service.build_views([connection])[0]

        return Template(
            template_name=_FORM_TEMPLATE,
            context={
                "connection": ConnectionView.build(view).payload(),
                "form_action": f"/integrations/connections/{connection_id}/update",
            },
        )

    @post("/{connection_id:uuid}/update")
    async def update(
        self, connection_id: uuid_pkg.UUID, request: Request, db: AsyncSession, user: User
    ) -> Template:
        """Edit a connection. A blank credential field means "leave it alone"."""
        error = None

        try:
            payload = await ConnectionUpdateRequest.from_form(request)
            await connection_service.update_connection(
                db,
                user.id,
                connection_id,
                label=payload.label,
                base_url=payload.base_url,
                api_key=payload.api_key,
                username=payload.username,
                password=payload.password,
            )
        except HTTPException as exc:
            if exc.status_code == 404:
                raise
            error = str(exc.detail)

        return await self._rows(db, user, error)

    @post("/{connection_id:uuid}/active")
    async def set_active(
        self, connection_id: uuid_pkg.UUID, request: Request, db: AsyncSession, user: User
    ) -> Template:
        """Park a connection without deleting it, or bring it back."""
        error = None

        try:
            form = await request.form()
            wanted = str(form.get("is_active", "")).strip().lower() in ("true", "on", "1")
            await connection_service.set_connection_active(
                db, user.id, connection_id, wanted
            )
        except HTTPException as exc:
            if exc.status_code == 404:
                raise
            error = str(exc.detail)

        return await self._rows(db, user, error)

    @post("/{connection_id:uuid}/revoke")
    async def revoke(
        self, connection_id: uuid_pkg.UUID, db: AsyncSession, user: User
    ) -> Template:
        """
        Delete every secret this connection holds.

        The connection row survives on purpose: workflows point at it by uuid, and deleting
        it would turn "reconnect this" into a step whose connection no longer exists.
        """
        error = None

        try:
            await connection_service.revoke_connection(
                db, user.id, connection_id, reason="revoked from the connections page"
            )
        except HTTPException as exc:
            if exc.status_code == 404:
                raise
            error = str(exc.detail)

        return await self._rows(db, user, error)

    @post("/{connection_id:uuid}/delete")
    async def delete_connection(
        self, connection_id: uuid_pkg.UUID, db: AsyncSession, user: User
    ) -> Template:
        """Remove a connection, its credential and its operations."""
        error = None

        try:
            await connection_service.delete_connection(db, user.id, connection_id)
        except HTTPException as exc:
            if exc.status_code == 404:
                raise
            error = str(exc.detail)

        return await self._rows(db, user, error)

    @post("/{connection_id:uuid}/private-hosts")
    async def set_private_hosts(
        self, connection_id: uuid_pkg.UUID, request: Request, db: AsyncSession, user: User
    ) -> Template:
        """
        The on-premise escape hatch.

        ``is_admin`` is read here and **decided** in the service. See the module docstring:
        this is the one setting that lets a request reach inside the network, and a check
        living in a handler is one a second handler can skip.
        """
        error = None

        try:
            payload = await PrivateHostRequest.from_form(request)
            await connection_service.set_private_host_access(
                db,
                user.id,
                connection_id,
                is_admin=str(getattr(user, "role", "")) == ADMIN_ROLE,
                allow=payload.allow,
                hosts=payload.hosts,
                cidrs=payload.cidrs,
            )
        except HTTPException as exc:
            if exc.status_code == 404:
                raise
            error = str(exc.detail)

        return await self._rows(db, user, error)

    # --------------------------
    # TESTING
    # --------------------------
    @post("/{connection_id:uuid}/test")
    async def test(
        self, connection_id: uuid_pkg.UUID, request: Request, db: AsyncSession, user: User
    ) -> Template:
        """
        Make one real call and show what came back.

        **It sends something.** A connection that parses is not a connection that works,
        and the failures worth catching — a key with the wrong scope, a base URL missing
        its version segment, a gateway answering HTML with a 200 — are invisible without a
        request.

        A failed call is a rendered partial, not an exception. Only "there is nothing to
        test" and "that is not your connection" raise, and the first of those is rendered
        into the same panel.
        """
        query = ConnectionTestQuery.from_query(request)

        try:
            result = await connection_service.test_connection(
                db, user.id, connection_id, operation_id=query.operation_id
            )
        except HTTPException as exc:
            if exc.status_code == 404:
                raise
            result = {"ok": False, "message": str(exc.detail)}

        return Template(
            template_name=_TEST_TEMPLATE,
            context={"result": ConnectionTestView.build(result).payload()},
        )

    # --------------------------
    # OPERATIONS
    # --------------------------
    @get("/{connection_id:uuid}/operations")
    async def operations(
        self, connection_id: uuid_pkg.UUID, db: AsyncSession, user: User
    ) -> dict:
        """
        Everything this connection can do, from whichever source it has.

        A declared connector's operations come from its spec, a REST connection's from
        rows — and the branch is in the service, because to everybody upstream they are the
        same thing. That is the whole point of operations being data.
        """
        found = await connection_service.list_operations(db, user.id, connection_id)
        return {"operations": OperationView.payload_for_many(found)}

    @post("/{connection_id:uuid}/operations")
    async def save_operation(
        self, connection_id: uuid_pkg.UUID, request: Request, db: AsyncSession, user: User
    ) -> dict:
        """
        Create or replace one user-authored operation.

        The schema is handed to the service as a **single mapping**, because that is what
        an operation is: ``integration_rest_operations``' columns are ``OperationSpec``'s
        fields, and a call site enumerating them would be a third place that list is
        written down and can fall behind the other two.
        """
        payload = await OperationSaveRequest.from_form(request)
        saved = await connection_service.save_operation(
            db, user.id, connection_id, payload.operation()
        )

        return {"ok": True, "operation_id": saved.operation_id}

    @delete("/{connection_id:uuid}/operations/{operation_id:str}", status_code=200)
    async def delete_operation(
        self,
        connection_id: uuid_pkg.UUID,
        operation_id: str,
        db: AsyncSession,
        user: User,
    ) -> dict:
        """Remove one operation. Steps naming it fail with the registry's own sentence,
        which lists what the connection does still offer."""
        await connection_service.delete_operation(
            db, user.id, connection_id, operation_id
        )
        return {"ok": True}

    @get("/{connection_id:uuid}/schema")
    async def operation_schema(
        self, connection_id: uuid_pkg.UUID, request: Request, db: AsyncSession, user: User
    ) -> dict:
        """
        What one operation reads and what it will accept — the mapping grid's source.

        ``outputs`` fills the source column, ``inputs`` the destination column, and
        ``required`` is what the panel marks red and what Publish refuses. All three come
        from the operation's own declaration, so the picker cannot offer a field the
        request builder would reject.
        """
        query = ConnectionTestQuery.from_query(request)
        found = await connection_service.connection_schema(
            db, user.id, connection_id, query.operation_id
        )

        return OperationSchemaView.build(found).payload()

    # --------------------------
    # HELPERS
    # --------------------------
    async def _rows(
        self, db: AsyncSession, user: User, error: Optional[str] = None
    ) -> Template:
        """The connections table, plus any error. Every mutation answers with this, so the
        list on screen is always the list the database holds."""
        connections = await connection_service.list_connections(db, user.id)

        return Template(
            template_name=_ROWS_TEMPLATE,
            context={
                "user": user,
                "connections": ConnectionView.payload_for_many(
                    connection_service.build_views(connections)
                ),
                "connectors": ConnectorView.payload_for_many(
                    registry.describe_connectors()
                ),
                "error": error,
            },
        )
