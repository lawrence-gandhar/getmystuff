"""
Tests for the engine / Mongo client cache and circuit breaker in
app/db/db_utils.py.

These manage connections to *user-supplied* external databases. No real
external host is contacted: engines are built against file-backed SQLite URLs
under tmp_path, and the Mongo client is constructed but never used to reach a
server — AsyncIOMotorClient does no I/O at construction time.

**This module records a live application bug.** The circuit-breaker constants
``CIRCUIT_FAILURE_LIMIT`` and ``CIRCUIT_RESET_SECONDS`` come straight from
``os.getenv`` and are never cast, so they are ``None`` (or a ``str`` when set).
``_register_failure`` then evaluates ``wrapper.failures >= CIRCUIT_FAILURE_LIMIT``
— ``int >= None`` — and raises ``TypeError``. Because ``_register_failure`` is
called from the ``except`` arm of ``test_rdbms_connection``, a failed connection
test does not return ``False`` as documented; it raises. See
``TestCircuitBreakerIsBroken`` below for the full statement of the defect.

The file half of the same module casts its equivalents to int at import time
(``_FILE_CIRCUIT_FAILURE_LIMIT``), so it works — the fix is to do the same here.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from app.db import db_utils
from app.db.db_utils import (
    EngineWrapper,
    MongoWrapper,
    _register_failure,
    _register_success,
    build_mongo_uri,
    close_mongo_client,
    dispose_engine,
    get_engine,
    get_mongo_client,
)

check_rdbms_connection = db_utils.test_rdbms_connection
check_mongo_connection = db_utils.test_mongo_connection


@pytest.fixture(autouse=True)
async def clean_caches():
    """
    Isolate the two module-global caches per test.

    Engines are disposed on the way out rather than dropped: an undisposed
    AsyncEngine leaves its pool's connections open and asyncio warns about them
    at loop close, which turns into confusing noise in an unrelated test.
    """
    db_utils._engine_cache.clear()
    db_utils._mongo_cache.clear()
    yield
    for wrapper in list(db_utils._engine_cache.values()):
        await wrapper.engine.dispose()
    for mongo_wrapper in list(db_utils._mongo_cache.values()):
        mongo_wrapper.client.close()
    db_utils._engine_cache.clear()
    db_utils._mongo_cache.clear()


# File-backed SQLite, not in-memory.
#
# ``get_engine`` always passes pool_size / max_overflow / pool_timeout, and an
# in-memory aiosqlite URL resolves to StaticPool, which rejects all three with
# "Invalid argument(s) ... sent to create_engine()". A file-backed URL gets a
# pool that accepts them, so it exercises the real code path instead of an
# artificial one. These connect to the local filesystem only — no network.
@pytest.fixture
def sqlite_url(tmp_path) -> str:  # noqa: ANN001
    return f"sqlite+aiosqlite:///{tmp_path / 'primary.db'}"


@pytest.fixture
def other_sqlite_url(tmp_path) -> str:  # noqa: ANN001
    return f"sqlite+aiosqlite:///{tmp_path / 'secondary.db'}"


# ---------------------------------------------------------------------------
# Engine cache
# ---------------------------------------------------------------------------
class TestGetEngine:
    async def test_creates_and_caches_an_engine(self, sqlite_url: str) -> None:
        engine = await get_engine(sqlite_url)

        assert engine is not None
        assert db_utils._engine_cache[sqlite_url].engine is engine

    async def test_second_call_returns_the_cached_engine(self, sqlite_url: str) -> None:
        """The whole point of the cache: building a new pool per query would
        defeat pooling entirely."""
        first = await get_engine(sqlite_url)
        second = await get_engine(sqlite_url)

        assert first is second
        assert len(db_utils._engine_cache) == 1

    async def test_different_urls_get_different_engines(self, sqlite_url: str, other_sqlite_url: str) -> None:
        first = await get_engine(sqlite_url)
        second = await get_engine(other_sqlite_url)

        assert first is not second
        assert len(db_utils._engine_cache) == 2

    async def test_refreshes_last_used_on_a_cache_hit(self, sqlite_url: str) -> None:
        await get_engine(sqlite_url)
        wrapper = db_utils._engine_cache[sqlite_url]
        wrapper.last_used = 0.0

        await get_engine(sqlite_url)

        assert wrapper.last_used > 0.0

    async def test_a_new_wrapper_starts_with_a_closed_circuit(self, sqlite_url: str) -> None:
        await get_engine(sqlite_url)
        wrapper = db_utils._engine_cache[sqlite_url]

        assert wrapper.failures == 0
        assert wrapper.circuit_open_until is None

    async def test_an_open_circuit_blocks_access(self, sqlite_url: str) -> None:
        await get_engine(sqlite_url)
        db_utils._engine_cache[sqlite_url].circuit_open_until = time.time() + 60

        with pytest.raises(Exception, match="circuit open"):
            await get_engine(sqlite_url)

    async def test_an_expired_circuit_allows_access_again(self, sqlite_url: str) -> None:
        engine = await get_engine(sqlite_url)
        db_utils._engine_cache[sqlite_url].circuit_open_until = time.time() - 1

        assert await get_engine(sqlite_url) is engine

    async def test_concurrent_callers_share_one_engine(self, sqlite_url: str) -> None:
        """``_lock`` serialises creation; without it two coroutines racing on a
        cold cache would each build a pool and one would be orphaned."""
        engines = await asyncio.gather(*[get_engine(sqlite_url) for _ in range(8)])

        assert len({id(e) for e in engines}) == 1
        assert len(db_utils._engine_cache) == 1


class TestDisposeEngine:
    async def test_removes_the_entry_from_the_cache(self, sqlite_url: str) -> None:
        await get_engine(sqlite_url)
        await dispose_engine(sqlite_url)

        assert sqlite_url not in db_utils._engine_cache

    async def test_is_safe_for_an_unknown_url(self) -> None:
        await dispose_engine("sqlite+aiosqlite:///never-created")

    async def test_only_disposes_the_named_url(self, sqlite_url: str, other_sqlite_url: str) -> None:
        await get_engine(sqlite_url)
        await get_engine(other_sqlite_url)

        await dispose_engine(sqlite_url)

        assert list(db_utils._engine_cache) == [other_sqlite_url]

    async def test_a_disposed_url_is_rebuilt_on_next_use(self, sqlite_url: str) -> None:
        first = await get_engine(sqlite_url)
        await dispose_engine(sqlite_url)
        second = await get_engine(sqlite_url)

        assert first is not second


# ---------------------------------------------------------------------------
# Mongo client cache
# ---------------------------------------------------------------------------
class TestGetMongoClient:
    URI = "mongodb://user:pw@mongo.invalid:27017"

    async def test_creates_and_caches_a_client(self) -> None:
        """AsyncIOMotorClient performs no I/O at construction, so this never
        touches the network — the autouse guard in conftest would fail it if it
        did."""
        client = await get_mongo_client(self.URI)

        assert client is not None
        assert db_utils._mongo_cache[self.URI].client is client

    async def test_second_call_returns_the_cached_client(self) -> None:
        assert await get_mongo_client(self.URI) is await get_mongo_client(self.URI)
        assert len(db_utils._mongo_cache) == 1

    async def test_refreshes_last_used_on_a_cache_hit(self) -> None:
        await get_mongo_client(self.URI)
        wrapper = db_utils._mongo_cache[self.URI]
        wrapper.last_used = 0.0

        await get_mongo_client(self.URI)

        assert wrapper.last_used > 0.0

    async def test_an_open_circuit_blocks_access(self) -> None:
        await get_mongo_client(self.URI)
        db_utils._mongo_cache[self.URI].circuit_open_until = time.time() + 60

        with pytest.raises(Exception, match="Mongo temporarily unavailable"):
            await get_mongo_client(self.URI)

    async def test_an_expired_circuit_allows_access_again(self) -> None:
        client = await get_mongo_client(self.URI)
        db_utils._mongo_cache[self.URI].circuit_open_until = time.time() - 1

        assert await get_mongo_client(self.URI) is client


class TestCloseMongoClient:
    URI = "mongodb://user:pw@mongo.invalid:27017"

    async def test_removes_the_entry(self) -> None:
        await get_mongo_client(self.URI)
        await close_mongo_client(self.URI)

        assert self.URI not in db_utils._mongo_cache

    async def test_is_safe_for_an_unknown_uri(self) -> None:
        await close_mongo_client("mongodb://never:seen@h:1")

    async def test_the_uri_built_by_build_mongo_uri_round_trips(self) -> None:
        uri = build_mongo_uri("mongo.invalid", "27017", "user", "pw")
        await get_mongo_client(uri)
        await close_mongo_client(uri)

        assert db_utils._mongo_cache == {}


# ---------------------------------------------------------------------------
# The circuit breaker — and the bug in it
# ---------------------------------------------------------------------------
class TestRegisterSuccess:
    async def test_clears_failure_state(self) -> None:
        """``_register_success`` touches neither unconverted constant, so it is
        the one half of the breaker that works."""
        wrapper = EngineWrapper(engine=None, last_used=0.0, failures=4)
        wrapper.circuit_open_until = time.time() + 60

        await _register_success(wrapper)

        assert wrapper.failures == 0
        assert wrapper.circuit_open_until is None

    async def test_is_a_no_op_on_a_healthy_wrapper(self) -> None:
        wrapper = MongoWrapper(client=None, last_used=0.0)
        await _register_success(wrapper)

        assert wrapper.failures == 0
        assert wrapper.circuit_open_until is None


class TestCircuitBreaker:
    """
    Regression tests for a fixed defect.

    ``CIRCUIT_FAILURE_LIMIT`` and ``CIRCUIT_RESET_SECONDS`` came straight from
    ``os.getenv`` and were never cast, so they were ``None`` when unset and
    ``str`` when set. ``_register_failure`` then evaluated
    ``wrapper.failures >= CIRCUIT_FAILURE_LIMIT`` and raised ``TypeError``, which
    meant:

    1. the breaker could never open — it was dead code, and an unreachable user
       database was retried forever;
    2. ``test_rdbms_connection`` / ``test_mongo_connection`` call
       ``_register_failure`` from inside ``except``, so they *raised* instead of
       returning the ``False`` their signatures promise — a wrong database
       password produced an unhandled 500 rather than a readable message.

    Both constants are parsed by ``_int_from_env`` at import now.
    """

    def test_the_constants_are_integers(self) -> None:
        assert isinstance(db_utils.CIRCUIT_FAILURE_LIMIT, int)
        assert isinstance(db_utils.CIRCUIT_RESET_SECONDS, int)
        assert isinstance(db_utils.ENGINE_TTL_SECONDS, int)

    def test_the_file_section_aliases_agree(self) -> None:
        """The file half used to keep its own int-cast copies to work around the
        raw strings. They are plain aliases now and must not drift."""
        assert db_utils._FILE_CIRCUIT_FAILURE_LIMIT == db_utils.CIRCUIT_FAILURE_LIMIT
        assert db_utils._FILE_CIRCUIT_RESET_SECONDS == db_utils.CIRCUIT_RESET_SECONDS
        assert db_utils._FILE_TTL_SECONDS == db_utils.ENGINE_TTL_SECONDS

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [(None, 7), ("", 7), ("   ", 7), ("12", 12), ("  30  ", 30), ("0", 0)],
    )
    def test_int_from_env_parses_or_falls_back(
        self, monkeypatch: pytest.MonkeyPatch, raw, expected: int  # noqa: ANN001
    ) -> None:
        if raw is None:
            monkeypatch.delenv("SOME_SETTING", raising=False)
        else:
            monkeypatch.setenv("SOME_SETTING", raw)

        assert db_utils._int_from_env("SOME_SETTING", 7) == expected

    def test_a_non_numeric_setting_falls_back_and_warns(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A typo in the environment must not take the process down, but it must
        not silently change behaviour either."""
        monkeypatch.setenv("SOME_SETTING", "five")

        with caplog.at_level("WARNING"):
            assert db_utils._int_from_env("SOME_SETTING", 7) == 7

        assert "not a whole number" in caplog.text

    async def test_failures_accumulate_below_the_limit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(db_utils, "CIRCUIT_FAILURE_LIMIT", 3)
        wrapper = EngineWrapper(engine=None, last_used=0.0)

        await _register_failure(wrapper)
        await _register_failure(wrapper)

        assert wrapper.failures == 2
        assert wrapper.circuit_open_until is None

    async def test_reaching_the_limit_opens_the_circuit_and_resets_the_count(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(db_utils, "CIRCUIT_FAILURE_LIMIT", 3)
        monkeypatch.setattr(db_utils, "CIRCUIT_RESET_SECONDS", 60)
        wrapper = EngineWrapper(engine=None, last_used=0.0)
        before = time.time()

        for _ in range(3):
            await _register_failure(wrapper)

        assert wrapper.failures == 0
        assert wrapper.circuit_open_until >= before + 60

    async def test_it_works_for_mongo_wrappers_too(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(db_utils, "CIRCUIT_FAILURE_LIMIT", 2)
        wrapper = MongoWrapper(client=None, last_used=0.0)

        await _register_failure(wrapper)
        await _register_failure(wrapper)

        assert wrapper.circuit_open_until is not None

    async def test_a_failed_connection_test_returns_false_as_documented(self) -> None:
        """The user-visible half of the fix: the ``-> bool`` contract is honoured
        on the failure path instead of raising."""
        url = "sqlite+aiosqlite:////nonexistent_directory_xyz/app.db"

        assert await check_rdbms_connection(url) is False

    async def test_a_failed_mongo_test_returns_false_as_documented(self) -> None:
        """The ping fails without contacting a server — the conftest network
        guard blocks the outbound attempt — and the except arm now returns
        False."""
        uri = "mongodb://user:pw@mongo.invalid:27017"

        assert await check_mongo_connection(uri, "appdb") is False

    async def test_repeated_failures_open_the_circuit_end_to_end(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The whole point of the breaker, which was unreachable before: after
        enough failures ``get_engine`` refuses rather than retrying."""
        monkeypatch.setattr(db_utils, "CIRCUIT_FAILURE_LIMIT", 3)
        url = "sqlite+aiosqlite:////nonexistent_directory_xyz/app.db"

        for _ in range(3):
            assert await check_rdbms_connection(url) is False

        assert db_utils._engine_cache[url].circuit_open_until is not None

        with pytest.raises(Exception, match="circuit open"):
            await get_engine(url)


class TestRdbmsConnectionSuccessPath:
    async def test_a_reachable_database_returns_true(self, sqlite_url: str) -> None:
        """The success path does work: it never reaches ``_register_failure``."""
        assert await check_rdbms_connection(sqlite_url) is True

    async def test_success_clears_prior_failures(self, sqlite_url: str) -> None:
        await get_engine(sqlite_url)
        wrapper = db_utils._engine_cache[sqlite_url]
        wrapper.failures = 3
        wrapper.circuit_open_until = None

        assert await check_rdbms_connection(sqlite_url) is True
        assert wrapper.failures == 0

    async def test_the_engine_is_cached_by_the_connection_test(self, sqlite_url: str) -> None:
        await check_rdbms_connection(sqlite_url)
        assert sqlite_url in db_utils._engine_cache


# ---------------------------------------------------------------------------
# TTL cleanup
# ---------------------------------------------------------------------------
class TestCleanupIdleConnections:
    async def test_evicts_engines_and_clients_idle_past_the_ttl(
        self, monkeypatch: pytest.MonkeyPatch, sqlite_url: str, other_sqlite_url: str
    ) -> None:
        """
        Driven by stubbing asyncio.sleep so exactly one pass runs — the real
        loop waits 300s before its first sweep.

        ``ENGINE_TTL_SECONDS`` is another uncast getenv value, so it is patched
        to an int here; left as None the comparison would raise the same
        TypeError as the failure counter.
        """
        await get_engine(sqlite_url)
        await get_engine(other_sqlite_url)
        db_utils._engine_cache[other_sqlite_url].last_used = time.time() - 10_000

        monkeypatch.setattr(db_utils, "ENGINE_TTL_SECONDS", 1800)

        calls = {"n": 0}

        async def one_pass_then_stop(seconds: float) -> None:
            calls["n"] += 1
            if calls["n"] > 1:
                raise asyncio.CancelledError

        monkeypatch.setattr(db_utils.asyncio, "sleep", one_pass_then_stop)

        with pytest.raises(asyncio.CancelledError):
            await db_utils.cleanup_idle_connections()

        assert other_sqlite_url not in db_utils._engine_cache
        assert sqlite_url in db_utils._engine_cache
