"""The email template schemas."""

from __future__ import annotations

import uuid

import pytest
from litestar.exceptions import HTTPException

from app.schemas.email_dispatch import TemplateCreateRequest

class TestTemplateRequests:
    def test_the_variable_list_arrives_as_one_json_field(self):
        payload = TemplateCreateRequest.parse(
            {
                "name": "Alert",
                "subject_template": "{{X}} failed",
                "body_html_template": "<p>{{X}}</p>",
                "variables_json": '[{"name": "X", "required": true}]',
            }
        )
        assert payload.variables_json == [{"name": "X", "required": True}]

    def test_a_template_with_no_variables_posts_cleanly(self):
        """The hidden field is absent when nothing was added, and that must not 400."""
        payload = TemplateCreateRequest.parse(
            {
                "name": "Alert",
                "subject_template": "Something happened",
                "body_html_template": "<p>Something happened</p>",
            }
        )
        assert payload.variables_json == []
