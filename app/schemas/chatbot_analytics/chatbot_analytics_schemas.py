"""
app/schemas/chatbot_analytics/chatbot_analytics_schemas.py

Pydantic schemas for the Chatbot Analytics module.

Read-only: the dashboard takes a period and an optional agent filter, and returns
figures. So there is one request schema, and its whole job is to make sure the
numbers on screen are the numbers for the scope the user asked for.

That matters more here than in a form. A filter that silently falls back to the
default does not fail visibly — it shows a real dashboard for the wrong scope, and
a person reading it has no way to tell. Both fields are therefore refused rather
than defaulted when present but unreadable, which is the behaviour the module's
own ``_parse_chatbot_filter`` already had and this schema preserves.
"""

from pydantic import Field, field_validator

from app.schemas.base import OptionalUUID, QueryRequest
from app.services.chatbot_analytics.chatbot_analytics_service import (
    DEFAULT_PERIOD,
    PERIOD_OPTIONS,
)


class AnalyticsDashboardQuery(QueryRequest):
    """
    The dashboard's filter bar: how far back to look, and whose turns to count.

    A blank ``chatbot_id`` means "all agents" — the unfiltered view, and the one
    the page opens on.
    """

    period: str = Field(default=DEFAULT_PERIOD, title="Period")
    chatbot_id: OptionalUUID = Field(default=None, title="Chatbot")

    @field_validator("period", mode="before")
    @classmethod
    def validate_period(cls, v: object) -> object:
        """
        An absent period is the default; a present but unknown one is refused.

        The distinction is the point: no ``?period=`` at all is the first paint,
        while ``?period=all-time`` is a request for a window this dashboard cannot
        compute, and answering it with 7 days would be a wrong answer presented as
        a right one.
        """
        if v is None or (isinstance(v, str) and not v.strip()):
            return DEFAULT_PERIOD

        key = str(v).strip()
        if key not in PERIOD_OPTIONS:
            allowed = ", ".join(PERIOD_OPTIONS)
            raise ValueError(
                f"Period must be one of {allowed}. Please pick one from the list."
            )

        return key
