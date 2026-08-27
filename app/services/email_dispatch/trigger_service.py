"""
Business logic for triggers: the standing instructions that send mail without a canvas.

A trigger is "whenever *that* happens, send this". Two kinds:

* ``event`` — subscribes to one of ``app/utils/events.EVENT_NAMES``.
* ``webhook`` — an external system POSTs to a per-trigger URL.

Both reach ``dispatch_service.enqueue_email``, so there is no second send path.

**Bindings are validated at save time against the template's declaration.** A trigger that
could not render is refused here rather than discovered at three in the morning, and the
available-sources set passed to ``assert_bindable`` is the honest one for a trigger: event
payload and literals only, because a trigger has no chat session, no upstream node and no
record in hand.

**The webhook secret is generated, not chosen.** An operator picking their own HMAC key
picks a weak one, and there is no reason to let them: ``secrets.token_urlsafe`` produces it,
it is shown exactly once, and rotating it issues a new one. Same for the endpoint id, which
is a *separate* uuid from the row's own so that leaking the URL is fixable by rotating one
column rather than rebuilding every caller.

**Disabling is the safe operation; deleting is not always available.** A trigger that has
sent mail leaves messages pointing at it (``ON DELETE SET NULL``), so deleting one is fine
and the log survives. But its template and server are ``ON DELETE RESTRICT``, which is why
``template_service`` asks this module before letting either go.
"""

import logging
import secrets
import uuid as uuid_pkg
from typing import Any, Dict, List, NoReturn, Optional

from litestar.exceptions import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.db_utils import CRUDQueryBuilder
from app.models.email_dispatch import (
    BINDING_EVENT,
    BINDING_LITERAL,
    MIN_WEBHOOK_INTERVAL_SECONDS,
    TRIGGER_EVENT,
    TRIGGER_KIND_LABELS,
    TRIGGER_KIND_VALUES,
    TRIGGER_WEBHOOK,
    EmailTrigger,
)
from app.models.workspaces import Workspace
from app.services.email_dispatch import dispatch_service, variable_sources
from app.services.email_dispatch.errors import RenderError
from app.utils.crypto import encrypt_secret
from app.utils.events import EVENT_NAME_LABELS, EVENT_NAME_VALUES
from app.utils.validators import require_text

logger = logging.getLogger(__name__)

trigger_crud = CRUDQueryBuilder(EmailTrigger)
workspace_crud = CRUDQueryBuilder(Workspace)

_NAME_MAX = 255

#: Bytes of entropy in a generated webhook secret. 32 bytes is 256 bits, which is the size
#: of the HMAC-SHA256 key it becomes — more would be hashed down, less would be the weakest
#: part of the scheme.
_SECRET_BYTES = 32

#: What a trigger can actually offer a binding. No chat session, no upstream node, no record
#: — so those sources are refused at save time rather than failing at fire time.
TRIGGER_BINDING_SOURCES = frozenset({BINDING_LITERAL, BINDING_EVENT})


# --------------------------------------------------------------------------
# Read
# --------------------------------------------------------------------------


def build_view(trigger: EmailTrigger, *, reveal_secret: str = "") -> Dict[str, Any]:
    """
    One trigger shaped for a template.

    ``webhook_secret_encrypted`` is never included. ``reveal_secret`` is the plaintext, and
    it is passed in by the *one* caller that has just generated it — creation and rotation —
    rather than read back from the row. That asymmetry is the point: the secret exists in a
    response exactly once, at the moment it is issued, and is unrecoverable afterwards.
    """
    return {
        "uuid": str(trigger.uuid),
        "name": trigger.name,
        "kind": trigger.kind,
        "kind_label": TRIGGER_KIND_LABELS.get(trigger.kind, trigger.kind),
        "event_name": trigger.event_name or "",
        "event_label": EVENT_NAME_LABELS.get(trigger.event_name or "", ""),
        "webhook_endpoint_id": str(trigger.webhook_endpoint_id or ""),
        "webhook_url": (
            f"/public/emails/webhooks/{trigger.webhook_endpoint_id}"
            if trigger.webhook_endpoint_id
            else ""
        ),
        "has_secret": bool(trigger.webhook_secret_encrypted),
        # Shown once, then gone. Empty on every ordinary read.
        "reveal_secret": reveal_secret,
        "min_interval_seconds": trigger.min_interval_seconds,
        "recipients": dict(trigger.recipients or {}),
        "variable_bindings": dict(trigger.variable_bindings or {}),
        "is_enabled": trigger.is_enabled,
        "last_fired_at": trigger.last_fired_at,
        "template_name": trigger.template.name if trigger.template else "",
        "template_uuid": str(trigger.template.uuid) if trigger.template else "",
        "smtp_name": trigger.smtp_config.name if trigger.smtp_config else "",
        "smtp_uuid": str(trigger.smtp_config.uuid) if trigger.smtp_config else "",
        "created_at": trigger.created_at,
    }


async def list_views(db: AsyncSession, user_id: int) -> List[Dict[str, Any]]:
    """Every trigger this user owns. The template and server come back eagerly loaded."""
    triggers = await trigger_crud.get_many(
        db, filters={"user_id": user_id}, order_by="name"
    )
    return [build_view(trigger) for trigger in triggers]


async def get_trigger(
    db: AsyncSession, user_id: int, trigger_id: uuid_pkg.UUID
) -> EmailTrigger:
    """Resolve a trigger by its public uuid, scoped to its owner. 404 rather than 403."""
    trigger = await trigger_crud.get_by_uuid(
        db, trigger_id, extra_filters={"user_id": user_id}
    )
    if not trigger:
        raise HTTPException(status_code=404, detail="Trigger not found")
    return trigger


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def _validated_kind(raw: Any) -> str:
    kind = str(raw or "").strip().lower()
    if kind not in TRIGGER_KIND_VALUES:
        offered = ", ".join(sorted(TRIGGER_KIND_VALUES))
        raise HTTPException(
            status_code=400,
            detail=f"'{kind}' is not a trigger type. Choose one of: {offered}.",
        )
    return kind


def _validated_event_name(kind: str, raw: Any) -> Optional[str]:
    """
    The event a trigger listens for — required for ``event``, refused for ``webhook``.

    A rule about the *combination* of two fields, which is why it is here rather than in the
    schema. ``SCHEMAS.md`` rule 3: the service half is authoritative.
    """
    name = str(raw or "").strip()
    if kind != TRIGGER_EVENT:
        return None
    if not name:
        raise HTTPException(
            status_code=400, detail="Choose which event should send this email."
        )
    if name not in EVENT_NAME_VALUES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"'{name}' is not something this application announces. Choose one of "
                "the listed events."
            ),
        )
    return name


def _validated_interval(kind: str, raw: Any) -> int:
    """
    The throttle. Only meaningful for a webhook, and floored rather than optional there.

    A public unauthenticated endpoint with no floor is a way to make this application send
    mail as fast as somebody can POST to it, which is why zero is not on offer.
    """
    if kind != TRIGGER_WEBHOOK:
        return 0
    if raw in (None, ""):
        return 60
    try:
        interval = int(raw)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400, detail="The minimum gap must be a number of seconds."
        ) from None
    if interval < MIN_WEBHOOK_INTERVAL_SECONDS:
        raise HTTPException(
            status_code=400,
            detail=(
                "A webhook trigger needs at least "
                f"{MIN_WEBHOOK_INTERVAL_SECONDS} second(s) between firings, so a caller "
                "cannot make this send mail as fast as it can post."
            ),
        )
    return interval


def _validated_recipients(raw: Any) -> Dict[str, List[str]]:
    """
    The three address lists, as authored — still containing their ``{{VARIABLE}}``s.

    Deliberately **not** rendered here: the values do not exist until the trigger fires. What
    is checked is the shape, plus that at least one TO entry was written, because a trigger
    with nobody to email is a trigger that will fail every time it fires and it is better to
    say so now.
    """
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise HTTPException(
            status_code=400, detail="The recipient lists are not filled in correctly."
        )

    cleaned: Dict[str, List[str]] = {}
    for key in ("to", "cc", "bcc"):
        entries = raw.get(key) or []
        if isinstance(entries, str):
            entries = [part.strip() for part in entries.split(",")]
        if not isinstance(entries, list):
            raise HTTPException(
                status_code=400,
                detail=f"The {key.upper()} list is not filled in correctly.",
            )
        cleaned[key] = [str(entry).strip() for entry in entries if str(entry).strip()]

    if not cleaned["to"]:
        raise HTTPException(
            status_code=400,
            detail="Add at least one TO address. It may be a {{VARIABLE}}.",
        )
    return cleaned


def _validated_bindings(raw: Any, *, declared: Any) -> Dict[str, Any]:
    """
    The bindings, checked against the template's declaration and against what a trigger can
    offer.

    The same ``assert_bindable`` every canvas node validator calls, with the trigger's own
    narrower source set — so a trigger cannot be saved bound to a chat session it will never
    have.
    """
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise HTTPException(
            status_code=400, detail="The variable bindings are not filled in correctly."
        )

    try:
        variable_sources.assert_bindable(
            raw, declared=declared, available=TRIGGER_BINDING_SOURCES
        )
        # Path shapes too: a malformed path is a typo worth catching at the keyboard rather
        # than at fire time.
        for name, binding in raw.items():
            path = str((binding or {}).get("path") or "").strip()
            if path:
                variable_sources.assert_path(path, name=str(name).upper())
    except RenderError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc

    return raw


async def _resolved_workspace_id(
    db: AsyncSession, user_id: int, workspace_uuid: Any
) -> Optional[int]:
    raw = str(workspace_uuid or "").strip()
    if not raw:
        return None
    try:
        parsed = uuid_pkg.UUID(raw)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400, detail="That workspace is not valid."
        ) from None
    workspace = await workspace_crud.get_by_uuid(
        db, parsed, extra_filters={"user_id": user_id}
    )
    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return workspace.id


def _fail_on_duplicate_name(name: str, exc: IntegrityError) -> NoReturn:
    raise HTTPException(
        status_code=400, detail=f"You already have a trigger named '{name}'."
    ) from exc


# --------------------------------------------------------------------------
# Write
# --------------------------------------------------------------------------


async def create_trigger(
    db: AsyncSession,
    user_id: int,
    *,
    name: str,
    kind: str,
    template_uuid: Any,
    smtp_uuid: Any,
    recipients: Any,
    variable_bindings: Any,
    event_name: Any = None,
    min_interval_seconds: Any = None,
    workspace_uuid: Any = None,
) -> Dict[str, Any]:
    """
    Store a new trigger.

    Returns the **view**, not the row, because a webhook trigger's generated secret is in it
    — and that is the only moment it can be. The caller renders it once and it is gone.
    """
    clean_name = require_text(name, "Trigger name", _NAME_MAX)
    clean_kind = _validated_kind(kind)

    template = await dispatch_service.resolve_template(db, user_id, template_uuid)
    config = await dispatch_service.resolve_config(db, user_id, smtp_uuid)

    values: Dict[str, Any] = {
        "user_id": user_id,
        "workspace_id": await _resolved_workspace_id(db, user_id, workspace_uuid),
        "name": clean_name,
        "kind": clean_kind,
        "event_name": _validated_event_name(clean_kind, event_name),
        "min_interval_seconds": _validated_interval(clean_kind, min_interval_seconds),
        "template_id": template.id,
        "smtp_config_id": config.id,
        "recipients": _validated_recipients(recipients),
        "variable_bindings": _validated_bindings(
            variable_bindings, declared=template.variables
        ),
        "is_enabled": True,
    }

    secret = ""
    if clean_kind == TRIGGER_WEBHOOK:
        secret = secrets.token_urlsafe(_SECRET_BYTES)
        values["webhook_endpoint_id"] = uuid_pkg.uuid4()
        values["webhook_secret_encrypted"] = encrypt_secret(secret)

    try:
        trigger = await trigger_crud.create(db, values)
    except IntegrityError as exc:
        await db.rollback()
        _fail_on_duplicate_name(clean_name, exc)

    # Re-read so the eagerly-loaded template and config relationships are populated for the
    # view; `create` refreshes the row but not its relationships.
    trigger = await get_trigger(db, user_id, trigger.uuid)
    return build_view(trigger, reveal_secret=secret)


async def update_trigger(
    db: AsyncSession,
    user_id: int,
    trigger_id: uuid_pkg.UUID,
    *,
    name: str,
    template_uuid: Any,
    smtp_uuid: Any,
    recipients: Any,
    variable_bindings: Any,
    event_name: Any = None,
    min_interval_seconds: Any = None,
    workspace_uuid: Any = None,
) -> Dict[str, Any]:
    """
    Edit a trigger.

    ``kind`` is **not** editable, and that is deliberate rather than an omission. Turning a
    webhook trigger into an event trigger would leave a live URL that external systems are
    still calling pointing at something with different semantics; turning an event trigger
    into a webhook would silently mint a secret nobody was shown. Either way the honest
    operation is a new trigger and a deleted old one, which is also what leaves the delivery
    log readable.
    """
    trigger = await get_trigger(db, user_id, trigger_id)

    clean_name = require_text(name, "Trigger name", _NAME_MAX)
    template = await dispatch_service.resolve_template(db, user_id, template_uuid)
    config = await dispatch_service.resolve_config(db, user_id, smtp_uuid)

    values: Dict[str, Any] = {
        "name": clean_name,
        "workspace_id": await _resolved_workspace_id(db, user_id, workspace_uuid),
        "event_name": _validated_event_name(trigger.kind, event_name),
        "min_interval_seconds": _validated_interval(
            trigger.kind, min_interval_seconds
        ),
        "template_id": template.id,
        "smtp_config_id": config.id,
        "recipients": _validated_recipients(recipients),
        "variable_bindings": _validated_bindings(
            variable_bindings, declared=template.variables
        ),
    }

    try:
        updated = await trigger_crud.update(db, trigger.id, values)
    except IntegrityError as exc:
        await db.rollback()
        _fail_on_duplicate_name(clean_name, exc)

    if updated is None:
        raise HTTPException(status_code=404, detail="Trigger not found")

    return build_view(await get_trigger(db, user_id, trigger_id))


async def set_enabled(
    db: AsyncSession, user_id: int, trigger_id: uuid_pkg.UUID, is_enabled: bool
) -> EmailTrigger:
    """
    Switch a trigger on or off.

    A disabled webhook endpoint answers 404, not 403 — see ``webhook_routes``. The URL stays
    valid so switching it back on does not need every caller reconfigured.
    """
    trigger = await get_trigger(db, user_id, trigger_id)
    updated = await trigger_crud.update(
        db, trigger.id, {"is_enabled": bool(is_enabled)}
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="Trigger not found")
    return updated


async def rotate_secret(
    db: AsyncSession, user_id: int, trigger_id: uuid_pkg.UUID
) -> Dict[str, Any]:
    """
    Issue a new signing secret and a new endpoint id.

    Both together, because the reason to rotate is that one of them leaked and they leak as
    a pair — a URL is usually written down next to the secret that signs it. Every existing
    caller stops working immediately, which is the intended effect of a rotation and is said
    plainly in the UI.
    """
    trigger = await get_trigger(db, user_id, trigger_id)
    if trigger.kind != TRIGGER_WEBHOOK:
        raise HTTPException(
            status_code=400,
            detail="Only a webhook trigger has a signing secret.",
        )

    secret = secrets.token_urlsafe(_SECRET_BYTES)
    await trigger_crud.update(
        db,
        trigger.id,
        {
            "webhook_endpoint_id": uuid_pkg.uuid4(),
            "webhook_secret_encrypted": encrypt_secret(secret),
        },
    )
    return build_view(await get_trigger(db, user_id, trigger_id), reveal_secret=secret)


async def delete_trigger(
    db: AsyncSession, user_id: int, trigger_id: uuid_pkg.UUID
) -> None:
    """
    Delete a trigger.

    Always allowed. Messages it sent point at it with ``ON DELETE SET NULL`` and carry their
    own copy of the template name and trigger kind, so the delivery log stays readable
    afterwards — which is the whole reason those columns are denormalised.
    """
    trigger = await get_trigger(db, user_id, trigger_id)
    await trigger_crud.delete(db, trigger.id)
