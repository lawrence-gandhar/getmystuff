"""
Business logic for one AI Fallback node's knowledge base: uploading
supporting documents (pdf/txt/docx) or typed text, "training" (extracting
plain text from uploads, then chunking + embedding it via the in-built local
Ollama model and storing the vectors in pgvector), and retrieving grounding
context for a live chatbot answer via vector similarity search.
"""

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import docx
from litestar.exceptions import HTTPException
from pypdf import PdfReader
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.db_utils import CRUDQueryBuilder
from app.models.flow_builder import FlowNodeKnowledgeBase, FlowNodeKnowledgeDocument
from app.services.ai_inbuilt import embedding_service
from app.services.flow_builder import flow_service
from app.utils.file_utils import (
    ALLOWED_KB_EXTENSIONS,
    MAX_KB_FILE_SIZE_BYTES,
    ensure_knowledge_base_upload_dir,
    normalize_filename,
)

logger = logging.getLogger(__name__)

kb_crud = CRUDQueryBuilder(FlowNodeKnowledgeBase)
kb_document_crud = CRUDQueryBuilder(FlowNodeKnowledgeDocument)

_MAX_EXTRACTED_CHARS = 200_000
_MAX_MANUAL_TEXT_CHARS = 50_000
# Kept well under ai_fallback_service._MAX_KB_CONTEXT_CHARS (the cap on this text
# plus live pipeline/tool config text combined) so the composed prompt has room
# left for those other blocks without needing OLLAMA_NUM_CTX raised further.
_MAX_CONTEXT_CHARS = 4000
_MAX_CONTEXT_CHUNKS = 5

_MIME_TYPES = {
    "pdf": "application/pdf",
    "txt": "text/plain",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


# --------------------------------------------------------------------------
# Ownership / lookup — builder-side (owned by the authenticated user)
# --------------------------------------------------------------------------

async def get_or_create_knowledge_base(
    db: AsyncSession,
    user_id: int,
    flow_id: uuid.UUID,
    node_id: str,
) -> FlowNodeKnowledgeBase:
    flow = await flow_service.get_flow(db, user_id, flow_id)  # ownership check
    kb = await kb_crud.get_one(db, filters={"flow_id": flow.id, "node_id": node_id})
    if kb:
        return kb
    return await kb_crud.create(db, {"flow_id": flow.id, "node_id": node_id, "status": "untrained"})


def _document_summary(doc: FlowNodeKnowledgeDocument) -> dict:
    return {
        "id": str(doc.uuid),
        "source_type": doc.source_type,
        "label": doc.original_filename or doc.label or "Typed text",
        "size_bytes": doc.size_bytes,
        "extraction_status": doc.extraction_status,
        "error_message": doc.error_message,
        "created_at": doc.created_at.isoformat() if doc.created_at else None,
    }


async def get_knowledge_base_state(
    db: AsyncSession,
    user_id: int,
    flow_id: uuid.UUID,
    node_id: str,
) -> dict:
    kb = await get_or_create_knowledge_base(db, user_id, flow_id, node_id)
    documents = await kb_document_crud.get_many(
        db, filters={"knowledge_base_id": kb.id}, order_by="created_at",
    )
    return {
        "status": kb.status,
        "trained_at": kb.trained_at.isoformat() if kb.trained_at else None,
        "error_message": kb.error_message,
        "documents": [_document_summary(d) for d in documents],
    }


# --------------------------------------------------------------------------
# Write — upload / manual text / delete
# --------------------------------------------------------------------------

def _validate_upload(filename: str, content: bytes) -> str:
    if not filename:
        raise HTTPException(status_code=400, detail="A file name is required")
    normalized = normalize_filename(filename)
    ext = Path(normalized).suffix.lstrip(".")
    if ext not in ALLOWED_KB_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '.{ext}'. Allowed: {', '.join(sorted(ALLOWED_KB_EXTENSIONS))}",
        )
    if not content:
        raise HTTPException(status_code=400, detail=f"'{filename}' is empty")
    if len(content) > MAX_KB_FILE_SIZE_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"'{filename}' exceeds the {MAX_KB_FILE_SIZE_BYTES // (1024 * 1024)} MB limit",
        )
    return normalized


async def upload_documents(
    db: AsyncSession,
    user_id: int,
    flow_id: uuid.UUID,
    node_id: str,
    file_payloads: List[dict],
) -> List[dict]:
    """
    Save uploaded knowledge-base files to disk and record them, pending
    text extraction by train_knowledge_base. `file_payloads` is
    `[{"filename": str, "content": bytes}, ...]`, matching file_service's
    framework-agnostic convention.
    """
    kb = await get_or_create_knowledge_base(db, user_id, flow_id, node_id)
    upload_dir = ensure_knowledge_base_upload_dir(str(kb.uuid))

    results: List[dict] = []
    any_ok = False

    for payload in file_payloads:
        original_name: str = payload["filename"]
        content: bytes = payload["content"]

        try:
            normalized = _validate_upload(original_name, content)
        except HTTPException as exc:
            results.append({"original_filename": original_name, "status": "error", "message": str(exc.detail)})
            continue

        stored_name = f"{uuid.uuid4().hex}_{normalized}"
        file_path = upload_dir / stored_name
        await asyncio.to_thread(file_path.write_bytes, content)

        ext = Path(normalized).suffix.lstrip(".")
        doc = await kb_document_crud.create(db, {
            "knowledge_base_id": kb.id,
            "source_type": "upload",
            "original_filename": original_name,
            "stored_filename": stored_name,
            "file_path": str(file_path),
            "mime_type": _MIME_TYPES.get(ext),
            "size_bytes": len(content),
            "extraction_status": "pending",
        })
        any_ok = True
        results.append({"original_filename": original_name, "status": "ok", "document_id": str(doc.uuid)})

    if any_ok:
        await kb_crud.update(db, kb.id, {"status": "untrained"})

    return results


async def add_manual_text(
    db: AsyncSession,
    user_id: int,
    flow_id: uuid.UUID,
    node_id: str,
    label: str,
    text: str,
) -> dict:
    text = (text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Text is required")
    if len(text) > _MAX_MANUAL_TEXT_CHARS:
        raise HTTPException(
            status_code=400,
            detail=f"Text must not exceed {_MAX_MANUAL_TEXT_CHARS:,} characters",
        )

    kb = await get_or_create_knowledge_base(db, user_id, flow_id, node_id)
    doc = await kb_document_crud.create(db, {
        "knowledge_base_id": kb.id,
        "source_type": "manual",
        "label": (label or "").strip() or "Typed text",
        "content": text,
        "extraction_status": "extracted",
    })
    await kb_crud.update(db, kb.id, {"status": "untrained"})
    return _document_summary(doc)


async def delete_document(
    db: AsyncSession,
    user_id: int,
    flow_id: uuid.UUID,
    node_id: str,
    document_id: uuid.UUID,
) -> None:
    kb = await get_or_create_knowledge_base(db, user_id, flow_id, node_id)
    doc = await kb_document_crud.get_by_uuid(db, document_id, extra_filters={"knowledge_base_id": kb.id})
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    if doc.file_path:
        await asyncio.to_thread(lambda: Path(doc.file_path).unlink(missing_ok=True))

    await kb_document_crud.delete(db, doc.id)
    await kb_crud.update(db, kb.id, {"status": "untrained"})


# --------------------------------------------------------------------------
# Training — extract text from any not-yet-extracted uploads
# --------------------------------------------------------------------------

def _extract_pdf(path: Path) -> str:
    reader = PdfReader(str(path))
    return "\n\n".join((page.extract_text() or "") for page in reader.pages)


def _extract_docx(path: Path) -> str:
    document = docx.Document(str(path))
    return "\n\n".join(p.text for p in document.paragraphs if p.text.strip())


def _extract_txt(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


_EXTRACTORS = {"pdf": _extract_pdf, "docx": _extract_docx, "txt": _extract_txt}


async def _extract_pending_uploads(db: AsyncSession, knowledge_base_id: int) -> None:
    documents = await kb_document_crud.get_many(db, filters={"knowledge_base_id": knowledge_base_id})
    for doc in documents:
        if doc.source_type != "upload" or doc.extraction_status == "extracted":
            continue
        try:
            ext = Path(doc.stored_filename or "").suffix.lstrip(".")
            extractor = _EXTRACTORS.get(ext)
            if not extractor:
                raise ValueError(f"No text extractor available for '.{ext}' files")
            text = await asyncio.to_thread(extractor, Path(doc.file_path))
            text = text.strip()[:_MAX_EXTRACTED_CHARS]
            if not text:
                raise ValueError("No extractable text found in this file")
            await kb_document_crud.update(db, doc.id, {
                "content": text, "extraction_status": "extracted", "error_message": None,
            })
        except Exception as exc:
            await kb_document_crud.update(db, doc.id, {
                "extraction_status": "error", "error_message": str(exc)[:500],
            })


async def _embed_extracted_documents(db: AsyncSession, knowledge_base_id: int) -> int:
    """
    Chunk + embed every extracted document not already embedded under the
    currently configured embed model, and return how many documents now have
    vectors stored. Only stale/missing documents are (re-)embedded — a
    document's extracted content is immutable after creation (no edit
    endpoint), so re-embedding unchanged content would waste an Ollama
    round-trip; keying the skip on the embed model means an OLLAMA_EMBED_MODEL
    change automatically makes every document stale and re-embeds it on the
    next Train click.
    """
    documents = await kb_document_crud.get_many(
        db, filters={"knowledge_base_id": knowledge_base_id, "extraction_status": "extracted"},
    )
    pending_ids = [doc.id for doc in documents]
    to_embed = set(await embedding_service.get_documents_needing_embedding(db, knowledge_base_id, pending_ids))

    embedded_count = len(pending_ids) - len(to_embed)
    for doc in documents:
        if doc.id not in to_embed:
            continue
        try:
            await embedding_service.embed_document(db, doc.id, knowledge_base_id, doc.content or "")
            embedded_count += 1
        except HTTPException as exc:
            logger.warning("Embedding failed for knowledge document %s: %s", doc.id, exc.detail)
            await kb_document_crud.update(db, doc.id, {"error_message": str(exc.detail)[:500]})

    return embedded_count


async def train_knowledge_base(
    db: AsyncSession,
    user_id: int,
    flow_id: uuid.UUID,
    node_id: str,
) -> dict:
    kb = await get_or_create_knowledge_base(db, user_id, flow_id, node_id)
    documents = await kb_document_crud.get_many(db, filters={"knowledge_base_id": kb.id})
    if not documents:
        raise HTTPException(
            status_code=400,
            detail="Add at least one document or block of typed text before training",
        )

    await _extract_pending_uploads(db, kb.id)
    embedded_count = await _embed_extracted_documents(db, kb.id)

    # A knowledge base must never be marked "trained" while holding zero
    # vectors — that would silently return no grounding context at answer time.
    if embedded_count == 0:
        await kb_crud.update(db, kb.id, {
            "status": "failed",
            "error_message": "None of the documents could be trained — check each one's error below.",
            "trained_at": None,
        })
    else:
        await kb_crud.update(db, kb.id, {
            "status": "trained", "error_message": None, "trained_at": datetime.now(timezone.utc),
        })

    return await get_knowledge_base_state(db, user_id, flow_id, node_id)


# --------------------------------------------------------------------------
# Runtime retrieval — keyed on the internal flow_id (int), no ownership
# check, mirroring flow_service.get_active_flow's runtime-facing precedent.
# --------------------------------------------------------------------------

async def retrieve_context(db: AsyncSession, flow_id: int, node_id: str, query: str) -> Optional[str]:
    kb = await kb_crud.get_one(db, filters={"flow_id": flow_id, "node_id": node_id})
    if not kb or kb.status != "trained":
        return None

    try:
        chunks = await embedding_service.retrieve_similar_chunks(db, kb.id, query, _MAX_CONTEXT_CHUNKS)
    except HTTPException as exc:
        # The embedding service's error text (e.g. "Could not reach the local
        # Ollama server...") is correct for the operator-facing Train page but
        # must never reach a website visitor — log the real cause, surface a
        # generic one here.
        logger.exception("Knowledge base retrieval failed for flow %s node %s: %s", flow_id, node_id, exc.detail)
        raise HTTPException(
            status_code=503,
            detail="The assistant is temporarily unavailable. Please try again in a moment.",
        )

    if not chunks:
        return None

    return "\n\n---\n\n".join(chunks)[:_MAX_CONTEXT_CHARS]
