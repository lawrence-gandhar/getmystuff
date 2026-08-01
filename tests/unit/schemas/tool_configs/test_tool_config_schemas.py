"""
Tests for app/schemas/tool_configs/tool_config_schemas.py.

``config_json`` is the highest-value input in the application to get wrong: it
carries the query the user built in the browser, referring to real tables and
columns that are interpolated into generated SQL rather than bound as parameters.

The split these tests pin is deliberate. This schema guarantees the field is a JSON
*object* of bounded size; ``tool_config_service.validated_query_config`` — which
knows which tables the query is allowed to touch, because it has just reflected
them — validates every reference inside it. So there are tests asserting that a
query referring to an unknown column *passes here*: if it failed, the rule would
have been duplicated, and the copy without the reflected schema in hand would be
the wrong one.
"""

from __future__ import annotations

import uuid

import pytest
from litestar.exceptions import HTTPException

from app.schemas.tool_configs import (
    SchemaCascadeQuery,
    TableColumnsResponse,
    ToolConfigCreateRequest,
    ToolConfigDeleteRequest,
    ToolConfigListQuery,
    ToolConfigSetEnabledRequest,
    ToolConfigUpdateRequest,
    ToolConfigView,
)

VALID_UUID = "3f4b2c1e-0000-4000-8000-000000000001"
WRITE_FORMS = [ToolConfigCreateRequest, ToolConfigUpdateRequest]
MUTATIONS = WRITE_FORMS + [ToolConfigSetEnabledRequest, ToolConfigDeleteRequest]


def _detail(schema, data: dict) -> str:
    with pytest.raises(HTTPException) as exc_info:
        schema.parse(data)
    return str(exc_info.value.detail)


def _valid() -> dict:
    return {"tool_name": "total_units", "table_name": "sales_data"}


@pytest.mark.parametrize("schema", MUTATIONS)
class TestEveryMutationCarriesTheAgentFilter:
    def test_the_filter_is_declared(self, schema) -> None:
        assert "agent_filter" in schema.model_fields

    def test_an_unreadable_filter_is_refused(self, schema) -> None:
        data = {"agent_filter": "nope"}
        if schema in WRITE_FORMS:
            data.update(_valid())
        assert _detail(schema, data) == "Agent filter is not a valid selection"


@pytest.mark.parametrize("schema", WRITE_FORMS, ids=["create", "update"])
class TestWriteForms:
    def test_a_minimal_valid_form(self, schema) -> None:
        payload = schema.parse(_valid())
        assert payload.tool_name == "total_units"
        assert payload.table_name == "sales_data"
        assert payload.config_json == {}

    def test_tool_name_is_lowercased(self, schema) -> None:
        assert schema.parse({**_valid(), "tool_name": "Total_Units"}).tool_name == (
            "total_units"
        )

    def test_tool_name_must_be_an_identifier(self, schema) -> None:
        """
        The name is handed to the model as a tool identifier and reaches SQL
        identifiers downstream, so it is a plain lowercase identifier or nothing.
        """
        assert "must start with a letter" in _detail(
            schema, {**_valid(), "tool_name": "9lives"}
        )

    def test_tool_name_is_required(self, schema) -> None:
        assert _detail(schema, {"table_name": "sales"}) == "Tool name is required"

    def test_table_name_is_required(self, schema) -> None:
        assert _detail(schema, {"tool_name": "t"}) == "Table name is required"

    @pytest.mark.parametrize(
        "table_name",
        ["sales; drop table users", "sales'--", 'sales"x', "sales`x", "salés"],
    )
    def test_a_table_name_that_could_break_an_identifier_is_refused(
        self, schema, table_name: str
    ) -> None:
        """The injection guard — this name is interpolated, not parameterised."""
        assert "is not a valid name" in _detail(
            schema, {**_valid(), "table_name": table_name}
        )

    def test_a_file_datasource_object_name_is_allowed(self, schema) -> None:
        payload = schema.parse({**_valid(), "table_name": "sales_data.csv"})
        assert payload.table_name == "sales_data.csv"

    def test_the_query_is_parsed_from_the_hidden_field(self, schema) -> None:
        payload = schema.parse(
            {**_valid(), "config_json": '{"columns": [{"table": "sales_data"}]}'}
        )
        assert payload.config_json == {"columns": [{"table": "sales_data"}]}

    def test_a_malformed_query_is_refused_rather_than_saved_as_empty(
        self, schema
    ) -> None:
        """
        Saving ``{}`` instead would throw away the query the user just built and
        report success.
        """
        assert "could not be read" in _detail(
            schema, {**_valid(), "config_json": "{not json"}
        )

    def test_a_query_referring_to_unknown_columns_passes_this_layer(
        self, schema
    ) -> None:
        """
        Deliberate. Deciding whether ``no_such_column`` exists needs the reflected
        schema, which only tool_config_service has. Rejecting it here would mean
        two implementations of the same guard, and this one would be guessing.
        """
        payload = schema.parse(
            {**_valid(), "config_json": '{"columns": [{"column": "no_such_column"}]}'}
        )
        assert payload.config_json["columns"][0]["column"] == "no_such_column"

    def test_missing_dropdowns_are_left_for_the_service(self, schema) -> None:
        """
        "Required" and "you don't own that" are the same query, so both belong to
        the service — splitting them would report "Data agent is required" for an
        agent that exists but belongs to someone else.
        """
        payload = schema.parse(_valid())
        assert payload.data_agent_id is None
        assert payload.datasource_id is None


class TestSetEnabled:
    def test_reads_the_toggle(self) -> None:
        assert ToolConfigSetEnabledRequest.parse({"is_enabled": "true"}).is_enabled
        assert not ToolConfigSetEnabledRequest.parse({"is_enabled": "false"}).is_enabled

    def test_an_absent_flag_is_false(self) -> None:
        assert ToolConfigSetEnabledRequest.parse({}).is_enabled is False


class TestListQuery:
    def test_the_data_agents_page_link_is_parsed(self) -> None:
        assert ToolConfigListQuery.parse({"agent": VALID_UUID}).agent == (
            uuid.UUID(VALID_UUID)
        )

    def test_no_filter_lists_everything(self) -> None:
        assert ToolConfigListQuery.parse({}).agent is None


class TestSchemaCascadeQuery:
    def test_the_first_cascade_step_has_no_table_yet(self) -> None:
        query = SchemaCascadeQuery.parse({"datasource_id": VALID_UUID})
        assert query.table == ""

    def test_table_exposes_a_string_not_none(self) -> None:
        """The services take a string; ``None`` would reach them as "None"."""
        assert SchemaCascadeQuery.parse({}).table == ""

    def test_a_table_name_is_still_held_to_the_safe_character_set(self) -> None:
        """
        It came from a dropdown the server rendered, but it is read back out of
        the user's database — so it is checked regardless.
        """
        with pytest.raises(HTTPException):
            SchemaCascadeQuery.parse({"table_name": "sales; drop table x"})


class TestTableColumnsResponse:
    def test_a_failure_reports_in_the_payload_rather_than_raising(self) -> None:
        """
        The join builder shows the reason beside the join row; raising would
        replace the whole offcanvas with an error page mid-edit.
        """
        payload = TableColumnsResponse.failure("sales", "Could not connect").payload()
        assert payload == {
            "table_name": "sales",
            "columns": [],
            "error": "Could not connect",
        }

    def test_a_success_carries_no_error(self) -> None:
        payload = TableColumnsResponse.build(
            {"table_name": "sales", "columns": ["a", "b"], "error": None}
        ).payload()
        assert payload["error"] is None
        assert payload["columns"] == ["a", "b"]


class TestToolConfigView:
    def test_related_ids_default_to_empty_strings_for_preselection(self) -> None:
        view = ToolConfigView.build(
            {"uuid": "u", "tool_name": "t", "table_name": "tbl"}
        )
        assert (view.agent_id, view.datasource_id) == ("", "")

    def test_no_internal_id_is_exposed(self) -> None:
        payload = ToolConfigView.payload_for(
            {"id": 9, "uuid": "u", "tool_name": "t", "table_name": "tbl"}
        )
        assert "id" not in payload
