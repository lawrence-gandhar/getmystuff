"""
Request and view schemas for SMTP servers.

**No view here carries a secret, and that is asserted by a test rather than trusted.**
``SmtpConfigView`` names every field explicitly and ``password_encrypted`` is not among
them, so adding a secret column to the model cannot leak it through a response — a new
column is absent from a schema until somebody adds it deliberately.

**The password field is write-only, and blank means "leave it".** The form posts an empty
credential box on every edit of an unrelated field, so blank cannot mean "clear it" or a
routine save would wipe the credentials. ``clear_password`` is the separate, explicit
signal for removal. See ``smtp_service.update_config``.

Schemas bound the shape; the service owns the rules. Whether a port is in range and whether
plaintext-plus-a-password is allowed both live in ``smtp_service``, because the second is a
rule about the *combination* of two fields and the first has a sentence to say about the
number. ``SCHEMAS.md`` rule 3: where a check is split, the service half is authoritative.
"""

from typing import Optional

from pydantic import Field

from app.schemas.base import (
    MAX_NAME_LENGTH,
    CheckboxBool,
    FormRequest,
    OptionalText,
    OptionalUUID,
    RequiredText,
    ResponseSchema,
)

#: Longest an email address may be. RFC 5321's ceiling, and the column width.
MAX_ADDRESS_LENGTH = 320


class SmtpConfigCreateRequest(FormRequest):
    """The Add-server form."""

    name: RequiredText = Field(title="Server name", max_length=MAX_NAME_LENGTH)
    host: RequiredText = Field(title="Host", max_length=255)
    # Not `int`: the form posts a string and Pydantic's own coercion error ("Input should
    # be a valid integer") is less useful than the service's "The port must be a whole
    # number, such as 587." Kept as text here and parsed there, where the sentence lives.
    port: RequiredText = Field(title="Port", max_length=5)
    security: RequiredText = Field(title="Connection security", max_length=16)
    from_email: RequiredText = Field(title="From address", max_length=MAX_ADDRESS_LENGTH)
    from_name: OptionalText = Field(
        default=None, title="From name", max_length=MAX_NAME_LENGTH
    )
    reply_to: OptionalText = Field(
        default=None, title="Reply-To address", max_length=MAX_ADDRESS_LENGTH
    )
    username: OptionalText = Field(
        default=None, title="Username", max_length=MAX_NAME_LENGTH
    )
    password: OptionalText = Field(default=None, title="Password", max_length=1024)
    timeout_seconds: OptionalText = Field(default=None, title="Timeout", max_length=4)
    workspace_id: OptionalUUID = Field(default=None, title="Shared with workspace")


class SmtpConfigUpdateRequest(SmtpConfigCreateRequest):
    """
    The Edit-server form.

    Adds the one field that only makes sense on an edit. See the module docstring on why
    removal needs its own flag rather than reusing a blank password.
    """

    clear_password: CheckboxBool = Field(
        default=False, title="Remove the stored password"
    )


class SmtpSetActiveRequest(FormRequest):
    """The on/off toggle."""

    is_active: CheckboxBool = Field(default=False, title="Active")


class SmtpConfigView(ResponseSchema):
    """
    One server, as a template or a JSON caller sees it.

    ``has_password`` is a boolean, never the value — not even masked. A masked secret in a
    response is still a secret in the DOM.
    """

    uuid: str = Field(title="Server")
    name: str = Field(title="Name")
    host: str = Field(title="Host")
    port: int = Field(title="Port")
    security: str = Field(title="Security")
    security_label: str = Field(default="", title="Security")
    username: str = Field(default="", title="Username")
    has_password: bool = Field(default=False, title="Password stored")
    from_email: str = Field(title="From address")
    from_name: str = Field(default="", title="From name")
    reply_to: str = Field(default="", title="Reply-To")
    timeout_seconds: int = Field(default=30, title="Timeout")
    is_active: bool = Field(default=True, title="Active")
    last_test_ok: Optional[bool] = Field(default=None, title="Last test")
    last_test_message: str = Field(default="", title="Last test result")


class SmtpChoiceView(ResponseSchema):
    """
    One server as a node's dropdown entry.

    ``disabled_reason`` is empty for a usable server and a sentence for one that is not.
    Unavailable options are **offered and flagged, not hidden** — the house rule, so a node
    already pointing at a switched-off server stays editable.
    """

    uuid: str = Field(title="Server")
    label: str = Field(title="Name")
    detail: str = Field(default="", title="Details")
    disabled_reason: str = Field(default="", title="Unavailable because")
