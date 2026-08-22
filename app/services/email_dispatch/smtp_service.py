"""
Business logic for SMTP servers: create, edit, test, delete.

**The password is write-only from the outside.** An edit form comes back with the
credential field *empty* and a placeholder saying a password is stored, and an empty field
on save means "leave it alone" rather than "clear it". That asymmetry is deliberate:
returning the real value — even masked — puts the secret in the DOM of every page that
renders the form, where a browser extension, a screenshot or a stray HTML cache can reach
it. ``credential_service`` makes the same call for integration credentials.

**Testing does not send an email.** The Send-test button connects, authenticates and hangs
up. Operators press test buttons repeatedly, and a probe that delivers mail to whatever
address happened to be in the form is a test with a side effect on somebody's inbox.

**A config in use cannot be deleted quietly.** ``email_triggers.smtp_config_id`` is
``ON DELETE RESTRICT``, so the database would refuse anyway — but it would refuse with an
``IntegrityError``, and "2 triggers send through this server" is a sentence an operator can
act on. Asking first is what turns a stack trace into an answer.
"""

import logging
import uuid as uuid_pkg
from datetime import datetime, timezone
from typing import Any, Dict, List, NoReturn, Optional

from litestar.exceptions import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.db_utils import CRUDQueryBuilder
from app.db.email_dispatch.queries import smtp_configs_in_use
from app.models.email_dispatch import (
    SECURITY_NONE,
    SMTP_SECURITY_LABELS,
    SMTP_SECURITY_VALUES,
    EmailSmtpConfig,
)
from app.models.workspaces import Workspace
from app.services.email_dispatch import sender
from app.services.email_dispatch.errors import DispatchError, RenderError, SendError
from app.services.email_dispatch.rendering import validated_address
from app.utils.crypto import decrypt_secret, encrypt_secret
from app.utils.validators import optional_text, require_text

logger = logging.getLogger(__name__)

config_crud = CRUDQueryBuilder(EmailSmtpConfig)
workspace_crud = CRUDQueryBuilder(Workspace)

_NAME_MAX = 255
_HOST_MAX = 255
_EMAIL_MAX = 320

#: Ports an operator is likely to have meant. Not a restriction — a relay on 2525 is
#: ordinary — but anything outside 1..65535 is refused, and 25 gets a warning in the docs
#: because most hosting providers block it outbound.
_MIN_PORT = 1
_MAX_PORT = 65535

#: Bounds on the per-send timeout. The floor stops a config that can never finish a
#: handshake; the ceiling stops one message holding a worker for an hour.
_MIN_TIMEOUT = 5
_MAX_TIMEOUT = 300


# --------------------------------------------------------------------------
# Read
# --------------------------------------------------------------------------


def build_view(config: EmailSmtpConfig) -> Dict[str, Any]:
    """
    One config shaped for a template.

    Built field by field rather than from ``__dict__``, and that is the safeguard: adding a
    secret column to the model cannot leak it through here, because a new column is absent
    from this dict until somebody adds it deliberately. ``build_connection_views`` in the
    integrations module takes the same approach for the same reason.

    ``has_password`` rather than the password, masked or otherwise. See the module
    docstring.
    """
    return {
        "uuid": str(config.uuid),
        "name": config.name,
        "host": config.host,
        "port": config.port,
        "security": config.security,
        "security_label": SMTP_SECURITY_LABELS.get(config.security, config.security),
        "username": config.username or "",
        "has_password": bool(config.password_encrypted),
        "from_email": config.from_email,
        "from_name": config.from_name or "",
        "reply_to": config.reply_to or "",
        "timeout_seconds": config.timeout_seconds,
        "is_active": config.is_active,
        "last_tested_at": config.last_tested_at,
        "last_test_ok": config.last_test_ok,
        "last_test_message": config.last_test_message or "",
        "created_at": config.created_at,
    }


async def list_views(db: AsyncSession, user_id: int) -> List[Dict[str, Any]]:
    """Every SMTP config this user owns, for the list page."""
    configs = await config_crud.get_many(
        db, filters={"user_id": user_id}, order_by="name"
    )
    return [build_view(config) for config in configs]


async def get_config(
    db: AsyncSession, user_id: int, config_id: uuid_pkg.UUID
) -> EmailSmtpConfig:
    """
    Resolve a config by its public uuid, scoped to its owner.

    The 404 for a config that exists but belongs to someone else is deliberate — a 403
    there would confirm the uuid is real.
    """
    config = await config_crud.get_by_uuid(
        db, config_id, extra_filters={"user_id": user_id}
    )
    if not config:
        raise HTTPException(status_code=404, detail="SMTP server not found")
    return config


async def choices(db: AsyncSession, user_id: int) -> List[Dict[str, Any]]:
    """
    The user's servers as ``{uuid, label, detail, disabled_reason}`` for a node's dropdown.

    Inactive and untested servers are **offered and flagged, not hidden** — the house rule
    ``graph_service.node_options`` states. A node already pointing at a switched-off server
    has to remain editable, and an operator looking for a server they know exists needs to
    see why it is not usable rather than wonder where it went.
    """
    configs = await config_crud.get_many(
        db, filters={"user_id": user_id}, order_by="name"
    )
    offered: List[Dict[str, Any]] = []
    for config in configs:
        reason = ""
        if not config.is_active:
            reason = "Switched off"
        elif config.last_test_ok is False:
            reason = "Last test failed"
        offered.append(
            {
                "uuid": str(config.uuid),
                "label": config.name,
                "detail": f"{config.host}:{config.port} as {config.from_email}",
                "disabled_reason": reason,
            }
        )
    return offered


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def _validated_port(raw: Any) -> int:
    try:
        port = int(raw)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400, detail="The port must be a whole number, such as 587."
        ) from None
    if not _MIN_PORT <= port <= _MAX_PORT:
        raise HTTPException(
            status_code=400,
            detail=f"The port must be between {_MIN_PORT} and {_MAX_PORT}.",
        )
    return port


def _validated_timeout(raw: Any) -> int:
    if raw in (None, ""):
        return 30
    try:
        timeout = int(raw)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400, detail="The timeout must be a number of seconds."
        ) from None
    if not _MIN_TIMEOUT <= timeout <= _MAX_TIMEOUT:
        raise HTTPException(
            status_code=400,
            detail=(
                f"The timeout must be between {_MIN_TIMEOUT} and {_MAX_TIMEOUT} seconds."
            ),
        )
    return timeout


def _validated_security(raw: Any) -> str:
    security = str(raw or "").strip().lower()
    if security not in SMTP_SECURITY_VALUES:
        offered = ", ".join(sorted(SMTP_SECURITY_VALUES))
        raise HTTPException(
            status_code=400,
            detail=f"'{security}' is not a connection type. Choose one of: {offered}.",
        )
    return security


def _validated_email(raw: Any, label: str, *, required: bool) -> Optional[str]:
    """
    An address, checked by the same rule the renderer uses.

    Imported from ``rendering`` rather than re-implemented so a config cannot hold a
    ``from_email`` the sender would later refuse — one definition of "an address", checked
    at the point it is stored.
    """
    value = str(raw or "").strip()
    if not value:
        if required:
            raise HTTPException(status_code=400, detail=f"{label} is required.")
        return None
    try:
        return validated_address(value)
    except RenderError as exc:
        raise HTTPException(status_code=400, detail=f"{label}: {exc.message}") from exc


async def _resolved_workspace_id(
    db: AsyncSession, user_id: int, workspace_uuid: Any
) -> Optional[int]:
    """
    The internal id for a workspace uuid, or ``None`` for "shared with nobody".

    Scoped to the owner, so a config cannot be shared into a workspace the user does not
    have. The bigint never leaves this module — it goes straight onto the row.
    """
    raw = str(workspace_uuid or "").strip()
    if not raw:
        return None
    try:
        parsed = uuid_pkg.UUID(raw)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="That workspace is not valid.") from None

    workspace = await workspace_crud.get_by_uuid(
        db, parsed, extra_filters={"user_id": user_id}
    )
    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return workspace.id


def _fail_on_duplicate_name(name: str, exc: IntegrityError) -> NoReturn:
    raise HTTPException(
        status_code=400,
        detail=f"You already have an SMTP server named '{name}'.",
    ) from exc


# --------------------------------------------------------------------------
# Write
# --------------------------------------------------------------------------


async def create_config(
    db: AsyncSession,
    user_id: int,
    *,
    name: str,
    host: str,
    port: Any,
    security: str,
    from_email: str,
    username: str = "",
    password: str = "",
    from_name: str = "",
    reply_to: str = "",
    timeout_seconds: Any = 30,
    workspace_uuid: Any = None,
) -> EmailSmtpConfig:
    """
    Store a new SMTP server.

    Refuses plaintext-with-credentials here as well as at send time. Catching it at save is
    what stops an operator configuring something that looks fine and only discovers at
    three in the morning that every send is refused — and the check has to exist in both
    places, because a config can be edited into that state and because the sender must never
    trust that it was not.
    """
    clean_name = require_text(name, "Server name", _NAME_MAX)
    clean_host = require_text(host, "Host", _HOST_MAX).lower()
    clean_security = _validated_security(security)
    clean_port = _validated_port(port)
    clean_from = _validated_email(from_email, "The From address", required=True)
    clean_reply_to = _validated_email(reply_to, "The Reply-To address", required=False)
    clean_username = optional_text(username, "Username", _NAME_MAX)

    if clean_security == SECURITY_NONE and password:
        raise HTTPException(
            status_code=400,
            detail=(
                "A password cannot be sent over an unencrypted connection — it would "
                "travel in readable form. Choose STARTTLS or SSL/TLS, or leave the "
                "credentials blank if the relay authenticates by address."
            ),
        )

    workspace_id = await _resolved_workspace_id(db, user_id, workspace_uuid)

    try:
        return await config_crud.create(
            db,
            {
                "user_id": user_id,
                "workspace_id": workspace_id,
                "name": clean_name,
                "host": clean_host,
                "port": clean_port,
                "security": clean_security,
                "username": clean_username,
                "password_encrypted": encrypt_secret(password) if password else None,
                "from_email": clean_from,
                "from_name": optional_text(from_name, "From name", _NAME_MAX),
                "reply_to": clean_reply_to,
                "timeout_seconds": _validated_timeout(timeout_seconds),
            },
        )
    except IntegrityError as exc:
        # Rollback before raising: the HTMX route re-renders the list in this same session,
        # and a failed transaction would make that render fail instead of showing the error.
        await db.rollback()
        _fail_on_duplicate_name(clean_name, exc)


async def update_config(
    db: AsyncSession,
    user_id: int,
    config_id: uuid_pkg.UUID,
    *,
    name: str,
    host: str,
    port: Any,
    security: str,
    from_email: str,
    username: str = "",
    password: str = "",
    from_name: str = "",
    reply_to: str = "",
    timeout_seconds: Any = 30,
    workspace_uuid: Any = None,
    clear_password: bool = False,
) -> EmailSmtpConfig:
    """
    Edit a server.

    **An empty password field leaves the stored one alone.** It is the only way an edit form
    can work without putting the secret in the page — so "no new password" and "remove the
    password" cannot both be the empty string, and ``clear_password`` is the explicit
    second signal for the latter. Without that separation, every save of an unrelated field
    would silently wipe the credentials.
    """
    config = await get_config(db, user_id, config_id)

    clean_name = require_text(name, "Server name", _NAME_MAX)
    clean_security = _validated_security(security)

    # Whether this save will end with a password stored, given the three-way choice between
    # keeping, replacing and clearing. Needed before the plaintext check, which is about
    # the *outcome* rather than about what was typed.
    keeps_password = bool(
        password or (config.password_encrypted and not clear_password)
    )
    if clean_security == SECURITY_NONE and keeps_password:
        raise HTTPException(
            status_code=400,
            detail=(
                "This server has a password stored, which cannot be sent over an "
                "unencrypted connection. Choose STARTTLS or SSL/TLS, or tick 'Remove "
                "the stored password' as well."
            ),
        )

    values: Dict[str, Any] = {
        "name": clean_name,
        "host": require_text(host, "Host", _HOST_MAX).lower(),
        "port": _validated_port(port),
        "security": clean_security,
        "username": optional_text(username, "Username", _NAME_MAX),
        "from_email": _validated_email(from_email, "The From address", required=True),
        "from_name": optional_text(from_name, "From name", _NAME_MAX),
        "reply_to": _validated_email(reply_to, "The Reply-To address", required=False),
        "timeout_seconds": _validated_timeout(timeout_seconds),
        "workspace_id": await _resolved_workspace_id(db, user_id, workspace_uuid),
    }

    if password:
        values["password_encrypted"] = encrypt_secret(password)
    elif clear_password:
        values["password_encrypted"] = None

    try:
        updated = await config_crud.update(db, config.id, values)
    except IntegrityError as exc:
        await db.rollback()
        _fail_on_duplicate_name(clean_name, exc)

    if updated is None:
        raise HTTPException(status_code=404, detail="SMTP server not found")
    return updated


async def set_active(
    db: AsyncSession, user_id: int, config_id: uuid_pkg.UUID, is_active: bool
) -> EmailSmtpConfig:
    """
    Switch a server on or off.

    Switching off does not touch messages already queued for it; they fail at send time
    with a sentence naming the server. That is deliberate — cancelling somebody's queued
    mail as a side effect of a toggle would be a surprising amount of action for one
    checkbox.
    """
    config = await get_config(db, user_id, config_id)
    updated = await config_crud.update(db, config.id, {"is_active": bool(is_active)})
    if updated is None:
        raise HTTPException(status_code=404, detail="SMTP server not found")
    return updated


async def delete_config(
    db: AsyncSession, user_id: int, config_id: uuid_pkg.UUID
) -> None:
    """Delete a server, unless a trigger still sends through it. See the module docstring."""
    config = await get_config(db, user_id, config_id)

    in_use = await smtp_configs_in_use(db, [config.id])
    count = in_use.get(config.id, 0)
    if count:
        raise HTTPException(
            status_code=400,
            detail=(
                f"'{config.name}' cannot be deleted because {count} "
                f"trigger{'s' if count != 1 else ''} send{'' if count != 1 else 's'} "
                "through it. Delete or re-point those triggers first."
            ),
        )

    await config_crud.delete(db, config.id)


# --------------------------------------------------------------------------
# Testing
# --------------------------------------------------------------------------


async def test_config(
    db: AsyncSession, user_id: int, config_id: uuid_pkg.UUID
) -> Dict[str, Any]:
    """
    Connect, authenticate, hang up — and record the outcome on the row.

    Returns ``{"ok": bool, "message": str}`` rather than raising for a failed test, because
    a server refusing the credentials is a *result* of the test and not an error in
    performing it: the operator asked a question and got an answer. Only "that config is
    not yours" is an exception here.

    The result is written to ``last_tested_at`` / ``last_test_ok`` / ``last_test_message``
    because "the email never arrived" gets reported to whoever runs the platform rather
    than to whoever configured this, and the first question is always whether it ever
    worked.
    """
    config = await get_config(db, user_id, config_id)

    target = sender.SmtpTarget(
        host=config.host,
        port=int(config.port),
        security=config.security,
        username=config.username,
        password=(
            decrypt_secret(config.password_encrypted)
            if config.password_encrypted
            else None
        ),
        timeout_seconds=int(config.timeout_seconds or 30),
    )

    try:
        message = await sender.verify_target(target)
        ok = True
    except (SendError, DispatchError) as exc:
        message = exc.message
        ok = False
    except Exception:  # noqa: BLE001
        # An unexpected fault in the test path must not put a traceback on the page, and
        # must not be reported as "the server refused" either — we do not know that.
        logger.exception("SMTP test failed unexpectedly for config %s", config.uuid)
        message = (
            "Something went wrong while testing this server. Please try again, and "
            "contact support if the problem continues."
        )
        ok = False

    await config_crud.update(
        db,
        config.id,
        {
            "last_tested_at": datetime.now(timezone.utc),
            "last_test_ok": ok,
            "last_test_message": message[:2000],
        },
    )

    return {"ok": ok, "message": message}
