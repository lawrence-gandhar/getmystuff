"""
Routes for one AI Fallback node's knowledge base (Flow Builder). Returns
JSON rather than HTML partials — matching FlowBuilderController's own
convention (see graph()/save()), since the Flow Builder canvas and its
properties panel are entirely client-rendered by flow_builder.js rather
than server-templated partials.
"""

import uuid

from litestar import Controller, get, post
from litestar.connection import Request
from litestar.exceptions import HTTPException
from litestar.response import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.auth import require_auth
from app.models.user import User
from app.services.flow_builder import knowledge_base_service

_JSON = "application/json"


def _error_response(exc: HTTPException) -> Response:
    return Response({"message": str(exc.detail)}, media_type=_JSON, status_code=exc.status_code)


class KnowledgeBaseController(Controller):
    path = "/flow-builder/{flow_id:uuid}/nodes/{node_id:str}/knowledge-base"
    dependencies = {"user": require_auth}

    # --------------------------
    # STATE — status + document list
    # --------------------------
    @get("/")
    async def state(
        self,
        flow_id: uuid.UUID,
        node_id: str,
        db: AsyncSession,
        user: User,
    ) -> Response:
        try:
            state = await knowledge_base_service.get_knowledge_base_state(db, user.id, flow_id, node_id)
        except HTTPException as e:
            return _error_response(e)
        return Response(state, media_type=_JSON, status_code=200)

    # --------------------------
    # UPLOAD — one or more files
    # --------------------------
    @post("/upload")
    async def upload(
        self,
        flow_id: uuid.UUID,
        node_id: str,
        request: Request,
        db: AsyncSession,
        user: User,
    ) -> Response:
        form = await request.form()
        raw_files = form.getall("files")
        file_payloads = [
            {"filename": uf.filename, "content": await uf.read()}
            for uf in raw_files
            if hasattr(uf, "filename") and uf.filename
        ]
        if not file_payloads:
            return Response({"message": "No files were provided"}, media_type=_JSON, status_code=400)

        try:
            results = await knowledge_base_service.upload_documents(
                db, user.id, flow_id, node_id, file_payloads,
            )
        except HTTPException as e:
            return _error_response(e)

        return Response({"results": results}, media_type=_JSON, status_code=200)

    # --------------------------
    # MANUAL TEXT
    # --------------------------
    @post("/manual-text")
    async def manual_text(
        self,
        flow_id: uuid.UUID,
        node_id: str,
        request: Request,
        db: AsyncSession,
        user: User,
    ) -> Response:
        try:
            payload = await request.json()
        except Exception:
            return Response({"message": "Invalid request body"}, media_type=_JSON, status_code=400)

        try:
            document = await knowledge_base_service.add_manual_text(
                db, user.id, flow_id, node_id,
                label=payload.get("label", ""),
                text=payload.get("text", ""),
            )
        except HTTPException as e:
            return _error_response(e)

        return Response({"document": document}, media_type=_JSON, status_code=200)

    # --------------------------
    # DELETE A DOCUMENT
    # --------------------------
    @post("/documents/{document_id:uuid}/delete")
    async def delete_document(
        self,
        flow_id: uuid.UUID,
        node_id: str,
        document_id: uuid.UUID,
        db: AsyncSession,
        user: User,
    ) -> Response:
        try:
            await knowledge_base_service.delete_document(db, user.id, flow_id, node_id, document_id)
        except HTTPException as e:
            return _error_response(e)

        return Response({}, media_type=_JSON, status_code=200)

    # --------------------------
    # TRAIN
    # --------------------------
    @post("/train")
    async def train(
        self,
        flow_id: uuid.UUID,
        node_id: str,
        db: AsyncSession,
        user: User,
    ) -> Response:
        try:
            state = await knowledge_base_service.train_knowledge_base(db, user.id, flow_id, node_id)
        except HTTPException as e:
            return _error_response(e)

        return Response(state, media_type=_JSON, status_code=200)
