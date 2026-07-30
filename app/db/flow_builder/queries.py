"""
Flow-builder-specific data access that doesn't fit the generic
CRUDQueryBuilder (a join across two tables in one statement).
Everything else in the Flow Builder module goes through CRUDQueryBuilder
like the rest of the app — this file exists so that one raw-SQL exception
lives in the module's own db subpackage instead of leaking into the
service layer or polluting the shared, model-agnostic app/db/db_utils.py.
"""

from typing import List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chatbot import ChatbotApiKey
from app.models.flow_builder import ChatbotFlow


async def fetch_flows_with_chatbot_names(
    db: AsyncSession, user_id: int
) -> List[Tuple[ChatbotFlow, Optional[str]]]:
    """
    Every flow this user owns, paired with the name of the chatbot it is
    attached to (``None`` when unattached) — the Flow Builder list page shows
    that column, and an outer join beats one extra query per row.
    """
    result = await db.execute(
        select(ChatbotFlow, ChatbotApiKey.name)
        .outerjoin(ChatbotApiKey, ChatbotApiKey.id == ChatbotFlow.chatbot_key_id)
        .where(ChatbotFlow.user_id == user_id)
        .order_by(ChatbotFlow.created_at.desc())
    )
    return [(flow, chatbot_name) for flow, chatbot_name in result.all()]
