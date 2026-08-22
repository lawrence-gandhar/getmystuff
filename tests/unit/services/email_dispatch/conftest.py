"""
Fixtures shared by the email dispatch tests.

``email_sessions`` is the load-bearing one, and forgetting it produces a failure that looks
like something else entirely. The send worker, the heartbeat and the attempt log all open
their **own** session — a background task has no injected session — and they all go through
``message_store.open_session``, which wraps the engine built at import from
``DATABASE_URL``. In the container that variable points at the *development* PostgreSQL
database, so without this the worker would read and write there while the assertions looked
at the in-memory one. It does not fail cleanly: the test either passes against the wrong
database or reports "expected 1 message, got 0" with nothing to explain it.

``no_smtp`` is the second guard. ``sender.send_message`` is the only function in the module
that touches a socket, and the autouse ``block_network`` fixture in the root conftest raises
on any non-loopback TCP. Rather than let each test remember to patch it, this replaces it
everywhere with a recorder and makes a test that *wants* a particular outcome say so by
setting ``no_smtp.result`` or ``no_smtp.error``. A test that forgets gets a successful send
and a recorded call, never a real connection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.email_dispatch import (
    SECURITY_STARTTLS,
    EmailSmtpConfig,
    EmailTemplate,
)
from app.services.email_dispatch import sender
from app.utils.crypto import encrypt_secret


@pytest.fixture(autouse=True)
def email_sessions(db_engine, monkeypatch: pytest.MonkeyPatch):  # noqa: ANN001, ANN201
    """Point ``message_store.open_session`` at the per-test database. See the module
    docstring — this is the fixture whose absence does not announce itself."""
    from app.services.email_dispatch import message_store

    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    monkeypatch.setattr(message_store, "open_session", factory)

    return factory


@dataclass
class FakeSmtp:
    """
    Stands in for the SMTP transport, and records what it was asked to send.

    ``error`` takes precedence over ``result``: a test setting both is asking for a failure,
    which is the less usual case and therefore the one it must have meant.
    """

    calls: List[Dict[str, Any]] = field(default_factory=list)
    result: Optional[sender.SendResult] = None
    error: Optional[BaseException] = None

    @property
    def call_count(self) -> int:
        return len(self.calls)

    @property
    def last(self) -> Dict[str, Any]:
        assert self.calls, "the sender was never called"
        return self.calls[-1]


@pytest.fixture(autouse=True)
def no_smtp(monkeypatch: pytest.MonkeyPatch) -> FakeSmtp:
    """Replace the transport everywhere. Autouse, so no test can reach a real server."""
    fake = FakeSmtp()

    async def _send(**kwargs: Any) -> sender.SendResult:
        fake.calls.append(kwargs)
        if fake.error is not None:
            raise fake.error
        return fake.result or sender.SendResult(
            response="250 Ok: queued as ABC123", message_id="<test@getmystuff>"
        )

    monkeypatch.setattr(sender, "send_message", _send)
    return fake


@pytest.fixture
async def smtp_config(db, user) -> EmailSmtpConfig:  # noqa: ANN001
    """A usable SMTP config owned by ``user``, with a real encrypted password."""
    config = EmailSmtpConfig(
        user_id=user.id,
        name="Transactional relay",
        host="smtp.example.com",
        port=587,
        security=SECURITY_STARTTLS,
        username="postmaster@example.com",
        password_encrypted=encrypt_secret("hunter2"),
        from_email="alerts@example.com",
        from_name="GetMyStuff Alerts",
        timeout_seconds=30,
    )
    db.add(config)
    await db.commit()
    await db.refresh(config)
    return config


@pytest.fixture
async def template(db, user) -> EmailTemplate:  # noqa: ANN001
    """A template declaring one required variable and one with a default."""
    row = EmailTemplate(
        user_id=user.id,
        name="Run failed",
        subject_template="{{WORKFLOW}} failed",
        body_html_template="<p>{{WORKFLOW}} failed. Severity: {{SEVERITY}}.</p>",
        body_text_template="{{WORKFLOW}} failed. Severity: {{SEVERITY}}.",
        variables=[
            {"name": "WORKFLOW", "label": "Workflow", "required": True, "default": ""},
            {"name": "SEVERITY", "label": "Severity", "required": False, "default": "normal"},
        ],
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row
