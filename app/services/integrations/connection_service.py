"""
Connections: one authenticated relationship with one external account, and its operations.

Three rules run through the whole file.

**A secret never comes back out the way it went in.** Credentials live in their own table
behind a unique foreign key, this module never selects from it for a view, and
``build_connection_views`` reads the connection row alone. Adding a secret to a payload
here would mean joining a table that is deliberately not joined — which is the structural
version of a rule that would otherwise be a comment somebody deletes.

**Many connections per connector is the ordinary case.** Three Shopify stores, forty
GoHighLevel locations. That is why uniqueness is on
``(user_id, connector_id, external_account_id)`` rather than on the connector alone, and
why this module reads nothing like ``ai_settings_service``, whose one-active-key-per-
provider rule is right for its problem and wrong for this one.

**Testing a connection makes one real call and reports what came back.** Not a URL parse,
not a ping — the failures worth catching are a key with the wrong scope, a base URL missing
its version segment, and a WAF that answers HTML with a 200, and none of those are visible
without sending something. It goes through the same ``send()`` every node uses, so the
egress guard, the redirect refusal and the byte cap are the ones production runs on.

Generic REST is the Phase 1 connector, so operation authoring lives here too: a user's
operations are ``integration_rest_operations`` rows whose columns *are* ``OperationSpec``'s
fields, and ``load_operation`` turns either source into the same frozen dataclass. One code
path for building a request, whether the operation was written in Python or in a form.
"""

import logging
import uuid as uuid_pkg
from typing import Any, Dict, List, Mapping, Optional, Sequence

from litestar.exceptions import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.db_utils import CRUDQueryBuilder
from app.db.integrations.queries import get_rest_operation, list_rest_operations
from app.models.integrations import (
    AUTH_API_KEY,
    AUTH_BASIC,
    AUTH_NONE,
    CONNECTION_ACTIVE,
    CONNECTION_DISABLED,
    CREDENTIAL_PRIVATE_HOSTS_ENABLED,
    OPERATION_KINDS,
    OPERATION_READ,
    IntegrationConnection,
    IntegrationRestOperation,
)
from app.services.integrations.connectors import registry, spec as connector_spec
from app.services.integrations.connectors.spec import ConnectorSpec, OperationSpec
from app.services.integrations.credentials import credential_service
from app.services.integrations.errors import IntegrationFailure
from app.services.integrations.mapping import paths
from app.services.integrations.runtime import pagination, request_builder, sender
from app.utils import outbound_http

logger = logging.getLogger(__name__)

connection_crud = CRUDQueryBuilder(IntegrationConnection)
operation_crud = CRUDQueryBuilder(IntegrationRestOperation)

#: Said whenever a connection uuid does not resolve for this user, whether it is somebody
#: else's or nobody's. The same sentence for both, so guessing uuids confirms nothing.
NO_SUCH_CONNECTION = "That connection does not exist."

#: How many host entries and CIDR entries the on-premise escape hatch will hold. Small on
#: purpose — an allow-list long enough to be convenient is one nobody audits, and this is
#: the only setting in the module that can point a request inside the network.
MAX_ALLOWLIST_ENTRIES = 10


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


async def list_connections(
    db: AsyncSession, user_id: int
) -> List[IntegrationConnection]:
    """This user's connections, newest first."""
    return await connection_crud.get_many(
        db, filters={"user_id": user_id}, order_by="created_at", desc=True
    )


async def get_connection(
    db: AsyncSession, user_id: int, connection_id: uuid_pkg.UUID
) -> IntegrationConnection:
    """
    One connection, scoped to its owner **in the query**.

    Same reasoning as ``flow_service.get_flow`` and as ``connector_nodes.resolve_target``,
    which is the run-time path to the same row: the guarantee is that another user's row
    is never loaded, not that it is loaded and then rejected.
    """
    connection = await connection_crud.get_by_uuid(
        db, connection_id, extra_filters={"user_id": user_id}
    )
    if connection is None:
        raise HTTPException(status_code=404, detail=NO_SUCH_CONNECTION)
    return connection


def connector_labels() -> Dict[str, str]:
    """``{connector_id: label}`` for the list page."""
    return {spec.connector_id: spec.label for spec in registry.all_connectors()}


def build_views(connections: Sequence[IntegrationConnection]) -> List[dict]:
    """
    Connections shaped for a template or a JSON response.

    Delegates to ``credential_service.build_connection_views``, which is the function that
    provably cannot serialise a secret, rather than assembling a second dictionary here
    that would have to be audited separately.
    """
    return credential_service.build_connection_views(
        list(connections), connector_labels=connector_labels()
    )


async def connection_schema(
    db: AsyncSession,
    user_id: int,
    connection_id: uuid_pkg.UUID,
    operation_id: str,
) -> Dict[str, Any]:
    """
    What one operation reads and what it will accept, for the mapping grid.

    ``outputs`` feeds the source-path column, ``inputs`` the destination column, and
    ``required`` is what the panel marks in red and what ``publish_flow`` refuses. All
    three come from the operation's own declaration, so the picker cannot offer a field
    the request builder would reject.
    """
    connection = await get_connection(db, user_id, connection_id)
    _, operation = await _resolve(db, connection, operation_id)

    return {
        "connection_uuid": str(connection.uuid),
        "operation_id": operation.operation_id,
        "operation_label": operation.label or operation.operation_id,
        "kind": operation.kind,
        "inputs": [_field_view(field) for field in operation.inputs],
        "outputs": [_field_view(field) for field in operation.outputs],
        "required": [field.name for field in operation.inputs if field.required],
    }


def _field_view(field: Any) -> Dict[str, Any]:
    return {
        "name": field.name,
        "label": field.label or field.name,
        "type": field.type,
        "required": field.required,
        "description": field.description,
        "path": field.path or field.name,
    }


async def list_operations(
    db: AsyncSession, user_id: int, connection_id: uuid_pkg.UUID
) -> List[dict]:
    """
    Every operation this connection offers, from whichever source it has one.

    A declared connector's operations are in its spec; a generic REST connection's are
    rows. The branch is here rather than in the caller because the two are the same thing
    to everybody upstream — that is the whole point of operations being data.
    """
    connection = await get_connection(db, user_id, connection_id)
    connector = _require_connector(connection)

    if not connector.operations_are_user_defined:
        return [connector_spec.describe_operation(op) for op in connector.operations]

    rows = await list_rest_operations(db, connection.id)
    return [connector_spec.describe_operation(connector_spec.load_operation(row)) for row in rows]


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


async def create_connection(
    db: AsyncSession,
    user_id: int,
    *,
    connector_id: str,
    label: str,
    base_url: Optional[str] = None,
    external_account_id: Optional[str] = None,
    api_key: Optional[str] = None,
    username: Optional[str] = None,
    password: Optional[str] = None,
) -> IntegrationConnection:
    """
    Create a connection and store its credential, in that order.

    **The connection row lands first because the credential has a foreign key to it**, and
    the two are separate commits rather than one because ``store_credential`` owns the
    encryption and is the only function that should. A crash between them leaves a
    connection with no credential — visible, recoverable by supplying one, and far better
    than the alternative shape where a caller passes ``api_key_encrypted`` and is one
    omission away from writing a plaintext secret into a column named for ciphertext.

    ``auth_kind`` comes from the connector's spec, never from the caller. A connection
    claiming ``none`` against a connector that needs a key would send unauthenticated
    requests and get a 401 that reads like a bad key.
    """
    connector = _require_connector_id(connector_id)
    account_id = _validated_account_id(connector, external_account_id)

    await _require_unused_account(db, user_id, connector.connector_id, account_id)

    connection = await connection_crud.create(
        db,
        {
            "user_id": user_id,
            "connector_id": connector.connector_id,
            "label": _validated_label(label),
            "auth_kind": connector.auth.kind,
            "base_url": _validated_base_url(connector, base_url),
            "external_account_id": account_id or None,
            "status": CONNECTION_ACTIVE,
            "is_active": True,
        },
    )

    secrets = _credential_fields(connector, api_key=api_key, username=username, password=password)
    if secrets:
        await credential_service.store_credential(
            db, connection, user_id=user_id, **secrets
        )

    return connection


async def update_connection(
    db: AsyncSession,
    user_id: int,
    connection_id: uuid_pkg.UUID,
    *,
    label: str,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    username: Optional[str] = None,
    password: Optional[str] = None,
) -> IntegrationConnection:
    """
    Edit a connection, replacing its credential only when a new one was typed.

    **A blank secret means "leave it alone", not "clear it".** The edit form shows a
    masked key, so the field arrives empty on every save where the user did not retype it;
    treating that as a deletion would silently break a working connection every time
    somebody fixed a typo in its label. Clearing a credential is
    :func:`revoke_connection`, which says what it does.

    ``connector_id`` cannot be changed. It decides the auth kind, the operations and the
    egress rules, so changing it would keep a credential issued by one vendor against
    another's endpoints — a new connection is the honest way to say that.
    """
    connection = await get_connection(db, user_id, connection_id)
    connector = _require_connector(connection)

    updated = await connection_crud.update(
        db,
        connection.id,
        {
            "label": _validated_label(label),
            "base_url": _validated_base_url(connector, base_url)
            if base_url is not None
            else connection.base_url,
        },
    )

    secrets = _credential_fields(connector, api_key=api_key, username=username, password=password)
    if secrets:
        await credential_service.store_credential(
            db, updated, user_id=user_id, **secrets
        )

    return updated


async def set_connection_active(
    db: AsyncSession,
    user_id: int,
    connection_id: uuid_pkg.UUID,
    is_active: bool,
) -> IntegrationConnection:
    """
    Park a connection without deleting it, or bring it back.

    Separate from ``status``: a connection can be perfectly authenticated and deliberately
    switched off, and conflating the two would make "disabled by me" indistinguishable
    from "the vendor revoked this" on the page where the difference decides what to do
    next.
    """
    connection = await get_connection(db, user_id, connection_id)

    values: Dict[str, Any] = {"is_active": bool(is_active)}

    # A connection switched off by hand and one revoked by a vendor both stop working, and
    # only the first should come back by pressing a button.
    if connection.status == CONNECTION_ACTIVE and not is_active:
        values["status"] = CONNECTION_DISABLED
    elif connection.status == CONNECTION_DISABLED and is_active:
        values["status"] = CONNECTION_ACTIVE

    return await connection_crud.update(db, connection.id, values)


async def revoke_connection(
    db: AsyncSession,
    user_id: int,
    connection_id: uuid_pkg.UUID,
    *,
    reason: str = "",
) -> IntegrationConnection:
    """
    Delete every secret this connection holds and mark it revoked.

    The connection row survives, and that is deliberate: workflows point at it by uuid, and
    deleting it would turn a "reconnect this" message into a step whose connection no
    longer exists. Reconnecting supplies a credential and the connection works again with
    every workflow still pointing at it.
    """
    connection = await get_connection(db, user_id, connection_id)
    await credential_service.revoke(db, connection, user_id=user_id, reason=reason)
    await db.refresh(connection)
    return connection


async def delete_connection(
    db: AsyncSession, user_id: int, connection_id: uuid_pkg.UUID
) -> None:
    """
    Remove a connection, its credential and its operations.

    The credential goes with it by cascade, which is the point of it being one row behind
    one foreign key: deleting provably leaves nothing rather than leaving whatever a
    hand-written cleanup forgot.

    Workflows pointing at it are **not** rewritten. A step whose connection is gone fails
    with a sentence telling the author to choose one, which is a better outcome than
    silently repointing it at something else.
    """
    connection = await get_connection(db, user_id, connection_id)
    await connection_crud.delete(db, connection.id)


async def set_private_host_access(
    db: AsyncSession,
    user_id: int,
    connection_id: uuid_pkg.UUID,
    *,
    is_admin: bool,
    allow: bool,
    hosts: Optional[Sequence[str]] = None,
    cidrs: Optional[Sequence[str]] = None,
) -> IntegrationConnection:
    """
    The on-premise escape hatch, gated three ways.

    **The admin check is here, in the service, not in the route.** A business rule a second
    route could skip is not a rule, and this is the one setting in the module that lets a
    request reach an address inside the network.

    The other two gates: the connector's own spec has to permit it at all — which is
    ``sap_odata`` and nothing else, so a generic REST connection with a user-supplied base
    URL can never be aimed inwards — and the list is bounded and explicit, with no
    wildcards. Both a host and a CIDR must match at request time; a hostname alone falls to
    a DNS answer the operator does not control, and a CIDR alone permits any name that
    happens to resolve into it.

    Every change writes an audit event carrying the old list and the new one, because the
    question asked after an incident is what it *was*, not what it is.
    """
    connection = await get_connection(db, user_id, connection_id)
    connector = _require_connector(connection)

    if not is_admin:
        raise HTTPException(
            status_code=403,
            detail=(
                "Only an administrator can let a connection reach an address inside your "
                "network."
            ),
        )

    if allow and not connector.allows_private_hosts:
        raise HTTPException(
            status_code=400,
            detail=(
                f"'{connector.label}' connections cannot be pointed at a private address. "
                "This is only available for connectors that talk to on-premise systems."
            ),
        )

    allowlist = _validated_allowlist(hosts, cidrs) if allow else None

    if allow and not (allowlist["hosts"] and allowlist["cidrs"]):
        raise HTTPException(
            status_code=400,
            detail=(
                "A private address needs both a host and a network range. Either one on "
                "its own would allow more than you mean it to."
            ),
        )

    previous = connection.private_host_allowlist

    updated = await connection_crud.update(
        db,
        connection.id,
        {"allow_private_hosts": bool(allow), "private_host_allowlist": allowlist},
    )

    await credential_service.record_event(
        db,
        updated,
        CREDENTIAL_PRIVATE_HOSTS_ENABLED,
        user_id=user_id,
        detail={"allow": bool(allow), "was": previous, "now": allowlist},
    )

    return updated


# ---------------------------------------------------------------------------
# Testing
# ---------------------------------------------------------------------------


async def test_connection(
    db: AsyncSession,
    user_id: int,
    connection_id: uuid_pkg.UUID,
    *,
    operation_id: str = "",
) -> Dict[str, Any]:
    """
    Make one real call and report what came back.

    **It sends something.** A connection that parses is not a connection that works: the
    failures worth catching here are a key with the wrong scope, a base URL missing its
    version segment, and a gateway answering HTML with a 200 — and none of them are
    visible without a request. It goes through ``sender.send``, so the egress guard, the
    redirect refusal, the byte cap and the retry rules are exactly the ones a run uses.

    Never raises for a failed call. A 401 is an *answer*, and the page needs to show it
    next to a Reconnect button rather than as a 500. Only a connection this user does not
    have raises, and that is a 404 from :func:`get_connection`.

    A read operation is chosen by default because a write test would create a record in
    somebody's production system to prove it could.
    """
    connection = await get_connection(db, user_id, connection_id)
    connector = _require_connector(connection)

    operation = await _operation_to_test(db, connection, connector, operation_id)

    try:
        from app.services.integrations.nodes import connector_nodes

        target = await connector_nodes.resolve_target(
            db,
            {"connection_uuid": str(connection.uuid), "operation_id": operation.operation_id},
            user_id=user_id,
        )
        request = request_builder.build_request(
            operation,
            # Page one's arguments, not an empty mapping. A paged read is not testable
            # without them: Shopify's GraphQL declares `$first: Int!`, so a test that
            # omitted the page size would fail on every Shopify connection ever made, and
            # fail with a message about a variable rather than about the connection.
            # Testing a read the way it will actually be read is also the more honest
            # test.
            pagination.first_page_arguments(operation.page_rule),
            base_url=target.base_url,
        )
        read = await sender.send(request, operation, target.context)
    except IntegrationFailure as exc:
        # **Everything that is not a 2xx arrives here**, because ``send`` raises for a
        # bad status rather than returning one — which is right for a node, where a 401
        # has to take the drawn error path. The status is carried on the exception, and
        # pulling it off is what lets this page put a Reconnect button next to the
        # message instead of a generic "it did not work".
        return _test_result(
            False, str(exc), operation, status_code=getattr(exc, "status_code", None)
        )
    except Exception as exc:  # noqa: BLE001
        # Anything the runtime did not classify — a DNS failure, a TLS mismatch. Logged
        # with the traceback and reported as one sentence, because the browser gets the
        # sentence and the log gets the detail.
        logger.warning(
            "Integration connection test failed for %s: %s", connection.uuid, exc,
            exc_info=True,
        )
        return _test_result(False, _readable(exc), operation)

    records = paths.read_records(read.payload, operation.records_path)

    return _test_result(
        True,
        f"Connected. '{operation.label or operation.operation_id}' returned "
        f"{len(records)} record{'' if len(records) == 1 else 's'}.",
        operation,
        status_code=read.status_code,
        record_count=len(records),
    )


async def _operation_to_test(
    db: AsyncSession,
    connection: IntegrationConnection,
    connector: ConnectorSpec,
    operation_id: str,
) -> OperationSpec:
    """
    Which call to make. The one named, or the connector's first read.

    A connection with nothing to call is refused with a sentence saying so rather than
    with a green tick that proves nothing — which is what a "test" that skipped the
    request when there was no operation would amount to.
    """
    if operation_id:
        _, operation = await _resolve(db, connection, operation_id)
        return operation

    if connector.operations_are_user_defined:
        rows = await list_rest_operations(db, connection.id)
        readable = [
            connector_spec.load_operation(row) for row in rows if row.kind == OPERATION_READ
        ]
    else:
        readable = [op for op in connector.operations if op.kind == OPERATION_READ]

    if not readable:
        raise HTTPException(
            status_code=400,
            detail=(
                "There is nothing to test yet — this connection has no read operation. "
                "Add one, then test it."
            ),
        )

    return readable[0]


def _test_result(
    ok: bool,
    message: str,
    operation: OperationSpec,
    *,
    status_code: Optional[int] = None,
    record_count: Optional[int] = None,
) -> Dict[str, Any]:
    """The shape the test partial renders. No response body, no headers — a vendor's error
    body can carry the token that was sent back to you."""
    return {
        "ok": ok,
        "message": message,
        "operation_id": operation.operation_id,
        "operation_label": operation.label or operation.operation_id,
        "status_code": status_code,
        "record_count": record_count,
    }


def _readable(exc: BaseException) -> str:
    """An unclassified failure as one sentence for the browser. The type name rather than
    the repr, because a repr can contain the URL and the URL can contain a key."""
    return (
        f"Could not reach this connection ({type(exc).__name__}). Check the address and "
        "try again."
    )


# ---------------------------------------------------------------------------
# Generic REST operations
# ---------------------------------------------------------------------------


async def save_operation(
    db: AsyncSession,
    user_id: int,
    connection_id: uuid_pkg.UUID,
    operation: Mapping[str, Any],
) -> IntegrationRestOperation:
    """
    Create or replace one user-authored operation.

    Takes the operation as **one mapping** rather than as twenty keyword arguments,
    because that is what it is: ``integration_rest_operations``' columns are
    ``OperationSpec``'s fields, and a signature enumerating them would be a third place
    that list is written down and can fall behind the other two.

    **The row is validated by turning it into an ``OperationSpec``** before it is written —
    ``load_operation`` is the same function ``resolve_operation`` calls at run time, so an
    operation that saves is an operation that loads. Validating with a second set of rules
    written for this form is how the two drift, and the drift shows up as a workflow that
    saved fine and fails at 3am.

    Upserted on ``(connection, operation_id)``, matching the unique constraint. Editing an
    operation a published workflow already uses changes what that workflow does without a
    new version — an honest limitation of operations being rows rather than part of the
    snapshot, and the reason every run step records ``operation_hash``: a replay whose hash
    differs is detectably not the same run.
    """
    connection = await get_connection(db, user_id, connection_id)
    connector = _require_connector(connection)

    if not connector.operations_are_user_defined:
        raise HTTPException(
            status_code=400,
            detail=(
                f"'{connector.label}' has a fixed set of operations. You can only write "
                "your own on a REST API connection."
            ),
        )

    values = _operation_values(connection.id, operation)
    _refuse_unloadable(values)

    existing = await get_rest_operation(db, connection.id, values["operation_id"])

    if existing is not None:
        return await operation_crud.update(db, existing.id, values)

    return await operation_crud.create(db, values)


def _operation_values(connection_id: int, operation: Mapping[str, Any]) -> Dict[str, Any]:
    """
    One submitted operation as the row it becomes.

    Every field is read by name and defaulted, so a form that omits an optional one writes
    ``NULL`` rather than whatever the previous save left there — an update that carried
    stale halves of two different operations would be a request nobody could reproduce.

    Empty containers become ``NULL`` rather than ``{}``. The loader treats them
    identically, and one of the two is what a column with nothing in it should look like
    when somebody reads the table by hand.
    """
    return {
        "connection_id": connection_id,
        "operation_id": _validated_operation_id(operation.get("operation_id")),
        "label": _validated_label(operation.get("label")),
        "description": str(operation.get("description") or "").strip() or None,
        "kind": _validated_operation_kind(operation.get("kind")),
        "method": _validated_method(operation.get("method")),
        "path": str(operation.get("path") or "").strip(),
        "query_template": _mapping_or_none(operation.get("query_template")),
        "header_template": _mapping_or_none(operation.get("header_template")),
        "body_template": _mapping_or_none(operation.get("body_template")),
        "inputs": _fields_or_none(operation.get("inputs")),
        "outputs": _fields_or_none(operation.get("outputs")),
        "records_path": str(operation.get("records_path") or "").strip() or None,
        "page_rule": _mapping_or_none(operation.get("page_rule")),
        "idempotent": bool(operation.get("idempotent")),
        "idempotency_header": str(operation.get("idempotency_header") or "").strip() or None,
        "ordered": bool(operation.get("ordered")),
        "timeout_seconds": _timeout_or_none(operation.get("timeout_seconds")),
    }


def _mapping_or_none(value: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(value, Mapping) or not value:
        return None
    return dict(value)


def _fields_or_none(value: Any) -> Optional[List[Dict[str, Any]]]:
    """
    A list of field descriptors, or nothing.

    **A malformed entry is refused, not skipped.** This used to filter non-mappings out,
    which meant a form with one bad row saved green and lost that field — and the first
    anybody heard of it was a mapping panel that would not offer a field they had just
    declared. ``OperationSaveRequest`` refuses the same input one layer up with the entry
    number in it; this is the guarantee for the callers that never go through a form.
    """
    if not isinstance(value, (list, tuple)) or not value:
        return None

    fields: List[Dict[str, Any]] = []
    for index, entry in enumerate(value, start=1):
        if not isinstance(entry, Mapping):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Field {index} of this operation is not in the expected format. "
                    "Each field needs a name and a type."
                ),
            )
        fields.append(dict(entry))

    return fields or None


def _timeout_or_none(value: Any) -> Optional[int]:
    """
    How long to wait for this operation, if it says.

    Bounded because it is the only per-operation number that can make a node outlive its
    own timeout — ``run_node`` caps a node at an hour, and an operation asking for longer
    would be silently overruled there rather than refused here.
    """
    if value in (None, ""):
        return None

    try:
        seconds = int(value)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400,
            detail=f"'{value}' is not a timeout. Give a whole number of seconds.",
        )

    if not 1 <= seconds <= 3600:
        raise HTTPException(
            status_code=400,
            detail="A timeout has to be between 1 second and an hour.",
        )

    return seconds


async def delete_operation(
    db: AsyncSession,
    user_id: int,
    connection_id: uuid_pkg.UUID,
    operation_id: str,
) -> None:
    """Remove one user-authored operation. Steps naming it fail with the registry's own
    sentence, which lists what the connection does offer."""
    connection = await get_connection(db, user_id, connection_id)

    row = await get_rest_operation(db, connection.id, str(operation_id or "").strip())
    if row is None:
        raise HTTPException(status_code=404, detail="That operation does not exist.")

    await operation_crud.delete(db, row.id)


def _refuse_unloadable(values: Mapping[str, Any]) -> None:
    """
    Refuse an operation the run-time loader would reject, before it is stored.

    A row that cannot be turned into an ``OperationSpec`` is a row that saves green and
    fails inside a node — with a message about a dataclass rather than about the form
    field somebody filled in wrong.
    """
    try:
        connector_spec.load_operation(dict(values))
    except (ValueError, TypeError, IntegrationFailure) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def _require_connector_id(connector_id: str) -> ConnectorSpec:
    """The spec for a connector the caller named."""
    try:
        return registry.require(str(connector_id or "").strip())
    except IntegrationFailure as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _require_connector(connection: IntegrationConnection) -> ConnectorSpec:
    """The spec for a connection that already exists. A stored ``connector_id`` with no
    spec means the connector was removed from the build underneath a live connection —
    worth the registry's own sentence naming it, rather than an ``AttributeError`` from
    whichever line touched the spec first."""
    return _require_connector_id(connection.connector_id)


async def _resolve(
    db: AsyncSession, connection: IntegrationConnection, operation_id: str
):
    """``resolve_operation``'s pair, with its refusal turned into a 400. The registry's
    sentence already lists what the connection does offer, so it is passed through
    unchanged."""
    try:
        return await registry.resolve_operation(db, connection, operation_id)
    except IntegrationFailure as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def _require_unused_account(
    db: AsyncSession,
    user_id: int,
    connector_id: str,
    external_account_id: Optional[str],
) -> None:
    """
    Refuse a second connection to the same external account, before the index does.

    Only when there *is* an account — a generic REST connection has none, Postgres treats
    NULLs as distinct in a unique constraint, and several such connections coexisting under
    one connector is correct rather than a loophole.
    """
    account = (external_account_id or "").strip()
    if not account:
        return

    existing = await connection_crud.get_one(
        db,
        filters={
            "user_id": user_id,
            "connector_id": connector_id,
            "external_account_id": account,
        },
    )
    if existing is not None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"You are already connected to {account} as '{existing.label}'. Edit that "
                "connection instead of adding a second one."
            ),
        )


def _credential_fields(
    connector: ConnectorSpec,
    *,
    api_key: Optional[str],
    username: Optional[str],
    password: Optional[str],
) -> Dict[str, Any]:
    """
    The secrets to store for this connector's auth kind, or nothing.

    Named by auth kind rather than by "whatever the caller sent", so a form that posts an
    API key at a Basic-auth connection stores nothing rather than storing a key that will
    never be read. Empty means "nothing was typed" — see :func:`update_connection` on why
    that is not a deletion.

    OAuth is deliberately absent: those credentials arrive from a callback, not a form, and
    that path is Phase 2's.
    """
    kind = connector.auth.kind

    if kind == AUTH_API_KEY and (api_key or "").strip():
        return {"api_key": api_key.strip()}

    if kind == AUTH_BASIC and (password or "").strip():
        return {"username": (username or "").strip(), "password": password}

    if kind == AUTH_NONE:
        return {}

    return {}


def _validated_label(label: Optional[str]) -> str:
    cleaned = (label or "").strip()

    if not cleaned:
        raise HTTPException(
            status_code=400,
            detail="Give this a name you will recognise later, like 'Shopify EU'.",
        )

    if len(cleaned) > 255:
        raise HTTPException(
            status_code=400, detail="That name is longer than 255 characters."
        )

    return cleaned


def _validated_base_url(connector: ConnectorSpec, base_url: Optional[str]) -> Optional[str]:
    """
    The root every operation's path is joined onto.

    **Refused outright for a connector that computes its own.** A Shopify base URL is
    derived from the shop domain, and letting one be typed is a way to point a trusted
    connector — and the credential issued for it — at an untrusted host.

    The shape is checked here; whether the address is one this deployment may reach is
    checked at send time by the egress guard, against the resolved IP. Doing it here as
    well would be a check whose answer expires.
    """
    cleaned = (base_url or "").strip().rstrip("/")

    if not connector.base_url_is_user_supplied:
        return None

    if not cleaned:
        raise HTTPException(
            status_code=400,
            detail="This connection needs the address of the API it talks to.",
        )

    try:
        outbound_http.validate_outbound_url_shape(
            cleaned,
            policy=outbound_http.EgressPolicy(require_https=connector.requires_https),
            label="The API address",
        )
    except outbound_http.EgressError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return cleaned


def _validated_account_id(
    connector: ConnectorSpec, external_account_id: Optional[str]
) -> str:
    """
    The account this connection belongs to — a Shopify shop domain, a GoHighLevel
    location.

    Checked against the connector's own pattern rather than against a rule written here,
    so the connector that knows what its accounts look like is the one that says so.

    **This is the earlier of two checks, not the only one.** The connector re-checks the
    same value in ``render_base_url``, immediately before it becomes a hostname. The
    duplication is deliberate: this one exists so the person typing gets a sentence rather
    than a failed run, and the later one exists so the *request* is safe even if some
    future code path writes the column without coming through here. Only the second is
    load-bearing for security; only the first is any use to a human.
    """
    try:
        return connector.validated_account_id(external_account_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _validated_operation_id(operation_id: str) -> str:
    """
    The name a workflow step refers to this operation by.

    Restricted to identifier characters because it goes into a step's ``operation_id``,
    into log lines and into the AI catalogue. Somewhere in that chain a space or a slash
    stops being decorative.
    """
    cleaned = str(operation_id or "").strip()

    if not cleaned:
        raise HTTPException(status_code=400, detail="This operation needs an id.")

    if len(cleaned) > 64:
        raise HTTPException(
            status_code=400, detail="An operation id cannot be longer than 64 characters."
        )

    if not all(char.isalnum() or char in "_-." for char in cleaned):
        raise HTTPException(
            status_code=400,
            detail=(
                f"'{cleaned}' is not a usable id. Use letters, numbers, dots, dashes and "
                "underscores — for example 'list_customers'."
            ),
        )

    return cleaned


def _validated_operation_kind(kind: str) -> str:
    cleaned = str(kind or "").strip()
    if cleaned not in OPERATION_KINDS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"'{kind}' is not a kind of operation. Choose one of: "
                f"{', '.join(sorted(OPERATION_KINDS))}."
            ),
        )
    return cleaned


def _validated_method(method: str) -> str:
    """
    The HTTP verb, upper-cased and from a closed set.

    A closed set rather than "whatever was typed" because the verb decides whether the
    retry rules treat this as a write, and a lower-case ``post`` that failed that
    comparison would be retried after a timeout — which is how a timed-out order becomes
    two orders.
    """
    cleaned = str(method or "").strip().upper()
    allowed = ("GET", "POST", "PUT", "PATCH", "DELETE")

    if cleaned not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"'{method}' is not a request method. Use one of: {', '.join(allowed)}.",
        )

    return cleaned


def _validated_allowlist(
    hosts: Optional[Sequence[str]], cidrs: Optional[Sequence[str]]
) -> Dict[str, List[str]]:
    """
    The private-host allow-list: explicit entries, bounded, no wildcards.

    A wildcard is refused rather than expanded, because ``*.internal`` allows whatever DNS
    says tomorrow and the point of the list is that somebody chose each entry.
    """
    cleaned_hosts = _bounded(hosts, "host")
    cleaned_cidrs = _bounded(cidrs, "network range")

    for entry in cleaned_hosts + cleaned_cidrs:
        if "*" in entry:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"'{entry}' has a wildcard in it. List each address explicitly — a "
                    "wildcard allows whatever DNS returns tomorrow."
                ),
            )

    return {"hosts": cleaned_hosts, "cidrs": cleaned_cidrs}


def _bounded(values: Optional[Sequence[str]], noun: str) -> List[str]:
    cleaned: List[str] = []

    for value in values or []:
        entry = str(value or "").strip().lower()
        if entry and entry not in cleaned:
            cleaned.append(entry)

    if len(cleaned) > MAX_ALLOWLIST_ENTRIES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"A connection can list at most {MAX_ALLOWLIST_ENTRIES} {noun} entries. A "
                "longer list is one nobody reviews."
            ),
        )

    return cleaned
