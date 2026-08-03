"""
app/schemas/tool_configs/tool_config_schemas.py

Pydantic schemas for Tool Configs — one saved query, owned by one data agent.

The important boundary here is ``config_json``. It is a hidden field carrying the
query the user built in the browser: columns, aggregations, grouping, filters and
joins, all referring to real tables and columns in the user's own database. Those
references are interpolated into generated SQL rather than bound as parameters,
which makes the field the highest-value input in the application to get wrong.

So the split is deliberate: this schema guarantees the field is a JSON *object* of
bounded size, and ``tool_config_service.validated_query_config`` — which knows
which tables the query is allowed to touch, because it has just reflected them —
validates every reference inside it. Restating the inner rules here would mean two
implementations of the same guard, and the one with the schema in hand is the one
that has to be right.

``sql_query`` is the same boundary drawn once more, for the other way a tool
config can be written. It is bounded and its mode is checked here; whether the
statement is a single read-only one is decided by
``tool_config_service.validated_tool_sql`` (which shares that rule with the
executor and with Ask AI, via ``app.utils.sql_guard``). Restating a SQL guard in
a schema would be the same duplication, with the copy that runs at request time
being the one nobody checks against the copy that runs at query time.

``agent_filter`` is on every mutation for the same reason the workspace filter is
on every Data Agents mutation: the list can be narrowed to one agent, and a
rebuilt table has to keep showing the same subset.
"""

from typing import Optional

from pydantic import Field, field_validator

from app.models.tool_configs import QUERY_MODE_BUILDER, QUERY_MODE_VALUES
from app.schemas.base import (
    MAX_DESCRIPTION_LENGTH,
    MAX_NAME_LENGTH,
    CheckboxBool,
    FormRequest,
    IdentifierName,
    JsonObjectField,
    ObjectName,
    OptionalText,
    OptionalUUID,
    QueryRequest,
    ResponseSchema,
)

#: A raw tool query travels in a textarea. Bounded well above
#: ``sql_guard.MAX_SQL_LENGTH`` (which is the real limit, applied by the service)
#: so an over-long statement is refused with that rule's wording rather than
#: with a generic length error from here.
MAX_TOOL_SQL_LENGTH = 20_000


class ToolConfigFilterMixin(FormRequest):
    """The hidden agent filter carried by every mutation on the tool configs page."""

    agent_filter: OptionalUUID = Field(default=None, title="Agent filter")


class ToolConfigCreateRequest(ToolConfigFilterMixin):
    """
    The create form.

    ``data_agent_id`` and ``datasource_id`` are optional at this layer and
    required by the service, which is where "required" can be checked *and*
    ownership verified in the same query — splitting it would report "Data agent is
    required" for an agent that exists but belongs to someone else.

    The form always submits both queries — the builder's ``config_json`` and the
    ``sql_query`` textarea — and ``query_mode`` says which one is meant. The other
    is discarded by the service rather than stored, so a tool holds exactly one
    query however the operator arrived at it.
    """

    data_agent_id: OptionalUUID = Field(default=None, title="Data agent")
    datasource_id: OptionalUUID = Field(default=None, title="Datasource")
    tool_name: IdentifierName = Field(title="Tool name")
    table_name: ObjectName = Field(title="Table name")
    description: OptionalText = Field(
        default=None, title="Description", max_length=MAX_DESCRIPTION_LENGTH
    )
    query_mode: str = Field(default=QUERY_MODE_BUILDER, title="Query mode")
    config_json: JsonObjectField = Field(default_factory=dict, title="Query")
    sql_query: str = Field(
        default="", title="SQL query", max_length=MAX_TOOL_SQL_LENGTH
    )

    @field_validator("query_mode")
    @classmethod
    def validate_query_mode(cls, v: str) -> str:
        """
        Blank means the builder — the mode a form that predates SQL mode, or one
        rendered before a datasource was picked, submits.
        """
        mode = (v or "").strip().lower() or QUERY_MODE_BUILDER

        if mode not in QUERY_MODE_VALUES:
            raise ValueError("Query mode is not one of the available options")

        return mode


class ToolConfigUpdateRequest(ToolConfigCreateRequest):
    """The edit form — the same fields as create."""


class ToolConfigSetEnabledRequest(ToolConfigFilterMixin):
    """The enable / disable toggle."""

    is_enabled: CheckboxBool = Field(default=False, title="Enabled")


class ToolConfigDeleteRequest(ToolConfigFilterMixin):
    """Delete carries only the filter, which still has to be validated."""


class ToolConfigListQuery(QueryRequest):
    """``?agent=<uuid>`` — what the tool count on the Data Agents page links to."""

    agent: OptionalUUID = Field(default=None, title="Data agent")


class SchemaCascadeQuery(QueryRequest):
    """
    The read-only schema endpoints behind the form's cascades: the tables in a
    datasource, the columns of a table, and the query builder rendered over them.

    ``table_name`` is optional because the first cascade step happens before a
    table is chosen. It is an ``ObjectName`` when present — it is read back out of
    the user's database, so it is held to the identifier-safe set even though it
    came from a dropdown the server rendered.
    """

    datasource_id: OptionalUUID = Field(default=None, title="Datasource")
    table_name: Optional[ObjectName] = Field(
        default=None, title="Table name", max_length=MAX_NAME_LENGTH
    )

    @property
    def table(self) -> str:
        """The table name as the services want it — a string, empty when unset."""
        return self.table_name or ""


class ToolConfigView(ResponseSchema):
    """
    One row of the tool configs table.

    ``agent_id`` and ``datasource_id`` are the related rows' public uuids, empty
    when unset so an unselected ``<option value="">`` matches for preselection.
    ``preview`` is the rendered SQL-ish summary of the saved query, for display
    only — in builder mode the Deep Agents executor builds the real query from
    reflected tables, and in SQL mode the preview *is* the stored statement.
    """

    uuid: str = Field(title="Tool config")
    tool_name: str = Field(title="Tool name")
    table_name: str = Field(title="Table name")
    description: Optional[str] = Field(default=None, title="Description")
    query_mode: str = Field(default=QUERY_MODE_BUILDER, title="Query mode")
    is_enabled: bool = Field(default=True, title="Enabled")
    agent_id: str = Field(default="", title="Data agent")
    agent_name: Optional[str] = Field(default=None, title="Data agent name")
    datasource_id: str = Field(default="", title="Datasource")
    datasource_name: Optional[str] = Field(default=None, title="Datasource name")
    config: dict = Field(default_factory=dict, title="Query")
    sql_query: str = Field(default="", title="SQL query")
    preview: str = Field(default="", title="Query preview")


class TableColumnsResponse(ResponseSchema):
    """
    The JSON body of ``GET /tool-configs/columns`` — one table's columns, for the
    join builder.

    A connection failure travels in ``error`` rather than being raised, so the
    builder can show the reason beside the join row instead of the whole offcanvas
    being replaced by an error page mid-edit.
    """

    table_name: str = Field(default="", title="Table name")
    columns: list = Field(default_factory=list, title="Columns")
    error: Optional[str] = Field(default=None, title="Error")

    @classmethod
    def failure(cls, table_name: str, message: str) -> "TableColumnsResponse":
        return cls(table_name=table_name, columns=[], error=message)
