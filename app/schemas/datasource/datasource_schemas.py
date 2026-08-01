"""
app/schemas/datasource/datasource_schemas.py

Pydantic schemas for the Datasource module — connections, uploaded files, table
and column status, and the Configurations page's tool base config.

Responsibilities
----------------
- Strip and normalize `datasource_name` before it ever reaches the ORM.
- Enforce the character-set contract at the application layer so the DB
  unique index is the last line of defence, not the first.
- Provide separate Create / Update schemas so callers can selectively
  require fields.
- Parse every form, query string and JSON body this module's routes accept, so
  nothing reaches `datasource_service` as an unvalidated string.

``DatasourceCreateSchema`` / ``DatasourceUpdateSchema`` are the original pair and
are still what ``datasource_service`` validates the name with. They deliberately
subclass ``BaseModel`` rather than the request bases: the service catches
``ValidationError`` from them directly, and the request schemas below reuse their
normalizer so there is exactly one definition of what a datasource name may be.
"""

import re
from typing import Optional

from pydantic import BaseModel, Field, field_validator

from app.schemas.base import (
    MAX_NAME_LENGTH,
    CheckboxBool,
    FormRequest,
    IdentifierName,
    JsonArrayField,
    JsonObjectField,
    ObjectName,
    OptionalText,
    OptionalUUID,
    QueryRequest,
    RequiredText,
    ResponseSchema,
)
from app.utils.file_utils import FILE_BASED_TYPES

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


# --------------------------------------------------------------------------
# Vocabularies
#
# Spelled out rather than left as free text because both values are branched on
# throughout the module: db_type decides whether a connection is tested or files
# are ingested, and status decides whether a table is offered to the AI at all.
# A value outside these sets means the form was bypassed.
# --------------------------------------------------------------------------

#: Engines reachable over a connection string.
CONNECTION_DB_TYPES: frozenset[str] = frozenset(
    {"postgres", "mysql", "mongodb", "sqlite", "oracle"}
)

#: Every db_type the create form offers — connection engines plus file formats.
#: Sourced from FILE_BASED_TYPES so adding a file format in one place is enough.
ALL_DB_TYPES: frozenset[str] = CONNECTION_DB_TYPES | FILE_BASED_TYPES

#: The two states a table or column can be in.
OBJECT_STATUSES: frozenset[str] = frozenset({"active", "inactive"})

#: Values the table list's status filter accepts. "all" is not a status — it is
#: the absence of the filter — which is why it is listed here and not above.
STATUS_FILTERS: frozenset[str] = OBJECT_STATUSES | {"all"}

#: Sort directions the table list accepts.
SORT_ORDERS: frozenset[str] = frozenset({"az", "za"})

_MAX_PREVIEW_PAGE = 100_000


# --------------------------------------------------------------------------
# Requests
# --------------------------------------------------------------------------

class DatasourceCreateRequest(FormRequest):
    """
    The create-datasource form.

    Connection fields are optional *here* on purpose. Whether a host and a
    database name are required depends on ``db_type`` — a CSV datasource has
    neither — and that rule already lives in ``create_datasource``, which also
    owns the reachability test. The schema's job is the part that can be decided
    without touching anything: the name's character set, the engine being one we
    support, and the port being a port.
    """

    datasource_name: str = Field(title="Datasource name")
    db_type: RequiredText = Field(title="Database type")
    host: OptionalText = Field(default=None, title="Host", max_length=MAX_NAME_LENGTH)
    port: OptionalText = Field(default=None, title="Port", max_length=10)
    database_name: OptionalText = Field(
        default=None, title="Database name", max_length=MAX_NAME_LENGTH
    )
    username: OptionalText = Field(
        default=None, title="Username", max_length=MAX_NAME_LENGTH
    )
    password: OptionalText = Field(
        default=None, title="Password", max_length=MAX_NAME_LENGTH
    )

    @field_validator("datasource_name", mode="before")
    @classmethod
    def validate_and_normalize(cls, v: str) -> str:
        return _normalize_datasource_name(v)

    @field_validator("db_type")
    @classmethod
    def validate_db_type(cls, v: str) -> str:
        if v not in ALL_DB_TYPES:
            raise ValueError(
                "Database type is not one we support. Please pick one from the list."
            )
        return v

    @field_validator("port")
    @classmethod
    def validate_port(cls, v: Optional[str]) -> Optional[str]:
        """
        A port is passed to the driver as a string, so it stays a string — but it
        has to *be* a number, or the connection attempt fails with a driver error
        no user can act on.
        """
        if v is None:
            return None
        if not v.isdigit() or not 1 <= int(v) <= 65535:
            raise ValueError("Port must be a number between 1 and 65535")
        return v

    @property
    def is_file_based(self) -> bool:
        """Whether this datasource ingests files instead of holding a connection."""
        return self.db_type in FILE_BASED_TYPES


class DatasourceNameRequest(FormRequest):
    """
    A datasource name on its own — the rename form and the blur-triggered
    availability check both post exactly this.
    """

    datasource_name: str = Field(title="Datasource name")

    @field_validator("datasource_name", mode="before")
    @classmethod
    def validate_and_normalize(cls, v: str) -> str:
        return _normalize_datasource_name(v)


class ObjectStatusRequest(FormRequest):
    """
    The status a table or column is being switched to.

    Replaces a bare ``if new_status not in {...}: raise HTTPException(400)`` that
    sent an empty 400 — the user saw a failed request with no explanation.
    """

    status: RequiredText = Field(title="Status")

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        if v not in OBJECT_STATUSES:
            raise ValueError("Status must be either active or inactive")
        return v


class TableListQuery(QueryRequest):
    """
    The table list's search / filter / sort controls.

    Every field has a default because the list is rendered before any of them are
    touched. ``search`` is lowercased here rather than in the route, so the
    service receives the same casing whichever caller it came from.
    """

    search: str = Field(default="", title="Search", max_length=MAX_NAME_LENGTH)
    status_filter: str = Field(default="all", title="Status filter")
    sort_by: str = Field(default="az", title="Sort order")

    @field_validator("search")
    @classmethod
    def normalize_search(cls, v: str) -> str:
        return v.lower()

    @field_validator("status_filter")
    @classmethod
    def validate_status_filter(cls, v: str) -> str:
        if v not in STATUS_FILTERS:
            raise ValueError(
                "Status filter must be all, active or inactive"
            )
        return v

    @field_validator("sort_by")
    @classmethod
    def validate_sort_by(cls, v: str) -> str:
        if v not in SORT_ORDERS:
            raise ValueError("Sort order must be either az or za")
        return v


class FileExistsRequest(FormRequest):
    """One filename, checked for a previous version before the upload starts."""

    filename: RequiredText = Field(title="Filename", max_length=MAX_NAME_LENGTH)


class FileUploadRequest(FormRequest):
    """
    The upload form's non-file fields.

    The files themselves are read with
    ``app.utils.file_utils.read_upload_payloads`` — a stream is not something a
    schema can validate, and its rules (extension, size) are enforced by
    ``file_service`` once the bytes are in hand.

    The widget posts ``override=yes``/``no``; ``CheckboxBool`` already accepts
    those tokens, so no per-field handling is needed here.
    """

    override: CheckboxBool = Field(default=False, title="Override existing files")


class FilePreviewQuery(QueryRequest):
    """
    Which page of an uploaded file to preview, and optionally which file.

    ``page`` was previously read with a bare ``int()`` inside a try/except that
    fell back to 1, so ``?page=abc`` silently showed page one. It is now a
    rejected request, because a page number that cannot be read is a broken link,
    not a default.
    """

    page: int = Field(default=1, ge=1, le=_MAX_PREVIEW_PAGE, title="Page")
    file_id: OptionalUUID = Field(default=None, title="File")


class ToolNameRequest(FormRequest):
    """The tool name posted by the Configurations page's blur validation."""

    tool_name: IdentifierName = Field(title="Tool name")


class ToolBaseConfigCreateRequest(FormRequest):
    """
    The Configurations page's Tool Base Config panel.

    ``subquery_configs`` used to be read with a ``json.loads`` wrapped in a
    try/except that fell back to ``[]``: a malformed payload discarded every
    subquery the user had built and reported success. It is now refused with a
    message that says to rebuild it.
    """

    tool_name: IdentifierName = Field(title="Tool name")
    table_name: ObjectName = Field(title="Table name")
    base_config: JsonObjectField = Field(default_factory=dict, title="Base config")
    subquery_configs: JsonArrayField = Field(
        default_factory=list, title="Subquery configs"
    )


# --------------------------------------------------------------------------
# Responses
# --------------------------------------------------------------------------

class DatasourceFileView(ResponseSchema):
    """
    One uploaded file, as the preview's file selector needs it.

    ``id`` carries the file's *public uuid*, not its bigint primary key — the key
    is named ``id`` because the preview script reads ``f.id`` when building the
    ``<option>`` list. No internal identifier is exposed.
    """

    id: str = Field(title="File")
    filename: str = Field(title="Filename")


class TableStatusView(ResponseSchema):
    """One row of the table list: the object's name and whether it is active."""

    table_name: str = Field(title="Table name")
    status: str = Field(title="Status")


class DatasourceDetailsResponse(ResponseSchema):
    """
    The JSON body of ``GET /datasource/{id}/details``.

    ``configuration_data`` is the saved per-table config, whose keys are the
    user's own table names — so it stays an open dict rather than being modelled
    field by field.
    """

    datasource_name: str = Field(title="Datasource name")
    objects: list = Field(default_factory=list, title="Objects")
    configuration_data: dict = Field(default_factory=dict, title="Configuration data")


class FilePreviewResponse(ResponseSchema):
    """
    One page of an uploaded file.

    Covers all three shapes the readers produce — a table (columns + rows), a
    formatted JSON/XML document (``content``), and an error — because the widget
    reads one payload and branches on ``type``. Modelling them as one schema with
    optional halves is what lets the endpoint declare a single return contract
    instead of returning whichever dict the reader happened to build.
    """

    type: str = Field(default="table", title="Preview type")
    file_id: Optional[str] = Field(default=None, title="File")
    filename: Optional[str] = Field(default=None, title="Filename")
    files: list[DatasourceFileView] = Field(default_factory=list, title="Files")
    columns: list[str] = Field(default_factory=list, title="Columns")
    rows: list[list] = Field(default_factory=list, title="Rows")
    content: Optional[str] = Field(default=None, title="Content")
    page: int = Field(default=1, title="Page")
    has_next: bool = Field(default=False, title="Has next page")
    error: Optional[str] = Field(default=None, title="Error")

    @classmethod
    def failure(cls, message: str) -> "FilePreviewResponse":
        """A preview that could not be produced, in the shape the widget reads."""
        return cls(error=message)


class FileUploadResultView(ResponseSchema):
    """
    What happened to one file in an upload batch.

    ``status`` is ``ok`` or ``error``; the other fields are only populated on the
    successful branch, which is why they default rather than being required.
    """

    status: str = Field(title="Status")
    original_filename: str = Field(title="Filename")
    stored_filename: Optional[str] = Field(default=None, title="Stored filename")
    version: Optional[int] = Field(default=None, title="Version")
    message: Optional[str] = Field(default=None, title="Message")


class FileExistsResponse(ResponseSchema):
    """Whether a previous version of one filename already exists."""

    exists: bool = Field(title="Exists")
    version: int = Field(default=0, title="Current version")

    @property
    def next_version(self) -> int:
        return self.version + 1
