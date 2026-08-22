"""The delivery-log schemas."""

from __future__ import annotations

import uuid

import pytest
from litestar.exceptions import HTTPException

from app.schemas.email_dispatch import (
    MessageFilterRequest,
    MessageView,
    SendTestRequest,
)

class TestMessageRequests:
    def test_the_log_page_is_reachable_with_no_query_string(self):
        """Every QueryRequest field must have a default, or the bare URL is a 400."""
        payload = MessageFilterRequest.parse({})
        assert payload.status is None
        assert payload.page is None

    def test_send_test_parses_both_uuids(self):
        template_id = uuid.uuid4()
        payload = SendTestRequest.parse(
            {
                "template_id": str(template_id),
                "smtp_config_id": str(uuid.uuid4()),
                "to_address": " me@example.com ",
            }
        )
        assert payload.template_id == template_id
        assert payload.to_address == "me@example.com"

    def test_send_test_refuses_a_malformed_uuid_with_a_400(self):
        with pytest.raises(HTTPException) as caught:
            SendTestRequest.parse(
                {
                    "template_id": "not-a-uuid",
                    "smtp_config_id": str(uuid.uuid4()),
                    "to_address": "me@example.com",
                }
            )
        assert caught.value.status_code == 400


class TestViewsBuild:
    def test_a_view_builds_from_a_plain_dict_and_keeps_the_public_uuid(self):
        view = MessageView.build(
            {
                "uuid": "9d3c8a1e-0000-4000-8000-000000000000",
                "subject": "Nightly sync failed",
                "status": "sent",
            }
        )
        assert view.uuid == "9d3c8a1e-0000-4000-8000-000000000000"
        assert view.to_addresses == []
        # CLAUDE.md's identifier rule: nothing built for a template carries the bigint id.
        assert "id" not in MessageView.model_fields
