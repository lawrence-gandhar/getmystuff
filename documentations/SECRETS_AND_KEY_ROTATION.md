# Secrets at rest, and how to change the key

Everything this application stores on behalf of a user that is not theirs to lose —
a database password, an AI provider key, a set of chatbot request headers, and
(from the Integration Platform onward) OAuth access and refresh tokens — is
encrypted with one symmetric key held in the environment.

This page is the operational half of [app/utils/crypto.py](../app/utils/crypto.py):
what the key is, what happens when it is wrong, and the exact sequence for changing
it without making every stored secret unreadable.

**Read the [Rotating the key](#rotating-the-key) section before you change
`FERNET_KEY`.** Changing it on its own is not a rotation. It is a data-loss event
that reports itself as "this action's saved headers could not be read" a few hours
later, per credential, as each one is next used.

---

## What is encrypted, and what is not

| Table | Column | Holds |
|---|---|---|
| `datasources` | `password_encrypted` | The password for a customer's own database |
| `ai_api_keys` | `api_key_encrypted` | An OpenAI / Anthropic provider key |
| `chatbot_actions` | `headers_encrypted` | A JSON list of request headers, which is where a bearer token for a customer's API ends up |

Everything else on those rows is plaintext, deliberately. `datasources` keeps
host, port, database name and username readable because they are configuration
rather than credentials, they are shown in the UI, and encrypting them would mean
decrypting on every list query for no gain. The convention is **one `*_encrypted`
column per secret, and nothing else encrypted** — new tables follow it, which is
why the Integration Platform's design puts every token in its own column on a
separate `integration_credentials` table rather than encrypting a JSON blob.

Ciphertext is Fernet: AES-128-CBC with an HMAC-SHA256 tag and a random IV, stored
as url-safe base64 text in a `String` column. Two consequences worth knowing:
the same password encrypts to a different value every time (so equal ciphertexts
cannot leak which users share a password), and a modified byte fails the HMAC
rather than decrypting to corrupted output.

---

## The key

| Variable | Meaning |
|---|---|
| `FERNET_KEY` | **Required.** The primary key. Everything is encrypted with this one. |
| `FERNET_KEY_OLD` | Optional, comma-separated, newest first. Retired keys that can still *decrypt*. Never used to encrypt. |

Both live in `.env`, which is gitignored, so every environment has its own.
`crypto.py` calls `load_dotenv()` itself rather than trusting the caller, because
it is imported by services that can run before `main.py` does. A real environment
variable always wins over the file.

`MultiFernet([primary, *retired])` is what makes a key change survivable: it
encrypts with the first key and decrypts with any of them.

**A missing key stops the process at import**, the same way `app/db/auth/auth.py`
refuses to import without `JWT_SECRET_KEY`, and for the same reason. The two
alternatives are both worse: a random per-process fallback makes every stored
secret unreadable after a restart, and a hardcoded fallback is the vulnerability
this replaced. A malformed key is refused by name — without that, a truncated or
quoted value surfaces as a `binascii` error from inside the `cryptography`
package during the import of some unrelated service, which is a stack trace
nobody can act on.

Generate a key with:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

---

## The state this repository is in

`crypto.py` used to read `FERNET_KEY` into a module variable and then never use
it, encrypting everything with a literal written into the file:

```python
SECRET_KEY = os.getenv("FERNET_KEY")        # read
fernet = Fernet("dw7Al3yLv3bfMt8yf45nnQbF33v7LggE9JLMgh32Ws4=")   # and ignored
```

That literal is in this repository's git history. It is reproduced here and in
[the re-encryption migration](../alembic/versions/c4b19e7a5f83_reencrypt_secrets_under_env_fernet_key.py)
because it is already public — there is nothing to protect by omitting it, and
the migration cannot decrypt the existing rows without it.

So: **every ciphertext written before that fix is encrypted with a key anybody
with source access already has.** Survivable for a database password on a
single-tenant install; not survivable for an OAuth refresh token granting
standing write access to a merchant's storefront, which is why fixing it was a
prerequisite for the Integration Platform rather than a follow-up.

`FERNET_KEY` in `.env` and in [tests/conftest.py](../tests/conftest.py) is
currently set to that same legacy literal. That is a **deliberate intermediate
state**: it keeps existing data readable and makes the migration a verified no-op,
and it means the deployment is no more exposed than it was — but it is not the
finished job. **Rotating to a fresh key is a real outstanding task**, and the
whole point of the work described here is that it is now a background
re-encryption rather than a data-loss event.

---

## The re-encryption migration

[`c4b19e7a5f83_reencrypt_secrets_under_env_fernet_key.py`](../alembic/versions/c4b19e7a5f83_reencrypt_secrets_under_env_fernet_key.py)
moves existing rows from the legacy literal onto whatever `FERNET_KEY` is set to.
It is applied at startup like every other revision (see [MIGRATIONS.md](MIGRATIONS.md)).

It is **deterministic and needs no per-deployment configuration**: the old code
used that one literal regardless of the environment, so there is exactly one key
to decrypt from. When `FERNET_KEY` equals the legacy literal it returns
immediately, because the data is already under the configured key.

Three behaviours it is worth knowing before you rely on it:

**A row it cannot read is left exactly as it is.** A value that decrypts under
neither the legacy key nor the configured one is counted, and the migration then
fails naming the table and the count. Overwriting such a row would destroy the
only copy of a secret whose key might still be recoverable from a backup or
another environment — and doing that silently during an automatic startup upgrade
would be the worst possible moment for it.

**A row already under the new key is skipped, not treated as a fault.** A
half-finished previous run looks exactly like that, so the pass is resumable.

**An empty string is skipped entirely.** Several `datasources` rows hold `''`
because a SQLite datasource has no password. That was never ciphertext, and
encrypting it would make an empty password indistinguishable from a real one.

**`downgrade()` raises `NotImplementedError`.** Reverting every secret in the
database to a key published in this repository's history is a security regression
wearing a rollback's clothes. Recovery from a backup taken before the revision is
the supported path, and the exception says so.

---

## Rotating the key

Read the whole procedure before starting. The ordering is the entire content of
it; steps 2 and 3 in the other order lock you out of your own data.

### 0. Back up the database first

```bash
docker exec getmystuff-db-1 pg_dump -U getmystuff getmystuff > backup-before-rotation.sql
```

Not optional. There is no downgrade path, and a backup taken before the rewrite
is the recovery route named in the exception itself.

### 1. Generate the new key

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

### 2. Put the *current* key into `FERNET_KEY_OLD` first

Edit `.env` so the key that is in `FERNET_KEY` today moves into `FERNET_KEY_OLD`:

```ini
FERNET_KEY_OLD=dw7Al3yLv3bfMt8yf45nnQbF33v7LggE9JLMgh32Ws4=
```

Do this as its own edit. Every existing row is still readable at this point and
nothing has changed about what is written.

### 3. Then set `FERNET_KEY` to the new value

```ini
FERNET_KEY=<the key from step 1>
FERNET_KEY_OLD=dw7Al3yLv3bfMt8yf45nnQbF33v7LggE9JLMgh32Ws4=
```

### 4. Restart the app

```bash
docker compose restart app
```

The startup migration now sees `FERNET_KEY != LEGACY_KEY` and re-encrypts the
three columns, printing a count per column. From here on, **new** secrets are
written under the new key and **old** ones are read with either.

### 5. Verify before removing the old key

Confirm every stored secret is readable under the primary key *alone* — that is
the condition for dropping `FERNET_KEY_OLD`, and the only way to know it is to
check rather than assume:

```bash
docker exec getmystuff-app-1 sh -c 'cd /app && FERNET_KEY_OLD= python - <<"PY"
import asyncio
from sqlalchemy import text
from app.db.db_sessions import engine
from app.utils.crypto import is_readable

COLUMNS = [
    ("datasources", "password_encrypted"),
    ("ai_api_keys", "api_key_encrypted"),
    ("chatbot_actions", "headers_encrypted"),
]

async def main():
    async with engine.connect() as connection:
        for table, column in COLUMNS:
            rows = await connection.execute(
                text(f"SELECT id, {column} FROM {table} "
                     f"WHERE {column} IS NOT NULL AND LENGTH({column}) > 0")
            )
            bad = [row[0] for row in rows if not is_readable(str(row[1]))]
            print(f"{table}.{column}: {len(bad)} unreadable {bad[:20]}")

asyncio.run(main())
PY'
```

Expected output when the rotation is complete:

```
datasources.password_encrypted: 0 unreadable []
ai_api_keys.api_key_encrypted: 0 unreadable []
chatbot_actions.headers_encrypted: 0 unreadable []
```

`FERNET_KEY_OLD=` is blanked for this check on purpose — with the retired key
still configured, everything reads fine and the check proves nothing.

Zero unreadable rows in all three means step 6 is safe. Anything else means some
rows were not rewritten; **leave `FERNET_KEY_OLD` in place** and find out why
before going further.

### 6. Remove `FERNET_KEY_OLD` and restart

Once and only once step 5 is clean.

### 7. Rotate the leaked credentials themselves

Re-encrypting under a new key protects the ciphertext going forward. It does not
help with a secret whose *plaintext* was exposed by the old key being public.
Any provider key or database password that has been in this database since before
the fix should be regenerated at its source — which is a conversation with each
customer, not a command.

---

## What goes wrong, and what it looks like

| Mistake | Symptom | Recovery |
|---|---|---|
| `FERNET_KEY` changed without `FERNET_KEY_OLD` | Every existing secret raises `InvalidToken` on next use. The app starts fine — nothing decrypts at boot — so this surfaces hours later as "could not be read" messages, one credential at a time | Put the old key back in `FERNET_KEY_OLD` immediately. Nothing is lost as long as the old key still exists somewhere |
| The old key is genuinely gone | Same, permanently | Restore the pre-rotation backup, or clear the affected `*_encrypted` columns and have each credential re-entered |
| `FERNET_KEY` unset | The process refuses to start, naming the variable | Set it |
| `FERNET_KEY` malformed | The process refuses to start, naming the variable and how to generate a valid one | Regenerate |
| Migration reports unreadable rows | It raises, naming the table and up to 20 row ids. Those rows are untouched | Those rows were written under a key the migration does not have. Put it in `FERNET_KEY_OLD` and run again, or clear them and re-enter the credentials |

The first row is the one that matters, because it is the failure mode with the
longest delay between cause and symptom. Nothing in the application decrypts a
secret at startup, so there is no boot-time check that would catch it — which is
precisely why the check in step 5 is written out above rather than left as
"verify it works".

---

## Rotating again, later

The migration handles the one specific move off the legacy literal. A *second*
rotation — new key to another new key — needs its own pass, because nothing in
the application walks the tables on its own.

`crypto.rotate(token)` is the operation that pass needs: it reads with any
configured key and re-encrypts under the primary one. The shape to copy is
`reencrypt_column` in the existing revision, which is factored out of `upgrade()`
specifically so it can be reused and unit-tested. A new Alembic revision doing
the same three columns with `legacy` bound to the previous `FERNET_KEY` is a
twenty-line file.

An alternative worth considering when the tables are large: leave both keys
configured and let rows migrate lazily as each secret is next written. The cost
is that `FERNET_KEY_OLD` can never be dropped with confidence, because nothing
tells you when the last stale row was rewritten. The batch pass is preferred for
exactly that reason.

---

## Tests

| File | Covers |
|---|---|
| [tests/unit/utils/test_crypto.py](../tests/unit/utils/test_crypto.py) | Round trip, non-determinism, tamper rejection, `is_readable`, and the import-time refusals — the last group in a **subprocess run from a directory with no `.env`**, because `load_dotenv()` searching upward from the project root would otherwise find the developer's own key and the "missing key" case could never happen |
| [tests/unit/db/test_reencrypt_migration.py](../tests/unit/db/test_reencrypt_migration.py) | `reencrypt_column` with two genuinely different keys, batching past `BATCH_SIZE`, the resumable case, the empty-string case, and the assertion this all exists for: **an unreadable row is reported and left byte-identical** |

`TestKeyRotation` in the first file builds its own `MultiFernet` rather than
reimporting the module, because `FERNET_KEY` is read once at import and the suite
sets it. `test_a_dropped_key_makes_its_rows_unreadable` is the hazard in this
page's first table, pinned as a test.

The suite sets `FERNET_KEY` to the legacy literal, which makes the migration's
`upgrade()` a no-op under test — deliberate, and the reason `reencrypt_column`
exists as a separate function at all.
