"""
Business logic for the Chatbot Analytics dashboard — the performance view over
every turn the user's agents have answered.

Reads the per-turn log written by chatbot_turn_service (duration, token cost,
provider/model, success or failure) and turns it into the finished figures a
page renders: headline tiles, a time series with its empty buckets filled in,
per-agent and per-model breakdowns, and the slowest/failed turns worth looking
at first.

All the shaping happens here rather than in the template: bar heights,
percentages, human-readable durations and token counts are computed once, so
the page stays presentation-only (see CLAUDE.md — templates hold UI logic, not
business logic).
"""

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from litestar.exceptions import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.chatbot_analytics import queries
from app.db.db_utils import CRUDQueryBuilder
from app.models.chatbot import TURN_TYPE_FLOW, ChatbotApiKey

chatbot_key_crud = CRUDQueryBuilder(ChatbotApiKey)

# Selectable ranges, as {value: (label, days)}. The value is what the form
# submits; anything not in here is rejected rather than coerced, so the range
# shown always matches the range queried.
PERIOD_OPTIONS: Dict[str, Dict[str, Any]] = {
    "24h": {"label": "Last 24 hours", "days": 1},
    "7d": {"label": "Last 7 days", "days": 7},
    "30d": {"label": "Last 30 days", "days": 30},
    "90d": {"label": "Last 90 days", "days": 90},
}
DEFAULT_PERIOD = "7d"

# Below this many days the daily chart would be a single bar, so the series is
# bucketed by hour instead.
_HOURLY_THRESHOLD_DAYS = 2

# How many rows the "slowest" and "failures" tables show. Both are diagnostic
# starting points, not exhaustive logs — the full history stays on each agent's
# own conversation view.
_DETAIL_ROW_LIMIT = 10

# Most x-axis ticks that stay legible across the chart's width.
_MAX_AXIS_LABELS = 12

_TURN_TYPE_LABELS = {"ai": "Answered by AI", TURN_TYPE_FLOW: "Answered by flow"}

_PROVIDER_LABELS = {
    "anthropic": "Anthropic (Claude)",
    "openai-compatible": "OpenAI-compatible",
    "in_built": "In-built local model",
}


# --------------------------------------------------------------------------
# Formatting — one definition each, shared by every table and tile
# --------------------------------------------------------------------------

def format_duration(milliseconds: Optional[float]) -> str:
    """'840 ms' / '2.4 s' — whichever reads faster at that magnitude."""
    if not milliseconds:
        return "—"
    if milliseconds < 1000:
        return f"{round(milliseconds)} ms"
    return f"{milliseconds / 1000:.1f} s"


def format_count(value: Optional[int]) -> str:
    """Thousands separated; large token totals abbreviated to keep tiles readable."""
    value = int(value or 0)
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 10_000:
        return f"{value / 1000:.1f}k"
    return f"{value:,}"


def _percentage(part: int, whole: int) -> float:
    return round((part / whole) * 100, 1) if whole else 0.0


def _bar_height(value: float, peak: float) -> float:
    """
    A bar's height as a percentage of the chart area.

    Non-zero values get a 4% floor so a genuinely small bar is still visible
    next to a tall one — a bar that renders at zero pixels is indistinguishable
    from a bucket with no traffic at all, which is a different fact.
    """
    if not peak or value <= 0:
        return 0.0
    return max(round((value / peak) * 100, 1), 4.0)


# --------------------------------------------------------------------------
# Filters
# --------------------------------------------------------------------------

def resolve_period(period: str) -> Dict[str, Any]:
    """Validate the selected range and return its label/days/start time."""
    key = (period or DEFAULT_PERIOD).strip()
    if key not in PERIOD_OPTIONS:
        raise HTTPException(
            status_code=400,
            detail="That time range is not available. Choose one of the listed ranges.",
        )

    option = PERIOD_OPTIONS[key]
    days = option["days"]
    return {
        "key": key,
        "label": option["label"],
        "days": days,
        "since": datetime.now(timezone.utc) - timedelta(days=days),
        "bucket": "hour" if days < _HOURLY_THRESHOLD_DAYS else "day",
    }


async def _resolve_chatbot(
    db: AsyncSession, user_id: int, chatbot_uuid: Optional[uuid.UUID]
) -> Optional[ChatbotApiKey]:
    """Ownership check for a single-agent filter; None means "all agents"."""
    if chatbot_uuid is None:
        return None

    chatbot_key = await chatbot_key_crud.get_by_uuid(
        db, chatbot_uuid, extra_filters={"user_id": user_id}
    )
    if not chatbot_key:
        raise HTTPException(status_code=404, detail="Chatbot not found")
    return chatbot_key


# --------------------------------------------------------------------------
# View building
# --------------------------------------------------------------------------

def _build_totals(totals: Dict[str, Any]) -> Dict[str, Any]:
    messages = int(totals.get("messages") or 0)
    errors = int(totals.get("errors") or 0)
    successes = messages - errors
    total_tokens = int(totals.get("total_tokens") or 0)

    return {
        "messages": messages,
        "messages_display": format_count(messages),
        "errors": errors,
        "successes": successes,
        "success_rate": _percentage(successes, messages),
        "error_rate": _percentage(errors, messages),
        "avg_ms": totals.get("avg_ms") or 0,
        "avg_display": format_duration(totals.get("avg_ms")),
        "p95_display": format_duration(totals.get("p95_ms")),
        "max_display": format_duration(totals.get("max_ms")),
        "request_tokens": int(totals.get("request_tokens") or 0),
        "request_tokens_display": format_count(totals.get("request_tokens")),
        "response_tokens": int(totals.get("response_tokens") or 0),
        "response_tokens_display": format_count(totals.get("response_tokens")),
        "total_tokens": total_tokens,
        "total_tokens_display": format_count(total_tokens),
        "llm_calls": int(totals.get("llm_calls") or 0),
        "avg_tokens_display": format_count(round(total_tokens / messages) if messages else 0),
        # True when at least one turn's tokens came from a length estimate
        # rather than a provider's own report — shown as a caveat on the page.
        "has_estimates": int(totals.get("estimated_rows") or 0) > 0,
    }


def _fill_buckets(rows: List[Dict[str, Any]], since: datetime, bucket: str) -> List[Dict[str, Any]]:
    """
    Return one entry per bucket from `since` to now, including the quiet ones.

    Without this a chart silently closes the gaps and a day with no traffic
    looks like a day that was never in the range.
    """
    step = timedelta(hours=1) if bucket == "hour" else timedelta(days=1)
    by_start = {_bucket_key(row["bucket"], bucket): row for row in rows if row.get("bucket")}

    start = _truncate(since, bucket)
    now = _truncate(datetime.now(timezone.utc), bucket)

    filled: List[Dict[str, Any]] = []
    cursor = start
    while cursor <= now:
        row = by_start.get(_bucket_key(cursor, bucket)) or {}
        filled.append(
            {
                "start": cursor,
                "label": cursor.strftime("%H:%M" if bucket == "hour" else "%d %b"),
                "messages": int(row.get("messages") or 0),
                "errors": int(row.get("errors") or 0),
                "avg_ms": float(row.get("avg_ms") or 0),
                "total_tokens": int(row.get("total_tokens") or 0),
            }
        )
        cursor += step

    return filled


def _truncate(value: datetime, bucket: str) -> datetime:
    value = value.astimezone(timezone.utc)
    if bucket == "hour":
        return value.replace(minute=0, second=0, microsecond=0)
    return value.replace(hour=0, minute=0, second=0, microsecond=0)


def _bucket_key(value: datetime, bucket: str) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return _truncate(value, bucket).isoformat()


def _build_series(rows: List[Dict[str, Any]], since: datetime, bucket: str) -> Dict[str, Any]:
    """Chart-ready buckets: raw values plus the bar heights the page draws."""
    buckets = _fill_buckets(rows, since, bucket)

    peak_messages = max((b["messages"] for b in buckets), default=0)
    peak_ms = max((b["avg_ms"] for b in buckets), default=0.0)

    # A 90-day range has far more buckets than an axis can label legibly, so
    # only every nth tick gets text — the bars themselves stay one per bucket.
    label_every = max(1, len(buckets) // _MAX_AXIS_LABELS)

    for index, entry in enumerate(buckets):
        entry["messages_height"] = _bar_height(entry["messages"], peak_messages)
        entry["errors_height"] = _bar_height(entry["errors"], peak_messages)
        entry["avg_height"] = _bar_height(entry["avg_ms"], peak_ms)
        entry["avg_display"] = format_duration(entry["avg_ms"])
        entry["show_label"] = index % label_every == 0

    return {
        "buckets": buckets,
        "peak_messages": peak_messages,
        "peak_avg_display": format_duration(peak_ms),
        "granularity": "hour" if bucket == "hour" else "day",
        "has_data": peak_messages > 0,
    }


def _build_chatbot_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    views = []
    for row in rows:
        messages = int(row.get("messages") or 0)
        errors = int(row.get("errors") or 0)
        views.append(
            {
                "uuid": row.get("chatbot_uuid"),
                "name": row.get("chatbot_name"),
                "is_active": row.get("is_active"),
                "messages": messages,
                "errors": errors,
                "success_rate": _percentage(messages - errors, messages),
                "avg_display": format_duration(row.get("avg_ms")),
                "p95_display": format_duration(row.get("p95_ms")),
                "request_tokens_display": format_count(row.get("request_tokens")),
                "response_tokens_display": format_count(row.get("response_tokens")),
                "total_tokens_display": format_count(row.get("total_tokens")),
                "last_message_at": row.get("last_message_at"),
            }
        )
    return views


def _build_model_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {
            "provider": _PROVIDER_LABELS.get(row.get("provider"), row.get("provider") or "—"),
            "model": row.get("model") or "—",
            "messages": int(row.get("messages") or 0),
            "avg_display": format_duration(row.get("avg_ms")),
            "request_tokens_display": format_count(row.get("request_tokens")),
            "response_tokens_display": format_count(row.get("response_tokens")),
            "total_tokens_display": format_count(row.get("total_tokens")),
        }
        for row in rows
    ]


def _build_turn_types(rows: List[Dict[str, Any]], total_messages: int) -> List[Dict[str, Any]]:
    return [
        {
            "label": _TURN_TYPE_LABELS.get(row.get("turn_type"), row.get("turn_type") or "—"),
            "messages": int(row.get("messages") or 0),
            "share": _percentage(int(row.get("messages") or 0), total_messages),
            "avg_display": format_duration(row.get("avg_ms")),
            "total_tokens_display": format_count(row.get("total_tokens")),
        }
        for row in rows
    ]


def _build_detail_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {
            **row,
            "duration_display": format_duration(row.get("response_time_ms")),
            "total_tokens_display": format_count(row.get("total_tokens")),
        }
        for row in rows
    ]


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------

async def build_dashboard(
    db: AsyncSession,
    user_id: int,
    period: str = DEFAULT_PERIOD,
    chatbot_uuid: Optional[uuid.UUID] = None,
) -> Dict[str, Any]:
    """
    Everything the Chatbot Analytics page shows, for one user, one time range
    and either one agent or all of them.

    Returns structured data only — no HTML — so the same figures can back a
    JSON endpoint later without touching this function.
    """
    window = resolve_period(period)
    chatbot_key = await _resolve_chatbot(db, user_id, chatbot_uuid)
    key_id = chatbot_key.id if chatbot_key else None
    since = window["since"]

    totals = await queries.fetch_totals(db, user_id, key_id, since)
    series_rows = await queries.fetch_time_series(db, user_id, key_id, since, window["bucket"])
    per_chatbot = await queries.fetch_per_chatbot(db, user_id, key_id, since)
    model_usage = await queries.fetch_model_usage(db, user_id, key_id, since)
    turn_types = await queries.fetch_turn_type_split(db, user_id, key_id, since)
    slowest = await queries.fetch_slowest_turns(db, user_id, key_id, since, _DETAIL_ROW_LIMIT)
    failures = await queries.fetch_recent_failures(db, user_id, key_id, since, _DETAIL_ROW_LIMIT)

    built_totals = _build_totals(totals)

    return {
        "totals": built_totals,
        "series": _build_series(series_rows, since, window["bucket"]),
        "chatbot_rows": _build_chatbot_rows(per_chatbot),
        "model_rows": _build_model_rows(model_usage),
        "turn_types": _build_turn_types(turn_types, built_totals["messages"]),
        "slowest": _build_detail_rows(slowest),
        "failures": _build_detail_rows(failures),
        "period": window["key"],
        "period_label": window["label"],
        "period_options": PERIOD_OPTIONS,
        "selected_chatbot_uuid": str(chatbot_uuid) if chatbot_uuid else "",
        "selected_chatbot_name": chatbot_key.name if chatbot_key else "All agents",
    }
