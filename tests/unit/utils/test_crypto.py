"""
Tests for app/utils/crypto.py — Fernet encryption of stored datasource passwords.

These are the credentials for user-supplied external databases, so the round
trip is the security-critical property: what comes back out must be exactly what
went in, and the stored form must not be the plaintext.
"""

from __future__ import annotations

import pytest
from cryptography.fernet import InvalidToken

from app.utils.crypto import decrypt_password, encrypt_password


class TestRoundTrip:
    @pytest.mark.parametrize(
        "secret",
        [
            "hunter2",
            "",
            "p@ssw0rd with spaces",
            "unicode-Ω-ñ-日本語",
            "x" * 4096,
            "line\nbreak\ttab",
        ],
    )
    def test_decrypt_undoes_encrypt(self, secret: str) -> None:
        assert decrypt_password(encrypt_password(secret)) == secret

    def test_ciphertext_does_not_contain_the_plaintext(self) -> None:
        token = encrypt_password("hunter2")
        assert "hunter2" not in token

    def test_encryption_is_non_deterministic(self) -> None:
        """Fernet embeds a random IV and a timestamp, so the same password
        encrypts to a different token each time. Equal ciphertexts would leak
        which users share a password."""
        assert encrypt_password("same") != encrypt_password("same")

    def test_returns_str_not_bytes(self) -> None:
        """The column is a String, so both directions must hand back str."""
        token = encrypt_password("hunter2")
        assert isinstance(token, str)
        assert isinstance(decrypt_password(token), str)


class TestRejection:
    def test_a_garbage_token_raises_rather_than_returning_junk(self) -> None:
        with pytest.raises(InvalidToken):
            decrypt_password("not-a-real-fernet-token")

    def test_a_tampered_token_is_rejected(self) -> None:
        """Fernet is authenticated encryption — flipping a byte must fail the
        HMAC rather than silently decrypt to corrupted output."""
        token = encrypt_password("hunter2")
        tampered = token[:-2] + ("AA" if not token.endswith("AA") else "BB")

        with pytest.raises(InvalidToken):
            decrypt_password(tampered)
