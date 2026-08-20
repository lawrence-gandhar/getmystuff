"""
Tests for the re-encryption migration's helper,
``c4b19e7a5f83_reencrypt_secrets_under_env_fernet_key.reencrypt_column``.

**Why the helper is tested rather than ``upgrade()``.** The upgrade path is a
deliberate no-op on every developer machine and under this suite, because
``tests/conftest.py`` sets ``FERNET_KEY`` to the same literal the old code
hardcoded — so the branch that actually rewrites rows would never run. Extracting
the loop gives it two distinct keys and a real table to work on.

Runs against the suite's SQLite database. The statements are plain
``SELECT``/``UPDATE`` over one text column, which is the same on both engines; the
migration's Postgres-specific parts are the surrounding Alembic machinery, and those
are Alembic's to get right.

The assertion that matters most is the negative one: **a row that cannot be
decrypted is left exactly as it was.** Overwriting it would destroy the only copy of
a secret whose key might still be recoverable, and doing that during an automatic
startup upgrade would be the worst possible moment.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from cryptography.fernet import Fernet

REVISION_PATH = (
    Path(__file__).resolve().parents[3]
    / "alembic"
    / "versions"
    / "c4b19e7a5f83_reencrypt_secrets_under_env_fernet_key.py"
)


def _load_revision():  # noqa: ANN202
    """Alembic's versions directory is not a package, so load it by path."""
    spec = importlib.util.spec_from_file_location("reencrypt_revision", REVISION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


revision = _load_revision()


@pytest.fixture
def secrets_table():
    """A one-column stand-in for any of the three real secret tables."""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(
            sa.text("CREATE TABLE widgets (id INTEGER PRIMARY KEY, secret TEXT)")
        )
    return engine


@pytest.fixture
def keys():
    old = Fernet(revision.LEGACY_KEY.encode())
    new = Fernet(Fernet.generate_key())
    return old, new


def _rows(connection) -> dict:  # noqa: ANN001
    return {
        row[0]: row[1]
        for row in connection.execute(sa.text("SELECT id, secret FROM widgets")).fetchall()
    }


class TestReencryptColumn:
    def test_every_legacy_row_is_rewritten_and_still_says_the_same_thing(
        self, secrets_table, keys
    ) -> None:
        old, new = keys
        with secrets_table.begin() as connection:
            connection.execute(
                sa.text("INSERT INTO widgets (id, secret) VALUES (:i, :s)"),
                [
                    {"i": 1, "s": old.encrypt(b"hunter2").decode()},
                    {"i": 2, "s": old.encrypt(b"correct horse").decode()},
                ],
            )

            rewritten, unreadable = revision.reencrypt_column(
                connection, "widgets", "id", "secret", old, new
            )

            assert (rewritten, unreadable) == (2, [])
            stored = _rows(connection)
            assert new.decrypt(stored[1].encode()) == b"hunter2"
            assert new.decrypt(stored[2].encode()) == b"correct horse"

    def test_a_row_already_under_the_new_key_is_left_alone(
        self, secrets_table, keys
    ) -> None:
        """
        A half-finished previous run looks exactly like this, so the pass has to be
        resumable rather than treating it as a fault.
        """
        old, new = keys
        already = new.encrypt(b"done earlier").decode()
        with secrets_table.begin() as connection:
            connection.execute(
                sa.text("INSERT INTO widgets (id, secret) VALUES (1, :s)"), {"s": already}
            )

            rewritten, unreadable = revision.reencrypt_column(
                connection, "widgets", "id", "secret", old, new
            )

            assert (rewritten, unreadable) == (0, [])
            assert _rows(connection)[1] == already

    def test_an_unreadable_row_is_reported_and_untouched(
        self, secrets_table, keys
    ) -> None:
        """The negative assertion this whole helper exists for."""
        old, new = keys
        stranger = Fernet(Fernet.generate_key()).encrypt(b"written elsewhere").decode()
        with secrets_table.begin() as connection:
            connection.execute(
                sa.text("INSERT INTO widgets (id, secret) VALUES (:i, :s)"),
                [
                    {"i": 1, "s": old.encrypt(b"fine").decode()},
                    {"i": 2, "s": stranger},
                    {"i": 3, "s": "not ciphertext at all"},
                ],
            )

            rewritten, unreadable = revision.reencrypt_column(
                connection, "widgets", "id", "secret", old, new
            )

            assert rewritten == 1
            assert sorted(unreadable) == [2, 3]

            stored = _rows(connection)
            assert stored[2] == stranger
            assert stored[3] == "not ciphertext at all"

    @pytest.mark.parametrize("value", ["", None])
    def test_an_empty_value_is_skipped_entirely(
        self, secrets_table, keys, value
    ) -> None:
        """
        Several ``datasources`` rows hold ``''`` because a SQLite datasource has no
        password. Encrypting that would make an empty password indistinguishable
        from a real one — and it would be reported as unreadable if it were read.
        """
        old, new = keys
        with secrets_table.begin() as connection:
            connection.execute(
                sa.text("INSERT INTO widgets (id, secret) VALUES (1, :s)"), {"s": value}
            )

            rewritten, unreadable = revision.reencrypt_column(
                connection, "widgets", "id", "secret", old, new
            )

            assert (rewritten, unreadable) == (0, [])
            assert _rows(connection)[1] == value

    def test_more_rows_than_one_batch(self, secrets_table, keys) -> None:
        """Batching is insurance against the deployment where these tables are big."""
        old, new = keys
        count = revision.BATCH_SIZE + 7
        with secrets_table.begin() as connection:
            connection.execute(
                sa.text("INSERT INTO widgets (id, secret) VALUES (:i, :s)"),
                [
                    {"i": i, "s": old.encrypt(f"secret-{i}".encode()).decode()}
                    for i in range(1, count + 1)
                ],
            )

            rewritten, unreadable = revision.reencrypt_column(
                connection, "widgets", "id", "secret", old, new
            )

            assert (rewritten, unreadable) == (count, [])
            stored = _rows(connection)
            assert new.decrypt(stored[count].encode()) == f"secret-{count}".encode()


class TestTheRevisionItself:
    def test_the_legacy_key_is_the_one_the_old_code_used(self) -> None:
        """
        If this ever stops matching, the migration decrypts nothing and every row
        is reported unreadable. Pinned so that a well-meaning edit is caught here
        rather than at somebody's startup.
        """
        assert revision.LEGACY_KEY == "dw7Al3yLv3bfMt8yf45nnQbF33v7LggE9JLMgh32Ws4="
        Fernet(revision.LEGACY_KEY.encode())  # parses as a real key

    def test_all_three_secret_columns_are_covered(self) -> None:
        assert {(table, column) for table, _, column in revision.SECRET_COLUMNS} == {
            ("datasources", "password_encrypted"),
            ("ai_api_keys", "api_key_encrypted"),
            ("chatbot_actions", "headers_encrypted"),
        }

    def test_downgrade_refuses(self) -> None:
        """
        Reverting every secret to a key published in this repository's history is a
        security regression wearing a rollback's clothes.
        """
        with pytest.raises(NotImplementedError, match="restore from a database backup"):
            revision.downgrade()
