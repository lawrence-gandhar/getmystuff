"""
Tests for the MongoDB functions in app/db/db_utils.py.

There is no Mongo server in the test environment and the conftest network guard
would block one anyway, so ``AsyncIOMotorClient`` is replaced with a fake that
implements the small slice of the motor API these functions actually touch:
``client[db].list_collection_names()``, ``client[db][coll].find_one()``,
``client[db][coll].find(...).limit(n).to_list(length=n)`` and ``close()``.

The fake is deliberately narrow. Anything the production code starts calling
that it does not implement raises ``AttributeError`` and fails the test, rather
than a permissive mock quietly returning another mock and letting a broken call
pass.

"""

from __future__ import annotations

import time

import pytest

from app.db import db_utils
from app.db.db_utils import (
    close_mongo_client,
    fetch_mongo_collections,
    fetch_mongo_rows,
    fetch_mongo_schema,
    get_mongo_client,
)

check_mongo_connection = db_utils.test_mongo_connection

URI = "mongodb://user:pw@mongo.invalid:27017"
DATABASE = "shop"


# ---------------------------------------------------------------------------
# A narrow fake of the motor API
# ---------------------------------------------------------------------------
class FakeCursor:
    def __init__(self, documents: list) -> None:
        self._documents = documents
        self._limit: int | None = None

    def limit(self, count: int) -> "FakeCursor":
        self._limit = count
        return self

    async def to_list(self, length: int | None = None) -> list:
        cap = min(x for x in (self._limit, length) if x is not None) if (
            self._limit is not None or length is not None
        ) else None
        return list(self._documents[:cap]) if cap is not None else list(self._documents)


class FakeCollection:
    def __init__(self, documents: list, fail: bool = False) -> None:
        self._documents = documents
        self._fail = fail
        self.find_calls: list = []

    async def find_one(self):  # noqa: ANN201
        if self._fail:
            raise RuntimeError("mongo unavailable")
        return self._documents[0] if self._documents else None

    def find(self, query: dict, projection: dict) -> FakeCursor:
        if self._fail:
            raise RuntimeError("mongo unavailable")
        self.find_calls.append((query, projection))
        # Honour the {"_id": 0} projection the caller passes.
        excluded = {key for key, keep in projection.items() if not keep}
        return FakeCursor(
            [{k: v for k, v in doc.items() if k not in excluded} for doc in self._documents]
        )


class FakeDatabase:
    def __init__(self, collections: dict, fail: bool = False) -> None:
        self._collections = collections
        self._fail = fail

    def __getitem__(self, name: str) -> FakeCollection:
        return self._collections.setdefault(
            name, FakeCollection([], fail=self._fail)
        )

    async def list_collection_names(self) -> list:
        if self._fail:
            raise RuntimeError("mongo unavailable")
        return sorted(self._collections)

    async def command(self, name: str):  # noqa: ANN201
        if self._fail:
            raise RuntimeError("mongo unavailable")
        return {"ok": 1}


class FakeMongoClient:
    """Stands in for AsyncIOMotorClient. Construction does no I/O, as in motor."""

    instances: list = []

    def __init__(self, uri: str, **kwargs) -> None:  # noqa: ANN003
        self.uri = uri
        self.kwargs = kwargs
        self.closed = False
        self.databases: dict = {}
        FakeMongoClient.instances.append(self)

    def __getitem__(self, name: str) -> FakeDatabase:
        return self.databases.setdefault(name, FakeDatabase({}))

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def fake_mongo(monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    """Replace the motor client and isolate the module-global cache."""
    FakeMongoClient.instances = []
    db_utils._mongo_cache.clear()
    monkeypatch.setattr(db_utils, "AsyncIOMotorClient", FakeMongoClient)
    yield FakeMongoClient
    db_utils._mongo_cache.clear()


@pytest.fixture
async def seeded_client():  # noqa: ANN201
    """A cached client whose ``shop`` database holds two collections."""
    client = await get_mongo_client(URI)
    database = FakeDatabase(
        {
            "customers": FakeCollection(
                [
                    {"_id": "a1", "name": "Ada", "age": 36, "vip": True},
                    {"_id": "a2", "name": "Grace", "age": 45, "vip": False},
                ]
            ),
            "orders": FakeCollection([{"_id": "o1", "total": 9.99}]),
        }
    )
    client.databases[DATABASE] = database
    return client


@pytest.fixture
async def failing_client():  # noqa: ANN201
    """
    A client whose database and every collection under it raise. The collections
    dict starts empty so ``FakeDatabase.__getitem__`` mints a failing
    ``FakeCollection`` on demand — seeding it with a placeholder would hand the
    caller that placeholder instead.
    """
    client = await get_mongo_client(URI)
    client.databases[DATABASE] = FakeDatabase({}, fail=True)
    return client


@pytest.fixture
def integer_constants(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Pin the circuit-breaker thresholds to known values.

    These are real ints from the environment now, so this no longer *repairs*
    anything — it just stops the trip-count tests depending on whatever
    CIRCUIT_FAILURE_LIMIT happens to be configured as.
    """
    monkeypatch.setattr(db_utils, "CIRCUIT_FAILURE_LIMIT", 3)
    monkeypatch.setattr(db_utils, "CIRCUIT_RESET_SECONDS", 60)


# ---------------------------------------------------------------------------
# Client construction
# ---------------------------------------------------------------------------
class TestClientConstruction:
    async def test_passes_the_pool_and_timeout_settings(self) -> None:
        client = await get_mongo_client(URI)

        assert client.uri == URI
        assert client.kwargs == {"maxPoolSize": 50, "serverSelectionTimeoutMS": 5000}

    async def test_only_one_client_is_built_per_uri(self) -> None:
        await get_mongo_client(URI)
        await get_mongo_client(URI)

        assert len(FakeMongoClient.instances) == 1

    async def test_close_mongo_client_closes_the_underlying_client(self) -> None:
        client = await get_mongo_client(URI)

        await close_mongo_client(URI)

        assert client.closed is True
        assert URI not in db_utils._mongo_cache


# ---------------------------------------------------------------------------
# fetch_mongo_collections
# ---------------------------------------------------------------------------
class TestFetchMongoCollections:
    async def test_lists_collection_names(self, seeded_client) -> None:  # noqa: ANN001
        assert await fetch_mongo_collections(URI, DATABASE) == ["customers", "orders"]

    async def test_registers_success(self, seeded_client) -> None:  # noqa: ANN001
        db_utils._mongo_cache[URI].failures = 2

        await fetch_mongo_collections(URI, DATABASE)

        assert db_utils._mongo_cache[URI].failures == 0

    async def test_an_unknown_database_lists_nothing(self, seeded_client) -> None:  # noqa: ANN001
        """Mongo creates databases lazily, so an unknown name is empty rather
        than an error."""
        assert await fetch_mongo_collections(URI, "no_such_db") == []

    async def test_a_failure_propagates(
        self, failing_client, integer_constants  # noqa: ANN001
    ) -> None:
        with pytest.raises(RuntimeError, match="mongo unavailable"):
            await fetch_mongo_collections(URI, DATABASE)

        assert db_utils._mongo_cache[URI].failures == 1

    async def test_the_original_cause_is_no_longer_masked(
        self, failing_client  # noqa: ANN001
    ) -> None:
        """
        Regression test for a fixed defect: ``_register_failure`` used to raise
        ``TypeError`` from the uncast circuit-breaker constants, replacing the
        real cause before it could reach the caller. The underlying error now
        propagates intact even without the ``integer_constants`` fixture.
        """
        with pytest.raises(RuntimeError, match="mongo unavailable"):
            await fetch_mongo_collections(URI, DATABASE)


# ---------------------------------------------------------------------------
# fetch_mongo_schema
# ---------------------------------------------------------------------------
class TestFetchMongoSchema:
    async def test_infers_field_names_and_python_types_from_one_document(
        self, seeded_client  # noqa: ANN001
    ) -> None:
        schema = await fetch_mongo_schema(URI, DATABASE, "customers")

        assert schema == [
            {"column": "_id", "type": "str"},
            {"column": "name", "type": "str"},
            {"column": "age", "type": "int"},
            {"column": "vip", "type": "bool"},
        ]

    async def test_an_empty_collection_yields_an_empty_schema(
        self, seeded_client  # noqa: ANN001
    ) -> None:
        """Sampling a single document is the whole inference strategy, so an
        empty collection has no schema to report — and must not raise."""
        assert await fetch_mongo_schema(URI, DATABASE, "empty_collection") == []

    async def test_an_empty_collection_still_registers_success(
        self, seeded_client  # noqa: ANN001
    ) -> None:
        db_utils._mongo_cache[URI].failures = 2

        await fetch_mongo_schema(URI, DATABASE, "empty_collection")

        assert db_utils._mongo_cache[URI].failures == 0

    async def test_the_schema_reflects_only_the_sampled_document(
        self, seeded_client  # noqa: ANN001
    ) -> None:
        """
        Recorded behaviour worth knowing: Mongo is schemaless, and this samples
        exactly one document via ``find_one()``. A collection whose documents
        differ in shape reports only the first document's fields — later fields
        are invisible to anything built on this.
        """
        seeded_client.databases[DATABASE]._collections["mixed"] = FakeCollection(
            [{"a": 1}, {"a": 1, "b": 2, "c": 3}]
        )

        schema = await fetch_mongo_schema(URI, DATABASE, "mixed")

        assert [entry["column"] for entry in schema] == ["a"]

    async def test_registers_success(self, seeded_client) -> None:  # noqa: ANN001
        db_utils._mongo_cache[URI].failures = 1

        await fetch_mongo_schema(URI, DATABASE, "customers")

        assert db_utils._mongo_cache[URI].failures == 0

    async def test_a_failure_propagates(
        self, failing_client, integer_constants  # noqa: ANN001
    ) -> None:
        with pytest.raises(RuntimeError, match="mongo unavailable"):
            await fetch_mongo_schema(URI, DATABASE, "customers")

        assert db_utils._mongo_cache[URI].failures == 1


# ---------------------------------------------------------------------------
# fetch_mongo_rows
# ---------------------------------------------------------------------------
class TestFetchMongoRows:
    async def test_returns_documents(self, seeded_client) -> None:  # noqa: ANN001
        rows = await fetch_mongo_rows(URI, DATABASE, "customers")

        assert [row["name"] for row in rows] == ["Ada", "Grace"]

    async def test_the_object_id_is_projected_away(self, seeded_client) -> None:  # noqa: ANN001
        """``{"_id": 0}`` — an ObjectId is not JSON-serialisable and means
        nothing to a caller profiling the data."""
        rows = await fetch_mongo_rows(URI, DATABASE, "customers")

        assert all("_id" not in row for row in rows)

    async def test_the_projection_is_what_the_code_sends(self, seeded_client) -> None:  # noqa: ANN001
        await fetch_mongo_rows(URI, DATABASE, "customers")

        collection = seeded_client.databases[DATABASE]._collections["customers"]
        assert collection.find_calls == [({}, {"_id": 0})]

    async def test_the_limit_is_applied(self, seeded_client) -> None:  # noqa: ANN001
        rows = await fetch_mongo_rows(URI, DATABASE, "customers", limit=1)
        assert len(rows) == 1

    async def test_the_default_limit_is_500(self, seeded_client) -> None:  # noqa: ANN001
        rows = await fetch_mongo_rows(URI, DATABASE, "customers")
        assert len(rows) == 2

    async def test_an_empty_collection_returns_nothing(self, seeded_client) -> None:  # noqa: ANN001
        assert await fetch_mongo_rows(URI, DATABASE, "empty_collection") == []

    async def test_registers_success(self, seeded_client) -> None:  # noqa: ANN001
        db_utils._mongo_cache[URI].failures = 2

        await fetch_mongo_rows(URI, DATABASE, "customers")

        assert db_utils._mongo_cache[URI].failures == 0

    async def test_a_failure_propagates(
        self, failing_client, integer_constants  # noqa: ANN001
    ) -> None:
        with pytest.raises(RuntimeError, match="mongo unavailable"):
            await fetch_mongo_rows(URI, DATABASE, "customers")

        assert db_utils._mongo_cache[URI].failures == 1


# ---------------------------------------------------------------------------
# test_mongo_connection
# ---------------------------------------------------------------------------
class TestMongoConnection:
    async def test_a_reachable_server_returns_true(self, seeded_client) -> None:  # noqa: ANN001
        assert await check_mongo_connection(URI, DATABASE) is True

    async def test_success_clears_prior_failures(self, seeded_client) -> None:  # noqa: ANN001
        db_utils._mongo_cache[URI].failures = 2

        await check_mongo_connection(URI, DATABASE)

        assert db_utils._mongo_cache[URI].failures == 0

    async def test_a_failure_returns_false_once_the_constants_are_repaired(
        self, failing_client, integer_constants  # noqa: ANN001
    ) -> None:
        """The documented contract, reachable only with finding 1 fixed."""
        assert await check_mongo_connection(URI, DATABASE) is False

    async def test_repeated_failures_open_the_circuit(
        self, failing_client, integer_constants  # noqa: ANN001
    ) -> None:
        for _ in range(3):
            assert await check_mongo_connection(URI, DATABASE) is False

        assert db_utils._mongo_cache[URI].circuit_open_until is not None

        with pytest.raises(Exception, match="Mongo temporarily unavailable"):
            await get_mongo_client(URI)


# ---------------------------------------------------------------------------
# TTL cleanup — the Mongo half
# ---------------------------------------------------------------------------
class TestCleanupIdleMongoClients:
    async def test_closes_and_evicts_idle_clients(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        The Mongo arm of ``cleanup_idle_connections``. Unlike an engine, a motor
        client is closed synchronously, so the eviction must call ``close()`` —
        dropping the dict entry alone would leak its sockets.
        """
        import asyncio

        fresh_uri = "mongodb://fresh.invalid:27017"
        stale_uri = "mongodb://stale.invalid:27017"
        fresh = await get_mongo_client(fresh_uri)
        stale = await get_mongo_client(stale_uri)
        db_utils._mongo_cache[stale_uri].last_used = time.time() - 10_000

        monkeypatch.setattr(db_utils, "ENGINE_TTL_SECONDS", 1800)

        calls = {"n": 0}

        async def one_pass_then_stop(seconds: float) -> None:
            calls["n"] += 1
            if calls["n"] > 1:
                raise asyncio.CancelledError

        monkeypatch.setattr(db_utils.asyncio, "sleep", one_pass_then_stop)

        with pytest.raises(asyncio.CancelledError):
            await db_utils.cleanup_idle_connections()

        assert stale.closed is True
        assert stale_uri not in db_utils._mongo_cache
        assert fresh.closed is False
        assert fresh_uri in db_utils._mongo_cache
