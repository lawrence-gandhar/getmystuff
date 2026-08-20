"""
Reading a response, and the three things that are refused rather than tolerated.

**An oversized body raises instead of being truncated.** ``chatbot_action_service`` caps
at 256 KiB and keeps the first 256 KiB, which is right there — the result is shown to a
person, who can see it was cut off. Here the result is *parsed*, and a truncated JSON
document is invalid JSON at best and a silently short record list at worst. "We synced
the first 4,000 of 12,000 orders" reported as success is the outcome this whole module
is arranged to prevent.

**A 2xx that is not JSON is a permanent failure naming the content type.** A WAF
challenge page, an expired-session redirect rendered as HTML and a maintenance notice all
arrive as ``200 text/html``. Parsing one as an empty list reports "0 records synced" as
success, and nobody investigates a green run.

**An error body is read, redacted and kept.** The destination's own message is almost
always more specific than anything we could compose about it — "email has already been
taken" versus "the write failed" — so it goes on the step row and into the record's
``message``. It is redacted first, because an error body echoing the request can contain
the ``Authorization`` header that produced it.
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional

from app.services.integrations.engine.flow_state import redact
from app.services.integrations.errors import NodeFailure

logger = logging.getLogger(__name__)


#: The most one response may be. Larger than the chatbot's cap because a page of 500
#: records with thirty fields each is legitimately over a megabyte, and smaller than any
#: amount that would threaten a worker holding several concurrently.
MAX_RESPONSE_BYTES = 8 * 1024 * 1024

#: How much of an error body to keep. Enough to hold a vendor's validation message and
#: not enough to put a rendered HTML page in a log row.
MAX_ERROR_TEXT = 2000

_JSON_TYPES = ("application/json", "+json", "text/json")


@dataclass(frozen=True)
class ReadResponse:
    """What one call came back with, once it is safe to keep."""

    status_code: int
    headers: Mapping[str, str] = field(default_factory=dict)
    payload: Any = None

    #: The body as text, redacted and capped, for a non-JSON or error response. Empty
    #: when ``payload`` holds the parsed document.
    text: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300


async def read_json(response: Any, *, max_bytes: int = MAX_RESPONSE_BYTES) -> ReadResponse:
    """
    Stream a response, refuse it if it is too large, and parse it.

    Streamed rather than read whole so the cap is enforced *before* the bytes are in
    memory. Reading first and checking the length afterwards is not a cap; it is a
    report of how much was already allocated.
    """
    raw = await _read_capped(response, max_bytes)
    status = int(getattr(response, "status_code", 0))
    headers = dict(getattr(response, "headers", {}) or {})

    if not raw:
        # 204 No Content, and the several APIs that return an empty body for a
        # successful delete. Not an error and not a record.
        return ReadResponse(status_code=status, headers=headers, payload=None)

    content_type = str(headers.get("content-type") or headers.get("Content-Type") or "")

    if not _is_json(content_type):
        return _refuse_or_keep(status, headers, raw, content_type)

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NodeFailure(
            f"This API said it was sending JSON and sent something that is not "
            f"({exc}). The first part of it was: {_snippet(raw)}",
            permanent=True,
            status_code=status,
        ) from exc

    return ReadResponse(status_code=status, headers=headers, payload=payload)


async def _read_capped(response: Any, max_bytes: int) -> bytes:
    chunks = []
    total = 0

    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > max_bytes:
            raise NodeFailure(
                f"This API sent more than {max_bytes // (1024 * 1024)} MB in one "
                "response, which is more than can be read at once. Reduce the batch "
                "size on this step so it asks for fewer records at a time.",
                permanent=True,
            )
        chunks.append(chunk)

    return b"".join(chunks)


def _is_json(content_type: str) -> bool:
    lowered = content_type.lower()
    return any(marker in lowered for marker in _JSON_TYPES)


def _refuse_or_keep(
    status: int, headers: Mapping[str, str], raw: bytes, content_type: str
) -> ReadResponse:
    """
    A non-JSON body: refused on success, kept on failure.

    The asymmetry is the point. On a 2xx we asked for records and got a document — that
    is a failure however cheerful the status line is. On a 4xx or 5xx the body is the
    explanation, whatever its type, and throwing it away would leave the operator with a
    status code and nothing else.
    """
    text = _snippet(raw)

    if 200 <= status < 300:
        raise NodeFailure(
            f"This API returned '{content_type or 'no content type'}' rather than JSON, "
            "so there are no records in it. That usually means a sign-in page, a "
            "security check or a maintenance notice was returned instead of the data. "
            f"It began: {text}",
            permanent=True,
            status_code=status,
        )

    return ReadResponse(status_code=status, headers=dict(headers), text=text)


def _snippet(raw: bytes) -> str:
    """
    The start of a body, decoded leniently and capped.

    ``errors="replace"`` because this is going into a message a person reads, and
    failing to decode an error body would replace the explanation with an explanation of
    why there is no explanation.
    """
    return raw[:MAX_ERROR_TEXT].decode("utf-8", errors="replace").strip()


def failure_message(response: ReadResponse, *, label: str) -> str:
    """
    One sentence for a response that did not succeed.

    Prefers the destination's own message where it sent one, because "email has already
    been taken" is worth more than "the write failed" — and a vendor's validation text is
    the thing the operator has to act on.
    """
    detail = vendor_message(response)
    reason = _reason(response.status_code)

    if detail:
        return f"{label} was refused ({response.status_code} {reason}): {detail}"
    return f"{label} was refused with {response.status_code} {reason}."


def vendor_message(response: ReadResponse) -> str:
    """
    The destination's own explanation, redacted, or ``""``.

    Redacted before it is returned rather than by the caller: an error body frequently
    echoes the request that caused it, headers included, and this is the one function
    every error path goes through.
    """
    payload = response.payload
    if payload is None and response.text:
        return response.text[:MAX_ERROR_TEXT]

    cleaned = redact(payload)

    if isinstance(cleaned, Mapping):
        for key in ("message", "error_description", "error", "detail", "errors", "title"):
            found = cleaned.get(key)
            if isinstance(found, str) and found.strip():
                return found.strip()[:MAX_ERROR_TEXT]
            if found is not None:
                return json.dumps(found, default=str)[:MAX_ERROR_TEXT]

    if cleaned is None:
        return ""

    return json.dumps(cleaned, default=str)[:MAX_ERROR_TEXT]


def _reason(status: int) -> str:
    return _REASONS.get(status, "")


# Only the ones whose meaning changes what an operator should do. A generic table would
# be a dependency on `http.HTTPStatus` for strings nobody reads.
_REASONS: Dict[int, str] = {
    400: "the request was rejected",
    401: "not signed in",
    403: "not allowed",
    404: "not found",
    409: "it conflicts with something already there",
    413: "too much data",
    422: "the data was not accepted",
    429: "too many requests",
    500: "the other system had an error",
    502: "the other system is unreachable",
    503: "the other system is unavailable",
    504: "the other system timed out",
}


def retry_after_seconds(headers: Mapping[str, str]) -> Optional[float]:
    """
    ``Retry-After`` in seconds, when the server sent one as a number.

    The HTTP-date form is deliberately not parsed. It requires a clock comparison
    against a server whose clock we cannot see, and every API in scope sends the numeric
    form; guessing at a date would produce a wait that is wrong in whichever direction
    the clocks differ.
    """
    for name in ("retry-after", "Retry-After"):
        raw = headers.get(name)
        if raw is None:
            continue
        try:
            seconds = float(str(raw).strip())
        except (TypeError, ValueError):
            logger.debug("Ignoring non-numeric Retry-After: %r", raw)
            return None
        return max(0.0, seconds)

    return None
