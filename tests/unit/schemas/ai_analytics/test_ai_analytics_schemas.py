"""
Tests for app/schemas/ai_analytics/ai_analytics_schemas.py.

The cross-field rule is what these tests are mostly about. ``target_type`` decides
what identifies the target — a ``file`` needs ``file_id``, a ``table`` or
``collection`` is named — and a half-specified target that reaches the service does
not fail cleanly: it fails deep inside ``_load_one_target`` with
"file_id is required for file targets", after a datasource has been loaded and a
history row written.
"""

from __future__ import annotations

import uuid

import pytest
from litestar.exceptions import HTTPException

from app.schemas.ai_analytics import (
    MAX_ANALYTICS_PROMPT_LENGTH,
    TARGET_TYPES,
    AiAnalyticsGenerateRequest,
    AiAnalyticsHistoryQuery,
)

VALID_UUID = "3f4b2c1e-0000-4000-8000-000000000001"


def _detail(schema, data: dict) -> str:
    with pytest.raises(HTTPException) as exc_info:
        schema.parse(data)
    return str(exc_info.value.detail)


def _valid(**extra) -> dict:
    return {
        "target_type": "table",
        "target_name": "sales_data",
        "prompt": "Which region grew fastest?",
        **extra,
    }


class TestGenerate:
    def test_a_valid_table_target(self) -> None:
        payload = AiAnalyticsGenerateRequest.parse(_valid())
        assert payload.target_type == "table"
        assert payload.target_name == "sales_data"
        assert payload.file_id is None

    def test_a_valid_file_target(self) -> None:
        payload = AiAnalyticsGenerateRequest.parse(
            _valid(target_type="file", target_name="sales.csv", file_id=VALID_UUID)
        )
        assert payload.file_id == uuid.UUID(VALID_UUID)

    @pytest.mark.parametrize("target_type", sorted(TARGET_TYPES))
    def test_every_declared_target_type_is_accepted(self, target_type: str) -> None:
        data = _valid(target_type=target_type)
        if target_type == "file":
            data["file_id"] = VALID_UUID
        assert AiAnalyticsGenerateRequest.parse(data).target_type == target_type

    def test_an_unknown_target_type_is_refused(self) -> None:
        assert _detail(AiAnalyticsGenerateRequest, _valid(target_type="cube")) == (
            "Target type must be one of datasource, file, table or collection"
        )

    def test_a_file_target_must_say_which_file(self) -> None:
        """
        Before this rule the request reached ``_load_one_target``, which refused it
        with a message written for a developer — and only after a history row had
        been written.
        """
        assert _detail(
            AiAnalyticsGenerateRequest, _valid(target_type="file", target_name="a.csv")
        ) == "Please choose which file to analyse"

    def test_target_name_is_required_for_every_type(self) -> None:
        """Restates what ``generate_analytics`` already enforces, but earlier."""
        data = _valid()
        del data["target_name"]
        assert _detail(AiAnalyticsGenerateRequest, data) == "Target name is required"

    def test_the_prompt_is_required(self) -> None:
        assert _detail(AiAnalyticsGenerateRequest, _valid(prompt="   ")) == (
            "Prompt is required"
        )

    def test_the_prompt_is_bounded(self) -> None:
        """Every accepted prompt is a paid model call."""
        at_cap = "p" * MAX_ANALYTICS_PROMPT_LENGTH
        assert AiAnalyticsGenerateRequest.parse(_valid(prompt=at_cap)).prompt == at_cap
        assert "cannot be longer than 2000" in _detail(
            AiAnalyticsGenerateRequest,
            _valid(prompt="p" * (MAX_ANALYTICS_PROMPT_LENGTH + 1)),
        )

    @pytest.mark.parametrize(
        "target_name", ["sales; drop table users", 'sales"x', "salés"]
    )
    def test_a_target_name_that_could_break_an_identifier_is_refused(
        self, target_name: str
    ) -> None:
        """
        It came from a dropdown, but it is interpolated into a generated query
        rather than bound as a parameter.
        """
        assert "is not a valid name" in _detail(
            AiAnalyticsGenerateRequest, _valid(target_name=target_name)
        )

    def test_a_file_object_name_is_allowed(self) -> None:
        payload = AiAnalyticsGenerateRequest.parse(
            _valid(target_type="file", target_name="sales data.csv", file_id=VALID_UUID)
        )
        assert payload.target_name == "sales data.csv"

    def test_an_unreadable_file_reference_is_refused(self) -> None:
        assert _detail(
            AiAnalyticsGenerateRequest, _valid(target_type="file", file_id="nope")
        ) == "File is not a valid selection"


class TestHistoryQuery:
    def test_no_target_yet_means_no_history_rather_than_a_bad_request(self) -> None:
        """The panel is opened before a target has been chosen."""
        query = AiAnalyticsHistoryQuery.parse({})
        assert (query.target_type, query.target_name) == ("", "")

    def test_a_valid_target_filter(self) -> None:
        query = AiAnalyticsHistoryQuery.parse(
            {"target_type": "collection", "target_name": "orders"}
        )
        assert (query.target_type, query.target_name) == ("collection", "orders")

    def test_a_present_but_unknown_target_type_is_refused(self) -> None:
        with pytest.raises(HTTPException):
            AiAnalyticsHistoryQuery.parse({"target_type": "cube"})
