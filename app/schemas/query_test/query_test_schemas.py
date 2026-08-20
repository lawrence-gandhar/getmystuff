"""
app/schemas/query_test/query_test_schemas.py

Pydantic schemas for Test Query — running a tool query once, before it is saved, to
find out whether the database will accept it.

The request is deliberately the **same fields the save posts**, and nothing else:
which datasource, which tables, which mode, and the query in whichever of the two
shapes that mode uses. Both callers — the Tool Configs form and the Ask AI panel —
send their own form as it stands, so the statement that is tested is the statement
that will be saved. A schema that accepted anything narrower would let the two drift,
and a test of a query nobody is about to save is worth nothing.

Nothing here decides whether the query is *valid*. That is the job of the validators
the save itself goes through (``tool_config_service.validated_query_config`` and
``validated_tool_sql``) and, after them, of the database. This schema guarantees only
that the payload has the right shape and bounded size — the same split
``tool_config_schemas`` draws around ``config_json`` and ``sql_query``, for the same
reason: the guard with the reflected schema in hand is the one that has to be right.
"""

from typing import List

from pydantic import Field, field_validator

from app.models.tool_configs import QUERY_MODE_BUILDER, QUERY_MODE_VALUES
from app.schemas.base import (
    FormRequest,
    JsonArrayField,
    JsonObjectField,
    ObjectName,
    OptionalUUID,
    ResponseSchema,
)
from app.schemas.tool_configs import MAX_TOOL_SQL_LENGTH, MAX_TOOL_TABLES


class QueryTestRequest(FormRequest):
    """
    One press of **Test Query**.

    Posted with ``hx-include`` from whichever form is open, so it arrives carrying
    that form's other fields too — the tool name, the description, the agent. Those
    are ignored (``extra="ignore"`` on every request schema), which is what lets one
    endpoint serve two panels without either of them building a payload by hand.

    ``table_names`` is a multi-select and so is declared in ``multi_fields``: read as
    a single value it would test a query against one table when the user picked four,
    and report a pass for a query that is about to be saved reading more than it just
    proved it could read.
    """

    multi_fields = ("table_names",)

    datasource_id: OptionalUUID = Field(default=None, title="Datasource")
    table_names: List[ObjectName] = Field(
        default_factory=list, title="Tables", max_length=MAX_TOOL_TABLES
    )
    query_mode: str = Field(default=QUERY_MODE_BUILDER, title="Query mode")
    config_json: JsonObjectField = Field(default_factory=dict, title="Query")
    sql_query: str = Field(
        default="", title="SQL query", max_length=MAX_TOOL_SQL_LENGTH
    )
    # The tools this one embeds, from the same hidden field the save posts. Carried
    # for the same reason every other field is: a nested tool tested without its
    # children is a different, unrestricted query, and a pass on that says nothing
    # about the tool that would be created.
    children_json: JsonArrayField = Field(
        default_factory=list, title="Nested tools",
    )
    # The values the statement asks the assistant for, and what to try them with.
    # Carried for the same reason as `children_json`: a statement holding
    # `:department_id` cannot run without one, so a test that left it out would only
    # ever report a missing parameter.
    sql_params_json: JsonArrayField = Field(
        default_factory=list, title="Assistant-supplied values",
    )
    test_values_json: JsonObjectField = Field(
        default_factory=dict, title="Test values",
    )

    @field_validator("query_mode")
    @classmethod
    def validate_query_mode(cls, v: str) -> str:
        """Blank means the builder, matching the Tool Configs form's own default."""
        mode = (v or "").strip().lower() or QUERY_MODE_BUILDER

        if mode not in QUERY_MODE_VALUES:
            raise ValueError("Query mode is not one of the available options")

        return mode


class QueryTestResponse(ResponseSchema):
    """
    What the test found, as the partial renders it.

    ``passed`` drives which alert is shown and ``message`` is the sentence inside it
    — the database's own words when the database is what refused, because the person
    reading this is the person who has to fix the query.

    ``columns`` and ``row_count`` are the whole of what a passing test reports. **No
    values.** Proving the query runs needs one row fetched, not one row displayed,
    and a panel that starts printing rows is a data export nobody asked for — in Ask
    AI it would also break the promise the feature is built on, that the panel shows
    structure and never contents.
    """

    passed: bool = Field(title="Passed")
    message: str = Field(title="Result")
    columns: List[str] = Field(default_factory=list, title="Columns returned")
    row_count: int = Field(default=0, title="Rows read")
