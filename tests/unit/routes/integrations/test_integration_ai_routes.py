"""
Tests for ``app/routes/integrations/integration_ai_routes.py``.

**The row count is the assertion.** Every refusal here is checked twice: that the reason
came back, and that ``SELECT count(*) FROM integration_flows`` is still zero. Asserting the
refusal alone would pass an implementation that saved first and validated after — which is
the failure this whole design is arranged to prevent, and which looks identical from the
outside until the day somebody asks for a workflow against a connection they do not have.

The provider is stubbed at ``ai_analytics_service.answer_structured``, which is the seam
every AI surface in this application shares. That keeps these tests about *this* module —
the catalogue it builds, the resolution it does, the row it does or does not write — rather
than about anybody's SDK.

There is also a test that the panel survives a provider being unreachable, because
"the AI is down" landing on top of somebody's workflow list is the failure mode an HTMX
partial exists to avoid.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest
from sqlalchemy import func, select

from app.models.integrations import (
    AUTH_API_KEY,
    OPERATION_READ,
    OPERATION_WRITE,
    IntegrationConnection,
    IntegrationFlow,
    IntegrationRestOperation,
)
from app.routes.integrations import IntegrationAIController
from app.schemas.integrations.workflow_draft_schemas import WorkflowDraft
from app.services.integrations.credentials import credential_service

BASE = "https://api.example.com"


@pytest.fixture
def client(auth_client_factory):  # noqa: ANN001, ANN201
    return auth_client_factory(IntegrationAIController)


@pytest.fixture
async def connection(db, user) -> IntegrationConnection:  # noqa: ANN001
    row = IntegrationConnection(
        user_id=user.id,
        connector_id="rest_generic",
        label="Acme CRM",
        auth_kind=AUTH_API_KEY,
        base_url=BASE,
    )
    db.add(row)
    await db.commit()
    await credential_service.store_credential(db, row, user_id=user.id, api_key="sk-1")

    db.add_all([
        IntegrationRestOperation(
            connection_id=row.id, operation_id="list_contacts", label="List contacts",
            kind=OPERATION_READ, method="GET", path="/contacts", records_path="data",
            outputs=[{"name": "email", "type": "string"}],
        ),
        IntegrationRestOperation(
            connection_id=row.id, operation_id="create_contact", label="Create contact",
            kind=OPERATION_WRITE, method="POST", path="/contacts",
            inputs=[{"name": "email", "type": "string", "required": True}],
        ),
    ])
    await db.commit()
    return row


@pytest.fixture
def stub_model(monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    """
    Answer as whichever provider the user has, without one.

    Patched at ``answer_structured`` — the seam every AI surface here shares — so these
    tests exercise this module's own catalogue, resolution and persistence rather than an
    SDK's.
    """
    answers: List[Dict[str, Any]] = []
    calls: List[str] = []

    async def _answer(db, user_id, system_prompt, user_content, output_model, **kwargs):  # noqa: ANN001
        calls.append(user_content)
        payload = answers.pop(0) if answers else {"unsupported": True, "reason": "nothing set"}
        return WorkflowDraft.parse(payload)

    from app.services.ai_analytics import ai_analytics_service

    monkeypatch.setattr(ai_analytics_service, "answer_structured", _answer)

    return {"answers": answers, "calls": calls}


async def flow_count(db) -> int:  # noqa: ANN001
    result = await db.execute(select(func.count()).select_from(IntegrationFlow))
    return int(result.scalar() or 0)


def good_draft() -> Dict[str, Any]:
    return {
        "name": "Contacts sync",
        "steps": [
            {"ref": "read", "type": "connector_read", "label": "Read contacts",
             "connection": "Acme CRM", "operation": "list_contacts"},
            {"ref": "loop", "type": "batch", "label": "Each batch", "batch_size": 200},
            {"ref": "write", "type": "connector_write", "label": "Create contact",
             "connection": "Acme CRM", "operation": "create_contact",
             "mappings": [{"source": "email", "target": "email"}]},
        ],
    }


class TestGenerating:
    async def test_a_good_draft_comes_back_for_review(
        self, db, client, connection, stub_model
    ) -> None:
        stub_model["answers"].append(good_draft())

        response = client.post(
            "/integrations/ai/generate", data={"instruction": "sync contacts"}
        )

        assert response.status_code == 200
        assert "Contacts sync" in response.text
        assert "A draft, not a workflow" in response.text

    async def test_generating_saves_nothing(
        self, db, client, connection, stub_model
    ) -> None:
        """
        **The property the two-request split exists for.** Generate returns a drawing in a
        hidden field; Save is a separate press. An implementation that stored the draft
        would pass every other test in this file.
        """
        stub_model["answers"].append(good_draft())

        client.post("/integrations/ai/generate", data={"instruction": "sync contacts"})

        assert await flow_count(db) == 0

    async def test_the_catalogue_reaches_the_model(
        self, db, client, connection, stub_model
    ) -> None:
        """
        The single most effective anti-hallucination measure here, and it works by
        omission: what is absent from this text is what the model cannot name.
        """
        stub_model["answers"].append(good_draft())

        client.post("/integrations/ai/generate", data={"instruction": "sync contacts"})

        prompt = stub_model["calls"][0]
        assert "Acme CRM" in prompt
        assert "create_contact" in prompt
        assert "sync contacts" in prompt

    async def test_the_users_request_comes_last(
        self, db, client, connection, stub_model
    ) -> None:
        """A model reading a long catalogue and then a one-sentence request keeps the
        request in view. The other order pushes it behind everything it has to reason
        about."""
        stub_model["answers"].append(good_draft())

        client.post("/integrations/ai/generate", data={"instruction": "sync contacts"})

        prompt = stub_model["calls"][0]
        assert prompt.index("Acme CRM") < prompt.index("sync contacts")

    async def test_a_credential_never_reaches_the_prompt(
        self, db, client, connection, stub_model
    ) -> None:
        """The catalogue is built from connection rows and connector specs; the credential
        table is not joined, which is the structural reason rather than a filter."""
        stub_model["answers"].append(good_draft())

        client.post("/integrations/ai/generate", data={"instruction": "sync contacts"})

        assert "sk-1" not in stub_model["calls"][0]


class TestRefusals:
    async def test_a_hallucinated_connection_is_refused_with_the_real_names(
        self, db, client, connection, stub_model
    ) -> None:
        draft = good_draft()
        draft["steps"][0]["connection"] = "Shopify Prod"
        # Twice: one repair is attempted before the refusal stands.
        stub_model["answers"].extend([draft, dict(draft)])

        response = client.post(
            "/integrations/ai/generate", data={"instruction": "sync shopify orders"}
        )

        assert "Shopify Prod" in response.text
        assert "Acme CRM" in response.text
        assert await flow_count(db) == 0

    async def test_a_hallucinated_target_field_is_refused(
        self, db, client, connection, stub_model
    ) -> None:
        draft = good_draft()
        draft["steps"][2]["mappings"] = [
            {"source": "email", "target": "customer_email"}
        ]
        stub_model["answers"].extend([draft, dict(draft)])

        response = client.post(
            "/integrations/ai/generate", data={"instruction": "sync contacts"}
        )

        assert "customer_email" in response.text
        assert await flow_count(db) == 0

    async def test_a_decline_is_shown_and_not_retried(
        self, db, client, connection, stub_model
    ) -> None:
        """
        ``unsupported=True`` is never retried. The model has answered the question — asking
        the same model the same thing again to get a different answer is how a decline
        becomes a hallucination.
        """
        stub_model["answers"].append(
            {"unsupported": True, "reason": "There is no Stripe connection."}
        )

        response = client.post(
            "/integrations/ai/generate", data={"instruction": "sync stripe to xero"}
        )

        assert "no Stripe connection" in response.text
        assert len(stub_model["calls"]) == 1
        assert await flow_count(db) == 0

    async def test_a_repairable_draft_is_asked_once_more(
        self, db, client, connection, stub_model
    ) -> None:
        """
        One repair, then refuse. A canvas rendered with a step pointed at a nonexistent
        connection invites somebody to press Publish, which is why this does not degrade to
        a warning the way ``sql_assist`` does.
        """
        broken = good_draft()
        broken["steps"][0]["connection"] = "Shopify Prod"
        stub_model["answers"].extend([broken, good_draft()])

        response = client.post(
            "/integrations/ai/generate", data={"instruction": "sync contacts"}
        )

        assert len(stub_model["calls"]) == 2
        assert "could not be used" in stub_model["calls"][1]
        assert "Contacts sync" in response.text

    async def test_no_connections_declines_before_calling_a_model(
        self, db, client, stub_model
    ) -> None:
        """There is nothing to build out of, and spending a model call to be told so is a
        call nobody needed."""
        response = client.post(
            "/integrations/ai/generate", data={"instruction": "sync contacts"}
        )

        assert "no usable connections" in response.text
        assert stub_model["calls"] == []

    async def test_an_empty_instruction_is_refused(self, client) -> None:
        response = client.post("/integrations/ai/generate", data={"instruction": "   "})

        assert "Say what you want" in response.text

    async def test_a_provider_failure_lands_in_the_panel(
        self, db, client, connection, monkeypatch
    ) -> None:
        """
        **Not on top of the workflow list.** An AI panel is an HTMX partial precisely so a
        503 from a provider is a div somebody can close, and every surface here stays usable
        without it.
        """
        async def _explode(*args, **kwargs):  # noqa: ANN002, ANN003
            raise RuntimeError("connection reset")

        from app.services.ai_analytics import ai_analytics_service
        monkeypatch.setattr(ai_analytics_service, "answer_structured", _explode)

        response = client.post(
            "/integrations/ai/generate", data={"instruction": "sync contacts"}
        )

        assert response.status_code == 200
        assert "canvas still works" in response.text
        assert await flow_count(db) == 0


class TestSaving:
    async def test_a_saved_draft_is_a_draft(
        self, db, client, connection, stub_model
    ) -> None:
        """
        Switched off and unpublished. **The AI request schemas contain no ``is_active``
        field at all** — a field that cannot be set cannot be set wrongly — and this asserts
        the row that lands agrees.
        """
        stub_model["answers"].append(good_draft())
        generated = client.post(
            "/integrations/ai/generate", data={"instruction": "sync contacts"}
        )

        graph = _hidden_value(generated.text, "graph_data")
        client.post(
            "/integrations/ai/save-draft",
            data={"name": "Contacts sync", "graph_data": graph},
        )

        flow = (await db.execute(select(IntegrationFlow))).scalars().one()
        assert flow.is_active is False
        assert flow.created_by_ai is True

    async def test_it_is_marked_as_written_by_ai(
        self, db, client, connection, stub_model
    ) -> None:
        """Somebody looking at a workflow that fires at 3am is entitled to know a model
        drafted it. It is the only thing about the row that differs."""
        stub_model["answers"].append(good_draft())
        generated = client.post(
            "/integrations/ai/generate", data={"instruction": "sync contacts"}
        )

        client.post(
            "/integrations/ai/save-draft",
            data={
                "name": "Contacts sync",
                "graph_data": _hidden_value(generated.text, "graph_data"),
            },
        )

        flow = (await db.execute(select(IntegrationFlow))).scalars().one()
        assert flow.created_by_ai is True

    async def test_a_duplicate_name_is_refused_in_the_panel(
        self, db, client, user, connection, stub_model
    ) -> None:
        """Through ``create_flow`` like every other new workflow, so the name rule is the
        same rule rather than a second one written for this path."""
        db.add(IntegrationFlow(user_id=user.id, name="Contacts sync", graph_data={}))
        await db.commit()

        stub_model["answers"].append(good_draft())
        generated = client.post(
            "/integrations/ai/generate", data={"instruction": "sync contacts"}
        )

        response = client.post(
            "/integrations/ai/save-draft",
            data={
                "name": "Contacts sync",
                "graph_data": _hidden_value(generated.text, "graph_data"),
            },
        )

        assert "already have a workflow" in response.text
        assert await flow_count(db) == 1

    async def test_a_mangled_drawing_is_refused_rather_than_saved_empty(
        self, db, client, connection
    ) -> None:
        """Defaulting to ``{}`` would store a workflow with no steps, and the person who
        pressed Save on a draft they had just read would get a blank canvas."""
        response = client.post(
            "/integrations/ai/save-draft",
            data={"name": "Contacts sync", "graph_data": "not json"},
        )

        assert "could not be read" in response.text
        assert await flow_count(db) == 0


def _hidden_value(html: str, name: str) -> str:
    """
    The value of a hidden input, unescaped.

    A regex over the rendered page rather than a re-serialised object, because what the
    next request actually posts is what the browser found in the DOM — and the escaping in
    between is part of what is being tested.
    """
    import html as html_module
    import re

    match = re.search(rf'name="{name}" value="([^"]*)"', html)
    assert match, f"no hidden {name} field in the response"
    return html_module.unescape(match.group(1))
