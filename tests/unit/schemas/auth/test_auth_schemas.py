"""
Tests for app/schemas/auth/auth_schemas.py.

The login form is the only payload in the application reached before there is a
signed-in user, so its messages are the only ones an anonymous caller sees. These
tests pin both halves of that: the form's own rules are enforced, and the messages
say nothing about which accounts exist.
"""

from __future__ import annotations

import pytest
from litestar.exceptions import HTTPException

from app.schemas.auth import LoginRequest


def _detail(data: dict) -> str:
    with pytest.raises(HTTPException) as exc_info:
        LoginRequest.parse(data)
    return str(exc_info.value.detail)


class TestAcceptance:
    def test_a_normal_login(self) -> None:
        payload = LoginRequest.parse({"email": "a@b.com", "password": "secret"})
        assert (payload.email, payload.password) == ("a@b.com", "secret")

    def test_email_is_lowercased_so_one_account_has_one_spelling(self) -> None:
        assert LoginRequest.parse(
            {"email": "  Harish.Kumar@Example.COM ", "password": "x"}
        ).email == "harish.kumar@example.com"

    @pytest.mark.parametrize(
        "email",
        [
            "a@b",                      # no dot is still deliverable on an intranet
            "first+tag@sub.domain.io",  # plus addressing
            "x@[127.0.0.1]",            # bracketed literal
        ],
    )
    def test_permissive_by_design(self, email: str) -> None:
        """
        A strict RFC-5322 pattern rejects addresses that are valid and
        deliverable. The only thing this check buys is skipping a database round
        trip for something that cannot be an address at all.
        """
        assert LoginRequest.parse({"email": email, "password": "x"}).email == email


class TestRejection:
    def test_email_is_required(self) -> None:
        assert _detail({"password": "x"}) == "Email is required"

    def test_password_is_required(self) -> None:
        assert _detail({"email": "a@b.com"}) == "Password is required"

    @pytest.mark.parametrize("email", ["nope", "@b.com", "a@", "@", "a"])
    def test_something_that_cannot_be_an_address(self, email: str) -> None:
        assert _detail({"email": email, "password": "x"}) == (
            "Email must be a valid email address"
        )

    def test_a_whitespace_only_password_is_empty(self) -> None:
        assert _detail({"email": "a@b.com", "password": "   "}) == "Password is required"


class TestNoAccountDisclosure:
    def test_no_message_hints_at_whether_an_account_exists(self) -> None:
        """
        The credential check's deliberately vague "Invalid credentials" is in
        app.db.auth. Nothing here may undermine it by revealing that a given
        address is or is not registered.
        """
        messages = [
            _detail({"password": "x"}),
            _detail({"email": "a@b.com"}),
            _detail({"email": "nope", "password": "x"}),
        ]

        for message in messages:
            lowered = message.lower()
            assert "account" not in lowered
            assert "registered" not in lowered
            assert "exist" not in lowered
