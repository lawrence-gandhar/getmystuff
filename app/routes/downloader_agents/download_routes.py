"""
Serving a finished export, and streaming the progress of one being built.

Two controllers, because the two audiences are authenticated in genuinely different ways
and combining them would mean one handler deciding which rules apply — the split
``DeepAgentController`` / ``PublicChatbotController`` already makes for the same reason.

``DownloadController``
    The operator, on the agent console. ``require_auth`` and an ownership check through
    the agent that owns the tool.

``PublicDownloadController``
    A widget visitor, who has no session and no cookie. Authenticated by the chatbot
    key's uuid *and* the conversation's session token, both of which they already have —
    the token is the part that matters, because a widget key identifies a public website
    rather than a person.

**Everything streams.** A download is a ``Stream`` over an async chunk generator, so an
export of several hundred megabytes never becomes several hundred megabytes of process
memory; progress is a ``ServerSentEvent`` feed, so "it is being prepared" becomes "part 12
of 97". The repo's only previous download built its content in memory
(``chatbot_settings_routes.download_widget``), which is right for a 4 KB script and wrong
for this.

**A file is only ever served for a ``ready`` export.** Not for one still building — that
file is half written — and not for one whose expiry has passed, even if the bytes are
still on disk waiting for the reaper. Both are 404s with the same sentence as an export
that never existed, because distinguishing them tells an anonymous caller which uuids are
real.

No business logic here. Resolving, authorising and phrasing all belong to
``download_service``; this module reads a path off a row it was given and streams it.
"""

import asyncio
import json
import logging
import uuid as uuid_pkg
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Optional

from litestar import Controller, get
from litestar.config.cors import CORSConfig
from litestar.connection import Request
from litestar.exceptions import HTTPException
from litestar.middleware.base import DefineMiddleware
from litestar.middleware.cors import CORSMiddleware
from litestar.response import ServerSentEvent, Stream
from litestar.response.sse import ServerSentEventMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.auth import require_auth
from app.models.chatbot import ChatbotApiKey
from app.models.downloader_agents import EXPORT_READY
from app.models.user import User
from app.schemas.downloader_agents import DownloadExportView, PublicDownloadQuery
from app.services.downloader_agents.base import download_service as svc
from app.services.downloader_agents.base import part_store, progress
from app.services.downloader_agents.base.part_writer import writer_for

logger = logging.getLogger(__name__)

_JSON = "application/json"

# How much of the artifact is read per chunk. 64 KiB: the size at which the syscall
# overhead stops mattering and well below anything that would make a slow client's
# buffered chunk significant.
_CHUNK_BYTES = 64 * 1024

# Same reasoning as PublicChatbotController's: scoped to this controller rather than
# app-wide, because the rest of the application relies on cookie sessions and must not get
# a permissive cross-origin policy. A widget runs on a third-party site, so its download
# link has to be fetchable from there — and the real authorisation is the key-plus-token
# check in the handler, not CORS, which cannot see either.
_public_cors = DefineMiddleware(
    CORSMiddleware,
    config=CORSConfig(
        allow_origins=["*"],
        allow_methods=["GET", "OPTIONS"],
        allow_headers=["Content-Type"],
        allow_credentials=False,
    ),
)


class DownloadController(Controller):
    """Exports belonging to the signed-in operator."""

    path = "/downloads"
    dependencies = {"user": require_auth}

    @get("/{export_id:uuid}")
    async def download(
        self,
        export_id: uuid_pkg.UUID,
        db: AsyncSession,
        user: User,
    ) -> Stream:
        """Stream one finished export as an attachment."""
        export = await svc.owned_export(db, user.id, export_id)

        return _streamed_artifact(export)

    @get("/{export_id:uuid}/status")
    async def status(
        self,
        export_id: uuid_pkg.UUID,
        db: AsyncSession,
        user: User,
    ) -> dict:
        """Where one export has got to, as JSON."""
        export = await svc.owned_export(db, user.id, export_id)

        return DownloadExportView.of(
            export, _url_if_ready(export, svc.console_download_url(export.uuid)),
        ).payload()

    @get("/{export_id:uuid}/events")
    async def events(
        self,
        export_id: uuid_pkg.UUID,
        db: AsyncSession,
        user: User,
    ) -> ServerSentEvent:
        """
        Stream this export's build progress until it finishes.

        The export is resolved and authorised **before** the stream is returned — once
        Litestar starts sending an event stream the status code is already committed, so
        an unauthorised caller has to be refused here or not at all.
        """
        export = await svc.owned_export(db, user.id, export_id)

        return _streamed_progress(
            export.uuid,
            lambda row: _url_if_ready(row, svc.console_download_url(row.uuid)),
        )


class FileDownloadController(Controller):
    """
    The visitor-facing file itself, at the path the artifact is stored under.

        SITE_URL/file_downloaders/<session-id>/<file-name>?key=<widget-key-uuid>

    A URL that names the session and the file rather than an export uuid, because that
    is how the artifact is stored — ``uploads/file_downloaders/<session>/<file>`` — so
    the link and the directory are the same two facts and cannot drift apart.

    **It looks like a static path and is nothing of the kind.** Every request is
    resolved to an export row and refused unless all four hold: the widget key is
    active, the session in the path is the session that produced the file, the export
    is ``ready``, and its window has not closed. Serving this directory statically
    would hand every visitor every other visitor's data, which is why the files live
    under ``uploads/`` and not under ``static/``.
    """

    path = "/file_downloaders"
    middleware = [_public_cors]

    @get("/{session_id:str}/{file_name:str}")
    async def download(
        self,
        session_id: str,
        file_name: str,
        request: Request,
        db: AsyncSession,
    ) -> Stream:
        """Stream one finished export as an attachment, for the visitor who asked."""
        export = await _session_download(request, db, session_id, file_name)

        return _streamed_artifact(export, session_id=session_id)


class PublicDownloadController(Controller):
    """
    An export's progress and status, for one widget visitor's own conversation.

    Keyed by export uuid rather than by file, because both are asked while the export
    is still being built — there is no file name until the merge succeeds, and finding
    out when there will be one is the point of the call. The finished file is served by
    :class:`FileDownloadController`.

    ``/public/downloads/{export_id}`` is still here, and still serves the file. Nothing
    the application generates points at it any more — every link now names the session
    and the file — but a link handed out before that change is in somebody's chat
    transcript, and breaking it would turn a working download into an error for no
    reason the visitor could understand.
    """

    path = "/public/downloads"
    middleware = [_public_cors]

    @get("/{export_id:uuid}")
    async def download(
        self,
        export_id: uuid_pkg.UUID,
        request: Request,
        db: AsyncSession,
    ) -> Stream:
        """Stream one finished export as an attachment, for the visitor who asked."""
        export = await _visitor_export(request, db, export_id)

        return _streamed_artifact(export)

    @get("/{export_id:uuid}/status")
    async def status(
        self,
        export_id: uuid_pkg.UUID,
        request: Request,
        db: AsyncSession,
    ) -> dict:
        """Where one export has got to, as JSON."""
        export, key_uuid, _token = await _visitor_export(
            request, db, export_id, with_scope=True,
        )

        return DownloadExportView.of(
            export,
            _url_if_ready(export, svc.visitor_download_url(export, key_uuid)),
        ).payload()

    @get("/{export_id:uuid}/events")
    async def events(
        self,
        export_id: uuid_pkg.UUID,
        request: Request,
        db: AsyncSession,
    ) -> ServerSentEvent:
        """Stream this export's build progress until it finishes."""
        export, key_uuid, _token = await _visitor_export(
            request, db, export_id, with_scope=True,
        )

        return _streamed_progress(
            export.uuid,
            lambda row: _url_if_ready(row, svc.visitor_download_url(row, key_uuid)),
        )


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------

async def _session_download(
    request: Request,
    db: AsyncSession,
    session_id: str,
    file_name: str,
) -> Any:
    """
    Resolve one artifact for the visitor whose session produced it.

    Four checks, and the file is served only if all four pass:

    1. the ``key`` query parameter names a chatbot key that is **active** — switching a
       widget off has to stop its links working, not just its chat;
    2. that key, the session in the path and the file name together name an export
       this application actually produced;
    3. the export is ``ready`` (:func:`_streamed_artifact`) — a file still being merged
       is a half-written file;
    4. its window has not closed (:func:`_streamed_artifact` again) — checked here as
       well as by the reaper, so a lapsed link cannot be served in the minutes before
       the next sweep.

    None of that is inferable from the path, which is the point of naming it: the URL
    reads like a static file and the response is authorised on every single request.
    """
    query = PublicDownloadQuery.from_query(request)

    if not query.key:
        raise HTTPException(status_code=404, detail=svc.NOT_FOUND_MESSAGE)

    try:
        key_uuid = uuid_pkg.UUID(query.key)
    except ValueError as exc:
        raise HTTPException(
            status_code=404, detail=svc.NOT_FOUND_MESSAGE,
        ) from exc

    chatbot_key = await _key_by_uuid(db, key_uuid)

    if chatbot_key is None:
        # An inactive key lands here too — _key_by_uuid filters on is_active.
        raise HTTPException(status_code=404, detail=svc.NOT_FOUND_MESSAGE)

    return await svc.session_download(
        db, chatbot_key.id, session_id, file_name,
    )


async def _visitor_export(
    request: Request,
    db: AsyncSession,
    export_id: uuid_pkg.UUID,
    with_scope: bool = False,
) -> Any:
    """
    Resolve one export for an anonymous widget visitor.

    The key is matched on the chatbot key's **uuid**, not its publishable ``api_key``
    value. That is deliberate: this link is spoken aloud by an assistant into a chat
    transcript, and a link carrying the widget's credential would put that credential in
    the transcript. The uuid is an opaque identifier and the session token is what
    actually scopes the request.
    """
    query = PublicDownloadQuery.from_query(request)

    if not query.key or not query.session_token:
        # Both, always. A key with no token would let any visitor of a public widget read
        # every export ever produced for it.
        raise HTTPException(status_code=404, detail=svc.NOT_FOUND_MESSAGE)

    try:
        key_uuid = uuid_pkg.UUID(query.key)
    except ValueError as exc:
        raise HTTPException(
            status_code=404, detail=svc.NOT_FOUND_MESSAGE,
        ) from exc

    chatbot_key = await _key_by_uuid(db, key_uuid)

    if chatbot_key is None:
        raise HTTPException(status_code=404, detail=svc.NOT_FOUND_MESSAGE)

    export = await svc.visitor_export(
        db, chatbot_key.id, query.session_token, export_id,
    )

    if with_scope:
        return export, str(chatbot_key.uuid), query.session_token

    return export


async def _key_by_uuid(db: AsyncSession, key_uuid: uuid_pkg.UUID) -> Optional[Any]:
    """One chatbot key by its public uuid, active only."""
    statement = (
        select(ChatbotApiKey)
        .where(ChatbotApiKey.uuid == key_uuid, ChatbotApiKey.is_active.is_(True))
        .limit(1)
    )

    return (await db.execute(statement)).scalars().first()


def _url_if_ready(export: Any, url: str) -> Optional[str]:
    """
    The download link, but only for an export that can actually be fetched.

    Returning a link for an export that is still building would be a link that 404s, and
    a consumer cannot tell that apart from a broken route.
    """
    return url if export.status == EXPORT_READY else None


def _streamed_artifact(export: Any, session_id: Optional[str] = None) -> Stream:
    """
    One export's bytes, as an attachment, read from disk in chunks.

    Every refusal is a 404, with one of two sentences. An export whose time is up is told
    so; everything else — not ready, file deleted, a stored path outside its own directory
    — is one answer, because those are an operator's problem and not the visitor's.

    Expiry is checked **first**, and the order is the point. Testing the status first put
    an export the reaper had already swept into the not-found branch, so a visitor whose
    link had simply timed out was told it "could not be found" — which reads like the
    application lost their file. See ``download_service.has_lapsed``.

    ``session_id`` is the session segment from the URL, present only on the
    ``/file_downloaders`` route. When it is given, the stored path is re-checked against
    *that* session's directory rather than the export's own: the segment came off the
    wire, and this is what stops one session's URL ever resolving into another's folder
    even if the row lookup above were somehow wrong about which export it named.
    """
    if svc.has_lapsed(export):
        # Refused here as well as by the reaper, so a lapsed link cannot be served in the
        # window before the reaper next runs — and so one that the reaper *has* been round
        # for gets the same sentence rather than a different, misleading one.
        raise HTTPException(status_code=404, detail=svc.EXPIRED_MESSAGE)

    if export.status != EXPORT_READY or not export.file_path:
        raise HTTPException(status_code=404, detail=svc.NOT_FOUND_MESSAGE)

    try:
        path = _resolved_path(export, session_id)
    except ValueError:
        logger.error(
            "Export %s has a file_path outside its own directory: %s",
            export.uuid,
            export.file_path,
        )
        raise HTTPException(status_code=404, detail=svc.NOT_FOUND_MESSAGE) from None

    if not path.is_file():
        logger.warning("Export %s is ready but its file is missing", export.uuid)
        raise HTTPException(status_code=404, detail=svc.NOT_FOUND_MESSAGE)

    file_name = export.file_name or path.name
    writer = writer_for(export.file_format)

    headers = {"Content-Disposition": f'attachment; filename="{file_name}"'}

    if export.byte_size:
        # So the browser can show a progress bar rather than an indeterminate spinner.
        headers["Content-Length"] = str(export.byte_size)

    return Stream(
        _chunks(path),
        media_type=writer.media_type,
        headers=headers,
    )


def _resolved_path(export: Any, session_id: Optional[str]) -> Path:
    """
    The artifact's path, re-checked against the directory it is supposed to be in.

    Two roots because there are two of them on disk during the changeover: an export
    written before the artifact moved to ``uploads/file_downloaders/<session>/`` still
    has a path under ``uploads/exports/<uuid>/``, and its link is in somebody's chat
    transcript. Whichever containment rule the stored path satisfies is the one that
    applies; a path satisfying neither is the failure this function exists to catch.
    """
    if session_id is not None:
        return part_store.resolve_within_downloads(session_id, export.file_path)

    try:
        return part_store.resolve_within_downloads(
            export.session_token, export.file_path,
        )
    except ValueError:
        return part_store.resolve_within_export(export.uuid, export.file_path)


async def _chunks(path: Path) -> AsyncIterator[bytes]:
    """
    Read one file in fixed chunks, off the event loop.

    ``asyncio.to_thread`` per read rather than one blocking read of the whole file: the
    point of streaming is that a 500 MB artifact never exists in memory, and the point of
    the thread is that reading it does not stall every other request while it happens.
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


def _streamed_progress(
    export_uuid: Any,
    url_for: Callable[[Any], Optional[str]],
) -> ServerSentEvent:
    """
    One export's progress as named SSE events.

    The event name is set per message from the frame's own ``event`` field, so a browser
    can either switch on ``event.type`` or read the payload — and the two cannot disagree,
    because they come from the same value.
    """

    async def messages() -> AsyncIterator[ServerSentEventMessage]:
        async for frame in progress.stream_progress(export_uuid, url_for):
            yield ServerSentEventMessage(
                data=json.dumps(frame, default=str),
                event=str(frame.get("event") or "progress"),
            )

    return ServerSentEvent(messages())
