# app/db/db_utils.py

import asyncio
import time
from typing import Dict, Optional, Any, TypeVar, Type, List
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, AsyncEngine
from sqlalchemy import text, select, and_
from sqlalchemy.exc import SQLAlchemyError

from motor.motor_asyncio import AsyncIOMotorClient


T = TypeVar("T")


# ==========================================================
# CONFIG
# ==========================================================

ENGINE_TTL_SECONDS = 60 * 30        # 30 min inactivity cleanup
CIRCUIT_FAILURE_LIMIT = 5
CIRCUIT_RESET_SECONDS = 60


# ==========================================================
# INTERNAL STATE
# ==========================================================

@dataclass
class EngineWrapper:
    engine: AsyncEngine
    last_used: float
    failures: int = 0
    circuit_open_until: Optional[float] = None


@dataclass
class MongoWrapper:
    client: AsyncIOMotorClient
    last_used: float
    failures: int = 0
    circuit_open_until: Optional[float] = None


_engine_cache: Dict[str, EngineWrapper] = {}
_mongo_cache: Dict[str, MongoWrapper] = {}

_lock = asyncio.Lock()


# ==========================================================
# URL BUILDERS
# ==========================================================

def build_rdbms_url(
    db_type: str,
    host: str,
    port: str,
    database: str,
    username: str,
    password: str,
) -> str:

    if db_type == "postgres":
        return f"postgresql+asyncpg://{username}:{password}@{host}:{port}/{database}"

    if db_type == "mysql":
        return f"mysql+aiomysql://{username}:{password}@{host}:{port}/{database}"

    if db_type == "sqlite":
        return f"sqlite+aiosqlite:///{database}"

    raise ValueError("Unsupported RDBMS")


def build_mongo_uri(host: str, port: str, username: str, password: str) -> str:
    return f"mongodb://{username}:{password}@{host}:{port}"


# ==========================================================
# ENGINE MANAGER
# ==========================================================

async def get_engine(url: str) -> AsyncEngine:
    async with _lock:

        wrapper = _engine_cache.get(url)

        # Circuit breaker check
        if wrapper:
            if wrapper.circuit_open_until and time.time() < wrapper.circuit_open_until:
                raise Exception("Database temporarily unavailable (circuit open)")

            wrapper.last_used = time.time()
            return wrapper.engine

        # Create new engine
        engine = create_async_engine(
            url,
            pool_size=10,
            max_overflow=20,
            pool_timeout=30,
            pool_recycle=1800,
            pool_pre_ping=True,
            future=True,
        )

        _engine_cache[url] = EngineWrapper(
            engine=engine,
            last_used=time.time(),
        )

        return engine


async def dispose_engine(url: str):
    async with _lock:
        wrapper = _engine_cache.pop(url, None)
        if wrapper:
            await wrapper.engine.dispose()


# ==========================================================
# MONGO MANAGER
# ==========================================================

async def get_mongo_client(uri: str) -> AsyncIOMotorClient:
    async with _lock:

        wrapper = _mongo_cache.get(uri)

        if wrapper:
            if wrapper.circuit_open_until and time.time() < wrapper.circuit_open_until:
                raise Exception("Mongo temporarily unavailable (circuit open)")

            wrapper.last_used = time.time()
            return wrapper.client

        client = AsyncIOMotorClient(
            uri,
            maxPoolSize=50,
            serverSelectionTimeoutMS=5000,
        )

        _mongo_cache[uri] = MongoWrapper(
            client=client,
            last_used=time.time(),
        )

        return client


async def close_mongo_client(uri: str):
    async with _lock:
        wrapper = _mongo_cache.pop(uri, None)
        if wrapper:
            wrapper.client.close()


# ==========================================================
# CIRCUIT BREAKER HANDLING
# ==========================================================

async def _register_failure(wrapper):
    wrapper.failures += 1

    if wrapper.failures >= CIRCUIT_FAILURE_LIMIT:
        wrapper.circuit_open_until = time.time() + CIRCUIT_RESET_SECONDS
        wrapper.failures = 0


async def _register_success(wrapper):
    wrapper.failures = 0
    wrapper.circuit_open_until = None


# ==========================================================
# CONNECTION TESTING
# ==========================================================

async def test_rdbms_connection(url: str) -> bool:
    engine = await get_engine(url)
    wrapper = _engine_cache[url]

    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        await _register_success(wrapper)
        return True

    except SQLAlchemyError:
        await _register_failure(wrapper)
        return False


async def test_mongo_connection(uri: str, database: str) -> bool:
    client = await get_mongo_client(uri)
    wrapper = _mongo_cache[uri]

    try:
        await client[database].command("ping")
        await _register_success(wrapper)
        return True

    except Exception:
        await _register_failure(wrapper)
        return False


# ==========================================================
# METADATA FETCH
# ==========================================================

async def fetch_rdbms_tables(url: str, db_type: str):

    if db_type == "postgres":
        query = """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema='public'
        """

    elif db_type == "mysql":
        query = "SHOW TABLES"

    elif db_type == "sqlite":
        query = "SELECT name FROM sqlite_master WHERE type='table'"

    else:
        raise ValueError("Unsupported RDBMS")

    engine = await get_engine(url)
    wrapper = _engine_cache[url]

    try:
        async with engine.connect() as conn:
            result = await conn.execute(text(query))
            rows = result.fetchall()

        await _register_success(wrapper)
        return [row[0] for row in rows]

    except SQLAlchemyError:
        await _register_failure(wrapper)
        raise


async def fetch_mongo_collections(uri: str, database: str):
    client = await get_mongo_client(uri)
    wrapper = _mongo_cache[uri]

    try:
        collections = await client[database].list_collection_names()
        await _register_success(wrapper)
        return collections

    except Exception:
        await _register_failure(wrapper)
        raise


async def fetch_rdbms_schema(url: str, db_type: str, table_name: str):
    """Fetch schema (columns and types) for an RDBMS table."""

    if db_type == "postgres":
        query = """
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_name = :table_name
            ORDER BY ordinal_position
        """

    elif db_type == "mysql":
        query = """
            SELECT COLUMN_NAME as column_name, DATA_TYPE as data_type
            FROM information_schema.COLUMNS
            WHERE TABLE_NAME = :table_name
            ORDER BY ORDINAL_POSITION
        """

    elif db_type == "sqlite":
        # SQLite uses PRAGMA which doesn't support parameters, so we use raw table name
        # Note: in production, validate table_name to prevent injection
        query = f"PRAGMA table_info([{table_name}])"

    else:
        raise ValueError("Unsupported RDBMS")

    engine = await get_engine(url)
    wrapper = _engine_cache[url]

    try:
        async with engine.connect() as conn:
            if db_type == "sqlite":
                # PRAGMA table_info returns (cid, name, type, notnull, dflt_value, pk)
                result = await conn.execute(text(query))
                rows = result.fetchall()
                schema = [{"column": row[1], "type": row[2]} for row in rows]
            else:
                # Postgres and MySQL with :table_name parameter
                result = await conn.execute(text(query), {"table_name": table_name})
                rows = result.fetchall()
                schema = [{"column": row[0], "type": row[1]} for row in rows]

        await _register_success(wrapper)
        return schema

    except SQLAlchemyError:
        await _register_failure(wrapper)
        raise


async def fetch_mongo_schema(uri: str, database: str, collection: str):
    """Fetch schema (field names and types) for a MongoDB collection by sampling a document."""
    client = await get_mongo_client(uri)
    wrapper = _mongo_cache[uri]

    try:
        db = client[database]
        # Sample one document to infer schema
        doc = await db[collection].find_one()

        if not doc:
            # Empty collection
            await _register_success(wrapper)
            return []

        # Extract field names and infer types from the sample
        schema = []
        for key, value in doc.items():
            field_type = type(value).__name__
            schema.append({"column": key, "type": field_type})

        await _register_success(wrapper)
        return schema

    except Exception:
        await _register_failure(wrapper)
        raise


# ==========================================================
# AUTO CLEANUP TASK (TTL)
# ==========================================================

async def cleanup_idle_connections():
    """
    Call this in background task.
    """

    while True:
        await asyncio.sleep(300)  # every 5 minutes

        now = time.time()

        async with _lock:

            # Clean RDBMS engines
            for url in list(_engine_cache.keys()):
                wrapper = _engine_cache[url]
                if now - wrapper.last_used > ENGINE_TTL_SECONDS:
                    await wrapper.engine.dispose()
                    del _engine_cache[url]

            # Clean Mongo clients
            for uri in list(_mongo_cache.keys()):
                wrapper = _mongo_cache[uri]
                if now - wrapper.last_used > ENGINE_TTL_SECONDS:
                    wrapper.client.close()
                    del _mongo_cache[uri]


# ==========================================================
# GENERIC CRUD OPERATIONS WITH AGGREGATION SUPPORT
# ==========================================================

class CRUDQueryBuilder:
    """Generic CRUD builder for SQLAlchemy models with aggregation support."""

    def __init__(self, model: Type[T]):
        """Initialize CRUD builder with a SQLAlchemy model."""
        self.model = model

    async def create(self, db: AsyncSession, data: Dict[str, Any]) -> T:
        """Create a new record."""
        instance = self.model(**data)
        db.add(instance)
        await db.commit()
        await db.refresh(instance)
        return instance

    async def get_one(
        self,
        db: AsyncSession,
        filters: Dict[str, Any] | None = None,
    ) -> T | None:
        """Get a single record by filters."""
        query = select(self.model)

        if filters:
            conditions = [
                getattr(self.model, key) == value
                for key, value in filters.items()
            ]
            query = query.where(and_(*conditions))

        result = await db.execute(query)
        return result.scalar_one_or_none()

    async def get_many(
        self,
        db: AsyncSession,
        filters: Dict[str, Any] | None = None,
        skip: int = 0,
        limit: int | None = None,
        order_by: str | None = None,
        desc: bool = False,
    ) -> List[T]:
        """Get multiple records with optional pagination and ordering."""
        query = select(self.model)

        if filters:
            conditions = [
                getattr(self.model, key) == value
                for key, value in filters.items()
            ]
            query = query.where(and_(*conditions))

        if order_by:
            column = getattr(self.model, order_by)
            query = query.order_by(column.desc() if desc else column)

        query = query.offset(skip)
        if limit:
            query = query.limit(limit)

        result = await db.execute(query)
        return result.scalars().all()

    async def update(
        self,
        db: AsyncSession,
        record_id: Any,
        data: Dict[str, Any],
    ) -> T | None:
        """Update a record by ID."""
        record = await db.get(self.model, record_id)
        if not record:
            return None

        for key, value in data.items():
            setattr(record, key, value)

        await db.commit()
        await db.refresh(record)
        return record

    async def delete(self, db: AsyncSession, record_id: Any) -> bool:
        """Delete a record by ID."""
        record = await db.get(self.model, record_id)
        if not record:
            return False

        await db.delete(record)
        await db.commit()
        return True

    async def count(
        self,
        db: AsyncSession,
        filters: Dict[str, Any] | None = None,
    ) -> int:
        """Count records matching filters."""
        query = select(self.model)

        if filters:
            conditions = [
                getattr(self.model, key) == value
                for key, value in filters.items()
            ]
            query = query.where(and_(*conditions))

        result = await db.execute(query)
        return len(result.scalars().all())

    async def aggregate(
        self,
        db: AsyncSession,
        aggregations: Dict[str, Any],
        filters: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        """
        Execute aggregations on the model.

        Args:
            db: AsyncSession instance
            aggregations: Dict of {column_name: aggregation_func}
                         e.g., {"id": func.count(), "amount": func.sum()}
            filters: Optional dict of filter conditions

        Returns:
            Dict with aggregation results
        """
        query = select(*aggregations.values())

        if filters:
            conditions = [
                getattr(self.model, key) == value
                for key, value in filters.items()
            ]
            query = query.select_from(self.model).where(and_(*conditions))
        else:
            query = query.select_from(self.model)

        result = await db.execute(query)
        row = result.first()

        if not row:
            return {name: None for name in aggregations.keys()}

        return {name: value for name, value in zip(aggregations.keys(), row)}