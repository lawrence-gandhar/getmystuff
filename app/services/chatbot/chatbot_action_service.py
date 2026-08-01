"""
Chatbot actions — owner-defined HTTP calls the agent may make on a visitor's
behalf (look up an order, check availability, create a ticket) and whose
response is then used to answer.

Ownership vs. attachment
------------------------
An action belongs to a **user** and lives in their Actions library; a
ChatbotActionLink row attaches it to a chatbot, and the same action can be
attached to as many chatbots as they like. Only *active* actions can be attached
and only active ones run, so the library switch is a single off-button across
every chatbot using it.

How a turn uses them
--------------------
Rather than native per-provider tool calling (three separate implementations
that would also collide with the forced structured output every provider path
here already uses), an action turn is a *router pass*:

    1. no active actions          -> nothing extra happens at all
    2. one structured LLM call    -> "which action, with which parameters?"
    3. execute the chosen action  -> HTTP request, bounded response
    4. the normal answer call     -> with the response injected as context

One extra round-trip, one action per turn, no chaining — a deliberate trade for
working identically on Claude, any OpenAI-compatible endpoint, and the in-built
local model.

Placeholders
------------
``{{VAR}}`` is one of the chatbot's prompt variables; ``{{param.NAME}}`` is a
value the model supplied for this call. Headers accept ``{{VAR}}`` only: header
values hold credentials, and a value the model derived from visitor text must
never be able to reach a request header.

Egress safety
-------------
An action is user-authored outbound HTTP from the server, i.e. textbook SSRF
surface. Every request is https, is resolved and IP-checked immediately before
being sent, never follows redirects (the standard way to slip past an IP check),
is timeout-bounded, and has its response body byte-capped before any of it is
shown to a model.
"""

import asyncio
import ipaddress
import json
import logging
import re
import socket
import uuid
from dataclasses import dataclass
from typing import List, Optional, Tuple
from urllib.parse import quote, urlparse

import httpx
from litestar.exceptions import HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.chatbot.queries import (
    fetch_action_attachment_names,
    fetch_actions_for_key,
    fetch_attachable_actions,
)
from app.db.db_utils import CRUDQueryBuilder
from app.models.chatbot import (
    ACTION_HTTP_METHODS,
    ACTION_PARAMETER_TYPES,
    ChatbotAction,
    ChatbotActionLink,
    ChatbotApiKey,
)
from app.services.ai_analytics.ai_analytics_service import answer_structured
from app.services.chatbot.chatbot_ai_settings_service import LlmChoice
from app.services.chatbot.chatbot_service import get_chatbot_key
from app.utils.crypto import decrypt_password, encrypt_password
from app.utils.turn_recorder import record_action

logger = logging.getLogger(__name__)

action_crud = CRUDQueryBuilder(ChatbotAction)
action_link_crud = CRUDQueryBuilder(ChatbotActionLink)

_ACTION_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
_PARAM_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,49}$")
_HEADER_NAME_RE = re.compile(r"^[A-Za-z0-9-]{1,100}$")
_PLACEHOLDER_RE = re.compile(r"\{\{\s*(param\.)?([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")

_MAX_ACTIONS_PER_USER = 30
_MAX_PARAMETERS_PER_ACTION = 10
_MAX_HEADERS_PER_ACTION = 10
_MAX_DESCRIPTION_LEN = 500
_MAX_URL_LEN = 1000
_MAX_BODY_TEMPLATE_LEN = 4000
_TIMEOUT_RANGE = (1, 30)

# Hard byte cap on what is read off the wire, and character cap on what is then
# put in front of a model.
_MAX_RESPONSE_BYTES = 256 * 1024
_MAX_RESPONSE_CHARS = 4000


# --------------------------------------------------------------------------
# Input / output shapes
# --------------------------------------------------------------------------

@dataclass
class ActionInput:
    """Raw form values for one action, before validation."""
    name: str
    description: str
    http_method: str
    url: str
    headers_json: str
    body_template: str
    parameters_json: str
    timeout_seconds: str


@dataclass
class ActionOutcome:
    """What happened when an action ran, for the model and for the audit trail."""
    action_name: str
    status: str            # "success" | "error"
    context_text: str      # injected into the answer call
    detail: str = ""       # internal-facing; never sent to a visitor
    http_status: Optional[int] = None


class ActionParameterValue(BaseModel):
    name: str
    value: str


class ActionSelection(BaseModel):
    """
    The router's answer. Parameters are a name/value list rather than a free-form
    object because a free-form object has no fixed JSON schema, and every
    provider path here enforces a schema.
    """
    action: Optional[str] = Field(
        default=None,
        description="Name of the action to call, or null if none of them apply to this message.",
    )
    parameters: List[ActionParameterValue] = Field(
        default_factory=list,
        description="Values for the chosen action's parameters, as strings.",
    )


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

def _validate_name(value: str) -> str:
    value = (value or "").strip().lower()
    if not _ACTION_NAME_RE.match(value):
        raise HTTPException(
            status_code=400,
            detail=(
                "Action name must be 3-64 characters: lowercase letters, numbers and "
                "underscores only, starting with a letter (e.g. lookup_order_status)"
            ),
        )
    return value


def _validate_description(value: str) -> str:
    value = (value or "").strip()
    if not value:
        raise HTTPException(
            status_code=400,
            detail="Describe when this action should be used — the AI decides from this text",
        )
    if len(value) > _MAX_DESCRIPTION_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"Action description must not exceed {_MAX_DESCRIPTION_LEN} characters",
        )
    return value


def _validate_method(value: str) -> str:
    value = (value or "").strip().upper()
    if value not in ACTION_HTTP_METHODS:
        raise HTTPException(
            status_code=400,
            detail=f"HTTP method must be one of: {', '.join(ACTION_HTTP_METHODS)}",
        )
    return value


def _validate_timeout(value: str) -> int:
    low, high = _TIMEOUT_RANGE
    try:
        timeout = int(str(value).strip())
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"Timeout must be a whole number of seconds ({low}-{high})")
    if not low <= timeout <= high:
        raise HTTPException(status_code=400, detail=f"Timeout must be between {low} and {high} seconds")
    return timeout


def _parse_json_list(raw: str, label: str) -> list:
    raw = (raw or "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=400,
            detail=f"{label} could not be read. Please re-enter them and save again.",
        )
    if not isinstance(parsed, list):
        raise HTTPException(status_code=400, detail=f"{label} must be a list")
    return parsed


def _validate_parameters(raw: str) -> List[dict]:
    parsed = _parse_json_list(raw, "Action parameters")
    if len(parsed) > _MAX_PARAMETERS_PER_ACTION:
        raise HTTPException(
            status_code=400,
            detail=f"An action can have at most {_MAX_PARAMETERS_PER_ACTION} parameters",
        )

    parameters: List[dict] = []
    seen: set = set()

    for item in parsed:
        if not isinstance(item, dict):
            raise HTTPException(status_code=400, detail="Each action parameter must have a name and a type")

        name = str(item.get("name") or "").strip().lower()
        param_type = str(item.get("type") or "string").strip().lower()
        description = str(item.get("description") or "").strip()
        required = bool(item.get("required"))

        if not _PARAM_NAME_RE.match(name):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Parameter name {name!r} is invalid — use lowercase letters, numbers "
                    "and underscores only, starting with a letter"
                ),
            )
        if param_type not in ACTION_PARAMETER_TYPES:
            raise HTTPException(
                status_code=400,
                detail=f"Parameter {name}'s type must be one of: {', '.join(ACTION_PARAMETER_TYPES)}",
            )
        if not description:
            raise HTTPException(
                status_code=400,
                detail=f"Parameter {name} needs a description so the AI knows what to put in it",
            )
        if name in seen:
            raise HTTPException(status_code=400, detail=f"Parameter {name} is defined more than once")

        seen.add(name)
        parameters.append({
            "name": name,
            "type": param_type,
            "description": description,
            "required": required,
        })

    return parameters


def _validate_headers(raw: str) -> List[dict]:
    parsed = _parse_json_list(raw, "Action headers")
    if len(parsed) > _MAX_HEADERS_PER_ACTION:
        raise HTTPException(
            status_code=400,
            detail=f"An action can have at most {_MAX_HEADERS_PER_ACTION} headers",
        )

    headers: List[dict] = []
    seen: set = set()

    for item in parsed:
        if not isinstance(item, dict):
            raise HTTPException(status_code=400, detail="Each header must have a name and a value")

        key = str(item.get("key") or "").strip()
        value = str(item.get("value") or "").strip()

        if not _HEADER_NAME_RE.match(key):
            raise HTTPException(
                status_code=400,
                detail=f"Header name {key!r} is invalid — letters, numbers and hyphens only",
            )
        if not value:
            raise HTTPException(status_code=400, detail=f"Header {key} needs a value")
        if key.lower() in seen:
            raise HTTPException(status_code=400, detail=f"Header {key} is defined more than once")

        for is_param, _name in _PLACEHOLDER_RE.findall(value):
            if is_param:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Header {key} cannot use {{{{param.*}}}} placeholders — header values may only "
                        "reference prompt variables, because AI-supplied values must never reach a header"
                    ),
                )

        seen.add(key.lower())
        headers.append({"key": key, "value": value})

    return headers


def _validate_placeholders(text: str, field_label: str, parameters: List[dict]) -> None:
    """
    Every {{param.NAME}} used must be a parameter this action declares, and must
    be a *required* one — an optional parameter the AI leaves out would render
    into a broken URL or body, so the mismatch is caught at save time instead.
    """
    by_name = {p["name"]: p for p in parameters}

    for is_param, name in _PLACEHOLDER_RE.findall(text or ""):
        if not is_param:
            continue

        declared = by_name.get(name.lower())
        if not declared:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{field_label} references {{{{param.{name}}}}} but this action has no "
                    f"parameter named {name} — add it under Parameters first"
                ),
            )
        if not declared.get("required"):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{field_label} references {{{{param.{name}}}}}, so that parameter must be "
                    "marked Required — otherwise the request would be built with a missing value"
                ),
            )


def _validate_url_template(url: str, parameters: List[dict]) -> str:
    url = (url or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="Action URL is required")
    if len(url) > _MAX_URL_LEN:
        raise HTTPException(status_code=400, detail=f"Action URL must not exceed {_MAX_URL_LEN} characters")

    _validate_placeholders(url, "The URL", parameters)

    # Validate the shape with placeholders neutralised — an unsubstituted
    # {{...}} is not a legal URL character sequence.
    _validate_outbound_url_shape(_PLACEHOLDER_RE.sub("placeholder", url))
    return url


def _validate_body_template(body: str, parameters: List[dict]) -> Optional[str]:
    body = (body or "").strip()
    if not body:
        return None
    if len(body) > _MAX_BODY_TEMPLATE_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"Request body must not exceed {_MAX_BODY_TEMPLATE_LEN} characters",
        )

    _validate_placeholders(body, "The request body", parameters)

    # A body whose placeholders are filled with plausible values must be valid
    # JSON — catching a broken template now beats failing mid-conversation.
    probe = _PLACEHOLDER_RE.sub("placeholder", body)
    try:
        json.loads(probe)
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=400,
            detail=(
                "Request body must be valid JSON once its placeholders are filled in "
                '(e.g. {"order_id": "{{param.order_id}}"})'
            ),
        )
    return body


# --------------------------------------------------------------------------
# Egress safety
# --------------------------------------------------------------------------

def _validate_outbound_url_shape(url: str) -> Tuple[str, int]:
    """Scheme/host checks that don't need DNS. Returns (host, port)."""
    parsed = urlparse(url)

    if parsed.scheme != "https":
        raise HTTPException(status_code=400, detail="Action URL must start with https://")
    if not parsed.hostname:
        raise HTTPException(status_code=400, detail="Action URL must include a hostname")
    if parsed.username or parsed.password:
        raise HTTPException(
            status_code=400,
            detail="Action URL must not contain a username or password — use a header instead",
        )

    return parsed.hostname, parsed.port or 443


async def _assert_public_host(host: str, port: int) -> None:
    """
    Reject a host that resolves to any non-public address — loopback, private
    ranges, link-local (which is where cloud instance-metadata endpoints such as
    169.254.169.254 live), multicast and reserved space.

    Note this is a check-then-request, so it narrows rather than fully closes
    DNS rebinding: a hostile DNS server could answer differently for the actual
    connection. Closing that completely means pinning the resolved IP at the
    transport layer, which httpx does not expose directly.
    """
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise HTTPException(
            status_code=400,
            detail=f"The host {host} could not be resolved. Check the action URL.",
        )

    for info in infos:
        address = info[4][0]
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"The host {host} resolves to a private or internal address "
                    f"({address}). Actions may only call public endpoints."
                ),
            )


# --------------------------------------------------------------------------
# Placeholder rendering
# --------------------------------------------------------------------------

def _coerce_param(value: str, param_type: str) -> Tuple[str, str]:
    """
    Validate an AI-supplied value against its declared type.

    Returns (url_text, body_literal): the plain text to put in a URL, and the
    JSON fragment to put in a body. Strings are escaped *without* surrounding
    quotes, so a template writes "{{param.id}}" with quotes for a string and
    {{param.qty}} bare for a number/boolean — the usual JSON-template
    convention.
    """
    text = (value or "").strip()

    if param_type == "number":
        try:
            float(text)
        except ValueError:
            raise ValueError(f"expected a number but the AI supplied {text!r}")
        return text, text

    if param_type == "boolean":
        lowered = text.lower()
        if lowered not in ("true", "false"):
            raise ValueError(f"expected true or false but the AI supplied {text!r}")
        return lowered, lowered

    return text, json.dumps(text)[1:-1]


def _render(text: str, variables: dict, params: dict, mode: str) -> str:
    """
    Substitute {{VAR}} / {{param.NAME}} in `text`.

    `mode` decides escaping: "url" percent-encodes each value, "body" inserts
    the JSON fragment, "header" allows variables only (enforced at save time
    too, so reaching the error here means the row was edited outside the app).
    An unknown placeholder raises rather than being left in place — a half-built
    URL or body must never be sent.
    """
    def _replace(match: re.Match) -> str:
        is_param, name = match.group(1), match.group(2)

        # Parameter names are stored lower-cased and variable names upper-cased,
        # so placeholders resolve regardless of how they were typed.
        if is_param:
            name = name.lower()
            if mode == "header":
                raise ValueError("header values cannot reference AI-supplied parameters")
            if name not in params:
                raise ValueError(f"no value was supplied for parameter {name}")
            url_text, body_literal = params[name]
            return quote(url_text, safe="") if mode == "url" else body_literal

        name = name.upper()
        if name not in variables:
            raise ValueError(f"{{{{{name}}}}} is not a defined prompt variable")
        value = variables[name]
        if mode == "url":
            return quote(value, safe="")
        if mode == "body":
            return json.dumps(value)[1:-1]
        return value

    rendered = _PLACEHOLDER_RE.sub(_replace, text or "")

    if mode == "header" and ("\r" in rendered or "\n" in rendered):
        # A variable value carrying a line break would split the request.
        raise ValueError("header values cannot contain line breaks")

    return rendered


def _decrypt_headers(action: ChatbotAction) -> List[dict]:
    if not action.headers_encrypted:
        return []
    try:
        # json.JSONDecodeError is a ValueError, so one clause covers both a
        # failed decryption and unreadable plaintext.
        return json.loads(decrypt_password(action.headers_encrypted))
    except ValueError:
        logger.exception("Stored headers for action %s could not be decrypted", action.uuid)
        raise HTTPException(
            status_code=500,
            detail="This action's saved headers could not be read. Please re-enter them.",
        )


def build_action_views(
    actions: List[ChatbotAction], attachment_counts: Optional[dict] = None
) -> List[dict]:
    """
    Actions shaped for the UI: public uuid only, plus the parameter and header
    lists pre-serialised for the edit form's hidden JSON fields.

    One shaping function serves both the library page and a chatbot's Actions
    tab; `attachment_counts` ({action id: chatbots attached}, from
    queries.count_action_attachments) is what the library adds on top.

    Header values are decrypted here because they are shown back to the owner,
    who typed them in — the encryption protects them at rest in the database, it
    isn't an attempt to hide them from that owner.
    """
    counts = attachment_counts or {}
    return [
        {
            "uuid": str(action.uuid),
            "name": action.name,
            "description": action.description,
            "http_method": action.http_method,
            "url": action.url,
            "body_template": action.body_template or "",
            "timeout_seconds": action.timeout_seconds,
            "is_active": action.is_active,
            "parameters": action.parameters or [],
            "parameters_json": json.dumps(action.parameters or []),
            "headers_json": json.dumps(_decrypt_headers(action)),
            "attached_count": counts.get(action.id, 0),
        }
        for action in actions
    ]


# --------------------------------------------------------------------------
# Library CRUD — actions belong to the user, not to one chatbot
# --------------------------------------------------------------------------

async def get_user_actions(db: AsyncSession, user_id: int) -> List[ChatbotAction]:
    """Every action in this user's library, in name order."""
    return await action_crud.get_many(db, filters={"user_id": user_id}, order_by="name")


async def get_action(db: AsyncSession, user_id: int, action_id: uuid.UUID) -> ChatbotAction:
    action = await action_crud.get_by_uuid(db, action_id, extra_filters={"user_id": user_id})
    if not action:
        raise HTTPException(status_code=404, detail="Action not found")
    return action


async def get_actions_for_chatbot(
    db: AsyncSession, user_id: int, key_id: uuid.UUID
) -> List[ChatbotAction]:
    """Actions attached to one chatbot (active or not, so the owner can see them all)."""
    key = await get_chatbot_key(db, user_id, key_id)
    return await fetch_actions_for_key(db, key.id)


async def get_attachable_actions(
    db: AsyncSession, user_id: int, key_id: uuid.UUID
) -> List[ChatbotAction]:
    """Library actions this chatbot could still be given — active, not yet attached."""
    key = await get_chatbot_key(db, user_id, key_id)
    return await fetch_attachable_actions(db, user_id, key.id)


async def get_active_actions_by_key_id(db: AsyncSession, chatbot_key_id: int) -> List[ChatbotAction]:
    """
    Runtime lookup — the caller already resolved the chatbot from its publishable
    key. Only active actions run, so deactivating one in the library switches it
    off on every chatbot it is attached to.
    """
    return await fetch_actions_for_key(db, chatbot_key_id, active_only=True)


def _build_action_data(fields: ActionInput) -> dict:
    """Validate a submitted action into a column dict. Raises before anything is written."""
    parameters = _validate_parameters(fields.parameters_json)
    headers = _validate_headers(fields.headers_json)

    return {
        "name": _validate_name(fields.name),
        "description": _validate_description(fields.description),
        "http_method": _validate_method(fields.http_method),
        "url": _validate_url_template(fields.url, parameters),
        "headers_encrypted": encrypt_password(json.dumps(headers)) if headers else None,
        "body_template": _validate_body_template(fields.body_template, parameters),
        "parameters": parameters,
        "timeout_seconds": _validate_timeout(fields.timeout_seconds),
    }


async def _assert_name_available(
    db: AsyncSession, user_id: int, name: str, exclude_id: Optional[int] = None
) -> None:
    existing = await action_crud.get_one(db, filters={"user_id": user_id, "name": name})
    if existing and existing.id != exclude_id:
        raise HTTPException(
            status_code=400,
            detail=f"You already have an action named {name} — pick another name",
        )


async def create_action(db: AsyncSession, user_id: int, fields: ActionInput) -> ChatbotAction:
    existing_count = await action_crud.count(db, filters={"user_id": user_id})
    if existing_count >= _MAX_ACTIONS_PER_USER:
        raise HTTPException(
            status_code=400,
            detail=(
                f"You already have the maximum of {_MAX_ACTIONS_PER_USER} actions. "
                "Delete one before adding another."
            ),
        )

    data = _build_action_data(fields)
    await _assert_name_available(db, user_id, data["name"])

    return await action_crud.create(db, {"user_id": user_id, **data})


async def update_action(
    db: AsyncSession, user_id: int, action_id: uuid.UUID, fields: ActionInput
) -> ChatbotAction:
    """
    Edit a library action. The change applies to every chatbot it is attached to
    — that is the point of a shared action, and the UI says so before saving.
    """
    action = await get_action(db, user_id, action_id)
    data = _build_action_data(fields)
    await _assert_name_available(db, user_id, data["name"], exclude_id=action.id)
    return await action_crud.update(db, action.id, data)


async def toggle_action_active(
    db: AsyncSession, user_id: int, action_id: uuid.UUID
) -> ChatbotAction:
    """
    Flip the library switch. Deactivating leaves attachments alone: the action
    simply stops running and stops being offered to other chatbots.
    """
    action = await get_action(db, user_id, action_id)
    return await action_crud.update(db, action.id, {"is_active": not action.is_active})


async def delete_action(db: AsyncSession, user_id: int, action_id: uuid.UUID) -> None:
    """Delete from the library; ON DELETE CASCADE removes every attachment with it."""
    action = await get_action(db, user_id, action_id)
    await action_crud.delete(db, action.id)


async def get_attachment_names(db: AsyncSession, user_id: int, action_id: uuid.UUID) -> List[str]:
    """Chatbot names one action is attached to, for confirmation messages."""
    action = await get_action(db, user_id, action_id)
    return await fetch_action_attachment_names(db, action.id)


# --------------------------------------------------------------------------
# Attachment — which chatbots use which library action
# --------------------------------------------------------------------------

async def attach_action(
    db: AsyncSession, user_id: int, key_id: uuid.UUID, action_id: uuid.UUID
) -> None:
    """
    Give one chatbot an existing library action. Ownership of both sides is
    checked; an inactive action is refused rather than silently attached and
    never run.
    """
    key = await get_chatbot_key(db, user_id, key_id)
    action = await get_action(db, user_id, action_id)

    if not action.is_active:
        raise HTTPException(
            status_code=400,
            detail=f"The action {action.name} is inactive — activate it in Actions before adding it",
        )

    existing = await action_link_crud.get_one(
        db, filters={"chatbot_key_id": key.id, "action_id": action.id}
    )
    if existing:
        return  # already attached; adding twice is a no-op, not an error

    await action_link_crud.create(db, {"chatbot_key_id": key.id, "action_id": action.id})


async def detach_action(
    db: AsyncSession, user_id: int, key_id: uuid.UUID, action_id: uuid.UUID
) -> None:
    """Remove the action from this chatbot only — it stays in the library."""
    key = await get_chatbot_key(db, user_id, key_id)
    action = await get_action(db, user_id, action_id)

    link = await action_link_crud.get_one(
        db, filters={"chatbot_key_id": key.id, "action_id": action.id}
    )
    if not link:
        raise HTTPException(status_code=404, detail="That action is not attached to this chatbot")

    await action_link_crud.delete(db, link.id)


async def create_and_attach_action(
    db: AsyncSession, user_id: int, key_id: uuid.UUID, fields: ActionInput
) -> ChatbotAction:
    """
    The quick-create path from a chatbot's Actions tab: save to the library and
    attach in one step, so a one-off action doesn't need a detour to /actions.
    """
    await get_chatbot_key(db, user_id, key_id)  # fail before creating anything
    action = await create_action(db, user_id, fields)
    await attach_action(db, user_id, key_id, action.uuid)
    return action


# --------------------------------------------------------------------------
# Runtime — routing and execution
# --------------------------------------------------------------------------

def build_action_catalog(actions: List[ChatbotAction]) -> List[dict]:
    """The action list as the router sees it — no URLs, headers or bodies."""
    return [
        {
            "name": action.name,
            "description": action.description,
            "parameters": [
                {
                    "name": p.get("name"),
                    "type": p.get("type"),
                    "description": p.get("description"),
                    "required": bool(p.get("required")),
                }
                for p in (action.parameters or [])
            ],
        }
        for action in actions
    ]


_ROUTER_SYSTEM_PROMPT = (
    "Decide whether a visitor's message needs one of the business's configured "
    "actions called first. Pick one ONLY if its description clearly covers the "
    "request AND every required parameter is present in the message; otherwise "
    "return null — no call is the normal case, and a wrong call is worse than "
    "none. Never invent a parameter value; give every value as a string."
)


async def _select_action(
    db: AsyncSession,
    user_id: int,
    actions: List[ChatbotAction],
    visitor_message: str,
    llm_choice: LlmChoice,
) -> Optional[Tuple[ChatbotAction, dict]]:
    """Ask the model which action to run. Returns (action, raw params) or None."""
    catalog = build_action_catalog(actions)
    user_content = (
        f"Available actions (JSON):\n{json.dumps(catalog)}\n\n"
        f"Visitor message: {visitor_message}"
    )

    selection = await answer_structured(
        db,
        user_id,
        _ROUTER_SYSTEM_PROMPT,
        user_content,
        ActionSelection,
        llm_choice.forced_key_uuid,
        llm_choice.use_inbuilt_llm,
    )

    chosen_name = (selection.action or "").strip().lower()
    if not chosen_name or chosen_name in ("null", "none"):
        return None

    action = next((a for a in actions if a.name == chosen_name), None)
    if not action:
        # A hallucinated action name is not an error worth showing anyone — the
        # turn simply proceeds without an action.
        logger.info("Action router picked unknown action %r; ignoring", chosen_name)
        return None

    supplied = {p.name.strip().lower(): p.value for p in selection.parameters}
    return action, supplied


def _prepare_parameters(action: ChatbotAction, supplied: dict) -> dict:
    """
    Type-check the model's parameter values against the action's declared
    schema, returning {name: (url_text, body_literal)}.
    """
    prepared: dict = {}

    for declared in action.parameters or []:
        name = str(declared.get("name") or "")
        param_type = str(declared.get("type") or "string")
        raw = supplied.get(name)

        if raw is None or not str(raw).strip():
            if declared.get("required"):
                raise ValueError(f"required parameter {name} was not supplied")
            continue

        prepared[name] = _coerce_param(str(raw), param_type)

    return prepared


def _failed(action_name: str, guidance: str, detail: str, http_status: Optional[int] = None) -> ActionOutcome:
    """A failure outcome: what the model is told, plus internal-only detail."""
    return ActionOutcome(
        action_name=action_name,
        status="error",
        context_text=f"The action `{action_name}` {guidance}",
        detail=detail,
        http_status=http_status,
    )


def _prepare_request(
    action: ChatbotAction, params: dict, variables: dict
) -> Tuple[str, dict, Optional[str]]:
    """
    Render the action's URL, headers and body. Raises ValueError when a
    placeholder can't be resolved or the rendered body isn't valid JSON.
    """
    url = _render(action.url, variables, params, mode="url")

    try:
        stored_headers = _decrypt_headers(action)
    except HTTPException as exc:
        raise ValueError(str(exc.detail))

    headers = {
        item["key"]: _render(item["value"], variables, params, mode="header")
        for item in stored_headers
    }

    body: Optional[str] = None
    if action.body_template:
        body = _render(action.body_template, variables, params, mode="body")
        json.loads(body)  # JSONDecodeError is a ValueError — handled by the caller
        headers.setdefault("Content-Type", "application/json")

    return url, headers, body


async def execute_action(action: ChatbotAction, params: dict, variables: dict) -> ActionOutcome:
    """
    Render and send one action's HTTP request, returning a bounded description
    of the response for the answering model.

    Never raises for a failed call: a broken endpoint should degrade the answer,
    not break the conversation. Failures are logged with detail and reported to
    the model in general terms.
    """
    try:
        url, headers, body = _prepare_request(action, params, variables)
    except ValueError as exc:
        logger.warning("Action %s could not be prepared: %s", action.name, exc)
        return _failed(
            action.name,
            "could not be run because it is misconfigured. Answer without it and do not "
            "mention the failure in technical terms.",
            str(exc),
        )

    try:
        host, port = _validate_outbound_url_shape(url)
        await _assert_public_host(host, port)
    except HTTPException as exc:
        logger.warning("Action %s blocked by egress policy: %s", action.name, exc.detail)
        return _failed(
            action.name,
            "could not be run because its endpoint is not allowed. Answer without it and "
            "do not mention the failure in technical terms.",
            str(exc.detail),
        )

    try:
        async with httpx.AsyncClient(
            timeout=float(action.timeout_seconds),
            follow_redirects=False,
        ) as client:
            async with client.stream(
                action.http_method,
                url,
                headers=headers,
                content=body.encode("utf-8") if body else None,
            ) as response:
                chunks: List[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    chunks.append(chunk)
                    total += len(chunk)
                    if total >= _MAX_RESPONSE_BYTES:
                        break
                raw = b"".join(chunks)[:_MAX_RESPONSE_BYTES]
                status_code = response.status_code
    except httpx.TimeoutException:
        logger.warning("Action %s timed out after %ss", action.name, action.timeout_seconds)
        return _failed(
            action.name,
            "did not respond in time. Tell the visitor you could not retrieve that "
            "information right now.",
            "timeout",
        )
    except httpx.RequestError as exc:
        logger.warning("Action %s request failed: %s", action.name, exc)
        return _failed(
            action.name,
            "could not be reached. Tell the visitor you could not retrieve that "
            "information right now.",
            str(exc),
        )

    text = raw.decode("utf-8", errors="replace")[:_MAX_RESPONSE_CHARS]

    if not 200 <= status_code < 300:
        logger.warning("Action %s returned HTTP %s", action.name, status_code)
        return _failed(
            action.name,
            "returned an error and no data is available. Tell the visitor you could not "
            "retrieve that information right now.",
            f"HTTP {status_code}: {text[:500]}",
            http_status=status_code,
        )

    return ActionOutcome(
        action_name=action.name,
        status="success",
        context_text=(
            f"Result of the action `{action.name}` (HTTP {status_code}) — treat this as "
            f"authoritative current data and answer from it:\n{text}"
        ),
        http_status=status_code,
    )


def action_audit(outcome: Optional[ActionOutcome]) -> Optional[dict]:
    """The audit record stored with the answer — never the raw response body."""
    if not outcome:
        return None
    return {
        "name": outcome.action_name,
        "status": outcome.status,
        "http_status": outcome.http_status,
    }


async def maybe_run_action(
    db: AsyncSession,
    chatbot_key: ChatbotApiKey,
    visitor_message: str,
    llm_choice: LlmChoice,
    variables: dict,
) -> Optional[ActionOutcome]:
    """
    Route and, if an action applies, run it. Returns None when the chatbot has
    no active actions (the common case — no extra LLM call is made) or when the
    model decides none apply.

    Whatever happened is reported to the open turn record here, at the one
    point an action actually runs, so every answer path gets the action into
    its conversation log without having to pass it along by hand.
    """
    outcome = await _route_and_run_action(db, chatbot_key, visitor_message, llm_choice, variables)
    record_action(action_audit(outcome))
    return outcome


async def _route_and_run_action(
    db: AsyncSession,
    chatbot_key: ChatbotApiKey,
    visitor_message: str,
    llm_choice: LlmChoice,
    variables: dict,
) -> Optional[ActionOutcome]:
    actions = await get_active_actions_by_key_id(db, chatbot_key.id)
    if not actions:
        return None

    try:
        selected = await _select_action(db, chatbot_key.user_id, actions, visitor_message, llm_choice)
    except HTTPException as exc:
        # The router failing must not take the whole turn down — the answer call
        # that follows will surface any real provider outage on its own.
        logger.warning("Action routing failed for chatbot %s: %s", chatbot_key.uuid, exc.detail)
        return None

    if not selected:
        return None

    action, supplied = selected

    try:
        params = _prepare_parameters(action, supplied)
    except ValueError as exc:
        logger.warning("Action %s not run: %s", action.name, exc)
        return _failed(
            action.name,
            "needs information the visitor has not provided yet. Ask them for the missing "
            "detail instead of guessing.",
            str(exc),
        )

    return await execute_action(action, params, variables)
