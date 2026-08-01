"""
Tests for app/schemas/workspaces/workspace_schemas.py.

Workspaces is the simplest feature package and the one the others are modelled on,
so these tests double as the reference for what a feature's schema tests should
cover: acceptance, the normalization that changes what gets stored, the rejection
messages, and an explicit check that the rules needing a database were *not*
duplicated here.
"""

from __future__ import annotations

import pytest
from litestar.exceptions import HTTPException

from app.schemas.base import MAX_DESCRIPTION_LENGTH, MAX_NAME_LENGTH
from app.schemas.workspaces import (
    WorkspaceCreateRequest,
    WorkspaceSetActiveRequest,
    WorkspaceUpdateRequest,
    WorkspaceView,
)

BOTH = [WorkspaceCreateRequest, WorkspaceUpdateRequest]


def _detail(schema, data: dict) -> str:
    with pytest.raises(HTTPException) as exc_info:
        schema.parse(data)
    return str(exc_info.value.detail)


@pytest.mark.parametrize("schema", BOTH, ids=["create", "update"])
class TestNameAndDescription:
    def test_accepts_a_name_only(self, schema) -> None:
        payload = schema.parse({"name": "Sales team"})
        assert payload.name == "Sales team"
        assert payload.description is None

    def test_name_is_trimmed(self, schema) -> None:
        assert schema.parse({"name": "  Sales  "}).name == "Sales"

    def test_a_blank_description_clears_the_column(self, schema) -> None:
        """
        ``None`` rather than ``""``: an emptied textarea means "no description",
        and storing an empty string would make the column's nullability a lie.
        """
        assert schema.parse({"name": "x", "description": "  "}).description is None

    def test_name_is_required(self, schema) -> None:
        assert _detail(schema, {}) == "Workspace name is required"

    def test_a_whitespace_only_name_is_empty(self, schema) -> None:
        assert _detail(schema, {"name": "   "}) == "Workspace name is required"

    def test_name_length_boundary(self, schema) -> None:
        assert len(schema.parse({"name": "a" * MAX_NAME_LENGTH}).name) == MAX_NAME_LENGTH
        assert "cannot be longer than 255" in _detail(
            schema, {"name": "a" * (MAX_NAME_LENGTH + 1)}
        )

    def test_description_length_boundary(self, schema) -> None:
        long_enough = "d" * MAX_DESCRIPTION_LENGTH
        assert schema.parse(
            {"name": "x", "description": long_enough}
        ).description == long_enough
        assert "cannot be longer than 2000" in _detail(
            schema, {"name": "x", "description": "d" * (MAX_DESCRIPTION_LENGTH + 1)}
        )


class TestSetActive:
    @pytest.mark.parametrize(
        ("raw", "expected"), [("true", True), ("false", False), ("on", True)]
    )
    def test_reads_the_toggle(self, raw: str, expected: bool) -> None:
        assert WorkspaceSetActiveRequest.parse({"is_active": raw}).is_active is expected

    def test_an_absent_flag_is_false(self) -> None:
        assert WorkspaceSetActiveRequest.parse({}).is_active is False

    def test_an_unrecognised_token_is_refused_rather_than_read_as_false(self) -> None:
        """
        A value we did not render cannot have come from our form, and reading it
        as ``False`` would archive a workspace the caller asked to restore.
        """
        with pytest.raises(HTTPException):
            WorkspaceSetActiveRequest.parse({"is_active": "sure"})


class TestNoBusinessRulesLeakedIn:
    def test_a_duplicate_name_is_not_this_layers_problem(self) -> None:
        """
        Uniqueness needs a query, and the race behind it needs the unique index.
        Both live in workspace_service; a name that happens to be taken must pass
        here so the service can produce the message that knows *why*.
        """
        assert WorkspaceCreateRequest.parse({"name": "Already Taken"}).name == (
            "Already Taken"
        )


class TestWorkspaceView:
    def test_exposes_the_public_uuid_and_no_internal_id(self) -> None:
        payload = WorkspaceView.payload_for(
            {"id": 7, "uuid": "u-1", "name": "Sales", "agent_count": 3}
        )
        assert payload["uuid"] == "u-1"
        assert "id" not in payload

    def test_counts_default_to_zero(self) -> None:
        view = WorkspaceView.build({"uuid": "u", "name": "n"})
        assert (view.agent_count, view.is_active) == (0, True)
