"""
Request and view schemas for email templates.

**The declared variable list arrives as one hidden JSON field**, not as repeated form
inputs. Straight from the Agents section's ``variables_json``, and for the stated reason:
one field means exactly one place to validate its shape. Repeated inputs would need the
route to zip three parallel arrays and to decide what a mismatched length meant.

The schema only checks that the field *is* a JSON array. Whether the names are legal,
whether there are too many, and whether the bodies reference something undeclared are all
``rendering.parse_declaration`` and ``rendering.assert_declared`` — the service half, which
is authoritative. A schema that also knew the naming rule would be a second copy of it.
"""

from pydantic import Field

from app.schemas.base import (
    MAX_DESCRIPTION_LENGTH,
    MAX_NAME_LENGTH,
    CheckboxBool,
    FormRequest,
    JsonArrayField,
    OptionalText,
    OptionalUUID,
    RequiredText,
    ResponseSchema,
)

#: RFC 5322's line-length ceiling for a header. A longer subject is folded or truncated by
#: the receiving server, so it is refused here rather than silently mangled in transit.
MAX_SUBJECT_LENGTH = 998

#: Longest a body may be. Generous — an HTML email with inline styles is easily 30 kB — but
#: bounded, because the text is copied onto every message row that uses it.
MAX_BODY_LENGTH = 200_000


class TemplateCreateRequest(FormRequest):
    """The Add-template form."""

    name: RequiredText = Field(title="Template name", max_length=MAX_NAME_LENGTH)
    description: OptionalText = Field(
        default=None, title="Description", max_length=MAX_DESCRIPTION_LENGTH
    )
    subject_template: RequiredText = Field(
        title="Subject", max_length=MAX_SUBJECT_LENGTH
    )
    body_html_template: RequiredText = Field(
        title="Message body", max_length=MAX_BODY_LENGTH
    )
    body_text_template: OptionalText = Field(
        default=None, title="Plain-text body", max_length=MAX_BODY_LENGTH
    )
    # The hidden field the variable-row editor serialises into. Defaults to an empty list
    # so a template with no variables posts cleanly rather than 400-ing on a missing field.
    variables_json: JsonArrayField = Field(
        default_factory=list, title="Variables"
    )
    workspace_id: OptionalUUID = Field(default=None, title="Shared with workspace")


class TemplateUpdateRequest(TemplateCreateRequest):
    """The Edit-template form. Identical shape — kept as its own name so a future
    edit-only field has somewhere to go without changing the create form."""


class TemplateSetActiveRequest(FormRequest):
    """The on/off toggle."""

    is_active: CheckboxBool = Field(default=False, title="Active")


class TemplateView(ResponseSchema):
    """One template, as a template or a JSON caller sees it."""

    uuid: str = Field(title="Template")
    name: str = Field(title="Name")
    description: str = Field(default="", title="Description")
    subject_template: str = Field(title="Subject")
    body_html_template: str = Field(title="Message body")
    body_text_template: str = Field(default="", title="Plain-text body")
    variables: list = Field(default_factory=list, title="Variables")
    variable_names: list = Field(default_factory=list, title="Variable names")
    is_active: bool = Field(default=True, title="Active")


class TemplateChoiceView(ResponseSchema):
    """
    One template as a node's dropdown entry.

    ``variables`` rides along deliberately: a node's property panel has to draw one binding
    row per declared variable the instant a template is picked, and a second round trip
    would let an operator save the node before its bindings had loaded.
    """

    uuid: str = Field(title="Template")
    label: str = Field(title="Name")
    detail: str = Field(default="", title="Subject")
    disabled_reason: str = Field(default="", title="Unavailable because")
    variables: list = Field(default_factory=list, title="Variables")
