"""
Request and view schemas for the delivery log.

``MessageFilterRequest`` is a ``QueryRequest``, so **every field must have a default** —
the log page is reachable with no query string at all, and a required filter would make the
bare URL a 400.

``MessageView`` is the log row. It carries the *rendered* subject and the denormalised
template and server names rather than following the foreign keys, which is what lets the log
still read correctly after a template has been deleted — the property the whole
render-at-enqueue decision exists to buy.
"""

from typing import Optional

from pydantic import Field

from app.schemas.base import (
    FormRequest,
    OptionalText,
    QueryRequest,
    RequiredText,
    RequiredUUID,
    ResponseSchema,
)

#: Rows per page of the log. Small enough to render fast on a table that only grows.
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200


class MessageFilterRequest(QueryRequest):
    """
    The log page's filters.

    ``status`` and ``source`` are validated against the vocabularies in the service rather
    than here: an unknown value has a sentence to say, and the frozensets live in the model
    file where the chips and the dispatcher also read them.
    """

    status: OptionalText = Field(default=None, title="Status")
    source: OptionalText = Field(default=None, title="Source")
    search: OptionalText = Field(default=None, title="Search", max_length=255)
    page: OptionalText = Field(default=None, title="Page", max_length=6)


class SendTestRequest(FormRequest):
    """
    The Send-test form on the delivery log.

    Small enough to have been read straight off the request, and it was, until the schema
    audit pointed out that "small enough" is how a route ends up with its own private idea of
    what a valid payload is. The two uuids are parsed here rather than in the handler, so a
    mistyped one is a 400 with a sentence instead of a ``ValueError`` the route has to catch
    alongside its ``HTTPException``.
    """

    template_id: RequiredUUID = Field(title="Template")
    smtp_config_id: RequiredUUID = Field(title="Send through")
    to_address: RequiredText = Field(title="To", max_length=320)


class MessageView(ResponseSchema):
    """
    One row of the delivery log.

    No body fields. The list renders subject, recipients and outcome; the full HTML goes in
    the detail view, because putting a 30 kB body in every row of a fifty-row table makes
    the page unreadable and slow at the same time.
    """

    uuid: str = Field(title="Email")
    subject: str = Field(title="Subject")
    to_addresses: list = Field(default_factory=list, title="To")
    cc_addresses: list = Field(default_factory=list, title="Cc")
    bcc_addresses: list = Field(default_factory=list, title="Bcc")
    from_email: str = Field(default="", title="From")
    status: str = Field(title="Status")
    status_label: str = Field(default="", title="Status")
    source: str = Field(default="", title="Source")
    source_label: str = Field(default="", title="Source")
    source_ref: str = Field(default="", title="Came from")
    template_name: str = Field(default="", title="Template")
    smtp_host: str = Field(default="", title="Server")
    attempt: int = Field(default=0, title="Attempts")
    max_attempts: int = Field(default=0, title="Attempt limit")
    error_message: str = Field(default="", title="Error")
    smtp_response: str = Field(default="", title="Server response")
    can_retry: bool = Field(default=False, title="Can be retried")
    can_cancel: bool = Field(default=False, title="Can be cancelled")


class MessageDetailView(MessageView):
    """The detail pane: the log row plus what was actually sent and every attempt."""

    body_html: str = Field(default="", title="Message body")
    body_text: str = Field(default="", title="Plain-text body")
    attempts: list = Field(default_factory=list, title="Attempts")


class AttemptView(ResponseSchema):
    """One try at sending, for the detail pane's timeline."""

    uuid: str = Field(title="Attempt")
    attempt: int = Field(title="Attempt number")
    status: str = Field(title="Outcome")
    error_message: str = Field(default="", title="Error")
    smtp_response: str = Field(default="", title="Server response")
    retryable: bool = Field(default=False, title="Worth retrying")
    duration_ms: Optional[int] = Field(default=None, title="Took")
    worker: str = Field(default="", title="Sent by")
