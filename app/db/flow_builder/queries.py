"""
Flow-builder-specific data access that doesn't fit the generic
CRUDQueryBuilder (a bulk UPDATE across many rows in one statement).
Everything else in the Flow Builder module goes through CRUDQueryBuilder
like the rest of the app — this file exists so that one raw-SQL exception
lives in the module's own db subpackage instead of leaking into the
service layer or polluting the shared, model-agnostic app/db/db_utils.py.
"""

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.flow_builder import ChatbotFlow


async def deactivate_other_flows(
    db: AsyncSession,
    chatbot_key_id: int,
    except_flow_id: int,
) -> None:
    """Clear is_active on every flow for this chatbot key except the given one."""
    await db.execute(
        update(ChatbotFlow)
        .where(
            ChatbotFlow.chatbot_key_id == chatbot_key_id,
            ChatbotFlow.id != except_flow_id,
        )
        .values(is_active=False)
    )
