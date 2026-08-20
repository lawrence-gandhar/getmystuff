"""
Graph Designer — an authored LangGraph, its runs, and the steps each run took.

``/tool-graphs`` draws a graph it did not author: ``tool_graph_service`` derives nodes
and edges from ``tool_config_links`` every time the page is opened, which is why that
module owns no table. This one is the opposite. A user *draws* the graph, so the
drawing is the source of truth and has to be stored — including the node positions,
which nothing can recompute because nothing else knows where the user put them.

Three tables, and the split is by lifetime rather than by subject:

``ToolGraph``
    The design. Long-lived, edited in place, and the only one of the three a user
    thinks of as a thing they own.

``ToolGraphRun``
    One execution. Short-lived and immutable once finished, except for the two fields
    a pause needs: ``status`` and ``interrupt_payload``. It also carries ``thread_id``
    — the LangGraph checkpointer thread the run is parked on — for exactly the reason
    ``DownloadExport.thread_id`` exists: an ``interrupt()`` fires inside one task and
    is resumed by a different request, and that string is the only handle connecting
    the two.

``ToolGraphRunStep``
    One node, one pass. The log the canvas dock reads.

**Why the steps are rows and not an in-memory list.** The task running the graph and
the request streaming the dock are different tasks, and under more than one replica
they are different processes — the same argument
``app/services/downloader_agents/base/progress.py`` makes for reading export progress
from ``download_export_parts``. An event bus would work only in the configuration this
application is not guaranteed to run in, and a browser that reconnects halfway through
a long run would see the second half of the story.

**Why the previews are capped in the column, not at render time.** ``output_preview``
and ``state_preview`` exist so the dock can show what a node produced. A run over a
two-hundred-row query would otherwise put that result set in a log row, once per
iteration of a loop — a table that grows faster than the data it describes. The caps
are applied by ``graph_run_service`` before the row is written, so what is stored is
already the preview; nothing downstream has to remember to trim.

**Statuses are plain strings, not Enum types.** Same reasoning as
``ToolConfig.query_mode`` and ``DownloadExport.status``: adding a state should be a
constant and a validator, not a migration that rewrites a type every table using it
has to be locked for. Every write goes through ``app/services/graph_designer/``, which
validates against the frozensets below.

Only ``uuid`` ever leaves this module. The bigint ``id`` is the primary key and the
target of the foreign keys between these three tables, and nothing else — every URL,
form field and JSON payload names a graph, a run or a step by its uuid.
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
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


# ----------------------------------------------------------------------------
# Node types
# ----------------------------------------------------------------------------
# What a node may be. The value is what the canvas writes into `graph_data` and what
# `node_runners` dispatches on, so the two cannot drift: there is one list.
#
# The four the user picks between for "what does this node hold" are a SQL statement, a
# union of one statement per pass of a loop, a literal value, and an existing tool config;
# the rest are control flow. START is not in this list because LangGraph supplies it —
# NODE_START is the authored node the run begins at, which is a different thing and is why
# it exists at all (a graph has to say where it starts, and a drawing cannot leave that to
# reading order).
#
# `sql_union` sits beside `sql` rather than being a mode of it because the two differ in
# *when* they run, not in what they hold: a `sql` node runs on the visit, and a `sql_union`
# node appends on every visit but the last. A switch on one node type would mean every
# reader of a SQL node had to ask which kind it was looking at.
NODE_START = "start"
NODE_SQL = "sql"
NODE_SQL_UNION = "sql_union"
NODE_VALUE = "value"
NODE_TOOL_CONFIG = "tool_config"
NODE_HUMAN = "human"
NODE_BRANCH = "branch"
NODE_FOR_EACH = "for_each"
NODE_DO_UNTIL = "do_until"
NODE_SUCCESS = "success"
NODE_FAILURE = "failure"

NODE_TYPES = (
    (NODE_START, "Start"),
    (NODE_SQL, "SQL statement"),
    (NODE_SQL_UNION, "Union"),
    (NODE_VALUE, "Value"),
    (NODE_TOOL_CONFIG, "Tool config"),
    (NODE_HUMAN, "Ask a human"),
    (NODE_BRANCH, "Branch"),
    (NODE_FOR_EACH, "For each"),
    (NODE_DO_UNTIL, "Do until"),
    (NODE_SUCCESS, "Success"),
    (NODE_FAILURE, "Failure"),
)

NODE_TYPE_VALUES = frozenset(value for value, _ in NODE_TYPES)
NODE_TYPE_LABELS = dict(NODE_TYPES)

# The two terminal node types. Both compile to LangGraph's END; the difference is what
# the run is recorded as having done, which is the whole point of the user being able
# to draw a failure path rather than only an error state.
TERMINAL_NODE_TYPES = frozenset({NODE_SUCCESS, NODE_FAILURE})

# The two loop node types. Kept as its own set because three separate rules key off
# "is this a loop" — the back edge a cycle is allowed to travel, the iteration
# ceiling, and the computed recursion limit.
LOOP_NODE_TYPES = frozenset({NODE_FOR_EACH, NODE_DO_UNTIL})


# ----------------------------------------------------------------------------
# What a `value` node holds
# ----------------------------------------------------------------------------
# All three are parsed from JSON; they exist separately because they validate
# differently, and validating them alike would let a shape through that the node
# downstream cannot use.
#
#   list → a flat array of scalars. The shape an IN comparison takes, so this is the
#          kind that can feed a SQL node's parameter directly.
#   array → an array, nesting allowed. Rows, tuples, a matrix.
#   dict → an object. Named values, which is what a SQL node's declared parameters
#          are filled from.
VALUE_KIND_LIST = "list"
VALUE_KIND_ARRAY = "array"
VALUE_KIND_DICT = "dict"

VALUE_KINDS = (
    (VALUE_KIND_LIST, "List of values"),
    (VALUE_KIND_ARRAY, "Array"),
    (VALUE_KIND_DICT, "Dictionary"),
)

VALUE_KIND_VALUES = frozenset(value for value, _ in VALUE_KINDS)


# ----------------------------------------------------------------------------
# What a `human` node asks for
# ----------------------------------------------------------------------------
# The control the dock renders, and how the resumed answer is validated. `confirm` is
# separate from `choice` with two options because a yes/no is the one a graph branches
# on without the author having to name the ports.
HUMAN_EXPECTS_TEXT = "text"
HUMAN_EXPECTS_CHOICE = "choice"
HUMAN_EXPECTS_CONFIRM = "confirm"

HUMAN_EXPECTS = (
    (HUMAN_EXPECTS_TEXT, "Free text"),
    (HUMAN_EXPECTS_CHOICE, "One of a list"),
    (HUMAN_EXPECTS_CONFIRM, "Yes or no"),
)

HUMAN_EXPECTS_VALUES = frozenset(value for value, _ in HUMAN_EXPECTS)


# ----------------------------------------------------------------------------
# How a wired value reaches a SQL node's parameter
# ----------------------------------------------------------------------------
# The distinction is not a detail of binding — it decides how many values the
# statement is given and therefore what shape it must be written in. An expanding
# bind parameter always renders parenthesised, so the two are not interchangeable:
#
#   one     → a single value, bound as a scalar. `dd.id = :dept_id`. What every
#             binding did before this constant existed, and still the default.
#   in_list → the whole list at once, as an expanding IN. `dd.id IN :dept_ids`.
#             One round trip instead of one per value.
#
# `in_list` is the same word `tool_config_links.binding_mode` uses for the same
# thing — see documentations/TOOL_CHAIN_ITERATION.md, which is where the rule is
# written out in full. Its companion there is called `each` and means "run the
# statement once per value"; that name is deliberately **not** reused here, because
# in a graph the `for_each` node is what iterates and a binding never does.
BINDING_MODE_ONE = "one"
BINDING_MODE_IN_LIST = "in_list"

BINDING_MODES = (
    (BINDING_MODE_ONE, "One value"),
    (BINDING_MODE_IN_LIST, "All the values, as a list"),
)

BINDING_MODE_VALUES = frozenset(value for value, _ in BINDING_MODES)


# ----------------------------------------------------------------------------
# Run statuses
# ----------------------------------------------------------------------------
# `awaiting_input` is not a kind of `running`, and separating them is what makes the
# dock able to say "this run is waiting for you" rather than showing a spinner over a
# graph that will never move on its own.
RUN_RUNNING = "running"
RUN_AWAITING_INPUT = "awaiting_input"
RUN_SUCCEEDED = "succeeded"
RUN_FAILED = "failed"
RUN_CANCELLED = "cancelled"

RUN_STATUSES = frozenset(
    {
        RUN_RUNNING,
        RUN_AWAITING_INPUT,
        RUN_SUCCEEDED,
        RUN_FAILED,
        RUN_CANCELLED,
    }
)

# The statuses that end a run. The progress stream stops when it sees one, so a
# consumer knows to stop rather than inferring it from silence.
TERMINAL_RUN_STATUSES = frozenset({RUN_SUCCEEDED, RUN_FAILED, RUN_CANCELLED})


# ----------------------------------------------------------------------------
# Run scopes
# ----------------------------------------------------------------------------
# Whether this run is the whole graph or a selection of it. Both go through the same
# execution path — testing a node has to run the node that will run, not a lookalike —
# so the scope is recorded rather than being a different code path.
SCOPE_FULL = "full"
SCOPE_SELECTION = "selection"

RUN_SCOPES = frozenset({SCOPE_FULL, SCOPE_SELECTION})


# ----------------------------------------------------------------------------
# Step statuses
# ----------------------------------------------------------------------------
# `skipped` is a real outcome, not an absence: a node outside a tested selection gets
# a row saying so, because a node missing from the log is indistinguishable from a
# node the run never reached.
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


class ToolGraph(Base):
    """
    One authored graph: the drawing, and the two switches that decide who runs it.

    Ownership and association are separate concerns, the same split
    ``ChatbotFlow`` makes:

    * ``user_id`` is who owns the graph — the only thing needed to read, edit or run
      it from the designer, so a graph can exist attached to nothing at all.
    * ``data_agent_id`` is which data agent may *call* it as a tool: nullable and
      **unique**. Postgres permits many NULLs in a unique column, so that single
      constraint expresses both halves of the relationship — one graph per agent, one
      agent per graph.
    * ``workspace_id`` is the other way to be callable: every agent in that workspace
      may call it. Nullable and **not** unique, because "shared with a team" is a
      one-to-many by nature — a workspace holding three shared graphs gives each of
      its agents three more tools.
    * ``is_active`` is an independent published/draft toggle. An agent only calls a
      graph while that graph is active, so a graph can be parked mid-edit without
      being detached from whatever it belongs to.

    **The two attachments are mutually exclusive**, enforced by ``graph_service``
    rather than by a constraint, because the refusal has a sentence to say. Holding
    both would hand the same graph to an agent twice — once as its own and once
    through its workspace — and a model offered two identically named tools cannot
    choose between them. One graph, one answer to "who may call this".

    Deleting an agent or a workspace detaches the graph (``ON DELETE SET NULL``)
    rather than destroying work the user may want to point somewhere else.
    """

    __tablename__ = "tool_graphs"

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

    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Which data agent may call this graph as a tool. Unique, so a graph cannot be
    # claimed by two agents and an agent cannot hold two graphs — see the class
    # docstring. NULL is the ordinary state for a graph that is only run from the
    # designer.
    data_agent_id: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        ForeignKey("data_agents.id", ondelete="SET NULL"),
        nullable=True,
        unique=True,
        index=True,
    )

    # Which workspace this graph is shared with: every data agent assigned to that
    # workspace may call it. Deliberately **not** unique, unlike `data_agent_id` — a
    # workspace is a team's shelf and may hold several shared graphs. The index is
    # what `fetch_agent_graphs` reads it by, once per prompt rebuild.
    workspace_id: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        ForeignKey("workspaces.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    # What the graph does, in the operator's words. Doubles as the tool description
    # when the graph is attached to an agent, which is why it is worth asking for:
    # a model choosing between tools reads this and nothing else.
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Whole-graph JSON: {"nodes": [{id, type, position:{x,y}, data}],
    #                    "edges": [{id, source, source_port, target}]}
    #
    # Replaced wholesale on every save — plain JSONB rather than MutableDict, the
    # same call `ChatbotFlow.graph_data` makes for the same write pattern. The node
    # positions live in here because they are the user's own authored layout and
    # nothing can recompute them; that is the difference between this table and
    # `tool_graphs`' derived drawing, which deliberately stores no position.
    graph_data: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )

    # Whether an agent may read this graph's *whole* result and filter or total it in
    # polars — see documentations/AGENT_RECURSIVE_DATAFRAMES.md.
    #
    # Off by default and set per graph, exactly as `tool_configs.allow_recursive_aggregate`
    # is and for the same reason: switching it on says "reading every record this
    # produces is acceptable", which is a judgement about one result set rather than
    # about an agent's capabilities. It matters slightly more here than there, because a
    # graph can be a loop over eighty-two departments rather than a single statement, so
    # "run the whole thing and hold the result" is a larger promise to make on somebody's
    # behalf.
    allow_recursive_aggregate: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
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

    runs: Mapped[list["ToolGraphRun"]] = relationship(
        "ToolGraphRun",
        back_populates="graph",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        # Two graphs of the same name under one user is a naming collision the
        # designer's own list could not resolve, and once a graph is a tool it is a
        # duplicate tool name to a model. Case-insensitive, and written as a
        # `text()` expression for the same reason `uq_tool_config_agent_name_lower`
        # is — see the note there about Alembic and functional indexes.
        Index(
            "uq_tool_graphs_user_name_lower",
            "user_id",
            text("lower(name)"),
            unique=True,
        ),
    )


class ToolGraphRun(Base):
    """
    One execution of one graph — the whole of it, or a tested selection.

    A run is written before the graph is compiled, so a compilation that fails is
    still a run somebody can look at rather than a button that did nothing.

    ``thread_id`` is the LangGraph checkpointer thread this run is parked on. It is
    stored rather than held in memory because a ``human`` node's ``interrupt()``
    fires inside the background task and the answer arrives in a later HTTP request,
    which is the same seam ``DownloadExport.thread_id`` spans.
    """

    __tablename__ = "tool_graph_runs"

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

    tool_graph_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("tool_graphs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # See RUN_STATUSES.
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=RUN_RUNNING,
        server_default=RUN_RUNNING,
        index=True,
    )

    # "full" | "selection" — see RUN_SCOPES.
    scope: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=SCOPE_FULL,
        server_default=SCOPE_FULL,
    )

    # The node ids this run was asked to cover, when `scope` is "selection". A list of
    # the client-minted string ids out of `graph_data["nodes"]`, not foreign keys —
    # nodes are JSONB entries, not rows, the same pointer
    # `ChatbotFlowSession.current_node_id` is.
    selected_nodes: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)

    # What the run was started with: the values a SQL node's declared parameters need,
    # supplied either by the test panel or by the model calling the graph as a tool.
    inputs: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    # The checkpointer thread. Indexed because resuming looks a run up by it.
    thread_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    # The question currently awaiting an answer, as the `human` node's interrupt
    # produced it: {node_id, prompt, expects, choices}. Non-NULL only while `status`
    # is `awaiting_input` — cleared on resume, so a stale prompt cannot be answered
    # twice.
    interrupt_payload: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    # What the run finally produced, already capped the way a step preview is. Read by
    # the dock's summary line and by the tool wrapper when an agent called the graph.
    result_preview: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    # The sentence to show for a failed run. Stored rather than composed at read time,
    # so the dock, the log and an agent relaying it all use the same words.
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    started_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        index=True,
    )

    finished_at: Mapped[Optional[DateTime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    graph: Mapped["ToolGraph"] = relationship("ToolGraph", back_populates="runs")

    steps: Mapped[list["ToolGraphRunStep"]] = relationship(
        "ToolGraphRunStep",
        back_populates="run",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="ToolGraphRunStep.sequence",
    )


class ToolGraphRunStep(Base):
    """
    One node, one pass through it — the row the canvas dock is drawn from.

    A step is written as ``running`` when the node starts and updated when it ends, so
    a node that hangs is visible as a step that never finished rather than as a gap.
    That is the whole reason the row is written twice instead of once at the end.

    ``iteration`` is which pass of an enclosing loop this was. Zero for a node that
    runs once, which is most of them; a ``for_each`` body's third pass is
    ``iteration = 2``. Without it a loop's rows are indistinguishable from one
    another and the dock cannot group them.
    """

    __tablename__ = "tool_graph_run_steps"

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

    run_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("tool_graph_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Position in the run. Assigned by the runner, not by insertion order, because
    # the dock reads the log in the order the nodes ran and two rows can be written
    # in the same millisecond.
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # The node this step is about — the string id from `graph_data["nodes"]`, not a
    # foreign key. Editing the graph while an old run is on screen is therefore
    # survivable: the id may no longer resolve, and the dock says so rather than
    # failing to load.
    node_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    # Denormalised on purpose. A step has to stay readable after the node it names has
    # been renamed or deleted, and a log that changes retroactively when the graph is
    # edited is a log nobody can trust.
    node_type: Mapped[str] = mapped_column(String(32), nullable=False)
    node_label: Mapped[str] = mapped_column(String(255), nullable=False, default="")

    # Which pass of the enclosing loop. See the class docstring.
    iteration: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )

    # See STEP_STATUSES.
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=STEP_RUNNING,
        server_default=STEP_RUNNING,
    )

    # How long the node took. NULL while it is still running, which is how the dock
    # tells a slow node from a finished one without comparing timestamps.
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # One human-readable line: what the node did, or why it could not. A
    # ToolQueryError's message lands here verbatim — the operator is the audience it
    # was written for.
    message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # What the node produced, and the graph state after it — both **already capped**
    # by graph_run_service before the row is written. See the module docstring: a log
    # row must never carry a result set, and capping at write time is what makes that
    # true of the table rather than only of one renderer.
    output_preview: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    state_preview: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    started_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    finished_at: Mapped[Optional[DateTime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    run: Mapped["ToolGraphRun"] = relationship("ToolGraphRun", back_populates="steps")

    __table_args__ = (
        # Reading one run's steps in order is the hot path: it happens once per poll
        # of the progress stream, for as long as somebody is watching a run.
        Index("ix_tool_graph_run_steps_run_sequence", "run_id", "sequence"),
    )
