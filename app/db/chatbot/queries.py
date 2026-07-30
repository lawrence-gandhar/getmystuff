"""
Chatbot-specific data access that doesn't fit a single CRUDQueryBuilder call —
the AI-settings get-or-create, and every query that has to join actions to the
chatbots they are attached to.

``get_or_create_ai_settings`` lives here rather than in a service because two
services need it — chatbot_service (which creates the row the moment a chatbot
is created) and chatbot_ai_settings_service (which reads/updates it) — and a
service-to-service import between those two would be circular.
"""

from typing import Dict, List

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.db_utils import CRUDQueryBuilder
from app.models.chatbot import (
    ChatbotAction,
    ChatbotActionLink,
    ChatbotAiSettings,
    ChatbotApiKey,
)

ai_settings_crud = CRUDQueryBuilder(ChatbotAiSettings)


async def get_or_create_ai_settings(db: AsyncSession, chatbot_key_id: int) -> ChatbotAiSettings:
    """
    Return this chatbot's AI settings row, creating it with the default agent
    name / system prompt / variables if it doesn't exist yet.

    New chatbots get their row up front (see chatbot_service.create_chatbot_key),
    so the create branch only ever fires for chatbots that predate this feature.
    """
    existing = await ai_settings_crud.get_one(db, filters={"chatbot_key_id": chatbot_key_id})
    if existing:
        return existing

    return await ai_settings_crud.create(db, {"chatbot_key_id": chatbot_key_id})


# --------------------------------------------------------------------------
# Action attachment — actions are user-owned and shared, so every
# chatbot-scoped question about them is a join through chatbot_action_links.
# --------------------------------------------------------------------------

async def fetch_actions_for_key(
    db: AsyncSession, chatbot_key_id: int, active_only: bool = False
) -> List[ChatbotAction]:
    """
    Actions attached to one chatbot, in name order.

    ``active_only`` is what the answer path uses: an action deactivated in the
    library stops running everywhere it is attached, without being detached.
    """
    query = (
        select(ChatbotAction)
        .join(ChatbotActionLink, ChatbotActionLink.action_id == ChatbotAction.id)
        .where(ChatbotActionLink.chatbot_key_id == chatbot_key_id)
        .order_by(ChatbotAction.name)
    )
    if active_only:
        query = query.where(ChatbotAction.is_active.is_(True))

    result = await db.execute(query)
    return list(result.scalars().all())


async def fetch_attachable_actions(
    db: AsyncSession, user_id: int, chatbot_key_id: int
) -> List[ChatbotAction]:
    """The user's active actions that aren't attached to this chatbot yet."""
    attached = (
        select(ChatbotActionLink.action_id)
        .where(ChatbotActionLink.chatbot_key_id == chatbot_key_id)
        .scalar_subquery()
    )
    result = await db.execute(
        select(ChatbotAction)
        .where(
            ChatbotAction.user_id == user_id,
            ChatbotAction.is_active.is_(True),
            ChatbotAction.id.not_in(attached),
        )
        .order_by(ChatbotAction.name)
    )
    return list(result.scalars().all())


async def count_action_attachments(db: AsyncSession, user_id: int) -> Dict[int, int]:
    """
    ``{action_id: number of chatbots attached}`` for one user — one grouped query
    rather than a per-row count, since the library page shows this for every action.
    """
    result = await db.execute(
        select(ChatbotActionLink.action_id, func.count(ChatbotActionLink.id))
        .join(ChatbotAction, ChatbotAction.id == ChatbotActionLink.action_id)
        .where(ChatbotAction.user_id == user_id)
        .group_by(ChatbotActionLink.action_id)
    )
    return dict(result.all())


async def fetch_action_attachment_names(db: AsyncSession, action_id: int) -> List[str]:
    """
    Names of the chatbots one action is attached to — used to warn the owner
    before they edit or delete something several chatbots depend on.
    """
    result = await db.execute(
        select(ChatbotApiKey.name)
        .join(ChatbotActionLink, ChatbotActionLink.chatbot_key_id == ChatbotApiKey.id)
        .where(ChatbotActionLink.action_id == action_id)
        .order_by(ChatbotApiKey.name)
    )
    return list(result.scalars().all())
