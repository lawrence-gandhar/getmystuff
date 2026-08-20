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

from typing import List, Optional

from pydantic import Field, field_validator

from app.db.db_utils import MAX_REFLECTED_TABLES
from app.models.tool_configs import QUERY_MODE_BUILDER, QUERY_MODE_VALUES
from app.schemas.base import (
    MAX_DESCRIPTION_LENGTH,
    MAX_NAME_LENGTH,
    CheckboxBool,
    FormRequest,
    IdentifierName,
    JsonArrayField,
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

#: How many tables one tool may read. Matched to ``db_utils.MAX_REFLECTED_TABLES``
#: because that is what bounds the schema read the form's pickers do — a limit the
#: user would otherwise meet as a truncated dropdown rather than as a message.
MAX_TOOL_TABLES = MAX_REFLECTED_TABLES

#: How many tools one tool may embed. Mirrors
#: ``tool_chain_service.MAX_CHILDREN_PER_TOOL`` — restated rather than imported so
#: the schema layer does not depend on a service, which is the direction every other
#: schema in this package takes with its caps.
MAX_NESTED_TOOLS = 5

#: How many values one SQL-mode tool may ask the assistant for. Every one of them
#: becomes a field the model has to fill correctly on every call, so the ceiling is
#: about how much a tool call can be got wrong rather than about storage. Mirrors
#: ``tool_config_service._MAX_SQL_PARAMS``, restated for the same reason as above.
MAX_SQL_PARAMS = 5


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

    ``table_names`` is a multi-select, so it is declared in ``multi_fields``: read
    with a plain ``get`` it would yield the first table only, which is how a tool
    over four tables silently becomes a tool over one. Which of them is the primary
    table, and whether the builder's joins agree with the list, is
    ``tool_config_service``'s decision — it is the layer that knows what a base table
    means. This schema guarantees only that each name is identifier-safe and that
    there are not more of them than a query has any business reading.
    """

    multi_fields = ("table_names",)

    data_agent_id: OptionalUUID = Field(default=None, title="Data agent")
    datasource_id: OptionalUUID = Field(default=None, title="Datasource")
    tool_name: IdentifierName = Field(title="Tool name")
    table_names: List[ObjectName] = Field(
        default_factory=list, title="Tables", max_length=MAX_TOOL_TABLES
    )
    description: OptionalText = Field(
        default=None, title="Description", max_length=MAX_DESCRIPTION_LENGTH
    )
    query_mode: str = Field(default=QUERY_MODE_BUILDER, title="Query mode")
    # An unticked checkbox is absent from a form body, so the default is what an
    # edit that turned the capability off actually posts — which is why it has to
    # be False rather than "whatever was stored".
    allow_recursive_aggregate: CheckboxBool = Field(
        default=False, title="Allow whole-result grouping",
    )
    config_json: JsonObjectField = Field(default_factory=dict, title="Query")
    sql_query: str = Field(
        default="", title="SQL query", max_length=MAX_TOOL_SQL_LENGTH
    )
    # The things this tool embeds — `[{"child_id" | "child_graph_id", "child_column",
    # "parent_reference", "binding_mode", "value_alias"}]` — from the Nested Tools
    # card's hidden field. A JSON array rather than repeated form keys for the same
    # reason `config_json` is one object: five parallel multi-selects could arrive at
    # different lengths, and then a row would silently pair the wrong column with the
    # wrong tool.
    #
    # Which of the two child keys a row carries is decided in
    # `tool_chain_service._validated_link` rather than here. The shape is genuinely a
    # union — one row names a tool config, another names a graph — and expressing it at
    # this layer would mean a discriminated model whose only job is to hand both branches
    # to the same validator anyway.
    children_json: JsonArrayField = Field(
        default_factory=list, title="Nested tools",
    )
    # The values the assistant may supply for a SQL-mode statement —
    # `[{"param", "type", "required", "description"}]`. Same reasoning as
    # `children_json`, and the same division of labour: bounded here, meaningful in
    # `tool_config_service.validated_sql_params`, which is the layer that knows
    # whether the statement actually uses the name.
    sql_params_json: JsonArrayField = Field(
        default_factory=list, title="Assistant-supplied values",
    )

    @field_validator("table_names", mode="before")
    @classmethod
    def drop_blank_table_names(cls, v: object) -> object:
        """
        Drop empty entries before each one is held to the object-name rule.

        A multi-select can post a blank value, and "" failing the name check would
        report *"Table names is required"* — a sentence about the wrong thing. Dropped
        here, the empty selection is reported by the validator below in the words the
        user needs.
        """
        if isinstance(v, (list, tuple)):
            return [name for name in v if str(name or "").strip()]
        return v

    @field_validator("table_names")
    @classmethod
    def validate_table_names(cls, v: List[str]) -> List[str]:
        """
        At least one table — a tool with nothing to read is not a tool.

        Written as a validator rather than as ``min_length=1`` so the sentence is a
        sentence: the generic list-too-short message would read "Tables needs at
        least 1 entries".
        """
        if not v:
            raise ValueError("Select at least one table for this tool to read")

        return v

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

    @field_validator("children_json")
    @classmethod
    def validate_children(cls, v: list) -> list:
        """
        Shape only: a list of objects, of a bounded length, each naming a tool, a
        column and a target.

        Whether the tool may actually be embedded — the same owner, the same
        datasource, enabled, not a cycle, not too deep — is
        ``tool_chain_service.validated_children``'s decision, because every one of
        those questions needs the database. Restating any of them here would be the
        same duplication ``config_json`` is deliberately spared, with the copy that
        cannot see the other tools being the one that runs first.
        """
        if len(v) > MAX_NESTED_TOOLS:
            raise ValueError(
                f"A tool can embed at most {MAX_NESTED_TOOLS} other tools"
            )

        for entry in v:
            if not isinstance(entry, dict):
                raise ValueError("Nested tools are not in the expected format")

        return v

    @field_validator("sql_params_json")
    @classmethod
    def validate_sql_params(cls, v: list) -> list:
        """
        Shape only: a bounded list of objects.

        Whether a declared name is actually used by the statement, and whether the
        type is one this application can coerce to, is
        ``tool_config_service.validated_sql_params``' decision — it has the statement
        in hand, and this schema does not.
        """
        if len(v) > MAX_SQL_PARAMS:
            raise ValueError(
                f"A tool can ask the assistant for at most {MAX_SQL_PARAMS} values"
            )

        for entry in v:
            if not isinstance(entry, dict):
                raise ValueError(
                    "Assistant-supplied values are not in the expected format"
                )

        return v


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

    ``table_names`` is empty on the first cascade step, which happens before any
    table is chosen. Each name is an ``ObjectName`` — they are read back out of the
    user's database, so they are held to the identifier-safe set even though they
    came from a dropdown the server rendered.

    ``table_name`` is still accepted as a single value alongside it: the join
    builder's ``/tool-configs/columns`` fetch asks about one specific table, and that
    is a different question from "which tables is this tool over".

    ``exclude`` is the tool being edited, sent by ``/tool-configs/child-options`` so
    the Nested Tools picker never offers a tool the tool itself. It is optional
    because the new-tool form has nothing to exclude yet.
    """

    multi_fields = ("table_names",)

    datasource_id: OptionalUUID = Field(default=None, title="Datasource")
    exclude: OptionalUUID = Field(default=None, title="Tool being edited")
    table_name: Optional[ObjectName] = Field(
        default=None, title="Table name", max_length=MAX_NAME_LENGTH
    )
    table_names: List[ObjectName] = Field(
        default_factory=list, title="Tables", max_length=MAX_TOOL_TABLES
    )

    @property
    def table(self) -> str:
        """The single table name as the services want it — "" when unset."""
        return self.table_name or ""

    @property
    def tables(self) -> List[str]:
        """
        Every selected table, primary first, with blanks dropped.

        Falls back to a lone ``table_name`` so a cascade request from a form that
        predates the multi-select — or a hand-built URL — still resolves to a table
        list rather than to nothing.
        """
        names = [str(name) for name in self.table_names if name]

        if not names and self.table_name:
            names = [str(self.table_name)]

        return names


class ToolConfigView(ResponseSchema):
    """
    One row of the tool configs table.

    ``agent_id`` and ``datasource_id`` are the related rows' public uuids, empty
    when unset so an unselected ``<option value="">`` matches for preselection.
    ``preview`` is the rendered SQL-ish summary of the saved query, for display
    only — in builder mode the Deep Agents executor builds the real query from
    reflected tables, and in SQL mode the preview *is* the stored statement.

    ``table_name`` is the primary table and ``extra_tables`` the rest; ``tables``
    is the two joined up in display order, which is what the list page and the edit
    form's multi-select both want. Built here rather than in the template so the row
    and the form cannot disagree about the order.
    """

    uuid: str = Field(title="Tool config")
    tool_name: str = Field(title="Tool name")
    table_name: str = Field(title="Table name")
    extra_tables: List[str] = Field(default_factory=list, title="Other tables")
    description: Optional[str] = Field(default=None, title="Description")
    query_mode: str = Field(default=QUERY_MODE_BUILDER, title="Query mode")
    is_enabled: bool = Field(default=True, title="Enabled")
    allow_recursive_aggregate: bool = Field(
        default=False, title="Allow whole-result grouping",
    )
    agent_id: str = Field(default="", title="Data agent")
    agent_name: Optional[str] = Field(default=None, title="Data agent name")
    datasource_id: str = Field(default="", title="Datasource")
    datasource_name: Optional[str] = Field(default=None, title="Datasource name")
    config: dict = Field(default_factory=dict, title="Query")
    sql_query: str = Field(default="", title="SQL query")
    sql_params: List[dict] = Field(
        default_factory=list, title="Assistant-supplied values",
    )
    preview: str = Field(default="", title="Query preview")

    @field_validator("extra_tables", "sql_params", mode="before")
    @classmethod
    def default_extra_tables(cls, v: object) -> object:
        """
        ``NULL`` on either column means "none" — every row written before the column
        existed reads that way, so it becomes an empty list rather than a validation
        error when this schema is built straight from an ORM row.
        """
        return v or []

    @property
    def tables(self) -> List[str]:
        """Every table this tool reads, primary first."""
        return [self.table_name, *self.extra_tables]


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


class ChildToolOption(ResponseSchema):
    """
    One thing the Nested Tools picker may offer, and the columns it returns.

    ``columns`` is empty when the output cannot be known without running it — a
    SQL-mode tool, a builder tool that selects everything, or any graph. The form then
    takes a typed name instead of a dropdown, and the chain checks it against the real
    result. Promising a column list there would mean inventing one.

    ``kind`` says which of the two things it is: ``"tool"`` for a tool config, ``"graph"``
    for a Graph Designer graph. The form needs it because the two are posted under
    different keys — ``child_id`` and ``child_graph_id`` — and because a graph is worth
    labelling in a list a person reads.
    """

    uuid: str = Field(title="Tool")
    tool_name: str = Field(title="Tool name")
    query_mode: str = Field(default=QUERY_MODE_BUILDER, title="Query mode")
    columns: List[str] = Field(default_factory=list, title="Columns returned")
    kind: str = Field(default="tool", title="Kind")


class ChildToolOptionsResponse(ResponseSchema):
    """
    The JSON body of ``GET /tool-configs/child-options`` — every tool that may be
    embedded in the one being edited.

    Filtered by the same rules that would refuse the link on save (same owner, same
    datasource, enabled, not itself, not something that already embeds it), so the
    picker cannot offer a choice the form would then reject.

    Published **graphs** are in the same list, marked ``kind: "graph"``. One list rather
    than two because the form draws one set of rows and the choice is the same choice —
    what runs first and supplies the values.
    """

    tools: List[ChildToolOption] = Field(default_factory=list, title="Tools")
    error: Optional[str] = Field(default=None, title="Error")

    @classmethod
    def failure(cls, message: str) -> "ChildToolOptionsResponse":
        return cls(tools=[], error=message)
