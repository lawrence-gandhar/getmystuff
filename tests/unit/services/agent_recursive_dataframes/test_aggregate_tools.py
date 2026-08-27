"""
The agent tool, and the promise that switching it off changes nothing.

The second half is the one that matters for "do not break existing functionality".
An agent with no opted-in tool must get the tool list it got before this module
existed and a routing prompt byte-identical to the one it had — and that is
asserted here rather than argued, because it is the sort of claim that stops being
true quietly.
"""

from __future__ import annotations

from typing import Any, Callable, Dict

import pytest

pytest.importorskip(
    "langgraph", reason="langgraph is installed in the container only (see Dockerfile)",
)

from app.services.agent_recursive_dataframes import (  # noqa: E402
    aggregate_service,
    aggregate_tools,
)
from app.services.deep_agents.prompt_builder import (  # noqa: E402
    AGGREGATE_TOOL,
    build_tool_routing_prompt,
)
from app.services.deep_agents.query_executor import (  # noqa: E402
    NOT_AVAILABLE,
    ToolQueryError,
)


@pytest.fixture
def permitted(tool_entry: Callable) -> Dict[str, Any]:
    return tool_entry(rows=400)


@pytest.fixture
def forbidden(tool_entry: Callable) -> Dict[str, Any]:
    return {**tool_entry(rows=400), "allow_recursive_aggregate": False}


# --- The opt-in gate -----------------------------------------------------


class TestTheOptInGate:
    def test_no_opted_in_tool_means_no_context_and_no_tool(
        self, forbidden: Dict[str, Any],
    ) -> None:
        context = aggregate_tools.aggregate_context([forbidden], model=None)

        assert context is None
        assert aggregate_tools.build_aggregate_tools(context) == []

    def test_one_opted_in_tool_is_enough(
        self, permitted: Dict[str, Any], forbidden: Dict[str, Any],
    ) -> None:
        context = aggregate_tools.aggregate_context([forbidden, permitted], None)

        assert context is not None
        assert [entry["tool_name"] for entry in context.tools] == ["sales_records"]

    def test_only_opted_in_tools_reach_the_tool(
        self, permitted: Dict[str, Any], forbidden: Dict[str, Any],
    ) -> None:
        """
        The filtering happens once, here. A tool that was not opted in must not be
        groupable by naming it in the instruction either.
        """
        context = aggregate_tools.aggregate_context(
            [{**forbidden, "tool_name": "secret_tool"}, permitted], None,
        )

        assert "secret_tool" not in [entry["tool_name"] for entry in context.tools]

    def test_an_empty_tool_list_gives_no_context(self) -> None:
        assert aggregate_tools.aggregate_context([], None) is None

    def test_the_built_tool_is_bound_once_not_once_per_tool_config(
        self, permitted: Dict[str, Any], tool_entry: Callable,
    ) -> None:
        second = {**tool_entry(rows=10), "tool_name": "invoices"}
        context = aggregate_tools.aggregate_context([permitted, second], None)

        built = aggregate_tools.build_aggregate_tools(context)

        assert len(built) == 1
        assert built[0].name == AGGREGATE_TOOL


class TestNothingChangesWhenItIsOff:
    def test_the_routing_prompt_is_unchanged_with_every_tool_opted_out(
        self, forbidden: Dict[str, Any],
    ) -> None:
        """
        The load-bearing regression guard. Compared against the prompt built from an
        entry that has no such key at all — which is what every stored tool looked
        like before the column existed.
        """
        before = {
            key: value for key, value in forbidden.items()
            if key != "allow_recursive_aggregate"
        }

        assert build_tool_routing_prompt("Agent", [forbidden]) == (
            build_tool_routing_prompt("Agent", [before])
        )

    def test_the_routing_prompt_never_names_the_tool_when_it_is_off(
        self, forbidden: Dict[str, Any],
    ) -> None:
        assert AGGREGATE_TOOL not in build_tool_routing_prompt("Agent", [forbidden])

    def test_the_routing_prompt_names_it_and_the_tool_once_opted_in(
        self, permitted: Dict[str, Any],
    ) -> None:
        prompt = build_tool_routing_prompt("Agent", [permitted])

        assert AGGREGATE_TOOL in prompt
        assert "sales_records" in prompt

    def test_only_opted_in_tools_are_named_in_the_prompt(
        self, permitted: Dict[str, Any], forbidden: Dict[str, Any],
    ) -> None:
        prompt = build_tool_routing_prompt(
            "Agent", [permitted, {**forbidden, "tool_name": "invoices"}],
        )

        # Both are still described as data tools; only one is offered for grouping.
        assert "invoices" in prompt
        assert prompt.count("`invoices`") == 0


# --- The tool itself -----------------------------------------------------


class TestTheTool:
    def test_its_description_names_the_tools_and_the_refusals(
        self, permitted: Dict[str, Any],
    ) -> None:
        built = aggregate_tools.build_aggregate_tools(
            aggregate_tools.aggregate_context([permitted], None),
        )[0]

        assert "sales_records" in built.description
        # A model that does not know a median is unavailable will ask for one.
        assert "medians" in built.description
        assert "count, sum, avg, min, max" in built.description

    def test_it_takes_an_instruction_and_an_optional_tool_name(
        self, permitted: Dict[str, Any],
    ) -> None:
        built = aggregate_tools.build_aggregate_tools(
            aggregate_tools.aggregate_context([permitted], None),
        )[0]
        fields = built.args_schema.model_fields

        assert set(fields) == {"instruction", "tool_name"}
        assert fields["instruction"].is_required()
        assert not fields["tool_name"].is_required()

    async def test_a_failure_becomes_tool_output_rather_than_a_raise(
        self, permitted: Dict[str, Any], monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        A raise here would end the whole chat turn with a 500 for something the
        model can say out loud and move on from.
        """
        async def refuse(*args: Any, **kwargs: Any) -> Any:
            raise ToolQueryError("that cannot be grouped", advice=NOT_AVAILABLE)

        monkeypatch.setattr(aggregate_service, "aggregate", refuse)

        built = aggregate_tools.build_aggregate_tools(
            aggregate_tools.aggregate_context([permitted], None),
        )[0]
        reply = await built.coroutine(instruction="median spend")

        assert reply.startswith("TOOL FAILED:")
        assert "that cannot be grouped" in reply
        assert NOT_AVAILABLE in reply

    async def test_an_unexpected_error_also_becomes_tool_output(
        self, permitted: Dict[str, Any], monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def explode(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("something unforeseen")

        monkeypatch.setattr(aggregate_service, "aggregate", explode)

        built = aggregate_tools.build_aggregate_tools(
            aggregate_tools.aggregate_context([permitted], None),
        )[0]
        reply = await built.coroutine(instruction="totals by region")

        assert reply.startswith("TOOL FAILED:")
        assert "something unforeseen" in reply

    async def test_a_result_reports_the_totals_and_how_many_records_they_cover(
        self, permitted: Dict[str, Any], monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def answer(*args: Any, **kwargs: Any) -> Dict[str, Any]:
            return {
                "tool_name": "sales_records",
                "summary": "sum of amount from 'sales_records', grouped by region.",
                "columns": ["region", "sum_amount"],
                "rows": [{"region": "north", "sum_amount": 12.5}],
                "group_count": 1,
                "records_read": 404,
                "total_records": 404,
            }

        monkeypatch.setattr(aggregate_service, "aggregate", answer)

        built = aggregate_tools.build_aggregate_tools(
            aggregate_tools.aggregate_context([permitted], None),
        )[0]
        reply = await built.coroutine(instruction="total amount by region")

        assert "grouped by region" in reply
        assert "404" in reply
        assert "north" in reply

    async def test_a_capped_result_says_how_many_groups_there_really_were(
        self, permitted: Dict[str, Any], monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        The one thing a model must not get wrong about a total: 200 rows out of
        4,821 groups is not the whole answer, and describe_result already says so.
        """
        async def answer(*args: Any, **kwargs: Any) -> Dict[str, Any]:
            return {
                "tool_name": "sales_records",
                "summary": "counts by region.",
                "columns": ["region", "n"],
                "rows": [{"region": f"r{i}", "n": i} for i in range(200)],
                "group_count": 4_821,
                "records_read": 90_000,
                "total_records": 90_000,
            }

        monkeypatch.setattr(aggregate_service, "aggregate", answer)

        built = aggregate_tools.build_aggregate_tools(
            aggregate_tools.aggregate_context([permitted], None),
        )[0]
        reply = await built.coroutine(instruction="counts by region")

        assert "4821" in reply or "4,821" in reply

    async def test_no_records_reads_as_an_answer_not_a_failure(
        self, permitted: Dict[str, Any], monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def answer(*args: Any, **kwargs: Any) -> Dict[str, Any]:
            return {
                "tool_name": "sales_records",
                "summary": "counts by region.",
                "columns": ["region", "n"],
                "rows": [],
                "group_count": 0,
                "records_read": 0,
                "total_records": 0,
            }

        monkeypatch.setattr(aggregate_service, "aggregate", answer)

        built = aggregate_tools.build_aggregate_tools(
            aggregate_tools.aggregate_context([permitted], None),
        )[0]
        reply = await built.coroutine(instruction="counts by region")

        assert not reply.startswith("TOOL FAILED:")
        assert "no records" in reply

    async def test_the_tool_name_argument_is_passed_through(
        self, permitted: Dict[str, Any], monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        seen: Dict[str, Any] = {}

        async def capture(tools, instruction, model, tool_name=None):  # noqa: ANN001
            seen["tool_name"] = tool_name
            return {"rows": [], "group_count": 0, "records_read": 0,
                    "summary": "", "columns": [], "tool_name": ""}

        monkeypatch.setattr(aggregate_service, "aggregate", capture)

        built = aggregate_tools.build_aggregate_tools(
            aggregate_tools.aggregate_context([permitted], None),
        )[0]

        await built.coroutine(instruction="totals", tool_name="sales_records")
        assert seen["tool_name"] == "sales_records"

        # An empty string means "not given", not "a tool called nothing".
        await built.coroutine(instruction="totals", tool_name="")
        assert seen["tool_name"] is None


# --- End to end through the tool -----------------------------------------


class TestThroughTheTool:
    async def test_a_real_run_reports_real_totals(
        self, permitted: Dict[str, Any], sqlite_answer: Callable,
    ) -> None:
        """
        No mocking below the tool: a stub model plans it, the graph runs it against
        a real SQLite database, and the numbers are checked against SQLite.
        """
        from tests.unit.services.agent_recursive_dataframes.test_aggregate_planner import (  # noqa: E501
            StubModel,
            _measure,
            _plan,
        )

        model = StubModel(plan=_plan(
            group_by=["region"], aggregations=[_measure("sum", "amount")],
        ))
        built = aggregate_tools.build_aggregate_tools(
            aggregate_tools.aggregate_context([permitted], model),
        )[0]

        reply = await built.coroutine(
            instruction="total amount by region", tool_name="sales_records",
        )

        assert not reply.startswith("TOOL FAILED:")

        expected = sqlite_answer(
            permitted["datasource"],
            "SELECT SUM(amount) AS total FROM sales WHERE region = 'north'",
        )[0]["total"]

        assert f"{expected}" in reply or f"{round(expected, 1)}" in reply
