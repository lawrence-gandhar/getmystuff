"""
Tool Configs — one query a data agent is allowed to run, managed from its own
module.

A tool config is defined here in full: which datasource and table it reads, which
columns, aggregations, group-bys and filters make up the query, and the name the
agent calls it by. It is deliberately independent of the Configurations section
(``DatasourceToolBaseConfig``) — nothing is shared or referenced between the two,
so editing one never changes the other.

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
    table_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    # {"columns": [...], "aggregations": [...], "group_by": [...], "filters": [...]}
    # — see this module's docstring. MutableDict-wrapped like the datasource
    # configs so an in-place key change is picked up by the session rather than
    # silently lost.
    config: Mapped[dict] = mapped_column(
        MutableDict.as_mutable(JSONB),
        nullable=False,
        default=dict,
    )

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
