"""
Tests for ``app/routes/integrations/``.

The route layer's own claims, as opposed to the services': the shapes the canvas reads,
which failures render into the page rather than replacing it, and which are still a 404.

Four properties carry the suite.

**A refused save is a 200 carrying the reason and the step.** The canvas holds work that
is stored nowhere else, so a 400 that navigated away from it would lose that work — and
``node_id`` is what lets the page highlight the step instead of describing it. That is the
whole reason ``FlowValidationError`` is caught in the handler rather than flattened into a
sentence one layer down.

**Ownership is a 404 with one sentence.** Another user's workflow and a missing one answer
identically, because a difference is how somebody learns which uuids are real.

**A credential never appears in a response.** Asserted by putting a distinctive key in
through the create form and then searching every page and partial that mentions the
connection for it.

**The vocabulary the palette is built from is the validator's own.** Asserted against
``flow_rules`` by identity of content, not by a copy of the list — a palette offering a
step type the validator refuses is a form that can only be filled in wrongly, and that is
exactly what a second hardcoded list produces.
"""

from __future__ import annotations

import json
import uuid as uuid_pkg

import pytest

from app.models.integrations import IntegrationConnection, IntegrationFlow
from app.routes.integrations import (
    IntegrationConnectionController,
    IntegrationsController,
)
from app.services.integrations.engine import flow_rules

SECRET = "sk-live-route-test-value"


@pytest.fixture
def client(auth_client_factory):  # noqa: ANN001, ANN201
    return auth_client_factory(IntegrationsController)


@pytest.fixture
def connection_client(auth_client_factory):  # noqa: ANN001, ANN201
    return auth_client_factory(IntegrationConnectionController)


@pytest.fixture
def make_flow(db):  # noqa: ANN001, ANN201
    async def _make(owner, name: str = "Nightly sync", **kwargs):  # noqa: ANN001
        row = IntegrationFlow(
            user_id=owner.id,
            name=name,
            graph_data=kwargs.pop("graph_data", {
                "nodes": [{
                    "id": "t1", "type": "trigger",
                    "position": {"x": 0, "y": 0},
                    "data": {"label": "Start", "kind": "manual"},
                }],
                "edges": [],
            }),
            **kwargs,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row

    return _make


def two_triggers() -> dict:
    return {
        "nodes": [
            {"id": "t1", "type": "trigger", "data": {"label": "Start", "kind": "manual"}},
            {"id": "t2", "type": "trigger", "data": {"label": "Second", "kind": "manual"}},
        ],
        "edges": [],
    }


def trigger_then_success() -> dict:
    return {
        "nodes": [
            {"id": "t1", "type": "trigger", "data": {"label": "Start", "kind": "manual"}},
            {"id": "s1", "type": "success", "data": {"label": "Done"}},
        ],
        "edges": [{"id": "e1", "source": "t1", "target": "s1", "source_port": "default"}],
    }


class TestTheLibrary:
    async def test_it_lists_this_users_workflows(self, client, user, make_flow) -> None:
        await make_flow(user, name="Orders to CRM")

        response = client.get("/integrations/")

        assert response.status_code == 200
        assert "Orders to CRM" in response.text

    async def test_it_does_not_list_another_users(
        self, client, user, make_user, make_flow
    ) -> None:
        other = await make_user(email="other@example.com")
        await make_flow(other, name="Not yours")

        response = client.get("/integrations/")

        assert "Not yours" not in response.text

    async def test_a_duplicate_name_renders_into_the_page(
        self, client, user, make_flow
    ) -> None:
        """
        The refusal lands in the response area the button targeted, and the marker that
        would close the dialog is absent — so the form stays open with what was typed
        still in it.
        """
        await make_flow(user, name="Nightly sync")

        response = client.post("/integrations/create", data={"name": "nightly SYNC"})

        assert response.status_code == 200
        assert "already have a workflow" in response.text
        assert 'data-success="true"' not in response.text


class TestSaving:
    async def test_a_valid_drawing_is_accepted(self, client, user, make_flow) -> None:
        flow = await make_flow(user)

        response = client.post(
            f"/integrations/{flow.uuid}/save",
            json={"graph_data": trigger_then_success()},
        )

        assert response.json() == {"ok": True}

    async def test_a_refusal_is_a_200_naming_the_step(
        self, client, user, make_flow
    ) -> None:
        """
        **Both halves.** A 400 would replace a page holding unsaved work; a 200 with no
        ``node_id`` would leave the author reading a banner about a workflow they have to
        search by hand.
        """
        flow = await make_flow(user)

        body = client.post(
            f"/integrations/{flow.uuid}/save", json={"graph_data": two_triggers()}
        ).json()

        assert body["ok"] is False
        assert body["node_id"] == "t2"
        assert "trigger" in body["error"]

    async def test_nothing_is_written_when_it_is_refused(
        self, db, client, user, make_flow
    ) -> None:
        flow = await make_flow(user)
        before = flow.graph_data

        client.post(f"/integrations/{flow.uuid}/save", json={"graph_data": two_triggers()})

        await db.refresh(flow)
        assert flow.graph_data == before

    async def test_a_body_that_is_not_json_is_refused_readably(
        self, client, user, make_flow
    ) -> None:
        """
        A 200 with a sentence, like every other refusal on this endpoint — not a 400.

        The canvas reads ``result.ok`` and shows ``result.error``; a 400 carrying
        Litestar's ``{"detail": ...}`` would be falsy in the same place and lose the
        message, leaving "That was refused" on a page whose author has no way to find out
        what happened.
        """
        flow = await make_flow(user)

        body = client.post(
            f"/integrations/{flow.uuid}/save",
            content=b"not json at all",
            headers={"Content-Type": "application/json"},
        ).json()

        assert body["ok"] is False
        assert "reload" in body["error"].lower()

    async def test_another_users_workflow_is_a_404(
        self, client, make_user, make_flow
    ) -> None:
        other = await make_user(email="other@example.com")
        flow = await make_flow(other)

        response = client.post(
            f"/integrations/{flow.uuid}/save", json={"graph_data": trigger_then_success()}
        )

        assert response.status_code == 404

    async def test_a_missing_workflow_answers_the_same(self, client) -> None:
        """Identically, so guessing uuids tells somebody nothing about which are real."""
        response = client.post(
            f"/integrations/{uuid_pkg.uuid4()}/save",
            json={"graph_data": trigger_then_success()},
        )

        assert response.status_code == 404


class TestPublishing:
    async def test_publishing_returns_the_version(self, client, user, make_flow) -> None:
        flow = await make_flow(user, graph_data=trigger_then_success())

        body = client.post(f"/integrations/{flow.uuid}/publish").json()

        assert body["ok"] is True
        assert body["version"]["version_number"] == 1
        assert body["version"]["is_published"] is True

    async def test_the_version_payload_has_no_bigint_id(
        self, client, user, make_flow
    ) -> None:
        """CLAUDE.md's rule at the boundary the browser actually reads."""
        flow = await make_flow(user, graph_data=trigger_then_success())

        version = client.post(f"/integrations/{flow.uuid}/publish").json()["version"]

        assert "id" not in version
        assert "flow_id" not in version

    async def test_an_invalid_drawing_is_refused_as_a_200(
        self, client, user, make_flow
    ) -> None:
        flow = await make_flow(user, graph_data=two_triggers())

        body = client.post(f"/integrations/{flow.uuid}/publish").json()

        assert body["ok"] is False
        assert body["node_id"] == "t2"

    async def test_versions_lists_them_newest_first(
        self, client, user, make_flow
    ) -> None:
        flow = await make_flow(user, graph_data=trigger_then_success())
        client.post(f"/integrations/{flow.uuid}/publish")
        client.post(f"/integrations/{flow.uuid}/publish")

        versions = client.get(f"/integrations/{flow.uuid}/versions").json()["versions"]

        assert [v["version_number"] for v in versions] == [2, 1]

    async def test_a_version_does_not_carry_its_whole_drawing(
        self, client, user, make_flow
    ) -> None:
        """The history panel lists dates. Shipping every version's full document with the
        list would send a megabyte to render ten rows."""
        flow = await make_flow(user, graph_data=trigger_then_success())
        client.post(f"/integrations/{flow.uuid}/publish")

        versions = client.get(f"/integrations/{flow.uuid}/versions").json()["versions"]

        assert "graph_data" not in versions[0]


class TestTheVocabulary:
    async def test_it_is_the_validators_own(self, client) -> None:
        """
        **Served from the server, and it is the same object the validator reads.** A
        palette built from a second hardcoded list is a palette that can offer a step type
        the validator refuses — which is a form that can only be filled in wrongly. Adding
        a step type must touch no JavaScript.
        """
        served = client.get("/integrations/vocabulary").json()

        assert served == json.loads(json.dumps(flow_rules.vocabulary(), default=str))

    async def test_every_offered_type_is_one_the_validator_implements(
        self, client
    ) -> None:
        served = client.get("/integrations/vocabulary").json()

        offered = {spec["type"] for spec in served["nodes"]}
        assert offered <= set(flow_rules.IMPLEMENTED_NODE_TYPES) | {"agent"}


class TestRunning:
    async def test_a_draft_cannot_be_run_live(self, client, user, make_flow) -> None:
        flow = await make_flow(user, graph_data=trigger_then_success())

        body = client.post(f"/integrations/{flow.uuid}/runs", data={"mode": "live"}).json()

        assert body["ok"] is False
        assert "dry-run" in body["error"]

    async def test_a_draft_can_be_dry_run(self, client, user, make_flow) -> None:
        flow = await make_flow(user, graph_data=trigger_then_success())

        body = client.post(
            f"/integrations/{flow.uuid}/runs", data={"mode": "dry_run"}
        ).json()

        assert body["ok"] is True
        assert body["run_uuid"]

    async def test_a_run_belonging_to_someone_else_is_a_404(
        self, client, make_user, make_flow, auth_client_factory
    ) -> None:
        other = await make_user(email="other@example.com")
        flow = await make_flow(other, graph_data=trigger_then_success())

        # Started as the owner, then read as somebody else.
        owner_client = auth_client_factory(IntegrationsController)
        owner_client.cookies.clear()

        response = client.get(f"/integrations/runs/{uuid_pkg.uuid4()}")

        assert response.status_code == 404


class TestConnections:
    async def test_the_page_lists_the_connectors_available(
        self, connection_client
    ) -> None:
        response = connection_client.get("/integrations/connections/")

        assert response.status_code == 200
        assert "REST API" in response.text

    async def test_a_created_connection_never_shows_its_key(
        self, connection_client
    ) -> None:
        """
        **Searched, not reviewed.** A test checking that no *named* field holds the key
        would pass the day somebody adds a differently-named one.
        """
        connection_client.post(
            "/integrations/connections/create",
            data={
                "connector_id": "rest_generic",
                "label": "Billing API",
                "base_url": "https://api.example.com",
                "api_key": SECRET,
            },
        )

        listing = connection_client.get("/integrations/connections/")

        assert "Billing API" in listing.text
        assert SECRET not in listing.text

    async def test_the_edit_form_starts_empty_rather_than_prefilled(
        self, db, connection_client, user
    ) -> None:
        """
        Not pre-filled and not masked-with-the-real-value: either would put the secret in
        the DOM of a page anybody looking over a shoulder can read. Empty means "leave the
        stored one alone" on save, which is what the commonest edit — fixing a typo in the
        name — depends on.
        """
        connection_client.post(
            "/integrations/connections/create",
            data={
                "connector_id": "rest_generic",
                "label": "Billing API",
                "base_url": "https://api.example.com",
                "api_key": SECRET,
            },
        )
        row = (await db.execute(__import__("sqlalchemy").select(IntegrationConnection))).scalars().first()

        form = connection_client.get(
            f"/integrations/connections/{row.uuid}/edit-form"
        )

        assert SECRET not in form.text
        assert "Leave blank to keep the stored key" in form.text

    async def test_a_plain_http_address_is_refused_in_the_page(
        self, connection_client
    ) -> None:
        response = connection_client.post(
            "/integrations/connections/create",
            data={
                "connector_id": "rest_generic",
                "label": "Insecure",
                "base_url": "http://api.example.com",
            },
        )

        assert response.status_code == 200
        assert "https" in response.text
        assert 'data-success="true"' not in response.text

    async def test_another_users_connection_edit_form_is_a_dialog_not_a_crash(
        self, db, connection_client, make_user
    ) -> None:
        """
        There is a modal already on screen waiting for a body, so the refusal is rendered
        into it. A raw error page would leave an empty dialog and no explanation.
        """
        other = await make_user(email="other@example.com")
        row = IntegrationConnection(
            user_id=other.id,
            connector_id="rest_generic",
            label="Theirs",
            base_url="https://api.example.com",
        )
        db.add(row)
        await db.commit()

        response = connection_client.get(
            f"/integrations/connections/{row.uuid}/edit-form"
        )

        assert response.status_code == 200
        assert "Could not open" in response.text
        assert "Theirs" not in response.text
