"""
Tests for app/schemas/query_test/query_test_schemas.py.

The request is posted by ``hx-include`` from whichever form is open, so two
properties matter more than the field list: it has to **accept the fields it does
not care about** (the tool name, the description, the agent — the Tool Configs form
sends all of them), and it has to read the tables multi-select as a list, because a
test that silently covers one of four tables reports a pass for a query that has not
been tested.

What is deliberately *not* here is any judgement about the query itself. That is the
same split ``tool_config_schemas`` draws: the shape is checked here, the references
are checked by ``tool_config_service`` with the reflected schema in hand, and whether
it runs is the database's answer.
"""

from __future__ import annotations

import pytest
from litestar.exceptions import HTTPException

from app.schemas.query_test import QueryTestRequest, QueryTestResponse

VALID_UUID = "3f4b2c1e-0000-4000-8000-000000000001"


def _detail(data: dict) -> str:
    with pytest.raises(HTTPException) as exc_info:
        QueryTestRequest.parse(data)
    return str(exc_info.value.detail)


class TestQueryTestRequestShape:
    def test_the_builder_form_parses(self) -> None:
        payload = QueryTestRequest.parse(
            {
                "datasource_id": VALID_UUID,
                "table_names": ["orders", "customers"],
                "query_mode": "builder",
                "config_json": '{"columns": [{"column": "total"}]}',
                "sql_query": "",
            }
        )

        assert payload.table_names == ["orders", "customers"]
        assert payload.config_json == {"columns": [{"column": "total"}]}

    def test_the_sql_panel_form_parses(self) -> None:
        payload = QueryTestRequest.parse(
            {
                "datasource_id": VALID_UUID,
                "table_names": ["orders"],
                "query_mode": "sql",
                "sql_query": "SELECT DISTINCT status FROM orders",
            }
        )

        assert payload.query_mode == "sql"
        assert payload.sql_query == "SELECT DISTINCT status FROM orders"

    def test_the_other_fields_the_form_sends_are_ignored(self) -> None:
        """The button posts the whole form. Rejecting it for carrying a tool name
        would mean each panel building a payload by hand."""
        payload = QueryTestRequest.parse(
            {
                "datasource_id": VALID_UUID,
                "table_names": ["orders"],
                "tool_name": "orders_by_status",
                "description": "Whatever",
                "data_agent_id": VALID_UUID,
                "agent_filter": VALID_UUID,
                "history_json": "[]",
            }
        )

        assert payload.table_names == ["orders"]

    def test_a_blank_mode_means_the_builder(self) -> None:
        assert QueryTestRequest.parse({"query_mode": ""}).query_mode == "builder"

    def test_an_absent_mode_means_the_builder(self) -> None:
        assert QueryTestRequest.parse({}).query_mode == "builder"

    def test_an_unknown_mode_is_refused(self) -> None:
        assert "not one of the available options" in _detail({"query_mode": "mongo"})

    def test_an_unselected_datasource_is_none_and_not_an_empty_string(self) -> None:
        """The service reports "pick a datasource" from ``None``; ``""`` would reach
        the uuid lookup instead."""
        assert QueryTestRequest.parse({"datasource_id": ""}).datasource_id is None

    def test_a_table_name_that_could_break_out_of_an_identifier_is_refused(
        self,
    ) -> None:
        assert "Table" in _detail({"table_names": ["orders; drop table x"]})

    def test_more_tables_than_a_query_may_read_is_refused(self) -> None:
        from app.schemas.query_test.query_test_schemas import MAX_TOOL_TABLES

        detail = _detail({"table_names": [f"t{n}" for n in range(MAX_TOOL_TABLES + 1)]})

        assert "Tables" in detail

    def test_config_json_that_is_not_an_object_is_refused(self) -> None:
        assert _detail({"config_json": "[1, 2, 3]"})

    def test_an_over_long_statement_is_refused(self) -> None:
        from app.schemas.query_test.query_test_schemas import MAX_TOOL_SQL_LENGTH

        assert _detail({"sql_query": "x" * (MAX_TOOL_SQL_LENGTH + 1)})


class TestQueryTestResponseShape:
    def test_a_pass_carries_the_shape_that_came_back(self) -> None:
        payload = QueryTestResponse.build(
            {
                "passed": True,
                "message": "The query ran successfully and returned name, qty.",
                "columns": ["name", "qty"],
                "row_count": 1,
            }
        ).payload()

        assert payload == {
            "passed": True,
            "message": "The query ran successfully and returned name, qty.",
            "columns": ["name", "qty"],
            "row_count": 1,
        }

    def test_a_failure_needs_no_columns(self) -> None:
        payload = QueryTestResponse.build(
            {"passed": False, "message": "no such column: nope"}
        ).payload()

        assert payload["columns"] == []
        assert payload["row_count"] == 0
