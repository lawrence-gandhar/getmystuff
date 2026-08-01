"""
app/schemas/auth/auth_schemas.py

Pydantic schemas for authentication.

One request shape, and it is the only one in the application that is reached
before there is a signed-in user — so it is also the only one where a validation
message is visible to someone who is not yet a customer. The messages here say
what to fix about the form and nothing about the account: "Email is required" is
fine, "no account with that email" is not, because it tells an unauthenticated
caller which addresses are registered.

The credential check itself is `app.db.auth.authenticate_user`, and the
deliberately vague "Invalid credentials" it produces stays exactly as it was.
This schema only decides whether there is anything worth checking.
"""

from pydantic import Field, field_validator

from app.schemas.base import MAX_NAME_LENGTH, FormRequest, RequiredText

# Deliberately permissive. A strict RFC-5322 pattern rejects addresses that are
# valid and deliverable, and the only thing this check protects against is a
# database round trip for something that cannot be an address at all.
_MIN_EMAIL_LENGTH = 3


class LoginRequest(FormRequest):
    """The login form: an email address and a password."""

    email: RequiredText = Field(title="Email", max_length=MAX_NAME_LENGTH)
    password: RequiredText = Field(title="Password", max_length=MAX_NAME_LENGTH)

    @field_validator("email")
    @classmethod
    def validate_email_shape(cls, v: str) -> str:
        """
        An address must contain an ``@`` with something either side, and is
        lowercased so the same account cannot be reached under two spellings.
        """
        if len(v) < _MIN_EMAIL_LENGTH or "@" not in v:
            raise ValueError("Email must be a valid email address")

        local, _, domain = v.rpartition("@")
        if not local or not domain:
            raise ValueError("Email must be a valid email address")

        return v.lower()
