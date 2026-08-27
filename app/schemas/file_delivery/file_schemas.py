"""
Schemas for the file blocks — app/schemas/file_delivery/.

Two payloads cross a boundary in this feature and both are declared here rather than
assembled as dict literals where they are returned.

``GeneratedFileView`` is one file as a JSON caller sees it — the shape a future status
endpoint and any JSON response would use, and the one place the ``uuid``-not-``id`` rule
for this table is enforced.

``FileButtonView`` is the download button as the **widget** reads it, and it is the one
worth reading twice. Its ``colour`` goes into an inline ``style`` attribute on a page this
application does not own, so the validator here is a security boundary rather than tidiness:
``#rrggbb`` or nothing. It is the third gate on that value — the canvas validator refuses a
bad one at save, the runner re-checks it at run time against
``flow_builder_runner.COLOUR_PATTERN``, and this refuses to build a payload with one. Three,
because the value is operator-authored, the target is a style attribute, and the cost of
being wrong is landed on every visitor of somebody else's site.
"""

from __future__ import annotations

import re
import uuid as uuid_pkg
from datetime import datetime
from typing import Any, Optional

from pydantic import Field, field_validator

from app.models.file_delivery import FILE_FORMAT_VALUES
from app.schemas.base import ResponseSchema

# The one colour pattern. Mirrors ``flow_builder_runner.COLOUR_PATTERN`` rather than
# importing it, the way ``ai_analytics_schemas.TARGET_TYPES`` mirrors its service's set: the
# schema layer does not import services. The two are covered by one test asserting they
# accept the same strings, which is what stops a mirror becoming a divergence.
_COLOUR_PATTERN = re.compile(r"#[0-9a-fA-F]{6}")


class GeneratedFileView(ResponseSchema):
    """
    One file a block made, as a JSON caller sees it.

    ``download_url`` is not a column: the same file is reachable at two different prefixes
    depending on who is asking — the owner's authenticated route or the visitor's public
    one — so the caller supplies it and this only carries it. The same arrangement
    ``DownloadExportView`` makes, and for the same reason.
    """

    uuid: uuid_pkg.UUID = Field(title="File")
    status: str = Field(title="Status")
    file_format: str = Field(title="File format")
    file_name: str = Field(title="File name")

    row_count: int = Field(default=0, title="Rows written")
    byte_size: int = Field(default=0, title="File size")

    created_at: Optional[datetime] = Field(default=None, title="Created")
    expires_at: Optional[datetime] = Field(default=None, title="Available until")

    download_url: Optional[str] = Field(default=None, title="Download")

    @classmethod
    def of(cls, record: Any, download_url: Optional[str] = None) -> "GeneratedFileView":
        """Build the view for one ``GeneratedFile`` row."""
        return cls.build(
            {
                "uuid": record.uuid,
                "status": record.status,
                "file_format": record.file_format,
                "file_name": record.file_name,
                "row_count": record.row_count,
                "byte_size": record.byte_size,
                "created_at": record.created_at,
                "expires_at": record.expires_at,
                "download_url": download_url,
            }
        )


class FileButtonView(ResponseSchema):
    """
    The download button one turn is offering, as the widget reads it.

    Present on a turn only when the operator ticked *show a download button* — the field
    is absent on every other turn, which is nearly all of them, and the widget draws
    nothing when it is absent.
    """

    url: str = Field(title="Link", max_length=2048)
    label: str = Field(title="Button text", max_length=120)
    colour: str = Field(title="Button colour", max_length=7)
    file_name: str = Field(title="File name", max_length=255)
    file_format: str = Field(title="File format", max_length=16)
    byte_size: int = Field(default=0, title="File size")

    @field_validator("colour")
    @classmethod
    def _colour_is_hex(cls, value: str) -> str:
        """
        A hex colour, or the payload is refused.

        Refused rather than defaulted, unlike the runner's ``_safe_colour``: by the time a
        value reaches this schema it has already passed the save-time validator and the
        runner, so anything wrong here is a bug in this application rather than a mistake
        an operator made, and quietly substituting blue would hide it.
        """
        if not _COLOUR_PATTERN.fullmatch(value or ""):
            raise ValueError("A button colour must be a hex colour such as #0d6efd")

        return value

    @field_validator("file_format")
    @classmethod
    def _known_format(cls, value: str) -> str:
        """One of the formats this application writes."""
        if value not in FILE_FORMAT_VALUES:
            raise ValueError(f"{value!r} is not a file format this application writes")

        return value
