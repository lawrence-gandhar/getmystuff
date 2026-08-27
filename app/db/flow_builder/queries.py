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


async def fetch_attached_chatbot_name(db: AsyncSession, flow_id: int) -> Optional[str]:
    """
    The name of the chatbot one flow is attached to, or ``None``.

    The single-row form of the join above, and it exists for one caller:
    ``flow_service.set_flow_kind`` refuses to make an attached flow generic and names the
    agent to detach it from, because "detach it first" is not useful advice without saying
    from what. Keyed on the internal id — the service has the row in hand already.
    """
    result = await db.execute(
        select(ChatbotApiKey.name)
        .join(ChatbotFlow, ChatbotFlow.chatbot_key_id == ChatbotApiKey.id)
        .where(ChatbotFlow.id == flow_id)
    )
    return result.scalar_one_or_none()
