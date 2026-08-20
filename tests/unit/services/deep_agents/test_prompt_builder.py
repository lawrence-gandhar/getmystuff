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

from types import SimpleNamespace

from app.services.deep_agents.prompt_builder import (
    build_tool_routing_prompt,
    compose_runtime_prompt,
)
from app.services.deep_agents.query_executor import DISPLAY_ROW_LIMIT


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
        "table_names": ["inventory_items"],
        "datasource_name": "warehouse",
        "db_type": "postgres",
    }
    tool.update(overrides)
    return tool


class TestSqlModeTools:
    def test_the_statement_is_quoted_in_full(self) -> None:
        prompt = build_tool_routing_prompt("Stock", [sql_tool()])

        assert "SELECT DISTINCT name FROM inventory_items ORDER BY name" in prompt

    def test_every_table_the_statement_reads_is_named(self) -> None:
        """
        Nothing here parses a FROM clause, so the tool config records which tables its
        statement reads and the prompt states them. Before that it named the primary
        table and waved at "any tables its query joins" — telling the model a
        two-table tool was a one-table tool.
        """
        prompt = build_tool_routing_prompt(
            "Stock",
            [sql_tool(
                sql_query="SELECT i.name FROM inventory_items i JOIN suppliers s ON s.item_id = i.id",
                table_names=["inventory_items", "suppliers"],
            )],
        )

        assert "Reads: inventory_items, suppliers in warehouse (postgres)." in prompt

    def test_a_tool_with_no_recorded_list_falls_back_to_its_primary_table(self) -> None:
        """Every row written before the tables were recorded has only the one."""
        prompt = build_tool_routing_prompt("Stock", [sql_tool(table_names=[])])

        assert "Reads: inventory_items in warehouse (postgres)." in prompt

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


class TestAToolThatSelectsNoColumns:
    """
    What the prompt promises has to match what
    ``query_executor._selected_columns`` actually returns for an empty selection —
    every *active* column of every table the query reads, prefixed with its table once
    a join is involved. A model told it will receive "every column of sales" goes
    looking for one the user switched off; one told the field is ``sales_sku`` can
    quote it.
    """

    def test_the_promise_says_active_rather_than_every_column(self) -> None:
        prompt = build_tool_routing_prompt("Stock", [builder_tool(config={})])

        assert "Returns: every active column of sales." in prompt

    def test_a_joined_tool_names_both_tables_and_the_field_prefix(self) -> None:
        joined = builder_tool(
            config={
                "joins": [
                    {
                        "type": "inner",
                        "table": "regions",
                        "left_table": "sales",
                        "left_column": "region",
                        "right_column": "code",
                    }
                ]
            }
        )

        prompt = build_tool_routing_prompt("Stock", [joined])

        assert "every active column of sales, regions" in prompt
        assert "'sales_id'" in prompt


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


class TestTheModelIsToldItCannotFilter:
    """
    The rule that stops a dead end becoming an endless one.

    A tool takes no arguments, so rephrasing a question runs the identical query and
    returns the identical result. Without being told that, a model handed a tool
    failure improvises the obvious-sounding remedy — "give me a date range", "pick a
    smaller period" — and the visitor supplies one, and gets the same refusal, and
    supplies another. Three turns of that were observed against a real widget: "latest
    projects", "August", "August 2026", one identical answer each time.
    """

    def test_the_rule_is_in_every_generated_prompt(self) -> None:
        prompt = build_tool_routing_prompt("Sales", [builder_tool()])
        # Line wrapping in the template is not what is being asserted.
        flat = " ".join(prompt.split())

        assert "Most tools take NO arguments" in flat
        assert "never ask them to narrow their question" in flat
        assert "returns exactly the same result" in flat
        # The escape hatch is named, so the rule cannot be read as forbidding the
        # parameters a tool may legitimately declare.
        assert "unless a tool declares a parameter" in flat

    def test_it_survives_the_operators_own_prompt(self) -> None:
        """
        The grounding rules are appended after the persona, so an operator cannot
        remove this one by writing their own instructions.
        """
        composed = compose_runtime_prompt(
            "You are a helpful assistant. Offer to filter results.",
            build_tool_routing_prompt("Sales", [builder_tool()]),
        )

        assert "Most tools take NO arguments" in " ".join(composed.split())


class TestParameterisedFilters:
    """
    How an open filter is described to the model, and how a fixed one still is.

    The model gets the parameters twice — once as a JSON schema on the tool, once
    here in prose. That is deliberate duplication: the schema says a field exists,
    the prompt says which column it narrows, and choosing between two tools is done
    from the prompt.
    """

    def _tool(self, **overrides) -> dict:  # noqa: ANN003
        tool = builder_tool()
        tool["config"] = dict(tool["config"])
        tool["config"]["filters"] = [
            {"column": "status", "operator": "=", "value": "paid"},
            {
                "column": "sold_at", "operator": ">", "agent_supplied": True,
                "required": True, "param": "sold_after",
                "description": "ISO date.",
            },
        ]
        tool.update(overrides)
        return tool

    def test_the_open_filter_is_named_with_its_column_and_comparison(self) -> None:
        prompt = build_tool_routing_prompt("Sales", [self._tool()])

        assert "Parameters you supply when calling this tool:" in prompt
        assert "sold_after (required): narrows sold_at > <your value>." in prompt
        assert "ISO date." in prompt

    def test_a_fixed_filter_is_still_described_as_fixed(self) -> None:
        """
        The regression this guards. The "you cannot widen or change it" sentence is
        built from the filter list, and an open filter swept into it would tell the
        model a parameter it *can* set is immovable.
        """
        prompt = build_tool_routing_prompt("Sales", [self._tool()])

        assert "Always restricted to: status = 'paid'." in prompt
        assert "sold_at" not in prompt.split("Always restricted to:")[1].split("\n")[0]

    def test_an_optional_parameter_says_so(self) -> None:
        tool = self._tool()
        tool["config"]["filters"][1]["required"] = False
        prompt = build_tool_routing_prompt("Sales", [tool])

        assert "sold_after (optional)" in prompt

    def test_a_tool_with_no_open_filters_gets_no_parameter_section(self) -> None:
        prompt = build_tool_routing_prompt("Sales", [builder_tool()])

        assert "Parameters you supply" not in prompt

    def test_the_model_is_told_not_to_invent_a_value(self) -> None:
        """
        An invented date produces a confident, precise, wrong answer — the exact
        failure the grounding rules exist to prevent, arrived at through a door
        this feature opened.
        """
        flat = " ".join(build_tool_routing_prompt("Sales", [self._tool()]).split())

        assert "Do not invent or guess one" in flat
        assert "Never invent one, never guess a date" in flat


class TestAFailedToolIsNotADeadEnd:
    """
    The rule that stops one misconfigured tool silencing an agent that has a working
    one.

    Observed against a real agent: `fetch_project_details` embedded `fetch_projects`
    in a chain that tripped a cap, so it failed on every question — while
    `fetch_projects` sat beside it, enabled, reading the same table, and answering
    perfectly when the model happened to pick it. "TOOL FAILED" was being read as
    "the data is unavailable", and the visitor was told the request could not be
    answered at all.
    """

    def test_the_model_is_told_to_try_another_tool_first(self) -> None:
        flat = " ".join(build_tool_routing_prompt("Sales", [builder_tool()]).split())

        assert "TOOL FAILED" in flat
        assert "check whether another tool listed above covers the question" in flat

    def test_the_retry_is_bounded_to_one_alternative(self) -> None:
        """
        Unbounded, "try another" becomes a model working through every tool it has
        on a question none of them answer — several database round trips inside a
        turn a visitor is waiting on.
        """
        flat = " ".join(build_tool_routing_prompt("Sales", [builder_tool()]).split())

        assert "Try ONE alternative, not every tool in turn" in flat

    def test_giving_up_is_still_the_end_state(self) -> None:
        """Rule 11's loop-prevention must survive: it still stops, it just stops later."""
        flat = " ".join(build_tool_routing_prompt("Sales", [builder_tool()]).split())

        assert "cannot answer this at the moment and stop" in flat


class TestAResultIsDescribedAsWhatItIs:
    """
    The rule against a true table under a false heading.

    Observed against a real agent: asked for "the list of projects in a department",
    it called a projects tool that filters on nothing, and headed the result
    "Projects in the department". Every row was real and the answer was wrong — the
    reader was told they were looking at one department's projects when they were
    looking at all of them, and nothing in the reply gave them a way to notice.

    Rules 11 and 12 cover what the model may *pass* to a tool. Neither covered what
    it may *claim* about the rows that came back, and a heading is a claim.
    """

    def test_the_model_is_told_to_describe_the_rows_it_got(self) -> None:
        flat = " ".join(build_tool_routing_prompt("Sales", [builder_tool()]).split())

        assert "Describe the rows you actually got, never the rows that were asked for" in flat

    def test_naming_a_narrowing_the_query_never_applied_is_called_false(self) -> None:
        flat = " ".join(build_tool_routing_prompt("Sales", [builder_tool()]).split())

        assert "you have the UNNARROWED result" in flat
        assert "is a false answer even though every row in it is real" in flat

    def test_the_heading_is_held_to_the_same_rules_as_a_figure(self) -> None:
        """
        The gap this closes. The grounding rules were read as being about numbers,
        and the sentence introducing a table is where the falsehood actually went.
        """
        flat = " ".join(build_tool_routing_prompt("Sales", [builder_tool()]).split())

        assert "The heading and the sentence above a table are claims about the data" in flat

    def test_it_does_not_reopen_asking_the_user_to_narrow(self) -> None:
        """
        Rule 11 forbids "I could answer if you gave me a date range" when no tool
        takes one. The fix for a mislabelled result must not become permission to
        ask again — the model shows what it has and names the limit, in one reply.
        """
        flat = " ".join(build_tool_routing_prompt("Sales", [builder_tool()]).split())

        assert "never ask them to narrow their question" in flat
        assert "then show it" in flat


class TestTheAnswerFormatRule:
    """
    The model has to be *told* to write Markdown, because for a long time it was told
    the opposite by the interface: the widget escaped everything, so a table came out
    as a wall of pipe characters and prose was the only thing that read correctly.
    Now the widget renders Markdown, and rows belong in a table.
    """

    def test_the_model_is_told_to_put_rows_in_a_table(self) -> None:
        flat = " ".join(build_tool_routing_prompt("Sales", [builder_tool()]).split())

        assert "Put rows in a Markdown table" in flat
        assert "|---|---|" in flat

    def test_links_and_images_are_still_forbidden(self) -> None:
        """
        The renderer does not build anchors — `[x](javascript:…)` is how Markdown
        becomes script execution — so a model writing one produces visible syntax.
        Rule 10 already bans URLs; this says why it also applies to Markdown links.
        """
        flat = " ".join(build_tool_routing_prompt("Sales", [builder_tool()]).split())

        assert "Do NOT write links or images" in flat

    def test_the_row_limit_is_restated_with_its_real_number(self) -> None:
        """
        A formatting rule that invited a table without restating the cap is an
        invitation to paste two hundred rows into a chat bubble.
        """
        flat = " ".join(build_tool_routing_prompt("Sales", [builder_tool()]).split())

        assert f"{DISPLAY_ROW_LIMIT} rows, then the total" in flat


class TestAnIteratingNestedTool:
    """
    A link that runs the tool once per value changes what the *result* is, so the
    prompt says so.

    Not because the model can do anything about it — it cannot, the tool takes no
    such argument — but because without it a model reads a wide result and calls the
    tool again per value, which is a loop it cannot get out of.
    """

    def _chain(self, iterates: bool):  # noqa: ANN202
        child = SimpleNamespace(
            tool=SimpleNamespace(tool_name="every_department"),
            iterates=iterates,
        )
        return SimpleNamespace(children=[child])

    def test_it_says_one_call_covers_every_value(self) -> None:
        prompt = build_tool_routing_prompt(
            "reporter", [builder_tool(chain=self._chain(True))],
        )

        assert "runs once for every value every_department returns" in prompt
        assert "one call already covers every one of them" in prompt

    def test_a_list_link_says_nothing_of_the_kind(self) -> None:
        """It is one run and one result set, which is what a nested tool has always
        been — so the paragraph would be describing something that is not happening."""
        prompt = build_tool_routing_prompt(
            "reporter", [builder_tool(chain=self._chain(False))],
        )

        assert "runs once for every value" not in prompt
        assert "every_department" in prompt  # still named as a restriction

    def test_a_tool_with_no_chain_is_unchanged(self) -> None:
        prompt = build_tool_routing_prompt("reporter", [builder_tool()])

        assert "runs once for every value" not in prompt


class TestDeclaredSqlValuesInThePrompt:
    """
    The model gets these as a JSON schema on the tool already, so the lines are
    duplication — deliberately, and for the reason the filter lines are: a model
    choosing *between* tools reads the prompt, not the schema.
    """

    def test_each_declared_value_is_listed_with_its_type(self) -> None:
        prompt = build_tool_routing_prompt("reporter", [sql_tool(
            sql_query="SELECT name FROM inventory_items WHERE qty > :floor",
            sql_params=[{
                "param": "floor", "type": "number", "required": True,
                "description": "The minimum quantity.",
            }],
        )])

        assert "- floor (required, number). The minimum quantity." in prompt

    def test_an_optional_one_says_so(self) -> None:
        prompt = build_tool_routing_prompt("reporter", [sql_tool(
            sql_query="SELECT name FROM inventory_items WHERE (:since IS NULL)",
            sql_params=[{"param": "since", "type": "text", "required": False}],
        )])

        assert "- since (optional, text)." in prompt

    def test_the_do_not_invent_rule_comes_with_them(self) -> None:
        """The same sentence the builder-mode filters get, because it is the same
        rule: a value nobody gave is a question for the visitor."""
        prompt = build_tool_routing_prompt("reporter", [sql_tool(
            sql_query="SELECT name FROM inventory_items WHERE qty > :floor",
            sql_params=[{"param": "floor", "required": True}],
        )])

        assert "Do not invent or guess one" in prompt

    def test_a_statement_with_none_declares_nothing(self) -> None:
        prompt = build_tool_routing_prompt("reporter", [sql_tool()])

        assert "Parameters you supply" not in prompt
