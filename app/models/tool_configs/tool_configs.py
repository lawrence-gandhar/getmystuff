"""
Tool Configs — one query a data agent is allowed to run, managed from its own
module.

A tool config is defined here in full: which datasource and table it reads, what
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
from sqlalchemy.ext.mutable import MutableDict
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
FILTER_OPERATORS = ("=", "!=", ">", "<", "LIKE")
FILTER_OPERATOR_VALUES = frozenset(FILTER_OPERATORS)

# (value, display label) — how the query is written. See this module's docstring
# for what each mode stores and what it guarantees.
QUERY_MODE_BUILDER = "builder"
QUERY_MODE_SQL = "sql"
QUERY_MODES = (
    (QUERY_MODE_BUILDER, "Query builder"),
    (QUERY_MODE_SQL, "SQL query"),
)
QUERY_MODE_VALUES = frozenset(value for value, _ in QUERY_MODES)


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
    # Required in both query modes. In SQL mode the statement may read several
    # tables, and this is the primary one — what the list page, the routing prompt
    # and the edit form name as the tool's source.
    table_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

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

    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

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
