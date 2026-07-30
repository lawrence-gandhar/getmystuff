import uuid as uuid_pkg
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db.base import Base

# nomic-embed-text produces 768-dimensional vectors. Changing OLLAMA_EMBED_MODEL
# to a model with a different output size requires a new migration to alter
# this column's dimension (and a full re-embed of every existing chunk).
EMBEDDING_DIMENSIONS = 768


class KnowledgeChunk(Base):
    """
    One embedded text chunk belonging to a Flow Builder AI Fallback node's
    knowledge base document (app.models.flow_builder.FlowNodeKnowledgeDocument).

    Lives in its own ai_inbuilt module (rather than flow_builder) because this
    table is purely a vector-storage concern owned by the in-built local LLM
    integration — the FK below references flow_node_knowledge_documents /
    flow_node_knowledge_bases by table name only (resolved lazily by
    SQLAlchemy via Base.metadata), so this module has no Python import
    dependency on app.models.flow_builder.

    knowledge_base_id is denormalized alongside document_id (rather than
    requiring a join) so the hot retrieval path — filter by knowledge base,
    order by vector distance — can use the HNSW index directly. This is safe
    because a document's owning knowledge base is set once at creation and
    never changes, and both FKs cascade from the same delete.
    """
    __tablename__ = "ai_inbuilt_knowledge_chunks"

    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=True,
    )

    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True),
        default=uuid_pkg.uuid4,
        unique=True,
        index=True,
        nullable=False,
    )

    document_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("flow_node_knowledge_documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    knowledge_base_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("flow_node_knowledge_bases.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Ordinal position of this chunk within its document.
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)

    content: Mapped[str] = mapped_column(Text, nullable=False)

    embedding: Mapped[list] = mapped_column(Vector(EMBEDDING_DIMENSIONS), nullable=False)

    # Which embedding model produced this vector (e.g. "nomic-embed-text").
    # Lets a future embed-model change be detected as staleness rather than
    # silently mixing incompatible vectors in similarity search.
    embed_model: Mapped[str] = mapped_column(String(100), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        index=True,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        Index("ux_ai_inbuilt_chunks_doc_index", "document_id", "chunk_index", unique=True),
        Index("ix_ai_inbuilt_chunks_kb_model", "knowledge_base_id", "embed_model"),
        Index(
            "ix_ai_inbuilt_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )
