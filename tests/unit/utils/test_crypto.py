"""
Tests for app/utils/crypto.py — Fernet encryption of stored datasource passwords.

These are the credentials for user-supplied external databases, so the round
trip is the security-critical property: what comes back out must be exactly what
went in, and the stored form must not be the plaintext.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from app.utils.crypto import decrypt_password, encrypt_password, is_readable


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


class TestIsReadable:
    """
    Used by the re-encryption pass to decide whether a row may be touched. A row
    that cannot be read must be reported and left alone — overwriting it destroys
    the only copy of a secret whose key might still be recoverable.
    """

    def test_a_token_this_process_wrote_is_readable(self) -> None:
        assert is_readable(encrypt_password("hunter2"))

    @pytest.mark.parametrize("value", ["", "not-a-token", "gAAAAA-truncated"])
    def test_anything_else_is_not(self, value: str) -> None:
        assert not is_readable(value)

    def test_an_empty_string_is_not_ciphertext(self) -> None:
        """
        Several ``datasources`` rows hold ``''`` because a SQLite datasource has no
        password. That was never encrypted, and the re-encryption pass must skip it
        rather than turn an empty password into ciphertext — which would make it
        indistinguishable from a real one.
        """
        assert not is_readable("")


class TestKeyRotation:
    """
    ``MultiFernet`` is what makes a key change a background re-encryption rather
    than a data-loss event: it encrypts with the first key and decrypts with any of
    them. These tests build their own MultiFernet rather than reimporting the
    module, because ``FERNET_KEY`` is read once at import and the suite sets it.
    """

    def test_a_retired_key_still_decrypts(self) -> None:
        old, new = Fernet(Fernet.generate_key()), Fernet(Fernet.generate_key())
        written_under_the_old_key = old.encrypt(b"hunter2")

        combined = MultiFernet([new, old])

        assert combined.decrypt(written_under_the_old_key) == b"hunter2"

    def test_rotate_moves_a_value_onto_the_primary_key(self) -> None:
        old, new = Fernet(Fernet.generate_key()), Fernet(Fernet.generate_key())
        combined = MultiFernet([new, old])

        rotated = combined.rotate(old.encrypt(b"hunter2"))

        # Readable by the new key alone, which is what lets the old one be dropped
        # from the environment once every row has been through this.
        assert new.decrypt(rotated) == b"hunter2"
        with pytest.raises(InvalidToken):
            old.decrypt(rotated)

    def test_a_dropped_key_makes_its_rows_unreadable(self) -> None:
        """
        The hazard the runbook exists for: changing FERNET_KEY without putting the
        old value in FERNET_KEY_OLD makes every existing secret unreadable.
        """
        old, new = Fernet(Fernet.generate_key()), Fernet(Fernet.generate_key())
        written_under_the_old_key = old.encrypt(b"hunter2")

        with pytest.raises(InvalidToken):
            MultiFernet([new]).decrypt(written_under_the_old_key)


PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _import_crypto_with(fernet_key: str | None) -> subprocess.CompletedProcess:
    """
    Import ``app.utils.crypto`` in a fresh interpreter with a chosen FERNET_KEY.

    A subprocess because the module is already imported here, and reloading it
    would leave this suite's own ``fernet`` bound to a half-built module if the
    reload raised.

    **Run from a directory with no .env.** ``crypto.load_dotenv()`` searches
    upward from the working directory, so running at the project root would find
    the developer's own key and the "missing key" case could never happen. The
    project is reached through PYTHONPATH instead.
    """
    env = {
        key: value
        for key, value in os.environ.items()
        if key != "FERNET_KEY"
    }
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    if fernet_key is not None:
        env["FERNET_KEY"] = fernet_key

    return subprocess.run(
        [sys.executable, "-c", "import app.utils.crypto"],
        capture_output=True,
        text=True,
        cwd=tempfile.gettempdir(),
        env=env,
    )


class TestFailsFastWithoutAKey:
    """
    A missing key is not a condition to degrade through. A random per-process
    fallback would make every stored secret unreadable after a restart, and a
    hardcoded fallback is the vulnerability this replaced.
    """

    def test_importing_without_fernet_key_raises(self) -> None:
        completed = _import_crypto_with(None)

        assert completed.returncode != 0
        assert "FERNET_KEY is not set" in completed.stderr

    def test_an_empty_fernet_key_counts_as_missing(self) -> None:
        completed = _import_crypto_with("")

        assert completed.returncode != 0
        assert "FERNET_KEY is not set" in completed.stderr

    def test_a_malformed_key_names_the_variable(self) -> None:
        """
        Without this, a truncated or quoted key surfaces as a binascii error from
        inside the cryptography package during the import of some unrelated
        service — a stack trace nobody can act on.
        """
        completed = _import_crypto_with("obviously-not-base64")

        assert completed.returncode != 0
        assert "FERNET_KEY is not a valid Fernet key" in completed.stderr

    def test_a_valid_key_imports_cleanly(self) -> None:
        completed = _import_crypto_with(Fernet.generate_key().decode())

        assert completed.returncode == 0, completed.stderr
