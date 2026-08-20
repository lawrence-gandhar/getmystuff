"""
Nesting something inside a tool config — the edge, and nothing else.

A tool config answers one question with one query. A **link** says that something
else's result restricts it: the child runs first, one named column of its result
becomes a list of values, and the parent's query is filtered to those values. That
is a sub-query expressed as two reusable things rather than as one large statement,
which is the point — the child stays callable on its own, keeps its own
description, and can be embedded by more than one parent.

**A child is either another tool config or a Graph Designer graph.** The second is newer
and changes nothing about what the edge *means*: a graph is still a thing that runs first
and produces values. What it adds is that a graph may stop mid-run to ask a person a
question, which no tool config can do — so a chain has a third outcome besides rows and
"nothing matched", and ``tool_chain_graph`` carries it as the question itself.

The relationship is a row and not a JSON list on the parent, unlike
``ChatbotFlow.graph_data``, because three things need to *query* it rather than
read one parent's copy of it: the list page (which tools embed which), the agent
runtime (a parent's agent gains every transitive child), and the delete guard (a
tool that something embeds may not be removed). A JSONB list of child uuids would
make each of those a scan.

**A link is a filter, so losing one silently is the failure mode to design
against.** If a link disappeared while its parent kept running, the parent's
result would quietly widen — the same reason
``app.services.deep_agents.query_executor`` fails a tool loudly rather than
dropping a filter whose column was switched off. Both foreign keys cascade so a
deleted agent or datasource cannot strand rows, and ``tool_chain_service`` refuses
to delete or disable a tool that is embedded, which is where that guarantee
actually lives.

The direction of the edge is parent → child, and the tree is walked depth-first at
run time by :mod:`app.services.tool_configs.tool_chain_graph`, which turns it into
LangGraph nodes. Cycles are refused when a link is saved (see
``tool_chain_service.replace_child_links``); nothing at run time re-checks, because
a cycle that reached the graph would be a bug in that validator, not a state to
recover from.
"""

import uuid as uuid_pkg
from typing import Optional

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


# How a child's values reach its parent. Two shapes, and the difference is not a
# detail of binding — it decides how many times the parent runs.
#
#   in_list → the whole list at once, as an expanding IN. The parent runs once.
#             What every link did before this column existed, and still the
#             default: one round trip, one result set, no multiplication.
#
#   each    → one value at a time, bound as a plain scalar. The parent runs once
#             per value and the rows are concatenated. Needed whenever the value
#             is not on the right-hand side of an IN — `dd.id = :x`, or inside a
#             string the database builds, as in
#             `LIKE CONCAT('%s:1:"', :x, '"%')` — because an expanding parameter
#             always renders parenthesised and is a syntax error in both.
#
# Stored as a String rather than a native enum for the same reason `query_mode`
# is: adding a third shape should be a constant and a validator, not a migration
# that rewrites a type every table using it has to be locked for.
BINDING_MODE_IN_LIST = "in_list"
BINDING_MODE_EACH = "each"

BINDING_MODES = (
    (BINDING_MODE_IN_LIST, "Match any of the values"),
    (BINDING_MODE_EACH, "Run once per value"),
)

BINDING_MODE_VALUES = frozenset(value for value, _ in BINDING_MODES)


class ToolConfigLink(Base):
    """
    One thing embedded inside a tool: *the values ``child.child_column`` returns
    restrict ``parent`` at ``parent_reference``.*

    **The child is either another tool config or a Graph Designer graph**, and exactly
    one of ``child_id`` / ``child_graph_id`` is set — enforced by
    ``ck_tool_config_links_one_child``. Two nullable columns rather than one column plus
    a discriminator, because a foreign key is the thing that keeps a child from being
    deleted out from under its parent, and a discriminator would give up both foreign
    keys to save one column.

    The edge means the same thing either way, which is the point: something runs first,
    one named part of its result becomes a list of values, and the parent's query is
    filtered to them. What differs is only what "runs first" involves — one query, or a
    whole drawn graph that may stop to ask a question.
    """

    __tablename__ = "tool_config_links"

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

    # The tool that embeds. Its rows are what the agent finally sees.
    parent_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("tool_configs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # The tool that runs first and supplies values. NULL when the child is a graph.
    child_id: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        ForeignKey("tool_configs.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    # The graph that runs first and supplies values. NULL when the child is a tool
    # config. Exactly one of the two is set — see the class docstring and
    # `ck_tool_config_links_one_child`.
    #
    # CASCADE, matching `child_id`: a deleted graph takes its links with it rather than
    # leaving a parent whose filter silently disappeared, which is the failure mode this
    # whole module is designed against. `tool_chain_service` refuses the delete first,
    # where the message can name the parent — the cascade is the backstop for a row
    # removed some other way.
    child_graph_id: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        ForeignKey("tool_graphs.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    # Which part of the child's result is collected. One name, not the row: what
    # crosses the boundary is a list of values for an IN comparison, so the child's
    # other columns are never read by the parent and never leave the database.
    #
    # For a tool-config child it is a column of its result. For a graph child it is a
    # key of the graph's last output — the same name, because a graph node's rows are
    # dictionaries exactly as a query's are, and the one case where a graph returns a
    # bare list rather than rows is handled by reading the list itself.
    child_column: Mapped[str] = mapped_column(String(255), nullable=False)

    # Where those values land in the parent, and what it means depends on the
    # parent's query_mode — the same split the parent's own query already has:
    #
    #   builder → a column reference ("client_id" or "projects.client_id"),
    #             validated against the tables the parent's query reads;
    #   sql     → the name of a bind parameter, written as `:name` in the
    #             statement. The statement is never rewritten; the list is bound
    #             at execution as an expanding parameter.
    #
    # One column rather than two nullable ones: a link always has exactly one
    # target, and which kind it is is already recorded on the parent.
    parent_reference: Mapped[str] = mapped_column(String(255), nullable=False)

    # Whether those values are matched all at once or iterated. See BINDING_MODES.
    # A parent may have at most one `each` child, refused when the link is saved:
    # two would be a cartesian product of two result sets, and the row cap makes
    # that a truncated answer rather than a bigger one.
    binding_mode: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=BINDING_MODE_IN_LIST,
        server_default=BINDING_MODE_IN_LIST,
    )

    # For an iterating link only: the name under which each row records the value
    # that produced it. Rows from twenty runs of one statement are otherwise
    # indistinguishable once concatenated, and a statement that filters on a
    # department without selecting it is perfectly ordinary SQL — so this is what
    # makes a fan-out result groupable by the thing it was fanned out over.
    #
    # Optional, because a query that already returns the value needs no second
    # copy of it. Asking for one anyway is refused at run time as a column
    # collision rather than overwriting what the database returned.
    value_alias: Mapped[str] = mapped_column(String(255), nullable=True)

    # Evaluation and display order among a parent's children. Meaningful: children
    # run in this order and the first one to return nothing stops the chain, so an
    # operator can put the cheapest or most selective child first.
    position: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
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

    __table_args__ = (
        # The same child may feed a parent twice — a tool returning client ids
        # could restrict both `owner_id` and `billed_to_id` — so the target is
        # part of the key. What is refused is the same child bound to the same
        # target twice, which would AND a list against itself.
        UniqueConstraint(
            "parent_id",
            "child_id",
            "parent_reference",
            name="uq_tool_config_links_parent_child_target",
        ),
        # The same rule for a graph child. A separate constraint rather than one
        # three-column one, because with `child_id` NULL Postgres treats every row as
        # distinct and graph links would never collide in the constraint above.
        UniqueConstraint(
            "parent_id",
            "child_graph_id",
            "parent_reference",
            name="uq_tool_config_links_parent_graph_target",
        ),
        # Exactly one child. A CHECK rather than a service rule — unlike the
        # attachment exclusivity on `tool_graphs`, which has an operator on the far
        # side who needs a sentence. No form can produce a link with two children or
        # none; that would be a bug in this application, and a constraint is how a bug
        # is stopped from becoming data.
        CheckConstraint(
            "(child_id IS NOT NULL) <> (child_graph_id IS NOT NULL)",
            name="ck_tool_config_links_one_child",
        ),
        # Reading a parent's children in the order they run is the hot path: it
        # happens on every nested tool call and on every list render.
        Index("ix_tool_config_links_parent_position", "parent_id", "position"),
    )
