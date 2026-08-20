"""
Storing a connection's secrets, and handing the sender exactly one header.

**A secret never travels as a value.** :func:`auth_for` returns the finished
``(name, value)`` pair the socket needs and nothing else; no caller ever holds a token,
and :class:`~app.services.integrations.runtime.sender.SendContext` has no field that
could keep one. That is why nothing the engine logs, previews or hashes can contain a
credential — it is a property of the shapes rather than of whoever remembers to strip
them.

**The view a route builds selects from the connection alone.** ``integration_credentials``
is a separate table behind a unique foreign key precisely so that
:func:`build_connection_views` *cannot* serialise a secret by accident — there is nothing
on the row it reads. The alternative, six more columns on the connection, makes every
response schema one forgotten exclusion away from leaking.

**Revoking is one ``DELETE``.** Not "null the columns", which leaves whatever a previous
migration or a partial write put there, and not a soft delete. One statement that
provably leaves nothing.

Everything ``*_encrypted`` goes through ``app/utils/crypto.py`` — see
``documentations/SECRETS_AND_KEY_ROTATION.md`` for what the key is and how it changes.
"""

import logging
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.integrations import (
    AUTH_API_KEY,
    AUTH_BASIC,
    AUTH_NONE,
    AUTH_OAUTH2,
    CONNECTION_ACTIVE,
    CONNECTION_NEEDS_REAUTH,
    CREDENTIAL_CONNECTED,
    CREDENTIAL_REVOKED,
    IntegrationConnection,
    IntegrationCredential,
    IntegrationCredentialEvent,
)
from app.services.integrations.connectors.spec import (
    PLACEMENT_HEADER,
    PLACEMENT_QUERY,
    ConnectorSpec,
)
from app.services.integrations.errors import IntegrationFailure
from app.utils.crypto import decrypt_secret, encrypt_secret

logger = logging.getLogger(__name__)


#: How much of a secret a masked form shows. Four is enough for somebody to recognise
#: which key they pasted and not enough to be worth anything — the same number
#: ``ai_settings_service._mask_key`` settled on, and mirrored rather than imported so
#: this layer does not depend on that feature.
VISIBLE_SUFFIX = 4

#: Every column on ``integration_credentials`` that holds ciphertext. Named once so a
#: new secret cannot be added and forgotten by the encrypt-on-write path.
SECRET_FIELDS: Tuple[str, ...] = (
    "api_key",
    "access_token",
    "refresh_token",
    "client_secret",
    "password",
    "client_key",
)

#: The plaintext columns a caller may set. Listed so an unexpected key is a refusal
#: rather than a silently dropped field — a caller passing ``clientId`` and getting no
#: error would produce a connection that cannot authenticate for no visible reason.
PLAINTEXT_FIELDS: Tuple[str, ...] = (
    "client_id",
    "username",
    "client_cert_pem",
    "token_type",
    "scope",
    "expires_at",
)


def mask_secret(value: Optional[str]) -> str:
    """
    A secret as it may be shown back to its owner.

    Shown at all because the owner typed it and needs to recognise which one it is; the
    encryption protects the database, not the user from themselves. Anything short
    enough that four characters would be most of it is masked entirely.
    """
    text = str(value or "")
    if len(text) <= VISIBLE_SUFFIX:
        return "*" * len(text)
    return "••••••••" + text[-VISIBLE_SUFFIX:]


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


async def store_credential(
    db: AsyncSession,
    connection: IntegrationConnection,
    *,
    user_id: Optional[int] = None,
    **fields: Any,
) -> IntegrationCredential:
    """
    Create or replace the credential row for one connection.

    Secrets are named without the ``_encrypted`` suffix — ``api_key="sk-1"`` — and this
    function does the encrypting. The alternative, callers encrypting and passing
    ``api_key_encrypted``, means every call site is one omission away from writing a
    plaintext secret into a column named for ciphertext.

    An unknown field raises rather than being ignored. See :data:`PLAINTEXT_FIELDS`.
    """
    unknown = set(fields) - set(SECRET_FIELDS) - set(PLAINTEXT_FIELDS)
    if unknown:
        raise IntegrationFailure(
            f"Cannot store {', '.join(sorted(unknown))} on a connection's credentials — "
            "there is nowhere for it to go."
        )

    values: Dict[str, Any] = {}

    for name in SECRET_FIELDS:
        if name not in fields:
            continue
        raw = fields[name]
        values[f"{name}_encrypted"] = encrypt_secret(str(raw)) if raw else None

    for name in PLAINTEXT_FIELDS:
        if name in fields:
            values[name] = fields[name]

    credential = await _credential_for(db, connection.id)

    if credential is None:
        credential = IntegrationCredential(connection_id=connection.id, **values)
        db.add(credential)
    else:
        for name, value in values.items():
            setattr(credential, name, value)

    # A connection that had failed to authenticate is working again the moment somebody
    # supplies a new credential. Leaving it at `needs_reauth` would keep the red badge
    # up until the next run happened to succeed.
    if connection.status == CONNECTION_NEEDS_REAUTH:
        connection.status = CONNECTION_ACTIVE

    await db.commit()
    await db.refresh(credential)

    await record_event(
        db, connection, CREDENTIAL_CONNECTED, user_id=user_id,
        detail={"fields": sorted(name for name in fields if name in SECRET_FIELDS)},
    )

    return credential


async def revoke(
    db: AsyncSession,
    connection: IntegrationConnection,
    *,
    user_id: Optional[int] = None,
    reason: str = "",
) -> None:
    """
    Delete every secret for this connection and mark it revoked.

    The event is written **before** the delete, in the same transaction. Written after,
    a failure between the two would leave a connection with no credential and no record
    of why — which is the state somebody investigating an outage least wants to find.
    """
    await record_event(
        db, connection, CREDENTIAL_REVOKED, user_id=user_id,
        detail={"reason": reason} if reason else None, commit=False,
    )

    await db.execute(
        delete(IntegrationCredential).where(
            IntegrationCredential.connection_id == connection.id
        )
    )
    connection.status = "revoked"
    await db.commit()


async def record_event(
    db: AsyncSession,
    connection: IntegrationConnection,
    event: str,
    *,
    user_id: Optional[int] = None,
    detail: Optional[dict] = None,
    commit: bool = True,
) -> None:
    """
    One row on the credential audit trail.

    ``detail`` never holds a secret. Not "holds a masked secret" — holds none. A masking
    function is one refactor away from being bypassed, and this is the one table whose
    whole purpose is to be readable by somebody investigating an incident.

    Swallows its own failures. A run must not fail because its audit row could not be
    written, and a lost row is visible as a gap where the neighbouring rows are not.
    """
    try:
        db.add(
            IntegrationCredentialEvent(
                connection_id=connection.id,
                user_id=user_id,
                event=event,
                detail=_without_secrets(detail),
            )
        )
        if commit:
            await db.commit()
    except Exception:  # noqa: BLE001 — see the docstring
        logger.warning(
            "Could not record the '%s' credential event for connection %s",
            event, connection.uuid, exc_info=True,
        )


def _without_secrets(detail: Optional[dict]) -> Optional[dict]:
    """
    The belt to the docstring's braces.

    Callers are told not to put a secret in ``detail`` and this makes it true anyway,
    because the cost is one function call and the failure mode is a credential in a
    table built for reading.
    """
    if not detail:
        return None

    from app.services.integrations.engine.flow_state import redact

    return redact(detail)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


async def auth_for(
    db: AsyncSession,
    connection: IntegrationConnection,
    connector: ConnectorSpec,
) -> Tuple[Optional[Tuple[str, str]], Optional[Tuple[str, str]]]:
    """
    ``(header, query)`` for :class:`SendContext` — the finished pair, not the secret.

    Exactly one of the two is set, or neither for a connector that needs no credential.
    Returning the built pair rather than the token is what keeps every caller from
    holding one; see the module docstring.
    """
    kind = connector.auth.kind

    if kind == AUTH_NONE:
        return (None, None)

    credential = await _credential_for(db, connection.id)
    if credential is None:
        raise IntegrationFailure(
            f"'{connection.label}' has no saved credentials. Open it and supply them "
            "before running a workflow that uses it."
        )

    value = _auth_value(connection, connector, credential)
    name = connector.auth.name

    if connector.auth.placement == PLACEMENT_QUERY:
        return (None, (name, value))

    return ((name, value), None)


def _auth_value(
    connection: IntegrationConnection,
    connector: ConnectorSpec,
    credential: IntegrationCredential,
) -> str:
    """
    The credential rendered into the connector's ``value_template``.

    Substitution is by exact name — ``{api_key}``, ``{token}``, ``{basic}`` — and never
    by ``str.format``, for the reason ``request_builder._substitute`` gives at more
    length: ``format`` treats ``{0}`` and ``{a.b}`` as instructions, and a template is
    the last place to allow that.
    """
    kind = connector.auth.kind
    template = connector.auth.value_template

    if kind == AUTH_API_KEY:
        return _rendered(template, "api_key", _decrypted(credential, "api_key", connection))

    if kind == AUTH_OAUTH2:
        return _rendered(
            template, "token", _decrypted(credential, "access_token", connection)
        )

    if kind == AUTH_BASIC:
        import base64

        username = credential.username or ""
        password = _decrypted(credential, "password", connection)
        encoded = base64.b64encode(f"{username}:{password}".encode()).decode()
        return _rendered(template, "basic", encoded)

    raise IntegrationFailure(
        f"'{connection.label}' uses an authentication method this version cannot send "
        f"({kind})."
    )


def _rendered(template: str, name: str, value: str) -> str:
    placeholder = "{" + name + "}"
    if placeholder not in template:
        # A connector whose template does not name the credential it needs would send a
        # request with no credential in it, and get a 401 that reads like a bad key.
        raise IntegrationFailure(
            f"This connector's credential format does not include {placeholder}, so "
            "there is nowhere to put the credential."
        )
    return template.replace(placeholder, value)


def _decrypted(
    credential: IntegrationCredential,
    field: str,
    connection: IntegrationConnection,
) -> str:
    """
    One secret, or a sentence saying which connection to fix.

    A key that cannot be read is almost always ``FERNET_KEY`` having changed without
    ``FERNET_KEY_OLD``, and the message says re-enter rather than naming the environment
    variable — the person seeing it is the connection's owner, not the operator.
    """
    token = getattr(credential, f"{field}_encrypted", None)

    if not token:
        raise IntegrationFailure(
            f"'{connection.label}' has no saved {field.replace('_', ' ')}. Open the "
            "connection and supply it."
        )

    try:
        return decrypt_secret(token)
    except Exception as exc:  # noqa: BLE001 — InvalidToken and anything cryptography raises
        logger.error(
            "Could not decrypt %s for connection %s", field, connection.uuid, exc_info=True
        )
        raise IntegrationFailure(
            f"The saved credentials for '{connection.label}' could not be read. Open "
            "the connection and enter them again."
        ) from exc


async def reveal_for_owner(
    db: AsyncSession, connection: IntegrationConnection
) -> Dict[str, str]:
    """
    The masked form of every stored secret, for the connection's own edit page.

    Masked rather than plain, unlike ``chatbot_action_service``'s headers, and the
    difference is what they are for: a header list is edited as a whole and has to come
    back to be edited, whereas a connection's key is either kept or replaced. There is
    no workflow here that needs the plaintext on screen, so it does not go there.
    """
    credential = await _credential_for(db, connection.id)
    if credential is None:
        return {}

    masked: Dict[str, str] = {}
    for field in SECRET_FIELDS:
        token = getattr(credential, f"{field}_encrypted", None)
        if not token:
            continue
        try:
            masked[field] = mask_secret(decrypt_secret(token))
        except Exception:  # noqa: BLE001
            # A row written under a key we no longer have. Saying so beats an empty
            # field, which reads as "there is nothing saved" and invites a duplicate.
            masked[field] = "(cannot be read)"

    return masked


async def _credential_for(
    db: AsyncSession, connection_id: int
) -> Optional[IntegrationCredential]:
    result = await db.execute(
        select(IntegrationCredential).where(
            IntegrationCredential.connection_id == connection_id
        )
    )
    return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------


def build_connection_views(
    connections: List[IntegrationConnection],
    *,
    connector_labels: Optional[Dict[str, str]] = None,
) -> List[dict]:
    """
    Connections shaped for a template or a JSON response.

    **Reads the connection row only.** There is no credential in scope here, which is
    the structural reason this cannot leak one — see the module docstring. Adding a
    secret to this payload would require joining a table that is deliberately not
    joined.

    Public ``uuid`` only, never the bigint ``id``.
    """
    labels = connector_labels or {}

    return [
        {
            "uuid": str(connection.uuid),
            "label": connection.label,
            "connector_id": connection.connector_id,
            "connector_label": labels.get(connection.connector_id, connection.connector_id),
            "auth_kind": connection.auth_kind,
            "base_url": connection.base_url or "",
            "external_account_id": connection.external_account_id or "",
            "status": connection.status,
            "is_active": connection.is_active,
            "needs_reauth": connection.status == CONNECTION_NEEDS_REAUTH,
            "allow_private_hosts": connection.allow_private_hosts,
            "created_at": connection.created_at,
        }
        for connection in connections
    ]
