"""
Tests for app/schemas/ai_settings/ai_settings_schemas.py.

Two things here are worth more than the field rules.

**No view carries a secret.** The key value is the whole point of this module, and
a response schema is what puts data into a template context and from there into an
HTML page. There is a test that walks ``AIApiKeyView``'s fields rather than
checking a known set, so a future field named ``api_key`` fails it.

**A blank field on update means "unchanged", not "clear".** The edit form cannot
prefill the stored secret, so it renders an empty input. ``update_api_key``
distinguishes an absent field from a blank one (``if base_url is not None``), and
the update schema deliberately keeps ``""`` and ``None`` apart to preserve that.
Collapsing them would turn "clear this base URL" into a silent no-op.
"""

from __future__ import annotations

import pytest
from litestar.exceptions import HTTPException

from app.models.ai_settings import AI_PROVIDER_VALUES
from app.schemas.ai_settings import (
    MAX_API_KEY_LENGTH,
    AIApiKeyCreateRequest,
    AIApiKeyUpdateRequest,
    AIApiKeyView,
)


def _detail(schema, data: dict) -> str:
    with pytest.raises(HTTPException) as exc_info:
        schema.parse(data)
    return str(exc_info.value.detail)


def _valid() -> dict:
    return {"provider": "anthropic", "label": "Prod key", "api_key": "sk-abc"}


class TestCreate:
    def test_a_minimal_valid_form(self) -> None:
        payload = AIApiKeyCreateRequest.parse(_valid())
        assert payload.provider == "anthropic"
        assert payload.is_active is False
        assert payload.base_url is None
        assert payload.model_name is None

    @pytest.mark.parametrize("provider", sorted(AI_PROVIDER_VALUES))
    def test_every_provider_the_model_declares_is_accepted(self, provider: str) -> None:
        """
        The allowed set comes from ``AI_PROVIDERS`` in the model, so this passes
        for whatever is there rather than for a list copied into the test.
        """
        assert AIApiKeyCreateRequest.parse(
            {**_valid(), "provider": provider}
        ).provider == provider

    def test_an_unknown_provider_is_refused(self) -> None:
        assert _detail(AIApiKeyCreateRequest, {**_valid(), "provider": "acme_ai"}) == (
            "Provider is not one we support. Please pick one from the list."
        )

    def test_provider_is_required(self) -> None:
        data = _valid()
        del data["provider"]
        assert _detail(AIApiKeyCreateRequest, data) == "Provider is required"

    def test_the_key_is_required_on_create(self) -> None:
        data = _valid()
        del data["api_key"]
        assert _detail(AIApiKeyCreateRequest, data) == "API key is required"

    def test_the_key_is_bounded(self) -> None:
        """A paste of a whole file is refused here rather than stored and then
        failed against the provider's API."""
        at_cap = "k" * MAX_API_KEY_LENGTH
        assert AIApiKeyCreateRequest.parse({**_valid(), "api_key": at_cap}).api_key == at_cap
        assert "cannot be longer than 512" in _detail(
            AIApiKeyCreateRequest, {**_valid(), "api_key": "k" * (MAX_API_KEY_LENGTH + 1)}
        )

    @pytest.mark.parametrize(("raw", "expected"), [("on", True), (None, False)])
    def test_the_active_checkbox(self, raw, expected: bool) -> None:
        assert AIApiKeyCreateRequest.parse(
            {**_valid(), "is_active": raw}
        ).is_active is expected

    def test_a_blank_base_url_means_not_set(self) -> None:
        assert AIApiKeyCreateRequest.parse({**_valid(), "base_url": "  "}).base_url is None


class TestUpdatePreservesTheBlankVersusAbsentDistinction:
    """
    The single most important behaviour in this module's schemas, and the one a
    convenience type would have quietly broken.
    """

    def test_an_absent_field_is_none_meaning_leave_it_alone(self) -> None:
        payload = AIApiKeyUpdateRequest.parse({})
        assert payload.label is None
        assert payload.base_url is None
        assert payload.model_name is None

    def test_a_blank_base_url_stays_an_empty_string_meaning_clear_it(self) -> None:
        """
        ``update_api_key`` does ``if base_url is not None:
        data["base_url"] = base_url.strip() or None``. Normalizing ``""`` to
        ``None`` here would make clearing the field do nothing at all.
        """
        assert AIApiKeyUpdateRequest.parse({"base_url": ""}).base_url == ""

    def test_a_blank_model_name_stays_an_empty_string(self) -> None:
        assert AIApiKeyUpdateRequest.parse({"model_name": ""}).model_name == ""

    def test_a_blank_label_stays_an_empty_string_so_the_service_can_refuse_it(
        self,
    ) -> None:
        """
        The service raises "Label is required" for a blanked-out label. Reading it
        as absent would turn that validation error into a silent no-op.
        """
        assert AIApiKeyUpdateRequest.parse({"label": ""}).label == ""

    def test_a_blank_key_means_keep_the_stored_secret(self) -> None:
        """The edit form cannot prefill the secret, so empty must mean unchanged."""
        assert AIApiKeyUpdateRequest.parse({"api_key": ""}).api_key == ""

    def test_the_provider_cannot_be_changed(self) -> None:
        """A key belongs to the provider that issued it."""
        assert "provider" not in AIApiKeyUpdateRequest.model_fields

    def test_lengths_are_still_enforced(self) -> None:
        assert "cannot be longer than 255" in _detail(
            AIApiKeyUpdateRequest, {"label": "L" * 256}
        )


class TestViewCarriesNoSecret:
    _SECRET_HINTS = ("api_key", "key_encrypted", "secret", "password", "token")

    def test_no_field_could_hold_the_key_value(self) -> None:
        """
        Walks the declared fields rather than asserting a known set, so a future
        field named ``api_key`` fails this test instead of shipping.

        ``masked_key`` is allowed: it holds whatever the service chose to display,
        never the key.
        """
        offenders = [
            name
            for name in AIApiKeyView.model_fields
            if name != "masked_key"
            and any(hint in name for hint in self._SECRET_HINTS)
        ]
        assert offenders == []

    def test_a_secret_passed_in_is_not_carried_out(self) -> None:
        payload = AIApiKeyView.payload_for(
            {
                "id": 4,
                "uuid": "u-1",
                "label": "Prod",
                "provider": "anthropic",
                "api_key_encrypted": "gAAAA-secret",
            }
        )
        assert "api_key_encrypted" not in payload
        assert "id" not in payload
        assert payload["uuid"] == "u-1"
