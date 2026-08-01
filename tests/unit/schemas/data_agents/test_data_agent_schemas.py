"""
Tests for app/schemas/data_agents/data_agent_schemas.py.

Two things are worth pinning here beyond the usual field rules.

The **workspace filter** rides on every mutation. If a mutation lost it, the
rebuilt table would silently widen to every agent — which looks like the mutation
went somewhere else. The mixin is what guarantees all four carry it, so there is a
test that walks the schemas rather than trusting the class hierarchy.

The **optional foreign keys** are genuinely nullable columns: an agent may belong
to no workspace and may resolve its model from the user's active keys rather than a
named one. A blank dropdown therefore has to mean "none", not "invalid".
"""

from __future__ import annotations

import uuid

import pytest
from litestar.exceptions import HTTPException

from app.schemas.base import MAX_PROMPT_LENGTH
from app.schemas.data_agents import (
    DataAgentCreateRequest,
    DataAgentDeleteRequest,
    DataAgentListQuery,
    DataAgentSetActiveRequest,
    DataAgentUpdateRequest,
    DataAgentView,
)

VALID_UUID = "3f4b2c1e-0000-4000-8000-000000000001"

MUTATIONS = [
    DataAgentCreateRequest,
    DataAgentUpdateRequest,
    DataAgentSetActiveRequest,
    DataAgentDeleteRequest,
]

WRITE_FORMS = [DataAgentCreateRequest, DataAgentUpdateRequest]


def _detail(schema, data: dict) -> str:
    with pytest.raises(HTTPException) as exc_info:
        schema.parse(data)
    return str(exc_info.value.detail)


@pytest.mark.parametrize("schema", MUTATIONS)
class TestEveryMutationCarriesTheFilter:
    def test_the_filter_is_declared(self, schema) -> None:
        assert "workspace_filter" in schema.model_fields

    def test_a_blank_filter_means_unfiltered(self, schema) -> None:
        assert schema.parse(_minimum(schema, workspace_filter="")).workspace_filter is None

    def test_a_valid_filter_is_parsed_to_a_uuid(self, schema) -> None:
        payload = schema.parse(_minimum(schema, workspace_filter=VALID_UUID))
        assert payload.workspace_filter == uuid.UUID(VALID_UUID)

    def test_an_unreadable_filter_is_refused(self, schema) -> None:
        """
        Not defaulted to "all": a tampered filter must fail rather than quietly
        show the user a different subset than the one they were looking at.
        """
        assert _detail(schema, _minimum(schema, workspace_filter="nope")) == (
            "Workspace filter is not a valid selection"
        )


def _minimum(schema, **extra) -> dict:
    """The smallest valid payload for whichever mutation schema is under test."""
    data = {"name": "Sales agent"} if schema in WRITE_FORMS else {}
    data.update(extra)
    return data


@pytest.mark.parametrize("schema", WRITE_FORMS, ids=["create", "update"])
class TestWriteForms:
    def test_name_only_is_enough(self, schema) -> None:
        payload = schema.parse({"name": "Sales agent"})
        assert payload.name == "Sales agent"
        assert payload.description is None
        assert payload.system_prompt is None
        assert payload.workspace_id is None
        assert payload.llm_api_key_id is None

    def test_name_is_required(self, schema) -> None:
        assert _detail(schema, {}) == "Agent name is required"

    def test_a_blank_workspace_means_no_workspace(self, schema) -> None:
        """``data_agents.workspace_id`` is nullable — unassigned is a real state."""
        assert schema.parse({"name": "x", "workspace_id": ""}).workspace_id is None

    def test_a_blank_key_means_resolve_from_active_keys(self, schema) -> None:
        assert schema.parse({"name": "x", "llm_api_key_id": ""}).llm_api_key_id is None

    def test_a_malformed_workspace_selection_is_refused(self, schema) -> None:
        assert _detail(schema, {"name": "x", "workspace_id": "abc"}) == (
            "Workspace is not a valid selection"
        )

    def test_a_malformed_key_selection_is_refused(self, schema) -> None:
        assert _detail(schema, {"name": "x", "llm_api_key_id": "abc"}) == (
            "AI API key is not a valid selection"
        )

    def test_the_system_prompt_is_bounded(self, schema) -> None:
        """
        The prompt is sent to a language model on every turn, so its length is a
        recurring cost rather than only a column width.
        """
        at_cap = "p" * MAX_PROMPT_LENGTH
        assert schema.parse({"name": "x", "system_prompt": at_cap}).system_prompt == at_cap
        assert "cannot be longer than 20000" in _detail(
            schema, {"name": "x", "system_prompt": "p" * (MAX_PROMPT_LENGTH + 1)}
        )

    def test_a_blank_prompt_clears_the_column(self, schema) -> None:
        assert schema.parse({"name": "x", "system_prompt": "  "}).system_prompt is None


class TestSetActive:
    def test_reads_the_toggle(self) -> None:
        assert DataAgentSetActiveRequest.parse({"is_active": "true"}).is_active is True

    def test_an_absent_flag_is_false(self) -> None:
        assert DataAgentSetActiveRequest.parse({}).is_active is False


class TestDelete:
    def test_carries_nothing_but_the_filter(self) -> None:
        """
        Delete has no fields of its own and still gets a schema, because the
        filter has to be validated on the way in — a handler reading it raw would
        be the one place the rule is not enforced.
        """
        assert set(DataAgentDeleteRequest.model_fields) == {"workspace_filter"}


class TestListQuery:
    def test_no_filter_lists_everything(self) -> None:
        assert DataAgentListQuery.parse({}).workspace is None

    def test_the_workspaces_page_link_is_parsed(self) -> None:
        assert DataAgentListQuery.parse({"workspace": VALID_UUID}).workspace == (
            uuid.UUID(VALID_UUID)
        )

    def test_a_broken_link_is_refused(self) -> None:
        with pytest.raises(HTTPException):
            DataAgentListQuery.parse({"workspace": "1234"})


class TestDataAgentView:
    def test_related_ids_default_to_empty_strings_for_preselection(self) -> None:
        """
        ``""`` rather than ``None``: the edit form compares these against an
        unselected ``<option value="">``, and "None" would render as text.
        """
        view = DataAgentView.build({"uuid": "u", "name": "n"})
        assert (view.workspace_id, view.llm_api_key_id) == ("", "")

    def test_no_internal_id_is_exposed(self) -> None:
        payload = DataAgentView.payload_for({"id": 3, "uuid": "u", "name": "n"})
        assert "id" not in payload
        assert payload["uuid"] == "u"
