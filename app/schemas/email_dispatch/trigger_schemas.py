"""
Request and view schemas for triggers.

**Two hidden JSON fields**, for the same reason the template's variables are one: the
recipient lists and the variable bindings are nested structures edited as rows, and one
field each means exactly one place to validate their shape.

The schema checks only that they *are* a JSON object. Which sources are permitted, whether
every required variable is bound, and whether a path parses are all
``trigger_service._validated_bindings`` — the service half, which is authoritative, because
all three are rules about the combination of a binding and the template it points at.

``kind`` is on the create request and **not** on the update one. Changing a trigger's kind is
refused outright — see ``trigger_service.update_trigger`` — and leaving the field off the
edit form is how that refusal is expressed at the schema layer rather than as a check that
has to remember to run.

**No view carries the signing secret**, except ``reveal_secret``, which the service
populates once at creation and rotation and never again. A test pins that
``TriggerView`` has no field named for the ciphertext column.
"""


from pydantic import Field

from app.schemas.base import (
    MAX_NAME_LENGTH,
    CheckboxBool,
    FormRequest,
    JsonObjectField,
    OptionalText,
    OptionalUUID,
    RequiredText,
    RequiredUUID,
    ResponseSchema,
)


class TriggerCreateRequest(FormRequest):
    """The Add-trigger form."""

    name: RequiredText = Field(title="Trigger name", max_length=MAX_NAME_LENGTH)
    kind: RequiredText = Field(title="Trigger type", max_length=16)
    template_id: RequiredUUID = Field(title="Template")
    smtp_config_id: RequiredUUID = Field(title="Send through")
    # Required for an event trigger, refused for a webhook one. Optional here because that
    # is a rule about the combination of two fields; the service decides.
    event_name: OptionalText = Field(default=None, title="Event", max_length=100)
    min_interval_seconds: OptionalText = Field(
        default=None, title="Minimum gap between firings", max_length=6
    )
    recipients_json: JsonObjectField = Field(
        default_factory=dict, title="Recipients"
    )
    bindings_json: JsonObjectField = Field(
        default_factory=dict, title="Variable bindings"
    )
    workspace_id: OptionalUUID = Field(default=None, title="Shared with workspace")


class TriggerUpdateRequest(FormRequest):
    """
    The Edit-trigger form.

    Deliberately not a subclass of the create request: it must not carry ``kind``. See the
    module docstring.
    """

    name: RequiredText = Field(title="Trigger name", max_length=MAX_NAME_LENGTH)
    template_id: RequiredUUID = Field(title="Template")
    smtp_config_id: RequiredUUID = Field(title="Send through")
    event_name: OptionalText = Field(default=None, title="Event", max_length=100)
    min_interval_seconds: OptionalText = Field(
        default=None, title="Minimum gap between firings", max_length=6
    )
    recipients_json: JsonObjectField = Field(
        default_factory=dict, title="Recipients"
    )
    bindings_json: JsonObjectField = Field(
        default_factory=dict, title="Variable bindings"
    )
    workspace_id: OptionalUUID = Field(default=None, title="Shared with workspace")


class TriggerSetEnabledRequest(FormRequest):
    """The on/off toggle."""

    is_enabled: CheckboxBool = Field(default=False, title="Enabled")


class TriggerView(ResponseSchema):
    """
    One trigger, as a template sees it.

    ``has_secret`` is a boolean. ``reveal_secret`` holds the plaintext for exactly one
    response — the one that just generated it — and is empty on every ordinary read, because
    it is not recoverable from the row.
    """

    uuid: str = Field(title="Trigger")
    name: str = Field(title="Name")
    kind: str = Field(title="Type")
    kind_label: str = Field(default="", title="Type")
    event_name: str = Field(default="", title="Event")
    event_label: str = Field(default="", title="Event")
    webhook_url: str = Field(default="", title="Webhook URL")
    has_secret: bool = Field(default=False, title="Secret set")
    reveal_secret: str = Field(default="", title="Signing secret")
    min_interval_seconds: int = Field(default=0, title="Minimum gap")
    recipients: dict = Field(default_factory=dict, title="Recipients")
    variable_bindings: dict = Field(default_factory=dict, title="Variable bindings")
    is_enabled: bool = Field(default=True, title="Enabled")
    template_name: str = Field(default="", title="Template")
    template_uuid: str = Field(default="", title="Template")
    smtp_name: str = Field(default="", title="Server")
    smtp_uuid: str = Field(default="", title="Server")
