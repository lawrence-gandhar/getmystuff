"""
Symmetric encryption for every secret this application stores at rest.

Four columns depend on this module and every one of them is a credential
belonging to somebody else: ``datasources.password_encrypted``,
``ai_api_keys.api_key_encrypted``, ``chatbot_actions.headers_encrypted``, and
the integration platform's OAuth tokens.

**What this used to do, and why it changed.** The key was a literal written into
this file — ``Fernet("dw7Al3...")`` — while ``FERNET_KEY`` was read into a module
variable and then never used. That literal is in the repository's history, so
every ciphertext in every deployment was encrypted with a key anybody with source
access already had. Survivable for a database password on a single-tenant install;
not survivable for an OAuth refresh token granting standing write access to a
merchant's storefront.

So the key now comes from the environment and this module **fails at import when
it is missing**, exactly as ``app.db.auth.auth`` does for ``JWT_SECRET_KEY`` and
for the same reason: a missing key is not a condition to degrade through. A random
per-process fallback would make every stored secret unreadable after a restart, and
a hardcoded fallback is the vulnerability this replaced.

Migrating existing rows off the old literal is
``alembic/versions/c4b19e7a5f83_reencrypt_secrets_under_env_fernet_key.py``, and
the procedure for changing the key after that is
``documentations/SECRETS_AND_KEY_ROTATION.md``.
"""

import os
from typing import List

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from dotenv import load_dotenv

# This module is imported by services that can run before main.py does, so it
# loads the .env itself rather than trusting the caller. load_dotenv never
# overwrites a variable already present, so a real environment still wins.
load_dotenv()


def _parse_key(value: str, name: str) -> Fernet:
    """
    Build a Fernet from a configured key, or refuse with a sentence naming which
    variable is wrong.

    Without this, a truncated or quoted key surfaces as a `binascii.Error` from
    inside the cryptography package during import of some unrelated service —
    which is a stack trace nobody can act on.
    """
    try:
        return Fernet(value.encode())
    except (ValueError, TypeError) as exc:
        raise RuntimeError(
            f"{name} is not a valid Fernet key. It must be 32 url-safe base64-encoded "
            "bytes — generate one with:\n"
            '  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
        ) from exc


SECRET_KEY = os.getenv("FERNET_KEY")

if not SECRET_KEY:
    raise RuntimeError(
        "FERNET_KEY is not set. Every secret this application stores is encrypted "
        "with it — datasource passwords, AI provider keys, chatbot action headers "
        "and integration OAuth tokens — so it cannot start without one. Generate "
        "one with:\n"
        '  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"\n'
        "then add it to your .env as FERNET_KEY=<value>.\n\n"
        "If this deployment has existing data, set FERNET_KEY to the key that data "
        "was encrypted with, or you will not be able to read any of it back."
    )

# Retired keys, newest first, comma-separated. MultiFernet encrypts with the first
# key and decrypts with any of them, which is the whole reason a key change is a
# background re-encryption rather than a data-loss event: set the new key as
# FERNET_KEY, move the old one into FERNET_KEY_OLD, and every existing row stays
# readable while it is rewritten.
#
# Rows are NOT rewritten automatically. Nothing here walks the tables — see
# rotate() and the re-encryption migration for the pass that does.
_RETIRED_RAW: List[str] = [
    part.strip() for part in (os.getenv("FERNET_KEY_OLD") or "").split(",") if part.strip()
]

_KEYS: List[Fernet] = [
    _parse_key(SECRET_KEY, "FERNET_KEY"),
    *(
        _parse_key(value, f"FERNET_KEY_OLD entry {index + 1}")
        for index, value in enumerate(_RETIRED_RAW)
    ),
]

fernet = MultiFernet(_KEYS)


def encrypt_password(password: str) -> str:
    """Encrypt with the primary key."""
    return fernet.encrypt(password.encode()).decode()


def decrypt_password(token: str) -> str:
    """
    Decrypt with whichever configured key wrote it.

    Raises ``cryptography.fernet.InvalidToken`` when no configured key can read
    the value — which in practice means the key was changed without the old one
    being put in FERNET_KEY_OLD. Callers are expected to catch it and say
    something useful; see ``chatbot_action_service._decrypt_headers``, which turns
    it into "this action's saved headers could not be read. Please re-enter them."
    """
    return fernet.decrypt(token.encode()).decode()


# Aliases. The three original call sites encrypt a password and read honestly as
# encrypt_password; the integrations module stores access tokens, refresh tokens
# and client secrets, and calling that "password" at each of those sites would be
# a small lie repeated a dozen times.
encrypt_secret = encrypt_password
decrypt_secret = decrypt_password


def rotate(token: str) -> str:
    """
    Re-encrypt an existing value under the primary key, reading it with any
    configured key.

    This is the one operation a key rotation needs: after moving the old key into
    FERNET_KEY_OLD, a pass over each ``*_encrypted`` column calling this leaves
    every row readable by the primary key alone, at which point the old key can be
    dropped from the environment.
    """
    return fernet.rotate(token.encode()).decode()


def is_readable(token: str) -> bool:
    """
    Whether any configured key can decrypt this value.

    For the re-encryption pass and for diagnostics: a row that cannot be read must
    be reported and left alone, never overwritten. Overwriting it would destroy the
    only copy of a secret whose key might still be recoverable.
    """
    try:
        fernet.decrypt(token.encode())
    except (InvalidToken, ValueError, TypeError):
        return False
    return True
