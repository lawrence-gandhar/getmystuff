"""
The export one turn touched, carried up to whoever is rendering that turn.

**Why this exists.** A download tool runs several layers below the thing that answers a
visitor: the turn service calls the deep agent service, which builds a LangChain agent,
which calls a tool, which queues an export. Only that innermost layer knows an export
id — and only the outermost layer builds the payload the widget reads. Threading a
return value up through four layers that have no other interest in it would mean
changing every signature in between, and the deep agent's tool interface is fixed by
LangChain: a tool returns a string for the model, nothing else.

So the export is recorded in a context-local, exactly as ``utils/turn_recorder.py``
records token cost for the same reason and from the same depth. Same shape, deliberately:
a scope opened per turn, a setter called wherever the fact becomes known, and one read at
the top.

**What it is not.** Not a queue, not state, and not how the export is tracked — the
``download_exports`` row is all of those. This carries one already-persisted export up one
call stack so a single reply can mention it. If it is lost, nothing breaks: the file is
still built by the worker and the visitor can still ask "is it ready yet?", which resolves
through :func:`download_service.latest_export`. It is what turns a sentence into a button,
not what makes the file.

**Why it is only ever set for a build.** An *offer* is a question, and a question with a
progress bar under it is a lie — nothing is being made yet. So the offer path does not
call this. ``confirm_download`` and ``download_status`` do, because by then there is a row
worth watching.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Iterator, Optional

from app.models.downloader_agents import EXPORT_READY
from app.schemas.downloader_agents import DownloadNoticeView


@dataclass
class _Notice:
    """
    A mutable box for one turn's export, and the box is the whole point.

    **It cannot be a plain value in the ContextVar.** A tool does not run in the task that
    started the turn — LangGraph runs its nodes as their own tasks, and a new task gets a
    *copy* of the context. Rebinding a ContextVar inside that copy is invisible to the
    parent, so ``_current.set(payload)`` from inside a tool would set something nobody
    ever reads. Mutating an object the copy inherited by reference is visible, because
    both contexts point at the same object.

    This is exactly why ``utils/turn_recorder.py`` puts a ``TurnRecord`` in its ContextVar
    and appends to it rather than replacing it — the same constraint, reached from the
    same depth. It is not an implementation detail either module is free to change.
    """

    payload: Optional[dict] = None


_current: ContextVar[Optional[_Notice]] = ContextVar("download_notice", default=None)


@contextmanager
def download_scope() -> Iterator[None]:
    """
    Open a fresh notice scope for one turn.

    A new box every time, and the old binding restored on the way out. Without that, a
    task reused for a second turn would report the first turn's export — a visitor shown
    a download somebody else asked for, which is worse than being shown none.
    """
    token = _current.set(_Notice())
    try:
        yield
    finally:
        _current.reset(token)


def note_export(context: Any, export: Any) -> None:
    """
    Record that this turn is about ``export``, so the reply can carry it.

    ``context`` is a ``download_tools.DownloadContext`` — it is what knows whether the
    asker is an operator or a widget visitor, and therefore which of the two URL prefixes
    this export is reachable at. Passing it rather than the URLs keeps the audience
    decision in the one place that already makes it.

    Silently does nothing outside a :func:`download_scope`. That is the right behaviour
    rather than an error: the tools are also reachable from code paths that render no
    reply, and a download nobody is going to draw is not worth failing a turn over.

    The last call in a turn wins. A model that checks the status and then confirms a
    build has said two things about the same conversation, and the second is the current
    one.
    """
    notice = _current.get()

    if notice is None or export is None:
        return

    notice.payload = _notice_for(context, export)


def current_download() -> Optional[dict]:
    """This turn's export as a payload dict, or None if it touched no download."""
    notice = _current.get()

    return notice.payload if notice is not None else None


def _notice_for(context: Any, export: Any) -> dict:
    """
    One export as the widget reads it.

    ``download_url`` is present only for a ready export, matching
    ``DownloadExportView``: a link offered before the artifact exists is a link that
    404s. ``progress_url`` is always present, because "not ready yet" is exactly when
    watching it is worth doing.
    """
    from app.services.downloader_agents.base import download_service as svc

    if getattr(context, "is_visitor", False):
        scope = (export.uuid, context.chatbot_key_uuid, context.session_token)
        progress_url = svc.visitor_progress_url(*scope)
        status_url = svc.visitor_status_url(*scope)
    else:
        progress_url = svc.console_progress_url(export.uuid)
        status_url = svc.console_status_url(export.uuid)

    return DownloadNoticeView.of(
        export,
        download_url=(
            context.download_url(export) if export.status == EXPORT_READY else None
        ),
        progress_url=progress_url,
        status_url=status_url,
    ).payload()
