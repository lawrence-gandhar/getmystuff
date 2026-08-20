"""
Downloader Agents — the record of one offered, queued or finished export.

A data agent's tool answers a question; it does not move a table. When a tool's
query matches more records than an answer can reasonably print, the agent offers to
produce the whole set as a file instead, and these three rows are what make that
offer outlive the sentence it was made in.

**Why any of this is stored at all.** The offer and the acceptance happen in two
different HTTP turns, and the work happens in a third place — a worker task that has
no request, no session and no user. Nothing about that can be held in memory:

``DownloadExport``
    The offer, and later the artifact. It remembers *which* query was offered
    (``tool_config_id``), *who* it was offered to (``data_agent_id`` for the console,
    ``chatbot_key_id`` + ``session_token`` for a visitor), and ``thread_id`` — the
    LangGraph checkpointer thread the confirmation interrupt is parked on, which is
    the only handle the worker has on a graph run that started in a request.

``DownloadJob``
    The queue row. Claimed with ``FOR UPDATE SKIP LOCKED`` (see
    app/db/downloader_agents/queries.py) so more than one app process can drain the
    queue without two of them building the same file. ``heartbeat_at`` is what lets a
    job whose worker died be requeued rather than sitting claimed forever.

``DownloadExportPart``
    One batch of 50 records, one part file, one row. The parts are visible on disk,
    so this table is not how the *builder* keeps track of them — it is how a
    *reader* does. Progress is streamed to the browser by a different request than
    the one writing the files, and a reader cannot see the worker's variables.

**Status is a plain string, not an Enum type.** Same reasoning as
``ToolConfig.query_mode``: adding a state would otherwise need a migration on the
type itself, and every write goes through
app/services/downloader_agents/base/download_service.py, which validates it.

Only ``uuid`` ever leaves this module. The bigint ``id`` is for the primary key and
for foreign keys between these three tables, and nothing else — a download URL names
an export by its uuid.
"""

import uuid as uuid_pkg
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.db.base import Base


# ----------------------------------------------------------------------------
# Formats
# ----------------------------------------------------------------------------
# (value, extension, label). The value is what the agent passes and what is stored;
# the extension is what the file is actually called. "xls" is the folder and the
# spoken name, .xlsx is the format openpyxl writes — the two differ on purpose
# rather than by mistake, and keeping the mapping here means nothing has to guess.
FORMAT_CSV = "csv"
FORMAT_XLS = "xls"
FORMAT_PARQUET = "parquet"

EXPORT_FORMATS = (
    (FORMAT_CSV, ".csv", "CSV"),
    (FORMAT_XLS, ".xlsx", "Excel"),
    (FORMAT_PARQUET, ".parquet", "Parquet"),
)
EXPORT_FORMAT_VALUES = frozenset(value for value, _, _ in EXPORT_FORMATS)
EXPORT_FORMAT_EXTENSIONS = {value: extension for value, extension, _ in EXPORT_FORMATS}
EXPORT_FORMAT_LABELS = {value: label for value, _, label in EXPORT_FORMATS}


# ----------------------------------------------------------------------------
# Statuses
# ----------------------------------------------------------------------------
# An export's life. `offered` is the only state a visitor's "yes" may act on, and
# `ready` the only one a download may be served from — both checked in the service,
# so a stale link cannot serve a half-written file.
EXPORT_OFFERED = "offered"
EXPORT_DECLINED = "declined"
EXPORT_QUEUED = "queued"
EXPORT_BUILDING = "building"
EXPORT_READY = "ready"
EXPORT_FAILED = "failed"
EXPORT_EXPIRED = "expired"

EXPORT_STATUSES = frozenset(
    {
        EXPORT_OFFERED,
        EXPORT_DECLINED,
        EXPORT_QUEUED,
        EXPORT_BUILDING,
        EXPORT_READY,
        EXPORT_FAILED,
        EXPORT_EXPIRED,
    }
)

# A queue row's life. Deliberately shorter than the export's: the job says whether
# the *work* is waiting, running or over, and the export says what came of it.
JOB_QUEUED = "queued"
JOB_RUNNING = "running"
JOB_SUCCEEDED = "succeeded"
JOB_FAILED = "failed"

JOB_STATUSES = frozenset({JOB_QUEUED, JOB_RUNNING, JOB_SUCCEEDED, JOB_FAILED})

# One part file's life. `discarded` is not the same as `failed`: a discarded part is
# one whose file was deleted so the batch could be retried, and the retry that
# follows writes a new row. Keeping the discarded one is what makes "this batch was
# attempted three times" auditable after the fact.
PART_WRITTEN = "written"
PART_DISCARDED = "discarded"
PART_MERGED = "merged"

PART_STATUSES = frozenset({PART_WRITTEN, PART_DISCARDED, PART_MERGED})


class DownloadExport(Base):
    """
    One offer to export a tool's full result set, and the file it became.

    ``tool_config_id`` and ``data_agent_id`` both cascade: an export is a view of a
    tool belonging to an agent, and it cannot be rebuilt — or authorised — once
    either is gone, so the row goes with them. ``chatbot_key_id`` is
    ``SET NULL`` instead, because a deleted widget key should not destroy the audit
    trail of files that were produced for it.

    ``total_rows`` is an exact ``COUNT(*)``, not the length of a truncated sample.
    That distinction is the whole reason this feature exists, so the column means
    only that; when the count could not be established exactly it is NULL and
    ``count_is_lower_bound`` says so.
    """

    __tablename__ = "download_exports"

    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=True,
    )

    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True),
        default=uuid_pkg.uuid4,
        unique=True,
        index=True,
        nullable=False,
    )

    data_agent_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("data_agents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    tool_config_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("tool_configs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Present when the offer was made to a widget visitor rather than to the
    # operator on the agent console. Both are needed to authorise the download:
    # the key says which widget, the token which conversation.
    chatbot_key_id: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        ForeignKey("chatbot_api_keys.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    session_token: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
        index=True,
    )

    # The LangGraph checkpointer thread this export's run is parked on. The
    # confirmation `interrupt()` fires inside a request and is resumed inside the
    # worker, so this string is the only thing connecting the two.
    thread_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    # "csv" | "xls" | "parquet" — see EXPORT_FORMATS. Set when the offer is made
    # (CSV, because that is what the offer sentence says) and overwritten if the
    # user asks for another format when they confirm.
    file_format: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=FORMAT_CSV,
        server_default=FORMAT_CSV,
    )

    # Exact COUNT(*) of the tool's query at the moment the offer was made. NULL
    # only when the count could not be run exactly — see count_is_lower_bound.
    total_rows: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)

    # True when `total_rows` is "at least this many" rather than "this many": a SQL
    # mode statement the database would not let us wrap in a COUNT, counted by
    # streaming up to the ceiling instead. Said out loud because an approximate
    # total presented as an exact one is the failure this feature was built to fix.
    count_is_lower_bound: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False,
    )

    # See EXPORT_STATUSES.
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=EXPORT_OFFERED,
        server_default=EXPORT_OFFERED,
        index=True,
    )

    # Where the merged artifact is, relative to the project root, and what it is
    # called when it reaches the browser. Both NULL until the merge succeeds — an
    # export with a path is an export with a file.
    file_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    file_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    byte_size: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    checksum: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    # How many part files were written, and how many rows actually reached the
    # file. `rows_written` is not the same as `total_rows`: a table that grew while
    # the export ran makes them differ, and that is worth being able to see.
    part_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    rows_written: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0",
    )

    # The sentence the agent is to relay when this export failed. Stored rather than
    # composed at read time so the operator, the visitor and the log all see the
    # same words.
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # When the artifact may be deleted. A reaper honours this; the download route
    # also refuses a lapsed export even if the file is still on disk, so the two
    # cannot disagree.
    expires_at: Mapped[Optional[DateTime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True,
    )

    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        index=True,
    )

    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        index=True,
    )

    parts = relationship(
        "DownloadExportPart",
        back_populates="export",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    jobs = relationship(
        "DownloadJob",
        back_populates="export",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        # The lookup behind a bare "yes": the newest still-open offer for this
        # conversation. Ordered so status can be matched before the timestamp is
        # scanned.
        Index(
            "ix_download_exports_pending_lookup",
            "data_agent_id",
            "status",
            "created_at",
        ),
    )


class DownloadJob(Base):
    """
    One unit of queued work: build the artifact for ``export_id``.

    A separate table from the export rather than four more columns on it, because
    the two are claimed and updated at different rates by different code. The
    worker writes a heartbeat here every few seconds; the export is written once per
    milestone. Keeping them apart means progress traffic never contends with the row
    a download route reads.

    ``attempts`` counts *job* attempts — a worker dying and the job being requeued.
    Batch-level retries are counted on ``DownloadExportPart.attempts`` instead, and
    the two are deliberately not merged: one is about our own infrastructure, the
    other about the user's database.
    """

    __tablename__ = "download_jobs"

    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=True,
    )

    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True),
        default=uuid_pkg.uuid4,
        unique=True,
        index=True,
        nullable=False,
    )

    export_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("download_exports.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # See JOB_STATUSES.
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=JOB_QUEUED,
        server_default=JOB_QUEUED,
        index=True,
    )

    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )

    # Which worker holds it. Free text (host + task name), for reading a log rather
    # than for any decision — the claim itself is done by SKIP LOCKED, not by this.
    claimed_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    claimed_at: Mapped[Optional[DateTime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    # Bumped while the graph runs. A running job whose heartbeat has gone stale is
    # a worker that died mid-export, and the reaper requeues it.
    heartbeat_at: Mapped[Optional[DateTime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True,
    )

    finished_at: Mapped[Optional[DateTime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        index=True,
    )

    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    export = relationship("DownloadExport", back_populates="jobs")

    __table_args__ = (
        # The claim query: oldest queued job first. Matches the ORDER BY in
        # app/db/downloader_agents/queries.claim_next_job exactly.
        Index("ix_download_jobs_claim", "status", "created_at"),
    )


class DownloadExportPart(Base):
    """
    One batch of records written to one part file.

    Rows are kept for discarded attempts as well as successful ones, so
    ``part_number`` is **not** unique — three rows with the same number is exactly
    what "this batch failed twice before it worked" looks like. The successful one
    is the row whose status is not ``discarded``.

    ``path`` is recorded even for a discarded part, whose file no longer exists.
    That is not a dangling pointer by accident: it is the record of what was
    deleted, which is the only way to tell a cleanup that ran from one that did not.
    """

    __tablename__ = "download_export_parts"

    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=True,
    )

    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True),
        default=uuid_pkg.uuid4,
        unique=True,
        index=True,
        nullable=False,
    )

    export_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("download_exports.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # 1-based, matching the file name (part-0001.csv). 1-based because it is read by
    # people in progress messages — "part 1 of 97", not "part 0 of 97".
    part_number: Mapped[int] = mapped_column(Integer, nullable=False)

    # Which try produced this row: 1 for the first, up to MAX_BATCH_ATTEMPTS.
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1",
    )

    row_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )

    # See PART_STATUSES.
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=PART_WRITTEN,
        server_default=PART_WRITTEN,
        index=True,
    )

    path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    byte_size: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)

    # Why this attempt was discarded. NULL on a part that was written cleanly.
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        index=True,
    )

    export = relationship("DownloadExport", back_populates="parts")

    __table_args__ = (
        # The progress feed reads parts for one export in order. Not unique — see
        # the class docstring on why one part_number may have several rows.
        Index("ix_download_export_parts_order", "export_id", "part_number"),
    )
