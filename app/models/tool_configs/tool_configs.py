"""
Tool Configs — one query a data agent is allowed to run, managed from its own
module.

A tool config is defined here in full: which datasource and tables it reads, what
the query is, and the name the agent calls it by. It is deliberately independent
of the Configurations section (``DatasourceToolBaseConfig``) — nothing is shared
or referenced between the two, so editing one never changes the other.

**Two ways to write the query, one row.** ``query_mode`` says which:

``builder``
    ``config`` holds the structured query — columns, aggregations, grouping,
    filters, joins — and ``sql_query`` is NULL. Every identifier in it is checked
    against the tables the query reads, and the executor rebuilds it from
    reflected ``Column`` objects, so no part of it is ever string-interpolated
    into SQL.

``sql``
    ``sql_query`` holds one read-only statement written by the operator (or by Ask
    AI, when the query needs SQL the builder cannot express — a window function, a
    subquery, ``DISTINCT``, ``ORDER BY``), and ``config`` is ``{}``. It is held to
    :func:`app.utils.sql_guard.read_only_violation` on save *and* again on every
    run; whether it is otherwise valid is the database's answer to give.

The second mode exists because the first one is a deliberate subset of SQL, and a
query the operator has read and approved should not be unusable just because that
subset cannot hold it. The trade is explicit: builder mode is parameterised and
identifier-checked, SQL mode is exactly the text that was approved.

The ``config`` payload uses the same shape the Configurations builder produces, so
the two describe a query identically::

    {
      "columns":      [{"column": "sku",  "alias": ""}],
      "aggregations": [{"type": "count", "column": "id", "alias": "total"}],
      "group_by":     ["sku"],
      "filters":      [{"column": "qty", "operator": ">", "value": "0"}],
      "joins":        [{"type": "inner", "table": "orders",
                        "left_table": "products", "left_column": "sku",
                        "right_column": "product_sku"}]
    }

``joins`` is only ever populated for a relational datasource — see
app.utils.query_joins, which owns the join rules both forms share. Once a query has
a join, every column reference above is qualified as ``table.column`` instead of a
bare name, because with two tables in play a bare one is ambiguous. Configs saved
before a join was added keep their bare names and still mean the base table.

**Which tables a tool reads** is recorded on the row itself, in both modes:
``table_name`` is the primary one and ``extra_tables`` holds the rest. In builder
mode the extras are the tables a join may bring in; in SQL mode they are what the
statement reads, which nothing here can work out from the text.
"""

import uuid as uuid_pkg
from typing import Optional

from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.mutable import MutableDict, MutableList
from sqlalchemy.sql import func

from app.db.base import Base


# (value, display label) — stored lowercase, shown uppercase. Same set the
# Configurations builder offers, so a query means the same thing in both places.
AGGREGATION_FUNCTIONS = (
    ("count", "COUNT"),
    ("sum", "SUM"),
    ("avg", "AVG"),
    ("min", "MIN"),
    ("max", "MAX"),
)
AGGREGATION_FUNCTION_VALUES = frozenset(value for value, _ in AGGREGATION_FUNCTIONS)

# Comparison operators a filter may use — again matching the Configurations
# builder. Anything outside this set is rejected by tool_config_service rather
# than being carried into a query.
#
# The last four take **no value**: there is no right-hand side to `IS NULL`, and a
# form that demanded one for it would be asking for something that cannot be typed.
# See VALUELESS_FILTER_OPERATORS — every layer that touches a filter has to know
# which of these has a value, so the set is declared once here.
FILTER_OPERATORS = (
    "=", "!=", ">", "<", "LIKE",
    "IS NULL", "IS NOT NULL", "IS BLANK", "IS NOT BLANK",
)
FILTER_OPERATOR_VALUES = frozenset(FILTER_OPERATORS)

# Operators whose whole meaning is the column itself, with nothing compared against.
#
# **Blank is not the same as null, and both are why these exist.** A text column can
# be absent (NULL), empty ('') or whitespace ('   '), and to a person reading a report
# those are one thing: no value. Expressing that with the operators above took two
# filters that could not both be applied — the builder ANDs its conditions, so
# `technology != ''` silently keeps every NULL row, which is the exact bug these
# replace. `IS BLANK` is null-or-empty-or-whitespace; `IS NOT BLANK` is the useful
# one, and is what "not empty" means when asked for out loud.
VALUELESS_FILTER_OPERATORS = frozenset({
    "IS NULL", "IS NOT NULL", "IS BLANK", "IS NOT BLANK",
})

# (value, display label) — how the query is written. See this module's docstring
# for what each mode stores and what it guarantees.
QUERY_MODE_BUILDER = "builder"
QUERY_MODE_SQL = "sql"
QUERY_MODES = (
    (QUERY_MODE_BUILDER, "Query builder"),
    (QUERY_MODE_SQL, "SQL query"),
)
QUERY_MODE_VALUES = frozenset(value for value, _ in QUERY_MODES)

# (value, display label) — what a SQL-mode tool's declared parameter holds, so its
# value can be typed before it is bound. Builder mode needs no equivalent: it
# coerces against the reflected column instead, which is a better answer wherever it
# is available. It is not available here — nothing parses the statement — so the
# operator supplies it.
SQL_PARAM_TYPES = (
    ("text", "Text"),
    ("number", "Number"),
    ("boolean", "True / false"),
)

SQL_PARAM_TYPE_VALUES = frozenset(value for value, _ in SQL_PARAM_TYPES)


class ToolConfig(Base):
    """
    One tool an agent may use, and the query behind it.

    ``is_enabled`` revokes the tool without losing the definition — the quick way
    to switch a capability off while a datasource is investigated.

    Both foreign keys cascade. The agent is the tool's owner, and a tool config
    pointed at a deleted datasource would have nothing to read, so in either case
    the row goes with it.
    """
    __tablename__ = "tool_configs"

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

    datasource_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("datasources.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # The name the agent calls the tool by — a lowercase identifier, normalised by
    # tool_config_service (see app.utils.validators.require_identifier).
    tool_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    # Shown to the model as the tool's purpose — how it decides when to call it.
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Table, collection or file the query reads. A plain name, not a FK: the
    # object lives in the *user's own* database, not in ours.
    #
    # Required in both query modes. This is the **primary** table: in builder mode
    # it is the base table every join hangs off and every bare column reference
    # means, and in SQL mode it is the one the list page and the routing prompt lead
    # with. It stays a scalar column precisely because that role is singular.
    table_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    # The other tables the query reads, as a list of plain names — never including
    # ``table_name``. A query over more than one table is the ordinary case in both
    # modes, and before this column existed only the first one was recorded: a SQL
    # tool joining two tables reported one, so the routing prompt understated the
    # tool's scope and nothing could check the others were still switched on in Data
    # Sources.
    #
    # In builder mode these are the tables the Joins card may join to — the join
    # entries in ``config`` supply the ON conditions and must name tables from this
    # list. In SQL mode they are simply what the statement reads; nothing here
    # parses the SQL to verify that, which is why the form asks.
    #
    # NULL and [] both mean "one table", which is what every row written before this
    # column existed means.
    extra_tables: Mapped[Optional[list]] = mapped_column(
        MutableList.as_mutable(JSONB),
        nullable=True,
    )

    # "builder" or "sql" — see QUERY_MODES and this module's docstring. Not an
    # Enum type: adding a third mode would then need a migration on the type
    # itself, and the value is validated by tool_config_service on every write.
    query_mode: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=QUERY_MODE_BUILDER,
        server_default=QUERY_MODE_BUILDER,
    )

    # {"columns": [...], "aggregations": [...], "group_by": [...], "filters": [...]}
    # — see this module's docstring. Empty in SQL mode. MutableDict-wrapped like
    # the datasource configs so an in-place key change is picked up by the session
    # rather than silently lost.
    config: Mapped[dict] = mapped_column(
        MutableDict.as_mutable(JSONB),
        nullable=False,
        default=dict,
    )

    # The statement itself, in SQL mode only; NULL in builder mode. Nullable
    # rather than defaulted to "" so "this tool has no SQL of its own" is a state
    # the column states, not one inferred from an empty string.
    sql_query: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # The values the agent may supply for this statement, in SQL mode only; NULL in
    # builder mode. Each entry is
    # ``{"param": "department_id", "type": "number", "required": true,
    #    "description": "The department to report on."}``.
    #
    # Builder mode's equivalent lives inside ``config["filters"]`` as an
    # ``agent_supplied`` entry, because there the parameter *is* a filter — it has a
    # column and an operator the operator chose. A SQL statement has no filters to
    # open: nothing here parses it, so what a placeholder compares against is
    # whatever the operator wrote around it. Hence a separate list, and hence
    # ``type`` — with no reflected column to coerce against, the operator says what
    # the value holds so ``id = :x`` gets an int rather than the string a tool
    # argument always arrives as.
    #
    # The guarantee is unchanged either way: what the model supplies is a *value*,
    # bound as a parameter, on the right-hand side of a comparison the operator
    # wrote. It never chooses a name, a column, an operator or any SQL text.
    #
    # NULL and [] both mean "this tool takes no arguments", which is what every row
    # written before this column existed means.
    sql_params: Mapped[Optional[list]] = mapped_column(
        MutableList.as_mutable(JSONB),
        nullable=True,
    )

    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Whether an agent may read this tool's *whole* result set and group it in
    # memory — see app/services/agent_recursive_dataframes/ and
    # documentations/AGENT_RECURSIVE_DATAFRAMES.md.
    #
    # Off by default, and that default is what makes the feature additive: with it
    # off nothing is bound into the agent and the generated routing prompt is
    # byte-identical to what it was. It lives here rather than on data_agents
    # because it is a property of *this query* — turning it on says "reading every
    # record this returns is acceptable", which is a judgement about one tool's
    # result set, not about an agent's capabilities.
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

    data_agent = relationship("DataAgent", back_populates="tool_configs")

    __table_args__ = (
        # A tool name is how the model addresses the tool, so it has to be
        # unambiguous within one agent. Two different agents may each have a
        # "total_units". See the note on uq_workspace_user_name_lower about
        # Alembic and functional indexes.
        Index(
            "uq_tool_config_agent_name_lower",
            "data_agent_id",
            text("lower(tool_name)"),
            unique=True,
        ),
    )
