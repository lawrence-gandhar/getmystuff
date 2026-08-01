"""
Shared input validation.

These are the checks the Workspaces, Data Agents and Tool Configs modules all
need, kept in one place so the same input is held to the same rule and — just as
importantly — rejected with the same wording everywhere. Every message here is
written to be shown to the user verbatim; nothing leaks a column name or a stack
trace.

Frontend validation (``required``, ``maxlength``, ``pattern``) mirrors these rules
in the templates. These functions are the enforcement — the forms are only the
courtesy.
"""

import json
import re
import uuid as uuid_pkg
from typing import Optional

from litestar.exceptions import HTTPException

# A tool name is passed to the model as a tool identifier and interpolated into
# SQL identifiers downstream, so it is restricted to a plain lowercase identifier
# — the same rule the Configurations page applies (_TOOL_NAME_RE there).
_IDENTIFIER_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")

# A table or column name in the *user's own* database. It is not a value we can
# parameterise — it ends up as an identifier in a generated query — so it is
# restricted to characters that cannot break out of one. Dots, spaces and dashes
# are allowed because file-based datasources have object names like
# "sales_data.csv".
#
# The pattern spells out [A-Za-z0-9_] rather than using \w on purpose: \w matches
# Unicode letters, which would let through homoglyphs and combining characters that
# have no business in an identifier being interpolated into a query.
_OBJECT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_ .\-]*$")


def require_text(value: Optional[str], field_label: str, max_length: int) -> str:
    """Trim and require a non-empty value no longer than ``max_length``."""
    text_value = (value or "").strip()

    if not text_value:
        raise HTTPException(status_code=400, detail=f"{field_label} is required")

    if len(text_value) > max_length:
        raise HTTPException(
            status_code=400,
            detail=f"{field_label} cannot be longer than {max_length} characters",
        )

    return text_value


def optional_text(
    value: Optional[str],
    field_label: str,
    max_length: int,
) -> Optional[str]:
    """
    Trim an optional value, returning ``None`` when blank so an empty textarea
    clears the column instead of storing an empty string.
    """
    text_value = (value or "").strip()

    if not text_value:
        return None

    if len(text_value) > max_length:
        raise HTTPException(
            status_code=400,
            detail=f"{field_label} cannot be longer than {max_length} characters",
        )

    return text_value


def require_identifier(value: Optional[str], field_label: str) -> str:
    """
    Validate a name that is used as an identifier and return it lowercased.

    Casing is normalised on the way in rather than only compared
    case-insensitively, because the value is handed onwards as an identifier and
    mixed casing there is a needless source of confusion.
    """
    identifier = require_text(value, field_label, 255).lower()

    if not _IDENTIFIER_PATTERN.match(identifier):
        raise HTTPException(
            status_code=400,
            detail=(
                f"{field_label} must start with a letter and contain only "
                "lowercase letters, numbers and underscores — for example "
                "'total_units'"
            ),
        )

    return identifier


def require_object_name(value: Optional[str], field_label: str) -> str:
    """
    Require a table or column name that cannot break out of an identifier.

    These names are chosen from live dropdowns read off the user's own database, so
    a rejection here means the form was bypassed — but they are interpolated into a
    generated query rather than bound as parameters, so they are checked on the way
    in regardless.
    """
    name = str(value or "").strip()

    if not name:
        raise HTTPException(status_code=400, detail=f"{field_label} is required")

    if len(name) > 255:
        raise HTTPException(
            status_code=400,
            detail=f"{field_label} cannot be longer than 255 characters",
        )

    if not _OBJECT_NAME_PATTERN.match(name):
        raise HTTPException(
            status_code=400,
            detail=f"{field_label} '{name}' is not a valid name",
        )

    return name


def require_uuid(raw: Optional[str], field_label: str) -> uuid_pkg.UUID:
    """Require a dropdown selection and parse it as a UUID."""
    selection = parse_optional_uuid(raw, field_label)

    if selection is None:
        raise HTTPException(status_code=400, detail=f"{field_label} is required")

    return selection


def parse_optional_uuid(
    raw: Optional[str],
    field_label: str,
) -> Optional[uuid_pkg.UUID]:
    """
    Turn an optional dropdown value into a UUID.

    An unselected ``<option value="">`` arrives as an empty string and means
    "nothing chosen" (``None``). Anything present but unparseable is rejected here
    rather than passed on to a query that would fail deeper down with an
    unreadable database error.
    """
    raw_value = (raw or "").strip()

    if not raw_value:
        return None

    try:
        return uuid_pkg.UUID(raw_value)
    except (ValueError, AttributeError, TypeError) as exc:
        raise HTTPException(
            status_code=400,
            detail=f"{field_label} is not a valid selection",
        ) from exc


def parse_json_object(raw: Optional[str], field_label: str) -> dict:
    """
    Parse a hidden JSON form field into a dict.

    Blank means "nothing configured yet" and yields ``{}``. Anything that isn't a
    JSON *object* is a user mistake with a fixable message, never a 500.
    """
    raw_value = (raw or "").strip()

    if not raw_value:
        return {}

    try:
        parsed = json.loads(raw_value)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"{field_label} could not be read — please rebuild the query below",
        ) from exc

    if not isinstance(parsed, dict):
        raise HTTPException(
            status_code=400,
            detail=f"{field_label} is not in the expected format",
        )

    return parsed
