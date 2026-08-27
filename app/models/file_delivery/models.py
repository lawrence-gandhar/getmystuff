"""
One file a block made, and who is allowed to fetch it.

**Why this is not a ``download_exports`` row.** That table's three ``NOT NULL`` foreign
keys say what it is — one offer to export *a data agent tool's* result set, parked on a
checkpointer thread — and a file a Create File block wrote has no tool, no agent and no
thread. Making three columns nullable to fit this in would delete the invariant that
table's own docstring states. So this module owns its own table, and the reuse runs the
other way: the two audiences' URL shapes, the expiry rule and the streaming route are
modelled on ``downloader_agents`` rather than borrowed from it.

**Two statuses, not five.** ``download_exports`` moves through offered → queued →
building → ready because the file is built by a background worker over minutes. Here the
row is inserted **after** the bytes are on disk, so "a row exists" means "a file exists",
and the only later transition is the reaper's: ready → expired.
"""

import uuid as uuid_pkg
from datetime import datetime as DateTime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    DateTime as SADateTime,
    ForeignKey,
    Index,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


# ----------------------------------------------------------------------------
# Formats
# ----------------------------------------------------------------------------
# The value is what a canvas writes into its node data and what `file_writer`
# dispatches on, so there is one list and the two cannot drift.
#
# Its own vocabulary rather than `downloader_agents.EXPORT_FORMATS`, for two reasons that
# are both about honesty at the edges: that list has no TXT, and it spells the Excel
# format "xls" while writing `.xlsx` — a name this feature would have to explain in a
# dropdown an operator reads. Here the value, the extension and the label agree.
FORMAT_CSV = "csv"
FORMAT_XLSX = "xlsx"
FORMAT_TXT = "txt"
FORMAT_PARQUET = "parquet"

FILE_FORMATS = (
    (FORMAT_CSV, ".csv", "CSV", "text/csv"),
    (
        FORMAT_XLSX,
        ".xlsx",
        "Excel (XLSX)",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ),
    (FORMAT_TXT, ".txt", "Text", "text/plain"),
    (FORMAT_PARQUET, ".parquet", "Parquet", "application/vnd.apache.parquet"),
)

FILE_FORMAT_VALUES = frozenset(value for value, _, _, _ in FILE_FORMATS)
FILE_FORMAT_EXTENSIONS = {value: extension for value, extension, _, _ in FILE_FORMATS}
FILE_FORMAT_LABELS = {value: label for value, _, label, _ in FILE_FORMATS}
FILE_FORMAT_MEDIA_TYPES = {value: media for value, _, _, media in FILE_FORMATS}


# ----------------------------------------------------------------------------
# Where a Create File block's rows come from
# ----------------------------------------------------------------------------
# The vocabulary is here, and each canvas's *subset* of it is declared by that canvas's
# runner (``FLOW_DATA_SOURCES``, ``GRAPH_DATA_SOURCES``) — the arrangement
# ``email_dispatch`` uses for its binding sources, and for the same reason: what a source
# means is one question, and which canvas can serve it is another.
#
#   block     a named block earlier in this conversation — a Run Graph block's result or
#             an AI Fallback block's answer table. Flow Builder only: a graph has node
#             outputs instead, which is the next entry.
#   node      a named node's output in this run, optionally through a path into it.
#             Graph Designer only.
#   variable  a conversation variable holding a dataset (JSON) or text. Flow Builder only.
SOURCE_BLOCK = "block"
SOURCE_NODE = "node"
SOURCE_VARIABLE = "variable"

DATA_SOURCES = (
    (SOURCE_BLOCK, "A block earlier in this conversation"),
    (SOURCE_NODE, "An earlier node's output"),
    (SOURCE_VARIABLE, "A variable"),
)

DATA_SOURCE_VALUES = frozenset(value for value, _ in DATA_SOURCES)
DATA_SOURCE_LABELS = dict(DATA_SOURCES)


# ----------------------------------------------------------------------------
# Which canvas made it
# ----------------------------------------------------------------------------
# Recorded because the two are fetched by different people through different routes: a
# flow's file belongs to one visitor's conversation and is served on the public route
# against a widget key and a session token; a graph's file belongs to the operator who
# owns the graph and is served on the authenticated route. A row that did not say which
# it was would have to be guessed at from whether `session_token` was set, and a guess
# about who may read somebody's data is not a thing to leave implicit.
ORIGIN_FLOW = "flow"
ORIGIN_GRAPH = "graph"

FILE_ORIGINS = ((ORIGIN_FLOW, "Chatbot flow"), (ORIGIN_GRAPH, "Pipeline run"))
FILE_ORIGIN_VALUES = frozenset(value for value, _ in FILE_ORIGINS)


# ----------------------------------------------------------------------------
# Status
# ----------------------------------------------------------------------------
FILE_READY = "ready"
FILE_EXPIRED = "expired"

FILE_STATUSES = frozenset({FILE_READY, FILE_EXPIRED})


class GeneratedFile(Base):
    """
    One file written by a Create File block, and the scope it may be fetched in.

    ``user_id`` cascades: the file is that person's data, and it cannot be authorised
    once they are gone. ``chatbot_key_id`` is ``SET NULL`` instead, matching
    ``DownloadExport`` — deleting a widget key should stop its links working (the route
    resolves the key first) without destroying the record of what was produced.

    ``file_path`` is relative to the project root, and it is written by
    ``file_service`` from the row's own uuid — never assembled from anything a visitor,
    an operator or a model supplied. The file *name* is operator-authored and goes
    through ``file_utils.normalize_filename`` first, so a name like ``../../etc/passwd``
    becomes a harmless flat one.
    """

    __tablename__ = "generated_files"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True),
        default=uuid_pkg.uuid4,
        unique=True,
        index=True,
        nullable=False,
    )

    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Both present for a flow's file and both NULL for a graph's: the key says which
    # widget, the token which conversation, and the public route needs both to
    # authorise. A key with no token would let any visitor of a public widget read every
    # file the widget ever produced.
    chatbot_key_id: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        ForeignKey("chatbot_api_keys.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    session_token: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True, index=True,
    )

    # See FILE_ORIGINS.
    origin: Mapped[str] = mapped_column(String(16), nullable=False, index=True)

    # The uuid of the flow or the graph run this file came out of, and the id of the
    # block inside it. Both are for the audit trail — "which drawing made this, and
    # where in it" — and neither authorises anything, which is why they are plain
    # strings rather than foreign keys: a file must outlive an edit to the graph that
    # produced it, or the link in somebody's chat transcript breaks for no reason they
    # could understand.
    source_ref: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    node_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    # See FILE_FORMATS.
    file_format: Mapped[str] = mapped_column(String(16), nullable=False)

    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    file_path: Mapped[str] = mapped_column(Text, nullable=False)
    byte_size: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0",
    )
    row_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0",
    )

    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=FILE_READY,
        server_default=FILE_READY,
        index=True,
    )

    # When the file may be deleted. The reaper honours it and so does every route, so a
    # lapsed link cannot be served in the minutes before the next sweep.
    expires_at: Mapped[Optional[DateTime]] = mapped_column(
        SADateTime(timezone=True), nullable=True, index=True,
    )

    created_at: Mapped[DateTime] = mapped_column(
        SADateTime(timezone=True), server_default=func.now(), index=True,
    )

    updated_at: Mapped[DateTime] = mapped_column(
        SADateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        index=True,
    )

    __table_args__ = (
        # The visitor route's lookup: this widget, this conversation, this file. Ordered
        # so the key and the token narrow the scan before the status is read.
        Index(
            "ix_generated_files_visitor_lookup",
            "chatbot_key_id",
            "session_token",
            "status",
        ),
    )
