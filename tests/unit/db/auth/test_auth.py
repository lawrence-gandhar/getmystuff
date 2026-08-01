"""
Tests for app/db/auth/auth.py — password hashing, JWT issuing/decoding and the
``require_auth`` dependency every controller hangs off.

This is the module the whole authenticated surface depends on, so the negative
paths matter more than the happy one: a wrong password, an expired token, a
refresh token presented where an access token is required, and a token signed
by someone else must each be rejected, and rejected as a 401 rather than by
leaking an internal error.

Tokens are minted with real ``jose`` calls rather than stubs — the point is to
prove the signature and expiry checks actually work.
"""

from __future__ import annotations

import os
import uuid as uuid_pkg
from datetime import datetime, timedelta, timezone

import dotenv
import pytest
from jose import jwt
from litestar.exceptions import HTTPException

from app.db.auth import auth as auth_module
from app.db.auth.auth import (
    ALGORITHM,
    SECRET_KEY,
    _truncate_password,
    authenticate_user,
    create_access_token,
    create_refresh_token,
    decode_token,
    get_current_user,
    hash_password,
    require_auth,
    require_role,
    verify_password,
)
from tests.conftest import TEST_PASSWORD


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------
class TestPasswordHashing:
    def test_hash_then_verify_round_trips(self) -> None:
        digest = hash_password("hunter2")
        assert verify_password("hunter2", digest) is True

    def test_a_wrong_password_does_not_verify(self) -> None:
        digest = hash_password("hunter2")
        assert verify_password("hunter3", digest) is False

    def test_the_digest_is_not_the_plaintext(self) -> None:
        digest = hash_password("hunter2")
        assert "hunter2" not in digest
        assert digest.startswith("$2")

    def test_the_same_password_hashes_differently_each_time(self) -> None:
        """bcrypt salts per call; equal digests would reveal shared passwords."""
        assert hash_password("hunter2") != hash_password("hunter2")

    @pytest.mark.parametrize(
        "password",
        ["", "a", "p@ss w0rd!", "unicode-Ω-ñ", "x" * 71],
    )
    def test_round_trips_for_a_range_of_inputs(self, password: str) -> None:
        assert verify_password(password, hash_password(password)) is True


class TestPasswordTruncation:
    def test_truncates_to_72_bytes(self) -> None:
        assert len(_truncate_password("x" * 200)) == 72

    def test_returns_bytes(self) -> None:
        assert isinstance(_truncate_password("abc"), bytes)

    def test_truncation_is_by_bytes_not_characters(self) -> None:
        """Multi-byte characters count against the 72-byte bcrypt limit, so a
        36-character string of 3-byte characters is already over it."""
        assert len(_truncate_password("Ω" * 100)) <= 72

    def test_passwords_differing_only_after_72_bytes_are_interchangeable(self) -> None:
        """
        Recorded as real behaviour, not as an endorsement. bcrypt ignores
        everything past 72 bytes, and the helper makes that explicit rather than
        letting the library truncate silently — so two long passwords sharing a
        72-byte prefix verify against each other's hash.
        """
        base = "x" * 72
        digest = hash_password(base + "AAAA")

        assert verify_password(base + "ZZZZ", digest) is True


# ---------------------------------------------------------------------------
# Token creation and decoding
# ---------------------------------------------------------------------------
class TestTokenCreation:
    def test_an_access_token_carries_the_subject_and_type(self) -> None:
        user_uuid = uuid_pkg.uuid4()
        payload = decode_token(create_access_token(str(user_uuid)))

        assert payload["sub"] == str(user_uuid)
        assert payload["type"] == "access"

    def test_a_refresh_token_is_typed_differently(self) -> None:
        payload = decode_token(create_refresh_token(str(uuid_pkg.uuid4())))
        assert payload["type"] == "refresh"

    def test_tokens_carry_issued_at_and_expiry(self) -> None:
        payload = decode_token(create_access_token(str(uuid_pkg.uuid4())))

        assert payload["exp"] > payload["iat"]

    def test_access_tokens_expire_in_an_hour(self) -> None:
        payload = decode_token(create_access_token(str(uuid_pkg.uuid4())))
        lifetime = payload["exp"] - payload["iat"]

        assert lifetime == pytest.approx(60 * 60, abs=5)

    def test_refresh_tokens_expire_in_a_week(self) -> None:
        payload = decode_token(create_refresh_token(str(uuid_pkg.uuid4())))
        lifetime = payload["exp"] - payload["iat"]

        assert lifetime == pytest.approx(7 * 24 * 60 * 60, abs=5)

    def test_a_uuid_object_is_stringified(self) -> None:
        user_uuid = uuid_pkg.uuid4()
        assert decode_token(create_access_token(user_uuid))["sub"] == str(user_uuid)


class TestDecodeToken:
    def test_rejects_a_malformed_token(self) -> None:
        with pytest.raises(HTTPException) as excinfo:
            decode_token("not-a-jwt")

        assert excinfo.value.status_code == 401
        assert excinfo.value.detail == "Invalid token"

    def test_rejects_a_token_signed_with_another_key(self) -> None:
        """The signature check is what stops a client minting its own token."""
        forged = jwt.encode(
            {"sub": "x", "type": "access"}, "a-different-key", algorithm=ALGORITHM
        )

        with pytest.raises(HTTPException) as excinfo:
            decode_token(forged)

        assert excinfo.value.detail == "Invalid token"

    def test_rejects_an_expired_token_with_a_distinct_message(self) -> None:
        expired = jwt.encode(
            {
                "sub": "x",
                "type": "access",
                "exp": datetime.now(timezone.utc) - timedelta(minutes=1),
            },
            SECRET_KEY,
            algorithm=ALGORITHM,
        )

        with pytest.raises(HTTPException) as excinfo:
            decode_token(expired)

        assert excinfo.value.status_code == 401
        assert excinfo.value.detail == "Token expired"

    @pytest.mark.parametrize("token", ["", "a.b.c", "...."])
    def test_rejects_assorted_junk(self, token: str) -> None:
        with pytest.raises(HTTPException):
            decode_token(token)


class TestSigningKeyComesFromTheEnvironment:
    """
    Regression tests for a fixed security defect.

    ``SECRET_KEY`` used to be the literal ``"super-secret-key"``, committed to
    the repository and identical in every deployment — anyone with source access
    could mint a valid access token for any user uuid. It is now read from
    ``JWT_SECRET_KEY``, and the module raises at import when that is unset rather
    than falling back to anything guessable.
    """

    def test_the_old_committed_key_no_longer_signs_anything(self) -> None:
        """The demonstration of the original vulnerability, inverted: a token
        forged with the previously-hardcoded key is now rejected."""
        forged = jwt.encode(
            {
                "sub": str(uuid_pkg.uuid4()),
                "type": "access",
                "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            },
            "super-secret-key",
            algorithm="HS256",
        )

        with pytest.raises(HTTPException) as excinfo:
            decode_token(forged)

        assert excinfo.value.detail == "Invalid token"

    def test_the_key_is_whatever_the_environment_supplies(self) -> None:
        assert SECRET_KEY == os.environ["JWT_SECRET_KEY"]
        assert SECRET_KEY != "super-secret-key"

    def test_importing_without_the_variable_fails_loudly(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        A missing signing key must stop the process, not degrade.

        The module is reloaded with the variable cleared, which is the only way
        to exercise an import-time guard. It is reloaded again afterwards so the
        rest of the session keeps the real key — without that, every later test
        would be importing a half-initialised module.

        ``dotenv.load_dotenv`` is patched at its source module, not on
        ``auth_module``: the reload re-runs ``from dotenv import load_dotenv``,
        which would overwrite a patch applied to the importing module and put
        JWT_SECRET_KEY straight back from the .env file.
        """
        import importlib

        monkeypatch.delenv("JWT_SECRET_KEY", raising=False)
        monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: None)

        try:
            with pytest.raises(RuntimeError, match="JWT_SECRET_KEY is not set"):
                importlib.reload(auth_module)
        finally:
            monkeypatch.undo()
            importlib.reload(auth_module)

    def test_the_error_says_how_to_generate_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import importlib

        monkeypatch.delenv("JWT_SECRET_KEY", raising=False)
        monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: None)

        try:
            with pytest.raises(RuntimeError) as excinfo:
                importlib.reload(auth_module)
            assert "secrets.token_urlsafe" in str(excinfo.value)
        finally:
            monkeypatch.undo()
            importlib.reload(auth_module)


# ---------------------------------------------------------------------------
# authenticate_user
# ---------------------------------------------------------------------------
class TestAuthenticateUser:
    async def test_returns_the_user_for_correct_credentials(self, db, user) -> None:  # noqa: ANN001
        result = await authenticate_user(db, user.email, TEST_PASSWORD)

        assert result is not None
        assert result.id == user.id

    async def test_returns_none_for_a_wrong_password(self, db, user) -> None:  # noqa: ANN001
        assert await authenticate_user(db, user.email, "wrong") is None

    async def test_returns_none_for_an_unknown_email(self, db, user) -> None:  # noqa: ANN001
        assert await authenticate_user(db, "nobody@example.com", TEST_PASSWORD) is None

    async def test_email_matching_is_case_sensitive(self, db, user) -> None:  # noqa: ANN001
        """
        Recorded behaviour: the lookup is a plain equality on ``email``, so
        "Tester@example.com" does not find "tester@example.com". Worth knowing —
        signup does not appear to normalise case either, so two accounts can
        differ only by capitalisation.
        """
        assert await authenticate_user(db, user.email.upper(), TEST_PASSWORD) is None

    async def test_a_deactivated_account_cannot_authenticate(
        self, db, make_user  # noqa: ANN001
    ) -> None:
        """
        Regression test for a fixed defect: ``authenticate_user`` used to ignore
        ``is_active`` entirely, so a deactivated account could still exchange its
        password for a token.
        """
        disabled = await make_user("disabled@example.com", is_active=False)

        assert await authenticate_user(db, disabled.email, TEST_PASSWORD) is None

    async def test_deactivation_is_indistinguishable_from_a_wrong_password(
        self, db, make_user  # noqa: ANN001
    ) -> None:
        """Both return ``None``, and the check runs *after* the password
        comparison — so the response never reveals that a given email belongs to
        a disabled account rather than to no account at all."""
        disabled = await make_user("disabled@example.com", is_active=False)

        assert await authenticate_user(db, disabled.email, TEST_PASSWORD) is None
        assert await authenticate_user(db, disabled.email, "wrong") is None
        assert await authenticate_user(db, "nobody@example.com", TEST_PASSWORD) is None


# ---------------------------------------------------------------------------
# get_current_user
# ---------------------------------------------------------------------------
class TestGetCurrentUser:
    async def test_resolves_a_valid_access_token(self, db, user) -> None:  # noqa: ANN001
        resolved = await get_current_user(db, create_access_token(str(user.uuid)))
        assert resolved.id == user.id

    async def test_rejects_a_refresh_token(self, db, user) -> None:  # noqa: ANN001
        """A refresh token must not be usable as a session credential — it lives
        far longer, so accepting it would extend a session sevenfold."""
        with pytest.raises(HTTPException) as excinfo:
            await get_current_user(db, create_refresh_token(str(user.uuid)))

        assert excinfo.value.status_code == 401
        assert excinfo.value.detail == "Invalid token type"

    async def test_rejects_a_token_with_no_type(self, db, user) -> None:  # noqa: ANN001
        token = jwt.encode(
            {
                "sub": str(user.uuid),
                "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            },
            SECRET_KEY,
            algorithm=ALGORITHM,
        )

        with pytest.raises(HTTPException, match="Invalid token type"):
            await get_current_user(db, token)

    async def test_rejects_a_token_with_no_subject(self, db) -> None:  # noqa: ANN001
        token = jwt.encode(
            {
                "type": "access",
                "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            },
            SECRET_KEY,
            algorithm=ALGORITHM,
        )

        with pytest.raises(HTTPException) as excinfo:
            await get_current_user(db, token)

        assert excinfo.value.detail == "Invalid token payload"

    @pytest.mark.parametrize("subject", ["not-a-uuid", "12345", "", "   "])
    async def test_rejects_a_subject_that_is_not_a_uuid(self, db, subject: str) -> None:  # noqa: ANN001
        """A bigint id in ``sub`` — the old style — must be rejected rather than
        used to look up a row; the column is the public uuid."""
        token = jwt.encode(
            {
                "sub": subject,
                "type": "access",
                "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            },
            SECRET_KEY,
            algorithm=ALGORITHM,
        )

        with pytest.raises(HTTPException) as excinfo:
            await get_current_user(db, token)

        assert excinfo.value.status_code == 401

    async def test_rejects_a_valid_token_for_a_deleted_user(self, db) -> None:  # noqa: ANN001
        token = create_access_token(str(uuid_pkg.uuid4()))

        with pytest.raises(HTTPException) as excinfo:
            await get_current_user(db, token)

        assert excinfo.value.status_code == 401
        assert excinfo.value.detail == "User not found"

    async def test_finds_the_right_user_among_several(self, db, user, make_user) -> None:  # noqa: ANN001
        other = await make_user("other@example.com")

        resolved = await get_current_user(db, create_access_token(str(other.uuid)))

        assert resolved.id == other.id
        assert resolved.id != user.id

    async def test_a_token_issued_before_deactivation_stops_working(
        self, db, make_user  # noqa: ANN001
    ) -> None:
        """
        Deactivation has to take effect immediately, not when the token expires.
        The token here is minted while the account is live, so only the check
        inside ``get_current_user`` can reject it — an hour of continued access
        would otherwise be possible after an account is disabled.
        """
        account = await make_user("soon-disabled@example.com")
        token = create_access_token(str(account.uuid))
        assert (await get_current_user(db, token)).id == account.id

        account.is_active = False
        await db.commit()

        with pytest.raises(HTTPException) as excinfo:
            await get_current_user(db, token)

        assert excinfo.value.status_code == 401
        assert excinfo.value.detail == "This account has been deactivated"


# ---------------------------------------------------------------------------
# require_role
# ---------------------------------------------------------------------------
class TestRequireRole:
    def test_passes_for_a_matching_role(self, user) -> None:  # noqa: ANN001
        require_role(user, "admin")

    def test_raises_403_for_a_mismatched_role(self, user) -> None:  # noqa: ANN001
        with pytest.raises(HTTPException) as excinfo:
            require_role(user, "superuser")

        assert excinfo.value.status_code == 403
        assert excinfo.value.detail == "Insufficient permissions"

    def test_an_object_with_no_role_is_rejected(self) -> None:
        """getattr's default means a role-less object fails closed rather than
        raising AttributeError."""
        with pytest.raises(HTTPException) as excinfo:
            require_role(object(), "admin")

        assert excinfo.value.status_code == 403


# ---------------------------------------------------------------------------
# require_auth (the Litestar dependency)
# ---------------------------------------------------------------------------
class FakeRequest:
    """Minimal stand-in — require_auth only reads ``cookies``."""

    def __init__(self, cookies: dict) -> None:
        self.cookies = cookies


class TestRequireAuth:
    async def test_returns_the_user_for_a_valid_cookie(self, db, user) -> None:  # noqa: ANN001
        request = FakeRequest({"access_token": create_access_token(str(user.uuid))})

        assert (await require_auth(request, db)).id == user.id

    async def test_raises_401_when_the_cookie_is_absent(self, db) -> None:  # noqa: ANN001
        with pytest.raises(HTTPException) as excinfo:
            await require_auth(FakeRequest({}), db)

        assert excinfo.value.status_code == 401
        assert excinfo.value.detail == "Not authenticated"

    async def test_raises_401_for_an_empty_cookie(self, db) -> None:  # noqa: ANN001
        with pytest.raises(HTTPException, match="Not authenticated"):
            await require_auth(FakeRequest({"access_token": ""}), db)

    async def test_ignores_other_cookies(self, db, user) -> None:  # noqa: ANN001
        request = FakeRequest(
            {
                "session": "irrelevant",
                "access_token": create_access_token(str(user.uuid)),
            }
        )

        assert (await require_auth(request, db)).id == user.id

    async def test_propagates_the_reason_a_token_was_rejected(self, db, user) -> None:  # noqa: ANN001
        """The specific 401 detail from get_current_user survives, so a log can
        tell an expired session from a forged one."""
        request = FakeRequest({"access_token": create_refresh_token(str(user.uuid))})

        with pytest.raises(HTTPException) as excinfo:
            await require_auth(request, db)

        assert excinfo.value.detail == "Invalid token type"


class TestModuleConfiguration:
    def test_algorithm_and_lifetimes(self) -> None:
        assert ALGORITHM == "HS256"
        assert auth_module.ACCESS_TOKEN_EXPIRE_MINUTES == 60
        assert auth_module.REFRESH_TOKEN_EXPIRE_DAYS == 7

    def test_bcrypt_is_the_configured_scheme(self) -> None:
        assert "bcrypt" in auth_module.pwd_context.schemes()
