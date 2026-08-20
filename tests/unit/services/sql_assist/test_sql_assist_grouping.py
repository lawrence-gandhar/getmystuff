"""
Tests for what Ask AI does about a query the database would refuse to run.

MySQL's default sql_mode includes ONLY_FULL_GROUP_BY and PostgreSQL enforces the
same rule, so a generated query that selects a column it neither aggregates nor
groups is not a query at all — it is an error waiting to be raised, either in front
of the user or, worse, in front of a visitor once the query has been saved as a tool.

Three things are asserted here, in the order they take effect:

* the prompt tells the model the rule, so most attempts never break it;
* a query that breaks it anyway sends the model back to write another one, told
  exactly what was wrong — never patched here, because adding the column to the
  grouping would answer a different question than the explanation beside it;
* a second failure is shown to the user as a warning next to the query, not raised.
  The check is a heuristic and nothing is executed, so the user reading the SQL is
  better placed than a refusal that leaves them nothing to look at.

The model is stubbed at the seam the service imports (``answer_structured``): what is
under test is the retry and the warning, not any provider.
"""

from __future__ import annotations

from typing import List

import pytest

from app.services.sql_assist import sql_assist_service as svc
from tests.unit.services.sql_assist.conftest import CUSTOMERS, ORDERS

GOOD_SQL = "SELECT orders.total, COUNT(*) FROM orders GROUP BY orders.total"
BAD_SQL = "SELECT orders.customer_id, COUNT(*) FROM orders GROUP BY orders.total"


def _draft(sql: str, explanation: str = "It counts.") -> svc.SqlDraft:
    return svc.SqlDraft(sql=sql, explanation=explanation, assumptions=[])


@pytest.fixture
def stub_model(monkeypatch: pytest.MonkeyPatch) -> dict:
    """
    Answer every call to the model from a queue, recording what it was asked.

    The last answer is repeated once the queue runs dry, so a test only has to say
    what it wants to be different about each attempt.
    """
    state: dict = {"answers": [], "asked": []}

    async def fake_answer_structured(  # noqa: ANN202
        db, user_id, system_prompt, user_content, model, **kwargs  # noqa: ANN001
    ):
        state["asked"].append(user_content)
        answers: List[svc.SqlDraft] = state["answers"]
        return answers.pop(0) if len(answers) > 1 else answers[0]

    monkeypatch.setattr(svc, "answer_structured", fake_answer_structured)
    return state


class TestThePromptStatesTheRule:
    def test_the_model_is_told_every_selected_column_must_be_grouped(self) -> None:
        system_prompt, _ = svc._build_prompts("MySQL", [ORDERS], "count them", [])

        assert "must either be inside an aggregate function or be listed in the "\
            "GROUP BY" in system_prompt

    def test_the_rule_is_named_so_the_model_knows_it_is_not_advice(self) -> None:
        system_prompt, _ = svc._build_prompts("MySQL", [ORDERS], "count them", [])

        assert "ONLY_FULL_GROUP_BY" in system_prompt

    def test_mixing_an_aggregate_with_a_plain_column_is_called_out(self) -> None:
        """The same refusal without a GROUP BY at all — the shape a model reaches
        for when asked "how many projects per client"."""
        system_prompt, _ = svc._build_prompts("MySQL", [ORDERS], "count them", [])

        assert "may not mix an aggregate with a plain column" in system_prompt

    def test_the_tool_conversion_is_told_the_same_thing(self) -> None:
        """A builder config that selects an ungrouped column is refused when the tool
        is saved, so the conversion is told the rule rather than left to discover it
        through a failed save."""
        system_prompt, _ = svc._build_tool_prompts(
            "MySQL", "mysql", [ORDERS], GOOD_SQL,
        )

        assert "must also appear in group_by" in system_prompt


class TestTheRetry:
    async def test_a_sound_query_is_returned_untouched_and_asked_once(
        self, stub_model  # noqa: ANN001
    ) -> None:
        stub_model["answers"] = [_draft(GOOD_SQL)]

        draft, warnings = await svc._regrouped(
            None, 1, "system", "user", _draft(GOOD_SQL), [ORDERS], None, False,
        )

        assert draft.sql == GOOD_SQL
        assert warnings == []
        assert stub_model["asked"] == []

    async def test_a_bad_query_is_written_again(self, stub_model) -> None:  # noqa: ANN001
        stub_model["answers"] = [_draft(GOOD_SQL)]

        draft, warnings = await svc._regrouped(
            None, 1, "system", "user", _draft(BAD_SQL), [ORDERS], None, False,
        )

        assert draft.sql == GOOD_SQL
        assert warnings == []

    async def test_the_model_is_told_which_column_was_wrong(
        self, stub_model  # noqa: ANN001
    ) -> None:
        stub_model["answers"] = [_draft(GOOD_SQL)]

        await svc._regrouped(
            None, 1, "system", "user", _draft(BAD_SQL), [ORDERS], None, False,
        )

        asked = stub_model["asked"][0]
        assert "orders.customer_id" in asked
        assert "ONLY_FULL_GROUP_BY" in asked
        assert BAD_SQL in asked

    async def test_a_second_failure_keeps_the_first_query_and_warns(
        self, stub_model  # noqa: ANN001
    ) -> None:
        """The user sees the query and what is wrong with it — which beats being told
        to try again with nothing on the screen."""
        stub_model["answers"] = [_draft("SELECT orders.id, COUNT(*) FROM orders "
                                        "GROUP BY orders.total")]

        draft, warnings = await svc._regrouped(
            None, 1, "system", "user", _draft(BAD_SQL), [ORDERS], None, False,
        )

        assert draft.sql == BAD_SQL
        assert len(warnings) == 1
        assert "orders.customer_id" in warnings[0]
        assert "GROUP BY" in warnings[0]

    async def test_a_retry_that_answers_nothing_keeps_the_first_query(
        self, stub_model  # noqa: ANN001
    ) -> None:
        """An empty sql means "the schema cannot answer this" — which is not true
        here, since a query was already written. The warning is the honest answer."""
        stub_model["answers"] = [_draft("", "The schema has no such column.")]

        draft, warnings = await svc._regrouped(
            None, 1, "system", "user", _draft(BAD_SQL), [ORDERS], None, False,
        )

        assert draft.sql == BAD_SQL
        assert len(warnings) == 1

    async def test_a_retry_refused_by_the_read_only_guard_does_not_fail_the_turn(
        self, stub_model  # noqa: ANN001
    ) -> None:
        """The first query is still readable and its fault is known, so it is kept.
        Failing here would leave the user nothing over a retry they never asked for."""
        stub_model["answers"] = [_draft("SELECT * FROM orders GROUP BY orders.total")]

        draft, warnings = await svc._regrouped(
            None, 1, "system", "user", _draft(BAD_SQL), [ORDERS], None, False,
        )

        assert draft.sql == BAD_SQL
        assert len(warnings) == 1

    async def test_the_retry_is_asked_only_once(self, stub_model) -> None:  # noqa: ANN001
        stub_model["answers"] = [_draft(BAD_SQL)]

        await svc._regrouped(
            None, 1, "system", "user", _draft(BAD_SQL), [ORDERS], None, False,
        )

        assert len(stub_model["asked"]) == 1

    async def test_a_column_of_a_grouped_primary_key_is_not_second_guessed(
        self, stub_model  # noqa: ANN001
    ) -> None:
        """Both databases allow it, so the reflected keys go to the check — otherwise
        the most ordinary grouped query there is would be rewritten for nothing."""
        dependent = (
            "SELECT customers.id, customers.name, COUNT(*) FROM customers "
            "GROUP BY customers.id"
        )
        stub_model["answers"] = [_draft(GOOD_SQL)]

        draft, warnings = await svc._regrouped(
            None, 1, "system", "user", _draft(dependent), [CUSTOMERS], None, False,
        )

        assert draft.sql == dependent
        assert warnings == []
        assert stub_model["asked"] == []


class TestThePrimaryKeysHandedToTheCheck:
    def test_each_table_s_key_is_taken_from_the_reflection(self) -> None:
        assert svc._primary_keys([ORDERS, CUSTOMERS]) == {
            "orders": ["id"], "customers": ["id"],
        }

    def test_a_view_has_no_key_and_says_so(self) -> None:
        """A view reflects without key entries at all — an empty list, not a guess."""
        assert svc._primary_keys([{"table": "monthly_totals", "columns": []}]) == {
            "monthly_totals": [],
        }


class TestWhatTheGenerateCallReturns:
    async def test_a_warning_travels_back_with_the_draft(
        self, db, user, make_datasource, stub_reflection, stub_model  # noqa: ANN001
    ) -> None:
        stub_model["answers"] = [_draft(BAD_SQL)]
        datasource = await make_datasource(user, configuration_data={})

        result = await svc.generate_sql(
            db,
            user.id,
            datasource.uuid,
            ["orders"],
            "how many orders per customer",
            "in_built",
        )

        assert result["draft"].sql == BAD_SQL
        assert len(result["warnings"]) == 1

    async def test_a_sound_query_carries_no_warnings(
        self, db, user, make_datasource, stub_reflection, stub_model  # noqa: ANN001
    ) -> None:
        stub_model["answers"] = [_draft(GOOD_SQL)]
        datasource = await make_datasource(user, configuration_data={})

        result = await svc.generate_sql(
            db, user.id, datasource.uuid, ["orders"], "totals please", "in_built",
        )

        assert result["warnings"] == []
        assert result["history"][-1]["sql"] == GOOD_SQL
