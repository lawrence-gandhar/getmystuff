"""
Serving a file a Create File block wrote.

Two controllers, because the two audiences are authenticated in genuinely different ways
and one handler deciding which rules apply is how a rule gets applied to the wrong caller.
The same split ``DownloadController`` / ``PublicDownloadController`` already makes.

``GeneratedFileController``
    The owner. ``require_auth``, and the ownership filter is part of the lookup rather
    than a check after it — see ``file_service.owner_file``. Serves files from either
    canvas: a flow's file is as much this operator's data as a pipeline's.

``PublicGeneratedFileController``
    A widget visitor, who has no session and no cookie. Authorised by the chatbot key's
    uuid *and* the conversation's session token, both of which the widget already holds,
    plus the file having come from a flow at all. It looks like a static path and is
    nothing of the kind — which is why these files live under ``uploads/`` and not under
    ``static/``, whose contents main.py serves with no authentication whatsoever.

**Everything streams**, in 64 KiB chunks off the event loop, so a large Parquet file never
becomes that much process memory.

**A lapsed file is refused here as well as by the reaper.** The sweep deletes bytes; this
is what enforces the rule in the minutes before it next runs. A 410 rather than a 404 for
that case, and its own sentence: "could not be found" reads as though the application lost
the file and sends somebody looking for a link that worked yesterday.

No business logic here. Resolving, authorising and phrasing all belong to
``file_service``; this module reads a path off a row it was handed and streams it.
"""

import asyncio
import logging
import uuid as uuid_pkg
from pathlib import Path
from typing import Any, AsyncIterator

from litestar import Controller, get
from litestar.config.cors import CORSConfig
from litestar.connection import Request
from litestar.exceptions import HTTPException
from litestar.middleware.base import DefineMiddleware
from litestar.middleware.cors import CORSMiddleware
from litestar.response import Stream
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.auth import require_auth
from app.db.db_utils import CRUDQueryBuilder
from app.models.chatbot import ChatbotApiKey
from app.models.file_delivery import GeneratedFile
from app.models.user import User
from app.schemas.downloader_agents import PublicDownloadQuery
from app.schemas.file_delivery import GeneratedFileView
from app.services.file_delivery import file_service as svc

logger = logging.getLogger(__name__)

key_crud = CRUDQueryBuilder(ChatbotApiKey)

# How much of the file is read per chunk. 64 KiB, matching the export route: the size at
# which syscall overhead stops mattering and well below anything that would make one slow
# client's buffered chunk significant.
_CHUNK_BYTES = 64 * 1024

# Scoped to the public controller rather than app-wide, for the reason
# ``PublicChatbotController`` and the public download route both give: the rest of the
# application relies on cookie sessions and must not be handed a permissive cross-origin
# policy. A widget runs on somebody else's site, so its download link has to be fetchable
# from there — and the real authorisation is the key-plus-token check in the handler, which
# CORS cannot see and does not replace.
_public_cors = DefineMiddleware(
    CORSMiddleware,
    config=CORSConfig(
        allow_origins=["*"],
        allow_methods=["GET", "OPTIONS"],
        allow_headers=["Content-Type"],
        allow_credentials=False,
    ),
)


class GeneratedFileController(Controller):
    """Files belonging to the signed-in owner, from either canvas."""

    path = "/generated_files"
    dependencies = {"user": require_auth}

    @get("/{file_id:uuid}")
    async def download(
        self,
        file_id: uuid_pkg.UUID,
        db: AsyncSession,
        user: User,
    ) -> Stream:
        """Stream one file as an attachment."""
        record = await svc.owner_file(db, user.id, file_id)

        return _streamed_file(record)

    @get("/{file_id:uuid}/status")
    async def status(
        self,
        file_id: uuid_pkg.UUID,
        db: AsyncSession,
        user: User,
    ) -> dict:
        """
        One file's details as JSON: what it is, how big, and how long it has left.

        Here because a pipeline's Download File node puts a URL into its outputs and the
        operator reading that run may want to know whether it is still good without
        starting a download of it.
        """
        record = await svc.owner_file(db, user.id, file_id)

        return GeneratedFileView.of(
            record, svc.owner_download_url(record.uuid),
        ).payload()


class PublicGeneratedFileController(Controller):
    """
    A file one widget conversation produced, for the visitor whose conversation it was.

        SITE_URL/public/generated_files/<file-uuid>?key=<widget-key-uuid>&session_token=…

    The key is matched on the chatbot key's **uuid**, never its publishable ``api_key``
    value — this link is handed to a visitor and lives in a chat transcript, and a link
    carrying the widget's credential would put that credential in the transcript. The uuid
    is opaque, and the session token is what actually scopes the request.
    """

    path = "/public/generated_files"
    middleware = [_public_cors]

    @get("/{file_id:uuid}")
    async def download(
        self,
        file_id: uuid_pkg.UUID,
        request: Request,
        db: AsyncSession,
    ) -> Stream:
        """Stream one file as an attachment, for the visitor who was given it."""
        record = await _visitor_file(request, db, file_id)

        return _streamed_file(record)


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------

async def _visitor_file(
    request: Request, db: AsyncSession, file_id: uuid_pkg.UUID,
) -> GeneratedFile:
    """
    Resolve one file for an anonymous widget visitor.

    Both the key and the token, always. A key alone would let any visitor of a public
    widget read every file that widget ever produced, which is the failure this whole
    route shape exists to prevent.
    """
    query = PublicDownloadQuery.from_query(request)

    if not query.key or not query.session_token:
        raise HTTPException(status_code=404, detail=svc.NOT_FOUND_MESSAGE)

    try:
        key_uuid = uuid_pkg.UUID(query.key)
    except ValueError as exc:
        raise HTTPException(
            status_code=404, detail=svc.NOT_FOUND_MESSAGE,
        ) from exc

    chatbot_key = await _key_by_uuid(db, key_uuid)

    if chatbot_key is None:
        # An inactive key lands here too — _key_by_uuid filters on is_active, because
        # switching a widget off has to stop its links working and not just its chat.
        raise HTTPException(status_code=404, detail=svc.NOT_FOUND_MESSAGE)

    return await svc.visitor_file(
        db, chatbot_key.id, query.session_token, file_id,
    )


async def _key_by_uuid(db: AsyncSession, key_uuid: uuid_pkg.UUID) -> Any:
    """
    One chatbot key by its public uuid, **active only**.

    Through the generic builder rather than a hand-written select, per CLAUDE.md — the
    filter is a plain equality on two columns and there is nothing here generic CRUD cannot
    express.

    The ``is_active`` half is part of the lookup rather than a check after it, because
    switching a widget off has to stop its links working and not only its chat.
    """
    return await key_crud.get_by_uuid(
        db, key_uuid, extra_filters={"is_active": True},
    )


def _streamed_file(record: GeneratedFile) -> Stream:
    """
    One file's bytes, as an attachment, read from disk in chunks.

    Expiry and existence are both decided by ``file_service.assert_servable`` rather than
    here, so the owner route and the public route cannot come to different conclusions
    about the same file — the mistake the export route's ordering bug was: an expired file
    reported as "could not be found", which reads like the application lost it.
    """
    path = svc.assert_servable(record)

    headers = {
        "Content-Disposition": f'attachment; filename="{record.file_name}"',
    }

    if record.byte_size:
        # So the browser shows a progress bar rather than an indeterminate spinner.
        headers["Content-Length"] = str(record.byte_size)

    return Stream(
        _chunks(path),
        media_type=svc.media_type_of(record),
        headers=headers,
    )


async def _chunks(path: Path) -> AsyncIterator[bytes]:
    """
    Read one file in fixed chunks, off the event loop.

    ``asyncio.to_thread`` per read rather than one blocking read of the whole file: the
    point of streaming is that a large file never exists in memory, and the point of the
    thread is that reading it does not stall every other request while it happens. A copy
    of ``download_routes._chunks`` deliberately — it is nine lines, and importing a private
    helper out of another feature's route module would couple the two files' internals
    rather than their contracts.
    """

    def _open() -> Any:
        return open(path, "rb")

    handle = await asyncio.to_thread(_open)

    try:
        while True:
            chunk = await asyncio.to_thread(handle.read, _CHUNK_BYTES)

            if not chunk:
                return

            yield chunk
    finally:
        await asyncio.to_thread(handle.close)
