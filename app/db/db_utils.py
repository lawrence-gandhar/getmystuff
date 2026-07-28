# app/db/db_utils.py

import asyncio
import re
import time
from typing import Dict, Optional, Any, TypeVar, Type, List
from dataclasses import dataclass
from urllib.parse import quote_plus

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, AsyncEngine
from sqlalchemy import text, select, and_
from sqlalchemy.exc import SQLAlchemyError

from motor.motor_asyncio import AsyncIOMotorClient


from dotenv import load_dotenv
import os

load_dotenv()


T = TypeVar("T")


# ==========================================================
# CONFIG
# ==========================================================

ENGINE_TTL_SECONDS = os.getenv("ENGINE_TTL_SECONDS")        # 30 min inactivity cleanup
CIRCUIT_FAILURE_LIMIT = os.getenv("CIRCUIT_FAILURE_LIMIT")
CIRCUIT_RESET_SECONDS = os.getenv("CIRCUIT_RESET_SECONDS")


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

    u = quote_plus(username or "")
    p = quote_plus(password or "")

    if db_type == "postgres":
        return f"postgresql+asyncpg://{u}:{p}@{host}:{port}/{database}"

    if db_type == "mysql":
        return f"mysql+aiomysql://{u}:{p}@{host}:{port}/{database}"

    if db_type == "sqlite":
        return f"sqlite+aiosqlite:///{database}"

    if db_type == "oracle":
        raise ValueError(
            "Oracle is not yet supported. Please choose PostgreSQL, MySQL, or SQLite."
        )

    raise ValueError(f"Unsupported database type: {db_type!r}")


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

    except Exception as e:
        print(f"[test_rdbms_connection] Connection failed: {type(e).__name__}: {e}")
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
# ROW SAMPLING (for data profiling / AI analytics)
# ==========================================================

# Table/collection names reaching these functions were already discovered via
# fetch_rdbms_tables / fetch_mongo_collections, but the name still cannot be
# parameterized in SQL — validate against a strict identifier charset before
# quoting and embedding it, as defense in depth against injection.
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_]+$")


def _quote_identifier(db_type: str, name: str) -> str:
    if not _IDENTIFIER_RE.match(name):
        raise ValueError(f"Invalid table identifier: {name!r}")
    if db_type == "mysql":
        return f"`{name}`"
    return f'"{name}"'  # postgres / sqlite


async def fetch_rdbms_rows(
    url: str,
    db_type: str,
    table_name: str,
    limit: int = 500,
) -> List[Dict[str, Any]]:
    """Fetch a bounded sample of rows from an RDBMS table as plain dicts."""

    quoted = _quote_identifier(db_type, table_name)
    query = f"SELECT * FROM {quoted} LIMIT :row_limit"

    engine = await get_engine(url)
    wrapper = _engine_cache[url]

    try:
        async with engine.connect() as conn:
            result = await conn.execute(text(query), {"row_limit": limit})
            rows = result.mappings().all()

        await _register_success(wrapper)
        return [dict(row) for row in rows]

    except SQLAlchemyError:
        await _register_failure(wrapper)
        raise


async def fetch_mongo_rows(
    uri: str,
    database: str,
    collection: str,
    limit: int = 500,
) -> List[Dict[str, Any]]:
    """Fetch a bounded sample of documents from a MongoDB collection."""

    client = await get_mongo_client(uri)
    wrapper = _mongo_cache[uri]

    try:
        cursor = client[database][collection].find({}, {"_id": 0}).limit(limit)
        rows = await cursor.to_list(length=limit)
        await _register_success(wrapper)
        return rows

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


# ==============================================================================
# FILE DATASOURCE
# — Supports CSV, Excel (xls/xlsx), Parquet, JSON arrays/JSONL, and Avro.
# — Mirrors the EngineWrapper / MongoWrapper pattern: caching, circuit breaker,
#   TTL-based cleanup, and async-safe blocking I/O via run_in_executor.
# ==============================================================================

from pathlib import Path  # noqa: E402  (kept here to isolate the new section)

# ------------------------------------------------------------------------------
# File-section typed config
# The module-level CONFIG vars are raw strings (os.getenv).  Cast them to int
# here so the file circuit-breaker arithmetic is type-safe without touching
# the original declarations above.
# ------------------------------------------------------------------------------

_FILE_TTL_SECONDS: int = int(ENGINE_TTL_SECONDS) if ENGINE_TTL_SECONDS else 1800
_FILE_CIRCUIT_FAILURE_LIMIT: int = int(CIRCUIT_FAILURE_LIMIT) if CIRCUIT_FAILURE_LIMIT else 5
_FILE_CIRCUIT_RESET_SECONDS: int = int(CIRCUIT_RESET_SECONDS) if CIRCUIT_RESET_SECONDS else 60

# Canonical set of accepted file-type strings (after normalisation).
_SUPPORTED_FILE_TYPES: frozenset[str] = frozenset(
    {"csv", "excel", "parquet", "json", "avro"}
)


# ------------------------------------------------------------------------------
# Wrapper dataclass
# ------------------------------------------------------------------------------

@dataclass
class FileDataSourceWrapper:
    """
    Tracks circuit-breaker state and last-access time for a cached file
    datasource.  The file itself is never held open; this wrapper is purely
    a metadata / state container — mirroring EngineWrapper and MongoWrapper.
    """

    path: str                            # Resolved absolute path
    file_type: str                       # Normalised type: csv | excel | parquet | json | avro
    last_used: float
    failures: int = 0
    circuit_open_until: Optional[float] = None


# Central cache keyed by resolved absolute path.
_file_cache: Dict[str, FileDataSourceWrapper] = {}


# ------------------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------------------

def _resolve_safe_path(raw_path: str) -> str:
    """
    Resolve *raw_path* to an absolute path and guard against path-traversal
    attacks.

    Raises:
        ValueError:       If the path contains ``..`` traversal components.
        FileNotFoundError: If the resolved path does not point to a regular file.
    """
    parts = Path(raw_path).parts
    if ".." in parts:
        raise ValueError(f"Path traversal detected in supplied path: {raw_path!r}")

    resolved = Path(raw_path).resolve()

    if not resolved.is_file():
        raise FileNotFoundError(
            f"File not found or is not a regular file: {resolved}"
        )

    return str(resolved)


def _normalise_file_type(file_type: str, path: str) -> str:
    """
    Normalise *file_type* to one of the canonical strings in
    ``_SUPPORTED_FILE_TYPES``.

    Accepts ``"auto"`` as a special value — the type is then inferred from the
    file extension.  The aliases ``"xls"`` and ``"xlsx"`` are mapped to
    ``"excel"``.

    Raises:
        ValueError: If the resolved type is not supported.
    """
    ft = file_type.lower().strip()

    if ft == "auto":
        ft = Path(path).suffix.lstrip(".").lower()

    # Alias xls / xlsx → excel
    if ft in ("xls", "xlsx"):
        ft = "excel"

    if ft not in _SUPPORTED_FILE_TYPES:
        raise ValueError(
            f"Unsupported file type {file_type!r}. "
            f"Accepted values: {sorted(_SUPPORTED_FILE_TYPES)}"
        )

    return ft


def _is_jsonl(path: str) -> bool:
    """
    Peek at the first non-empty line of *path* to decide whether the file is
    newline-delimited JSON (JSONL / JSON Lines) rather than a JSON array.

    Returns True when the first content line starts with ``{``.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                stripped = line.strip()
                if stripped:
                    return stripped.startswith("{")
    except OSError:
        pass
    return False


def _parse_avro_schema(avro_schema: Any) -> List[Dict[str, str]]:
    """
    Convert a *fastavro* ``writer_schema`` object into the canonical
    ``[{"column": ..., "type": ...}]`` format.

    Handles both dict-style schemas (parsed JSON) and fastavro parsed-schema
    objects.  Union types (e.g. ``["null", "string"]``) are collapsed to the
    first non-null member.
    """
    if avro_schema is None:
        return []

    # fastavro may return either a plain dict or a parsed schema object.
    schema_fields: Optional[list] = None
    if isinstance(avro_schema, dict):
        schema_fields = avro_schema.get("fields", [])
    elif hasattr(avro_schema, "fields"):
        schema_fields = list(avro_schema.fields)

    if not schema_fields:
        return []

    result: List[Dict[str, str]] = []
    for field in schema_fields:
        if isinstance(field, dict):
            name: str = field.get("name", "unknown")
            raw_type: Any = field.get("type", "unknown")
        else:
            name = getattr(field, "name", "unknown")
            raw_type = getattr(field, "type", "unknown")

        # Avro union types appear as lists: ["null", "string"]
        if isinstance(raw_type, list):
            non_null = [t for t in raw_type if t != "null"]
            type_str: str = str(non_null[0]) if non_null else "null"
        elif isinstance(raw_type, dict):
            type_str = raw_type.get("type", str(raw_type))
        else:
            type_str = str(raw_type)

        result.append({"column": name, "type": type_str})

    return result


# ------------------------------------------------------------------------------
# File-specific circuit-breaker helpers
# (mirrors _register_failure / _register_success but uses type-safe constants)
# ------------------------------------------------------------------------------

async def _file_register_failure(wrapper: FileDataSourceWrapper) -> None:
    """
    Increment the failure counter on *wrapper*.  Open the circuit once the
    failure threshold is reached, preventing further attempts for
    ``_FILE_CIRCUIT_RESET_SECONDS``.
    """
    wrapper.failures += 1
    if wrapper.failures >= _FILE_CIRCUIT_FAILURE_LIMIT:
        wrapper.circuit_open_until = time.time() + _FILE_CIRCUIT_RESET_SECONDS
        wrapper.failures = 0


async def _file_register_success(wrapper: FileDataSourceWrapper) -> None:
    """Reset failure state on *wrapper* after a successful operation."""
    wrapper.failures = 0
    wrapper.circuit_open_until = None


# ------------------------------------------------------------------------------
# Cache manager
# ------------------------------------------------------------------------------

async def get_file_datasource(path: str, file_type: str) -> FileDataSourceWrapper:
    """
    Return a :class:`FileDataSourceWrapper` for *path*, creating and caching
    one on first access.

    The wrapper tracks circuit-breaker state and last-access time; it does
    **not** hold the file open.  Subsequent calls with the same resolved path
    return the cached wrapper and update ``last_used``.

    Args:
        path:      Absolute or relative path to the file.
        file_type: One of ``csv``, ``excel``, ``parquet``, ``json``, ``avro``,
                   or ``auto`` (inferred from extension).  Also accepts the
                   aliases ``xls`` and ``xlsx``.

    Raises:
        ValueError:    On path-traversal attempt or unsupported file type.
        FileNotFoundError: When the file does not exist.
        Exception:     When the circuit is currently open for this path.
    """
    safe_path = _resolve_safe_path(path)
    ft = _normalise_file_type(file_type, safe_path)

    async with _lock:
        wrapper = _file_cache.get(safe_path)

        if wrapper:
            if wrapper.circuit_open_until and time.time() < wrapper.circuit_open_until:
                raise Exception(
                    f"File datasource temporarily unavailable (circuit open): {safe_path}"
                )
            wrapper.last_used = time.time()
            return wrapper

        wrapper = FileDataSourceWrapper(
            path=safe_path,
            file_type=ft,
            last_used=time.time(),
        )
        _file_cache[safe_path] = wrapper
        return wrapper


async def close_file_datasource(path: str) -> None:
    """
    Evict the wrapper for *path* from the cache.

    Because file datasources hold no persistent connection, this is purely a
    cache-management operation.  Safe to call even if the path is not cached.
    """
    safe_path = str(Path(path).resolve())
    async with _lock:
        _file_cache.pop(safe_path, None)


# ------------------------------------------------------------------------------
# Connection test
# ------------------------------------------------------------------------------

async def test_file_connection(path: str, file_type: str) -> bool:
    """
    Verify that *path* exists, is readable, and can be minimally parsed.

    Reads only the smallest possible slice of the file (1 row / schema header)
    to avoid unnecessary I/O.  Registers circuit-breaker state accordingly.

    Returns:
        ``True`` on success, ``False`` on any failure.
    """
    try:
        wrapper = await get_file_datasource(path, file_type)
    except Exception:
        return False

    def _probe() -> bool:
        ft = wrapper.file_type
        sp = wrapper.path

        if ft == "csv":
            import pandas as pd
            # Read exactly 1 row — cheap existence and parse verification.
            chunk_iter = pd.read_csv(sp, chunksize=1, nrows=1)
            chunk = next(chunk_iter)
            return len(chunk.columns) > 0

        if ft == "excel":
            import pandas as pd
            df = pd.read_excel(sp, nrows=1)
            return len(df.columns) > 0

        if ft == "parquet":
            import pyarrow.parquet as pq
            pf = pq.ParquetFile(sp)
            # Accessing metadata does not load row data.
            return pf.metadata.num_row_groups >= 0

        if ft == "json":
            import pandas as pd
            lines = _is_jsonl(sp)
            if lines:
                df = pd.read_json(sp, lines=True, nrows=1)
            else:
                df = pd.read_json(sp)
                df = df.head(1)
            return len(df.columns) > 0

        if ft == "avro":
            import fastavro
            with open(sp, "rb") as fh:
                reader = fastavro.reader(fh)
                return reader.writer_schema is not None

        raise ValueError(f"Unsupported file type: {ft!r}")

    try:
        await asyncio.get_running_loop().run_in_executor(None, _probe)
        await _file_register_success(wrapper)
        return True
    except Exception:
        await _file_register_failure(wrapper)
        return False


# ------------------------------------------------------------------------------
# Schema extraction
# ------------------------------------------------------------------------------

async def fetch_file_schema(path: str, file_type: str) -> List[Dict[str, str]]:
    """
    Infer and return the schema of a file datasource.

    For CSV / Excel / JSON the schema is inferred from a small sample (up to
    500 rows) to keep memory usage bounded.  For Parquet the schema is read
    directly from file metadata without loading any row data.  For Avro the
    schema is extracted from the file header.

    Returns:
        A list of ``{"column": <name>, "type": <type_string>}`` dicts.
    """
    wrapper = await get_file_datasource(path, file_type)

    def _read_schema() -> List[Dict[str, str]]:
        ft = wrapper.file_type
        sp = wrapper.path

        if ft == "csv":
            import pandas as pd
            # A 500-row sample is sufficient for dtype inference while keeping
            # memory usage well-bounded for very large files.
            df = pd.read_csv(sp, nrows=500)
            return [
                {"column": col, "type": str(dtype)}
                for col, dtype in df.dtypes.items()
            ]

        if ft == "excel":
            import pandas as pd
            df = pd.read_excel(sp, nrows=500)
            return [
                {"column": col, "type": str(dtype)}
                for col, dtype in df.dtypes.items()
            ]

        if ft == "parquet":
            import pyarrow.parquet as pq
            # read_schema() reads only the file footer — no row data loaded.
            schema = pq.read_schema(sp)
            return [
                {"column": field.name, "type": str(field.type)}
                for field in schema
            ]

        if ft == "json":
            import pandas as pd
            lines = _is_jsonl(sp)
            if lines:
                df = pd.read_json(sp, lines=True, nrows=500)
            else:
                # JSON arrays must be fully parsed before slicing; document
                # this limitation for very large JSON files.
                df = pd.read_json(sp)
                df = df.head(500)
            return [
                {"column": col, "type": str(dtype)}
                for col, dtype in df.dtypes.items()
            ]

        if ft == "avro":
            import fastavro
            with open(sp, "rb") as fh:
                reader = fastavro.reader(fh)
                avro_schema = reader.writer_schema
            return _parse_avro_schema(avro_schema)

        raise ValueError(f"Unsupported file type: {ft!r}")

    try:
        schema = await asyncio.get_running_loop().run_in_executor(None, _read_schema)
        await _file_register_success(wrapper)
        return schema
    except Exception:
        await _file_register_failure(wrapper)
        raise


# ------------------------------------------------------------------------------
# Preview (first N rows)
# ------------------------------------------------------------------------------

async def fetch_file_preview(
    path: str,
    file_type: str,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """
    Return up to *limit* rows from the file as a list of plain dicts.

    Reads only the required rows where the format supports it (CSV via
    ``nrows``, Parquet via ``iter_batches``).  For formats that require full
    parsing first (JSON arrays, Excel, Avro) the slice is applied after an
    initial in-memory parse.

    Returns:
        A list of row dicts: ``[{"col1": val, "col2": val, ...}, ...]``
    """
    wrapper = await get_file_datasource(path, file_type)

    def _read_preview() -> List[Dict[str, Any]]:
        ft = wrapper.file_type
        sp = wrapper.path

        if ft == "csv":
            import pandas as pd
            df = pd.read_csv(sp, nrows=limit)
            return df.to_dict(orient="records")

        if ft == "excel":
            import pandas as pd
            df = pd.read_excel(sp, nrows=limit)
            return df.to_dict(orient="records")

        if ft == "parquet":
            import pyarrow.parquet as pq
            pf = pq.ParquetFile(sp)
            # iter_batches streams row-groups; reading only *limit* rows avoids
            # loading the full file.
            batch_iter = pf.iter_batches(batch_size=limit)
            try:
                batch = next(batch_iter)
            except StopIteration:
                return []
            return batch.to_pandas().to_dict(orient="records")

        if ft == "json":
            import pandas as pd
            lines = _is_jsonl(sp)
            if lines:
                df = pd.read_json(sp, lines=True, nrows=limit)
            else:
                df = pd.read_json(sp)
                df = df.head(limit)
            return df.to_dict(orient="records")

        if ft == "avro":
            import fastavro
            records: List[Dict[str, Any]] = []
            with open(sp, "rb") as fh:
                reader = fastavro.reader(fh)
                for record in reader:
                    records.append(dict(record))
                    if len(records) >= limit:
                        break
            return records

        raise ValueError(f"Unsupported file type: {ft!r}")

    try:
        preview = await asyncio.get_running_loop().run_in_executor(None, _read_preview)
        await _file_register_success(wrapper)
        return preview
    except Exception:
        await _file_register_failure(wrapper)
        raise


# ------------------------------------------------------------------------------
# File listing (tables abstraction)
# ------------------------------------------------------------------------------

async def fetch_file_listing(path: str, file_type: str) -> List[str]:
    """
    Return the logical "tables" exposed by a file.

    - **Excel**: returns all sheet names.
    - **All other types**: returns a single-element list containing the
      file's base name (the file itself is the table).

    This function is the file-datasource counterpart of
    :func:`fetch_rdbms_tables` and :func:`fetch_mongo_collections`.
    """
    wrapper = await get_file_datasource(path, file_type)

    def _list() -> List[str]:
        ft = wrapper.file_type
        sp = wrapper.path

        if ft == "excel":
            import pandas as pd
            xl = pd.ExcelFile(sp)
            return xl.sheet_names  # type: ignore[return-value]

        # For every other format the file represents a single table.
        return [Path(sp).name]

    try:
        listing = await asyncio.get_running_loop().run_in_executor(None, _list)
        await _file_register_success(wrapper)
        return listing
    except Exception:
        await _file_register_failure(wrapper)
        raise


# ------------------------------------------------------------------------------
# TTL cleanup — file datasources
# ------------------------------------------------------------------------------

async def cleanup_idle_file_datasources() -> None:
    """
    Background task that evicts idle :class:`FileDataSourceWrapper` entries
    from ``_file_cache`` after ``_FILE_TTL_SECONDS`` of inactivity.

    Run alongside :func:`cleanup_idle_connections`::

        asyncio.create_task(cleanup_idle_connections())
        asyncio.create_task(cleanup_idle_file_datasources())

    Because file datasources hold no persistent I/O handle, eviction is a
    pure cache-removal operation with no teardown cost.
    """
    while True:
        await asyncio.sleep(300)  # check every 5 minutes

        now = time.time()
        async with _lock:
            for path in list(_file_cache.keys()):
                wrapper = _file_cache[path]
                if now - wrapper.last_used > _FILE_TTL_SECONDS:
                    del _file_cache[path]


# ==============================================================================
# UNIFIED SOURCE ABSTRACTION  (bonus)
# Provides a single entry-point for listing logical "tables" regardless of
# whether the backing store is an RDBMS, MongoDB, or a file.
# ==============================================================================

async def fetch_tables_or_collections_or_files(
    source_type: str,
    # RDBMS kwargs
    url: Optional[str] = None,
    db_type: Optional[str] = None,
    # MongoDB kwargs
    uri: Optional[str] = None,
    database: Optional[str] = None,
    # File kwargs
    path: Optional[str] = None,
    file_type: Optional[str] = None,
) -> List[str]:
    """
    Unified abstraction that returns the list of addressable "tables" for any
    supported datasource type.

    +--------------+-------------------------------------------+
    | source_type  | Returns                                   |
    +==============+===========================================+
    | ``"rdbms"``  | Table names in the target database        |
    +--------------+-------------------------------------------+
    | ``"mongo"``  | Collection names in the target database   |
    +--------------+-------------------------------------------+
    | ``"file"``   | Sheet names (Excel) or ``[filename]``     |
    +--------------+-------------------------------------------+

    Args:
        source_type: ``"rdbms"``, ``"mongo"``, or ``"file"``.
        url:         RDBMS connection URL  (required for ``"rdbms"``).
        db_type:     ``"postgres"`` / ``"mysql"`` / ``"sqlite"``
                     (required for ``"rdbms"``).
        uri:         MongoDB connection URI  (required for ``"mongo"``).
        database:    MongoDB database name   (required for ``"mongo"``).
        path:        File path               (required for ``"file"``).
        file_type:   File type string        (required for ``"file"``).

    Returns:
        List of table / collection / sheet names.

    Raises:
        ValueError: For an unknown *source_type* or missing required kwargs.
    """
    if source_type == "rdbms":
        if not url or not db_type:
            raise ValueError(
                "fetch_tables_or_collections_or_files: "
                "'url' and 'db_type' are required for source_type='rdbms'."
            )
        return await fetch_rdbms_tables(url=url, db_type=db_type)

    if source_type == "mongo":
        if not uri or not database:
            raise ValueError(
                "fetch_tables_or_collections_or_files: "
                "'uri' and 'database' are required for source_type='mongo'."
            )
        return await fetch_mongo_collections(uri=uri, database=database)

    if source_type == "file":
        if not path or not file_type:
            raise ValueError(
                "fetch_tables_or_collections_or_files: "
                "'path' and 'file_type' are required for source_type='file'."
            )
        return await fetch_file_listing(path=path, file_type=file_type)

    raise ValueError(
        f"Unknown source_type: {source_type!r}. "
        "Expected one of: 'rdbms', 'mongo', 'file'."
    )
