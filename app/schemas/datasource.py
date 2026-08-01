"""
app/schemas/datasource.py

Pydantic DTOs for DataSource validation.

Responsibilities
----------------
- Strip and normalize `datasource_name` before it ever reaches the ORM.
- Enforce the character-set contract at the application layer so the DB
  unique index is the last line of defence, not the first.
- Provide separate Create / Update schemas so callers can selectively
  require fields.
"""

import re
from pydantic import BaseModel, field_validator

# Allowed pattern: lowercase letters, digits, underscores only.
_NAME_PATTERN = re.compile(r'^[a-z0-9_]+$')


def _normalize_datasource_name(v: str) -> str:
    """
    Shared normalization + validation logic reused by both schemas.

    Transformations applied (in order):
      1. Reject anything that is not a string.
      2. Strip leading / trailing whitespace.
      3. Convert to lowercase.
      4. Validate the result is non-empty, within the 255-char limit,
         and matches the allowed character set.

    Raises:
        ValueError: With a human-readable message on any violation.
    """
    # Runs under mode="before", so `v` is whatever the caller sent — a JSON body
    # can put null, a number or a list here. Calling .strip() on those raises
    # AttributeError, which Pydantic does NOT wrap into a ValidationError, so it
    # escaped the schema entirely and reached the user as
    # "'NoneType' object has no attribute 'strip'". Raising ValueError instead
    # keeps every failure inside the ValidationError contract callers expect.
    if not isinstance(v, str):
        raise ValueError("datasource_name must be text")

    v = v.strip().lower()

    if not v:
        raise ValueError("datasource_name cannot be empty")

    if len(v) > 255:
        raise ValueError("datasource_name exceeds the maximum length of 255 characters")

    if not _NAME_PATTERN.match(v):
        raise ValueError(
            "datasource_name may only contain lowercase letters (a-z), "
            "digits (0-9), and underscores (_)"
        )

    return v


class DatasourceCreateSchema(BaseModel):
    """DTO used when creating a new DataSource.  datasource_name is required."""

    datasource_name: str

    @field_validator("datasource_name", mode="before")
    @classmethod
    def validate_and_normalize(cls, v: str) -> str:
        return _normalize_datasource_name(v)


class DatasourceUpdateSchema(BaseModel):
    """DTO used when renaming an existing DataSource.  datasource_name is required."""

    datasource_name: str

    @field_validator("datasource_name", mode="before")
    @classmethod
    def validate_and_normalize(cls, v: str) -> str:
        return _normalize_datasource_name(v)
