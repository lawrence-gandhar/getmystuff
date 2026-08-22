"""
Email Dispatch — the SMTP servers mail goes out through, the templates it is written
from, the triggers that decide when, and one permanent row per message.

See ``documentations/EMAIL_DISPATCH.md`` for the design this implements. What follows is
the reasoning specific to the tables.

**Why this module exists at all.** Before it, nothing in the platform could tell a human
anything. A graph run that found a problem, a sync that failed at three in the morning,
an agent that produced an answer worth circulating — every one of them ended silently.

**The message row is the queue *and* the log.** ``integration_runs`` is split from
``integration_run_jobs`` because a run is a rich domain object and a job is queue
mechanics; the run row is written once per milestone while the job row takes a heartbeat
every few seconds, and progress traffic should not contend with the row a page is
reading. Here the message *is* the unit of work: there is no second domain object behind
it, so a second table would be two rows that must agree about one email. One table, and
the claim columns live on it.

**The content is rendered at enqueue and stored, not rendered at send.** This is the
decision the whole module turns on, and the alternative fails in two separate ways. The
variables come from a live run — a graph's ``outputs``, a chat session's variables, an
agent's prompt variables — and none of that exists any more by the time a worker picks
the row up thirty seconds or three retries later. And a log that stores the template plus
a variable map cannot answer "what did we actually send them", because the template has
been edited since. So ``subject``, ``body_html`` and ``body_text`` are the finished text,
and a retry re-sends bytes that are identical by construction rather than by hope.

**Everything a message needs to stay readable is copied onto it.** ``template_name``,
``smtp_host``, ``from_email`` and ``trigger_kind`` are denormalised at enqueue and the
foreign keys are all ``ON DELETE SET NULL``. The same call ``integration_runs`` makes
about its trigger kind, for the same reason: deleting a template must not turn six months
of delivery history into rows that say nothing. A log that only reads correctly while
every row it points at still exists is not a log.

**Secrets stay on their own rows rather than in a separate table.**
``integration_credentials`` is a table of its own because it holds six different secrets
*plus* OAuth refresh state and a compare-and-set lock, and because revoking a connection
has to provably leave nothing behind. Neither applies here: an SMTP config has exactly one
secret and a trigger has exactly one, there is no refresh flow and no expiry. So
``password_encrypted`` and ``webhook_secret_encrypted`` sit on their own rows the way
``datasources.password_encrypted`` and ``chatbot_actions.headers_encrypted`` do. What
keeps them out of a response is the schema layer — every view names its fields explicitly
and a test asserts none of them is a secret — rather than the physical layout.

**Statuses and kinds are plain strings, not Enum types.** The same call
``ToolConfig.query_mode``, ``DownloadExport.status`` and ``IntegrationRun.status`` all
make: adding a state should be a constant and a validator, not a migration that rewrites
a type while every table using it is locked. Every write goes through
``app/services/email_dispatch/``, which validates against the frozensets below.

**Ownership is the user; the workspace is who else may use it.** ``user_id`` is not null
and is the only thing needed to read, edit or send with a config, so a config can exist
attached to nothing. ``workspace_id`` is nullable and not unique — "shared with a team" is
a one-to-many by nature — and deleting a workspace detaches (``ON DELETE SET NULL``)
rather than destroying an SMTP server somebody spent an afternoon getting past a
corporate relay. That is the split ``ToolGraph`` and ``DataAgent`` already make.

**Deliberately not here.** No scheduled-trigger columns: ``cron_expression`` and
``next_run_at`` would need a scheduler loop, this module was not asked for one, and a
column added now would be a promise the product does not keep. No bounce/complaint table:
SMTP tells us at send time whether a recipient was rejected and that goes in
``smtp_response``; asynchronous bounce handling needs an inbound mail path that does not
exist. No event table behind the trigger — provenance is ``EmailMessage.source`` and
``source_ref``, which is what anybody asking "why was this sent" actually needs.

Only ``uuid`` ever leaves this module. The bigint ``id`` is the primary key and the target
of the foreign keys between these tables, and nothing else — every URL, form field and
JSON payload names a config, a template, a trigger or a message by its uuid.
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
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


# ============================================================================
# Vocabularies
# ============================================================================
# Each is a tuple of ``(value, label)`` pairs, a ``frozenset`` of the values derived from
# it, and a label dict derived from it. The house convention, and it is load-bearing: the
# validator, the dispatcher and the form that offers the choice all read the same list, so
# none of the three can offer or accept something the others do not know about.


# ----------------------------------------------------------------------------
# How the SMTP connection is secured
# ----------------------------------------------------------------------------
# `starttls` is the default everywhere it is offered, because it is what port 587 wants
# and port 587 is what nearly every provider documents. `none` exists for a relay inside
# a private network and is refused by `smtp_service` unless the host is allow-listed —
# plaintext SMTP to a public host would put the password on the wire.

SECURITY_NONE = "none"
SECURITY_STARTTLS = "starttls"
SECURITY_SSL = "ssl"

SMTP_SECURITIES = (
    (SECURITY_STARTTLS, "STARTTLS (usually port 587)"),
    (SECURITY_SSL, "SSL/TLS (usually port 465)"),
    (SECURITY_NONE, "No encryption (private relay only)"),
)
SMTP_SECURITY_VALUES = frozenset(value for value, _ in SMTP_SECURITIES)
SMTP_SECURITY_LABELS = dict(SMTP_SECURITIES)


# ----------------------------------------------------------------------------
# Where a message is in its life
# ----------------------------------------------------------------------------
# `sending` is a real persisted state and not a transient one held in the worker, which is
# what makes a dead worker detectable at all: a row stuck in `sending` with a stale
# heartbeat is a worker that died, and there is nowhere else that fact could be recorded.

MESSAGE_QUEUED = "queued"
MESSAGE_SENDING = "sending"
MESSAGE_SENT = "sent"
MESSAGE_FAILED = "failed"
MESSAGE_CANCELLED = "cancelled"

MESSAGE_STATUSES = (
    (MESSAGE_QUEUED, "Queued"),
    (MESSAGE_SENDING, "Sending"),
    (MESSAGE_SENT, "Sent"),
    (MESSAGE_FAILED, "Failed"),
    (MESSAGE_CANCELLED, "Cancelled"),
)
MESSAGE_STATUS_VALUES = frozenset(value for value, _ in MESSAGE_STATUSES)
MESSAGE_STATUS_LABELS = dict(MESSAGE_STATUSES)

#: Statuses from which nothing further will happen on its own. A message in one of these
#: is only moved again by an operator pressing Retry.
TERMINAL_MESSAGE_STATUSES = frozenset(
    {MESSAGE_SENT, MESSAGE_FAILED, MESSAGE_CANCELLED}
)


# ----------------------------------------------------------------------------
# What asked for this message
# ----------------------------------------------------------------------------

SOURCE_MANUAL = "manual"
SOURCE_NODE = "node"
SOURCE_EVENT = "event"
SOURCE_WEBHOOK = "webhook"

MESSAGE_SOURCES = (
    (SOURCE_MANUAL, "Sent by hand"),
    (SOURCE_NODE, "An Email node in a flow"),
    (SOURCE_EVENT, "Something happened in the app"),
    (SOURCE_WEBHOOK, "An external system called in"),
)
MESSAGE_SOURCE_VALUES = frozenset(value for value, _ in MESSAGE_SOURCES)
MESSAGE_SOURCE_LABELS = dict(MESSAGE_SOURCES)


# ----------------------------------------------------------------------------
# What makes a trigger fire
# ----------------------------------------------------------------------------
# There is no `schedule` kind. Adding one means adding a scheduler loop, and this module
# deliberately adds exactly one background loop (the send worker). Naming the kind here
# without the loop behind it would put a choice in the UI that never fires.

TRIGGER_EVENT = "event"
TRIGGER_WEBHOOK = "webhook"

TRIGGER_KINDS = (
    (TRIGGER_EVENT, "When something happens in the app"),
    (TRIGGER_WEBHOOK, "When an external system calls in"),
)
TRIGGER_KIND_VALUES = frozenset(value for value, _ in TRIGGER_KINDS)
TRIGGER_KIND_LABELS = dict(TRIGGER_KINDS)


# ----------------------------------------------------------------------------
# Where a template variable's value comes from
# ----------------------------------------------------------------------------
# The closed list that replaces an expression evaluator. A binding names one of these and
# nothing else, which is the same discipline `engine/transform.py` applies to record
# transforms and `mapping/paths.py` to path reads: a named source and a restricted reader,
# never a string somebody's code will evaluate.
#
# Not every source is available on every canvas — a Flow Builder node has session
# variables and no upstream node outputs, an integration node has records and no chat
# session. `variable_sources.resolve_bindings` refuses an unavailable source by name
# rather than resolving it to an empty string, because an email sent with a blank where a
# customer's name should be is worse than one not sent.

BINDING_LITERAL = "literal"
BINDING_AGENT = "agent"
BINDING_SESSION = "session"
BINDING_NODE = "node"
BINDING_RECORD = "record"
BINDING_EVENT = "event"

BINDING_SOURCES = (
    (BINDING_LITERAL, "A fixed value"),
    (BINDING_AGENT, "A variable from the Agents section"),
    (BINDING_SESSION, "A value the conversation collected"),
    (BINDING_NODE, "The output of an earlier node"),
    (BINDING_RECORD, "A field on the current record"),
    (BINDING_EVENT, "A field on the incoming payload"),
)
BINDING_SOURCE_VALUES = frozenset(value for value, _ in BINDING_SOURCES)
BINDING_SOURCE_LABELS = dict(BINDING_SOURCES)


# ============================================================================
# Caps
# ============================================================================
# Named here rather than inline in the validators so the form, the service and the docs
# can quote one number. `USER_GUIDE.md` §"Every limit in one place" reads these.

#: Declared variables per template. Matches the Agents section's own cap so an operator
#: who has met one has met both.
MAX_TEMPLATE_VARIABLES = 30

#: Characters in one substituted value. Also the Agents section's number.
MAX_VARIABLE_VALUE_LENGTH = 500

#: What a variable may be called. Upper-case only, so `{{company}}` and `{{COMPANY}}`
#: cannot be two different variables — the same rule `chatbot_ai_settings_service`
#: enforces, and for the same reason.
VARIABLE_NAME_PATTERN = r"^[A-Z][A-Z0-9_]{0,49}$"

#: Addresses across to + cc + bcc on one message. A template is not a mailing list; a
#: send that wants more than this wants a different feature, and saying so is kinder than
#: timing out against the provider's own limit.
MAX_RECIPIENTS = 50

#: How many times a retryable failure is retried before the message is left failed.
DEFAULT_MAX_ATTEMPTS = 5

#: The floor on a webhook trigger's throttle. A public endpoint with no floor is a way to
#: make this application send mail as fast as somebody can POST.
MIN_WEBHOOK_INTERVAL_SECONDS = 1


# ============================================================================
# Configuration
# ============================================================================


class EmailSmtpConfig(Base):
    """
    One SMTP server this application may send through.

    Many per user is the ordinary case rather than an edge case — a transactional relay
    for receipts and a separate one for internal alerts, so that a blown sending quota on
    one does not take out the other. Uniqueness is therefore on ``(user_id, name)``:
    the name is what an operator picks in a node's dropdown, and two configs with one
    name would make that choice meaningless.

    **The password is the only ciphertext.** Host, port and username are plaintext, the
    same split ``datasources`` makes. Encrypting the host would buy nothing — it is in
    every message row's ``smtp_host`` for the log to be readable — and would cost the
    ability to query by it.

    ``last_tested_at`` / ``last_test_ok`` / ``last_test_message`` exist because "the
    email never arrived" is reported to whoever runs the platform, not to whoever
    configured this row, and the first question is always whether it ever worked. Written
    by the Send-test button and by nothing else: a *send* failure belongs on the message
    it failed, not smeared over the config.
    """

    __tablename__ = "email_smtp_configs"

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

    # Which workspace this config is shared with: anyone working in that workspace may
    # send through it. Not unique — a team's shelf may hold several servers. SET NULL on
    # delete, so dissolving a team detaches the config rather than destroying it.
    workspace_id: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        ForeignKey("workspaces.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    host: Mapped[str] = mapped_column(String(255), nullable=False)

    port: Mapped[int] = mapped_column(Integer, nullable=False)

    # See SMTP_SECURITIES.
    security: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=SECURITY_STARTTLS,
        server_default=SECURITY_STARTTLS,
    )

    # Nullable: an internal relay that authenticates by IP has no credentials at all, and
    # requiring a username would mean inventing one.
    username: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # Fernet ciphertext via app/utils/crypto.py. Nullable for the same reason as
    # `username` — no credentials is a legitimate configuration, not a missing field.
    password_encrypted: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # The envelope sender. Separate from the account's own email because a relay
    # frequently authenticates as one identity and is permitted to send as another.
    from_email: Mapped[str] = mapped_column(String(320), nullable=False)

    from_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    reply_to: Mapped[Optional[str]] = mapped_column(String(320), nullable=True)

    timeout_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=30, server_default="30",
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true"),
    )

    last_tested_at: Mapped[Optional[DateTime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    last_test_ok: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)

    last_test_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
    )

    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(),
    )

    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_email_smtp_configs_user_name"),
    )


class EmailTemplate(Base):
    """
    One email an operator has written, with its placeholders declared.

    **The declared variable list is the contract.** ``variables`` holds
    ``[{"name", "label", "required", "default"}]`` and the bodies may only reference names
    on it — the same relationship ``ChatbotAiSettings.variables`` has with its system
    prompt, and refused at save time for the same reason. Without the declaration a node
    could not offer anything to bind: the property panel builds one row per declared
    variable, so a template that declares nothing is a template no node can fill in.

    **Both bodies, and the HTML one is not optional-by-accident.** ``body_text_template``
    is nullable because plenty of internal alerts are plain text and inventing an HTML
    version of them is noise. When both are present the message is sent ``multipart/
    alternative``, which is what stops a text-only client showing markup.

    Uniqueness on ``(user_id, name)`` for the same reason as the SMTP config: the name is
    what a node's dropdown shows.
    """

    __tablename__ = "email_templates"

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

    workspace_id: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        ForeignKey("workspaces.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    subject_template: Mapped[str] = mapped_column(Text, nullable=False)

    body_html_template: Mapped[str] = mapped_column(Text, nullable=False)

    # Nullable, and when it is NULL the message goes out as HTML only. See the docstring.
    body_text_template: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # [{"name": "CUSTOMER", "label": "Customer name", "required": true, "default": ""}]
    # Order is the display order in the settings UI and in a node's property panel, so it
    # is never sorted — the operator's grouping is information.
    #
    # A plain JSONB column, replaced wholesale rather than mutated: like `graph_data` and
    # unlike nothing here, because SQLAlchemy cannot see an in-place change to a
    # non-Mutable JSON column and the write would vanish without an error.
    variables: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list,
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true"),
    )

    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
    )

    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(),
    )

    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_email_templates_user_name"),
    )


class EmailTrigger(Base):
    """
    A standing instruction to send a template when something happens.

    This is the half of the module that works without a canvas. A node in a graph is an
    explicit "send here"; a trigger is "whenever *that* occurs, send this", and the two
    reach the same ``dispatch_service.enqueue_email`` so there is no second send path.

    ``kind`` decides which of the two column groups matters, and the service refuses the
    combination rather than a constraint doing it, because the refusal has a sentence to
    say:

    * ``event`` uses ``event_name`` — one of ``app/utils/events.EVENT_NAMES``.
    * ``webhook`` uses ``webhook_endpoint_id`` and ``webhook_secret_encrypted``.

    **The endpoint id is not this row's ``uuid``.** It is a second, separately rotatable
    UUID, and that separation is the point: leaking the public URL must be fixable by
    rotating one column, not by deleting the trigger and rebuilding every external system
    that calls it. ``ChatbotFlowSession`` makes the same distinction for the same reason —
    its ``session_token`` is never the row's own ``uuid``.

    ``min_interval_seconds`` is a floor between firings, and a public unauthenticated
    endpoint is why it is not optional. Without it, the URL is a way to make this
    application send mail as fast as somebody can POST to it.

    ``variable_bindings`` maps each of the template's declared variables to where its
    value comes from — ``{"CUSTOMER": {"source": "event", "path": "customer.name"}}``.
    Validated against the template's declared list at save time, so a trigger cannot be
    saved half-bound and discovered at three in the morning.
    """

    __tablename__ = "email_triggers"

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

    workspace_id: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        ForeignKey("workspaces.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    # See TRIGGER_KINDS.
    kind: Mapped[str] = mapped_column(String(16), nullable=False, index=True)

    # Set when kind == event. Indexed together with `is_enabled` below, because the
    # subscriber reads this table by exactly that pair on every publish.
    event_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    # Set when kind == webhook. The public URL segment, rotatable independently of the
    # row's own uuid — see the class docstring.
    webhook_endpoint_id: Mapped[Optional[uuid_pkg.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True, unique=True, index=True,
    )

    # Fernet ciphertext. The HMAC key the caller signs its body with.
    webhook_secret_encrypted: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    min_interval_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=60, server_default="60",
    )

    # RESTRICT rather than SET NULL or CASCADE, and this is the one place in the module
    # that refuses a delete instead of absorbing it. A trigger whose template vanished
    # would fire into nothing on an event nobody is watching, which is the definition of
    # a silent failure. The service deletes or disables the trigger first and says so.
    template_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("email_templates.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    smtp_config_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("email_smtp_configs.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    # {"to": [...], "cc": [...], "bcc": [...]}. Entries may contain {{VARIABLE}}, which
    # is how "email whoever the event was about" is expressed without a rule engine.
    recipients: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict,
    )

    # {"CUSTOMER": {"source": "event", "path": "customer.name"}}
    variable_bindings: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict,
    )

    is_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true"),
    )

    last_fired_at: Mapped[Optional[DateTime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
    )

    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(),
    )

    template = relationship("EmailTemplate", lazy="selectin")
    smtp_config = relationship("EmailSmtpConfig", lazy="selectin")

    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_email_triggers_user_name"),
        # What the event subscriber reads on every publish, in the order it filters by.
        # Without this the bus would table-scan email_triggers once per published event.
        Index(
            "ix_email_triggers_event_lookup",
            "event_name",
            "is_enabled",
        ),
    )


# ============================================================================
# The queue, which is also the log
# ============================================================================


class EmailMessage(Base):
    """
    One email: what was sent, to whom, whether it arrived, and how many tries it took.

    Claimed with ``FOR UPDATE SKIP LOCKED`` exactly as ``integration_run_jobs`` is, and
    the claim additionally refuses a message whose SMTP config already has one in
    ``sending``. **That correlated ``NOT EXISTS`` is the subtlest query in the module.**
    It is what stops eight workers opening eight simultaneous connections to one
    provider and being rate-limited or blocked as a suspected sender; per-server
    serialisation is the thing that actually bounds pressure, and putting it in the claim
    rather than in the worker is what makes it true when it matters — two workers in the
    check-then-act window is precisely the case a worker-side check misses.

    ``next_attempt_at`` is what makes a delayed retry expressible without a second
    mechanism: a message that failed and should be tried again in thirty seconds is a
    message whose ``next_attempt_at`` moved and whose status went back to ``queued``.

    **A dead worker fails the message; it does not resume it.** A worker that stopped
    reporting mid-``sending`` may already have handed the message to the server, and
    trying again would deliver it twice. ``requeue_stale_emails`` therefore marks it
    ``failed`` with a reason that says delivery is unknown, and leaves an operator to
    press Retry knowing what they are risking. The same call ``requeue_stale_run_jobs``
    makes about a half-written CRM, and the same reason: a duplicate is not always
    cheaper than a gap, so the decision belongs to a person.

    ``idempotency_key`` is unique and nullable. Nullable because a hand-sent test has
    nothing to be idempotent about; unique because an event delivered twice, or a webhook
    retried by a caller that did not see our 200, must produce one email. Postgres permits
    many NULLs in a unique column, which is what lets one constraint express both.
    """

    __tablename__ = "email_messages"

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

    workspace_id: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        ForeignKey("workspaces.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # ------------------------------------------------------------------
    # Where it came from. All three SET NULL: the log outlives its sources.
    # ------------------------------------------------------------------

    trigger_id: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        ForeignKey("email_triggers.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    template_id: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        ForeignKey("email_templates.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    smtp_config_id: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        ForeignKey("email_smtp_configs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # See MESSAGE_SOURCES.
    source: Mapped[str] = mapped_column(String(16), nullable=False, index=True)

    # Free text naming the specific origin: a graph run uuid, a flow node id, a trigger
    # uuid. Deliberately not a foreign key — it points into four different tables
    # depending on `source`, and two of the things it can name (a node id inside a JSONB
    # drawing) are not rows at all.
    source_ref: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # ------------------------------------------------------------------
    # What was sent. Denormalised at enqueue — see the module docstring.
    # ------------------------------------------------------------------

    template_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    trigger_kind: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)

    smtp_host: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    from_email: Mapped[str] = mapped_column(String(320), nullable=False)

    from_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    reply_to: Mapped[Optional[str]] = mapped_column(String(320), nullable=True)

    # Lists of addresses. JSONB rather than a child table: they are written once, read
    # once, and never queried by member.
    to_addresses: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list,
    )

    cc_addresses: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list,
    )

    bcc_addresses: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list,
    )

    subject: Mapped[str] = mapped_column(Text, nullable=False)

    body_html: Mapped[str] = mapped_column(Text, nullable=False)

    body_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # ------------------------------------------------------------------
    # Queue state
    # ------------------------------------------------------------------

    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=MESSAGE_QUEUED,
        server_default=MESSAGE_QUEUED,
        index=True,
    )

    attempt: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )

    max_attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=DEFAULT_MAX_ATTEMPTS,
        server_default=str(DEFAULT_MAX_ATTEMPTS),
    )

    # Not claimable before this. Defaults to now, moved forward by the retry backoff.
    next_attempt_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True,
    )

    # Higher goes first. A test somebody is sitting and watching should not wait behind a
    # thousand-row overnight digest.
    priority: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )

    claimed_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    claimed_at: Mapped[Optional[DateTime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    heartbeat_at: Mapped[Optional[DateTime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True,
    )

    idempotency_key: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True, unique=True,
    )

    # ------------------------------------------------------------------
    # Outcome
    # ------------------------------------------------------------------

    # The sentence shown to whoever is reading the log. Written for an operator, not
    # copied from a driver exception — `retry.classify` turns the driver's words into
    # these.
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # What the server actually said, verbatim, truncated. Kept alongside
    # `error_message` rather than instead of it: the operator needs the sentence and
    # whoever they escalate to needs the raw code.
    smtp_response: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    sent_at: Mapped[Optional[DateTime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True,
    )

    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(),
    )

    # Named `attempt_log` and not `attempts`, because `attempt` (the counter, one letter
    # away) is right above it. Two attributes on one class differing by an `s`, one an int
    # and one a list, is a typo that type-checks.
    attempt_log = relationship(
        "EmailMessageAttempt",
        back_populates="message",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="EmailMessageAttempt.attempt",
    )

    __table_args__ = (
        # The claim query, in its ORDER BY order: highest priority first, then the
        # longest-due. Matches `claim_next_email` exactly.
        Index(
            "ix_email_messages_claim",
            "status",
            text("priority DESC"),
            "next_attempt_at",
        ),
        # The log page: one user's messages, newest first, optionally filtered by status.
        Index("ix_email_messages_user_created", "user_id", text("created_at DESC")),
    )


class EmailMessageAttempt(Base):
    """
    One try at sending one message.

    Kept as its own table rather than as a counter on the message, because "it eventually
    sent" and "it sent on the fifth try after four timeouts" are different operational
    facts and only one of them is visible from a counter. This is the table that answers
    "why was this an hour late".

    Written by the worker on every attempt including the successful one, and — like
    ``ToolGraphRunStep`` and ``integration_run_steps`` — writing it must never be what
    fails a send. ``message_store`` wraps these inserts the way ``run_store._quietly``
    does: the log is an observation of the work, not part of it.
    """

    __tablename__ = "email_message_attempts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True),
        default=uuid_pkg.uuid4,
        unique=True,
        index=True,
        nullable=False,
    )

    message_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("email_messages.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # 1-based, matching `EmailMessage.attempt` at the moment this row was written.
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)

    # MESSAGE_SENT or MESSAGE_FAILED. Deliberately drawn from the message vocabulary
    # rather than given its own: an attempt's outcome and a message's outcome are the
    # same kind of fact, and two lists would need keeping in step.
    status: Mapped[str] = mapped_column(String(16), nullable=False)

    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    smtp_response: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Whether `retry` judged this failure worth another go. Recorded rather than
    # re-derived later from the message text — the same rule `IntegrationFailure` states:
    # retryability is decided by the code that made the call.
    retryable: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false"),
    )

    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Host plus pid, for reading a log rather than for any decision.
    worker: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    started_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
    )

    message = relationship("EmailMessage", back_populates="attempt_log")
