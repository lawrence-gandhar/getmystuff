"""
Aggregate reads over the chatbot turn log (``chatbot_messages``) for the
Chatbot Analytics dashboard.

Every one of these is a grouped aggregate over a date range — the sort of read
CRUDQueryBuilder's row-level generics can't express — so they live in this
feature's own query module, the same way flow_builder and chatbot already keep
their joined reads next to db_utils rather than inside it.

Two rules hold across the whole module:

* Ownership is enforced in SQL, by joining every query to
  ``chatbot_api_keys`` and filtering on ``user_id``. No caller can accidentally
  read another account's traffic by passing the wrong id.
* Percentiles use ``percentile_cont``, so the "slow tail" figures the dashboard
  reports come from the database rather than from loading every row into
  Python to sort it.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import Float, and_, case, cast, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chatbot import ChatbotApiKey, ChatbotMessage

# Response-time percentile reported alongside the average. The average alone
# hides the slow tail that visitors actually complain about.
_SLOW_PERCENTILE = 0.95


def _error_flag():
    """1 for a failed turn, 0 otherwise — summed to count errors in-database."""
    return case((ChatbotMessage.status == "error", 1), else_=0)


def _percentile_ms():
    return func.percentile_cont(_SLOW_PERCENTILE).within_group(
        ChatbotMessage.response_time_ms.asc()
    )


def _scoped(
    columns: List[Any],
    user_id: int,
    chatbot_key_id: Optional[int],
    since: Optional[datetime],
):
    """A select over the turn log, restricted to one user's chatbots."""
    conditions = [ChatbotApiKey.user_id == user_id]
    if chatbot_key_id is not None:
        conditions.append(ChatbotMessage.chatbot_key_id == chatbot_key_id)
    if since is not None:
        conditions.append(ChatbotMessage.created_at >= since)

    return (
        select(*columns)
        .select_from(ChatbotMessage)
        .join(ChatbotApiKey, ChatbotApiKey.id == ChatbotMessage.chatbot_key_id)
        .where(and_(*conditions))
    )


async def fetch_totals(
    db: AsyncSession,
    user_id: int,
    chatbot_key_id: Optional[int] = None,
    since: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Headline figures for the whole selected period."""
    query = _scoped(
        [
            func.count(ChatbotMessage.id).label("messages"),
            func.coalesce(func.sum(_error_flag()), 0).label("errors"),
            func.avg(cast(ChatbotMessage.response_time_ms, Float)).label("avg_ms"),
            _percentile_ms().label("p95_ms"),
            func.max(ChatbotMessage.response_time_ms).label("max_ms"),
            func.coalesce(func.sum(ChatbotMessage.request_tokens), 0).label("request_tokens"),
            func.coalesce(func.sum(ChatbotMessage.response_tokens), 0).label("response_tokens"),
            func.coalesce(func.sum(ChatbotMessage.total_tokens), 0).label("total_tokens"),
            func.coalesce(func.sum(ChatbotMessage.llm_call_count), 0).label("llm_calls"),
            func.coalesce(func.sum(case((ChatbotMessage.tokens_estimated, 1), else_=0)), 0).label(
                "estimated_rows"
            ),
        ],
        user_id,
        chatbot_key_id,
        since,
    )
    row = (await db.execute(query)).mappings().first()
    return dict(row) if row else {}


async def fetch_time_series(
    db: AsyncSession,
    user_id: int,
    chatbot_key_id: Optional[int] = None,
    since: Optional[datetime] = None,
    bucket: str = "day",
) -> List[Dict[str, Any]]:
    """
    One row per time bucket with traffic, failures and speed.

    ``bucket`` is a ``date_trunc`` unit ("hour" or "day") chosen by the service
    from the selected period — never taken from user input, since it goes into
    the SQL function call itself.

    Truncation happens after an explicit conversion to UTC. ``date_trunc`` on a
    ``timestamptz`` otherwise cuts days at midnight in the *database session's*
    time zone, which would not line up with the UTC buckets the service builds
    to fill the quiet gaps — and mismatched keys silently render as an empty
    chart.
    """
    day = func.date_trunc(bucket, func.timezone("UTC", ChatbotMessage.created_at)).label("bucket")

    query = _scoped(
        [
            day,
            func.count(ChatbotMessage.id).label("messages"),
            func.coalesce(func.sum(_error_flag()), 0).label("errors"),
            func.avg(cast(ChatbotMessage.response_time_ms, Float)).label("avg_ms"),
            func.coalesce(func.sum(ChatbotMessage.total_tokens), 0).label("total_tokens"),
        ],
        user_id,
        chatbot_key_id,
        since,
    ).group_by(day).order_by(day)

    return [dict(row) for row in (await db.execute(query)).mappings().all()]


async def fetch_per_chatbot(
    db: AsyncSession,
    user_id: int,
    chatbot_key_id: Optional[int] = None,
    since: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """Per-agent breakdown, busiest first."""
    query = (
        _scoped(
            [
                ChatbotApiKey.uuid.label("chatbot_uuid"),
                ChatbotApiKey.name.label("chatbot_name"),
                ChatbotApiKey.is_active.label("is_active"),
                func.count(ChatbotMessage.id).label("messages"),
                func.coalesce(func.sum(_error_flag()), 0).label("errors"),
                func.avg(cast(ChatbotMessage.response_time_ms, Float)).label("avg_ms"),
                _percentile_ms().label("p95_ms"),
                func.coalesce(func.sum(ChatbotMessage.request_tokens), 0).label("request_tokens"),
                func.coalesce(func.sum(ChatbotMessage.response_tokens), 0).label("response_tokens"),
                func.coalesce(func.sum(ChatbotMessage.total_tokens), 0).label("total_tokens"),
                func.max(ChatbotMessage.created_at).label("last_message_at"),
            ],
            user_id,
            chatbot_key_id,
            since,
        )
        .group_by(ChatbotApiKey.uuid, ChatbotApiKey.name, ChatbotApiKey.is_active)
        .order_by(desc("messages"))
    )
    return [dict(row) for row in (await db.execute(query)).mappings().all()]


async def fetch_model_usage(
    db: AsyncSession,
    user_id: int,
    chatbot_key_id: Optional[int] = None,
    since: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """
    Token spend per provider/model.

    Turns that never reached a model (a flow answering from its own script)
    are excluded rather than grouped under a blank name — they belong in the
    turn-type split, not in a model cost table.
    """
    query = (
        _scoped(
            [
                ChatbotMessage.llm_provider.label("provider"),
                ChatbotMessage.llm_model.label("model"),
                func.count(ChatbotMessage.id).label("messages"),
                func.avg(cast(ChatbotMessage.response_time_ms, Float)).label("avg_ms"),
                func.coalesce(func.sum(ChatbotMessage.request_tokens), 0).label("request_tokens"),
                func.coalesce(func.sum(ChatbotMessage.response_tokens), 0).label("response_tokens"),
                func.coalesce(func.sum(ChatbotMessage.total_tokens), 0).label("total_tokens"),
            ],
            user_id,
            chatbot_key_id,
            since,
        )
        .where(ChatbotMessage.llm_provider.is_not(None))
        .group_by(ChatbotMessage.llm_provider, ChatbotMessage.llm_model)
        .order_by(desc("total_tokens"))
    )
    return [dict(row) for row in (await db.execute(query)).mappings().all()]


async def fetch_turn_type_split(
    db: AsyncSession,
    user_id: int,
    chatbot_key_id: Optional[int] = None,
    since: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """How many turns a flow handled by itself versus how many cost a model call."""
    query = (
        _scoped(
            [
                ChatbotMessage.turn_type.label("turn_type"),
                func.count(ChatbotMessage.id).label("messages"),
                func.avg(cast(ChatbotMessage.response_time_ms, Float)).label("avg_ms"),
                func.coalesce(func.sum(ChatbotMessage.total_tokens), 0).label("total_tokens"),
            ],
            user_id,
            chatbot_key_id,
            since,
        )
        .group_by(ChatbotMessage.turn_type)
        .order_by(desc("messages"))
    )
    return [dict(row) for row in (await db.execute(query)).mappings().all()]


async def fetch_slowest_turns(
    db: AsyncSession,
    user_id: int,
    chatbot_key_id: Optional[int] = None,
    since: Optional[datetime] = None,
    limit: int = 10,
) -> List[Dict[str, Any]]:
    """The individual turns that took longest — where to start when tuning."""
    query = _scoped(
        [
            ChatbotApiKey.name.label("chatbot_name"),
            ChatbotMessage.visitor_message.label("visitor_message"),
            ChatbotMessage.response_time_ms.label("response_time_ms"),
            ChatbotMessage.total_tokens.label("total_tokens"),
            ChatbotMessage.llm_model.label("model"),
            ChatbotMessage.status.label("status"),
            ChatbotMessage.created_at.label("created_at"),
        ],
        user_id,
        chatbot_key_id,
        since,
    ).order_by(ChatbotMessage.response_time_ms.desc()).limit(limit)

    return [dict(row) for row in (await db.execute(query)).mappings().all()]


async def fetch_recent_failures(
    db: AsyncSession,
    user_id: int,
    chatbot_key_id: Optional[int] = None,
    since: Optional[datetime] = None,
    limit: int = 10,
) -> List[Dict[str, Any]]:
    """Most recent failed turns, with the message the visitor was shown."""
    query = (
        _scoped(
            [
                ChatbotApiKey.name.label("chatbot_name"),
                ChatbotMessage.visitor_message.label("visitor_message"),
                ChatbotMessage.error_message.label("error_message"),
                ChatbotMessage.response_time_ms.label("response_time_ms"),
                ChatbotMessage.created_at.label("created_at"),
            ],
            user_id,
            chatbot_key_id,
            since,
        )
        .where(ChatbotMessage.status == "error")
        .order_by(ChatbotMessage.created_at.desc())
        .limit(limit)
    )
    return [dict(row) for row in (await db.execute(query)).mappings().all()]
