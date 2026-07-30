"""
ai_inbuilt-specific data access that doesn't fit the generic CRUDQueryBuilder:
batched chunk inserts/deletes and pgvector similarity ordering. CRUDQueryBuilder's
get_many only orders by a plain column-name string, so a vector-distance
ORDER BY is structurally out of reach for it — this file exists so that raw
SQLAlchemy Core construction lives in the module's own db subpackage instead
of leaking into the service layer, matching app/db/flow_builder/queries.py's
precedent.
"""

from typing import List, Set, Tuple

from sqlalchemy import delete, distinct, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai_inbuilt import KnowledgeChunk


async def insert_chunks(db: AsyncSession, rows: List[dict]) -> None:
    """Batched multi-row insert — avoids one round-trip/commit per chunk."""
    if not rows:
        return
    await db.execute(insert(KnowledgeChunk), rows)


async def delete_chunks_for_document(db: AsyncSession, document_id: int) -> int:
    result = await db.execute(
        delete(KnowledgeChunk).where(KnowledgeChunk.document_id == document_id)
    )
    return result.rowcount or 0


async def documents_with_current_chunks(
    db: AsyncSession,
    knowledge_base_id: int,
    embed_model: str,
) -> Set[int]:
    """document_ids that already have chunks embedded under embed_model — drives
    train_knowledge_base's re-embed staleness check (skip already-current docs,
    re-embed everything if the configured embed model has changed)."""
    result = await db.execute(
        select(distinct(KnowledgeChunk.document_id)).where(
            KnowledgeChunk.knowledge_base_id == knowledge_base_id,
            KnowledgeChunk.embed_model == embed_model,
        )
    )
    return set(result.scalars().all())


async def search_similar_chunks(
    db: AsyncSession,
    knowledge_base_id: int,
    query_embedding: List[float],
    limit: int,
) -> List[Tuple[str, float]]:
    """Top `limit` chunks for this knowledge base, nearest first (cosine distance)."""
    distance = KnowledgeChunk.embedding.cosine_distance(query_embedding).label("distance")
    result = await db.execute(
        select(KnowledgeChunk.content, distance)
        .where(KnowledgeChunk.knowledge_base_id == knowledge_base_id)
        .order_by(distance)
        .limit(limit)
    )
    return [(row.content, row.distance) for row in result.all()]
