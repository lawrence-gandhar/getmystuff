"""
The transforms a field mapping may apply — a fixed table of named functions.

**Not expressions.** Not a mini-language, not a sandboxed ``eval``, not a template
engine. The same posture ``node_runners._condition_holds`` and
``mapping/paths.py`` take, for the same reason: this input is authored by a user
through a form and, in Phase 1, sometimes by a language model. Anything with an
evaluator in it is a remote code execution waiting for the first person who pastes
something clever into a text box, and every "safe" expression evaluator this codebase
could reach for has had a sandbox escape.

A closed table costs the user flexibility they will occasionally miss. What they get
back is that the palette can enumerate exactly what is available, the validator can
refuse an unknown name at save time rather than at three in the morning, and the AI
prompt can list the whole vocabulary in forty lines.

**Two rules the whole table obeys.**

*``None`` passes through untouched.* A transform is not where required-ness is
decided — that rule runs first, in ``mapping/record_validation.py``. Uppercasing a
field that was simply not sent should not turn an absent value into ``"NONE"``, and it
should not raise either.

*Nothing is guessed.* A transform that cannot do its job to a value raises, and the
record is recorded as failed with the reason. It never falls back to the original value
and never substitutes a default. ``"abc"`` through ``to_number`` is a refusal; a record
written into somebody's CRM with a silently-zeroed amount is a wrong record with nothing
in the log to find it by, which is strictly worse than one that failed. This is the same
argument ``app/utils/type_coercion.py`` makes, and the type transforms below delegate to
it rather than re-implementing it.
"""

import json
import re
from typing import Any, Callable, Dict, List, Tuple

from app.utils import type_coercion

_WHITESPACE = re.compile(r"\s+")
_NON_DIGIT = re.compile(r"\D+")


def _text(value: Any) -> str:
    """
    The string form a text transform operates on.

    Goes through ``type_coercion.coerce_value(..., "string")`` rather than ``str()`` so
    that a dict becomes JSON rather than a Python repr — ``str({"a": True})`` is
    ``{'a': True}``, which is neither JSON nor anything an API accepts, and a mapping
    that produced it would fail somewhere much further downstream.
    """
    return type_coercion.coerce_value(value, "string")


def _trim(value: Any) -> str:
    return _text(value).strip()


def _collapse_whitespace(value: Any) -> str:
    return _WHITESPACE.sub(" ", _text(value)).strip()


def _digits_only(value: Any) -> str:
    """
    Every non-digit removed.

    For phone numbers, which arrive as ``+44 (0)20 7946 0958`` from one system and are
    refused as anything but digits by the next. Deliberately not a phone *parser*: this
    module has no idea which country the number is from, and a parser that guesses is a
    parser that silently drops a leading zero.
    """
    return _NON_DIGIT.sub("", _text(value))


def _json_encode(value: Any) -> str:
    return json.dumps(value, default=str, separators=(",", ":"))


def _json_decode(value: Any) -> Any:
    return type_coercion.coerce_value(value, "json")


def _first(value: Any) -> Any:
    """
    The first element of a list, or the value itself if it is not one.

    For the shape ``a[*].email`` produces when a record has exactly one of something —
    a list of one, where the destination wants a scalar. Returns ``None`` for an empty
    list rather than raising, because "this record had no addresses" is a fact about the
    record, not a fault in the mapping.
    """
    if isinstance(value, (list, tuple)):
        return value[0] if value else None
    return value


def _last(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        return value[-1] if value else None
    return value


def _coerce(target: str) -> Callable[[Any], Any]:
    def _run(value: Any) -> Any:
        return type_coercion.coerce_value(value, target)

    return _run


# name -> (function, label, what it does — the sentence the picker and the AI prompt
# both show). One table, so the palette cannot offer a transform the runner does not
# have and the model cannot be told about one that does not exist.
TRANSFORMS: Dict[str, Tuple[Callable[[Any], Any], str, str]] = {
    "trim": (_trim, "Trim", "Remove leading and trailing whitespace"),
    "collapse_whitespace": (
        _collapse_whitespace,
        "Collapse whitespace",
        "Replace every run of whitespace with a single space, and trim",
    ),
    "lower": (lambda v: _text(v).lower(), "Lowercase", "Convert to lower case"),
    "upper": (lambda v: _text(v).upper(), "Uppercase", "Convert to upper case"),
    "title": (lambda v: _text(v).title(), "Title case", "Capitalise each word"),
    "digits_only": (
        _digits_only,
        "Digits only",
        "Remove everything that is not a digit — for phone numbers and reference codes",
    ),
    "to_string": (_coerce("string"), "As text", "Convert to text"),
    "to_number": (_coerce("number"), "As a number", "Convert to a number, refusing anything that is not one"),
    "to_integer": (
        _coerce("integer"),
        "As a whole number",
        "Convert to a whole number, refusing a fraction rather than truncating it",
    ),
    "to_boolean": (_coerce("boolean"), "As true/false", "Convert to true or false"),
    "to_date": (_coerce("date"), "As a date", "Convert to an ISO date (YYYY-MM-DD)"),
    "to_datetime": (
        _coerce("datetime"),
        "As a date and time",
        "Convert to an ISO date and time",
    ),
    "json_encode": (_json_encode, "As JSON text", "Serialise the value to JSON text"),
    "json_decode": (
        _json_decode,
        "Parse JSON",
        "Parse JSON text into an object or array",
    ),
    "first": (_first, "First item", "Take the first item of a list"),
    "last": (_last, "Last item", "Take the last item of a list"),
}

TRANSFORM_NAMES: Tuple[str, ...] = tuple(sorted(TRANSFORMS))


def is_known(name: str) -> bool:
    """Whether a transform exists. Used by ``validate_flow`` at save time."""
    return name in TRANSFORMS


def describe_transforms() -> List[Dict[str, str]]:
    """
    The whole table, for ``/integrations/vocabulary`` and the AI prompt renderer.

    Built from ``TRANSFORMS`` rather than written out separately, so a transform added
    below appears in the picker and in the model's instructions without either being
    edited.
    """
    return [
        {"name": name, "label": label, "description": description}
        for name, (_, label, description) in sorted(TRANSFORMS.items())
    ]


def apply_transform(name: str, value: Any) -> Any:
    """
    Run one named transform.

    An unknown name raises with the alternatives listed — the message shape
    ``frame_ops._require_column`` uses, because "unknown transform 'uppercase'" is
    considerably less useful than the same sentence ending with "did you mean upper?".
    Mirrored rather than imported: importing that helper would drag polars and a
    deep-agents exception into a module whose whole value is having no dependencies.
    """
    entry = TRANSFORMS.get(name)
    if entry is None:
        raise ValueError(
            f"There is no transform called '{name}'. The available transforms are: "
            + ", ".join(TRANSFORM_NAMES)
        )

    # See the module docstring: absent is not malformed.
    if value is None:
        return None

    function = entry[0]
    try:
        return function(value)
    except ValueError:
        # type_coercion's own sentences already name the value and the target type,
        # and they are better than anything this layer could compose.
        raise
    except Exception as exc:  # noqa: BLE001 — a transform must not leak a raw traceback
        raise ValueError(
            f"The '{name}' transform could not be applied to this value: {exc}"
        ) from exc


def apply_all(names: Any, value: Any) -> Any:
    """
    Run a chain of transforms left to right.

    A list because "trim, then lowercase" is the ordinary case and making the user pick
    one would push them toward asking for expressions. Order matters and is the user's:
    ``digits_only`` then ``to_integer`` works, and the reverse does not.
    """
    result = value
    for name in names or ():
        result = apply_transform(str(name), result)
    return result
