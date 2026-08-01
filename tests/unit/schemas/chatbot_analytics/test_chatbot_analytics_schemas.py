"""
Tests for app/schemas/chatbot_analytics/chatbot_analytics_schemas.py.

The distinction these tests exist for: an *absent* filter is the default view,
while a *present but unreadable* one is refused. It matters more here than in a
form, because a filter that silently falls back does not fail visibly — it renders
real figures for the wrong scope, and nothing on screen says so.
"""

from __future__ import annotations

import uuid

import pytest
from litestar.exceptions import HTTPException

from app.schemas.chatbot_analytics import AnalyticsDashboardQuery
from app.services.chatbot_analytics.chatbot_analytics_service import (
    DEFAULT_PERIOD,
    PERIOD_OPTIONS,
)

VALID_UUID = "3f4b2c1e-0000-4000-8000-000000000001"


def _detail(data: dict) -> str:
    with pytest.raises(HTTPException) as exc_info:
        AnalyticsDashboardQuery.parse(data)
    return str(exc_info.value.detail)


class TestPeriod:
    def test_the_first_paint_uses_the_default(self) -> None:
        assert AnalyticsDashboardQuery.parse({}).period == DEFAULT_PERIOD

    @pytest.mark.parametrize("period", ["", "   ", None])
    def test_an_empty_period_is_the_default(self, period) -> None:
        assert AnalyticsDashboardQuery.parse({"period": period}).period == DEFAULT_PERIOD

    @pytest.mark.parametrize("period", sorted(PERIOD_OPTIONS))
    def test_every_window_the_service_can_compute_is_accepted(self, period: str) -> None:
        """
        Sourced from ``PERIOD_OPTIONS`` rather than a copied list, so adding a
        window to the service does not leave this test asserting a stale set.
        """
        assert AnalyticsDashboardQuery.parse({"period": period}).period == period

    def test_a_window_the_dashboard_cannot_compute_is_refused(self) -> None:
        """
        Answering ``?period=all-time`` with 7 days would be a wrong answer
        presented as a right one.
        """
        detail = _detail({"period": "all-time"})
        assert "Period must be one of" in detail
        assert DEFAULT_PERIOD in detail


class TestChatbotFilter:
    def test_no_filter_means_all_agents(self) -> None:
        assert AnalyticsDashboardQuery.parse({}).chatbot_id is None

    def test_a_blank_filter_means_all_agents(self) -> None:
        assert AnalyticsDashboardQuery.parse({"chatbot_id": ""}).chatbot_id is None

    def test_a_valid_filter_is_parsed(self) -> None:
        assert AnalyticsDashboardQuery.parse({"chatbot_id": VALID_UUID}).chatbot_id == (
            uuid.UUID(VALID_UUID)
        )

    def test_an_unreadable_filter_is_refused_rather_than_ignored(self) -> None:
        """A broken link must never show figures for the wrong scope."""
        assert _detail({"chatbot_id": "not-an-id"}) == (
            "Chatbot is not a valid selection"
        )
