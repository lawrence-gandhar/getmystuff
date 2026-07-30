"""
Chunk + embed + store, and similarity retrieval, for the in-built local LLM's
knowledge base pipeline. Deliberately takes plain scalars (document_id,
knowledge_base_id, content) rather than ORM instances, so this module has no
import dependency on app.models.flow_builder — the dependency direction stays
one-way (flow_builder -> ai_inbuilt).
"""

from typing import List

from litestar.exceptions import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.ai_inbuilt import queries
from app.models.ai_inbuilt import EMBEDDING_DIMENSIONS
from app.services.ai_inbuilt import ollama_client
from app.services.ai_inbuilt.chunking import split_text

_MAX_CHUNKS_PER_DOCUMENT = 400


async def embed_document(
    db: AsyncSession,
    document_id: int,
    knowledge_base_id: int,
    content: str,
) -> int:
    """
    Chunk `content`, embed every chunk, and (re)store them for `document_id`.
    Delete-then-insert in one transaction makes re-embedding idempotent.
    Returns the number of chunks stored.
    """
    chunks = split_text(content)
    if not chunks:
        return 0

    if len(chunks) > _MAX_CHUNKS_PER_DOCUMENT:
        raise HTTPException(
            status_code=400,
            detail=(
                f"This document produced {len(chunks)} chunks, exceeding the "
                f"{_MAX_CHUNKS_PER_DOCUMENT} limit per document. Split it into "
                "smaller files and upload them separately."
            ),
        )

    vectors = await ollama_client.embed_texts(chunks, expected_dimensions=EMBEDDING_DIMENSIONS)

    await queries.delete_chunks_for_document(db, document_id)
    await queries.insert_chunks(db, [
        {
            "document_id": document_id,
            "knowledge_base_id": knowledge_base_id,
            "chunk_index": index,
            "content": chunk,
            "embedding": vector,
            "embed_model": ollama_client.OLLAMA_EMBED_MODEL,
        }
        for index, (chunk, vector) in enumerate(zip(chunks, vectors))
    ])
    await db.commit()

    return len(chunks)


async def remove_document_chunks(db: AsyncSession, document_id: int) -> int:
    removed = await queries.delete_chunks_for_document(db, document_id)
    await db.commit()
    return removed


async def get_documents_needing_embedding(
    db: AsyncSession,
    knowledge_base_id: int,
    document_ids: List[int],
) -> List[int]:
    """Of `document_ids`, which ones have no chunks under the currently
    configured embed model (i.e. need (re-)embedding on this Train run)."""
    current = await queries.documents_with_current_chunks(
        db, knowledge_base_id, ollama_client.OLLAMA_EMBED_MODEL,
    )
    return [doc_id for doc_id in document_ids if doc_id not in current]


async def retrieve_similar_chunks(
    db: AsyncSession,
    knowledge_base_id: int,
    query: str,
    limit: int,
) -> List[str]:
    """Embed `query` and return the `limit` most similar chunks' text, nearest first."""
    query_vector = await ollama_client.embed_text(query, expected_dimensions=EMBEDDING_DIMENSIONS)
    rows = await queries.search_similar_chunks(db, knowledge_base_id, query_vector, limit)
    return [content for content, _distance in rows]
