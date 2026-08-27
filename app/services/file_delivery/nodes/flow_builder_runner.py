"""
The Create File and Download File blocks' behaviour inside a chatbot conversation.

**Neither block says anything to the visitor by itself.** Create File writes a file and
hops on; Download File hops on too, unless the operator ticked *show a download button*,
which is the one thing on either block that produces something a visitor sees. That is the
Email node's rule and it is here for the same reason: a block that announced "I have made
your file" would be putting words in the operator's mouth. If they want the visitor told,
that is a Send Message block next to this one, which they wrote.

**What each block leaves behind is a variable.** Create File stores the file's *path*
under its variable name and Download File stores the *link* under its own, so an operator
can say ``Your file is ready: {{FILE_URL}}`` in their own words, or bind ``{{LINK}}`` into
an Email node and mail it. Two variables rather than one because they are for two
different audiences — the path is an operator-facing fact for a log or a later step, the
link is the thing you give a person.

**Download File names its Create File block; it does not look at the wire.** An operator
can put a Send Message between the two, and a named reference survives that while "the
block wired into me" does not. It is the arrangement a Timer node's ``timer_node`` already
uses on the graph canvas.

**A file is scoped to the conversation that made it, in the database and in the URL.** The
row carries the widget key and the session token, the public route requires both, and
``file_service.visitor_file`` additionally refuses any file that did not come from a flow.
A visitor of a public widget who guesses another file's uuid gets a 404, which is the
whole reason the link is not a static path.
"""

import logging
import re
from typing import Any, Dict, Mapping

from app.models.file_delivery import (
    FILE_FORMAT_VALUES,
    ORIGIN_FLOW,
    SOURCE_BLOCK,
    SOURCE_VARIABLE,
)
from app.services.file_delivery import file_service, row_source
from app.services.file_delivery.errors import FileFailure, SourceError

logger = logging.getLogger(__name__)

#: What a Create File block in a *flow* may read its rows from. A conversation has the
#: results of blocks that have already run and its own variables, and nothing else — there
#: are no upstream node outputs here, because this engine's state is one flat string map
#: plus the block-results record beside it.
FLOW_DATA_SOURCES = frozenset({SOURCE_BLOCK, SOURCE_VARIABLE})

#: The default button colour, used when the operator ticked the button and left the colour
#: alone. Bootstrap's primary, which is what every other button in this application is, so
#: an untouched button looks deliberate rather than like a missing setting.
DEFAULT_BUTTON_COLOUR = "#0d6efd"

#: What the button says when its text was left empty. Not a refusal: a button with no
#: label is a bug, and this feature's job is to hand somebody a file, not to argue about
#: the wording.
DEFAULT_BUTTON_LABEL = "Download file"


async def run_create_file_node(
    db,
    node: dict,
    *,
    chatbot_key,
    session,
) -> Dict[str, Any]:
    """
    Write the file this block describes. Returns what was written.

    Raises :class:`FileFailure` for anything the caller should route down ``error``. Takes
    the caller's session — a chat turn has one — so the row lands in the same transaction
    as the conversation's own variable updates and the turn stays atomic.

    The file name is interpolated by the *caller* before it arrives here, so
    ``orders-{{DATE}}`` is already ``orders-2026-08-26``. This module does not know about
    ``{{}}``: text interpolation is the engine's job and it has three deliberately
    different sets of semantics for it, none of which should be re-implemented here.
    """
    data = node.get("data") or {}
    node_id = str(node.get("id") or "")
    label = _label(node)

    file_format = str(data.get("file_format") or "").strip().lower()

    if file_format not in FILE_FORMAT_VALUES:
        raise SourceError(
            f"'{label}' has no file format chosen.", block=label,
        )

    payload = await row_source.resolve_flow_data(
        db,
        user_id=int(chatbot_key.user_id),
        node_results=session.node_results or {},
        variables=session.variables or {},
        data=data.get("data") or {},
        block_label=label,
    )

    record = await file_service.create_file(
        db,
        user_id=int(chatbot_key.user_id),
        payload=payload,
        file_format=file_format,
        name_stem=str(data.get("file_name") or ""),
        origin=ORIGIN_FLOW,
        chatbot_key_id=int(chatbot_key.id),
        session_token=session.session_token,
        # The session token rather than the flow's id, for the reason the Email node's
        # `source_ref` gives: when somebody asks "where did this file come from", the
        # answer people mean is which conversation.
        source_ref=f"chat {session.session_token}",
        node_id=node_id,
    )

    logger.info(
        "Flow Create File block %s wrote %s (%d row(s), %d bytes) from %s",
        node_id,
        record.file_name,
        record.row_count,
        record.byte_size,
        payload.description,
    )

    return {
        "file_uuid": str(record.uuid),
        "file_name": record.file_name,
        "file_path": record.file_path,
        "file_format": record.file_format,
        "row_count": record.row_count,
        "byte_size": record.byte_size,
    }


async def run_download_file_node(
    db,
    node: dict,
    *,
    chatbot_key,
    session,
    file_uuid: str,
) -> Dict[str, Any]:
    """
    Turn a file this conversation made into a link, and optionally a button.

    ``file_uuid`` is what the named Create File block recorded — the caller reads it out of
    the session, because the caller is the one that knows which block was named and what
    that block left behind.

    The file is resolved through :func:`file_service.visitor_file` rather than trusted from
    the session, which is not paranoia about our own data: a file's window can close
    between the turn that made it and the turn that offers it, and a button linking to a
    lapsed file is worse than no button. Resolving it here means the block fails and takes
    its ``error`` port instead.
    """
    data = node.get("data") or {}
    label = _label(node)

    record = await file_service.visitor_file(
        db, int(chatbot_key.id), session.session_token, file_uuid,
    )

    if file_service.is_expired(record):
        raise SourceError(
            f"The file '{label}' offers has expired, so there is nothing to hand over. "
            f"Files last {file_service.ttl_phrase()} — offer it in the same "
            "conversation that makes it.",
            block=label,
        )

    url = file_service.visitor_download_url(
        record.uuid, chatbot_key.uuid, session.session_token,
    )

    return {
        "url": url,
        "file_uuid": str(record.uuid),
        "file_name": record.file_name,
        "file_format": record.file_format,
        "byte_size": record.byte_size,
        "row_count": record.row_count,
        "button": _button_payload(data, url, record) if _wants_button(data) else None,
    }


def _wants_button(data: Mapping[str, Any]) -> bool:
    """Whether the operator asked for a button. Absent means no — a block shows nothing
    until somebody says it should."""
    return bool(data.get("show_button"))


def _button_payload(
    data: Mapping[str, Any], url: str, record,  # noqa: ANN001 — GeneratedFile
) -> Dict[str, Any]:
    """
    The button as the widget reads it.

    The colour is passed through ``_safe_colour``, which is not decoration: this value
    lands in an inline ``style`` attribute on somebody else's page. The save-time validator
    refuses anything that is not ``#rrggbb``, and this is the second gate — a node saved by
    an older version, or edited in the database, must not be able to put arbitrary text
    into a style.
    """
    return {
        "label": str(data.get("button_text") or "").strip() or DEFAULT_BUTTON_LABEL,
        "colour": _safe_colour(data.get("button_colour")),
        "url": url,
        "file_name": record.file_name,
        "file_format": record.file_format,
        "byte_size": record.byte_size,
    }


#: A hex colour and nothing else. Both the save-time validators and the runner check
#: against this one pattern, so what an operator is allowed to type and what can reach a
#: style attribute cannot drift apart.
COLOUR_PATTERN = re.compile(r"#[0-9a-fA-F]{6}")


def _safe_colour(value: Any) -> str:
    """A ``#rrggbb`` colour, or the default. Never anything else."""
    text = str(value or "").strip()

    return text if COLOUR_PATTERN.fullmatch(text) else DEFAULT_BUTTON_COLOUR


def _label(node: Mapping[str, Any]) -> str:
    """
    What to call this block in a message to the operator.

    The block's own label if it has one, else its type, else its id. A sentence naming
    "n7" is worth less than one naming "Make the CSV", and a flow's blocks carry a label
    the canvas shows.
    """
    data = node.get("data") or {}

    return (
        str(data.get("label") or "").strip()
        or str(node.get("label") or "").strip()
        or str(node.get("type") or "").replace("_", " ").strip()
        or str(node.get("id") or "this block")
    )


def wrap_failure(exc: Exception) -> str:
    """
    One failure as the sentence the log and the operator's canvas get.

    A :class:`FileFailure` already carries an operator-facing sentence, so it is used as
    written. Anything else is reduced to a fixed line — an unplanned exception's text is
    written for a developer, and a flow must never put one in front of anybody.
    """
    if isinstance(exc, FileFailure):
        return exc.message

    return "The file could not be created. Please try again."
