"""
The SMTP server schemas, and the guard that no view carries a secret.

That guard is the load-bearing test of the whole email schema layer. The module keeps
``password_encrypted`` and ``webhook_secret_encrypted`` on ordinary rows rather than in a
separate credentials table, so what stops one reaching a response is that every view names
its fields explicitly — and that is only a safeguard if something checks it. Otherwise adding
a column to the model and a field to the view is two easy edits away from putting a password
in the DOM.
"""

from __future__ import annotations

import uuid

import pytest
from litestar.exceptions import HTTPException

from app.schemas.email_dispatch import (
    AttemptView,
    MessageView,
    SmtpChoiceView,
    SmtpConfigCreateRequest,
    SmtpConfigUpdateRequest,
    SmtpConfigView,
    TemplateView,
    TriggerView,
)

SECRET_ISH = ("password", "secret", "encrypted", "token")

ALL_VIEWS = (
    SmtpConfigView,
    SmtpChoiceView,
    TemplateView,
    TriggerView,
    MessageView,
    AttemptView,
)


class TestNoViewLeaksASecret:
    @pytest.mark.parametrize("view", ALL_VIEWS, ids=lambda v: v.__name__)
    def test_no_field_looks_like_a_stored_secret(self, view):  # noqa: ANN001
        """
        The safeguard that replaces a separate credentials table.

        ``reveal_secret`` on ``TriggerView`` is the one deliberate exception and is allowed by
        name: the service fills it exactly once, at creation and rotation, from a value it
        just generated — never read back off the row.
        """
        offenders = [
            name
            for name in view.model_fields
            if any(word in name.lower() for word in SECRET_ISH)
            and name not in {"has_password", "has_secret", "reveal_secret"}
        ]
        assert not offenders, (
            f"{view.__name__} exposes {offenders}. A secret must never reach a response — "
            "expose a boolean saying one is stored instead."
        )

    def test_the_smtp_view_says_whether_a_password_is_stored_not_what_it_is(self):
        assert "has_password" in SmtpConfigView.model_fields
        assert "password" not in SmtpConfigView.model_fields

    def test_the_trigger_view_says_whether_a_secret_is_set(self):
        assert "has_secret" in TriggerView.model_fields
        assert "webhook_secret_encrypted" not in TriggerView.model_fields


class TestSmtpRequests:
    def test_a_create_trims_and_keeps_what_was_typed(self):
        payload = SmtpConfigCreateRequest.parse(
            {
                "name": "  Relay  ",
                "host": " smtp.example.com ",
                "port": "587",
                "security": "starttls",
                "from_email": "alerts@example.com",
            }
        )
        assert payload.name == "Relay"
        assert payload.host == "smtp.example.com"
        # Kept as text: the service's "must be a whole number, such as 587" is a better
        # sentence than Pydantic's, so the parse is deliberately deferred to it.
        assert payload.port == "587"
        assert payload.workspace_id is None

    def test_an_omitted_optional_becomes_none_rather_than_empty_string(self):
        payload = SmtpConfigCreateRequest.parse(
            {
                "name": "Relay",
                "host": "smtp.example.com",
                "port": "587",
                "security": "starttls",
                "from_email": "a@b.com",
                "from_name": "",
            }
        )
        assert payload.from_name is None

    def test_a_missing_required_field_is_a_400_not_a_validation_error(self):
        """SCHEMAS.md rule 1: a validation failure reaches a route as an HTTPException, never
        as Pydantic's own exception."""
        with pytest.raises(HTTPException) as caught:
            SmtpConfigCreateRequest.parse({"host": "smtp.example.com"})
        assert caught.value.status_code == 400

    def test_only_the_update_form_can_clear_the_password(self):
        """Blank means "leave it", so removal needs its own explicit signal — and it belongs
        on the edit form only, where there is something to remove."""
        assert "clear_password" in SmtpConfigUpdateRequest.model_fields
        assert "clear_password" not in SmtpConfigCreateRequest.model_fields

    def test_clear_password_is_false_unless_the_box_was_ticked(self):
        payload = SmtpConfigUpdateRequest.parse(
            {
                "name": "Relay",
                "host": "smtp.example.com",
                "port": "587",
                "security": "starttls",
                "from_email": "a@b.com",
            }
        )
        assert payload.clear_password is False
