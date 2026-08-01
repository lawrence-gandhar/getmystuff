"""
Proves the fixture layer itself works.

If these fail, every other test in the suite is suspect — fix these first. Each
one guards a specific piece of tests/conftest.py that is easy to break.
"""

from __future__ import annotations

import uuid as uuid_pkg

from sqlalchemy import select

from app.db.base import Base
from app.models.user.user import User


async def test_all_tables_create_on_sqlite(db_engine) -> None:
    """The four @compiles shims cover every column type in the schema."""
    async with db_engine.connect() as conn:
        names = await conn.run_sync(
            lambda sync_conn: sync_conn.dialect.get_table_names(sync_conn)
        )
    assert set(Base.metadata.tables) <= set(names)
    assert len(names) >= 20


async def test_bigint_primary_key_autoincrements(db) -> None:
    """Without the BigInteger->INTEGER shim this fails on a NOT NULL id."""
    record = User(
        uuid=uuid_pkg.uuid4(),
        email="pk@example.com",
        password="x",
        role="user",
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)
    assert isinstance(record.id, int)
    assert record.id > 0


async def test_user_fixture_is_persisted(db, user) -> None:
    found = (await db.execute(select(User).where(User.uuid == user.uuid))).scalar_one()
    assert found.email == "tester@example.com"


def test_unauthenticated_request_is_redirected(client_factory) -> None:
    """
    main.http_exception_handler turns a 401 into a redirect to the login page,
    so an unauthenticated route must never surface a bare 401.
    """
    from app.routes.dashboard import DashboardController

    with client_factory(DashboardController) as client:
        response = client.get("/user/dashboard", follow_redirects=False)

    assert response.status_code in (301, 302, 307)
    assert "/auth/login" in response.headers.get("location", "")


def test_authenticated_request_renders(auth_client_factory) -> None:
    """The JWT-cookie client reaches a controller that requires auth."""
    from app.routes.dashboard import DashboardController

    with auth_client_factory(DashboardController) as client:
        response = client.get("/user/dashboard")

    assert response.status_code == 200
    assert len(response.text) > 100


def test_network_guard_blocks_outbound_connections() -> None:
    """A missed mock must raise immediately rather than hang or hit the network."""
    import socket

    import pytest

    with pytest.raises(RuntimeError, match="Blocked outbound network connection"):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(1)
            sock.connect(("example.com", 80))
