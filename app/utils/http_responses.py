"""Helpers for building the small HTML fragments HTMX swaps into the page.

Routes return human-readable messages on failure rather than raw stack traces
or JSON payloads. Because those messages can embed values the user typed
(datasource name, host, database name), every message is HTML-escaped before it
reaches the browser.
"""

from html import escape

from litestar.response import Response

# Shown when we have nothing safe or useful to say about a failure.
GENERIC_ERROR_MESSAGE = (
    "Something went wrong while processing your request. "
    "Please try again, and contact support if the problem continues."
)


def html_error_response(detail: object, status_code: int = 400) -> Response:
    """Build a Bootstrap danger alert as an HTMX-swappable HTML response.

    Args:
        detail: The human-readable failure message. Coerced to ``str`` and
            HTML-escaped, so user-supplied values are safe to interpolate.
        status_code: HTTP status to preserve (400 / 409 / 422 / 5xx) so the
            client can distinguish a conflict from bad input.

    Returns:
        A ``Response`` carrying the alert markup as ``text/html``.
    """
    message = str(detail).strip() or GENERIC_ERROR_MESSAGE

    return Response(
        f"<div class='alert alert-danger'>{escape(message)}</div>",
        status_code=status_code,
        media_type="text/html",
    )


def html_success_response(message: str, status_code: int = 200) -> Response:
    """Build a Bootstrap success alert as an HTMX-swappable HTML response."""
    return Response(
        f"<div class='alert alert-success'>{escape(message)}</div>",
        status_code=status_code,
        media_type="text/html",
    )
