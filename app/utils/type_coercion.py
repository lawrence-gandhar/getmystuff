"""
Turning a piece of text into the value a declared type promised.

Two callers want this and they want different halves of it. A chatbot action has
parameters a language model fills in as strings, and needs the text back in two
escaped forms — one for a URL, one as a JSON fragment. Integration field mapping
has values arriving from somebody else's API, already typed as JSON, and needs a
real Python value to put in a request body.

Both are the same question — *does this value honour the type its field declared?* —
and answering it twice would produce two vocabularies of type names and two sets of
refusal messages for the identical mistake.

**Refusals are ``ValueError``, not ``HTTPException``.** A coercion failure is a
fact about a value, and the two callers turn it into different things: the chatbot
folds it into a sentence the model reads back, an integration marks the record
invalid and carries on with the batch. Same rule, message next to the rule, the
exception type left to whoever asked — ``utils/datasource_status.py``'s doctrine.
"""

import json
from datetime import date, datetime
from typing import Any, Tuple

# The vocabulary. `string`, `number` and `boolean` are what
# `ACTION_PARAMETER_TYPES` already offers and must keep meaning exactly what they
# meant; the rest exist for field mapping, where a target API distinguishes an
# integer id from a float amount and a date from a timestamp.
TYPES: Tuple[str, ...] = (
    "string",
    "number",
    "integer",
    "boolean",
    "date",
    "datetime",
    "json",
)

_TRUE = frozenset({"true", "1", "yes", "y", "on"})
_FALSE = frozenset({"false", "0", "no", "n", "off"})


def coerce_value(value: Any, target_type: str) -> Any:
    """
    Return ``value`` as ``target_type``, or raise ``ValueError`` saying why not.

    ``None`` passes through untouched at every type. "Absent" is not "malformed",
    and a required-field check is a separate rule that runs before this one —
    conflating them means an optional field that was simply not sent gets reported
    as a type error.

    **Nothing here guesses.** ``"abc"`` for a number is a refusal, never ``0``;
    ``""`` for an integer is a refusal, never ``None``. A silently defaulted value
    is a wrong record written to somebody's CRM with nothing in the log to find.
    """
    if target_type not in TYPES:
        raise ValueError(f"unknown type {target_type!r} — expected one of {', '.join(TYPES)}")

    if value is None:
        return None

    if target_type == "string":
        return value if isinstance(value, str) else _stringify(value)

    if target_type == "number":
        return _to_float(value)

    if target_type == "integer":
        return _to_int(value)

    if target_type == "boolean":
        return _to_bool(value)

    if target_type == "date":
        return _to_datetime(value).date().isoformat()

    if target_type == "datetime":
        return _to_datetime(value).isoformat()

    return _to_json(value)


def coerce_to_url_and_body(value: str, target_type: str) -> Tuple[str, str]:
    """
    The chatbot-action form: ``(url_text, body_literal)``.

    A string is JSON-escaped **without** its surrounding quotes, so a template
    writes ``"{{param.id}}"`` with quotes for a string and ``{{param.qty}}`` bare
    for a number or boolean — the ordinary JSON-template convention, and the
    behaviour ``chatbot_action_service._coerce_param`` has always had.
    """
    text = (value or "").strip()

    if target_type == "number":
        try:
            float(text)
        except ValueError:
            raise ValueError(f"expected a number but the AI supplied {text!r}")
        return text, text

    if target_type == "boolean":
        lowered = text.lower()
        if lowered not in ("true", "false"):
            raise ValueError(f"expected true or false but the AI supplied {text!r}")
        return lowered, lowered

    return text, json.dumps(text)[1:-1]


def describe_type(target_type: str) -> str:
    """A human phrase for a type, for a refusal message or a form hint."""
    return {
        "string": "text",
        "number": "a number",
        "integer": "a whole number",
        "boolean": "true or false",
        "date": "a date (YYYY-MM-DD)",
        "datetime": "a date and time (ISO 8601)",
        "json": "a JSON object or array",
    }.get(target_type, target_type)


# --------------------------------------------------------------------------
# The individual conversions
# --------------------------------------------------------------------------

def _stringify(value: Any) -> str:
    # A dict or list rendered with str() gives Python's repr — single quotes,
    # True instead of true — which is not JSON and not what any API wants.
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _to_float(value: Any) -> float:
    if isinstance(value, bool):
        # bool is a subclass of int, so float(True) is 1.0. Almost certainly a
        # mapping mistake rather than an intent, so it is refused rather than
        # quietly turned into a quantity of 1.
        raise ValueError(f"expected {describe_type('number')} but got true/false")
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError(f"expected {describe_type('number')} but got {value!r}")


def _to_int(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError(f"expected {describe_type('integer')} but got true/false")
    if isinstance(value, int):
        return value
    number = _to_float(value)
    if number != int(number):
        # Truncating 10.5 to 10 loses half a unit of whatever this is, silently.
        raise ValueError(f"expected {describe_type('integer')} but got {value!r}")
    return int(number)


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    lowered = str(value).strip().lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise ValueError(f"expected {describe_type('boolean')} but got {value!r}")


def _to_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)

    text = str(value).strip()
    if not text:
        raise ValueError(f"expected {describe_type('datetime')} but got an empty value")

    # "Z" is legal ISO 8601 and fromisoformat did not accept it before 3.11.
    normalised = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        return datetime.fromisoformat(normalised)
    except ValueError:
        raise ValueError(f"expected {describe_type('datetime')} but got {value!r}")


def _to_json(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError):
        raise ValueError(f"expected {describe_type('json')} but got {value!r}")
    if not isinstance(parsed, (dict, list)):
        raise ValueError(f"expected {describe_type('json')} but got {value!r}")
    return parsed
