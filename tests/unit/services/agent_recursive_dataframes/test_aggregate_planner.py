"""
Turning an instruction into a plan — written from the premise that the model is wrong.

A model naming a column that does not exist is the expected case here, not the
exceptional one, so most of this is about what happens then: the refusal has to name
the tool's real columns, and nothing unvalidated may reach the data.

The other thing asserted throughout is how often the model is *not* called. A named
tool and a single available tool both resolve without one, and the stub counts
calls so that stays true.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List

import pytest

from app.schemas.agent_recursive_dataframes import AggregationPlan
from app.services.agent_recursive_dataframes import aggregate_planner as planner
from app.services.deep_agents.query_executor import ToolQueryError
from app.services.downloader_agents.base.record_reader import RecordSource


def _source(entry: Dict[str, Any]) -> RecordSource:
    """
    The tool entry as the one source it resolves to when it embeds nothing.

    Built here rather than imported from ``aggregate_service``: that module pulls
    ``aggregate_graph`` and so LangGraph, which is installed in the container only,
    and everything in this file is testable without it.
    """
    return RecordSource(
        datasource=entry["datasource"],
        config=dict(entry.get("config") or {}),
        table_name=entry["table_name"],
        sql_query=entry.get("sql_query"),
        table_names=list(entry.get("table_names") or []),
        sql_params=list(entry.get("sql_params") or []),
    )

# --- A stub model --------------------------------------------------------


class StubStructured:
    def __init__(self, owner: "StubModel", plan: Any) -> None:
        self._owner, self._plan = owner, plan

    async def ainvoke(self, messages: Any, config: Any = None) -> Any:
        self._owner.calls.append(messages)
        self._owner.configs.append(config)

        if isinstance(self._plan, Exception):
            raise self._plan

        return self._plan


class StubModel:
    """
    A chat model that answers with whatever it was constructed with, and counts.

    ``calls`` is what the "the model is never asked" tests assert on — the cheapest
    correct answer is the one that costs nothing, and that has to be checked rather
    than hoped for.
    """

    def __init__(self, plan: Any = None, reply: str = "") -> None:
        self.plan, self.reply, self.calls = plan, reply, []
        # Every config the planner passed. Asserted on by the tag tests: a planning call
        # that reached the provider untagged would have its tokens streamed into somebody's
        # answer as if the assistant had written them.
        self.configs: list = []

    def with_structured_output(self, schema: Any) -> StubStructured:  # noqa: ANN401
        return StubStructured(self, self.plan)

    async def ainvoke(self, messages: Any, config: Any = None) -> Any:
        self.calls.append(messages)
        self.configs.append(config)

        if isinstance(self.reply, Exception):
            raise self.reply

        class _Reply:
            content = self.reply

        return _Reply()


def _plan(**fields: Any) -> AggregationPlan:
    return AggregationPlan.model_construct(
        group_by=fields.get("group_by", []),
        aggregations=fields.get("aggregations", []),
        unsupported=fields.get("unsupported", False),
        reason=fields.get("reason", ""),
    )


def _measure(type_: str, column: str = "") -> Any:
    from app.schemas.agent_recursive_dataframes import PlannedAggregation

    return PlannedAggregation.model_construct(type=type_, column=column, alias="")


COLUMNS = ["id", "region", "amount", "sold_on"]


@pytest.fixture
def entry(tool_entry: Callable) -> Dict[str, Any]:
    return tool_entry(rows=50)


# --- Choosing the tool ---------------------------------------------------


class TestChoosingTheTool:
    async def test_a_named_tool_resolves_without_asking_the_model(
        self, entry: Dict[str, Any],
    ) -> None:
        model = StubModel()
        other = {**entry, "tool_name": "invoices"}

        chosen = await planner.choose_tool(
            [entry, other], "totals by region", model, tool_name="sales_records",
        )

        assert chosen["tool_name"] == "sales_records"
        assert model.calls == [], "the model was asked something already decided"

    async def test_a_named_tool_matches_regardless_of_casing(
        self, entry: Dict[str, Any],
    ) -> None:
        chosen = await planner.choose_tool(
            [entry], "totals", StubModel(), tool_name="  SALES_Records ",
        )

        assert chosen["tool_name"] == "sales_records"

    async def test_a_single_tool_resolves_without_asking_the_model(
        self, entry: Dict[str, Any],
    ) -> None:
        model = StubModel()

        assert await planner.choose_tool([entry], "totals", model) is not None
        assert model.calls == []

    async def test_a_name_that_does_not_exist_lists_the_ones_that_do(
        self, entry: Dict[str, Any],
    ) -> None:
        with pytest.raises(ToolQueryError) as caught:
            await planner.choose_tool(
                [entry], "totals", StubModel(), tool_name="ivnoices",
            )

        assert "ivnoices" in str(caught.value)
        assert "sales_records" in str(caught.value)

    async def test_no_tools_at_all_says_so_rather_than_failing_obscurely(
        self,
    ) -> None:
        with pytest.raises(ToolQueryError, match="No tool is set up"):
            await planner.choose_tool([], "totals", StubModel())

    async def test_the_model_picks_between_several_by_name(
        self, entry: Dict[str, Any],
    ) -> None:
        model = StubModel(reply="invoices")
        other = {**entry, "tool_name": "invoices"}

        chosen = await planner.choose_tool([entry, other], "unpaid totals", model)

        assert chosen["tool_name"] == "invoices"
        assert len(model.calls) == 1

    async def test_a_model_naming_nothing_real_is_refused(
        self, entry: Dict[str, Any],
    ) -> None:
        model = StubModel(reply="whatever_table")

        with pytest.raises(ToolQueryError) as caught:
            await planner.choose_tool(
                [entry, {**entry, "tool_name": "invoices"}], "totals", model,
            )

        assert "sales_records" in str(caught.value)

    async def test_the_catalogue_never_contains_a_value_from_the_data(
        self, entry: Dict[str, Any],
    ) -> None:
        """
        The model choosing a tool sees names, descriptions and tables — the same
        fields the routing prompt already showed it, and no records.
        """
        model = StubModel(reply="sales_records")

        await planner.choose_tool(
            [entry, {**entry, "tool_name": "invoices"}], "totals", model,
        )

        sent = str(model.calls[0])

        assert "sales_records" in sent
        assert "north" not in sent and "amount" not in sent


# --- The columns ---------------------------------------------------------


class TestProbingColumns:
    async def test_the_real_columns_come_from_the_database(
        self, entry: Dict[str, Any],
    ) -> None:
        assert await planner.probe_columns(
            entry, _source(entry),
        ) == COLUMNS

    async def test_a_sql_mode_tools_columns_are_read_the_same_way(
        self, tool_entry: Callable,
    ) -> None:
        entry = tool_entry(
            rows=20, sql_query="SELECT region, amount AS spend FROM sales",
        )

        assert await planner.probe_columns(
            entry, _source(entry),
        ) == ["region", "spend"]

    async def test_a_broken_tool_fails_readably(self, entry: Dict[str, Any]) -> None:
        entry["table_name"] = "nope"
        entry["table_names"] = ["nope"]

        with pytest.raises(ToolQueryError):
            await planner.probe_columns(
                entry, _source(entry),
            )


# --- Validation ----------------------------------------------------------


class TestValidation:
    def test_a_hallucinated_column_is_refused_with_the_real_names(
        self, entry: Dict[str, Any],
    ) -> None:
        proposed = _plan(group_by=["regoin"], aggregations=[_measure("count")])

        with pytest.raises(ToolQueryError) as caught:
            planner.validate_plan(proposed, COLUMNS, entry)

        assert "regoin" in str(caught.value)
        for column in COLUMNS:
            assert column in str(caught.value)

    def test_columns_are_matched_case_insensitively_and_normalised(
        self, entry: Dict[str, Any],
    ) -> None:
        """
        The spelling that comes out is the tool's, not the model's. polars matches
        column names byte for byte, so "Region" left as-is would fail three nodes
        later where the column can no longer be explained.
        """
        proposed = _plan(
            group_by=["  Region "], aggregations=[_measure("sum", "AMOUNT")],
        )

        validated = planner.validate_plan(proposed, COLUMNS, entry)

        assert validated.group_by == ["region"]
        assert validated.aggregations[0].column == "amount"

    @pytest.mark.parametrize("function", ["count_distinct", "median", "percentile"])
    def test_a_function_with_no_exact_fold_is_refused(
        self, entry: Dict[str, Any], function: str,
    ) -> None:
        proposed = _plan(aggregations=[_measure(function, "amount")])

        with pytest.raises(ToolQueryError) as caught:
            planner.validate_plan(proposed, COLUMNS, entry)

        assert function in str(caught.value)
        assert "sum" in str(caught.value)

    def test_unsupported_becomes_the_models_own_reason(
        self, entry: Dict[str, Any],
    ) -> None:
        proposed = _plan(
            unsupported=True, reason="A median cannot be built from batches.",
        )

        with pytest.raises(ToolQueryError, match="median"):
            planner.validate_plan(proposed, COLUMNS, entry)

    def test_grouping_with_no_measure_is_refused(self, entry: Dict[str, Any]) -> None:
        """
        Grouping needs something to report per group. Since filters exist, an empty
        measure list on its own is no longer a mistake — it asks for the matching
        records — but grouping them and measuring nothing still is.
        """
        with pytest.raises(ToolQueryError, match="nothing was measured"):
            planner.validate_plan(_plan(group_by=["region"]), COLUMNS, entry)

    def test_a_plan_asking_for_nothing_at_all_is_refused(
        self, entry: Dict[str, Any],
    ) -> None:
        with pytest.raises(ToolQueryError, match="Nothing was asked for"):
            planner.validate_plan(_plan(), COLUMNS, entry)

    def test_only_count_may_be_asked_without_a_column(
        self, entry: Dict[str, Any],
    ) -> None:
        with pytest.raises(ToolQueryError, match="needs a column"):
            planner.validate_plan(_plan(aggregations=[_measure("sum")]), COLUMNS, entry)

    def test_the_same_group_column_twice_is_refused(
        self, entry: Dict[str, Any],
    ) -> None:
        proposed = _plan(
            group_by=["region", "Region"], aggregations=[_measure("count")],
        )

        with pytest.raises(ToolQueryError, match="twice"):
            planner.validate_plan(proposed, COLUMNS, entry)

    def test_averaging_what_the_tool_already_averaged_is_refused(
        self, entry: Dict[str, Any],
    ) -> None:
        entry["config"] = {"aggregations": [{"type": "avg", "column": "amount"}]}
        proposed = _plan(aggregations=[_measure("avg", "avg_amount")])

        with pytest.raises(ToolQueryError, match="already an average"):
            planner.validate_plan(proposed, ["avg_amount"], entry)


class TestAliases:
    def test_aliases_are_assigned_not_taken_from_the_model(
        self, entry: Dict[str, Any],
    ) -> None:
        proposed = _plan(
            group_by=["region"],
            aggregations=[_measure("count"), _measure("sum", "amount")],
        )

        validated = planner.validate_plan(proposed, COLUMNS, entry)

        assert [item.alias for item in validated.aggregations] == [
            "record_count", "sum_amount",
        ]

    def test_an_alias_never_collides_with_a_group_key(
        self, entry: Dict[str, Any],
    ) -> None:
        """
        A collision would put a total in the column holding the group's name — the
        grouping would still look like a grouping and the key would be gone.
        """
        proposed = _plan(
            group_by=["amount"], aggregations=[_measure("sum", "id")],
        )
        # Group by a whole-number column so the float rule does not fire first.
        validated = planner.validate_plan(proposed, ["amount", "id"], entry)

        assert validated.aggregations[0].alias not in validated.group_by

    def test_two_measures_over_the_same_column_get_distinct_names(
        self, entry: Dict[str, Any],
    ) -> None:
        proposed = _plan(
            aggregations=[_measure("sum", "amount"), _measure("sum", "amount")],
        )

        validated = planner.validate_plan(proposed, COLUMNS, entry)
        aliases = [item.alias for item in validated.aggregations]

        assert len(set(aliases)) == 2


# --- The schema's own refusals -------------------------------------------


class TestSchemaBounds:
    def test_the_schema_refuses_an_unfoldable_function_before_the_planner_does(
        self,
    ) -> None:
        from litestar.exceptions import HTTPException

        with pytest.raises(HTTPException) as caught:
            AggregationPlan.parse({
                "aggregations": [{"type": "median", "column": "amount"}],
            })

        assert "median" in str(caught.value.detail)

    def test_too_many_group_columns_is_refused(self) -> None:
        from litestar.exceptions import HTTPException

        with pytest.raises(HTTPException):
            AggregationPlan.parse({
                "group_by": ["a", "b", "c", "d", "e"],
                "aggregations": [{"type": "count"}],
            })

    def test_repeating_a_group_column_is_refused(self) -> None:
        from litestar.exceptions import HTTPException

        with pytest.raises(HTTPException):
            AggregationPlan.parse({
                "group_by": ["region", "region"],
                "aggregations": [{"type": "count"}],
            })


# --- End to end, with a stub model ---------------------------------------


class TestPlan:
    async def test_an_instruction_becomes_a_validated_plan(
        self, entry: Dict[str, Any],
    ) -> None:
        model = StubModel(plan=_plan(
            group_by=["Region"],
            aggregations=[_measure("count"), _measure("avg", "amount")],
        ))

        plan_data = await planner.plan(
            entry,
            COLUMNS,
            "how many and the average amount by region",
            model,
        )

        assert plan_data["group_by"] == ["region"]
        assert [a["alias"] for a in plan_data["aggregations"]] == [
            "record_count", "avg_amount",
        ]

    async def test_an_empty_instruction_is_refused_before_anything_runs(
        self, entry: Dict[str, Any],
    ) -> None:
        model = StubModel()

        with pytest.raises(ToolQueryError, match="No instruction"):
            planner.validated_instruction("   ")

        assert model.calls == []

    async def test_a_provider_failure_becomes_a_readable_refusal(
        self, entry: Dict[str, Any],
    ) -> None:
        model = StubModel(plan=RuntimeError("the provider fell over"))

        with pytest.raises(ToolQueryError) as caught:
            await planner.plan(
                entry,
                COLUMNS,
                "totals by region",
                model,
            )

        assert "could not be turned into a grouping" in str(caught.value)

    async def test_no_model_at_all_is_refused_rather_than_guessed(
        self, entry: Dict[str, Any],
    ) -> None:
        with pytest.raises(ToolQueryError, match="No language model"):
            await planner.plan(
                entry,
                COLUMNS,
                "totals by region",
                None,
            )

    async def test_the_prompt_carries_column_names_and_no_values(
        self, entry: Dict[str, Any],
    ) -> None:
        model = StubModel(plan=_plan(aggregations=[_measure("count")]))

        await planner.plan(
            entry, COLUMNS, "how many records", model,
        )

        sent = str(model.calls[0])

        for column in COLUMNS:
            assert column in sent
        # Names, never rows: probe_tool_query reports no values at all.
        assert "north" not in sent


class TestThePlannersCallsAreTaggedAsInternal:
    """
    The planner's model call is machinery, not conversation, and the streamer has to be
    able to tell.

    ``astream_events`` reports ``on_chat_model_stream`` for **every** chat-model call in a
    turn. There used to be exactly one — the agent answering — and ``aggregate_records``
    added a second that runs *inside a tool*. So the plan's raw JSON was streamed as answer
    text, and a visitor saw it printed above the answer:

        {"group_by":["crm_id","department"],...}**Total revenue in August:** 4,100,165.90

    Tagged rather than given a separate model instance: it is the same model, the same key
    and the same rate limit, and the only thing that differs is whether a human is meant to
    read the output. ``deep_agent_service`` drops tagged tokens and keeps counting their
    cost, which is the right split — the call is real spend and unreal prose.
    """

    async def test_the_planning_call_carries_the_internal_tag(
        self, entry: Dict[str, Any],
    ) -> None:
        from app.services.deep_agents.prompt_builder import INTERNAL_CALL_TAG

        model = StubModel(plan=_plan(aggregations=[_measure("count")]))

        await planner.plan(entry, COLUMNS, "how many records", model)

        assert model.configs[0]["tags"] == [INTERNAL_CALL_TAG]

    async def test_the_tool_choosing_call_carries_it_too(
        self, entry: Dict[str, Any],
    ) -> None:
        """
        The other nested call. A bare tool name streamed into an answer reads as the
        assistant having said it, which is a smaller leak than the JSON and the same bug.
        """
        from app.services.deep_agents.prompt_builder import INTERNAL_CALL_TAG

        model = StubModel(reply="sales_records")
        second = {**entry, "tool_name": "other_tool"}

        await planner.choose_tool([entry, second], "totals by region", model)

        assert model.configs[0]["tags"] == [INTERNAL_CALL_TAG]

    async def test_no_model_call_is_made_when_the_tool_is_named(
        self, entry: Dict[str, Any],
    ) -> None:
        """
        Still the cheapest path, and worth re-asserting beside the tag: a tagged call that
        did not need to happen is still a call.
        """
        model = StubModel(reply="sales_records")

        await planner.choose_tool(
            [entry], "totals by region", model, tool_name=entry["tool_name"],
        )

        assert model.configs == []


class TestSummary:
    def test_the_summary_describes_what_actually_ran(
        self, entry: Dict[str, Any],
    ) -> None:
        summary = planner.plan_summary(
            {
                "group_by": ["region"],
                "aggregations": [
                    {"type": "count", "column": "", "alias": "record_count"},
                    {"type": "sum", "column": "amount", "alias": "sum_amount"},
                ],
            },
            entry,
        )

        assert "the number of records" in summary
        assert "sum of amount" in summary
        assert "grouped by region" in summary

    def test_an_ungrouped_plan_says_so(self, entry: Dict[str, Any]) -> None:
        summary = planner.plan_summary(
            {"group_by": [], "aggregations": [
                {"type": "sum", "column": "amount", "alias": "sum_amount"},
            ]},
            entry,
        )

        assert "over every record" in summary
