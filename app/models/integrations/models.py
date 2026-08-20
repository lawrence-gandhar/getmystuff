"""
Integration Platform — the workflow, the versions it was published as, every run it
took, and the connections it authenticates through.

See ``documentations/INTEGRATIONS.md`` for the design this implements. What follows is
the reasoning specific to the tables.

**Why this is not more Graph Designer tables.** ``tool_graphs`` runs the live drawing
and keeps its result in memory between super-steps. That is right for a query tool a
person is watching. It is wrong for something that writes into a merchant's storefront
on a schedule at three in the morning, and the differences are structural rather than
incremental:

*a run is pinned to a version, not to the drawing*
    ``integration_flow_versions`` is an immutable snapshot, and
    ``integration_runs.flow_version_id`` names the one that ran. An audit trail whose
    drawing can be edited afterwards is not an audit trail, and a replay that
    recompiles from the live graph replays a different workflow.

*records are counted, not carried*
    Four ``BigInteger`` counters on the run and one row per *interesting* record in
    ``integration_run_records``. A 50,000-record sync must not put 50,000 rows in a log
    table, and it must not put them in the LangGraph state either — see
    ``engine/record_buffer.py``.

*there is a queue and a scheduler*
    ``integration_run_jobs`` and ``integration_triggers``. Graph Designer needs neither
    because somebody is always watching; "on a schedule" is the product here, and a
    restart must not lose a run.

*failure has three levels, not two*
    A record failed, a node failed, the run failed. Collapsing the first into the
    second is how "3 of 50,000 records had a bad email address" becomes "the sync
    failed". A run with any failed, invalid or skipped record ends ``partial`` — never
    ``succeeded`` — for the same reason ``downloader_agents`` refuses to hand back a
    part-complete export: a result that silently contains some of the data is worse
    than none, because nothing about it says so.

**Secrets live in their own table.** ``integration_credentials`` is one row per
connection behind a unique foreign key, rather than six more columns on
``integration_connections``. Two reasons, both practical: the connection view a route
builds selects from the connection alone and therefore *cannot* serialise a secret by
accident, and revoking is one ``DELETE`` that provably leaves nothing behind. Each
secret gets its own ``*_encrypted`` column, following ``datasources`` — host, port and
username are plaintext there, and only the password is ciphertext.

**Statuses and kinds are plain strings, not Enum types.** The same call
``ToolConfig.query_mode``, ``DownloadExport.status`` and ``ToolGraphRun.status`` all
make: adding a state should be a constant and a validator, not a migration that
rewrites a type while every table using it is locked. Every write goes through
``app/services/integrations/``, which validates against the frozensets below.

**The node vocabulary lives here and nowhere else.** ``NODE_TYPES`` and ``NODE_PORTS``
are read by ``engine/flow_rules.py`` (which validates), by ``/integrations/vocabulary``
(which the palette is drawn from) and by the AI prompt renderer (which tells a model
what it may write). One list, so the palette cannot offer a node the validator refuses
and a model cannot be told about a node that does not exist. A node type declared here
but with no runner registered in ``engine/node_runners.py`` is refused by
``validate_flow`` and absent from the palette — that is how the Phase 2 and Phase 3
types below can be named now and become usable later without a second list appearing.

**Deliberately not here.** No dead-letter table: a failed record already carries its
full payload in ``integration_run_records.payload``, which is what a replay needs. No
webhook-receipt table: the run row's ``idempotency_key`` is the receipt. The two
webhook tables named in the design are genuinely deferred to Phase 3 rather than
created empty, because their shape turns on a question that is not settled — where each
vendor's signing secret comes from differs per vendor, and a column added now would
have to be changed then. ``integration_cursors`` *is* created now, unused until Phase 2,
because its shape is settled and one migration is cheaper than two.

Only ``uuid`` ever leaves this module. The bigint ``id`` is the primary key and the
target of the foreign keys between these tables, and nothing else — every URL, form
field and JSON payload names a flow, a run or a connection by its uuid.
"""

import uuid as uuid_pkg
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


# ----------------------------------------------------------------------------
# Node types
# ----------------------------------------------------------------------------
# What a node may be. The value is what the canvas writes into `graph_data`, what
# `flow_rules` validates and what `node_runners` dispatches on — one list, so none of
# the three can drift from the others.
#
# The Phase 2 and Phase 3 entries are named here on purpose rather than added later.
# A node type with no registered runner is refused by `validate_flow` and omitted from
# the palette, so naming it early costs nothing and keeps the vocabulary in one place;
# adding it later as a second list is what produces a palette offering something the
# validator rejects.

# --- Phase 1 -----------------------------------------------------------------
NODE_TRIGGER = "trigger"
NODE_CONNECTOR_READ = "connector_read"
NODE_CONNECTOR_WRITE = "connector_write"
NODE_TRANSFORM = "transform"
NODE_VALIDATE = "validate"
NODE_FILTER = "filter"
NODE_BRANCH = "branch"
NODE_BATCH = "batch"
NODE_SUCCESS = "success"
NODE_FAILURE = "failure"

# --- Phase 2 -----------------------------------------------------------------
NODE_JOIN = "join"
NODE_AGGREGATE = "aggregate"
NODE_ERROR_HANDLER = "error_handler"
NODE_DELAY = "delay"
NODE_APPROVAL = "approval"

# --- Phase 3 -----------------------------------------------------------------
# The one non-deterministic node, and the explicit escape hatch from that rule. It
# stays unrunnable until a runner is registered for it; see the module docstring and
# documentations/INTEGRATIONS.md on what "always act as Agent AI" honestly means when
# the runtime has to be replayable.
NODE_AGENT = "agent"

NODE_TYPES = (
    (NODE_TRIGGER, "Trigger"),
    (NODE_CONNECTOR_READ, "Read from"),
    (NODE_CONNECTOR_WRITE, "Write to"),
    (NODE_TRANSFORM, "Transform"),
    (NODE_VALIDATE, "Validate"),
    (NODE_FILTER, "Filter"),
    (NODE_BRANCH, "Branch"),
    (NODE_BATCH, "Batch"),
    (NODE_JOIN, "Join"),
    (NODE_AGGREGATE, "Aggregate"),
    (NODE_ERROR_HANDLER, "Error handler"),
    (NODE_DELAY, "Wait"),
    (NODE_APPROVAL, "Ask for approval"),
    (NODE_AGENT, "AI step"),
    (NODE_SUCCESS, "Success"),
    (NODE_FAILURE, "Failure"),
)

NODE_TYPE_VALUES = frozenset(value for value, _ in NODE_TYPES)
NODE_TYPE_LABELS = dict(NODE_TYPES)

# Both compile to LangGraph's END. The difference is what the run is recorded as
# having done, which is the point of the author being able to draw a failure path
# rather than only reach an error state.
TERMINAL_NODE_TYPES = frozenset({NODE_SUCCESS, NODE_FAILURE})

# The only node that may be re-entered. A cycle is legal exactly when it passes
# through one of these, which is what `validate_flow` cuts on before testing the rest
# of the graph for acyclicity.
LOOP_NODE_TYPES = frozenset({NODE_BATCH})

# The node types that talk to somebody else's server. Three rules key off this set:
# a connection is required, a rate limit applies, and a `dry_run` suppresses the call.
CONNECTOR_NODE_TYPES = frozenset({NODE_CONNECTOR_READ, NODE_CONNECTOR_WRITE})


# ----------------------------------------------------------------------------
# Ports
# ----------------------------------------------------------------------------
# The named exits from a node. `default`, `error`, `body`, `done` and `else` keep the
# spelling Graph Designer uses, because they mean the same thing there and a user who
# has drawn one canvas has drawn both. The four new ones name splits Graph Designer has
# no equivalent for, because it has no records to split.
PORT_DEFAULT = "default"
PORT_ERROR = "error"
PORT_ELSE = "else"
PORT_BODY = "body"
PORT_DONE = "done"
PORT_VALID = "valid"
PORT_INVALID = "invalid"
PORT_KEPT = "kept"
PORT_DROPPED = "dropped"

# Which exits each node type offers, in the order the canvas draws them.
#
# `branch` is absent because its ports are authored: one per condition the user wrote,
# plus `else`. It is the one node whose port list cannot be static, and `flow_rules`
# derives it from the node's own data.
#
# A `trigger` has no `error` port: nothing has happened yet that could fail. A terminal
# node has no ports at all, which is what makes "an edge out of a terminal" a refusal
# the validator can state rather than a shape the canvas has to prevent.
NODE_PORTS = {
    NODE_TRIGGER: (PORT_DEFAULT,),
    NODE_CONNECTOR_READ: (PORT_DEFAULT, PORT_ERROR),
    NODE_CONNECTOR_WRITE: (PORT_DEFAULT, PORT_ERROR),
    NODE_TRANSFORM: (PORT_DEFAULT, PORT_ERROR),
    NODE_VALIDATE: (PORT_VALID, PORT_INVALID, PORT_ERROR),
    NODE_FILTER: (PORT_KEPT, PORT_DROPPED, PORT_ERROR),
    NODE_BATCH: (PORT_BODY, PORT_DONE),
    NODE_JOIN: (PORT_DEFAULT, PORT_ERROR),
    NODE_AGGREGATE: (PORT_DEFAULT, PORT_ERROR),
    NODE_ERROR_HANDLER: (PORT_DEFAULT,),
    NODE_DELAY: (PORT_DEFAULT,),
    NODE_APPROVAL: (PORT_DEFAULT, PORT_ELSE),
    NODE_AGENT: (PORT_DEFAULT, PORT_ERROR),
    NODE_SUCCESS: (),
    NODE_FAILURE: (),
}

PORT_VALUES = frozenset(
    {
        PORT_DEFAULT,
        PORT_ERROR,
        PORT_ELSE,
        PORT_BODY,
        PORT_DONE,
        PORT_VALID,
        PORT_INVALID,
        PORT_KEPT,
        PORT_DROPPED,
    }
)


# ----------------------------------------------------------------------------
# Batch sizes
# ----------------------------------------------------------------------------
# The unit of one pass through a `batch` node's body is a batch, not a record. 50,000
# records one at a time is 50,000 super-steps, 50,000 checkpoint writes and 50,000 step
# rows; at 500 per pass it is 100 of each.
#
# MAX_BATCH_SIZE is enforced in *validation*, not merely defaulted, because the records
# in a batch are held in process memory (`engine/record_buffer.py`). A default can be
# overridden on the node; a validation bound cannot.
MIN_BATCH_SIZE = 1
DEFAULT_BATCH_SIZE = 500
MAX_BATCH_SIZE = 5000


# ----------------------------------------------------------------------------
# Version statuses
# ----------------------------------------------------------------------------
# A version is written once and never edited. `published` is the one a trigger runs;
# publishing again archives the previous one in the same transaction, so there is
# always exactly one — enforced by a partial unique index *and* in `publish_flow`,
# because a partial index silently does not exist on SQLite unless `sqlite_where` is
# set and the test suite runs on SQLite.
VERSION_PUBLISHED = "published"
VERSION_ARCHIVED = "archived"

VERSION_STATUSES = frozenset({VERSION_PUBLISHED, VERSION_ARCHIVED})


# ----------------------------------------------------------------------------
# Trigger kinds and overlap policy
# ----------------------------------------------------------------------------
TRIGGER_MANUAL = "manual"
TRIGGER_SCHEDULE = "schedule"
TRIGGER_WEBHOOK = "webhook"

TRIGGER_KINDS = (
    (TRIGGER_MANUAL, "Run by hand"),
    (TRIGGER_SCHEDULE, "On a schedule"),
    (TRIGGER_WEBHOOK, "When something happens"),
)

TRIGGER_KIND_VALUES = frozenset(value for value, _ in TRIGGER_KINDS)

# What to do when a tick arrives and the previous run of this flow has not finished.
#
#   skip           → do not start another. **Writes a run row with status `skipped`**
#                    rather than doing nothing: one row per skipped tick is the only
#                    way an operator ever discovers that their five-minute sync takes
#                    seven minutes.
#   queue          → let it wait, bounded at OVERLAP_QUEUE_LIMIT, then degrade to skip.
#   cancel_previous → stop the one in flight and start this one.
OVERLAP_SKIP = "skip"
OVERLAP_QUEUE = "queue"
OVERLAP_CANCEL_PREVIOUS = "cancel_previous"

OVERLAP_POLICIES = (
    (OVERLAP_SKIP, "Skip this run"),
    (OVERLAP_QUEUE, "Wait for the previous run"),
    (OVERLAP_CANCEL_PREVIOUS, "Cancel the previous run"),
)

OVERLAP_POLICY_VALUES = frozenset(value for value, _ in OVERLAP_POLICIES)
OVERLAP_QUEUE_LIMIT = 3

# A schedule faster than this is refused. Not a performance guard — a one-minute floor
# is what keeps a misconfigured trigger from spending an API quota on nothing, and
# every vendor in scope rate-limits hard enough that sub-minute polling is a way to get
# an application suspended rather than a way to get data sooner.
MIN_INTERVAL_SECONDS = 60


# ----------------------------------------------------------------------------
# Run statuses
# ----------------------------------------------------------------------------
# `partial` is the one worth arguing about, and it is deliberate: a run that read
# 50,000 records and failed to write 3 of them did not succeed. Reporting it as
# `succeeded` with a counter nobody reads is how a silent data-loss bug lives for
# months. See the module docstring.
#
# `skipped` is a run that never started because `overlap_policy` said not to. It is a
# real row rather than an absence, for the reason given at OVERLAP_SKIP.
#
# `awaiting_input` and its companion column `interrupt_payload` are unused until the
# Phase 2 `approval` node. They are named now because the column has to exist for the
# node to be addable without a migration, and a status the column implies should not be
# invented separately later.
RUN_QUEUED = "queued"
RUN_RUNNING = "running"
RUN_AWAITING_INPUT = "awaiting_input"
RUN_SUCCEEDED = "succeeded"
RUN_PARTIAL = "partial"
RUN_FAILED = "failed"
RUN_CANCELLED = "cancelled"
RUN_SKIPPED = "skipped"

RUN_STATUSES = frozenset(
    {
        RUN_QUEUED,
        RUN_RUNNING,
        RUN_AWAITING_INPUT,
        RUN_SUCCEEDED,
        RUN_PARTIAL,
        RUN_FAILED,
        RUN_CANCELLED,
        RUN_SKIPPED,
    }
)

# The statuses that end a run. The progress stream stops when it sees one, so a browser
# knows the story is over rather than inferring it from silence.
TERMINAL_RUN_STATUSES = frozenset(
    {RUN_SUCCEEDED, RUN_PARTIAL, RUN_FAILED, RUN_CANCELLED, RUN_SKIPPED}
)

# What a run is allowed to touch.
#
#   live    → every write happens.
#   dry_run → every `connector_write` builds and validates its payload, records what it
#             *would* have sent as a `sample` record row, and calls nothing.
#
# This is the affordance that replaces Graph Designer's partial runs. Running "just the
# write node" against a live CRM with no upstream data writes garbage into somebody's
# production system, so that selection is not offered at all; a whole run that sends
# nothing is the safe way to answer the same question.
RUN_MODE_LIVE = "live"
RUN_MODE_DRY_RUN = "dry_run"

RUN_MODES = frozenset({RUN_MODE_LIVE, RUN_MODE_DRY_RUN})


# ----------------------------------------------------------------------------
# Step statuses
# ----------------------------------------------------------------------------
STEP_RUNNING = "running"
STEP_SUCCEEDED = "succeeded"
STEP_FAILED = "failed"
STEP_SKIPPED = "skipped"
STEP_AWAITING_INPUT = "awaiting_input"

STEP_STATUSES = frozenset(
    {
        STEP_RUNNING,
        STEP_SUCCEEDED,
        STEP_FAILED,
        STEP_SKIPPED,
        STEP_AWAITING_INPUT,
    }
)


# ----------------------------------------------------------------------------
# Record outcomes
# ----------------------------------------------------------------------------
# Only *interesting* records get a row. A run that moves 50,000 records writes 50,000
# to the counters and none of them here unless something happened to them.
#
#   failed  → the destination refused it. `payload` holds the whole record, because
#             replaying the failures is the point of writing the row at all.
#   invalid → it did not survive validation and never reached the destination.
#   skipped → deliberately not sent: a duplicate, or filtered out where that is
#             surprising enough to record.
#   sample  → what a `dry_run` would have sent. Capped much harder than the rest.
RECORD_FAILED = "failed"
RECORD_INVALID = "invalid"
RECORD_SKIPPED = "skipped"
RECORD_SAMPLE = "sample"

RECORD_OUTCOMES = frozenset(
    {RECORD_FAILED, RECORD_INVALID, RECORD_SKIPPED, RECORD_SAMPLE}
)


# ----------------------------------------------------------------------------
# How much of a run gets written down
# ----------------------------------------------------------------------------
# Three caps, all on the *log* and none of them on the counters. That split is the whole
# idea: `records_read` and friends on the run row stay exact however large the run gets,
# and these bound how many individual rows the run is allowed to explain itself with.
#
# The alternative — capping by dropping counts — produces a run page that says 500 where
# the truth is 50,000, and there is nothing on the page to say otherwise. A page that
# says "50,000 records, 1,000 of the failures listed" is honest about both numbers.

#: Passes of one `(run_id, node_id)` before `run_store.finish_step` stops inserting and
#: folds into a rollup row. A hundred-pass loop is readable; a ten-thousand-pass backfill
#: would write a log table larger than the data it describes.
STEP_COLLAPSE_AFTER = 500

#: Failed and invalid record rows per run. `payload` holds the whole record for these, so
#: this cap bounds real bytes rather than row count alone — and a run where a hundred
#: thousand records failed does not need a hundred thousand rows to make its point.
MAX_LOGGED_FAILURES = 1000

#: `sample` rows per run — what a dry run would have sent. Capped far harder because
#: samples are a demonstration, not an audit: twenty is enough to check a mapping.
MAX_LOGGED_SAMPLES = 20


# ----------------------------------------------------------------------------
# Queue job statuses
# ----------------------------------------------------------------------------
JOB_QUEUED = "queued"
JOB_RUNNING = "running"
JOB_SUCCEEDED = "succeeded"
JOB_FAILED = "failed"
JOB_CANCELLED = "cancelled"

JOB_STATUSES = frozenset(
    {JOB_QUEUED, JOB_RUNNING, JOB_SUCCEEDED, JOB_FAILED, JOB_CANCELLED}
)


# ----------------------------------------------------------------------------
# Connections
# ----------------------------------------------------------------------------
# What a connection is currently good for. `needs_reauth` is separate from `disabled`
# because they are somebody else's decision and the user's respectively: the first
# earns a red badge and a Reconnect button, the second is a switch the user threw.
CONNECTION_ACTIVE = "active"
CONNECTION_NEEDS_REAUTH = "needs_reauth"
CONNECTION_REVOKED = "revoked"
CONNECTION_DISABLED = "disabled"

CONNECTION_STATUSES = frozenset(
    {
        CONNECTION_ACTIVE,
        CONNECTION_NEEDS_REAUTH,
        CONNECTION_REVOKED,
        CONNECTION_DISABLED,
    }
)

# How a connection proves who it is. The value selects which `integration_credentials`
# columns are populated and which branch of `credential_service` reads them.
AUTH_NONE = "none"
AUTH_API_KEY = "api_key"
AUTH_BASIC = "basic"
AUTH_OAUTH2 = "oauth2"
AUTH_MTLS = "mtls"

AUTH_KINDS = (
    (AUTH_NONE, "No authentication"),
    (AUTH_API_KEY, "API key"),
    (AUTH_BASIC, "Username and password"),
    (AUTH_OAUTH2, "OAuth"),
    (AUTH_MTLS, "Client certificate"),
)

AUTH_KIND_VALUES = frozenset(value for value, _ in AUTH_KINDS)

# The audit trail on a credential. Everything that changes what a connection can do, or
# records that it stopped being able to do it. `detail` never holds a secret.
CREDENTIAL_CONNECTED = "connected"
CREDENTIAL_REFRESHED = "refreshed"
CREDENTIAL_REFRESH_FAILED = "refresh_failed"
CREDENTIAL_REVOKED = "revoked"
CREDENTIAL_REAUTH_REQUIRED = "reauth_required"
CREDENTIAL_PRIVATE_HOSTS_ENABLED = "private_hosts_enabled"
CREDENTIAL_ALLOWLIST_CHANGED = "allowlist_changed"

CREDENTIAL_EVENTS = frozenset(
    {
        CREDENTIAL_CONNECTED,
        CREDENTIAL_REFRESHED,
        CREDENTIAL_REFRESH_FAILED,
        CREDENTIAL_REVOKED,
        CREDENTIAL_REAUTH_REQUIRED,
        CREDENTIAL_PRIVATE_HOSTS_ENABLED,
        CREDENTIAL_ALLOWLIST_CHANGED,
    }
)

# Whether an operation reads or writes. Not cosmetic: a `connector_read` node may only
# choose a `read` operation and a `connector_write` only a `write` one, which is what
# stops a workflow drawn as a read from posting to somebody's store.
OPERATION_READ = "read"
OPERATION_WRITE = "write"

OPERATION_KINDS = frozenset({OPERATION_READ, OPERATION_WRITE})

# How long an OAuth install has to come back with its callback. Ten minutes is longer
# than any consent screen and short enough that a stolen state is worthless by the time
# it is found.
OAUTH_STATE_TTL_SECONDS = 600


# ============================================================================
# The workflow
# ============================================================================


class IntegrationFlow(Base):
    """
    The editable drawing — a workflow as its author currently has it, which is not
    necessarily the workflow that runs.

    That distinction is the whole reason ``IntegrationFlowVersion`` exists. This row is
    replaced wholesale on every save, including while a run of an earlier version is
    still in flight; nothing here is safe to read from a running workflow.

    ``redacted_fields`` is a per-flow list of extra field paths to strip before anything
    is written to a preview or a log, on top of the deny-list ``engine/flow_state.py``
    applies to everything. Graph Designer previews the operator's own query results;
    this previews webhook bodies and third-party API responses, either of which can
    carry a bearer token that nobody here chose to store.
    """

    __tablename__ = "integration_flows"

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

    # Which team's shelf this sits on. Detaches rather than deletes when the workspace
    # goes, the same call `ToolGraph.workspace_id` makes — a workflow somebody spent an
    # afternoon on should survive the workspace being tidied up.
    workspace_id: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        ForeignKey("workspaces.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # {"nodes": [{id, type, position:{x,y}, data}],
    #  "edges": [{id, source, source_port, target}]}
    #
    # Plain JSONB rather than MutableDict: replaced wholesale on save, so change
    # tracking would cost something and buy nothing. Same call `ToolGraph.graph_data`
    # and `ChatbotFlow.graph_data` make.
    graph_data: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    # Whether triggers on this flow may fire. Independent of whether a version is
    # published, so a flow can be parked mid-investigation without unpublishing the
    # version its history refers to.
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false"),
    )

    # The batch size a `batch` node uses when it does not set its own. Bounded by
    # MIN_BATCH_SIZE…MAX_BATCH_SIZE in validation, not merely here.
    default_batch_size: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=DEFAULT_BATCH_SIZE,
        server_default=str(DEFAULT_BATCH_SIZE),
    )

    # Extra field paths to redact from every preview and log row. A flat list of
    # strings; see the class docstring.
    redacted_fields: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)

    # Whether the first draft came out of the workflow generator. Recorded because "the
    # AI wrote this" is a useful thing to know when reading a workflow six months later,
    # and because it is the only honest way to measure whether the generator is any
    # good. It is not a permission: a generated flow is published by a human like any
    # other, and editing one does not clear the flag.
    created_by_ai: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false"),
    )

    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True,
    )

    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        index=True,
    )

    versions: Mapped[list["IntegrationFlowVersion"]] = relationship(
        "IntegrationFlowVersion",
        back_populates="flow",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="IntegrationFlowVersion.version_number",
    )

    triggers: Mapped[list["IntegrationTrigger"]] = relationship(
        "IntegrationTrigger",
        back_populates="flow",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        # Two workflows of the same name under one user is a collision the library
        # cannot resolve and a run history nobody can read. Case-insensitive, written as
        # a `text()` expression for the same reason `uq_tool_graphs_user_name_lower` is:
        # Alembic's autogenerate cannot see a functional index and will propose creating
        # it on every revision forever unless the migration writes it by hand.
        Index(
            "uq_integration_flows_user_name_lower",
            "user_id",
            text("lower(name)"),
            unique=True,
        ),
    )


class IntegrationFlowVersion(Base):
    """
    An immutable snapshot of a flow, frozen at the moment somebody pressed Publish.

    **This is what actually runs.** A trigger fires a version, a run records which
    version it was, and a replay re-runs *that* version rather than whatever the drawing
    has become since. Graph Designer recompiles from the live graph when a paused run
    resumes, so editing while paused resumes a different topology — survivable for a
    query tool somebody is watching, not for something that writes into a CRM on a
    schedule.

    ``graph_hash`` is the sha256 of the canonical JSON of ``graph_data``. It makes "is
    this the same workflow" answerable without comparing two documents, and it is half
    of the determinism claim: the other half is the per-operation hash on each step row.

    Exactly one version per flow is ``published``. Enforced twice on purpose — a partial
    unique index, and a check inside ``publish_flow`` — because a partial index silently
    does not exist on SQLite unless ``sqlite_where`` is given, and the test suite runs on
    SQLite. A constraint that is only real in production is a constraint no test covers.
    """

    __tablename__ = "integration_flow_versions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True),
        default=uuid_pkg.uuid4,
        unique=True,
        index=True,
        nullable=False,
    )

    flow_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("integration_flows.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # 1-based and never reused. The number a person says out loud ("it broke in
    # version 4"), so it counts publishes rather than being a surrogate key.
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)

    # The frozen drawing. Never edited after insert.
    graph_data: Mapped[dict] = mapped_column(JSONB, nullable=False)

    # sha256 of the canonical JSON of graph_data, hex. See the class docstring.
    graph_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    # See VERSION_STATUSES.
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=VERSION_PUBLISHED,
        server_default=VERSION_PUBLISHED,
    )

    published_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True,
    )

    flow: Mapped["IntegrationFlow"] = relationship(
        "IntegrationFlow", back_populates="versions"
    )

    __table_args__ = (
        UniqueConstraint(
            "flow_id", "version_number", name="uq_integration_flow_versions_number"
        ),
        # One published version per flow. `sqlite_where` is set as well as
        # `postgresql_where` — see the class docstring for why leaving it off would make
        # this a constraint that only exists in production.
        Index(
            "uq_integration_flow_versions_one_published",
            "flow_id",
            unique=True,
            postgresql_where=text("status = 'published'"),
            sqlite_where=text("status = 'published'"),
        ),
    )


class IntegrationTrigger(Base):
    """
    What starts a run, and when.

    **Nothing about a schedule lives in memory.** ``next_run_at`` is on the row,
    computed and stored inside the same transaction that claims the trigger and enqueues
    the run. That is the whole reason the column exists: a scheduler that holds its
    timetable in a process loses it on deploy, and a freshly started scheduler must be
    able to fire a trigger that came due while nothing was running.

    ``kind`` is denormalised onto every run this trigger starts, because a run has to
    stay readable after the trigger is deleted — the same argument the step rows make
    for ``node_type``.
    """

    __tablename__ = "integration_triggers"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True),
        default=uuid_pkg.uuid4,
        unique=True,
        index=True,
        nullable=False,
    )

    flow_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("integration_flows.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Which `trigger` node on the drawing this configures — the client-minted string id
    # out of `graph_data["nodes"]`, not a foreign key. A flow has exactly one trigger
    # node today; the column is here so that stays a validation rule rather than a
    # schema assumption.
    node_id: Mapped[str] = mapped_column(String(64), nullable=False)

    # See TRIGGER_KIND_VALUES.
    kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default=TRIGGER_MANUAL, server_default=TRIGGER_MANUAL,
    )

    is_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false"),
    )

    # Phase 1 is interval-only. Floor of MIN_INTERVAL_SECONDS, enforced in validation.
    interval_seconds: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Phase 3. Cron needs a `croniter` dependency this project does not have, which is
    # exactly why Phase 1 is interval-only — the column is here so adding the dependency
    # is the whole of that change.
    cron_expression: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, default="UTC", server_default="UTC",
    )

    # When this is next due. NULL when the trigger is disabled or is not a schedule.
    # Backfilled whenever a trigger is enabled or its interval is edited; see the class
    # docstring on why it is a column rather than a computation.
    next_run_at: Mapped[Optional[DateTime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True,
    )

    last_fired_at: Mapped[Optional[DateTime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    # See OVERLAP_POLICY_VALUES.
    overlap_policy: Mapped[str] = mapped_column(
        String(20), nullable=False, default=OVERLAP_SKIP, server_default=OVERLAP_SKIP,
    )

    # Whether a trigger that has fallen behind fires once for every slot it missed.
    # Refused in Phase 1 — firing twelve missed hourly slots costs twelve times the API
    # quota for zero extra data, because an incremental sync's single catch-up run reads
    # everything those twelve would have.
    catch_up: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false"),
    )

    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
    )

    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(),
    )

    flow: Mapped["IntegrationFlow"] = relationship(
        "IntegrationFlow", back_populates="triggers"
    )

    __table_args__ = (
        # The scheduler's claim, in column order: matches
        # `WHERE is_enabled AND kind = 'schedule' AND next_run_at <= now()
        #  ORDER BY next_run_at` exactly. Every tick runs this query, so it is the one
        # index in this module that is read on a timer rather than on a page view.
        Index(
            "ix_integration_triggers_due", "is_enabled", "kind", "next_run_at",
        ),
    )


# ============================================================================
# Execution
# ============================================================================


class IntegrationRun(Base):
    """
    One execution: what it ran, what fired it, how far it got, and what it moved.

    The row is written *before* the version is compiled, so a compilation failure is
    still a run somebody can open rather than a button that did nothing.

    **The counters are the run's real story and the log is a sample of it.** A 50,000
    record sync writes four numbers here and at most a bounded handful of rows in
    ``integration_run_records``. ``records_log_truncated`` says the log stopped; the
    counters never do, which is why the run page shows "1,203 failed" and "1,000
    logged" as two separate numbers rather than pretending they are one.

    ``idempotency_key`` is what stops a schedule firing the same slot twice and a vendor
    redelivering the same webhook twice. **The unique insert is the dedupe** — checking
    first and inserting after is racy at exactly the moment it matters.

    ``cancel_requested`` is the durable half of cancellation; the in-process task
    dictionary is the fast half. Both are needed: the flag survives a request finishing
    and a different worker holding the run, and the task cancel makes it immediate in
    the single-replica case. The flag is written *before* the task is cancelled, or
    teardown races the write and the dock shows a run that stopped for no stated reason.
    """

    __tablename__ = "integration_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True),
        default=uuid_pkg.uuid4,
        unique=True,
        index=True,
        nullable=False,
    )

    flow_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("integration_flows.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # The pinned topology. NULL only for a manual run of a flow that has never been
    # published — which is also precisely the run that cannot be replayed, because
    # there is nothing to replay it *as*. Every scheduled and webhook run has one,
    # because only a published flow can carry an enabled trigger.
    flow_version_id: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        ForeignKey("integration_flow_versions.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    trigger_id: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        ForeignKey("integration_triggers.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Denormalised from the trigger, which may be deleted or reconfigured. See
    # TRIGGER_KIND_VALUES.
    trigger_kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default=TRIGGER_MANUAL, server_default=TRIGGER_MANUAL,
    )

    # See RUN_STATUSES.
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=RUN_QUEUED,
        server_default=RUN_QUEUED,
        index=True,
    )

    # "live" | "dry_run" — see RUN_MODES.
    mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default=RUN_MODE_LIVE, server_default=RUN_MODE_LIVE,
    )

    # `{trigger_uuid}:{scheduled_for}` for a schedule, the vendor's event id for a
    # webhook, NULL for a run somebody started by hand. See the class docstring and the
    # partial unique index below.
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # The LangGraph checkpointer thread. Same seam `ToolGraphRun.thread_id` spans: an
    # interrupt fires inside a worker task and is answered by a later HTTP request, and
    # this string is the only handle connecting the two.
    thread_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    # Phase 2, with the `approval` node. Non-NULL only while `status` is
    # `awaiting_input`, cleared on resume so a stale question cannot be answered twice.
    interrupt_payload: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    # What the run finished with, already capped and redacted by `flow_state.preview_of`
    # before it is written. Nothing downstream has to remember to trim, which is what
    # makes the cap a property of the table rather than of one renderer.
    result_preview: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    # The sentence to show for a failed run, in the operator's words. Written by the
    # engine from the recorded failure — **never** by the AI triage layer, which only
    # ever renders into a panel and is not permitted to modify the run it explains.
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # BigInteger because a full historical backfill of a busy store is not bounded by
    # anything an Integer covers, and discovering that at 2,147,483,648 records is a
    # bad way to discover it.
    #
    # These are bumped with `UPDATE … SET x = x + :n`, never read-modify-write: two
    # nodes can add at once, and the lost update would be silent.
    records_read: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0",
    )
    records_written: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0",
    )
    records_failed: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0",
    )
    records_skipped: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0",
    )

    # Whether `integration_run_records` stopped accepting rows for this run. The
    # counters above kept counting; see the class docstring.
    records_log_truncated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false"),
    )

    # The durable half of cancellation. Polled at the top of every node and between
    # chunks, so the contract stated in the UI is honest: cancel stops at the next
    # record boundary, not mid-request.
    cancel_requested: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false"),
    )

    # Which attempt of this run's *job* this is — a worker died and the job was
    # requeued. Deliberately not the same counter as a retried HTTP request, which is
    # about somebody else's server rather than about our own infrastructure.
    attempt: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1",
    )

    # The run this one repeats. A replay is a new run against the *same*
    # `flow_version_id`, which is what the versions table is for.
    replay_of_run_id: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        ForeignKey("integration_runs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # The schedule slot this run is for, which is not the same as when it started — a
    # queued run starts late. Half of the idempotency key, and the reason a late run is
    # still recognisably the 09:00 run.
    scheduled_for: Mapped[Optional[DateTime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    started_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True,
    )

    finished_at: Mapped[Optional[DateTime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    # Bumped while the run is moving. A `running` run whose heartbeat has gone stale is
    # a worker that died; Phase 1 requeues the job, marks the run failed with a sentence
    # saying so, and offers Replay. Resuming instead is Phase 4 work, gated on every
    # write node carrying an idempotency template — half-resuming a write into a CRM is
    # worse than a clear failure with a button.
    heartbeat_at: Mapped[Optional[DateTime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True,
    )

    steps: Mapped[list["IntegrationRunStep"]] = relationship(
        "IntegrationRunStep",
        back_populates="run",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="IntegrationRunStep.sequence",
    )

    __table_args__ = (
        # The dedupe. Partial so that the many manual runs with no key do not collide
        # with each other, unique so that a redelivered webhook or a double-fired
        # schedule slot loses the race instead of running twice. `sqlite_where` is set
        # for the reason given on the versions table.
        Index(
            "uq_integration_runs_idempotency",
            "flow_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
            sqlite_where=text("idempotency_key IS NOT NULL"),
        ),
        # The run list, and the scheduler's overlap check: this flow's runs, newest
        # first.
        Index("ix_integration_runs_flow_started", "flow_id", "started_at"),
    )


class IntegrationRunStep(Base):
    """
    One node, one pass through it.

    Written as ``running`` when the node starts and updated when it ends, so a node that
    hangs shows as a step that never finished rather than as a gap in the log.

    ``batch_index`` is which pass of the enclosing ``batch`` this was — zero for a node
    that runs once, which is most of them. Without it a loop's rows are
    indistinguishable and the dock cannot group them.

    **Rows collapse.** After ``STEP_COLLAPSE_AFTER`` passes for one ``(run_id,
    node_id)``, ``run_store.finish_step`` stops inserting and folds into the existing
    row marked ``is_rollup``, accumulating ``records_in``/``records_out`` and counting
    passes in ``rollup_count``. A hundred-pass loop is readable; a ten-thousand-pass
    backfill would otherwise write a log table larger than the data it describes. The
    count is kept rather than dropped, so the dock can say "one row standing for 9,500
    passes" instead of quietly implying there were 500.

    ``operation_hash`` is sha256 of the canonical JSON of the operation this step
    executed. It is the half of the determinism claim that the version hash does not
    cover: a replay that produces a different operation hash is detectably not the same
    run, which is only possible because operations are data rather than Python.

    ``egress_policy`` and ``resolved_ip`` are recorded whenever a request goes out under
    the private-host allow-list. If somebody's on-premise SAP door is open, every call
    through it should be answerable later with what it actually connected to.
    """

    __tablename__ = "integration_run_steps"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True),
        default=uuid_pkg.uuid4,
        unique=True,
        index=True,
        nullable=False,
    )

    run_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("integration_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Position in the run, assigned by the runner rather than by insertion order —
    # two rows can be written in the same millisecond and the dock reads the log in
    # the order the nodes ran.
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # The string id out of `graph_data["nodes"]`, not a foreign key. A node deleted
    # from a later version leaves this unresolvable, and the dock says so rather than
    # failing to load.
    node_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    # Denormalised on purpose: a log that changes retroactively when somebody renames a
    # node is a log nobody can trust.
    node_type: Mapped[str] = mapped_column(String(32), nullable=False)
    node_label: Mapped[str] = mapped_column(String(255), nullable=False, default="")

    # Which pass of the enclosing batch. See the class docstring.
    batch_index: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )

    # Which retry of this node. Distinct from `IntegrationRun.attempt`, which counts
    # the run being requeued.
    attempt: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1",
    )

    # See STEP_STATUSES.
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=STEP_RUNNING, server_default=STEP_RUNNING,
    )

    # How many records went in and how many came out. The two rarely match, and where
    # they do not is where a workflow is quietly dropping data — which is the single
    # most useful thing this log has to say.
    records_in: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0",
    )
    records_out: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0",
    )

    # Whether this row stands for many passes, and how many. See the class docstring.
    is_rollup: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false"),
    )
    rollup_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )

    # NULL while the node is still running, which is how the dock tells a slow node
    # from a finished one without comparing timestamps.
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # One human-readable line: what the node did, or why it could not.
    message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # What the node produced and the state after it — both already capped **and
    # redacted** before the row is written. This is the table an API response body can
    # reach, so redaction happening at write time is what makes it a property of the
    # data rather than of whoever renders it.
    output_preview: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    state_preview: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    # sha256 hex of the operation this step ran. NULL for a node that calls nobody.
    operation_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    # "public" for an ordinary call, "private_allowlisted" for one that went through
    # the on-premise escape hatch. NULL for a node that made no request.
    egress_policy: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    # What the hostname actually resolved to. 45 characters covers an IPv6 address
    # with an IPv4-mapped tail.
    resolved_ip: Mapped[Optional[str]] = mapped_column(String(45), nullable=True)

    started_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
    )

    finished_at: Mapped[Optional[DateTime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    run: Mapped["IntegrationRun"] = relationship(
        "IntegrationRun", back_populates="steps"
    )

    __table_args__ = (
        # Reading one run's log in order — once per poll, for as long as anybody is
        # watching.
        Index("ix_integration_run_steps_run_sequence", "run_id", "sequence"),
        # Finding the rollup row for a node without scanning the run's whole log. Read
        # once per pass of a long loop, which is the hottest write path in the module.
        Index("ix_integration_run_steps_run_node", "run_id", "node_id"),
    )


class IntegrationRunRecord(Base):
    """
    One record that did something other than move successfully.

    A run that writes 50,000 records cleanly writes nothing here. That asymmetry is the
    design: the counters carry the volume and this table carries the detail, so it stays
    small enough to read and cheap enough to keep.

    ``payload`` holds the **whole** record for a ``failed`` outcome, because replaying
    the failures is what the row is for. For the other outcomes it holds what is useful
    to see, already redacted.

    ``retryable`` is decided when the failure happens, not when somebody presses Replay.
    A ``ReadTimeout`` on a non-idempotent write is the case that matters: the request
    may well have reached the server, so re-sending it could duplicate an order, and
    only the code that made the call knows that. Deciding it later, from a stored
    message, is how a merchant ends up with two of everything.
    """

    __tablename__ = "integration_run_records"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True),
        default=uuid_pkg.uuid4,
        unique=True,
        index=True,
        nullable=False,
    )

    run_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("integration_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # The step this happened in. Nullable and SET NULL because step rows collapse: the
    # row a record was written against may be folded into a rollup, and losing the
    # pointer must not lose the record.
    step_id: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        ForeignKey("integration_run_steps.id", ondelete="SET NULL"),
        nullable=True,
    )

    node_id: Mapped[str] = mapped_column(String(64), nullable=False)

    batch_index: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )

    # See RECORD_OUTCOMES.
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)

    # The record's identity at each end: what it was called where it came from, and
    # what it became where it went. `target_key` is NULL for anything that never
    # arrived, which is most rows in this table.
    source_key: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    target_key: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # Why, in a sentence somebody can act on. For a rejected write this is the
    # destination's own message, which is usually more specific than anything we could
    # compose about it.
    message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # See the class docstring. Redacted before it is written.
    payload: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    retryable: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false"),
    )

    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
    )

    __table_args__ = (
        # The failed-records page, and the replay-the-failures query: one run's rows of
        # one outcome.
        Index("ix_integration_run_records_run_outcome", "run_id", "outcome"),
    )


class IntegrationRunJob(Base):
    """
    The queue row. One per run, claimed with ``FOR UPDATE SKIP LOCKED``.

    Kept apart from the run for the same reason ``download_jobs`` is kept apart from
    ``download_exports``: the worker writes a heartbeat here every few seconds while the
    run row is written once per milestone, and progress traffic should not contend with
    the row a page is reading.

    ``available_at`` is what makes a delayed retry expressible without a second
    mechanism — a job that failed and should be tried again in thirty seconds is a job
    whose ``available_at`` moved.

    The claim additionally refuses a job whose flow already has a run in flight, which
    is what makes ``overlap_policy = queue`` mean anything. That correlated ``NOT
    EXISTS`` is the subtlest query in the module and has its own test.
    """

    __tablename__ = "integration_run_jobs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True),
        default=uuid_pkg.uuid4,
        unique=True,
        index=True,
        nullable=False,
    )

    # Unique: a run is queued once. A second job for the same run would be two workers
    # executing one run, which is the failure this whole table exists to prevent.
    run_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("integration_runs.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
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

    # Higher runs first. A run somebody is sitting and watching should not wait behind
    # a nightly backfill.
    priority: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )

    # Not claimable before this. Defaults to now, moved forward by a backoff.
    available_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True,
    )

    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )

    # Which worker holds it: host plus task name, for reading a log rather than for any
    # decision. The claim itself is done by SKIP LOCKED, not by this.
    claimed_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    claimed_at: Mapped[Optional[DateTime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    # A running job whose heartbeat has gone stale is a worker that died.
    heartbeat_at: Mapped[Optional[DateTime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True,
    )

    finished_at: Mapped[Optional[DateTime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True,
    )

    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(),
    )

    __table_args__ = (
        # The claim query, in its ORDER BY order: highest priority first, then oldest
        # available. Matches `claim_next_job` exactly.
        Index(
            "ix_integration_run_jobs_claim",
            "status",
            text("priority DESC"),
            "available_at",
        ),
    )


class IntegrationCursor(Base):
    """
    Where an incremental read got to.

    Created in Phase 1 and **unused until Phase 2**, when incremental reads land. It is
    here now because its shape is settled and one migration is cheaper than two —
    adding a table later means a second Alembic revision, a second review and a second
    deploy for three columns.

    Keyed by ``(flow_id, node_id)`` rather than by version, because the watermark is a
    fact about the data this workflow has already seen and republishing the drawing does
    not un-see it.

    ``cursor_value`` is opaque text. A timestamp for one connector, a record id for
    another, a vendor's own cursor token for a third — interpreting it is the read
    operation's job, and giving it a type here would mean guessing which vendors are
    coming.
    """

    __tablename__ = "integration_cursors"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True),
        default=uuid_pkg.uuid4,
        unique=True,
        index=True,
        nullable=False,
    )

    flow_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("integration_flows.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    node_id: Mapped[str] = mapped_column(String(64), nullable=False)

    cursor_value: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Which run last moved it. Answers "why is this cursor here" without a guess.
    updated_by_run_id: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        ForeignKey("integration_runs.id", ondelete="SET NULL"),
        nullable=True,
    )

    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
    )

    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(),
    )

    __table_args__ = (
        UniqueConstraint("flow_id", "node_id", name="uq_integration_cursors_node"),
    )


# ============================================================================
# Connections and credentials
# ============================================================================


class IntegrationConnection(Base):
    """
    One authenticated relationship with one external account.

    **Many connections per connector is the point**, and that is a deliberate departure
    from ``ai_api_keys``, which allows one active key per provider. Three Shopify stores
    and forty GoHighLevel locations are the ordinary case here, not an edge case, so the
    uniqueness is on ``(user_id, connector_id, external_account_id)`` rather than on
    ``(user_id, connector_id)``.

    ``external_account_id`` is an identity, not a secret — a shop domain, a GHL location
    id — so it is plaintext and searchable. It is NULL for a generic REST connection,
    which has no account concept; Postgres treats NULLs as distinct in a unique
    constraint, so several such connections can coexist under one connector, which is
    correct.

    ``parent_connection_id`` is the agency-to-location relationship: a GoHighLevel
    company install issues location tokens, and the child connections are real
    connections with their own credentials rather than views onto the parent.

    **No secret is on this row.** Everything credential-shaped lives in
    ``IntegrationCredential`` behind a unique foreign key, so a connection view built
    for a template or a JSON response cannot serialise one by accident, and revoking is
    one ``DELETE`` that provably leaves nothing.

    ``allow_private_hosts`` and ``private_host_allowlist`` are the on-premise escape
    hatch. Setting them is gated three ways — an admin, a connector whose spec permits
    it at all, and a bounded explicit list — and every change writes a
    ``IntegrationCredentialEvent`` carrying the old and new list. The admin check lives
    in the service rather than the route, so a second route cannot skip it.
    """

    __tablename__ = "integration_connections"

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

    # Which connector spec this is an instance of: "rest_generic", "shopify",
    # "gohighlevel", "sap_odata". A string rather than a foreign key because the specs
    # are code and data files, not rows — see connectors/registry.py.
    connector_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    # What the user calls it. "Shopify EU", not the connector's name — this is what the
    # canvas shows and what the workflow generator resolves a model's spelling against.
    label: Mapped[str] = mapped_column(String(255), nullable=False)

    # See AUTH_KIND_VALUES.
    auth_kind: Mapped[str] = mapped_column(
        String(32), nullable=False, default=AUTH_NONE, server_default=AUTH_NONE,
    )

    # The root every operation's path is joined onto. NULL when the connector's spec
    # computes it — a Shopify base URL is derived from the shop domain, and letting it
    # be typed would be a way to point a trusted connector somewhere untrusted.
    base_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)

    # The account this connection is *for*. See the class docstring.
    external_account_id: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )

    parent_connection_id: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        ForeignKey("integration_connections.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # See CONNECTION_STATUSES.
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=CONNECTION_ACTIVE,
        server_default=CONNECTION_ACTIVE,
        index=True,
    )

    # The on-premise escape hatch. See the class docstring.
    allow_private_hosts: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false"),
    )

    # {"hosts": ["sap.internal:443"], "cidrs": ["10.42.0.0/16"]}. Both halves are
    # required for a private address to be permitted — a hostname alone falls to a DNS
    # answer the operator does not control, and a CIDR alone permits any hostname that
    # happens to resolve into the range.
    private_host_allowlist: Mapped[Optional[dict]] = mapped_column(
        JSONB, nullable=True
    )

    # The user's own on/off switch, independent of `status`. A connection can be
    # perfectly authenticated and deliberately parked.
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true"),
    )

    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True,
    )

    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(),
    )

    credential: Mapped[Optional["IntegrationCredential"]] = relationship(
        "IntegrationCredential",
        back_populates="connection",
        uselist=False,
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "connector_id",
            "external_account_id",
            name="uq_integration_connections_account",
        ),
    )


class IntegrationCredential(Base):
    """
    The secrets for one connection — one row, one connection, nothing else in here.

    **One column per secret**, following ``datasources``: host, port and username are
    plaintext there and only the password is ciphertext. The alternative — a single
    encrypted JSON blob — reads as tidier and is worse in every operational way: you
    cannot tell whether a refresh token exists without decrypting, a re-encryption pass
    has nothing to iterate, and a partial write corrupts every secret at once instead of
    one.

    Everything ``*_encrypted`` goes through ``app/utils/crypto.py``. Everything else on
    this row is deliberately readable: ``client_id`` is public by definition, ``scope``
    and ``expires_at`` have to be queryable to decide whether a refresh is due, and a
    client certificate is a public document — its *key* is the ciphertext beside it.

    **The refresh lock is a compare-and-set, not a row lock.** ``refresh_lock_token``
    plus ``refresh_lock_expires_at`` are claimed with a conditional ``UPDATE``; losers
    poll briefly. A ``FOR UPDATE`` would hold an open transaction and a pooled
    connection for the length of an outbound HTTP call, across every concurrent node in
    every run. The TTL also survives a refresher that crashed, which a transaction lock
    only manages by accident — and a CAS works on SQLite, so the two-concurrent-refreshers
    test runs in the ordinary suite instead of not existing.

    **Rotation ordering is load-bearing.** GoHighLevel and Shopify online tokens rotate
    the refresh token on use, so the sequence is exchange → write → commit → *then*
    use. If the exchange succeeds and the write does not, the stored token is already
    dead and the connection is locked out permanently. And a ``400 invalid_grant`` is
    never retried: retrying it is exactly how a working connection gets burned.
    """

    __tablename__ = "integration_credentials"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True),
        default=uuid_pkg.uuid4,
        unique=True,
        index=True,
        nullable=False,
    )

    connection_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("integration_connections.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )

    # --- ciphertext ---------------------------------------------------------
    api_key_encrypted: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    access_token_encrypted: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    refresh_token_encrypted: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    client_secret_encrypted: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    password_encrypted: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    client_key_encrypted: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # --- plaintext, on purpose ----------------------------------------------
    client_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    username: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # The certificate, not the key. The key is `client_key_encrypted` above.
    client_cert_pem: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    token_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    # What the token was actually granted, as the provider reported it — which is not
    # always what was asked for. A node failing on a missing scope should be able to say
    # which scope, rather than "403".
    scope: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Indexed because the refresh check reads it on every connector node.
    expires_at: Mapped[Optional[DateTime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True,
    )

    refreshed_at: Mapped[Optional[DateTime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    # Consecutive failures. Reset on success. A connection that has failed to refresh
    # repeatedly is one to stop retrying and start asking the user about.
    refresh_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )

    # The CAS lock. See the class docstring.
    refresh_lock_token: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    refresh_lock_expires_at: Mapped[Optional[DateTime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
    )

    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(),
    )

    connection: Mapped["IntegrationConnection"] = relationship(
        "IntegrationConnection", back_populates="credential"
    )


class IntegrationOAuthState(Base):
    """
    One OAuth install in flight.

    **The state value itself is never stored** — only ``sha256(state)``. Somebody with
    read access to this table therefore cannot complete an install they did not start,
    which is the same reasoning that keeps a password out of a users table. The callback
    hashes what it was given and looks that up.

    ``consumed_at`` is set **in the same transaction as the lookup and before the token
    exchange**, so a replayed callback loses the race rather than exchanging a second
    time. Marking it after the exchange would make the window exactly as long as the
    provider's response time.

    ``redirect_after`` is validated as a relative path before it is stored. An
    unvalidated one is an open redirect wearing a convenience feature's clothes.
    """

    __tablename__ = "integration_oauth_states"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True),
        default=uuid_pkg.uuid4,
        unique=True,
        index=True,
        nullable=False,
    )

    # Who started it. Cross-checked against the callback's own session, so a state
    # minted by one user cannot be completed by another.
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    connector_id: Mapped[str] = mapped_column(String(64), nullable=False)

    # Set when this is a reconnect of an existing connection rather than a new install.
    connection_id: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        ForeignKey("integration_connections.id", ondelete="CASCADE"),
        nullable=True,
    )

    # sha256 hex of the state parameter. See the class docstring.
    state_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True,
    )

    # PKCE. Encrypted because it is the second half of the proof, and a stolen one
    # combined with an intercepted code completes the exchange.
    code_verifier_encrypted: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Where to send the browser afterwards. A relative path, validated before storage.
    redirect_after: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)

    # OAUTH_STATE_TTL_SECONDS from creation. Indexed because the reaper reads it.
    expires_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True,
    )

    consumed_at: Mapped[Optional[DateTime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
    )


class IntegrationCredentialEvent(Base):
    """
    The audit trail on a connection's credentials.

    Every event that changes what a connection can do, or records that it stopped being
    able to do it. This is the table somebody reads when asking "when did this stop
    working, and did anyone change anything" — a question that is otherwise answered by
    guessing.

    ``detail`` never holds a secret. Not "holds a masked secret" — holds none. A masking
    function is one refactor away from being bypassed, and this is the one table whose
    whole purpose is to be readable by somebody investigating an incident.
    """

    __tablename__ = "integration_credential_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True),
        default=uuid_pkg.uuid4,
        unique=True,
        index=True,
        nullable=False,
    )

    connection_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("integration_connections.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Who did it. NULL for a background refresh, which nobody did — and that
    # distinction is exactly what makes the column worth having.
    user_id: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    # See CREDENTIAL_EVENTS.
    event: Mapped[str] = mapped_column(String(32), nullable=False)

    # Context, never a secret. For an allow-list change: the old and new lists. For a
    # reauth_required raised mid-run: the run's uuid.
    detail: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True,
    )


# ============================================================================
# Connector data
# ============================================================================


class IntegrationRestOperation(Base):
    """
    One operation on a generic REST connection, as a row.

    The columns mirror ``connectors/spec.py::OperationSpec`` field for field, and
    ``load_operation()`` returns the same frozen dataclass whether it came from here or
    from a vendor connector's own declaration. **One request builder, one pagination
    implementation, one retry path** — building the vendor connectors first and this
    later would have produced two of each, and the user-facing one is the one that rots.

    Operations being data rather than Python is also what makes determinism checkable:
    every step row records ``sha256`` of the operation's canonical JSON, so a replay
    that ran something different is detectable. A Python operation can only record a
    module path and a commit, and a hotfix silently changes what "replay" means.

    ``method``, ``path`` and the templates are the whole of the request. Building it is
    a **pure** function — no HTTP, no database, no credential — which is what turns
    every URL-escaping and injection question into a table-driven unit test.
    """

    __tablename__ = "integration_rest_operations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True),
        default=uuid_pkg.uuid4,
        unique=True,
        index=True,
        nullable=False,
    )

    connection_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("integration_connections.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # The stable name a node refers to. Unique per connection, so renaming the label
    # does not rewire every workflow using it.
    operation_id: Mapped[str] = mapped_column(String(64), nullable=False)

    label: Mapped[str] = mapped_column(String(255), nullable=False)

    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # "read" | "write" — see OPERATION_KINDS. A `connector_read` node may only pick a
    # read and a `connector_write` only a write.
    kind: Mapped[str] = mapped_column(String(8), nullable=False)

    method: Mapped[str] = mapped_column(String(8), nullable=False)

    # Joined onto the connection's base_url. May contain `{name}` placeholders, each
    # filled from a declared input and escaped with `quote(safe="")` — a path parameter
    # is the shortest route from a record's contents to a request for a different URL.
    path: Mapped[str] = mapped_column(String(512), nullable=False)

    # {name: template}. Values are typed and rendered, never string-concatenated.
    query_template: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    header_template: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    # The body, as a structure. **Assembled from typed values and serialised**, never
    # templated as a string. The chatbot's actions template JSON because a language
    # model supplies strings and there is nothing else to do; here the values are typed
    # before they arrive, so producing invalid JSON is not a failure mode that has to
    # exist.
    body_template: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    # A list of FieldSpec dicts: what this operation accepts, and what it returns. The
    # field list *is* the schema — no `jsonschema` dependency, because the same list is
    # already needed to draw the mapping panel's field picker, and one description of a
    # field is better than two that can disagree.
    inputs: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    outputs: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)

    # Where the records are in the response body: a restricted path like
    # `data.orders`. Empty means the body is the list.
    records_path: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # A PageRule dict: which of the six pagination kinds, and its parameters.
    page_rule: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    # Whether re-sending this request is safe. **False by default, and that default is
    # the safe one**: a write that is retried after a read timeout may already have
    # happened, and creating a second order is not something a backoff can undo.
    idempotent: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false"),
    )

    # A header the destination honours for deduplication, if it has one. Its presence
    # is what makes retrying a timed-out write safe, so it is stored per operation
    # rather than assumed per vendor.
    idempotency_header: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True
    )

    # Whether the destination cares about the order records arrive in. Forces
    # parallelism to 1 — `asyncio.gather` reorders the wire even though it preserves
    # the results, which is a correctness bug for an order-sensitive destination and
    # invisible everywhere else.
    ordered: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false"),
    )

    timeout_seconds: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
    )

    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "connection_id", "operation_id", name="uq_integration_rest_operations_id"
        ),
    )


class IntegrationSyncKey(Base):
    """
    What a record from one system became in another.

    The second layer of write safety, under the idempotency rules in
    ``engine/idempotency.py``: before creating, look the natural key up here and switch
    to an update if it is already present. Shopify's ``POST /orders.json`` has no
    idempotency header, so a retried create after a timeout duplicates the order and no
    amount of backoff prevents it — this table is how the *next* run avoids repeating
    it.

    ``natural_key_sha256`` rather than the key itself: a natural key is usually an email
    address or a customer name, which is somebody's personal data and does not need to
    be stored to be matched.
    """

    __tablename__ = "integration_sync_keys"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True),
        default=uuid_pkg.uuid4,
        unique=True,
        index=True,
        nullable=False,
    )

    connection_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("integration_connections.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    operation_id: Mapped[str] = mapped_column(String(64), nullable=False)

    natural_key_sha256: Mapped[str] = mapped_column(String(64), nullable=False)

    # The destination's own id for the record. Text because an id is a vendor's
    # decision — a bigint, a GID URI, a UUID.
    target_record_id: Mapped[str] = mapped_column(String(255), nullable=False)

    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
    )

    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "connection_id",
            "operation_id",
            "natural_key_sha256",
            name="uq_integration_sync_keys_natural",
        ),
    )


class IntegrationRateCounter(Base):
    """
    How many calls this connection has made today.

    **Persisted rather than held in memory, and that is not an optimisation.**
    GoHighLevel caps a location at 200,000 requests per day; an in-memory counter resets
    on every deploy, so a day with four deploys can spend four times the cap while
    believing it spent one. A marketplace application that blows its daily cap gets
    suspended, which is the most account-endangering failure in this whole module.

    Bumped with ``INSERT … ON CONFLICT DO UPDATE … RETURNING`` so the read and the write
    are one statement — the per-second bucket can afford to be approximate, but the
    daily cap cannot.

    The per-second limits stay in memory (``runtime/rate_limiter.py``), because at that
    rate a database round trip per request would cost more than the limit does. Note
    that an in-process bucket is per worker, so under ``uvicorn --workers N`` the
    effective send rate is N×; the sync worker runs as a single in-process loop for
    that reason, and this table is the backstop that holds regardless.
    """

    __tablename__ = "integration_rate_counters"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True),
        default=uuid_pkg.uuid4,
        unique=True,
        index=True,
        nullable=False,
    )

    connection_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("integration_connections.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # A date rather than a timestamp: the window this counts is the vendor's own
    # calendar day, and storing an instant would invite arithmetic that gets the
    # boundary wrong.
    window_start_date: Mapped[Date] = mapped_column(Date, nullable=False)

    count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0",
    )

    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "connection_id",
            "window_start_date",
            name="uq_integration_rate_counters_window",
        ),
    )
