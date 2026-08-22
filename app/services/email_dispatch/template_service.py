"""
Business logic for email templates: create, edit, preview, delete.

**The declared variable list is validated against the bodies on every save**, in both
directions. A template referencing ``{{GHOST}}`` without declaring it is refused, because
a node could never bind it and the send would fail at three in the morning instead of at
the keyboard. A *declared but unused* variable is allowed and merely reported — an operator
mid-edit who has added the row before the placeholder is not making a mistake, and refusing
that would make the form fight them.

**Preview renders with the declared defaults and obvious stand-ins**, never with real data,
and never sends anything. What it is for is checking that the markup and the placeholders
look right, which does not need a live value; wiring it to real values would make Preview a
way to read data the operator may not otherwise have access to.

**A template in use cannot be deleted quietly.** ``email_triggers.template_id`` is
``ON DELETE RESTRICT``, so the database refuses anyway — but with an ``IntegrityError``
rather than a sentence. Asking first is what turns a stack trace into an answer.
"""

import logging
import uuid as uuid_pkg
from typing import Any, Dict, List, NoReturn, Optional

from litestar.exceptions import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.db_utils import CRUDQueryBuilder
from app.db.email_dispatch.queries import templates_in_use
from app.models.email_dispatch import EmailTemplate
from app.models.workspaces import Workspace
from app.services.email_dispatch import rendering
from app.services.email_dispatch.errors import RenderError
from app.utils.validators import optional_text, require_text

logger = logging.getLogger(__name__)

template_crud = CRUDQueryBuilder(EmailTemplate)
workspace_crud = CRUDQueryBuilder(Workspace)

_NAME_MAX = 255
_DESCRIPTION_MAX = 2000
_SUBJECT_MAX = 998  # RFC 5322's line-length ceiling for a header.
_BODY_MAX = 200_000


# --------------------------------------------------------------------------
# Read
# --------------------------------------------------------------------------


def build_view(template: EmailTemplate) -> Dict[str, Any]:
    """
    One template shaped for a template (the naming is unfortunate and unavoidable).

    ``variables`` is passed through as the declared list, which is what the settings UI
    rehydrates its editor rows from and what a node's property panel builds its binding
    rows from. Order is preserved, never sorted — it is the operator's grouping.
    """
    return {
        "uuid": str(template.uuid),
        "name": template.name,
        "description": template.description or "",
        "subject_template": template.subject_template,
        "body_html_template": template.body_html_template,
        "body_text_template": template.body_text_template or "",
        "variables": list(template.variables or []),
        "variable_names": rendering.declared_names(template.variables),
        "is_active": template.is_active,
        "created_at": template.created_at,
        "updated_at": template.updated_at,
    }


async def list_views(db: AsyncSession, user_id: int) -> List[Dict[str, Any]]:
    """Every template this user owns, for the list page."""
    templates = await template_crud.get_many(
        db, filters={"user_id": user_id}, order_by="name"
    )
    return [build_view(template) for template in templates]


async def get_template(
    db: AsyncSession, user_id: int, template_id: uuid_pkg.UUID
) -> EmailTemplate:
    """
    Resolve a template by its public uuid, scoped to its owner.

    404 rather than 403 for somebody else's template — a 403 would confirm the uuid is real.
    """
    template = await template_crud.get_by_uuid(
        db, template_id, extra_filters={"user_id": user_id}
    )
    if not template:
        raise HTTPException(status_code=404, detail="Email template not found")
    return template


def _choice(
    template: EmailTemplate,
    *,
    workspace_id: Optional[int] = None,
    workspace_names: Optional[Dict[int, str]] = None,
) -> Dict[str, Any]:
    """
    One template as a dropdown row.

    ``variables`` rides along deliberately: the property panel has to draw one binding row
    per declared variable the instant a template is picked, and a second round trip to
    fetch them would make the panel flicker or — worse — let an operator save a node before
    its bindings had loaded.

    ``disabled_reason`` means **picking this will not work**, so the only thing that earns
    it is a switched-off template.

    A workspace is *not* one of those things. "Ownership is the user; the workspace is who
    else may use it" — see the model's own note. ``workspace_id`` **widens** who can reach
    a template; it never narrows what its owner may do with it. So a template shared with a
    team is still the owner's to send from any of their graphs, and when ``workspace_names``
    is supplied the workspace is written into ``detail`` as information — which team also
    has this — rather than into ``disabled_reason`` as a refusal.
    """
    detail = (template.subject_template or "")[:80]

    if workspace_names is not None and template.workspace_id not in (None, workspace_id):
        shared_with = workspace_names.get(template.workspace_id)

        if shared_with:
            detail = f"shared with {shared_with}" + (f" · {detail}" if detail else "")

    return {
        "uuid": str(template.uuid),
        "label": template.name,
        "detail": detail,
        "disabled_reason": "" if template.is_active else "Switched off",
        "variables": list(template.variables or []),
    }


async def choices(db: AsyncSession, user_id: int) -> List[Dict[str, Any]]:
    """
    The user's templates as ``{uuid, label, detail, disabled_reason, variables}`` for a
    node's dropdown.

    Unscoped: every template the user owns is selectable. Used where there is no workspace
    to scope by — the Flow Builder canvas and this module's own pages.
    """
    templates = await template_crud.get_many(
        db, filters={"user_id": user_id}, order_by="name"
    )
    return [_choice(template) for template in templates]


async def choices_for_workspace(
    db: AsyncSession,
    user_id: int,
    workspace_id: Optional[int],
) -> List[Dict[str, Any]]:
    """
    The same list, saying which team each template is shared with.

    **Every template the user owns is selectable, including ones shared elsewhere.**
    ``workspace_id`` records who *else* may use a template; it does not take it away from
    its owner. A graph and a template belonging to the same person can always be used
    together, and refusing that would mean sharing a template with a team quietly removed
    the owner's own access to it.

    What ``workspace_id`` buys is context, not permission: a template shared with another
    team is labelled with that team's name, so an operator picking between two similarly
    named templates can tell them apart. Templates in this graph's workspace, and ones
    shared with nobody, are shown plainly.

    ``workspace_id`` of ``None`` is the ordinary case rather than an error — a graph
    attached to a data agent has no workspace at all, and attachment and sharing are
    mutually exclusive, so this is the state most graphs are in.

    Takes the internal bigint rather than the uuid because its only caller has already
    resolved the graph row and holds it.
    """
    templates = await template_crud.get_many(
        db, filters={"user_id": user_id}, order_by="name"
    )
    names = await _workspace_names(db, user_id)

    return [
        _choice(template, workspace_id=workspace_id, workspace_names=names)
        for template in templates
    ]


async def _workspace_names(db: AsyncSession, user_id: int) -> Dict[int, str]:
    """This user's workspaces as ``{id: name}``, for labelling a template's row."""
    workspaces = await workspace_crud.get_many(db, filters={"user_id": user_id})

    return {workspace.id: workspace.name for workspace in workspaces}


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


async def _resolved_workspace_id(
    db: AsyncSession, user_id: int, workspace_uuid: Any
) -> Optional[int]:
    """The internal id for a workspace uuid, or ``None``. Scoped to the owner, so a template
    cannot be shared into a workspace the user does not have."""
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


def _validated_content(
    *,
    subject_template: str,
    body_html_template: str,
    body_text_template: str,
    variables: Any,
) -> Dict[str, Any]:
    """
    Check the three bodies and the declaration against each other.

    Every :class:`RenderError` raised in here becomes a 400 with the renderer's own
    sentence. That translation happens once, here, rather than in the route — the route's
    job is to catch ``HTTPException`` and render a banner, and a service that leaks a
    second exception type makes every caller handle two.
    """
    try:
        declared = rendering.parse_declaration(variables)
    except RenderError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc

    clean_subject = require_text(subject_template, "Subject", _SUBJECT_MAX)
    clean_html = require_text(body_html_template, "Message body", _BODY_MAX)
    clean_text = optional_text(body_text_template, "Plain-text body", _BODY_MAX)

    try:
        rendering.assert_declared(
            subject_template=clean_subject,
            body_html_template=clean_html,
            body_text_template=clean_text,
            variables=declared,
        )
    except RenderError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc

    return {
        "subject_template": clean_subject,
        "body_html_template": clean_html,
        "body_text_template": clean_text,
        "variables": declared,
    }


def _fail_on_duplicate_name(name: str, exc: IntegrityError) -> NoReturn:
    raise HTTPException(
        status_code=400,
        detail=f"You already have a template named '{name}'.",
    ) from exc


# --------------------------------------------------------------------------
# Write
# --------------------------------------------------------------------------


async def create_template(
    db: AsyncSession,
    user_id: int,
    *,
    name: str,
    subject_template: str,
    body_html_template: str,
    body_text_template: str = "",
    description: str = "",
    variables: Any = None,
    workspace_uuid: Any = None,
) -> EmailTemplate:
    """Store a new template."""
    clean_name = require_text(name, "Template name", _NAME_MAX)
    content = _validated_content(
        subject_template=subject_template,
        body_html_template=body_html_template,
        body_text_template=body_text_template,
        variables=variables,
    )

    try:
        return await template_crud.create(
            db,
            {
                "user_id": user_id,
                "workspace_id": await _resolved_workspace_id(
                    db, user_id, workspace_uuid
                ),
                "name": clean_name,
                "description": optional_text(
                    description, "Description", _DESCRIPTION_MAX
                ),
                **content,
            },
        )
    except IntegrityError as exc:
        # Rollback before raising: the HTMX route re-renders the list in this same session.
        await db.rollback()
        _fail_on_duplicate_name(clean_name, exc)


async def update_template(
    db: AsyncSession,
    user_id: int,
    template_id: uuid_pkg.UUID,
    *,
    name: str,
    subject_template: str,
    body_html_template: str,
    body_text_template: str = "",
    description: str = "",
    variables: Any = None,
    workspace_uuid: Any = None,
) -> EmailTemplate:
    """
    Edit a template.

    **Editing does not touch messages already sent or queued.** Their text was rendered and
    stored at enqueue, so the delivery log keeps saying what actually went out — which is
    the property the whole render-at-enqueue decision exists to buy.

    Removing a variable that a trigger still binds is *allowed*, and the trigger's next
    firing refuses with a sentence naming it. Blocking the edit here would need this
    function to know about triggers, and would leave an operator unable to fix a template
    without first unpicking every trigger that uses it. The failure is loud and immediate
    either way; this way the operator can work in the order they want to.
    """
    template = await get_template(db, user_id, template_id)

    clean_name = require_text(name, "Template name", _NAME_MAX)
    content = _validated_content(
        subject_template=subject_template,
        body_html_template=body_html_template,
        body_text_template=body_text_template,
        variables=variables,
    )

    try:
        updated = await template_crud.update(
            db,
            template.id,
            {
                "name": clean_name,
                "description": optional_text(
                    description, "Description", _DESCRIPTION_MAX
                ),
                "workspace_id": await _resolved_workspace_id(
                    db, user_id, workspace_uuid
                ),
                **content,
            },
        )
    except IntegrityError as exc:
        await db.rollback()
        _fail_on_duplicate_name(clean_name, exc)

    if updated is None:
        raise HTTPException(status_code=404, detail="Email template not found")
    return updated


async def set_active(
    db: AsyncSession, user_id: int, template_id: uuid_pkg.UUID, is_active: bool
) -> EmailTemplate:
    """Switch a template on or off. An off template refuses at enqueue with a sentence."""
    template = await get_template(db, user_id, template_id)
    updated = await template_crud.update(
        db, template.id, {"is_active": bool(is_active)}
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="Email template not found")
    return updated


async def delete_template(
    db: AsyncSession, user_id: int, template_id: uuid_pkg.UUID
) -> None:
    """Delete a template, unless a trigger still uses it. See the module docstring."""
    template = await get_template(db, user_id, template_id)

    in_use = await templates_in_use(db, [template.id])
    count = in_use.get(template.id, 0)
    if count:
        raise HTTPException(
            status_code=400,
            detail=(
                f"'{template.name}' cannot be deleted because {count} "
                f"trigger{'s' if count != 1 else ''} "
                f"use{'' if count != 1 else 's'} it. "
                "Delete or re-point those triggers first."
            ),
        )

    await template_crud.delete(db, template.id)


# --------------------------------------------------------------------------
# Preview
# --------------------------------------------------------------------------


def sample_values(variables: Any) -> Dict[str, str]:
    """
    Obvious stand-ins for a preview.

    A declared default is used where there is one — that is the real value the send would
    use. Otherwise the variable's own name in guillemets, which is deliberately something
    nobody could mistake for data: ``«CUSTOMER»`` in a preview is unmistakably a
    placeholder, whereas a plausible-looking "Jane Smith" invites an operator to believe
    the wiring works when nothing has been bound yet.
    """
    values: Dict[str, str] = {}
    for item in variables or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip().upper()
        if not name:
            continue
        values[name] = str(item.get("default") or "") or f"«{name}»"
    return values


def preview(template: EmailTemplate) -> Dict[str, Any]:
    """
    Render the template with stand-in values, for the Preview pane.

    Returns the error rather than raising it: a preview of a half-written template is the
    normal case while somebody is typing, and a 400 would replace the pane they are working
    in with a banner. The pane shows what is wrong and keeps their work on screen — the same
    reason a canvas save error renders into a banner instead of replacing the page.
    """
    try:
        subject, body_html, body_text = rendering.render_message(
            subject_template=template.subject_template,
            body_html_template=template.body_html_template,
            body_text_template=template.body_text_template,
            variables=list(template.variables or []),
            values=sample_values(template.variables),
        )
    except RenderError as exc:
        return {"ok": False, "error": exc.message}

    return {
        "ok": True,
        "error": "",
        "subject": subject,
        "body_html": body_html,
        "body_text": body_text or "",
    }


def unused_variables(template: EmailTemplate) -> List[str]:
    """
    Declared variables no body references.

    Reported, never refused — see the module docstring. Shown as a hint on the form so an
    operator who *meant* to use one can see they have not, without being stopped from
    saving work in progress.
    """
    used = (
        rendering.placeholders_in(template.subject_template)
        | rendering.placeholders_in(template.body_html_template)
        | rendering.placeholders_in(template.body_text_template)
    )
    return [
        name for name in rendering.declared_names(template.variables) if name not in used
    ]
