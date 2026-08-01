"""
Tests for app/schemas/common.py — the response shapes shared across features.

Small surface, but two properties matter enough to pin: the ``{"status",
"message"}`` envelope CLAUDE.md specifies is the one shape errors travel in, and a
choice view keeps ``is_active`` so a form can still show an archived selection it
already has saved.
"""

from __future__ import annotations

import pytest
from litestar.exceptions import HTTPException

from app.schemas.common import (
    ChoiceView,
    DatasourceChoiceView,
    ErrorResponse,
    FragmentResponse,
    LabelledChoiceView,
    StatusResponse,
)


class TestStatusResponse:
    def test_success_envelope(self) -> None:
        assert StatusResponse.success("Saved.").payload() == {
            "status": "success",
            "message": "Saved.",
        }

    def test_error_envelope(self) -> None:
        assert StatusResponse.error("Nope.").payload() == {
            "status": "error",
            "message": "Nope.",
        }

    def test_status_is_restricted_to_the_two_values(self) -> None:
        """
        A third value would mean clients need a third code path. The Literal is
        what stops one being introduced by a typo.
        """
        with pytest.raises(HTTPException):
            StatusResponse.parse({"status": "partial", "message": "?"})


class TestErrorResponse:
    def test_carries_only_a_message(self) -> None:
        assert ErrorResponse.of("Bad thing").payload() == {"message": "Bad thing"}


class TestChoiceViews:
    def test_is_active_defaults_to_true(self) -> None:
        assert ChoiceView.build({"uuid": "u", "name": "n"}).is_active is True

    def test_an_archived_choice_is_still_a_valid_choice(self) -> None:
        """
        Not cosmetic: an archived workspace stays in the list so a row already
        pointing at it can be edited without being silently moved off it.
        """
        view = ChoiceView.build({"uuid": "u", "name": "n", "is_active": False})
        assert view.is_active is False

    def test_labelled_choice_defaults_its_provider(self) -> None:
        view = LabelledChoiceView.build({"uuid": "u", "label": "My key"})
        assert view.provider == ""

    def test_datasource_choice_carries_the_join_capability(self) -> None:
        """
        The query builder needs to know whether joins are possible before it has
        fetched anything from the datasource.
        """
        view = DatasourceChoiceView.build(
            {"uuid": "u", "name": "sales", "db_type": "postgres", "supports_joins": True}
        )
        assert (view.db_type, view.supports_joins) == ("postgres", True)

    def test_a_choice_view_never_carries_a_bigint_id(self) -> None:
        payload = ChoiceView.payload_for({"uuid": "u", "name": "n", "id": 42})
        assert "id" not in payload


class TestFragmentResponse:
    def test_no_error_is_the_success_signal(self) -> None:
        """
        The existing convention across six modules, kept rather than replaced so
        no template has to change: the modal's after-request hook closes itself
        when `error` is absent.
        """
        assert FragmentResponse().succeeded is True

    def test_an_error_marks_the_fragment_as_failed(self) -> None:
        assert FragmentResponse(error="Name is required").succeeded is False

    def test_context_merges_extras(self) -> None:
        context = FragmentResponse(error=None).context(workspaces=[1, 2])
        assert context == {"error": None, "workspaces": [1, 2]}
