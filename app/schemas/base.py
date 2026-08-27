"""
app/schemas/base.py

Shared infrastructure for the whole schema layer. Sits at the top level of
``app/schemas/`` the same way ``db_utils.py`` sits at the top level of ``db/``:
it belongs to no single feature and every feature depends on it.

What lives here and why
-----------------------

**The error bridge.** This is the reason the layer needs a base at all. The
application's error contract is a ``litestar.exceptions.HTTPException`` whose
``detail`` is a sentence written for the person reading the screen — routes render
it straight into a Bootstrap alert and services raise nothing else. Pydantic's
native failure is a ``ValidationError`` whose message is
``1 validation error for X / name / Value error, ...``. If that ever escaped a
schema it would land in front of a user verbatim. So every entry point here
catches ``ValidationError`` and re-raises the project's own exception with a
readable message built from the field's ``title``.

**The request base classes.** ``FormRequest``, ``JsonRequest`` and
``QueryRequest`` differ only in where they read from, but that difference matters:
an HTML form sends ``""`` for an untouched field and repeats a key for a
multi-select, a JSON body sends real ``null`` and real lists, and a query string
sends neither. Each class converts its own source into the shape the field
validators expect, so a schema author never writes that conversion again.

**The annotated field types.** ``OptionalText``, ``RequiredText`` and friends
encode the rules ``app/utils/validators.py`` already owns, so a schema field and a
service check reject the same input with the same wording. The validators module
stays the single source of those rules; this module only makes them declarative.

What does not live here: business rules. A schema decides whether a payload is
*shaped* correctly — required, length, pattern, type, enum membership. Whether a
name is already taken, whether the caller owns the row, whether a datasource can
be reached: those need the database and stay in the service layer.
"""

from __future__ import annotations

import json
import math
import uuid as uuid_pkg
from typing import Any, ClassVar, Mapping, NoReturn, Optional, Sequence, Type, TypeVar

from litestar.connection import Request
from litestar.exceptions import HTTPException
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
)
from typing_extensions import Annotated

from app.utils.validators import (
    parse_json_object,
    require_identifier,
    require_object_name,
)

SchemaT = TypeVar("SchemaT", bound="AppBaseSchema")

# Length caps. Named rather than inlined so a field and its documented rule can
# never drift, and so the frontend `maxlength` attributes have one number to
# mirror. 255 is the column width used by every string column in app/models.
MAX_NAME_LENGTH = 255
MAX_DESCRIPTION_LENGTH = 2000
MAX_PROMPT_LENGTH = 20000
MAX_URL_LENGTH = 2048

# Hand-routed connectors. Every canvas in the application draws its connectors for
# the user and lets them override the route by dragging the line, and each stores
# the result the same way: a ``waypoints`` list of ``{"x": .., "y": ..}`` on the
# edge, in canvas pixels.
#
# Here rather than in three feature packages because three of them need exactly
# these two numbers, and a cap that differs between canvases is a cap that is
# wrong on two of them. The browsers enforce the same values; a cap only the client
# knows is not a cap.
#
# 4 is what the routing can express: with a fixed exit stub and a fixed entry stub,
# four free points already give more distinct orthogonal routes than anybody draws.
# 200_000 is a runaway ceiling rather than a design limit — 500 nodes in a single
# row at the widest step size is about 106_000px, and the canvas grows to fit, so
# anything past this is a client that has lost track of where it is.
MAX_EDGE_WAYPOINTS = 4
MAX_CANVAS_COORD = 200_000

# The tokens an HTML control can send for a boolean. A checkbox sends "on" when
# ticked and nothing at all when not; the toggle buttons in this application post
# an explicit "true"/"false". Everything here is accepted; anything else is
# refused rather than quietly read as false, because a value we don't recognise
# means the caller is not the form we designed for.
_TRUE_TOKENS = {"true", "on", "yes", "1", "t", "y"}
_FALSE_TOKENS = {"false", "off", "no", "0", "f", "n", ""}


# --------------------------------------------------------------------------
# Human-readable messages
#
# Pydantic reports a machine-readable `type` for every failure. Each one is
# mapped to a sentence the user can act on. A failure type that is missing from
# this table falls back to "<Label> is not valid" — never to Pydantic's own
# wording, which names the model class and the internal field.
# --------------------------------------------------------------------------

_WRONG_SHAPE = "{label} is not in the expected format"
_NOT_A_NUMBER = "{label} must be a number"
_NOT_A_WHOLE_NUMBER = "{label} must be a whole number"
_NOT_A_BOOLEAN = "{label} must be either on or off"
_NOT_A_SELECTION = "{label} is not a valid selection"
_NOT_ALLOWED_VALUE = "{label} is not one of the allowed values"
_REQUIRED = "{label} is required"

_TYPE_MESSAGES = {
    "missing": _REQUIRED,
    "string_type": "{label} must be text",
    "string_too_short": _REQUIRED,
    "string_too_long": "{label} cannot be longer than {max_length} characters",
    "string_pattern_mismatch": _WRONG_SHAPE,
    "int_type": _NOT_A_WHOLE_NUMBER,
    "int_parsing": _NOT_A_WHOLE_NUMBER,
    "float_type": _NOT_A_NUMBER,
    "float_parsing": _NOT_A_NUMBER,
    "decimal_parsing": _NOT_A_NUMBER,
    "greater_than": "{label} must be greater than {gt}",
    "greater_than_equal": "{label} cannot be less than {ge}",
    "less_than": "{label} must be less than {lt}",
    "less_than_equal": "{label} cannot be greater than {le}",
    "bool_type": _NOT_A_BOOLEAN,
    "bool_parsing": _NOT_A_BOOLEAN,
    "uuid_type": _NOT_A_SELECTION,
    "uuid_parsing": _NOT_A_SELECTION,
    "enum": _NOT_ALLOWED_VALUE,
    "literal_error": _NOT_ALLOWED_VALUE,
    "list_type": _WRONG_SHAPE,
    "dict_type": _WRONG_SHAPE,
    "model_type": _WRONG_SHAPE,
    "too_short": "{label} needs at least {min_length} entries",
    "too_long": "{label} cannot have more than {max_length} entries",
    "extra_forbidden": "{label} is not a field this request accepts",
    "json_invalid": "{label} could not be read",
}

# Prefixes Pydantic puts in front of a message raised by our own validator. The
# validator already wrote the full sentence, so the prefix is stripped and the
# sentence used as-is.
_MESSAGE_PREFIXES = ("Value error, ", "Assertion failed, ")


def _prettify(field_name: str) -> str:
    """``send_button_text`` -> ``Send button text``, for a field with no title."""
    return field_name.replace("_", " ").strip().capitalize() or "This value"


def _label_for(model: Type[BaseModel], loc: Sequence[Any]) -> str:
    """
    The user-facing name of whichever field failed.

    Walks ``loc`` so a failure inside a nested model or a list entry is named for
    the field the user filled in, not for the internal path. A list index is
    rendered as "entry N" (1-based) because a person counting rows on screen
    starts at one.
    """
    label = "This value"
    current: Optional[Type[BaseModel]] = model

    for part in loc:
        if isinstance(part, int):
            label = f"{label} (entry {part + 1})"
            continue

        name = str(part)
        info = getattr(current, "model_fields", {}).get(name) if current else None

        if info is None:
            # An unexpected path element (an extra field, a union tag). Name it
            # as best we can rather than dropping the context entirely.
            label = _prettify(name)
            current = None
            continue

        label = info.title or _prettify(name)
        annotation = info.annotation
        current = annotation if isinstance(annotation, type) and issubclass(
            annotation, BaseModel
        ) else None

    return label


def _message_for(error: Mapping[str, Any], model: Type[BaseModel]) -> str:
    """Turn one Pydantic error dict into a sentence for the user."""
    raw_message = str(error.get("msg", ""))

    for prefix in _MESSAGE_PREFIXES:
        if raw_message.startswith(prefix):
            # Raised by one of our own validators, which writes the whole
            # sentence — including the field label — on purpose.
            return raw_message[len(prefix):]

    label = _label_for(model, error.get("loc") or ())
    template = _TYPE_MESSAGES.get(str(error.get("type")))

    if template is None:
        return f"{label} is not valid"

    context = {"label": label, **(error.get("ctx") or {})}
    try:
        return template.format(**context)
    except (KeyError, IndexError):
        # A constraint whose context Pydantic named differently than expected.
        # Degrade to the generic sentence rather than raising inside error
        # handling, which would replace a 400 with a 500.
        return f"{label} is not valid"


def validation_error_detail(exc: ValidationError, model: Type[BaseModel]) -> str:
    """
    One sentence describing the first failure.

    Only the first: a form posts every field at once, so a single missing value
    can cascade into several errors, and a wall of them buried in an alert is
    less useful than the one thing to fix. The user fixes it, resubmits, and
    hears about the next.
    """
    errors = exc.errors()

    if not errors:
        return "That request could not be read. Please check the form and try again."

    return _message_for(errors[0], model)


def raise_request_error(exc: ValidationError, model: Type[BaseModel]) -> NoReturn:
    """Convert a request-side ``ValidationError`` into the project's 400."""
    raise HTTPException(
        status_code=400, detail=validation_error_detail(exc, model)
    ) from exc


def raise_response_error(exc: ValidationError, model: Type[BaseModel]) -> NoReturn:
    """
    Convert a response-side ``ValidationError`` into a 500.

    A response that fails its own schema is a defect in this application, not
    something the caller did — so it must not be reported as a 400, and the
    internal detail must not be shown. The readable reason still travels in the
    exception's ``extra`` so it reaches the log without reaching the screen.
    """
    raise HTTPException(
        status_code=500,
        detail=(
            "The server could not build a valid response for that request. "
            "Please try again, and report this if it keeps happening."
        ),
        extra={"schema": model.__name__, "reason": validation_error_detail(exc, model)},
    ) from exc


# --------------------------------------------------------------------------
# Source conversion
# --------------------------------------------------------------------------

def form_to_dict(
    form: Any,
    multi_fields: Sequence[str] = (),
) -> dict[str, Any]:
    """
    Flatten a Litestar ``FormMultiDict`` (or any mapping) into a plain dict.

    ``multi_fields`` are read with ``getall`` so every value of a repeated key
    survives. Reading a multi-select with a plain ``get`` returns the first value
    only, which is how a query built against four tables silently becomes a query
    against one — hence naming them explicitly rather than guessing from the
    annotation.

    Upload objects are dropped. A file is not something a schema can validate
    (see ``app.utils.file_utils.read_upload_payloads``), and leaving it in would
    fail the field's type check for reasons that have nothing to do with the user.
    """
    multi = set(multi_fields)
    data: dict[str, Any] = {}

    keys = form.keys() if hasattr(form, "keys") else ()

    for key in keys:
        name = str(key)

        if name in multi:
            if hasattr(form, "getall"):
                values = list(form.getall(name, []))
            else:
                raw = form.get(name)
                values = list(raw) if isinstance(raw, (list, tuple)) else [raw]
            data[name] = [v for v in values if not _is_upload(v)]
            continue

        value = form.get(name)
        if _is_upload(value):
            continue
        data[name] = value

    # A repeated key the caller declared but the form did not send at all must
    # still arrive as an empty list, not be missing — "nothing selected" is a
    # valid state for a multi-select and should not read as "field absent".
    for name in multi:
        data.setdefault(name, [])

    return data


def _is_upload(value: Any) -> bool:
    """A multipart file part, as opposed to a scalar form value."""
    return hasattr(value, "filename") and hasattr(value, "read")


# --------------------------------------------------------------------------
# Base classes
# --------------------------------------------------------------------------

class AppBaseSchema(BaseModel):
    """
    Common configuration for every schema in the application.

    ``str_strip_whitespace`` is on globally because every text field in this
    application is trimmed before it is stored or compared — doing it here means
    no validator has to remember to, and a value that is only whitespace reaches
    the required-check as empty rather than as a one-space string that passes it.
    """

    model_config = ConfigDict(
        str_strip_whitespace=True,
        populate_by_name=True,
        validate_default=True,
    )

    @classmethod
    def parse(cls: Type[SchemaT], data: Mapping[str, Any]) -> SchemaT:
        """
        Validate a plain mapping, raising the project's 400 on failure.

        The entry point every other constructor funnels through, and the one
        tests use directly — a schema can be exercised without building a
        request.
        """
        try:
            return cls.model_validate(dict(data))
        except ValidationError as exc:
            raise_request_error(exc, cls)

    def payload(self) -> dict[str, Any]:
        """
        JSON-ready dict of this schema.

        ``mode="json"`` is deliberate: it renders ``UUID`` and ``datetime`` as
        strings, which is what a browser needs and what the hand-built dicts this
        layer replaced were doing by accident.
        """
        return self.model_dump(mode="json")


class RequestSchema(AppBaseSchema):
    """
    Base for anything read off an incoming request.

    ``extra="ignore"`` rather than ``"forbid"``: the HTMX forms in this
    application carry fields no single handler cares about — the CSRF token, the
    page's current filter, the hidden state a partial needs to rebuild itself —
    and rejecting a request for carrying one of those would break every form on
    the site. Anything a handler *does* care about is a declared field, so
    ignoring the rest loses nothing.
    """

    model_config = ConfigDict(extra="ignore")


class FormRequest(RequestSchema):
    """
    A payload posted as an HTML form (``application/x-www-form-urlencoded`` or
    ``multipart/form-data``).

    Subclasses list repeated keys in ``multi_fields``; everything else is read as
    a single value.
    """

    multi_fields: ClassVar[tuple[str, ...]] = ()

    @classmethod
    async def from_form(
        cls: Type[SchemaT],
        request: Request,
        **overrides: Any,
    ) -> SchemaT:
        """
        Read and validate the request's form body.

        ``overrides`` are applied last, for the values that come from the URL
        rather than the body — a path parameter the schema also wants to hold.
        Litestar caches the parsed form on the request, so a handler that also
        needs the uploaded files (via ``app.utils.file_utils``) does not re-parse
        the body.
        """
        form = await request.form()
        return cls.from_form_data(form, **overrides)

    @classmethod
    def from_form_data(
        cls: Type[SchemaT],
        form: Any,
        **overrides: Any,
    ) -> SchemaT:
        """Validate an already-parsed form. Split out so tests can call it."""
        data = form_to_dict(form, getattr(cls, "multi_fields", ()))
        data.update(overrides)
        return cls.parse(data)


class JsonRequest(RequestSchema):
    """A payload posted as a JSON body."""

    #: Shown when the body is not JSON at all. Overridden per schema where the
    #: surrounding feature already has its own wording for that case.
    invalid_body_message: ClassVar[str] = (
        "That request body could not be read. Please try again."
    )

    @classmethod
    async def from_json(
        cls: Type[SchemaT],
        request: Request,
        **overrides: Any,
    ) -> SchemaT:
        """
        Read and validate the request's JSON body.

        A body that isn't JSON is a 400 with the schema's own message, not the
        ``AttributeError`` that ``(body or {}).get(...)`` produced when the body
        parsed to a list or a string.
        """
        try:
            body = await request.json()
        except Exception as exc:  # noqa: BLE001 — any parse failure is one 400
            raise HTTPException(
                status_code=400, detail=cls.invalid_body_message
            ) from exc

        if not isinstance(body, Mapping):
            raise HTTPException(status_code=400, detail=cls.invalid_body_message)

        data = dict(body)
        data.update(overrides)
        return cls.parse(data)


class QueryRequest(RequestSchema):
    """
    Filters and options read off the query string.

    Every field must have a default: a query string is nearly always partial, and
    a missing filter means "unfiltered", not "bad request".
    """

    multi_fields: ClassVar[tuple[str, ...]] = ()

    @classmethod
    def from_query(
        cls: Type[SchemaT],
        request: Request,
        **overrides: Any,
    ) -> SchemaT:
        """Validate the request's query parameters."""
        data = form_to_dict(request.query_params, getattr(cls, "multi_fields", ()))
        data.update(overrides)
        return cls.parse(data)


class ResponseSchema(AppBaseSchema):
    """
    Base for anything sent back to a client.

    ``from_attributes`` lets a schema be built straight from an ORM row, so the
    response contract is declared once instead of being re-typed as a dict
    literal at each place that returns it.
    """

    model_config = ConfigDict(from_attributes=True, extra="ignore")

    @classmethod
    def build(cls: Type[SchemaT], source: Any) -> SchemaT:
        """Validate an ORM row / dict as this response, raising 500 on failure."""
        try:
            return cls.model_validate(source)
        except ValidationError as exc:
            raise_response_error(exc, cls)

    @classmethod
    def build_many(cls: Type[SchemaT], sources: Any) -> list[SchemaT]:
        """The list form of :meth:`build`."""
        return [cls.build(source) for source in (sources or ())]

    @classmethod
    def payload_for(cls, source: Any) -> dict[str, Any]:
        """Validate and serialize in one step, for a JSON handler."""
        return cls.build(source).payload()

    @classmethod
    def payload_for_many(cls, sources: Any) -> list[dict[str, Any]]:
        """Validate and serialize a collection, for a JSON handler."""
        return [item.payload() for item in cls.build_many(sources)]


# --------------------------------------------------------------------------
# Reusable field types
#
# Each one wraps a rule that already exists in app/utils/validators.py or in a
# service, so the schema and the older check reject the same input with the same
# sentence. Declaring them once here is what keeps that true as fields are added.
# --------------------------------------------------------------------------

def validate_edge_waypoints(
    edges: Any,
    max_waypoints: int = MAX_EDGE_WAYPOINTS,
) -> Any:
    """
    Bound the one key the canvas layer owns on a connector it has hand-routed.

    Every canvas save schema in this application deliberately allows extra keys:
    the drawing's shape belongs to the client, and pinning it here would mean two
    places to change for every node type. ``waypoints`` is the exception worth
    checking, and it is worth checking for one reason above the others.

    A payload of ``{"waypoints": [{"x": NaN, "y": 0}]}`` satisfies every rule this
    layer would otherwise apply. ``json.dumps`` then writes a bare ``NaN``, which
    **PostgreSQL's ``jsonb`` rejects** — so the save dies as a 500 with a stack
    trace and no sentence, which is exactly what this project's error contract
    exists to prevent. ``Infinity`` behaves the same way. Neither is reachable from
    the canvas; both are reachable from a hand-made request.

    The count cap matters for the same reason ``MAX_GRAPH_EDGES`` does: without it
    the new key is an unbounded document into a JSONB column, slipping past the
    caps that were added to stop precisely that.

    Anything that is not an edge object, or an edge with no ``waypoints``, is left
    exactly as it is — the vocabulary is still the service's to interpret.

    :param edges: the posted ``edges`` collection, whatever shape it arrived in
    :param max_waypoints: bends allowed per connector
    :returns: ``edges`` unchanged, when every bend on it is usable
    :raises ValueError: with a sentence a reader can act on
    """
    if not isinstance(edges, list):
        return edges

    for edge in edges:
        if not isinstance(edge, dict) or "waypoints" not in edge:
            continue

        waypoints = edge["waypoints"]
        if waypoints is None:
            continue
        if not isinstance(waypoints, list):
            raise ValueError(
                "A connector's bend points could not be read. Reload the canvas "
                "and try saving again."
            )
        if len(waypoints) > max_waypoints:
            raise ValueError(
                f"A connector cannot have more than {max_waypoints} bend points."
            )

        for point in waypoints:
            if not isinstance(point, dict) or "x" not in point or "y" not in point:
                raise ValueError("A connector's bend point is missing its position.")

            for value in (point["x"], point["y"]):
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ValueError(
                        "A connector's bend point is missing its position."
                    )
                # ``math.isfinite`` and not a comparison: NaN fails every
                # comparison silently, including the range check below.
                if not math.isfinite(value):
                    raise ValueError(
                        "A connector's bend point is not a valid position."
                    )
                if value < 0 or value > MAX_CANVAS_COORD:
                    raise ValueError(
                        "A connector's bend point is outside the canvas."
                    )

    return edges


def _blank_to_none(value: Any) -> Any:
    """An untouched form field and an unselected dropdown both mean "nothing"."""
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return value


def _coerce_bool(value: Any, info: ValidationInfo) -> Any:
    """
    Read an HTML control's boolean.

    An absent checkbox is ``False`` rather than a missing field, so a form that
    simply doesn't include the box behaves as "not ticked". A token we don't
    recognise is refused: it cannot have come from the form we rendered, and
    reading it as ``False`` would silently disable something the caller asked to
    enable.
    """
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        token = value.strip().lower()
        if token in _TRUE_TOKENS:
            return True
        if token in _FALSE_TOKENS:
            return False
        raise ValueError(f"{_field_label(info)} must be either on or off")
    return value


def _field_label(info: ValidationInfo) -> str:
    """
    The user-facing label for the field currently being validated.

    ``ValidationInfo`` exposes the field's *name*, not its ``title`` — so the name
    is prettified. That is why field names in this layer read as english
    (``tool_name``, not ``tname``): the name is part of the error message.
    """
    return _prettify(info.field_name or "")


def _identifier(value: Any, info: ValidationInfo) -> Any:
    """
    A lowercase identifier, via the shared validator.

    ``require_identifier`` raises ``HTTPException`` with the finished sentence —
    the same exception, with the same wording, that the services raise for the
    same input. It is allowed to propagate rather than being repackaged as a
    ``ValueError`` and rebuilt into an identical 400.
    """
    if value is None:
        return value
    return require_identifier(str(value), _field_label(info))


def _object_name(value: Any, info: ValidationInfo) -> Any:
    """A table / collection / column name in the user's own database."""
    if value is None:
        return value
    return require_object_name(str(value), _field_label(info))


def _json_object(value: Any, info: ValidationInfo) -> Any:
    """A hidden field carrying a JSON object, e.g. a built query config."""
    if isinstance(value, dict):
        return value
    if value is None:
        return {}
    return parse_json_object(str(value), _field_label(info))


def _json_array(value: Any, info: ValidationInfo) -> Any:
    """
    A hidden field carrying a JSON array.

    Blank means "nothing yet" and yields ``[]``. Unparseable, or parseable but not
    a list, is refused. The hand-rolled version this replaces swallowed both into
    ``[]`` — so a browser that posted a malformed list had the user's work
    discarded and was told the save succeeded.
    """
    if isinstance(value, list):
        return value
    raw = "" if value is None else str(value).strip()
    if not raw:
        return []
    label = _field_label(info)
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise ValueError(
            f"{label} could not be read — please rebuild it and try again"
        ) from exc
    if not isinstance(parsed, list):
        raise ValueError(f"{label} is not in the expected format")
    return parsed


def _uuid_or_none(value: Any) -> Any:
    """An optional dropdown selection, as a UUID."""
    cleaned = _blank_to_none(value)
    if cleaned is None or isinstance(cleaned, uuid_pkg.UUID):
        return cleaned
    return str(cleaned)


#: Trimmed text that must be present. Pair with ``Field(title=..., max_length=...)``.
RequiredText = Annotated[str, Field(min_length=1)]

#: Trimmed text where blank means "clear this value" rather than "empty string".
OptionalText = Annotated[Optional[str], BeforeValidator(_blank_to_none)]

#: A dropdown selection that must be made.
RequiredUUID = Annotated[uuid_pkg.UUID, BeforeValidator(_uuid_or_none)]

#: A dropdown selection that may be left blank, meaning "none".
OptionalUUID = Annotated[
    Optional[uuid_pkg.UUID], BeforeValidator(_uuid_or_none)
]

#: A checkbox or a toggle button's value.
CheckboxBool = Annotated[bool, BeforeValidator(_coerce_bool)]

#: A lowercase identifier used as a tool/datasource name.
IdentifierName = Annotated[str, BeforeValidator(_identifier)]

#: A table, collection or column name read from the user's own database.
ObjectName = Annotated[str, BeforeValidator(_object_name)]

#: A hidden form field holding a JSON object.
JsonObjectField = Annotated[dict, BeforeValidator(_json_object)]

#: A hidden form field holding a JSON array.
JsonArrayField = Annotated[list, BeforeValidator(_json_array)]
