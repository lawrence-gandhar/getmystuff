"""
app/schemas/ai_settings/ai_settings_schemas.py

Pydantic schemas for AI Settings — the user's own provider API keys.

The secret is the whole point of this module, and it shapes two decisions here:

**There is no response schema holding a key.** ``AIApiKeyView`` exposes the label,
the provider and the active flag; the key value itself never leaves the service
layer, and adding it to a view schema would put it in a template context and from
there into an HTML page.

**Update treats a blank key as "leave it alone".** The edit form cannot prefill the
existing secret, so it renders an empty field — and an empty field has to mean "I
did not change it", not "clear it". That is why ``api_key`` is optional on update
and required on create, and it is the single most important difference between the
two schemas.

Which providers exist, and the rule that only one key per provider may be active,
stay where they were: ``AI_PROVIDERS`` in the model, the activation rule in
`ai_settings_service` — it deactivates siblings, which needs the database.
"""

from typing import Optional

from pydantic import Field, field_validator

from app.schemas.base import (
    MAX_NAME_LENGTH,
    MAX_URL_LENGTH,
    CheckboxBool,
    FormRequest,
    OptionalText,
    RequiredText,
    ResponseSchema,
)
from app.models.ai_settings import AI_PROVIDER_VALUES

#: A provider key is long, but not unbounded — a paste of a whole file into the
#: field should be refused rather than stored and then failed against the API.
MAX_API_KEY_LENGTH = 512


class AIApiKeyCreateRequest(FormRequest):
    """
    The add-key form.

    ``base_url`` and ``model_name`` are optional because only some providers need
    them — Azure OpenAI needs a deployment URL, a self-hosted gateway needs both,
    and Anthropic needs neither.
    """

    provider: RequiredText = Field(title="Provider")
    label: RequiredText = Field(title="Label", max_length=MAX_NAME_LENGTH)
    api_key: RequiredText = Field(title="API key", max_length=MAX_API_KEY_LENGTH)
    is_active: CheckboxBool = Field(default=False, title="Active")
    base_url: OptionalText = Field(default=None, title="Base URL", max_length=MAX_URL_LENGTH)
    model_name: OptionalText = Field(
        default=None, title="Model name", max_length=MAX_NAME_LENGTH
    )

    @field_validator("provider")
    @classmethod
    def validate_provider(cls, v: str) -> str:
        if v not in AI_PROVIDER_VALUES:
            raise ValueError("Provider is not one we support. Please pick one from the list.")
        return v


class AIApiKeyUpdateRequest(FormRequest):
    """
    The edit-key form. The provider is absent by design — a key belongs to the
    provider that issued it, so it cannot be changed.

    These fields deliberately use plain ``Optional[str]`` rather than
    ``OptionalText``, because here ``""`` and ``None`` mean different things and
    ``OptionalText`` would collapse them:

    * **absent** (``None``) — the field was not submitted; leave the column alone.
    * **empty** (``""``)   — the field was submitted blank; that is a request to
      *clear* ``base_url`` / ``model_name``, and an error for ``label``.

    ``update_api_key`` implements exactly that distinction (``if base_url is not
    None: data["base_url"] = base_url.strip() or None``). Normalizing ``""`` to
    ``None`` here would silently turn "clear this" into "change nothing" — and turn
    a blanked-out label from a validation error into a no-op.
    """

    label: Optional[str] = Field(default=None, title="Label", max_length=MAX_NAME_LENGTH)
    api_key: Optional[str] = Field(
        default=None, title="API key", max_length=MAX_API_KEY_LENGTH
    )
    base_url: Optional[str] = Field(
        default=None, title="Base URL", max_length=MAX_URL_LENGTH
    )
    model_name: Optional[str] = Field(
        default=None, title="Model name", max_length=MAX_NAME_LENGTH
    )


class AIApiKeyView(ResponseSchema):
    """
    One row of the keys table. Deliberately holds no secret.

    ``provider_display`` is the human name ("Anthropic (Claude)") the model
    resolves from the stored value; ``masked_key`` is whatever the service chose to
    show, never the key itself.
    """

    uuid: str = Field(title="Key")
    label: str = Field(title="Label")
    provider: str = Field(title="Provider")
    provider_display: str = Field(default="", title="Provider name")
    masked_key: str = Field(default="", title="Key")
    base_url: Optional[str] = Field(default=None, title="Base URL")
    model_name: Optional[str] = Field(default=None, title="Model name")
    is_active: bool = Field(default=False, title="Active")
