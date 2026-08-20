"""
The two tools that let an agent offer a download and then actually produce it.

Every other data tool in this application takes no arguments, deliberately: see
``app.services.deep_agents.tool_factory``, which refuses to let model-generated text near
a query. These two are the exception and it is worth being precise about why they are not
a hole in that rule. Neither carries any part of a query. ``confirm_download`` takes an
export id and a file format; ``download_status`` takes an export id. The export id names a
row this application wrote, and is checked against the conversation it belongs to; the
format is checked against a list of three. Nothing either tool accepts can reach a
database as anything but a parameter, which is the property the no-arguments rule exists
to protect — arrived at differently.

**Where the offer comes from.** :func:`describe_tool_result` is called by the tool factory
after every data tool runs. When the result is bigger than the display budget it counts
the real total, records an offer, and runs the export graph to its confirmation interrupt
— and the interrupt's payload *is* the sentence the model is told to repeat. So the number
in front of the user came from a ``COUNT(*)``, and the promise of a file came from a graph
that is already paused waiting to make one.

**Nothing here raises at the model.** A tool that raises aborts the whole turn and gives
the visitor a 500 for something recoverable, so every failure comes back as tool output
instead — the same choice ``tool_factory`` makes for ``ToolQueryError``. An offer that
cannot be made is not mentioned at all: the user still gets their answer, and the log
gets the reason.

**The conversation is the scope.** :class:`DownloadContext` carries who is asking. On the
operator console that is the agent; in a widget it is the agent plus the visitor's
session token, and the token is what stops one visitor confirming another's offer or
downloading their file.
"""

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from langchain_core.tools import StructuredTool

from app.models.downloader_agents import (
    EXPORT_BUILDING,
    EXPORT_FAILED,
    EXPORT_OFFERED,
    EXPORT_QUEUED,
    EXPORT_READY,
    FORMAT_CSV,
)
from app.schemas.downloader_agents import ConfirmDownloadArgs, DownloadStatusArgs
from app.services.deep_agents.prompt_builder import (
    CONFIRM_DOWNLOAD_TOOL,
    DOWNLOAD_STATUS_TOOL,
)
from app.services.deep_agents.query_executor import DISPLAY_ROW_LIMIT, describe_result
from app.services.downloader_agents.base import download_notice
from app.services.downloader_agents.base import download_service as svc
from app.services.downloader_agents.base import job_queue
from app.services.downloader_agents.base import record_reader
from app.services.downloader_agents.base.record_reader import RecordSource, count_records

logger = logging.getLogger(__name__)


# What the agent is told once a build has been queued. Not the offer sentence — that one
# asked a question, this one answers it — and it names the status tool so the model has
# something to do when the user asks again.
_QUEUED_REPLY = (
    "The file is being created now. Tell the user it is being prepared. A progress "
    "indicator and a download button are already shown to them below your reply, so do "
    "not write a link or a URL yourself and do not promise to send one. If they ask "
    f"again, call `{DOWNLOAD_STATUS_TOOL}`."
)


@dataclass(frozen=True)
class DownloadContext:
    """
    Who is asking, so an export can be scoped to their conversation.

    ``data_agent_id`` is always present. The chatbot fields are set only for a widget
    visitor, and both of them together — the key alone identifies a public website, not a
    person, so without the token any visitor could act on any other's offer.
    """

    data_agent_id: int
    session_token: Optional[str] = None
    chatbot_key_id: Optional[int] = None
    chatbot_key_uuid: Optional[str] = None

    @property
    def is_visitor(self) -> bool:
        return bool(self.chatbot_key_id and self.session_token)

    def download_url(self, export: Any) -> str:
        """
        The link this asker can actually fetch the file from.

        Takes the export row rather than its uuid because a visitor's link names the
        session and the *file* — ``/file_downloaders/<session>/<name>`` — so the row is
        what it is built from. The console's link is still the uuid: the operator is on
        this application's own origin and needs no session in the path.
        """
        if self.is_visitor:
            return svc.visitor_download_url(export, self.chatbot_key_uuid)

        return svc.console_download_url(export.uuid)


# --------------------------------------------------------------------------
# The offer, made from a data tool's result
# --------------------------------------------------------------------------

async def describe_tool_result(
    entry: dict,
    rows: List[Dict[str, Any]],
    context: Optional[DownloadContext],
) -> str:
    """
    Render a data tool's rows for the model, with a download offer when there are more.

    The cheap path is the common one: a result inside the display budget needs no count
    and no offer, and does not pay for either. Past the budget it is worth one
    ``COUNT(*)`` to be able to say how many there really are.

    Every failure degrades to the plain description. An export offer is an extra; the
    answer is not, and an offer that could not be prepared must never cost the user
    their reply.
    """
    if context is None or len(rows) <= DISPLAY_ROW_LIMIT:
        return describe_result(rows)

    try:
        return await _describe_with_offer(entry, rows, context)
    except Exception:  # noqa: BLE001 — an offer is an extra, the answer is not
        logger.exception(
            "Could not prepare a download offer for tool %s", entry.get("tool_name"),
        )
        return describe_result(rows)


async def _describe_with_offer(
    entry: dict,
    rows: List[Dict[str, Any]],
    context: DownloadContext,
) -> str:
    """
    Count the result, record an offer, and pause a graph ready to build it.

    The order matters. Counting first means a set past the ceiling is refused before an
    offer is made rather than after; recording the offer before starting the graph means
    the graph has a row (and a thread id) to work with; and starting the graph is what
    produces the sentence, so nothing composes it twice.
    """
    from app.services.downloader_agents.base.download_graph import start_export_offer

    source = RecordSource(
        datasource=entry["datasource"],
        config=dict(entry.get("config") or {}),
        table_name=entry["table_name"],
        sql_query=entry.get("sql_query"),
        table_names=list(entry.get("table_names") or []),
    )

    counted = await count_records(source)

    if counted.exceeds_ceiling:
        # No offer and no export row: there is nothing to confirm. The model is told the
        # total and the refusal, which together are more use than an offer that would
        # have to be withdrawn.
        return describe_result(
            rows,
            total_rows=counted.total,
            count_is_lower_bound=counted.is_lower_bound,
            # Read through the module rather than from a name bound at import: the
            # ceiling is configurable, and a copy would let this sentence and the check
            # that produced it name two different limits.
            offer=svc.too_large_message(
                counted.total, record_reader.MAX_EXPORT_ROWS,
            ),
        )

    if counted.total <= DISPLAY_ROW_LIMIT:
        # The sample was bigger than the real total — the query changed under us, or the
        # count and the read disagree. Nothing to offer, and the count is the honest
        # figure.
        return describe_result(rows, total_rows=counted.total)

    async with svc.open_session() as db:
        export = await svc.create_offer(
            db,
            data_agent_id=context.data_agent_id,
            tool_config_id=int(entry["id"]),
            total_rows=counted.total,
            count_is_lower_bound=counted.is_lower_bound,
            file_format=FORMAT_CSV,
            chatbot_key_id=context.chatbot_key_id,
            session_token=context.session_token,
        )
        export_uuid = str(export.uuid)

    payload = await start_export_offer(export_uuid, FORMAT_CSV)

    if not payload:
        # The graph finished without pausing, which means its own count refused the set.
        # The export row carries the reason.
        async with svc.open_session() as db:
            refused = await svc.get_export(db, export_uuid)
            message = refused.error_message if refused else None

        return describe_result(
            rows,
            total_rows=counted.total,
            count_is_lower_bound=counted.is_lower_bound,
            offer=message,
        )

    return describe_result(
        rows,
        total_rows=counted.total,
        count_is_lower_bound=counted.is_lower_bound,
        offer=str(payload.get("question") or ""),
    )


# --------------------------------------------------------------------------
# The tools
# --------------------------------------------------------------------------

def build_download_tools(context: DownloadContext) -> List[StructuredTool]:
    """
    The two download tools, bound to one conversation.

    Returned as a list so the tool factory can extend its own set with it. Empty is never
    correct: an agent that can make an offer must be able to honour it, and an agent
    whose result happens to be small has simply not used these yet.
    """
    return [
        StructuredTool.from_function(
            coroutine=_confirm_download_for(context),
            name=CONFIRM_DOWNLOAD_TOOL,
            description=(
                "Call this when the user agrees to the downloadable file you offered "
                "them. It starts building the file and reports back. Use it for a plain "
                "'yes' as well as for 'yes, as a spreadsheet'."
            ),
            args_schema=ConfirmDownloadArgs,
        ),
        StructuredTool.from_function(
            coroutine=_download_status_for(context),
            name=DOWNLOAD_STATUS_TOOL,
            description=(
                "Call this when the user asks whether their file is ready, or where it "
                "is. It reports the progress and gives you the download link once the "
                "file exists."
            ),
            args_schema=DownloadStatusArgs,
        ),
    ]


def _confirm_download_for(context: DownloadContext):
    """The ``confirm_download`` coroutine, closed over one conversation."""

    async def confirm_download(
        export_id: Optional[str] = None,
        file_format: str = FORMAT_CSV,
    ) -> str:
        args = ConfirmDownloadArgs.parse(
            {"export_id": export_id, "file_format": file_format},
        )

        async with svc.open_session() as db:
            export = await _resolve_for_confirmation(db, context, args)

            if export is None:
                return (
                    "There is no download waiting to be confirmed. Ask the user what "
                    "they would like a list of, run the tool for it, and offer the file "
                    "again."
                )

            if export.status != EXPORT_OFFERED:
                return _already_underway(context, export)

            await svc.mark_queued(db, export, args.file_format)
            await job_queue.enqueue_export(db, export)
            export_uuid = str(export.uuid)

            # After the transition, so the widget is handed the state the export is
            # actually in — "queued", never the "offered" it was a line ago.
            download_notice.note_export(context, export)

        logger.info(
            "Export %s confirmed as %s and queued", export_uuid, args.file_format,
        )

        return _QUEUED_REPLY

    return confirm_download


def _download_status_for(context: DownloadContext):
    """The ``download_status`` coroutine, closed over one conversation."""

    async def download_status(export_id: Optional[str] = None) -> str:
        args = DownloadStatusArgs.parse({"export_id": export_id})

        async with svc.open_session() as db:
            export = await _resolve_for_status(db, context, args)

            if export is None:
                return (
                    "There is no file being prepared for this conversation. If the user "
                    "wants one, run the tool for the list they asked about and offer it."
                )

            download_notice.note_export(context, export)

            return _describe_status(export)

    return download_status


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------

async def _resolve_for_confirmation(db, context: DownloadContext, args) -> Any:
    """
    The export a confirmation refers to, scoped to this conversation.

    A model-supplied id is looked up **and then checked** against the conversation. An id
    that names a real export belonging to someone else resolves to None here, which the
    caller reports as "nothing to confirm" — the same answer as an id that never existed,
    because telling the two apart would confirm which ids are real.
    """
    if not args.wants_latest():
        export = await _scoped_export(db, context, args.export_id)

        if export is not None:
            return export

        # A model that produced an id we cannot place is a model that lost the thread,
        # not necessarily a wrong conversation. Falling through to the latest offer is
        # what makes a mistyped id behave like a plain "yes".

    open_offer = await svc.latest_open_offer(
        db, context.data_agent_id, context.session_token,
    )

    if open_offer is not None:
        return open_offer

    # No *open* offer, but there may be an export already underway — a second "yes",
    # which is common when a user repeats themselves or a model calls the tool twice in
    # one turn. Returning it lets the caller report its state instead of claiming there
    # was nothing on offer, which would be both wrong and confusing to be told after
    # having just said yes.
    return await svc.latest_export(db, context.data_agent_id, context.session_token)


async def _resolve_for_status(db, context: DownloadContext, args) -> Any:
    """The export a status question refers to, of any status."""
    if args.wants_latest():
        return await svc.latest_export(db, context.data_agent_id, context.session_token)

    export = await _scoped_export(db, context, args.export_id)

    if export is None:
        return await svc.latest_export(
            db, context.data_agent_id, context.session_token,
        )

    return export


async def _scoped_export(db, context: DownloadContext, raw_uuid: str) -> Any:
    """
    One export by a model-supplied uuid, or None if it is not this conversation's.

    The uuid is not trusted — a model will pass "the-export-id", a truncated uuid, or a
    whole sentence. ``get_export`` coerces it and returns None for anything that is not a
    uuid, which lands in the same "not this conversation's" branch below.
    """
    export = await svc.get_export(db, raw_uuid)

    if export is None or export.data_agent_id != context.data_agent_id:
        return None

    if context.session_token and export.session_token != context.session_token:
        return None

    return export


def _already_underway(context: DownloadContext, export) -> str:
    """
    What to say about an offer that has already been acted on.

    A second "yes" is common — a user repeats themselves, or the model calls the tool
    twice in one turn — and it must not produce a second identical file. So the answer to
    "confirm this" for an export that is past ``offered`` is its current state.
    """
    # Noted here as well as on the first confirmation: a user who says "yes" twice should
    # see the same card the first "yes" gave them, not lose it for having repeated
    # themselves.
    download_notice.note_export(context, export)

    return _describe_status(export)


def _describe_status(export) -> str:
    """
    One export's state, phrased for the model to relay.

    **No URL is given to the model, in any state.** The interface renders the download
    itself, as a button under the reply, from the export this turn recorded through
    :mod:`download_notice` — so a link in the answer text would be a second, worse copy
    of a control the user already has. It also could not work: the answer is rendered as
    plain text, so a model writing markdown produces a visible ``[Download](…)`` rather
    than a link, which is exactly what it used to do.

    What the model is told instead is that the button exists, so it can refer to it in
    words without inventing an address for it.
    """
    if export.status == EXPORT_READY:
        return (
            f"The file is ready and a download button is already shown to the user "
            f"below your reply — do not write a link or a URL yourself. It contains "
            f"{export.rows_written} record(s) and is available for the next "
            f"{svc.ttl_phrase()}. "
            f"Just tell them it is ready."
        )

    if export.status == EXPORT_FAILED:
        return (
            "The file could not be created. Tell the user exactly this: "
            f"{export.error_message or svc.FAILURE_MESSAGE}"
        )

    if export.status in {EXPORT_QUEUED, EXPORT_BUILDING}:
        written = export.rows_written or 0
        total = export.total_rows or 0
        progress = f" {written} of {total} record(s) written so far." if written else ""

        return (
            "The file is still being prepared." + progress + " Tell the user it is on "
            "its way and to ask again in a moment."
        )

    if export.status == EXPORT_OFFERED:
        return (
            "That file has been offered but not confirmed. Ask the user whether they "
            "would like it."
        )

    return (
        "That download is no longer available. Tell the user it has expired and offer "
        "to prepare it again."
    )
