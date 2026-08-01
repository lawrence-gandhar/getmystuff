"""
Tests for app/schemas/deep_agents/deep_agent_schemas.py.

The console's question is the one free-text field sent straight to a language
model, so its cap is a cost control as much as a validation — a large paste would
be tokenised and paid for before anything noticed.

The cascade's ``workspace_id`` has a non-obvious rule that is easy to "fix" into a
bug: a blank workspace lists *every* agent rather than none, because an agent's
workspace is nullable and an unassigned agent has to remain pickable.
"""

from __future__ import annotations

import uuid

import pytest
from litestar.exceptions import HTTPException

from app.schemas.deep_agents import (
    MAX_QUESTION_LENGTH,
    AgentOptionsQuery,
    DeepAgentAskRequest,
)

VALID_UUID = "3f4b2c1e-0000-4000-8000-000000000001"


def _detail(schema, data: dict) -> str:
    with pytest.raises(HTTPException) as exc_info:
        schema.parse(data)
    return str(exc_info.value.detail)


class TestAgentOptionsQuery:
    def test_a_blank_workspace_is_none_which_means_list_every_agent(self) -> None:
        """
        Deliberate, and the opposite of what "no filter, no results" would
        suggest: ``data_agents.workspace_id`` is nullable, so an unassigned agent
        must still be reachable from the unfiltered cascade.
        """
        assert AgentOptionsQuery.parse({"workspace_id": ""}).workspace_id is None

    def test_the_field_name_defaults_to_the_shared_one(self) -> None:
        assert AgentOptionsQuery.parse({}).select_name == "data_agent_id"

    def test_a_host_form_can_name_the_select_itself(self) -> None:
        """Neither host form dictates the other's markup."""
        assert AgentOptionsQuery.parse({"field_name": "llm_agent"}).select_name == (
            "llm_agent"
        )

    def test_a_whitespace_only_field_name_falls_back(self) -> None:
        assert AgentOptionsQuery.parse({"field_name": "   "}).select_name == (
            "data_agent_id"
        )

    def test_the_current_selection_is_parsed(self) -> None:
        assert AgentOptionsQuery.parse({"selected": VALID_UUID}).selected == (
            uuid.UUID(VALID_UUID)
        )

    def test_an_unreadable_selection_is_refused(self) -> None:
        assert _detail(AgentOptionsQuery, {"selected": "abc"}) == (
            "Data agent is not a valid selection"
        )


class TestAskRequest:
    def test_a_normal_question(self) -> None:
        assert DeepAgentAskRequest.parse(
            {"question": "  How many orders shipped late?  "}
        ).question == "How many orders shipped late?"

    @pytest.mark.parametrize("question", ["", "   ", "\t\n"])
    def test_an_empty_box_is_refused(self, question: str) -> None:
        """Replaces the handler's hand-rolled "Type a question first." check."""
        assert _detail(DeepAgentAskRequest, {"question": question}) == (
            "Question is required"
        )

    def test_a_missing_field_is_refused(self) -> None:
        assert _detail(DeepAgentAskRequest, {}) == "Question is required"

    def test_the_cap_boundary(self) -> None:
        at_cap = "q" * MAX_QUESTION_LENGTH
        assert DeepAgentAskRequest.parse({"question": at_cap}).question == at_cap
        assert "cannot be longer than 2000" in _detail(
            DeepAgentAskRequest, {"question": "q" * (MAX_QUESTION_LENGTH + 1)}
        )

    def test_the_cap_matches_the_other_prompt_boxes(self) -> None:
        """
        A person typing into a console and a person typing into Ask AI should not
        hit two different limits.
        """
        from app.schemas.ai_analytics import MAX_ANALYTICS_PROMPT_LENGTH
        from app.schemas.sql_assist import MAX_SQL_PROMPT_LENGTH

        assert MAX_QUESTION_LENGTH == MAX_SQL_PROMPT_LENGTH == MAX_ANALYTICS_PROMPT_LENGTH
