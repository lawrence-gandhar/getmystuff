"""
Schemas for Downloader Agents — app/schemas/downloader_agents/.

Three kinds of payload cross a boundary in this feature, and all three are declared
here rather than assembled as dict literals at the point of return.

**Tool arguments are request schemas.** This is the part worth explaining. Every
other data tool in the application takes no arguments at all, deliberately — see
app/services/deep_agents/tool_factory, which refuses to let model-generated text near
a query. The two tools this feature adds *do* take arguments, because "which export"
and "which format" are choices rather than query text. That makes a model the source
of a request payload, and a model is exactly as untrusted as a browser: it
hallucinates uuids, invents formats and passes the word "latest". So the argument
schemas live in the schema layer with every other request, and the tools receive
validated values or a readable refusal.

LangChain reads a tool's ``args_schema`` as a plain Pydantic model, and
``AppBaseSchema`` is one — so these classes are handed to ``StructuredTool`` directly.
Their field descriptions are part of the prompt the model sees, which is why they are
written as instructions to a reader rather than as notes to a developer.

**Views and events are response schemas.** ``DownloadExportView`` is what a status
call returns; ``DownloadProgressEvent`` is one frame of the SSE progress stream. Both
expose ``uuid`` and never the bigint ``id``, and both are built through
``ResponseSchema`` so a malformed response is a 500 in the log rather than a broken
payload in the browser.
"""

from __future__ import annotations

import uuid as uuid_pkg
from datetime import datetime
from typing import Any, ClassVar, Optional

from pydantic import Field, field_validator

from app.models.downloader_agents import (
    EXPORT_FORMAT_VALUES,
    FORMAT_CSV,
)
from app.schemas.base import (
    AppBaseSchema,
    OptionalText,
    QueryRequest,
    ResponseSchema,
)

# What the model may pass instead of a uuid to mean "the offer you just made me".
# Accepted explicitly because it is what a model actually does when it did not keep
# the id, and because the alternative — refusing — turns a working "yes" into an
# apology. Matched case-insensitively after trimming.
LATEST_EXPORT_TOKENS = frozenset({"", "latest", "last", "previous", "recent", "none"})


# --------------------------------------------------------------------------
# Tool arguments
# --------------------------------------------------------------------------

class ConfirmDownloadArgs(AppBaseSchema):
    """
    Arguments for the ``confirm_download`` tool.

    Both fields have defaults, so a model that calls the tool with an empty object
    still produces a valid request: the newest open offer, as CSV. That is the
    common case — the user said "yes" and nothing else.
    """

    export_id: OptionalText = Field(
        default=None,
        title="Export",
        max_length=64,
        description=(
            "The export id from the offer you made, if you still have it. Leave it "
            "out or pass 'latest' to use the most recent offer in this conversation."
        ),
    )

    file_format: str = Field(
        default=FORMAT_CSV,
        title="File format",
        max_length=16,
        description=(
            "The file the user asked for: 'csv', 'xls' for an Excel workbook, or "
            "'parquet'. Use 'csv' unless they asked for something else."
        ),
    )

    @field_validator("file_format", mode="before")
    @classmethod
    def _known_format(cls, value: Any) -> str:
        """
        Accept the formats we write, and nothing else.

        A model asked for a download will happily propose 'excel', 'xlsx', 'txt' or
        'sheet'. The near-misses that unambiguously mean one of ours are mapped;
        anything else is refused by name, because silently writing a CSV for
        someone who asked for Parquet is worse than saying no.
        """
        token = str(value or FORMAT_CSV).strip().lower().lstrip(".")

        aliases = {
            "xlsx": "xls",
            "excel": "xls",
            "spreadsheet": "xls",
            "sheet": "xls",
            "comma-separated": FORMAT_CSV,
            "text": FORMAT_CSV,
            "txt": FORMAT_CSV,
            "pq": "parquet",
        }
        token = aliases.get(token, token)

        if token not in EXPORT_FORMAT_VALUES:
            allowed = ", ".join(sorted(EXPORT_FORMAT_VALUES))
            raise ValueError(
                f"File format must be one of: {allowed}. '{value}' is not a format "
                "this application can write."
            )

        return token

    def wants_latest(self) -> bool:
        """Whether ``export_id`` is a real id or a stand-in for "the last offer"."""
        return (self.export_id or "").strip().lower() in LATEST_EXPORT_TOKENS


class DownloadStatusArgs(AppBaseSchema):
    """
    Arguments for the ``download_status`` tool.

    Same defaulting as :class:`ConfirmDownloadArgs`: a user asking "is it ready
    yet?" has given the model nothing to identify the export with, so the newest one
    in the conversation is the only sensible answer.
    """

    export_id: OptionalText = Field(
        default=None,
        title="Export",
        max_length=64,
        description=(
            "The export id you were given, if you have it. Leave it out or pass "
            "'latest' for the most recent export in this conversation."
        ),
    )

    def wants_latest(self) -> bool:
        """Whether ``export_id`` is a real id or a stand-in for "the last offer"."""
        return (self.export_id or "").strip().lower() in LATEST_EXPORT_TOKENS


# --------------------------------------------------------------------------
# Requests
# --------------------------------------------------------------------------

class PublicDownloadQuery(QueryRequest):
    """
    What a widget visitor must present to fetch their own export.

    There is no logged-in user on this route, so the pair below *is* the
    authorisation: the key says which widget the export belongs to, the token which
    conversation within it. Both are required — a key alone would let any visitor of
    a public widget read every export ever produced for it.
    """

    key: OptionalText = Field(
        default=None, title="Chatbot key", max_length=64,
    )
    session_token: OptionalText = Field(
        default=None, title="Session", max_length=255,
    )


# --------------------------------------------------------------------------
# Responses
# --------------------------------------------------------------------------

class DownloadExportView(ResponseSchema):
    """
    One export, as a status call or a progress consumer sees it.

    ``download_url`` is not a column — the same export is reachable at two
    different prefixes depending on who is asking (the operator console or the public
    widget), so the caller supplies it and this schema only carries it. It is None
    until the artifact is ready, which is what makes "is there a link yet?" a
    question about the payload rather than about the status string.
    """

    uuid: uuid_pkg.UUID = Field(title="Export")
    status: str = Field(title="Status")
    file_format: str = Field(title="File format")
    file_name: Optional[str] = Field(default=None, title="File name")

    total_rows: Optional[int] = Field(default=None, title="Total records")
    count_is_lower_bound: bool = Field(default=False, title="Count is approximate")

    part_count: int = Field(default=0, title="Parts written")
    rows_written: int = Field(default=0, title="Records written")
    byte_size: Optional[int] = Field(default=None, title="File size")

    error_message: Optional[str] = Field(default=None, title="Problem")

    created_at: Optional[datetime] = Field(default=None, title="Created")
    expires_at: Optional[datetime] = Field(default=None, title="Available until")

    download_url: Optional[str] = Field(default=None, title="Download")

    @classmethod
    def of(cls, export: Any, download_url: Optional[str] = None) -> "DownloadExportView":
        """
        Build the view for one ``DownloadExport`` row.

        Takes the url rather than deriving it, for the reason in the class
        docstring. Passing None for an export that is not ready is correct and is
        what every caller does.
        """
        return cls.build(
            {
                "uuid": export.uuid,
                "status": export.status,
                "file_format": export.file_format,
                "file_name": export.file_name,
                "total_rows": export.total_rows,
                "count_is_lower_bound": bool(export.count_is_lower_bound),
                "part_count": export.part_count,
                "rows_written": export.rows_written,
                "byte_size": export.byte_size,
                "error_message": export.error_message,
                "created_at": export.created_at,
                "expires_at": export.expires_at,
                "download_url": download_url,
            }
        )


class DownloadNoticeView(ResponseSchema):
    """
    The export a single reply is about, as the widget reads it.

    A deliberately smaller thing than :class:`DownloadExportView`. That one answers
    "tell me everything about this export" for a status call; this one answers "what
    should I put under this message?", and the difference is ``progress_url`` — the
    thing a reply needs and a status call, having just been made, does not.

    Both URLs are supplied rather than derived, for the reason
    :class:`DownloadExportView` gives: the same export is reachable at two prefixes
    depending on who is asking.
    """

    uuid: uuid_pkg.UUID = Field(title="Export")
    status: str = Field(title="Status")
    file_format: str = Field(title="File format")
    file_name: Optional[str] = Field(default=None, title="File name")

    total_rows: Optional[int] = Field(default=None, title="Total records")
    rows_written: int = Field(default=0, title="Records written")
    byte_size: Optional[int] = Field(default=None, title="File size")

    error_message: Optional[str] = Field(default=None, title="Problem")

    #: Only ever set once the artifact exists — see :meth:`of`.
    download_url: Optional[str] = Field(default=None, title="Download")
    #: Always set: "not ready yet" is exactly when watching this is worth doing.
    progress_url: Optional[str] = Field(default=None, title="Progress")
    #: What a client polls when the progress stream drops. A build can outlast one SSE
    #: connection, and a card frozen by a dead socket must not read as a dead build.
    status_url: Optional[str] = Field(default=None, title="Status")

    @classmethod
    def of(
        cls,
        export: Any,
        download_url: Optional[str] = None,
        progress_url: Optional[str] = None,
        status_url: Optional[str] = None,
    ) -> "DownloadNoticeView":
        """Build the notice for one ``DownloadExport`` row."""
        return cls.build(
            {
                "uuid": export.uuid,
                "status": export.status,
                "file_format": export.file_format,
                "file_name": export.file_name,
                "total_rows": export.total_rows,
                "rows_written": export.rows_written,
                "byte_size": export.byte_size,
                "error_message": export.error_message,
                "download_url": download_url,
                "progress_url": progress_url,
                "status_url": status_url,
            }
        )


class DownloadProgressEvent(ResponseSchema):
    """
    One frame of the build-progress stream.

    Deliberately flat and small. It is serialised once per completed batch, so an
    export of a hundred thousand records emits two thousand of these — anything
    that had to be assembled from the database per frame would make the progress
    feed more expensive than the work it reports on.

    ``event`` is the SSE event name as well as a field, so a browser can either
    switch on ``event.type`` or read the payload; the two never disagree because
    they come from the same value.
    """

    #: The event names this schema is used for. Kept as a class constant so the
    #: route and the tests name them from one place.
    PROGRESS: ClassVar[str] = "progress"
    RETRY: ClassVar[str] = "retry"
    READY: ClassVar[str] = "ready"
    FAILED: ClassVar[str] = "failed"

    event: str = Field(title="Event")
    export_id: uuid_pkg.UUID = Field(title="Export")
    status: str = Field(title="Status")

    #: 1-based part number this frame is about; None on ready/failed frames, which
    #: are about the export as a whole.
    part: Optional[int] = Field(default=None, title="Part")
    of: Optional[int] = Field(default=None, title="Total parts")
    attempt: Optional[int] = Field(default=None, title="Attempt")

    rows_written: int = Field(default=0, title="Records written")
    total_rows: Optional[int] = Field(default=None, title="Total records")

    #: A sentence for a person. Present on retry and failed frames; None on the
    #: ordinary per-part frames, which have nothing to say that the numbers don't.
    message: Optional[str] = Field(default=None, title="Message")
    download_url: Optional[str] = Field(default=None, title="Download")

    #: Set on the ``ready`` frame only, because that is when they first exist. A client
    #: that has been watching a build since it was queued has never seen either — the
    #: name is chosen when the parts are merged and the size is the merged artifact's —
    #: so without them here it would render a finished file as an unnamed one of
    #: unknown size, and offer a download attribute with nothing in it.
    file_name: Optional[str] = Field(default=None, title="File name")
    byte_size: Optional[int] = Field(default=None, title="File size")
