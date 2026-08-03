"""
Tests for app/services/deep_agents/prompt_builder.py.

The routing prompt is what tells an agent which tool answers which question, and
it is built in Python precisely so it cannot describe a tool the agent does not
have. These tests hold it to the two things a wrong prompt would cost:

* it must never promise a field the tool does not return — a model quoting a
  field name that is not in the result is the failure this feature exists to
  prevent;
* it must describe a SQL-mode tool without inventing a field list for it, because
  nothing here has parsed the statement's SELECT list.

Pure functions, no I/O — the rows are supplied exactly as
``prompt_sync_service.collect_agent_tools`` shapes them.
"""

from __future__ import annotations

from app.services.deep_agents.prompt_builder import (
    build_tool_routing_prompt,
    compose_runtime_prompt,
)


def builder_tool(**overrides) -> dict:  # noqa: ANN003
    tool = {
        "tool_name": "units_per_sku",
        "description": "Units sold per SKU.",
        "table_name": "sales",
        "query_mode": "builder",
        "config": {
            "columns": [{"column": "sku", "alias": ""}],
            "aggregations": [{"type": "sum", "column": "qty", "alias": "units"}],
            "group_by": ["sku"],
            "filters": [{"column": "region", "operator": "=", "value": "EU"}],
            "joins": [],
        },
        "sql_query": None,
        "datasource_name": "warehouse",
        "db_type": "postgres",
    }
    tool.update(overrides)
    return tool


def sql_tool(**overrides) -> dict:  # noqa: ANN003
    tool = {
        "tool_name": "distinct_items",
        "description": "Every distinct item name.",
        "table_name": "inventory_items",
        "query_mode": "sql",
        "config": {},
        "sql_query": "SELECT DISTINCT name FROM inventory_items ORDER BY name",
        "datasource_name": "warehouse",
        "db_type": "postgres",
    }
    tool.update(overrides)
    return tool


class TestSqlModeTools:
    def test_the_statement_is_quoted_in_full(self) -> None:
        prompt = build_tool_routing_prompt("Stock", [sql_tool()])

        assert "SELECT DISTINCT name FROM inventory_items ORDER BY name" in prompt

    def test_no_field_list_is_promised(self) -> None:
        """Nothing has parsed the SELECT list, so claiming a field list would be a
        guess — and a wrong one has the model quoting a column that is not there."""
        prompt = build_tool_routing_prompt("Stock", [sql_tool()])

        assert "Returns fields:" not in prompt
        assert "exactly as they come back in the result" in prompt

    def test_the_purpose_and_datasource_still_appear(self) -> None:
        prompt = build_tool_routing_prompt("Stock", [sql_tool()])

        assert "Every distinct item name." in prompt
        assert "warehouse (postgres)" in prompt

    def test_a_sql_tool_with_no_description_still_gets_an_entry(self) -> None:
        prompt = build_tool_routing_prompt("Stock", [sql_tool(description="")])

        assert "## distinct_items" in prompt
        assert "not described by the operator" in prompt

    def test_both_modes_appear_in_one_prompt(self) -> None:
        prompt = build_tool_routing_prompt("Stock", [builder_tool(), sql_tool()])

        assert "## units_per_sku" in prompt
        assert "## distinct_items" in prompt
        assert "You have 2 data tools" in prompt


class TestBuilderModeTools:
    def test_an_aliased_aggregation_is_named_by_its_alias(self) -> None:
        """The alias is what the result column is called, and is not guessable
        from the config by a model reading only the SQL."""
        prompt = build_tool_routing_prompt("Stock", [builder_tool()])

        assert "units (SUM of qty)" in prompt

    def test_a_fixed_filter_is_stated_as_unchangeable(self) -> None:
        prompt = build_tool_routing_prompt("Stock", [builder_tool()])

        assert "region = 'EU'" in prompt
        assert "cannot" in prompt

    def test_grouping_warns_against_reading_a_group_total_as_an_overall_one(
        self,
    ) -> None:
        prompt = build_tool_routing_prompt("Stock", [builder_tool()])

        assert "One row per distinct sku" in prompt


class TestNoTools:
    def test_an_agent_with_no_tools_is_told_to_refuse(self) -> None:
        prompt = build_tool_routing_prompt("Stock", [])

        assert "NO data tools" in prompt
        assert "invent figures" in prompt
        assert "Do not attempt to answer from general knowledge" in prompt


class TestComposeRuntimePrompt:
    def test_the_operators_prompt_comes_first(self) -> None:
        composed = compose_runtime_prompt("Be terse.", "# Data tools")

        assert composed.startswith("Be terse.")

    def test_a_missing_section_is_dropped_rather_than_leaving_blank_lines(
        self,
    ) -> None:
        assert compose_runtime_prompt(None, "# Data tools") == "# Data tools"
        assert compose_runtime_prompt("  ", "# Data tools") == "# Data tools"
