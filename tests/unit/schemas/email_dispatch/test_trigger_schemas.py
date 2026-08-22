"""The trigger schemas."""

from __future__ import annotations

import uuid

import pytest
from litestar.exceptions import HTTPException

from app.schemas.email_dispatch import (
    TriggerCreateRequest,
    TriggerUpdateRequest,
)

class TestTriggerRequests:
    def test_the_update_form_cannot_change_the_kind(self):
        """Turning a webhook trigger into an event one would leave a live URL that callers
        are still using pointing at different semantics. Leaving the field off the edit form
        is how that refusal is expressed at the schema layer."""
        assert "kind" in TriggerCreateRequest.model_fields
        assert "kind" not in TriggerUpdateRequest.model_fields

    def test_recipients_and_bindings_are_each_one_json_object(self):
        payload = TriggerCreateRequest.parse(
            {
                "name": "Tell ops",
                "kind": "event",
                "template_id": str(uuid.uuid4()),
                "smtp_config_id": str(uuid.uuid4()),
                "recipients_json": '{"to": ["ops@example.com"]}',
                "bindings_json": '{"X": {"source": "event", "path": "a.b"}}',
            }
        )
        assert payload.recipients_json == {"to": ["ops@example.com"]}
        assert payload.bindings_json["X"]["source"] == "event"

    def test_event_name_is_optional_at_the_schema_and_decided_by_the_service(self):
        """Required for an event trigger, refused for a webhook — a rule about the
        combination of two fields, so the service owns it. SCHEMAS.md rule 3."""
        payload = TriggerCreateRequest.parse(
            {
                "name": "By webhook",
                "kind": "webhook",
                "template_id": str(uuid.uuid4()),
                "smtp_config_id": str(uuid.uuid4()),
            }
        )
        assert payload.event_name is None
