"""
Shared test fixtures for the GetMyStuff suite.

Read documentations/TESTING.md before changing anything in here — several pieces
look removable but are not. In particular the four ``@compiles`` shims below are
what make an SQLite test database possible at all, and the JWT-cookie client is
the only way to reach an authenticated route (dependency-injection overrides do
not work; see ``auth_client``).

This module must be imported before any application module that touches
``Base.metadata``, which pytest guarantees by loading conftest first.
"""

from __future__ import annotations

import os
import socket
import uuid as uuid_pkg
from pathlib import Path
from typing import AsyncIterator, Callable, Iterator

# ---------------------------------------------------------------------------
# Environment
#
# Set before importing anything under app/: app/db/db_sessions.py calls
# create_async_engine(DATABASE_URL) at module scope, so an unset or Postgres URL
# would either explode on import or point the suite at a real database.
#
# python-dotenv's load_dotenv() does not overwrite variables that are already
# set, so this reliably wins over the committed .env.
# ---------------------------------------------------------------------------
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite://")
os.environ.setdefault("FERNET_KEY", "dw7Al3yLv3bfMt8yf45nnQbF33v7LggE9JLMgh32Ws4=")
# app/db/auth/auth.py raises at import when this is unset — deliberately, so a
# deployment can never run on a guessable signing key. A fixed value here keeps
# the suite deterministic; it is a test key and signs nothing outside this run.
os.environ.setdefault("JWT_SECRET_KEY", "test-only-jwt-signing-key-not-used-anywhere-else")
os.environ.setdefault("OLLAMA_BASE_URL", "http://ollama.invalid:11434")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-anthropic-key")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

import pytest  # noqa: E402
from sqlalchemy import BigInteger  # noqa: E402
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID  # noqa: E402
from sqlalchemy.ext.compiler import compiles  # noqa: E402


# ---------------------------------------------------------------------------
# SQLite type shims
#
# The models are written against PostgreSQL. Four column types have no SQLite
# rendering, and Base.metadata.create_all() raises CompileError without these.
# Each shim is load-bearing:
#
#   JSONB      -> JSON      SQLite's JSON1 extension covers what the ORM needs.
#   PG UUID    -> CHAR(36)  Stored as the canonical hyphenated string.
#   Vector     -> BLOB      Lets the pgvector table CREATE. Similarity search
#                           (the `<=>` operator) still does NOT work on SQLite,
#                           so tests touching retrieve_similar_chunks() must
#                           mock the query layer.
#   BigInteger -> INTEGER   MANDATORY. Every model uses a BigInteger
#                           autoincrement primary key, but SQLite only
#                           auto-assigns a rowid for "INTEGER PRIMARY KEY"
#                           exactly. Without this every insert fails with
#                           "NOT NULL constraint failed: <table>.id".
#
# Registered at import time, before any metadata is compiled.
# ---------------------------------------------------------------------------
@compiles(JSONB, "sqlite")
def _compile_jsonb(type_, compiler, **kw) -> str:  # noqa: ANN001
    return "JSON"


@compiles(PGUUID, "sqlite")
def _compile_uuid(type_, compiler, **kw) -> str:  # noqa: ANN001
    return "CHAR(36)"


@compiles(BigInteger, "sqlite")
def _compile_bigint(type_, compiler, **kw) -> str:  # noqa: ANN001
    return "INTEGER"


try:
    from pgvector.sqlalchemy import Vector

    @compiles(Vector, "sqlite")
    def _compile_vector(type_, compiler, **kw) -> str:  # noqa: ANN001
        return "BLOB"

except ImportError:  # pragma: no cover - pgvector is a hard dependency
    pass


from litestar import Litestar  # noqa: E402
from litestar.contrib.jinja import JinjaTemplateEngine  # noqa: E402
from litestar.di import Provide  # noqa: E402
from litestar.exceptions import HTTPException  # noqa: E402
from litestar.middleware.session.server_side import ServerSideSessionConfig  # noqa: E402
from litestar.plugins.flash import FlashConfig, FlashPlugin  # noqa: E402
from litestar.plugins.htmx import HTMXPlugin, HTMXRequest  # noqa: E402
from litestar.static_files.config import StaticFilesConfig  # noqa: E402
from litestar.template.config import TemplateConfig  # noqa: E402
from litestar.testing import TestClient  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

import app.db.models  # noqa: E402,F401  (populates Base.metadata)
from app.db.auth.auth import create_access_token, hash_password  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.models.user.user import User  # noqa: E402


# Repo root. Templates and static files are configured with relative paths in
# main.py, so the suite must resolve them from here rather than the cwd.
PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Network guard
# ---------------------------------------------------------------------------
_real_socket_connect = socket.socket.connect


@pytest.fixture(autouse=True)
def block_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Fail fast on any real outbound TCP connection.

    Every external boundary in this codebase is supposed to be mocked (see the
    mock fixtures below). Without this guard a missed mock does not fail — it
    hangs until a timeout, or worse, silently succeeds against a real service
    and makes the suite depend on the network. Turning it into an immediate,
    named error is what keeps the test suite honest.

    Loopback is allowed so an in-process server or the local Ollama container is
    still reachable when a test genuinely wants it. Mark a test with
    ``@pytest.mark.external`` to opt out entirely.
    """
    if request.node.get_closest_marker("external"):
        return

    def guarded_connect(self: socket.socket, address):  # noqa: ANN001, ANN202
        host = address[0] if isinstance(address, tuple) else address
        if host in ("127.0.0.1", "::1", "localhost"):
            return _real_socket_connect(self, address)
        raise RuntimeError(
            f"Blocked outbound network connection to {host!r} during a test. "
            "Mock the external boundary (see the mock_* fixtures in "
            "tests/conftest.py) or mark the test with @pytest.mark.external."
        )

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
@pytest.fixture
async def db_engine():
    """
    A fresh in-memory SQLite database per test.

    StaticPool keeps a single connection alive for the engine's lifetime. That
    is required, not an optimisation: ``sqlite+aiosqlite://`` gives each new
    connection its own private empty database, so without StaticPool the tables
    created here would be invisible to the next connection.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
        future=True,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield engine

    await engine.dispose()


@pytest.fixture
async def db(db_engine) -> AsyncIterator[AsyncSession]:  # noqa: ANN001
    """An AsyncSession bound to the per-test database."""
    session_factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------
# bcrypt is deliberately slow (~250 ms per hash at the default cost). Hashing the
# same password once and reusing the digest keeps every user fixture realistic —
# it is a genuine bcrypt hash that verify_password() accepts — while keeping a
# few hundred tests to a sane runtime. Tests that specifically exercise hashing
# call hash_password() directly.
TEST_PASSWORD = "correct-horse"
_TEST_PASSWORD_HASH: str | None = None


def hashed_test_password() -> str:
    """A real bcrypt digest of ``TEST_PASSWORD``, computed at most once per run."""
    global _TEST_PASSWORD_HASH
    if _TEST_PASSWORD_HASH is None:
        _TEST_PASSWORD_HASH = hash_password(TEST_PASSWORD)
    return _TEST_PASSWORD_HASH


@pytest.fixture
async def user(db: AsyncSession) -> User:
    """A persisted, active admin user."""
    record = User(
        uuid=uuid_pkg.uuid4(),
        email="tester@example.com",
        password=hashed_test_password(),
        role="admin",
        is_active=True,
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)
    return record


@pytest.fixture
def make_user(db: AsyncSession) -> Callable:
    """Factory for additional users, e.g. to assert cross-user isolation."""

    async def _make(email: str, *, role: str = "user", is_active: bool = True) -> User:
        record = User(
            uuid=uuid_pkg.uuid4(),
            email=email,
            password=hashed_test_password(),
            role=role,
            is_active=is_active,
        )
        db.add(record)
        await db.commit()
        await db.refresh(record)
        return record

    return _make


# ---------------------------------------------------------------------------
# Application / client
# ---------------------------------------------------------------------------
def _template_config() -> TemplateConfig:
    return TemplateConfig(
        engine=JinjaTemplateEngine,
        directory=PROJECT_ROOT / "templates",
    )


def build_test_app(*route_handlers, db_session: AsyncSession) -> Litestar:
    """
    Build a Litestar app mirroring main.py, wired to the test session.

    main.app is deliberately NOT reused: its on_startup hook runs
    Base.metadata.create_all against the real engine, seeds a user, and calls
    ollama_client.preload_models() over the network. Constructing an equivalent
    app from the same controllers gives the same routing, middleware and
    exception handling without those side effects. main.py's own functions are
    covered directly by tests/test_main.py.
    """
    from main import http_exception_handler

    template_config = _template_config()

    async def provide_db() -> AsyncIterator[AsyncSession]:
        yield db_session

    return Litestar(
        route_handlers=list(route_handlers),
        debug=True,
        request_class=HTMXRequest,
        template_config=template_config,
        plugins=[
            HTMXPlugin(),
            FlashPlugin(config=FlashConfig(template_config=template_config)),
        ],
        middleware=[ServerSideSessionConfig().middleware],
        static_files_config=[
            StaticFilesConfig(directories=[PROJECT_ROOT / "static"], path="/static")
        ],
        dependencies={"db": Provide(provide_db)},
        exception_handlers={HTTPException: http_exception_handler},
    )


@pytest.fixture
def app_factory(db: AsyncSession) -> Callable[..., Litestar]:
    """Build a Litestar app from the given controllers, bound to the test session."""

    def _factory(*route_handlers) -> Litestar:
        return build_test_app(*route_handlers, db_session=db)

    return _factory


@pytest.fixture
def client_factory(app_factory: Callable[..., Litestar]) -> Callable[..., TestClient]:
    """
    Build an unauthenticated TestClient for the given controllers.

    ``raise_server_exceptions=False`` so a handler raising produces a real HTTP
    response and the registered exception handler is exercised — which is what
    the routes actually do in production.
    """

    def _factory(*route_handlers) -> TestClient:
        return TestClient(app=app_factory(*route_handlers), raise_server_exceptions=False)

    return _factory


@pytest.fixture
def auth_client_factory(
    client_factory: Callable[..., TestClient], user: User
) -> Callable[..., TestClient]:
    """
    Build a TestClient authenticated as ``user``.

    Authentication is a real signed JWT in the ``access_token`` cookie, not a
    stubbed dependency. That is not a stylistic choice: every controller sets
    ``dependencies = {"user": require_auth}`` as a CLASS attribute, which
    overrides any app-level ``user`` provider, so injecting a fake user is
    silently ignored and the request 401s. Minting a token is the only approach
    that works, and it has the side benefit of covering the real auth path.
    """

    def _factory(*route_handlers) -> TestClient:
        client = client_factory(*route_handlers)
        client.cookies.set("access_token", create_access_token(str(user.uuid)))
        return client

    return _factory


# ---------------------------------------------------------------------------
# External boundary mocks
#
# These five are the only places the application reaches outside its process.
# ---------------------------------------------------------------------------
@pytest.fixture
def mock_ollama(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Stub the local-LLM client (app/services/ai_inbuilt/ollama_client.py)."""
    from app.services.ai_inbuilt import ollama_client

    calls: dict = {"chat": [], "embed": []}

    async def fake_chat(system_prompt: str, user_content: str, *, json_mode: bool = True):
        calls["chat"].append((system_prompt, user_content, json_mode))
        return ollama_client.ChatCompletion(
            text='{"answer": "stubbed"}' if json_mode else "stubbed",
            prompt_tokens=1,
            output_tokens=1,
        )

    async def fake_embed_texts(texts, expected_dimensions=None):  # noqa: ANN001
        calls["embed"].append(list(texts))
        width = expected_dimensions or 8
        return [[0.0] * width for _ in texts]

    async def fake_embed_text(text, expected_dimensions=None):  # noqa: ANN001
        result = await fake_embed_texts([text], expected_dimensions)
        return result[0]

    async def noop() -> None:
        return None

    monkeypatch.setattr(ollama_client, "chat", fake_chat)
    monkeypatch.setattr(ollama_client, "embed_texts", fake_embed_texts)
    monkeypatch.setattr(ollama_client, "embed_text", fake_embed_text)
    monkeypatch.setattr(ollama_client, "preload_models", noop)
    monkeypatch.setattr(ollama_client, "close_client", noop)
    return calls


@pytest.fixture
def mock_llm_sdks(monkeypatch: pytest.MonkeyPatch) -> dict:
    """
    Stub the Anthropic and OpenAI SDK calls in ai_analytics_service.

    Patches the module's private ``_call_*_core`` helpers rather than the SDK
    classes: the service owns those seams, so the stub stays valid even if the
    vendor client construction changes.
    """
    from app.services.ai_analytics import ai_analytics_service as svc

    calls: dict = {"claude": [], "openai": []}
    reply = '{"summary": "stubbed", "tables": []}'

    async def fake_claude(*args, **kwargs):  # noqa: ANN002, ANN003
        calls["claude"].append((args, kwargs))
        return reply

    async def fake_openai(*args, **kwargs):  # noqa: ANN002, ANN003
        calls["openai"].append((args, kwargs))
        return reply

    for name, fake in (("_call_claude_core", fake_claude), ("_call_openai_core", fake_openai)):
        if hasattr(svc, name):
            monkeypatch.setattr(svc, name, fake)
    return calls


@pytest.fixture
def mock_outbound_http(monkeypatch: pytest.MonkeyPatch) -> dict:
    """
    Stub outbound webhook execution in chatbot_action_service.

    Also neutralises ``_assert_public_host``, which performs a real DNS lookup
    as an SSRF guard. Tests that target the guard itself should not use this
    fixture — they should call it directly.
    """
    from app.services.chatbot import chatbot_action_service as svc

    calls: list = []

    class FakeResponse:
        status_code = 200
        text = '{"ok": true}'
        headers = {"content-type": "application/json"}

        def json(self) -> dict:
            return {"ok": True}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            pass

        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, *exc) -> None:  # noqa: ANN002
            return None

        async def request(self, method, url, **kwargs):  # noqa: ANN001, ANN003
            calls.append({"method": method, "url": url, **kwargs})
            return FakeResponse()

    monkeypatch.setattr(svc.httpx, "AsyncClient", FakeAsyncClient)
    if hasattr(svc, "_assert_public_host"):
        monkeypatch.setattr(svc, "_assert_public_host", lambda *a, **k: None)
    return {"calls": calls}


@pytest.fixture
def mock_deep_agent(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Stub the LangChain/deepagents runtime so no model is constructed."""
    from app.services.deep_agents import deep_agent_service as svc

    calls: list = []

    async def fake_answer(*args, **kwargs):  # noqa: ANN002, ANN003
        calls.append((args, kwargs))
        return {"answer": "stubbed deep agent reply", "tools_called": []}

    if hasattr(svc, "answer_with_deep_agent"):
        monkeypatch.setattr(svc, "answer_with_deep_agent", fake_answer)
    return {"calls": calls}


@pytest.fixture
def mock_external_datasources(monkeypatch: pytest.MonkeyPatch) -> dict:
    """
    Stub connections to user-supplied external databases in db_utils.

    These reach arbitrary user-configured Postgres/MySQL/Mongo hosts, which must
    never be contacted from a test.
    """
    from app.db import db_utils

    async def fake_test_rdbms(url: str) -> bool:
        return True

    async def fake_test_mongo(uri: str, database: str) -> bool:
        return True

    monkeypatch.setattr(db_utils, "test_rdbms_connection", fake_test_rdbms)
    monkeypatch.setattr(db_utils, "test_mongo_connection", fake_test_mongo)
    return {}


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------
@pytest.fixture
def upload_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Redirect file-upload writes into tmp_path instead of the real app/uploads."""
    from app.utils import file_utils

    monkeypatch.setattr(file_utils, "UPLOAD_BASE", tmp_path / "uploads")
    monkeypatch.setattr(file_utils, "WIDGET_UPLOAD_BASE", tmp_path / "widgets")
    monkeypatch.setattr(file_utils, "KNOWLEDGE_BASE_UPLOAD_BASE", tmp_path / "kb")
    yield tmp_path
